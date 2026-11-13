from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.validation.repository import (
    ValidationConflictError,
    validate_invoice_from_db,
)
from app.modules.validation.service import downstream_exceptions, evaluate_invoice
from app.schemas.validation import RuleConfig, ValidationInput


def sample():
    return ValidationInput(
        invoice_id=uuid4(),
        data_version=1,
        reference_date=date(2026, 11, 12),
        invoice={
            "supplier": {"nit_base": "900.654.321", "dv": "4"},
            "prefix": "FE",
            "number": "1",
            "issue_date": "2026-09-20",
            "subtotal": "100.00",
            "total": "119.00",
            "retentions_total": "10",
            "net_payable": "109",
            "lines": [
                {
                    "line_no": 1,
                    "quantity": "2",
                    "unit_price": "50",
                    "discount_amount": "0",
                    "amount": "100",
                }
            ],
            "taxes": [{"type": "IVA", "base": "100", "rate": "0.19", "amount": "19"}],
        },
        purchase_order={"subtotal": "100"},
    )


def config(**kwargs):
    return RuleConfig(
        open_periods=[{"start": "2026-09-01", "end": "2026-09-30"}], **kwargs
    )


def checks(data, cfg=None):
    return {c.rule_id: c for c in evaluate_invoice(data, cfg or config()).checks}


def test_all_rules_pass_and_money_serializes_as_strings():
    data = sample()
    # Tutor algorithm: reverse weighted digits modulo 11.
    weights = [3, 7, 13, 17, 19, 23, 29, 37, 41, 43, 47, 53, 59, 67, 71]
    rem = sum(int(d) * weights[i] for i, d in enumerate(reversed("900654321"))) % 11
    data.invoice.supplier.dv = str(rem if rem in (0, 1) else 11 - rem)
    result = evaluate_invoice(data, config())
    assert all(c.result == "passed" for c in result.checks)
    assert not result.requires_human_review
    assert result.model_dump(mode="json")["checks"][0]["expected_amount"] == "100.00"


@pytest.mark.parametrize(
    "rule,field,value",
    [
        ("V1", "line", "103"),
        ("V2", "subtotal", "103"),
        ("V3", "tax", "22"),
        ("V4", "total", "122"),
        ("D2", "po", "99"),
    ],
)
def test_amount_errors(rule, field, value):
    data = sample()
    if field == "line":
        data.invoice.lines[0].amount = Decimal(value)
    elif field == "tax":
        data.invoice.taxes[0].amount = Decimal(value)
    elif field == "po":
        data.purchase_order.subtotal = Decimal(value)
    else:
        setattr(data.invoice, field, Decimal(value))
    assert checks(data)[rule].result == "failed"


def test_internally_consistent_invoice_differs_from_po():
    data = sample()
    data.purchase_order.subtotal = Decimal(96)
    result = checks(data)
    assert all(result[r].result == "passed" for r in ("V1", "V2", "V3", "V4"))
    assert result["D2"].difference == Decimal("4.00")
    assert result["D2"].difference_pct == Decimal("4.1667")
    adapted = downstream_exceptions(evaluate_invoice(data, config()))
    assert any(e["difference_percentage"] == "4.1667" for e in adapted)


@pytest.mark.parametrize("amount,expected", [("120", "passed"), ("120.01", "failed")])
def test_tolerance_boundary(amount, expected):
    data = sample()
    data.invoice.total = Decimal(amount)
    assert checks(data)["V4"].result == expected


def test_null_discount_is_inferred_only_when_line_adds_up_without_it():
    data = sample()
    data.invoice.lines[0].discount_amount = None
    check = checks(data)["V1"]
    assert check.result == "passed"
    assert check.reason_code == "NO_DISCOUNT_INFERRED"


def test_null_discount_with_mismatch_stays_uncertain():
    data = sample()
    data.invoice.lines[0].discount_amount = None
    data.invoice.lines[0].amount = Decimal(90)
    check = checks(data)["V1"]
    assert check.result == "not_evaluated"
    assert check.reason_code == "UNCERTAIN_DISCOUNT"
    assert check.invoice_line_no == 1


def test_known_no_discount_lines_still_forces_zero_discount():
    data = sample()
    data.invoice.lines[0].discount_amount = None
    data.invoice.lines[0].amount = Decimal(90)
    data.known_no_discount_lines = [1]
    assert checks(data)["V1"].result == "failed"


def test_multiple_lines_and_missing_amount():
    data = sample()
    data.invoice.lines.append(data.invoice.lines[0].model_copy(update={"line_no": 2}))
    data.invoice.subtotal = Decimal(200)
    assert checks(data)["V2"].result == "passed"
    assert (
        len([c for c in evaluate_invoice(data, config()).checks if c.rule_id == "V1"])
        == 2
    )
    data.invoice.lines[1].amount = None
    assert checks(data)["V2"].result == "not_evaluated"


