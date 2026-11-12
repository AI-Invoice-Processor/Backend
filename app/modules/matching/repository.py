import json
from dataclasses import dataclass
from datetime import date
from uuid import UUID

import psycopg

from app.schemas.matching import InvoiceMatchingResponse, MatchingStatus


@dataclass(frozen=True)
class InvoiceMatchSource:
    invoice_id: UUID
    document_id: UUID
    supplier_nit_base: str | None
    supplier_name: str | None
    po_reference: str | None
    issue_date: date | None


@dataclass(frozen=True)
class SupplierRecord:
    supplier_id: UUID
    nit_base: str
    dv: str
    legal_name: str


@dataclass(frozen=True)
class PurchaseOrderRecord:
    purchase_order_id: UUID
    order_number: str
    supplier_id: UUID
    contract_id: UUID | None


@dataclass(frozen=True)
class ContractRecord:
    contract_id: UUID
    contract_number: str
    supplier_id: UUID


@dataclass(frozen=True)
class SupportDocumentRecord:
    support_document_id: UUID
    number: str
    support_type: str


def get_invoice_match_source(
    cur: psycopg.Cursor, invoice_id: UUID
) -> InvoiceMatchSource | None:
    cur.execute(
        """
        SELECT id, document_id, supplier_nit_base, supplier_name,
               po_reference, issue_date
          FROM invoices
         WHERE id = %s
        """,
        (invoice_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return InvoiceMatchSource(
        invoice_id=row[0],
        document_id=row[1],
        supplier_nit_base=row[2],
        supplier_name=row[3],
        po_reference=row[4],
        issue_date=row[5],
    )


def _supplier_from_row(row: tuple) -> SupplierRecord:
    return SupplierRecord(
        supplier_id=row[0],
        nit_base=row[1],
        dv=row[2],
        legal_name=row[3],
    )


def find_supplier_by_nit(
    cur: psycopg.Cursor, nit_base: str
) -> SupplierRecord | None:
    cur.execute(
        """
        SELECT id, nit_base, dv, legal_name
          FROM suppliers
         WHERE nit_base = %s
        """,
        (nit_base,),
    )
    row = cur.fetchone()
    return _supplier_from_row(row) if row else None


def list_suppliers(cur: psycopg.Cursor) -> list[SupplierRecord]:
    cur.execute(
        """
        SELECT id, nit_base, dv, legal_name
          FROM suppliers
         ORDER BY legal_name, nit_base
        """
    )
    return [_supplier_from_row(row) for row in cur.fetchall()]


def find_supplier_by_id(
    cur: psycopg.Cursor, supplier_id: UUID
) -> SupplierRecord | None:
    cur.execute(
        """
        SELECT id, nit_base, dv, legal_name
          FROM suppliers
         WHERE id = %s
        """,
        (supplier_id,),
    )
    row = cur.fetchone()
    return _supplier_from_row(row) if row else None


def find_purchase_order(
    cur: psycopg.Cursor, order_number: str
) -> PurchaseOrderRecord | None:
    cur.execute(
        """
        SELECT id, order_number, supplier_id, contract_id
          FROM purchase_orders
         WHERE UPPER(order_number) = UPPER(%s)
        """,
        (order_number,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return PurchaseOrderRecord(
        purchase_order_id=row[0],
        order_number=row[1],
        supplier_id=row[2],
        contract_id=row[3],
    )


def find_contract_by_id(
    cur: psycopg.Cursor, contract_id: UUID
) -> ContractRecord | None:
    cur.execute(
        """
        SELECT id, contract_number, supplier_id
          FROM contracts
         WHERE id = %s
        """,
        (contract_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return ContractRecord(
        contract_id=row[0],
        contract_number=row[1],
        supplier_id=row[2],
    )


def find_active_contracts(
    cur: psycopg.Cursor, supplier_id: UUID, issue_date: date | None
) -> list[ContractRecord]:
    if issue_date is None:
        cur.execute(
            """
            SELECT id, contract_number, supplier_id
              FROM contracts
             WHERE supplier_id = %s
             ORDER BY start_date, contract_number
            """,
            (supplier_id,),
        )
    else:
        cur.execute(
            """
            SELECT id, contract_number, supplier_id
              FROM contracts
             WHERE supplier_id = %s
               AND start_date <= %s
               AND end_date >= %s
             ORDER BY start_date, contract_number
            """,
            (supplier_id, issue_date, issue_date),
        )
    return [
        ContractRecord(
            contract_id=row[0],
            contract_number=row[1],
            supplier_id=row[2],
        )
        for row in cur.fetchall()
    ]


def list_support_documents(
    cur: psycopg.Cursor, purchase_order_id: UUID
) -> list[SupportDocumentRecord]:
    cur.execute(
        """
        SELECT id, number, support_type
          FROM support_documents
         WHERE purchase_order_id = %s
         ORDER BY received_date, number
        """,
        (purchase_order_id,),
    )
    return [
        SupportDocumentRecord(
            support_document_id=row[0],
            number=row[1],
            support_type=row[2],
        )
        for row in cur.fetchall()
    ]


def persist_matching_result(
    cur: psycopg.Cursor,
    *,
    invoice_id: UUID,
    document_id: UUID,
    result: InvoiceMatchingResponse,
    supplier_id: UUID | None,
    purchase_order_id: UUID | None,
    contract_id: UUID | None,
) -> None:
    if result.matching_status == MatchingStatus.COMPLETED:
        processing_status = "success"
    elif result.matching_status == MatchingStatus.FAILED:
        processing_status = "failed"
    else:
        processing_status = "partial"
    cur.execute(
        """
        UPDATE invoices
           SET supplier_id = %s,
               purchase_order_id = %s,
               contract_id = %s,
               match_status = %s,
               match_detail = %s::jsonb,
               updated_at = now()
         WHERE id = %s
        """,
        (
            supplier_id,
            purchase_order_id,
            contract_id,
            result.matching_status.value,
            json.dumps(result.model_dump(mode="json")),
            invoice_id,
        ),
    )
    cur.execute(
        """
        UPDATE documents
           SET processing_stage = 'matching',
               processing_status = %s,
               error = NULL
         WHERE id = %s
        """,
        (processing_status, document_id),
    )
