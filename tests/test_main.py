from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pymupdf
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings
from app.main import create_app
from app.schemas.extraction import (
    ExtractionConfidence,
    ExtractionMethod,
    ExtractionStatus,
    InvoiceExtractionOutput,
)


def make_client(
    tmp_path: Path | None = None,
    mock_conn: MagicMock | None = None,
) -> TestClient:
    conn = mock_conn or MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    cur.fetchone.return_value = None

    def _fake_db():
        yield conn

    settings = Settings(
        database_url="postgresql://unused:unused@127.0.0.1:1/unused",
        allowed_origins="http://localhost:5173",
        documents_dir=tmp_path or Path("/tmp/test-documents"),
    )
    return TestClient(create_app(settings=settings, db_dependency=_fake_db))


def test_health_is_public() -> None:
    response = make_client().get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "service": "backend"}


def test_openapi_and_swagger_docs_are_available() -> None:
    client = make_client()

    openapi_response = client.get("/api/openapi.json")
    docs_response = client.get("/api/docs")
    redoc_response = client.get("/api/redoc")

    assert openapi_response.status_code == 200
    assert openapi_response.json()["info"]["title"] == "AI Invoice Processor API"
    assert "/api/invoices" in openapi_response.json()["paths"]
    assert docs_response.status_code == 200
    assert "Swagger UI" in docs_response.text
    assert redoc_response.status_code == 200


def test_invoice_list_starts_empty() -> None:
    response = make_client().get("/api/invoices")

    assert response.status_code == 200
    assert response.json() == []


def test_invoice_detail_returns_404_when_missing() -> None:
    response = make_client().get(f"/api/invoices/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "HTTP_404"


def test_upload_rejects_unsupported_file_type() -> None:
    response = make_client().post(
        "/api/invoices",
        files={"file": ("invoice.txt", b"not an invoice", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "HTTP_400"


def test_upload_invoice_persists_file_and_runs_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_conn = MagicMock()
    mock_cur = mock_conn.cursor.return_value.__enter__.return_value
    mock_cur.fetchone.return_value = ("EXTRAIDA", None)

    def _fake_extract(*, cur, document_id, invoice_id, file_path):
        return InvoiceExtractionOutput(
            document_id=document_id,
            extraction_status=ExtractionStatus.SUCCESS,
            extraction_method=ExtractionMethod.DIGITAL_TEXT,
            confidence=ExtractionConfidence(overall=0.98, fields={}),
            extracted_at=datetime.now(UTC),
        )

    monkeypatch.setattr(main_module, "extract_invoice_document", _fake_extract)

    client = make_client(tmp_path=tmp_path, mock_conn=mock_conn)

    pdf_path = tmp_path / "sample.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Factura FE-9001")
    doc.save(pdf_path)
    doc.close()

    with pdf_path.open("rb") as f:
        response = client.post(
            "/api/invoices",
            files={"file": ("FE-9001.pdf", f, "application/pdf")},
        )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["business_status"] == "EXTRAIDA"
    assert (tmp_path / f"{body['document_id']}.pdf").exists()


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
