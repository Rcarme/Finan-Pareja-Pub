"""Modelos de períodos, cierres de mes, arrastres y configuración (RN-20 a RN-26)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BigIntId, TimestampMixin


class Period(Base, TimestampMixin):
    """Período mensual (día 1 del mes). Estado open o closed (RN-11, RN-20)."""

    __tablename__ = "periods"

    period: Mapped[date] = mapped_column(Date, primary_key=True)  # YYYY-MM-01
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    close_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class Close(Base):
    """Registro histórico de cálculo y resultado de cierre de mes (RN-24)."""

    __tablename__ = "closes"

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    period: Mapped[date] = mapped_column(
        Date, ForeignKey("periods.period", ondelete="RESTRICT"), nullable=False, index=True
    )
    repartible_bruto: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    parte: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    retenido: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(100), unique=True, nullable=True, index=True
    )
    reverted_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reverted_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    period_rel: Mapped["Period"] = relationship("Period")


class Carryover(Base):
    """Arrastre de cargos personales no cubiertos al cierre (RN-22, RN-42)."""

    __tablename__ = "carryovers"
    __table_args__ = (
        CheckConstraint("amount > 0", name="chk_carryover_amount_positive"),
        CheckConstraint("status IN ('active', 'reverted')", name="chk_carryover_status"),
    )

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    from_period: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    applied_in_period: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Setting(Base):
    """Configuración del sistema en formato clave-valor JSONB (RN-23)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )


class FxRate(Base):
    """Cotización de referencia del dólar oficial con historial (RN-29)."""

    __tablename__ = "fx_rates"
    __table_args__ = (
        CheckConstraint("rate > 0", name="chk_fx_rate_positive"),
        Index("uq_fx_rates_source_publication", "source", "published_at", unique=True),
    )

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    source: Mapped[str] = mapped_column(
        String(50), default="manual", server_default="manual", nullable=False
    )
    buy: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    sell: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
