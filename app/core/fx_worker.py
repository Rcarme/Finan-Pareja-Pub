"""RN-29: tarea periódica con sesión propia, cancelable y desactivada en tests."""

import asyncio
import logging

from app.core.db import get_session_factory
from app.services.fx_service import refresh_official_rate

logger = logging.getLogger(__name__)


def refresh_once() -> None:
    """Sesión exclusiva de la tarea; una falla no interrumpe la aplicación."""
    with get_session_factory()() as db:
        try:
            refresh_official_rate(db)
        except Exception:
            db.rollback()
            logger.exception("Falló la actualización automática del dólar oficial.")


async def run_fx_worker() -> None:
    """Consulta al arrancar y luego cada hora sin bloquear el event loop."""
    while True:
        await asyncio.to_thread(refresh_once)
        await asyncio.sleep(3600)
