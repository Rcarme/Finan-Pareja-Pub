"""Rutas web para el libro contable: dashboard, transacciones, anulación y conciliación.

Reglas asociadas: RN-03 a RN-11, RN-18, RN-19, RN-40, RN-41.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.formatters import parse_decimal, parse_period, today_ar
from app.core.security import generate_csrf_token, validate_csrf
from app.models.auth import User
from app.models.envelopes import Envelope, EnvelopeMovement
from app.models.ledger import Account, Transaction
from app.models.obligations import ObligationItem
from app.services.category_service import alias_category_ids, canonical_categories, list_categories
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
    ConcurrencyError,
    create_transaction,
    get_account_balance,
    get_available_fund,
    get_opening_date,
    get_total_fund,
    has_future_card_installments,
    reconcile_account,
    update_transaction,
    void_transaction,
)
from app.web.query_types import OptionalIdentifier, parse_checkbox

ledger_router = APIRouter(prefix="", tags=["ledger"])


def _ensure_csrf_token(request: Request) -> str:
    """Retorna el token CSRF existente o genera uno nuevo."""
    token = request.cookies.get("csrf_token")
    if not token:
        token = generate_csrf_token()
    return token


@ledger_router.get("/", response_class=HTMLResponse)
@ledger_router.get("/full", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> Response:
    """Pantalla principal con Fondo, Disponible, cuentas y últimos movimientos (RN-08, RN-18)."""
    current_user = getattr(request.state, "user", None)
    if getattr(current_user, "ui_mode", "full") == "simple" and request.url.path != "/full":
        return RedirectResponse(url="/s", status_code=303)
    from app.services.ownership_service import get_ownership_summary

    ownership = get_ownership_summary(db)
    total_fund = get_total_fund(db)
    available_fund = get_available_fund(db)

    # Cuentas activas con sus saldos contables
    accounts_db = (
        db.execute(select(Account).where(Account.active.is_(True)).order_by(Account.id))
        .scalars()
        .all()
    )
    accounts_data = [
        {
            "id": a.id,
            "name": a.name,
            "type": a.type,
            "currency": a.currency,
            "is_virtual": a.is_virtual,
            "estimated": bool(ownership and a.id in ownership.estimated_accounts),
            "balance": ownership.account_balances.get(a.id, Decimal("0.00"))
            if ownership
            else get_account_balance(db, a.id),
        }
        for a in accounts_db
    ]

    # Últimos 10 movimientos
    recent_txs_db = (
        db.execute(
            select(Transaction).order_by(Transaction.date.desc(), Transaction.id.desc()).limit(10)
        )
        .scalars()
        .all()
    )

    # Mapeo de nombres para despliegue
    from app.services.envelope_service import get_envelope_balance

    savings_envelope = db.scalar(
        select(Envelope).where(Envelope.name == "Ahorro común", Envelope.active.is_(True))
    )
    current_savings_ars = (
        get_envelope_balance(db, savings_envelope.id)
        if savings_envelope is not None
        else (ownership.savings_ars if ownership else Decimal("0.00"))
    )
    users_map = {u.id: u.display_name for u in db.execute(select(User)).scalars().all()}
    categories_map = {key: category.name for key, category in canonical_categories(db).items()}
    accounts_map = {a.id: a.name for a in accounts_db}

    recent_txs = [
        {
            "id": t.id,
            "description": t.description,
            "amount": t.amount,
            "kind": t.kind,
            "date": t.date,
            "period": t.period,
            "status": t.status,
            "account_name": accounts_map.get(t.account_id, "Desconocida"),
            "category_name": categories_map.get(t.category_id or 0),
            "charged_to_name": users_map.get(t.charged_to_user_id or 0),
        }
        for t in recent_txs_db
    ]

    from app.models.obligations import ObligationItem
    from app.services.obligation_service import get_next_reinforcement_provision

    today = today_ar()
    current_period = date(today.year, today.month, 1)

    next_obligation = db.execute(
        select(ObligationItem)
        .where(
            ObligationItem.status == "pendiente",
            ObligationItem.due_period >= current_period,
        )
        .order_by(ObligationItem.due_period.asc(), ObligationItem.kind.asc())
        .limit(1)
    ).scalar_one_or_none()
    provision_suggestion = get_next_reinforcement_provision(db, current_period=current_period)

    from app.services.recurring_service import (
        get_pending_recurrings_for_period,
        has_unresolved_recurrings_for_period,
    )
    from app.services.rent_service import get_rent_status_summary

    rent_summary = get_rent_status_summary(db, period=current_period)
    unresolved_recurrings = has_unresolved_recurrings_for_period(db, period=current_period)
    pending_recurrings = get_pending_recurrings_for_period(
        db, period=current_period, status_filter="pending"
    )

    from app.services.backup_service import get_backup_health
    from app.web.app import templates

    backup_health = get_backup_health(db)
    csrf_token = _ensure_csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "title": "Tandem",
            "csrf_token": csrf_token,
            "user": current_user,
            "active_nav": "home",
            "total_fund": total_fund,
            "available_fund": available_fund,
            "accounts": accounts_data,
            "ownership": ownership,
            "current_savings_ars": current_savings_ars,
            "users_map": users_map,
            "accounts_map": accounts_map,
            "recent_transactions": recent_txs,
            "next_obligation": next_obligation,
            "provision_suggestion": provision_suggestion,
            "rent_summary": rent_summary,
            "unresolved_recurrings": unresolved_recurrings,
            "pending_recurrings_count": len(pending_recurrings),
            "backup_health": backup_health,
        },
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


@ledger_router.get("/transactions", response_class=HTMLResponse)
def list_transactions(
    request: Request,
    period: str | None = None,
    kind: str | None = None,
    account_id: OptionalIdentifier = None,
    category_id: OptionalIdentifier = None,
    status: str = "active",
    q: str | None = None,
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 50,
) -> Response:
    """Lista de movimientos con filtros por período, tipo, cuenta, categoría y búsqueda (M-06)."""
    current_user = getattr(request.state, "user", None)
    today = today_ar()
    selected_period_str = period or today.strftime("%Y-%m")

    filter_period = parse_period(selected_period_str)
    if not 1 <= page <= 1000000 or not 1 <= page_size <= 100:
        raise ValueError("La página debe ser positiva y su tamaño entre 1 y 100.")

    stmt = select(Transaction)

    if selected_period_str:
        stmt = stmt.where(Transaction.period == filter_period)

    if kind:
        stmt = stmt.where(Transaction.kind == kind)

    if account_id:
        stmt = stmt.where(
            or_(Transaction.account_id == account_id, Transaction.to_account_id == account_id)
        )

    if category_id:
        stmt = stmt.where(Transaction.category_id.in_(alias_category_ids(db, category_id)))

    if q and q.strip():
        q_escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Transaction.description.ilike(f"%{q_escaped}%", escape="\\"))

    if status == "active":
        stmt = stmt.where(Transaction.status == "active")
    elif status == "voided":
        stmt = stmt.where(Transaction.status == "voided")

    total_count = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    totals = db.execute(
        stmt.with_only_columns(Transaction.kind, func.sum(Transaction.amount))
        .where(Transaction.status == "active")
        .group_by(Transaction.kind)
    ).all()
    total_incomes = sum((amount for kind, amount in totals if kind == "income"), Decimal("0.00"))
    total_expenses = sum(
        (amount for kind, amount in totals if kind in ("expense", "distribution")), Decimal("0.00")
    )
    stmt = stmt.order_by(Transaction.date.desc(), Transaction.id.desc())
    txs_db = db.execute(stmt.offset((page - 1) * page_size).limit(page_size)).scalars().all()

    accounts_all = db.execute(select(Account).where(Account.active.is_(True))).scalars().all()
    accounts_map = {a.id: a.name for a in accounts_all}
    users_map = {u.id: u.display_name for u in db.execute(select(User)).scalars().all()}
    categories_map = {key: category.name for key, category in canonical_categories(db).items()}

    transactions_data = []

    for t in txs_db:
        transactions_data.append(
            {
                "id": t.id,
                "description": t.description,
                "amount": t.amount,
                "kind": t.kind,
                "date": t.date,
                "period": t.period,
                "status": t.status,
                "account_name": accounts_map.get(t.account_id, "Desconocida"),
                "to_account_name": accounts_map.get(t.to_account_id or 0),
                "category_name": categories_map.get(t.category_id or 0),
                "charged_to_name": users_map.get(t.charged_to_user_id or 0),
            }
        )

    from app.web.app import templates

    return templates.TemplateResponse(
        request=request,
        name="transactions/list.html",
        context={
            "title": "Movimientos - Finanzas",
            "user": current_user,
            "active_nav": "transactions",
            "transactions": transactions_data,
            "current_period": selected_period_str,
            "filter_kind": kind or "",
            "filter_account_id": account_id,
            "filter_status": status,
            "filter_q": q or "",
            "accounts": accounts_all,
            "total_incomes": total_incomes,
            "total_expenses": total_expenses,
            "total_count": total_count,
            "page": page,
            "previous_url": "/transactions?"
            + urlencode(dict(request.query_params) | {"page": page - 1, "page_size": page_size})
            if page > 1
            else None,
            "next_url": "/transactions?"
            + urlencode(dict(request.query_params) | {"page": page + 1, "page_size": page_size})
            if page * page_size < total_count
            else None,
        },
    )


@ledger_router.get("/transactions/new", response_class=HTMLResponse)
def new_transaction_form(
    request: Request,
    kind: str = "expense",
    activity: str = "household",
    db: Session = Depends(get_db),
) -> Response:
    """Formulario de carga de nuevo movimiento (RN-03 a RN-07, RN-14, RN-40)."""
    current_user = getattr(request.state, "user", None)
    csrf_token = _ensure_csrf_token(request)

    accounts_db = (
        db.execute(
            select(Account)
            .where(
                Account.active.is_(True), Account.is_virtual.is_(False), Account.currency == "ARS"
            )
            .order_by(Account.id)
        )
        .scalars()
        .all()
    )
    accounts_data = [
        {"id": a.id, "name": a.name, "balance": get_account_balance(db, a.id)} for a in accounts_db
    ]

    categories = list_categories(db, current_user.id if current_user else 0, show_all=True)

    users = db.execute(select(User).order_by(User.id)).scalars().all()
    envelopes = (
        db.execute(select(Envelope).where(Envelope.active.is_(True)).order_by(Envelope.id))
        .scalars()
        .all()
    )

    from app.services.account_preference_service import get_preferred_expense_account

    preferred = (
        get_preferred_expense_account(db, current_user.id)
        if current_user and kind == "expense"
        else None
    )
    today = today_ar()
    from app.web.app import templates

    response = templates.TemplateResponse(
        request=request,
        name="transactions/new.html",
        context={
            "title": "Nuevo Movimiento - Finanzas",
            "user": current_user,
            "csrf_token": csrf_token,
            "opening_date": get_opening_date(db),
            "default_kind": kind,
            "default_activity": activity
            if activity in {"household", "business", "personal"}
            else "household",
            "accounts": accounts_data,
            "categories": categories,
            "users": users,
            "envelopes": envelopes,
            "today": today.isoformat(),
            "current_period": today.strftime("%Y-%m"),
            "default_account_id": preferred.id
            if preferred
            else (accounts_data[0]["id"] if accounts_data else None),
        },
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


@ledger_router.post("/transactions")
def create_transaction_action(
    request: Request,
    kind: str = Form(...),
    amount: str = Form(...),
    account_id: int = Form(...),
    tx_date: str = Form(...),
    period: str | None = Form(None),
    description: str = Form(...),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    to_account_id: int | None = Form(None),
    category_id: int | None = Form(None),
    show_all_categories: str = Form(""),
    charged_to_user_id: int | None = Form(None),
    envelope_id: int | None = Form(None),
    recipient_user_id: int | None = Form(None),
    opening_omission: str = Form(""),
    confirm_overdraft: str | None = Form(None),
    activity: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Procesa el alta de un movimiento contable (RN-04 a RN-07, RN-11, RN-14, RN-16, RN-18)."""
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
            route="transactions.create",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/", status_code=303)
    except OperationInProgressError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    try:
        amount_decimal = parse_decimal(amount)
        parsed_date = date.fromisoformat(tx_date)
        parsed_period: date | None = None
        if period and period.strip():
            parsed_period = parse_period(period)
        if not description or not description.strip():
            raise ValueError("La descripción es obligatoria.")

        if kind == "distribution":
            effective_period = parsed_period or date(parsed_date.year, parsed_date.month, 1)
            acc_bal = get_account_balance(db, account_id)
            avail_bal = get_available_fund(db, effective_period)
            if (
                amount_decimal > acc_bal or (avail_bal - amount_decimal) < Decimal("0.00")
            ) and confirm_overdraft not in ("1", "on", "true"):
                raise ValueError(
                    "Advertencia: este retiro deja el Disponible negativo o supera el saldo "
                    "de la cuenta. Marcá la casilla 'Entiendo' para confirmar el retiro."
                )

        tx = create_transaction(
            db=db,
            kind=kind,
            amount=amount_decimal,
            account_id=account_id,
            tx_date=parsed_date,
            period=parsed_period,
            description=description.strip(),
            user_id=current_user.id,
            category_id=category_id if kind in ("income", "expense") else None,
            to_account_id=to_account_id if kind == "transfer" else None,
            charged_to_user_id=charged_to_user_id if kind in ("income", "expense") else None,
            use_category_default=False,
            activity=activity if isinstance(activity, str) else None,
            envelope_id=envelope_id if kind == "expense" else None,
            recipient_user_id=recipient_user_id if kind == "distribution" else None,
            idempotency_key=idempotency_token.strip() if idempotency_token else None,
            opening_omission=parse_checkbox(opening_omission),
        )
        if idem is not None:
            finish_operation(db, idem, result_entity="Transaction", result_id=tx.id)
        return RedirectResponse(url="/", status_code=303)
    except (ValueError, InvalidOperation, ClosedPeriodError) as e:
        if idem is not None:
            abort_operation(db, idem)
        # Re-render con error
        accounts_db = db.scalars(
            select(Account)
            .where(
                Account.active.is_(True), Account.is_virtual.is_(False), Account.currency == "ARS"
            )
            .order_by(Account.id)
        ).all()
        accounts_data = [
            {"id": a.id, "name": a.name, "balance": get_account_balance(db, a.id)}
            for a in accounts_db
        ]
        categories = list_categories(db, current_user.id, show_all=True, include_id=category_id)
        users = db.execute(select(User)).scalars().all()
        envelopes = db.execute(select(Envelope).where(Envelope.active.is_(True))).scalars().all()
        from app.web.app import templates

        err_msg = "El formato del monto es inválido." if isinstance(e, InvalidOperation) else str(e)
        return templates.TemplateResponse(
            request=request,
            name="transactions/new.html",
            context={
                "title": "Nuevo Movimiento - Finanzas",
                "user": current_user,
                "csrf_token": _ensure_csrf_token(request),
                "error": err_msg,
                "default_kind": kind,
                "default_activity": activity,
                "opening_date": get_opening_date(db),
                "opening_omission": opening_omission == "1",
                "is_retry": True,
                "form_category_id": category_id,
                "show_all_categories": bool(show_all_categories),
                "form_charged_to_user_id": charged_to_user_id,
                "form_envelope_id": envelope_id,
                "form_to_account_id": to_account_id,
                "form_confirm_overdraft": confirm_overdraft,
                "form_amount": amount,
                "form_recipient_user_id": recipient_user_id,
                "form_description": description,
                "accounts": accounts_data,
                "categories": categories,
                "users": users,
                "envelopes": envelopes,
                "today": tx_date,
                "current_period": period,
                "default_account_id": account_id,
            },
            status_code=400,
        )


