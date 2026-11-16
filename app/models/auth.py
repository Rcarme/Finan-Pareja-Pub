"""Modelos para usuarios y sesiones de autenticación (ARCHITECTURE.md)."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BigIntId, TimestampMixin


class User(Base, TimestampMixin):
    """Usuario del sistema (pareja de dos usuarios con iguales permisos)."""

    __tablename__ = "users"
    __table_args__ = (CheckConstraint("ui_mode IN ('simple', 'full')", name="chk_user_ui_mode"),)

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ui_mode: Mapped[str] = mapped_column(
        String(10), default="full", server_default="full", nullable=False
    )
    nav_modules: Mapped[str] = mapped_column(
        String(255),
        default="home,list,spending,ai,new,business",
        server_default="home,list,spending,ai,new,business",
        nullable=False,
    )

    def has_nav(self, module_name: str) -> bool:
        """Indica si el usuario tiene habilitado el módulo en su navegación."""
        mods = [m.strip().lower() for m in (self.nav_modules or "").split(",") if m.strip()]
        return module_name.lower() in mods

    sessions: Mapped[list["SessionModel"]] = relationship(
        "SessionModel", back_populates="user", cascade="all, delete-orphan"
    )


class SessionModel(Base):
    """Sesiones de usuario server-side con revocación (ARCHITECTURE.md § 4)."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Token hasheado (SHA-256)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="sessions")
