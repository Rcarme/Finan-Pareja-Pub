"""Lógica de dominio pura para cuotas y refuerzos del departamento (RN-27 a RN-31).

Sin efectos secundarios ni I/O. Cobertura al 100% obligatoria.
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class ProvisionCalculation:
    """Resultado del cálculo de provisión mensual sugerida para un refuerzo (RN-30)."""

    target_usd: Decimal
    fx_rate: Decimal
    target_ars: Decimal
    current_balance: Decimal
    cierres_restantes: int
    needed_amount: Decimal
    monthly_suggested: Decimal

    @property
    def suggested_ars(self) -> Decimal:
        """Alias del aporte mensual sugerido en pesos (H-04)."""
        return self.monthly_suggested


@dataclass(frozen=True)
class ScheduleItemDefinition:
    """Definición pura de un vencimiento del cronograma anual (RN-27)."""

    due_period: date
    kind: str
    usd_amount: Decimal


def calculate_effective_fx_rate(ars_amount: Decimal, usd_amount: Decimal) -> Decimal:
    """Calcula la cotización efectiva en pesos por dólar a 4 decimales (RN-28).

    fx_rate_effective = ars / usd (ROUND_HALF_UP).
    """
    if usd_amount <= Decimal("0.00"):
        raise ValueError("El monto en USD debe ser mayor a cero.")
    if ars_amount <= Decimal("0.00"):
        raise ValueError("El monto en ARS debe ser mayor a cero.")

    rate = ars_amount / usd_amount
    return rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def calculate_reinforcement_provision(
    target_usd: Decimal,
    fx_rate: Decimal,
    current_balance: Decimal,
    current_period: date,
    due_period: date,
) -> ProvisionCalculation:
    """Calcula la provisión mensual sugerida para el próximo refuerzo (RN-30).

    Objetivo en ARS = USD del próximo refuerzo * cotización de referencia vigente.
    cierres_restantes = cantidad de períodos desde el actual hasta el mes
    anterior al vencimiento (mínimo 1).
    sugerencia = max(0, objetivo - saldo_sobre) / cierres_restantes.
    """
    target_ars = (target_usd * fx_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    months_diff = (due_period.year - current_period.year) * 12 + (
        due_period.month - current_period.month
    )
    cierres_restantes = max(1, months_diff)

    needed_amount = max(Decimal("0.00"), target_ars - current_balance)
    monthly_suggested = (needed_amount / Decimal(cierres_restantes)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    return ProvisionCalculation(
        target_usd=target_usd,
        fx_rate=fx_rate,
        target_ars=target_ars,
        current_balance=current_balance,
        cierres_restantes=cierres_restantes,
        needed_amount=needed_amount,
        monthly_suggested=monthly_suggested,
    )


def generate_annual_schedule_items(
    year: int,
    monthly_usd: Decimal = Decimal("1800.00"),
    july_reinforcement: Decimal = Decimal("1500.00"),
    december_reinforcement: Decimal = Decimal("2500.00"),
) -> list[ScheduleItemDefinition]:
    """Genera los 14 ítems del cronograma anual: 12 cuotas mensuales y 2 refuerzos (RN-27)."""
    items: list[ScheduleItemDefinition] = []

    for month in range(1, 13):
        due_period = date(year, month, 1)
        items.append(
            ScheduleItemDefinition(
                due_period=due_period,
                kind="cuota",
                usd_amount=monthly_usd,
            )
        )
        if month == 7 and july_reinforcement > Decimal("0.00"):
            items.append(
                ScheduleItemDefinition(
                    due_period=due_period,
                    kind="refuerzo",
                    usd_amount=july_reinforcement,
                )
            )
        elif month == 12 and december_reinforcement > Decimal("0.00"):
            items.append(
                ScheduleItemDefinition(
                    due_period=due_period,
                    kind="refuerzo",
                    usd_amount=december_reinforcement,
                )
            )

    return items
