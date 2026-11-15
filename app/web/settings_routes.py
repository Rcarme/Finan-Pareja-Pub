"""Rutas web para la pantalla de Configuración general (SPEC §4 Pantalla 11, RN-03, RN-23, RN-45).

Permite crear cuentas contables, crear categorías personalizadas, ajustar la regla de reparto
de cierre (por defecto 30/20/50) y definir el monto objetivo del Colchón.
"""

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.formatters import parse_decimal, parse_period, today_ar
from app.core.security import generate_csrf_token, validate_csrf
from app.core.transactions import ConcurrencyError
from app.domain.close_engine import CloseSplitConfig
from app.domain.ui_catalog import COLORS, ICONS
from app.models.auth import User
from app.models.envelopes import Envelope
from app.models.ledger import Account
from app.services.category_service import list_categories, merge_categories, set_category_owner
from app.services.close_service import get_close_split_config, update_close_split_config
from app.services.envelope_service import get_envelope_balance, update_buffer_target
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
    configure_account,
    create_account,
    create_category,
    delete_category,
    get_account_balance,
    get_total_fund,
    rename_account,
)
from app.services.ownership_service import ownership_enabled

settings_router = APIRouter(prefix="/settings", tags=["settings"])


def _ensure_csrf_token(request: Request) -> str:
    token = request.cookies.get("csrf_token")
    if not token:
        token = generate_csrf_token()
    return token


