import unicodedata
from collections.abc import Iterable
from uuid import UUID

import psycopg
from rapidfuzz import fuzz

from app.schemas.matching import (
    ContractMatch,
    InvoiceMatchingResponse,
    MatchingCandidate,
    MatchingStatus,
    PurchaseOrderMatch,
    SupplierMatch,
    SupportDocumentMatch,
)

from .repository import (
    ContractRecord,
    InvoiceMatchSource,
    PurchaseOrderRecord,
    SupplierRecord,
    find_active_contracts,
    find_contract_by_id,
    find_purchase_order,
    find_supplier_by_id,
    find_supplier_by_nit,
    get_invoice_match_source,
    list_suppliers,
    list_support_documents,
    persist_matching_result,
)

SUPPLIER_FUZZY_MIN_SCORE = 0.85
SUPPLIER_FUZZY_MIN_MARGIN = 0.05


class InvoiceNotFoundError(LookupError):
    """La factura solicitada no existe en la base de datos."""


def _normalise_text(value: str | None) -> str:
    if not value:
        return ""
    without_accents = unicodedata.normalize("NFKD", value)
    return "".join(
        char for char in without_accents if not unicodedata.combining(char)
    ).upper().strip()


def _normalise_identifier(value: str | None) -> str | None:
    normalised = _normalise_text(value)
    return normalised or None


def _candidate(
    *,
    entity: str,
    candidate_id: UUID,
    label: str,
    score: float | None,
    reason: str,
) -> MatchingCandidate:
    return MatchingCandidate(
        entity=entity,
        candidate_id=str(candidate_id),
        label=label,
        score=round(score, 4) if score is not None else None,
        reason=reason,
    )


def _supplier_candidate_scores(
    supplier_name: str, suppliers: Iterable[SupplierRecord]
) -> list[tuple[SupplierRecord, float]]:
    query = _normalise_text(supplier_name)
    scored = [
        (
            supplier,
            fuzz.token_set_ratio(query, _normalise_text(supplier.legal_name)) / 100,
        )
        for supplier in suppliers
    ]
    return sorted(scored, key=lambda item: item[1], reverse=True)


def _match_supplier(
    cur: psycopg.Cursor,
    source: InvoiceMatchSource,
    candidates: list[MatchingCandidate],
) -> SupplierRecord | None:
    if source.supplier_nit_base:
        exact_supplier = find_supplier_by_nit(cur, source.supplier_nit_base)
        if exact_supplier:
            return exact_supplier

    if not source.supplier_name:
        return None

    scored = _supplier_candidate_scores(source.supplier_name, list_suppliers(cur))
    if not scored or scored[0][1] < SUPPLIER_FUZZY_MIN_SCORE:
        return None

    top_supplier, top_score = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else None
    is_ambiguous = (
        second_score is not None
        and top_score - second_score < SUPPLIER_FUZZY_MIN_MARGIN
    )
    if is_ambiguous:
        for supplier, score in scored[:5]:
            if score < SUPPLIER_FUZZY_MIN_SCORE:
                break
            candidates.append(
                _candidate(
                    entity="supplier",
                    candidate_id=supplier.supplier_id,
                    label=supplier.legal_name,
                    score=score,
                    reason="Coincidencia aproximada de nombre ambigua.",
                )
            )
        return None

    return top_supplier


def _supplier_output(supplier: SupplierRecord | None) -> SupplierMatch:
    if not supplier:
        return SupplierMatch()
    return SupplierMatch(
        supplier_id=str(supplier.supplier_id),
        nit_base=supplier.nit_base,
        name=supplier.legal_name,
        matched=True,
    )


def _purchase_order_output(
    purchase_order: PurchaseOrderRecord | None,
) -> PurchaseOrderMatch:
    if not purchase_order:
        return PurchaseOrderMatch()
    return PurchaseOrderMatch(
        purchase_order_id=str(purchase_order.purchase_order_id),
        order_number=purchase_order.order_number,
        matched=True,
    )


def _contract_output(contract: ContractRecord | None) -> ContractMatch:
    if not contract:
        return ContractMatch()
    return ContractMatch(
        contract_id=str(contract.contract_id),
        contract_number=contract.contract_number,
        matched=True,
    )


