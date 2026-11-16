"""Rutas web para la pantalla de Departamento, cuotas y provisión de refuerzos (RN-27 a RN-31)."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.formatters import parse_decimal, parse_fx_rate, today_ar
from app.core.security import generate_csrf_token, validate_csrf
from app.models.envelopes import Envelope
from app.models.ledger import Account
from app.models.obligations import ObligationItem
from app.services.envelope_service import get_envelope_balance
from app.services.fx_service import get_fx_sync_status
from app.services.idempotency_service import (
    DuplicateCompletedError,
    OperationInProgressError,
    abort_operation,
    begin_operation,
    finish_operation,
    operation_payload,
)
from app.services.ledger_service import ClosedPeriodError, get_account_balance
from app.services.obligation_service import (
    create_obligation_item,
    ensure_obligation,
    generate_obligation_schedule,
    get_department_summary,
    get_fx_rate_history,
    get_latest_fx_rate,
    pay_obligation_item,
    record_fx_rate,
)

obligation_router = APIRouter(prefix="/obligations", tags=["obligations"])


def _ensure_csrf_token(request: Request) -> str:
    """Retorna el token CSRF existente o genera uno nuevo."""
    token = request.cookies.get("csrf_token")
    if not token:
        token = generate_csrf_token()
    return token


@obligation_router.get("", response_class=HTMLResponse)
def get_department_page(
    request: Request,
    year: int | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Pantalla Departamento: cronograma, estado, totales USD/ARS y provisión (RN-27 a RN-31)."""
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    target_year = year or today_ar().year
    try:
        summary = get_department_summary(db=db, year=target_year)
    except ValueError:
        # N6: el GET no genera nada; muestra estado vacío con acción explícita.
        summary = {
            "obligation": None,
            "year": target_year,
            "items": [],
            "schedule_items": [],
            "total_paid_usd": Decimal("0.00"),
            "total_pending_usd": Decimal("0.00"),
            "total_paid_ars": Decimal("0.00"),
            "latest_fx": get_latest_fx_rate(db, as_of_date=today_ar()),
            "fx_history": get_fx_rate_history(db, limit=10),
            "provision": None,
            "next_due": None,
        }

    csrf_token = _ensure_csrf_token(request)
    from app.web.app import templates

    response = templates.TemplateResponse(
        request=request,
        name="obligations/list.html",
        context={
            "title": "Departamento - Finanzas",
            "user": user,
            "summary": summary,
            "fx_sync_status": get_fx_sync_status(db),
            "csrf_token": csrf_token,
            "today_str": today_ar().isoformat(),
            "active_nav": "obligations",
        },
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


@obligation_router.post("/fx-rate")
def update_fx_rate(
    request: Request,
    date_str: str = Form(..., alias="date"),
    rate_str: str = Form(..., alias="rate"),
    note: str | None = Form(None),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Registra una nueva cotización de referencia (RN-29)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    try:
        idem = begin_operation(
            db,
            payload=operation_payload(locals()),
            token=idempotency_token,
            route="obligations.fx-rate",
            user_id=user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/obligations", status_code=303)
    except OperationInProgressError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    try:
        parsed_date = date.fromisoformat(date_str)
        parsed_rate = parse_fx_rate(rate_str)
        fx = record_fx_rate(
            db=db,
            fx_date=parsed_date,
            rate=parsed_rate,
            user_id=user.id,
            note=note,
        )
        if idem is not None:
            finish_operation(db, idem, result_entity="FxRate", result_id=fx.id)
        return RedirectResponse(url=f"/obligations?year={parsed_date.year}", status_code=303)
    except (ValueError, ClosedPeriodError) as e:
        if idem is not None:
            abort_operation(db, idem)
        from app.web.app import templates

        summary = get_department_summary(db=db, year=today_ar().year)
        token = _ensure_csrf_token(request)
        return templates.TemplateResponse(
            request=request,
            name="obligations/list.html",
            context={
                "title": "Departamento - Finanzas",
                "user": user,
                "summary": summary,
                "fx_sync_status": get_fx_sync_status(db),
                "csrf_token": token,
                "today_str": today_ar().isoformat(),
                "error": str(e),
                "active_nav": "obligations",
            },
            status_code=400,
        )


@obligation_router.post("/generate-schedule")
def generate_schedule(
    request: Request,
    year: int = Form(...),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Genera o completa el cronograma anual en USD (RN-27)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    # N6: generación explícita con el usuario real (la lectura nunca escribe).
    try:
        ob = ensure_obligation(db=db, user_id=user.id)
        generate_obligation_schedule(db=db, obligation_id=ob.id, year=year, user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url=f"/obligations?year={year}", status_code=303)


@obligation_router.post("/items")
def create_obligation_item_post(
    request: Request,
    due_period: str = Form(...),
    kind: str = Form("refuerzo"),
    usd_amount: str = Form(...),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Agrega manualmente un vencimiento (cuota o refuerzo) al cronograma en USD (RN-27)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    idem = None
    try:
        idem = begin_operation(
            db,
            payload=operation_payload(locals()),
            token=idempotency_token,
            route="/obligations/items",
            user_id=user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/obligations", status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        parts = due_period.strip().split("-")
        parsed_period = date(int(parts[0]), int(parts[1]), 1)
        dec_usd = parse_decimal(usd_amount)
        item = create_obligation_item(
            db=db,
            due_period=parsed_period,
            kind=kind,
            usd_amount=dec_usd,
            user_id=user.id,
        )
        if idem is not None:
            finish_operation(db, idem, result_entity="ObligationItem", result_id=item.id)
        return RedirectResponse(url=f"/obligations?year={parsed_period.year}", status_code=303)
    except (ValueError, ClosedPeriodError) as exc:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@obligation_router.get("/{item_id}/pay", response_class=HTMLResponse)
def get_pay_item_page(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Formulario para pagar una cuota o refuerzo (RN-28, RN-31)."""
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    item = db.get(ObligationItem, item_id)
    if not item:
        return RedirectResponse(url="/obligations", status_code=303)

    accounts_stmt = (
        select(Account)
        .where(
            Account.active == True,  # noqa: E712
            Account.is_virtual.is_(False),
            Account.type != "credit",
            Account.currency == "ARS",
        )
        .order_by(Account.name.asc())
    )
    accounts = db.execute(accounts_stmt).scalars().all()
    accounts_with_balance = [
        {"id": a.id, "name": a.name, "balance": get_account_balance(db, a.id)} for a in accounts
    ]

    latest_fx = get_latest_fx_rate(db, as_of_date=today_ar())
    estimated_ars = (
        (item.usd_amount * latest_fx.rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if latest_fx
        else Decimal("0.00")
    )
    initial_amount = str(estimated_ars).replace(".", ",") if estimated_ars > 0 else ""

    env_stmt = select(Envelope).where(Envelope.name == "Refuerzos depto")
    ref_env = db.execute(env_stmt).scalar_one_or_none()
    envelope_balance = get_envelope_balance(db, ref_env.id) if ref_env else Decimal("0.00")

    csrf_token = _ensure_csrf_token(request)
    from app.web.app import templates

    response = templates.TemplateResponse(
        request=request,
        name="obligations/pay.html",
        context={
            "title": "Pagar Vencimiento - Finanzas",
            "user": user,
            "item": item,
            "accounts": accounts_with_balance,
            "latest_fx": latest_fx,
            "estimated_ars": estimated_ars,
            "initial_amount": initial_amount,
            "envelope_balance": envelope_balance,
            "csrf_token": csrf_token,
            "today_str": today_ar().isoformat(),
            "active_nav": "obligations",
        },
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


@obligation_router.post("/{item_id}/pay")
def pay_obligation_action(
    item_id: int,
    request: Request,
    account_id: int = Form(...),
    amount_str: str = Form(..., alias="amount"),
    date_str: str = Form(..., alias="date"),
    pay_from_envelope: bool = Form(False),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Registra el pago de un vencimiento en pesos y genera el gasto contable (RN-28, RN-31)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    item = db.get(ObligationItem, item_id)
    if not item:
        return RedirectResponse(url="/obligations", status_code=303)

    try:
        idem = begin_operation(
            db,
            payload=operation_payload(locals()),
            token=idempotency_token,
            route="obligations.pay",
            user_id=user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url=f"/obligations?year={item.due_period.year}", status_code=303)
    except OperationInProgressError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    try:
        parsed_amount = parse_decimal(amount_str)
        payment_date = date.fromisoformat(date_str)
        period = date(payment_date.year, payment_date.month, 1)

        tx = pay_obligation_item(
            db=db,
            item_id=item.id,
            account_id=account_id,
            ars_amount=parsed_amount,
            payment_date=payment_date,
            period=period,
            user_id=user.id,
            pay_from_envelope=pay_from_envelope,
        )
        if idem is not None:
            finish_operation(db, idem, result_entity="Transaction", result_id=tx.id)
        return RedirectResponse(url=f"/obligations?year={item.due_period.year}", status_code=303)
    except (ValueError, ClosedPeriodError) as e:
        from app.web.app import templates

        accounts_stmt = (
            select(Account)
            .where(
                Account.active == True,  # noqa: E712
                Account.is_virtual.is_(False),
                Account.type != "credit",
                Account.currency == "ARS",
            )
            .order_by(Account.name.asc())
        )
        accounts = db.execute(accounts_stmt).scalars().all()
        accounts_with_balance = [
            {"id": a.id, "name": a.name, "balance": get_account_balance(db, a.id)} for a in accounts
        ]
        latest_fx = get_latest_fx_rate(db, as_of_date=today_ar())
        estimated_ars = (
            (item.usd_amount * latest_fx.rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if latest_fx
            else Decimal("0.00")
        )
        ref_env = db.execute(
            select(Envelope).where(Envelope.name == "Refuerzos depto")
        ).scalar_one_or_none()
        envelope_balance = get_envelope_balance(db, ref_env.id) if ref_env else Decimal("0.00")
        token = _ensure_csrf_token(request)

        if idem is not None and not isinstance(e, HTTPException):
            abort_operation(db, idem)
        return templates.TemplateResponse(
            request=request,
            name="obligations/pay.html",
            context={
                "title": "Pagar Vencimiento - Finanzas",
                "user": user,
                "item": item,
                "accounts": accounts_with_balance,
                "latest_fx": latest_fx,
                "estimated_ars": estimated_ars,
                "initial_amount": amount_str,
                "envelope_balance": envelope_balance,
                "csrf_token": token,
                "today_str": today_ar().isoformat(),
                "error": str(e),
                "active_nav": "obligations",
            },
            status_code=400,
        )
