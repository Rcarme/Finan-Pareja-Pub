"""Lógica de dominio pura para reportes, gráficos y exportaciones (RN-43, SPEC.md §6).

Contiene cálculos analíticos y modelos de datos inmutables:
- Resumen mensual y comparaciones porcentuales intermensuales.
- Distribución de gastos por categoría y proporciones.
- Proyecciones y sugerencias para metas de ahorro.
- Métricas consolidadas del departamento (USD, ARS, tipo de cambio efectivo ponderado).
- Aportes informativos por persona (no alteran el reparto 50/50).
- Reporte de Cargos personales (RN-43): detalle por persona, período, descuento y arrastre.
- Exportes CSV (RFC 4180 con UTF-8-sig) y XLSX (OpenXML nativo sin librerías externas).
"""

import csv
import datetime as dt
import io
import re
import xml.sax.saxutils as saxutils
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.domain.envelopes import calculate_monthly_goal_contribution


@dataclass(frozen=True)
class MonthlySummaryData:
    """Datos consolidados de un período mensual."""

    period: date
    total_income: Decimal
    total_expense: Decimal
    total_savings: Decimal
    total_distributed: Decimal


@dataclass(frozen=True)
class MonthlyComparisonReport:
    """Comparativa mensual con el período anterior."""

    current: MonthlySummaryData
    previous: MonthlySummaryData | None
    net_result: Decimal
    income_diff: Decimal
    expense_diff: Decimal
    net_diff: Decimal
    income_pct_change: Decimal | None
    expense_pct_change: Decimal | None


@dataclass(frozen=True)
class CategoryExpenseItem:
    """Gasto agregado por categoría con su peso relativo."""

    category_id: int
    category_name: str
    amount: Decimal
    percentage: Decimal
    transaction_count: int


@dataclass(frozen=True)
class CategoryExpensesReport:
    """Reporte de distribución de gastos por categoría."""

    total_expense: Decimal
    items: list[CategoryExpenseItem]


BALANCE_ADJUSTMENT_CATEGORY = "Ajuste de conciliación"


@dataclass(frozen=True)
class BalanceAdjustmentItem:
    """Corrección técnica trazable, separada del consumo por categoría."""

    transaction_id: int
    date: date
    description: str
    amount: Decimal
    kind: str


@dataclass(frozen=True)
class BalanceAdjustmentsReport:
    """Ajustes del período conservando ingresos y egresos por separado."""

    items: list[BalanceAdjustmentItem]
    total_income: Decimal
    total_expense: Decimal


def is_balance_adjustment_category(name: str | None, is_system: bool | None) -> bool:
    """RN-19: una categoría homónima del usuario no identifica conciliación."""
    return is_system is True and name == BALANCE_ADJUSTMENT_CATEGORY


def calculate_balance_adjustments_summary(
    items: list[BalanceAdjustmentItem],
) -> BalanceAdjustmentsReport:
    """Suma ambos sentidos sin compensarlos ni alterar su efecto contable."""
    return BalanceAdjustmentsReport(
        items=items,
        total_income=sum((item.amount for item in items if item.kind == "income"), Decimal("0.00")),
        total_expense=sum(
            (item.amount for item in items if item.kind == "expense"), Decimal("0.00")
        ),
    )


@dataclass(frozen=True)
class MonthlyEvolutionItem:
    """Punto en la serie temporal de 12 meses."""

    period: date
    income: Decimal
    expense: Decimal
    net: Decimal
    fund_balance: Decimal
    available_balance: Decimal


@dataclass(frozen=True)
class MonthlyEvolutionReport:
    """Serie temporal de los últimos 12 meses."""

    items: list[MonthlyEvolutionItem]


@dataclass(frozen=True)
class SavingsGoalItem:
    """Estado y proyección de una meta o sobre de ahorro."""

    envelope_id: int
    envelope_name: str
    category_name: str
    target_amount: Decimal | None
    current_balance: Decimal
    progress_pct: Decimal | None
    target_date: date | None
    months_remaining: int | None
    suggested_monthly_savings: Decimal | None


@dataclass(frozen=True)
class SavingsGoalsReport:
    """Reporte consolidado de ahorros y metas."""

    items: list[SavingsGoalItem]


@dataclass(frozen=True)
class DepartmentPaymentItem:
    """Pago individual de cuota o refuerzo del departamento."""

    item_id: int
    due_date: date
    payment_date: date
    usd_amount: Decimal
    ars_amount: Decimal
    effective_fx_rate: Decimal


