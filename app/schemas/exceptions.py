"""Stage 5 schemas: Intelligent exception evaluation with Jev TypeSafe AI."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

RiskLevel = Literal["low", "medium", "high", "critical"]

ExceptionCategory = Literal[
    "amount_mismatch",
    "tax_mismatch",
    "supplier_mismatch",
    "duplicate_invoice",
    "missing_support",
    "contract_expired",
    "purchase_order_missing",
    "incomplete_information",
    "other",
]

DecisionType = Literal[
    "REQUIERE_REVISION",
    "APROBADO_DIRECTO",
    "RECHAZO_AUTOMATICO",
]


class ExceptionFindingInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = Field(
        ...,
        description="Tipo de regla o excepción detectada (ej. DIFERENCIA_VS_OC, IVA_DISCREPANCIA)",
    )
    difference: str | None = Field(
        default=None,
        description="Diferencia monetaria absoluta cuantificada en string decimal",
    )
    difference_percentage: str | None = Field(
        default=None,
        description="Porcentaje de diferencia respecto al valor esperado",
    )
    severity: str | None = Field(
        default=None,
        description="Severidad reportada por la regla financiera (low, medium, high, critical)",
    )
    description: str | None = Field(
        default=None,
        description="Descripción o mensaje detallado del hallazgo",
    )


class EvaluationContextInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    supplier_matched: bool = Field(
        default=True,
        description="Indica si el proveedor fue asociado exitosamente en base de datos",
    )
    contract_active: bool = Field(
        default=True,
        description="Indica si el contrato asociado está vigente a la fecha de emisión",
    )
    support_complete: bool = Field(
        default=True,
        description="Indica si los documentos soporte (remisiones, actas) están completos",
    )
    duplicate_detected: bool = Field(
        default=False,
        description="Indica si se detectó una factura duplicada por hash o identidad fiscal",
    )
    extraction_confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Nivel de confianza de la extracción OCR / AI de la factura",
    )
    po_reference: str | None = Field(
        default=None,
        description="Referencia de la orden de compra si existe",
    )


class Stage5EvaluationInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    invoice_id: UUID = Field(..., description="Identificador único de la factura")
    exceptions: list[ExceptionFindingInput] = Field(
        default_factory=list,
        description="Lista de hallazgos detectados por el motor de validación financiera",
    )
    context: EvaluationContextInput = Field(
        default_factory=EvaluationContextInput,
        description="Contexto de asociación y soporte de la factura",
    )


class RiskAssessment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    risk_level: RiskLevel = Field(
        ...,
        description="Nivel de riesgo clasificado (low, medium, high, critical)",
    )
    risk_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Puntuación cuantitativa de riesgo calibrada entre 0.0 y 1.0",
    )
    exception_category: str = Field(
        ...,
        description="Categoría principal de la excepción clasificada",
    )
    requires_human_review: bool = Field(
        ...,
        description="Determina si el caso requiere revisión humana obligatoria",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confianza del juicio emitido por Jev o motor de decisión",
    )


class AuditTrace(BaseModel):
    model_config = ConfigDict(extra="allow")

    evaluator: Literal["jev", "fallback_rules", "claude"] = "jev"
    model: str | None = None
    latency_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    information_sufficiency_probability: float | None = None
    ambiguity_probability: float | None = None
    category_probabilities: dict[str, float] = Field(default_factory=dict)
    risk_probabilities: dict[str, float] = Field(default_factory=dict)
    uncertainty_detected: bool = False
    uncertainty_reasons: list[str] = Field(default_factory=list)
    guardrail_reasons: list[str] = Field(default_factory=list)


class Stage5EvaluationOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: str = Field(default="0.2.0")
    invoice_id: UUID
    risk_assessment: RiskAssessment
    decision: DecisionType
    audit_trace: AuditTrace | None = Field(
        default=None,
        description="Detalle técnico de trazabilidad, latencia, probabilidades y costos",
    )
