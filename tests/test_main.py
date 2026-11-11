from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def make_client() -> TestClient:
    settings = Settings(
        database_url="postgresql://unused:unused@127.0.0.1:1/unused",
        allowed_origins="http://localhost:5173",
    )
    return TestClient(create_app(settings))


def test_health_is_public() -> None:
    response = make_client().get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "service": "backend"}


def test_invoice_list_starts_empty() -> None:
    response = make_client().get("/api/invoices")

    assert response.status_code == 200
    assert response.json() == []


def test_upload_rejects_unsupported_file_type() -> None:
    response = make_client().post(
        "/api/invoices",
        files={"file": ("invoice.txt", b"not an invoice", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "HTTP_400"


def test_cors_is_limited_to_configured_origin() -> None:
    response = make_client().options(
        "/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
