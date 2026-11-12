from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import cv2
import numpy as np
import pymupdf
import pytest

from app.modules.extraction import service
from app.modules.extraction.ocr import extract_document_text
from app.modules.extraction.parsers import (
    parse_cop_amount,
    parse_tax_rate,
    split_invoice_number,
    split_nit,
)
from app.modules.extraction.service import (
    _RawConfidence,
    _RawEvidence,
    _RawLine,
    _RawLLMExtraction,
    _RawRetention,
    _RawTax,
    extract_invoice_document,
)
from app.schemas.extraction import ExtractionMethod, ExtractionStatus


# ---------------------------------------------------------------------------
# 1. Tests de parsers deterministas
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$52.000.000", Decimal(52000000)),
        ("1.190.000,50", Decimal("1190000.50")),
        ("43697478.99", Decimal("43697478.99")),
        ("52,000,000.00", Decimal("52000000.00")),
        ("$ 3.926.000", Decimal(3926000)),
        (None, None),
        ("", None),
        ("---", None),
    ],
)
def test_parse_cop_amount(raw: str | None, expected: Decimal | None) -> None:
    assert parse_cop_amount(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("19%", Decimal("0.1900")),
        ("19", Decimal("0.1900")),
        ("0.19", Decimal("0.1900")),
        ("15%", Decimal("0.1500")),
        ("0,7%", Decimal("0.0070")),
        (None, None),
    ],
)
def test_parse_tax_rate(raw: str | None, expected: Decimal | None) -> None:
    assert parse_tax_rate(raw) == expected


def test_split_nit_and_invoice_number() -> None:
    assert split_nit("900.654.321-0") == ("900654321", "0")
    assert split_nit("900654321") == ("900654321", None)
    assert split_nit(None) == (None, None)

    assert split_invoice_number("FE-9001") == ("FE", "9001")
    assert split_invoice_number("fe 1034") == ("FE", "1034")
    assert split_invoice_number("9001") == (None, "9001")


# ---------------------------------------------------------------------------
# 2. Tests de lectura híbrida PyMuPDF / OCR
# ---------------------------------------------------------------------------
def test_extract_digital_pdf_text(tmp_path: Path) -> None:
    pdf_path = tmp_path / "FE-9001.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "FACTURA ELECTRONICA DE VENTA FE-9001\n"
        "NIT: 900.654.321-0 - Tecnologias Pacifico S.A.S.\n"
        "TOTAL FACTURA: $61.880.000",
    )
    doc.save(pdf_path)
    doc.close()

    result = extract_document_text(pdf_path)
    assert result.method == ExtractionMethod.DIGITAL_TEXT
    assert result.ocr_confidence == 0.99
    assert "FE-9001" in result.full_text
    assert "61.880.000" in result.full_text


def test_extract_blank_image_triggers_ocr(tmp_path: Path) -> None:
    img_path = tmp_path / "ilegible.png"
    blank = np.full((200, 400, 3), 255, dtype=np.uint8)
    cv2.imwrite(str(img_path), blank)

    result = extract_document_text(img_path)
    assert result.method == ExtractionMethod.OCR
    assert result.pages_text == [(1, "")]


# ---------------------------------------------------------------------------
# 3. Tests del servicio de extracción con Claude mockeado
# ---------------------------------------------------------------------------
def _build_digital_pdf(path: Path, body: str) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), body)
    doc.save(path)
    doc.close()
    return path


