"""Database adapter. Caller owns the transaction; no writes outside its cursor."""

import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from uuid import UUID

from app.modules.validation.rules import normalize_nit
from app.modules.validation.service import evaluate_invoice
from app.schemas.validation import (
    RULE_IDS,
    RuleConfig,
    ValidationInput,
    ValidationOutput,
)
from app.state_machine import InvoiceStatus, transition_invoice_status


class InvoiceNotFoundError(ValueError):
    pass


class ValidationConflictError(ValueError):
    pass


def validate_invoice_from_db(
    cur, invoice_id: UUID, config: RuleConfig, reference_date: date
):
    cur.execute(
        """SELECT i.document_id, i.business_status, i.data_version, i.extraction_json,
        i.purchase_order_id, i.extraction_confidence, d.duplicate_of_invoice_id, d.sha256,
        json_build_object('prefix',i.prefix,'number',i.number,'issue_date',i.issue_date,
          'subtotal',i.subtotal::text,'total',i.total::text,'currency',i.currency,
          'supplier_nit_base',i.supplier_nit_base,'supplier_dv',i.supplier_dv,'supplier_name',i.supplier_name)
        FROM invoices i JOIN documents d ON d.id=i.document_id WHERE i.id=%s FOR UPDATE OF i""",
        (invoice_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise InvoiceNotFoundError(str(invoice_id))
    (
        document_id,
        state,
        version,
        extraction,
        po_id,
        confidence,
        duplicate_of,
        sha,
        current,
    ) = row
    if state not in ("EXTRAIDA", "EXCEPCION", "VALIDADA"):
        raise ValidationConflictError(
            "Solo se validan facturas EXTRAIDA, EXCEPCION o VALIDADA"
        )
    if not extraction or not extraction.get("data"):
        raise ValidationConflictError("No hay extracción estructurada para validar")
    # The snapshot preserves original precision and duplicate data omitted from normalized tables.
    source = deepcopy(extraction["data"])
    if not duplicate_of:
        # Revalidation uses corrected relational data, not the original OCR snapshot.
        for field in (
            "prefix",
            "number",
            "issue_date",
            "subtotal",
            "total",
            "currency",
        ):
            source[field] = current[field]
        source["supplier"] = {
            "nit_base": current["supplier_nit_base"],
            "dv": current["supplier_dv"],
            "name": current["supplier_name"],
        }
        cur.execute(
            """SELECT line_no, description, quantity, unit_price, discount_amount, amount
                    FROM invoice_lines WHERE invoice_id=%s ORDER BY line_no""",
            (invoice_id,),
        )
        source["lines"] = [
            dict(
                zip(
                    (
                        "line_no",
                        "description",
                        "quantity",
                        "unit_price",
                        "discount_amount",
                        "amount",
                    ),
                    r,
                )
            )
            for r in cur.fetchall()
        ]
        cur.execute(
            "SELECT tax_type, rate, base, amount FROM invoice_taxes WHERE invoice_id=%s",
            (invoice_id,),
        )
        source["taxes"] = [
            dict(zip(("type", "rate", "base", "amount"), r)) for r in cur.fetchall()
        ]
    nit = normalize_nit(source.get("supplier", {}).get("nit_base"))
    prefix, number = source.get("prefix"), source.get("number")
    duplicates = [duplicate_of] if duplicate_of else []
    if nit and prefix and number:
        cur.execute(
            """SELECT id FROM invoices WHERE supplier_nit_base=%s AND prefix=%s
                    AND number=%s AND id<>%s""",
            (nit, prefix, number, invoice_id),
        )
        duplicates.extend(r[0] for r in cur.fetchall())
    cur.execute(
        """SELECT id FROM documents WHERE sha256=%s AND id<>%s
           AND duplicate_of_invoice_id IS DISTINCT FROM %s
           AND received_at <= (SELECT received_at FROM documents WHERE id=%s)""",
        (sha, document_id, invoice_id, document_id),
    )
    document_ids = [r[0] for r in cur.fetchall()]
    reloads = []
    if document_ids:
        cur.execute(
            "SELECT id FROM invoices WHERE document_id=ANY(%s) AND id<>%s",
            (document_ids, invoice_id),
        )
        reloads = [r[0] for r in cur.fetchall()]
    po = None
    if po_id:
        cur.execute(
            "SELECT order_number, subtotal FROM purchase_orders WHERE id=%s", (po_id,)
        )
        order = cur.fetchone()
        if order:
            po = {"order_number": order[0], "subtotal": order[1]}
    blockers = []
    if duplicate_of:
        blockers.append("PREEXISTING_DUPLICATE")
    if confidence is None or confidence < Decimal("0.85"):
        blockers.append("LOW_EXTRACTION_CONFIDENCE")
    # Existing non-financial exceptions must not be silently resolved by arithmetic.
    cur.execute(
        """SELECT e.id FROM exceptions e JOIN rule_checks c ON c.id=e.rule_check_id
        WHERE e.invoice_id=%s AND e.resolution_status='OPEN' AND c.rule_id<>ALL(%s)""",
        (invoice_id, [r for r in RULE_IDS if config.enabled.get(r, True)]),
    )
    if cur.fetchall():
        blockers.append("EXISTING_EXTERNAL_EXCEPTION")
    data = ValidationInput(
        invoice_id=invoice_id,
        data_version=version,
        invoice=source,
        purchase_order=po,
        reference_date=reference_date,
        duplicate_invoice_ids=duplicates,
        same_document_invoice_ids=reloads,
        external_blockers=blockers,
    )
    output = evaluate_invoice(data, config)
    # Config/input and exact check IDs identify a run without a schema migration.
    for c in output.checks:
        # Keep only references that can be traced to the original document.
        c.evidence_refs.extend(
            dict(e, document_id=str(document_id))
            for e in extraction.get("evidence", [])
        )
        for amount in (c.expected_amount, c.actual_amount, c.difference):
            if amount is not None and abs(amount) >= Decimal("1e16"):
                raise ValidationConflictError("Resultado fuera de NUMERIC(18,2)")
        if c.difference_pct is not None and abs(c.difference_pct) >= Decimal("1e5"):
            raise ValidationConflictError("Porcentaje fuera de NUMERIC(9,4)")
        refs = c.evidence_refs + [{"validation_run_id": str(output.validation_run_id)}]
        cur.execute(
            """INSERT INTO rule_checks (id,invoice_id,data_version,ruleset_version,evaluation_date,
            rule_id,invoice_line_no,result,expected_amount,actual_amount,difference,difference_pct,tolerance,
            expected_text,actual_text,reason,evidence_refs) VALUES
            (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
            (
                c.rule_check_id,
                invoice_id,
                version,
                config.ruleset_version,
                reference_date,
                c.rule_id,
                c.invoice_line_no,
                c.result,
                c.expected_amount,
                c.actual_amount,
                c.difference,
                c.difference_pct,
                c.tolerance,
                c.expected_text,
                c.actual_text,
                c.reason,
                json.dumps(refs),
            ),
        )
    checks_by_id = {c.rule_check_id: c for c in output.checks}
    for e in output.exceptions:
        e.evidence_refs = checks_by_id[e.rule_check_id].evidence_refs.copy()
        cur.execute(
            """INSERT INTO exceptions (id,invoice_id,rule_check_id,exception_type,severity,message,
            requires_review,resolution_status) VALUES (%s,%s,%s,%s,%s,%s,true,'OPEN')""",
            (
                e.exception_id,
                invoice_id,
                e.rule_check_id,
                e.type,
                e.severity,
                e.description,
            ),
        )
    cur.execute(
        """SELECT payload->>'run_sequence' FROM invoice_events
        WHERE invoice_id=%s AND event_type='VALIDATION_COMPLETED'""",
        (invoice_id,),
    )
    # The invoice row lock serializes sequence allocation, including repeated calls
    # inside one transaction (where now() would otherwise produce equal timestamps).
    sequence = (
        max((int(r[0]) for r in cur.fetchall() if r[0] is not None), default=0) + 1
    )
    payload = {
        "run_sequence": sequence,
        "validation_run_id": str(output.validation_run_id),
        "data_version": version,
        "configuration": config.model_dump(mode="json"),
        "input": data.model_dump(mode="json"),
        "result": output.model_dump(mode="json"),
    }
    target = (
        InvoiceStatus.EXCEPCION
        if output.requires_human_review
        else InvoiceStatus.VALIDADA
    )
    if state != target:
        transition_invoice_status(
            cur,
            invoice_id=invoice_id,
            to_status=target,
            event_type="VALIDATION_COMPLETED",
            payload=payload,
        )
    else:
        cur.execute(
            """INSERT INTO invoice_events (invoice_id,document_id,event_type,actor_type,
            from_status,to_status,payload) VALUES (%s,%s,'VALIDATION_COMPLETED','system',%s,%s,%s::jsonb)""",
            (invoice_id, document_id, state, state, json.dumps(payload)),
        )
    return output


def get_current_validation_from_db(cur, invoice_id: UUID) -> ValidationOutput | None:
    """Select only the latest completed run of the invoice's current data version."""
    cur.execute(
        """SELECT e.payload->'result'
        FROM invoices i JOIN invoice_events e ON e.invoice_id=i.id
        WHERE i.id=%s AND e.event_type='VALIDATION_COMPLETED'
          AND (e.payload->>'data_version')::integer=i.data_version
        ORDER BY COALESCE((e.payload->>'run_sequence')::bigint,0) DESC,
                 e.occurred_at DESC, e.id DESC LIMIT 1""",
        (invoice_id,),
    )
    row = cur.fetchone()
    return ValidationOutput.model_validate(row[0]) if row else None
