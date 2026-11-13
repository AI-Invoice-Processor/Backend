"""Unit and integration tests for Stage 5: Intelligent exception evaluation with Jev."""

from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.modules.exceptions.adapter import (
    JevConnectionError,
    JevExceptionAdapter,
    JevJudgmentResult,
)
from app.modules.exceptions.evaluator import (
    ExceptionEvaluatorService,
    RuleBasedFallbackEvaluator,
)
from app.modules.exceptions.repository import (
    evaluate_invoice_from_db,
    record_exception_evaluation_event,
    update_invoice_risk_fields,
)
from app.schemas.exceptions import (
    EvaluationContextInput,
    ExceptionFindingInput,
    Stage5EvaluationInput,
    Stage5EvaluationOutput,
)


@pytest.fixture
def sample_po_exception_input() -> Stage5EvaluationInput:
    return Stage5EvaluationInput(
        invoice_id=uuid4(),
        exceptions=[
            ExceptionFindingInput(
                type="DIFERENCIA_VS_OC",
                difference="2000000.00",
                difference_percentage="4.00",
                severity="high",
                description="Diferencia de $2,000,000 COP (4%) contra orden de compra",
            )
        ],
        context=EvaluationContextInput(
            supplier_matched=True,
            contract_active=True,
            support_complete=True,
            duplicate_detected=False,
            extraction_confidence=0.98,
        ),
    )


@pytest.fixture
def mock_jev_adapter():
    adapter = MagicMock(spec=JevExceptionAdapter)
    adapter.evaluate_findings.return_value = JevJudgmentResult(
        category="amount_mismatch",
        category_confidence=0.96,
        category_probabilities={"amount_mismatch": 0.96, "other": 0.04},
        risk_level="high",
        risk_confidence=0.94,
        risk_probabilities={"high": 0.85, "medium": 0.10, "critical": 0.05, "low": 0.0},
        information_sufficiency=0.95,
        ambiguity_detected=0.08,
        input_tokens=180,
        output_tokens=4,
        latency_ms=120.5,
        model_version="jev-1.14.0",
        raw_state={},
    )
    return adapter


# 1. Test schema normalization and contracts
def test_stage5_schema_contract_matching(sample_po_exception_input, mock_jev_adapter):
    service = ExceptionEvaluatorService(jev_adapter=mock_jev_adapter)
    output = service.evaluate_exceptions(sample_po_exception_input)

    assert isinstance(output, Stage5EvaluationOutput)
    assert output.schema_version == "0.2.0"
    assert output.invoice_id == sample_po_exception_input.invoice_id
    assert output.risk_assessment.risk_level == "high"
    assert output.risk_assessment.exception_category == "amount_mismatch"
    assert output.risk_assessment.requires_human_review is True
    assert output.decision == "REQUIERE_REVISION"
    assert 0.0 <= output.risk_assessment.risk_score <= 1.0
    assert 0.0 <= output.risk_assessment.confidence <= 1.0


# 2. Test guardrail: Never allow Jev to override mandatory failed validations
def test_guardrail_mandatory_exceptions_cannot_be_overridden_by_jev(
    sample_po_exception_input,
):
    # Even if Jev erroneously reports "low" risk with 1.0 confidence:
    lenient_adapter = MagicMock(spec=JevExceptionAdapter)
    lenient_adapter.evaluate_findings.return_value = JevJudgmentResult(
        category="amount_mismatch",
        category_confidence=0.99,
        category_probabilities={"amount_mismatch": 1.0},
        risk_level="low",  # Jev says low
        risk_confidence=0.99,
        risk_probabilities={"low": 1.0},
        information_sufficiency=0.99,
        ambiguity_detected=0.01,
        input_tokens=100,
        output_tokens=4,
        latency_ms=50.0,
        model_version="jev-1.14.0",
        raw_state={},
    )
    service = ExceptionEvaluatorService(jev_adapter=lenient_adapter)
    output = service.evaluate_exceptions(sample_po_exception_input)

    # Business rule in code ensures requires_human_review stays True!
    assert output.risk_assessment.requires_human_review is True
    assert output.decision == "REQUIERE_REVISION"
    assert any(
        "Excepciones obligatorias presentes" in r
        for r in output.audit_trace.guardrail_reasons
    )


# 3. Test uncertainty: low confidence, high ambiguity, or insufficient info triggers human review
@pytest.mark.parametrize(
    "ambiguity, sufficiency, confidence, expected_reason_keyword",
    [
        (0.75, 0.90, 0.95, "ambigüedad"),
        (0.10, 0.40, 0.95, "insuficiente"),
        (0.10, 0.90, 0.55, "Confianza"),
    ],
)
def test_uncertainty_handling_triggers_human_review(
    ambiguity, sufficiency, confidence, expected_reason_keyword
):
    inp = Stage5EvaluationInput(
        invoice_id=uuid4(),
        exceptions=[],  # No hard exceptions
        context=EvaluationContextInput(),
    )
    adapter = MagicMock(spec=JevExceptionAdapter)
    adapter.evaluate_findings.return_value = JevJudgmentResult(
        category="other",
        category_confidence=confidence,
        category_probabilities={"other": confidence},
        risk_level="low",
        risk_confidence=confidence,
        risk_probabilities={"low": confidence},
        information_sufficiency=sufficiency,
        ambiguity_detected=ambiguity,
        input_tokens=100,
        output_tokens=4,
        latency_ms=50.0,
        model_version="jev-1.14.0",
        raw_state={},
    )
    service = ExceptionEvaluatorService(jev_adapter=adapter)
    output = service.evaluate_exceptions(inp)

    assert output.risk_assessment.requires_human_review is True
    assert output.decision == "REQUIERE_REVISION"
    assert output.audit_trace.uncertainty_detected is True
    assert any(
        expected_reason_keyword.lower() in r.lower()
        for r in output.audit_trace.uncertainty_reasons
    )


