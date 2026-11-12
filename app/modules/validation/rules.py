"""Pure deterministic rules. No clock, database or external services."""

import re
from decimal import Decimal, localcontext

from app.schemas.validation import RuleCheck


def missing(rule, reason="Datos insuficientes", code="MISSING_DATA", **kwargs):
    return RuleCheck(
        rule_id=rule, result="not_evaluated", reason=reason, reason_code=code, **kwargs
    )


def money(rule, expected, actual, config, **kwargs):
    if expected is None or actual is None:
        return missing(rule, **kwargs)
    with localcontext() as ctx:
        ctx.prec = 60
        quantum = Decimal(1).scaleb(-config.precision)
        expected = expected.quantize(quantum, rounding=config.rounding)
        actual = actual.quantize(quantum, rounding=config.rounding)
        difference = actual - expected
        pct = (
            (difference / abs(expected) * 100).quantize(
                Decimal("0.0001"), rounding=config.rounding
            )
            if expected
            else None
        )
    tolerance = (
        config.purchase_order_tolerance if rule == "D2" else config.arithmetic_tolerance
    )
    passed = abs(difference) <= tolerance
    return RuleCheck(
        rule_id=rule,
        result="passed" if passed else "failed",
        expected_amount=expected,
        actual_amount=actual,
        difference=difference,
        difference_pct=pct,
        tolerance=tolerance,
        reason=f"Esperado {expected}; declarado {actual}; diferencia {difference}.",
        reason_code="WITHIN_TOLERANCE" if passed else "AMOUNT_MISMATCH",
        **kwargs,
    )


def v1(data, config):
    checks = []
    for line in data.invoice.lines:
        discount = line.discount_amount
        if discount is None and line.line_no in data.known_no_discount_lines:
            discount = Decimal(0)
        expected = None
        if all(v is not None for v in (line.quantity, line.unit_price, discount)):
            with localcontext() as ctx:
                ctx.prec = 60
                expected = line.quantity * line.unit_price - discount
        checks.append(
            money("V1", expected, line.amount, config, invoice_line_no=line.line_no)
        )
    return checks or [missing("V1", "No hay líneas declaradas")]


def v2(data, config):
    lines = data.invoice.lines
    if not lines or any(line.amount is None for line in lines):
        return [missing("V2", "Líneas vacías o importes incompletos")]
    return [
        money(
            "V2",
            sum((line.amount for line in lines), Decimal(0)),
            data.invoice.subtotal,
            config,
        )
    ]


def v3(data, config):
    checks = []
    for index, tax in enumerate(data.invoice.taxes):
        if tax.type.upper() != "IVA":
            continue
        if tax.rate is None or tax.base is None or tax.amount is None:
            check = missing("V3", "IVA sin base, tarifa o importe")
        elif tax.rate not in config.vat_rates:
            check = missing("V3", "Tarifa IVA no soportada", "UNSUPPORTED_VAT_RATE")
        else:
            check = money("V3", tax.base * tax.rate, tax.amount, config)
        check.evidence_refs = [{"field": f"taxes[{index}]"}]
        checks.append(check)
    return checks or [missing("V3", "No hay IVA declarado")]


def v4(data, config):
    invoice = data.invoice
    if (
        invoice.subtotal is None
        or not invoice.taxes
        or any(t.type.upper() != "IVA" or t.amount is None for t in invoice.taxes)
    ):
        return [missing("V4", "Subtotal o impuestos incompletos/fuera del modelo IVA")]
    return [
        money(
            "V4",
            invoice.subtotal + sum((t.amount for t in invoice.taxes), Decimal(0)),
            invoice.total,
            config,
        )
    ]


def normalize_nit(value):
    if not value or not re.fullmatch(r"[0-9.\s]+", value):
        return None
    digits = re.sub(r"[.\s]", "", value)
    return digits if 1 <= len(digits) <= 15 else None


def v5(data, config):
    nit = normalize_nit(data.invoice.supplier.nit_base)
    if nit is None:
        return [missing("V5", "NIT ausente o formato inválido", "INVALID_NIT")]
    dv = data.invoice.supplier.dv
    if dv is None:
        return [missing("V5", "DV ausente", "MISSING_DV")]
    weights = (3, 7, 13, 17, 19, 23, 29, 37, 41, 43, 47, 53, 59, 67, 71)
    remainder = (
        sum(int(digit) * weight for digit, weight in zip(reversed(nit), weights)) % 11
    )
    expected = str(remainder if remainder < 2 else 11 - remainder)
    return [
        RuleCheck(
            rule_id="V5",
            result="passed" if dv == expected else "failed",
            expected_text=expected,
            actual_text=dv,
            reason="Comparación matemática del DV; no acredita existencia del proveedor.",
            reason_code="VALID_DV" if dv == expected else "INVALID_DV",
        )
    ]


def v6(data, config):
    invoice = data.invoice
    duplicates = set(data.duplicate_invoice_ids) - {data.invoice_id}
    reloads = set(data.same_document_invoice_ids) - {data.invoice_id}
    if duplicates or reloads:
        return [
            RuleCheck(
                rule_id="V6",
                result="failed",
                actual_text=",".join(sorted(map(str, duplicates | reloads))),
                reason="Recarga del mismo documento"
                if reloads
                else "Identidad NIT + prefijo + número duplicada",
                reason_code="DOCUMENT_REUPLOAD" if reloads else "DUPLICATE_IDENTITY",
            )
        ]
    if (
        not normalize_nit(invoice.supplier.nit_base)
        or not invoice.prefix
        or not invoice.number
    ):
        return [missing("V6", "Identidad incompleta para consultar duplicados")]
    return [
        RuleCheck(
            rule_id="V6",
            result="passed",
            reason="Sin otro registro con la misma identidad o documento",
            reason_code="NO_DUPLICATE",
        )
    ]


def v7(data, config):
    issued = data.invoice.issue_date
    if issued is None:
        return [missing("V7", "Fecha ausente")]
    if issued > data.reference_date:
        result, code, reason = "failed", "FUTURE_DATE", "Fecha futura"
    elif not config.open_periods:
        return [
            missing("V7", "No hay periodos abiertos configurados", "MISSING_PERIODS")
        ]
    elif not any(p.start <= issued <= p.end for p in config.open_periods):
        result, code, reason = (
            "failed",
            "CLOSED_PERIOD",
            "Fecha fuera de periodos abiertos",
        )
    else:
        result, code, reason = (
            "passed",
            "VALID_DATE",
            "Fecha no futura en periodo abierto",
        )
    return [
        RuleCheck(
            rule_id="V7",
            result=result,
            reason_code=code,
            reason=reason,
            actual_text=issued.isoformat(),
            expected_text=data.reference_date.isoformat(),
        )
    ]


def d2(data, config):
    if data.purchase_order is None:
        return [missing("D2", "No hay OC asociada", "MISSING_PURCHASE_ORDER")]
    return [money("D2", data.purchase_order.subtotal, data.invoice.subtotal, config)]


RULES = {"V1": v1, "V2": v2, "V3": v3, "V4": v4, "V5": v5, "V6": v6, "V7": v7, "D2": d2}
