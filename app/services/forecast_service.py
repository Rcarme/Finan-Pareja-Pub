"""Proyección de doce meses mediante consultas; no genera movimientos ni vencimientos."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.formatters import today_ar
from app.domain.forecast import ForecastEntry, ForecastMonth, build_forecast, shift_month
from app.domain.obligations import generate_annual_schedule_items
from app.domain.reports import is_balance_adjustment_category
from app.models.cards import CardStatement
from app.models.close import FxRate
from app.models.ledger import Category, Transaction
from app.models.obligations import Obligation, ObligationItem
from app.models.recurring import RecurringPending, RecurringTemplate
from app.models.rent import RentContract
from app.services.obligation_service import get_latest_fx_rate


@dataclass(frozen=True)
class ForecastReport:
    months: list[ForecastMonth]
    fx_rate: FxRate | None
    limitations: tuple[str, ...]


def _obligation_entries(db: Session, start: date, end: date) -> list[ForecastEntry]:
    obligations = db.scalars(select(Obligation).where(Obligation.active.is_(True))).all()
    items = db.scalars(
        select(ObligationItem).where(
            ObligationItem.due_period >= start, ObligationItem.due_period < end
        )
    ).all()
    existing = {(item.obligation_id, item.due_period, item.kind): item for item in items}
    result: list[ForecastEntry] = []
    for obligation in obligations:
        for year in range(start.year, end.year + 1):
            for definition in generate_annual_schedule_items(year, obligation.monthly_usd):
                if not start <= definition.due_period < end:
                    continue
                stored = existing.get((obligation.id, definition.due_period, definition.kind))
                if stored is not None and stored.status != "pendiente":
                    continue
                result.append(
                    ForecastEntry(
                        definition.due_period,
                        "expense",
                        stored.usd_amount if stored is not None else definition.usd_amount,
                        obligation.name + " — " + definition.kind,
                        "obligation",
                        True,
                        "USD",
                    )
                )
    return result


def _card_entries(db: Session, start: date) -> list[ForecastEntry]:
    statements = db.scalars(select(CardStatement).where(CardStatement.status == "confirmed")).all()
    transaction_ids = {
        int(row["transaction_id"])
        for statement in statements
        for row in statement.rows
        if row.get("transaction_id") is not None
    }
    transactions = {
        transaction.id: transaction
        for transaction in db.scalars(
            select(Transaction).where(Transaction.id.in_(transaction_ids))
        )
    }
    latest: dict[int, CardStatement] = {}

    def chronology(statement: CardStatement) -> tuple[date, int]:
        periods = [
            transactions[int(row["transaction_id"])].period
            if row.get("transaction_id") is not None and int(row["transaction_id"]) in transactions
            else date.fromisoformat(str(row["date"])).replace(day=1)
            for row in statement.rows
        ]
        return max(periods, default=date.min), statement.id or 0

    for statement in statements:
        previous = latest.get(statement.account_id)
        if previous is None or chronology(statement) > chronology(previous):
            latest[statement.account_id] = statement
    result: list[ForecastEntry] = []
    for statement in latest.values():
        for row in statement.rows:
            if row.get("review_status") == "discarded":
                continue
            remaining = int(row["remaining_installments"])
            transaction_id = row.get("transaction_id")
            posted = transactions.get(int(transaction_id)) if transaction_id is not None else None
            if posted is not None and posted.status != "active":
                continue
            base = (
                posted.period
                if posted is not None
                else max(start, date.fromisoformat(str(row["date"])).replace(day=1))
            )
            currency = str(row["currency"])
            amount = (
                Decimal(str(row["future_estimated_amount"]))
                if "future_estimated_amount" in row
                else (
                    Decimal(str(row.get("posted_amount", row["amount"])))
                    if currency == "ARS"
                    else Decimal(str(row["amount"]))
                )
            )
            first_offset = 1 if posted is not None else 0
            for offset in range(first_offset, remaining + 1):
                result.append(
                    ForecastEntry(
                        shift_month(base, offset),
                        str(row["kind"]),
                        amount,
                        str(row["merchant"]) + " — cuota de tarjeta",
                        "card",
                        True,
                        currency,
                    )
                )
    return result


def get_forecast(db: Session, *, as_of: date | None = None) -> ForecastReport:
    """Usa datos vigentes y separa registros de estimaciones; nunca consulta una API."""
    today = as_of or today_ar()
    start = today.replace(day=1)
    end = shift_month(start, 12)
    transactions = db.scalars(
        select(Transaction).where(
            Transaction.period >= start,
            Transaction.period < end,
            Transaction.status == "active",
            Transaction.activity != "personal",
            Transaction.kind.in_(("income", "expense")),
        )
    ).all()
    all_categories = {category.id: category for category in db.scalars(select(Category))}
    categories = {cid: cat.name for cid, cat in all_categories.items()}
    entries = [
        ForecastEntry(tx.period, tx.kind, tx.amount, tx.description, "ledger", False)
        for tx in transactions
        if not (
            tx.category_id in all_categories
            and is_balance_adjustment_category(
                all_categories[tx.category_id].name,
                getattr(all_categories[tx.category_id], "is_system", False),
            )
        )
    ]
    contracts = db.scalars(select(RentContract).where(RentContract.active.is_(True))).all()
    rent_registered = {
        tx.period
        for tx in transactions
        if tx.kind == "income"
        and (
            tx.generated_by == "rent_collection"
            or categories.get(tx.category_id or 0) == "Alquiler cobrado"
        )
    }
    pending = {
        (item.template_id, item.period): item.status
        for item in db.scalars(
            select(RecurringPending).where(
                RecurringPending.period >= start, RecurringPending.period < end
            )
        )
    }
    templates = db.scalars(
        select(RecurringTemplate).where(RecurringTemplate.active.is_(True))
    ).all()
    for offset in range(12):
        period = shift_month(start, offset)
        for template in templates:
            tpl_cat = all_categories.get(template.category_id)
            if tpl_cat is not None and (
                getattr(tpl_cat, "activity", "household") == "personal"
                or is_balance_adjustment_category(
                    tpl_cat.name, getattr(tpl_cat, "is_system", False)
                )
            ):
                continue
            if pending.get((template.id, period)) in ("confirmed", "dismissed"):
                continue
            if (
                contracts
                and template.kind == "income"
                and categories.get(template.category_id) == "Alquiler cobrado"
            ):
                continue
            entries.append(
                ForecastEntry(
                    period,
                    template.kind,
                    template.estimated_amount,
                    template.name,
                    "recurring",
                    True,
                )
            )
        if period not in rent_registered:
            for contract in contracts:
                entries.append(
                    ForecastEntry(
                        period, "income", contract.current_amount, "Alquiler cobrado", "rent", True
                    )
                )
    entries.extend(_obligation_entries(db, start, end))
    entries.extend(_card_entries(db, start))
    fx_rate = get_latest_fx_rate(db, as_of_date=today)
    months = build_forecast(start, entries, fx_rate.rate if fx_rate is not None else None)
    return ForecastReport(
        months=months,
        fx_rate=fx_rate,
        limitations=(
            "Es una referencia: no registra gastos ni modifica saldos.",
            "Recurrentes y alquiler mantienen el importe vigente; no se proyecta inflación ni IPC.",
            "Las cuotas de tarjeta se estiman desde el último resumen confirmado de cada cuenta.",
            "Los vencimientos USD futuros siguen el cronograma configurado, con el dólar vigente.",
        ),
    )
