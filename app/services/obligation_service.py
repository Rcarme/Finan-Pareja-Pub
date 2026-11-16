"""Servicios para cuotas del departamento, cronograma y provisión de refuerzos (RN-27 a RN-31)."""

from calendar import monthrange
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db import acquire_advisory_lock
from app.core.formatters import today_ar
from app.core.transactions import atomic_service, commit_service
from app.domain.obligations import (
    ProvisionCalculation,
    calculate_effective_fx_rate,
    calculate_reinforcement_provision,
    generate_annual_schedule_items,
)
from app.models.close import FxRate
from app.models.envelopes import Envelope
from app.models.ledger import Category, Transaction
from app.models.obligations import Obligation, ObligationItem
from app.services.envelope_service import get_envelope_balance
from app.services.ledger_service import check_period_open, create_transaction


@atomic_service
def ensure_obligation(db: Session, user_id: int) -> Obligation:
    """Obtiene o crea la obligación "Departamento" (acción explícita, auditada, N6)."""
    ob_stmt = select(Obligation).where(Obligation.name == "Departamento")
    ob = db.execute(ob_stmt).scalar_one_or_none()
    if ob is not None:
        return ob
    ob = Obligation(name="Departamento", monthly_usd=Decimal("1800.00"), active=True)
    db.add(ob)
    db.flush()
    record_audit(
        db=db,
        user_id=user_id,
        entity="Obligation",
        entity_id=ob.id,
        action="INSERT",
        after={
            "name": ob.name,
            "monthly_usd": str(ob.monthly_usd),
            "active": ob.active,
        },
    )
    commit_service(db)
    db.refresh(ob)
    return ob


def get_obligation_schedule(
    db: Session,
    obligation_id: int,
    year: int,
) -> list[ObligationItem]:
    """Lectura pura del cronograma anual (N6: no escribe ni hace commit)."""
    start_date = date(year, 1, 1)
    end_date = date(year, 12, 31)
    return list(
        db.execute(
            select(ObligationItem)
            .where(
                ObligationItem.obligation_id == obligation_id,
                ObligationItem.due_period >= start_date,
                ObligationItem.due_period <= end_date,
            )
            .order_by(ObligationItem.due_period.asc(), ObligationItem.kind.asc())
        )
        .scalars()
        .all()
    )


@atomic_service
def generate_obligation_schedule(
    db: Session,
    obligation_id: int,
    year: int,
    user_id: int,
    monthly_usd: Decimal = Decimal("1800.00"),
    july_reinforcement: Decimal = Decimal("1500.00"),
    december_reinforcement: Decimal = Decimal("2500.00"),
) -> list[ObligationItem]:
    """Acción explícita: genera el cronograma anual auditado con el usuario real (N6, RN-27).

    Solo crea los ítems faltantes sin alterar los ya existentes ni los pagados.
    """
    if not 2000 <= year <= 2100:
        raise ValueError("El año del cronograma debe estar entre 2000 y 2100.")
    existing_items = get_obligation_schedule(db, obligation_id, year)

    existing_map = {(item.due_period, item.kind): item for item in existing_items}
    definitions = generate_annual_schedule_items(
        year=year,
        monthly_usd=monthly_usd,
        july_reinforcement=july_reinforcement,
        december_reinforcement=december_reinforcement,
    )

    new_items_added = False
    for item_def in definitions:
        key = (item_def.due_period, item_def.kind)
        if key not in existing_map:
            new_item = ObligationItem(
                obligation_id=obligation_id,
                due_period=item_def.due_period,
                kind=item_def.kind,
                usd_amount=item_def.usd_amount,
                status="pendiente",
            )
            db.add(new_item)
            db.flush()
            record_audit(
                db=db,
                user_id=user_id,
                entity="ObligationItem",
                entity_id=new_item.id,
                action="INSERT",
                after={
                    "obligation_id": new_item.obligation_id,
                    "due_period": new_item.due_period.isoformat(),
                    "kind": new_item.kind,
                    "usd_amount": str(new_item.usd_amount),
                    "status": new_item.status,
                },
            )
            new_items_added = True

    if new_items_added:
        commit_service(db)

    return get_obligation_schedule(db, obligation_id, year)


