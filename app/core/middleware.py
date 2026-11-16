"""Middleware de autenticación global y contexto de sesión con detección de Onboarding."""

import uuid
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.core.audit import current_client_ip, current_request_id, current_user_id
from app.core.auth import get_active_session
from app.core.db import get_session_factory
from app.core.security import protect_response, secure_cookies
from app.models.auth import SessionModel, User
from app.services.setup_service import is_onboarded

PUBLIC_PREFIXES = (
    "/healthz",
    "/setup",
    "/login",
    "/static",
    "/manifest.webmanifest",
    "/manifest.json",
    "/sw.js",
    "/offline",
)

_IS_ONBOARDED_CACHE: bool | None = None


def _check_onboarded_sync() -> bool:
    global _IS_ONBOARDED_CACHE
    if _IS_ONBOARDED_CACHE is True:
        return True
    db = get_session_factory()()
    try:
        status = is_onboarded(db)
        if status:
            _IS_ONBOARDED_CACHE = True
        return status
    except Exception:
        # Si la tabla no existe aún (ej. antes de correr migraciones), no bloquear healthz
        return False
    finally:
        db.close()


def invalidate_onboarding_cache() -> None:
    global _IS_ONBOARDED_CACHE
    _IS_ONBOARDED_CACHE = None


def _load_session(raw_token: str) -> tuple[SessionModel, User] | None:
    db = get_session_factory()()
    try:
        data = get_active_session(db, raw_token)
        if data is not None:
            db.expunge_all()
        return data
    finally:
        db.close()


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Exige sesión activa en todas las rutas protegidas y redirige a /setup si no hay usuarios."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Asignar identificador de petición e IP del cliente
        req_id = str(uuid.uuid4())
        client_ip = request.client.host if request.client else "127.0.0.1"

        token_req = current_request_id.set(req_id)
        token_ip = current_client_ip.set(client_ip)
        token_user = current_user_id.set(None)

        try:
            # 1. Chequeo de Onboarding inicial
            is_static_or_health = path.startswith("/static") or path in (
                "/healthz",
                "/manifest.webmanifest",
                "/manifest.json",
                "/sw.js",
                "/offline",
            )

            if not is_static_or_health:
                onboarded = await run_in_threadpool(_check_onboarded_sync)
                if not onboarded:
                    if not path.startswith("/setup"):
                        return RedirectResponse(url="/setup", status_code=302)
                elif path == "/setup" or path.startswith("/setup/"):
                    return RedirectResponse(url="/login", status_code=302)

            is_public = any(
                path == prefix or path.startswith(f"{prefix}/") for prefix in PUBLIC_PREFIXES
            )
            session_token = request.cookies.get("session_id")
            user = None
            session_record = None

            if session_token and (not is_public or path in ("/login", "/healthz")):
                auth_data = await run_in_threadpool(_load_session, session_token)
                if auth_data:
                    session_record, user = auth_data
                    request.state.user = user
                    request.state.session = session_record
                    current_user_id.set(user.id)

            if not is_public and user is None:
                accept_header = request.headers.get("accept", "")
                if "text/html" in accept_header or "*/*" in accept_header or not accept_header:
                    response: Response = RedirectResponse(url="/login", status_code=302)
                else:
                    response = JSONResponse(status_code=401, content={"detail": "No autenticado"})
            else:
                response = await call_next(request)

            has_session_cookie = any(
                name.lower() == b"set-cookie" and value.startswith(b"session_id=")
                for name, value in response.raw_headers
            )
            if session_token and session_record is not None and not has_session_cookie:
                exp = session_record.expires_at
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=UTC)
                response.set_cookie(
                    "session_id",
                    session_token,
                    httponly=True,
                    samesite="lax",
                    secure=secure_cookies(),
                    max_age=max(0, int((exp - datetime.now(UTC)).total_seconds())),
                    expires=exp.astimezone(UTC),
                )
            protect_response(request, response)
            return response
        finally:
            current_user_id.reset(token_user)
            current_request_id.reset(token_req)
            current_client_ip.reset(token_ip)
