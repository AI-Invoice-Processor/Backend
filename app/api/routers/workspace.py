import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.dependencies import Settings, get_db, get_settings
from app.modules.exceptions.adapter import JevExceptionAdapter
from app.modules.exceptions.evaluator import ExceptionEvaluatorService
from app.modules.exceptions.repository import evaluate_invoice_from_db
from app.modules.invoices.repository import (
    get_invoice_detail_from_db,
    list_invoices_from_db,
)
from app.modules.proposals.claude_service import ClaudeProposalExplainer
from app.modules.proposals.repository import (
    generate_proposal_from_db,
    get_accounting_proposal_from_db,
)
from app.modules.proposals.service import ProposalService
from app.modules.validation.repository import (
    get_current_validation_from_db,
    validate_invoice_from_db,
)
from app.state_machine import (
    InvalidStateTransitionError,
    InvoiceStatus,
    transition_invoice_status,
)

router = APIRouter(prefix="/api/workspace", tags=["workspace"])
logger = logging.getLogger(__name__)


class HumanAction(BaseModel):
    action: str = Field(
        pattern="^(correct_ocr|authorize_difference|attach_support|approve_proposal|reject_proposal)$"
    )
    reason: str = Field(default="", max_length=2000)
    support_doc: str | None = Field(default=None, max_length=180)
    new_value: str | None = Field(default=None, max_length=120)
    field: str | None = Field(default=None, pattern="^(subtotal|total)$")
    actor_name: str = Field(default="Usuario de revisión", max_length=120)