@atomic_service
def create_obligation_item(
    db: Session,
    due_period: date,
    kind: str,
    usd_amount: Decimal,
    user_id: int,
) -> ObligationItem:
    """Crea o actualiza un vencimiento en el cronograma del Departamento (RN-27, RN-37)."""
    if kind not in ("cuota", "refuerzo"):
        raise ValueError("El tipo de vencimiento debe ser 'cuota' o 'refuerzo'.")
    if usd_amount <= Decimal("0.00"):
        raise ValueError("El monto en USD debe ser estrictamente positivo.")

    normalized_period = date(due_period.year, due_period.month, 1)
    check_period_open(db, normalized_period)
    ob = ensure_obligation(db, user_id=user_id)

    existing = db.execute(
        select(ObligationItem)
        .where(
            ObligationItem.obligation_id == ob.id,
            ObligationItem.due_period == normalized_period,
            ObligationItem.kind == kind,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    if existing is not None:
        if existing.status != "pendiente":
            raise ValueError("No se puede modificar un vencimiento ya pagado.")
        before = {
            "obligation_id": existing.obligation_id,
            "due_period": existing.due_period.isoformat(),
            "kind": existing.kind,
            "usd_amount": str(existing.usd_amount),
            "status": existing.status,
        }
        existing.usd_amount = usd_amount
        record_audit(
            db=db,
            user_id=user_id,
            entity="ObligationItem",
            entity_id=existing.id,
            action="UPDATE",
            before=before,
            after={
                "obligation_id": existing.obligation_id,
                "due_period": existing.due_period.isoformat(),
                "kind": existing.kind,
                "usd_amount": str(existing.usd_amount),
                "status": existing.status,
            },
        )
        commit_service(db)
        db.refresh(existing)
        return existing

    item = ObligationItem(
        obligation_id=ob.id,
        due_period=normalized_period,
        kind=kind,
        usd_amount=usd_amount,
        status="pendiente",
    )
    db.add(item)
    db.flush()
    record_audit(
        db=db,
        user_id=user_id,
        entity="ObligationItem",
        entity_id=item.id,
        action="INSERT",
        after={
            "obligation_id": item.obligation_id,
            "due_period": item.due_period.isoformat(),
            "kind": item.kind,
            "usd_amount": str(item.usd_amount),
            "status": item.status,
        },
    )
    commit_service(db)
    db.refresh(item)
    return item


@atomic_service
def record_fx_rate(
    db: Session,
    fx_date: date,
    rate: Decimal,
    user_id: int,
    note: str | None = None,
) -> FxRate:
    """Registra una nueva cotización de referencia con historial y auditoría (RN-29)."""
    if not rate.is_finite() or rate <= Decimal("0.00"):
        raise ValueError("La cotización debe ser mayor a cero.")
    if rate > Decimal("99999999.9999"):
        raise ValueError("La cotización supera el límite máximo permitido (99.999.999,9999).")
    if note and len(note.strip()) > 255:
        raise ValueError("La nota de cotización no puede superar los 255 caracteres.")

    fx_rate = FxRate(
        date=fx_date,
        rate=rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        note=note.strip() if note else None,
        source="manual",
        created_by=user_id,
    )
    db.add(fx_rate)
    db.flush()

    record_audit(
        db=db,
        user_id=user_id,
        entity="fx_rates",
        entity_id=fx_rate.id,
        action="create",
        after={"date": str(fx_rate.date), "rate": str(fx_rate.rate), "note": fx_rate.note},
    )

    commit_service(db)
    db.refresh(fx_rate)
    return fx_rate


def get_latest_fx_rate(db: Session, as_of_date: date | None = None) -> FxRate | None:
    """Obtiene la cotización de referencia vigente a una fecha (por defecto hoy) (RN-29)."""
    target_date = as_of_date or today_ar()
    stmt = (
        select(FxRate)
        .where(FxRate.date <= target_date)
        .order_by(FxRate.date.desc(), FxRate.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_fx_rate_history(db: Session, limit: int = 15) -> list[FxRate]:
    """Obtiene el historial reciente de cotizaciones cargadas."""
    stmt = select(FxRate).order_by(FxRate.date.desc(), FxRate.id.desc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


def get_next_reinforcement_provision(
    db: Session,
    current_period: date | None = None,
) -> ProvisionCalculation | None:
    """Calcula la provisión del próximo refuerzo según el saldo del sobre (RN-30)."""
    today = today_ar()
    period = current_period or date(today.year, today.month, 1)

    ob_stmt = select(Obligation).where(Obligation.name == "Departamento")
    ob = db.execute(ob_stmt).scalar_one_or_none()
    if not ob:
        return None

    # Buscar el próximo refuerzo pendiente
    stmt = (
        select(ObligationItem)
        .where(
            ObligationItem.obligation_id == ob.id,
            ObligationItem.kind == "refuerzo",
            ObligationItem.status == "pendiente",
            ObligationItem.due_period >= period,
        )
        .order_by(ObligationItem.due_period.asc())
        .limit(1)
    )
    next_reinforcement = db.execute(stmt).scalar_one_or_none()

    if next_reinforcement is None:
        # N6: las lecturas no generan cronogramas; la generación es una acción explícita.
        return None

    # Obtener cotización de referencia
    period_end = date(period.year, period.month, monthrange(period.year, period.month)[1])
    reference_date = min(today, period_end)
    latest_fx = get_latest_fx_rate(db, as_of_date=reference_date)
    if latest_fx is None:
        return None
    fx_rate_val = latest_fx.rate

    # Obtener saldo del sobre "Refuerzos depto"
    ref_env = db.execute(
        select(Envelope).where(Envelope.name == "Refuerzos depto")
    ).scalar_one_or_none()
    current_balance = (
        get_envelope_balance(db, ref_env.id, period_cutoff=period) if ref_env else Decimal("0.00")
    )

    return calculate_reinforcement_provision(
        target_usd=next_reinforcement.usd_amount,
        fx_rate=fx_rate_val,
        current_balance=current_balance,
        current_period=period,
        due_period=next_reinforcement.due_period,
    )


@atomic_service
def pay_obligation_item(
    db: Session,
    item_id: int,
    account_id: int,
    ars_amount: Decimal,
    payment_date: date,
    period: date,
    user_id: int,
    pay_from_envelope: bool = False,
    envelope_id: int | None = None,
) -> Transaction:
    """Paga una cuota o refuerzo en pesos, calculando la cotización efectiva (RN-28, RN-31).

    Crea un movimiento de gasto (expense) enlazado al item y lo pasa a estado 'pagado'.
    Si pay_from_envelope es True, asocia el gasto al sobre correspondiente (RN-31).
    """
    acquire_advisory_lock(db, 170170, shared=True)
    item = db.execute(
        select(ObligationItem)
        .where(ObligationItem.id == item_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if item is None:
        raise ValueError("Vencimiento no encontrado.")

    if item.status == "pagado":
        raise ValueError("Este vencimiento ya se encuentra pagado.")

    fx_rate_effective = calculate_effective_fx_rate(
        ars_amount=ars_amount, usd_amount=item.usd_amount
    )

    # Determinar categoría "Compromiso mensual"
    cat_stmt = select(Category).where(Category.name == "Compromiso mensual")
    cat = db.execute(cat_stmt).scalar_one_or_none()
    if cat is None:
        # Fallback si no está inicializada
        cat = Category(
            name="Compromiso mensual",
            kind="expense",
            default_period_offset=0,
            is_system=True,
            active=True,
        )
        db.add(cat)
        db.flush()
        record_audit(
            db=db,
            user_id=user_id,
            entity="Category",
            entity_id=cat.id,
            action="INSERT",
            after={
                "name": cat.name,
                "kind": cat.kind,
                "is_system": cat.is_system,
                "active": cat.active,
            },
        )

    # Si se paga desde sobre, buscar sobre de provisión o el especificado
    target_env_id: int | None = None
    if pay_from_envelope:
        if envelope_id:
            target_env_id = envelope_id
        else:
            ref_env = db.execute(
                select(Envelope).where(Envelope.name == "Refuerzos depto")
            ).scalar_one_or_none()
            if ref_env:
                target_env_id = ref_env.id

    desc = f"Pago {item.kind} depto {item.due_period.strftime('%m/%Y')} (USD {item.usd_amount})"

    tx = create_transaction(
        db=db,
        kind="expense",
        amount=ars_amount,
        account_id=account_id,
        tx_date=payment_date,
        period=period,
        description=desc,
        user_id=user_id,
        category_id=cat.id,
        envelope_id=target_env_id,
        usd_amount=item.usd_amount,
        fx_rate_effective=fx_rate_effective,
        commit=False,
    )

    before = {"status": item.status, "transaction_id": item.transaction_id}

    item.status = "pagado"
    item.transaction_id = tx.id

    record_audit(
        db=db,
        user_id=user_id,
        entity="obligation_items",
        entity_id=item.id,
        action="update",
        before=before,
        after={"status": item.status, "transaction_id": item.transaction_id},
    )

    commit_service(db)
    db.refresh(item)
    db.refresh(tx)
    return tx


def get_department_summary(db: Session, year: int) -> dict[str, Any]:
    """Resumen de cuotas y refuerzos. Lectura pura (N6): no crea ni commitea nada."""
    ob_stmt = select(Obligation).where(Obligation.name == "Departamento")
    ob = db.execute(ob_stmt).scalar_one_or_none()
    if not ob:
        raise ValueError(
            "Aún no hay obligación 'Departamento'. Generá el cronograma con la acción explícita."
        )

    items = get_obligation_schedule(db=db, obligation_id=ob.id, year=year)

    total_paid_usd = Decimal("0.00")
    total_pending_usd = Decimal("0.00")
    total_paid_ars = Decimal("0.00")

    for it in items:
        if it.status == "pagado":
            total_paid_usd += it.usd_amount
            if it.transaction_id:
                tx = db.get(Transaction, it.transaction_id)
                if tx and tx.status == "active":
                    total_paid_ars += tx.amount
        else:
            total_pending_usd += it.usd_amount

    today_date = today_ar()
    latest_fx = get_latest_fx_rate(db, as_of_date=today_date)
    fx_history = get_fx_rate_history(db, limit=10)
    current_period = date(today_date.year, today_date.month, 1)
    provision = get_next_reinforcement_provision(db, current_period=current_period)

    # Próximo vencimiento pendiente
    next_due = next((it for it in items if it.status == "pendiente"), None)

    return {
        "obligation": ob,
        "year": year,
        "items": items,
        "schedule_items": items,
        "total_paid_usd": total_paid_usd,
        "total_pending_usd": total_pending_usd,
        "total_paid_ars": total_paid_ars,
        "latest_fx": latest_fx,
        "fx_history": fx_history,
        "provision": provision,
        "next_due": next_due,
    }
