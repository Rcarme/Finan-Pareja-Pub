"""RN-29: consulta acotada a DolarApi y registro auditado de referencias oficiales."""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.config import settings
from app.core.db import acquire_advisory_lock
from app.core.transactions import atomic_service, commit_service
from app.domain.fx import OfficialQuote, parse_official_quote
from app.models.close import FxRate, Setting

SOURCE = "dolarapi/oficial/promedio"
OFFICIAL_URL = "https://dolarapi.com/v1/dolares/oficial"
MAX_RESPONSE_BYTES = 65536
logger = logging.getLogger(__name__)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> Request | None:
        """El proveedor está fijado; no seguir redirecciones a otros destinos."""
        return None  # noqa: RET501


def _fetch_bytes(request: Request) -> bytes:
    with build_opener(_NoRedirects()).open(request, timeout=5) as response:
        raw: bytes = response.read(MAX_RESPONSE_BYTES + 1)
    return raw


def fetch_official_quote(*, now: datetime | None = None) -> OfficialQuote:
    """Dos intentos como máximo, sin float, con timeout y respuesta limitada."""
    timestamp = now or datetime.now(UTC)
    request = Request(
        OFFICIAL_URL, headers={"Accept": "application/json", "User-Agent": "Finanzas/1.0"}
    )
    try:
        raw = _fetch_bytes(request)
    except (URLError, TimeoutError):
        raw = _fetch_bytes(request)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("La respuesta del proveedor supera el límite permitido.")
    payload: object = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
    if not isinstance(payload, dict):
        raise ValueError("La respuesta del proveedor no es un objeto JSON.")
    return parse_official_quote(payload, timestamp)


def _latest(db: Session, source: str | None = None) -> FxRate | None:
    stmt = select(FxRate)
    if source is not None:
        stmt = stmt.where(FxRate.source == source)
    return db.execute(
        stmt.order_by(FxRate.date.desc(), FxRate.id.desc()).limit(1)
    ).scalar_one_or_none()


def _is_recent(row: FxRate | None, now: datetime) -> bool:
    return (
        row is not None
        and row.fetched_at is not None
        and row.fetched_at >= now - timedelta(hours=1)
    )


def get_fx_sync_status(db: Session) -> dict[str, str | None]:
    """Estado operativo, separado de la fecha de publicación del proveedor."""
    row = db.get(Setting, "fx_sync_status")
    if row is None or not isinstance(row.value, dict):
        return {"last_attempt": None, "last_success": None, "last_error": None}
    return {
        key: val if isinstance(val := row.value.get(key), str) else None
        for key in ("last_attempt", "last_success", "last_error")
    }


def _save_sync_status(db: Session, timestamp: datetime, error: str | None) -> None:
    row = db.get(Setting, "fx_sync_status")
    previous = get_fx_sync_status(db)
    attempt = previous["last_attempt"]
    if attempt is not None and datetime.fromisoformat(attempt) > timestamp:
        return
    value = {
        "last_attempt": timestamp.isoformat(),
        "last_success": timestamp.isoformat() if error is None else previous["last_success"],
        "last_error": error,
    }
    action = "INSERT" if row is None else "UPDATE"
    if row is None:
        row = Setting(key="fx_sync_status", value=value, updated_by=None)
        db.add(row)
    else:
        row.value = value
        row.updated_by = None
    db.flush()
    record_audit(
        db=db,
        entity="Setting",
        entity_id=1,
        action=action,
        before={"key": "fx_sync_status", "value": previous},
        after={"key": "fx_sync_status", "value": value},
    )


@atomic_service
def refresh_official_rate(
    db: Session,
    *,
    now: datetime | None = None,
    fetch_quote: Callable[[], OfficialQuote] | None = None,
) -> FxRate | None:
    """Actualiza si corresponde; cualquier fallo del proveedor conserva el último dato."""
    timestamp = now or datetime.now(UTC)
    automatic = _latest(db, SOURCE)
    if _is_recent(automatic, timestamp):
        return _latest(db)
    try:
        quote = fetch_quote() if fetch_quote is not None else fetch_official_quote(now=timestamp)
    except (URLError, TimeoutError, ValueError, UnicodeError, OSError):
        logger.warning("No se pudo actualizar la cotización oficial; se conserva la última válida.")
        acquire_advisory_lock(db, 290015, shared=False)
        _save_sync_status(
            db,
            timestamp,
            "No se pudo consultar DolarApi; se conserva la última cotización válida.",
        )
        commit_service(db)
        return _latest(db)

    # La red se consulta antes del lock; otro proceso puede haber guardado mientras tanto.
    acquire_advisory_lock(db, 290015, shared=False)
    automatic = _latest(db, SOURCE)
    if _is_recent(automatic, timestamp):
        commit_service(db)
        return _latest(db)
    if automatic and automatic.published_at and quote.published_at < automatic.published_at:
        _save_sync_status(
            db,
            timestamp,
            "DolarApi devolvió una publicación anterior; se conserva la última válida.",
        )
        commit_service(db)
        return _latest(db)

    existing = db.execute(
        select(FxRate).where(FxRate.source == SOURCE, FxRate.published_at == quote.published_at)
    ).scalar_one_or_none()
    if existing is not None:
        before = {"fetched_at": existing.fetched_at.isoformat() if existing.fetched_at else None}
        existing.fetched_at = timestamp
        record_audit(
            db=db,
            entity="fx_rate",
            entity_id=existing.id,
            action="update",
            before=before,
            after={"fetched_at": timestamp.isoformat(), "actor": "system"},
        )
        _save_sync_status(db, timestamp, None)
        commit_service(db)
        return _latest(db)
    row = FxRate(
        date=quote.published_at.astimezone(ZoneInfo(settings.APP_TIMEZONE)).date(),
        rate=quote.rate,
        buy=quote.buy,
        sell=quote.sell,
        source=SOURCE,
        published_at=quote.published_at,
        fetched_at=timestamp,
        created_by=None,
        note="Dólar oficial · promedio compra y venta · DolarApi",
    )
    db.add(row)
    db.flush()
    record_audit(
        db=db,
        entity="fx_rate",
        entity_id=row.id,
        action="create",
        after={
            "source": SOURCE,
            "buy": str(row.buy),
            "sell": str(row.sell),
            "rate": str(row.rate),
            "published_at": quote.published_at.isoformat(),
            "fetched_at": timestamp.isoformat(),
            "actor": "system",
        },
    )
    _save_sync_status(db, timestamp, None)
    commit_service(db)
    db.refresh(row)
    return row
