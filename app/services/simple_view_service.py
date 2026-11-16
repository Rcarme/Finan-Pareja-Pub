"""Lecturas informativas y preferencias de la vista simple, con escrituras auditadas."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, aliased

from app.core.audit import record_audit
from app.core.db import acquire_advisory_lock
from app.core.formatters import today_ar
from app.core.transactions import atomic_service, commit_service
from app.domain.close_engine import extract_close_snapshot_metrics
from app.domain.reports import BALANCE_ADJUSTMENT_CATEGORY, is_balance_adjustment_category
from app.domain.ui_catalog import date_window, display_percentage, validate_appearance
from app.models.auth import User
from app.models.close import Close
from app.models.envelopes import Envelope
from app.models.ledger import Account, Category, Transaction
from app.services.category_service import alias_category_ids, canonical_category
from app.services.close_service import preview_close
from app.services.envelope_service import get_envelope_balance
from app.services.forecast_service import get_forecast
from app.services.ledger_service import (
    create_transaction,
    get_account_balance,
    get_available_fund,
    void_transaction,
)


@dataclass(frozen=True)
class CategorySpend:
    category_id: int
    name: str
    icon: str
    color: str
    amount: Decimal
    percentage: Decimal


@dataclass(frozen=True)
class CloseShare:
    period: date
    amount: Decimal
    charges: Decimal
    carryover: Decimal
    total_savings: Decimal


@dataclass(frozen=True)
class SimpleSummary:
    span: str
    start_date: date
    end_date: date
    income: Decimal
    expense: Decimal
    available: Decimal
    categories: list[CategorySpend]
    close_share: CloseShare | None
    personal_balance: Decimal | None = None
    adjustment_income: Decimal = Decimal("0.00")
    adjustment_expense: Decimal = Decimal("0.00")


def _latest_share(db: Session, user_id: int, today: date) -> CloseShare | None:
    close = db.scalars(
        select(Close)
        .where(Close.status == "active", Close.period <= today.replace(day=1))
        .order_by(Close.period.desc(), Close.id.desc())
        .limit(1)
    ).first()
    if close is None:
        return None
    metrics = extract_close_snapshot_metrics(close.payload)
    if close.payload.get("user_a_id") == user_id:
        amount = metrics.distribution_user_a
    elif close.payload.get("user_b_id") == user_id:
        amount = metrics.distribution_user_b
    else:
        return None
    charges, carryover = metrics.user_deductions[user_id]
    return CloseShare(close.period, amount, charges, carryover, metrics.total_savings)


def get_simple_dashboard(
    db: Session,
    user: User,
    window: str = "month",
    *,
    as_of: date | None = None,
) -> SimpleSummary:
    from app.services.ownership_service import get_ownership_summary

    ownership = get_ownership_summary(db)
    today = as_of or today_ar()
    start, end = date_window(window, today)
    filters = [
        Transaction.status == "active",
        Transaction.activity != "personal",
    ]
    if window == "month":
        filters.append(Transaction.period == date(today.year, today.month, 1))
    else:
        filters.extend([Transaction.date >= start, Transaction.date < end])
    original_category = aliased(Category)
    rows = db.execute(
        select(
            Transaction.kind,
            Category.id,
            Category.name,
            Category.icon,
            Category.color,
            Category.is_system,
            func.sum(Transaction.amount),
        )
        .select_from(Transaction)
        .outerjoin(original_category, Transaction.category_id == original_category.id)
        .outerjoin(
            Category,
            Category.id == func.coalesce(original_category.merged_into_id, original_category.id),
        )
        .where(*filters, Transaction.kind.in_(("income", "expense")))
        .group_by(
            Transaction.kind,
            Category.id,
            Category.name,
            Category.icon,
            Category.color,
            Category.is_system,
        )
        .order_by(func.sum(Transaction.amount).desc(), Category.id)
    ).all()
    consumption = [
        row for row in rows if not is_balance_adjustment_category(row.name, row.is_system)
    ]
    income = sum((row[-1] for row in consumption if row.kind == "income"), Decimal("0.00"))
    expense = sum((row[-1] for row in consumption if row.kind == "expense"), Decimal("0.00"))
    adjustments = [row for row in rows if is_balance_adjustment_category(row.name, row.is_system)]
    categories = [
        CategorySpend(category_id, name, icon, color, amount, display_percentage(amount, expense))
        for kind, category_id, name, icon, color, _, amount in consumption
        if kind == "expense" and category_id is not None
    ]
    return SimpleSummary(
        window,
        start,
        end,
        income,
        expense,
        get_available_fund(db),
        categories,
        _latest_share(db, user.id, today),
        ownership.personal_by_user.get(user.id) if ownership else None,
        sum((row[-1] for row in adjustments if row.kind == "income"), Decimal("0.00")),
        sum((row[-1] for row in adjustments if row.kind == "expense"), Decimal("0.00")),
    )


def get_simple_transactions(
    db: Session,
    *,
    span: str = "month",
    as_of: date | None = None,
    kind: str | None = None,
    category_id: int | None = None,
    account_id: int | None = None,
    search_query: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_voided: bool = False,
    exclude_personal: bool = False,
) -> list[Transaction]:
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("Paginación inválida.")
    if kind not in (None, "income", "expense"):
        raise ValueError("Tipo de movimiento inválido.")
    today = as_of or today_ar()
    start, end = date_window(span, today)
    if span == "month":
        period_val = date(today.year, today.month, 1)
        statement = select(Transaction).where(
            Transaction.period == period_val,
            Transaction.kind.in_(("income", "expense")),
        )
    else:
        statement = select(Transaction).where(
            Transaction.date >= start,
            Transaction.date < end,
            Transaction.kind.in_(("income", "expense")),
        )
    if not include_voided:
        statement = statement.where(Transaction.status == "active")
    if exclude_personal:
        statement = statement.where(Transaction.activity != "personal")
    if kind is not None:
        statement = statement.where(Transaction.kind == kind)
    if category_id is not None:
        statement = statement.where(
            Transaction.category_id.in_(alias_category_ids(db, category_id))
        )
    if account_id is not None:
        statement = statement.where(Transaction.account_id == account_id)
    if search_query and search_query.strip():
        statement = statement.where(Transaction.description.ilike(f"%{search_query.strip()}%"))
    return list(
        db.scalars(
            statement.order_by(Transaction.date.desc(), Transaction.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )


def get_top_categories(
    db: Session,
    kind: str,
    *,
    as_of: date | None = None,
    limit: int = 7,
    user_id: int | None = None,
    show_all: bool = False,
) -> list[Category]:
    if kind not in ("income", "expense") or not 1 <= limit <= 20:
        raise ValueError("Selección de categorías inválida.")
    today = as_of or today_ar()
    original_category = aliased(Category)
    statement = (
        select(Category)
        .outerjoin(
            Transaction,
            and_(
                Transaction.category_id.in_(
                    select(original_category.id)
                    .where(
                        func.coalesce(original_category.merged_into_id, original_category.id)
                        == Category.id
                    )
                    .correlate(Category)
                ),
                Transaction.status == "active",
                Transaction.kind == kind,
                Transaction.date >= today - timedelta(days=89),
                Transaction.date <= today,
            ),
        )
        .where(
            Category.active.is_(True),
            Category.merged_into_id.is_(None),
            Category.kind == kind,
            ~(Category.is_system.is_(True) & (Category.name == BALANCE_ADJUSTMENT_CATEGORY)),
        )
        .group_by(Category.id)
        .order_by(func.count(Transaction.id).desc(), Category.name, Category.id)
        .limit(limit)
    )
    if user_id is not None and not show_all:
        statement = statement.where(
            (Category.owner_user_id.is_(None)) | (Category.owner_user_id == user_id)
        )
    return list(db.scalars(statement))


def resolve_default_account(db: Session) -> Account:
    account = db.scalars(
        select(Account)
        .where(
            Account.active.is_(True),
            Account.type != "credit",
            Account.is_virtual.is_(False),
            Account.currency == "ARS",
        )
        .order_by(Account.id)
        .limit(1)
    ).first()
    if account is None:
        raise ValueError("No hay una cuenta activa disponible para el alta rápida.")
    return account


@atomic_service
def set_ui_mode(db: Session, user_id: int, mode: str) -> User:
    if mode not in ("simple", "full"):
        raise ValueError("El modo debe ser simple o full.")
    user = db.get(User, user_id)
    if user is None or not user.active:
        raise ValueError("El usuario no está disponible.")
    before = user.ui_mode
    if before == mode:
        return user
    user.ui_mode = mode
    db.flush()
    record_audit(
        db,
        "User",
        user.id,
        "UPDATE",
        before={"ui_mode": before},
        after={"ui_mode": mode},
        user_id=user_id,
    )
    commit_service(db)
    return user


VALID_NAV_MODULES = {
    "home",
    "list",
    "spending",
    "ai",
    "new",
    "business",
    "cards",
    "close",
    "envelopes",
}


@atomic_service
def set_nav_modules(db: Session, user_id: int, modules: list[str]) -> User:
    """Configura los módulos activos de navegación para el usuario."""
    cleaned = [m.strip().lower() for m in modules if m.strip().lower() in VALID_NAV_MODULES]
    if not cleaned:
        cleaned = ["home", "new"]
    joined = ",".join(cleaned)
    user = db.get(User, user_id)
    if user is None or not user.active:
        raise ValueError("El usuario no está disponible.")
    before = getattr(user, "nav_modules", "home,list,spending,ai,new,business")
    if before == joined:
        return user
    user.nav_modules = joined
    db.flush()
    record_audit(
        db,
        "User",
        user.id,
        "UPDATE",
        before={"nav_modules": before},
        after={"nav_modules": joined},
        user_id=user_id,
    )
    commit_service(db)
    return user


@atomic_service
def update_category_appearance(
    db: Session,
    category_id: int,
    icon: str,
    color: str,
    user_id: int,
) -> Category:
    validate_appearance(icon, color)
    acquire_advisory_lock(db, 170170, shared=True)
    category = db.scalar(
        select(Category)
        .where(Category.id == category_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if category is None:
        raise ValueError("La categoría no está disponible.")
    before = {"icon": category.icon, "color": category.color}
    if before == {"icon": icon, "color": color}:
        return category
    category.icon, category.color = icon, color
    category.version += 1
    db.flush()
    record_audit(
        db,
        "Category",
        category.id,
        "UPDATE",
        before=before,
        after={"icon": icon, "color": color},
        user_id=user_id,
    )
    commit_service(db)
    return category


@atomic_service
def simple_create_transaction(
    db: Session,
    user_id: int,
    kind: str,
    amount: Decimal,
    category_id: int,
    tx_date: date,
    comment: str = "",
    charged_to_user_id: int | None = None,
    use_category_default: bool = True,
    account_id: int | None = None,
    activity: str | None = None,
    opening_omission: bool = False,
) -> Transaction:
    category = canonical_category(db, category_id)
    if (
        kind not in ("income", "expense")
        or category is None
        or not category.active
        or category.kind != kind
    ):
        raise ValueError("La categoría no corresponde al movimiento.")
    return create_transaction(
        db,
        kind,
        amount,
        account_id if account_id is not None else resolve_default_account(db).id,
        tx_date,
        period=None,
        description=comment.strip() or category.name,
        user_id=user_id,
        category_id=category.id,
        charged_to_user_id=charged_to_user_id,
        use_category_default=use_category_default,
        generated_by="simple",
        activity=activity,
        opening_omission=opening_omission,
    )


@atomic_service
def undo_quick_transaction(
    db: Session,
    transaction_id: int,
    user_id: int,
    expected_version: int | None = None,
    *,
    now: datetime | None = None,
) -> Transaction:
    transaction = db.get(Transaction, transaction_id)
    if (
        transaction is None
        or transaction.created_by != user_id
        or transaction.generated_by != "simple"
        or transaction.status != "active"
    ):
        raise ValueError("No se puede deshacer esta alta rápida.")
    timestamp = now or datetime.now(UTC)
    elapsed = timestamp - transaction.created_at
    if not timedelta(0) <= elapsed <= timedelta(seconds=60):
        raise ValueError("Solo podés deshacer un alta rápida durante 60 segundos.")
    return void_transaction(
        db,
        transaction_id,
        "Deshacer alta rápida",
        user_id,
        expected_version=expected_version if expected_version is not None else transaction.version,
    )


def get_simple_cards_view(db: Session) -> dict[str, Any]:
    """Obtiene el resumen móvil de tarjetas, saldo actual y cuotas futuras."""
    cards = db.scalars(
        select(Account)
        .where(Account.type == "credit", Account.active.is_(True))
        .order_by(Account.name)
    ).all()
    card_items: list[dict[str, Any]] = []
    total_debt = Decimal("0.00")
    for card in cards:
        bal = get_account_balance(db, card.id)
        debt = -bal if bal < Decimal("0.00") else Decimal("0.00")
        total_debt += debt
        card_items.append(
            {
                "id": card.id,
                "name": card.name,
                "currency": card.currency,
                "balance": bal,
                "debt": debt,
            }
        )
    report = get_forecast(db)
    month_names = [
        "",
        "Enero",
        "Febrero",
        "Marzo",
        "Abril",
        "Mayo",
        "Junio",
        "Julio",
        "Agosto",
        "Septiembre",
        "Octubre",
        "Noviembre",
        "Diciembre",
    ]
    upcoming_months: list[dict[str, Any]] = []
    if report.months:
        for m in report.months[:4]:
            card_entries = [
                {"description": e.label, "amount": e.amount}
                for e in m.entries
                if getattr(e, "source", "") == "card"
            ]
            if card_entries:
                total_card_month = sum(
                    (Decimal(str(e["amount"])) for e in card_entries), Decimal("0.00")
                )
                upcoming_months.append(
                    {
                        "period": m.period,
                        "display": f"{month_names[m.period.month]} {m.period.year}",
                        "expense": total_card_month,
                        "entries": card_entries,
                    }
                )
    return {
        "cards": card_items,
        "total_debt": total_debt,
        "upcoming_months": upcoming_months,
    }


def get_simple_close_view(db: Session, period: date | None = None) -> dict[str, Any]:
    """Obtiene el resumen móvil del cierre mensual, liquidación y distribución."""
    today = today_ar()
    curr_period = period or date(today.year, today.month, 1)
    existing_close = db.scalar(
        select(Close).where(Close.period == curr_period, Close.status == "active")
    )
    preview_data = None
    if existing_close is None:
        try:
            p = preview_close(db, curr_period)
            res = p.result
            preview_data = {
                "period": curr_period,
                "is_deficit": res.is_deficit,
                "available": res.available,
                "excedent": res.excedent,
                "buffer": res.buffer_allocation,
                "savings": res.savings_allocation,
                "distributable": res.distributable,
                "charges_a": res.total_charges_a,
                "charges_b": res.total_charges_b,
                "distribution_a": res.distribution_user_a,
                "distribution_b": res.distribution_user_b,
            }
        except Exception:
            preview_data = None

    recent_closes = list(
        db.scalars(
            select(Close).where(Close.status == "active").order_by(Close.period.desc()).limit(6)
        ).all()
    )
    return {
        "period": curr_period,
        "is_closed": existing_close is not None,
        "existing_close": existing_close,
        "preview": preview_data,
        "recent_closes": recent_closes,
    }


def get_simple_envelopes_view(db: Session) -> dict[str, Any]:
    """Obtiene el resumen móvil de sobres y metas de ahorro."""
    available = get_available_fund(db)
    envelopes = list(
        db.scalars(select(Envelope).where(Envelope.active.is_(True)).order_by(Envelope.id)).all()
    )
    items: list[dict[str, Any]] = []
    total_reserved = Decimal("0.00")
    for env in envelopes:
        bal = get_envelope_balance(db, env.id)
        total_reserved += bal
        pct = Decimal("0.00")
        if env.target_amount and env.target_amount > Decimal("0.00"):
            pct = min(
                Decimal("100.00"),
                ((bal / env.target_amount) * Decimal("100")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                ),
            )
        items.append(
            {
                "id": env.id,
                "name": env.name,
                "is_system": env.is_system,
                "balance": bal,
                "target_amount": env.target_amount,
                "percentage": pct,
            }
        )
    return {
        "available_fund": available,
        "envelopes": items,
        "total_reserved": total_reserved,
    }
