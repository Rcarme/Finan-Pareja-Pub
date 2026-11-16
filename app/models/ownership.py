"""Append-only personal allocations; corrections use signed reversals (F18)."""

from datetime import date
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, Date, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntId, TimestampMixin


class OwnershipEntry(Base, TimestampMixin):
    __tablename__ = "ownership_entries"
    __table_args__ = (
        CheckConstraint("currency IN ('ARS', 'USD')", name="chk_ownership_currency_valid"),
    )
    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=True
    )
    bucket: Mapped[str] = mapped_column(
        String(12), default="personal", server_default="personal", nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="ARS")
    period: Mapped[date] = mapped_column(Date, nullable=False)
    close_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("closes.id", ondelete="RESTRICT"), nullable=True
    )
    reversal_of: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("ownership_entries.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