# 4. Test fallback mechanism when Jev is unavailable (network, timeout, API down)
def test_fallback_when_jev_is_unavailable(sample_po_exception_input):
    failing_adapter = MagicMock(spec=JevExceptionAdapter)
    failing_adapter.evaluate_findings.side_effect = JevConnectionError(
        "Connection timeout to api.typesafe.ai"
    )

    service = ExceptionEvaluatorService(jev_adapter=failing_adapter)
    output = service.evaluate_exceptions(sample_po_exception_input)

    assert output.schema_version == "0.2.0"
    assert output.risk_assessment.requires_human_review is True
    assert output.decision == "REQUIERE_REVISION"
    assert output.audit_trace.evaluator == "fallback_rules"
    assert any("Fallo de Jev" in r for r in output.audit_trace.guardrail_reasons)


# 5. Test rule-based evaluator handling duplicate invoices and vendor mismatches
def test_rule_based_fallback_critical_cases():
    dup_input = Stage5EvaluationInput(
        invoice_id=uuid4(),
        exceptions=[],
        context=EvaluationContextInput(duplicate_detected=True),
    )
    output = RuleBasedFallbackEvaluator.evaluate(dup_input)
    assert output.risk_assessment.risk_level == "critical"
    assert output.risk_assessment.risk_score == 1.0
    assert output.risk_assessment.exception_category == "duplicate_invoice"
    assert output.decision == "REQUIERE_REVISION"

    vendor_mismatch_input = Stage5EvaluationInput(
        invoice_id=uuid4(),
        exceptions=[],
        context=EvaluationContextInput(supplier_matched=False),
    )
    output_vendor = RuleBasedFallbackEvaluator.evaluate(vendor_mismatch_input)
    assert output_vendor.risk_assessment.risk_level == "critical"
    assert output_vendor.risk_assessment.exception_category == "supplier_mismatch"
    assert output_vendor.decision == "REQUIERE_REVISION"


# 6. Test DB repository: updating invoices and persisting audit events
def test_repository_db_operations():
    cur = MagicMock()
    inv_id = uuid4()
    doc_id = uuid4()

    update_invoice_risk_fields(
        cur,
        invoice_id=inv_id,
        risk_level="high",
        risk_score=0.92,
        risk_confidence=0.95,
    )
    assert cur.execute.called
    update_call = cur.execute.call_args[0]
    assert "UPDATE invoices" in update_call[0]
    assert update_call[1][0] == "high"
    assert update_call[1][1] == Decimal("0.92")
    assert update_call[1][2] == Decimal("0.95")
    assert update_call[1][3] == inv_id

    # Record event
    output = Stage5EvaluationOutput(
        invoice_id=inv_id,
        risk_assessment={
            "risk_level": "high",
            "risk_score": 0.92,
            "exception_category": "amount_mismatch",
            "requires_human_review": True,
            "confidence": 0.95,
        },
        decision="REQUIERE_REVISION",
    )
    cur.reset_mock()
    record_exception_evaluation_event(
        cur,
        invoice_id=inv_id,
        document_id=doc_id,
        business_status="EXCEPCION",
        output=output,
    )
    assert cur.execute.called
    event_call = cur.execute.call_args[0]
    assert "INSERT INTO invoice_events" in event_call[0]
    assert event_call[1][0] == inv_id
    assert event_call[1][1] == doc_id
    assert event_call[1][2] == "EXCEPTION_EVALUATED"


# 7. Test DB adapter flow evaluate_invoice_from_db
def test_evaluate_invoice_from_db_mocked(mock_jev_adapter):
    cur = MagicMock()
    inv_id = uuid4()
    doc_id = uuid4()

    # SELECT invoice and document row
    cur.fetchone.side_effect = [
        (
            doc_id,
            "EXCEPCION",
            uuid4(),
            uuid4(),
            uuid4(),
            Decimal("0.98"),
            None,
            None,
        ),  # invoice row
        None,  # get_current_validation_from_db
        (uuid4(),),  # contract dates
        (1,),  # support documents count
    ]
    cur.fetchall.return_value = [
        (
            "DIFERENCIA_VS_OC",
            "high",
            "Diferencia de monto",
            Decimal(2000000),
            Decimal("4.0"),
        ),
    ]

    service = ExceptionEvaluatorService(jev_adapter=mock_jev_adapter)
    out = evaluate_invoice_from_db(cur, inv_id, service)

    assert out.invoice_id == inv_id
    assert out.risk_assessment.risk_level == "high"
    assert out.decision == "REQUIERE_REVISION"


# 8. Test FastAPI endpoint via TestClient
def test_fastapi_evaluate_exceptions_endpoint(
    mock_jev_adapter, sample_po_exception_input
):
    from fastapi.testclient import TestClient

    from app.main import app

    with patch(
        "app.api.routers.invoices.JevExceptionAdapter", return_value=mock_jev_adapter
    ):
        client = TestClient(app)
        payload = sample_po_exception_input.model_dump(mode="json")
        response = client.post("/api/invoices/evaluate-exceptions", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["schema_version"] == "0.2.0"
        assert data["invoice_id"] == str(sample_po_exception_input.invoice_id)
        assert data["risk_assessment"]["risk_level"] == "high"
        assert data["risk_assessment"]["exception_category"] == "amount_mismatch"
        assert data["risk_assessment"]["requires_human_review"] is True
        assert data["decision"] == "REQUIERE_REVISION"
