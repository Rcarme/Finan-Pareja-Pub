"""Gestión de base de datos con SQLAlchemy 2.0 y guardias de seguridad (AGENTS.md regla 7).

N1 (F05.2): el engine es PEREZOSO y se reconstruye si cambia el entorno. Nunca se asume
en el import a qué base se apunta: `get_db()` aborta en modo test si el engine ligado no
apunta a `finanzas_test`, aunque `ENVIRONMENT` se haya fijado después del import.
"""

from collections.abc import Generator
from threading import Lock
from urllib.parse import urlparse

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

_ENGINE_LOCK = Lock()
_cached_engine: Engine | None = None
_cached_env_key: str | None = None


def verify_test_database_guard(url: str) -> None:
    """Verifica que la base de datos de test sea finanzas_test o SQLite en memoria."""
    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme == "sqlite":
        return
    db_name = parsed.path.lstrip("/")
    if db_name != "finanzas_test":
        msg = (
            "PROHIBIDO: Los tests solo pueden ejecutarse contra finanzas_test o SQLite. "
            f"Se detectó intento de conexión a base: '{db_name}'."
        )
        raise RuntimeError(msg)


def _resolve_db_url() -> str:
    """Resuelve la URL activa LEYENDO el entorno actual (no el del import)."""
    if settings.ENVIRONMENT == "test":
        url = settings.TEST_DATABASE_URL or settings.DATABASE_URL or "sqlite:///:memory:"
        verify_test_database_guard(url)
        return url
    return settings.DATABASE_URL


def _to_engine_url(db_url: str) -> str:
    """psycopg 3 driver para SQLAlchemy."""
    if db_url.startswith("postgresql://"):
        return db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return db_url


def get_engine() -> Engine:
    """Devuelve el engine ligado al entorno ACTUAL; lo reconstruye si cambió (N1)."""
    global _cached_engine, _cached_env_key
    key = f"{settings.ENVIRONMENT}|{_resolve_db_url()}"
    if _cached_engine is None or _cached_env_key != key:
        with _ENGINE_LOCK:
            key = f"{settings.ENVIRONMENT}|{_resolve_db_url()}"
            if _cached_engine is None or _cached_env_key != key:
                if _cached_engine is not None:
                    _cached_engine.dispose()
                raw_url = _resolve_db_url()
                engine_url = _to_engine_url(raw_url)
                if "sqlite" in engine_url:
                    from sqlalchemy.pool import StaticPool

                    _cached_engine = create_engine(
                        engine_url,
                        connect_args={"check_same_thread": False},
                        poolclass=StaticPool,
                    )
                else:
                    _cached_engine = create_engine(engine_url, pool_pre_ping=True)
                _cached_env_key = key
    assert _cached_engine is not None
    return _cached_engine


def reset_db_engine() -> None:
    """Descarta el engine cacheado (usado por tests al forzar el entorno)."""
    global _cached_engine, _cached_env_key
    with _ENGINE_LOCK:
        if _cached_engine is not None:
            _cached_engine.dispose()
            _cached_engine = None
            _cached_env_key = None


def get_session_factory() -> sessionmaker[Session]:
    """Fábrica de sesiones ligada al engine actual."""
    return sessionmaker(autocommit=False, autoflush=False, bind=get_engine())


def __getattr__(name: str) -> object:
    """Compatibilidad: `engine` y `SessionLocal` se resuelven perezosamente (N1)."""
    if name == "engine":
        return get_engine()
    if name == "SessionLocal":
        return get_session_factory()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_db() -> Generator[Session, None, None]:
    """Dependency para inyección de sesiones SQLAlchemy en rutas FastAPI."""
    if settings.ENVIRONMENT == "test":
        engine_url = get_engine().url
        is_sqlite = engine_url.drivername.startswith("sqlite")
        if not is_sqlite and engine_url.database != "finanzas_test":
            raise RuntimeError(
                "PROHIBIDO: get_db() en modo test con engine ligado a "
                f"'{engine_url.database}' (debe ser 'finanzas_test' o SQLite)."
            )
    db = get_session_factory()()
    try:
        yield db
    except Exception:
        pending_id = db.info.get("pending_operation")
        db.rollback()
        if pending_id is not None:
            from app.models.idempotency import IdempotencyKey
            from app.services.idempotency_service import abort_operation

            reservation = db.get(IdempotencyKey, pending_id)
            if reservation is not None:
                abort_operation(db, reservation)
        raise
    finally:
        db.close()


def check_db_connection() -> bool:
    """Verifica si la base de datos responde a una consulta básica."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
