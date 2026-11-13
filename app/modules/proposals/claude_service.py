"""Claude API service for generating structured exception explanations asynchronously."""

import asyncio
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
    """Invokes Anthropic Claude API using Structured Tool Outputs asynchronously.

    Ensures non-blocking I/O so the FastAPI event loop can serve other concurrent
    requests while awaiting Claude's response.
    """

    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-6"):
        self.api_key = api_key
        self.model = model

    async def explain_exceptions(
        self, input_data: Stage6ProposalInput
    ) -> tuple[Explanation, ProposalMetadata]:
        """Generate structured explanation asynchronously using Claude API without blocking the thread."""
        if not self.api_key:
            logger.warning(
                "ANTHROPIC_API_KEY no configurada. Usando generador contextual determinista de contingencia."
            )
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
            passed_checks=[c.model_dump() for c in input_data.passed_checks],
            risk_assessment=input_data.risk_assessment.model_dump(),
            document_references=doc_refs,
        )

        start_time = time.perf_counter()
        try:
            # Fully non-blocking async HTTP request via AsyncAnthropic
            async with anthropic.AsyncAnthropic(api_key=self.api_key) as client:
                message = await client.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    system=EXPLANATION_SYSTEM_PROMPT,
                    tools=[EXPLANATION_TOOL_SCHEMA],
                    tool_choice={"type": "tool", "name": "generate_accounting_explanation"},
                    messages=[{"role": "user", "content": user_prompt}],
                )
        except Exception as exc:
            logger.warning("Error invocando Claude API de forma asíncrona: %s", exc)
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

    def explain_exceptions_sync(
        self, input_data: Stage6ProposalInput
    ) -> tuple[Explanation, ProposalMetadata]:
        """Synchronous wrapper for contexts where an event loop is not running."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(self.explain_exceptions(input_data))).result()
        return asyncio.run(self.explain_exceptions(input_data))

    def _fallback_explanation(
        self, input_data: Stage6ProposalInput, reason: str
    ) -> tuple[Explanation, ProposalMetadata]:
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
            issue_count = len(exceptions)
            details = (
                f"Se {'detectó' if issue_count == 1 else 'detectaron'} {issue_count} "
                f"{'inconsistencia técnica' if issue_count == 1 else 'inconsistencias técnicas'}. "
                f"El total facturado es de ${input_data.amounts.total} COP. "
            )
            expected_value = str(first_exc.expected or "").strip()
            if expected_value and expected_value.upper() != "N/A":
                details += f"El valor esperado registrado para la primera excepción es ${expected_value} COP. "
            else:
                details += "No se reportó un valor esperado para la primera excepción. "
            details += f"El nivel de riesgo es {input_data.risk_assessment.risk_level.lower()}."
            recommendation = (
                "Solicitar al área solicitante los soportes necesarios para revisar las inconsistencias. "
                "Si se confirma una diferencia en el valor cobrado, pedir al proveedor la nota crédito "
                "correspondiente antes de continuar con la causación contable."
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