@settings_router.get("", response_class=HTMLResponse)
def settings_index(
    request: Request,
    success: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Pantalla de Configuración general (SPEC §4 Pantalla 11)."""
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    csrf_token = _ensure_csrf_token(request)

    accounts_db = (
        db.execute(select(Account).where(Account.active.is_(True)).order_by(Account.id))
        .scalars()
        .all()
    )
    accounts = [
        {
            "id": a.id,
            "name": a.name,
            "type": a.type,
            "owner_user_id": a.owner_user_id,
            "currency": a.currency,
            "is_virtual": a.is_virtual,
            "opening_balance": a.opening_balance,
            "version": a.version,
            "balance": get_total_fund(db) if a.is_virtual else get_account_balance(db, a.id),
        }
        for a in accounts_db
    ]

    categories = list_categories(db, current_user.id, show_all=True)
    users = db.execute(select(User).where(User.active.is_(True)).order_by(User.id)).scalars().all()
    users_map = {u.id: u.display_name for u in users}

    from app.services.account_preference_service import get_preferred_expense_account

    preferred = get_preferred_expense_account(db, current_user.id)
    split_cfg = get_close_split_config(db)
    colchon_env = db.execute(
        select(Envelope).where(Envelope.name == "Colchón", Envelope.active.is_(True))
    ).scalar_one_or_none()
    colchon_balance = get_envelope_balance(db, colchon_env.id) if colchon_env else Decimal("0.00")

    from app.web.app import templates

    context: dict[str, Any] = {
        "request": request,
        "title": "Configuración - Finanzas",
        "user": current_user,
        "csrf_token": csrf_token,
        "active_nav": "settings",
        "accounts": accounts,
        "preferred_account_id": preferred.id if preferred else None,
        "categories": categories,
        "category_versions": {str(category.id): category.version for category in categories},
        "merge_values": getattr(request.state, "category_merge_values", {}),
        "merge_categories": categories,
        "users": users,
        "users_map": users_map,
        "split_cfg": split_cfg,
        "colchon_env": colchon_env,
        "colchon_balance": colchon_balance,
        "success": success,
        "error": error,
        "appearance_icons": ICONS,
        "appearance_colors": COLORS,
        "ownership_active": ownership_enabled(db),
        "current_period": today_ar().strftime("%Y-%m"),
    }
    response = templates.TemplateResponse(
        request=request,
        name="settings/index.html",
        context=context,
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")
    return response


def _settings_error(request: Request, db: Session, error: str, status: int = 400) -> Response:
    response = settings_index(request=request, db=db, error=error)
    response.status_code = status
    return response


@settings_router.post("/accounts")
def create_account_post(
    request: Request,
    name: str = Form(...),
    account_type: str = Form("bank", alias="type"),
    opening_balance: str = Form("0,00"),
    owner_user_id: int | None = Form(None),
    currency: str = Form("ARS"),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Crea una nueva cuenta contable (RN-03)."""
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
            route="/settings/accounts",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/settings?success=cuenta_creada", status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        dec_balance = parse_decimal(opening_balance) if opening_balance.strip() else Decimal("0.00")
        acc = create_account(
            db=db,
            name=name,
            account_type=account_type,
            opening_balance=dec_balance,
            user_id=current_user.id,
            owner_user_id=owner_user_id if isinstance(owner_user_id, int) else None,
            currency=currency if isinstance(currency, str) else "ARS",
        )
        if idem is not None:
            finish_operation(db, idem, result_entity="Account", result_id=acc.id)
        return RedirectResponse(url="/settings?success=cuenta_creada", status_code=303)
    except (ValueError, InvalidOperation) as exc:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@settings_router.post("/accounts/{account_id}/rename")
def rename_account_post(
    account_id: int,
    request: Request,
    name: str = Form(...),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Renombra una cuenta contable existente (RN-03, RN-37)."""
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
            route=f"/settings/accounts/{account_id}/rename",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/settings?success=cuenta_renombrada", status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        acc = rename_account(
            db=db,
            account_id=account_id,
            new_name=name,
            user_id=current_user.id,
        )
        if idem is not None:
            finish_operation(db, idem, result_entity="Account", result_id=acc.id)
        return RedirectResponse(url="/settings?success=cuenta_renombrada", status_code=303)
    except ValueError as exc:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@settings_router.post("/categories")
def create_category_post(
    request: Request,
    name: str = Form(...),
    kind: str = Form(...),
    activity: str = Form("household"),
    default_period_offset: str = Form("0"),
    default_charged_to_user_id: str | None = Form(None),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Crea una nueva categoría de ingresos o gastos (RN-05, RN-41)."""
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
            route="/settings/categories",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/settings?success=categoria_creada", status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        offset_int = int(default_period_offset.strip()) if default_period_offset.strip() else 0
        charged_uid = (
            int(default_charged_to_user_id)
            if default_charged_to_user_id and default_charged_to_user_id.strip()
            else None
        )
        cat = create_category(
            db=db,
            name=name,
            kind=kind,
            user_id=current_user.id,
            default_period_offset=offset_int,
            default_charged_to_user_id=charged_uid,
            activity=activity if isinstance(activity, str) else "household",
        )
        if idem is not None:
            finish_operation(db, idem, result_entity="Category", result_id=cat.id)
        return RedirectResponse(url="/settings?success=categoria_creada", status_code=303)
    except ValueError as exc:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@settings_router.post("/categories/{category_id}/delete")
def delete_category_post(
    category_id: int,
    request: Request,
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Desactiva (elimina de la vista activa) una categoría contable (RN-05, RN-37)."""
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
            route=f"/settings/categories/{category_id}/delete",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/settings?success=categoria_eliminada", status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        cat = delete_category(
            db=db,
            category_id=category_id,
            user_id=current_user.id,
        )
        if idem is not None:
            finish_operation(db, idem, result_entity="Category", result_id=cat.id)
        return RedirectResponse(url="/settings?success=categoria_eliminada", status_code=303)
    except ValueError as exc:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@settings_router.post("/close-split")
def update_close_split_post(
    request: Request,
    savings_pct: str = Form(...),
    buffer_pct: str = Form(...),
    distribution_pct: str = Form(...),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Actualiza la configuración de porcentajes de cierre (RN-23)."""
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
            route="/settings/close-split",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/settings?success=porcentajes_actualizados", status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        cfg = CloseSplitConfig(
            savings_pct=parse_decimal(savings_pct),
            buffer_pct=parse_decimal(buffer_pct),
            distribution_pct=parse_decimal(distribution_pct),
        )
        update_close_split_config(db=db, config=cfg, user_id=current_user.id)
        if idem is not None:
            finish_operation(db, idem, result_entity="Setting", result_id=1)
        return RedirectResponse(url="/settings?success=porcentajes_actualizados", status_code=303)
    except (ValueError, InvalidOperation) as exc:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@settings_router.post("/buffer-target")
def update_buffer_target_post(
    request: Request,
    target_amount: str = Form(""),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Actualiza el monto objetivo (tope) del sobre Colchón (RN-22, RN-45)."""
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
            route="/settings/buffer-target",
            user_id=current_user.id,
        )
    except DuplicateCompletedError:
        return RedirectResponse(url="/settings?success=colchon_actualizado", status_code=303)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        dec_target = parse_decimal(target_amount) if target_amount.strip() else None
        env = update_buffer_target(db=db, target_amount=dec_target, user_id=current_user.id)
        if idem is not None:
            finish_operation(db, idem, result_entity="Envelope", result_id=env.id)
        return RedirectResponse(url="/settings?success=colchon_actualizado", status_code=303)
    except (ValueError, InvalidOperation) as exc:
        if idem is not None:
            abort_operation(db, idem)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@settings_router.post("/preferred-expense-account")
def preferred_account_post(
    request: Request,
    account_id: str = Form(""),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    from app.services.account_preference_service import set_preferred_expense_account

    if not validate_csrf(request, csrf_token):
        raise HTTPException(403, "Token CSRF inválido.")
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        set_preferred_expense_account(
            db, user.id, int(account_id) if account_id.strip() else None, user.id
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return RedirectResponse("/settings", status_code=303)


@settings_router.post("/accounts/{account_id}/credit-correction")
def account_credit_correction_post(
    request: Request,
    account_id: int,
    debt_amount: str = Form(...),
    owner_user_id: str = Form(""),
    period: str = Form(...),
    reason: str = Form(...),
    expected_version: int = Form(...),
    confirmed: str = Form(""),
    csrf_token: str | None = Form(None),
    idempotency_token: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Explicit correction of an unused account, without creating an expense (Q26)."""
    from app.services.account_profile_service import correct_account_to_credit

    if not validate_csrf(request, csrf_token):
        raise HTTPException(403, "Token CSRF inválido.")
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if confirmed != "yes" or not idempotency_token.strip():
        return _settings_error(request, db, "Confirmá la deuda y usá el formulario de corrección.")
    idem = None
    try:
        idem = begin_operation(
            db,
            token=idempotency_token,
            route=f"/settings/accounts/{account_id}/credit-correction",
            user_id=user.id,
            payload=operation_payload(locals()),
        )
        assert idem is not None
        account = correct_account_to_credit(
            db,
            account_id=account_id,
            debt_amount=parse_decimal(debt_amount),
            owner_user_id=int(owner_user_id) if owner_user_id.strip() else None,
            period=parse_period(period),
            reason=reason,
            user_id=user.id,
            expected_version=expected_version,
        )
        finish_operation(db, idem, result_entity="Account", result_id=account.id)
    except DuplicateCompletedError:
        return RedirectResponse("/settings?success=cuenta_corregida", status_code=303)
    except OperationInProgressError as error:
        return _settings_error(request, db, str(error), 409)
    except (ConcurrencyError, ValueError, InvalidOperation, ClosedPeriodError) as error:
        if idem is not None:
            abort_operation(db, idem)
        return _settings_error(
            request, db, str(error), 409 if isinstance(error, ConcurrencyError) else 400
        )
    return RedirectResponse("/settings?success=cuenta_corregida", status_code=303)


@settings_router.post("/accounts/{account_id}/profile")
def account_profile_post(
    request: Request,
    account_id: int,
    account_type: str = Form(...),
    owner_user_id: str = Form(""),
    currency: str = Form("ARS"),
    is_virtual: str = Form(""),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    if not validate_csrf(request, csrf_token):
        raise HTTPException(403, "Token CSRF inválido.")
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        configure_account(
            db,
            account_id,
            account_type,
            int(owner_user_id) if owner_user_id.strip() else None,
            currency,
            is_virtual == "yes",
            user.id,
        )
    except ValueError as error:
        return _settings_error(request, db, str(error))
    return RedirectResponse("/settings", status_code=303)


def _category_settings_error(
    request: Request, db: Session, target: str, error: str, status: int
) -> Response:
    if target == "/s/settings":
        from app.web.simple_routes import simple_settings

        request.state.category_settings_error = error
        response = simple_settings(request, db)
        response.status_code = status
        return response
    return _settings_error(request, db, error, status)


@settings_router.post("/categories/{category_id}/owner")
def category_owner_post(
    request: Request,
    category_id: int,
    expected_version: int = Form(...),
    owner_user_id: str = Form(""),
    activity: str | None = Form(None),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    return_to: str = Form("/settings"),
    db: Session = Depends(get_db),
) -> Response:
    """RN-05: organize visibility and activity without changing historical accounting."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(403, "Token CSRF inválido.")
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)
    target = "/s/settings" if return_to == "/s/settings" else "/settings"
    clean_activity = activity.strip() if isinstance(activity, str) and activity.strip() else None
    operation = None
    try:
        payload_data: dict[str, Any] = {
            "category_id": category_id,
            "owner_user_id": owner_user_id,
            "expected_version": expected_version,
        }
        if clean_activity is not None:
            payload_data["activity"] = clean_activity
        operation = begin_operation(
            db,
            token=idempotency_token,
            user_id=user.id,
            route=f"settings.category.owner:{category_id}",
            payload=operation_payload(payload_data),
        )
        owner = int(owner_user_id) if owner_user_id.strip() else None
        category = set_category_owner(
            db,
            category_id,
            owner,
            user.id,
            expected_version,
            activity=clean_activity,
        )
        if operation is not None:
            finish_operation(db, operation, result_entity="Category", result_id=category.id)
        return RedirectResponse(target, status_code=303)
    except DuplicateCompletedError:
        return RedirectResponse(target, status_code=303)
    except (ConcurrencyError, OperationInProgressError) as exc:
        if operation is not None:
            abort_operation(db, operation)
        return _category_settings_error(request, db, target, str(exc), 409)
    except ValueError as exc:
        if operation is not None:
            abort_operation(db, operation)
        return _category_settings_error(request, db, target, str(exc), 400)


@settings_router.post("/categories/merge")
def category_merge_post(
    request: Request,
    target_id: int = Form(...),
    source_ids: list[int] = Form([]),
    expected_versions: str = Form(...),
    csrf_token: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """RN-05: explicitly select historical aliases; never rewrite movements."""
    if not validate_csrf(request, csrf_token):
        raise HTTPException(403, "Token CSRF inválido.")
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)
    operation = None
    try:
        versions_raw = json.loads(expected_versions)
        if not isinstance(versions_raw, dict):
            raise ValueError("Recargá las versiones de las categorías.")
        versions = {int(key): int(value) for key, value in versions_raw.items()}
        operation = begin_operation(
            db,
            token=idempotency_token,
            user_id=user.id,
            route="settings.categories.merge",
            payload={
                "target_id": target_id,
                "source_ids": sorted(source_ids),
                "expected_versions": versions,
            },
        )
        category = merge_categories(db, target_id, source_ids, user.id, versions)
        if operation is not None:
            finish_operation(db, operation, result_entity="Category", result_id=category.id)
        return RedirectResponse("/settings", status_code=303)
    except DuplicateCompletedError:
        return RedirectResponse("/settings", status_code=303)
    except (ConcurrencyError, OperationInProgressError) as exc:
        if operation is not None:
            abort_operation(db, operation)
        request.state.category_merge_values = {"target_id": target_id, "source_ids": source_ids}
        return _settings_error(request, db, str(exc), 409)
    except (ValueError, TypeError) as exc:
        if operation is not None:
            abort_operation(db, operation)
        request.state.category_merge_values = {"target_id": target_id, "source_ids": source_ids}
        return _settings_error(request, db, str(exc))


@settings_router.post("/gemini-api-key")
def update_gemini_key_action(
    request: Request,
    api_key: str = Form(""),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url="/settings?error=Token+de+seguridad+inválido", status_code=303)
    from app.services.ai_assistant_service import update_gemini_api_key

    update_gemini_api_key(db, api_key.strip(), current_user.id)
    return RedirectResponse(
        url="/settings?success=Clave+de+Google+Gemini+actualizada+correctamente", status_code=303
    )
