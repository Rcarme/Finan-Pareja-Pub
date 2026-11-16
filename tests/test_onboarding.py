"""Tests unitarios para el servicio de Onboarding y configuración inicial."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.security import decrypt_secret
from app.models.base import Base
from app.models.ledger import Account, Category
from app.services.setup_service import get_system_setting, initialize_instance, is_onboarded


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def test_onboarding_lifecycle(db_session):
    # Inicialmente la base de datos está vacía
    assert not is_onboarded(db_session)

    # Inicializar hogar
    user1, user2 = initialize_instance(
        db_session,
        user1_username="alex",
        user1_display_name="Alex",
        user1_password="Password123456!",
        user2_username="sam",
        user2_display_name="Sam",
        user2_password="Password654321!",
        currency="ARS",
        split_pct=50,
        gemini_api_key="test-dummy-api-key-gemini",
        business_enabled=True,
        business_name="Estudio Creativo",
        seed_defaults=True,
    )

    assert user1.id is not None
    assert user2.id is not None
    assert user1.username == "alex"
    assert user2.username == "sam"
    assert is_onboarded(db_session)

    # Verificar settings guardados
    assert get_system_setting(db_session, "app_currency") == "ARS"
    assert get_system_setting(db_session, "split_pct") == 50

    business_cfg = get_system_setting(db_session, "business_config")
    assert business_cfg["enabled"] is True
    assert business_cfg["name"] == "Estudio Creativo"

    gemini_cfg = get_system_setting(db_session, "gemini_api_key")
    assert gemini_cfg["api_key"].startswith("enc:")
    assert decrypt_secret(gemini_cfg["api_key"]) == "test-dummy-api-key-gemini"

    # Verificar que se crearon cuentas y categorías
    accounts = db_session.query(Account).all()
    assert len(accounts) >= 2
    for acc in accounts:
        assert acc.currency == "ARS"

    categories = db_session.query(Category).all()
    assert len(categories) > 10

    # No se puede reinicializar una instancia ya configurada
    with pytest.raises(ValueError, match="El sistema ya fue configurado previamente"):
        initialize_instance(
            db_session,
            user1_username="otro1",
            user1_display_name="Otro 1",
            user1_password="Password123456!",
            user2_username="otro2",
            user2_display_name="Otro 2",
            user2_password="Password123456!",
        )


def test_onboarding_validation_rules(db_session):
    # Moneda no soportada como base
    with pytest.raises(
        ValueError, match="moneda base del libro mayor doméstico es estrictamente ARS"
    ):
        initialize_instance(
            db_session,
            user1_username="alex",
            user1_display_name="Alex",
            user1_password="Password123456!",
            user2_username="sam",
            user2_display_name="Sam",
            user2_password="Password654321!",
            currency="EUR",
        )

    # Porcentaje de reparto fuera de rango
    with pytest.raises(ValueError, match="reparto debe estar entre 0"):
        initialize_instance(
            db_session,
            user1_username="alex",
            user1_display_name="Alex",
            user1_password="Password123456!",
            user2_username="sam",
            user2_display_name="Sam",
            user2_password="Password654321!",
            currency="ARS",
            split_pct=150,
        )

    # Contraseña corta (< 12 caracteres)
    with pytest.raises(ValueError, match="al menos 12 caracteres"):
        initialize_instance(
            db_session,
            user1_username="alex",
            user1_display_name="Alex",
            user1_password="corta",
            user2_username="sam",
            user2_display_name="Sam",
            user2_password="Password654321!",
            currency="ARS",
        )


def test_setup_token_protection():
    """Verifica que _is_setup_token_valid valide correctamente contra SETUP_TOKEN."""
    from app.core.config import settings
    from app.web.setup_routes import _is_setup_token_valid

    original_token = settings.SETUP_TOKEN
    original_env = settings.ENVIRONMENT
    try:
        # 1. En desarrollo sin SETUP_TOKEN, cualquier valor (o None) es válido
        settings.ENVIRONMENT = "development"
        settings.SETUP_TOKEN = None
        assert _is_setup_token_valid(None) is True
        assert _is_setup_token_valid("cualquier-token") is True

        # 2. En producción sin SETUP_TOKEN, siempre es inválido (bloquea onboarding no protegido)
        settings.ENVIRONMENT = "production"
        settings.SETUP_TOKEN = None
        assert _is_setup_token_valid(None) is False
        assert _is_setup_token_valid("intento") is False

        # 3. Con SETUP_TOKEN configurado, requiere coincidencia exacta de tiempo constante
        settings.SETUP_TOKEN = "secret-installation-token-12345"
        assert _is_setup_token_valid(None) is False
        assert _is_setup_token_valid("") is False
        assert _is_setup_token_valid("token_incorrecto") is False
        assert _is_setup_token_valid("secret-installation-token-12345") is True
        assert _is_setup_token_valid("  secret-installation-token-12345  ") is True
    finally:
        settings.SETUP_TOKEN = original_token
        settings.ENVIRONMENT = original_env


def test_setup_rate_limiting():
    """Verifica que /setup bloquee tras 5 intentos fallidos consecutivos de token."""
    from app.core.security import (
        clear_failed_setup_attempts,
        is_setup_rate_limited,
        record_failed_setup_attempt,
    )

    test_ip = "192.168.10.50"
    clear_failed_setup_attempts(test_ip)
    assert not is_setup_rate_limited(test_ip)

    for _ in range(5):
        record_failed_setup_attempt(test_ip)

    assert is_setup_rate_limited(test_ip)
    clear_failed_setup_attempts(test_ip)
    assert not is_setup_rate_limited(test_ip)


def test_setup_csrf_cookie_set_on_errors():
    """Verifica que /setup siempre establezca la cookie csrf_token coincidente
    en respuestas de error.
    """
    from starlette.testclient import TestClient

    from app.core.config import settings
    from app.core.db import get_engine
    from app.core.middleware import invalidate_onboarding_cache
    from app.models.base import Base
    from app.web.app import app

    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    invalidate_onboarding_cache()

    client = TestClient(app)

    # 1. GET /setup debe retornar 200 y establecer cookie csrf_token
    resp_get = client.get("/setup")
    assert resp_get.status_code == 200
    assert "csrf_token" in resp_get.cookies
    initial_csrf = resp_get.cookies["csrf_token"]
    assert len(initial_csrf) > 10

    # 2. POST /setup con token de instalación incorrecto (403) debe renovar y setear cookie
    original_token = settings.SETUP_TOKEN
    settings.SETUP_TOKEN = "real-setup-token"
    try:
        resp_fail = client.post(
            "/setup",
            data={
                "csrf_token": initial_csrf,
                "setup_token": "wrong-token",
                "user1_username": "alex",
                "user1_password": "Password123456!",
                "user2_username": "sam",
                "user2_password": "Password654321!",
            },
            cookies={"csrf_token": initial_csrf},
            follow_redirects=False,
        )
        assert resp_fail.status_code == 403
        assert "csrf_token" in resp_fail.cookies
        # La cookie devuelta debe coincidir con el token del formulario renderizado
        new_cookie_token = resp_fail.cookies["csrf_token"]
        assert f'value="{new_cookie_token}"' in resp_fail.text
    finally:
        settings.SETUP_TOKEN = original_token
        invalidate_onboarding_cache()
