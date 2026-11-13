"""Independent adapter for TypeSafe Jev System One model.

This module acts as an isolated boundary between Jev TypeSafe AI and the application.
It strictly receives structured python findings and context (never raw document bytes/images)
and interacts with the official typesafe-sdk TypeSafeClient.
"""

import asyncio
import logging
import time
from typing import Any

from typesafe_sdk import (
    AsyncTypeSafeClient,
    ChoiceAnswer,
    NoulAnswer,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeError,
    TypeSafeRateLimitError,
)

from app.modules.exceptions.questions import build_stage5_questions

logger = logging.getLogger(__name__)


class JevAdapterError(Exception):
    """Base error raised when Jev evaluation fails."""


class JevConnectionError(JevAdapterError):
    """Raised on connection timeout or network unreachability."""


class JevAuthenticationError(JevAdapterError):
    """Raised on invalid or missing API key."""


class JevJudgmentResult:
    """Normalized structured judgments returned by the Jev adapter."""

    def __init__(
        self,
        *,
        category: str,
        category_confidence: float,
        category_probabilities: dict[str, float],
        risk_level: str,
        risk_confidence: float,
        risk_probabilities: dict[str, float],
        information_sufficiency: float,
        ambiguity_detected: float,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        model_version: str,
        raw_state: dict[str, Any],
    ):
        self.category = category
        self.category_confidence = category_confidence
        self.category_probabilities = category_probabilities
        self.risk_level = risk_level
        self.risk_confidence = risk_confidence
        self.risk_probabilities = risk_probabilities
        self.information_sufficiency = information_sufficiency
        self.ambiguity_detected = ambiguity_detected
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.model_version = model_version
        self.raw_state = raw_state


class JevExceptionAdapter:
    """Adapter encapsulating all interactions with TypeSafe AI Jev."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key
        self.model = model or "jev-latest"

    async def evaluate_findings(
        self,
        *,
        findings: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> JevJudgmentResult:
        """Send computed findings and validation context to Jev in a single bundled request asynchronously.

        Never sends raw documents. Uses non-blocking I/O.
        """
        if not self.api_key:
            raise JevAuthenticationError("TYPESAFE_API_KEY no está configurada")

        # State formatted following the human test: descriptive keys, clean context
        state: dict[str, Any] = {
            "findings": findings,
            "context": context,
        }

        questions = build_stage5_questions()

        start_time = time.perf_counter()
        try:
            async with AsyncTypeSafeClient(
                api_key=self.api_key, model=self.model
            ) as client:
                response = await client.system_one(
                    state=state,
                    questions=questions,
                )
        except (TypeSafeAPITimeoutError, TypeSafeAPIConnectionError) as exc:
            logger.warning("Error de conectividad/timeout con Jev API: %s", exc)
            raise JevConnectionError(f"No se pudo conectar con Jev: {exc}") from exc
        except TypeSafeAuthenticationError as exc:
            logger.error("Error de autenticación con Jev API: %s", exc)
            raise JevAuthenticationError(
                f"Autenticación fallida con Jev: {exc}"
            ) from exc
        except (TypeSafeRateLimitError, TypeSafeAPIError, TypeSafeError) as exc:
            logger.warning("Error retornado por la API de TypeSafe Jev: %s", exc)
            raise JevAdapterError(f"Error en TypeSafe Jev: {exc}") from exc
        except Exception as exc:
            logger.exception("Error inesperado al invocar Jev TypeSafe")
            raise JevAdapterError(f"Error inesperado en adaptador Jev: {exc}") from exc

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        # Unpack structured typed responses
        answers = response.answers

        cat_ans = answers.get("category")
        category = cat_ans.choice if isinstance(cat_ans, ChoiceAnswer) else "other"
        cat_conf = (
            float(cat_ans.confidence) if isinstance(cat_ans, ChoiceAnswer) else 0.5
        )
        cat_probs = (
            {k: float(v) for k, v in cat_ans.probabilities.items()}
            if isinstance(cat_ans, ChoiceAnswer)
            else {}
        )

        risk_ans = answers.get("risk_level")
        risk_level = risk_ans.choice if isinstance(risk_ans, ChoiceAnswer) else "high"
        risk_conf = (
            float(risk_ans.confidence) if isinstance(risk_ans, ChoiceAnswer) else 0.5
        )
        risk_probs = (
            {k: float(v) for k, v in risk_ans.probabilities.items()}
            if isinstance(risk_ans, ChoiceAnswer)
            else {}
        )

        suff_ans = answers.get("information_sufficiency")
        suff_prob = float(suff_ans.noul) if isinstance(suff_ans, NoulAnswer) else 0.5

        amb_ans = answers.get("ambiguity_detected")
        amb_prob = float(amb_ans.noul) if isinstance(amb_ans, NoulAnswer) else 0.5

        input_tok = response.usage.input_tokens if response.usage else 0
        output_tok = response.usage.output_tokens if response.usage else 0

        return JevJudgmentResult(
            category=category,
            category_confidence=cat_conf,
            category_probabilities=cat_probs,
            risk_level=risk_level,
            risk_confidence=risk_conf,
            risk_probabilities=risk_probs,
            information_sufficiency=suff_prob,
            ambiguity_detected=amb_prob,
            input_tokens=input_tok,
            output_tokens=output_tok,
            latency_ms=elapsed_ms,
            model_version=response.model or self.model,
            raw_state=state,
        )

    def evaluate_findings_sync(
        self,
        *,
        findings: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> JevJudgmentResult:
        """Synchronous wrapper for contexts where an event loop is not already running."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    lambda: asyncio.run(
                        self.evaluate_findings(findings=findings, context=context)
                    )
                ).result()
        return asyncio.run(self.evaluate_findings(findings=findings, context=context))
