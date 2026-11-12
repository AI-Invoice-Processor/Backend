import hashlib
from typing import Annotated, Any
from uuid import UUID, uuid4

import psycopg
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)

import app.main as main_module
from app.api.dependencies import Settings, get_db, get_settings
from app.modules.invoices import (
    get_invoice_business_and_duplicate_status,
    get_invoice_detail_from_db,
    list_invoices_from_db,
    record_initial_invoice,
)
from app.schemas.invoice import InvoiceDetail, InvoiceListItem, UploadInvoiceResponse

router = APIRouter(prefix="/api/invoices", tags=["invoices"])

ALLOWED_CONTENT_TYPES = {"application/pdf", "image/png", "image/jpeg"}
EXTENSION_BY_CONTENT_TYPE = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}


@router.get(
    "",
    summary="Listar facturas",
    response_description="Lista de facturas recibidas y procesadas.",
    response_model=list[InvoiceListItem],
)
def list_invoices(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> list[InvoiceListItem]:
    with conn.cursor() as cur:
        return list_invoices_from_db(cur)


@router.get(
    "/{invoice_id}",
    summary="Obtener detalle de una factura",
    responses={404: {"description": "Factura no encontrada."}},
    response_model=InvoiceDetail,
)
def get_invoice_detail(
    invoice_id: UUID,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> InvoiceDetail:
    with conn.cursor() as cur:
        detail = get_invoice_detail_from_db(cur, invoice_id)
        if not detail:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Factura no encontrada.",
            )
        return detail


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cargar y extraer una factura (Etapas 1 y 2)",
    response_description="La factura fue almacenada y procesada en extracción.",
    responses={
        400: {"description": "Tipo de archivo no permitido."},
        413: {"description": "El archivo supera el tamaño máximo permitido."},
    },
    response_model=UploadInvoiceResponse,
)
def upload_invoice(
    file: Annotated[UploadFile, File(...)],
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    batch_id: Annotated[UUID | None, Form()] = None,
) -> dict[str, Any]:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El archivo debe ser PDF, PNG o JPG.",
        )

    content = file.file.read(settings.max_upload_size + 1)
    if len(content) > settings.max_upload_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="El archivo supera el tamaño máximo permitido de 10 MB.",
        )

    document_id = uuid4()
    invoice_id = uuid4()
    ext = EXTENSION_BY_CONTENT_TYPE[file.content_type]
    original_name = file.filename or f"invoice{ext}"
    sha256 = hashlib.sha256(content).hexdigest()

    settings.documents_dir.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{document_id}{ext}"
    file_path = settings.documents_dir / stored_filename
    file_path.write_bytes(content)
    storage_ref = f"documents/{stored_filename}"

    # Etapa 1: Registrar documento y factura en estado RECIBIDA
    with conn.transaction(), conn.cursor() as cur:
        record_initial_invoice(
            cur,
            document_id=document_id,
            invoice_id=invoice_id,
            original_name=original_name,
            content_type=file.content_type,
            storage_ref=storage_ref,
            sha256=sha256,
            size_bytes=len(content),
            batch_id=batch_id,
        )

    # Etapa 2: OCR + Extracción estructurada con Claude
    with conn.transaction(), conn.cursor() as cur:
        extraction = main_module.extract_invoice_document(
            cur=cur,
            document_id=document_id,
            invoice_id=invoice_id,
            file_path=file_path,
        )
        business_status, duplicate_of = get_invoice_business_and_duplicate_status(
            cur, invoice_id
        )

    return {
        "status": "accepted",
        "filename": original_name,
        "size_bytes": len(content),
        "document_id": str(document_id),
        "invoice_id": str(invoice_id),
        "batch_id": str(batch_id) if batch_id else None,
        "storage_path": storage_ref,
        "processing_status": extraction.extraction_status.value,
        "business_status": business_status,
        "duplicate_of_invoice_id": duplicate_of,
        "extraction": extraction.model_dump(mode="json"),
    }
