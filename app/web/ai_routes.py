"""Rutas web del Asistente Contable con IA para la vista simple."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.formatters import today_ar
from app.models.auth import User
from app.models.ledger import Account, Category
from app.services.ai_assistant_service import (
    check_duplicates_for_draft_items,
    confirm_ai_draft_transactions,
    get_ai_context_status,
    parse_raw_text_with_gemini,
)
from app.services.category_service import list_categories
from app.services.idempotency_service import (
    DuplicateCompletedError,
    OperationInProgressError,
    abort_operation,
    begin_operation,
    finish_operation,
)
from app.web.simple_routes import _csrf_guard, _render, _user

logger = logging.getLogger("app.web.ai")

ai_router = APIRouter(prefix="/s/ai", tags=["ai"])


@ai_router.get("")
def ai_home(request: Request, db: Session = Depends(get_db)) -> Response:
    """Pantalla inicial del Asistente IA para pegar texto y revisar estado de sincronización."""
    _user(request)
    context_status = get_ai_context_status(db)
    categories = list_categories(db, 0, show_all=True, kind="expense")
    accounts = list(
        db.scalars(
            select(Account)
            .where(Account.active.is_(True), Account.is_virtual.is_(False))
            .order_by(Account.name)
        ).all()
    )
    users = list(db.scalars(select(User).where(User.active.is_(True))).all())

    return _render(
        request,
        db,
        "ai",
        nav="ai",
        context_status=context_status,
        raw_text="",
        draft_items=None,
        all_categories=categories,
        available_accounts=accounts,
        users=users,
        error=None,
    )


@ai_router.post("/parse")
def ai_parse(
    request: Request,
    raw_text: str = Form(""),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Analiza texto desestructurado con Gemini y genera borradores para revisión interactiva."""
    _csrf_guard(request, csrf_token)
    user = _user(request)
    context_status = get_ai_context_status(db)
    categories = list_categories(db, 0, show_all=True, kind="expense")
    accounts = list(
        db.scalars(
            select(Account)
            .where(Account.active.is_(True), Account.is_virtual.is_(False))
            .order_by(Account.name)
        ).all()
    )
    users = list(db.scalars(select(User).where(User.active.is_(True))).all())

    cats_by_name = {c.name.lower(): c for c in categories}
    accs_by_name = {a.name.lower(): a for a in accounts}
    users_by_name = {u.display_name.lower(): u for u in users}
    for u in users:
        users_by_name[u.username.lower()] = u

    text_to_parse = raw_text.strip()
    if not text_to_parse:
        return _render(
            request,
            db,
            "ai",
            nav="ai",
            context_status=context_status,
            raw_text=raw_text,
            draft_items=None,
            all_categories=categories,
            available_accounts=accounts,
            users=users,
            error="Pegá o escribí algún texto para que la IA lo analice.",
            status=400,
        )

    try:
        parsed_items = parse_raw_text_with_gemini(db, text_to_parse)
    except ValueError as exc:
        logger.info("Validación al analizar texto con IA: %s", exc)
        return _render(
            request,
            db,
            "ai",
            nav="ai",
            context_status=context_status,
            raw_text=raw_text,
            draft_items=None,
            all_categories=categories,
            available_accounts=accounts,
            users=users,
            error=str(exc),
            status=400,
        )
    except Exception as exc:
        logger.error("Error al analizar texto con IA: %s", exc)
        return _render(
            request,
            db,
            "ai",
            nav="ai",
            context_status=context_status,
            raw_text=raw_text,
            draft_items=None,
            all_categories=categories,
            available_accounts=accounts,
            users=users,
            error=(
                "No se pudo procesar el texto con el servicio de IA. "
                "Verificá tu clave de API o intentá nuevamente."
            ),
            status=400,
        )

    display_items: list[dict[str, Any]] = []
    items_with_dups = check_duplicates_for_draft_items(db, parsed_items, today_ar())

    for idx, (item, warning) in enumerate(items_with_dups):
        matched_cat = cats_by_name.get(item.category_name.lower())
        cat_id = matched_cat.id if matched_cat else (categories[0].id if categories else None)

        matched_acc = accs_by_name.get(item.account_name.lower())
        acc_id = matched_acc.id if matched_acc else (accounts[0].id if accounts else None)

        charged_to_id = None
        if item.charged_to_user_name:
            matched_user = users_by_name.get(item.charged_to_user_name.lower())
            if matched_user:
                charged_to_id = matched_user.id
        elif item.activity == "personal":
            charged_to_id = user.id

        display_items.append(
            {
                "index": idx,
                "description": item.description,
                "amount": str(item.amount),
                "category_id": cat_id,
                "account_id": acc_id,
                "activity": item.activity,
                "charged_to_user_id": charged_to_id,
                "tx_date": item.tx_date or today_ar().isoformat(),
                "notes": item.notes or "",
                "duplicate_warning": warning,
                "category_warning": item.category_warning,
                "account_warning": item.account_warning,
            }
        )

    return _render(
        request,
        db,
        "ai",
        nav="ai",
        context_status=context_status,
        raw_text=raw_text,
        draft_items=display_items,
        all_categories=categories,
        available_accounts=accounts,
        users=users,
        error=None,
    )