def test_disabled_rules_and_config_change():
    data = sample()
    data.invoice.total = Decimal(121)
    assert checks(data)["V4"].result == "failed"
    assert checks(data, config(arithmetic_tolerance="2"))["V4"].result == "passed"
    disabled = config(enabled={"V4": False})
    assert checks(data, disabled)["V4"].reason_code == "DISABLED"


def test_dv_cases():
    data = sample()
    data.invoice.supplier.dv = "9"
    assert checks(data)["V5"].result == "failed"
    data.invoice.supplier.dv = None
    assert checks(data)["V5"].reason_code == "MISSING_DV"
    data.invoice.supplier.nit_base = "abc"
    assert checks(data)["V5"].reason_code == "INVALID_NIT"


def test_duplicate_reupload_and_self_reprocessing():
    data = sample()
    data.duplicate_invoice_ids = [data.invoice_id]
    assert checks(data)["V6"].result == "passed"
    data.duplicate_invoice_ids.append(uuid4())
    assert checks(data)["V6"].reason_code == "DUPLICATE_IDENTITY"
    data.same_document_invoice_ids = [uuid4()]
    assert checks(data)["V6"].reason_code == "DOCUMENT_REUPLOAD"


def test_date_future_closed_missing_periods():
    data = sample()
    data.invoice.issue_date = date(2026, 11, 13)
    assert checks(data)["V7"].reason_code == "FUTURE_DATE"
    data.invoice.issue_date = date(2026, 10, 1)
    assert checks(data)["V7"].reason_code == "CLOSED_PERIOD"
    assert checks(data, RuleConfig())["V7"].reason_code == "MISSING_PERIODS"


def test_missing_po_and_zero_percentage_denominator():
    data = sample()
    data.purchase_order = None
    assert checks(data)["D2"].result == "not_evaluated"
    data = sample()
    data.purchase_order.subtotal = Decimal(0)
    assert checks(data)["D2"].difference_pct is None


def test_float_input_is_rejected():
    payload = sample().model_dump(mode="json")
    payload["invoice"]["total"] = 119.0
    with pytest.raises(ValidationError):
        ValidationInput.model_validate(payload)


def test_rounding_explicit():
    data = sample()
    data.invoice.lines[0].quantity = Decimal(1)
    data.invoice.lines[0].unit_price = Decimal("1.005")
    data.invoice.lines[0].amount = Decimal("1.01")
    assert checks(data, config(arithmetic_tolerance="0"))["V1"].result == "passed"
    assert (
        checks(data, config(arithmetic_tolerance="0", rounding="ROUND_HALF_EVEN"))[
            "V1"
        ].result
        == "failed"
    )


def test_repository_revalidation_appends_results_without_state_transition():
    data = sample()
    cur = MagicMock()
    cur.fetchone.return_value = (
        uuid4(),
        "EXCEPCION",
        1,
        {"data": data.invoice.model_dump(mode="json")},
        None,
        Decimal("0.70"),
        None,
        "sha",
        dict(
            data.invoice.model_dump(mode="json"),
            supplier_nit_base="900654321",
            supplier_dv="4",
            supplier_name="Proveedor",
        ),
    )
    cur.fetchall.return_value = []
    first = validate_invoice_from_db(
        cur, data.invoice_id, config(), data.reference_date
    )
    second = validate_invoice_from_db(
        cur, data.invoice_id, config(), data.reference_date
    )
    assert first.validation_run_id != second.validation_run_id
    sql = "\n".join(c.args[0] for c in cur.execute.call_args_list)
    assert "INSERT INTO rule_checks" in sql
    assert "INSERT INTO exceptions" in sql
    assert "INSERT INTO invoice_events" in sql
    assert "DELETE" not in sql
    assert "UPDATE invoices" not in sql
    assert "LOW_EXTRACTION_CONFIDENCE" in first.blocking_reasons


def test_repository_rejects_terminal_state():
    cur = MagicMock()
    cur.fetchone.return_value = (
        uuid4(),
        "APROBADA",
        1,
        {},
        None,
        None,
        None,
        "sha",
        {},
    )
    with pytest.raises(ValidationConflictError):
        validate_invoice_from_db(cur, uuid4(), config(), date(2026, 11, 12))


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "10000000000000000"])
def test_nonfinite_or_out_of_range_money_rejected(invalid):
    payload = sample().model_dump(mode="json")
    payload["invoice"]["total"] = invalid
    with pytest.raises(ValidationError):
        ValidationInput.model_validate(payload)


def test_endpoint_integration_with_injected_service(monkeypatch):
    from importlib import import_module

    from fastapi.testclient import TestClient

    from app.config import Settings

    create_app = import_module("app.main").create_app
    invoices = import_module("app.api.routers.invoices")

    conn = MagicMock()

    def db():
        yield conn

    def validate(cur, invoice_id, cfg, reference_date):
        data = sample()
        data.invoice_id = invoice_id
        data.reference_date = reference_date
        return evaluate_invoice(data, cfg)

    monkeypatch.setattr(invoices, "validate_invoice_from_db", validate)
    client = TestClient(
        create_app(settings=Settings(financial_validation=config()), db_dependency=db)
    )
    response = client.post(
        f"/api/invoices/{uuid4()}/validation?reference_date=2026-11-12"
    )
    assert response.status_code == 200
    assert response.json()["schema_version"] == "0.2.0"
    assert isinstance(response.json()["checks"][0]["actual_amount"], str)
    assert conn.transaction.called


