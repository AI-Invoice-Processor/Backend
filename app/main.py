from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.dependencies import get_db, get_settings
from app.api.routers import invoices, system
from app.config import Settings
from app.modules.extraction import extract_invoice_document

__all__ = ["app", "create_app", "extract_invoice_document"]


def create_app(
    settings: Settings | None = None,
    db_dependency: Callable[..., Any] | None = None,
) -> FastAPI:
    config = settings or get_settings()

    app = FastAPI(
        title=config.app_name,
        description=(
            "API para recepción y procesamiento de facturas. "
            "La documentación interactiva está disponible en /api/docs."
        ),
        version="0.2.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        openapi_tags=[
            {
                "name": "system",
                "description": "Comprobaciones de estado y disponibilidad.",
            },
            {
                "name": "invoices",
                "description": "Operaciones de recepción, extracción y consulta.",
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

    # Dependency overrides si se pasan en create_app
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: settings
    if db_dependency is not None:
        app.dependency_overrides[get_db] = db_dependency

    # Exception handlers
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

    # Routers modulares
    app.include_router(system.router)
    app.include_router(invoices.router)

    return app


app = create_app()
