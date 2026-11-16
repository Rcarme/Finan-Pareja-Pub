"""Servicio de consulta y filtrado de auditoría (RN-37, SPEC.md §6).

Permite listar y filtrar registros inmutables de audit_log por usuario,
entidad, acción y rango temporal, con soporte de paginado.
"""

from datetime import UTC, date, datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import resolve_audit_action_values, resolve_audit_entity_values
from app.models.audit import AuditLog


def get_audit_logs_filtered(
    db: Session,
    user_id: int | None = None,
    entity: str | None = None,
    action: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    """Retorna registros de auditoría que coinciden con los filtros y el conteo total."""
    stmt = select(AuditLog)
    count_stmt = select(func.count(AuditLog.id))

    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
        count_stmt = count_stmt.where(AuditLog.user_id == user_id)

    if entity is not None and entity.strip():
        entity_values = resolve_audit_entity_values(entity)
        stmt = stmt.where(AuditLog.entity.in_(entity_values))
        count_stmt = count_stmt.where(AuditLog.entity.in_(entity_values))

    if action is not None and action.strip():
        action_values = resolve_audit_action_values(action)
        stmt = stmt.where(AuditLog.action.in_(action_values))
        count_stmt = count_stmt.where(AuditLog.action.in_(action_values))

    if from_date is not None:
        dt_from = datetime.combine(from_date, time.min, tzinfo=UTC)
        stmt = stmt.where(AuditLog.at >= dt_from)
        count_stmt = count_stmt.where(AuditLog.at >= dt_from)

    if to_date is not None:
        dt_to = datetime.combine(to_date, time.max, tzinfo=UTC)
        stmt = stmt.where(AuditLog.at <= dt_to)
        count_stmt = count_stmt.where(AuditLog.at <= dt_to)

    total: int = db.execute(count_stmt).scalar() or 0

    stmt = stmt.order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(limit).offset(offset)
    logs = list(db.execute(stmt).scalars().all())

    return logs, total
