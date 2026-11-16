"""Rutas web para el cierre de mes y reparto 30/20/50 (RN-20 a RN-26, RN-44).

Asistente de 4 pasos (revisar, conciliar, provisión/sobres, confirmar con vista previa),
ejecución atómica con CSRF e idempotencia, detalle de período cerrado, historial y
reapertura formal.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from app.core.db import get_db
from app.core.formatters import format_decimal_ar, parse_decimal, today_ar
from app.core.security import generate_csrf_token, validate_csrf
from app.models.auth import User
from app.models.close import Close, Period
from app.models.envelopes import Envelope
from app.models.ledger import Account, Transaction
from app.services.close_service import (
    execute_close,
    get_close_split_config,
    preview_close,
    reopen_close,
)
from app.services.envelope_service import get_envelope_balance
from app.services.idempotency_service import OperationInProgressError
from app.services.ledger_service import (
    ClosedPeriodError,
    ConcurrencyError,
    get_account_balance,
    get_available_fund,
)
from app.services.obligation_service import get_next_reinforcement_provision

close_router = APIRouter(prefix="/close", tags=["close"])


def _ensure_csrf_token(request: Request) -> str:
    """Retorna el token CSRF existente o genera uno nuevo."""
    token = request.cookies.get("csrf_token")
    if not token:
        token = generate_csrf_token()
    return token


def _normalize_period(period_str: str | None) -> date:
    """Parsea y normaliza una cadena YYYY-MM o YYYY-MM-DD al primer día del mes."""
    if not period_str:
        today = today_ar()
        return date(today.year, today.month, 1)
    try:
        parts = period_str.strip().split("-")
        if len(parts) in (2, 3):
            if len(parts) == 3:
                date.fromisoformat(period_str.strip())
            return date(int(parts[0]), int(parts[1]), 1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Período inválido. Usá AAAA-MM.") from exc
    raise HTTPException(status_code=400, detail="Período inválido. Usá AAAA-MM.")


@close_router.get("", response_class=HTMLResponse)
def close_index(request: Request, db: Session = Depends(get_db)) -> Response:
    """Página de entrada de cierre: redirige al asistente para el período actual/anterior."""
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    today = today_ar()
    period = date(today.year, today.month, 1)
    return RedirectResponse(url=f"/close/wizard?step=1&period={period}", status_code=302)


@close_router.get("/wizard", response_class=HTMLResponse)
def close_wizard(
    request: Request,
    step: int = 1,
    period: str | None = None,
    reinforcement_reservation: str | None = None,
    r: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Asistente de cierre de mes de 4 pasos (RN-20 a RN-26)."""
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    if step not in (1, 2, 3, 4):
        raise HTTPException(status_code=400, detail="Paso de cierre inválido.")
    csrf_token = _ensure_csrf_token(request)
    normalized_period = _normalize_period(period)

    # Verificar si el período ya está cerrado
    p_row = db.get(Period, normalized_period)
    is_closed = p_row is not None and p_row.status == "closed"

    # Contexto común
    context: dict[str, object] = {
        "request": request,
        "user": current_user,
        "csrf_token": csrf_token,
        "active_nav": "close",
        "period": normalized_period,
        "period_str": normalized_period.strftime("%Y-%m-%d"),
        "period_display": normalized_period.strftime("%Y-%m"),
        "is_closed": is_closed,
        "step": step,
        "split_config": get_close_split_config(db),
    }

    from app.web.app import templates

    # PASO 1: Revisión de movimientos e ingresos vs gastos
    if step == 1:
        # Totales del período
        txs = (
            db.execute(
                select(Transaction).where(
                    Transaction.period == normalized_period,
                    Transaction.status == "active",
                )
            )
            .scalars()
            .all()
        )
        total_income = sum((t.amount for t in txs if t.kind == "income"), Decimal("0.00"))
        total_expenses = sum(
            (t.amount for t in txs if t.kind in ("expense", "distribution")), Decimal("0.00")
        )
        available = get_available_fund(db, normalized_period)

        # Advertencia si el período aún no concluyó (RN-26)
        today = today_ar()
        next_month = date(
            normalized_period.year + (1 if normalized_period.month == 12 else 0),
            1 if normalized_period.month == 12 else normalized_period.month + 1,
            1,
        )
        is_period_current_or_future = today < next_month

        context.update(
            {
                "total_income": total_income,
                "total_expenses": total_expenses,
                "available": available,
                "is_deficit": available <= Decimal("0.00"),
                "is_period_current_or_future": is_period_current_or_future,
            }
        )
        template_name = "close/wizard_step1.html"

    # PASO 2: Conciliación de cuentas
    elif step == 2:
        accounts = db.execute(select(Account).where(Account.active == True)).scalars().all()  # noqa: E712
        accounts_data = [
            {
                "id": a.id,
                "name": a.name,
                "type": a.type,
                "balance": get_account_balance(db, a.id),
            }
            for a in accounts
        ]
        context.update({"accounts": accounts_data})
        template_name = "close/wizard_step2.html"

    # PASO 3: Provisión de refuerzos y destinos de fondos
    elif step == 3:
        suggested_r = get_next_reinforcement_provision(db, normalized_period)
        suggested_amount = suggested_r.suggested_ars if suggested_r is not None else Decimal("0.00")
        raw_r = reinforcement_reservation if reinforcement_reservation is not None else r
        default_r_str = (
            raw_r.strip() if (raw_r and raw_r.strip()) else format_decimal_ar(suggested_amount)
        )

        users = db.execute(select(User).order_by(User.id)).scalars().all()
        accounts = db.execute(select(Account).where(Account.active == True)).scalars().all()  # noqa: E712
        all_envelopes = (
            db.execute(select(Envelope).where(Envelope.active == True)).scalars().all()  # noqa: E712
        )
        savings_envelopes = [
            {"envelope": env, "balance": get_envelope_balance(db, env.id, normalized_period)}
            for env in all_envelopes
            if not env.is_system or env.name == "Ahorro común"
        ]

        context.update(
            {
                "suggested_reinforcement": suggested_amount,
                "users": users,
                "accounts": accounts,
                "savings_envelopes": savings_envelopes,
                "reinforcement_reservation": default_r_str,
                "selected_destinations": request.query_params,
            }
        )
        template_name = "close/wizard_step3.html"

    # PASO 4: Vista previa y confirmación
    else:
        raw_r = (
            reinforcement_reservation
            if (reinforcement_reservation and reinforcement_reservation.strip())
            else (r if (r and r.strip()) else None)
        )
        r_val: Decimal | None = None
        if raw_r is not None:
            try:
                r_val = parse_decimal(raw_r)
            except (ValueError, InvalidOperation) as exc:
                raise HTTPException(
                    status_code=400, detail="Reserva inválida. Ingresá un monto en pesos."
                ) from exc

        try:
            preview = preview_close(db, normalized_period, reinforcement_reservation=r_val)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        users = db.execute(select(User).order_by(User.id)).scalars().all()
        accounts = db.execute(select(Account).where(Account.active == True)).scalars().all()  # noqa: E712

        savings_destinations: dict[int, Decimal] = {}
        selected_envelopes: dict[int, Envelope] = {}
        for key, value in request.query_params.items():
            if key.startswith("savings_dest_") and value.strip():
                try:
                    envelope_id = int(key.removeprefix("savings_dest_"))
                    amount = parse_decimal(value)
                    envelope = db.get(Envelope, envelope_id)
                    if amount < 0 or envelope is None or not envelope.active:
                        raise ValueError("Destino de ahorro inválido o monto negativo.")
                    if envelope.is_system and envelope.name != "Ahorro común":
                        raise ValueError("Ese sobre no admite destinos de ahorro.")
                except (ValueError, InvalidOperation) as exc:
                    raise HTTPException(
                        status_code=400, detail="Destino de ahorro inválido."
                    ) from exc
                savings_destinations[envelope_id] = amount
                selected_envelopes[envelope_id] = envelope
        back_params = dict(request.query_params)
        back_params.update({"step": "3", "period": normalized_period.isoformat()})

        context.update(
            {
                "preview": preview,
                "result": preview.result,
                "reinforcement_reservation": preview.result.reinforcement_reservation,
                "users": users,
                "accounts": accounts,
                "savings_destinations": savings_destinations,
                "selected_envelopes": selected_envelopes,
                "back_url": "/close/wizard?" + urlencode(back_params),
            }
        )
        template_name = "close/wizard_step4.html"

    response = templates.TemplateResponse(
        request=request,
        name=template_name,
        context=context,
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


def _execute_close_form(
    db: Session,
    form_data: FormData,
    period: date,
    reinforcement_reservation: Decimal,
    user_id: int,
    idempotency_token: str | None,
) -> None:
    """Procesa el formulario con el acceso síncrono a BD fuera del event loop."""
    users = db.execute(select(User)).scalars().all()
    dest_accounts: dict[int, int] = {}
    for user in users:
        account_value = form_data.get(f"dest_account_{user.id}")
        if account_value:
            dest_accounts[user.id] = int(str(account_value))

    savings_destinations: dict[int, Decimal] = {}
    for key, value in form_data.items():
        if key.startswith("savings_dest_") and value:
            envelope_id = int(key.removeprefix("savings_dest_"))
            savings_destinations[envelope_id] = parse_decimal(str(value))

    execute_close(
        db=db,
        period=period,
        reinforcement_reservation=reinforcement_reservation,
        destination_accounts=dest_accounts,
        user_id=user_id,
        savings_destinations=savings_destinations or None,
        idempotency_key=idempotency_token.strip() if idempotency_token else None,
    )


@close_router.post("/execute", response_class=HTMLResponse)
async def execute_close_route(
    request: Request,
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    period: str = Form(...),
    reinforcement_reservation: str = Form("0,00"),
    db: Session = Depends(get_db),
) -> Response:
    """Ejecuta el cierre atómico en una única transacción de BD (RN-24)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    try:
        normalized_period = _normalize_period(period)
        r_dec = parse_decimal(reinforcement_reservation)

        form_data = await request.form()
        await run_in_threadpool(
            _execute_close_form,
            db,
            form_data,
            normalized_period,
            r_dec,
            current_user.id,
            idempotency_token,
        )

        target_url = f"/close/{normalized_period.strftime('%Y-%m-%d')}"
        return RedirectResponse(url=target_url, status_code=303)
    except (OperationInProgressError, ConcurrencyError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except (ClosedPeriodError, ValueError, InvalidOperation) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@close_router.get("/history", response_class=HTMLResponse)
def close_history(request: Request, db: Session = Depends(get_db)) -> Response:
    """Listado histórico de cierres de mes (RN-24, RN-25)."""
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    csrf_token = _ensure_csrf_token(request)
    periods = db.execute(select(Period).order_by(Period.period.desc())).scalars().all()

    from app.web.app import templates

    context = {
        "request": request,
        "user": current_user,
        "csrf_token": csrf_token,
        "active_nav": "close",
        "periods": periods,
    }
    response = templates.TemplateResponse(
        request=request,
        name="close/history.html",
        context=context,
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


@close_router.get("/{period_str}", response_class=HTMLResponse)
def close_detail(
    request: Request,
    period_str: str,
    db: Session = Depends(get_db),
) -> Response:
    """Detalle de un período cerrado con su snapshot inmutable y reapertura (RN-24, RN-25)."""
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    csrf_token = _ensure_csrf_token(request)
    normalized_period = _normalize_period(period_str)

    period_row = db.get(Period, normalized_period)
    if not period_row:
        raise HTTPException(status_code=404, detail="Período no encontrado.")

    close_row = (
        db.execute(select(Close).where(Close.period == normalized_period).order_by(Close.id.desc()))
        .scalars()
        .first()
    )

    # Verificar si es el último período cerrado (para habilitar botón de reapertura)
    subsequent_closed = db.execute(
        select(Period).where(Period.status == "closed", Period.period > normalized_period)
    ).first()
    can_reopen = (period_row.status == "closed") and (subsequent_closed is None)

    users_map = {u.id: u.display_name for u in db.execute(select(User)).scalars().all()}

    from app.web.app import templates

    context = {
        "request": request,
        "user": current_user,
        "csrf_token": csrf_token,
        "active_nav": "close",
        "period": period_row,
        "close": close_row,
        "snapshot": period_row.close_snapshot,
        "can_reopen": can_reopen,
        "users_map": users_map,
    }
    response = templates.TemplateResponse(
        request=request,
        name="close/detail.html",
        context=context,
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


@close_router.post("/{period_str}/reopen", response_class=HTMLResponse)
def reopen_close_route(
    request: Request,
    period_str: str,
    csrf_token: str | None = Form(None),
    reason: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Reapertura formal de un período cerrado con motivo obligatorio (RN-25)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    if not reason or not reason.strip():
        raise HTTPException(
            status_code=400, detail="El motivo de reapertura es obligatorio (RN-25)."
        )

    normalized_period = _normalize_period(period_str)

    try:
        reopen_close(
            db=db,
            period=normalized_period,
            reason=reason.strip(),
            user_id=current_user.id,
        )
        return RedirectResponse(
            url=f"/close/wizard?step=1&period={normalized_period.strftime('%Y-%m-%d')}&reopened=1",
            status_code=303,
        )
    except ClosedPeriodError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
