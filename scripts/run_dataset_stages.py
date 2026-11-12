"""Ejecuta una prueba de integración de las etapas 1, 2 y 3 con el dataset.

La lectura de PDFs, la API, PostgreSQL y el matching son reales. La única
dependencia sustituida es Claude: la extracción estructurada se construye a
partir de los CSV del dataset para mantener la prueba determinista y sin
llamadas externas.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import psycopg
from fastapi.testclient import TestClient

import app.modules.extraction.service as extraction_service
from app.config import Settings
from app.main import create_app
from app.modules.extraction.service import (
    _RawConfidence,
    _RawEvidence,
    _RawLine,
    _RawLLMExtraction,
    _RawRetention,
    _RawTax,
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def _build_extractor(
    *,
    invoices: list[dict[str, str]],
    suppliers: dict[str, dict[str, str]],
    lines_by_invoice: dict[str, list[dict[str, str]]],
) -> Any:
    by_number = {row["numero"]: row for row in invoices}

    def fake_extractor(document_text: str) -> _RawLLMExtraction:
        match = re.search(r"\bFE-\d+\b", document_text)
        if not match or match.group(0) not in by_number:
            raise AssertionError(
                "No se pudo identificar el número de factura en el PDF: "
                f"{document_text[:160]!r}"
            )

        invoice = by_number[match.group(0)]
        supplier = suppliers[invoice["proveedor_id"]]
        invoice_lines = lines_by_invoice[invoice["id"]]
        return _RawLLMExtraction(
            nit_raw=supplier["nit_formateado"],
            supplier_name=supplier["razon_social"],
            invoice_number_raw=invoice["numero"],
            issue_date=invoice["fecha_emision"],
            currency="COP",
            purchase_order_reference=invoice["oc_numero_referenciada"] or None,
            lines=[
                _RawLine(
                    line_no=index,
                    description=line["descripcion"],
                    quantity_raw=line["cantidad"],
                    unit_price_raw=line["valor_unitario"],
                    amount_raw=line["valor_total"],
                )
                for index, line in enumerate(invoice_lines, start=1)
            ],
            taxes=[
                _RawTax(
                    type="IVA",
                    rate_raw="19%",
                    base_raw=invoice["subtotal"],
                    amount_raw=invoice["iva"],
                )
            ],
            subtotal_raw=invoice["subtotal"],
            total_raw=invoice["total_factura"],
            retentions=[
                _RawRetention(
                    amount_raw=invoice["retenciones"],
                )
            ],
            retentions_total_raw=invoice["retenciones"],
            net_payable_raw=invoice["valor_a_pagar"],
            evidence=[
                _RawEvidence(
                    field="total",
                    value=invoice["total_factura"],
                    page=1,
                    text=f"TOTAL FACTURA: {invoice['total_factura']}",
                )
            ],
            field_confidences=[
                _RawConfidence(field="supplier.nit_base", score=0.99),
                _RawConfidence(field="supplier.dv", score=0.99),
                _RawConfidence(field="total", score=0.99),
            ],
        )

    return fake_extractor


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        required=True,
        help="Directorio raíz del dataset, el que contiene la carpeta salida.",
    )
    parser.add_argument(
        "--database-url",
        default="postgresql://invoice:local-dev-password-change-me@127.0.0.1:5432/invoices",
    )
    parser.add_argument(
        "--documents-dir",
        type=Path,
        default=Path("/tmp/dataset-stage-documents"),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cantidad máxima de facturas; 0 ejecuta todas.",
    )
    return parser.parse_args()


def _load_dataset(dataset_dir: Path) -> dict[str, Any]:
    output_dir = dataset_dir / "salida"
    invoices = _read_csv(output_dir / "facturas.csv")
    suppliers = {
        row["id"]: row for row in _read_csv(output_dir / "proveedores.csv")
    }
    lines_by_invoice: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in _read_csv(output_dir / "facturas_lineas.csv"):
        lines_by_invoice[row["factura_id"]].append(row)
    labels = {
        row["factura_id"]: row
        for row in json.loads((output_dir / "etiquetas.json").read_text())
    }
    purchase_orders = {
        row["numero"]: row for row in _read_csv(output_dir / "ordenes_compra.csv")
    }
    contracts = {
        row["id"]: row for row in _read_csv(output_dir / "contratos.csv")
    }
    supports_by_order_id: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in _read_csv(output_dir / "soportes.csv"):
        supports_by_order_id[row["oc_id"]].append(row)
    return {
        "output_dir": output_dir,
        "invoices": invoices,
        "suppliers": suppliers,
        "lines_by_invoice": lines_by_invoice,
        "labels": labels,
        "purchase_orders": purchase_orders,
        "contracts": contracts,
        "supports_by_order_id": supports_by_order_id,
    }


def _db_dependency(database_url: str):
    def get_test_db():
        with psycopg.connect(database_url) as connection:
            yield connection

    return get_test_db


def _run() -> int:
    args = _parse_args()
    dataset = _load_dataset(args.dataset_dir)
    invoices = dataset["invoices"]
    if args.limit > 0:
        invoices = invoices[: args.limit]

    args.documents_dir.mkdir(parents=True, exist_ok=True)
    fake_extractor = _build_extractor(
        invoices=dataset["invoices"],
        suppliers=dataset["suppliers"],
        lines_by_invoice=dataset["lines_by_invoice"],
    )
    original_extractor = extraction_service._call_claude_extractor
    extraction_service._call_claude_extractor = fake_extractor

    settings = Settings(
        database_url=args.database_url,
        documents_dir=args.documents_dir,
        allowed_origins="http://localhost:5173",
    )
    app = create_app(
        settings=settings,
        db_dependency=_db_dependency(args.database_url),
    )

    extraction_statuses: Counter[str] = Counter()
    matching_statuses: Counter[str] = Counter()
    failures: list[dict[str, Any]] = []
    po_by_number = dataset["purchase_orders"]
    uploaded = 0

    try:
        with TestClient(app) as client:
            for invoice in invoices:
                pdf_path = dataset["output_dir"] / invoice["archivo_pdf"]
                if not pdf_path.exists():
                    failures.append(
                        {
                            "invoice": invoice["numero"],
                            "stage": "stage1",
                            "error": f"No existe el PDF: {pdf_path}",
                        }
                    )
                    continue

                with pdf_path.open("rb") as file:
                    response = client.post(
                        "/api/invoices",
                        files={
                            "file": (
                                pdf_path.name,
                                file,
                                "application/pdf",
                            )
                        },
                    )

                if response.status_code != 202:
                    failures.append(
                        {
                            "invoice": invoice["numero"],
                            "stage": "stage1-2-3",
                            "status_code": response.status_code,
                            "body": response.text[:500],
                        }
                    )
                    continue

                uploaded += 1
                body = response.json()
                extraction = body.get("extraction") or {}
                matching = body.get("matching") or {}
                extraction_statuses[extraction.get("extraction_status", "missing")] += 1
                matching_statuses[matching.get("matching_status", "missing")] += 1

                expected_order = invoice["oc_numero_referenciada"]
                expected_supplier = dataset["suppliers"][invoice["proveedor_id"]]
                supplier_match = matching.get("supplier", {})
                if (
                    not supplier_match.get("matched")
                    or supplier_match.get("nit_base") != expected_supplier["nit"]
                ):
                    failures.append(
                        {
                            "invoice": invoice["numero"],
                            "stage": "stage3",
                            "error": "El proveedor no se asoció por NIT.",
                            "matching": matching,
                        }
                    )

                order_match = matching.get("purchase_order", {})
                if expected_order:
                    if (
                        not order_match.get("matched")
                        or order_match.get("order_number")
                        != expected_order
                    ):
                        failures.append(
                            {
                                "invoice": invoice["numero"],
                                "stage": "stage3",
                                "error": "La orden referenciada no se asoció.",
                                "matching": matching,
                            }
                        )
                    else:
                        expected_po = po_by_number[expected_order]
                        expected_contract = dataset["contracts"].get(
                            expected_po["contrato_id"]
                        )
                        contract_match = matching.get("contract", {})
                        if expected_contract and (
                            not contract_match.get("matched")
                            or contract_match.get("contract_number")
                            != expected_contract["numero"]
                        ):
                            failures.append(
                                {
                                    "invoice": invoice["numero"],
                                    "stage": "stage3",
                                    "error": "El contrato no se asoció.",
                                    "matching": matching,
                                }
                            )

                        expected_supports = dataset["supports_by_order_id"].get(
                            expected_po["id"], []
                        )
                        expected_support_numbers = {
                            support["numero"] for support in expected_supports
                        }
                        actual_support_numbers = {
                            support["number"]
                            for support in matching.get("support_documents", [])
                        }
                        if expected_support_numbers != actual_support_numbers:
                            failures.append(
                                {
                                    "invoice": invoice["numero"],
                                    "stage": "stage3",
                                    "error": "Los soportes no coinciden con la orden.",
                                    "expected": sorted(expected_support_numbers),
                                    "actual": sorted(actual_support_numbers),
                                }
                            )
                elif order_match.get("matched"):
                    failures.append(
                        {
                            "invoice": invoice["numero"],
                            "stage": "stage3",
                            "error": "Se asoció una orden aunque la factura no la informa.",
                            "matching": matching,
                        }
                    )

        with psycopg.connect(args.database_url) as connection:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT d.processing_stage, d.processing_status, i.match_status,
                           COUNT(*)
                      FROM documents d
                      JOIN invoices i ON i.document_id = d.id
                     GROUP BY d.processing_stage, d.processing_status, i.match_status
                     ORDER BY d.processing_stage, d.processing_status, i.match_status
                    """
                )
                database_summary = [
                    {
                        "processing_stage": row[0],
                        "processing_status": row[1],
                        "match_status": row[2],
                        "count": row[3],
                    }
                    for row in cur.fetchall()
                ]
    finally:
        extraction_service._call_claude_extractor = original_extractor

    summary = {
        "dataset": str(args.dataset_dir),
        "facturas_evaluadas": len(invoices),
        "uploads_accepted": uploaded,
        "stage2_extraction_status": dict(extraction_statuses),
        "stage3_matching_status": dict(matching_statuses),
        "database_summary": database_summary,
        "failures": failures,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 1 if failures or uploaded != len(invoices) else 0


if __name__ == "__main__":
    raise SystemExit(_run())
