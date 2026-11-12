import json
from uuid import UUID

import psycopg

from app.schemas.invoice import (
    InvoiceDetail,
    InvoiceEventItem,
    InvoiceLineItem,
    InvoiceListItem,
    InvoiceTaxItem,
)
from app.state_machine import InvoiceStatus


def list_invoices_from_db(cur: psycopg.Cursor) -> list[InvoiceListItem]:
    cur.execute(
        """
        SELECT i.id,
               i.document_id,
               d.file_name,
               d.batch_id,
               d.processing_status,
               i.supplier_nit_base,
               i.supplier_dv,
               i.supplier_name,
               i.prefix,
               i.number,
               i.issue_date,
               i.subtotal,
               i.total,
               i.po_reference,
               i.extraction_confidence,
               i.business_status,
               i.data_version,
               i.created_at
          FROM invoices i
          JOIN documents d ON d.id = i.document_id
         ORDER BY i.created_at DESC
        """
    )
    rows = cur.fetchall()

    results: list[InvoiceListItem] = []
    for r in rows:
        invoice_num = f"{r[8]}-{r[9]}" if r[8] and r[9] else r[9]
        conf = float(r[14]) if r[14] is not None else None
        results.append(
            InvoiceListItem(
                invoice_id=str(r[0]),
                document_id=str(r[1]),
                file_name=r[2],
                batch_id=str(r[3]) if r[3] else None,
                processing_status=r[4],
                supplier_nit_base=r[5],
                supplier_dv=r[6],
                supplier_name=r[7],
                invoice_number=invoice_num,
                issue_date=r[10].isoformat() if r[10] else None,
                subtotal=str(r[11]) if r[11] is not None else None,
                total=str(r[12]) if r[12] is not None else None,
                po_reference=r[13],
                extraction_confidence=conf,
                business_status=r[15],
                data_version=r[16],
                created_at=r[17].isoformat() if r[17] else None,
            )
        )
    return results


def get_invoice_detail_from_db(
    cur: psycopg.Cursor, invoice_id: UUID
) -> InvoiceDetail | None:
    cur.execute(
        """
        SELECT i.id,
               i.document_id,
               d.file_name,
               d.storage_ref,
               d.processing_status,
               d.duplicate_of_invoice_id,
               i.supplier_nit_base,
               i.supplier_dv,
               i.supplier_name,
               i.prefix,
               i.number,
               i.issue_date,
               i.currency,
               i.subtotal,
               i.total,
               i.retentions_total,
               i.net_payable,
               i.po_reference,
               i.extraction_method,
               i.extraction_confidence,
               i.extraction_json,
               i.business_status,
               i.data_version
          FROM invoices i
          JOIN documents d ON d.id = i.document_id
         WHERE i.id = %s
        """,
        (invoice_id,),
    )
    row = cur.fetchone()
    if not row:
        return None

    cur.execute(
        """
        SELECT line_no, description, quantity, unit_price,
               discount_amount, amount
          FROM invoice_lines
         WHERE invoice_id = %s
         ORDER BY line_no ASC
        """,
        (invoice_id,),
    )
    lines = [
        InvoiceLineItem(
            line_no=ln[0],
            description=ln[1],
            quantity=str(ln[2]) if ln[2] is not None else None,
            unit_price=str(ln[3]) if ln[3] is not None else None,
            discount_amount=str(ln[4]) if ln[4] is not None else None,
            amount=str(ln[5]) if ln[5] is not None else None,
        )
        for ln in cur.fetchall()
    ]

    cur.execute(
        """
        SELECT tax_type, rate, base, amount
          FROM invoice_taxes
         WHERE invoice_id = %s
        """,
        (invoice_id,),
    )
    taxes = [
        InvoiceTaxItem(
            type=tx[0],
            rate=str(tx[1]) if tx[1] is not None else None,
            base=str(tx[2]) if tx[2] is not None else None,
            amount=str(tx[3]) if tx[3] is not None else None,
        )
        for tx in cur.fetchall()
    ]

    cur.execute(
        """
        SELECT event_type, actor_type, actor_name,
               from_status, to_status, rule_ref, reason, occurred_at
          FROM invoice_events
         WHERE invoice_id = %s
         ORDER BY occurred_at ASC
        """,
        (invoice_id,),
    )
    events = [
        InvoiceEventItem(
            event_type=ev[0],
            actor_type=ev[1],
            actor_name=ev[2],
            from_status=ev[3],
            to_status=ev[4],
            rule_ref=ev[5],
            reason=ev[6],
            occurred_at=ev[7].isoformat() if ev[7] else None,
        )
        for ev in cur.fetchall()
    ]

    conf = float(row[19]) if row[19] is not None else None
    return InvoiceDetail(
        invoice_id=str(row[0]),
        document_id=str(row[1]),
        file_name=row[2],
        storage_ref=row[3],
        processing_status=row[4],
        duplicate_of_invoice_id=str(row[5]) if row[5] else None,
        supplier_nit_base=row[6],
        supplier_dv=row[7],
        supplier_name=row[8],
        prefix=row[9],
        number=row[10],
        issue_date=row[11].isoformat() if row[11] else None,
        currency=row[12],
        subtotal=str(row[13]) if row[13] is not None else None,
        total=str(row[14]) if row[14] is not None else None,
        retentions_total=str(row[15]) if row[15] is not None else None,
        net_payable=str(row[16]) if row[16] is not None else None,
        po_reference=row[17],
        extraction_method=row[18],
        extraction_confidence=conf,
        extraction_json=row[20],
        business_status=row[21],
        data_version=row[22],
        lines=lines,
        taxes=taxes,
        events=events,
    )


