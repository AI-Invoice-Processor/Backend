from typing import Annotated

import psycopg
from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Settings, get_settings

ALLOWED_CONTENT_TYPES = {"application/pdf", "image/png", "image/jpeg"}


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()
    app = FastAPI(
        title=config.app_name,
        description=(
            "API para recepción y procesamiento de facturas. "
            "La documentación interactiva está disponible en /api/docs."
        ),
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        openapi_tags=[
            {
                "name": "system",
                "description": "Comprobaciones de estado y disponibilidad del servicio.",
            },
            {
                "name": "invoices",
                "description": "Operaciones de recepción y consulta de facturas.",
            },
        ],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_, exc: HTTPException) -> JSONResponse:
        detail = (
            exc.detail if isinstance(exc.detail, str) else "La solicitud no es válida."
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": f"HTTP_{exc.status_code}", "message": detail}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_, __) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "La solicitud no cumple el formato esperado.",
                }
            },
        )

    @app.get(
        "/health",
        tags=["system"],
        summary="Comprobar estado del servicio",
        response_description="El servicio está activo.",
    )
    async def health() -> dict[str, str]:
        return {"status": "healthy", "service": "backend"}

    @app.get(
        "/ready",
        tags=["system"],
        summary="Comprobar disponibilidad de dependencias",
        response_description="El servicio y PostgreSQL están disponibles.",
        responses={503: {"description": "La base de datos no está disponible."}},
    )
    async def ready() -> dict[str, str]:
        try:
            with psycopg.connect(config.database_url, connect_timeout=3) as connection:
                connection.execute("SELECT 1")
        except psycopg.Error as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="La base de datos no está disponible.",
            ) from exc
        return {"status": "ready", "service": "backend"}

    @app.get(
        "/api/invoices",
        tags=["invoices"],
        summary="Listar facturas",
        response_description="Lista de facturas procesadas.",
    )
    async def list_invoices() -> list[dict[str, str]]:
        """Contrato inicial; la persistencia real se incorporará en la siguiente fase."""
        return []

    @app.post(
        "/api/invoices",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["invoices"],
        summary="Cargar una factura",
        response_description="La factura fue aceptada para procesamiento.",
        responses={
            400: {"description": "Tipo de archivo no permitido."},
            413: {"description": "El archivo supera el tamaño máximo permitido."},
        },
    )
    async def upload_invoice(
        file: Annotated[UploadFile, File(...)],
    ) -> dict[str, str | int]:
        if file.content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El archivo debe ser PDF, PNG o JPG.",
            )

        content = await file.read(config.max_upload_size + 1)
        if len(content) > config.max_upload_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="El archivo supera el tamaño máximo permitido de 10 MB.",
            )

        return {
            "status": "accepted",
            "filename": file.filename or "invoice",
            "size_bytes": len(content),
        }

    return app


app = create_app()