def test_extract_invoice_fe9001_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = _build_digital_pdf(
        tmp_path / "FE-9001.pdf",
        "Factura Electronica FE-9001 - Tecnologias Pacifico S.A.S. NIT 900.654.321-0 Total $61.880.000",
    )

    fake_llm_response = _RawLLMExtraction(
        nit_raw="900.654.321-0",
        supplier_name="Tecnologías Pacífico S.A.S.",
        invoice_number_raw="FE-9001",
        issue_date="2026-09-20",
        currency="COP",
        purchase_order_reference="OC-2026-9001",
        lines=[
            _RawLine(
                line_no=1,
                description="Unidad pactada de servicio especializado - concepto 1",
                quantity_raw="26",
                unit_price_raw="$2.000.000",
                discount_amount_raw=None,
                amount_raw="$52.000.000",
            )
        ],
        taxes=[
            _RawTax(
                type="IVA",
                rate_raw="19%",
                base_raw="$52.000.000",
                amount_raw="$9.880.000",
            )
        ],
        subtotal_raw="$52.000.000",
        total_raw="$61.880.000",
        retentions=[
            _RawRetention(
                type="UNSPECIFIED",
                rate_raw=None,
                base_raw=None,
                amount_raw="$3.926.000",
            )
        ],
        retentions_total_raw=None,
        net_payable_raw="$57.954.000",
        evidence=[
            _RawEvidence(
                field="total",
                value="61880000.00",
                page=1,
                text="TOTAL FACTURA: $61.880.000",
            )
        ],
        field_confidences=[
            _RawConfidence(field="supplier.nit_base", score=0.99),
            _RawConfidence(field="supplier.dv", score=0.95),
            _RawConfidence(field="total", score=0.97),
            _RawConfidence(field="lines[0].quantity", score=0.96),
        ],
    )

    monkeypatch.setattr(
        service, "_call_claude_extractor", lambda _text: fake_llm_response
    )

    doc_id = uuid4()
    inv_id = uuid4()
    cur = MagicMock()
    # Simula consulta FOR UPDATE de transition_invoice_status: ('RECIBIDA', doc_id)
    cur.fetchone.side_effect = [None, ("RECIBIDA", doc_id)]

    output = extract_invoice_document(
        cur=cur,
        document_id=doc_id,
        invoice_id=inv_id,
        file_path=pdf_path,
    )

    assert output.schema_version == "0.2.0"
    assert output.extraction_status == ExtractionStatus.SUCCESS
    assert output.extraction_method == ExtractionMethod.DIGITAL_TEXT
    assert output.data is not None
    assert output.data.supplier.nit_base == "900654321"
    assert output.data.supplier.dv == "0"
    assert output.data.prefix == "FE"
    assert output.data.number == "9001"
    assert output.data.lines[0].quantity == Decimal(26)
    assert output.data.lines[0].discount_amount is None
    assert output.data.subtotal == Decimal(52000000)
    assert output.data.total == Decimal(61880000)
    assert output.data.retentions_total == Decimal(3926000)
    assert output.data.net_payable == Decimal(57954000)
    assert output.errors == []

    # Verifica que la transición final en BD fue hacia EXTRAIDA
    executed_sql = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list)
    assert "INSERT INTO invoice_lines" in executed_sql
    assert "INSERT INTO invoice_taxes" in executed_sql
    assert "INSERT INTO invoice_events" in executed_sql


def test_extract_preserves_arithmetic_error_without_fixing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regla de oro: Etapa 2 NO debe corregir sumas malas (caso FE-1034)."""
    pdf_path = _build_digital_pdf(
        tmp_path / "FE-1034.pdf",
        "Factura Electronica FE-1034 Subtotal $50.000.000 IVA $9.500.000 Total declarado $59.550.000",
    )

    fake_bad_sum = _RawLLMExtraction(
        nit_raw="219.540.831-5",
        supplier_name="Suministros Andinos S.A.S.",
        invoice_number_raw="FE-1034",
        issue_date="2026-06-10",
        subtotal_raw="$50.000.000",
        taxes=[
            _RawTax(
                type="IVA",
                rate_raw="19%",
                base_raw="$50.000.000",
                amount_raw="$9.500.000",
            )
        ],
        total_raw="$59.550.000",
        field_confidences=[_RawConfidence(field="total", score=0.98)],
    )

    monkeypatch.setattr(service, "_call_claude_extractor", lambda _text: fake_bad_sum)

    doc_id = uuid4()
    inv_id = uuid4()
    cur = MagicMock()
    cur.fetchone.side_effect = [None, ("RECIBIDA", doc_id)]

    output = extract_invoice_document(
        cur=cur,
        document_id=doc_id,
        invoice_id=inv_id,
        file_path=pdf_path,
    )

    assert output.data is not None
    assert output.data.subtotal == Decimal(50000000)
    assert output.data.taxes[0].amount == Decimal(9500000)
    # Debe conservar el error de +$50.000 para que Etapa 4 (V4) lo detecte
    assert output.data.total == Decimal(59550000)


def test_extract_unreadable_document_transitions_to_exception(tmp_path: Path) -> None:
    img_path = tmp_path / "corrupto.png"
    blank = np.full((200, 400, 3), 255, dtype=np.uint8)
    cv2.imwrite(str(img_path), blank)

    doc_id = uuid4()
    inv_id = uuid4()
    cur = MagicMock()
    cur.fetchone.return_value = ("RECIBIDA", doc_id)

    output = extract_invoice_document(
        cur=cur,
        document_id=doc_id,
        invoice_id=inv_id,
        file_path=img_path,
    )

    assert output.extraction_status == ExtractionStatus.FAILED
    assert output.data is None
    assert output.errors[0].code == "UNREADABLE_DOCUMENT"
