"""Protecciones de seguridad: CSRF, rate limiting y cabeceras HTTP (ARCHITECTURE.md § 4)."""

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

# Estructura en memoria para rate limiting:
# - (ip, username) -> list[datetime] (límite específico por IP/usuario)
# - username -> list[tuple[str, datetime]] (límite distribuido multi-IP contra fuerza bruta masiva)
FAILED_LOGIN_ATTEMPTS: dict[tuple[str, str], list[datetime]] = {}
FAILED_USERNAME_ATTEMPTS: dict[str, list[tuple[str, datetime]]] = {}
FAILED_SETUP_ATTEMPTS: dict[str, list[datetime]] = {}
MAX_ATTEMPTS = 5
MAX_GLOBAL_USER_ATTEMPTS = 25
MAX_SETUP_ATTEMPTS = 5
MIN_DISTINCT_IPS_FOR_GLOBAL_BLOCK = 3
WINDOW_MINUTES = 15


def _derive_fernet_key(key_material: str | None = None) -> bytes:
    """Deriva una clave de 32 bytes para Fernet mediante HKDF-SHA256 con salt e info."""
    key_str = key_material or settings.APP_SECRET_KEY
    if not key_str:
        if settings.ENVIRONMENT.lower() in {"production", "prod"}:
            raise RuntimeError("APP_SECRET_KEY no configurado en entorno de producción.")
        key_str = "insecure-dev-fallback-key-material-32-chars-min"
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"finan-pareja-fernet-salt-v1",
        info=b"finan-pareja-secret-encryption",
    )
    derived = hkdf.derive(key_str.encode("utf-8"))
    return base64.urlsafe_b64encode(derived)


