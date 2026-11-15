"""Servicio de generación de reportes y agregaciones contables (RN-43, SPEC.md §6).

Realiza consultas parametrizadas a la base de datos y utiliza la lógica pura de
dominio (app.domain.reports) para generar:
- Resumen mensual y comparativa con el mes anterior.
- Gastos agrupados por categoría con desglose porcentual.
- Evolución histórica de ingresos, gastos y fondos (12 meses).
- Progreso de sobres y metas de ahorro con proyecciones.
- Métricas consolidadas del departamento y cotizaciones efectivas ponderadas.
- Aportes informativos por persona.
- Reporte de Cargos personales (RN-43).
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.core.formatters import today_ar
from app.domain.close_engine import extract_close_snapshot_metrics
from app.domain.reports import (
    BALANCE_ADJUSTMENT_CATEGORY,
    BalanceAdjustmentItem,
    BalanceAdjustmentsReport,
    CategoryExpensesReport,
    ContributionsReport,
    DepartmentMetricsReport,
    DepartmentPaymentItem,
    DepartmentUpcomingItem,
    MonthlyComparisonReport,
    MonthlyEvolutionItem,
    MonthlyEvolutionReport,
    MonthlySummaryData,
    PersonalChargeDetailItem,
    PersonalChargesReport,
    SavingsGoalsReport,
    calculate_balance_adjustments_summary,
    calculate_category_expenses_summary,
    calculate_contributions_summary,
    calculate_department_metrics,
    calculate_monthly_comparison,
    calculate_personal_charges_summary,
    calculate_savings_goals_projections,
    is_balance_adjustment_category,
)
from app.models.auth import User
from app.models.close import Close
from app.models.envelopes import Envelope
from app.models.ledger import Category, Transaction
from app.models.obligations import ObligationItem
from app.services.envelope_service import get_envelope_balance
from app.services.ledger_service import get_available_fund, get_total_fund
from app.services.ownership_service import personal_charge_conditions


def _normalize_period(p: date) -> date:
    return date(p.year, p.month, 1)


def _previous_period_date(p: date) -> date:
    if p.month == 1:
        return date(p.year - 1, 12, 1)
    return date(p.year, p.month - 1, 1)


def _get_period_summary_data(db: Session, period: date) -> MonthlySummaryData:
    norm_p = _normalize_period(period)

    # 1. Ingresos
    income_val: Decimal = db.execute(
        select(func.coalesce(func.sum(Transaction.amount), Decimal("0.00"))).where(
            Transaction.period == norm_p,
            Transaction.kind == "income",
            Transaction.status == "active",
        )
    ).scalar() or Decimal("0.00")

    # 2. Gastos
    expense_val: Decimal = db.execute(
        select(func.coalesce(func.sum(Transaction.amount), Decimal("0.00"))).where(
            Transaction.period == norm_p,
            Transaction.kind == "expense",
            Transaction.status == "active",
        )
    ).scalar() or Decimal("0.00")

    # 3. Datos de cierre si existe (C-02: esquema único compartido con close_engine)
    close_obj = (
        db.execute(select(Close).where(Close.period == norm_p, Close.status == "active"))
        .scalars()
        .first()
    )

    savings_val = Decimal("0.00")
    distributed_val = Decimal("0.00")
    if close_obj is not None:
        metrics = extract_close_snapshot_metrics(close_obj.payload)
        savings_val = metrics.total_savings
        distributed_val = metrics.total_distributed

    return MonthlySummaryData(
        period=norm_p,
        total_income=income_val,
        total_expense=expense_val,
        total_savings=savings_val,
        total_distributed=distributed_val,
    )


def get_monthly_summary_report(db: Session, period: date) -> MonthlyComparisonReport:
    """Genera el resumen mensual del período y su comparativa con el mes previo."""
    current_data = _get_period_summary_data(db, period)
    prev_period = _previous_period_date(period)

    # Verificar si hubo actividad en el mes previo
    has_prev = (
        db.execute(
            select(Transaction.id)
            .where(Transaction.period == prev_period, Transaction.status == "active")
            .limit(1)
        ).first()
        is not None
    )

    previous_data = _get_period_summary_data(db, prev_period) if has_prev else None
    return calculate_monthly_comparison(current=current_data, previous=previous_data)


def get_category_expenses_report(db: Session, period: date) -> CategoryExpensesReport:
    """Calcula gastos agregados por categoría para el período (M-04)."""
    norm_p = _normalize_period(period)
    original_category = aliased(Category)

    stmt = (
        select(
            func.coalesce(Category.name, "Sin categoría"),
            func.coalesce(Category.id, 0),
            func.coalesce(func.sum(Transaction.amount), Decimal("0.00")).label("total"),
            func.count(Transaction.id).label("count"),
            Category.is_system,
        )
        .select_from(Transaction)
        .outerjoin(original_category, Transaction.category_id == original_category.id)
        .outerjoin(
            Category,
            Category.id == func.coalesce(original_category.merged_into_id, original_category.id),
        )
        .where(
            Transaction.period == norm_p,
            Transaction.kind == "expense",
            Transaction.status == "active",
            Transaction.activity != "personal",
        )
        .group_by(Category.name, Category.id, Category.is_system)
    )

    rows = db.execute(stmt).all()
    raw_items: list[tuple[str, int, Decimal, int]] = [
        (str(row[0]), int(row[1]), Decimal(str(row[2])), int(row[3]))
        for row in rows
        if not is_balance_adjustment_category(row[0], row[4])
    ]

    return calculate_category_expenses_summary(raw_items)


def get_balance_adjustments_report(db: Session, period: date) -> BalanceAdjustmentsReport:
    """RN-19: detalle informativo de ajustes oficiales sin modificar el libro."""
    rows = db.scalars(
        select(Transaction)
        .join(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.period == _normalize_period(period),
            Transaction.status == "active",
            Transaction.kind.in_(("income", "expense")),
            Category.is_system.is_(True),
            Category.name == BALANCE_ADJUSTMENT_CATEGORY,
        )
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    )
    return calculate_balance_adjustments_summary(
        [BalanceAdjustmentItem(tx.id, tx.date, tx.description, tx.amount, tx.kind) for tx in rows]
    )


def get_monthly_evolution_report(
    db: Session, months: int = 12, as_of_period: date | None = None
) -> MonthlyEvolutionReport:
    """Genera la serie temporal de los últimos N meses (ingresos, gastos, neto y fondos, H-01)."""
    if as_of_period is not None:
        anchor = date(as_of_period.year, as_of_period.month, 1)
    else:
        max_tx_period = db.execute(select(func.max(Transaction.period))).scalar()
        today = today_ar()
        today_period = date(today.year, today.month, 1)
        anchor = max(max_tx_period, today_period) if max_tx_period is not None else today_period

    periods_list: list[date] = []
    cursor = anchor

    for _ in range(months):
        periods_list.append(cursor)
        cursor = _previous_period_date(cursor)

    # Ordenar cronológicamente (más antiguo al más reciente)
    periods_list.reverse()

    items: list[MonthlyEvolutionItem] = []
    for p in periods_list:
        inc: Decimal = db.execute(
            select(func.coalesce(func.sum(Transaction.amount), Decimal("0.00"))).where(
                Transaction.period == p,
                Transaction.kind == "income",
                Transaction.status == "active",
            )
        ).scalar() or Decimal("0.00")

        exp: Decimal = db.execute(
            select(func.coalesce(func.sum(Transaction.amount), Decimal("0.00"))).where(
                Transaction.period == p,
                Transaction.kind == "expense",
                Transaction.status == "active",
            )
        ).scalar() or Decimal("0.00")

        net = inc - exp
        fund_balance = get_total_fund(db, period_cutoff=p)
        available_balance = get_available_fund(db, period_cutoff=p)

        items.append(
            MonthlyEvolutionItem(
                period=p,
                income=inc,
                expense=exp,
                net=net,
                fund_balance=fund_balance,
                available_balance=available_balance,
            )
        )

    return MonthlyEvolutionReport(items=items)


def get_savings_goals_report(db: Session, as_of_date: date | None = None) -> SavingsGoalsReport:
    """Genera el reporte de sobres y metas de ahorro con proyecciones (C-03, H-02)."""
    effective_as_of = as_of_date or today_ar()
    cutoff_period = date(effective_as_of.year, effective_as_of.month, 1)

    envelopes = list(
        db.execute(
            select(Envelope).where(Envelope.active == True).order_by(Envelope.name)  # noqa: E712
        )
        .scalars()
        .all()
    )

    goals_data: list[dict[str, Any]] = []
    for env in envelopes:
        balance = get_envelope_balance(db, env.id, period_cutoff=cutoff_period)

        goals_data.append(
            {
                "envelope_id": env.id,
                "envelope_name": env.name,
                "category_name": "Ahorro común"
                if env.type == "savings"
                else ("Colchón" if env.type == "buffer" else "Meta"),
                "target_amount": env.target_amount,
                "current_balance": balance,
                "target_date": env.target_date,
            }
        )

    items = calculate_savings_goals_projections(goals_data, as_of_date=effective_as_of)
    return SavingsGoalsReport(items=items)


def get_department_report(db: Session, year: int | None = None) -> DepartmentMetricsReport:
    """Genera el reporte acumulado del departamento y sus vencimientos (M-05)."""
    paid_stmt = (
        select(ObligationItem)
        .where(ObligationItem.status == "pagado")
        .order_by(ObligationItem.due_period.asc())
    )
    if year is not None:
        paid_stmt = paid_stmt.where(
            ObligationItem.due_period >= date(year, 1, 1),
            ObligationItem.due_period <= date(year, 12, 31),
        )
    paid_items = list(db.execute(paid_stmt).scalars().all())

    payments: list[DepartmentPaymentItem] = []
    for item in paid_items:
        tx = db.get(Transaction, item.transaction_id) if item.transaction_id else None
        ars_amt = tx.amount if tx else Decimal("0.00")
        pay_date = tx.date if tx else item.due_period
        fx_rate = (
            tx.fx_rate_effective
            if (tx and tx.fx_rate_effective)
            else (
                (ars_amt / item.usd_amount).quantize(Decimal("0.0001"))
                if item.usd_amount > Decimal("0.00")
                else Decimal("0.0000")
            )
        )

        payments.append(
            DepartmentPaymentItem(
                item_id=item.id,
                due_date=item.due_period,
                payment_date=pay_date,
                usd_amount=item.usd_amount,
                ars_amount=ars_amt,
                effective_fx_rate=fx_rate,
            )
        )

    # 2. Próximos vencimientos impagos (sin truncar con limit(10), filtrando por año si aplica)
    upcoming_stmt = (
        select(ObligationItem)
        .where(ObligationItem.status == "pendiente")
        .order_by(ObligationItem.due_period.asc())
    )
    if year is not None:
        upcoming_stmt = upcoming_stmt.where(
            ObligationItem.due_period >= date(year, 1, 1),
            ObligationItem.due_period <= date(year, 12, 31),
        )
    upcoming_items_db = list(db.execute(upcoming_stmt).scalars().all())

    upcoming: list[DepartmentUpcomingItem] = [
        DepartmentUpcomingItem(
            item_id=u.id,
            due_date=u.due_period,
            usd_amount=u.usd_amount,
            status="Pendiente",
        )
        for u in upcoming_items_db
    ]

    return calculate_department_metrics(payments=payments, upcoming_items=upcoming)


def get_contributions_report(db: Session, period: date) -> ContributionsReport:
    """Genera el reporte informativo de aportes generados por cada persona (RN-51, D10)."""
    from app.services.business_service import get_business_summary

    norm_p = _normalize_period(period)

    users = list(
        db.execute(select(User).where(User.active == True).order_by(User.id)).scalars().all()  # noqa: E712
    )

    common_categories = ("Alquiler cobrado", "Reintegros de tarjeta")

    # Ingresos comunes del hogar (no atribuibles individualmente a una persona)
    common_income: Decimal = db.execute(
        select(func.coalesce(func.sum(Transaction.amount), Decimal("0.00")))
        .select_from(Transaction)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.period == norm_p,
            Transaction.kind == "income",
            Transaction.status == "active",
            Transaction.activity == "household",
            Transaction.charged_to_user_id.is_(None),
            Category.owner_user_id.is_(None),
            Category.name.in_(common_categories),
            ~(Category.is_system.is_(True) & (Category.name == BALANCE_ADJUSTMENT_CATEGORY)),
        )
    ).scalar() or Decimal("0.00")

    has_business_tx = (
        db.execute(
            select(func.count())
            .select_from(Transaction)
            .where(
                Transaction.period == norm_p,
                Transaction.status == "active",
                Transaction.activity == "business",
            )
        ).scalar()
        or 0
    ) > 0

    user_incomes: list[tuple[int, str, str, Decimal]] = []
    for u in users:
        amt = Decimal("0.00")
        if u.has_nav("business"):
            if has_business_tx:
                # El resultado neto del negocio (cobros - gastos) aporta al hogar
                b_summary = get_business_summary(db, norm_p)
                net_business = max(b_summary.result, Decimal("0.00"))
                amt += net_business

                # Otros ingresos del hogar de la actividad general
                other_incomes_stmt = (
                    select(func.coalesce(func.sum(Transaction.amount), Decimal("0.00")))
                    .select_from(Transaction)
                    .outerjoin(Category, Transaction.category_id == Category.id)
                    .where(
                        Transaction.period == norm_p,
                        Transaction.kind == "income",
                        Transaction.status == "active",
                        Transaction.activity != "business",
                        Transaction.charged_to_user_id.is_(None),
                        or_(
                            Category.owner_user_id == u.id,
                            and_(
                                Category.owner_user_id.is_(None),
                                Transaction.created_by == u.id,
                                ~Category.name.in_(common_categories),
                            ),
                        ),
                        Category.activity != "business",
                        ~Category.name.ilike("Retiro %"),
                        ~(
                            Category.is_system.is_(True)
                            & (Category.name == BALANCE_ADJUSTMENT_CATEGORY)
                        ),
                    )
                )
                amt += db.execute(other_incomes_stmt).scalar() or Decimal("0.00")
            else:
                user_attribution = or_(
                    and_(Transaction.activity != "business", Category.owner_user_id == u.id),
                    and_(
                        Transaction.activity != "business",
                        Category.owner_user_id.is_(None),
                        Transaction.created_by == u.id,
                        ~Category.name.in_(common_categories),
                    ),
                )
                amt_res = db.execute(
                    select(func.coalesce(func.sum(Transaction.amount), Decimal("0.00")))
                    .select_from(Transaction)
                    .outerjoin(Category, Transaction.category_id == Category.id)
                    .where(
                        Transaction.period == norm_p,
                        Transaction.kind == "income",
                        Transaction.status == "active",
                        Transaction.activity != "personal",
                        Transaction.charged_to_user_id.is_(None),
                        user_attribution,
                        ~(
                            Category.is_system.is_(True)
                            & (Category.name == BALANCE_ADJUSTMENT_CATEGORY)
                        ),
                    )
                ).scalar() or Decimal("0.00")
                amt += amt_res
        else:
            user_attribution = or_(
                and_(Transaction.activity != "business", Category.owner_user_id == u.id),
                and_(
                    Transaction.activity != "business",
                    Category.owner_user_id.is_(None),
                    Transaction.created_by == u.id,
                    ~Category.name.in_(common_categories),
                ),
            )
            amt_res = db.execute(
                select(func.coalesce(func.sum(Transaction.amount), Decimal("0.00")))
                .select_from(Transaction)
                .outerjoin(Category, Transaction.category_id == Category.id)
                .where(
                    Transaction.period == norm_p,
                    Transaction.kind == "income",
                    Transaction.status == "active",
                    Transaction.activity != "personal",
                    Transaction.charged_to_user_id.is_(None),
                    user_attribution,
                    ~(
                        Category.is_system.is_(True)
                        & (Category.name == BALANCE_ADJUSTMENT_CATEGORY)
                    ),
                )
            ).scalar() or Decimal("0.00")
            amt += amt_res

        user_incomes.append((u.id, u.username, u.display_name or u.username, amt))

    return calculate_contributions_summary(user_incomes, common_income)


def get_personal_charges_report(db: Session, period: date) -> PersonalChargesReport:
    """RN-42, RN-43: Genera el reporte de cargos personales y reembolsos por usuario."""
    norm_p = _normalize_period(period)

    # Movimientos con cargo personal en el período
    stmt = (
        select(Transaction)
        .where(
            *personal_charge_conditions(db, norm_p),
        )
        .order_by(Transaction.date.asc(), Transaction.id.asc())
    )
    transactions = list(db.execute(stmt).scalars().all())

    users_map = {
        u.id: (u.username, u.display_name or u.username)
        for u in db.execute(select(User)).scalars().all()
    }

    charges: list[PersonalChargeDetailItem] = []
    for tx in transactions:
        uid = tx.charged_to_user_id
        assert uid is not None
        uname, dname = users_map.get(uid, ("usuario", "Usuario"))
        # RN-42: un ingreso con cargo a una persona actúa como reembolso (resta sus cargos)
        signed_amount = -tx.amount if tx.kind == "income" else tx.amount
        charges.append(
            PersonalChargeDetailItem(
                transaction_id=tx.id,
                user_id=uid,
                username=uname,
                display_name=dname,
                date=tx.date,
                description=tx.description or "Cargo personal",
                amount=signed_amount,
                period=tx.period,
            )
        )

    # Buscar deducciones registradas en el cierre por ID de usuario (C-02)
    close_obj = (
        db.execute(select(Close).where(Close.period == norm_p, Close.status == "active"))
        .scalars()
        .first()
    )

    close_deductions: dict[int, tuple[Decimal, Decimal]] = {}
    if close_obj is not None:
        metrics = extract_close_snapshot_metrics(close_obj.payload)
        close_deductions = dict(metrics.user_deductions)

    return calculate_personal_charges_summary(
        charges=charges,
        close_deductions=close_deductions,
        period=norm_p,
    )
