"""Proyección mensual pura; referencias estimadas separadas de movimientos reales."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class ForecastEntry:
    period: date
    kind: str
    amount: Decimal
    label: str
    source: str
    estimated: bool
    currency: str = "ARS"


@dataclass(frozen=True)
class ForecastMonth:
    period: date
    registered_income: Decimal | None
    registered_expense: Decimal | None
    estimated_income: Decimal | None
    estimated_expense: Decimal | None
    unconverted_usd: Decimal
    entries: tuple[ForecastEntry, ...]


def shift_month(period: date, offset: int) -> date:
    index = period.year * 12 + period.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def _total(
    entries: Sequence[ForecastEntry], kind: str, estimated: bool, fx_rate: Decimal | None
) -> Decimal | None:
    total = Decimal("0.00")
    for entry in entries:
        if entry.kind != kind or entry.estimated != estimated:
            continue
        if entry.currency == "USD":
            if fx_rate is None:
                return None
            total += (entry.amount * fx_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            total += entry.amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return total


def build_forecast(
    start: date,
    entries: Sequence[ForecastEntry],
    fx_rate: Decimal | None = None,
    *,
    months: int = 12,
) -> list[ForecastMonth]:
    """USD sin cotización deja el total desconocido, conservando cada compromiso."""
    if months <= 0:
        raise ValueError("La cantidad de meses debe ser positiva.")
    if fx_rate is not None and (
        not isinstance(fx_rate, Decimal) or not fx_rate.is_finite() or fx_rate <= 0
    ):
        raise ValueError("La cotización debe ser un Decimal positivo y finito.")
    for entry in entries:
        if (
            not isinstance(entry.amount, Decimal)
            or not entry.amount.is_finite()
            or entry.amount < 0
        ):
            raise ValueError("El monto debe ser un Decimal no negativo y finito.")
        if entry.kind not in ("income", "expense"):
            raise ValueError("Tipo de proyección inválido.")
        if entry.currency not in ("ARS", "USD"):
            raise ValueError("Moneda de proyección inválida.")
    result: list[ForecastMonth] = []
    for offset in range(months):
        period = shift_month(start, offset)
        current = tuple(entry for entry in entries if entry.period.replace(day=1) == period)
        missing_usd = sum(
            (entry.amount for entry in current if entry.currency == "USD" and fx_rate is None),
            Decimal("0.00"),
        )
        result.append(
            ForecastMonth(
                period=period,
                registered_income=_total(current, "income", False, fx_rate),
                registered_expense=_total(current, "expense", False, fx_rate),
                estimated_income=_total(current, "income", True, fx_rate),
                estimated_expense=_total(current, "expense", True, fx_rate),
                unconverted_usd=missing_usd,
                entries=current,
            )
        )
    return result