def test_repository_reads_corrected_values_and_uses_shared_transition(monkeypatch):
    from app.modules.validation import repository

    data = sample()
    data.invoice.supplier.dv = "1"
    # Derive expected DV with the tutor's algorithm.
    data.invoice.supplier.dv = checks(data)["V5"].expected_text or "1"
    current = dict(
        data.invoice.model_dump(mode="json"),
        supplier_nit_base="900654321",
        supplier_dv=data.invoice.supplier.dv,
        supplier_name="Proveedor",
    )
    current["total"] = "120.00"  # Changed since extraction; within 1 COP tolerance.
    cur = MagicMock()
    cur.fetchone.return_value = (
        uuid4(),
        "EXTRAIDA",
        2,
        {"data": data.invoice.model_dump(mode="json")},
        None,
        Decimal("0.99"),
        None,
        "sha",
        current,
    )
    cur.fetchall.side_effect = [
        [(1, "Servicio", Decimal(2), Decimal(50), Decimal(0), Decimal(100))],
        [("IVA", Decimal("0.19"), Decimal(100), Decimal(19))],
        [],
        [],
        [],
        [],
    ]
    # No OC -> requires review; check the transition is delegated, not a direct UPDATE.
    transition = MagicMock()
    monkeypatch.setattr(repository, "transition_invoice_status", transition)
    output = validate_invoice_from_db(
        cur, data.invoice_id, config(), data.reference_date
    )
    assert next(c for c in output.checks if c.rule_id == "V4").actual_amount == Decimal(
        "120.00"
    )
    assert output.data_version == 2
    transition.assert_called_once()
    assert transition.call_args.kwargs["to_status"] == "EXCEPCION"


def test_settings_configuration_from_environment(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv(
        "FINANCIAL_VALIDATION", '{"enabled":{"D2":false},"arithmetic_tolerance":"2.00"}'
    )
    cfg = Settings().financial_validation
    assert cfg.enabled["D2"] is False
    assert cfg.arithmetic_tolerance == Decimal("2.00")


def test_latest_run_filters_current_version():
    from app.modules.validation.repository import get_current_validation_from_db

    result = evaluate_invoice(sample(), config())
    cur = MagicMock()
    cur.fetchone.return_value = (result.model_dump(mode="json"),)
    assert (
        get_current_validation_from_db(cur, result.invoice_id).validation_run_id
        == result.validation_run_id
    )
    sql = cur.execute.call_args.args[0]
    assert "i.data_version" in sql
    assert "run_sequence" in sql
    cur.fetchone.return_value = None
    assert get_current_validation_from_db(cur, result.invoice_id) is None


@pytest.mark.parametrize(
    "error,expected", [("missing", 404), ("conflict", 409), ("sql", 503)]
)
def test_endpoint_errors_and_rollback(error, expected, monkeypatch):
    from importlib import import_module

    import psycopg
    from fastapi.testclient import TestClient

    from app.config import Settings

    main = import_module("app.main")
    router = import_module("app.api.routers.invoices")
    from app.modules.validation.repository import InvoiceNotFoundError

    conn = MagicMock()
    conn.transaction.return_value.__exit__.return_value = False

    def db():
        yield conn

    errors = {
        "missing": InvoiceNotFoundError("missing"),
        "conflict": ValidationConflictError("conflict"),
        "sql": psycopg.OperationalError("private connection detail"),
    }
    monkeypatch.setattr(
        router, "validate_invoice_from_db", MagicMock(side_effect=errors[error])
    )
    response = TestClient(main.create_app(settings=Settings(), db_dependency=db)).post(
        f"/api/invoices/{uuid4()}/validation"
    )
    assert response.status_code == expected
    assert "private connection detail" not in response.text
    assert conn.transaction.return_value.__exit__.call_args.args[0] is type(
        errors[error]
    )


def test_production_reference_date_rejected(monkeypatch):
    from importlib import import_module

    from fastapi.testclient import TestClient

    from app.config import Settings

    main = import_module("app.main")
    router = import_module("app.api.routers.invoices")
    conn = MagicMock()

    def db():
        yield conn

    execute = MagicMock()
    monkeypatch.setattr(router, "validate_invoice_from_db", execute)
    client = TestClient(
        main.create_app(settings=Settings(environment="production"), db_dependency=db)
    )
    assert (
        client.post(
            f"/api/invoices/{uuid4()}/validation?reference_date=2030-01-01"
        ).status_code
        == 400
    )
    execute.assert_not_called()
