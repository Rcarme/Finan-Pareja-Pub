"""Servicio transaccional de cierre de mes, cálculo y reapertura (RN-20 a RN-26).

Orquesta las operaciones contables del cierre en una ÚNICA transacción atómica,
respetando RN-11, RN-24, RN-25, RN-37 y el invariante post-cierre (Disponible == retenido).
"""

import contextlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db import acquire_advisory_lock
from app.core.formatters import today_ar
from app.core.transactions import atomic_service, commit_service
from app.domain.close_engine import (
    CloseCalculationInput,
    CloseCalculationResult,
    CloseSplitConfig,
    build_close_snapshot_payload,
    calculate_close,
)
from app.models.auth import User
from app.models.close import Carryover, Close, Period, Setting
from app.models.envelopes import Envelope, EnvelopeMovement
from app.models.ledger import Transaction
from app.models.recurring import RecurringPending
from app.services.envelope_service import (
    allocate_to_envelope,
    get_envelope_balance,
    validate_envelope_deductions,
)
from app.services.idempotency_service import (
    DuplicateCompletedError,
    abort_operation,
    begin_operation,
    finish_operation,
)
from app.services.ledger_service import (
    ClosedPeriodError as ClosedPeriodError,
)
from app.services.ledger_service import (
    ConcurrencyError as ConcurrencyError,
)
from app.services.ledger_service import (
    check_period_open,
    create_transaction,
    get_available_fund,
    void_transaction,
)
from app.services.obligation_service import get_next_reinforcement_provision
from app.services.ownership_service import (
    ownership_enabled,
    personal_charge_conditions,
    record_personal_allocation,
    reverse_close_allocations,
)


@dataclass(frozen=True)
class ClosePreviewDTO:
    """Datos de previsualización para el asistente de cierre de mes (RN-20)."""

    period: date
    result: CloseCalculationResult
    is_period_ended: bool
    pending_recurring_count: int
    warnings: list[str]


def get_close_split_config(db: Session) -> CloseSplitConfig:
    """Lee la configuración de porcentajes de cierre desde settings (RN-23)."""
    setting = db.get(Setting, "close_split")
    if setting is None:
        return CloseSplitConfig()

    val = setting.value
    cfg = CloseSplitConfig(
        savings_pct=Decimal(str(val.get("ahorro", 30))),
        buffer_pct=Decimal(str(val.get("colchon", 20))),
        distribution_pct=Decimal(str(val.get("repartir", 50))),
    )
    cfg.validate()
    return cfg


@atomic_service
def update_close_split_config(db: Session, config: CloseSplitConfig, user_id: int) -> None:
    """Actualiza la configuración de porcentajes auditada (RN-23, RN-37, N-02)."""
    config.validate()
    setting = db.get(Setting, "close_split")
    new_val = {
        "ahorro": str(config.savings_pct),
        "colchon": str(config.buffer_pct),
        "repartir": str(config.distribution_pct),
    }
    before = setting.value if setting else None

    if setting is None:
        setting = Setting(key="close_split", value=new_val, updated_by=user_id)
        db.add(setting)
        action = "INSERT"
    else:
        setting.value = new_val
        setting.updated_by = user_id
        action = "UPDATE"

    db.flush()
    record_audit(
        db=db,
        entity="Setting",
        entity_id=1,
        action=action,
        before={"key": "close_split", "value": before} if before else None,
        after={"key": "close_split", "value": new_val},
        user_id=user_id,
    )
    commit_service(db)


def _get_active_pair(db: Session) -> tuple[User, User]:
    stmt = select(User).where(User.active.is_(True)).order_by(User.id)
    users = db.execute(stmt).scalars().all()
    user_a = users[0]
    user_b = users[1] if len(users) > 1 else users[0]
    return user_a, user_b


def _protect_opening_history(db: Session, period: date) -> None:
    opening = db.get(Setting, "ownership_opening")
    if isinstance(opening, Setting) and period < date.fromisoformat(opening.value["date"]).replace(
        day=1
    ):
        raise ClosedPeriodError("El período precede la apertura; conserva su cierre histórico.")


