import hashlib
from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

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
from pydantic import ValidationError

import app.main as main_module
from app.api.dependencies import Settings, get_db, get_settings
from app.modules.exceptions import (
    ExceptionEvaluatorService,
    InvoiceNotFoundError as ExceptionInvoiceNotFoundError,
    JevExceptionAdapter,
    evaluate_invoice_from_db,
)
from app.modules.invoices import (
    get_invoice_business_and_duplicate_status,
    get_invoice_detail_from_db,
    list_invoices_from_db,
    record_initial_invoice,
)
from app.modules.matching import InvoiceNotFoundError, match_invoice_from_db
from app.modules.validation.repository import (
    InvoiceNotFoundError as ValidationInvoiceNotFoundError,
)
from app.modules.validation.repository import (
    ValidationConflictError,
    validate_invoice_from_db,
)
from app.schemas.exceptions import Stage5EvaluationInput, Stage5EvaluationOutput
from app.schemas.invoice import InvoiceDetail, InvoiceListItem, UploadInvoiceResponse
from app.schemas.matching import InvoiceMatchingResponse
from app.schemas.validation import ValidationOutput

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


@router.post(
    "/{invoice_id}/matching",
    summary="Buscar y asociar documentos relacionados (Etapa 3)",
    description=(
        "Reejecuta la Etapa 3 usando los datos de la factura persistidos por la "
        "Etapa 2. No requiere body JSON: el proveedor se busca primero por "
        "`nit_base`, la orden por `order_number` y RapidFuzz solo se aplica al "
        "nombre del proveedor como fallback. Si la coincidencia es ambigua, "
        "devuelve candidatos sin asociar automáticamente."
    ),
    response_description="La factura fue asociada contra los datos maestros disponibles.",
    responses={404: {"description": "Factura no encontrada."}},
    response_model=InvoiceMatchingResponse,
)
def match_invoice(
    invoice_id: UUID,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> InvoiceMatchingResponse:
    try:
        with conn.transaction(), conn.cursor() as cur:
            return match_invoice_from_db(cur, invoice_id)
    except InvoiceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Factura no encontrada.",
        ) from exc


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

    matching = None
    if extraction.data is not None:
        # La Etapa 3 usa los datos ya persistidos por la Etapa 2 y tiene su
        # propia transacción para que un fallo de matching no revierta OCR.
        with conn.transaction(), conn.cursor() as cur:
            matching = match_invoice_from_db(cur, invoice_id)

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
        "matching": matching.model_dump(mode="json") if matching else None,
    }


@router.post(
    "/{invoice_id}/validation",
    response_model=ValidationOutput,
    summary="Ejecutar validación financiera determinista (Etapa 4)",
)
def validate_invoice(
    invoice_id: UUID,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    reference_date: date | None = None,
) -> ValidationOutput:
    if reference_date is not None and settings.environment.lower() not in {
        "development",
        "test",
        "testing",
    }:
        raise HTTPException(
            status_code=400,
            detail="reference_date solo está disponible en desarrollo/pruebas",
        )
    try:
        with conn.transaction(), conn.cursor() as cur:
            return validate_invoice_from_db(
                cur,
                invoice_id,
                settings.financial_validation,
                reference_date or datetime.now(ZoneInfo("America/Bogota")).date(),
            )
    except ValidationInvoiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Factura no encontrada") from exc
    except ValidationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    except ValidationError as exc:
        raise HTTPException(
            status_code=409, detail="Datos persistidos incompatibles con validación"
        ) from exc
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=503, detail="No se pudo completar la validación en PostgreSQL"
        ) from exc


@router.post(
    "/{invoice_id}/evaluate-exceptions",
    response_model=Stage5EvaluationOutput,
    summary="Evaluación inteligente de excepciones con Jev TypeSafe AI (Etapa 5)",
    description=(
        "Clasifica las excepciones detectadas por la validación financiera y evalúa el riesgo "
        "usando Jev TypeSafe AI. Actualiza risk_level, risk_score y risk_confidence en la factura."
    ),
)
def evaluate_invoice_exceptions(
    invoice_id: UUID,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Stage5EvaluationOutput:
    jev_adapter = JevExceptionAdapter(
        api_key=settings.typesafe_api_key,
        model=settings.typesafe_model,
    )
    service = ExceptionEvaluatorService(jev_adapter=jev_adapter)
    try:
        with conn.transaction(), conn.cursor() as cur:
            return evaluate_invoice_from_db(cur, invoice_id, service)
    except ExceptionInvoiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Factura no encontrada") from exc
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=503, detail="Error de base de datos durante la evaluación de excepciones"
        ) from exc


@router.post(
    "/evaluate-exceptions",
    response_model=Stage5EvaluationOutput,
    summary="Evaluación directa de excepciones con Jev (Etapa 5 sin DB)",
    description=(
        "Evalúa directamente un payload de hallazgos calculados y contexto sin requerir "
        "persistencia previa. Útil para microservicios, pipelines de prueba y auditorías."
    ),
)
def evaluate_exceptions_direct(
    payload: Stage5EvaluationInput,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Stage5EvaluationOutput:
    jev_adapter = JevExceptionAdapter(
        api_key=settings.typesafe_api_key,
        model=settings.typesafe_model,
    )
    service = ExceptionEvaluatorService(jev_adapter=jev_adapter)
    return service.evaluate_exceptions(payload)

