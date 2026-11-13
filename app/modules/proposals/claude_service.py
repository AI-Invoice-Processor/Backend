"""Claude API service for generating structured exception explanations."""

import logging
import time
from typing import Any

import anthropic

from app.modules.proposals.prompts import (
    EXPLANATION_SYSTEM_PROMPT,
    EXPLANATION_TOOL_SCHEMA,
    build_explanation_user_prompt,
)
from app.schemas.proposals import Explanation, ProposalMetadata, Stage6ProposalInput

logger = logging.getLogger(__name__)


class ClaudeServiceError(Exception):
    """Base error for Claude service."""


class ClaudeProposalExplainer:
    """Invokes Anthropic Claude API using Structured Tool Outputs to explain accounting exceptions."""

    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-6"):
        self.api_key = api_key
        self.model = model

    def explain_exceptions(self, input_data: Stage6ProposalInput) -> tuple[Explanation, ProposalMetadata]:
        """Generate structured explanation using Claude API or graceful fallback if unreachable."""
        if not self.api_key:
            logger.warning("ANTHROPIC_API_KEY no configurada. Usando generador contextual determinista de contingencia.")
            return self._fallback_explanation(input_data, reason="Sin API Key configurada")

        doc_refs = input_data.document_references
        inv_num = doc_refs.get("number") or str(input_data.invoice_id)[:8]
        prefix = doc_refs.get("prefix") or ""
        full_invoice_num = f"{prefix}-{inv_num}".strip("-")

        user_prompt = build_explanation_user_prompt(
            invoice_ref=full_invoice_num,
            supplier_name=input_data.supplier.name,
            amounts=input_data.amounts.model_dump(),
            validation_exceptions=[e.model_dump() for e in input_data.validation_exceptions],
            risk_assessment=input_data.risk_assessment.model_dump(),
            document_references=doc_refs,
        )

        start_time = time.perf_counter()
        try:
            client = anthropic.Anthropic(api_key=self.api_key)
            message = client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=EXPLANATION_SYSTEM_PROMPT,
                tools=[EXPLANATION_TOOL_SCHEMA],
                tool_choice={"type": "tool", "name": "generate_accounting_explanation"},
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as exc:
            logger.warning("Error invocando Claude API para explicación contable: %s", exc)
            return self._fallback_explanation(input_data, reason=f"Fallo en Claude API: {exc}")

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        # Extract structured tool output
        explanation_data: dict[str, Any] | None = None
        for block in message.content:
            if block.type == "tool_use" and block.name == "generate_accounting_explanation":
                explanation_data = block.input
                break

        if not explanation_data:
            logger.warning("Claude no devolvió bloque tool_use esperado. Activando fallback.")
            return self._fallback_explanation(input_data, reason="Claude response missing tool_use block")

        explanation = Explanation(
            summary=explanation_data.get("summary", "Excepción detectada en validación financiera."),
            details=explanation_data.get("details", "Discrepancia entre documentos de respaldo."),
            recommendation=explanation_data.get("recommendation", "Revisar soporte antes de causar."),
        )

        metadata = ProposalMetadata(
            latency_ms=round(elapsed_ms, 2),
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            model=self.model,
            account_validation_status="VALIDATED",
        )

        return explanation, metadata

    def _fallback_explanation(self, input_data: Stage6ProposalInput, reason: str) -> tuple[Explanation, ProposalMetadata]:
        """Contingency explanation when Claude cannot be reached."""
        start_time = time.perf_counter()
        exceptions = input_data.validation_exceptions
        doc_refs = input_data.document_references
        inv_num = doc_refs.get("number") or str(input_data.invoice_id)[:8]
        prefix = doc_refs.get("prefix") or ""
        full_inv = f"{prefix}-{inv_num}".strip("-")

        if exceptions:
            first_exc = exceptions[0]
            summary = (
                f"La factura {full_inv} presenta excepción de tipo {first_exc.type} "
                f"con una diferencia cuantificada de ${first_exc.difference or '0'} COP."
            )
            details = (
                f"Se detectaron {len(exceptions)} inconsistencia(s) técnica(s). "
                f"Valor facturado: ${input_data.amounts.total} COP frente al valor esperado: ${first_exc.expected or 'N/A'} COP. "
                f"Evaluación de riesgo: {input_data.risk_assessment.risk_level.upper()}."
            )
            recommendation = (
                "Solicitar revisión documental al área solicitante o nota crédito al proveedor "
                "antes de proceder con la causación contable."
            )
        else:
            summary = f"La factura {full_inv} requiere revisión humana por alertas de contexto contable."
            details = f"Riesgo reportado: {input_data.risk_assessment.risk_level}. No se cuenta con aprobación directa."
            recommendation = "Validar contratos y soportes asociados manualmente."

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        explanation = Explanation(summary=summary, details=details, recommendation=recommendation)
        metadata = ProposalMetadata(
            latency_ms=round(elapsed_ms, 2),
            input_tokens=0,
            output_tokens=0,
            model=f"contingency-rules ({reason})",
            account_validation_status="PENDING_VALIDATION",
        )
        return explanation, metadata
