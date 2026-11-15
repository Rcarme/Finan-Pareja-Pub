"""Lectura mensual del consumo real; no infiere atribución desde cuentas o categorías."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.reports import is_balance_adjustment_category
from app.domain.spending import (
    SpendingPerson,
    SpendingReport,
    SpendingSource,
    build_spending_report,
    is_spending_refund,
)
from app.models.auth import User
from app.models.ledger import Account, Category, Transaction
from app.services.category_service import canonical_categories


def get_spending_people(db: Session) -> tuple[SpendingPerson, ...]:
    """Retorna los integrantes activos para agrupaciones del reporte de consumo."""
    users = list(db.scalars(select(User).where(User.active.is_(True)).order_by(User.id)).all())
    return tuple(SpendingPerson(u.id, u.username, u.display_name) for u in users)


def get_spending_report(db: Session, period: date) -> SpendingReport:
    people = get_spending_people(db)
    if len(people) != 2:
        raise ValueError(
            "Deben existir exactamente dos integrantes activos para comparar el consumo."
        )
    canonical = canonical_categories(db)
    sources = []
    rows = db.execute(
        select(Transaction, Account, Category)
        .join(Account, Account.id == Transaction.account_id)
        .join(Category, Category.id == Transaction.category_id)
        .where(
            Transaction.period == period.replace(day=1),
            Transaction.status == "active",
            Transaction.activity.in_(("household", "personal")),
            Transaction.kind.in_(("expense", "income")),
            Account.currency == "ARS",
        )
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    )
    for tx, account, original in rows:
        category = canonical.get(
            original.id,
            canonical.get(original.merged_into_id, original)
            if original.merged_into_id is not None
            else original,
        )
        if is_balance_adjustment_category(
            original.name, original.is_system
        ) or is_balance_adjustment_category(category.name, category.is_system):
            continue
        if tx.kind == "income" and not is_spending_refund(
            tx.kind, original.name, category.name, tx.generated_by, account.type
        ):
            continue
        sources.append(
            SpendingSource(
                tx.id,
                tx.date,
                tx.description,
                tx.kind,
                tx.amount,
                tx.charged_to_user_id,
                category.id,
                category.name,
            )
        )
    return build_spending_report(period, (people[0], people[1]), sources)
