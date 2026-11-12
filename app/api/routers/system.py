from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import Settings, get_settings

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    summary="Comprobar estado del servicio",
    response_description="El servicio está activo.",
)
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "backend"}


@router.get(
    "/ready",
    summary="Comprobar disponibilidad de dependencias",
    response_description="El servicio y PostgreSQL están disponibles.",
    responses={503: {"description": "La base de datos no está disponible."}},
)
async def ready(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    try:
        with psycopg.connect(settings.database_url, connect_timeout=3) as connection:
            connection.execute("SELECT 1")
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="La base de datos no está disponible.",
        ) from exc
    return {"status": "ready", "service": "backend"}
