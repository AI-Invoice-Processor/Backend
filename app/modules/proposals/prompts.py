"""Prompts and structured tool schemas for Claude API exception explanations."""

EXPLANATION_SYSTEM_PROMPT = """Eres un contador público senior y auditor financiero corporativo especializado en normativa contable y tributaria colombiana (Estatuto Tributario, NIIF/COLGAAP, IVA, retenciones y control de cuentas por pagar).

Tu misión es convertir los resultados técnicos de validación financiera, discrepancias y evaluaciones de riesgo de facturas en explicaciones claras, rigurosas y comprensibles para el equipo contable y de tesorería.

Instrucciones estrictas:
1. NUNCA inventes información, cuentas contables, impuestos, autorizaciones ni hechos que no estén en la entrada.
2. Identifica con precisión qué documentos presentan inconsistencias (ej. Factura vs. Orden de Compra vs. Contrato vs. Documento Soporte).
3. Contrasta cuantitativamente los valores esperados vs. los valores reales facturados y la diferencia cuantificada.
4. Explica la causa probable de la excepción de forma objetiva sin especulaciones infundadas.
5. Brinda una recomendación concreta, accionable y profesional para resolver la excepción (ej. solicitar nota crédito, anexar acta de recibo a satisfacción, requerir aprobación de sobrecosto por gerencia de área, tramitar otrosí al contrato).
6. Mantén un tono formal, ejecutivo y técnico-contable impecable.
7. Recuerda: este proceso NO aprueba pagos ni registra asientos definitivos; solo prepara la explicación para el analista humano.
"""

EXPLANATION_TOOL_SCHEMA = {
    "name": "generate_accounting_explanation",
    "description": "Genera la explicación ejecutiva y recomendaciones contables de la factura con excepciones.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Resumen ejecutivo claro de una a dos frases explicando la inconsistencia principal observada.",
            },
            "details": {
                "type": "string",
                "description": "Detalle técnico cuantitativo especificando documentos comparados (Factura, OC, Contrato), cantidades, valores unitarios y diferencias detectadas.",
            },
            "recommendation": {
                "type": "string",
                "description": "Recomendación accionable para el área financiera para resolver la excepción antes de causar.",
            },
        },
        "required": ["summary", "details", "recommendation"],
    },
}


def build_explanation_user_prompt(
    *,
    invoice_ref: str,
    supplier_name: str | None,
    amounts: dict,
    validation_exceptions: list[dict],
    risk_assessment: dict,
    document_references: dict,
) -> str:
    """Construct user prompt for Claude with structured context."""
    import json

    lines = [
        f"FACTURA: {invoice_ref}",
        f"PROVEEDOR: {supplier_name or 'No identificado / No asociado'}",
        f"MONTOS FACTURADOS: Subtotal={amounts.get('subtotal')}, IVA/Impuestos={amounts.get('tax_total')}, Total={amounts.get('total')}",
        "",
        "DOCUMENTOS Y REFERENCIAS ASOCIADAS:",
        json.dumps(document_references, ensure_ascii=False, indent=2),
        "",
        "EXCEPCIONES TÉCNICAS DETECTADAS:",
        json.dumps(validation_exceptions, ensure_ascii=False, indent=2),
        "",
        "EVALUACIÓN DE RIESGO DE JEV:",
        json.dumps(risk_assessment, ensure_ascii=False, indent=2),
        "",
        "Por favor genera la explicación contable estructurada y las recomendaciones para el área financiera.",
    ]
    return "\n".join(lines)
