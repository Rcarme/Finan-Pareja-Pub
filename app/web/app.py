"""Aplicación principal FastAPI con middlewares de seguridad y autenticación (ARCHITECTURE.md)."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core.config import settings
from app.core.db import check_db_connection
from app.core.formatters import format_currency, format_date, format_decimal_ar, format_fx_ar
from app.core.middleware import AuthenticationMiddleware
from app.core.security import RequestBodyLimitMiddleware, SecurityHeadersMiddleware
from app.domain.ui_catalog import ICONS, RECOMMENDED_EMOJIS
from app.web.ai_routes import ai_router
from app.web.audit_routes import audit_router
from app.web.auth_routes import auth_router
from app.web.business_routes import business_router
from app.web.card_routes import card_router
from app.web.close_routes import close_router
from app.web.envelope_routes import envelope_router
from app.web.forecast_routes import forecast_router
from app.web.ledger_routes import ledger_router
from app.web.obligation_routes import obligation_router
from app.web.ownership_routes import ownership_router
from app.web.pwa_routes import pwa_router
from app.web.recurring_routes import recurring_router
from app.web.rent_routes import rent_router
from app.web.report_routes import report_router
from app.web.retirement_routes import retirement_router
from app.web.settings_routes import settings_router
from app.web.setup_routes import setup_router
from app.web.simple_routes import simple_router
from app.web.spending_routes import spending_router


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """La tarea automática nunca consulta la red ni escribe durante tests."""
    task: asyncio.Task[None] | None = None
    if settings.FX_AUTOMATIC_ENABLED and settings.ENVIRONMENT != "test":
        from app.core.fx_worker import run_fx_worker

        task = asyncio.create_task(run_fx_worker())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="Tandem",
    description="Gestión compartida de finanzas en pareja",
    version="1.0.0",
    lifespan=lifespan,
)


logger = logging.getLogger("app.web")


@app.exception_handler(ValueError)
async def invalid_input_handler(request: Request, exc: ValueError) -> JSONResponse:
    """Validaciones fuera de formularios específicos siguen siendo errores recuperables."""
    logger.warning("ValueError en %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# Configuración de templates Jinja2
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Registrar filtros Jinja2 personalizados para formato es-AR
templates.env.filters["currency"] = format_currency
templates.env.filters["format_currency"] = format_currency
templates.env.filters["decimal_ar"] = format_decimal_ar
templates.env.filters["format_decimal_ar"] = format_decimal_ar
templates.env.filters["fx_ar"] = format_fx_ar
templates.env.filters["format_fx_ar"] = format_fx_ar
templates.env.filters["date"] = format_date


def _new_form_idempotency_token() -> str:
    from app.services.idempotency_service import new_idempotency_token

    return new_idempotency_token()


def _get_csp_nonce(request: Any = None) -> str:
    if request and hasattr(request, "state"):
        return str(getattr(request.state, "csp_nonce", ""))
    return ""


templates.env.globals["idem_token"] = _new_form_idempotency_token
templates.env.globals["csp_nonce"] = _get_csp_nonce
templates.env.globals["svg_icons"] = ICONS
templates.env.globals["recommended_emojis"] = RECOMMENDED_EMOJIS

# Archivos estáticos
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Middlewares de seguridad y autenticación
app.add_middleware(AuthenticationMiddleware)
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

# Rutas
app.include_router(setup_router)
app.include_router(auth_router)
app.include_router(card_router)
app.include_router(ledger_router)
app.include_router(retirement_router)
app.include_router(envelope_router)
app.include_router(forecast_router)
app.include_router(obligation_router)
app.include_router(close_router)
app.include_router(rent_router)
app.include_router(recurring_router)
app.include_router(report_router)
app.include_router(audit_router)
app.include_router(settings_router)
app.include_router(spending_router)
app.include_router(ai_router)
app.include_router(simple_router)
app.include_router(pwa_router)
app.include_router(business_router)
app.include_router(ownership_router)


@app.get("/healthz", response_model=None)
def healthz(request: Request) -> dict[str, Any] | JSONResponse:
    """Endpoint de salud para monitorización y healthcheck (RN-38, ARCHITECTURE.md §5, §6).

    Provee un chequeo mínimo sin revelar metadata a visitantes anónimos; detalla
    el estado de backup y entorno únicamente a usuarios autenticados mediante sesión activa.
    """
    db_ok = check_db_connection()
    is_authenticated = bool(getattr(request.state, "user", None))

    # Para monitoreo anónimo / Docker probes: respuesta mínima sin fuga de información interna
    if not is_authenticated:
        payload = {"status": "ok" if db_ok else "degraded"}
        if not db_ok:
            return JSONResponse(status_code=503, content=payload)
        return payload

    backup_info: dict[str, Any] = {
        "status": "unknown",
        "hours_since_backup": None,
        "is_verified": False,
        "alert": None,
    }
    if db_ok:
        from app.core.db import get_session_factory
        from app.services.backup_service import get_backup_health

        db = get_session_factory()()
        try:
            health = get_backup_health(db)
            backup_info = {
                "status": health.status,
                "hours_since_backup": health.hours_since_backup,
                "is_verified": health.is_verified,
                "alert": health.alert_message,
            }
        except Exception:
            backup_info["status"] = "error"
        finally:
            db.close()

    detailed_payload = {
        "status": "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "backup": backup_info,
        "environment": settings.ENVIRONMENT,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if not db_ok:
        return JSONResponse(status_code=503, content=detailed_payload)
    return detailed_payload
