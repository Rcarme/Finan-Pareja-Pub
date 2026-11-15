"""Base declarativa y helpers comunes para modelos SQLAlchemy (ARCHITECTURE.md)."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Entero de 64 bits compatible con PostgreSQL (BIGINT) y SQLite (INTEGER para rowid)
BigIntId = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    """Base declarativa para todos los modelos del proyecto."""

    pass


class TimestampMixin:
    """Mixin para tablas con trazabilidad de creación y actualización."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OptimisticLockMixin:
    """Mixin para control de concurrencia optimista con columna version (RN-10)."""

    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
    )
