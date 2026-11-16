"""Servicio para plantillas y pendientes recurrentes mensuales (RN-35, RN-26, RN-37).

Gestiona:
- Creación, actualización y consulta de plantillas recurrentes.
- Generación mensual idempotente de pendientes.
- Confirmación en un toque que crea transacción contable sin commit prematuro.
- Descarte de pendientes sin impacto en el libro contable.
- Detección de pendientes no resueltos para advertencia previa al cierre de mes (RN-26).
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.transactions import atomic_service, commit_service
from app.domain.recurring import (
    RecurringPendingCreation,
    RecurringTemplateInput,
    filter_templates_for_pending_generation,
    validate_pending_confirmation,
    validate_recurring_template,
)
from app.models.auth import User
from app.models.ledger import Account, Transaction
from app.models.recurring import RecurringPending, RecurringTemplate
from app.services.category_service import canonical_category
from app.services.ledger_service import check_period_open, create_transaction


def _validate_template_references(db: Session, tpl_input: RecurringTemplateInput) -> int:
    account = db.get(Account, tpl_input.account_id)
    if account is None:
        raise ValueError("La cuenta debe existir y estar activa.")
    if account.is_virtual is True:
        raise ValueError("El fondo común es calculado; elegí una cuenta o tarjeta real.")
    if account.currency == "USD":
        raise ValueError("Esta cuenta está en USD; no admite movimientos expresados en pesos.")
    if not account.active:
        raise ValueError("La cuenta debe existir y estar activa.")

    category = canonical_category(db, tpl_input.category_id)
    if category is None or not category.active or category.kind != tpl_input.kind:
        raise ValueError(
            "La categoría debe ser compatible con el tipo de movimiento y estar activa."
        )

    if tpl_input.charged_to_user_id is not None:
        user = db.get(User, tpl_input.charged_to_user_id)
        if user is None or not user.active:
            raise ValueError("La persona debe existir y estar activa.")

    return category.id


@atomic_service
def create_recurring_template(
    db: Session,
    tpl_input: RecurringTemplateInput,
    user_id: int,
    commit: bool = True,
) -> RecurringTemplate:
    """Crea una plantilla recurrente mensual auditada (RN-35, RN-37)."""
    validate_recurring_template(tpl_input)
    canonical_cat_id = _validate_template_references(db, tpl_input)

    tpl = RecurringTemplate(
        kind=tpl_input.kind,
        name=tpl_input.name,
        category_id=canonical_cat_id,
        account_id=tpl_input.account_id,
        day_of_month=tpl_input.day_of_month,
        estimated_amount=tpl_input.estimated_amount,
        charged_to_user_id=tpl_input.charged_to_user_id,
        active=tpl_input.active,
    )
    db.add(tpl)
    db.flush()

    record_audit(
        db=db,
        entity="RecurringTemplate",
        entity_id=tpl.id,
        action="INSERT",
        before=None,
        after={
            "id": tpl.id,
            "kind": tpl.kind,
            "name": tpl.name,
            "category_id": tpl.category_id,
            "account_id": tpl.account_id,
            "day_of_month": tpl.day_of_month,
            "estimated_amount": str(tpl.estimated_amount),
            "charged_to_user_id": tpl.charged_to_user_id,
            "active": tpl.active,
        },
        user_id=user_id,
    )

    if commit:
        commit_service(db)

    return tpl


@atomic_service
def update_recurring_template(
    db: Session,
    template_id: int,
    tpl_input: RecurringTemplateInput,
    user_id: int,
    commit: bool = True,
) -> RecurringTemplate:
    """Actualiza una plantilla recurrente existente con auditoría (RN-35, RN-37)."""
    validate_recurring_template(tpl_input)

    tpl = db.execute(
        select(RecurringTemplate)
        .where(RecurringTemplate.id == template_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    if tpl is None:
        raise ValueError(f"Plantilla recurrente #{template_id} no encontrada.")

    canonical_cat_id = _validate_template_references(db, tpl_input)

    before: dict[str, Any] = {
        "id": tpl.id,
        "kind": tpl.kind,
        "name": tpl.name,
        "category_id": tpl.category_id,
        "account_id": tpl.account_id,
        "day_of_month": tpl.day_of_month,
        "estimated_amount": str(tpl.estimated_amount),
        "charged_to_user_id": tpl.charged_to_user_id,
        "active": tpl.active,
    }

    tpl.kind = tpl_input.kind
    tpl.name = tpl_input.name
    tpl.category_id = canonical_cat_id
    tpl.account_id = tpl_input.account_id
    tpl.day_of_month = tpl_input.day_of_month
    tpl.estimated_amount = tpl_input.estimated_amount
    tpl.charged_to_user_id = tpl_input.charged_to_user_id
    tpl.active = tpl_input.active

    after: dict[str, Any] = {
        "id": tpl.id,
        "kind": tpl.kind,
        "name": tpl.name,
        "category_id": tpl.category_id,
        "account_id": tpl.account_id,
        "day_of_month": tpl.day_of_month,
        "estimated_amount": str(tpl.estimated_amount),
        "charged_to_user_id": tpl.charged_to_user_id,
        "active": tpl.active,
    }

    record_audit(
        db=db,
        entity="RecurringTemplate",
        entity_id=tpl.id,
        action="UPDATE",
        before=before,
        after=after,
        user_id=user_id,
    )

    if commit:
        commit_service(db)

    return tpl


def get_all_recurring_templates(db: Session) -> list[RecurringTemplate]:
    """Obtiene todas las plantillas recurrentes ordenadas por día del mes."""
    return list(
        db.execute(
            select(RecurringTemplate).order_by(
                RecurringTemplate.day_of_month.asc(), RecurringTemplate.name.asc()
            )
        )
        .scalars()
        .all()
    )


def get_recurring_template(db: Session, template_id: int) -> RecurringTemplate | None:
    """Obtiene una plantilla recurrente por su ID."""
    return db.get(RecurringTemplate, template_id)


@atomic_service
def generate_recurring_pendings_for_period(
    db: Session,
    period: date,
    commit: bool = True,
    user_id: int | None = None,
) -> list[RecurringPending]:
    """Genera pendientes mensuales a partir de plantillas activas de forma idempotente (RN-35)."""
    normalized_period = date(period.year, period.month, 1)
    check_period_open(db, normalized_period)

    templates = list(
        db.execute(
            select(RecurringTemplate).where(RecurringTemplate.active == True)  # noqa: E712
        )
        .scalars()
        .all()
    )

    existing_template_ids = set(
        db.execute(
            select(RecurringPending.template_id).where(RecurringPending.period == normalized_period)
        )
        .scalars()
        .all()
    )

    tpl_inputs = [
        RecurringTemplateInput(
            template_id=t.id,
            kind=t.kind,
            name=t.name,
            category_id=t.category_id,
            account_id=t.account_id,
            day_of_month=t.day_of_month,
            estimated_amount=t.estimated_amount,
            charged_to_user_id=t.charged_to_user_id,
            active=t.active,
        )
        for t in templates
    ]

    to_create = filter_templates_for_pending_generation(
        tpl_inputs, normalized_period, existing_template_ids
    )

    created_pendings: list[RecurringPending] = []
    for item in to_create:
        pending = RecurringPending(
            template_id=item.template_id,
            period=normalized_period,
            status="pending",
            transaction_id=None,
        )
        db.add(pending)
        created_pendings.append(pending)

    if created_pendings:
        db.flush()
        for pending in created_pendings:
            record_audit(
                db=db,
                entity="RecurringPending",
                entity_id=pending.id,
                action="INSERT",
                before=None,
                after={
                    "id": pending.id,
                    "template_id": pending.template_id,
                    "period": pending.period.isoformat(),
                    "status": pending.status,
                    "transaction_id": pending.transaction_id,
                },
                user_id=user_id,
            )

    if commit:
        commit_service(db)

    return created_pendings


@atomic_service
def confirm_recurring_pending(
    db: Session,
    pending_id: int,
    payment_date: date,
    user_id: int,
    actual_amount: Decimal | None = None,
    commit: bool = True,
) -> Transaction:
    """Confirma un pendiente recurrente, generando la transacción contable (RN-35)."""
    pending = db.execute(
        select(RecurringPending)
        .where(RecurringPending.id == pending_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    if pending is None:
        raise ValueError(f"Pendiente recurrente #{pending_id} no encontrado.")

    if pending.status != "pending":
        raise ValueError(
            f"El movimiento recurrente #{pending_id} no se encuentra pendiente "
            f"(estado actual: '{pending.status}')."
        )

    template = pending.template or db.get(RecurringTemplate, pending.template_id)
    if template is None:
        raise ValueError(f"Plantilla asociada al pendiente #{pending_id} no encontrada.")

    pending_creation = RecurringPendingCreation(
        pending_id=pending.id,
        template_id=template.id,
        kind=template.kind,
        category_id=template.category_id,
        account_id=template.account_id,
        estimated_amount=template.estimated_amount,
        charged_to_user_id=template.charged_to_user_id,
        period=pending.period,
        status=pending.status,
    )
    final_amount = validate_pending_confirmation(pending_creation, actual_amount)

    before_pending: dict[str, Any] = {
        "id": pending.id,
        "template_id": pending.template_id,
        "period": pending.period.isoformat(),
        "status": pending.status,
        "transaction_id": pending.transaction_id,
    }

    # Crear transacción contable con commit=False para asegurar atomicidad
    tx = create_transaction(
        db=db,
        kind=template.kind,
        amount=final_amount,
        account_id=template.account_id,
        tx_date=payment_date,
        period=pending.period,
        description=template.name,
        user_id=user_id,
        category_id=template.category_id,
        charged_to_user_id=template.charged_to_user_id,
        generated_by="recurring",
        commit=False,
    )

    pending.status = "confirmed"
    pending.transaction_id = tx.id

    after_pending: dict[str, Any] = {
        "id": pending.id,
        "template_id": pending.template_id,
        "period": pending.period.isoformat(),
        "status": pending.status,
        "transaction_id": pending.transaction_id,
    }

    record_audit(
        db=db,
        entity="RecurringPending",
        entity_id=pending.id,
        action="UPDATE",
        before=before_pending,
        after=after_pending,
        user_id=user_id,
    )

    if commit:
        commit_service(db)

    return tx


@atomic_service
def dismiss_recurring_pending(
    db: Session,
    pending_id: int,
    user_id: int,
    commit: bool = True,
) -> RecurringPending:
    """Descarta un pendiente recurrente del mes sin impacto en el libro contable (RN-35)."""
    pending = db.execute(
        select(RecurringPending)
        .where(RecurringPending.id == pending_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    if pending is None:
        raise ValueError(f"Pendiente recurrente #{pending_id} no encontrado.")

    if pending.status != "pending":
        raise ValueError(
            f"El movimiento recurrente #{pending_id} no se encuentra pendiente "
            f"(estado actual: '{pending.status}')."
        )

    check_period_open(db, pending.period)

    before_pending: dict[str, Any] = {
        "id": pending.id,
        "template_id": pending.template_id,
        "period": pending.period.isoformat(),
        "status": pending.status,
        "transaction_id": pending.transaction_id,
    }

    pending.status = "dismissed"

    after_pending: dict[str, Any] = {
        "id": pending.id,
        "template_id": pending.template_id,
        "period": pending.period.isoformat(),
        "status": pending.status,
        "transaction_id": pending.transaction_id,
    }

    record_audit(
        db=db,
        entity="RecurringPending",
        entity_id=pending.id,
        action="UPDATE",
        before=before_pending,
        after=after_pending,
        user_id=user_id,
    )

    if commit:
        commit_service(db)

    return pending


def get_pending_recurrings_for_period(
    db: Session,
    period: date,
    status_filter: str | None = None,
) -> list[RecurringPending]:
    """Obtiene los movimientos recurrentes del período, opcionalmente filtrados por estado."""
    normalized_period = date(period.year, period.month, 1)
    query = (
        select(RecurringPending)
        .where(RecurringPending.period == normalized_period)
        .order_by(RecurringPending.id.asc())
    )
    if status_filter is not None:
        query = query.where(RecurringPending.status == status_filter)

    return list(db.execute(query).scalars().all())


def has_unresolved_recurrings_for_period(db: Session, period: date) -> bool:
    """Verifica si quedan pendientes recurrentes sin confirmar ni descartar (RN-26)."""
    normalized_period = date(period.year, period.month, 1)
    count = db.execute(
        select(func.count(RecurringPending.id)).where(
            RecurringPending.period == normalized_period,
            RecurringPending.status == "pending",
        )
    ).scalar_one()
    return count > 0
