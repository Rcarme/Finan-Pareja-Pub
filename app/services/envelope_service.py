"""Servicios para la gestión de sobres, ahorros y metas financieras.

Reglas asociadas: RN-12 a RN-17, RN-44, RN-45.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.transactions import atomic_service, commit_service
from app.domain.envelopes import (
    EnvelopeMovementItem,
    compute_envelope_balance,
    validate_envelope_cutoffs,
)
from app.models.close import Period, Setting
from app.models.envelopes import Envelope, EnvelopeMovement
from app.services.ledger_service import check_period_open
from app.services.ownership_service import get_ownership_summary


def _env_to_dict(env: Envelope) -> dict[str, Any]:
    """Serializa un sobre para auditoría."""
    return {
        "id": env.id,
        "name": env.name,
        "type": env.type,
        "target_amount": str(env.target_amount) if env.target_amount else None,
        "target_date": env.target_date.isoformat() if env.target_date else None,
        "is_system": env.is_system,
        "active": env.active,
        "version": env.version,
    }


def _em_to_dict(em: EnvelopeMovement) -> dict[str, Any]:
    """Serializa un movimiento de sobre para auditoría."""
    return {
        "id": em.id,
        "envelope_id": em.envelope_id,
        "kind": em.kind,
        "amount": str(em.amount),
        "period": em.period.isoformat() if em.period else None,
        "transaction_id": em.transaction_id,
        "close_id": em.close_id,
        "status": em.status,
    }


def get_envelope_balance(
    db: Session,
    envelope_id: int,
    period_cutoff: date | None = None,
) -> Decimal:
    """Calcula el saldo contable actual de un sobre (RN-12)."""
    env = db.get(Envelope, envelope_id)
    if env is None:
        raise ValueError(f"Sobre con id {envelope_id} no encontrado.")

    stmt = select(EnvelopeMovement.kind, func.sum(EnvelopeMovement.amount)).where(
        EnvelopeMovement.envelope_id == envelope_id,
        EnvelopeMovement.status == "active",
    )
    summary = get_ownership_summary(db, period_cutoff)
    initial = Decimal("0.00")
    if summary is not None:
        opening = db.get(Setting, "ownership_opening")
        assert opening is not None
        stmt = stmt.where(
            or_(
                EnvelopeMovement.id > int(opening.value["max_envelope_movement_id"]),
                EnvelopeMovement.period > summary.opening_date.replace(day=1),
            )
        )
        if env.name == "Ahorro común":
            initial = summary.savings_ars
    if period_cutoff is not None:
        stmt = stmt.where(EnvelopeMovement.period <= period_cutoff)
    rows = db.execute(stmt.group_by(EnvelopeMovement.kind)).all()
    domain_movs = [
        EnvelopeMovementItem(kind=kind, amount=amount, envelope_id=envelope_id)
        for kind, amount in rows
    ]
    return initial + compute_envelope_balance(
        envelope_id=envelope_id,
        movements=domain_movs,
        period_cutoff=period_cutoff,
    )


def _lock_envelope(db: Session, envelope_id: int) -> Envelope | None:
    # Mismo orden que cierre/reapertura: bloqueo global antes de cualquier fila.
    db.execute(text("SELECT pg_advisory_xact_lock_shared(170170)"))
    return db.scalar(
        select(Envelope)
        .where(Envelope.id == envelope_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def validate_envelope_deductions(
    db: Session, envelope_id: int, deductions: dict[date, Decimal]
) -> None:
    """Preserva RN-15 en todos los cortes afectados, no solo saldo final o inicial."""
    _lock_envelope(db, envelope_id)
    first_period = min(deductions)
    cutoffs = set(db.scalars(select(Period.period).where(Period.period >= first_period)))
    cutoffs.update(deductions)
    balances = {cutoff: get_envelope_balance(db, envelope_id, cutoff) for cutoff in cutoffs}
    validate_envelope_cutoffs(balances, deductions)


@atomic_service
def allocate_to_envelope(
    db: Session,
    envelope_id: int,
    amount: Decimal,
    period: date,
    user_id: int,
    commit: bool = True,
    close_id: int | None = None,
) -> EnvelopeMovement:
    """Asigna dinero desde el Disponible hacia un sobre (RN-13)."""
    if amount <= Decimal("0.00"):
        raise ValueError("El monto a asignar debe ser estrictamente positivo.")

    normalized_period = date(period.year, period.month, 1)
    check_period_open(db, normalized_period)

    env = _lock_envelope(db, envelope_id)
    if env is None or not env.active:
        raise ValueError("El sobre no existe o está inactivo.")

    em = EnvelopeMovement(
        envelope_id=envelope_id,
        kind="allocate",
        amount=amount,
        period=normalized_period,
        status="active",
        close_id=close_id,
    )
    db.add(em)
    db.flush()

    record_audit(
        db=db,
        entity="EnvelopeMovement",
        entity_id=em.id,
        action="INSERT",
        after=_em_to_dict(em),
        user_id=user_id,
    )
    if commit:
        commit_service(db)
        db.refresh(em)
    return em


@atomic_service
def release_from_envelope(
    db: Session,
    envelope_id: int,
    amount: Decimal,
    period: date,
    user_id: int,
    commit: bool = True,
) -> EnvelopeMovement:
    """Libera dinero de un sobre al Disponible con bloqueo FOR UPDATE (RN-13, RN-15, M-02)."""
    if amount <= Decimal("0.00"):
        raise ValueError("El monto a liberar debe ser estrictamente positivo.")

    normalized_period = date(period.year, period.month, 1)
    check_period_open(db, normalized_period)

    env = _lock_envelope(db, envelope_id)
    if env is None or not env.active:
        raise ValueError("El sobre no existe o está inactivo.")

    validate_envelope_deductions(db, envelope_id, {normalized_period: amount})

    em = EnvelopeMovement(
        envelope_id=envelope_id,
        kind="release",
        amount=amount,
        period=normalized_period,
        status="active",
    )
    db.add(em)
    db.flush()

    record_audit(
        db=db,
        entity="EnvelopeMovement",
        entity_id=em.id,
        action="INSERT",
        after=_em_to_dict(em),
        user_id=user_id,
    )
    if commit:
        commit_service(db)
        db.refresh(em)
    return em


@atomic_service
def spend_from_envelope(
    db: Session,
    envelope_id: int,
    amount: Decimal,
    period: date,
    transaction_id: int,
    user_id: int,
    commit: bool = False,
) -> EnvelopeMovement:
    """Registra un gasto contra un sobre con bloqueo FOR UPDATE (RN-14, RN-15, M-02)."""
    normalized_period = period.replace(day=1)
    check_period_open(db, normalized_period)
    env = _lock_envelope(db, envelope_id)
    if env is None or not env.active:
        raise ValueError("El sobre no existe o está inactivo.")

    validate_envelope_deductions(db, envelope_id, {normalized_period: amount})

    em = EnvelopeMovement(
        envelope_id=envelope_id,
        kind="spend",
        amount=amount,
        period=normalized_period,
        transaction_id=transaction_id,
        status="active",
    )
    db.add(em)
    db.flush()
    record_audit(
        db=db,
        entity="EnvelopeMovement",
        entity_id=em.id,
        action="INSERT",
        after=_em_to_dict(em),
        user_id=user_id,
    )
    if commit:
        commit_service(db)
        db.refresh(em)
    return em


@atomic_service
def delete_envelope(db: Session, envelope_id: int, user_id: int) -> None:
    """Desactiva un sobre no perteneciente al sistema y sin saldo (RN-45)."""
    env = _lock_envelope(db, envelope_id)
    if env is None:
        raise ValueError("Sobre no encontrado.")

    if env.is_system:
        raise ValueError("No se pueden eliminar sobres del sistema (RN-45).")

    current_balance = get_envelope_balance(db, envelope_id)
    if current_balance != Decimal("0.00"):
        raise ValueError("No se puede eliminar un sobre con saldo. Liberá los fondos primero.")

    before = _env_to_dict(env)
    env.active = False
    env.version += 1
    after = _env_to_dict(env)

    record_audit(
        db=db,
        entity="Envelope",
        entity_id=env.id,
        action="UPDATE",
        before=before,
        after=after,
        user_id=user_id,
    )
    commit_service(db)


@atomic_service
def create_goal(
    db: Session,
    name: str,
    target_amount: Decimal | None,
    target_date: date | None,
    user_id: int,
    envelope_type: str = "goal",
) -> Envelope:
    """Crea una nueva meta financiera o sobre de ahorro (RN-17)."""
    if not name or not name.strip():
        raise ValueError("El nombre de la meta es obligatorio.")
    if len(name.strip()) > 100:
        raise ValueError("El nombre de la meta no puede superar los 100 caracteres.")
    if envelope_type not in ("goal", "savings"):
        raise ValueError("Tipo de sobre inválido.")
    if envelope_type == "goal" and (target_amount is None or target_date is None):
        raise ValueError("Las metas requieren monto objetivo y fecha límite.")
    if target_amount is not None:
        if target_amount <= Decimal("0.00"):
            raise ValueError("El monto objetivo debe ser mayor a cero.")
        if target_amount > Decimal("999999999999.99"):
            raise ValueError(
                "El monto supera el límite contable máximo permitido (999.999.999.999,99)."
            )

    clean_name = name.strip()
    db.execute(text("SELECT pg_advisory_xact_lock(170172)"))
    existing = db.execute(
        select(Envelope).where(func.lower(Envelope.name) == clean_name.lower())
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"Ya existe un sobre o meta con el nombre '{clean_name}'.")

    goal = Envelope(
        name=clean_name,
        type=envelope_type,
        target_amount=target_amount,
        target_date=target_date,
        is_system=False,
        active=True,
        version=1,
    )
    db.add(goal)
    db.flush()

    record_audit(
        db=db,
        entity="Envelope",
        entity_id=goal.id,
        action="INSERT",
        after=_env_to_dict(goal),
        user_id=user_id,
    )
    commit_service(db)
    db.refresh(goal)
    return goal


@atomic_service
def update_buffer_target(
    db: Session,
    target_amount: Decimal | None,
    user_id: int,
) -> Envelope:
    """Actualiza el monto objetivo opcional del sobre de sistema 'Colchón' (RN-23, RN-45)."""
    if target_amount is not None:
        if target_amount <= Decimal("0.00"):
            raise ValueError("El objetivo del colchón debe ser positivo o vacío.")
        if target_amount > Decimal("999999999999.99"):
            raise ValueError(
                "El monto supera el límite contable máximo permitido (999.999.999.999,99)."
            )

    db.execute(text("SELECT pg_advisory_xact_lock_shared(170170)"))
    colchon = db.execute(
        select(Envelope)
        .where(Envelope.name == "Colchón")
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if colchon is None:
        raise ValueError("El sobre de sistema 'Colchón' no existe.")

    before = _env_to_dict(colchon)
    colchon.target_amount = target_amount
    colchon.version += 1
    after = _env_to_dict(colchon)

    record_audit(
        db=db,
        entity="Envelope",
        entity_id=colchon.id,
        action="UPDATE",
        before=before,
        after=after,
        user_id=user_id,
    )
    commit_service(db)
    db.refresh(colchon)
    return colchon
