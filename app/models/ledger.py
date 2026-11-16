"""Modelos contables: cuentas, categorías y transacciones (RN-01 a RN-11, RN-40)."""

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from app.domain.emoji_catalog import EMOJI_ICONS
from app.domain.ui_catalog import ICONS
from app.models.base import Base, BigIntId, OptimisticLockMixin, TimestampMixin

CATEGORY_ICON_CONSTRAINT = (
    "icon IN (" + ",".join("'" + icon + "'" for icon in sorted(set(ICONS) | EMOJI_ICONS)) + ")"
)


class Account(Base, TimestampMixin, OptimisticLockMixin):
    """Cuenta del fondo común (banco, efectivo, billetera)."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # bank, cash, wallet
    opening_balance: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0.00"), nullable=False
    )
    opening_period: Mapped[dt.date] = mapped_column(
        Date,
        default=lambda: dt.date(1970, 1, 1),
        server_default="1970-01-01",
        nullable=False,
    )
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    currency: Mapped[str] = mapped_column(
        String(3), default="ARS", server_default="ARS", nullable=False
    )
    is_virtual: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    __table_args__ = (CheckConstraint("length(currency) = 3", name="chk_account_currency"),)


class Category(Base, TimestampMixin, OptimisticLockMixin):
    """Categoría de ingresos y gastos con cargo personal por defecto (RN-40, RN-41)."""

    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint(
            "merged_into_id IS NULL OR (merged_into_id <> id AND NOT active)",
            name="chk_category_merge_inactive",
        ),
        CheckConstraint(
            "activity IN ('household', 'business', 'personal')", name="chk_category_activity"
        ),
        CheckConstraint(
            CATEGORY_ICON_CONSTRAINT,
            name="chk_category_icon",
        ),
        CheckConstraint(
            "color IN ('blue','green','amber','rose','violet','teal','slate')",
            name="chk_category_color",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # income, expense
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True
    )
    default_charged_to_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    merged_into_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True
    )
    activity: Mapped[str] = mapped_column(
        String(20), default="household", server_default="household", nullable=False
    )
    default_period_offset: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )  # 0 o 1 (ej. sueldo)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    icon: Mapped[str] = mapped_column(
        String(24), default="receipt", server_default="receipt", nullable=False
    )
    color: Mapped[str] = mapped_column(
        String(16), default="slate", server_default="slate", nullable=False
    )


class Transaction(Base, TimestampMixin, OptimisticLockMixin):
    """Movimiento contable (ingreso, gasto, transferencia, retiro)."""

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, object]:  # noqa: N805
        return {"version_id_col": cls.version, "version_id_generator": False}

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(
            "activity IN ('household', 'business', 'personal')", name="chk_transaction_activity"
        ),
        CheckConstraint(
            "NOT internal_distribution OR kind = 'distribution'",
            name="chk_internal_distribution_kind",
        ),
        CheckConstraint(
            "NOT opening_omission OR kind = 'expense'", name="chk_opening_omission_expense"
        ),
        Index(
            "uq_active_rent_collection_period",
            "period",
            unique=True,
            postgresql_where=text("generated_by = 'rent_collection' AND status = 'active'"),
        ),
        CheckConstraint("amount > 0", name="chk_transaction_amount_positive"),
        CheckConstraint(
            "kind IN ('income', 'expense', 'transfer', 'distribution')",
            name="chk_transaction_kind_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'voided')",
            name="chk_transaction_status_valid",
        ),
        CheckConstraint(
            "(kind != 'transfer') OR (to_account_id IS NOT NULL AND to_account_id <> account_id)",
            name="chk_transfer_accounts_different",
        ),
        CheckConstraint(
            "(kind != 'distribution') OR (recipient_user_id IS NOT NULL)",
            name="chk_distribution_requires_recipient",
        ),
        CheckConstraint(
            "(status != 'voided') OR "
            "(void_reason IS NOT NULL AND voided_by IS NOT NULL AND voided_at IS NOT NULL)",
            name="chk_voided_requires_audit_data",
        ),
        CheckConstraint(
            "(kind NOT IN ('income', 'expense')) OR (category_id IS NOT NULL)",
            name="chk_transaction_category_required",
        ),
        CheckConstraint(
            "date >= '2000-01-01' AND date <= '2100-12-31'",
            name="chk_transaction_date_range",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    activity: Mapped[str] = mapped_column(
        String(20), default="household", server_default="household", nullable=False
    )
    internal_distribution: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    opening_omission: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    period: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)  # Día 1 del mes
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    to_account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=True
    )
    category_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    envelope_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("envelopes.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    charged_to_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    generated_by: Mapped[str] = mapped_column(String(50), default="user", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(100), unique=True, nullable=True, index=True
    )
    recipient_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)

    usd_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    fx_rate_effective: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    voided_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    voided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    account: Mapped["Account"] = relationship("Account", foreign_keys=[account_id])
    to_account: Mapped["Account | None"] = relationship("Account", foreign_keys=[to_account_id])
    category: Mapped["Category | None"] = relationship("Category")