def preview_close(
    db: Session,
    period: date,
    reinforcement_reservation: Decimal | None = None,
    split_config: CloseSplitConfig | None = None,
) -> ClosePreviewDTO:
    """Genera la vista previa idéntica al resultado final del cierre (RN-20 a RN-23, RN-26)."""
    normalized_period = date(period.year, period.month, 1)
    available = get_available_fund(db, normalized_period)

    # 1. Sugerencia de provisión de refuerzos (RN-30)
    if reinforcement_reservation is None:
        provision_sugg = get_next_reinforcement_provision(db, normalized_period)
        raw_r = provision_sugg.monthly_suggested if provision_sugg is not None else Decimal("0.00")
        max_r = max(available, Decimal("0.00"))
        r = min(raw_r, max_r)
    else:
        r = reinforcement_reservation

    # 2. Usuarios
    user_a, user_b = _get_active_pair(db)

    # 3. Cargos personales del período (gastos - reintegros) (RN-40, RN-42)
    def _user_period_charges(uid: int) -> Decimal:
        exp = db.execute(
            select(func.coalesce(func.sum(Transaction.amount), Decimal("0.00"))).where(
                *personal_charge_conditions(db, normalized_period, uid, "expense"),
            )
        ).scalar_one()
        inc = db.execute(
            select(func.coalesce(func.sum(Transaction.amount), Decimal("0.00"))).where(
                *personal_charge_conditions(db, normalized_period, uid, "income"),
            )
        ).scalar_one()
        return Decimal(str(exp)) - Decimal(str(inc))

    charges_a = _user_period_charges(user_a.id)
    charges_b = _user_period_charges(user_b.id)

    # 4. Arrastres pendientes no aplicados
    def _user_carryovers(uid: int) -> Decimal:
        val = db.execute(
            select(func.coalesce(func.sum(Carryover.amount), Decimal("0.00"))).where(
                Carryover.user_id == uid,
                Carryover.applied_in_period.is_(None),
                Carryover.status == "active",
                Carryover.from_period < normalized_period,
            )
        ).scalar_one()
        return Decimal(str(val))

    carryover_a = _user_carryovers(user_a.id)
    carryover_b = _user_carryovers(user_b.id)

    # 5. Colchón (tope y saldo)
    buffer_env = (
        db.execute(select(Envelope).where(Envelope.name == "Colchón", Envelope.active.is_(True)))
        .scalars()
        .first()
    )
    buffer_target = buffer_env.target_amount if buffer_env else None
    buffer_balance = (
        get_envelope_balance(db, buffer_env.id, normalized_period)
        if buffer_env
        else Decimal("0.00")
    )

    cfg = split_config if split_config is not None else get_close_split_config(db)

    calc_input = CloseCalculationInput(
        period=normalized_period,
        available=available,
        reinforcement_reservation=r,
        user_a_id=user_a.id,
        user_b_id=user_b.id,
        user_a_charges=charges_a,
        user_b_charges=charges_b,
        user_a_carryover=carryover_a,
        user_b_carryover=carryover_b,
        buffer_target=buffer_target,
        buffer_current_balance=buffer_balance,
        split_config=cfg,
    )
    result = calculate_close(calc_input)

    # Verificaciones y advertencias (RN-26)
    warnings: list[str] = []
    today = today_ar()
    current_first = date(today.year, today.month, 1)
    is_period_ended = normalized_period < current_first
    if not is_period_ended:
        warnings.append("El período aún no ha concluido (cierre anticipado).")

    # Recurrentes pendientes
    pending_rec_count = len(
        db.execute(
            select(RecurringPending).where(
                RecurringPending.period == normalized_period,
                RecurringPending.status == "pending",
            )
        )
        .scalars()
        .all()
    )
    if pending_rec_count > 0:
        warnings.append(
            f"Hay {pending_rec_count} gasto(s) recurrente(s) pendiente(s) "
            "sin resolver en este período."
        )

    return ClosePreviewDTO(
        period=normalized_period,
        result=result,
        is_period_ended=is_period_ended,
        pending_recurring_count=pending_rec_count,
        warnings=warnings,
    )