@ai_router.post("/confirm")
async def ai_confirm(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Confirma e ingresa atómicamente los movimientos revisados por el usuario."""
    form = await request.form()
    csrf_token = form.get("csrf_token")
    _csrf_guard(request, str(csrf_token) if csrf_token else None)
    user = _user(request)

    count_str = form.get("item_count", "0")
    try:
        count = int(str(count_str))
    except ValueError:
        count = 0

    confirmed_items: list[dict[str, Any]] = []
    for i in range(count):
        include_val = form.get(f"include_{i}")
        if include_val is None:
            continue
        desc = form.get(f"description_{i}")
        amount = form.get(f"amount_{i}")
        cat_id = form.get(f"category_id_{i}")
        acc_id = form.get(f"account_id_{i}")
        activity = form.get(f"activity_{i}")
        charged_to = form.get(f"charged_to_user_id_{i}")
        tx_date_str = form.get(f"tx_date_{i}")
        notes = form.get(f"notes_{i}")

        charged_to_val: int | None = None
        if charged_to and str(charged_to).strip():
            try:
                charged_to_val = int(str(charged_to).strip())
            except ValueError:
                charged_to_val = None

        confirmed_items.append(
            {
                "description": str(desc) if desc else "",
                "amount": str(amount) if amount else "0",
                "category_id": int(str(cat_id)) if cat_id else None,
                "account_id": int(str(acc_id)) if acc_id else None,
                "activity": str(activity) if activity else "household",
                "charged_to_user_id": charged_to_val,
                "tx_date": str(tx_date_str) if tx_date_str else today_ar().isoformat(),
                "notes": str(notes) if notes else "",
            }
        )

    if not confirmed_items:
        raise HTTPException(400, "No seleccionaste ningún movimiento para ingresar.")

    valid_cat_ids: set[int] = set(
        db.scalars(select(Category.id).where(Category.active.is_(True))).all()
    )
    valid_acc_ids: set[int] = set(
        db.scalars(select(Account.id).where(Account.active.is_(True))).all()
    )

    for it in confirmed_items:
        if it.get("category_id") not in valid_cat_ids:
            raise HTTPException(
                400, "Una o más categorías seleccionadas son inválidas o inexistentes."
            )
        if it.get("account_id") not in valid_acc_ids:
            raise HTTPException(
                400, "Una o más cuentas seleccionadas son inválidas o inexistentes."
            )

    idem_token = form.get("idempotency_token")
    operation = None
    try:
        operation = begin_operation(
            db,
            token=str(idem_token) if idem_token else None,
            route="simple.ai.confirm",
            user_id=user.id,
            payload={
                "items": [
                    {
                        "desc": it.get("description"),
                        "amount": str(it.get("amount")),
                        "category_id": it.get("category_id"),
                        "account_id": it.get("account_id"),
                        "activity": it.get("activity"),
                        "charged_to_user_id": it.get("charged_to_user_id"),
                        "tx_date": it.get("tx_date"),
                    }
                    for it in confirmed_items
                ]
            },
        )
    except DuplicateCompletedError as exc:
        return RedirectResponse(
            f"/s/list?saved={exc.record.result_id or 0}&ai_count={len(confirmed_items)}",
            status_code=303,
        )
    except OperationInProgressError as exc:
        raise HTTPException(409, str(exc)) from exc

    try:
        created_ids = confirm_ai_draft_transactions(db, confirmed_items, user.id)
        if operation is not None:
            last_id = created_ids[-1] if created_ids else 0
            finish_operation(db, operation, result_entity="Transaction", result_id=last_id)
    except Exception as exc:
        if operation is not None:
            abort_operation(db, operation)
        logger.warning("Error al confirmar movimientos de IA: %s", exc)
        raise HTTPException(400, f"Error al ingresar movimientos: {exc}") from exc

    last_id = created_ids[-1] if created_ids else 0
    return RedirectResponse(
        f"/s/list?saved={last_id}&ai_count={len(created_ids)}",
        status_code=303,
    )
