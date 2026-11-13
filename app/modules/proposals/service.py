"""Domain orchestrator service for Stage 6: Accounting Proposal & Explanation."""

import logging

from app.modules.proposals.claude_service import ClaudeProposalExplainer
from app.modules.proposals.template_service import generate_template_explanation
from app.schemas.proposals import (
    AccountingProposalData,
    ProposalMetadata,
    ProposalStatus,
    Stage6ProposalInput,
    Stage6ProposalOutput,
)

logger = logging.getLogger(__name__)


class ProposalService:
    """Orchestrates accounting proposal generation and exception explanation.

    Separates the generative explanation (via Template or Claude)
    from deterministic financial and accounting rules.
    """

    def __init__(self, claude_explainer: ClaudeProposalExplainer | None = None):
        self.claude_explainer = claude_explainer or ClaudeProposalExplainer()

    def generate_proposal(
        self, input_data: Stage6ProposalInput
    ) -> Stage6ProposalOutput:
        """Generate structured accounting proposal and explanation adhering to rules."""
        has_exceptions = bool(input_data.validation_exceptions)
        requires_review = input_data.risk_assessment.requires_human_review

        # Rule 1: No llamar a Claude para casos normales que puedan explicarse con plantilla
        if not has_exceptions and not requires_review:
            generated_by = "TEMPLATE"
            explanation = generate_template_explanation(input_data)
            status: ProposalStatus = "READY_FOR_APPROVAL"
            metadata = ProposalMetadata(
                latency_ms=0.5,
                input_tokens=0,
                output_tokens=0,
                model="deterministic-template",
                account_validation_status="VALIDATED",
            )
        else:
            # Rule 2: Utilizar Claude cuando existan excepciones o se requiera revisión
            generated_by = "CLAUDE"
            status: ProposalStatus = "BLOCKED_PENDING_REVIEW"
            explanation, metadata = self.claude_explainer.explain_exceptions(input_data)

        # Rule 4 & 5: Validar cuentas contables y centros de costo autorizados (sin inventar códigos)
        account_code, cost_center_code, val_status = self._validate_accounting_codes(
            input_data.accounting_context
        )
        metadata.account_validation_status = val_status

        proposal_data = AccountingProposalData(
            supplier_id=input_data.supplier.supplier_id,
            account_code=account_code,
            cost_center_code=cost_center_code,
            subtotal=str(input_data.amounts.subtotal),
            tax_total=str(input_data.amounts.tax_total),
            total=str(input_data.amounts.total),
            status=status,
        )

        return Stage6ProposalOutput(
            schema_version="0.2.0",
            invoice_id=input_data.invoice_id,
            generated_by=generated_by,
            explanation=explanation,
            accounting_proposal=proposal_data,
            metadata=metadata,
        )

    def _validate_accounting_codes(
        self, acct_ctx
    ) -> tuple[str | None, str | None, str]:
        """Validate suggested account and cost center against authorized master lists."""
        suggested_account = acct_ctx.suggested_account
        suggested_cc = acct_ctx.cost_center
        authorized_accounts = set(acct_ctx.authorized_accounts or [])
        authorized_ccs = set(acct_ctx.authorized_cost_centers or [])

        # If authorized lists are provided, enforce membership
        acct_valid = True
        if authorized_accounts and suggested_account:
            acct_valid = suggested_account in authorized_accounts

        cc_valid = True
        if authorized_ccs and suggested_cc:
            cc_valid = suggested_cc in authorized_ccs

        status = "VALIDATED"
        if not suggested_account or not suggested_cc or not acct_valid or not cc_valid:
            status = "PENDING_VALIDATION"

        final_acct = (
            suggested_account if (acct_valid or not authorized_accounts) else None
        )
        final_cc = suggested_cc if (cc_valid or not authorized_ccs) else None

        return final_acct, final_cc, status
