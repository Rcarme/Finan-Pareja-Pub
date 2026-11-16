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
