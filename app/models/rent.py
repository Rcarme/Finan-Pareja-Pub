"""Modelos para contrato de alquiler y registro de ajustes manuales por IPC (RN-32 a RN-34)."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BigIntId, TimestampMixin


class RentContract(Base, TimestampMixin):
    """Contrato de alquiler cobrado (ingreso del fondo común)."""

    __tablename__ = "rent_contract"
    __table_args__ = (CheckConstraint("current_amount > 0", name="chk_rent_amount_positive"),)

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    current_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    adjust_every_months: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    last_adjust_date: Mapped[date] = mapped_column(Date, nullable=False)
    next_adjust_date: Mapped[date] = mapped_column(Date, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    adjustments: Mapped[list["RentAdjustment"]] = relationship(
        "RentAdjustment", back_populates="contract"
    )


class RentAdjustment(Base):
    """Historial inmutable de ajustes de alquiler cargados manualmente (RN-33)."""

    __tablename__ = "rent_adjustments"
    __table_args__ = (
        CheckConstraint("old_amount > 0", name="chk_rent_old_amount_positive"),
        CheckConstraint("new_amount > 0", name="chk_rent_new_amount_positive"),
    )

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("rent_contract.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    old_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    new_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    ipc_pct: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    contract: Mapped["RentContract"] = relationship("RentContract", back_populates="adjustments")