@atomic_service
def execute_close(
    db: Session,
    period: date,
    reinforcement_reservation: Decimal,
    destination_accounts: dict[int, int],
    user_id: int,
    savings_destinations: dict[int, Decimal] | None = None,
    idempotency_key: str | None = None,
    split_config: CloseSplitConfig | None = None,
) -> Close:
    """Ejecuta el cierre de mes de forma atómica en una única transacción (RN-24)."""
    normalized_period = date(period.year, period.month, 1)
    _protect_opening_history(db, normalized_period)

    # Idempotencia previa
    idem = None
    if idempotency_key:
        try:
            idem = begin_operation(
                db,
                token=idempotency_key,
                route="close.execute",
                user_id=user_id,
                payload={
                    "period": normalized_period.isoformat(),
                    "reservation": str(reinforcement_reservation),
                    "accounts": destination_accounts,
                    "savings": savings_destinations,
                    "split": str(split_config),
                },
            )
        except DuplicateCompletedError as err:
            active_close = db.execute(
                select(Close).where(Close.period == normalized_period, Close.status == "active")
            ).scalar_one_or_none()
            if active_close is not None:
                return active_close
            raise ConcurrencyError(
                "Operación de cierre duplicada o previamente revertida."
            ) from err

    acquire_advisory_lock(db, 170170, shared=False)
    try:
        # Bloqueo estricto SELECT ... FOR UPDATE sobre el período
        period_row = db.execute(
            select(Period).where(Period.period == normalized_period).with_for_update()
        ).scalar_one_or_none()

        if period_row is None:
            period_row = Period(period=normalized_period, status="open")
            db.add(period_row)
            db.flush()
        elif period_row.status == "closed":
            raise ClosedPeriodError(
                f"El período {normalized_period.strftime('%Y-%m')} ya se encuentra cerrado."
            )

        check_period_open(db, normalized_period)

        # Cálculo exacto idéntico al preview
        preview = preview_close(
            db=db,
            period=normalized_period,
            reinforcement_reservation=reinforcement_reservation,
            split_config=split_config,
        )
        res = preview.result
        user_a, user_b = _get_active_pair(db)

        # Validar destinos de ahorro (RN-44 / H-15): debe coincidir exactamente con el ahorro
        # tanto en superávit como en déficit (donde savings_allocation es 0.00)
        if savings_destinations:
            if any(
                not amount.is_finite() or amount < 0 for amount in savings_destinations.values()
            ):
                raise ValueError("Los destinos de ahorro deben ser finitos y no negativos.")
            dest_sum = sum(savings_destinations.values())
            if dest_sum != res.savings_allocation:
                raise ValueError(
                    f"La suma de los destinos de ahorro ({dest_sum}) "
                    f"debe coincidir exactamente con el ahorro ({res.savings_allocation})."
                )

        if not res.is_deficit:
            for uid, amount in (
                (user_a.id, res.distribution_user_a),
                (user_b.id, res.distribution_user_b),
            ):
                if amount > Decimal("0.00") and not destination_accounts.get(uid):
                    raise ValueError(
                        "Seleccioná una cuenta de destino para todos los retiros del cierre."
                    )

        # Snapshot inmutable con esquema único (C-02)
        snapshot_payload = dict(
            build_close_snapshot_payload(
                result=res,
                user_a_id=user_a.id,
                user_b_id=user_b.id,
                destination_accounts=destination_accounts,
                savings_destinations=savings_destinations,
            )
        )
        applied_split = split_config or get_close_split_config(db)
        snapshot_payload["split_config"] = {
            "savings_pct": str(applied_split.savings_pct),
            "buffer_pct": str(applied_split.buffer_pct),
            "distribution_pct": str(applied_split.distribution_pct),
        }

        # Registro del cierre
        close_record = Close(
            period=normalized_period,
            repartible_bruto=res.distributable,
            parte=res.share_each,
            retenido=res.retained,
            payload=snapshot_payload,
            status="active",
            idempotency_key=idempotency_key,
            created_by=user_id,
        )
        db.add(close_record)
        db.flush()

        # Sobres del sistema
        env_refuerzos = db.execute(
            select(Envelope).where(Envelope.name == "Refuerzos depto")
        ).scalar_one()
        env_colchon = db.execute(select(Envelope).where(Envelope.name == "Colchón")).scalar_one()
        env_ahorro = db.execute(
            select(Envelope).where(Envelope.name == "Ahorro común")
        ).scalar_one()

        if not res.is_deficit:
            # 1. Reserva de refuerzos
            if res.reinforcement_reservation > Decimal("0.00"):
                allocate_to_envelope(
                    db=db,
                    envelope_id=env_refuerzos.id,
                    amount=res.reinforcement_reservation,
                    period=normalized_period,
                    user_id=user_id,
                    commit=False,
                    close_id=close_record.id,
                )

            # 2. Colchón
            if res.buffer_allocation > Decimal("0.00"):
                allocate_to_envelope(
                    db=db,
                    envelope_id=env_colchon.id,
                    amount=res.buffer_allocation,
                    period=normalized_period,
                    user_id=user_id,
                    commit=False,
                    close_id=close_record.id,
                )

            # 3. Ahorro (con posible redistribución RN-44)
            if res.savings_allocation > Decimal("0.00"):
                if savings_destinations:
                    for env_id, amt in savings_destinations.items():
                        if amt > Decimal("0.00"):
                            allocate_to_envelope(
                                db=db,
                                envelope_id=env_id,
                                amount=amt,
                                period=normalized_period,
                                user_id=user_id,
                                commit=False,
                                close_id=close_record.id,
                            )
                else:
                    allocate_to_envelope(
                        db=db,
                        envelope_id=env_ahorro.id,
                        amount=res.savings_allocation,
                        period=normalized_period,
                        user_id=user_id,
                        commit=False,
                        close_id=close_record.id,
                    )

            # 4. Retiros / Distribuciones (RN-07, RN-22)
            user_distributions = [
                (user_a.id, res.distribution_user_a),
                (user_b.id, res.distribution_user_b),
            ]
            for uid, amount in user_distributions:
                if amount > Decimal("0.00"):
                    account_id = destination_accounts[uid]
                    create_transaction(
                        db=db,
                        kind="distribution",
                        amount=amount,
                        account_id=account_id,
                        recipient_user_id=uid,
                        period=normalized_period,
                        tx_date=normalized_period,
                        description=f"Retiro cierre {normalized_period.strftime('%Y-%m')}",
                        user_id=user_id,
                        generated_by="close",
                        internal_distribution=ownership_enabled(db, normalized_period),
                        commit=False,
                    )
                    if ownership_enabled(db, normalized_period):
                        record_personal_allocation(
                            db, uid, amount, normalized_period, close_record.id, user_id
                        )

        # 5. Marcar arrastres previos aplicados ANTES de insertar los nuevos (C-01)
        # Se aplica tanto en superávit como en déficit porque res.total_charges_a/b
        # ya absorbe los arrastres previos pendientes.
        prev_carryovers = (
            db.execute(
                select(Carryover).where(
                    Carryover.applied_in_period.is_(None),
                    Carryover.status == "active",
                    Carryover.from_period < normalized_period,
                )
            )
            .scalars()
            .all()
        )
        for co in prev_carryovers:
            co.applied_in_period = normalized_period
            record_audit(
                db=db,
                entity="Carryover",
                entity_id=co.id,
                action="UPDATE",
                before={"applied_in_period": None, "status": "active"},
                after={"applied_in_period": normalized_period.isoformat(), "status": "active"},
                user_id=user_id,
            )

        # 6. Insertar nuevos arrastres generados en el período actual
        created_carryovers: list[Carryover] = []
        if res.new_carryover_user_a > Decimal("0.00"):
            co_a = Carryover(
                user_id=user_a.id,
                from_period=normalized_period,
                amount=res.new_carryover_user_a,
            )
            db.add(co_a)
            created_carryovers.append(co_a)
        if res.new_carryover_user_b > Decimal("0.00"):
            co_b = Carryover(
                user_id=user_b.id,
                from_period=normalized_period,
                amount=res.new_carryover_user_b,
            )
            db.add(co_b)
            created_carryovers.append(co_b)
        if created_carryovers:
            db.flush()
            for co in created_carryovers:
                record_audit(
                    db=db,
                    entity="Carryover",
                    entity_id=co.id,
                    action="INSERT",
                    before=None,
                    after={
                        "user_id": co.user_id,
                        "from_period": co.from_period.isoformat(),
                        "amount": str(co.amount),
                        "status": co.status,
                    },
                    user_id=user_id,
                )

        # Actualizar período cerrado
        period_row.status = "closed"
        period_row.closed_at = datetime.now(UTC)
        period_row.closed_by = user_id
        period_row.close_snapshot = snapshot_payload

        # Auditoría completa (RN-37)
        record_audit(
            db=db,
            entity="Period",
            entity_id=int(normalized_period.strftime("%Y%m%d")),
            action="close",
            before={"period": normalized_period.isoformat(), "status": "open"},
            after={"period": normalized_period.isoformat(), "status": "closed"},
            user_id=user_id,
        )
        record_audit(
            db=db,
            entity="Close",
            entity_id=close_record.id,
            action="INSERT",
            before=None,
            after=snapshot_payload,
            user_id=user_id,
        )

        if idem is not None:
            finish_operation(db, idem, result_entity="Close", result_id=close_record.id)

        # Un solo commit atómico para toda la operación (RN-24)
        commit_service(db)
        db.refresh(close_record)
        return close_record

    except Exception:
        db.rollback()
        if idem is not None:
            with contextlib.suppress(Exception):
                abort_operation(db, idem)
        raise


