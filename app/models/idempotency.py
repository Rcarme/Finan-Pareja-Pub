"""Registro genérico de claves de idempotencia para formularios (F05.2 N7).

Cada fila reserva un token único por ruta y referencia al resultado creado,
para que un doble envío (doble clic / doble POST) produzca un solo efecto.
Lo reutilizará el cierre de mes en F06.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntId


class IdempotencyKey(Base):
    """Reserva de token de idempotencia con referencia al resultado (N7)."""

    __tablename__ = "idempotency_keys"

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    route: Mapped[str] = mapped_column(String(100), nullable=False)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    result_entity: Mapped[str | None] = mapped_column(String(50), nullable=True)
    result_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_after_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