@ledger_router.get("/transactions/{transaction_id}", response_class=HTMLResponse)
def transaction_detail(
    request: Request,
    transaction_id: int,
    db: Session = Depends(get_db),
) -> Response:
    """Detalle de un movimiento contable y formulario de anulación (RN-09)."""
    current_user = getattr(request.state, "user", None)
    tx = db.get(Transaction, transaction_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado.")

    account = db.get(Account, tx.account_id)
    to_account = db.get(Account, tx.to_account_id) if tx.to_account_id else None
    category = canonical_categories(db).get(tx.category_id or 0)
    charged_to_user = db.get(User, tx.charged_to_user_id) if tx.charged_to_user_id else None
    created_by_user = db.get(User, tx.created_by)
    voided_by_user = db.get(User, tx.voided_by) if tx.voided_by else None

    from app.web.app import templates

    csrf_token = _ensure_csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="transactions/detail.html",
        context={
            "title": f"Detalle #{tx.id} - Finanzas",
            "user": current_user,
            "tx": tx,
            "account": account,
            "to_account": to_account,
            "category": category,
            "charged_to_user": charged_to_user,
            "created_by_user": created_by_user,
            "voided_by_user": voided_by_user,
            "csrf_token": csrf_token,
        },
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


@ledger_router.get("/transactions/{transaction_id}/edit", response_class=HTMLResponse)
def edit_transaction_form(
    request: Request,
    transaction_id: int,
    db: Session = Depends(get_db),
) -> Response:
    """Formulario de edición de un movimiento contable (RN-10)."""
    current_user = getattr(request.state, "user", None)
    tx = db.get(Transaction, transaction_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado.")

    if tx.status == "voided":
        raise HTTPException(status_code=400, detail="No se puede editar un movimiento anulado.")

    has_envelope = (tx.envelope_id is not None) or bool(
        db.execute(
            select(EnvelopeMovement).where(
                EnvelopeMovement.transaction_id == tx.id,
                EnvelopeMovement.status == "active",
            )
        )
        .scalars()
        .first()
    )
    has_obligation = bool(
        db.execute(select(ObligationItem).where(ObligationItem.transaction_id == tx.id))
        .scalars()
        .first()
    )
    is_locked = has_envelope or has_obligation

    accounts_db = (
        db.execute(select(Account).where(Account.active.is_(True)).order_by(Account.id))
        .scalars()
        .all()
    )
    categories = list_categories(
        db, current_user.id if current_user else 0, show_all=True, include_id=tx.category_id
    )
    users = db.execute(select(User).order_by(User.id)).scalars().all()

    from app.web.app import templates

    csrf_token = _ensure_csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="transactions/edit.html",
        context={
            "title": f"Editar Movimiento #{tx.id} - Finanzas",
            "user": current_user,
            "csrf_token": csrf_token,
            "tx": tx,
            "is_locked": is_locked,
            "has_future_installments": has_future_card_installments(db, tx.id),
            "accounts": accounts_db,
            "categories": categories,
            "users": users,
        },
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


@ledger_router.post("/transactions/{transaction_id}/edit")
def edit_transaction_action(
    request: Request,
    transaction_id: int,
    expected_version: int = Form(...),
    description: str = Form(...),
    tx_date: str = Form(...),
    csrf_token: str | None = Form(None),
    amount: str | None = Form(None),
    period: str | None = Form(None),
    kind: str | None = Form(None),
    account_id: int | None = Form(None),
    to_account_id: int | None = Form(None),
    category_id: int | None = Form(None),
    show_all_categories: str = Form(""),
    charged_to_user_id: int | None = Form(None),
    future_installments_update: str | None = Form(None),
    activity: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Procesa la edición de un movimiento contable con concurrencia optimista (RN-10)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    tx = db.get(Transaction, transaction_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado.")

    has_envelope = (tx.envelope_id is not None) or bool(
        db.execute(
            select(EnvelopeMovement).where(
                EnvelopeMovement.transaction_id == tx.id,
                EnvelopeMovement.status == "active",
            )
        )
        .scalars()
        .first()
    )
    has_obligation = bool(
        db.execute(select(ObligationItem).where(ObligationItem.transaction_id == tx.id))
        .scalars()
        .first()
    )
    is_locked = has_envelope or has_obligation

    try:
        parsed_date = date.fromisoformat(tx_date)
        parsed_amount = parse_decimal(amount) if (amount and not is_locked) else tx.amount
        parsed_period = parse_period(period) if period and not is_locked else tx.period
        effective_account_id = account_id if (account_id and not is_locked) else tx.account_id
        effective_kind = kind if (kind and not is_locked) else tx.kind
        choice = (
            future_installments_update.strip() or None
            if isinstance(future_installments_update, str)
            else None
        )
        if choice not in {None, "yes", "no"}:
            raise ValueError("Elegí si querés actualizar las estimaciones restantes.")
        if (
            parsed_amount != tx.amount
            and has_future_card_installments(db, tx.id)
            and choice is None
        ):
            raise ValueError("Elegí si la corrección también cambia las cuotas restantes.")

        update_transaction(
            db=db,
            transaction_id=tx.id,
            kind=effective_kind,
            amount=parsed_amount,
            account_id=effective_account_id,
            tx_date=parsed_date,
            period=parsed_period,
            description=description,
            user_id=current_user.id,
            expected_version=expected_version,
            category_id=category_id if effective_kind != "transfer" else None,
            to_account_id=to_account_id if effective_kind == "transfer" else None,
            recipient_user_id=tx.recipient_user_id,
            envelope_id=tx.envelope_id,
            charged_to_user_id=charged_to_user_id if effective_kind != "transfer" else None,
            usd_amount=tx.usd_amount,
            fx_rate_effective=tx.fx_rate_effective,
            future_installments_update=choice == "yes",
            activity=activity if isinstance(activity, str) else None,
        )
        return RedirectResponse(url=f"/transactions/{tx.id}", status_code=303)
    except ConcurrencyError as e:
        accounts_db = (
            db.execute(select(Account).where(Account.active.is_(True)).order_by(Account.id))
            .scalars()
            .all()
        )
        categories = list_categories(
            db, current_user.id if current_user else 0, show_all=True, include_id=tx.category_id
        )
        users = db.execute(select(User).order_by(User.id)).scalars().all()
        from app.web.app import templates

        return templates.TemplateResponse(
            request=request,
            name="transactions/edit.html",
            context={
                "title": f"Editar Movimiento #{tx.id} - Finanzas",
                "user": current_user,
                "csrf_token": _ensure_csrf_token(request),
                "tx": tx,
                "is_locked": is_locked,
                "has_future_installments": has_future_card_installments(db, tx.id),
                "accounts": accounts_db,
                "categories": categories,
                "users": users,
                "is_conflict": True,
                "error": f"Conflicto de concurrencia: {e}",
            },
            status_code=409,
        )
    except (ValueError, InvalidOperation, ClosedPeriodError) as e:
        accounts_db = (
            db.execute(select(Account).where(Account.active.is_(True)).order_by(Account.id))
            .scalars()
            .all()
        )
        categories = list_categories(
            db, current_user.id, show_all=True, include_id=category_id or tx.category_id
        )
        users = db.execute(select(User).order_by(User.id)).scalars().all()
        from app.web.app import templates

        err_msg = "El formato del monto es inválido." if isinstance(e, InvalidOperation) else str(e)
        return templates.TemplateResponse(
            request=request,
            name="transactions/edit.html",
            context={
                "title": f"Editar Movimiento #{tx.id} - Finanzas",
                "user": current_user,
                "csrf_token": _ensure_csrf_token(request),
                "tx": tx,
                "is_locked": is_locked,
                "has_future_installments": has_future_card_installments(db, tx.id),
                "accounts": accounts_db,
                "categories": categories,
                "users": users,
                "form_amount": amount,
                "form_activity": activity,
                "form_description": description,
                "form_date": tx_date,
                "form_period": period,
                "form_account_id": account_id,
                "form_to_account_id": to_account_id,
                "form_category_id": category_id,
                "show_all_categories": bool(show_all_categories),
                "form_charged_to_user_id": charged_to_user_id,
                "form_future_installments_update": future_installments_update,
                "form_expected_version": expected_version,
                "error": err_msg,
            },
            status_code=400,
        )


@ledger_router.post("/transactions/{transaction_id}/void")
def void_transaction_action(
    request: Request,
    transaction_id: int,
    reason: str = Form(""),
    expected_version: int = Form(...),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Anula un movimiento contable con motivo y control de concurrencia (RN-09, RN-10)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    try:
        void_transaction(
            db=db,
            transaction_id=transaction_id,
            reason=reason,
            user_id=current_user.id,
            expected_version=expected_version,
        )
        return RedirectResponse(url=f"/transactions/{transaction_id}", status_code=303)
    except ConcurrencyError as e:
        raise HTTPException(
            status_code=409,
            detail=f"{e} Recargá la página para ver la versión actualizada.",
        ) from e
    except (ValueError, ClosedPeriodError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _render_reconciliation(
    request: Request,
    db: Session,
    *,
    period: str | None = None,
    error: str | None = None,
    success_msg: str | None = None,
    status_code: int = 200,
) -> Response:
    from app.web.app import templates

    csrf_token = _ensure_csrf_token(request)
    accounts_db = (
        db.execute(
            select(Account)
            .where(Account.active.is_(True), Account.is_virtual.is_(False))
            .order_by(Account.id)
        )
        .scalars()
        .all()
    )
    response = templates.TemplateResponse(
        request=request,
        name="reconciliation.html",
        context={
            "title": "Conciliación de Cuentas - Finanzas",
            "user": getattr(request.state, "user", None),
            "active_nav": "reconciliation",
            "csrf_token": csrf_token,
            "accounts": [
                {
                    "id": a.id,
                    "name": a.name,
                    "type": a.type,
                    "currency": a.currency,
                    "balance": get_account_balance(db, a.id),
                }
                for a in accounts_db
            ],
            "current_period": period or today_ar().strftime("%Y-%m"),
            "error": error,
            "success_msg": success_msg,
        },
        status_code=status_code,
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


@ledger_router.get("/reconciliation", response_class=HTMLResponse)
def reconciliation_view(request: Request, db: Session = Depends(get_db)) -> Response:
    """Pantalla de conciliación de cuentas reales (RN-19)."""
    return _render_reconciliation(request, db)


@ledger_router.post("/reconciliation")
def reconcile_account_action(
    request: Request,
    account_id: int = Form(...),
    real_balance: str = Form(...),
    period: str = Form(...),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Conciliación serializada por servicio e idempotente por formulario (RN-19)."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    operation = None
    try:
        if not idempotency_token or not idempotency_token.strip():
            raise ValueError("Falta el identificador de conciliación. Recargá el formulario.")
        operation = begin_operation(
            db,
            payload=operation_payload(locals()),
            token=idempotency_token,
            route="accounts.reconcile",
            user_id=current_user.id,
        )
        adj_tx = reconcile_account(
            db=db,
            account_id=account_id,
            real_balance=parse_decimal(real_balance),
            period=parse_period(period),
            user_id=current_user.id,
        )
        if operation is not None:
            finish_operation(
                db,
                operation,
                result_entity="Transaction" if adj_tx else "Account",
                result_id=adj_tx.id if adj_tx else account_id,
            )
        success_msg = (
            "Cuenta conciliada: el saldo coincidía exactamente con el sistema (sin diferencias)."
            if adj_tx is None
            else (
                f"Cuenta conciliada exitosamente. Se generó el movimiento #{adj_tx.id} "
                "de ajuste de conciliación."
            )
        )
        return _render_reconciliation(request, db, period=period, success_msg=success_msg)
    except DuplicateCompletedError:
        return RedirectResponse("/reconciliation", status_code=303)
    except OperationInProgressError as error:
        return _render_reconciliation(request, db, period=period, error=str(error), status_code=409)
    except (ValueError, InvalidOperation, ClosedPeriodError) as error:
        if operation is not None:
            abort_operation(db, operation)
        message = (
            "El formato del saldo es inválido."
            if isinstance(error, InvalidOperation)
            else str(error)
        )
        return _render_reconciliation(request, db, period=period, error=message, status_code=400)
