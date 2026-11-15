"""Formateadores y parseadores para moneda y fechas en formato es-AR (RN-01, RN-02, AGENTS.md)."""

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from app.core.config import settings

CENT_EXPONENT = Decimal("0.01")
FX_EXPONENT = Decimal("0.0001")
_STRICT_DECIMAL_RE = re.compile(
    r"([+-]?)(?:\$\s?)?(?:(\d+(?:,\d{1,2})?)|(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?))"
)


def today_ar() -> date:
    """Retorna la fecha actual en zona horaria Argentina (America/Argentina/Buenos_Aires, M-07)."""
    tz = ZoneInfo(settings.APP_TIMEZONE)
    return datetime.now(tz).date()


def format_decimal_ar(amount: Decimal | str | None) -> str:
    """Formatea un Decimal a 2 decimales con separador es-AR sin símbolo $ (ej. 1.800,00)."""
    if amount is None:
        return "0,00"
    if isinstance(amount, str):
        try:
            amount = Decimal(amount)
        except Exception:
            return "0,00"
    quantized = amount.quantize(CENT_EXPONENT, rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    abs_amount = abs(quantized)
    int_part = int(abs_amount)
    cents_int = int((abs_amount - Decimal(int_part)) * Decimal(100))
    int_formatted = f"{int_part:,}".replace(",", ".")
    return f"{sign}{int_formatted},{cents_int:02d}"


def format_fx_ar(rate: Decimal | str | None) -> str:
    """Formatea una cotización Decimal a 4 decimales en formato es-AR (ej. 1.500,0000)."""
    if rate is None:
        return "0,0000"
    if isinstance(rate, str):
        try:
            rate = Decimal(rate)
        except Exception:
            return "0,0000"
    quantized = rate.quantize(FX_EXPONENT, rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    abs_rate = abs(quantized)
    int_part = int(abs_rate)
    frac_int = int((abs_rate - Decimal(int_part)) * Decimal(10000))
    int_formatted = f"{int_part:,}".replace(",", ".")
    return f"{sign}{int_formatted},{frac_int:04d}"


def format_currency(amount: Decimal | str | None) -> str:
    """Formatea un monto Decimal al formato rioplatense: $ 1.234.567,89.

    Reglas:
    - RN-01: Prohibido float, todo monto es Decimal.
    - RN-02: Redondeo ROUND_HALF_UP a dos decimales.
    - Formato: signo pesos, espacio, separador de miles con punto y decimales con coma.
    """
    if amount is None:
        return "$ 0,00"
    if isinstance(amount, str):
        try:
            amount = Decimal(amount)
        except Exception:
            return "$ 0,00"

    quantized = amount.quantize(CENT_EXPONENT, rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    abs_amount = abs(quantized)

    int_part = int(abs_amount)
    cents_decimal = (abs_amount - Decimal(int_part)) * Decimal(100)
    cents_int = int(cents_decimal)

    int_formatted = f"{int_part:,}".replace(",", ".")
    cents_formatted = f"{cents_int:02d}"

    return f"{sign}$ {int_formatted},{cents_formatted}"


def format_date(dt: date | datetime) -> str:
    """Formatea una fecha o datetime al formato dd/mm/aaaa."""
    return dt.strftime("%d/%m/%Y")


def parse_decimal(value: str) -> Decimal:
    """Parsea un string en formato rioplatense estricto a Decimal (H-15).

    Acepta enteros o decimales con coma (1 o 2 dígitos) y puntos de miles opcionales
    de a 3 dígitos, con signo y símbolo $ opcionales.
    Rechaza valores que excedan el rango de NUMERIC(14,2) (M-01).
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("El monto no puede estar vacío.")

    match = _STRICT_DECIMAL_RE.fullmatch(cleaned)
    if not match:
        raise ValueError("Formato de importe inválido; usá coma para decimales: 1.234,56")

    sign = match.group(1) or ""
    number_part = match.group(2) or match.group(3)
    normalized = f"{sign}{number_part.replace('.', '').replace(',', '.')}"
    dec = Decimal(normalized)

    max_numeric_14_2 = Decimal("999999999999.99")
    if dec.copy_abs() > max_numeric_14_2:
        raise ValueError(
            "El monto supera el límite contable máximo permitido (999.999.999.999,99)."
        )
    return dec.quantize(CENT_EXPONENT, rounding=ROUND_HALF_UP)


parse_es_ar_decimal = parse_decimal


def parse_fx_rate(value: str) -> Decimal:
    """Parsea una cotización en formato es-AR o estándar a Decimal con 4 decimales (M-01)."""
    cleaned = value.strip().replace("$", "").replace(" ", "")
    if not cleaned:
        raise ValueError("La cotización no puede estar vacía.")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "." in cleaned:
        parts = cleaned.split(".")
        if len(parts) == 2 and len(parts[1]) in (1, 2, 4):
            pass
        else:
            cleaned = cleaned.replace(".", "")
    try:
        dec = Decimal(cleaned).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    except Exception as exc:
        raise ValueError(f"Formato de cotización inválido: {value}") from exc

    max_fx = Decimal("99999999.9999")
    if not dec.is_finite():
        raise ValueError("El valor de cotización debe ser finito.")
    if abs(dec) > max_fx:
        raise ValueError("La cotización supera el límite máximo permitido (99.999.999,9999).")
    return dec


def parse_period(value: str) -> date:
    """Normaliza YYYY-MM o una fecha ISO válida al primer día del mes (RN-18)."""
    try:
        cleaned = value.strip()
        parsed = date.fromisoformat(cleaned + "-01" if len(cleaned) == 7 else cleaned)
        return date(parsed.year, parsed.month, 1)
    except ValueError as exc:
        raise ValueError("El período debe tener formato AAAA-MM y un mes válido.") from exc
