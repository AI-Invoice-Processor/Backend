from datetime import date
from unittest.mock import MagicMock
from uuid import uuid4

from app.modules.matching import match_invoice_from_db
from app.schemas.matching import MatchingStatus


def test_matching_persists_complete_supplier_order_contract_and_supports() -> None:
    invoice_id = uuid4()
    document_id = uuid4()
    supplier_id = uuid4()
    purchase_order_id = uuid4()
    contract_id = uuid4()
    support_id = uuid4()

    cur = MagicMock()
    cur.fetchone.side_effect = [
        (
            invoice_id,
            document_id,
            "900654321",
            "Tecnologias Pacifico SAS",
            "OC-2026-9001",
            date(2026, 9, 20),
        ),
        (supplier_id, "900654321", "0", "Tecnologías Pacífico S.A.S."),
        (purchase_order_id, "OC-2026-9001", supplier_id, contract_id),
        (contract_id, "CT-2026-001", supplier_id),
    ]
    cur.fetchall.return_value = [(support_id, "SR-2026-0001", "INFORME_SERVICIO")]

    result = match_invoice_from_db(cur, invoice_id)

    assert result.matching_status == MatchingStatus.COMPLETED
    assert result.supplier.matched is True
    assert result.supplier.supplier_id == str(supplier_id)
    assert result.purchase_order.order_number == "OC-2026-9001"
    assert result.contract.contract_number == "CT-2026-001"
    assert result.support_documents[0].number == "SR-2026-0001"
    assert result.missing_documents == []
    assert result.candidates == []

    executed_sql = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list)
    assert "UPDATE invoices" in executed_sql
    assert "UPDATE documents" in executed_sql
    assert "match_detail = %s::jsonb" in executed_sql


def test_matching_does_not_auto_associate_ambiguous_supplier_name() -> None:
    invoice_id = uuid4()
    document_id = uuid4()
    first_supplier_id = uuid4()
    second_supplier_id = uuid4()

    cur = MagicMock()
    cur.fetchone.side_effect = [
        (
            invoice_id,
            document_id,
            None,
            "Tecnologias Pacifico",
            None,
            date(2026, 9, 20),
        )
    ]
    cur.fetchall.return_value = [
        (first_supplier_id, "900654321", "0", "Tecnologias Pacifico SAS"),
        (second_supplier_id, "901111111", "1", "Tecnologias Pacifico SAS"),
    ]

    result = match_invoice_from_db(cur, invoice_id)

    assert result.matching_status == MatchingStatus.AMBIGUOUS
    assert result.supplier.matched is False
    assert result.supplier.supplier_id is None
    assert {candidate.entity for candidate in result.candidates} == {"supplier"}
    assert len(result.candidates) == 2
    assert "supplier" in result.missing_documents
    assert "purchase_order" in result.missing_documents
    assert "contract" in result.missing_documents


def test_matching_requires_supplier_verification_before_using_order() -> None:
    invoice_id = uuid4()
    document_id = uuid4()
    purchase_order_id = uuid4()
    order_supplier_id = uuid4()

    cur = MagicMock()
    cur.fetchone.side_effect = [
        (
            invoice_id,
            document_id,
            "900000000",
            "Proveedor inexistente",
            "OC-2026-9001",
            date(2026, 9, 20),
        ),
        None,
        (purchase_order_id, "OC-2026-9001", order_supplier_id, None),
    ]
    cur.fetchall.return_value = []

    result = match_invoice_from_db(cur, invoice_id)

    assert result.matching_status == MatchingStatus.AMBIGUOUS
    assert result.purchase_order.matched is False
    assert any(candidate.entity == "purchase_order" for candidate in result.candidates)
