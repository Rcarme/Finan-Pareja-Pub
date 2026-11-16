"""Rutas del asistente de configuración inicial (/setup)."""

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.auth import create_session
from app.core.config import settings
from app.core.db import get_db
from app.core.middleware import invalidate_onboarding_cache
from app.core.security import (
    clear_failed_setup_attempts,
    generate_csrf_token,
    get_client_ip,
    is_setup_rate_limited,
    record_failed_setup_attempt,
    secure_cookies,
    validate_csrf,
)
from app.services.setup_service import initialize_instance, is_onboarded

setup_router = APIRouter(prefix="/setup", tags=["setup"])


def _is_setup_token_valid(provided_token: str | None) -> bool:
    expected = (settings.SETUP_TOKEN or "").strip()
    is_prod = (settings.ENVIRONMENT or "").lower() in ("production", "prod")
    if is_prod and not expected:
        return False
    if not expected:
        return True
    if not provided_token or not provided_token.strip():
        return False
    return secrets.compare_digest(provided_token.strip(), expected)


def _render_setup_response(
    request: Request,
    context: dict[str, Any],
    status_code: int = 200,
) -> Response:
    from app.web.app import templates

    csrf_token = generate_csrf_token()
    context["csrf_token"] = csrf_token
    response = templates.TemplateResponse(
        request=request,
        name="setup.html",
        context=context,
        status_code=status_code,
    )
    response.set_cookie(
        "csrf_token",
        csrf_token,
        httponly=False,
        samesite="lax",
        secure=secure_cookies(),
    )
    return response


@setup_router.get("", response_class=HTMLResponse)
async def setup_page(request: Request, db: Session = Depends(get_db)) -> Response:
    if is_onboarded(db):
        return RedirectResponse(url="/login", status_code=302)

    is_prod = (settings.ENVIRONMENT or "").lower() in ("production", "prod")
    token_missing_in_prod = is_prod and not (settings.SETUP_TOKEN or "").strip()
    prod_error = (
        "Por seguridad, en entornos de producción es obligatorio definir la variable SETUP_TOKEN "
        "en el archivo .env antes de acceder al asistente de inicio."
        if token_missing_in_prod
        else None
    )

    return _render_setup_response(
        request=request,
        context={
            "error": prod_error,
            "require_setup_token": bool(settings.SETUP_TOKEN) or is_prod,
            "setup_token": "",
            "blocked_in_prod": token_missing_in_prod,
        },
        status_code=403 if token_missing_in_prod else 200,
    )


@setup_router.post("", response_class=HTMLResponse)
async def process_setup(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
    setup_token: str = Form(""),
    user1_username: str = Form(...),
    user1_display_name: str = Form(""),
    user1_password: str = Form(...),
    user2_username: str = Form(...),
    user2_display_name: str = Form(""),
    user2_password: str = Form(...),
    currency: str = Form("ARS"),
    split_pct: int = Form(50),
    business_enabled: bool = Form(False),
    business_name: str = Form(""),
    gemini_api_key: str = Form(""),
    seed_defaults: bool = Form(True),
) -> Response:
    if is_onboarded(db):
        return RedirectResponse(url="/login", status_code=302)

    client_ip = get_client_ip(request)
    if is_setup_rate_limited(client_ip):
        return _render_setup_response(
            request=request,
            context={
                "error": "Demasiados intentos fallidos. Por favor, esperá 15 minutos.",
                "require_setup_token": bool(settings.SETUP_TOKEN),
                "setup_token": "",
            },
            status_code=429,
        )

    if not validate_csrf(request, csrf_token):
        return _render_setup_response(
            request=request,
            context={
                "error": "El token de seguridad expiró. Por favor, recargá e intentá nuevamente.",
                "require_setup_token": bool(settings.SETUP_TOKEN),
                "setup_token": setup_token if _is_setup_token_valid(setup_token) else "",
            },
            status_code=400,
        )

    is_prod = (settings.ENVIRONMENT or "").lower() in ("production", "prod")
    if is_prod and not (settings.SETUP_TOKEN or "").strip():
        return _render_setup_response(
            request=request,
            context={
                "error": "Por seguridad, en producción debe configurarse SETUP_TOKEN en .env.",
                "require_setup_token": True,
                "setup_token": "",
                "blocked_in_prod": True,
            },
            status_code=403,
        )

    if (settings.SETUP_TOKEN or is_prod) and not _is_setup_token_valid(setup_token):
        record_failed_setup_attempt(client_ip)
        return _render_setup_response(
            request=request,
            context={
                "error": "Token de instalación previo inválido. Verificá SETUP_TOKEN en tu .env.",
                "require_setup_token": True,
                "setup_token": "",
            },
            status_code=403,
        )

    try:
        user1, _ = initialize_instance(
            db,
            user1_username=user1_username,
            user1_display_name=user1_display_name,
            user1_password=user1_password,
            user2_username=user2_username,
            user2_display_name=user2_display_name,
            user2_password=user2_password,
            currency=currency,
            split_pct=split_pct,
            gemini_api_key=gemini_api_key,
            business_enabled=business_enabled,
            business_name=business_name,
            seed_defaults=seed_defaults,
        )
        clear_failed_setup_attempts(client_ip)
        invalidate_onboarding_cache()

        # Iniciar sesión automáticamente con el usuario 1
        raw_token, _session_record = create_session(
            db,
            user_id=user1.id,
            user_agent=request.headers.get("user-agent"),
        )
        response = RedirectResponse(url="/s/home", status_code=302)
        response.set_cookie(
            "session_id",
            raw_token,
            httponly=True,
            samesite="lax",
            secure=secure_cookies(),
        )
        return response

    except ValueError as exc:
        return _render_setup_response(
            request=request,
            context={
                "error": str(exc),
                "form": {
                    "user1_username": user1_username,
                    "user1_display_name": user1_display_name,
                    "user2_username": user2_username,
                    "user2_display_name": user2_display_name,
                    "currency": currency,
                    "split_pct": split_pct,
                    "business_enabled": business_enabled,
                    "business_name": business_name,
                    "gemini_api_key": "",
                    "seed_defaults": seed_defaults,
                },
            },
            status_code=400,
        )
