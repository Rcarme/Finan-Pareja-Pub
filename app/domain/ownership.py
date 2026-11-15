"""Pure ownership rules: allocation is independent from physical location (F18)."""

from collections.abc import Iterable
from datetime import date
from decimal import Decimal


def validate_activity(value: str) -> str:
    if value not in {"household", "business", "personal"}:
        raise ValueError("La actividad debe ser hogar, negocio o personal.")
    return value


def validate_currency(value: str) -> str:
    if len(value) != 3 or not value.isalpha():
        raise ValueError("La moneda debe ser un código alfabético de 3 letras (ej. ARS, USD, EUR).")
    return value.upper()


def common_fund(
    physical_balances: Iterable[Decimal], personal_balances: Iterable[Decimal]
) -> Decimal:
    return sum(physical_balances, Decimal("0.00")) - sum(personal_balances, Decimal("0.00"))


def personal_balance(
    opening: Decimal, allocations: Iterable[Decimal], spending: Decimal
) -> Decimal:
    return opening + sum(allocations, Decimal("0.00")) - spending


def validate_money(value: Decimal, *, nonnegative: bool = False) -> None:
    if (
        not value.is_finite()
        or abs(value) > Decimal("999999999999.99")
        or value != value.quantize(Decimal("0.01"))
        or (nonnegative and value < 0)
    ):
        raise ValueError(
            "El importe debe ser finito, tener hasta dos decimales y respetar el límite contable."
        )


def credit_correction_delta(
    current: Decimal, debt: Decimal, reason: str, has_history: bool
) -> Decimal:
    """A debt photo is negative; preserve the original photo via its signed difference."""
    validate_money(current)
    validate_money(debt, nonnegative=True)
    if not reason.strip():
        raise ValueError("Indicá el motivo de la corrección.")
    if has_history:
        raise ValueError(
            "Esta cuenta tiene movimientos, resúmenes o ajustes: requiere revisión individual."
        )
    delta = -debt - current
    validate_money(delta)
    return delta


def is_historical_opening_charge(period: date, tx_date: date, opening_date: date) -> bool:
    """Only explicitly reviewed earlier months can be settled through this repair."""
    month = opening_date.replace(day=1)
    return tx_date <= opening_date and (period < month or tx_date < month)
