"""Core business decision engine for exception evaluation.

Adheres strictly to the TypeSafe craft principles:
- Judgments from Jev, decisions in code.
- Code enforces truth table and business constraints.
- Mandatory failed validations can NEVER be overridden.
- Uncertainty flags require human review.
- Safe fallback mechanism when Jev is unavailable.
"""

import json
import logging
import time
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.modules.exceptions.adapter import (
    JevAdapterError,
    JevExceptionAdapter,
    JevJudgmentResult,
)
from app.modules.exceptions.questions import (
    MAX_AMBIGUITY_THRESHOLD,
    MIN_CONFIDENCE_THRESHOLD,
    MIN_SUFFICIENCY_THRESHOLD,
    RISK_LEVEL_WEIGHTS,
)
from app.schemas.exceptions import (
    AuditTrace,
    DecisionType,
    EvaluationContextInput,
    ExceptionCategory,
    ExceptionFindingInput,
    RiskAssessment,
    RiskLevel,
    Stage5EvaluationInput,
    Stage5EvaluationOutput,
)

logger = logging.getLogger(__name__)


class RuleBasedFallbackEvaluator:
    """Deterministic fallback evaluator used when Jev is unavailable or fails."""

    @staticmethod
    def evaluate(data: Stage5EvaluationInput) -> Stage5EvaluationOutput:
        start_time = time.perf_counter()
        ctx = data.context
        exceptions = data.exceptions

        # Priority 1: Integrity and identity blockers
        if ctx.duplicate_detected:
            category = "duplicate_invoice"
            risk_level: RiskLevel = "critical"
            score = 1.00
            conf = 0.99
            reason = "Duplicado detectado por hash o número de factura"
        elif not ctx.supplier_matched:
            category = "supplier_mismatch"
            risk_level = "critical"
            score = 0.95
            conf = 0.95
            reason = "Proveedor no coincide con registros maestros"
        elif not ctx.contract_active:
            category = "contract_expired"
            risk_level = "high"
            score = 0.90
            conf = 0.92
            reason = "Contrato vencido o fuera del periodo de vigencia"
        elif not ctx.support_complete:
            category = "missing_support"
            risk_level = "medium"
            score = 0.60
            conf = 0.88
            reason = "Soporte documental incompleto (sin acta o remisión)"
        elif ctx.extraction_confidence < 0.85:
            category = "incomplete_information"
            risk_level = "high"
            score = 0.85
            conf = 0.85
            reason = "Baja confianza en la extracción OCR"
        elif not exceptions:
            category = "other"
            risk_level = "low"
            score = 0.10
            conf = 0.95
            reason = "Sin excepciones detectadas"
        else:
            # Analyze primary exception from findings
            first_exc = exceptions[0]
            exc_type = (first_exc.type or "").upper()
            diff_pct_val = 0.0
            if first_exc.difference_percentage:
                try:
                    diff_pct_val = abs(float(first_exc.difference_percentage))
                except (ValueError, TypeError):
                    diff_pct_val = 0.0

            if "OC" in exc_type or "ORDEN" in exc_type or "PRECIO" in exc_type or "D2" in exc_type:
                category = "amount_mismatch"
                if diff_pct_val > 5.0 or (first_exc.severity and first_exc.severity.lower() in ("high", "critical")):
                    risk_level = "high"
                    score = min(0.75 + (diff_pct_val / 100.0), 0.98)
                elif diff_pct_val > 0.0:
                    risk_level = "medium"
                    score = 0.45
                else:
                    risk_level = "low"
                    score = 0.20
            elif "IVA" in exc_type or "IMPUESTO" in exc_type or "RETENCION" in exc_type or "V4" in exc_type:
                category = "tax_mismatch"
                risk_level = "high"
                score = 0.82
            elif "NIT" in exc_type or "PROVEEDOR" in exc_type:
                category = "supplier_mismatch"
                risk_level = "critical"
                score = 0.95
            elif "SIN_OC" in exc_type:
                category = "purchase_order_missing"
                risk_level = "high"
                score = 0.85
            else:
                category = "other"
                risk_level = "medium"
                score = 0.50
            conf = 0.90
            reason = f"Regla tradicional activada para {exc_type}"

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        requires_review = bool(exceptions or risk_level in ("medium", "high", "critical") or not ctx.support_complete)
        decision: DecisionType = "REQUIERE_REVISION" if requires_review else "APROBADO_DIRECTO"

        trace = AuditTrace(
            evaluator="fallback_rules",
            model="deterministic-rule-engine-v1",
            latency_ms=elapsed_ms,
            information_sufficiency_probability=1.0,
            ambiguity_probability=0.0,
            uncertainty_detected=False,
            guardrail_reasons=[reason],
        )

        return Stage5EvaluationOutput(
            schema_version="0.2.0",
            invoice_id=data.invoice_id,
            risk_assessment=RiskAssessment(
                risk_level=risk_level,
                risk_score=round(score, 4),
                exception_category=category,
                requires_human_review=requires_review,
                confidence=round(conf, 4),
            ),
            decision=decision,
            audit_trace=trace,
        )


