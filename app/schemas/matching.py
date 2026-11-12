from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class MatchingStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"


class MatchingCandidate(BaseModel):
    entity: str
    candidate_id: str
    label: str
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str


class SupplierMatch(BaseModel):
    supplier_id: str | None = None
    nit_base: str | None = None
    name: str | None = None
    matched: bool = False


class PurchaseOrderMatch(BaseModel):
    purchase_order_id: str | None = None
    order_number: str | None = None
    matched: bool = False


class ContractMatch(BaseModel):
    contract_id: str | None = None
    contract_number: str | None = None
    matched: bool = False


class SupportDocumentMatch(BaseModel):
    support_document_id: str
    number: str
    type: str
    matched: bool = True


class InvoiceMatchingResponse(BaseModel):
    schema_version: str = "0.2.0"
    invoice_id: UUID
    matching_status: MatchingStatus
    supplier: SupplierMatch
    purchase_order: PurchaseOrderMatch
    contract: ContractMatch
    support_documents: list[SupportDocumentMatch] = Field(default_factory=list)
    missing_documents: list[str] = Field(default_factory=list)
    candidates: list[MatchingCandidate] = Field(default_factory=list)
