"""Carga CSV con revisión explícita, reglas locales y consumos USD pendientes."""

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.db import get_db
from app.core.security import generate_csrf_token, validate_csrf
from app.domain.card_import import ColumnMapping
from app.models.auth import User
from app.models.cards import CardStatement
from app.models.ledger import Account
from app.services.card_import_service import confirm_statement, preview_statement
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

card_router = APIRouter(prefix="/cards", tags=["cards"])


def _user(request: Request, csrf: str | None = None) -> User:
    user = getattr(request.state, "user", None)
    if not isinstance(user, User):
        raise HTTPException(401, "Iniciá sesión.")
    if csrf is not None and not validate_csrf(request, csrf):
        raise HTTPException(403, "Token CSRF inválido.")
    return user


def _render(
    request: Request,
    db: Session,
    statement: CardStatement | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    from app.web.app import templates

    token = request.cookies.get("csrf_token") or generate_csrf_token()
    response = templates.TemplateResponse(
        request=request,
        name="cards/index.html",
        context={
            "current_user": _user(request),
            "user": _user(request),
            "csrf_token": token,
            "error": error,
            "accounts": db.scalars(
                select(Account).where(Account.active.is_(True), Account.type == "credit")
            ).all(),
            "users": db.scalars(select(User)).all(),
            "categories": list_categories(db, _user(request).id, show_all=True),
            "canonical_categories": canonical_categories(db),
            "statements": db.scalars(
                select(CardStatement).order_by(CardStatement.id.desc()).limit(30)
            ).all(),
            "statement": statement,
        },
        status_code=status_code,
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie(
            "csrf_token", token, samesite="lax", secure=settings.ENVIRONMENT == "production"
        )
    return response


@card_router.get("")
def cards_index(request: Request, db: Session = Depends(get_db)) -> Response:
    return _render(request, db)


@card_router.get("/{statement_id}")
def cards_preview(request: Request, statement_id: int, db: Session = Depends(get_db)) -> Response:
    _user(request)
    statement = db.get(CardStatement, statement_id)
    if statement is None:
        raise HTTPException(404, "Resumen inexistente.")
    return _render(request, db, statement)


async def _preview_csrf(request: Request) -> None:
    form = await request.form()
    _user(request, str(form.get("csrf_token", "")))


@card_router.post("/preview", dependencies=[Depends(_preview_csrf)])
def upload_preview(
    request: Request,
    file: UploadFile = File(...),
    account_id: int = Form(...),
    owner_user_id: int = Form(...),
    date_column: str = Form(...),
    merchant_column: str = Form(...),
    amount_column: str = Form(...),
    currency_column: str = Form(""),
    installments_column: str = Form(""),
    delimiter: str = Form(";"),
    encoding: str = Form("utf-8-sig"),
    date_format: str = Form("%d/%m/%Y"),
    decimal_separator: str = Form(","),
    expense_positive: str = Form("yes"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    user = _user(request, csrf_token)
    try:
        statement = preview_statement(
            db,
            account_id,
            owner_user_id,
            file.file.read(2_000_001),
            ColumnMapping(
                date_column, merchant_column, amount_column, currency_column, installments_column
            ),
            user.id,
            delimiter=delimiter,
            encoding=encoding,
            date_format=date_format,
            decimal_separator=decimal_separator,
            expense_positive=expense_positive == "yes",
        )
    except ValueError as exc:
        return _render(request, db, error=str(exc), status_code=400)
    finally:
        file.file.close()
    return RedirectResponse(f"/cards/{statement.id}", status_code=303)


@card_router.post("/{statement_id}/confirm")
async def confirm_preview(
    request: Request, statement_id: int, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    return await run_in_threadpool(_confirm_form, request, statement_id, dict(form), db)


def _confirm_form(
    request: Request, statement_id: int, form: dict[str, Any], db: Session
) -> Response:
    user = _user(request, str(form.get("csrf_token", "")))
    statement = db.get(CardStatement, statement_id)
    if statement is None:
        raise HTTPException(404, "Resumen inexistente.")
    raw_token = form.get("idempotency_token")
    token = str(raw_token) if raw_token is not None else None
    try:
        idem = begin_operation(
            db,
            token=token,
            route=f"cards.confirm:{statement_id}",
            user_id=user.id,
            payload=operation_payload({"statement_id": statement_id, **form}),
        )
    except DuplicateCompletedError:
        return RedirectResponse(f"/cards/{statement_id}", status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        return _render(
            request,
            db,
            statement=db.get(CardStatement, statement_id),
            error=str(exc),
            status_code=400,
        )

    decisions: list[dict[str, Any]] = []
    for index in range(len(statement.rows)):
        decisions.append(
            {
                "selected": form.get(f"selected_{index}") == "yes",
                "category_id": str(form.get(f"category_{index}", "")),
                "charged_to_user_id": str(form.get(f"charged_{index}", "")),
                "kind": str(form.get(f"kind_{index}", "expense")),
                "amount": str(form.get(f"amount_{index}", "")),
                "date": str(form.get(f"date_{index}", statement.rows[index]["date"])),
                "period": str(form.get(f"period_{index}", statement.rows[index]["date"])),
                "settled_ars": str(form.get(f"ars_{index}", "")),
                "learn": form.get(f"learn_{index}") == "yes",
            }
        )
    try:
        confirm_statement(db, statement_id, decisions, user.id)
        if idem is not None:
            finish_operation(db, idem, result_entity="CardStatement", result_id=statement_id)
    except (ValueError, KeyError, ClosedPeriodError) as exc:
        if idem is not None:
            abort_operation(db, idem)
        return _render(
            request,
            db,
            statement=db.get(CardStatement, statement_id),
            error=str(exc),
            status_code=400,
        )
    return RedirectResponse(f"/cards/{statement_id}", status_code=303)
