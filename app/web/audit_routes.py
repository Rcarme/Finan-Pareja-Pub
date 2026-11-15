"""Rutas web para visualización y filtrado del log de auditoría (SPEC.md §6, RN-37, RN-38)."""

from datetime import date

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import KNOWN_AUDIT_ACTIONS, KNOWN_AUDIT_ENTITIES
from app.core.db import get_db
from app.models.auth import User
from app.services.audit_service import get_audit_logs_filtered
from app.web.query_types import OptionalIdentifier

audit_router = APIRouter(prefix="/audit", tags=["audit"])


def _parse_date(d_str: str | None) -> date | None:
    if not d_str:
        return None
    try:
        parts = d_str.strip().split("-")
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return None


@audit_router.get("", response_class=HTMLResponse)
def audit_index(
    request: Request,
    entity: str | None = None,
    entity_type: str | None = None,
    action: str | None = None,
    user_id: OptionalIdentifier = None,
    from_date: str | None = None,
    to_date: str | None = None,
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_db),
) -> Response:
    """Pantalla de auditoría con filtros por entidad, usuario, acción y rango de fechas."""
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    from app.web.app import templates

    parsed_from = _parse_date(from_date)
    parsed_to = _parse_date(to_date)

    effective_page = max(1, page)
    effective_size = min(max(1, page_size), 100)
    effective_entity = entity or entity_type

    logs, total_count = get_audit_logs_filtered(
        db=db,
        entity=effective_entity if effective_entity else None,
        action=action if action else None,
        user_id=user_id,
        from_date=parsed_from,
        to_date=parsed_to,
        limit=effective_size,
        offset=(effective_page - 1) * effective_size,
    )

    all_users = list(db.execute(select(User).order_by(User.id)).scalars().all())
    users_map = {u.id: (u.display_name or u.username) for u in all_users}

    total_pages = max(1, (total_count + effective_size - 1) // effective_size)

    return templates.TemplateResponse(
        request=request,
        name="audit/index.html",
        context={
            "user": current_user,
            "active_nav": "audit",
            "logs": logs,
            "total_count": total_count,
            "page": effective_page,
            "page_size": effective_size,
            "total_pages": total_pages,
            "users": all_users,
            "users_map": users_map,
            "known_entities": KNOWN_AUDIT_ENTITIES,
            "known_actions": KNOWN_AUDIT_ACTIONS,
            "filter_entity": effective_entity or "",
            "filter_action": action or "",
            "filter_user_id": user_id,
            "filter_from_date": from_date or "",
            "filter_to_date": to_date or "",
        },
    )
