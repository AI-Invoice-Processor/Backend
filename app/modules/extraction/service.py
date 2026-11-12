import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import anthropic
import psycopg
from pydantic import BaseModel, Field

from app.config import get_settings
from app.modules.extraction.ocr import extract_document_text
from app.modules.extraction.parsers import (
    parse_cop_amount,
    parse_tax_rate,
    split_invoice_number,
    split_nit,
)
from app.schemas.extraction import (
    ExtractedInvoiceData,
    ExtractedInvoiceLine,
    ExtractedRetention,
    ExtractedSupplier,
    ExtractedTax,
    ExtractionConfidence,
    ExtractionErrorItem,
    ExtractionEvidence,
    ExtractionStatus,
    InvoiceExtractionOutput,
)
from app.state_machine import InvoiceStatus, transition_invoice_status


# Esquema intermedio con strings crudos para que el LLM no haga aritmética ni float
class _RawLine(BaseModel):
    line_no: int
    description: str | None = None
    quantity_raw: str | None = None
    unit_price_raw: str | None = None
    discount_amount_raw: str | None = None
    amount_raw: str | None = None


class _RawTax(BaseModel):
    type: str
    rate_raw: str | None = None
    base_raw: str | None = None
    amount_raw: str | None = None


class _RawRetention(BaseModel):
    type: str = "UNSPECIFIED"
    rate_raw: str | None = None
    base_raw: str | None = None
    amount_raw: str


class _RawEvidence(BaseModel):
    field: str
    value: str | None = None
    page: int = 1
    text: str


class _RawConfidence(BaseModel):
    field: str
    score: float


class _RawLLMExtraction(BaseModel):
    nit_raw: str | None = None
    supplier_name: str | None = None
    invoice_number_raw: str | None = None
    issue_date: str | None = None
    currency: str = "COP"
    purchase_order_reference: str | None = None
    lines: list[_RawLine] = Field(default_factory=list)
    taxes: list[_RawTax] = Field(default_factory=list)
    subtotal_raw: str | None = None
    total_raw: str | None = None
    retentions: list[_RawRetention] = Field(default_factory=list)
    retentions_total_raw: str | None = None
    net_payable_raw: str | None = None
    evidence: list[_RawEvidence] = Field(default_factory=list)
    field_confidences: list[_RawConfidence] = Field(default_factory=list)


_EXTRACTION_TOOL_NAME = "registrar_factura"

SYSTEM_PROMPT = """Eres un extractor contable de facturas electrónicas colombianas.
Reglas estrictas:
1. Extrae ÚNICAMENTE los valores declarados literalmente en el texto.
2. NUNCA recalcules ni corrijas sumas, subtotales, IVA o totales aunque estén mal sumados.
3. Si un dato no aparece en el documento, devuelve null. Nunca inventes dígitos de verificación ni números de orden de compra.
4. Conserva los montos en *_raw tal como aparecen en el documento (ej. "$52.000.000").
5. Usa fechas ISO YYYY-MM-DD en issue_date.
6. En field_confidences usa estas claves exactas: supplier.nit_base, supplier.dv, prefix, number, issue_date, purchase_order_reference, subtotal, total, y lines[i].quantity / lines[i].unit_price / lines[i].amount.
7. En evidence incluye fragmentos textuales exactos del documento que respaldan cada campo clave."""


def _parse_iso_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def _call_claude_extractor(document_text: str) -> _RawLLMExtraction:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY no está configurada en el entorno.")

    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        max_retries=1,
        timeout=60.0,
    )
    # Tool use forzado en lugar de output_format: con este esquema la salida
    # estructurada tardaba >2.5 min; con tool use responde en ~12 s.
    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        tools=[
            {
                "name": _EXTRACTION_TOOL_NAME,
                "description": "Registra los campos extraídos de la factura.",
                "input_schema": _RawLLMExtraction.model_json_schema(),
            }
        ],
        tool_choice={"type": "tool", "name": _EXTRACTION_TOOL_NAME},
        messages=[
            {
                "role": "user",
                "content": (
                    f"Extrae los campos de la siguiente factura:\n\n{document_text}"
                ),
            }
        ],
    )
    tool_block = next(
        (block for block in response.content if block.type == "tool_use"), None
    )
    if tool_block is None:
        raise RuntimeError(
            f"Claude no devolvió la extracción (stop_reason={response.stop_reason})."
        )
    return _RawLLMExtraction.model_validate(tool_block.input)


