"""Servicio de liquidación y reparto mensual equitativo de compromisos (Q38, RN-51).

Agrupa ingresos netos reales, cuota del departamento en USD, resúmenes de tarjetas de crédito
y consumos particulares para generar el plan de liquidación y pagos por integrante.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.settlement import (
    MonthlyObligation,
    SettlementResult,
    UserIncome,
    calculate_monthly_settlement,
)
from app.models.ledger import Account, Transaction
from app.models.obligations import ObligationItem
from app.services.ledger_service import get_account_balance
from app.services.obligation_service import get_latest_fx_rate
from app.services.report_service import get_contributions_report, get_personal_charges_report


def get_monthly_settlement_report(db: Session, period: date) -> SettlementResult:
    """Genera la liquidación de compromisos del mes según ingresos proporcionales reales."""
    norm_p = date(period.year, period.month, 1)

    # 1. Ingresos computables del mes por integrante y fondo común
    contrib_report = get_contributions_report(db, norm_p)
    incomes = [
        UserIncome(
            user_id=it.user_id,
            username=it.username,
            display_name=it.display_name,
            income=it.total_income_generated,
        )
        for it in contrib_report.items
    ]
    common_income = contrib_report.common_income

    # 2. Obligaciones del mes
    obligations: list[MonthlyObligation] = []
    direct_payments: dict[int, Decimal] = {
        it.user_id: Decimal("0.00") for it in contrib_report.items
    }

    # 2a. Cuota departamento
    cuota_item = db.execute(
        select(ObligationItem).where(
            ObligationItem.due_period == norm_p, ObligationItem.kind == "cuota"
        )
    ).scalar_one_or_none()

    if cuota_item is not None and cuota_item.status == "pagado" and cuota_item.transaction_id:
        tx = db.get(Transaction, cuota_item.transaction_id)
        if tx is not None and tx.status == "active":
            dept_amount = tx.amount
            obligations.append(
                MonthlyObligation(
                    name=f"Cuota departamento (USD {cuota_item.usd_amount:,.2f})",
                    amount=dept_amount,
                    is_shared=True,
                    category="housing",
                )
            )
            # Imputar pago directo a quien originó o transfirió para la cuota
            payer_id = tx.created_by or 1
            direct_payments[payer_id] = direct_payments.get(payer_id, Decimal("0.00")) + dept_amount
    else:
        usd_amount = cuota_item.usd_amount if cuota_item is not None else Decimal("1800.00")
        fx_record = get_latest_fx_rate(db, as_of_date=norm_p)
        rate = fx_record.rate if fx_record is not None else Decimal("1517.49")
        dept_amount = (usd_amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        obligations.append(
            MonthlyObligation(
                name=f"Cuota departamento estimada (USD {usd_amount:,.2f})",
                amount=dept_amount,
                is_shared=True,
                category="housing",
            )
        )

    # 2b. Tarjetas de crédito (consumos comunes del hogar)
    card_accounts = list(
        db.scalars(
            select(Account).where(
                Account.type == "credit",
                Account.active == True,  # noqa: E712
            )
        ).all()
    )
    if card_accounts:
        card_ids = [acc.id for acc in card_accounts]
        card_common_exp: Decimal = db.execute(
            select(func.coalesce(func.sum(Transaction.amount), Decimal("0.00"))).where(
                Transaction.account_id.in_(card_ids),
                Transaction.period == norm_p,
                Transaction.kind == "expense",
                Transaction.status == "active",
                Transaction.activity == "household",
                Transaction.charged_to_user_id.is_(None),
            )
        ).scalar() or Decimal("0.00")

        card_common_inc: Decimal = db.execute(
            select(func.coalesce(func.sum(Transaction.amount), Decimal("0.00"))).where(
                Transaction.account_id.in_(card_ids),
                Transaction.period == norm_p,
                Transaction.kind == "income",
                Transaction.status == "active",
                Transaction.activity == "household",
                Transaction.charged_to_user_id.is_(None),
            )
        ).scalar() or Decimal("0.00")

        net_card_common = card_common_exp - card_common_inc
        if net_card_common > Decimal("0.00"):
            obligations.append(
                MonthlyObligation(
                    name="Tarjetas de crédito (consumos comunes)",
                    amount=net_card_common,
                    is_shared=True,
                    category="card",
                )
            )

    # 2c. Gastos comunes pagados en efectivo/billetera en fecha previa e imputados al período
    prior_common_txs = list(
        db.scalars(
            select(Transaction)
            .join(Account, Transaction.account_id == Account.id)
            .where(
                Transaction.period == norm_p,
                Transaction.date < norm_p,
                Transaction.kind == "expense",
                Transaction.status == "active",
                Transaction.activity == "household",
                Transaction.charged_to_user_id.is_(None),
                Account.type != "credit",
            )
        ).all()
    )
    for ptx in prior_common_txs:
        acc_obj = db.get(Account, ptx.account_id)
        payer_uid = (
            acc_obj.owner_user_id
            if (acc_obj is not None and acc_obj.owner_user_id is not None)
            else (ptx.created_by or 1)
        )
        obligations.append(
            MonthlyObligation(
                name=ptx.description or "Gasto común adelantado",
                amount=ptx.amount,
                is_shared=True,
                category="advance",
            )
        )
        direct_payments[payer_uid] = direct_payments.get(payer_uid, Decimal("0.00")) + ptx.amount

    # 3. Cargos personales individuales del mes
    personal_rep = get_personal_charges_report(db, norm_p)
    personal_charges_by_user = {u.user_id: u.current_period_charged for u in personal_rep.by_user}

    # 4. Saldos reales actuales en cuentas líquidas (ARS y USD) por integrante
    fx_rec = get_latest_fx_rate(db, as_of_date=norm_p)
    eff_fx = fx_rec.rate if fx_rec is not None else Decimal("1517.49")
    real_ars_by_user: dict[int, Decimal] = {
        it.user_id: Decimal("0.00") for it in contrib_report.items
    }
    real_usd_by_user: dict[int, Decimal] = {
        it.user_id: Decimal("0.00") for it in contrib_report.items
    }
    liquid_accounts = list(
        db.scalars(
            select(Account).where(
                Account.active == True,  # noqa: E712
                Account.type != "credit",
                Account.owner_user_id.is_not(None),
            )
        ).all()
    )
    for acc in liquid_accounts:
        assert acc.owner_user_id is not None
        bal = get_account_balance(db, acc.id)
        if acc.currency == "USD":
            real_usd_by_user[acc.owner_user_id] = (
                real_usd_by_user.get(acc.owner_user_id, Decimal("0.00")) + bal
            )
        else:
            real_ars_by_user[acc.owner_user_id] = (
                real_ars_by_user.get(acc.owner_user_id, Decimal("0.00")) + bal
            )

    return calculate_monthly_settlement(
        period=norm_p,
        incomes=incomes,
        obligations=obligations,
        personal_charges_by_user=personal_charges_by_user,
        common_income=common_income,
        direct_payments_by_user=direct_payments,
        real_balances_ars_by_user=real_ars_by_user,
        real_balances_usd_by_user=real_usd_by_user,
        fx_rate=eff_fx,
    )
