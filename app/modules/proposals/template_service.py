"""Deterministic template generator for clean invoices without exceptions."""

from app.schemas.proposals import Explanation, Stage6ProposalInput


def generate_template_explanation(input_data: Stage6ProposalInput) -> Explanation:
    """Generate high-quality standardized explanation when no exceptions exist."""
    amounts = input_data.amounts
    supplier_name = input_data.supplier.name or "Proveedor registrado"
    doc_refs = input_data.document_references
    inv_num = doc_refs.get("number") or str(input_data.invoice_id)[:8]
    prefix = doc_refs.get("prefix") or ""
    full_invoice_num = f"{prefix}-{inv_num}".strip("-")

    po_num = (
        doc_refs.get("order_number")
        or doc_refs.get("po_reference")
        or "orden de compra asociada"
    )
    contract_num = doc_refs.get("contract_number") or "contrato marco vigente"
    acct = input_data.accounting_context.suggested_account or "5135"
    cc = input_data.accounting_context.cost_center or "CC01"

    summary = (
        f"La factura {full_invoice_num} por valor de ${amounts.total} {amounts.currency} "
        f"ha sido validada satisfactoriamente sin inconsistencias financieras, fiscales ni contractuales."
    )

    details = (
        f"Los valores facturados (Subtotal: ${amounts.subtotal}, Impuestos: ${amounts.tax_total}, Total: ${amounts.total}) "
        f"concilian plenamente con la orden {po_num} y el {contract_num}. "
        f"El proveedor {supplier_name} se encuentra activo en los registros maestros y la documentación soporte está completa."
    )

    recommendation = (
        f"Proceder con la causación contable a la cuenta {acct} y centro de costo {cc}. "
        f"El documento se encuentra listo para aprobación de causación."
    )

    return Explanation(
        summary=summary,
        details=details,
        recommendation=recommendation,
    )


_DATA_GAP_PREFIXES = ("MISSING_", "UNCERTAIN_", "UNSUPPORTED_")


def is_data_gap(exc) -> bool:
    """True when the exception means 'could not evaluate', not a value mismatch."""
    code = (exc.reason_code or exc.type or "").upper()
    return code.startswith(_DATA_GAP_PREFIXES)


def generate_data_gap_explanation(input_data: Stage6ProposalInput) -> Explanation:
    """Deterministic explanation when every exception is a data gap (no LLM)."""
    doc_refs = input_data.document_references
    inv_num = doc_refs.get("number") or str(input_data.invoice_id)[:8]
    prefix = doc_refs.get("prefix") or ""
    full_invoice_num = f"{prefix}-{inv_num}".strip("-")

    items = []
    for exc in input_data.validation_exceptions:
        rule = exc.rule_id or exc.type
        line = f" (línea {exc.invoice_line_no})" if exc.invoice_line_no else ""
        items.append(f"{rule}{line}: {exc.description or 'datos insuficientes'}")

    passed = sorted({c.rule_id for c in input_data.passed_checks})
    passed_text = (
        f" Los controles {', '.join(passed)} sí se evaluaron y pasaron."
        if passed
        else ""
    )

    return Explanation(
        summary=(
            f"La factura {full_invoice_num} no tiene diferencias de valor detectadas, "
            f"pero {len(items)} control(es) no se pudieron evaluar por datos insuficientes."
        ),
        details="; ".join(items) + "." + passed_text,
        recommendation=(
            "Completar o corregir los datos indicados (o la configuración del sistema "
            "si el motivo es de configuración) y reprocesar la factura."
        ),
    )
