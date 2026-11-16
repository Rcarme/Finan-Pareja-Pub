"""Lógica de dominio pura para estado y verificación de backups (F10, ARCHITECTURE.md §6)."""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class BackupHealth:
    """Estado de salud inmutable del subsistema de backups."""

    status: str  # 'ok', 'warning', 'critical', 'unknown'
    hours_since_backup: int | None
    hours_since_verification: int | None
    is_verified: bool
    alert_message: str | None
    is_fresh: bool


def evaluate_backup_health(
    last_backup_at: datetime | None,
    last_verified_at: datetime | None,
    last_verification_status: str | None,
    current_time: datetime,
    remote_status: str | None = None,
) -> BackupHealth:
    """Evalúa la frescura e integridad de los backups sin I/O ni floats.

    Reglas de negocio (ARCHITECTURE.md §6):
    - Sin backup registrado -> 'critical'.
    - Si la última verificación falló -> 'critical'.
    - Si pasaron más de 36 h desde el último backup -> 'warning'.
    - Si el backup es <= 36 h y la verificación fue exitosa ('ok') -> 'ok'.
    """
    if last_backup_at is None:
        return BackupHealth(
            status="critical",
            hours_since_backup=None,
            hours_since_verification=None,
            is_verified=False,
            alert_message=(
                "No se registran backups en el sistema. Configure el contenedor de backup."
            ),
            is_fresh=False,
        )

    # Cálculo exacto de horas transcurridas usando división entera (sin floats)
    seconds_since_backup = int((current_time - last_backup_at).total_seconds())
    hours_since_backup = max(0, seconds_since_backup // 3600)

    hours_since_verification = None
    if last_verified_at is not None:
        seconds_since_verif = int((current_time - last_verified_at).total_seconds())
        hours_since_verification = max(0, seconds_since_verif // 3600)

    # 1. Chequeo de verificación fallida
    if last_verification_status == "failed":
        return BackupHealth(
            status="critical",
            hours_since_backup=hours_since_backup,
            hours_since_verification=hours_since_verification,
            is_verified=False,
            alert_message=(
                "La última verificación de integridad del backup falló. Revise los registros."
            ),
            is_fresh=False,
        )

    if remote_status in ("failed", "skipped"):
        return BackupHealth(
            status="critical" if remote_status == "failed" else "warning",
            hours_since_backup=hours_since_backup,
            hours_since_verification=hours_since_verification,
            is_verified=False,
            alert_message="La copia remota falló o no está configurada. Solo hay respaldo local.",
            is_fresh=current_time - last_backup_at <= timedelta(hours=36),
        )

    # 2. Chequeo de umbral de 36 horas
    verification_is_fresh = last_verified_at is not None and timedelta(
        0
    ) <= current_time - last_verified_at <= timedelta(days=8)
    if current_time - last_backup_at > timedelta(hours=36):
        return BackupHealth(
            status="warning",
            hours_since_backup=hours_since_backup,
            hours_since_verification=hours_since_verification,
            is_verified=last_verification_status == "ok" and verification_is_fresh,
            alert_message=(
                f"El último backup tiene {hours_since_backup} horas (mayor a 36 h requeridas)."
            ),
            is_fresh=False,
        )

    if last_verification_status == "ok" and not verification_is_fresh:
        return BackupHealth(
            status="warning",
            hours_since_backup=hours_since_backup,
            hours_since_verification=hours_since_verification,
            is_verified=False,
            alert_message="La verificación de restauración venció (máximo 8 días).",
            is_fresh=True,
        )

    # 3. Estado saludable
    is_verified = last_verification_status == "ok" and verification_is_fresh
    return BackupHealth(
        status="ok",
        hours_since_backup=hours_since_backup,
        hours_since_verification=hours_since_verification,
        is_verified=is_verified,
        alert_message=None,
        is_fresh=True,
    )
