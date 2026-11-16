"""Servicio de contrato de alquiler, cobro y ajustes manuales por IPC (RN-32 a RN-34, RN-37).

Gestiona:
- Consulta y creación del contrato único de alquiler.
- Registro de cobro de alquiler (ingreso en 'Alquiler cobrado').
- Registro de ajustes manuales con recálculo de próxima fecha e historial inmutable.
- Alertas para banner de ajuste próximo/vencido y aviso de cobro faltante día 6.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db import acquire_advisory_lock
from app.core.formatters import today_ar
from app.core.transactions import atomic_service, commit_service
from app.domain.rent import (
    RentAdjustAlertStatus,
    RentCollectionAlertStatus,
    calculate_next_adjust_date,
    evaluate_rent_adjust_reminder,
    evaluate_rent_collection_status,
    validate_rent_adjustment,
)
from app.models.ledger import Category, Transaction
from app.models.rent import RentAdjustment, RentContract
from app.services.ledger_service import check_period_open, create_transaction


@dataclass(frozen=True)
class RentStatusSummary:
    """Resumen consolidado del estado de alquiler del período y alertas activas."""

    contract: RentContract
    period: date
    as_of_date: date
    is_collected: bool
    collection_transaction: Transaction | None
    adjust_alert_status: RentAdjustAlertStatus
    collection_status: RentCollectionAlertStatus
    has_adjust_reminder: bool
    has_missing_collection_alert: bool


def get_rent_contract(db: Session) -> RentContract | None:
    """Lectura pura: obtiene el contrato activo de alquiler sin escribir en la BD (H-07)."""
    return (
        db.execute(
            select(RentContract).where(RentContract.active == True)  # noqa: E712
        )
        .scalars()
        .first()
    )


@atomic_service
def ensure_rent_contract(
    db: Session,
    user_id: int | None = None,
    default_amount: Decimal = Decimal("700000.00"),
    commit: bool = True,
) -> RentContract:
    """Obtiene el contrato activo de alquiler o crea el inicial explícitamente (RN-32, H-07)."""
    contract = get_rent_contract(db)
    if contract is not None:
        return contract

    today = today_ar()
    last_adjust = date(today.year, today.month, 1)
    next_adjust = calculate_next_adjust_date(last_adjust, 3)

    contract = RentContract(
        current_amount=default_amount,
        adjust_every_months=3,
        last_adjust_date=last_adjust,
        next_adjust_date=next_adjust,
        active=True,
    )
    db.add(contract)
    db.flush()

    record_audit(
        db=db,
        entity="RentContract",
        entity_id=contract.id,
        action="INSERT",
        before=None,
        after={
            "current_amount": str(contract.current_amount),
            "adjust_every_months": contract.adjust_every_months,
            "last_adjust_date": contract.last_adjust_date.isoformat(),
            "next_adjust_date": contract.next_adjust_date.isoformat(),
            "active": contract.active,
        },
        user_id=user_id,
    )

    if commit:
        commit_service(db)

    return contract


get_or_create_rent_contract = ensure_rent_contract


@atomic_service
def upsert_rent_contract(
    db: Session,
    current_amount: Decimal,
    adjust_every_months: int = 3,
    last_adjust_date: date | None = None,
    user_id: int | None = None,
) -> RentContract:
    """Crea o actualiza explícitamente los parámetros del contrato de alquiler (RN-32, RN-37)."""
    if current_amount <= Decimal("0.00"):
        raise ValueError("El monto del contrato debe ser estrictamente positivo.")
    if adjust_every_months < 1 or adjust_every_months > 24:
        raise ValueError("La frecuencia de ajuste debe estar entre 1 y 24 meses.")

    today = today_ar()
    effective_last_adj = last_adjust_date or date(today.year, today.month, 1)
    next_adjust = calculate_next_adjust_date(effective_last_adj, adjust_every_months)
    contract = get_rent_contract(db)

    if contract is None:
        contract = RentContract(
            current_amount=current_amount,
            adjust_every_months=adjust_every_months,
            last_adjust_date=effective_last_adj,
            next_adjust_date=next_adjust,
            active=True,
        )
        db.add(contract)
        db.flush()
        record_audit(
            db=db,
            entity="RentContract",
            entity_id=contract.id,
            action="INSERT",
            before=None,
            after={
                "current_amount": str(contract.current_amount),
                "adjust_every_months": contract.adjust_every_months,
                "last_adjust_date": contract.last_adjust_date.isoformat(),
                "next_adjust_date": contract.next_adjust_date.isoformat(),
                "active": contract.active,
            },
            user_id=user_id,
        )
    else:
        before = {
            "current_amount": str(contract.current_amount),
            "adjust_every_months": contract.adjust_every_months,
            "last_adjust_date": contract.last_adjust_date.isoformat(),
            "next_adjust_date": contract.next_adjust_date.isoformat(),
            "active": contract.active,
        }
        contract.current_amount = current_amount
        contract.adjust_every_months = adjust_every_months
        contract.last_adjust_date = effective_last_adj
        contract.next_adjust_date = next_adjust
        after = {
            "current_amount": str(contract.current_amount),
            "adjust_every_months": contract.adjust_every_months,
            "last_adjust_date": contract.last_adjust_date.isoformat(),
            "next_adjust_date": contract.next_adjust_date.isoformat(),
            "active": contract.active,
        }
        record_audit(
            db=db,
            entity="RentContract",
            entity_id=contract.id,
            action="UPDATE",
            before=before,
            after=after,
            user_id=user_id,
        )

    commit_service(db)
    db.refresh(contract)
    return contract


@atomic_service
def record_rent_collection(
    db: Session,
    period: date,
    payment_date: date,
    account_id: int,
    user_id: int,
    amount: Decimal,
    commit: bool = True,
) -> Transaction:
    """Registra el cobro de alquiler del período como movimiento income (RN-32)."""
    normalized_period = date(period.year, period.month, 1)
    check_period_open(db, normalized_period)
    acquire_advisory_lock(db, 170171, shared=False)

    # Verificar que no exista ya un cobro activo de alquiler en este período (H-08, Q22)
    existing_collection = (
        db.execute(
            select(Transaction).where(
                Transaction.period == normalized_period,
                Transaction.kind == "income",
                Transaction.status == "active",
                or_(
                    Transaction.generated_by == "rent_collection",
                    Transaction.category_id.in_(
                        select(Category.id).where(
                            Category.name == "Alquiler cobrado",
                            Category.kind == "income",
                        )
                    ),
                ),
            )
        )
        .scalars()
        .first()
    )
    if existing_collection is not None:
        period_str = normalized_period.strftime("%Y-%m")
        raise ValueError(
            f"Ya existe un cobro de alquiler registrado y activo para el período {period_str}."
        )

    # Buscar o crear categoría 'Alquiler cobrado'
    cat = (
        db.execute(
            select(Category).where(
                Category.name == "Alquiler cobrado",
                Category.kind == "income",
            )
        )
        .scalars()
        .first()
    )

    if cat is None:
        cat = Category(
            name="Alquiler cobrado",
            kind="income",
            default_period_offset=0,
            is_system=True,
            active=True,
        )
        db.add(cat)
        db.flush()
        record_audit(
            db=db,
            entity="Category",
            entity_id=cat.id,
            action="INSERT",
            before=None,
            after={"name": cat.name, "kind": cat.kind, "is_system": True},
            user_id=user_id,
        )

    return create_transaction(
        db=db,
        kind="income",
        amount=amount,
        account_id=account_id,
        tx_date=payment_date,
        period=normalized_period,
        description="Alquiler cobrado",
        user_id=user_id,
        category_id=cat.id,
        generated_by="rent_collection",
        commit=commit,
    )


@atomic_service
def record_rent_adjustment(
    db: Session,
    contract_id: int,
    adjust_date: date,
    new_amount: Decimal,
    ipc_pct: Decimal | None,
    note: str | None,
    user_id: int,
    commit: bool = True,
) -> RentAdjustment:
    """Registra ajuste manual de alquiler, actualiza contrato y audita (RN-11, RN-33, RN-37)."""
    check_period_open(db, date(adjust_date.year, adjust_date.month, 1))
    contract = db.execute(
        select(RentContract).where(RentContract.id == contract_id).with_for_update()
    ).scalar_one_or_none()

    if contract is None:
        raise ValueError(f"Contrato de alquiler #{contract_id} no encontrado.")

    validate_rent_adjustment(contract.current_amount, new_amount, ipc_pct)

    old_amount = contract.current_amount
    next_adjust = calculate_next_adjust_date(adjust_date, contract.adjust_every_months)

    before_contract: dict[str, Any] = {
        "current_amount": str(contract.current_amount),
        "last_adjust_date": contract.last_adjust_date.isoformat(),
        "next_adjust_date": contract.next_adjust_date.isoformat(),
    }

    # Actualizar contrato
    contract.current_amount = new_amount
    contract.last_adjust_date = adjust_date
    contract.next_adjust_date = next_adjust

    after_contract: dict[str, Any] = {
        "current_amount": str(contract.current_amount),
        "last_adjust_date": contract.last_adjust_date.isoformat(),
        "next_adjust_date": contract.next_adjust_date.isoformat(),
    }

    adj = RentAdjustment(
        contract_id=contract.id,
        date=adjust_date,
        old_amount=old_amount,
        new_amount=new_amount,
        ipc_pct=ipc_pct,
        note=note,
        created_by=user_id,
    )
    db.add(adj)
    db.flush()

    record_audit(
        db=db,
        entity="RentAdjustment",
        entity_id=adj.id,
        action="INSERT",
        before=None,
        after={
            "contract_id": adj.contract_id,
            "date": adj.date.isoformat(),
            "old_amount": str(adj.old_amount),
            "new_amount": str(adj.new_amount),
            "ipc_pct": str(adj.ipc_pct) if adj.ipc_pct is not None else None,
            "note": adj.note,
        },
        user_id=user_id,
    )

    record_audit(
        db=db,
        entity="RentContract",
        entity_id=contract.id,
        action="UPDATE",
        before=before_contract,
        after=after_contract,
        user_id=user_id,
    )

    if commit:
        commit_service(db)

    return adj


def get_rent_adjustment_history(db: Session, contract_id: int) -> list[RentAdjustment]:
    """Obtiene el historial cronológico descendente de ajustes de alquiler (RN-33)."""
    return list(
        db.execute(
            select(RentAdjustment)
            .where(RentAdjustment.contract_id == contract_id)
            .order_by(RentAdjustment.date.desc(), RentAdjustment.id.desc())
        )
        .scalars()
        .all()
    )


def get_rent_status_summary(
    db: Session,
    period: date,
    as_of_date: date | None = None,
) -> RentStatusSummary:
    """Evalúa estado del alquiler y alertas sin efectos de escritura (RN-33, RN-34, H-07)."""
    normalized_period = date(period.year, period.month, 1)
    today = as_of_date or today_ar()

    contract = get_rent_contract(db)
    if contract is None:
        last_adjust = date(today.year, today.month, 1)
        contract = RentContract(
            id=0,
            current_amount=Decimal("700000.00"),
            adjust_every_months=3,
            last_adjust_date=last_adjust,
            next_adjust_date=calculate_next_adjust_date(last_adjust, 3),
            active=True,
        )

    cat = (
        db.execute(
            select(Category).where(
                Category.name == "Alquiler cobrado",
                Category.kind == "income",
            )
        )
        .scalars()
        .first()
    )

    rent_identity = Transaction.generated_by == "rent_collection"
    if cat is not None:
        rent_identity = or_(
            rent_identity,
            Transaction.category_id == cat.id,
        )
    tx = (
        db.execute(
            select(Transaction).where(
                Transaction.period == normalized_period,
                rent_identity,
                Transaction.kind == "income",
                Transaction.status == "active",
            )
        )
        .scalars()
        .first()
    )

    is_collected = tx is not None
    adjust_alert_status = evaluate_rent_adjust_reminder(contract.next_adjust_date, today)
    collection_status = evaluate_rent_collection_status(normalized_period, today, is_collected)

    return RentStatusSummary(
        contract=contract,
        period=normalized_period,
        as_of_date=today,
        is_collected=is_collected,
        collection_transaction=tx,
        adjust_alert_status=adjust_alert_status,
        collection_status=collection_status,
        has_adjust_reminder=(
            adjust_alert_status in (RentAdjustAlertStatus.DUE_SOON, RentAdjustAlertStatus.OVERDUE)
        ),
        has_missing_collection_alert=(collection_status == RentCollectionAlertStatus.MISSING_ALERT),
    )
