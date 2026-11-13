"""Stage 6 schemas: Accounting proposal and exception explanation."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ProposalStatus = Literal[
    "BLOCKED_PENDING_REVIEW",
    "READY_FOR_APPROVAL",
    "APPROVED",
    "REJECTED",
    "OBSOLETE",
]

GeneratedBy = Literal["TEMPLATE", "CLAUDE"]


class SupplierContextInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    supplier_id: UUID | None = Field(default=None, description="UUID del proveedor si fue asociado")
    name: str | None = Field(default=None, description="Razón social del proveedor")
    nit: str | None = Field(default=None, description="NIT o identificación fiscal")


class AmountsContextInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    subtotal: str = Field(..., description="Subtotal cuantificado en string decimal")
    tax_total: str = Field(..., description="Total de impuestos en string decimal")
    total: str = Field(..., description="Total neto de la factura en string decimal")
    currency: str = Field(default="COP", description="Moneda de la factura")


class ValidationExceptionItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = Field(..., description="Tipo de excepción o regla (ej. DIFERENCIA_VS_OC, IVA_DISCREPANCIA)")
    expected: str | None = Field(default=None, description="Valor o texto esperado por la regla")
    actual: str | None = Field(default=None, description="Valor o texto real de la factura")
    difference: str | None = Field(default=None, description="Diferencia cuantificada")
    difference_percentage: str | None = Field(default=None, description="Porcentaje de diferencia")
    severity: str | None = Field(default=None, description="Severidad de la excepción")
    description: str | None = Field(default=None, description="Descripción del hallazgo")


class RiskAssessmentContextInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    risk_level: str = Field(default="low", description="Nivel de riesgo (low, medium, high, critical)")
    requires_human_review: bool = Field(default=False, description="Requiere revisión humana")
    risk_score: float | None = Field(default=None, description="Puntuación cuantitativa de riesgo")
    exception_category: str | None = Field(default=None, description="Categoría clasificada de la excepción")
    confidence: float | None = Field(default=None, description="Confianza de la evaluación")


class AccountingContextInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    suggested_account: str | None = Field(
        default=None,
        description="Código de cuenta sugerido por contrato o reglas maestras (ej. 5135)",
    )
    cost_center: str | None = Field(
        default=None,
        description="Código de centro de costo sugerido (ej. CC02)",
    )
    authorized_accounts: list[str] = Field(
        default_factory=list,
        description="Lista de cuentas contables autorizadas en el catálogo maestro",
    )
    authorized_cost_centers: list[str] = Field(
        default_factory=list,
        description="Lista de centros de costo autorizados en el catálogo maestro",
    )
    notes: str | None = Field(default=None, description="Notas contables adicionales")


class Stage6ProposalInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    invoice_id: UUID = Field(..., description="Identificador único de la factura")
    data_version: int = Field(default=1, ge=1, description="Versión de datos de la factura")
    supplier: SupplierContextInput = Field(default_factory=SupplierContextInput)
    amounts: AmountsContextInput
    validation_exceptions: list[ValidationExceptionItem] = Field(
        default_factory=list,
        description="Excepciones detectadas en Etapa 4",
    )
    risk_assessment: RiskAssessmentContextInput = Field(
        default_factory=RiskAssessmentContextInput,
        description="Evaluación de riesgo de Etapa 5",
    )
    accounting_context: AccountingContextInput = Field(
        default_factory=AccountingContextInput,
        description="Contexto de cuentas y centros de costo autorizados",
    )
    document_references: dict[str, Any] = Field(
        default_factory=dict,
        description="Referencias cruzadas (número de factura, OC, contrato, soporte)",
    )


class Explanation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = Field(..., description="Resumen conciso y claro de la situación contable")
    details: str = Field(..., description="Detalle técnico de documentos contrastados y discrepancias")
    recommendation: str = Field(..., description="Recomendación accionable para el área financiera")


class AccountingProposalData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    supplier_id: UUID | None = Field(default=None, description="Identificador del proveedor")
    account_code: str | None = Field(..., description="Código PUC de cuenta contable autorizado")
    cost_center_code: str | None = Field(..., description="Código de centro de costo autorizado")
    subtotal: str = Field(..., description="Subtotal a causar en string decimal")
    tax_total: str = Field(..., description="Total de impuestos a causar en string decimal")
    total: str = Field(..., description="Total de la causación en string decimal")
    status: ProposalStatus = Field(
        ...,
        description="Estado de la propuesta (BLOCKED_PENDING_REVIEW o READY_FOR_APPROVAL)",
    )


class ProposalMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    latency_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    account_validation_status: str = "VALIDATED"


class Stage6ProposalOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: str = Field(default="0.2.0")
    invoice_id: UUID
    generated_by: GeneratedBy
    explanation: Explanation
    accounting_proposal: AccountingProposalData
    metadata: ProposalMetadata | None = Field(
        default=None,
        description="Metadatos técnicos de ejecución, tokens y latencia",
    )
