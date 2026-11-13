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
