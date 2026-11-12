from pydantic import BaseModel, ConfigDict

from app.schemas.matching import InvoiceMatchingResponse


class InvoiceListItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    invoice_id: str
    document_id: str
    file_name: str
    batch_id: str | None = None
    processing_status: str | None = None
    supplier_nit_base: str | None = None
    supplier_dv: str | None = None
    supplier_name: str | None = None
    invoice_number: str | None = None
    issue_date: str | None = None
    subtotal: str | None = None
    total: str | None = None
    po_reference: str | None = None
    extraction_confidence: float | None = None
    business_status: str
    data_version: int
    created_at: str | None = None


class InvoiceLineItem(BaseModel):
    line_no: int
    description: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    discount_amount: str | None = None
    amount: str | None = None


class InvoiceTaxItem(BaseModel):
    type: str
    rate: str | None = None
    base: str | None = None
    amount: str | None = None


class InvoiceEventItem(BaseModel):
    event_type: str
    actor_type: str
    actor_name: str
    from_status: str | None = None
    to_status: str | None = None
    rule_ref: str | None = None
    reason: str | None = None
    occurred_at: str | None = None


class InvoiceDetail(BaseModel):
    invoice_id: str
    document_id: str
    file_name: str
    storage_ref: str
    processing_status: str | None = None
    duplicate_of_invoice_id: str | None = None
    supplier_nit_base: str | None = None
    supplier_dv: str | None = None
    supplier_name: str | None = None
    prefix: str | None = None
    number: str | None = None
    issue_date: str | None = None
    currency: str
    subtotal: str | None = None
    total: str | None = None
    retentions_total: str | None = None
    net_payable: str | None = None
    po_reference: str | None = None
    extraction_method: str | None = None
    extraction_confidence: float | None = None
    extraction_json: dict | None = None
    business_status: str
    data_version: int
    lines: list[InvoiceLineItem] = []
    taxes: list[InvoiceTaxItem] = []
    events: list[InvoiceEventItem] = []


class UploadInvoiceResponse(BaseModel):
    status: str
    filename: str
    size_bytes: int
    document_id: str
    invoice_id: str
    batch_id: str | None = None
    storage_path: str
    processing_status: str
    business_status: str
    duplicate_of_invoice_id: str | None = None
    extraction: dict
    matching: InvoiceMatchingResponse | None = None
