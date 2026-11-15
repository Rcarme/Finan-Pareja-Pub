"""Servicio para auditoría del estado de backups (F10, ARCHITECTURE.md §6)."""

from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.domain.backup import BackupHealth, evaluate_backup_health
from app.models.close import Setting

SETTING_KEY = "backup_metadata"


def get_backup_metadata(db: Session) -> dict[str, Any]:
    """Obtiene el diccionario con metadatos del último backup y verificación."""
    setting = db.get(Setting, SETTING_KEY)
    if setting is None or not isinstance(setting.value, dict):
        return {}
    return dict(setting.value)


def record_backup_run(
    db: Session,
    run_at: datetime,
    dump_path: str,
    size_bytes: int,
    encrypted: bool = True,
    destination: str = "drive",
    user_id: int | None = None,
    remote_status: str | None = None,
) -> dict[str, Any]:
    """Registra la ejecución exitosa de un backup diario con auditoría (RN-37)."""
    setting = db.get(Setting, SETTING_KEY)
    before = setting.value if setting else None

    current_data = dict(setting.value) if setting and isinstance(setting.value, dict) else {}
    current_data.update(
        {
            "last_backup_at": run_at.isoformat(),
            "dump_path": dump_path,
            "size_bytes": size_bytes,
            "encrypted": encrypted,
            "destination": destination,
            "remote_status": remote_status,
        }
    )

    if setting is None:
        setting = Setting(key=SETTING_KEY, value=current_data, updated_by=user_id)
        db.add(setting)
        action = "INSERT"
    else:
        setting.value = current_data
        setting.updated_by = user_id
        action = "UPDATE"

    db.flush()
    record_audit(
        db=db,
        entity="Setting",
        entity_id=1,
        action=action,
        before={"key": SETTING_KEY, "value": before} if before else None,
        after={"key": SETTING_KEY, "value": current_data},
        user_id=user_id,
    )
    db.commit()
    return current_data


def record_backup_verification(
    db: Session,
    verified_at: datetime,
    status: str,  # 'ok' | 'failed'
    details: dict[str, Any] | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Registra el resultado de la prueba periódica de restauración en base temporal."""
    setting = db.get(Setting, SETTING_KEY)
    before = setting.value if setting else None

    current_data = dict(setting.value) if setting and isinstance(setting.value, dict) else {}
    current_data.update(
        {
            "last_verified_at": verified_at.isoformat(),
            "last_verification_status": status,
            "verification_details": details or {},
        }
    )

    if setting is None:
        setting = Setting(key=SETTING_KEY, value=current_data, updated_by=user_id)
        db.add(setting)
        action = "INSERT"
    else:
        setting.value = current_data
        setting.updated_by = user_id
        action = "UPDATE"

    db.flush()
    record_audit(
        db=db,
        entity="Setting",
        entity_id=1,
        action=action,
        before={"key": SETTING_KEY, "value": before} if before else None,
        after={"key": SETTING_KEY, "value": current_data},
        user_id=user_id,
    )
    db.commit()
    return current_data


def get_backup_health(db: Session, current_time: datetime | None = None) -> BackupHealth:
    """Evalúa la salud actual de los backups contra las reglas de ARCHITECTURE.md §6."""
    now = current_time or datetime.now(UTC)
    meta = get_backup_metadata(db)

    last_backup_str = meta.get("last_backup_at")
    last_verified_str = meta.get("last_verified_at")
    verif_status = meta.get("last_verification_status")

    last_backup_at: datetime | None = None
    if last_backup_str:
        with suppress(ValueError):
            last_backup_at = datetime.fromisoformat(last_backup_str)

    last_verified_at: datetime | None = None
    if last_verified_str:
        with suppress(ValueError):
            last_verified_at = datetime.fromisoformat(last_verified_str)

    return evaluate_backup_health(
        last_backup_at=last_backup_at,
        last_verified_at=last_verified_at,
        last_verification_status=verif_status,
        current_time=now,
        remote_status=meta.get("remote_status"),
    )