def record_initial_invoice(
    cur: psycopg.Cursor,
    *,
    document_id: UUID,
    invoice_id: UUID,
    original_name: str,
    content_type: str,
    storage_ref: str,
    sha256: str,
    size_bytes: int,
    batch_id: UUID | None,
) -> None:
    cur.execute(
        """
        INSERT INTO documents (
            id, file_name, mime_type, category, storage_ref,
            sha256, size_bytes, batch_id,
            processing_stage, processing_status
        )
        VALUES (
            %s, %s, %s, 'invoice', %s,
            %s, %s, %s, 'reception', 'pending'
        )
        """,
        (
            document_id,
            original_name,
            content_type,
            storage_ref,
            sha256,
            size_bytes,
            batch_id,
        ),
    )
    cur.execute(
        """
        INSERT INTO invoices (id, document_id, business_status)
        VALUES (%s, %s, %s)
        """,
        (invoice_id, document_id, InvoiceStatus.RECIBIDA.value),
    )
    cur.execute(
        """
        INSERT INTO invoice_events (
            invoice_id, document_id, event_type, actor_type,
            actor_name, from_status, to_status, payload
        )
        VALUES (
            %s, %s, 'INVOICE_RECEIVED', 'system',
            'api-upload', NULL, %s, %s::jsonb
        )
        """,
        (
            invoice_id,
            document_id,
            InvoiceStatus.RECIBIDA.value,
            json.dumps({"file_name": original_name, "sha256": sha256}),
        ),
    )


def get_invoice_business_and_duplicate_status(
    cur: psycopg.Cursor, invoice_id: UUID
) -> tuple[str, str | None]:
    cur.execute(
        """
        SELECT i.business_status, d.duplicate_of_invoice_id
          FROM invoices i
          JOIN documents d ON d.id = i.document_id
         WHERE i.id = %s
        """,
        (invoice_id,),
    )
    status_row = cur.fetchone()
    business_status = status_row[0] if status_row else InvoiceStatus.EXTRAIDA.value
    duplicate_of = str(status_row[1]) if status_row and status_row[1] else None
    return business_status, duplicate_of
