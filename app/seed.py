import csv
import os
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import psycopg

from app.config import get_settings

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"
DEFAULT_SEED_DIR = Path(os.getenv("SEED_DATA_DIR", "/seed_data"))


def read_csv(seed_dir: Path, filename: str) -> list[dict[str, str]]:
    file_path = seed_dir / filename
    if not file_path.exists():
        raise FileNotFoundError(f"No se encontró el archivo requerido: {file_path}")
    with file_path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "t", "si", "sí", "yes"}


def seed_database(seed_dir: Path = DEFAULT_SEED_DIR) -> None:
    settings = get_settings()
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    accounts_rows = read_csv(seed_dir, "catalogo_cuentas.csv")
    cost_centers_rows = read_csv(seed_dir, "catalogo_centros_costo.csv")
    suppliers_rows = read_csv(seed_dir, "proveedores.csv")
    contracts_rows = read_csv(seed_dir, "contratos.csv")
    po_rows = read_csv(seed_dir, "ordenes_compra.csv")
    po_lines_rows = read_csv(seed_dir, "ordenes_compra_lineas.csv")
    supports_rows = read_csv(seed_dir, "soportes.csv")

    with (
        psycopg.connect(settings.database_url) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        # 0. Crear tablas e índices si no existen
        cur.execute(schema_sql)

        # 1. Catálogo de cuentas (PUC)
        for row in accounts_rows:
            cur.execute(
                """
                INSERT INTO accounts (code, name)
                VALUES (%s, %s)
                ON CONFLICT (code) DO NOTHING
                """,
                (row["codigo"].strip(), row["nombre"].strip()),
            )

        # 2. Catálogo de centros de costo
        for row in cost_centers_rows:
            cur.execute(
                """
                INSERT INTO cost_centers (code, name)
                VALUES (%s, %s)
                ON CONFLICT (code) DO NOTHING
                """,
                (row["codigo"].strip(), row["nombre"].strip()),
            )

        # 3. Proveedores (mapeo csv_id -> UUID real en PostgreSQL)
        supplier_uuid_by_csv_id: dict[str, UUID] = {}
        for row in suppliers_rows:
            csv_id = row["id"].strip()
            nit_base = row["nit"].strip()
            cur.execute(
                """
                INSERT INTO suppliers (
                    nit_base, dv, legal_name, person_type,
                    tax_regime, is_self_withholder, city
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (nit_base) DO NOTHING
                """,
                (
                    nit_base,
                    row["dv"].strip(),
                    row["razon_social"].strip(),
                    row["tipo_persona"].strip(),
                    row["regimen"].strip(),
                    parse_bool(row["es_autorretenedor"]),
                    row["ciudad"].strip() or None,
                ),
            )
            cur.execute(
                "SELECT id FROM suppliers WHERE nit_base = %s",
                (nit_base,),
            )
            supplier_uuid_by_csv_id[csv_id] = cur.fetchone()[0]

        # 4. Contratos (mapeo csv_id -> UUID real)
        contract_uuid_by_csv_id: dict[str, UUID] = {}
        for row in contracts_rows:
            csv_id = row["id"].strip()
            contract_number = row["numero"].strip()
            supplier_id = supplier_uuid_by_csv_id[row["proveedor_id"].strip()]
            cur.execute(
                """
                INSERT INTO contracts (
                    contract_number, supplier_id, contract_type, object,
                    total_amount, start_date, end_date, payment_days,
                    default_account, default_cost_center, pdf_ref
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (contract_number) DO NOTHING
                """,
                (
                    contract_number,
                    supplier_id,
                    row["tipo"].strip(),
                    row["objeto"].strip(),
                    Decimal(row["valor_total"].strip()),
                    date.fromisoformat(row["fecha_inicio"].strip()),
                    date.fromisoformat(row["fecha_fin"].strip()),
                    int(row["dias_pago"].strip()),
                    row["cuenta_contable_default"].strip(),
                    row["centro_costo_default"].strip(),
                    f"pdf/contratos/{contract_number}.pdf",
                ),
            )
            cur.execute(
                "SELECT id FROM contracts WHERE contract_number = %s",
                (contract_number,),
            )
            contract_uuid_by_csv_id[csv_id] = cur.fetchone()[0]

        # 5. Órdenes de compra (mapeo csv_id -> UUID real)
        po_uuid_by_csv_id: dict[str, UUID] = {}
        for row in po_rows:
            csv_id = row["id"].strip()
            order_number = row["numero"].strip()
            supplier_id = supplier_uuid_by_csv_id[row["proveedor_id"].strip()]
            raw_contract_id = row["contrato_id"].strip()
            contract_id = (
                contract_uuid_by_csv_id[raw_contract_id] if raw_contract_id else None
            )
            cur.execute(
                """
                INSERT INTO purchase_orders (
                    order_number, supplier_id, contract_id, order_date,
                    subtotal, tax_amount, total, status, pdf_ref
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (order_number) DO NOTHING
                """,
                (
                    order_number,
                    supplier_id,
                    contract_id,
                    date.fromisoformat(row["fecha"].strip()),
                    Decimal(row["subtotal"].strip()),
                    Decimal(row["iva"].strip()),
                    Decimal(row["total"].strip()),
                    row["estado"].strip(),
                    f"pdf/ordenes_compra/{order_number}.pdf",
                ),
            )
            cur.execute(
                "SELECT id FROM purchase_orders WHERE order_number = %s",
                (order_number,),
            )
            po_uuid_by_csv_id[csv_id] = cur.fetchone()[0]

        # 6. Líneas de órdenes de compra (generando line_no 1..N por OC)
        line_counter_by_oc: dict[str, int] = defaultdict(int)
        for row in po_lines_rows:
            oc_csv_id = row["oc_id"].strip()
            line_counter_by_oc[oc_csv_id] += 1
            line_no = line_counter_by_oc[oc_csv_id]
            po_id = po_uuid_by_csv_id[oc_csv_id]

            cur.execute(
                """
                INSERT INTO purchase_order_lines (
                    purchase_order_id, line_no, description,
                    quantity, unit_price, amount
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (purchase_order_id, line_no) DO NOTHING
                """,
                (
                    po_id,
                    line_no,
                    row["descripcion"].strip(),
                    Decimal(row["cantidad"].strip()),
                    Decimal(row["valor_unitario"].strip()),
                    Decimal(row["valor_total"].strip()),
                ),
            )

        # 7. Soportes de recepción
        for row in supports_rows:
            po_id = po_uuid_by_csv_id[row["oc_id"].strip()]
            support_number = row["numero"].strip()
            cur.execute(
                """
                INSERT INTO support_documents (
                    purchase_order_id, support_type, number,
                    received_date, received_amount, pdf_ref
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (number) DO NOTHING
                """,
                (
                    po_id,
                    row["tipo"].strip(),
                    support_number,
                    date.fromisoformat(row["fecha"].strip()),
                    Decimal(row["valor_recibido"].strip()),
                    f"pdf/soportes/{support_number}.pdf",
                ),
            )

        # Resumen de verificación en consola
        tables = [
            "accounts",
            "cost_centers",
            "suppliers",
            "contracts",
            "purchase_orders",
            "purchase_order_lines",
            "support_documents",
        ]
        print("Carga completada con éxito. Registros actuales en PostgreSQL:")
        for table in tables:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
            print(f"  - {table}: {count}")


if __name__ == "__main__":
    seed_database()
