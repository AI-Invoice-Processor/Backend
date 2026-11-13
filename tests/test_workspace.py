from unittest.mock import MagicMock
from uuid import uuid4

from app.api.routers import workspace
from app.config import Settings


def test_pipeline_does_not_expose_internal_error(monkeypatch):
    invoice_id = uuid4()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        (invoice_id,)
    ]
    monkeypatch.setattr(
        workspace,
        "_ensure_pipeline",
        MagicMock(side_effect=RuntimeError("secret database detail")),
    )

    result = workspace.process_workspace_pipeline(conn, Settings(), limit=1)

    assert result["failed"] == 1
    assert result["failures"] == [
        {"invoice_id": str(invoice_id), "error": "No se pudo procesar la factura."}
    ]
    assert "secret database detail" not in str(result)


def test_pipeline_processes_valid_invoice(monkeypatch):
    invoice_id = uuid4()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        (invoice_id,)
    ]
    ensure_pipeline = MagicMock()
    monkeypatch.setattr(workspace, "_ensure_pipeline", ensure_pipeline)

    result = workspace.process_workspace_pipeline(conn, Settings(), limit=1)

    assert result["processed"] == 1
    assert result["failed"] == 0
    assert result["invoice_ids"] == [str(invoice_id)]
    ensure_pipeline.assert_called_once()
