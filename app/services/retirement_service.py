"""Historia externa auditada y lectura de primas sin nuevas reservas ni egresos."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.core.audit import record_audit
from app.core.db import acquire_advisory_lock
from app.core.formatters import today_ar
from app.core.transactions import atomic_service, commit_service
from app.domain.retirement import (
    RetirementContributionInput,
    RetirementEntry,
    RetirementReport,
    build_retirement_report,
    is_retirement_contribution,
    retirement_breakdown_matches,
    validate_retirement_contributions,
)
from app.models.auth import User
from app.models.ledger import Account, Category, Transaction
from app.models.retirement import RetirementContribution
from app.services.category_service import canonical_categories


def _payload(row: RetirementContribution | RetirementContributionInput) -> dict[str, object]:
    return {
        "policy": row.policy,
        "date": row.date.isoformat(),
        "amount": format(row.amount, ".2f"),
        "source_account_id": row.source_account_id,
        "source_transaction_id": row.source_transaction_id,
        "reference": row.reference,
    }


def _valid_link(db: Session, row: RetirementContributionInput) -> Transaction:
    tx = db.get(Transaction, row.source_transaction_id)
    account = db.get(Account, row.source_account_id)
    if (
        tx is None
        or tx.status != "active"
        or tx.kind != "expense"
        or tx.activity != "household"
        or tx.account_id != row.source_account_id
        or tx.usd_amount is not None
        or tx.fx_rate_effective is not None
        or tx.date != row.date
        or account is None
        or account.currency != "ARS"
        or account.type != "credit"
    ):
        raise ValueError(
            "El desglose requiere un gasto activo del hogar en tarjeta ARS, "
            "sin FX y de igual fecha."
        )
    category = db.get(Category, tx.category_id)
    canonical = (
        db.get(Category, category.merged_into_id)
        if category and category.merged_into_id
        else category
    )
    if (
        category is None
        or canonical is None
        or not is_retirement_contribution(category.name, canonical.name, tx.description)
    ):
        raise ValueError("El movimiento no está identificado como aporte de retiro.")
    return tx


@atomic_service
def create_retirement_contributions(
    db: Session,
    contributions: Sequence[RetirementContributionInput],
    actor_id: int,
) -> tuple[RetirementContribution, ...]:
    inputs = validate_retirement_contributions(contributions, today_ar())
    acquire_advisory_lock(db, 170170, shared=False)
    actor = db.get(User, actor_id)
    if actor is None or not actor.active:
        raise ValueError("El actor debe existir y estar activo.")
    previous = {row.reference: row for row in db.scalars(select(RetirementContribution))}
    additions = []
    for row in inputs:
        existing = previous.get(row.reference)
        if existing is not None:
            if _payload(existing) != _payload(row):
                raise ValueError(
                    "La referencia ya identifica otro aporte; no se puede reemplazar su historia."
                )
            continue
        account = db.get(Account, row.source_account_id)
        if (
            account is None
            or account.currency != "ARS"
            or account.type != "credit"
            or account.is_virtual
            or not account.active
        ):
            raise ValueError("La cuenta de origen debe ser una tarjeta real y activa en ARS.")
        additions.append(row)
    linked: dict[int, list[RetirementContributionInput]] = defaultdict(list)
    for row in additions:
        if row.source_transaction_id is not None:
            linked[row.source_transaction_id].append(row)
    for identity, rows in linked.items():
        tx = _valid_link(db, rows[0])
        for row in rows[1:]:
            _valid_link(db, row)
        old = [row for row in previous.values() if row.source_transaction_id == identity]
        if not retirement_breakdown_matches(
            tx.amount, [row.amount for row in old] + [row.amount for row in rows]
        ):
            raise ValueError(
                "La suma del desglose debe coincidir exactamente con el gasto, sin duplicarlo."
            )
    for row in additions:
        created = RetirementContribution(**_payload_for_model(row), created_by=actor_id)
        db.add(created)
        db.flush()
        record_audit(
            db,
            "RetirementContribution",
            created.id,
            "INSERT",
            after=_payload(created),
            user_id=actor_id,
        )
        previous[created.reference] = created
    if additions:
        commit_service(db)
    return tuple(previous[row.reference] for row in inputs)


def _payload_for_model(row: RetirementContributionInput) -> dict[str, object]:
    return {
        "policy": row.policy,
        "date": row.date,
        "amount": row.amount,
        "source_account_id": row.source_account_id,
        "source_transaction_id": row.source_transaction_id,
        "reference": row.reference,
    }


def get_retirement_report(db: Session, period: date | None = None) -> RetirementReport:
    canonical = canonical_categories(db)
    users = {user.id: user.display_name for user in db.scalars(select(User))}
    target = aliased(Category)
    contributions = list(db.scalars(select(RetirementContribution)))
    grouped: dict[int, list[RetirementContribution]] = defaultdict(list)
    entries = []
    for row in contributions:
        if row.source_transaction_id is not None:
            grouped[row.source_transaction_id].append(row)
        else:
            account = db.get_one(Account, row.source_account_id)
            entries.append(
                RetirementEntry(
                    None,
                    row.date,
                    row.date.replace(day=1),
                    "Aporte externo documentado",
                    row.amount,
                    account.name,
                    "Seguro de retiro",
                    None,
                    None,
                    row.policy,
                    row.reference,
                )
            )
    rows = db.execute(
        select(Transaction, Account, Category, target)
        .join(Account, Account.id == Transaction.account_id)
        .join(Category, Category.id == Transaction.category_id)
        .outerjoin(target, target.id == Category.merged_into_id)
        .where(
            Transaction.status == "active",
            Transaction.kind == "expense",
            Transaction.activity == "household",
            Account.currency == "ARS",
        )
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    )
    warnings = 0
    seen_links = set()
    for tx, account, original, joined_target in rows:
        category = canonical.get(
            original.id, joined_target if joined_target is not None else original
        )
        parts = grouped.get(tx.id, [])
        if parts:
            seen_links.add(tx.id)
        if not parts and not is_retirement_contribution(
            original.name, category.name, tx.description
        ):
            continue
        user_name = (
            users.get(tx.charged_to_user_id, "Persona no identificada")
            if tx.charged_to_user_id is not None
            else None
        )
        valid = bool(parts) and all(
            part.source_account_id == tx.account_id and part.date == tx.date for part in parts
        )
        valid = valid and retirement_breakdown_matches(tx.amount, [part.amount for part in parts])
        valid = valid and tx.usd_amount is None and tx.fx_rate_effective is None
        if valid:
            for part in parts:
                entries.append(
                    RetirementEntry(
                        tx.id,
                        tx.date,
                        tx.period,
                        tx.description,
                        part.amount,
                        account.name,
                        category.name,
                        tx.charged_to_user_id,
                        user_name,
                        part.policy,
                        part.reference,
                    )
                )
        else:
            if parts:
                warnings += 1
            entries.append(
                RetirementEntry(
                    tx.id,
                    tx.date,
                    tx.period,
                    tx.description,
                    tx.amount,
                    account.name,
                    category.name,
                    tx.charged_to_user_id,
                    user_name,
                )
            )
    warnings += len(set(grouped) - seen_links)
    return replace(build_retirement_report(entries, period), allocation_warning_count=warnings)
