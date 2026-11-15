"""Pure domain logic for envelopes, savings, and financial goals.

Reglas asociadas: RN-12 a RN-17, RN-44, RN-45.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT_EXPONENT = Decimal("0.01")


@dataclass(frozen=True)
class EnvelopeMovementItem:
    """Represents an envelope movement for pure domain calculations."""

    kind: str  # allocate, release, spend
    amount: Decimal
    envelope_id: int
    status: str = "active"
    period: date | None = None


def compute_envelope_balance(
    envelope_id: int,
    movements: Sequence[EnvelopeMovementItem],
    period_cutoff: date | None = None,
) -> Decimal:
    """Calcula el saldo de un sobre considerando movimientos activos (RN-12)."""
    balance = Decimal("0.00")
    for m in movements:
        if m.status != "active":
            continue

        if period_cutoff is not None and m.period is not None and m.period > period_cutoff:
            continue

        if m.envelope_id == envelope_id:
            if m.kind == "allocate":
                balance += m.amount
            elif m.kind in ("release", "spend"):
                balance -= m.amount

    return balance


def validate_envelope_non_negative(current_balance: Decimal, deduction_amount: Decimal) -> None:
    """Valida que una liberación o gasto no deje el sobre con saldo negativo (RN-15)."""
    if current_balance - deduction_amount < Decimal("0.00"):
        raise ValueError("El sobre no puede quedar con saldo negativo.")


def validate_envelope_cutoffs(
    balances: Mapping[date, Decimal], deductions: Mapping[date, Decimal]
) -> None:
    """RN-15: una deducción retroactiva debe preservar cada corte posterior."""
    for cutoff, balance in balances.items():
        cumulative = sum(
            (amount for period, amount in deductions.items() if period <= cutoff),
            Decimal("0.00"),
        )
        validate_envelope_non_negative(balance, cumulative)


def compute_available_fund(
    total_fund: Decimal,
    envelope_balances: Sequence[Decimal],
) -> Decimal:
    """Calcula el Disponible = Fondo - suma(sobres).

    Puede ser negativo con alerta (RN-12, RN-16).
    """
    return total_fund - sum(envelope_balances, Decimal("0.00"))


def calculate_goal_progress(target_amount: Decimal, current_balance: Decimal) -> Decimal:
    """Calcula el porcentaje de progreso de una meta financiera (0 % a 100+ %)."""
    if target_amount <= Decimal("0.00"):
        return Decimal("0.00")
    progress = (current_balance / target_amount) * Decimal(100)
    return progress.quantize(CENT_EXPONENT, rounding=ROUND_HALF_UP)


def calculate_monthly_goal_contribution(
    target_amount: Decimal,
    current_balance: Decimal,
    current_period: date,
    target_date: date,
) -> Decimal:
    """Calcula el aporte mensual sugerido = (objetivo - saldo) / meses restantes (RN-17)."""
    remaining = max(Decimal("0.00"), target_amount - current_balance)
    months = (
        (target_date.year - current_period.year) * 12
        + (target_date.month - current_period.month)
        + 1
    )
    if months < 1:
        months = 1
    return (remaining / Decimal(months)).quantize(CENT_EXPONENT, rounding=ROUND_HALF_UP)


def validate_savings_redistribution(
    total_savings: Decimal,
    allocations: Mapping[int, Decimal],
) -> None:
    """Valida que la redistribución de ahorro sume de forma exacta el total disponible (RN-44)."""
    allocations_sum = sum(allocations.values(), Decimal("0.00"))
    if allocations_sum != total_savings:
        raise ValueError("La suma de las asignaciones debe coincidir exacta con el ahorro.")
