"""Gestión de autenticación, contraseñas Argon2id y sesiones server-side (ARCHITECTURE.md § 4)."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import SessionModel, User

ph = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=1)
SESSION_DURATION_DAYS = 30
SESSION_DURATION_SECONDS = SESSION_DURATION_DAYS * 86400


def hash_password(password: str) -> str:
    """Hashea una contraseña utilizando Argon2id."""
    return ph.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica si una contraseña en texto plano coincide con su hash Argon2id."""
    try:
        return ph.verify(hashed_password, plain_password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def validate_password_strength(password: str) -> tuple[bool, str]:
    """Valida la política de contraseñas: mínimo 12 caracteres (ARCHITECTURE.md § 4)."""
    if len(password) < 12:
        return False, "La contraseña debe tener al menos 12 caracteres."
    return True, ""


def hash_token(raw_token: str) -> str:
    """Calcula el hash SHA-256 de un token de sesión antes de persistirlo."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(
    db: Session,
    user_id: int,
    user_agent: str | None = None,
    duration_days: int = SESSION_DURATION_DAYS,
    *,
    commit: bool = True,
) -> tuple[str, SessionModel]:
    """Crea una sesión server-side con duración de 30 días deslizantes."""
    raw_token = secrets.token_hex(32)
    token_id = hash_token(raw_token)
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=duration_days)

    session_record = SessionModel(
        id=token_id,
        user_id=user_id,
        expires_at=expires_at,
        last_seen=now,
        user_agent=user_agent,
        revoked=False,
    )
    db.add(session_record)
    if commit:
        db.commit()
        db.refresh(session_record)
    else:
        db.flush()
    return raw_token, session_record


def get_active_session(db: Session, raw_token: str) -> tuple[SessionModel, User] | None:
    """Obtiene y desliza una sesión activa, verificando que no esté revocada ni expirada."""
    token_id = hash_token(raw_token)
    now = datetime.now(UTC)

    stmt = (
        select(SessionModel, User)
        .join(User, SessionModel.user_id == User.id)
        .where(
            SessionModel.id == token_id,
            SessionModel.revoked == False,  # noqa: E712
            SessionModel.expires_at > now,
            User.active == True,  # noqa: E712
        )
    )
    result = db.execute(stmt).first()
    if not result:
        return None

    session_record, user = result

    # La revocación se consulta siempre; la renovación se agrupa para evitar escrituras por asset.
    last_seen = session_record.last_seen
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)

    if last_seen is None or last_seen <= now - timedelta(minutes=5):
        session_record.last_seen = now
        session_record.expires_at = now + timedelta(days=SESSION_DURATION_DAYS)
        db.commit()
        db.refresh(session_record)
        db.refresh(user)

    return session_record, user


def revoke_session(db: Session, raw_token: str, *, commit: bool = True) -> bool:
    """Revoca inmediatamente una sesión server-side."""
    token_id = hash_token(raw_token)
    stmt = select(SessionModel).where(SessionModel.id == token_id)
    session_record = db.execute(stmt).scalar_one_or_none()
    if session_record:
        session_record.revoked = True
        if commit:
            db.commit()
        return True
    return False


def revoke_user_sessions(
    db: Session, user_id: int, keep_raw_token: str | None = None, *, commit: bool = True
) -> int:
    """Revoca todas las sesiones del usuario, preservando la sesión actual si se indica (H-18)."""
    stmt = select(SessionModel).where(
        SessionModel.user_id == user_id,
        SessionModel.revoked == False,  # noqa: E712
    )
    if keep_raw_token:
        keep_id = hash_token(keep_raw_token)
        stmt = stmt.where(SessionModel.id != keep_id)

    sessions = list(db.execute(stmt).scalars().all())
    for s in sessions:
        s.revoked = True
    if sessions and commit:
        db.commit()
    return len(sessions)
