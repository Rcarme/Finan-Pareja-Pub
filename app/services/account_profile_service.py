"""Audited correction of a photo account mistakenly classified as money (Q26)."""

from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db import acquire_advisory_lock
from app.core.transactions import ConcurrencyError, atomic_service, commit_service
from app.domain.ownership import credit_correction_delta
from app.models.auth import User
from app.models.cards import CardStatement
from app.models.close import Setting
from app.models.ledger import Account, Transaction
from app.models.ownership import OwnershipEntry
from app.services.ownership_service import get_ownership_summary


def account_has_history(db: Session, account_id: int) -> bool:
    """Even voided transactions or preview statements make reclassification unsafe."""
    return any(
        db.scalar(statement.limit(1)) is not None
        for statement in (
            select(Transaction.id).where(
                or_(Transaction.account_id == account_id, Transaction.to_account_id == account_id)
            ),
            select(CardStatement.id).where(CardStatement.account_id == account_id),
            select(OwnershipEntry.id).where(
                OwnershipEntry.account_id == account_id, OwnershipEntry.bucket == "physical"
            ),
        )
    )


def account_has_uncheckpointed_history(db: Session, account_id: int) -> bool:
    """The formal correction can keep old rows already absorbed by the opening photo."""
    opening = db.get(Setting, "ownership_opening")
    assert opening is not None
    value = opening.value
    return any(
        db.scalar(statement.limit(1)) is not None
        for statement in (
            select(Transaction.id).where(
                or_(Transaction.account_id == account_id, Transaction.to_account_id == account_id),
                or_(
                    Transaction.id > int(value["max_transaction_id"]),
                    Transaction.date > date.fromisoformat(value["date"]),
                ),
            ),
            select(CardStatement.id).where(CardStatement.account_id == account_id),
            select(OwnershipEntry.id).where(
                OwnershipEntry.account_id == account_id, OwnershipEntry.bucket == "physical"
            ),
        )
    )


def validate_virtual_conversion(db: Session, account: Account) -> None:
    opening = db.get(Setting, "ownership_opening")
    assert opening is not None
    value = opening.value
    balance = Decimal(str(value["account_balances"].get(str(account.id), "0")))
    used = db.scalar(
        select(Transaction.id)
        .where(
            Transaction.status == "active",
            or_(Transaction.account_id == account.id, Transaction.to_account_id == account.id),
            or_(
                Transaction.id > int(value["max_transaction_id"]),
                Transaction.date > date.fromisoformat(value["date"]),
            ),
        )
        .limit(1)
    )
    entries = db.scalar(
        select(OwnershipEntry.id)
        .where(OwnershipEntry.account_id == account.id, OwnershipEntry.bucket == "physical")
        .limit(1)
    )
    if (
        account.type == "credit"
        or account.currency != "ARS"
        or account.opening_balance
        or balance
        or used is not None
        or entries is not None
    ):
        raise ValueError(
            "Los saldos históricos son inmutables. Revisá los movimientos y el saldo de apertura "
            "antes de convertir esta cuenta en fondo calculado."
        )


@atomic_service
def correct_account_to_credit(
    db: Session,
    account_id: int,
    debt_amount: Decimal,
    owner_user_id: int | None,
    period: date,
    reason: str,
    user_id: int,
    expected_version: int,
) -> Account:
    """Append the signed physical correction; never rewrite the opening or allocations."""
    from app.services.ledger_service import check_period_open

    acquire_advisory_lock(db, 170170, shared=False)
    account = db.scalar(
        select(Account)
        .where(Account.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        account is None
        or not account.active
        or account.is_virtual
        or account.currency != "ARS"
        or account.type == "credit"
    ):
        raise ValueError("Elegí una cuenta real activa en pesos todavía clasificada como dinero.")
    if account.version != expected_version:
        raise ConcurrencyError("La cuenta cambió. Recargá y revisá la corrección.")
    owner = db.get(User, owner_user_id) if owner_user_id is not None else None
    if owner is None or not owner.active:
        raise ValueError("Elegí un titular activo para la tarjeta.")
    summary = get_ownership_summary(db)
    if summary is None:
        raise ValueError("Primero registrá la apertura del dinero.")
    normalized = period.replace(day=1)
    if normalized < summary.opening_date.replace(day=1):
        raise ValueError("La corrección no puede preceder la apertura.")
    check_period_open(db, normalized)
    delta = credit_correction_delta(
        summary.account_balances[account.id],
        debt_amount,
        reason,
        account_has_uncheckpointed_history(db, account.id),
    )
    before = {
        "type": account.type,
        "owner_user_id": account.owner_user_id,
        "balance": str(summary.account_balances[account.id]),
    }
    account.type = "credit"
    account.owner_user_id = owner_user_id
    account.version += 1
    if delta:
        entry = OwnershipEntry(
            bucket="physical",
            account_id=account.id,
            amount=delta,
            currency="ARS",
            period=normalized,
            reason=reason.strip(),
            created_by=user_id,
        )
        db.add(entry)
        db.flush()
        record_audit(
            db,
            "OwnershipEntry",
            entry.id,
            "INSERT",
            after={
                "bucket": "physical",
                "account_id": account.id,
                "amount": str(delta),
                "currency": "ARS",
                "reason": reason.strip(),
                "operation": "account_credit_correction",
            },
            user_id=user_id,
        )
    db.flush()
    record_audit(
        db,
        "Account",
        account.id,
        "UPDATE",
        before=before,
        after={
            "type": "credit",
            "owner_user_id": owner_user_id,
            "balance": str(-debt_amount),
            "reason": reason.strip(),
            "operation": "account_credit_correction",
        },
        user_id=user_id,
    )
    commit_service(db)
    return account
