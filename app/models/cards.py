"""Resúmenes normalizados y reglas locales por tarjeta/comercio."""

from typing import Any

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntId, TimestampMixin


class CardStatement(Base, TimestampMixin):
    __tablename__ = "card_statements"
    __table_args__ = (
        UniqueConstraint("account_id", "source_hash", name="uq_card_statement_source"),
    )

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rows: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="preview")


class MerchantRule(Base, TimestampMixin):
    __tablename__ = "merchant_rules"
    __table_args__ = (UniqueConstraint("account_id", "merchant", name="uq_merchant_rule_account"),)

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    merchant: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    charged_to_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