@dataclass(frozen=True)
class DepartmentUpcomingItem:
    """Vencimiento pendiente del departamento."""

    item_id: int
    due_date: date
    usd_amount: Decimal
    status: str


@dataclass(frozen=True)
class DepartmentMetricsReport:
    """Métricas acumuladas del departamento."""

    total_usd_paid: Decimal
    total_ars_paid: Decimal
    overall_effective_fx_rate: Decimal | None
    payments: list[DepartmentPaymentItem]
    upcoming_items: list[DepartmentUpcomingItem]


@dataclass(frozen=True)
class UserContributionItem:
    """Aporte informativo generado por cada persona en el período."""

    user_id: int
    username: str
    display_name: str
    total_income_generated: Decimal
    percentage_of_total: Decimal


@dataclass(frozen=True)
class ContributionsReport:
    """Reporte de aportes por persona (no afecta el reparto de cierre)."""

    total_income: Decimal
    items: list[UserContributionItem]
    common_income: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class PersonalChargeDetailItem:
    """Detalle de un movimiento marcado a cargo de una persona."""

    transaction_id: int
    user_id: int
    username: str
    display_name: str
    date: date
    description: str
    amount: Decimal
    period: dt.date | None = None


@dataclass(frozen=True)
class UserPersonalChargesSummary:
    """Resumen de cargos personales de un usuario en un período (RN-43)."""

    user_id: int
    username: str
    display_name: str
    total_charged: Decimal
    deducted_in_close: Decimal
    carried_over_to_next: Decimal
    charges: list[PersonalChargeDetailItem]
    current_period_charged: Decimal = Decimal("0.00")
    previous_period_charged: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class PersonalChargesReport:
    """Reporte de cargos personales de todos los usuarios en el período (RN-43)."""

    period: date
    by_user: list[UserPersonalChargesSummary]


