import json
from enum import StrEnum
from uuid import UUID

import psycopg


class InvoiceStatus(StrEnum):
    RECIBIDA = "RECIBIDA"
    EXTRAIDA = "EXTRAIDA"
    VALIDADA = "VALIDADA"
    EXCEPCION = "EXCEPCION"
    PROPUESTA = "PROPUESTA"
    APROBADA = "APROBADA"
    RECHAZADA = "RECHAZADA"


# Mapa exacto de transiciones permitidas según el diagrama del tutor
ALLOWED_TRANSITIONS: dict[InvoiceStatus, set[InvoiceStatus]] = {
    InvoiceStatus.RECIBIDA: {
        InvoiceStatus.EXTRAIDA,
        InvoiceStatus.EXCEPCION,
    },
    InvoiceStatus.EXTRAIDA: {
        InvoiceStatus.VALIDADA,
        InvoiceStatus.EXCEPCION,
    },
    InvoiceStatus.VALIDADA: {
        InvoiceStatus.PROPUESTA,
        InvoiceStatus.EXCEPCION,
    },
    InvoiceStatus.PROPUESTA: {
        InvoiceStatus.APROBADA,
        InvoiceStatus.EXCEPCION,
    },
    InvoiceStatus.EXCEPCION: {
        InvoiceStatus.VALIDADA,   # Vía acción humana: (corregida) + revalidación
        InvoiceStatus.RECHAZADA,  # Vía acción humana: rechazo definitivo
    },
    InvoiceStatus.APROBADA: set(),   # Estado terminal
    InvoiceStatus.RECHAZADA: set(),  # Estado terminal
}


class InvalidStateTransitionError(ValueError):
    pass


def transition_invoice_status(
    cur: psycopg.Cursor,
    *,
    invoice_id: UUID,
    to_status: InvoiceStatus,
    event_type: str,
    actor_type: str = "system",  # "system" | "ai" | "human"
    actor_name: str | None = None,
    rule_ref: str | None = None,
    reason: str | None = None,
    payload: dict | None = None,
    increment_version: bool = False,
) -> InvoiceStatus:
    """Valida la transición, actualiza invoices.business_status y registra el evento."""
    cur.execute(
        "SELECT business_status, document_id FROM invoices WHERE id = %s FOR UPDATE",
        (invoice_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Factura no encontrada: {invoice_id}")

    current_status = InvoiceStatus(row[0])
    document_id = row[1]

    if to_status not in ALLOWED_TRANSITIONS[current_status]:
        raise InvalidStateTransitionError(
            f"Transición no permitida: {current_status} -> {to_status}"
        )

    if increment_version:
        cur.execute(
            """
            UPDATE invoices
               SET business_status = %s,
                   data_version = data_version + 1,
                   updated_at = now()
             WHERE id = %s
            """,
            (to_status.value, invoice_id),
        )
    else:
        cur.execute(
            """
            UPDATE invoices
               SET business_status = %s,
                   updated_at = now()
             WHERE id = %s
            """,
            (to_status.value, invoice_id),
        )

    cur.execute(
        """
        INSERT INTO invoice_events (
            invoice_id, document_id, event_type, actor_type, actor_name,
            from_status, to_status, rule_ref, reason, payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            invoice_id,
            document_id,
            event_type,
            actor_type,
            actor_name,
            current_status.value,
            to_status.value,
            rule_ref,
            reason,
            json.dumps(payload) if payload is not None else None,
        ),
    )

    return to_status