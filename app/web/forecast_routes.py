"""Consulta de proyección mensual: RN-47, sin crear movimientos."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.services.forecast_service import get_forecast

forecast_router = APIRouter(prefix="/forecast", tags=["forecast"])


@forecast_router.get("")
def forecast_dashboard(request: Request, db: Session = Depends(get_db)) -> Response:
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    from app.web.app import templates

    return templates.TemplateResponse(
        request=request,
        name="forecast/index.html",
        context={
            "user": user,
            "active_nav": "forecast",
            "title": "Proyección — Finanzas",
            "report": get_forecast(db),
        },
    )
