"""Modelos de sobres y movimientos de sobres (RN-12 a RN-17, RN-45)."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BigIntId, OptimisticLockMixin, TimestampMixin


class Envelope(Base, TimestampMixin, OptimisticLockMixin):
    """Sobre virtual dentro del fondo (ahorro, meta, provisión, colchón)."""

    __tablename__ = "envelopes"

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # savings, goal, provision, buffer
    target_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    usd_reference: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    movements: Mapped[list["EnvelopeMovement"]] = relationship(
        "EnvelopeMovement", back_populates="envelope"
    )


class EnvelopeMovement(Base):
    """Movimiento de un sobre (asignación +, liberación -, gasto -)."""

    __tablename__ = "envelope_movements"
    __table_args__ = (
        CheckConstraint("amount > 0", name="chk_envelope_movement_amount_positive"),
        CheckConstraint(
            "kind IN ('allocate', 'release', 'spend')",
            name="chk_envelope_movement_kind_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'voided')",
            name="chk_envelope_movement_status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    envelope_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("envelopes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    period: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    transaction_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=True
    )
    close_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("closes.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    envelope: Mapped["Envelope"] = relationship("Envelope", back_populates="movements")