class ClaudeExceptionEvaluator:
    """Alternative evaluator using Anthropic Claude for benchmark comparison."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        self.api_key = api_key
        self.model = model

    def evaluate(self, data: Stage5EvaluationInput) -> Stage5EvaluationOutput:
        import anthropic

        start_time = time.perf_counter()
        client = anthropic.Anthropic(api_key=self.api_key)

        prompt = f"""You are a financial invoice compliance assistant.
Analyze these computed exceptions and context (never raw documents):
Findings: {json.dumps([e.model_dump(mode='json') for e in data.exceptions])}
Context: {json.dumps(data.context.model_dump(mode='json'))}

Classify into:
1. exception_category: one of [amount_mismatch, tax_mismatch, supplier_mismatch, duplicate_invoice, missing_support, contract_expired, purchase_order_missing, incomplete_information, other]
2. risk_level: one of [low, medium, high, critical]
3. risk_score: number between 0.0 and 1.0
4. confidence: number between 0.0 and 1.0
5. requires_human_review: boolean

Respond strictly with valid JSON only, without any markdown formatting or commentary:
{{"exception_category": "...", "risk_level": "...", "risk_score": 0.0, "confidence": 0.0, "requires_human_review": true}}
"""
        message = client.messages.create(
            model=self.model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        content_text = message.content[0].text.strip()
        if content_text.startswith("```"):
            content_text = content_text.strip("`").replace("json\n", "", 1).strip()
        parsed = json.loads(content_text)

        category = parsed.get("exception_category", "other")
        risk_level = parsed.get("risk_level", "high")
        risk_score = float(parsed.get("risk_score", 0.8))
        confidence = float(parsed.get("confidence", 0.9))
        req_review = bool(parsed.get("requires_human_review", True))

        # Enforce code guardrails
        if data.exceptions or data.context.duplicate_detected:
            req_review = True

        decision: DecisionType = "REQUIERE_REVISION" if req_review else "APROBADO_DIRECTO"

        trace = AuditTrace(
            evaluator="claude",
            model=self.model,
            latency_ms=elapsed_ms,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )

        return Stage5EvaluationOutput(
            schema_version="0.2.0",
            invoice_id=data.invoice_id,
            risk_assessment=RiskAssessment(
                risk_level=risk_level,
                risk_score=round(risk_score, 4),
                exception_category=category,
                requires_human_review=req_review,
                confidence=round(confidence, 4),
            ),
            decision=decision,
            audit_trace=trace,
        )


class ExceptionEvaluatorService:
    """Main domain service integrating Jev TypeSafe AI with code-level decision logic."""

    def __init__(
        self,
        jev_adapter: JevExceptionAdapter | None = None,
        fallback_evaluator: RuleBasedFallbackEvaluator | None = None,
    ):
        self.jev_adapter = jev_adapter or JevExceptionAdapter()
        self.fallback_evaluator = fallback_evaluator or RuleBasedFallbackEvaluator()

    def evaluate_exceptions(self, input_data: Stage5EvaluationInput) -> Stage5EvaluationOutput:
        """Evaluate invoice exceptions using Jev TypeSafe AI with code decision guardrails."""
        # Convert findings and context into clean python dictionaries (never raw files)
        findings_payload = [e.model_dump(mode="json") for e in input_data.exceptions]
        context_payload = input_data.context.model_dump(mode="json")

        # Attempt evaluation with Jev TypeSafe AI
        try:
            judgment = self.jev_adapter.evaluate_findings(
                findings=findings_payload,
                context=context_payload,
            )
            return self._apply_decision_rules(input_data, judgment)
        except JevAdapterError as exc:
            logger.warning(
                "Jev no disponible o falló para factura %s (%s). Activando fallback de seguridad por reglas.",
                input_data.invoice_id,
                exc,
            )
            fallback_result = self.fallback_evaluator.evaluate(input_data)
            if fallback_result.audit_trace:
                fallback_result.audit_trace.guardrail_reasons.append(
                    f"Fallo de Jev ({exc.__class__.__name__}): activado fallback por reglas de seguridad"
                )
            return fallback_result

    def _apply_decision_rules(
        self,
        data: Stage5EvaluationInput,
        judgment: JevJudgmentResult,
    ) -> Stage5EvaluationOutput:
        """Apply the decision truth table and safety rules over Jev judgments.

        'Judgments from Jev, decisions in code'.
        """
        uncertainty_reasons: list[str] = []
        guardrail_reasons: list[str] = []

        category = judgment.category
        risk_level = judgment.risk_level

        # Compute continuous risk score from Jev calibrated probabilities
        risk_probs = judgment.risk_probabilities
        if risk_probs:
            weighted_score = sum(
                risk_probs.get(lvl, 0.0) * RISK_LEVEL_WEIGHTS.get(lvl, 0.5)
                for lvl in ("low", "medium", "high", "critical")
            )
        else:
            weighted_score = RISK_LEVEL_WEIGHTS.get(risk_level, 0.70)

        # Contextual adjustment: if difference percentage is significant, blend into risk score
        diff_pct = 0.0
        for exc in data.exceptions:
            if exc.difference_percentage:
                try:
                    diff_pct = max(diff_pct, abs(float(exc.difference_percentage)))
                except (ValueError, TypeError):
                    pass

        if diff_pct > 0.0:
            pct_factor = min(diff_pct / 100.0, 0.25)
            risk_score = min(weighted_score + pct_factor, 1.0)
        else:
            risk_score = weighted_score

        # Combine model confidence
        overall_confidence = min(judgment.category_confidence, judgment.risk_confidence)

        # Check for Uncertainty as per TypeSafe Craft
        is_uncertain = False
        if overall_confidence < MIN_CONFIDENCE_THRESHOLD:
            is_uncertain = True
            uncertainty_reasons.append(
                f"Confianza de Jev ({overall_confidence:.2f}) inferior al umbral mínimo ({MIN_CONFIDENCE_THRESHOLD})"
            )

        if judgment.ambiguity_detected > MAX_AMBIGUITY_THRESHOLD:
            is_uncertain = True
            uncertainty_reasons.append(
                f"Alta ambigüedad detectada por Jev ({judgment.ambiguity_detected:.2f} > {MAX_AMBIGUITY_THRESHOLD})"
            )

        if judgment.information_sufficiency < MIN_SUFFICIENCY_THRESHOLD:
            is_uncertain = True
            uncertainty_reasons.append(
                f"Información disponible insuficiente según Jev ({judgment.information_sufficiency:.2f} < {MIN_SUFFICIENCY_THRESHOLD})"
            )

        # Mandatory Business Guardrails (REGLAS IMPORTANTES):
        # 1. No permitir que Jev anule validaciones obligatorias fallidas.
        has_mandatory_exceptions = bool(data.exceptions)
        has_context_failures = (
            data.context.duplicate_detected
            or not data.context.supplier_matched
            or not data.context.contract_active
            or not data.context.support_complete
            or data.context.extraction_confidence < 0.85
        )

        requires_review = False

        if has_mandatory_exceptions:
            requires_review = True
            guardrail_reasons.append(
                f"Excepciones obligatorias presentes ({len(data.exceptions)} hallazgo(s)): Jev no puede omitir revisión"
            )

        if has_context_failures:
            requires_review = True
            if data.context.duplicate_detected:
                guardrail_reasons.append("Factura duplicada detectada: revisión obligatoria")
            if not data.context.supplier_matched:
                guardrail_reasons.append("Proveedor no coincide con registros maestros")
            if not data.context.contract_active:
                guardrail_reasons.append("Contrato no vigente o inactivo")
            if not data.context.support_complete:
                guardrail_reasons.append("Documentación soporte incompleta")
            if data.context.extraction_confidence < 0.85:
                guardrail_reasons.append("Baja confianza de extracción OCR")

        # 2. Si Jev devuelve una respuesta incierta, recomendar revisión humana.
        if is_uncertain:
            requires_review = True
            guardrail_reasons.append("Incertidumbre detectada en juicio Jev: revisión humana requerida")

        # 3. Categorías de riesgo elevadas requieren revisión.
        if risk_level in ("medium", "high", "critical") or risk_score >= 0.40:
            requires_review = True

        # If perfectly clean and certain:
        if not requires_review and not has_mandatory_exceptions and not has_context_failures and not is_uncertain:
            category = "other"
            risk_level = "low"
            risk_score = min(risk_score, 0.10)
            decision = "APROBADO_DIRECTO"
        else:
            decision = "REQUIERE_REVISION"

        trace = AuditTrace(
            evaluator="jev",
            model=judgment.model_version,
            latency_ms=judgment.latency_ms,
            input_tokens=judgment.input_tokens,
            output_tokens=judgment.output_tokens,
            information_sufficiency_probability=judgment.information_sufficiency,
            ambiguity_probability=judgment.ambiguity_detected,
            category_probabilities=judgment.category_probabilities,
            risk_probabilities=judgment.risk_probabilities,
            uncertainty_detected=is_uncertain,
            uncertainty_reasons=uncertainty_reasons,
            guardrail_reasons=guardrail_reasons,
        )

        return Stage5EvaluationOutput(
            schema_version="0.2.0",
            invoice_id=data.invoice_id,
            risk_assessment=RiskAssessment(
                risk_level=risk_level,  # type: ignore[arg-type]
                risk_score=round(risk_score, 4),
                exception_category=category,
                requires_human_review=requires_review,
                confidence=round(overall_confidence, 4),
            ),
            decision=decision,
            audit_trace=trace,
        )
