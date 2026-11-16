"""Lógica de dominio pura para liquidación de compromisos y reparto proporcional a ingresos (Q38).

Calcula proporciones mensuales según ingresos reales netos (relación de dependencia
vs. resultado de actividad comercial), distribuye obligaciones compartidas y
computa instrucciones de pago exactas para la pareja.
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

CENTAVO = Decimal("0.01")
RATIO_PRECISION = Decimal("0.0001")


@dataclass(frozen=True)
class UserIncome:
    """Ingreso individual computable para el reparto del mes."""

    user_id: int
    username: str
    display_name: str
    income: Decimal


@dataclass(frozen=True)
class IncomeRatio:
    """Proporción porcentual asignada según ingresos reales."""

    user_id: int
    username: str
    display_name: str
    income: Decimal
    ratio: Decimal
    percentage: Decimal


@dataclass(frozen=True)
class MonthlyObligation:
    """Compromiso u obligación del mes a liquidar."""

    name: str
    amount: Decimal
    is_shared: bool = True
    charged_to_user_id: int | None = None
    category: str | None = None


@dataclass(frozen=True)
class SettlementParticipant:
    """Desglose de responsabilidad mensual y reparto de excedente por integrante."""

    user_id: int
    username: str
    display_name: str
    income: Decimal
    ratio: Decimal
    percentage: Decimal
    personal_expenses: Decimal
    shared_expenses_assigned: Decimal
    total_liability: Decimal
    direct_payments: Decimal
    net_remaining_to_pay: Decimal
    housing_share: Decimal = Decimal("0.00")
    card_common_share: Decimal = Decimal("0.00")
    common_income_share: Decimal = Decimal("0.00")
    surplus_remaining: Decimal = Decimal("0.00")
    savings_contribution: Decimal = Decimal("0.00")
    buffer_contribution: Decimal = Decimal("0.00")
    proportional_extra: Decimal = Decimal("0.00")
    equalization_adjustment: Decimal = Decimal("0.00")
    personal_extra: Decimal = Decimal("0.00")
    transfer_for_savings_and_equalization: Decimal = Decimal("0.00")
    total_transfer_to_central: Decimal = Decimal("0.00")
    real_balance_ars: Decimal = Decimal("0.00")
    real_balance_usd: Decimal = Decimal("0.00")
    real_balance_usd_ars: Decimal = Decimal("0.00")
    real_balance_total_ars: Decimal = Decimal("0.00")
    remaining_ars_after_obligations: Decimal = Decimal("0.00")
    remaining_ars_after_total_transfer: Decimal = Decimal("0.00")
    remaining_total_after_total_transfer: Decimal = Decimal("0.00")
    transfer_to_keep_equal_extra_in_ars: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class SettlementResult:
    """Resultado consolidado de la liquidación de compromisos y excedente del mes."""

    period: date
    participants: list[SettlementParticipant]
    obligations: list[MonthlyObligation]
    total_obligations: Decimal
    total_shared: Decimal
    common_income_applied: Decimal
    instructions: list[str]
    total_income: Decimal = Decimal("0.00")
    total_surplus: Decimal = Decimal("0.00")
    total_savings_target: Decimal = Decimal("0.00")
    total_buffer_target: Decimal = Decimal("0.00")
    total_personal_extra: Decimal = Decimal("0.00")


def calculate_income_ratios(incomes: list[UserIncome]) -> list[IncomeRatio]:
    """Calcula las proporciones exactas de cada integrante sobre el total de ingresos.

    Si no hay ingresos computables, distribuye equitativamente (1 / n).
    Garantiza que la suma de ratios sea exactamente Decimal('1.0000').
    """
    if not incomes:
        return []

    total_income = sum((max(Decimal("0.00"), inc.income) for inc in incomes), Decimal("0.00"))

    if total_income <= Decimal("0.00"):
        n = Decimal(len(incomes))
        even_ratio = (Decimal("1.0000") / n).quantize(RATIO_PRECISION, rounding=ROUND_HALF_UP)
        ratios: list[IncomeRatio] = []
        accum = Decimal("0.0000")
        for idx, inc in enumerate(incomes):
            if idx == len(incomes) - 1:
                r = Decimal("1.0000") - accum
            else:
                r = even_ratio
                accum += r
            pct = (r * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            ratios.append(
                IncomeRatio(
                    user_id=inc.user_id,
                    username=inc.username,
                    display_name=inc.display_name,
                    income=inc.income,
                    ratio=r,
                    percentage=pct,
                )
            )
        return ratios

    ratios = []
    accum_ratio = Decimal("0.0000")
    for idx, inc in enumerate(incomes):
        eff_income = max(Decimal("0.00"), inc.income)
        if idx == len(incomes) - 1:
            ratio = Decimal("1.0000") - accum_ratio
        else:
            ratio = (eff_income / total_income).quantize(RATIO_PRECISION, rounding=ROUND_HALF_UP)
            accum_ratio += ratio
        pct = (ratio * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        ratios.append(
            IncomeRatio(
                user_id=inc.user_id,
                username=inc.username,
                display_name=inc.display_name,
                income=inc.income,
                ratio=ratio,
                percentage=pct,
            )
        )
    return ratios


def calculate_monthly_settlement(
    period: date,
    incomes: list[UserIncome],
    obligations: list[MonthlyObligation],
    personal_charges_by_user: dict[int, Decimal],
    common_income: Decimal = Decimal("0.00"),
    direct_payments_by_user: dict[int, Decimal] | None = None,
    real_balances_ars_by_user: dict[int, Decimal] | None = None,
    real_balances_usd_by_user: dict[int, Decimal] | None = None,
    fx_rate: Decimal = Decimal("1517.49"),
) -> SettlementResult:
    """Distribuye las obligaciones del mes en base a proporciones de ingreso real.

    - Los consumos personales se imputan al 100 % al usuario correspondiente.
    - Lo común se amortigua con el ingreso común (alquiler, reintegros) y se reparte según ratio.
    - Los pagos directos (ej. cuota del depto pagada por uno) se acreditan para determinar
      el saldo restante en las demás obligaciones (tarjetas).
    """
    ratios = calculate_income_ratios(incomes)
    direct_payments = direct_payments_by_user or {}
    real_ars_map = real_balances_ars_by_user or {}
    real_usd_map = real_balances_usd_by_user or {}

    total_shared = sum(
        (obl.amount for obl in obligations if obl.is_shared),
        Decimal("0.00"),
    )
    total_housing = sum(
        (obl.amount for obl in obligations if obl.is_shared and obl.category == "housing"),
        Decimal("0.00"),
    )
    total_other_shared = total_shared - total_housing
    common_income_applied = min(total_shared, max(Decimal("0.00"), common_income))
    net_shared = total_shared - common_income_applied

    # Asignación proporcional de lo común neto y sub-rubros con preservación del centavo
    shared_assigned_by_user: dict[int, Decimal] = {}
    housing_by_user: dict[int, Decimal] = {}
    card_common_by_user: dict[int, Decimal] = {}
    common_inc_by_user: dict[int, Decimal] = {}
    accum_shared = Decimal("0.00")
    accum_housing = Decimal("0.00")
    accum_card_common = Decimal("0.00")
    for idx, r in enumerate(ratios):
        if idx == len(ratios) - 1:
            assigned = net_shared - accum_shared
            h_share = total_housing - accum_housing
            c_share = total_other_shared - accum_card_common
        else:
            assigned = (net_shared * r.ratio).quantize(CENTAVO, rounding=ROUND_HALF_UP)
            h_share = (total_housing * r.ratio).quantize(CENTAVO, rounding=ROUND_HALF_UP)
            c_share = (total_other_shared * r.ratio).quantize(CENTAVO, rounding=ROUND_HALF_UP)
            accum_shared += assigned
            accum_housing += h_share
            accum_card_common += c_share
        shared_assigned_by_user[r.user_id] = assigned
        housing_by_user[r.user_id] = h_share
        card_common_by_user[r.user_id] = c_share
        common_inc_by_user[r.user_id] = (h_share + c_share) - assigned

    total_obligations_amt = sum((obl.amount for obl in obligations), Decimal("0.00")) + sum(
        personal_charges_by_user.values(), Decimal("0.00")
    )

    # Primera pasada: calcular excedente, ahorro (30%), colchón (20%) y extra proporcional (50%)
    raw_rows: list[dict[str, Decimal]] = []
    for r in ratios:
        p_charge = personal_charges_by_user.get(r.user_id, Decimal("0.00"))
        s_assigned = shared_assigned_by_user.get(r.user_id, Decimal("0.00"))
        total_liability = s_assigned + p_charge
        direct_paid = direct_payments.get(r.user_id, Decimal("0.00"))
        net_remaining = total_liability - direct_paid

        surplus_rem = max(Decimal("0.00"), max(Decimal("0.00"), r.income) - total_liability)
        savings_contrib = (surplus_rem * Decimal("0.30")).quantize(CENTAVO, rounding=ROUND_HALF_UP)
        buffer_contrib = (surplus_rem * Decimal("0.20")).quantize(CENTAVO, rounding=ROUND_HALF_UP)
        prop_extra = surplus_rem - savings_contrib - buffer_contrib

        raw_rows.append(
            {
                "p_charge": p_charge,
                "s_assigned": s_assigned,
                "total_liability": total_liability,
                "direct_paid": direct_paid,
                "net_remaining": net_remaining,
                "surplus_rem": surplus_rem,
                "savings_contrib": savings_contrib,
                "buffer_contrib": buffer_contrib,
                "prop_extra": prop_extra,
            }
        )

    # Igualación 50/50 absoluto del Extra libre entre los integrantes
    total_extra_amt = sum((row["prop_extra"] for row in raw_rows), Decimal("0.00"))
    n_users = len(ratios)
    equal_extra_base = (
        (total_extra_amt / Decimal(n_users)).quantize(CENTAVO, rounding=ROUND_FLOOR)
        if n_users > 0
        else Decimal("0.00")
    )
    extra_remainder = total_extra_amt - (equal_extra_base * Decimal(n_users))

    participants: list[SettlementParticipant] = []
    for idx, (r, row) in enumerate(zip(ratios, raw_rows, strict=True)):
        # El primer integrante absorbe el centavo impar si existe
        eq_extra = equal_extra_base + (extra_remainder if idx == 0 else Decimal("0.00"))
        eq_adj = eq_extra - row["prop_extra"]
        transfer_sav_eq = row["savings_contrib"] + row["buffer_contrib"] - eq_adj
        total_transfer = max(Decimal("0.00"), row["net_remaining"] + transfer_sav_eq)

        real_ars = real_ars_map.get(r.user_id, Decimal("0.00"))
        real_usd = real_usd_map.get(r.user_id, Decimal("0.00"))
        real_usd_ars = (real_usd * fx_rate).quantize(CENTAVO, rounding=ROUND_HALF_UP)
        real_total_ars = real_ars + real_usd_ars
        rem_ars_obl = max(Decimal("0.00"), real_ars - max(Decimal("0.00"), row["net_remaining"]))
        rem_ars_tot = max(Decimal("0.00"), real_ars - total_transfer)
        rem_tot_after_tot = max(Decimal("0.00"), real_total_ars - total_transfer)
        transfer_keep_eq_ars = max(Decimal("0.00"), real_ars - eq_extra)

        participants.append(
            SettlementParticipant(
                user_id=r.user_id,
                username=r.username,
                display_name=r.display_name,
                income=r.income,
                ratio=r.ratio,
                percentage=r.percentage,
                personal_expenses=row["p_charge"],
                shared_expenses_assigned=row["s_assigned"],
                total_liability=row["total_liability"],
                direct_payments=row["direct_paid"],
                net_remaining_to_pay=row["net_remaining"],
                housing_share=housing_by_user.get(r.user_id, Decimal("0.00")),
                card_common_share=card_common_by_user.get(r.user_id, Decimal("0.00")),
                common_income_share=common_inc_by_user.get(r.user_id, Decimal("0.00")),
                surplus_remaining=row["surplus_rem"],
                savings_contribution=row["savings_contrib"],
                buffer_contribution=row["buffer_contrib"],
                proportional_extra=row["prop_extra"],
                equalization_adjustment=eq_adj,
                personal_extra=eq_extra,
                transfer_for_savings_and_equalization=transfer_sav_eq,
                total_transfer_to_central=total_transfer,
                real_balance_ars=real_ars,
                real_balance_usd=real_usd,
                real_balance_usd_ars=real_usd_ars,
                real_balance_total_ars=real_total_ars,
                remaining_ars_after_obligations=rem_ars_obl,
                remaining_ars_after_total_transfer=rem_ars_tot,
                remaining_total_after_total_transfer=rem_tot_after_tot,
                transfer_to_keep_equal_extra_in_ars=transfer_keep_eq_ars,
            )
        )

    total_income_amt = sum((max(Decimal("0.00"), p.income) for p in participants), Decimal("0.00"))
    total_surplus_amt = sum((p.surplus_remaining for p in participants), Decimal("0.00"))
    total_savings_amt = sum((p.savings_contribution for p in participants), Decimal("0.00"))
    total_buffer_amt = sum((p.buffer_contribution for p in participants), Decimal("0.00"))

    # Instrucciones claras de liquidación
    instructions: list[str] = []
    for p in participants:
        if p.direct_payments > Decimal("0.00"):
            instructions.append(
                f"{p.display_name} ya cubrió $ {p.direct_payments:,.2f} con pagos directos."
            )
        if p.net_remaining_to_pay > Decimal("0.00"):
            instructions.append(
                f"{p.display_name} debe transferir/pagar $ {p.net_remaining_to_pay:,.2f} "
                "en tarjetas/obligaciones pendientes."
            )
        elif p.net_remaining_to_pay < Decimal("0.00"):
            instructions.append(
                f"{p.display_name} tiene un saldo a favor de "
                f"$ {-p.net_remaining_to_pay:,.2f} tras sus pagos."
            )

    return SettlementResult(
        period=period,
        participants=participants,
        obligations=obligations,
        total_obligations=total_obligations_amt,
        total_shared=total_shared,
        common_income_applied=common_income_applied,
        instructions=instructions,
        total_income=total_income_amt,
        total_surplus=total_surplus_amt,
        total_savings_target=total_savings_amt,
        total_buffer_target=total_buffer_amt,
        total_personal_extra=total_extra_amt,
    )
