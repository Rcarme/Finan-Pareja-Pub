"""Vista cotidiana simplificada, sobre los mismos servicios y permisos contables."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.formatters import parse_decimal, parse_period, today_ar
from app.core.security import generate_csrf_token, secure_cookies, validate_csrf
from app.models.auth import User
from app.models.ledger import Account, Category, Transaction
from app.services.business_service import get_business_summary, get_business_transactions
from app.services.category_service import canonical_categories, list_categories
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
    get_opening_date,
    has_future_card_installments,
    resolve_transaction_period,
    update_transaction,
    void_transaction,
)
from app.services.retirement_service import get_retirement_report
from app.services.settlement_service import get_monthly_settlement_report
from app.services.simple_view_service import (
    get_simple_cards_view,
    get_simple_close_view,
    get_simple_dashboard,
    get_simple_envelopes_view,
    get_simple_transactions,
    get_top_categories,
    set_nav_modules,
    set_ui_mode,
    simple_create_transaction,
    undo_quick_transaction,
    update_category_appearance,
)
from app.web.query_types import OptionalIdentifier, parse_checkbox

simple_router = APIRouter(prefix="/s", tags=["simple"])


def _user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if not isinstance(user, User):
        raise HTTPException(401, "Iniciá sesión.")
    return user


def _csrf_guard(request: Request, csrf_token: str | None = Form(None)) -> None:
    _user(request)
    if not validate_csrf(request, csrf_token):
        raise HTTPException(403, "Token CSRF inválido.")


def _categories(
    db: Session, kind: str | None = None, include_id: int | None = None
) -> list[Category]:
    return list_categories(db, 0, show_all=True, include_id=include_id, kind=kind)


def _render(
    request: Request, db: Session, template: str, *, status: int = 200, **values: Any
) -> Response:
    from app.web.app import templates

    token = request.cookies.get("csrf_token") or generate_csrf_token()
    response = templates.TemplateResponse(
        request=request,
        name=f"simple/{template}.html",
        context={
            "user": _user(request),
            "csrf_token": token,
            "today": today_ar().isoformat(),
            "category_label": lambda name: name,
            "accounts": {acc.id: acc for acc in db.scalars(select(Account)).all()},
            **values,
        },
        status_code=status,
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", token, samesite="lax", secure=secure_cookies())
    return response


def _transaction(db: Session, transaction_id: int) -> Transaction:
    transaction = db.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(404, "Movimiento no encontrado.")
    if transaction.kind not in ("income", "expense"):
        raise HTTPException(400, "Este movimiento se revisa en la vista completa.")
    return transaction


def _operate(
    request: Request,
    db: Session,
    token: str,
    route: str,
    payload: dict[str, Any],
    callback: Callable[[], Transaction],
    success: Callable[[int], str],
    failure: Callable[[str, int], Response],
) -> Response:
    if not token:
        return failure("Recargá el formulario antes de guardar.", 400)
    operation = None
    try:
        operation = begin_operation(
            db,
            token=token,
            route=route,
            user_id=_user(request).id,
            payload=operation_payload(payload),
        )
        transaction = callback()
        if operation is not None:
            finish_operation(db, operation, result_entity="Transaction", result_id=transaction.id)
        return RedirectResponse(success(transaction.id), status_code=303)
    except DuplicateCompletedError as exc:
        return RedirectResponse(success(exc.record.result_id or 0), status_code=303)
    except OperationInProgressError as exc:
        return failure(str(exc), 409)
    except (ValueError, InvalidOperation, ClosedPeriodError, ConcurrencyError) as exc:
        if operation is not None:
            abort_operation(db, operation)
        return failure(str(exc), 409 if isinstance(exc, ConcurrencyError) else 400)
    except Exception:
        if operation is not None:
            abort_operation(db, operation)
        raise


def _parse_as_of(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return parse_period(raw)
    except ValueError:
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise HTTPException(400, "Fecha de referencia inválida.") from exc


@simple_router.get("")
def simple_home(
    request: Request,
    window: str = "month",
    as_of: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    user = _user(request)
    as_of_date = _parse_as_of(as_of)

    try:
        summary = get_simple_dashboard(db, user, window=window, as_of=as_of_date)
        latest = get_simple_transactions(
            db, span=window, as_of=as_of_date, limit=5, exclude_personal=False
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    prev_month_iso: str | None = None
    next_month_iso: str | None = None
    period_label: str | None = None

    curr_m = date(summary.start_date.year, summary.start_date.month, 1)
    if window == "month":
        prev_m = (curr_m - timedelta(days=1)).replace(day=1)
        prev_month_iso = prev_m.strftime("%Y-%m")

        today = today_ar()
        this_month = today.replace(day=1)
        next_m = (curr_m + timedelta(days=32)).replace(day=1)
        if next_m <= this_month:
            next_month_iso = next_m.strftime("%Y-%m")

        month_names = [
            "",
            "Enero",
            "Febrero",
            "Marzo",
            "Abril",
            "Mayo",
            "Junio",
            "Julio",
            "Agosto",
            "Septiembre",
            "Octubre",
            "Noviembre",
            "Diciembre",
        ]
        period_label = f"{month_names[curr_m.month]} {curr_m.year}"

    business_summary = get_business_summary(db, curr_m) if user.has_nav("business") else None

    return _render(
        request,
        db,
        "home",
        summary=summary,
        business_summary=business_summary,
        retirement=get_retirement_report(db, curr_m),
        settlement=get_monthly_settlement_report(db, curr_m),
        window=window,
        as_of=as_of,
        prev_month_iso=prev_month_iso,
        next_month_iso=next_month_iso,
        period_label=period_label,
        transactions=latest,
        categories=canonical_categories(db),
        nav="home",
    )


@simple_router.get("/business")
def simple_business(
    request: Request,
    period: str | None = None,
    kind: str | None = None,
    category_id: OptionalIdentifier = None,
    page: int = 1,
    db: Session = Depends(get_db),
) -> Response:
    _user(request)
    try:
        month = parse_period(period) if period else today_ar().replace(day=1)
        if page < 1:
            raise ValueError("Página inválida.")
        rows = get_business_transactions(
            db,
            month,
            kind=kind or None,
            category_id=category_id,
            limit=51,
            offset=(page - 1) * 50,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    available = [category for category in _categories(db) if category.activity == "business"]
    tax_category = next((cat for cat in available if cat.name == "Impuestos de la pelu"), None)
    return _render(
        request,
        db,
        "business",
        summary=get_business_summary(db, month),
        period=month.strftime("%Y-%m"),
        kind=kind or None,
        category_id=category_id,
        page=page,
        has_next=len(rows) > 50,
        transactions=rows[:50],
        categories=canonical_categories(db),
        business_categories=available,
        tax_category=tax_category,
        nav="business",
    )


@simple_router.get("/new")
def simple_new(
    request: Request,
    kind: str = "expense",
    activity: str = "household",
    category_id: OptionalIdentifier = None,
    db: Session = Depends(get_db),
) -> Response:
    _user(request)
    if kind not in ("expense", "income"):
        raise HTTPException(400, "Elegí gasto o ingreso.")
    initial_values: dict[str, Any] = (
        {"activity": activity} if activity in ("household", "business", "personal") else {}
    )
    if category_id is not None:
        category = next((cat for cat in _categories(db, kind) if cat.id == category_id), None)
        if category is None or category.activity != activity:
            raise HTTPException(400, "Elegí una categoría disponible para esta actividad.")
        initial_values["category_id"] = category_id
    return _new_form(request, db, kind, initial_values)


def _new_form(
    request: Request,
    db: Session,
    kind: str,
    values: dict[str, Any],
    error: str | None = None,
    status: int = 200,
) -> Response:
    from app.services.account_preference_service import get_preferred_expense_account

    preferred = get_preferred_expense_account(db, _user(request).id) if kind == "expense" else None
    available = _categories(db, kind, values.get("category_id"))
    available_ids = {category.id for category in available}
    top = [
        category
        for category in get_top_categories(db, kind, user_id=_user(request).id)
        if category.id in available_ids
    ]
    top_ids = {category.id for category in top}
    return _render(
        request,
        db,
        "new",
        kind=kind,
        top_categories=top,
        preferred_account_id=preferred.id if preferred else None,
        accounts=list(
            db.scalars(
                select(Account)
                .where(
                    Account.active.is_(True),
                    Account.is_virtual.is_(False),
                    Account.currency == "ARS",
                )
                .order_by(Account.id)
            )
        ),
        users=list(db.scalars(select(User).where(User.active.is_(True)).order_by(User.id))),
        other_categories=[category for category in available if category.id not in top_ids],
        opening_date=get_opening_date(db),
        opening_omission=values.get("opening_omission") == "1",
        values=values,
        error=error,
        status=status,
        nav="new",
    )


@simple_router.post("/new", dependencies=[Depends(_csrf_guard)])
def simple_create(
    request: Request,
    kind: str = Form(...),
    amount: str = Form(...),
    category_id: int | None = Form(None),
    show_all_categories: str = Form(""),
    tx_date: str = Form(...),
    comment: str = Form(""),
    account_id: int | None = Form(None),
    activity: str | None = Form(None),
    charged_to_user_id: int | None = Form(None),
    csrf_token: str | None = Form(None),
    idempotency_token: str = Form(""),
    opening_omission: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    _csrf_guard(request, csrf_token)
    clean_category_id = category_id if isinstance(category_id, int) else None
    values = {
        "opening_omission": opening_omission if isinstance(opening_omission, str) else "",
        "kind": kind,
        "amount": amount,
        "category_id": clean_category_id,
        "show_all_categories": show_all_categories,
        "tx_date": tx_date,
        "comment": comment,
        "account_id": account_id,
        "activity": activity,
        "charged_to_user_id": charged_to_user_id,
    }

    def create() -> Transaction:
        if clean_category_id is None:
            raise ValueError("Elegí una categoría.")
        return simple_create_transaction(
            db,
            user_id=_user(request).id,
            kind=kind,
            amount=parse_decimal(amount),
            category_id=clean_category_id,
            tx_date=date.fromisoformat(tx_date),
            comment=comment,
            account_id=account_id if isinstance(account_id, int) else None,
            activity=activity if isinstance(activity, str) else None,
            charged_to_user_id=charged_to_user_id if isinstance(charged_to_user_id, int) else None,
            use_category_default=False,
            opening_omission=parse_checkbox(opening_omission),
        )

    return _operate(
        request,
        db,
        idempotency_token,
        "simple.create",
        values,
        create,
        lambda identity: f"/s/list?saved={identity}&as_of={tx_date}",
        lambda error, status: _new_form(
            request, db, kind if kind in ("expense", "income") else "expense", values, error, status
        ),
    )


@simple_router.get("/list")
def simple_list(
    request: Request,
    window: str = "month",
    kind: str | None = None,
    category_id: OptionalIdentifier = None,
    account_id: OptionalIdentifier = None,
    q: str | None = None,
    page: int = 1,
    saved: int | None = None,
    as_of: str | None = None,
    scope: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    _user(request)
    kind = kind or None
    if page < 1 or kind not in (None, "income", "expense") or scope not in (None, "summary"):
        raise HTTPException(400, "Filtro inválido.")
    as_of_date = _parse_as_of(as_of)
    try:
        rows = get_simple_transactions(
            db,
            span=window,
            kind=kind,
            category_id=category_id,
            account_id=account_id,
            search_query=q,
            limit=51,
            offset=(page - 1) * 50,
            as_of=as_of_date,
            include_voided=scope != "summary",
            exclude_personal=scope == "summary",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    saved_tx = db.get(Transaction, saved) if saved else None
    now = datetime.now(UTC)
    undo_remaining = 0
    if (
        saved_tx
        and saved_tx.status == "active"
        and saved_tx.created_by == _user(request).id
        and saved_tx.generated_by == "simple"
    ):
        undo_remaining = max(
            0, int((saved_tx.created_at + timedelta(seconds=60) - now).total_seconds())
        )
    return _render(
        request,
        db,
        "list",
        transactions=rows[:50],
        has_next=len(rows) > 50,
        page=page,
        window=window,
        kind=kind,
        category_id=category_id,
        account_id=account_id,
        q=q or "",
        scope=scope,
        categories=canonical_categories(db),
        available_categories=sorted(
            canonical_categories(db).values(),
            key=lambda c: c.name,
        ),
        available_accounts=list(
            db.scalars(
                select(Account)
                .where(Account.active.is_(True), Account.is_virtual.is_(False))
                .order_by(Account.name)
            ).all()
        ),
        saved_tx=saved_tx,
        undo_remaining=undo_remaining,
        as_of=(as_of_date or today_ar()).isoformat(),
        nav="list",
    )


@simple_router.get("/{transaction_id:int}/edit")
def simple_edit(request: Request, transaction_id: int, db: Session = Depends(get_db)) -> Response:
    _user(request)
    transaction = _transaction(db, transaction_id)
    return _render(
        request,
        db,
        "edit",
        transaction=transaction,
        accounts=list(
            db.scalars(
                select(Account)
                .where(
                    Account.active.is_(True),
                    Account.is_virtual.is_(False),
                    Account.currency == "ARS",
                )
                .order_by(Account.id)
            )
        ),
        users=list(db.scalars(select(User).where(User.active.is_(True)).order_by(User.id))),
        categories=_categories(db, transaction.kind, transaction.category_id),
        has_future_installments=has_future_card_installments(db, transaction_id),
        values={},
        nav="list",
    )


@simple_router.post("/{transaction_id:int}/edit", dependencies=[Depends(_csrf_guard)])
def simple_update(
    request: Request,
    transaction_id: int,
    expected_version: int = Form(...),
    amount: str = Form(...),
    category_id: int | None = Form(None),
    show_all_categories: str = Form(""),
    tx_date: str = Form(...),
    comment: str = Form(""),
    account_id: int | None = Form(None),
    activity: str | None = Form(None),
    charged_to_user_id: int | None = Form(None),
    csrf_token: str | None = Form(None),
    idempotency_token: str = Form(""),
    future_installments_update: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    _csrf_guard(request, csrf_token)
    transaction = _transaction(db, transaction_id)
    clean_category_id = category_id if isinstance(category_id, int) else None
    values: dict[str, Any] = {
        "expected_version": expected_version,
        "amount": amount,
        "category_id": clean_category_id,
        "show_all_categories": show_all_categories,
        "tx_date": tx_date,
        "comment": comment,
        "account_id": account_id if isinstance(account_id, int) else transaction.account_id,
        "activity": activity if isinstance(activity, str) else transaction.activity,
        "charged_to_user_id": charged_to_user_id if isinstance(charged_to_user_id, int) else None,
        "future_installments_update": (
            future_installments_update.strip() or None
            if isinstance(future_installments_update, str)
            else None
        ),
    }

    def update() -> Transaction:
        if transaction.kind not in ("expense", "income") or transaction.usd_amount is not None:
            raise ValueError("Este movimiento se edita en la vista completa.")
        if clean_category_id is None:
            raise ValueError("Elegí una categoría.")
        category = db.get(Category, clean_category_id)
        if (
            category is None
            or category.kind != transaction.kind
            or (not category.active and clean_category_id != transaction.category_id)
        ):
            raise ValueError("Categoría inválida.")
        parsed_date = date.fromisoformat(tx_date)
        date_changed = parsed_date != transaction.date
        category_changed = clean_category_id != transaction.category_id
        parsed_amount = parse_decimal(amount)
        choice = values["future_installments_update"]
        if choice not in {None, "yes", "no"}:
            raise ValueError("Elegí si querés actualizar las estimaciones restantes.")
        if (
            parsed_amount != transaction.amount
            and has_future_card_installments(db, transaction_id)
            and choice is None
        ):
            raise ValueError("Elegí si la corrección también cambia las cuotas restantes.")
        effective_period = (
            resolve_transaction_period(parsed_date, category.default_period_offset)
            if (date_changed or category_changed) and not transaction.opening_omission
            else transaction.period
        )
        charged_to = (
            category.default_charged_to_user_id
            if category_changed
            else transaction.charged_to_user_id
        )
        return update_transaction(
            db,
            transaction_id=transaction_id,
            kind=transaction.kind,
            amount=parsed_amount,
            account_id=values["account_id"],
            tx_date=parsed_date,
            period=effective_period,
            description=comment.strip() or category.name,
            user_id=_user(request).id,
            expected_version=expected_version,
            category_id=clean_category_id,
            charged_to_user_id=values["charged_to_user_id"]
            if isinstance(activity, str)
            else charged_to,
            activity=values["activity"],
            envelope_id=transaction.envelope_id,
            usd_amount=transaction.usd_amount,
            fx_rate_effective=transaction.fx_rate_effective,
            future_installments_update=choice == "yes",
        )

    return _operate(
        request,
        db,
        idempotency_token,
        f"simple.edit:{transaction_id}",
        values,
        update,
        lambda identity: f"/s/{identity}",
        lambda error, status: _render(
            request,
            db,
            "edit",
            transaction=_transaction(db, transaction_id),
            accounts=list(
                db.scalars(
                    select(Account)
                    .where(
                        Account.active.is_(True),
                        Account.is_virtual.is_(False),
                        Account.currency == "ARS",
                    )
                    .order_by(Account.id)
                )
            ),
            users=list(db.scalars(select(User).where(User.active.is_(True)).order_by(User.id))),
            categories=_categories(
                db, transaction.kind, clean_category_id or transaction.category_id
            ),
            has_future_installments=has_future_card_installments(db, transaction_id),
            values=values,
            error=error,
            status=status,
            nav="list",
        ),
    )


@simple_router.post("/{transaction_id:int}/void", dependencies=[Depends(_csrf_guard)])
def simple_void(
    request: Request,
    transaction_id: int,
    reason: str = Form(...),
    expected_version: int = Form(...),
    csrf_token: str | None = Form(None),
    idempotency_token: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    _csrf_guard(request, csrf_token)
    _transaction(db, transaction_id)
    return _operate(
        request,
        db,
        idempotency_token,
        f"simple.void:{transaction_id}",
        {"reason": reason, "expected_version": expected_version},
        lambda: void_transaction(
            db, transaction_id, reason, _user(request).id, expected_version=expected_version
        ),
        lambda identity: f"/s/{identity}",
        lambda error, status: _detail(request, db, transaction_id, error, status),
    )


@simple_router.post("/{transaction_id:int}/undo", dependencies=[Depends(_csrf_guard)])
def simple_undo(
    request: Request,
    transaction_id: int,
    expected_version: int = Form(...),
    csrf_token: str | None = Form(None),
    idempotency_token: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    _csrf_guard(request, csrf_token)
    _transaction(db, transaction_id)
    return _operate(
        request,
        db,
        idempotency_token,
        f"simple.undo:{transaction_id}",
        {"expected_version": expected_version},
        lambda: undo_quick_transaction(
            db, transaction_id, _user(request).id, expected_version=expected_version
        ),
        lambda identity: "/s/list",
        lambda error, status: _detail(request, db, transaction_id, error, status),
    )


@simple_router.get("/settings")
def simple_settings(request: Request, db: Session = Depends(get_db)) -> Response:
    from app.domain.ui_catalog import COLORS, ICONS

    return _render(
        request,
        db,
        "settings",
        categories=_categories(db),
        users=list(db.scalars(select(User).where(User.active.is_(True)).order_by(User.id))),
        error=getattr(request.state, "category_settings_error", None),
        colors=COLORS,
        icons=ICONS,
        nav="home",
    )


@simple_router.post("/settings/mode", dependencies=[Depends(_csrf_guard)])
def simple_mode(
    request: Request,
    mode: str = Form(...),
    csrf_token: str | None = Form(None),
    idempotency_token: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    _csrf_guard(request, csrf_token)
    user = _user(request)
    target = "/s" if mode == "simple" else "/full"
    try:
        idem = begin_operation(
            db,
            token=idempotency_token,
            route="simple.settings.mode",
            user_id=user.id,
            payload=operation_payload({"mode": mode}),
        )
    except DuplicateCompletedError:
        return RedirectResponse(target, status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        set_ui_mode(db, user.id, mode)
        if idem is not None:
            finish_operation(db, idem, result_entity="User", result_id=user.id)
    except ValueError as exc:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(target, status_code=303)


@simple_router.post("/settings/navigation", dependencies=[Depends(_csrf_guard)])
async def simple_navigation_settings(
    request: Request,
    csrf_token: str | None = Form(None),
    idempotency_token: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    _csrf_guard(request, csrf_token)
    user = _user(request)
    form_data = await request.form()
    modules_list = [str(m).strip() for m in form_data.getlist("modules") if str(m).strip()]
    if not modules_list:
        prefix = "module_"
        for key in form_data:
            if key.startswith(prefix) and form_data[key]:
                modules_list.append(key[len(prefix) :])
    selected_modules = modules_list

    try:
        idem = begin_operation(
            db,
            token=idempotency_token,
            route="simple.settings.navigation",
            user_id=user.id,
            payload=operation_payload({"modules": selected_modules}),
        )
    except DuplicateCompletedError:
        return RedirectResponse("/s/settings?saved=nav", status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        set_nav_modules(db, user.id, selected_modules)
        if idem is not None:
            finish_operation(db, idem, result_entity="User", result_id=user.id)
    except Exception as exc:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(400, str(exc)) from exc

    return RedirectResponse("/s/settings?saved=nav", status_code=303)


@simple_router.get("/cards")
def simple_cards(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    _user(request)
    data = get_simple_cards_view(db)
    return _render(
        request,
        db,
        "cards",
        cards=data["cards"],
        total_debt=data["total_debt"],
        upcoming_months=data["upcoming_months"],
        nav="cards",
    )


@simple_router.get("/close")
def simple_close(
    request: Request,
    period: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    _user(request)
    period_date = _parse_as_of(period) if period else None
    data = get_simple_close_view(db, period_date)
    return _render(
        request,
        db,
        "close",
        period=data["period"],
        is_closed=data["is_closed"],
        existing_close=data["existing_close"],
        preview=data["preview"],
        recent_closes=data["recent_closes"],
        nav="close",
    )


@simple_router.get("/envelopes")
def simple_envelopes(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    _user(request)
    data = get_simple_envelopes_view(db)
    return _render(
        request,
        db,
        "envelopes",
        available_fund=data["available_fund"],
        envelopes=data["envelopes"],
        total_reserved=data["total_reserved"],
        nav="envelopes",
    )


@simple_router.post("/settings/category/{category_id}", dependencies=[Depends(_csrf_guard)])
def simple_category_style(
    request: Request,
    category_id: int,
    icon: str = Form(...),
    color: str = Form(...),
    csrf_token: str | None = Form(None),
    idempotency_token: str = Form(""),
    return_to: str = Form("/s/settings"),
    db: Session = Depends(get_db),
) -> Response:
    _csrf_guard(request, csrf_token)
    user = _user(request)
    target = "/settings" if return_to == "/settings" else "/s/settings"
    try:
        idem = begin_operation(
            db,
            token=idempotency_token,
            route=f"simple.settings.category:{category_id}",
            user_id=user.id,
            payload=operation_payload(
                {"category_id": category_id, "icon": icon, "color": color, "return_to": return_to}
            ),
        )
    except DuplicateCompletedError:
        return RedirectResponse(target, status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        update_category_appearance(db, category_id, icon, color, user.id)
        if idem is not None:
            finish_operation(db, idem, result_entity="Category", result_id=category_id)
    except ValueError as exc:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(target, status_code=303)


def _detail(
    request: Request, db: Session, transaction_id: int, error: str | None = None, status: int = 200
) -> Response:
    transaction = _transaction(db, transaction_id)
    return _render(
        request,
        db,
        "detail",
        transaction=transaction,
        category=canonical_categories(db).get(transaction.category_id or 0),
        error=error,
        status=status,
        nav="list",
    )


@simple_router.get("/{transaction_id:int}")
def simple_detail(request: Request, transaction_id: int, db: Session = Depends(get_db)) -> Response:
    _user(request)
    return _detail(request, db, transaction_id)