def _build_matching_result(
    cur: psycopg.Cursor, source: InvoiceMatchSource
) -> tuple[InvoiceMatchingResponse, UUID | None, UUID | None, UUID | None]:
    candidates: list[MatchingCandidate] = []
    supplier = _match_supplier(cur, source, candidates)

    purchase_order: PurchaseOrderRecord | None = None
    order_reference = _normalise_identifier(source.po_reference)
    if order_reference:
        potential_order = find_purchase_order(cur, order_reference)
        if potential_order:
            supplier_is_ambiguous = any(
                candidate.entity == "supplier" for candidate in candidates
            )
            if supplier and supplier.supplier_id != potential_order.supplier_id:
                candidates.append(
                    _candidate(
                        entity="purchase_order",
                        candidate_id=potential_order.purchase_order_id,
                        label=potential_order.order_number,
                        score=1.0,
                        reason="La orden existe, pero pertenece a otro proveedor.",
                    )
                )
            elif source.supplier_nit_base and not supplier:
                candidates.append(
                    _candidate(
                        entity="purchase_order",
                        candidate_id=potential_order.purchase_order_id,
                        label=potential_order.order_number,
                        score=1.0,
                        reason="No se pudo verificar el proveedor por NIT.",
                    )
                )
            elif supplier_is_ambiguous:
                candidates.append(
                    _candidate(
                        entity="purchase_order",
                        candidate_id=potential_order.purchase_order_id,
                        label=potential_order.order_number,
                        score=1.0,
                        reason="La asociación depende de un proveedor ambiguo.",
                    )
                )
            else:
                purchase_order = potential_order
                if supplier is None:
                    supplier = find_supplier_by_id(cur, potential_order.supplier_id)

    contract: ContractRecord | None = None
    if purchase_order and purchase_order.contract_id:
        potential_contract = find_contract_by_id(cur, purchase_order.contract_id)
        if potential_contract and (
            supplier is None or potential_contract.supplier_id == supplier.supplier_id
        ):
            contract = potential_contract
        elif potential_contract:
            candidates.append(
                _candidate(
                    entity="contract",
                    candidate_id=potential_contract.contract_id,
                    label=potential_contract.contract_number,
                    score=1.0,
                    reason="El contrato de la orden pertenece a otro proveedor.",
                )
            )
    elif supplier:
        active_contracts = find_active_contracts(
            cur, supplier.supplier_id, source.issue_date
        )
        if len(active_contracts) == 1:
            contract = active_contracts[0]
        elif len(active_contracts) > 1:
            for active_contract in active_contracts:
                candidates.append(
                    _candidate(
                        entity="contract",
                        candidate_id=active_contract.contract_id,
                        label=active_contract.contract_number,
                        score=1.0,
                        reason="Hay varios contratos vigentes para el proveedor.",
                    )
                )

    support_documents = []
    if purchase_order:
        support_documents = list_support_documents(
            cur, purchase_order.purchase_order_id
        )

    missing_documents: list[str] = []
    if supplier is None:
        missing_documents.append("supplier")
    if purchase_order is None:
        missing_documents.append("purchase_order")
    if contract is None:
        missing_documents.append("contract")
    if not support_documents:
        missing_documents.append("support_documents")

    if candidates:
        matching_status = MatchingStatus.AMBIGUOUS
    elif missing_documents:
        matching_status = MatchingStatus.PARTIAL
    else:
        matching_status = MatchingStatus.COMPLETED

    result = InvoiceMatchingResponse(
        invoice_id=source.invoice_id,
        matching_status=matching_status,
        supplier=_supplier_output(supplier),
        purchase_order=_purchase_order_output(purchase_order),
        contract=_contract_output(contract),
        support_documents=[
            SupportDocumentMatch(
                support_document_id=str(support.support_document_id),
                number=support.number,
                type=support.support_type,
            )
            for support in support_documents
        ],
        missing_documents=missing_documents,
        candidates=candidates,
    )
    return (
        result,
        supplier.supplier_id if supplier else None,
        purchase_order.purchase_order_id if purchase_order else None,
        contract.contract_id if contract else None,
    )


def match_invoice_from_db(
    cur: psycopg.Cursor, invoice_id: UUID
) -> InvoiceMatchingResponse:
    """Ejecuta la Etapa 3, persiste las asociaciones y devuelve su contrato."""
    source = get_invoice_match_source(cur, invoice_id)
    if source is None:
        raise InvoiceNotFoundError(f"Factura no encontrada: {invoice_id}")

    result, supplier_id, purchase_order_id, contract_id = _build_matching_result(
        cur, source
    )
    persist_matching_result(
        cur,
        invoice_id=source.invoice_id,
        document_id=source.document_id,
        result=result,
        supplier_id=supplier_id,
        purchase_order_id=purchase_order_id,
        contract_id=contract_id,
    )
    return result
