"""Tests unitarios para seguridad, sesiones y cifrado de secretos."""

from app.core.auth import hash_password, hash_token, validate_password_strength, verify_password
from app.core.security import (
    clear_failed_attempts,
    decrypt_secret,
    encrypt_secret,
    is_rate_limited,
    record_failed_attempt,
)


def test_password_strength_validation():
    valid, msg = validate_password_strength("short")
    assert not valid
    assert "12 caracteres" in msg

    valid, msg = validate_password_strength("validpassword123!")
    assert valid
    assert msg == ""


def test_password_hashing_and_verification():
    password = "SuperSecretPassword123!"
    hashed = hash_password(password)
    assert hashed != password
    assert verify_password(password, hashed)
    assert not verify_password("WrongPassword123!", hashed)


def test_session_token_hashing():
    raw_token = "a" * 64
    hashed = hash_token(raw_token)
    assert len(hashed) == 64
    assert hashed == hash_token(raw_token)


def test_secret_encryption_and_decryption():
    secret = "dummy-sample-secret-api-key"
    key_material = "master-app-secret-key-32-chars!!"

    encrypted = encrypt_secret(secret, key_material)
    assert encrypted != secret
    assert encrypted.startswith("enc:")

    decrypted = decrypt_secret(encrypted, key_material)
    assert decrypted == secret

    # Texto vacío o ya descifrado
    assert encrypt_secret("") == ""
    assert decrypt_secret("plain-text") == "plain-text"

    # Rotación de clave: token cifrado con clave anterior no debe provocar error 500 / InvalidToken
    rotated_key_material = "new-different-master-key-after-rotation"
    assert decrypt_secret(encrypted, rotated_key_material) == ""


def test_rate_limiting():
    ip = "192.168.1.100"
    username = "testuser"

    clear_failed_attempts(ip, username)
    assert not is_rate_limited(ip, username)

    for _ in range(5):
        record_failed_attempt(ip, username)

    assert is_rate_limited(ip, username)

    clear_failed_attempts(ip, username)
    assert not is_rate_limited(ip, username)


def test_distributed_rate_limiting_by_username():
    """Verifica que un ataque distribuido (múltiples IPs contra el mismo usuario) sea bloqueado."""
    username = "victim_user"

    # Limpiar intentos previos
    clear_failed_attempts("1.1.1.1", username)
    assert not is_rate_limited("1.1.1.1", username)

    # Simular 25 intentos desde 25 IPs distintas (1 intento por IP)
    for i in range(25):
        diff_ip = f"192.168.1.{i + 1}"
        record_failed_attempt(diff_ip, username)

    # Ahora cualquier IP nueva debe ser bloqueada para este usuario
    assert is_rate_limited("10.0.0.1", username)

    # Tras un login exitoso, se limpia el bloqueo global
    clear_failed_attempts("10.0.0.1", username)
    assert not is_rate_limited("10.0.0.1", username)


def test_healthz_anonymous_vs_authenticated():
    """Verifica que /healthz solo detalle backups y ambiente si hay una sesión activa."""
    from starlette.testclient import TestClient

    from app.core.auth import create_session
    from app.core.db import get_engine, get_session_factory
    from app.models.auth import User
    from app.models.base import Base
    from app.web.app import app

    Base.metadata.create_all(bind=get_engine())
    session = get_session_factory()()
    try:
        user = session.query(User).filter(User.username == "health_test_user").first()
        if not user:
            user = User(
                username="health_test_user",
                display_name="Health Tester",
                password_hash="dummy_hash_for_testing",
                active=True,
                ui_mode="simple",
            )
            session.add(user)
            session.commit()
            session.refresh(user)

        raw_token, _ = create_session(session, user.id, "pytest-agent")
    finally:
        session.close()

    client = TestClient(app)

    # 1. Anónimo: solo status mínimo
    resp_anon = client.get("/healthz")
    assert resp_anon.status_code == 200
    data_anon = resp_anon.json()
    assert data_anon == {"status": "ok"}
    assert "environment" not in data_anon
    assert "backup" not in data_anon

    # 2. Con sesión activa: reporte diagnóstico completo
    client.cookies.set("session_id", raw_token)
    resp_auth = client.get("/healthz")
    assert resp_auth.status_code == 200
    data_auth = resp_auth.json()
    assert data_auth["status"] == "ok"
    assert "environment" in data_auth
    assert "backup" in data_auth
    assert data_auth["database"] == "connected"


def test_login_rate_limiting_anti_lockout_for_shared_ip():
    """Verifica que un usuario bloqueado por 5 fallos no bloquee a otros usuarios del hogar."""
    from app.core.db import get_engine, get_session_factory
    from app.models.base import Base
    from app.services.auth_attempt_service import is_login_rate_limited, record_login_failure

    Base.metadata.create_all(bind=get_engine())
    session = get_session_factory()()
    shared_ip = "192.168.100.42"

    try:
        # Registrar 5 fallos para 'alex' desde shared_ip
        for _ in range(5):
            record_login_failure(session, shared_ip, "alex")

        # 'alex' debe estar bloqueado en shared_ip
        assert is_login_rate_limited(session, shared_ip, "alex") is True

        # 'sam' en esa misma IP compartida NO debe ser bloqueado (anti-lockout del hogar)
        assert is_login_rate_limited(session, shared_ip, "sam") is False
    finally:
        session.close()
