"""Lógica de dominio pura para plantillas y pendientes recurrentes (RN-35).

Sin I/O ni dependencias de base de datos.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class RecurringTemplateInput:
    """Datos de una plantilla recurrente mensual (RN-35)."""

    kind: str
    name: str
    category_id: int
    account_id: int
    day_of_month: int
    estimated_amount: Decimal
    charged_to_user_id: int | None = None
    template_id: int | None = None
    active: bool = True


@dataclass(frozen=True)
class RecurringPendingCreation:
    """Pendiente a generar para el mes a partir de una plantilla (RN-35)."""

    pending_id: int | None
    template_id: int
    kind: str
    category_id: int
    account_id: int
    estimated_amount: Decimal
    charged_to_user_id: int | None
    period: date
    status: str = "pending"


def validate_recurring_template(tpl: RecurringTemplateInput) -> None:
    """Valida los campos de una plantilla recurrente según RN-35."""
    if tpl.kind not in ("income", "expense"):
        raise ValueError(
            f"Tipo de plantilla inválido: '{tpl.kind}'. Debe ser 'income' o 'expense'."
        )

    if tpl.day_of_month < 1 or tpl.day_of_month > 31:
        raise ValueError(f"Día del mes inválido: {tpl.day_of_month}. Debe estar entre 1 y 31.")

    if tpl.estimated_amount <= Decimal("0.00"):
        raise ValueError("Monto estimado debe ser mayor a cero.")

    if not tpl.name.strip():
        raise ValueError("El nombre de la plantilla es obligatorio.")
    if len(tpl.name.strip()) > 100:
        raise ValueError("El nombre de la plantilla no puede superar los 100 caracteres.")


def filter_templates_for_pending_generation(
    templates: list[RecurringTemplateInput],
    period: date,
    already_generated_template_ids: set[int],
) -> list[RecurringPendingCreation]:
    """Genera la lista pura de pendientes a instanciar para el período (RN-35).

    Solo considera plantillas activas que aún no tengan un pendiente registrado en el mes.
    """
    to_create: list[RecurringPendingCreation] = []
    normalized_period = date(period.year, period.month, 1)

    for tpl in templates:
        if not tpl.active:
            continue
        if tpl.template_id is None:
            continue
        if tpl.template_id in already_generated_template_ids:
            continue

        to_create.append(
            RecurringPendingCreation(
                pending_id=None,
                template_id=tpl.template_id,
                kind=tpl.kind,
                category_id=tpl.category_id,
                account_id=tpl.account_id,
                estimated_amount=tpl.estimated_amount,
                charged_to_user_id=tpl.charged_to_user_id,
                period=normalized_period,
                status="pending",
            )
        )

    return to_create


def validate_pending_confirmation(
    pending: RecurringPendingCreation,
    actual_amount: Decimal | None,
) -> Decimal:
    """Valida el monto al confirmar un pendiente recurrente (RN-35).

    Si no se especifica monto, se utiliza el monto estimado de la plantilla.
    """
    amount = actual_amount if actual_amount is not None else pending.estimated_amount
    if amount <= Decimal("0.00"):
        raise ValueError(f"El monto debe ser mayor a cero: {amount}")
    return amount
