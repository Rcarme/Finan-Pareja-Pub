"""Consultas del negocio; los movimientos se escriben con ledger_service."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ledger import Transaction
from app.services.category_service import alias_category_ids


@dataclass(frozen=True)
class BusinessSummary:
    period: date
    income: Decimal
    expense: Decimal

    @property
    def result(self) -> Decimal:
        return self.income - self.expense


def get_business_summary(db: Session, period: date) -> BusinessSummary:
    normalized = period.replace(day=1)
    totals = dict(
        db.execute(
            select(Transaction.kind, func.sum(Transaction.amount))
            .where(
                Transaction.activity == "business",
                Transaction.status == "active",
                Transaction.period == normalized,
                Transaction.kind.in_(("income", "expense")),
            )
            .group_by(Transaction.kind)
        ).all()
    )
    return BusinessSummary(
        normalized, totals.get("income", Decimal("0.00")), totals.get("expense", Decimal("0.00"))
    )


def get_business_transactions(
    db: Session,
    period: date,
    *,
    kind: str | None = None,
    category_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Transaction]:
    """Detalle por mes contable; comparte actividad y exclusiones con el resumen."""
    if kind not in (None, "income", "expense") or not 1 <= limit <= 200 or offset < 0:
        raise ValueError("Filtro de actividad comercial inválido.")
    statement = select(Transaction).where(
        Transaction.activity == "business",
        Transaction.status == "active",
        Transaction.period == period.replace(day=1),
        Transaction.kind.in_(("income", "expense")),
    )
    if kind is not None:
        statement = statement.where(Transaction.kind == kind)
    if category_id is not None:
        statement = statement.where(
            Transaction.category_id.in_(alias_category_ids(db, category_id))
        )
    return list(
        db.scalars(
            statement.order_by(Transaction.date.desc(), Transaction.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
