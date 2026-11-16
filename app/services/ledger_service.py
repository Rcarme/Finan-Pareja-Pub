"""Servicios contables: transacciones, anulación, conciliación y saldos.

Reglas asociadas: RN-03 a RN-11, RN-18, RN-19, RN-40.
"""

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db import acquire_advisory_lock
from app.core.formatters import today_ar
from app.core.transactions import ConcurrencyError as ConcurrencyError
from app.core.transactions import atomic_service, commit_service
from app.domain.envelopes import EnvelopeMovementItem, compute_envelope_balance
from app.domain.ledger import (
    MovementItem,
    compute_account_balance,
    compute_total_fund,
    resolve_omission_period,
    validate_opening_omission,
)
from app.domain.ownership import validate_activity, validate_currency
from app.models.auth import User
from app.models.cards import CardStatement, MerchantRule
from app.models.close import Period, Setting
from app.models.envelopes import EnvelopeMovement
from app.models.ledger import Account, Category, Transaction
from app.models.obligations import ObligationItem
from app.models.recurring import RecurringPending, RecurringTemplate
from app.services.category_service import canonical_category
from app.services.ownership_service import get_ownership_summary, ownership_enabled


class ClosedPeriodError(Exception):
    """Excepción lanzada al intentar operar sobre un período cerrado (RN-11)."""


def _validate_transaction_references(
    db: Session,
    kind: str,
    account_id: int,
    category_id: int | None,
    to_account_id: int | None,
    charged_to_user_id: int | None,
    recipient_user_id: int | None,
    original: Transaction | None = None,
) -> Category | None:
    if kind not in {"income", "expense", "transfer", "distribution"}:
        raise ValueError("Tipo de movimiento inválido.")
    for selected_account_id in (account_id, to_account_id):
        if selected_account_id is None:
            continue
        account = db.get(Account, selected_account_id)
        historical = original is not None and selected_account_id in (
            original.account_id,
            original.to_account_id,
        )
        if account is None or (not account.active and not historical):
            raise ValueError("La cuenta debe existir y estar activa.")
        if account.currency == "USD":
            raise ValueError("Esta cuenta está en USD; no admite movimientos expresados en pesos.")
        if account.is_virtual is True and not historical:
            raise ValueError("El fondo común es calculado; elegí una cuenta o tarjeta real.")
    category = db.get(Category, category_id) if category_id is not None else None
    if kind in {"income", "expense"}:
        historical_category = original is not None and category_id == original.category_id
        if category is None or category.kind != kind:
            raise ValueError("La categoría debe ser compatible con el tipo de movimiento.")
        if not category.active and not historical_category:
            raise ValueError("La categoría debe estar activa.")
    elif category_id is not None:
        raise ValueError("Transferencias y retiros no llevan categoría.")
    for selected_user_id in (charged_to_user_id, recipient_user_id):
        if selected_user_id is not None:
            user = db.get(User, selected_user_id)
            if user is None or not user.active:
                raise ValueError("La persona debe existir y estar activa.")
    return category


def get_opening_date(db: Session) -> date | None:
    opening = db.get(Setting, "ownership_opening")
    return date.fromisoformat(opening.value["date"]) if isinstance(opening, Setting) else None


def _check_opening_mutation(db: Session, tx: Transaction, new_date: date | None = None) -> None:
    opening = db.get(Setting, "ownership_opening")
    if isinstance(opening, Setting):
        photo_date = date.fromisoformat(opening.value["date"])
        if not tx.internal_distribution and (
            (tx.id <= opening.value["max_transaction_id"] and tx.date <= photo_date)
            or (
                new_date is not None
                and new_date < photo_date
                and not (tx.opening_omission and tx.id > opening.value["max_transaction_id"])
            )
        ):
            raise ValueError(
                "El movimiento está incluido en la apertura; "
                "requiere una corrección auditada de la foto inicial."
            )


