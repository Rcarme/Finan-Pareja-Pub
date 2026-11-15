"""Rutas web para gestión de plantillas y movimientos recurrentes mensuales (RN-35)."""

from datetime import date
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.formatters import parse_decimal, parse_period, today_ar
from app.core.security import generate_csrf_token, validate_csrf
from app.domain.recurring import RecurringTemplateInput
from app.models.auth import User
from app.models.ledger import Account
from app.services.category_service import canonical_categories, list_categories
from app.services.idempotency_service import (
    DuplicateCompletedError,
    OperationInProgressError,
    abort_operation,
    begin_operation,
    finish_operation,
    operation_payload,
)
from app.services.ledger_service import ClosedPeriodError
from app.services.recurring_service import (
    confirm_recurring_pending,
    create_recurring_template,
    dismiss_recurring_pending,
    generate_recurring_pendings_for_period,
    get_all_recurring_templates,
    get_pending_recurrings_for_period,
    update_recurring_template,
)

recurring_router = APIRouter(prefix="/recurring", tags=["recurring"])


def _ensure_csrf_token(request: Request) -> str:
    token = request.cookies.get("csrf_token")
    if not token:
        token = generate_csrf_token()
    return token


def _normalize_period(period_str: str | None) -> date:
    today = today_ar()
    return parse_period(period_str) if period_str else date(today.year, today.month, 1)


