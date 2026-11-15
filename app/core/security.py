"""Protecciones de seguridad: CSRF, rate limiting y cabeceras HTTP (ARCHITECTURE.md § 4)."""

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

# Estructura en memoria para rate limiting: (ip, username) -> list[datetime]
FAILED_LOGIN_ATTEMPTS: dict[tuple[str, str], list[datetime]] = {}
MAX_ATTEMPTS = 5
WINDOW_MINUTES = 15


def _derive_fernet_key(key_material: str | None = None) -> bytes:
    """Deriva una clave simétrica de 32 bytes en base64 urlsafe para Fernet."""
    key_str = key_material or settings.APP_SECRET_KEY
    if not key_str:
        if settings.ENVIRONMENT.lower() in {"production", "prod"}:
            raise RuntimeError("APP_SECRET_KEY no configurado en entorno de producción.")
        key_str = "insecure-dev-fallback-key-material-32-chars-min"
    digest = hashlib.sha256(key_str.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(secret_text: str, key_material: str | None = None) -> str:
    """Cifra un secreto mediante Fernet (AES-128-CBC + HMAC-SHA256 autenticado)."""
    if not secret_text:
        return ""
    f = Fernet(_derive_fernet_key(key_material))
    token = f.encrypt(secret_text.encode("utf-8")).decode("ascii")
    return "enc:v2:" + token


def decrypt_secret(encrypted_text: str, key_material: str | None = None) -> str:
    """Descifra un texto cifrado con encrypt_secret, con migración hacia atrás."""
    if not encrypted_text or not encrypted_text.startswith("enc:"):
        return encrypted_text

    f = Fernet(_derive_fernet_key(key_material))

    # Formato v2 autenticado (Fernet)
    if encrypted_text.startswith("enc:v2:"):
        token = encrypted_text[7:].encode("ascii")
        return f.decrypt(token).decode("utf-8")

    # Formato legacy (intentar Fernet primero, luego fallback XOR para migración transparente)
    raw_payload = encrypted_text[4:]
    try:
        return f.decrypt(raw_payload.encode("ascii")).decode("utf-8")
    except (InvalidToken, Exception):
        # Fallback de compatibilidad retroactiva para tokens XOR generados previamente
        key_str = key_material or settings.APP_SECRET_KEY or "fallback-salt-key-material"
        legacy_key = hashlib.sha256(key_str.encode("utf-8")).digest()
        try:
            raw = base64.b64decode(raw_payload.encode("ascii"))
            decrypted = bytes(b ^ legacy_key[i % len(legacy_key)] for i, b in enumerate(raw))
            return decrypted.decode("utf-8")
        except Exception:
            return encrypted_text


def get_client_ip(request: Request) -> str:
    """Obtiene la IP real del cliente respetando cabeceras X-Forwarded-For detrás de proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # El primer valor es la IP real del cliente antes de cualquier proxy intermedio
        client_part = forwarded.split(",")[0].strip()
        if client_part:
            return client_part
    return request.client.host if request.client else "127.0.0.1"


def secure_cookies() -> bool:
    """Ambos nombres de producción requieren HTTPS; desarrollo y test permiten IP local."""
    return settings.ENVIRONMENT.lower() in {"production", "prod"}


def is_cacheable_asset(path: str) -> bool:
    return path.startswith("/static/") or path in {
        "/manifest.webmanifest",
        "/manifest.json",
        "/sw.js",
    }


def protect_response(request: Request, response: Response) -> None:
    """También normaliza CSRF creados por formularios que no definen Secure explícitamente."""
    if not is_cacheable_asset(request.url.path):
        response.headers["Cache-Control"] = "no-store, private"
    if secure_cookies():
        protected: list[tuple[bytes, bytes]] = []
        for name, value in response.raw_headers:
            if name.lower() == b"set-cookie":
                cookie = SimpleCookie()
                cookie.load(value.decode("latin-1"))
                for key in ("csrf_token", "session_id"):
                    if key in cookie:
                        cookie[key]["secure"] = True
                if cookie:
                    value = cookie.output(header="").strip().encode("latin-1")
            protected.append((name, value))
        response.raw_headers[:] = protected


def _purge_expired_attempts(now: datetime) -> None:
    """Purga todas las entradas vencidas del diccionario de intentos de login (L-05)."""
    cutoff = now - timedelta(minutes=WINDOW_MINUTES)
    for k in list(FAILED_LOGIN_ATTEMPTS.keys()):
        recent = [t for t in FAILED_LOGIN_ATTEMPTS[k] if t > cutoff]
        if recent:
            FAILED_LOGIN_ATTEMPTS[k] = recent
        else:
            FAILED_LOGIN_ATTEMPTS.pop(k, None)


def is_rate_limited(ip: str, username: str) -> bool:
    """Verifica si un usuario/IP excedió el límite de intentos fallidos (5 en 15 min)."""
    key = (ip, username.lower().strip())
    now = datetime.now(UTC)
    _purge_expired_attempts(now)
    recent_attempts = FAILED_LOGIN_ATTEMPTS.get(key, [])
    return len(recent_attempts) >= MAX_ATTEMPTS


def record_failed_attempt(ip: str, username: str) -> None:
    """Registra un intento de login fallido."""
    key = (ip, username.lower().strip())
    now = datetime.now(UTC)
    _purge_expired_attempts(now)
    recent_attempts = FAILED_LOGIN_ATTEMPTS.get(key, [])
    recent_attempts.append(now)
    FAILED_LOGIN_ATTEMPTS[key] = recent_attempts


def clear_failed_attempts(ip: str, username: str) -> None:
    """Limpia los intentos fallidos tras un login exitoso."""
    key = (ip, username.lower().strip())
    FAILED_LOGIN_ATTEMPTS.pop(key, None)


def generate_csrf_token() -> str:
    """Genera un token CSRF criptográficamente seguro."""
    return secrets.token_hex(24)


def validate_csrf(request: Request, form_csrf: str | None = None) -> bool:
    """Valida token CSRF tanto en formularios estándar como en HTMX (header X-CSRF-Token)."""
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token:
        return False

    header_token = request.headers.get("x-csrf-token") or request.headers.get("X-CSRF-Token")
    candidate_token = header_token or form_csrf

    if candidate_token is None:
        return False

    # compare_digest sobre str solo acepta ASCII; entradas Unicode deben fallar cerradas.
    if not cookie_token.isascii() or not candidate_token.isascii():
        return False

    return secrets.compare_digest(cookie_token, candidate_token)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Añade cabeceras de seguridad estrictas a todas las respuestas HTTP (sin 'unsafe-inline')."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        protect_response(request, response)
        headers = response.headers

        # CSP estricta: scripts asegurados exclusivamente con nonce por petición sin unsafe-inline;
        # estilos permiten 'self' y 'unsafe-inline' para preservar atributos de diseño HTML (Q08).
        csp_directives = [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ]
        headers["Content-Security-Policy"] = "; ".join(csp_directives)
        headers["X-Frame-Options"] = "DENY"
        headers["X-Content-Type-Options"] = "nosniff"
        headers["Referrer-Policy"] = "same-origin"
        headers["Permissions-Policy"] = "camera=(), geolocation=(), payment=()"
        if secure_cookies():
            headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"

        return response


class RequestBodyLimitMiddleware:
    """Acota los bytes reales antes de procesar multipart, sin confiar en Content-Length."""

    def __init__(self, app: ASGIApp, max_bytes: int = 2_100_000) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    announced = int(value)
                except ValueError:
                    response = JSONResponse({"detail": "Longitud del cuerpo inválida."}, 400)
                    await response(scope, receive, send)
                    return
                if announced > self.max_bytes or announced < 0:
                    await self._reject(scope, receive, send)
                    return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def buffered_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, buffered_receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": "La solicitud es demasiado grande; el CSV admite hasta 2 MB."}, 413
        )
        await response(scope, receive, send)
