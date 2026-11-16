import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Asegurar que el root del proyecto esté en el path de Python
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.models  # noqa: F401 - Registra todos los modelos SQLAlchemy en Base.metadata
from app.core.config import settings
from app.core.db import _to_engine_url
from app.models.base import Base

# Configuración de Alembic
config = context.config

# Logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata de los modelos para autogeneración y migraciones
target_metadata = Base.metadata


def get_url() -> str:
    """Resuelve la URL de conexión a la base de datos desde settings o alembic.ini."""
    cli_url = config.get_main_option("sqlalchemy.url")
    if cli_url:
        return _to_engine_url(cli_url)
    env_url = os.environ.get("DATABASE_URL") or settings.DATABASE_URL
    return _to_engine_url(env_url)


def run_migrations_offline() -> None:
    """Ejecuta migraciones en modo 'offline' (sin conexión activa)."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Ejecuta migraciones en modo 'online' (conectado al motor de base de datos)."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
