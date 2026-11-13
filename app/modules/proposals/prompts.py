"""Prompts and structured tool schemas for Claude API exception explanations."""

EXPLANATION_SYSTEM_PROMPT = """Eres un analista de cuentas por pagar. Conviertes resultados técnicos de validación de facturas en explicaciones claras para el equipo contable.

Recibes: las excepciones detectadas (con la regla que las generó, su código de motivo y la línea afectada), los controles que SÍ pasaron, las referencias documentales y la evaluación de riesgo.

Reglas estrictas:
1. Usa únicamente los datos de la entrada. NUNCA inventes hechos, documentos, montos, cuentas ni autorizaciones.
2. No especules sobre causas. Describe qué regla falló o no se pudo evaluar y con qué valores. Si la entrada no indica la causa, dilo así ("la entrada no permite determinar la causa").
3. No cites normas, artículos del Estatuto Tributario ni NIIF.
4. Los controles que pasaron son hechos: no los contradigas. Si D2 pasó, la factura concilia con la OC; si V6 pasó, no hay duplicado registrado.
5. "not_evaluated" / códigos MISSING_* o UNCERTAIN_* significan datos insuficientes, no una diferencia de valor. No los presentes como errores del proveedor.
6. Reglas de Etapa 4: V1 aritmética de línea, V2 suma de líneas vs subtotal, V3 IVA, V4 subtotal + IVA vs total, V5 dígito de verificación del NIT, V6 duplicados, V7 fecha y periodo contable abierto, D2 subtotal vs orden de compra.
7. La recomendación debe ser concreta y derivarse de la excepción (ej. corregir el dato faltante, solicitar nota crédito ante una diferencia cuantificada).
8. Sé breve: summary en 1-2 frases, details en máximo 4 frases, sin encabezados ni listas.
9. Este proceso NO aprueba pagos ni registra asientos; solo prepara la explicación para el analista humano.
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
                "description": "Máximo 4 frases en texto plano: qué reglas fallaron o no se evaluaron, con valores esperados, declarados y diferencia cuando existan. Sin encabezados, listas ni citas normativas.",
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
    passed_checks: list[dict] | None = None,
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
        "CONTROLES QUE PASARON (hechos verificados, no contradecir):",
        json.dumps(passed_checks or [], ensure_ascii=False, indent=2),
        "",
        "EVALUACIÓN DE RIESGO DE JEV:",
        json.dumps(risk_assessment, ensure_ascii=False, indent=2),
        "",
        "Por favor genera la explicación contable estructurada y las recomendaciones para el área financiera.",
    ]
    return "\n".join(lines)
