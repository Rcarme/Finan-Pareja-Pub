"""RN-38: límites persistentes de login sobre auditoría append-only existente."""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.auth import create_session
from app.core.db import acquire_advisory_lock
from app.core.transactions import atomic_service, commit_service
from app.models.audit import AuditLog

PAIR_LIMIT = 5
IP_LIMIT = 20
WINDOW_MINUTES = 15


def _username_hash(username: str) -> str:
    return hashlib.sha256(username.strip().casefold().encode("utf-8")).hexdigest()


def _lock_ip(db: Session, ip: str) -> None:
    # Serializar por IP evita que solicitudes paralelas atraviesen juntas el límite.
    lock_key = int.from_bytes(hashlib.sha256(("login:" + ip).encode()).digest()[:8], signed=True)
    acquire_advisory_lock(db, lock_key, shared=False)


def is_login_rate_limited(
    db: Session, ip: str, username: str, *, now: datetime | None = None
) -> bool:
    """El lock se mantiene hasta registrar el resultado o cerrar la sesión de BD."""
    _lock_ip(db, ip)
    cutoff = (now or datetime.now(UTC)) - timedelta(minutes=WINDOW_MINUTES)
    failures = (
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.ip == ip,
            AuditLog.action == "login_failed",
            AuditLog.at > cutoff,
        )
    )

    username_hash = _username_hash(username)
    # 1. Comprobar bloqueo específico usuario + IP (PAIR_LIMIT = 5)
    last_success = db.scalar(
        select(AuditLog.id)
        .where(
            AuditLog.ip == ip,
            AuditLog.action == "login",
            AuditLog.after["username_hash"].astext == username_hash,
        )
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    pair = failures.where(AuditLog.after["username_hash"].astext == username_hash)
    if last_success is not None:
        pair = pair.where(AuditLog.id > last_success)
    if (db.scalar(pair) or 0) >= PAIR_LIMIT:
        return True

    # 2. Política anti-lockout para IP compartida / proxy:
    # Bloquear a nivel IP solo ante ataque de spraying (>= 3 cuentas distintas) o masivo (>= 100)
    total_ip_failures = db.scalar(failures) or 0
    if total_ip_failures >= IP_LIMIT:
        distinct_targeted_users = (
            db.scalar(
                select(func.count(func.distinct(AuditLog.after["username_hash"].astext))).where(
                    AuditLog.ip == ip,
                    AuditLog.action == "login_failed",
                    AuditLog.at > cutoff,
                )
            )
            or 0
        )
        if distinct_targeted_users >= 3 or total_ip_failures >= 100:
            return True

    return False


@atomic_service
def record_login_failure(db: Session, ip: str, username: str) -> None:
    """No guarda el intento de contraseña ni nombres de usuarios desconocidos."""
    _lock_ip(db, ip)
    record_audit(
        db=db,
        entity="users",
        entity_id=0,
        action="login_failed",
        after={"username_hash": _username_hash(username)},
        ip=ip,
    )
    commit_service(db)


def _record_success(db: Session, ip: str, username: str, user_id: int) -> None:
    record_audit(
        db=db,
        entity="users",
        entity_id=user_id,
        action="login",
        user_id=user_id,
        after={"username": username, "username_hash": _username_hash(username)},
        ip=ip,
    )


@atomic_service
def record_login_success(db: Session, ip: str, username: str, user_id: int) -> None:
    """El éxito reinicia solo el contador de esa identidad; conserva el global por IP."""
    _lock_ip(db, ip)
    _record_success(db, ip, username, user_id)
    commit_service(db)


@atomic_service
def complete_login(
    db: Session, ip: str, username: str, user_id: int, user_agent: str | None = None
) -> str:
    """La sesión y su auditoría se confirman juntas, conservando el lock por IP."""
    _lock_ip(db, ip)
    raw_token, _ = create_session(db, user_id, user_agent, commit=False)
    _record_success(db, ip, username, user_id)
    commit_service(db)
    return raw_token
