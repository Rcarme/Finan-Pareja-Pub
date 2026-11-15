"""Lógica de dominio pura para el contrato de alquiler y recordatorios (RN-32 a RN-34).

Sin I/O ni dependencias de base de datos.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum


class RentAdjustAlertStatus(StrEnum):
    """Estado de la alerta de ajuste de alquiler en el inicio (RN-33)."""

    NONE = "none"
    DUE_SOON = "due_soon"
    OVERDUE = "overdue"


class RentCollectionAlertStatus(StrEnum):
    """Estado del cobro de alquiler del período en curso (RN-34)."""

    COLLECTED = "collected"
    IN_WINDOW = "in_window"
    MISSING_ALERT = "missing_alert"
    FUTURE = "future"


def calculate_next_adjust_date(last_adjust_date: date, adjust_every_months: int = 3) -> date:
    """Calcula la próxima fecha de ajuste de alquiler (RN-32).

    Suma la cantidad de meses especificada preservando el día o ajustando a fin de mes.
    """
    if adjust_every_months <= 0:
        raise ValueError(f"Frecuencia de ajuste inválida: {adjust_every_months}. Debe ser > 0.")

    total_months = last_adjust_date.year * 12 + (last_adjust_date.month - 1) + adjust_every_months
    new_year = total_months // 12
    new_month = total_months % 12 + 1
    # Mantener el día 1 u original
    return date(new_year, new_month, min(last_adjust_date.day, 28))


def evaluate_rent_adjust_reminder(next_adjust_date: date, today: date) -> RentAdjustAlertStatus:
    """Evalúa la necesidad de mostrar un banner recordatorio de ajuste (RN-33).

    - OVERDUE: Si hoy >= fecha del próximo ajuste.
    - DUE_SOON: Si faltan 7 días o menos para el ajuste.
    - NONE: Si faltan más de 7 días.
    """
    if today >= next_adjust_date:
        return RentAdjustAlertStatus.OVERDUE

    delta_days = (next_adjust_date - today).days
    if delta_days <= 7:
        return RentAdjustAlertStatus.DUE_SOON

    return RentAdjustAlertStatus.NONE


def evaluate_rent_collection_status(
    period: date, today: date, is_collected: bool
) -> RentCollectionAlertStatus:
    """Evalúa el estado de cobro de alquiler del período según RN-32 y RN-34.

    - COLLECTED: Ya registrado como cobrado en el período.
    - IN_WINDOW: Días 1 al 5 del período en curso sin cobro.
    - MISSING_ALERT: Pasado el día 6 sin cobro registrado en el período.
    - FUTURE: Período aún no alcanzado por la fecha actual.
    """
    if is_collected:
        return RentCollectionAlertStatus.COLLECTED

    period_start = date(period.year, period.month, 1)
    if today < period_start:
        return RentCollectionAlertStatus.FUTURE

    if today.year == period.year and today.month == period.month:
        if today.day <= 5:
            return RentCollectionAlertStatus.IN_WINDOW
        return RentCollectionAlertStatus.MISSING_ALERT

    return RentCollectionAlertStatus.MISSING_ALERT


def validate_rent_adjustment(
    old_amount: Decimal, new_amount: Decimal, ipc_pct: Decimal | None
) -> None:
    """Valida los datos de un ajuste de alquiler manual (RN-33)."""
    if old_amount <= Decimal("0.00"):
        raise ValueError("El monto anterior debe ser mayor a cero.")
    if new_amount <= Decimal("0.00"):
        raise ValueError("El monto nuevo debe ser mayor a cero.")
