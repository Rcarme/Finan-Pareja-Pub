"""Rutas web para sobres, ahorros y metas financieras.

Reglas asociadas: RN-12 a RN-17, RN-44, RN-45.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.formatters import parse_decimal, parse_period, today_ar
from app.core.security import generate_csrf_token, validate_csrf
from app.domain.envelopes import (
    calculate_goal_progress,
    calculate_monthly_goal_contribution,
)
from app.models.envelopes import Envelope
from app.services.envelope_service import (
    allocate_to_envelope,
    create_goal,
    delete_envelope,
    get_envelope_balance,
    release_from_envelope,
)
from app.services.idempotency_service import (
    DuplicateCompletedError,
    OperationInProgressError,
    abort_operation,
    begin_operation,
    finish_operation,
    operation_payload,
)
from app.services.ledger_service import (
    ClosedPeriodError,
    get_available_fund,
)
from app.services.retirement_service import get_retirement_report

envelope_router = APIRouter(prefix="/envelopes", tags=["envelopes"])


def _ensure_csrf_token(request: Request) -> str:
    """Retorna el token CSRF existente o genera uno nuevo."""
    token = request.cookies.get("csrf_token")
    if not token:
        token = generate_csrf_token()
    return token


def _parse_period_or_today(period_str: str | None) -> date:
    today = today_ar()
    return parse_period(period_str) if period_str else date(today.year, today.month, 1)


@envelope_router.get("", response_class=HTMLResponse)
def list_envelopes(request: Request, db: Session = Depends(get_db)) -> Response:
    """Pantalla principal de sobres y metas con barras de progreso y aportes (RN-12, RN-17)."""
    current_user = getattr(request.state, "user", None)
    csrf_token = _ensure_csrf_token(request)

    today = today_ar()
    current_period = date(today.year, today.month, 1)

    available_fund = get_available_fund(db)

    all_envelopes = (
        db.execute(select(Envelope).where(Envelope.active.is_(True)).order_by(Envelope.id))
        .scalars()
        .all()
    )

    system_envelopes = []
    goal_envelopes = []
    total_envelopes = Decimal("0.00")

    for env in all_envelopes:
        balance = get_envelope_balance(db, env.id)
        total_envelopes += balance

        if env.is_system:
            system_envelopes.append(
                {
                    "id": env.id,
                    "name": env.name,
                    "type": env.type,
                    "balance": balance,
                }
            )
        else:
            progress = (
                calculate_goal_progress(env.target_amount, balance)
                if env.target_amount
                else Decimal("0.00")
            )
            progress_clamped = min(Decimal("100.00"), progress)
            monthly_contrib = (
                calculate_monthly_goal_contribution(
                    target_amount=env.target_amount,
                    current_balance=balance,
                    current_period=current_period,
                    target_date=env.target_date,
                )
                if env.target_amount and env.target_date
                else None
            )

            goal_envelopes.append(
                {
                    "id": env.id,
                    "name": env.name,
                    "type": env.type,
                    "balance": balance,
                    "target_amount": env.target_amount,
                    "target_date": env.target_date,
                    "progress": progress,
                    "progress_clamped": progress_clamped,
                    "monthly_contribution": monthly_contrib,
                }
            )

    from app.web.app import templates

    response = templates.TemplateResponse(
        request=request,
        name="envelopes/list.html",
        context={
            "title": "Sobres y Metas - Finanzas",
            "user": current_user,
            "active_nav": "envelopes",
            "csrf_token": csrf_token,
            "available_fund": available_fund,
            "total_envelopes": total_envelopes,
            "system_envelopes": system_envelopes,
            "goal_envelopes": goal_envelopes,
            "retirement": get_retirement_report(db, current_period),
        },
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


@envelope_router.get("/new-goal", response_class=HTMLResponse)
def new_goal_form(request: Request) -> Response:
    """Formulario para definir una nueva meta financiera o sobre de ahorro (RN-17, RN-45)."""
    current_user = getattr(request.state, "user", None)
    csrf_token = _ensure_csrf_token(request)

    from app.web.app import templates

    response = templates.TemplateResponse(
        request=request,
        name="envelopes/new_goal.html",
        context={
            "title": "Nueva Meta - Finanzas",
            "user": current_user,
            "active_nav": "envelopes",
            "csrf_token": csrf_token,
        },
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


@envelope_router.post("/new-goal")
def create_goal_action(
    request: Request,
    name: str = Form(...),
    target_amount: str | None = Form(None),
    target_date: str | None = Form(None),
    envelope_type: str = Form("goal"),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Crea una meta de ahorro o sobre de ahorro persistente (RN-17, RN-45)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    try:
        idem = begin_operation(
            db,
            payload=operation_payload(locals()),
            token=idempotency_token,
            route="envelopes.new-goal",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/envelopes", status_code=303)
    except OperationInProgressError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    try:
        amount_dec = (
            parse_decimal(target_amount) if target_amount and target_amount.strip() else None
        )
        parsed_date: date | None = None
        if target_date and target_date.strip():
            parts = target_date.strip().split("-")
            if len(parts) == 2:
                parsed_date = date(int(parts[0]), int(parts[1]), 1)
            else:
                parsed_date = date.fromisoformat(target_date.strip())

        goal = create_goal(
            db=db,
            name=name,
            target_amount=amount_dec,
            target_date=parsed_date,
            user_id=current_user.id,
            envelope_type=envelope_type,
        )
        if idem is not None:
            finish_operation(db, idem, result_entity="Envelope", result_id=goal.id)
        return RedirectResponse(url="/envelopes", status_code=303)
    except (ValueError, InvalidOperation) as e:
        if idem is not None:
            abort_operation(db, idem)
        from app.web.app import templates

        return templates.TemplateResponse(
            request=request,
            name="envelopes/new_goal.html",
            context={
                "title": "Nueva Meta - Finanzas",
                "user": current_user,
                "active_nav": "envelopes",
                "csrf_token": _ensure_csrf_token(request),
                "error": str(e),
            },
            status_code=400,
        )


@envelope_router.post("/{envelope_id}/allocate")
def allocate_action(
    request: Request,
    envelope_id: int,
    amount: str = Form(...),
    period: str | None = Form(None),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Asigna dinero desde Disponible a un sobre (RN-13)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    try:
        idem = begin_operation(
            db,
            payload=operation_payload(locals()),
            token=idempotency_token,
            route="envelopes.allocate",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/envelopes", status_code=303)
    except OperationInProgressError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    try:
        amount_dec = parse_decimal(amount)
        current_period = _parse_period_or_today(period)

        em = allocate_to_envelope(
            db=db,
            envelope_id=envelope_id,
            amount=amount_dec,
            period=current_period,
            user_id=current_user.id,
        )
        if idem is not None:
            finish_operation(db, idem, result_entity="EnvelopeMovement", result_id=em.id)
        return RedirectResponse(url="/envelopes", status_code=303)
    except (ValueError, InvalidOperation, ClosedPeriodError) as e:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(status_code=400, detail=str(e)) from e


@envelope_router.post("/{envelope_id}/release")
def release_action(
    request: Request,
    envelope_id: int,
    amount: str = Form(...),
    period: str | None = Form(None),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Libera dinero de un sobre hacia Disponible (RN-13, RN-15)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    try:
        idem = begin_operation(
            db,
            payload=operation_payload(locals()),
            token=idempotency_token,
            route="envelopes.release",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/envelopes", status_code=303)
    except OperationInProgressError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    try:
        amount_dec = parse_decimal(amount)
        current_period = _parse_period_or_today(period)

        em = release_from_envelope(
            db=db,
            envelope_id=envelope_id,
            amount=amount_dec,
            period=current_period,
            user_id=current_user.id,
        )
        if idem is not None:
            finish_operation(db, idem, result_entity="EnvelopeMovement", result_id=em.id)
        return RedirectResponse(url="/envelopes", status_code=303)
    except (ValueError, InvalidOperation, ClosedPeriodError) as e:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(status_code=400, detail=str(e)) from e


@envelope_router.post("/{envelope_id}/delete")
def delete_envelope_action(
    request: Request,
    envelope_id: int,
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Da de baja lógica un sobre no del sistema que tenga saldo 0 (RN-16, RN-45)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    idem = None
    try:
        idem = begin_operation(
            db,
            payload=operation_payload(locals()),
            token=idempotency_token,
            route="envelopes.delete",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/envelopes", status_code=303)
    except OperationInProgressError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    try:
        delete_envelope(db=db, envelope_id=envelope_id, user_id=current_user.id)
        if idem is not None:
            finish_operation(db, idem, result_entity="Envelope", result_id=envelope_id)
        return RedirectResponse(url="/envelopes", status_code=303)
    except ValueError as e:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(status_code=400, detail=str(e)) from e
