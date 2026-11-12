"""Opt-in only: set NEPXA_VALIDATION_TEST_DSN to an isolated disposable database."""

import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from app.modules.validation.repository import (
    get_current_validation_from_db,
    validate_invoice_from_db,
)
from app.schemas.validation import RuleConfig


@pytest.mark.skipif(
    not os.getenv("NEPXA_VALIDATION_TEST_DSN"),
    reason="Requires isolated PostgreSQL DSN",
)
def test_real_postgres_runs_versions_duplicates_and_rollback():
    dsn = os.environ["NEPXA_VALIDATION_TEST_DSN"]
    with (
        psycopg.connect(dsn, client_encoding="UTF8") as conn,
        conn.transaction(force_rollback=True),
        conn.cursor() as cur,
    ):
        namespace = f"stage4_test_{uuid4().hex}"
        cur.execute(f'CREATE SCHEMA "{namespace}"')
        cur.execute(f'SET LOCAL search_path TO "{namespace}", public')
        cur.execute((Path(__file__).parents[1] / "app/schema.sql").read_text())
        invoice, doc = uuid4(), uuid4()
        data = {
            "supplier": {"nit_base": "900654321", "dv": "1"},
            "prefix": "FE",
            "number": "1",
            "issue_date": "2026-09-20",
            "subtotal": "100",
            "total": "119",
            "lines": [
                {
                    "line_no": 1,
                    "quantity": "2",
                    "unit_price": "50",
                    "discount_amount": "0",
                    "amount": "100",
                }
            ],
            "taxes": [{"type": "IVA", "base": "100", "rate": "0.19", "amount": "19"}],
        }
        from psycopg.types.json import Jsonb

        cur.execute(
            """INSERT INTO documents(id,file_name,mime_type,category,storage_ref,sha256,processing_status)
                    VALUES (%s,'a.pdf','application/pdf','invoice','test','original','success')""",
            (doc,),
        )
        cur.execute(
            """INSERT INTO invoices(id,document_id,business_status,extraction_json,
                    extraction_confidence,supplier_nit_base,supplier_dv,prefix,number,issue_date,subtotal,total)
                    VALUES (%s,%s,'EXTRAIDA',%s,0.99,'900654321','1','FE','1','2026-09-20',100,119)""",
            (invoice, doc, Jsonb({"data": data})),
        )
        cur.execute(
            "INSERT INTO invoice_lines(invoice_id,line_no,quantity,unit_price,discount_amount,amount) VALUES (%s,1,2,50,0,100)",
            (invoice,),
        )
        cur.execute(
            "INSERT INTO invoice_taxes(invoice_id,tax_type,rate,base,amount) VALUES (%s,'IVA',0.19,100,19)",
            (invoice,),
        )
        cfg = RuleConfig(
            enabled={"V5": False, "D2": False},
            open_periods=[{"start": "2026-09-01", "end": "2026-09-30"}],
        )
        first = validate_invoice_from_db(cur, invoice, cfg, date(2026, 11, 12))
        assert not first.requires_human_review
        second = validate_invoice_from_db(cur, invoice, cfg, date(2026, 11, 12))
        assert (
            get_current_validation_from_db(cur, invoice).validation_run_id
            == second.validation_run_id
        )
        cur.execute("SELECT count(*) FROM rule_checks WHERE invoice_id=%s", (invoice,))
        assert cur.fetchone()[0] == len(first.checks) + len(second.checks)
        cur.execute(
            "UPDATE invoices SET data_version=2,total=130 WHERE id=%s", (invoice,)
        )
        assert get_current_validation_from_db(cur, invoice) is None
        failed = validate_invoice_from_db(cur, invoice, cfg, date(2026, 11, 12))
        assert failed.requires_human_review
        assert next(
            c for c in failed.checks if c.rule_id == "V4"
        ).actual_amount == Decimal("130.00")
        cur.execute("UPDATE invoices SET total=119 WHERE id=%s", (invoice,))
        corrected = validate_invoice_from_db(cur, invoice, cfg, date(2026, 11, 12))
        assert not corrected.requires_human_review
        assert not validate_invoice_from_db(
            cur, invoice, cfg, date(2026, 11, 12)
        ).requires_human_review
        # Duplicate branch: no normalized lines/taxes or identity, only original snapshot.
        other_doc, other_invoice = uuid4(), uuid4()
        cur.execute(
            """INSERT INTO documents(id,file_name,mime_type,category,storage_ref,sha256,processing_status,duplicate_of_invoice_id)
            VALUES (%s,'dup.pdf','application/pdf','invoice','test','original','success',%s)""",
            (other_doc, invoice),
        )
        cur.execute(
            """INSERT INTO invoices(id,document_id,business_status,extraction_json,extraction_confidence)
            VALUES (%s,%s,'EXCEPCION',%s,0.99)""",
            (other_invoice, other_doc, Jsonb({"data": data})),
        )
        duplicate = validate_invoice_from_db(
            cur, other_invoice, cfg, date(2026, 11, 12)
        )
        assert next(c for c in duplicate.checks if c.rule_id == "V6").result == "failed"
        assert next(c for c in duplicate.checks if c.rule_id == "V1").result == "passed"
        assert (
            str(invoice)
            in next(c for c in duplicate.checks if c.rule_id == "V6").actual_text
        )
        # A later duplicate must not turn its original into a reupload on revalidation.
        assert not validate_invoice_from_db(
            cur, invoice, cfg, date(2026, 11, 12)
        ).requires_human_review
        cur.execute(
            "SELECT count(*) FROM invoice_events WHERE invoice_id=%s", (invoice,)
        )
        before = cur.fetchone()[0]
        with conn.transaction(force_rollback=True):
            validate_invoice_from_db(cur, invoice, cfg, date(2026, 11, 12))
        cur.execute(
            "SELECT count(*) FROM invoice_events WHERE invoice_id=%s", (invoice,)
        )
        assert cur.fetchone()[0] == before