def _json(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    return str(value)


def _matching(cur: psycopg.Cursor, invoice_id: UUID) -> dict:
    cur.execute(
        """
        SELECT i.match_status, i.match_detail,
               s.id, s.nit_base, s.legal_name,
               po.id, po.order_number,
               c.id, c.contract_number
          FROM invoices i
          LEFT JOIN suppliers s ON s.id = i.supplier_id
          LEFT JOIN purchase_orders po ON po.id = i.purchase_order_id
          LEFT JOIN contracts c ON c.id = i.contract_id
         WHERE i.id = %s
        """,
        (invoice_id,),
    )
    row = cur.fetchone()
    if not row:
        return {}
    return {
        "matching_status": row[0],
        "detail": row[1] or {},
        "supplier": {
            "supplier_id": str(row[2]) if row[2] else None,
            "nit_base": row[3],
            "name": row[4],
            "matched": bool(row[2]),
        },
        "purchase_order": {
            "purchase_order_id": str(row[5]) if row[5] else None,
            "order_number": row[6],
            "matched": bool(row[5]),
        },
        "contract": {
            "contract_id": str(row[7]) if row[7] else None,
            "contract_number": row[8],
            "matched": bool(row[7]),
        },
    }


def _validation(cur: psycopg.Cursor, invoice_id: UUID) -> dict:
    cur.execute(
        """
        SELECT rule_id, result, expected_amount, actual_amount, difference,
               difference_pct, expected_text, actual_text, reason, evidence_refs
          FROM rule_checks rc
          JOIN invoices i ON i.id = rc.invoice_id AND i.data_version = rc.data_version
         WHERE rc.invoice_id = %s
         ORDER BY rc.created_at, rc.rule_id
        """,
        (invoice_id,),
    )
    checks = [
        {
            "rule_id": row[0],
            "result": row[1],
            "expected_amount": _json(row[2]),
            "actual_amount": _json(row[3]),
            "difference": _json(row[4]),
            "difference_pct": _json(row[5]),
            "expected_text": row[6],
            "actual_text": row[7],
            "reason": row[8],
            "evidence_refs": row[9] or [],
        }
        for row in cur.fetchall()
    ]
    cur.execute(
        """
        SELECT id, exception_type, severity, message, requires_review,
               resolution_status, created_at
          FROM exceptions
         WHERE invoice_id = %s
         ORDER BY created_at, id
        """,
        (invoice_id,),
    )
    exceptions = [
        {
            "id": str(row[0]),
            "type": row[1],
            "severity": row[2],
            "message": row[3],
            "requires_review": row[4],
            "resolution_status": row[5],
            "created_at": _json(row[6]),
        }
        for row in cur.fetchall()
    ]
    return {"checks": checks, "exceptions": exceptions}


def _documents(cur: psycopg.Cursor, invoice_id: UUID, settings: Settings) -> list[dict]:
    cur.execute(
        """
        SELECT d.id, d.file_name, d.mime_type, d.category, d.storage_ref,
               d.processing_status, d.sha256, d.size_bytes, d.received_at
          FROM documents d
          JOIN invoices i ON i.document_id = d.id
         WHERE i.id = %s
        UNION ALL
        SELECT d.id, d.file_name, d.mime_type, d.category, d.storage_ref,
               d.processing_status, d.sha256, d.size_bytes, d.received_at
          FROM documents d
          JOIN purchase_orders po ON po.pdf_ref = d.storage_ref
          JOIN invoices i ON i.purchase_order_id = po.id
         WHERE i.id = %s
        UNION ALL
        SELECT d.id, d.file_name, d.mime_type, d.category, d.storage_ref,
               d.processing_status, d.sha256, d.size_bytes, d.received_at
          FROM documents d
          JOIN contracts c ON c.pdf_ref = d.storage_ref
          JOIN invoices i ON i.contract_id = c.id
         WHERE i.id = %s
        UNION ALL
        SELECT d.id, d.file_name, d.mime_type, d.category, d.storage_ref,
               d.processing_status, d.sha256, d.size_bytes, d.received_at
          FROM documents d
          JOIN support_documents sd ON sd.pdf_ref = d.storage_ref
          JOIN purchase_orders po ON po.id = sd.purchase_order_id
          JOIN invoices i ON i.purchase_order_id = po.id
         WHERE i.id = %s
        ORDER BY received_at
        """,
        (invoice_id, invoice_id, invoice_id, invoice_id),
    )
    documents_root = settings.documents_dir.resolve()
    return [
        {
            "document_id": str(row[0]),
            "file_name": row[1],
            "mime_type": row[2],
            "category": row[3],
            "storage_ref": row[4],
            "processing_status": row[5],
            "sha256": row[6],
            "size_bytes": row[7],
            "received_at": _json(row[8]),
            "available": (documents_root / Path(row[4]).name).resolve().is_file()
            and documents_root
            in (documents_root / Path(row[4]).name).resolve().parents,
        }
        for row in cur.fetchall()
    ]


def _case(cur: psycopg.Cursor, invoice_id: UUID, settings: Settings) -> dict | None:
    detail = get_invoice_detail_from_db(cur, invoice_id)
    if not detail:
        return None
    proposal = None
    try:
        proposal = get_accounting_proposal_from_db(cur, invoice_id)
    except (psycopg.Error, ValueError):
        proposal = None
    matching = _matching(cur, invoice_id)
    validation = _validation(cur, invoice_id)
    cur.execute(
        "SELECT risk_level, risk_score, risk_confidence FROM invoices WHERE id = %s",
        (invoice_id,),
    )
    risk_row = cur.fetchone() or (None, None, None)
    return {
        "invoice": detail.model_dump(mode="json"),
        "matching": matching,
        "validation": validation,
        "risk": {
            "level": risk_row[0],
            "score": _json(risk_row[1]),
            "confidence": _json(risk_row[2]),
        },
        "proposal": proposal.model_dump(mode="json") if proposal else None,
        "documents": _documents(cur, invoice_id, settings),
        "timeline": [event.model_dump(mode="json") for event in detail.events],
    }


def _ensure_pipeline(cur: psycopg.Cursor, invoice_id: UUID, settings: Settings) -> None:
    cur.execute(
        "SELECT business_status, risk_level FROM invoices WHERE id = %s FOR UPDATE",
        (invoice_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Factura no encontrada")
    business_status, risk_level = row

    if business_status in {
        "EXTRAIDA",
        "EXCEPCION",
        "VALIDADA",
    } and not get_current_validation_from_db(cur, invoice_id):
        validate_invoice_from_db(
            cur,
            invoice_id,
            settings.financial_validation,
            datetime.now(ZoneInfo("America/Bogota")).date(),
        )

    cur.execute("SELECT risk_level FROM invoices WHERE id = %s", (invoice_id,))
    risk_level = cur.fetchone()[0]
    if not risk_level:
        evaluate_invoice_from_db(
            cur,
            invoice_id,
            ExceptionEvaluatorService(
                jev_adapter=JevExceptionAdapter(
                    api_key=settings.typesafe_api_key,
                    model=settings.typesafe_model,
                )
            ),
        )

    if not get_accounting_proposal_from_db(cur, invoice_id):
        generate_proposal_from_db(
            cur,
            invoice_id,
            ProposalService(
                claude_explainer=ClaudeProposalExplainer(
                    api_key=settings.anthropic_api_key,
                    model=settings.anthropic_model,
                )
            ),
        )


@router.get("/cases")
def list_workspace_cases(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    with conn.cursor() as cur:
        items = list_invoices_from_db(cur)
        normalized_search = search.strip().lower() if search else None
        filtered = []
        for item in items:
            haystack = " ".join(
                str(value or "")
                for value in (
                    item.invoice_number,
                    item.supplier_name,
                    item.business_status,
                )
            ).lower()
            if normalized_search and normalized_search not in haystack:
                continue
            if status and item.business_status.lower() != status.lower():
                continue
            filtered.append(item.model_dump(mode="json"))
        return {"items": filtered[:limit], "total": len(filtered)}


@router.get("/cases/{invoice_id}")
def get_workspace_case(
    invoice_id: UUID,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    with conn.cursor() as cur:
        result = _case(cur, invoice_id, settings)
    if result is None:
        raise HTTPException(status_code=404, detail="Factura no encontrada")
    return result


@router.post("/cases/{invoice_id}/pipeline")
def process_workspace_case(
    invoice_id: UUID,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    with conn.transaction(), conn.cursor() as cur:
        _ensure_pipeline(cur, invoice_id, settings)
        result = _case(cur, invoice_id, settings)
    return result or {"invoice": {"invoice_id": str(invoice_id)}}


@router.post("/pipeline")
def process_workspace_pipeline(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=500, ge=1, le=1000),
) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM invoices ORDER BY created_at DESC LIMIT %s", (limit,)
        )
        invoice_ids = [row[0] for row in cur.fetchall()]

    processed: list[str] = []
    failures: list[dict[str, str]] = []
    # Isolate each invoice so a malformed legacy record cannot roll back the
    # cases already prepared for the workspace.
    for invoice_id in invoice_ids:
        try:
            with conn.transaction(), conn.cursor() as cur:
                _ensure_pipeline(cur, invoice_id, settings)
            processed.append(str(invoice_id))
        except Exception:
            logger.exception("Workspace pipeline failed for invoice %s", invoice_id)
            failures.append(
                {
                    "invoice_id": str(invoice_id),
                    "error": "No se pudo procesar la factura.",
                }
            )

    return {
        "processed": len(processed),
        "failed": len(failures),
        "invoice_ids": processed,
        "failures": failures,
    }


@router.get("/summary")
def get_workspace_summary(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE business_status = 'EXCEPCION'),
                   COUNT(*) FILTER (WHERE business_status IN ('PROPUESTA', 'APROBADA')),
                   COUNT(*) FILTER (WHERE business_status IN ('RECIBIDA', 'EXTRAIDA', 'VALIDADA'))
              FROM invoices
            """
        )
        total, review, ready, processing = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM documents")
        files = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COALESCE(NULLIF(e.exception_type, ''), 'OTROS'), COUNT(*)
              FROM exceptions e
             GROUP BY 1
             ORDER BY COUNT(*) DESC, 1
             LIMIT 8
            """
        )
        findings = [{"type": row[0], "count": row[1]} for row in cur.fetchall()]
        cur.execute(
            """
            SELECT EXTRACT(MONTH FROM i.issue_date)::int, COALESCE(SUM(i.subtotal), 0),
                   COALESCE(SUM(po.subtotal), 0)
              FROM invoices i
              LEFT JOIN purchase_orders po ON po.id = i.purchase_order_id
             WHERE i.issue_date IS NOT NULL
             GROUP BY 1
             ORDER BY 1
            """
        )
        monthly = [
            {"month": row[0], "invoiced": str(row[1]), "ordered": str(row[2])}
            for row in cur.fetchall()
        ]
    return {
        "total": total,
        "review": review,
        "ready": ready,
        "processing": processing,
        "files": files,
        "findings": findings,
        "monthly": monthly,
    }


@router.get("/cases/{invoice_id}/documents/{document_id}/content")
def get_workspace_document(
    invoice_id: UUID,
    document_id: UUID,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.file_name, d.mime_type, d.storage_ref
              FROM documents d
              JOIN invoices i ON i.document_id = d.id
             WHERE i.id = %s AND d.id = %s
            UNION
            SELECT d.file_name, d.mime_type, d.storage_ref
              FROM documents d
              JOIN purchase_orders po ON po.pdf_ref = d.storage_ref
              JOIN invoices i ON i.purchase_order_id = po.id
             WHERE i.id = %s AND d.id = %s
            UNION
            SELECT d.file_name, d.mime_type, d.storage_ref
              FROM documents d
              JOIN contracts c ON c.pdf_ref = d.storage_ref
              JOIN invoices i ON i.contract_id = c.id
             WHERE i.id = %s AND d.id = %s
            UNION
            SELECT d.file_name, d.mime_type, d.storage_ref
              FROM documents d
              JOIN support_documents sd ON sd.pdf_ref = d.storage_ref
              JOIN purchase_orders po ON po.id = sd.purchase_order_id
              JOIN invoices i ON i.purchase_order_id = po.id
             WHERE i.id = %s AND d.id = %s
            """,
            (
                invoice_id,
                document_id,
                invoice_id,
                document_id,
                invoice_id,
                document_id,
                invoice_id,
                document_id,
            ),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    path = (settings.documents_dir / Path(row[2]).name).resolve()
    documents_root = settings.documents_dir.resolve()
    if documents_root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Archivo no disponible")
    return FileResponse(
        path,
        media_type=row[1],
        filename=row[0],
        content_disposition_type="inline",
    )


@router.post("/cases/{invoice_id}/actions")
def apply_human_action(
    invoice_id: UUID,
    payload: HumanAction,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    transition_by_action = {
        "correct_ocr": InvoiceStatus.VALIDADA,
        "authorize_difference": InvoiceStatus.VALIDADA,
        "attach_support": InvoiceStatus.VALIDADA,
        "approve_proposal": InvoiceStatus.APROBADA,
        "reject_proposal": InvoiceStatus.RECHAZADA,
    }
    event_by_action = {
        "correct_ocr": "CORRECT_DATA",
        "authorize_difference": "AUTHORIZE_EXCEPTION",
        "attach_support": "ATTACH_SUPPORT",
        "approve_proposal": "APPROVE_PROPOSAL",
        "reject_proposal": "REJECT",
    }
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM invoices WHERE id = %s FOR UPDATE",
            (invoice_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Factura no encontrada")
        target_status = transition_by_action[payload.action]

        if payload.action == "correct_ocr":
            if not payload.field or payload.new_value is None:
                raise HTTPException(
                    status_code=422,
                    detail="La corrección OCR requiere campo y nuevo valor.",
                )
            try:
                normalized_value = payload.new_value.strip().replace("$", "")
                normalized_value = normalized_value.replace(".", "").replace(",", ".")
                corrected_value = Decimal(normalized_value)
            except (InvalidOperation, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail="El nuevo valor debe ser un importe decimal válido.",
                ) from exc
            if corrected_value < 0:
                raise HTTPException(
                    status_code=422,
                    detail="El nuevo valor no puede ser negativo.",
                )
            cur.execute(
                f"""
                UPDATE invoices
                   SET {payload.field} = %s,
                       extraction_json = jsonb_set(
                           COALESCE(extraction_json, '{{}}'::jsonb),
                           '{{data,{payload.field}}}', %s::jsonb, true
                       ),
                       updated_at = now()
                 WHERE id = %s
                """,
                (corrected_value, json.dumps(f"{corrected_value:.2f}"), invoice_id),
            )
        try:
            transition_invoice_status(
                cur,
                invoice_id=invoice_id,
                to_status=target_status,
                event_type=event_by_action[payload.action],
                actor_type="human",
                actor_name=payload.actor_name,
                reason=payload.reason or payload.support_doc,
                payload=payload.model_dump(mode="json"),
                increment_version=payload.action == "correct_ocr",
            )
        except InvalidStateTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        cur.execute(
            """
            INSERT INTO review_decisions (
                invoice_id, decision_type, decided_by, reason, requires_revalidation
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                invoice_id,
                event_by_action[payload.action],
                payload.actor_name,
                payload.reason or payload.support_doc,
                payload.action
                in {"correct_ocr", "authorize_difference", "attach_support"},
            ),
        )
        if payload.action in {"authorize_difference", "attach_support"}:
            cur.execute(
                "UPDATE exceptions SET resolution_status = 'AUTHORIZED' WHERE invoice_id = %s AND resolution_status = 'OPEN'",
                (invoice_id,),
            )
        if payload.action == "correct_ocr":
            cur.execute(
                "UPDATE exceptions SET resolution_status = 'CORRECTED' WHERE invoice_id = %s AND resolution_status = 'OPEN'",
                (invoice_id,),
            )
            validate_invoice_from_db(
                cur,
                invoice_id,
                settings.financial_validation,
                datetime.now(ZoneInfo("America/Bogota")).date(),
            )
        if payload.action == "reject_proposal":
            cur.execute(
                "UPDATE accounting_proposals SET status = 'REJECTED' WHERE invoice_id = %s AND status <> 'OBSOLETE'",
                (invoice_id,),
            )
        if payload.action == "approve_proposal":
            cur.execute(
                "UPDATE accounting_proposals SET status = 'APPROVED' WHERE invoice_id = %s AND status <> 'OBSOLETE'",
                (invoice_id,),
            )
        if payload.action in {"correct_ocr", "authorize_difference", "attach_support"}:
            proposal_service = ProposalService(
                claude_explainer=ClaudeProposalExplainer(
                    api_key=settings.anthropic_api_key,
                    model=settings.anthropic_model,
                )
            )
            generate_proposal_from_db(cur, invoice_id, proposal_service)
        result = _case(cur, invoice_id, settings)
    return result or {"invoice": {"invoice_id": str(invoice_id)}}
