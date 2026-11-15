"""Rutas web para reportes financieros, gráficos y exportes (SPEC.md §6, RN-43, RN-38)."""

from datetime import date

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.formatters import parse_period, today_ar
from app.models.close import Period
from app.services.export_service import (
    export_period_transactions_csv,
    export_period_transactions_xlsx,
)
from app.services.report_service import (
    get_balance_adjustments_report,
    get_category_expenses_report,
    get_contributions_report,
    get_department_report,
    get_monthly_evolution_report,
    get_monthly_summary_report,
    get_personal_charges_report,
    get_savings_goals_report,
)
from app.services.settlement_service import get_monthly_settlement_report

report_router = APIRouter(prefix="/reports", tags=["reports"])


def _normalize_period(period_str: str | None) -> date:
    today = today_ar()
    return parse_period(period_str) if period_str else date(today.year, today.month, 1)


@report_router.get("", response_class=HTMLResponse)
def reports_dashboard(
    request: Request,
    period: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Dashboard general de reportes financieros con gráficos interactivos y tablas accesibles."""
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    from app.web.app import templates

    norm_period = _normalize_period(period)

    # 1. Obtener datos de todos los reportes de SPEC.md §6
    monthly_summary = get_monthly_summary_report(db, period=norm_period)
    category_expenses = get_category_expenses_report(db, period=norm_period)
    balance_adjustments = get_balance_adjustments_report(db, period=norm_period)
    monthly_evolution = get_monthly_evolution_report(db, months=12, as_of_period=norm_period)
    savings_goals = get_savings_goals_report(db, as_of_date=norm_period)
    department_metrics = get_department_report(db, year=norm_period.year)
    contributions = get_contributions_report(db, period=norm_period)
    settlement = get_monthly_settlement_report(db, period=norm_period)

    # 2. Listado de períodos disponibles para el selector
    db_periods = list(
        db.execute(select(Period.period).order_by(Period.period.desc())).scalars().all()
    )
    if norm_period not in db_periods:
        db_periods.append(norm_period)
        db_periods.sort(reverse=True)

    # 3. Serializar datos para Chart.js
    categories_chart_data = {
        "labels": [item.category_name for item in category_expenses.items],
        "datasets": [
            {
                "data": [str(item.amount) for item in category_expenses.items],
                "backgroundColor": [
                    "#38bdf8",
                    "#f43f5e",
                    "#eab308",
                    "#10b981",
                    "#a855f7",
                    "#f97316",
                    "#06b6d4",
                    "#84cc16",
                ],
            }
        ],
    }

    evolution_chart_data = {
        "labels": [item.period.strftime("%m/%Y") for item in monthly_evolution.items],
        "datasets": [
            {
                "label": "Ingresos contables",
                "data": [str(item.income) for item in monthly_evolution.items],
                "borderColor": "#10b981",
                "backgroundColor": "rgba(16, 185, 129, 0.1)",
                "fill": False,
            },
            {
                "label": "Gastos contables",
                "data": [str(item.expense) for item in monthly_evolution.items],
                "borderColor": "#f43f5e",
                "backgroundColor": "rgba(244, 63, 94, 0.1)",
                "fill": False,
            },
            {
                "label": "Saldo Fondo",
                "data": [str(item.fund_balance) for item in monthly_evolution.items],
                "borderColor": "#38bdf8",
                "backgroundColor": "rgba(56, 189, 248, 0.1)",
                "fill": False,
            },
        ],
    }

    return templates.TemplateResponse(
        request=request,
        name="reports/index.html",
        context={
            "user": current_user,
            "active_nav": "reports",
            "current_period": norm_period,
            "available_periods": db_periods,
            "summary": monthly_summary,
            "category_expenses": category_expenses,
            "balance_adjustments": balance_adjustments,
            "evolution": monthly_evolution,
            "savings_goals": savings_goals,
            "department": department_metrics,
            "contributions": contributions,
            "settlement": settlement,
            "categories_chart_data": categories_chart_data,
            "evolution_chart_data": evolution_chart_data,
        },
    )


@report_router.get("/charges", response_class=HTMLResponse)
def personal_charges_report_view(
    request: Request,
    period: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Reporte detallado de Cargos personales (RN-43)."""
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    from app.web.app import templates

    norm_period = _normalize_period(period)
    charges_report = get_personal_charges_report(db, period=norm_period)

    return templates.TemplateResponse(
        request=request,
        name="reports/charges.html",
        context={
            "user": current_user,
            "active_nav": "reports",
            "current_period": norm_period,
            "report": charges_report,
        },
    )


@report_router.get("/export/csv")
def export_csv_route(
    request: Request,
    period: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Descarga de transacciones del período en CSV (RFC 4180 con BOM UTF-8)."""
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    norm_period = _normalize_period(period)
    content = export_period_transactions_csv(db, period=norm_period)

    filename = f"movimientos_{norm_period.year}_{norm_period.month:02d}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@report_router.get("/export/xlsx")
def export_xlsx_route(
    request: Request,
    period: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Descarga de transacciones del período en XLSX OpenXML nativo."""
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    norm_period = _normalize_period(period)
    content = export_period_transactions_xlsx(db, period=norm_period)

    filename = f"movimientos_{norm_period.year}_{norm_period.month:02d}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
