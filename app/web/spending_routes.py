"""Reporte informativo de consumo por persona, compartido entre ambas vistas."""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.formatters import parse_period, today_ar
from app.core.security import generate_csrf_token, secure_cookies
from app.models.auth import User
from app.services.category_service import canonical_category
from app.services.retirement_service import get_retirement_report
from app.services.spending_service import get_spending_people, get_spending_report
from app.web.query_types import OptionalIdentifier

spending_router = APIRouter(tags=["spending"])


@spending_router.get("/reports/spending")
@spending_router.get("/s/spending")
def spending_view(
    request: Request,
    period: str | None = None,
    group: str = "all",
    category_id: OptionalIdentifier = None,
    page: int = 1,
    db: Session = Depends(get_db),
) -> Response:
    user = getattr(request.state, "user", None)
    if not isinstance(user, User):
        raise HTTPException(401, "Iniciá sesión.")
    people_keys = {p.key for p in get_spending_people(db)} | {"all", "common"}
    if group not in people_keys or page < 1:
        raise HTTPException(400, "Elegí un grupo y una página válidos.")
    selected_category = None
    if category_id is not None:
        if category_id < 1:
            raise HTTPException(400, "Elegí una categoría válida.")
        selected_category = canonical_category(db, category_id)
        if selected_category is None:
            raise HTTPException(400, "Categoría no encontrada.")
        category_id = selected_category.id
    try:
        normalized = parse_period(period) if period else today_ar().replace(day=1)
        report = get_spending_report(db, normalized)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error

    simple_view = request.url.path.startswith("/s/")
    path = "/s/spending" if simple_view else "/reports/spending"

    def link(selected_group: str, category: int | None = None, number: int = 1) -> str:
        query: dict[str, str] = {
            "period": report.period.strftime("%Y-%m"),
            "group": selected_group,
        }
        if category is not None:
            query["category_id"] = str(category)
        if number != 1:
            query["page"] = str(number)
        return path + "?" + urlencode(query) + "#spending-detail"

    categories = {entry.category_id: entry.category_name for entry in report.entries}
    if selected_category is not None:
        categories[selected_category.id] = selected_category.name
    filtered = [
        entry
        for entry in report.entries
        if (group == "all" or entry.group_key == group)
        and (category_id is None or entry.category_id == category_id)
    ]
    offset = (page - 1) * 50
    from app.web.app import templates

    csrf_token = request.cookies.get("csrf_token") or generate_csrf_token()
    response = templates.TemplateResponse(
        request=request,
        name="reports/spending.html",
        context={
            "user": user,
            "csrf_token": csrf_token,
            "title": "Quién gastó · Finanzas",
            "report": report,
            "retirement": get_retirement_report(db, report.period),
            "simple_view": simple_view,
            "active_nav": "spending",
            "nav": "spending",
            "path": path,
            "group": group,
            "category_id": category_id,
            "categories": sorted(categories.items(), key=lambda item: item[1].casefold()),
            "entries": filtered[offset : offset + 50],
            "filtered_count": len(filtered),
            "page": page,
            "previous_url": link(group, category_id, page - 1) if page > 1 else None,
            "next_url": link(group, category_id, page + 1) if offset + 50 < len(filtered) else None,
            "spending_link": link,
        },
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, samesite="lax", secure=secure_cookies())
    return response
