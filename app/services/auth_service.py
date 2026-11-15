"""RN-37: cambios de credenciales y revocaciones auditados en una única transacción."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.auth import (
    hash_password,
    hash_token,
    revoke_session,
    revoke_user_sessions,
    validate_password_strength,
    verify_password,
)
from app.core.transactions import atomic_service, commit_service
from app.models.auth import SessionModel, User


@atomic_service
def change_password(
    db: Session,
    user_id: int,
    current_password: str,
    new_password: str,
    confirm_password: str,
    keep_raw_token: str | None = None,
) -> None:
    """Valida bajo bloqueo; nunca guarda contraseñas ni hashes en la auditoría."""
    user = db.scalar(
        select(User)
        .where(User.id == user_id)
        # No cambia la clave: permitir las FK de auditoría de un logout simultáneo.
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise ValueError("El usuario ya no está disponible.")
    if not verify_password(current_password, user.password_hash):
        raise ValueError("La contraseña actual es incorrecta.")
    if new_password != confirm_password:
        raise ValueError("La nueva contraseña y su confirmación no coinciden.")
    valid, reason = validate_password_strength(new_password)
    if not valid:
        raise ValueError(reason)
    user.password_hash = hash_password(new_password)
    revoke_user_sessions(db, user_id, keep_raw_token, commit=False)
    record_audit(
        db=db, entity="users", entity_id=user_id, action="password_change", user_id=user_id
    )
    commit_service(db)


@atomic_service
def logout(db: Session, raw_token: str) -> None:
    """La revocación y su evento persisten juntos, incluso sin usuario en request.state."""
    session = db.scalar(
        select(SessionModel)
        .where(SessionModel.id == hash_token(raw_token))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if session is None:
        return
    revoke_session(db, raw_token, commit=False)
    record_audit(
        db=db, entity="users", entity_id=session.user_id, action="logout", user_id=session.user_id
    )
    commit_service(db)