def resolve_transaction_period(
    tx_date: date, default_period_offset: int = 0, period: date | None = None
) -> date:
    """Comparte la imputación de categoría entre el alta y la edición simple."""
    if period is not None:
        return date(period.year, period.month, 1)
    total_month = tx_date.month - 1 + default_period_offset
    return date(tx_date.year + total_month // 12, total_month % 12 + 1, 1)


def check_period_open(db: Session, period_date: date) -> None:
    """Verifica que el período contable esté abierto y lo bloquea con FOR SHARE (RN-11, M-02)."""
    if not (2000 <= period_date.year <= 2100):
        raise ValueError(
            f"El período contable ({period_date:%Y-%m}) está fuera del rango permitido (2000-2100)."
        )
    p = date(period_date.year, period_date.month, 1)
    # El cierre usa el bloqueo exclusivo antes de calcular su snapshot.
    acquire_advisory_lock(db, 170170, shared=True)
    closed_cutoff = db.scalar(select(func.max(Period.period)).where(Period.status == "closed"))
    if closed_cutoff is not None and p <= closed_cutoff:
        raise ClosedPeriodError(
            f"El período {p:%Y-%m} afecta un período cerrado hasta {closed_cutoff:%Y-%m}; "
            "requiere reapertura formal."
        )
    db.execute(
        pg_insert(Period)
        .values(period=p, status="open")
        .on_conflict_do_nothing(index_elements=["period"])
    )
    period_record = db.execute(
        select(Period).where(Period.period == p).with_for_update(read=True)
    ).scalar_one_or_none()
    if period_record is not None and period_record.status == "closed":
        raise ClosedPeriodError(f"El período {p.strftime('%Y-%m')} se encuentra cerrado.")


def _tx_to_dict(tx: Transaction) -> dict[str, Any]:
    """Serializa los atributos de una transacción para auditoría."""
    return {
        "id": tx.id,
        "activity": tx.activity,
        "internal_distribution": tx.internal_distribution,
        "opening_omission": tx.opening_omission,
        "kind": tx.kind,
        "date": tx.date.isoformat() if tx.date else None,
        "period": tx.period.isoformat() if tx.period else None,
        "amount": str(tx.amount),
        "account_id": tx.account_id,
        "to_account_id": tx.to_account_id,
        "category_id": tx.category_id,
        "envelope_id": tx.envelope_id,
        "charged_to_user_id": tx.charged_to_user_id,
        "generated_by": tx.generated_by,
        "recipient_user_id": tx.recipient_user_id,
        "description": tx.description,
        "usd_amount": str(tx.usd_amount) if tx.usd_amount else None,
        "fx_rate_effective": str(tx.fx_rate_effective) if tx.fx_rate_effective else None,
        "status": tx.status,
        "void_reason": tx.void_reason,
        "voided_by": tx.voided_by,
        "voided_at": tx.voided_at.isoformat() if tx.voided_at else None,
        "idempotency_key": tx.idempotency_key,
        "version": tx.version,
    }


@atomic_service
def create_transaction(
    db: Session,
    kind: str,
    amount: Decimal,
    account_id: int,
    tx_date: date,
    period: date | None = None,
    description: str = "",
    user_id: int = 0,
    category_id: int | None = None,
    to_account_id: int | None = None,
    recipient_user_id: int | None = None,
    envelope_id: int | None = None,
    charged_to_user_id: int | None = None,
    usd_amount: Decimal | None = None,
    fx_rate_effective: Decimal | None = None,
    generated_by: str = "user",
    idempotency_key: str | None = None,
    commit: bool = True,
    use_category_default: bool = True,
    activity: str | None = None,
    internal_distribution: bool = False,
    opening_omission: bool = False,
) -> Transaction:
    """Crea un movimiento contable auditado con validación de período e idempotencia.

    Reglas: RN-03 a RN-07, RN-11, RN-18, RN-37, RN-41.
    """
    if idempotency_key:
        existing = db.execute(
            select(Transaction).where(Transaction.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    if not (2000 <= tx_date.year <= 2100):
        raise ValueError(
            f"La fecha de la transacción ({tx_date.isoformat()}) "
            "está fuera del rango contable permitido (2000-2100)."
        )
    if period is not None and not (2000 <= period.year <= 2100):
        raise ValueError(
            f"El período asignado ({period.isoformat()}) "
            "está fuera del rango contable permitido (2000-2100)."
        )

    if not description or not description.strip():
        raise ValueError("La descripción es obligatoria.")
    if len(description.strip()) > 255:
        raise ValueError("La descripción no puede superar los 255 caracteres.")

    max_amount = Decimal("999999999999.99")
    if amount <= Decimal("0.00"):
        raise ValueError("El monto debe ser estrictamente positivo.")
    if amount > max_amount:
        raise ValueError(
            "El monto supera el límite contable máximo permitido (999.999.999.999,99)."
        )
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    db.info.pop("_cached_ownership_summary", None)

    if kind in ("income", "expense") and category_id is None:
        raise ValueError("La categoría es obligatoria para ingresos y gastos (RN-05).")

    acquire_advisory_lock(db, 170170, shared=True)
    opening = db.get(Setting, "ownership_opening")
    photo_date = get_opening_date(db)
    validate_opening_omission(kind, tx_date, photo_date, opening_omission)
    if (
        isinstance(opening, Setting)
        and tx_date < date.fromisoformat(opening.value["date"])
        and not internal_distribution
        and not opening_omission
    ):
        raise ValueError(
            "La fecha está incluida en la foto inicial; "
            "registrá una corrección auditada de apertura."
        )
    cat = canonical_category(db, category_id) if category_id is not None else None
    if cat is not None:
        category_id = cat.id
    resolved_activity = validate_activity(
        activity or (cat.activity if cat and cat.activity else "household")
    )
    if resolved_activity == "business" and cat is not None and cat.activity != "business":
        raise ValueError("La actividad comercial requiere una categoría propia del negocio.")
    if resolved_activity == "business":
        charged_to_user_id = None
        use_category_default = False
    if resolved_activity == "personal" and charged_to_user_id is None:
        raise ValueError("El movimiento personal requiere indicar a quién corresponde.")
    if internal_distribution and (
        kind != "distribution" or generated_by != "close" or not ownership_enabled(db, period)
    ):
        raise ValueError("Solo el cierre con apertura puede registrar una asignación interna.")

    # Normalizar período; si no se especifica, aplicar default_period_offset (RN-18, H-05)
    normalized_period = resolve_transaction_period(
        tx_date, cat.default_period_offset if cat is not None else 0, period
    )

    if opening_omission:
        assert photo_date is not None
        normalized_period = resolve_omission_period(
            normalized_period, photo_date, period is not None
        )

    # Si no se especifica cargo personal, aplicar default_charged_to_user_id (RN-41, H-05)
    if (
        charged_to_user_id is None
        and cat is not None
        and cat.default_charged_to_user_id is not None
        and use_category_default
        and kind in ("income", "expense")
    ):
        charged_to_user_id = cat.default_charged_to_user_id

    check_period_open(db, normalized_period)

    _validate_transaction_references(
        db,
        kind,
        account_id,
        category_id,
        to_account_id,
        charged_to_user_id,
        recipient_user_id,
    )

    if kind == "transfer":
        if not to_account_id or to_account_id == account_id:
            raise ValueError("Una transferencia requiere una cuenta destino distinta de la origen.")
    elif kind == "distribution" and not recipient_user_id:
        raise ValueError("Una distribución requiere un usuario beneficiario.")

    tx = Transaction(
        activity=resolved_activity,
        internal_distribution=internal_distribution,
        opening_omission=opening_omission,
        kind=kind,
        amount=amount,
        account_id=account_id,
        to_account_id=to_account_id,
        category_id=category_id,
        envelope_id=envelope_id,
        charged_to_user_id=charged_to_user_id,
        date=tx_date,
        period=normalized_period,
        description=description.strip(),
        recipient_user_id=recipient_user_id,
        usd_amount=usd_amount,
        fx_rate_effective=fx_rate_effective,
        generated_by=generated_by,
        idempotency_key=idempotency_key,
        created_by=user_id,
        status="active",
        version=1,
    )
    db.add(tx)
    try:
        db.flush()
    except IntegrityError as exc:
        if idempotency_key and (
            "ix_transactions_idempotency_key" in str(exc)
            or "uq_transactions_idempotency_key" in str(exc)
        ):
            db.rollback()
            return db.execute(
                select(Transaction).where(Transaction.idempotency_key == idempotency_key)
            ).scalar_one()
        raise

    if kind == "expense" and envelope_id is not None:
        from app.services.envelope_service import spend_from_envelope

        spend_from_envelope(
            db=db,
            envelope_id=envelope_id,
            amount=amount,
            period=normalized_period,
            transaction_id=tx.id,
            user_id=user_id,
            commit=False,
        )

    record_audit(
        db=db,
        entity="Transaction",
        entity_id=tx.id,
        action="INSERT",
        after=_tx_to_dict(tx),
        user_id=user_id,
    )
    if commit:
        commit_service(db)
        db.refresh(tx)
    return tx


@atomic_service
def void_transaction(
    db: Session,
    transaction_id: int,
    reason: str,
    user_id: int,
    expected_version: int | None = None,
    commit: bool = True,
) -> Transaction:
    """Anula un movimiento contable con motivo y concurrencia (RN-09, RN-10, RN-11)."""
    if not reason or not reason.strip():
        raise ValueError("El motivo de anulación es obligatorio.")
    if len(reason.strip()) > 255:
        raise ValueError("El motivo de anulación no puede superar los 255 caracteres.")

    acquire_advisory_lock(db, 170170, shared=True)
    db.info.pop("_cached_ownership_summary", None)
    tx = db.get(Transaction, transaction_id)
    if tx is None:
        raise ValueError("Transacción no encontrada.")

    if tx.status == "voided":
        raise ValueError("El movimiento ya se encuentra anulado.")

    _check_opening_mutation(db, tx)
    check_period_open(db, tx.period)

    if expected_version is not None and tx.version != expected_version:
        msg = "Conflicto de concurrencia: el movimiento fue modificado por otro usuario."
        raise ConcurrencyError(msg)

    before = _tx_to_dict(tx)

    tx.status = "voided"
    tx.void_reason = reason.strip()
    tx.voided_by = user_id
    tx.voided_at = datetime.now(UTC)
    tx.version += 1

    # Si hay movimientos de sobres vinculados a esta transacción, anularlos
    envelope_movs = (
        db.execute(
            select(EnvelopeMovement).where(
                EnvelopeMovement.transaction_id == tx.id,
                EnvelopeMovement.status == "active",
            )
        )
        .scalars()
        .all()
    )
    for em in envelope_movs:
        em.status = "voided"
        record_audit(
            db=db,
            entity="EnvelopeMovement",
            entity_id=em.id,
            action="UPDATE",
            before={"id": em.id, "status": "active", "transaction_id": tx.id},
            after={"id": em.id, "status": "voided", "transaction_id": tx.id},
            user_id=user_id,
        )

    # Si había una cuota de departamento vinculada, revertirla a pendiente (RN-28)
    linked_item = db.execute(
        select(ObligationItem).where(ObligationItem.transaction_id == tx.id)
    ).scalar_one_or_none()
    if linked_item is not None:
        old_status = linked_item.status
        linked_item.status = "pendiente"
        linked_item.transaction_id = None
        record_audit(
            db=db,
            entity="ObligationItem",
            entity_id=linked_item.id,
            action="UPDATE",
            before={"id": linked_item.id, "status": old_status, "transaction_id": tx.id},
            after={"id": linked_item.id, "status": "pendiente", "transaction_id": None},
            user_id=user_id,
        )

    # Si había un pendiente recurrente vinculado, revertirlo a pendiente (RN-35)
    linked_recurring = db.execute(
        select(RecurringPending).where(RecurringPending.transaction_id == tx.id).with_for_update()
    ).scalar_one_or_none()
    if linked_recurring is not None:
        old_rec_status = linked_recurring.status
        linked_recurring.status = "pending"
        linked_recurring.transaction_id = None
        record_audit(
            db=db,
            entity="RecurringPending",
            entity_id=linked_recurring.id,
            action="UPDATE",
            before={"id": linked_recurring.id, "status": old_rec_status, "transaction_id": tx.id},
            after={"id": linked_recurring.id, "status": "pending", "transaction_id": None},
            user_id=user_id,
        )

    after = _tx_to_dict(tx)

    record_audit(
        db=db,
        entity="Transaction",
        entity_id=tx.id,
        action="UPDATE",
        before=before,
        after=after,
        user_id=user_id,
    )
    if commit:
        commit_service(db)
        db.refresh(tx)
    return tx


@atomic_service
def update_transaction(
    db: Session,
    transaction_id: int,
    kind: str,
    amount: Decimal,
    account_id: int,
    tx_date: date,
    period: date,
    description: str,
    user_id: int,
    expected_version: int,
    category_id: int | None = None,
    to_account_id: int | None = None,
    recipient_user_id: int | None = None,
    envelope_id: int | None = None,
    charged_to_user_id: int | None = None,
    usd_amount: Decimal | None = None,
    fx_rate_effective: Decimal | None = None,
    future_installments_update: bool = False,
    activity: str | None = None,
) -> Transaction:
    """Modifica una transacción existente con control de concurrencia optimista (RN-10)."""
    acquire_advisory_lock(db, 170170, shared=True)
    db.info.pop("_cached_ownership_summary", None)
    tx = db.get(Transaction, transaction_id)
    if tx is None:
        raise ValueError("Transacción no encontrada.")

    resolved_activity = validate_activity(activity or tx.activity or "household")
    if category_id is not None and category_id != tx.category_id:
        selected_category = canonical_category(db, category_id)
        if selected_category is not None:
            category_id = selected_category.id
    else:
        selected_category = db.get(Category, category_id) if category_id else None
    if (
        resolved_activity == "business"
        and selected_category is not None
        and selected_category.activity != "business"
    ):
        raise ValueError("La actividad comercial requiere una categoría propia del negocio.")
    if resolved_activity == "business":
        charged_to_user_id = None
    if resolved_activity == "personal" and charged_to_user_id is None:
        raise ValueError("El movimiento personal requiere indicar a quién corresponde.")
    if tx.status == "voided":
        raise ValueError("No se puede modificar un movimiento anulado.")

    if tx.generated_by == "rent_collection" and kind != "income":
        raise ValueError("Un cobro de alquiler debe conservar el tipo ingreso; anulá y recargá.")

    if tx.version != expected_version:
        msg = "Conflicto de concurrencia: el movimiento fue modificado por otro usuario."
        raise ConcurrencyError(msg)

    normalized_period = date(period.year, period.month, 1)
    _check_opening_mutation(db, tx, tx_date)
    if tx.opening_omission:
        if kind != "expense":
            raise ValueError("Una omisión de apertura debe conservar el tipo gasto.")
        photo_date = get_opening_date(db)
        assert photo_date is not None
        normalized_period = resolve_omission_period(normalized_period, photo_date, True)
    check_period_open(db, tx.period)
    if normalized_period != tx.period:
        check_period_open(db, normalized_period)

    # Validar inmutabilidad de movimientos vinculados a sobres o cuotas/refuerzos
    has_envelope = (tx.envelope_id is not None) or bool(
        db.execute(
            select(EnvelopeMovement).where(
                EnvelopeMovement.transaction_id == tx.id,
                EnvelopeMovement.status == "active",
            )
        )
        .scalars()
        .first()
    )
    has_obligation = bool(
        db.execute(select(ObligationItem).where(ObligationItem.transaction_id == tx.id))
        .scalars()
        .first()
    )

    if has_envelope or has_obligation:
        if kind != tx.kind:
            raise ValueError(
                "No se puede editar el tipo de un movimiento enlazado a un sobre o vencimiento; "
                "debe anular y recargar."
            )
        if amount != tx.amount:
            raise ValueError(
                "No se puede editar el monto de un movimiento enlazado a un sobre o vencimiento; "
                "debe anular y recargar."
            )
        if usd_amount != tx.usd_amount:
            raise ValueError(
                "No se puede editar el monto USD de un movimiento enlazado a un sobre o "
                "vencimiento; debe anular y recargar."
            )
        if normalized_period != tx.period:
            raise ValueError(
                "No se puede editar el período de un movimiento enlazado a un sobre o vencimiento; "
                "debe anular y recargar."
            )

    if has_envelope:
        if envelope_id != tx.envelope_id:
            raise ValueError(
                "No se puede modificar ni desvincular el sobre de un movimiento existente; "
                "debe anular y recargar."
            )
    elif envelope_id is not None:
        raise ValueError(
            "No se puede asignar un sobre a un movimiento existente; debe anular y recargar."
        )

    if not (2000 <= tx_date.year <= 2100):
        raise ValueError(
            f"La fecha de la transacción ({tx_date.isoformat()}) "
            "está fuera del rango contable permitido (2000-2100)."
        )
    if period is not None and not (2000 <= period.year <= 2100):
        raise ValueError(
            f"El período asignado ({period.isoformat()}) "
            "está fuera del rango contable permitido (2000-2100)."
        )

    if not description or not description.strip():
        raise ValueError("La descripción es obligatoria.")
    if len(description.strip()) > 255:
        raise ValueError("La descripción no puede superar los 255 caracteres.")

    if kind in ("income", "expense") and category_id is None:
        raise ValueError("La categoría es obligatoria para ingresos y gastos (RN-05).")

    max_amount = Decimal("999999999999.99")
    if amount <= Decimal("0.00"):
        raise ValueError("El monto debe ser estrictamente positivo.")
    if amount > max_amount:
        raise ValueError(
            "El monto supera el límite contable máximo permitido (999.999.999.999,99)."
        )
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    db.info.pop("_cached_ownership_summary", None)

    if kind == "transfer":
        if not to_account_id or to_account_id == account_id:
            raise ValueError("Una transferencia requiere una cuenta destino distinta de la origen.")
    elif kind == "distribution" and not recipient_user_id:
        raise ValueError("Una distribución requiere un usuario beneficiario.")

    before = _tx_to_dict(tx)

    _validate_transaction_references(
        db,
        kind,
        account_id,
        category_id,
        to_account_id,
        charged_to_user_id,
        recipient_user_id,
        original=tx,
    )

    if usd_amount is not None:
        if not usd_amount.is_finite() or usd_amount <= 0:
            raise ValueError("El monto USD debe ser positivo y finito.")
        fx_rate_effective = (amount / usd_amount).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        if fx_rate_effective <= 0 or fx_rate_effective > Decimal("9999999999.9999"):
            raise ValueError("La cotización efectiva está fuera del límite contable.")
    else:
        fx_rate_effective = None

    statements = db.scalars(
        select(CardStatement)
        .where(CardStatement.rows.contains([{"transaction_id": tx.id}]))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    for statement in statements:
        original_rows = statement.rows
        rows = [dict(row) for row in original_rows]
        for row in rows:
            if row.get("transaction_id") != tx.id:
                continue
            old_estimate = row.get(
                "future_estimated_amount",
                row["amount"]
                if row["currency"] == "USD"
                else row.get("posted_amount", row["amount"]),
            )
            if row["currency"] == "USD" and usd_amount is None:
                raise ValueError("Un consumo USD requiere conservar su importe en dólares.")
            row.update(
                posted_amount=str(amount),
                amount=str(usd_amount if usd_amount is not None else amount),
                date=tx_date.isoformat(),
                category_id=category_id,
                charged_to_user_id=charged_to_user_id,
                kind=kind,
            )
            if int(row.get("remaining_installments", 0)) > 0:
                row["future_estimated_amount"] = (
                    str(usd_amount if usd_amount is not None else amount)
                    if future_installments_update
                    else str(old_estimate)
                )
        statement.rows = rows
        record_audit(
            db,
            "CardStatement",
            statement.id,
            "UPDATE",
            {"rows": original_rows},
            {"rows": rows, "future_installments_update": future_installments_update},
            user_id,
        )

    tx.activity = resolved_activity
    tx.kind = kind
    tx.amount = amount
    tx.account_id = account_id
    tx.to_account_id = to_account_id
    tx.category_id = category_id
    tx.envelope_id = envelope_id
    tx.charged_to_user_id = charged_to_user_id
    tx.date = tx_date
    tx.period = normalized_period
    tx.description = description.strip()
    tx.recipient_user_id = recipient_user_id
    tx.usd_amount = usd_amount
    tx.fx_rate_effective = fx_rate_effective
    tx.version += 1

    after = _tx_to_dict(tx)

    record_audit(
        db=db,
        entity="Transaction",
        entity_id=tx.id,
        action="UPDATE",
        before=before,
        after=after,
        user_id=user_id,
    )
    commit_service(db)
    db.refresh(tx)
    return tx


def has_future_card_installments(db: Session, transaction_id: int) -> bool:
    """Consulta cuotas informadas por el resumen, sin lógica contable en las rutas."""
    statements = db.scalars(
        select(CardStatement).where(
            CardStatement.rows.contains([{"transaction_id": transaction_id}])
        )
    )
    return any(
        row.get("transaction_id") == transaction_id
        and int(row.get("remaining_installments", 0)) > 0
        for statement in statements
        for row in statement.rows
    )


def get_account_balance(db: Session, account_id: int) -> Decimal:
    """Obtiene el saldo contable actual de una cuenta (RN-08)."""
    account = db.get(Account, account_id)
    if account is None:
        raise ValueError(f"Cuenta con id {account_id} no encontrada.")

    summary = db.info.get("_cached_ownership_summary")
    if summary is None:
        summary = get_ownership_summary(db)
        if summary is not None:
            db.info["_cached_ownership_summary"] = summary
    if summary is not None and account_id in summary.account_balances:
        return summary.account_balances[account_id]
    rows = db.execute(
        select(
            Transaction.kind,
            Transaction.account_id,
            Transaction.to_account_id,
            func.sum(Transaction.amount),
        )
        .where(
            Transaction.status == "active",
            Transaction.internal_distribution.is_(False),
            or_(
                Transaction.account_id == account_id,
                and_(Transaction.kind == "transfer", Transaction.to_account_id == account_id),
            ),
        )
        .group_by(Transaction.kind, Transaction.account_id, Transaction.to_account_id)
    ).all()
    movements = [
        MovementItem(kind=kind, amount=amount, account_id=source, to_account_id=target)
        for kind, source, target, amount in rows
    ]
    return compute_account_balance(
        account_id=account.id,
        opening_balance=account.opening_balance,
        movements=movements,
    )


def get_total_fund(db: Session, period_cutoff: date | None = None) -> Decimal:
    """Calcula el total del Fondo Común activo hasta el período de corte (RN-06, RN-18)."""
    summary = get_ownership_summary(db, period_cutoff)
    if summary is not None:
        return summary.common_fund
    acc_stmt = select(Account).where(Account.active.is_(True), Account.currency == "ARS")
    if period_cutoff is not None:
        normalized_cutoff = date(period_cutoff.year, period_cutoff.month, 1)
        acc_stmt = acc_stmt.where(Account.opening_period <= normalized_cutoff)
    accounts = db.execute(acc_stmt).scalars().all()
    openings = {a.id: a.opening_balance for a in accounts}

    stmt = select(Transaction.kind, func.sum(Transaction.amount)).where(
        Transaction.status == "active",
        Transaction.account_id.in_(select(Account.id).where(Account.currency == "ARS")),
    )
    if period_cutoff is not None:
        stmt = stmt.where(Transaction.period <= period_cutoff)
    rows = db.execute(stmt.group_by(Transaction.kind)).all()
    movements = [MovementItem(kind=kind, amount=amount, account_id=0) for kind, amount in rows]
    return compute_total_fund(
        accounts_opening=openings,
        movements=movements,
        period_cutoff=period_cutoff,
    )


def get_available_fund(db: Session, period_cutoff: date | None = None) -> Decimal:
    """Calcula el Disponible(P) = Fondo(P) - suma de saldos de sobres (RN-12, RN-18)."""
    summary = get_ownership_summary(db, period_cutoff)
    total_fund = (
        summary.common_fund
        if summary is not None
        else get_total_fund(db, period_cutoff=period_cutoff)
    )

    env_stmt = select(EnvelopeMovement.kind, func.sum(EnvelopeMovement.amount)).where(
        EnvelopeMovement.status == "active"
    )
    opening = db.get(Setting, "ownership_opening") if summary is not None else None
    if opening is not None:
        env_stmt = env_stmt.where(
            or_(
                EnvelopeMovement.id > opening.value["max_envelope_movement_id"],
                EnvelopeMovement.period > date.fromisoformat(opening.value["date"]).replace(day=1),
            )
        )
    if period_cutoff is not None:
        env_stmt = env_stmt.where(EnvelopeMovement.period <= period_cutoff)
    movements = [
        EnvelopeMovementItem(kind=kind, amount=amount, envelope_id=0)
        for kind, amount in db.execute(env_stmt.group_by(EnvelopeMovement.kind))
    ]
    envelope_sum = compute_envelope_balance(0, movements)
    initial_savings = summary.savings_ars if summary else Decimal("0.00")
    return total_fund - envelope_sum - initial_savings


@atomic_service
def reconcile_account(
    db: Session,
    account_id: int,
    real_balance: Decimal,
    period: date,
    user_id: int,
    tx_date: date | None = None,
) -> Transaction | None:
    """Concilia una cuenta comparando saldo real vs esperado y generando ajuste (RN-19)."""
    # El cálculo y su ajuste excluyen todos los escritores contables coordinados.
    acquire_advisory_lock(db, 170170, shared=False)
    account = db.scalar(
        select(Account)
        .where(Account.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None or not account.active or account.is_virtual:
        raise ValueError("Elegí una cuenta real activa para conciliar.")
    if account.currency != "ARS":
        raise ValueError(
            "La conciliación en pesos no admite cuentas USD; "
            "usá una corrección auditada de apertura en dólares."
        )
    if not real_balance.is_finite() or abs(real_balance) > Decimal("999999999999.99"):
        raise ValueError("El saldo real debe ser finito y estar dentro del límite contable.")
    expected_balance = get_account_balance(db, account_id)
    difference = real_balance - expected_balance

    if difference == Decimal("0.00"):
        return None

    if difference > Decimal("0.00"):
        kind = "income"
        amount = difference
    else:
        kind = "expense"
        amount = abs(difference)

    # Buscar o crear categoría del sistema 'Ajuste de conciliación'
    cat = db.execute(
        select(Category).where(
            Category.name == "Ajuste de conciliación",
            Category.kind == kind,
            Category.is_system.is_(True),
        )
    ).scalar_one_or_none()

    if cat is None:
        cat = Category(
            name="Ajuste de conciliación",
            kind=kind,
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
            after={
                "name": cat.name,
                "kind": cat.kind,
                "is_system": cat.is_system,
                "active": cat.active,
            },
            user_id=user_id,
        )

    desc = f"Ajuste de conciliación (real: {real_balance} vs esperado: {expected_balance})"
    tx = create_transaction(
        db=db,
        kind=kind,
        amount=amount,
        account_id=account_id,
        category_id=cat.id,
        tx_date=tx_date or today_ar(),
        period=period,
        description=desc,
        user_id=user_id,
        commit=False,
    )
    commit_service(db)
    db.refresh(tx)
    return tx


def _invalidate_ai_cache() -> None:
    try:
        from app.services.ai_assistant_service import invalidate_ai_context_cache

        invalidate_ai_context_cache()
    except Exception:
        pass


@atomic_service
def create_account(
    db: Session,
    name: str,
    account_type: str,
    opening_balance: Decimal,
    user_id: int,
    owner_user_id: int | None = None,
    currency: str = "ARS",
    is_virtual: bool = False,
) -> Account:
    """Crea una cuenta contable del fondo común auditada (RN-08, RN-37, SPEC §4 Pantalla 11)."""
    if not name or not name.strip():
        raise ValueError("El nombre de la cuenta es obligatorio.")
    clean_name = name.strip()
    if len(clean_name) > 100:
        raise ValueError("El nombre de la cuenta no puede superar los 100 caracteres.")
    if account_type not in ("bank", "wallet", "cash", "credit"):
        raise ValueError("Tipo de cuenta inválido. Debe ser bank, wallet, cash o credit.")
    validate_currency(currency)
    if owner_user_id is not None:
        owner = db.get(User, owner_user_id)
        if owner is None or not owner.active:
            raise ValueError("El titular debe existir y estar activo.")
    acquire_advisory_lock(db, 170170, shared=True)
    if ownership_enabled(db) and opening_balance != Decimal("0"):
        raise ValueError(
            "Después de la apertura, una cuenta nueva comienza en cero; registrá su origen."
        )
    if opening_balance < Decimal("0.00"):
        raise ValueError("El saldo inicial no puede ser negativo.")

    existing = db.execute(
        select(Account).where(func.lower(Account.name) == clean_name.lower())
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"Ya existe una cuenta con el nombre '{clean_name}'.")

    today = today_ar()
    effective_opening_period = date(today.year, today.month, 1)
    latest_closed = db.scalar(select(func.max(Period.period)).where(Period.status == "closed"))
    if latest_closed is not None:
        next_open = resolve_transaction_period(latest_closed, 1)
        if next_open > effective_opening_period:
            effective_opening_period = next_open

    acc = Account(
        name=clean_name,
        type=account_type,
        owner_user_id=owner_user_id,
        currency=currency,
        is_virtual=is_virtual,
        opening_balance=opening_balance,
        opening_period=effective_opening_period,
        active=True,
        version=1,
    )
    db.add(acc)
    db.flush()

    record_audit(
        db=db,
        entity="Account",
        entity_id=acc.id,
        action="INSERT",
        after={
            "id": acc.id,
            "name": acc.name,
            "type": acc.type,
            "owner_user_id": acc.owner_user_id,
            "currency": acc.currency,
            "is_virtual": acc.is_virtual,
            "opening_balance": str(acc.opening_balance),
            "opening_period": acc.opening_period.isoformat(),
            "active": acc.active,
        },
        user_id=user_id,
    )
    commit_service(db)
    db.refresh(acc)
    _invalidate_ai_cache()
    return acc


@atomic_service
def rename_account(
    db: Session,
    account_id: int,
    new_name: str,
    user_id: int,
) -> Account:
    """Renombra una cuenta contable existente preservando historial y auditando (RN-03, RN-37)."""
    if not new_name or not new_name.strip():
        raise ValueError("El nombre de la cuenta es obligatorio.")
    clean_name = new_name.strip()
    if len(clean_name) > 100:
        raise ValueError("El nombre de la cuenta no puede superar los 100 caracteres.")

    acquire_advisory_lock(db, 170170, shared=True)
    acc = db.execute(
        select(Account)
        .where(Account.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if acc is None:
        raise ValueError("Cuenta contable no encontrada.")

    existing = db.execute(
        select(Account).where(
            func.lower(Account.name) == clean_name.lower(),
            Account.id != account_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"Ya existe una cuenta con el nombre '{clean_name}'.")

    old_name = acc.name
    if old_name == clean_name:
        return acc

    acc.name = clean_name
    acc.version += 1
    db.flush()

    record_audit(
        db=db,
        entity="Account",
        entity_id=acc.id,
        action="UPDATE",
        before={"id": acc.id, "name": old_name},
        after={"id": acc.id, "name": acc.name},
        user_id=user_id,
    )
    commit_service(db)
    db.refresh(acc)
    _invalidate_ai_cache()
    return acc


@atomic_service
def create_category(
    db: Session,
    name: str,
    kind: str,
    user_id: int,
    default_charged_to_user_id: int | None = None,
    default_period_offset: int = 0,
    activity: str = "household",
) -> Category:
    """Crea una categoría contable de ingresos o gastos auditada (RN-37, RN-41, SPEC §4)."""
    acquire_advisory_lock(db, 170170, shared=True)
    if not name or not name.strip():
        raise ValueError("El nombre de la categoría es obligatorio.")
    clean_name = name.strip()
    if len(clean_name) > 100:
        raise ValueError("El nombre de la categoría no puede superar los 100 caracteres.")
    validate_activity(activity)
    if activity == "business":
        default_charged_to_user_id = None
    if default_charged_to_user_id is not None:
        charged_user = db.get(User, default_charged_to_user_id)
        if charged_user is None or not charged_user.active:
            raise ValueError("La persona a cargo por defecto debe existir y estar activa.")
    if kind not in ("income", "expense"):
        raise ValueError("El tipo de categoría debe ser 'income' o 'expense'.")
    if default_period_offset not in (0, 1):
        raise ValueError("El desplazamiento de período debe ser 0 o 1.")

    existing = db.execute(
        select(Category)
        .where(func.lower(Category.name) == clean_name.lower())
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.merged_into_id is not None:
            raise ValueError("La categoría fue unificada; usá la categoría de destino.")
        if existing.active:
            raise ValueError(f"Ya existe una categoría con el nombre '{clean_name}'.")
        if existing.kind != kind:
            has_refs = (
                db.scalar(
                    select(Transaction.id).where(Transaction.category_id == existing.id).limit(1)
                )
                is not None
                or db.scalar(
                    select(RecurringTemplate.id)
                    .where(RecurringTemplate.category_id == existing.id)
                    .limit(1)
                )
                is not None
                or db.scalar(
                    select(MerchantRule.id).where(MerchantRule.category_id == existing.id).limit(1)
                )
                is not None
            )
            if has_refs:
                raise ValueError(
                    "No se puede cambiar el tipo de una categoría con movimientos o reglas "
                    "registradas."
                )
        before_state = {
            "id": existing.id,
            "name": existing.name,
            "kind": existing.kind,
            "active": existing.active,
        }
        existing.name = clean_name
        existing.kind = kind
        existing.activity = activity
        existing.default_charged_to_user_id = default_charged_to_user_id
        existing.default_period_offset = default_period_offset
        existing.active = True
        existing.version += 1
        db.flush()
        record_audit(
            db=db,
            entity="Category",
            entity_id=existing.id,
            action="UPDATE",
            before=before_state,
            after={
                "id": existing.id,
                "name": existing.name,
                "kind": existing.kind,
                "default_charged_to_user_id": existing.default_charged_to_user_id,
                "default_period_offset": existing.default_period_offset,
                "active": existing.active,
            },
            user_id=user_id,
        )
        commit_service(db)
        db.refresh(existing)
        return existing

    cat = Category(
        activity=activity,
        name=clean_name,
        kind=kind,
        default_charged_to_user_id=default_charged_to_user_id,
        default_period_offset=default_period_offset,
        is_system=False,
        active=True,
        version=1,
    )
    db.add(cat)
    db.flush()

    record_audit(
        db=db,
        entity="Category",
        entity_id=cat.id,
        action="INSERT",
        after={
            "id": cat.id,
            "name": cat.name,
            "kind": cat.kind,
            "default_charged_to_user_id": cat.default_charged_to_user_id,
            "default_period_offset": cat.default_period_offset,
            "active": cat.active,
        },
        user_id=user_id,
    )
    commit_service(db)
    db.refresh(cat)
    _invalidate_ai_cache()
    return cat


@atomic_service
def delete_category(
    db: Session,
    category_id: int,
    user_id: int,
) -> Category:
    """Desactiva una categoría contable preservando historial y auditando (RN-05, RN-37)."""
    acquire_advisory_lock(db, 170170, shared=True)
    cat = db.execute(
        select(Category)
        .where(Category.id == category_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if cat is None or not cat.active:
        raise ValueError("Categoría no encontrada.")
    if cat.is_system:
        raise ValueError("No se pueden eliminar categorías del sistema.")
    has_aliases = (
        db.scalar(select(Category.id).where(Category.merged_into_id == cat.id).limit(1)) is not None
    )
    if has_aliases:
        raise ValueError("No se puede desactivar una categoría que agrupa alias históricos.")

    cat.active = False
    cat.version += 1
    db.flush()

    record_audit(
        db=db,
        entity="Category",
        entity_id=cat.id,
        action="DELETE",
        before={"id": cat.id, "name": cat.name, "active": True},
        after={"id": cat.id, "name": cat.name, "active": False},
        user_id=user_id,
    )
    commit_service(db)
    db.refresh(cat)
    _invalidate_ai_cache()
    return cat


@atomic_service
def configure_account(
    db: Session,
    account_id: int,
    account_type: str,
    owner_user_id: int | None,
    currency: str,
    is_virtual: bool,
    user_id: int,
) -> Account:
    """Define physical account metadata without changing historical transactions."""
    validate_currency(currency)
    if account_type not in {"bank", "wallet", "cash", "credit"}:
        raise ValueError("Tipo de cuenta inválido.")
    acquire_advisory_lock(db, 170170, shared=False)
    account = db.scalar(
        select(Account)
        .where(Account.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise ValueError("Cuenta no encontrada.")
    if owner_user_id is not None:
        owner = db.get(User, owner_user_id)
        if owner is None or not owner.active:
            raise ValueError("El titular debe existir y estar activo.")
    if currency != account.currency and (
        account.opening_balance != Decimal("0")
        or db.scalar(
            select(Transaction.id)
            .where(
                or_(Transaction.account_id == account_id, Transaction.to_account_id == account_id)
            )
            .limit(1)
        )
        is not None
    ):
        raise ValueError("Una cuenta con movimientos no puede cambiar de moneda.")
    if ownership_enabled(db) and currency != account.currency:
        raise ValueError(
            "La moneda y la ubicación de una cuenta incluida en la apertura son inmutables."
        )
    if ownership_enabled(db) and is_virtual != account.is_virtual:
        from app.services.account_profile_service import validate_virtual_conversion

        if account.is_virtual or not is_virtual:
            raise ValueError("Un fondo calculado no puede convertirse en cuenta real.")
        validate_virtual_conversion(db, account)
    if (
        ownership_enabled(db)
        and account_type != account.type
        and "credit" in {account_type, account.type}
    ):
        from app.services.account_profile_service import account_has_history

        summary = get_ownership_summary(db)
        assert summary is not None
        if (
            account_type != "credit"
            or account.is_virtual
            or is_virtual
            or account.currency != "ARS"
            or account.opening_balance
            or summary.account_balances.get(account.id, Decimal("0"))
            or account_has_history(db, account.id)
        ):
            raise ValueError("No se puede cambiar entre dinero y deuda después de la apertura.")
    before = {
        "type": account.type,
        "owner_user_id": account.owner_user_id,
        "currency": account.currency,
        "is_virtual": account.is_virtual,
    }
    account.type, account.owner_user_id, account.currency, account.is_virtual = (
        account_type,
        owner_user_id,
        currency,
        is_virtual,
    )
    account.version += 1
    db.flush()
    record_audit(
        db,
        "Account",
        account.id,
        "UPDATE",
        before=before,
        after={
            "type": account.type,
            "owner_user_id": account.owner_user_id,
            "currency": account.currency,
            "is_virtual": account.is_virtual,
        },
        user_id=user_id,
    )
    commit_service(db)
    _invalidate_ai_cache()
    return account
