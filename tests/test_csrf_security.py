"""Tests exhaustivos para la protección CSRF en formularios y peticiones HTTP."""

from starlette.requests import Request
from starlette.testclient import TestClient

from app.core.security import generate_csrf_token, validate_csrf
from app.web.app import app


def _make_dummy_request(cookie_token: str | None = None) -> Request:
    headers = []
    if cookie_token:
        headers.append((b"cookie", f"csrf_token={cookie_token}".encode("ascii")))
    scope = {
        "type": "http",
        "method": "POST",
        "headers": headers,
    }
    return Request(scope)


def test_csrf_token_generation():
    token1 = generate_csrf_token()
    token2 = generate_csrf_token()
    assert len(token1) == 48
    assert len(token2) == 48
    assert token1 != token2


def test_csrf_validation_pure():
    token = generate_csrf_token()
    req = _make_dummy_request(cookie_token=token)

    # Coincidencia exacta: válido
    assert validate_csrf(req, token) is True

    # Token de formulario ausente o vacío: inválido
    assert validate_csrf(req, None) is False
    assert validate_csrf(req, "") is False

    # Cookie ausente: inválido
    req_no_cookie = _make_dummy_request(cookie_token=None)
    assert validate_csrf(req_no_cookie, token) is False

    # Token alterado o no coincidente: inválido
    assert validate_csrf(req, "invalid_token_1234567890abcdef1234567890abcdef") is False


def test_csrf_endpoint_rejection_without_token():
    from app.core.db import get_engine, get_session_factory
    from app.models.auth import User
    from app.models.base import Base

    Base.metadata.create_all(bind=get_engine())
    client = TestClient(app)

    # Limpiar estado previo para asegurar que no esté onboarded antes de probar /setup
    session_init = get_session_factory()()
    try:
        session_init.query(User).delete()
        session_init.commit()
    finally:
        session_init.close()

    # 1. Petición POST a /setup sin token CSRF -> rechazada con status 400 y mensaje de seguridad
    resp_setup = client.post(
        "/setup",
        data={
            "csrf_token": "token_invalido",
            "user1_username": "alex",
            "user1_display_name": "Alex",
            "user1_password": "Password123456!",
            "user2_username": "sam",
            "user2_display_name": "Sam",
            "user2_password": "Password654321!",
            "currency": "EUR",
            "split_pct": 50,
        },
        follow_redirects=False,
    )
    assert resp_setup.status_code == 400
    assert "token de seguridad" in resp_setup.text.lower()

    # 2. Con usuarios ya existentes en BD, petición POST a /login sin token CSRF -> 403
    session = get_session_factory()()
    try:
        session.add(
            User(
                username="alex",
                display_name="Alex",
                password_hash="dummy_hash_for_testing",
                active=True,
                ui_mode="simple",
            )
        )
        session.commit()
    finally:
        session.close()

    resp_login = client.post(
        "/login",
        data={"username": "alex", "password": "Password123456!"},
        follow_redirects=False,
    )
    assert resp_login.status_code == 403
    assert "csrf" in resp_login.json()["detail"].lower()


KNOWN_POST_ENDPOINTS = [
    "/setup",
    "/login",
    "/logout",
    "/change-password",
    "/cards/preview",
    "/transactions",
    "/reconciliation",
    "/envelopes/new-goal",
    "/obligations/fx-rate",
    "/obligations/generate-schedule",
    "/obligations/items",
    "/close/execute",
    "/rent/contract",
    "/rent/collection",
    "/rent/adjustment",
    "/recurring/templates",
    "/recurring/generate",
    "/settings/accounts",
    "/settings/categories",
    "/settings/close-split",
    "/settings/buffer-target",
    "/settings/preferred-expense-account",
    "/settings/gemini-api-key",
    "/s/ai/parse",
    "/s/ai/confirm",
    "/s/new",
    "/s/settings/mode",
    "/s/settings/navigation",
    "/settings/ownership",
    "/settings/ownership/convert",
    "/settings/ownership/adjust",
]


def test_all_post_endpoints_require_csrf_or_auth():
    """Verifica exhaustivamente que ninguna ruta POST se ejecute sin CSRF o autenticación."""
    from app.core.db import get_engine
    from app.models.base import Base

    Base.metadata.create_all(bind=get_engine())
    client = TestClient(app)
    post_routes: set[str] = set(KNOWN_POST_ENDPOINTS)

    def _collect_routes(routes):
        for route in routes:
            raw_methods = getattr(route, "methods", None) or []
            methods = {str(m).upper() for m in raw_methods}
            path = getattr(route, "path", "")
            if "POST" in methods and path:
                post_routes.add(path)
            sub_routes = getattr(route, "routes", None) or getattr(
                getattr(route, "app", None), "routes", None
            )
            if sub_routes:
                _collect_routes(sub_routes)

    if hasattr(app, "routes"):
        _collect_routes(app.routes)
    if hasattr(app, "router") and hasattr(app.router, "routes"):
        _collect_routes(app.router.routes)

    assert len(post_routes) > 0, "Deben existir rutas POST registradas en la aplicación"

    for path in sorted(post_routes):
        # Enviar petición POST sin cookie ni token CSRF ni sesión
        # Ignorar rutas con parámetros variables de URL como {id}
        if "{" in path:
            continue
        resp = client.post(path, data={}, follow_redirects=False)
        # Debe ser rechazada o redirigida a login/setup, nunca procesada con 200 OK
        assert resp.status_code in (302, 303, 400, 401, 403, 422), (
            f"Ruta {path} vulnerable: permitió POST sin token ni sesión (HTTP {resp.status_code})"
        )
