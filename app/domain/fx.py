"""Cotización oficial de referencia: validación pura y promedio decimal (RN-29)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_RATE = Decimal("99999999.9999")


def _amount(value: object) -> Decimal:
    if not isinstance(value, (Decimal, str)):
        raise ValueError("La cotización debe ser un número decimal válido.")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("La cotización debe ser un número decimal válido.") from exc
    if not amount.is_finite() or not Decimal("0") < amount <= MAX_RATE:
        raise ValueError("La cotización está fuera del rango permitido.")
    return amount


@dataclass(frozen=True)
class OfficialQuote:
    """Compra y venta del oficial; referencia estimativa, no cotización de pago."""

    buy: Decimal
    sell: Decimal
    published_at: datetime

    @property
    def rate(self) -> Decimal:
        return ((self.buy + self.sell) / Decimal("2")).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )


def parse_official_quote(payload: Mapping[str, object], now: datetime) -> OfficialQuote:
    """Acepta únicamente oficial USD, valores decimales y una fecha con zona horaria."""
    if payload.get("moneda") != "USD" or payload.get("casa") != "oficial":
        raise ValueError("La respuesta no corresponde al dólar oficial USD.")
    buy = _amount(payload.get("compra"))
    sell = _amount(payload.get("venta"))
    if buy > sell:
        raise ValueError("La cotización de compra supera a la de venta.")
    raw_timestamp = payload.get("fechaActualizacion")
    if not isinstance(raw_timestamp, str):
        raise ValueError("La cotización no incluye una fecha válida.")
    try:
        published_at = datetime.fromisoformat(raw_timestamp)
    except ValueError as exc:
        raise ValueError("La fecha de cotización es inválida.") from exc
    if published_at.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("Las fechas de cotización requieren zona horaria.")
    if published_at > now + timedelta(minutes=5):
        raise ValueError("La cotización tiene una fecha futura inválida.")
    return OfficialQuote(buy=buy, sell=sell, published_at=published_at)
