"""Modelo para auditoría inmutable append-only (RN-37, ARCHITECTURE.md § 3)."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntId


class AuditLog(Base):
    """Registro de auditoría inmutable de todas las mutaciones del sistema.

    Regla RN-37: Todo alta/edición/anulación/cierre/reapertura/cambio de configuración
    escribe una fila en audit_log. La tabla es append-only: la BD rechaza UPDATE y DELETE.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    entity: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    action: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # create, update, void, close, reopen
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
