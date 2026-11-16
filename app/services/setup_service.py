"""Servicio de inicialización y configuración inicial (Onboarding Wizard).

Permite que cualquier pareja configure el sistema desde el navegador en el primer arranque:
- Crea los dos usuarios con contraseñas seguras (Argon2id).
- Configura la moneda principal del hogar (ARS, EUR, USD, etc.).
- Configura el porcentaje de reparto de gastos compartidos.
- Configura opcionalmente la API Key de Google Gemini para el asistente contable.
- Configura el módulo opcional de negocio o actividad comercial independiente.
- Siembra categorías universales recomendadas y cuentas del fondo común.
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.auth import ph, validate_password_strength
from app.core.config import settings
from app.core.security import encrypt_secret
from app.models.auth import User
from app.models.close import Setting
from app.models.envelopes import Envelope
from app.models.ledger import Account, Category


def is_onboarded(db: Session) -> bool:
    """Indica si el sistema ya fue inicializado con al menos un usuario activo."""
    user_count = db.execute(select(func.count(User.id))).scalar() or 0
    return user_count > 0


def get_system_setting(db: Session, key: str, default: Any = None) -> Any:
    """Recupera una configuración del sistema."""
    setting = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
    if setting and setting.value is not None:
        if isinstance(setting.value, dict) and "val" in setting.value:
            return setting.value["val"]
        return setting.value
    return default


def set_system_setting(db: Session, key: str, value: Any, user_id: int | None = None) -> None:
    """Guarda o actualiza una configuración del sistema."""
    setting = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
    payload = value if isinstance(value, dict) else {"val": value}
    if setting is None:
        setting = Setting(key=key, value=payload, updated_by=user_id)
        db.add(setting)
    else:
        setting.value = payload
        setting.updated_by = user_id


def initialize_instance(
    db: Session,
    *,
    user1_username: str,
    user1_display_name: str,
    user1_password: str,
    user2_username: str,
    user2_display_name: str,
    user2_password: str,
    currency: str = "ARS",
    split_pct: int = 50,
    gemini_api_key: str | None = None,
    business_enabled: bool = False,
    business_name: str | None = None,
    seed_defaults: bool = True,
) -> tuple[User, User]:
    """Ejecuta la configuración inicial atómica del hogar."""
    # Advisory lock a nivel transacción en PostgreSQL para serializar concurrencia en /setup
    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(742198421)"))

    if is_onboarded(db):
        raise ValueError("El sistema ya fue configurado previamente.")

    # Validar split_pct
    if not (0 <= split_pct <= 100):
        raise ValueError("El porcentaje de reparto debe estar entre 0% y 100%.")

    # Validar usuarios
    u1_user = user1_username.strip().lower()
    u2_user = user2_username.strip().lower()
    if not u1_user or not u2_user:
        raise ValueError("Los nombres de usuario no pueden estar vacíos.")
    if u1_user == u2_user:
        raise ValueError("Los dos integrantes deben tener nombres de usuario distintos.")

    valid_pass1, msg1 = validate_password_strength(user1_password)
    if not valid_pass1:
        raise ValueError(f"Contraseña de {user1_display_name or u1_user}: {msg1}")

    valid_pass2, msg2 = validate_password_strength(user2_password)
    if not valid_pass2:
        raise ValueError(f"Contraseña de {user2_display_name or u2_user}: {msg2}")

    # Normalizar y validar moneda base soportada
    clean_currency = currency.strip().upper()
    if clean_currency != "ARS":
        raise ValueError(
            "Actualmente la moneda base del libro mayor doméstico es estrictamente ARS "
            "(con soporte bimonetario nativo para cuentas y sobres de ahorro en USD). "
            "La arquitectura multidivisa configurable está planificada en ROADMAP.md."
        )

    # 1. Crear los 2 usuarios
    user1 = User(
        username=u1_user,
        display_name=user1_display_name.strip() or u1_user.capitalize(),
        password_hash=ph.hash(user1_password),
        active=True,
        ui_mode="simple",
        nav_modules="home,list,spending,ai,new" + (",business" if business_enabled else ""),
    )
    user2 = User(
        username=u2_user,
        display_name=user2_display_name.strip() or u2_user.capitalize(),
        password_hash=ph.hash(user2_password),
        active=True,
        ui_mode="simple",
        nav_modules="home,list,spending,ai,new" + (",business" if business_enabled else ""),
    )
    db.add(user1)
    db.add(user2)
    db.flush()

    # 2. Guardar configuraciones del sistema
    set_system_setting(db, "app_currency", clean_currency, user1.id)
    set_system_setting(db, "split_pct", split_pct, user1.id)
    set_system_setting(
        db,
        "close_split",
        {"ahorro": 30, "colchon": 20, "repartir": 50},
        user1.id,
    )
    set_system_setting(db, "app_timezone", settings.APP_TIMEZONE, user1.id)

    b_name = (business_name or "Emprendimiento").strip() if business_enabled else ""
    set_system_setting(
        db,
        "business_config",
        {"enabled": business_enabled, "name": b_name},
        user1.id,
    )

    if gemini_api_key and gemini_api_key.strip():
        set_system_setting(
            db,
            "gemini_api_key",
            {"api_key": encrypt_secret(gemini_api_key.strip())},
            user1.id,
        )

    # 3. Sembrar datos sugeridos si se solicitó
    if seed_defaults:
        # Cuentas iniciales
        accounts_to_create = [
            ("Banco compartido", "bank", Decimal("0.00"), clean_currency),
            ("Efectivo", "cash", Decimal("0.00"), clean_currency),
        ]
        for name, acct_type, balance, curr in accounts_to_create:
            db.add(
                Account(
                    name=name, type=acct_type, opening_balance=balance, currency=curr, active=True
                )
            )

        # Sobres de sistema
        envelopes_to_create = [
            ("Ahorro común", "savings", True),
            ("Colchón de emergencia", "buffer", True),
            ("Compromisos e Inversiones", "provision", True),
        ]
        for env_name, env_type, is_sys in envelopes_to_create:
            db.add(Envelope(name=env_name, type=env_type, is_system=is_sys, active=True))

        # Categorías universales
        categories_to_create: list[tuple[str, str, int, int | None, bool, str]] = [
            ("Sueldo / Ingreso principal", "income", 1, None, True, "household"),
            ("Ingresos extra / Secundarios", "income", 0, None, True, "household"),
            ("Supermercado y Alimentos", "expense", 0, None, True, "household"),
            ("Servicios del Hogar", "expense", 0, None, True, "household"),
            ("Transporte y Combustible", "expense", 0, None, True, "household"),
            ("Salud y Farmacia", "expense", 0, None, True, "household"),
            ("Ocio, Salidas y Vacaciones", "expense", 0, None, True, "household"),
            ("Mascotas", "expense", 0, None, True, "household"),
            ("Regalos y Celebraciones", "expense", 0, None, True, "household"),
            ("Mantenimiento y Equipamiento", "expense", 0, None, True, "household"),
            ("Compromiso mensual / Cuota fija", "expense", 0, None, True, "household"),
            (
                f"Gastos personales de {user1.display_name}",
                "expense",
                0,
                user1.id,
                True,
                "personal",
            ),
            (
                f"Gastos personales de {user2.display_name}",
                "expense",
                0,
                user2.id,
                True,
                "personal",
            ),
        ]

        if business_enabled and b_name:
            categories_to_create.extend(
                [
                    (f"Ingresos {b_name}", "income", 0, None, True, "business"),
                    (f"Gastos e Insumos {b_name}", "expense", 0, None, True, "business"),
                    (f"Retiro de {b_name}", "income", 0, None, True, "household"),
                ]
            )

        for cat_name, kind, offset, charged_to, is_sys, activity in categories_to_create:
            db.add(
                Category(
                    name=cat_name,
                    kind=kind,
                    activity=activity,
                    default_period_offset=offset,
                    default_charged_to_user_id=charged_to,
                    is_system=is_sys,
                    active=True,
                )
            )

    db.commit()
    return user1, user2
