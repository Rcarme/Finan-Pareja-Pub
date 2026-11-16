"""Modelos para plantillas y pendientes recurrentes mensuales (RN-35)."""

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BigIntId, TimestampMixin


class RecurringTemplate(Base, TimestampMixin):
    """Plantilla de gasto o ingreso periódico (genera pendientes al inicio del mes)."""

    __tablename__ = "recurring_templates"
    __table_args__ = (
        CheckConstraint("day_of_month >= 1 AND day_of_month <= 31", name="chk_recurring_day_valid"),
        CheckConstraint("estimated_amount > 0", name="chk_recurring_amount_positive"),
        CheckConstraint("kind IN ('income', 'expense')", name="chk_recurring_template_kind_valid"),
    )

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    category_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    day_of_month: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    charged_to_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class RecurringPending(Base, TimestampMixin):
    """Movimiento recurrente pendiente del mes (debe ser confirmado o descartado por el usuario)."""

    __tablename__ = "recurring_pending"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'dismissed')",
            name="chk_recurring_pending_status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recurring_templates.id", ondelete="RESTRICT"), nullable=False
    )
    period: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False
    )  # pending, confirmed, dismissed
    transaction_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=True
    )

    template: Mapped["RecurringTemplate"] = relationship("RecurringTemplate")
