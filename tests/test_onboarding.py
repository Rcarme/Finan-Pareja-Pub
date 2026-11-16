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
        currency="EUR",
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
    assert get_system_setting(db_session, "app_currency") == "EUR"
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
        assert acc.currency == "EUR"

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
    # Moneda inválida (no alfabética o longitud distinta a 3)
    with pytest.raises(ValueError, match="moneda debe ser un código alfabético de 3 letras"):
        initialize_instance(
            db_session,
            user1_username="alex",
            user1_display_name="Alex",
            user1_password="Password123456!",
            user2_username="sam",
            user2_display_name="Sam",
            user2_password="Password654321!",
            currency="PESOS",
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
            currency="USD",
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
            currency="USD",
        )
