"""Apertura revisable; nunca aplica la foto inicial al desplegar la app."""

from collections.abc import Mapping
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.formatters import parse_decimal
from app.core.security import generate_csrf_token, validate_csrf
from app.models.auth import User
from app.models.ledger import Account, Transaction
from app.services.currency_service import convert_reserved_usd
from app.services.idempotency_service import (
    DuplicateCompletedError,
    OperationInProgressError,
    abort_operation,
    begin_operation,
    finish_operation,
    operation_payload,
)
from app.services.ledger_service import ClosedPeriodError
from app.services.ownership_service import (
    activate_ownership,
    adjust_ownership_opening,
    get_ownership_summary,
)

ownership_router = APIRouter(tags=["ownership"])


def _render_opening(
    request: Request,
    db: Session,
    opening_date: str,
    *,
    form_values: Mapping[str, str] | None = None,
    pending_selected: set[str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    from app.web.app import templates

    try:
        photo_date = date.fromisoformat(opening_date)
    except ValueError:
        # Only the query cutoff falls back; the entered value remains visible.
        photo_date = date(2026, 10, 9)
        error = error or "Fecha de apertura inválida."
        status_code = 400
    csrf = request.cookies.get("csrf_token") or generate_csrf_token()
    response = templates.TemplateResponse(
        request=request,
        name="settings/ownership.html",
        context={
            "title": "Apertura del dinero",
            "user": getattr(request.state, "user", None),
            "csrf_token": csrf,
            "ownership": get_ownership_summary(db),
            "opening_date": opening_date,
            "form_values": form_values or {},
            "pending_selected": pending_selected or set(),
            "error": error,
            "users_map": {
                person.id: person.display_name
                for person in db.scalars(select(User).where(User.active.is_(True)))
            },
            "accounts": list(
                db.scalars(
                    select(Account)
                    .where(Account.active.is_(True), Account.is_virtual.is_(False))
                    .order_by(Account.id)
                )
            ),
            "pending_charges": list(
                db.scalars(
                    select(Transaction)
                    .where(
                        Transaction.status == "active",
                        Transaction.kind == "expense",
                        Transaction.charged_to_user_id.is_not(None),
                        Transaction.activity == "household",
                        Transaction.date <= photo_date,
                        Transaction.period <= photo_date.replace(day=1),
                    )
                    .order_by(Transaction.date.desc(), Transaction.id.desc())
                )
            ),
            "users": list(db.scalars(select(User).where(User.active.is_(True)).order_by(User.id))),
        },
        status_code=status_code,
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf, httponly=False, samesite="lax")
    return response


@ownership_router.get("/settings/ownership", response_class=HTMLResponse)
def opening_form(
    request: Request, opening_date: str = "2026-10-09", db: Session = Depends(get_db)
) -> Response:
    return _render_opening(request, db, opening_date)


@ownership_router.post("/settings/ownership")
async def apply_opening(request: Request, db: Session = Depends(get_db)) -> Response:
    form = await request.form()
    token = str(form.get("csrf_token", ""))
    if not validate_csrf(request, token):
        raise HTTPException(403, "Token CSRF inválido.")
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    accounts = list(
        db.scalars(select(Account).where(Account.active.is_(True), Account.is_virtual.is_(False)))
    )
    users = list(db.scalars(select(User).where(User.active.is_(True))))
    try:
        if form.get("confirm") != "yes":
            raise ValueError("Confirmá los saldos y la separación antes de aplicar.")
        activate_ownership(
            db,
            opening_date=date.fromisoformat(str(form.get("opening_date", ""))),
            account_balances={
                account.id: parse_decimal(str(form.get(f"account_{account.id}", "")))
                for account in accounts
            },
            personal_balances={
                person.id: parse_decimal(str(form.get(f"personal_{person.id}", "")))
                for person in users
            },
            debt_usd_by_account={
                account.id: parse_decimal(str(form.get(f"debt_usd_{account.id}", "0")))
                for account in accounts
                if account.type == "credit" and account.currency == "ARS"
            },
            savings_ars=parse_decimal(str(form.get("savings_ars", ""))),
            savings_usd=parse_decimal(str(form.get("savings_usd", ""))),
            user_id=user.id,
            reason=str(form.get("reason", "")),
            estimated_accounts={
                account.id for account in accounts if form.get(f"estimated_{account.id}") == "yes"
            },
            pending_personal_transaction_ids={
                int(str(value)) for value in form.getlist("pending_charge")
            },
            token=str(form.get("idempotency_token", "")) or None,
        )
    except (ValueError, ClosedPeriodError) as error:
        db.rollback()
        return _render_opening(
            request,
            db,
            str(form.get("opening_date", "")),
            form_values={key: str(value) for key, value in form.items()},
            pending_selected={str(value) for value in form.getlist("pending_charge")},
            error=str(error),
            status_code=400,
        )
    return RedirectResponse("/settings/ownership", status_code=303)


@ownership_router.post("/settings/ownership/convert")
def convert_savings(
    request: Request,
    usd_account_id: int = Form(...),
    ars_account_id: int = Form(...),
    usd_amount: str = Form(...),
    ars_received: str = Form(...),
    period: str = Form(...),
    reason: str = Form(...),
    csrf_token: str | None = Form(None),
    idempotency_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    from app.core.formatters import parse_period

    if not validate_csrf(request, csrf_token):
        raise HTTPException(403, "Token CSRF inválido.")
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not idempotency_token.strip():
        raise HTTPException(400, "Falta el identificador de la conversión.")
    try:
        operation = begin_operation(
            db,
            payload=operation_payload(locals()),
            token=idempotency_token,
            route="ownership.convert",
            user_id=user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse("/settings/ownership", status_code=303)
    except OperationInProgressError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    try:
        entries = convert_reserved_usd(
            db,
            usd_account_id=usd_account_id,
            ars_account_id=ars_account_id,
            usd_amount=parse_decimal(usd_amount),
            ars_received=parse_decimal(ars_received),
            period=parse_period(period),
            reason=reason,
            user_id=user.id,
        )
        if operation is not None:
            finish_operation(db, operation, result_entity="OwnershipEntry", result_id=entries[0].id)
    except (ValueError, ClosedPeriodError) as error:
        if operation is not None:
            abort_operation(db, operation)
        raise HTTPException(400, str(error)) from error
    return RedirectResponse("/settings/ownership", status_code=303)


@ownership_router.post("/settings/ownership/adjust")
def adjust_opening(
    request: Request,
    period: str = Form(...),
    reason: str = Form(...),
    account_id: str = Form(""),
    personal_user_id: str = Form(""),
    physical_delta: str = Form("0"),
    personal_delta: str = Form("0"),
    savings_delta: str = Form("0"),
    currency: str = Form("ARS"),
    csrf_token: str | None = Form(None),
    idempotency_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    from app.core.formatters import parse_period

    if not validate_csrf(request, csrf_token):
        raise HTTPException(403, "Token CSRF inválido.")
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not isinstance(idempotency_token, str) or not idempotency_token.strip():
        raise HTTPException(400, "Falta el identificador de la corrección.")
    try:
        operation = begin_operation(
            db,
            payload=operation_payload(locals()),
            token=idempotency_token,
            route="ownership.adjust",
            user_id=user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse("/settings/ownership", status_code=303)
    except OperationInProgressError as error:
        raise HTTPException(409, str(error)) from error
    except (ValueError, ClosedPeriodError) as error:
        raise HTTPException(400, str(error)) from error
    try:
        entries = adjust_ownership_opening(
            db,
            period=parse_period(period),
            reason=reason,
            user_id=user.id,
            account_id=int(account_id) if account_id else None,
            personal_user_id=int(personal_user_id) if personal_user_id else None,
            physical_delta=parse_decimal(physical_delta),
            personal_delta=parse_decimal(personal_delta),
            savings_delta=parse_decimal(savings_delta),
            currency=currency,
        )
        if not entries:
            raise ValueError("Ingresá alguna diferencia para registrar una corrección.")
        if operation is not None:
            finish_operation(db, operation, result_entity="OwnershipEntry", result_id=entries[0].id)
    except (ValueError, ClosedPeriodError) as error:
        if operation is not None:
            abort_operation(db, operation)
        raise HTTPException(400, str(error)) from error
    return RedirectResponse("/settings/ownership", status_code=303)
