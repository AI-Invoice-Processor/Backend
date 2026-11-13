"""Database persistence and query adapter for Stage 6 accounting proposals."""

import asyncio
import json
from decimal import Decimal
from uuid import UUID

from app.modules.proposals.service import ProposalService
from app.modules.validation.repository import get_current_validation_from_db
from app.schemas.proposals import (
    AccountingContextInput,
    AmountsContextInput,
    PassedCheckItem,
    RiskAssessmentContextInput,
    Stage6ProposalInput,
    Stage6ProposalOutput,
    SupplierContextInput,
    ValidationExceptionItem,
)
from app.state_machine import InvoiceStatus, transition_invoice_status


class InvoiceNotFoundError(ValueError):
    pass


def get_accounting_catalogs(cur) -> tuple[list[str], list[str]]:
    """Fetch authorized accounts and cost centers from master tables."""
    cur.execute("SELECT code FROM accounts ORDER BY code")
    accounts = [r[0] for r in cur.fetchall()]

    cur.execute("SELECT code FROM cost_centers ORDER BY code")
    cost_centers = [r[0] for r in cur.fetchall()]

    return accounts, cost_centers


def save_accounting_proposal_to_db(
    cur,
    *,
    invoice_id: UUID,
    data_version: int,
    output: Stage6ProposalOutput,
) -> None:
    """Upsert proposal into accounting_proposals table."""
    prop = output.accounting_proposal
    exp = output.explanation

    subtotal_val = Decimal(prop.subtotal) if prop.subtotal else None
    tax_val = Decimal(prop.tax_total) if prop.tax_total else None
    total_val = Decimal(prop.total) if prop.total else None

    cur.execute(
        """
        INSERT INTO accounting_proposals (
            invoice_id,
            data_version,
            status,
            supplier_id,
            account_code,
            cost_center_code,
            subtotal,
            tax_total,
            total,
            explanation_summary,
            explanation_details,
            recommendation,
            generated_by
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (invoice_id, data_version) DO UPDATE SET
            status = EXCLUDED.status,
            supplier_id = EXCLUDED.supplier_id,
            account_code = EXCLUDED.account_code,
            cost_center_code = EXCLUDED.cost_center_code,
            subtotal = EXCLUDED.subtotal,
            tax_total = EXCLUDED.tax_total,
            total = EXCLUDED.total,
            explanation_summary = EXCLUDED.explanation_summary,
            explanation_details = EXCLUDED.explanation_details,
            recommendation = EXCLUDED.recommendation,
            generated_by = EXCLUDED.generated_by
        """,
        (
            invoice_id,
            data_version,
            prop.status,
            prop.supplier_id,
            prop.account_code,
            prop.cost_center_code,
            subtotal_val,
            tax_val,
            total_val,
            exp.summary,
            exp.details,
            exp.recommendation,
            output.generated_by,
        ),
    )


def record_proposal_generated_event(
    cur,
    *,
    invoice_id: UUID,
    document_id: UUID,
    business_status: str,
    output: Stage6ProposalOutput,
) -> None:
    """Persist an audit trail event in invoice_events."""
    payload = output.model_dump(mode="json")
    actor_name = (
        "Claude Sonnet" if output.generated_by == "CLAUDE" else "Template Engine"
    )
    actor_type = "ai" if output.generated_by == "CLAUDE" else "system"

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
            "PROPOSAL_GENERATED",
            actor_type,
            actor_name,
            business_status,
            business_status,
            f"Propuesta contable generada ({output.generated_by}): {output.accounting_proposal.status}",
            json.dumps(payload),
        ),
    )