def calculate_monthly_comparison(
    current: MonthlySummaryData,
    previous: MonthlySummaryData | None,
) -> MonthlyComparisonReport:
    """Calcula la comparativa absoluta y porcentual con el mes anterior."""
    net_result = current.total_income - current.total_expense

    if previous is None:
        return MonthlyComparisonReport(
            current=current,
            previous=None,
            net_result=net_result,
            income_diff=Decimal("0.00"),
            expense_diff=Decimal("0.00"),
            net_diff=Decimal("0.00"),
            income_pct_change=None,
            expense_pct_change=None,
        )

    prev_net = previous.total_income - previous.total_expense
    income_diff = current.total_income - previous.total_income
    expense_diff = current.total_expense - previous.total_expense
    net_diff = net_result - prev_net

    income_pct_change: Decimal | None = None
    if previous.total_income > Decimal("0.00"):
        income_pct_change = ((income_diff / previous.total_income) * Decimal("100.00")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    expense_pct_change: Decimal | None = None
    if previous.total_expense > Decimal("0.00"):
        expense_pct_change = ((expense_diff / previous.total_expense) * Decimal("100.00")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    return MonthlyComparisonReport(
        current=current,
        previous=previous,
        net_result=net_result,
        income_diff=income_diff,
        expense_diff=expense_diff,
        net_diff=net_diff,
        income_pct_change=income_pct_change,
        expense_pct_change=expense_pct_change,
    )


def calculate_category_expenses_summary(
    raw_items: list[tuple[str, int, Decimal, int]],
) -> CategoryExpensesReport:
    """Calcula totales y porcentajes de gastos agrupados por categoría.

    raw_items: tuplas (category_name, category_id, amount, transaction_count).
    """
    total = sum((item[2] for item in raw_items), Decimal("0.00"))
    sorted_items = sorted(raw_items, key=lambda x: x[2], reverse=True)

    items: list[CategoryExpenseItem] = []
    for cat_name, cat_id, amount, count in sorted_items:
        pct = Decimal("0.00")
        if total > Decimal("0.00"):
            pct = ((amount / total) * Decimal("100.00")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        items.append(
            CategoryExpenseItem(
                category_id=cat_id,
                category_name=cat_name,
                amount=amount,
                percentage=pct,
                transaction_count=count,
            )
        )

    return CategoryExpensesReport(total_expense=total, items=items)


def calculate_savings_goals_projections(
    goals_data: list[dict[str, Any]],
    as_of_date: date,
) -> list[SavingsGoalItem]:
    """Calcula el porcentaje de progreso y la cuota mensual sugerida para cada meta."""
    results: list[SavingsGoalItem] = []

    for g in goals_data:
        envelope_id: int = g["envelope_id"]
        envelope_name: str = g["envelope_name"]
        category_name: str = g.get("category_name", "Ahorro")
        target_amount: Decimal | None = g.get("target_amount")
        current_balance: Decimal = g.get("current_balance", Decimal("0.00"))
        target_date: date | None = g.get("target_date")

        progress_pct: Decimal | None = None
        if target_amount is not None and target_amount > Decimal("0.00"):
            progress_pct = ((current_balance / target_amount) * Decimal("100.00")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        months_remaining: int | None = None
        suggested_monthly: Decimal | None = None

        if target_date is not None and target_amount is not None:
            current_period = date(as_of_date.year, as_of_date.month, 1)
            raw_m = (
                (target_date.year - current_period.year) * 12
                + (target_date.month - current_period.month)
                + 1
            )
            months_remaining = max(1, raw_m) if target_amount > current_balance else max(0, raw_m)
            suggested_monthly = calculate_monthly_goal_contribution(
                target_amount=target_amount,
                current_balance=current_balance,
                current_period=current_period,
                target_date=target_date,
            )

        results.append(
            SavingsGoalItem(
                envelope_id=envelope_id,
                envelope_name=envelope_name,
                category_name=category_name,
                target_amount=target_amount,
                current_balance=current_balance,
                progress_pct=progress_pct,
                target_date=target_date,
                months_remaining=months_remaining,
                suggested_monthly_savings=suggested_monthly,
            )
        )

    return results


def calculate_department_metrics(
    payments: list[DepartmentPaymentItem],
    upcoming_items: list[DepartmentUpcomingItem] | None = None,
) -> DepartmentMetricsReport:
    """Calcula totales acumulados pagados y cotización efectiva ponderada."""
    total_usd = sum((p.usd_amount for p in payments), Decimal("0.00"))
    total_ars = sum((p.ars_amount for p in payments), Decimal("0.00"))

    overall_fx: Decimal | None = None
    if total_usd > Decimal("0.00"):
        overall_fx = (total_ars / total_usd).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    return DepartmentMetricsReport(
        total_usd_paid=total_usd,
        total_ars_paid=total_ars,
        overall_effective_fx_rate=overall_fx,
        payments=payments,
        upcoming_items=upcoming_items or [],
    )


def calculate_contributions_summary(
    user_incomes: list[tuple[int, str, str, Decimal]],
    common_income: Decimal = Decimal("0.00"),
) -> ContributionsReport:
    """Calcula el aporte de cada usuario al total de ingresos (puramente informativo)."""
    total = sum((u[3] for u in user_incomes), Decimal("0.00"))

    items: list[UserContributionItem] = []
    for user_id, username, display_name, amount in user_incomes:
        pct = Decimal("0.00")
        if total > Decimal("0.00"):
            pct = ((amount / total) * Decimal("100.00")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        items.append(
            UserContributionItem(
                user_id=user_id,
                username=username,
                display_name=display_name,
                total_income_generated=amount,
                percentage_of_total=pct,
            )
        )

    return ContributionsReport(total_income=total, items=items, common_income=common_income)


def calculate_personal_charges_summary(
    charges: list[PersonalChargeDetailItem],
    close_deductions: dict[int, tuple[Decimal, Decimal]],
    period: date = date(2028, 1, 1),
) -> PersonalChargesReport:
    """RN-43: Agrupa cargos personales por usuario y cruza con las deducciones del cierre."""
    # Agrupar cargos por usuario
    charges_by_user: dict[int, list[PersonalChargeDetailItem]] = {}
    users_meta: dict[int, tuple[str, str]] = {}

    for c in charges:
        charges_by_user.setdefault(c.user_id, []).append(c)
        users_meta[c.user_id] = (c.username, c.display_name)

    # Asegurar que usuarios con deducción registrada en cierre también figuren
    for uid in close_deductions:
        if uid not in users_meta:
            users_meta[uid] = (f"user_{uid}", f"Usuario {uid}")
            charges_by_user.setdefault(uid, [])

    summaries: list[UserPersonalChargesSummary] = []
    for uid in sorted(users_meta.keys()):
        uname, dname = users_meta[uid]
        user_charges = charges_by_user.get(uid, [])
        total_charged = sum((c.amount for c in user_charges), Decimal("0.00"))
        current_period_charged = sum(
            (c.amount for c in user_charges if c.period is None or c.period == period),
            Decimal("0.00"),
        )
        deducted, carried = close_deductions.get(uid, (Decimal("0.00"), Decimal("0.00")))

        summaries.append(
            UserPersonalChargesSummary(
                user_id=uid,
                username=uname,
                display_name=dname,
                total_charged=total_charged,
                deducted_in_close=deducted,
                carried_over_to_next=carried,
                charges=user_charges,
                current_period_charged=current_period_charged,
                previous_period_charged=total_charged - current_period_charged,
            )
        )

    return PersonalChargesReport(period=period, by_user=summaries)


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_INVALID_XML_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_formula(val: str) -> str:
    lines = val.split("\n")
    if any(line.lstrip().startswith(_FORMULA_PREFIXES) for line in lines):
        return f"'{val}"
    return val


def _clean_xml_text(s: str) -> str:
    return saxutils.escape(_INVALID_XML_CHARS_RE.sub("", s))


def generate_csv_content(headers: list[str], rows: list[list[Any]]) -> bytes:
    """Genera archivo CSV conforme a RFC 4180 con UTF-8-sig y protección de fórmulas."""
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    sanitized_headers = [_sanitize_formula(h) if isinstance(h, str) else str(h) for h in headers]
    writer.writerow(sanitized_headers)

    for row in rows:
        formatted_row: list[Any] = []
        for val in row:
            if isinstance(val, Decimal):
                formatted_row.append(str(val))
            elif isinstance(val, date):
                formatted_row.append(val.strftime("%Y-%m-%d"))
            elif val is None:
                formatted_row.append("")
            else:
                s = _sanitize_formula(str(val))
                formatted_row.append(s)
        writer.writerow(formatted_row)

    return output.getvalue().encode("utf-8-sig")


def _col_idx_to_name(col_idx: int) -> str:
    """Convierte un índice de columna 1-based (1, 2, 27) a letras Excel ('A', 'B', 'AA')."""
    name = ""
    while col_idx > 0:
        col_idx, rem = divmod(col_idx - 1, 26)
        name = chr(65 + rem) + name
    return name


def build_minimal_xlsx(
    headers: list[str],
    rows: list[list[Any]],
    sheet_name: str = "Datos",
) -> bytes:
    """Genera un archivo XLSX válido (OpenXML) nativamente con zipfile."""
    safe_sheet_name = _clean_xml_text(sheet_name[:31])

    # Construir contenido XML de la hoja
    sheet_lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]

    # Fila 1: Cabeceras
    sheet_lines.append('<row r="1">')
    for col_idx, h in enumerate(headers, start=1):
        cell_ref = f"{_col_idx_to_name(col_idx)}1"
        h_str = _sanitize_formula(str(h))
        escaped_h = _clean_xml_text(h_str)
        sheet_lines.append(f'<c r="{cell_ref}" t="inlineStr"><is><t>{escaped_h}</t></is></c>')
    sheet_lines.append("</row>")

    # Filas de datos
    for row_idx, row in enumerate(rows, start=2):
        sheet_lines.append(f'<row r="{row_idx}">')
        for col_idx, val in enumerate(row, start=1):
            cell_ref = f"{_col_idx_to_name(col_idx)}{row_idx}"
            if val is None:
                continue
            if isinstance(val, (int, Decimal)):
                sheet_lines.append(f'<c r="{cell_ref}"><v>{val}</v></c>')
            else:
                text_val = val.strftime("%Y-%m-%d") if isinstance(val, date) else str(val)
                text_val = _sanitize_formula(text_val)
                escaped_text = _clean_xml_text(text_val)
                sheet_lines.append(
                    f'<c r="{cell_ref}" t="inlineStr"><is><t>{escaped_text}</t></is></c>'
                )
        sheet_lines.append("</row>")

    sheet_lines.append("</sheetData>")
    sheet_lines.append("</worksheet>")
    sheet_xml = "\n".join(sheet_lines).encode("utf-8")

    content_types_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        b'  <Default Extension="rels" '
        b'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        b'  <Default Extension="xml" ContentType="application/xml"/>\n'
        b'  <Override PartName="/xl/workbook.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>\n'
        b'  <Override PartName="/xl/worksheets/sheet1.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>\n'
        b"</Types>"
    )

    rels_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        b'  <Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/'
        b'officeDocument/2006/relationships/officeDocument" '
        b'Target="xl/workbook.xml"/>\n'
        b"</Relationships>"
    )

    wb_rels_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        b'  <Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        b'Target="worksheets/sheet1.xml"/>\n'
        b"</Relationships>"
    )

    workbook_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">\n'
        f"  <sheets>\n"
        f'    <sheet name="{safe_sheet_name}" sheetId="1" r:id="rId1"/>\n'
        f"  </sheets>\n"
        f"</workbook>"
    ).encode()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    return buffer.getvalue()
