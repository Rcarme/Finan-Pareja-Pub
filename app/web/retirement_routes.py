"""Sobre externo informativo: aportes registrados sin reservar dinero otra vez."""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.formatters import parse_period, today_ar
from app.core.security import generate_csrf_token, secure_cookies
from app.models.auth import User
from app.services.retirement_service import get_retirement_report

retirement_router = APIRouter(tags=["retirement"])


@retirement_router.get("/s/retirement")
@retirement_router.get("/envelopes/retirement")
def retirement_view(
    request: Request,
    period: str | None = None,
    scope: str = "all",
    page: int = 1,
    db: Session = Depends(get_db),
) -> Response:
    user = getattr(request.state, "user", None)
    if not isinstance(user, User):
        raise HTTPException(401, "Iniciá sesión.")
    if scope not in ("all", "month") or page < 1:
        raise HTTPException(400, "Elegí un alcance y una página válidos.")
    try:
        month = parse_period(period) if period else today_ar().replace(day=1)
        report = get_retirement_report(db, month)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    assert report.selected_period is not None
    simple_view = request.url.path.startswith("/s/")
    path = "/s/retirement" if simple_view else "/envelopes/retirement"

    def link(period_value: str, scope_value: str, number: int = 1) -> str:
        query = {"period": period_value, "scope": scope_value}
        if number != 1:
            query["page"] = str(number)
        return path + "?" + urlencode(query) + "#retirement-detail"

    selected = report.selected_period.strftime("%Y-%m")
    filtered = [
        entry
        for entry in report.entries
        if scope == "all" or entry.period.replace(day=1) == report.selected_period
    ]
    offset = (page - 1) * 50
    csrf_token = request.cookies.get("csrf_token") or generate_csrf_token()
    from app.web.app import templates

    response = templates.TemplateResponse(
        request=request,
        name="retirement/detail.html",
        context={
            "user": user,
            "csrf_token": csrf_token,
            "title": "Retiro y ahorro externo",
            "simple_view": simple_view,
            "nav": "home",
            "active_nav": "envelopes",
            "path": path,
            "scope": scope,
            "page": page,
            "report": report,
            "entries": filtered[offset : offset + 50],
            "filtered_count": len(filtered),
            "retirement_link": link,
            "previous_url": link(selected, scope, page - 1) if page > 1 else None,
            "next_url": link(selected, scope, page + 1) if offset + 50 < len(filtered) else None,
        },
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, samesite="lax", secure=secure_cookies())
    return response