def _derive_legacy_sha256_fernet_key(key_material: str | None = None) -> bytes:
    """Derivación legacy SHA-256 para compatibilidad hacia atrás con secretos v2 previos."""
    key_str = (
        key_material or settings.APP_SECRET_KEY or "insecure-dev-fallback-key-material-32-chars-min"
    )
    digest = hashlib.sha256(key_str.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(secret_text: str, key_material: str | None = None) -> str:
    """Cifra un secreto mediante Fernet (AES-128-CBC + HMAC-SHA256 autenticado con HKDF)."""
    if not secret_text:
        return ""
    f = Fernet(_derive_fernet_key(key_material))
    token = f.encrypt(secret_text.encode("utf-8")).decode("ascii")
    return "enc:v2:" + token


def decrypt_secret(encrypted_text: str, key_material: str | None = None) -> str:
    """Descifra un texto cifrado con encrypt_secret, con soporte de HKDF, legacy Fernet y XOR."""
    if not encrypted_text or not encrypted_text.startswith("enc:"):
        return encrypted_text

    # 1. Intentar con la clave primaria derivada por HKDF
    f_primary = Fernet(_derive_fernet_key(key_material))

    if encrypted_text.startswith("enc:v2:"):
        token = encrypted_text[7:].encode("ascii")
        try:
            return f_primary.decrypt(token).decode("utf-8")
        except (InvalidToken, ValueError):
            # Fallback con clave legacy SHA256 directa si fue cifrado antes de migración HKDF
            try:
                f_legacy = Fernet(_derive_legacy_sha256_fernet_key(key_material))
                return f_legacy.decrypt(token).decode("utf-8")
            except (InvalidToken, ValueError):
                return ""

    # Formato legacy sin prefijo v2 (intentar Fernet primero, luego fallback XOR transparente)
    raw_payload = encrypted_text[4:]
    try:
        return f_primary.decrypt(raw_payload.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        pass

    try:
        f_legacy = Fernet(_derive_legacy_sha256_fernet_key(key_material))
        return f_legacy.decrypt(raw_payload.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        pass

    # Fallback de compatibilidad retroactiva para tokens XOR generados previamente
    key_str = key_material or settings.APP_SECRET_KEY or "fallback-salt-key-material"
    legacy_key = hashlib.sha256(key_str.encode("utf-8")).digest()
    try:
        raw = base64.b64decode(raw_payload.encode("ascii"))
        decrypted = bytes(b ^ legacy_key[i % len(legacy_key)] for i, b in enumerate(raw))
        return decrypted.decode("utf-8")
    except Exception:
        return ""


def get_client_ip(request: Request) -> str:
    """Retorna la IP del cliente resuelta confiablemente por el servidor ASGI."""
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
    """Purga todas las entradas vencidas de los diccionarios de rate limiting (L-05)."""
    cutoff = now - timedelta(minutes=WINDOW_MINUTES)
    for k in list(FAILED_LOGIN_ATTEMPTS.keys()):
        recent = [t for t in FAILED_LOGIN_ATTEMPTS[k] if t > cutoff]
        if recent:
            FAILED_LOGIN_ATTEMPTS[k] = recent
        else:
            FAILED_LOGIN_ATTEMPTS.pop(k, None)

    for user_key in list(FAILED_USERNAME_ATTEMPTS.keys()):
        recent_user = [
            (att_ip, t) for att_ip, t in FAILED_USERNAME_ATTEMPTS[user_key] if t > cutoff
        ]
        if recent_user:
            FAILED_USERNAME_ATTEMPTS[user_key] = recent_user
        else:
            FAILED_USERNAME_ATTEMPTS.pop(user_key, None)


def is_rate_limited(ip: str, username: str) -> bool:
    """Verifica si un usuario/IP excedió el límite de intentos por IP o distribuido multi-IP."""
    user_key = username.lower().strip()
    key = (ip, user_key)
    now = datetime.now(UTC)
    _purge_expired_attempts(now)
    recent_ip_user = FAILED_LOGIN_ATTEMPTS.get(key, [])
    if len(recent_ip_user) >= MAX_ATTEMPTS:
        return True
    recent_global_user = FAILED_USERNAME_ATTEMPTS.get(user_key, [])
    if len(recent_global_user) >= MAX_GLOBAL_USER_ATTEMPTS:
        distinct_ips = {att_ip for att_ip, _ in recent_global_user}
        if len(distinct_ips) >= MIN_DISTINCT_IPS_FOR_GLOBAL_BLOCK:
            return True
    return False


def record_failed_attempt(ip: str, username: str) -> None:
    """Registra un intento de login fallido tanto por IP+usuario como global por usuario."""
    user_key = username.lower().strip()
    key = (ip, user_key)
    now = datetime.now(UTC)
    _purge_expired_attempts(now)

    recent_attempts = FAILED_LOGIN_ATTEMPTS.get(key, [])
    recent_attempts.append(now)
    FAILED_LOGIN_ATTEMPTS[key] = recent_attempts

    recent_user_attempts = FAILED_USERNAME_ATTEMPTS.get(user_key, [])
    recent_user_attempts.append((ip, now))
    FAILED_USERNAME_ATTEMPTS[user_key] = recent_user_attempts


def clear_failed_attempts(ip: str, username: str) -> None:
    """Limpia los intentos fallidos tras un login exitoso."""
    user_key = username.lower().strip()
    key = (ip, user_key)
    FAILED_LOGIN_ATTEMPTS.pop(key, None)
    FAILED_USERNAME_ATTEMPTS.pop(user_key, None)


def is_setup_rate_limited(ip: str) -> bool:
    """Verifica si una IP excedió el límite de intentos fallidos en /setup (5 en 15 min)."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=WINDOW_MINUTES)
    attempts = [t for t in FAILED_SETUP_ATTEMPTS.get(ip, []) if t > cutoff]
    if attempts:
        FAILED_SETUP_ATTEMPTS[ip] = attempts
    else:
        FAILED_SETUP_ATTEMPTS.pop(ip, None)
    return len(attempts) >= MAX_SETUP_ATTEMPTS


def record_failed_setup_attempt(ip: str) -> None:
    """Registra un intento fallido de autorización en /setup."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=WINDOW_MINUTES)
    attempts = [t for t in FAILED_SETUP_ATTEMPTS.get(ip, []) if t > cutoff]
    attempts.append(now)
    FAILED_SETUP_ATTEMPTS[ip] = attempts


def clear_failed_setup_attempts(ip: str) -> None:
    """Limpia los intentos de setup tras una inicialización exitosa."""
    FAILED_SETUP_ATTEMPTS.pop(ip, None)


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
