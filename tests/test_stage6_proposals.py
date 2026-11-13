"""Unit and integration tests for Stage 6: Accounting Proposal & Explanation."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.proposals.claude_service import ClaudeProposalExplainer
from app.modules.proposals.repository import (
    record_proposal_generated_event,
    save_accounting_proposal_to_db,
)
from app.modules.proposals.service import ProposalService
from app.schemas.proposals import (
    AccountingContextInput,
    AmountsContextInput,
    Explanation,
    ProposalMetadata,
    RiskAssessmentContextInput,
    Stage6ProposalInput,
    Stage6ProposalOutput,
    SupplierContextInput,
    ValidationExceptionItem,
)


@pytest.fixture
def clean_invoice_input() -> Stage6ProposalInput:
    """Invoice without any exceptions."""
    return Stage6ProposalInput(
        invoice_id=uuid4(),
        data_version=1,
        supplier=SupplierContextInput(
            supplier_id=uuid4(),
            name="Tecnologías Pacífico S.A.S.",
            nit="900654321-4",
        ),
        amounts=AmountsContextInput(
            subtotal="50000000.00",
            tax_total="9500000.00",
            total="59500000.00",
            currency="COP",
        ),
        validation_exceptions=[],
        risk_assessment=RiskAssessmentContextInput(
            risk_level="low",
            requires_human_review=False,
            risk_score=0.05,
        ),
        accounting_context=AccountingContextInput(
            suggested_account="5135",
            cost_center="CC02",
            authorized_accounts=["5135", "5105", "1435"],
            authorized_cost_centers=["CC01", "CC02", "CC03"],
        ),
        document_references={
            "prefix": "FE",
            "number": "9001",
            "order_number": "OC-2026-9001",
            "contract_number": "CTR-2026-001",
        },
    )


@pytest.fixture
def exception_invoice_input() -> Stage6ProposalInput:
    """Invoice with PO discrepancy exception."""
    return Stage6ProposalInput(
        invoice_id=uuid4(),
        data_version=1,
        supplier=SupplierContextInput(
            supplier_id=uuid4(),
            name="Tecnologías Pacífico S.A.S.",
            nit="900654321-4",
        ),
        amounts=AmountsContextInput(
            subtotal="52000000.00",
            tax_total="9880000.00",
            total="61880000.00",
            currency="COP",
        ),
        validation_exceptions=[
            ValidationExceptionItem(
                type="DIFERENCIA_VS_OC",
                expected="50000000.00",
                actual="52000000.00",
                difference="2000000.00",
                difference_percentage="4.00",
                severity="high",
                description="Total facturado excede la orden de compra en $2,000,000 COP",
            )
        ],
        risk_assessment=RiskAssessmentContextInput(
            risk_level="high",
            requires_human_review=True,
            risk_score=0.92,
        ),
        accounting_context=AccountingContextInput(
            suggested_account="5135",
            cost_center="CC02",
            authorized_accounts=["5135", "5105", "1435"],
            authorized_cost_centers=["CC01", "CC02", "CC03"],
        ),
        document_references={
            "prefix": "FE",
            "number": "9001",
            "order_number": "OC-2026-9001",
            "contract_number": "CTR-2026-001",
        },
    )


# 1. Clean invoice: Must use TEMPLATE, never call Claude
def test_clean_invoice_uses_template_without_claude(clean_invoice_input):
    mock_claude = MagicMock(spec=ClaudeProposalExplainer)
    service = ProposalService(claude_explainer=mock_claude)

    output = service.generate_proposal(clean_invoice_input)

    assert output.schema_version == "0.2.0"
    assert output.invoice_id == clean_invoice_input.invoice_id
    assert output.generated_by == "TEMPLATE"
    assert output.accounting_proposal.status == "READY_FOR_APPROVAL"
    assert output.accounting_proposal.account_code == "5135"
    assert output.accounting_proposal.cost_center_code == "CC02"
    assert "FE-9001" in output.explanation.summary
    assert "OC-2026-9001" in output.explanation.details

    # Assert Claude was NOT called
    mock_claude.explain_exceptions.assert_not_called()


# 2. Exception invoice: Must use CLAUDE and status BLOCKED_PENDING_REVIEW
def test_exception_invoice_uses_claude(exception_invoice_input):
    mock_claude = MagicMock(spec=ClaudeProposalExplainer)
    mock_claude.explain_exceptions.return_value = (
        Explanation(
            summary="La factura FE-9001 supera en $2.000.000 (4%) el valor autorizado en la orden de compra OC-2026-9001.",
            details="Se facturaron $52.000.000 frente a $50.000.000 respaldados en la orden de compra OC-2026-9001.",
            recommendation="Solicitar nota crédito por $2.000.000 + IVA o anexar autorización escrita del área de compras.",
        ),
        ProposalMetadata(
            latency_ms=1150.0,
            input_tokens=220,
            output_tokens=65,
            model="claude-sonnet-4-6",
            account_validation_status="VALIDATED",
        ),
    )

    service = ProposalService(claude_explainer=mock_claude)
    output = service.generate_proposal(exception_invoice_input)

    assert output.schema_version == "0.2.0"
    assert output.generated_by == "CLAUDE"
    assert output.accounting_proposal.status == "BLOCKED_PENDING_REVIEW"
    assert output.accounting_proposal.subtotal == "52000000.00"
    assert output.accounting_proposal.total == "61880000.00"
    assert "nota crédito" in output.explanation.recommendation
    mock_claude.explain_exceptions.assert_called_once()


# 3. Rule: Do not invent accounts; validate against authorized accounts
def test_accounting_code_validation_rejects_unauthorized_account(
    exception_invoice_input,
):
    exception_invoice_input.accounting_context.suggested_account = "9999_INVENTADA"
    mock_claude = MagicMock(spec=ClaudeProposalExplainer)
    mock_claude.explain_exceptions.return_value = (
        Explanation(summary="Sum", details="Det", recommendation="Rec"),
        ProposalMetadata(latency_ms=10.0),
    )

    service = ProposalService(claude_explainer=mock_claude)
    output = service.generate_proposal(exception_invoice_input)

    assert output.accounting_proposal.account_code is None
    assert output.metadata.account_validation_status == "PENDING_VALIDATION"


# 4. Fallback when Claude fails or is unavailable
def test_claude_service_fallback_on_api_error(exception_invoice_input):
    explainer = ClaudeProposalExplainer(api_key=None)  # No API key triggers fallback
    explanation, metadata = explainer.explain_exceptions(exception_invoice_input)

    assert "FE-9001" in explanation.summary
    assert "DIFERENCIA_VS_OC" in explanation.summary
    assert (
        "nota crédito" in explanation.recommendation.lower()
        or "revisión" in explanation.recommendation.lower()
    )
    assert "contingency-rules" in metadata.model


# 5. Database operations: save proposal and event
def test_save_accounting_proposal_to_db():
    cur = MagicMock()
    inv_id = uuid4()
    supp_id = uuid4()

    output = Stage6ProposalOutput(
        invoice_id=inv_id,
        generated_by="CLAUDE",
        explanation=Explanation(
            summary="Resumen de prueba",
            details="Detalle de prueba",
            recommendation="Recomendación de prueba",
        ),
        accounting_proposal={
            "supplier_id": supp_id,
            "account_code": "5135",
            "cost_center_code": "CC02",
            "subtotal": "100000.00",
            "tax_total": "19000.00",
            "total": "119000.00",
            "status": "BLOCKED_PENDING_REVIEW",
        },
    )

    save_accounting_proposal_to_db(
        cur,
        invoice_id=inv_id,
        data_version=1,
        output=output,
    )

    assert cur.execute.called
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "INSERT INTO accounting_proposals" in sql
    assert params[0] == inv_id
    assert params[1] == 1
    assert params[2] == "BLOCKED_PENDING_REVIEW"
    assert params[4] == "5135"


def test_record_proposal_generated_event():
    cur = MagicMock()
    inv_id = uuid4()
    doc_id = uuid4()

    output = Stage6ProposalOutput(
        invoice_id=inv_id,
        generated_by="TEMPLATE",
        explanation=Explanation(summary="S", details="D", recommendation="R"),
        accounting_proposal={
            "account_code": "5135",
            "cost_center_code": "CC02",
            "subtotal": "100.00",
            "tax_total": "19.00",
            "total": "119.00",
            "status": "READY_FOR_APPROVAL",
        },
    )

    record_proposal_generated_event(
        cur,
        invoice_id=inv_id,
        document_id=doc_id,
        business_status="VALIDADA",
        output=output,
    )

    assert cur.execute.called
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "INSERT INTO invoice_events" in sql
    assert params[0] == inv_id
    assert params[1] == doc_id
    assert params[2] == "PROPOSAL_GENERATED"
    assert params[3] == "system"


# 6. Test FastAPI direct endpoint
def test_fastapi_proposal_endpoints(clean_invoice_input, exception_invoice_input):
    client = TestClient(app)

    # Clean invoice -> TEMPLATE
    res_clean = client.post(
        "/api/invoices/proposals", json=clean_invoice_input.model_dump(mode="json")
    )
    assert res_clean.status_code == 200
    data_clean = res_clean.json()
    assert data_clean["schema_version"] == "0.2.0"
    assert data_clean["generated_by"] == "TEMPLATE"
    assert data_clean["accounting_proposal"]["status"] == "READY_FOR_APPROVAL"

    # Exception invoice with mocked Claude
    mock_explainer = MagicMock(spec=ClaudeProposalExplainer)
    mock_explainer.explain_exceptions.return_value = (
        Explanation(summary="Sum", details="Det", recommendation="Rec"),
        ProposalMetadata(latency_ms=100.0),
    )
    with patch(
        "app.api.routers.invoices.ClaudeProposalExplainer", return_value=mock_explainer
    ):
        res_exc = client.post(
            "/api/invoices/proposals",
            json=exception_invoice_input.model_dump(mode="json"),
        )
        assert res_exc.status_code == 200
        data_exc = res_exc.json()
        assert data_exc["schema_version"] == "0.2.0"
        assert data_exc["generated_by"] == "CLAUDE"
        assert data_exc["accounting_proposal"]["status"] == "BLOCKED_PENDING_REVIEW"