@recurring_router.get("", response_class=HTMLResponse)
def recurring_index(
    request: Request,
    period: str | None = None,
    success: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Pantalla principal de recurrentes: listado de plantillas y pendientes del mes."""
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    csrf_token = _ensure_csrf_token(request)
    normalized_period = _normalize_period(period)

    templates_list = get_all_recurring_templates(db)
    pendings_list = get_pending_recurrings_for_period(db, period=normalized_period)

    accounts = list(
        db.execute(
            select(Account)
            .where(
                Account.active == True,  # noqa: E712
                Account.is_virtual.is_(False),
                Account.currency == "ARS",
            )
            .order_by(Account.name)
        )
        .scalars()
        .all()
    )
    categories = list_categories(db, current_user.id, show_all=True)
    users = list(
        db.execute(
            select(User).where(User.active == True).order_by(User.id)  # noqa: E712
        )
        .scalars()
        .all()
    )

    from app.web.app import templates

    context: dict[str, Any] = {
        "request": request,
        "user": current_user,
        "csrf_token": csrf_token,
        "active_nav": "recurring",
        "templates_list": templates_list,
        "pendings_list": pendings_list,
        "accounts": accounts,
        "categories": categories,
        "canonical_categories": canonical_categories(db),
        "users": users,
        "period": normalized_period,
        "period_str": normalized_period.strftime("%Y-%m-%d"),
        "period_display": normalized_period.strftime("%Y-%m"),
        "success": success,
        "error": error,
        "today_str": today_ar().strftime("%Y-%m-%d"),
    }

    response = templates.TemplateResponse(
        request=request, name="recurring/index.html", context=context
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


@recurring_router.post("/templates")
def recurring_template_create_post(
    request: Request,
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    name: str = Form(...),
    kind: str = Form(...),
    category_id: int | None = Form(None),
    account_id: int | None = Form(None),
    day_of_month: int = Form(...),
    estimated_amount: str = Form(...),
    charged_to_user_id: int | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Crea una nueva plantilla recurrente mensual (RN-35)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    idem = None
    try:
        idem = begin_operation(
            db,
            payload=operation_payload(locals()),
            token=idempotency_token,
            route="/recurring/templates",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/recurring?success=plantilla_creada", status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        if not isinstance(category_id, int):
            raise ValueError("Elegí una categoría.")
        if not isinstance(account_id, int):
            raise ValueError("Elegí una cuenta.")
        dec_amount = parse_decimal(estimated_amount)
        tpl_input = RecurringTemplateInput(
            kind=kind,
            name=name.strip(),
            category_id=category_id,
            account_id=account_id,
            day_of_month=day_of_month,
            estimated_amount=dec_amount,
            charged_to_user_id=charged_to_user_id if charged_to_user_id else None,
            active=True,
        )
        tpl = create_recurring_template(db, tpl_input, user_id=current_user.id)
        if idem is not None:
            finish_operation(db, idem, result_entity="RecurringTemplate", result_id=tpl.id)
        return RedirectResponse(url="/recurring?success=plantilla_creada", status_code=303)
    except (ValueError, ClosedPeriodError) as exc:
        if idem is not None:
            abort_operation(db, idem)
        return RedirectResponse(url=f"/recurring?error={quote(str(exc), safe='')}", status_code=303)


@recurring_router.post("/templates/{template_id}")
def recurring_template_update_post(
    template_id: int,
    request: Request,
    csrf_token: str | None = Form(None),
    name: str = Form(...),
    kind: str = Form(...),
    category_id: int | None = Form(None),
    account_id: int | None = Form(None),
    day_of_month: int = Form(...),
    estimated_amount: str = Form(...),
    charged_to_user_id: int | None = Form(None),
    active: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Actualiza una plantilla recurrente existente (RN-35)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    try:
        if not isinstance(category_id, int):
            raise ValueError("Elegí una categoría.")
        if not isinstance(account_id, int):
            raise ValueError("Elegí una cuenta.")
        dec_amount = parse_decimal(estimated_amount)
        is_active = active in ("true", "1", "on", True)
        tpl_input = RecurringTemplateInput(
            kind=kind,
            name=name.strip(),
            category_id=category_id,
            account_id=account_id,
            day_of_month=day_of_month,
            estimated_amount=dec_amount,
            charged_to_user_id=charged_to_user_id if charged_to_user_id else None,
            active=is_active,
        )
        update_recurring_template(
            db, template_id=template_id, tpl_input=tpl_input, user_id=current_user.id
        )
        return RedirectResponse(url="/recurring?success=plantilla_actualizada", status_code=303)
    except (ValueError, ClosedPeriodError) as exc:
        return RedirectResponse(url=f"/recurring?error={quote(str(exc), safe='')}", status_code=303)


@recurring_router.post("/generate")
def recurring_generate_post(
    request: Request,
    csrf_token: str | None = Form(None),
    period: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Genera pendientes mensuales a partir de plantillas activas (RN-35)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    try:
        normalized_period = _normalize_period(period)
        generate_recurring_pendings_for_period(
            db, period=normalized_period, user_id=current_user.id
        )
        return RedirectResponse(
            url=f"/recurring?period={normalized_period.strftime('%Y-%m')}&success=pendientes_generados",
            status_code=303,
        )
    except (ValueError, ClosedPeriodError) as exc:
        return RedirectResponse(url=f"/recurring?error={quote(str(exc), safe='')}", status_code=303)


@recurring_router.post("/pendings/{pending_id}/confirm")
def recurring_confirm_post(
    pending_id: int,
    request: Request,
    csrf_token: str | None = Form(None),
    payment_date: str = Form(...),
    actual_amount: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Confirma un movimiento recurrente en 1 toque generando movimiento contable (RN-35)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    try:
        p_date = date.fromisoformat(payment_date.strip())
        dec_amount = (
            parse_decimal(actual_amount) if actual_amount and actual_amount.strip() else None
        )

        confirm_recurring_pending(
            db=db,
            pending_id=pending_id,
            payment_date=p_date,
            user_id=current_user.id,
            actual_amount=dec_amount,
        )
        return RedirectResponse(url="/recurring?success=confirmado", status_code=303)
    except ClosedPeriodError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        return RedirectResponse(url=f"/recurring?error={quote(str(exc), safe='')}", status_code=303)


@recurring_router.post("/pendings/{pending_id}/dismiss")
def recurring_dismiss_post(
    pending_id: int,
    request: Request,
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Descarta un movimiento recurrente en 1 toque sin impacto en libro (RN-35)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    try:
        dismiss_recurring_pending(db, pending_id=pending_id, user_id=current_user.id)
        return RedirectResponse(url="/recurring?success=descartado", status_code=303)
    except (ValueError, ClosedPeriodError) as exc:
        return RedirectResponse(url=f"/recurring?error={quote(str(exc), safe='')}", status_code=303)
