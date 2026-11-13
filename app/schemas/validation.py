"""Stage 4 contract extensions; money stays Decimal internally and strings in JSON."""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.extraction import ExtractedInvoiceData

RULE_IDS = ("V1", "V2", "V3", "V4", "V5", "V6", "V7", "D2")


class ValidationModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def reject_floats(cls, value):
        def inspect(item):
            if isinstance(item, float):
                raise ValueError("Use strings decimales, no float")  # noqa: TRY004
            if isinstance(item, dict):
                for child in item.values():
                    inspect(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    inspect(child)

        inspect(value)
        return value


class InvoiceValidationData(ExtractedInvoiceData):
    @model_validator(mode="after")
    def finite_values(self):
        def inspect(value):
            if isinstance(value, Decimal) and (
                not value.is_finite() or abs(value) >= Decimal("1e16")
            ):
                raise ValueError("Importe no finito o fuera de NUMERIC(18,2)")
            if isinstance(value, dict):
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)

        inspect(self.model_dump())
        return self


class AccountingPeriod(ValidationModel):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("Periodo invertido")
        return self


class RuleConfig(ValidationModel):
    ruleset_version: str = "mvp-1"
    enabled: dict[str, bool] = Field(
        default_factory=lambda: dict.fromkeys(RULE_IDS, True)
    )
    arithmetic_tolerance: Decimal = Field(
        default=Decimal("1.00"), ge=0, lt=Decimal("1e14")
    )
    purchase_order_tolerance: Decimal = Field(
        default=Decimal("0.00"), ge=0, lt=Decimal("1e14")
    )
    precision: int = Field(default=2, ge=0, le=2)
    rounding: Literal["ROUND_HALF_UP", "ROUND_HALF_EVEN", "ROUND_DOWN"] = (
        "ROUND_HALF_UP"
    )
    vat_rates: list[Decimal] = Field(default_factory=lambda: [Decimal("0.19")])
    open_periods: list[AccountingPeriod] = Field(default_factory=list)
    severity: Literal["low", "medium", "high"] = "high"

    @field_validator("enabled")
    @classmethod
    def known_rules(cls, value):
        if set(value) - set(RULE_IDS):
            raise ValueError("Regla desconocida")
        return value

    @field_validator("vat_rates")
    @classmethod
    def valid_rates(cls, value):
        if not value or any(not r.is_finite() or r < 0 or r > 1 for r in value):
            raise ValueError("Tarifas inválidas")
        return value


class PurchaseOrderInput(ValidationModel):
    order_number: str | None = None
    subtotal: Decimal | None = None


class ValidationInput(ValidationModel):
    invoice_id: UUID
    data_version: int = Field(ge=1)
    invoice: InvoiceValidationData
    purchase_order: PurchaseOrderInput | None = None
    reference_date: date
    duplicate_invoice_ids: list[UUID] = Field(default_factory=list)
    same_document_invoice_ids: list[UUID] = Field(default_factory=list)
    known_no_discount_lines: list[int] = Field(default_factory=list)
    external_blockers: list[str] = Field(default_factory=list)


class RuleCheck(ValidationModel):
    rule_check_id: UUID = Field(default_factory=uuid4)
    rule_id: str
    invoice_line_no: int | None = None
    result: Literal["passed", "failed", "warning", "not_evaluated"]
    expected_amount: Decimal | None = None
    actual_amount: Decimal | None = None
    difference: Decimal | None = None
    difference_pct: Decimal | None = None
    expected_text: str | None = None
    actual_text: str | None = None
    reason: str
    reason_code: str
    tolerance: Decimal | None = None
    ruleset_version: str = ""
    evidence_refs: list[dict] = Field(default_factory=list)


class ValidationException(ValidationModel):
    exception_id: UUID = Field(default_factory=uuid4)
    rule_check_id: UUID
    rule_id: str
    type: str
    severity: str
    description: str
    expected_amount: Decimal | None = None
    actual_amount: Decimal | None = None
    difference: Decimal | None = None
    difference_pct: Decimal | None = None
    requires_review: bool = True
    evidence_refs: list[dict] = Field(default_factory=list)


class ValidationOutput(ValidationModel):
    schema_version: str = "0.2.0"
    invoice_id: UUID
    data_version: int
    validation_run_id: UUID = Field(default_factory=uuid4)
    validation_status: str = "completed"
    ruleset_version: str
    checks: list[RuleCheck]
    exceptions: list[ValidationException]
    requires_human_review: bool
    blocking_reasons: list[str] = Field(default_factory=list)
