"""Historial externo inmutable; nunca representa deuda, intereses ni una reserva líquida."""

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntId, TimestampMixin


class RetirementContribution(Base, TimestampMixin):
    __tablename__ = "retirement_contributions"
    __table_args__ = (
        CheckConstraint("length(policy) >= 2", name="chk_retirement_policy"),
        CheckConstraint("amount > 0", name="chk_retirement_amount_positive"),
    )
    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    policy: Mapped[str] = mapped_column(String(20), nullable=False)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    source_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    source_transaction_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=True
    )
    reference: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    created_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