@atomic_service
def reopen_close(db: Session, period: date, reason: str, user_id: int) -> Close:
    """Reabre formalmente el último período cerrado con motivo y reversión (RN-25)."""
    if not reason or not reason.strip():
        raise ValueError("El motivo de reapertura es obligatorio.")
    if len(reason.strip()) > 255:
        raise ValueError("El motivo de reapertura no puede superar los 255 caracteres.")

    normalized_period = date(period.year, period.month, 1)
    _protect_opening_history(db, normalized_period)

    # 1. Bloqueo estricto SELECT ... FOR UPDATE
    acquire_advisory_lock(db, 170170, shared=False)
    period_row = db.execute(
        select(Period)
        .where(Period.period == normalized_period)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    if period_row is None or period_row.status != "closed":
        raise ValueError(
            f"El período {normalized_period.strftime('%Y-%m')} no se encuentra cerrado."
        )

    # 2. Regla inquebrantable: nunca reabrir si hay otro posterior cerrado (RN-25)
    subsequent_closed = (
        db.execute(
            select(Period).where(
                Period.period > normalized_period,
                Period.status == "closed",
            )
        )
        .scalars()
        .all()
    )
    if len(subsequent_closed) > 0:
        raise ClosedPeriodError(
            "No se puede reabrir un período si existe otro posterior cerrado (RN-25)."
        )

    # 3. Obtener el Close activo
    close_record = db.execute(
        select(Close).where(Close.period == normalized_period, Close.status == "active")
    ).scalar_one()

    envelope_movs = list(
        db.scalars(
            select(EnvelopeMovement).where(
                EnvelopeMovement.close_id == close_record.id,
                EnvelopeMovement.status == "active",
            )
        )
    )
    deductions: dict[int, dict[date, Decimal]] = {}
    for movement in envelope_movs:
        periods = deductions.setdefault(movement.envelope_id, {})
        amount = movement.amount if movement.kind == "allocate" else -movement.amount
        periods[movement.period] = periods.get(movement.period, Decimal("0.00")) + amount
    for envelope_id in sorted(deductions):
        validate_envelope_deductions(db, envelope_id, deductions[envelope_id])

    # Abrir período temporalmente para permitir anular transacciones del mismo (RN-11)
    period_row.status = "open"
    db.flush()
    period_row.closed_at = None
    period_row.closed_by = None
    period_row.close_snapshot = None

    # 4. Anular transacciones de retiro creadas por el cierre
    distribution_txs = (
        db.execute(
            select(Transaction).where(
                Transaction.period == normalized_period,
                Transaction.kind == "distribution",
                Transaction.generated_by == "close",
                Transaction.status == "active",
            )
        )
        .scalars()
        .all()
    )
    for dt in distribution_txs:
        void_transaction(
            db=db,
            transaction_id=dt.id,
            reason=f"Reapertura de período: {reason}",
            user_id=user_id,
            commit=False,
        )

    reverse_close_allocations(db, close_record.id, user_id, f"Reapertura: {reason}")

    # 5. Anular movimientos de sobres originados por este cierre
    for em in envelope_movs:
        em.status = "voided"
        record_audit(
            db=db,
            entity="EnvelopeMovement",
            entity_id=em.id,
            action="VOID",
            before={"id": em.id, "status": "active"},
            after={"id": em.id, "status": "voided"},
            user_id=user_id,
        )

    # 6. Revertir arrastres con auditoría (RN-37, H-17)
    created_carryovers = (
        db.execute(
            select(Carryover).where(
                Carryover.from_period == normalized_period, Carryover.status == "active"
            )
        )
        .scalars()
        .all()
    )
    for co in created_carryovers:
        record_audit(
            db=db,
            entity="Carryover",
            entity_id=co.id,
            action="UPDATE",
            before={
                "id": co.id,
                "user_id": co.user_id,
                "amount": str(co.amount),
                "from_period": str(co.from_period),
                "status": co.status,
            },
            after={"status": "reverted"},
            user_id=user_id,
        )
        co.status = "reverted"

    applied_carryovers = (
        db.execute(
            select(Carryover).where(
                Carryover.applied_in_period == normalized_period, Carryover.status == "active"
            )
        )
        .scalars()
        .all()
    )
    for co in applied_carryovers:
        record_audit(
            db=db,
            entity="Carryover",
            entity_id=co.id,
            action="UPDATE",
            before={"applied_in_period": str(co.applied_in_period)},
            after={"applied_in_period": None},
            user_id=user_id,
        )
        co.applied_in_period = None

    # 7. Revertir Close y Period
    before_close = {
        "status": close_record.status,
        "reverted_reason": close_record.reverted_reason,
    }
    close_record.status = "reverted"
    close_record.reverted_reason = reason
    close_record.reverted_by = user_id
    close_record.reverted_at = datetime.now(UTC)

    period_row.status = "open"
    period_row.closed_at = None
    period_row.closed_by = None
    period_row.close_snapshot = None

    record_audit(
        db=db,
        entity="Close",
        entity_id=close_record.id,
        action="REVERT",
        before=before_close,
        after={
            "status": "reverted",
            "reverted_reason": reason,
            "reverted_by": user_id,
        },
        user_id=user_id,
    )
    record_audit(
        db=db,
        entity="Period",
        entity_id=int(normalized_period.strftime("%Y%m%d")),
        action="reopen",
        before={"period": normalized_period.isoformat(), "status": "closed"},
        after={"period": normalized_period.isoformat(), "status": "open"},
        user_id=user_id,
    )

    # Un solo commit atómico
    commit_service(db)
    db.refresh(close_record)
    return close_record
