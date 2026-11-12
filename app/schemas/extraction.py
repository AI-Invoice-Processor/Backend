from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class ExtractionStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ExtractionMethod(StrEnum):
    DIGITAL_TEXT = "DIGITAL_TEXT"
    OCR = "OCR"
    AI = "AI"


class ExtractedSupplier(BaseModel):
    nit_raw: str | None = None
    nit_base: str | None = None
    dv: str | None = Field(default=None, max_length=1)
    name: str | None = None


class ExtractedInvoiceLine(BaseModel):
    line_no: int = Field(ge=1)
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    discount_amount: Decimal | None = None
    amount: Decimal | None = None


class ExtractedTax(BaseModel):
    type: str
    rate: Decimal | None = None
    base: Decimal | None = None
    amount: Decimal | None = None


class ExtractedRetention(BaseModel):
    type: str = "UNSPECIFIED"
    rate: Decimal | None = None
    base: Decimal | None = None
    amount: Decimal


class ExtractedInvoiceData(BaseModel):
    supplier: ExtractedSupplier = Field(default_factory=ExtractedSupplier)
    invoice_number_raw: str | None = None
    prefix: str | None = None
    number: str | None = None
    issue_date: date | None = None
    currency: str = Field(default="COP", min_length=3, max_length=3)
    purchase_order_reference: str | None = None
    lines: list[ExtractedInvoiceLine] = Field(default_factory=list)
    taxes: list[ExtractedTax] = Field(default_factory=list)
    subtotal: Decimal | None = None
    total: Decimal | None = None
    retentions: list[ExtractedRetention] = Field(default_factory=list)
    retentions_total: Decimal | None = None
    net_payable: Decimal | None = None


class ExtractionEvidence(BaseModel):
    field: str
    value: str | None = None
    page: int = Field(ge=1)
    text: str


class ExtractionConfidence(BaseModel):
    overall: float = Field(ge=0.0, le=1.0)
    fields: dict[str, float] = Field(default_factory=dict)


class ExtractionErrorItem(BaseModel):
    code: str
    field: str | None = None
    message: str


class InvoiceExtractionOutput(BaseModel):
    schema_version: str = "0.2.0"
    document_id: UUID
    document_category: str = "invoice"
    extraction_status: ExtractionStatus
    extraction_method: ExtractionMethod
    data: ExtractedInvoiceData | None = None
    evidence: list[ExtractionEvidence] = Field(default_factory=list)
    confidence: ExtractionConfidence | None = None
    errors: list[ExtractionErrorItem] = Field(default_factory=list)
    extracted_at: datetime
