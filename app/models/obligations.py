"""Modelos de obligaciones y cuotas/refuerzos del departamento (RN-27 a RN-31)."""

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BigIntId, TimestampMixin


class Obligation(Base, TimestampMixin):
    """Obligación financiera (ej. cuota del departamento en USD)."""

    __tablename__ = "obligations"

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    monthly_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    items: Mapped[list["ObligationItem"]] = relationship(
        "ObligationItem", back_populates="obligation"
    )


class ObligationItem(Base, TimestampMixin):
    """Vencimiento mensual de cuota o refuerzo en USD (RN-27)."""

    __tablename__ = "obligation_items"
    __table_args__ = (
        UniqueConstraint(
            "obligation_id", "due_period", "kind", name="uq_obligation_due_period_kind"
        ),
    )

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    obligation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("obligations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    due_period: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # cuota, refuerzo
    usd_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="pendiente", nullable=False
    )  # pendiente, pagado

    transaction_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=True
    )

    obligation: Mapped["Obligation"] = relationship("Obligation", back_populates="items")