def extract_invoice_document(
    *,
    cur: psycopg.Cursor,
    document_id: UUID,
    invoice_id: UUID,
    file_path: Path,
) -> InvoiceExtractionOutput:
    """Ejecuta la Etapa 2 completa, guarda en PostgreSQL y actualiza el estado."""
    now = datetime.now(UTC)

    # 1. Lectura híbrida (PyMuPDF o OpenCV + Tesseract)
    doc_text = extract_document_text(file_path)
    clean_chars = sum(len(text.strip()) for _, text in doc_text.pages_text)

    if clean_chars < 30:
        output = InvoiceExtractionOutput(
            document_id=document_id,
            extraction_status=ExtractionStatus.FAILED,
            extraction_method=doc_text.method,
            data=None,
            evidence=[],
            confidence=ExtractionConfidence(overall=doc_text.ocr_confidence, fields={}),
            errors=[
                ExtractionErrorItem(
                    code="UNREADABLE_DOCUMENT",
                    field=None,
                    message="El documento no contiene texto legible suficiente.",
                )
            ],
            extracted_at=now,
        )
        cur.execute(
            """
            UPDATE documents
               SET processing_stage = 'extraction',
                   processing_status = 'failed',
                   error = %s::jsonb
             WHERE id = %s
            """,
            (json.dumps([e.model_dump() for e in output.errors]), document_id),
        )
        transition_invoice_status(
            cur,
            invoice_id=invoice_id,
            to_status=InvoiceStatus.EXCEPCION,
            event_type="EXTRACTION_FAILED",
            actor_type="ai",
            actor_name="tesseract-ocr",
            reason="Documento ilegible o vacío durante OCR.",
        )
        return output

    # 2. Extracción estructurada con Claude
    raw = _call_claude_extractor(doc_text.full_text)

    # 3. Normalización determinista en Python
    nit_base, dv = split_nit(raw.nit_raw)
    prefix, number = split_invoice_number(raw.invoice_number_raw)
    issue_date = _parse_iso_date(raw.issue_date)
    subtotal = parse_cop_amount(raw.subtotal_raw)
    total = parse_cop_amount(raw.total_raw)
    net_payable = parse_cop_amount(raw.net_payable_raw)

    lines = [
        ExtractedInvoiceLine(
            line_no=idx,
            description=line.description,
            quantity=parse_cop_amount(line.quantity_raw),
            unit_price=parse_cop_amount(line.unit_price_raw),
            discount_amount=parse_cop_amount(line.discount_amount_raw),
            amount=parse_cop_amount(line.amount_raw),
        )
        for idx, line in enumerate(raw.lines, start=1)
    ]

    taxes = [
        ExtractedTax(
            type=tax.type.upper(),
            rate=parse_tax_rate(tax.rate_raw),
            base=parse_cop_amount(tax.base_raw),
            amount=parse_cop_amount(tax.amount_raw),
        )
        for tax in raw.taxes
    ]

    retentions: list[ExtractedRetention] = []
    for ret in raw.retentions:
        parsed_amount = parse_cop_amount(ret.amount_raw)
        if parsed_amount is not None:
            retentions.append(
                ExtractedRetention(
                    type=ret.type.upper(),
                    rate=parse_tax_rate(ret.rate_raw),
                    base=parse_cop_amount(ret.base_raw),
                    amount=parsed_amount,
                )
            )

    retentions_total = parse_cop_amount(raw.retentions_total_raw)
    if retentions_total is None and retentions:
        retentions_total = sum((r.amount for r in retentions), Decimal("0.00"))

    extracted_data = ExtractedInvoiceData(
        supplier=ExtractedSupplier(
            nit_raw=raw.nit_raw,
            nit_base=nit_base,
            dv=dv,
            name=raw.supplier_name,
        ),
        invoice_number_raw=raw.invoice_number_raw,
        prefix=prefix,
        number=number,
        issue_date=issue_date,
        currency=(raw.currency or "COP").upper()[:3],
        purchase_order_reference=raw.purchase_order_reference,
        lines=lines,
        taxes=taxes,
        subtotal=subtotal,
        total=total,
        retentions=retentions,
        retentions_total=retentions_total,
        net_payable=net_payable,
    )

    # Calcular confianza global combinando OCR + confianza reportada por campo
    field_scores = [max(0.0, min(1.0, float(c.score))) for c in raw.field_confidences]
    llm_conf = sum(field_scores) / len(field_scores) if field_scores else 0.90
    overall_conf = round(min(doc_text.ocr_confidence, llm_conf), 4)

    errors: list[ExtractionErrorItem] = []
    if not nit_base:
        errors.append(
            ExtractionErrorItem(
                code="MISSING_NIT",
                field="supplier.nit_base",
                message="No se pudo extraer el NIT del emisor.",
            )
        )
    if total is None:
        errors.append(
            ExtractionErrorItem(
                code="MISSING_TOTAL",
                field="total",
                message="No se pudo extraer el valor total de la factura.",
            )
        )

    status = (
        ExtractionStatus.PARTIAL
        if errors or overall_conf < 0.85
        else ExtractionStatus.SUCCESS
    )

    output = InvoiceExtractionOutput(
        document_id=document_id,
        extraction_status=status,
        extraction_method=doc_text.method,
        data=extracted_data,
        evidence=[
            ExtractionEvidence(field=e.field, value=e.value, page=e.page, text=e.text)
            for e in raw.evidence
        ],
        confidence=ExtractionConfidence(
            overall=overall_conf,
            fields={
                c.field: round(max(0.0, min(1.0, float(c.score))), 4)
                for c in raw.field_confidences
            },
        ),
        errors=errors,
        extracted_at=now,
    )

    # 4. Persistencia en PostgreSQL
    extraction_payload = output.model_dump(mode="json")

    # Verificar duplicidad por (supplier_nit_base, prefix, number) antes de actualizar
    duplicate_of_id: UUID | None = None
    if nit_base and prefix and number:
        cur.execute(
            """
            SELECT id
              FROM invoices
             WHERE supplier_nit_base = %s
               AND prefix = %s
               AND number = %s
               AND id <> %s
             LIMIT 1
            """,
            (nit_base, prefix, number, invoice_id),
        )
        dup_row = cur.fetchone()
        if dup_row:
            duplicate_of_id = dup_row[0]

    if duplicate_of_id is not None:
        cur.execute(
            """
            UPDATE invoices
               SET supplier_nit_base = %s,
                   supplier_dv = %s,
                   supplier_name = %s,
                   issue_date = %s,
                   currency = %s,
                   subtotal = %s,
                   total = %s,
                   retentions_total = %s,
                   net_payable = %s,
                   po_reference = %s,
                   extraction_method = %s,
                   extraction_confidence = %s,
                   extraction_json = %s::jsonb,
                   updated_at = now()
             WHERE id = %s
            """,
            (
                nit_base,
                dv,
                raw.supplier_name,
                issue_date,
                extracted_data.currency,
                subtotal,
                total,
                retentions_total,
                net_payable,
                raw.purchase_order_reference,
                doc_text.method.value,
                Decimal(str(overall_conf)),
                json.dumps(extraction_payload),
                invoice_id,
            ),
        )
        cur.execute(
            """
            UPDATE documents
               SET processing_stage = 'extraction',
                   processing_status = 'success',
                   duplicate_of_invoice_id = %s
             WHERE id = %s
            """,
            (duplicate_of_id, document_id),
        )
        transition_invoice_status(
            cur,
            invoice_id=invoice_id,
            to_status=InvoiceStatus.EXCEPCION,
            event_type="DUPLICATE_INVOICE",
            actor_type="system",
            actor_name="db-unique-guard",
            reason=f"Factura duplicada de {prefix}-{number} (NIT {nit_base}).",
            payload={"duplicate_of_invoice_id": str(duplicate_of_id)},
        )
        return output

    cur.execute(
        """
        UPDATE invoices
           SET supplier_nit_base = %s,
               supplier_dv = %s,
               supplier_name = %s,
               prefix = %s,
               number = %s,
               issue_date = %s,
               currency = %s,
               subtotal = %s,
               total = %s,
               retentions_total = %s,
               net_payable = %s,
               po_reference = %s,
               extraction_method = %s,
               extraction_confidence = %s,
               extraction_json = %s::jsonb,
               updated_at = now()
         WHERE id = %s
        """,
        (
            nit_base,
            dv,
            raw.supplier_name,
            prefix,
            number,
            issue_date,
            extracted_data.currency,
            subtotal,
            total,
            retentions_total,
            net_payable,
            raw.purchase_order_reference,
            doc_text.method.value,
            Decimal(str(overall_conf)),
            json.dumps(extraction_payload),
            invoice_id,
        ),
    )

    cur.execute("DELETE FROM invoice_lines WHERE invoice_id = %s", (invoice_id,))
    for line in lines:
        cur.execute(
            """
            INSERT INTO invoice_lines (
                invoice_id, line_no, description, quantity,
                unit_price, discount_amount, amount
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                invoice_id,
                line.line_no,
                line.description,
                line.quantity,
                line.unit_price,
                line.discount_amount,
                line.amount,
            ),
        )

    cur.execute("DELETE FROM invoice_taxes WHERE invoice_id = %s", (invoice_id,))
    for tax in taxes:
        cur.execute(
            """
            INSERT INTO invoice_taxes (
                invoice_id, tax_type, rate, base, amount
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (invoice_id, tax.type, tax.rate, tax.base, tax.amount),
        )

    cur.execute(
        """
        UPDATE documents
           SET processing_stage = 'extraction',
               processing_status = %s
         WHERE id = %s
        """,
        (status.value, document_id),
    )

    # 5. Transición de estado de negocio
    if overall_conf < 0.85 or total is None:
        transition_invoice_status(
            cur,
            invoice_id=invoice_id,
            to_status=InvoiceStatus.EXCEPCION,
            event_type="BAJA_CONFIANZA_OCR",
            actor_type="ai",
            actor_name="claude-extractor",
            reason=(
                f"Confianza de extracción baja ({overall_conf:.2f}) "
                "o campos críticos faltantes."
            ),
            payload={"overall_confidence": overall_conf},
        )
    else:
        transition_invoice_status(
            cur,
            invoice_id=invoice_id,
            to_status=InvoiceStatus.EXTRAIDA,
            event_type="EXTRACTION_COMPLETED",
            actor_type="ai",
            actor_name="claude-extractor",
            payload={"method": doc_text.method.value, "confidence": overall_conf},
        )

    return output
