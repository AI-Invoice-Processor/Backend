import hashlib
import json
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID, uuid4

import psycopg
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.db import get_db
from app.modules.extraction import extract_invoice_document
from app.state_machine import InvoiceStatus

ALLOWED_CONTENT_TYPES = {"application/pdf", "image/png", "image/jpeg"}
EXTENSION_BY_CONTENT_TYPE = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}


def create_app(
    settings: Settings | None = None,
    db_dependency: Callable[..., Any] = get_db,
) -> FastAPI:
    config = settings or get_settings()
    app = FastAPI(
        title=config.app_name,
        description=(
            "API para recepción y procesamiento de facturas. "
            "La documentación interactiva está disponible en /api/docs."
        ),
        version="0.2.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        openapi_tags=[
            {
                "name": "system",
                "description": "Comprobaciones de estado y disponibilidad.",
            },
            {
                "name": "invoices",
                "description": "Operaciones de recepción, extracción y consulta.",
            },
        ],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_, exc: HTTPException) -> JSONResponse:
        detail = (
            exc.detail if isinstance(exc.detail, str) else "La solicitud no es válida."
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": f"HTTP_{exc.status_code}", "message": detail}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_, __) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "La solicitud no cumple el formato esperado.",
                }
            },
        )

    @app.get(
        "/health",
        tags=["system"],
        summary="Comprobar estado del servicio",
        response_description="El servicio está activo.",
    )
    async def health() -> dict[str, str]:
        return {"status": "healthy", "service": "backend"}

    @app.get(
        "/ready",
        tags=["system"],
        summary="Comprobar disponibilidad de dependencias",
        response_description="El servicio y PostgreSQL están disponibles.",
        responses={503: {"description": "La base de datos no está disponible."}},
    )
    async def ready() -> dict[str, str]:
        try:
            with psycopg.connect(config.database_url, connect_timeout=3) as connection:
                connection.execute("SELECT 1")
        except psycopg.Error as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="La base de datos no está disponible.",
            ) from exc
        return {"status": "ready", "service": "backend"}

    @app.get(
        "/api/invoices",
        tags=["invoices"],
        summary="Listar facturas",
        response_description="Lista de facturas recibidas y procesadas.",
    )
    def list_invoices(
        conn: Annotated[psycopg.Connection, Depends(db_dependency)],
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.id,
                       i.document_id,
                       d.file_name,
                       d.batch_id,
                       d.processing_status,
                       i.supplier_nit_base,
                       i.supplier_dv,
                       i.supplier_name,
                       i.prefix,
                       i.number,
                       i.issue_date,
                       i.subtotal,
                       i.total,
                       i.po_reference,
                       i.extraction_confidence,
                       i.business_status,
                       i.data_version,
                       i.created_at
                  FROM invoices i
                  JOIN documents d ON d.id = i.document_id
                 ORDER BY i.created_at DESC
                """
            )
            rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for r in rows:
            invoice_num = f"{r[8]}-{r[9]}" if r[8] and r[9] else r[9]
            conf = float(r[14]) if r[14] is not None else None
            results.append(
                {
                    "invoice_id": str(r[0]),
                    "document_id": str(r[1]),
                    "file_name": r[2],
                    "batch_id": str(r[3]) if r[3] else None,
                    "processing_status": r[4],
                    "supplier_nit_base": r[5],
                    "supplier_dv": r[6],
                    "supplier_name": r[7],
                    "invoice_number": invoice_num,
                    "issue_date": r[10].isoformat() if r[10] else None,
                    "subtotal": str(r[11]) if r[11] is not None else None,
                    "total": str(r[12]) if r[12] is not None else None,
                    "po_reference": r[13],
                    "extraction_confidence": conf,
                    "business_status": r[15],
                    "data_version": r[16],
                    "created_at": r[17].isoformat() if r[17] else None,
                }
            )
        return results

    @app.get(
        "/api/invoices/{invoice_id}",
        tags=["invoices"],
        summary="Obtener detalle de una factura",
        responses={404: {"description": "Factura no encontrada."}},
    )
    def get_invoice_detail(
        invoice_id: UUID,
        conn: Annotated[psycopg.Connection, Depends(db_dependency)],
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.id,
                       i.document_id,
                       d.file_name,
                       d.storage_ref,
                       d.processing_status,
                       d.duplicate_of_invoice_id,
                       i.supplier_nit_base,
                       i.supplier_dv,
                       i.supplier_name,
                       i.prefix,
                       i.number,
                       i.issue_date,
                       i.currency,
                       i.subtotal,
                       i.total,
                       i.retentions_total,
                       i.net_payable,
                       i.po_reference,
                       i.extraction_method,
                       i.extraction_confidence,
                       i.extraction_json,
                       i.business_status,
                       i.data_version
                  FROM invoices i
                  JOIN documents d ON d.id = i.document_id
                 WHERE i.id = %s
                """,
                (invoice_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Factura no encontrada.",
                )

            cur.execute(
                """
                SELECT line_no, description, quantity, unit_price,
                       discount_amount, amount
                  FROM invoice_lines
                 WHERE invoice_id = %s
                 ORDER BY line_no ASC
                """,
                (invoice_id,),
            )
            lines = [
                {
                    "line_no": ln[0],
                    "description": ln[1],
                    "quantity": str(ln[2]) if ln[2] is not None else None,
                    "unit_price": str(ln[3]) if ln[3] is not None else None,
                    "discount_amount": str(ln[4]) if ln[4] is not None else None,
                    "amount": str(ln[5]) if ln[5] is not None else None,
                }
                for ln in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT tax_type, rate, base, amount
                  FROM invoice_taxes
                 WHERE invoice_id = %s
                """,
                (invoice_id,),
            )
            taxes = [
                {
                    "type": tx[0],
                    "rate": str(tx[1]) if tx[1] is not None else None,
                    "base": str(tx[2]) if tx[2] is not None else None,
                    "amount": str(tx[3]) if tx[3] is not None else None,
                }
                for tx in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT event_type, actor_type, actor_name,
                       from_status, to_status, rule_ref, reason, occurred_at
                  FROM invoice_events
                 WHERE invoice_id = %s
                 ORDER BY occurred_at ASC
                """,
                (invoice_id,),
            )
            events = [
                {
                    "event_type": ev[0],
                    "actor_type": ev[1],
                    "actor_name": ev[2],
                    "from_status": ev[3],
                    "to_status": ev[4],
                    "rule_ref": ev[5],
                    "reason": ev[6],
                    "occurred_at": ev[7].isoformat() if ev[7] else None,
                }
                for ev in cur.fetchall()
            ]

        conf = float(row[19]) if row[19] is not None else None
        return {
            "invoice_id": str(row[0]),
            "document_id": str(row[1]),
            "file_name": row[2],
            "storage_ref": row[3],
            "processing_status": row[4],
            "duplicate_of_invoice_id": str(row[5]) if row[5] else None,
            "supplier_nit_base": row[6],
            "supplier_dv": row[7],
            "supplier_name": row[8],
            "prefix": row[9],
            "number": row[10],
            "issue_date": row[11].isoformat() if row[11] else None,
            "currency": row[12],
            "subtotal": str(row[13]) if row[13] is not None else None,
            "total": str(row[14]) if row[14] is not None else None,
            "retentions_total": str(row[15]) if row[15] is not None else None,
            "net_payable": str(row[16]) if row[16] is not None else None,
            "po_reference": row[17],
            "extraction_method": row[18],
            "extraction_confidence": conf,
            "extraction_json": row[20],
            "business_status": row[21],
            "data_version": row[22],
            "lines": lines,
            "taxes": taxes,
            "events": events,
        }

    @app.post(
        "/api/invoices",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["invoices"],
        summary="Cargar y extraer una factura (Etapas 1 y 2)",
        response_description="La factura fue almacenada y procesada en extracción.",
        responses={
            400: {"description": "Tipo de archivo no permitido."},
            413: {"description": "El archivo supera el tamaño máximo permitido."},
        },
    )
    def upload_invoice(
        file: Annotated[UploadFile, File(...)],
        conn: Annotated[psycopg.Connection, Depends(db_dependency)],
        batch_id: Annotated[UUID | None, Form()] = None,
    ) -> dict[str, Any]:
        if file.content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El archivo debe ser PDF, PNG o JPG.",
            )

        content = file.file.read(config.max_upload_size + 1)
        if len(content) > config.max_upload_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="El archivo supera el tamaño máximo permitido de 10 MB.",
            )

        document_id = uuid4()
        invoice_id = uuid4()
        ext = EXTENSION_BY_CONTENT_TYPE[file.content_type]
        original_name = file.filename or f"invoice{ext}"
        sha256 = hashlib.sha256(content).hexdigest()

        config.documents_dir.mkdir(parents=True, exist_ok=True)
        stored_filename = f"{document_id}{ext}"
        file_path = config.documents_dir / stored_filename
        file_path.write_bytes(content)
        storage_ref = f"documents/{stored_filename}"

        # Etapa 1: Registrar documento y factura en estado RECIBIDA
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents (
                    id, file_name, mime_type, category, storage_ref,
                    sha256, size_bytes, batch_id,
                    processing_stage, processing_status
                )
                VALUES (
                    %s, %s, %s, 'invoice', %s,
                    %s, %s, %s, 'reception', 'pending'
                )
                """,
                (
                    document_id,
                    original_name,
                    file.content_type,
                    storage_ref,
                    sha256,
                    len(content),
                    batch_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO invoices (id, document_id, business_status)
                VALUES (%s, %s, %s)
                """,
                (invoice_id, document_id, InvoiceStatus.RECIBIDA.value),
            )
            cur.execute(
                """
                INSERT INTO invoice_events (
                    invoice_id, document_id, event_type, actor_type,
                    actor_name, from_status, to_status, payload
                )
                VALUES (
                    %s, %s, 'INVOICE_RECEIVED', 'system',
                    'api-upload', NULL, %s, %s::jsonb
                )
                """,
                (
                    invoice_id,
                    document_id,
                    InvoiceStatus.RECIBIDA.value,
                    json.dumps({"file_name": original_name, "sha256": sha256}),
                ),
            )

        # Etapa 2: OCR + Extracción estructurada con Claude
        with conn.transaction(), conn.cursor() as cur:
            extraction = extract_invoice_document(
                cur=cur,
                document_id=document_id,
                invoice_id=invoice_id,
                file_path=file_path,
            )
            cur.execute(
                """
                SELECT i.business_status, d.duplicate_of_invoice_id
                  FROM invoices i
                  JOIN documents d ON d.id = i.document_id
                 WHERE i.id = %s
                """,
                (invoice_id,),
            )
            status_row = cur.fetchone()

        business_status = status_row[0] if status_row else InvoiceStatus.EXTRAIDA.value
        duplicate_of = str(status_row[1]) if status_row and status_row[1] else None

        return {
            "status": "accepted",
            "filename": original_name,
            "size_bytes": len(content),
            "document_id": str(document_id),
            "invoice_id": str(invoice_id),
            "batch_id": str(batch_id) if batch_id else None,
            "storage_path": storage_ref,
            "processing_status": extraction.extraction_status.value,
            "business_status": business_status,
            "duplicate_of_invoice_id": duplicate_of,
            "extraction": extraction.model_dump(mode="json"),
        }

    return app


app = create_app()
