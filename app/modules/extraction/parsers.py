import re
from decimal import Decimal, InvalidOperation


def parse_cop_amount(raw: str | None) -> Decimal | None:
    """Convierte montos colombianos o estándar a Decimal sin usar float.

    Ejemplos soportados:
      - "$52.000.000"    -> Decimal("52000000.00")
      - "1.190.000,50"   -> Decimal("1190000.50")
      - "43697478.99"    -> Decimal("43697478.99")
      - "52,000,000.00"  -> Decimal("52000000.00")
    """
    if raw is None:
        return None
    cleaned = re.sub(r"[^\d,.-]", "", raw.strip())
    if not cleaned or cleaned in {"-", ".", ","}:
        return None

    has_dot = "." in cleaned
    has_comma = "," in cleaned

    if has_dot and has_comma:
        # El último separador define el decimal
        if cleaned.rfind(",") > cleaned.rfind("."):
            normalized = cleaned.replace(".", "").replace(",", ".")
        else:
            normalized = cleaned.replace(",", "")
    elif has_comma:
        parts = cleaned.split(",")
        # Si hay varios grupos de miles (1,000,000) vs decimal colombiano (1500,50)
        if len(parts) > 2 or (
            len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3
        ):
            normalized = cleaned.replace(",", "")
        else:
            normalized = cleaned.replace(",", ".")
    elif has_dot:
        parts = cleaned.split(".")
        # En Colombia "$52.000.000" o "$50.000" usa puntos como miles
        if len(parts) > 2 or (
            len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3
        ):
            normalized = cleaned.replace(".", "")
        else:
            normalized = cleaned
    else:
        normalized = cleaned

    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def parse_tax_rate(raw: str | None) -> Decimal | None:
    """Normaliza tasas de impuesto a fracción decimal ('19%' o '19' -> Decimal('0.19'))."""
    if raw is None:
        return None
    has_percent = "%" in raw
    cleaned = re.sub(r"[^\d,.-]", "", raw.strip()).replace(",", ".")
    if not cleaned:
        return None
    try:
        val = Decimal(cleaned)
    except InvalidOperation:
        return None
    if has_percent or val > Decimal(1):
        val = val / Decimal(100)
    return val.quantize(Decimal("0.0001"))


def split_nit(nit_raw: str | None) -> tuple[str | None, str | None]:
    """Separa NIT base y dígito de verificación ('900.654.321-0' -> ('900654321', '0'))."""
    if not nit_raw:
        return None, None
    cleaned = nit_raw.strip()
    if "-" in cleaned:
        left, right = cleaned.rsplit("-", 1)
        base_digits = re.sub(r"\D", "", left)
        dv_digits = re.sub(r"\D", "", right)
        return (base_digits or None), (dv_digits[:1] if dv_digits else None)

    digits = re.sub(r"\D", "", cleaned)
    return (digits or None), None


def split_invoice_number(raw: str | None) -> tuple[str | None, str | None]:
    """Separa prefijo y consecutivo ('FE-9001' -> ('FE', '9001'))."""
    if not raw:
        return None, None
    cleaned = raw.strip()
    match = re.match(r"^([A-Za-z]+)[-\s]*(\d+)$", cleaned)
    if match:
        return match.group(1).upper(), match.group(2)
    if cleaned.isdigit():
        return None, cleaned
    return None, None