async def generate_proposal_from_db(
    cur,
    invoice_id: UUID,
    service: ProposalService,
) -> Stage6ProposalOutput:
    """Load invoice and context from DB, generate proposal, persist and update status."""
    cur.execute(
        """
        SELECT i.document_id, i.data_version, i.business_status, i.supplier_id,
               i.contract_id, i.purchase_order_id, i.subtotal, i.total,
               i.retentions_total, i.currency, i.prefix, i.number,
               i.risk_level, i.risk_score, i.risk_confidence,
               s.legal_name, s.nit_base, s.dv,
               c.contract_number, c.default_account, c.default_cost_center,
               po.order_number, po.subtotal, po.tax_amount, po.total
          FROM invoices i
          LEFT JOIN suppliers s ON s.id = i.supplier_id
          LEFT JOIN contracts c ON c.id = i.contract_id
          LEFT JOIN purchase_orders po ON po.id = i.purchase_order_id
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
        data_version,
        business_status,
        supplier_id,
        _contract_id,
        po_id,
        subtotal,
        total,
        _retentions_total,
        currency,
        prefix,
        number,
        risk_level,
        risk_score,
        risk_confidence,
        supplier_name,
        supplier_nit,
        supplier_dv,
        contract_number,
        default_account,
        default_cost_center,
        po_number,
        _po_subtotal,
        _po_tax,
        _po_total,
    ) = row

    # Calculate tax_total (total - subtotal)
    subtotal_dec = Decimal(str(subtotal or "0.00"))
    total_dec = Decimal(str(total or "0.00"))
    tax_dec = total_dec - subtotal_dec

    # 1. Fetch Stage 4 validation exceptions
    validation_output = get_current_validation_from_db(cur, invoice_id)
    validation_exceptions: list[ValidationExceptionItem] = []
    passed_checks: list[PassedCheckItem] = []

    if validation_output:
        checks_by_id = {c.rule_check_id: c for c in validation_output.checks}
        passed_checks = [
            PassedCheckItem(
                rule_id=c.rule_id, invoice_line_no=c.invoice_line_no, reason=c.reason
            )
            for c in validation_output.checks
            if c.result == "passed"
        ]
        cur.execute(
            """
            SELECT exception_type
              FROM exceptions
             WHERE invoice_id = %s AND resolution_status = 'OPEN'
            """,
            (invoice_id,),
        )
        open_exception_types = {row[0] for row in cur.fetchall()}
        for exc in validation_output.exceptions:
            if exc.type not in open_exception_types:
                continue
            source_check = checks_by_id.get(exc.rule_check_id)
            validation_exceptions.append(
                ValidationExceptionItem(
                    type=exc.type,
                    rule_id=exc.rule_id,
                    reason_code=source_check.reason_code if source_check else None,
                    invoice_line_no=source_check.invoice_line_no
                    if source_check
                    else None,
                    expected=str(exc.expected_amount)
                    if exc.expected_amount is not None
                    else None,
                    actual=str(exc.actual_amount)
                    if exc.actual_amount is not None
                    else None,
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
        # Fallback to direct query on exceptions table
        cur.execute(
            """
            SELECT e.exception_type, e.severity, e.message, c.expected_amount,
                   c.actual_amount, c.difference, c.difference_pct
              FROM exceptions e
              LEFT JOIN rule_checks c ON c.id = e.rule_check_id
             WHERE e.invoice_id = %s AND e.resolution_status = 'OPEN'
            """,
            (invoice_id,),
        )
        for r in cur.fetchall():
            validation_exceptions.append(
                ValidationExceptionItem(
                    type=r[0],
                    severity=r[1],
                    description=r[2],
                    expected=str(r[3]) if r[3] is not None else None,
                    actual=str(r[4]) if r[4] is not None else None,
                    difference=str(r[5]) if r[5] is not None else None,
                    difference_percentage=str(r[6]) if r[6] is not None else None,
                )
            )

    # 2. Authorized accounts and cost centers
    auth_accounts, auth_ccs = get_accounting_catalogs(cur)

    # 3. Support documents
    support_docs = []
    if po_id:
        cur.execute(
            "SELECT number, support_type, received_amount FROM support_documents WHERE purchase_order_id = %s",
            (po_id,),
        )
        support_docs = [
            {"number": r[0], "type": r[1], "amount": str(r[2])} for r in cur.fetchall()
        ]

    doc_references = {
        "invoice_number": f"{prefix or ''}-{number or ''}".strip("-"),
        "prefix": prefix,
        "number": number,
        "order_number": po_number,
        "contract_number": contract_number,
        "support_documents": support_docs,
        "currency": currency or "COP",
    }

    # 4. Assemble input
    requires_review = bool(
        validation_exceptions
        or (risk_level and risk_level.lower() in ("medium", "high", "critical"))
        or business_status == "EXCEPCION"
    )

    input_data = Stage6ProposalInput(
        invoice_id=invoice_id,
        data_version=data_version,
        supplier=SupplierContextInput(
            supplier_id=supplier_id,
            name=supplier_name,
            nit=f"{supplier_nit}-{supplier_dv}"
            if (supplier_nit and supplier_dv)
            else supplier_nit,
        ),
        amounts=AmountsContextInput(
            subtotal=str(subtotal_dec),
            tax_total=str(tax_dec),
            total=str(total_dec),
            currency=currency or "COP",
        ),
        validation_exceptions=validation_exceptions,
        passed_checks=passed_checks,
        risk_assessment=RiskAssessmentContextInput(
            risk_level=risk_level or "low",
            requires_human_review=requires_review,
            risk_score=float(risk_score) if risk_score is not None else None,
            confidence=float(risk_confidence) if risk_confidence is not None else None,
        ),
        accounting_context=AccountingContextInput(
            suggested_account=default_account or "5135",
            cost_center=default_cost_center or "CC01",
            authorized_accounts=auth_accounts,
            authorized_cost_centers=auth_ccs,
        ),
        document_references=doc_references,
    )

    # 5. Execute proposal service asynchronously
    res = service.generate_proposal(input_data)
    output = await res if asyncio.iscoroutine(res) else res

    # 6. Save proposal to database
    save_accounting_proposal_to_db(
        cur,
        invoice_id=invoice_id,
        data_version=data_version,
        output=output,
    )

    # 7. State transition if valid
    current_status = InvoiceStatus(business_status)
    if (
        output.accounting_proposal.status == "READY_FOR_APPROVAL"
        and current_status == InvoiceStatus.VALIDADA
    ):
        transition_invoice_status(
            cur,
            invoice_id=invoice_id,
            to_status=InvoiceStatus.PROPUESTA,
            event_type="PROPOSAL_GENERATED",
            actor_type="system" if output.generated_by == "TEMPLATE" else "ai",
            reason="Propuesta contable generada sin excepciones",
            payload=output.model_dump(mode="json"),
        )
    else:
        # Keep current business status (e.g. EXCEPCION) and record the event
        record_proposal_generated_event(
            cur,
            invoice_id=invoice_id,
            document_id=document_id,
            business_status=business_status,
            output=output,
        )

    return output


def get_accounting_proposal_from_db(
    cur, invoice_id: UUID
) -> Stage6ProposalOutput | None:
    """Retrieve the latest accounting proposal for an invoice."""
    cur.execute(
        """
        SELECT ap.invoice_id, ap.status, ap.supplier_id, ap.account_code,
               ap.cost_center_code, ap.subtotal, ap.tax_total, ap.total,
               ap.explanation_summary, ap.explanation_details, ap.recommendation,
               ap.generated_by
          FROM accounting_proposals ap
          JOIN invoices i ON i.id = ap.invoice_id AND i.data_version = ap.data_version
         WHERE ap.invoice_id = %s
         LIMIT 1
        """,
        (invoice_id,),
    )
    row = cur.fetchone()
    if not row:
        return None

    return Stage6ProposalOutput(
        schema_version="0.2.0",
        invoice_id=row[0],
        generated_by=row[11],
        explanation={
            "summary": row[8] or "",
            "details": row[9] or "",
            "recommendation": row[10] or "",
        },
        accounting_proposal={
            "supplier_id": row[2],
            "account_code": row[3],
            "cost_center_code": row[4],
            "subtotal": str(row[5] or "0.00"),
            "tax_total": str(row[6] or "0.00"),
            "total": str(row[7] or "0.00"),
            "status": row[1],
        },
    )
