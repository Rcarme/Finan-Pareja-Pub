"""Rutas web para gestión de contrato de alquiler, cobro y ajustes por IPC (RN-32 a RN-34)."""

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
from app.models.ledger import Account
from app.services.idempotency_service import (
    DuplicateCompletedError,
    OperationInProgressError,
    abort_operation,
    begin_operation,
    finish_operation,
    operation_payload,
)
from app.services.ledger_service import ClosedPeriodError
from app.services.rent_service import (
    get_or_create_rent_contract,
    get_rent_adjustment_history,
    get_rent_status_summary,
    record_rent_adjustment,
    record_rent_collection,
    upsert_rent_contract,
)

rent_router = APIRouter(prefix="/rent", tags=["rent"])


def _ensure_csrf_token(request: Request) -> str:
    token = request.cookies.get("csrf_token")
    if not token:
        token = generate_csrf_token()
    return token


def _normalize_period(period_str: str | None) -> date:
    today = today_ar()
    return parse_period(period_str) if period_str else date(today.year, today.month, 1)


@rent_router.get("", response_class=HTMLResponse)
def rent_index(
    request: Request,
    period: str | None = None,
    success: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Pantalla principal de alquiler: estado, historial y cobro/ajuste (H-07: solo lectura)."""
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    csrf_token = _ensure_csrf_token(request)
    normalized_period = _normalize_period(period)

    summary = get_rent_status_summary(db, period=normalized_period)
    contract = summary.contract
    history = get_rent_adjustment_history(db, contract_id=contract.id) if contract.id > 0 else []

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

    from app.web.app import templates

    context: dict[str, Any] = {
        "request": request,
        "user": current_user,
        "csrf_token": csrf_token,
        "active_nav": "rent",
        "contract": contract,
        "summary": summary,
        "history": history,
        "accounts": accounts,
        "period": normalized_period,
        "period_str": normalized_period.strftime("%Y-%m-%d"),
        "period_display": normalized_period.strftime("%Y-%m"),
        "success": success,
        "error": error,
        "today_str": today_ar().strftime("%Y-%m-%d"),
    }

    response = templates.TemplateResponse(request=request, name="rent/index.html", context=context)
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


@rent_router.post("/contract")
def rent_contract_post(
    request: Request,
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    current_amount: str = Form(...),
    adjust_every_months: int = Form(3),
    last_adjust_date: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Crea o configura explícitamente el contrato de alquiler activo (RN-32, RN-37, H-07)."""
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
            route="/rent/contract",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/rent?success=contrato_actualizado", status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        dec_amount = parse_decimal(current_amount)
        parsed_last_adj = (
            date.fromisoformat(last_adjust_date.strip())
            if last_adjust_date and last_adjust_date.strip()
            else None
        )
        contract = upsert_rent_contract(
            db=db,
            current_amount=dec_amount,
            adjust_every_months=adjust_every_months,
            last_adjust_date=parsed_last_adj,
            user_id=current_user.id,
        )
        if idem is not None:
            finish_operation(db, idem, result_entity="RentContract", result_id=contract.id)
        return RedirectResponse(url="/rent?success=contrato_actualizado", status_code=303)
    except (ValueError, ClosedPeriodError) as exc:
        if idem is not None:
            abort_operation(db, idem)
        return RedirectResponse(url=f"/rent?error={quote(str(exc), safe='')}", status_code=303)


@rent_router.post("/collection")
def rent_collection_post(
    request: Request,
    csrf_token: str | None = Form(None),
    period: str = Form(...),
    payment_date: str = Form(...),
    account_id: int = Form(...),
    amount: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Registra el cobro de alquiler del mes (RN-32, RN-37)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    try:
        normalized_period = _normalize_period(period)
        p_date = date.fromisoformat(payment_date.strip())
        dec_amount = parse_decimal(amount)

        record_rent_collection(
            db=db,
            period=normalized_period,
            payment_date=p_date,
            account_id=account_id,
            user_id=current_user.id,
            amount=dec_amount,
        )
        return RedirectResponse(
            url=f"/rent?period={normalized_period.strftime('%Y-%m')}&success=cobro_registrado",
            status_code=303,
        )
    except ClosedPeriodError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        return RedirectResponse(
            url=f"/rent?error={quote(str(exc), safe='')}",
            status_code=303,
        )


@rent_router.post("/adjustment")
def rent_adjustment_post(
    request: Request,
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    adjust_date: str = Form(...),
    new_amount: str = Form(...),
    ipc_pct: str | None = Form(None),
    note: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Registra un nuevo ajuste de alquiler manual por IPC (RN-33, RN-37)."""
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
            route="/rent/adjustment",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/rent?success=ajuste_registrado", status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        contract = get_or_create_rent_contract(db, user_id=current_user.id)
        adj_date = date.fromisoformat(adjust_date.strip())
        dec_new_amount = parse_decimal(new_amount)
        dec_ipc = parse_decimal(ipc_pct) if ipc_pct and ipc_pct.strip() else None

        adj = record_rent_adjustment(
            db=db,
            contract_id=contract.id,
            adjust_date=adj_date,
            new_amount=dec_new_amount,
            ipc_pct=dec_ipc,
            note=note.strip() if note and note.strip() else None,
            user_id=current_user.id,
        )
        if idem is not None:
            finish_operation(db, idem, result_entity="RentAdjustment", result_id=adj.id)
        return RedirectResponse(
            url="/rent?success=ajuste_registrado",
            status_code=303,
        )
    except ClosedPeriodError as exc:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        if idem is not None:
            abort_operation(db, idem)
        return RedirectResponse(
            url=f"/rent?error={quote(str(exc), safe='')}",
            status_code=303,
        )
