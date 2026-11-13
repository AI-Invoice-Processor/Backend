"""Database persistence and integration for Stage 5 exception evaluation."""

import json
from decimal import Decimal
from uuid import UUID

from app.modules.exceptions.evaluator import ExceptionEvaluatorService
from app.modules.validation.repository import get_current_validation_from_db
from app.schemas.exceptions import (
    EvaluationContextInput,
    ExceptionFindingInput,
    Stage5EvaluationInput,
    Stage5EvaluationOutput,
)


class InvoiceNotFoundError(ValueError):
    pass


def update_invoice_risk_fields(
    cur,
    *,
    invoice_id: UUID,
    risk_level: str,
    risk_score: float,
    risk_confidence: float,
) -> None:
    """Update invoices table with calculated risk assessment."""
    # The database contract supports low/medium/high. The evaluator can
    # return the more granular critical level, which is persisted as high
    # while its numeric score remains unchanged.
    persisted_risk_level = (
        "high" if risk_level.lower() == "critical" else risk_level.lower()
    )
    cur.execute(
        """
        UPDATE invoices
           SET risk_level = %s,
               risk_score = %s,
               risk_confidence = %s,
               updated_at = now()
         WHERE id = %s
        """,
        (
            persisted_risk_level,
            Decimal(str(risk_score)),
            Decimal(str(risk_confidence)),
            invoice_id,
        ),
    )


def record_exception_evaluation_event(
    cur,
    *,
    invoice_id: UUID,
    document_id: UUID,
    business_status: str,
    output: Stage5EvaluationOutput,
) -> None:
    """Persist an audit trail event in invoice_events."""
    payload = output.model_dump(mode="json")
    actor_name = "Jev TypeSafe AI"
    if output.audit_trace and output.audit_trace.evaluator == "fallback_rules":
        actor_name = "Fallback Rule Engine"
    elif output.audit_trace and output.audit_trace.evaluator == "claude":
        actor_name = "Claude Sonnet"

    cur.execute(
        """
        INSERT INTO invoice_events (
            invoice_id,
            document_id,
            event_type,
            actor_type,
            actor_name,
            from_status,
            to_status,
            reason,
            payload
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
        )
        """,
        (
            invoice_id,
            document_id,
            "EXCEPTION_EVALUATED",
            "ai",
            actor_name,
            business_status,
            business_status,
            f"Evaluación de riesgo: {output.risk_assessment.risk_level} ({output.risk_assessment.risk_score:.2f})",
            json.dumps(payload),
        ),
    )


def evaluate_invoice_from_db(
    cur,
    invoice_id: UUID,
    service: ExceptionEvaluatorService,
) -> Stage5EvaluationOutput:
    """Fetch invoice validation findings from DB, run Stage 5 evaluation, and update invoices."""
    cur.execute(
        """
        SELECT i.document_id, i.business_status, i.supplier_id, i.contract_id,
               i.purchase_order_id, i.extraction_confidence, i.issue_date,
               d.duplicate_of_invoice_id
          FROM invoices i
          JOIN documents d ON d.id = i.document_id
         WHERE i.id = %s
         FOR UPDATE OF i
        """,
        (invoice_id,),
    )
    row = cur.fetchone()
    if not row:
        raise InvoiceNotFoundError(f"Factura no encontrada: {invoice_id}")

    (
        document_id,
        business_status,
        supplier_id,
        contract_id,
        po_id,
        confidence,
        duplicate_of,
        issue_date,
    ) = (
        row[0],
        row[1],
        row[2],
        row[3],
        row[4],
        row[5],
        row[6],
        row[7],
    )

    # 1. Fetch Stage 4 validation exceptions
    validation_output = get_current_validation_from_db(cur, invoice_id)
    exceptions_input: list[ExceptionFindingInput] = []

    if validation_output:
        for exc in validation_output.exceptions:
            exceptions_input.append(
                ExceptionFindingInput(
                    type=exc.type,
                    difference=str(exc.difference)
                    if exc.difference is not None
                    else None,
                    difference_percentage=str(exc.difference_pct)
                    if exc.difference_pct is not None
                    else None,
                    severity=exc.severity,
                    description=exc.description,
                )
            )
    else:
        # Fallback to direct query on exceptions table if validation_output is not in event payload
        cur.execute(
            """
            SELECT e.exception_type, e.severity, e.message, c.difference, c.difference_pct
              FROM exceptions e
              LEFT JOIN rule_checks c ON c.id = e.rule_check_id
             WHERE e.invoice_id = %s AND e.resolution_status = 'OPEN'
            """,
            (invoice_id,),
        )
        for r in cur.fetchall():
            exceptions_input.append(
                ExceptionFindingInput(
                    type=r[0],
                    severity=r[1],
                    description=r[2],
                    difference=str(r[3]) if r[3] is not None else None,
                    difference_percentage=str(r[4]) if r[4] is not None else None,
                )
            )

    # 2. Check contract validity
    contract_active = True
    if contract_id:
        cur.execute(
            "SELECT start_date, end_date FROM contracts WHERE id = %s",
            (contract_id,),
        )
        c_row = cur.fetchone()
        if c_row and issue_date:
            c_start, c_end = c_row[0], c_row[1]
            if not (c_start <= issue_date <= c_end):
                contract_active = False

    # 3. Check support documents completeness
    support_complete = True
    if po_id:
        cur.execute(
            "SELECT COUNT(*) FROM support_documents WHERE purchase_order_id = %s",
            (po_id,),
        )
        s_count = cur.fetchone()[0]
        if s_count == 0:
            support_complete = False

    # 4. Assemble context
    context = EvaluationContextInput(
        supplier_matched=bool(supplier_id is not None),
        contract_active=contract_active,
        support_complete=support_complete,
        duplicate_detected=bool(duplicate_of is not None),
        extraction_confidence=float(confidence) if confidence is not None else 1.0,
    )

    input_data = Stage5EvaluationInput(
        invoice_id=invoice_id,
        exceptions=exceptions_input,
        context=context,
    )

    # 5. Execute Stage 5 Evaluation
    output = service.evaluate_exceptions(input_data)

    # 6. Update invoices table
    update_invoice_risk_fields(
        cur,
        invoice_id=invoice_id,
        risk_level=output.risk_assessment.risk_level,
        risk_score=output.risk_assessment.risk_score,
        risk_confidence=output.risk_assessment.confidence,
    )

    # 7. Record event
    record_exception_evaluation_event(
        cur,
        invoice_id=invoice_id,
        document_id=document_id,
        business_status=business_status,
        output=output,
    )

    return output
