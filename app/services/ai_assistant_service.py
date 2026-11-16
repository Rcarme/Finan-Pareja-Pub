"""Servicio del Asistente Contable con IA.

Gestiona el contexto en memoria, la comunicación con Gemini y la persistencia
atómica y auditada de los movimientos confirmados (RN-01, RN-03, RN-10).
"""

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.formatters import today_ar
from app.core.security import decrypt_secret, encrypt_secret
from app.core.transactions import atomic_service, commit_service
from app.domain.ai_assistant import (
    AIDraftItem,
    ExistingTxRecord,
    build_system_instruction,
    check_item_duplicates_pure,
    parse_llm_json_response,
    sanitize_draft_item,
)
from app.models.auth import User
from app.models.close import Setting
from app.models.ledger import Account, Category, Transaction
from app.services.ledger_service import create_transaction

logger = logging.getLogger("app.services.ai_assistant")

_CACHE_TTL_SECONDS = 3600
_cached_instruction: str | None = None
_cached_categories: list[str] = []
_cached_accounts: list[str] = []
_cached_users: list[str] = []
_cached_at: datetime | None = None


def get_effective_gemini_api_key(db: Session) -> str | None:
    """Obtiene la clave de Gemini (descifrándola si está en BD) o desde el entorno."""
    setting = db.execute(
        select(Setting).where(Setting.key == "gemini_api_key")
    ).scalar_one_or_none()
    if setting and isinstance(setting.value, dict) and setting.value.get("api_key"):
        val = str(setting.value["api_key"]).strip()
        if val:
            try:
                decrypted = decrypt_secret(val)
                if decrypted:
                    # Re-cifrado transparente en lectura: si no es Fernet v2 con HKDF,
                    # se migra y persiste automáticamente al estándar moderno
                    if not val.startswith("enc:v2:"):
                        from app.services.setup_service import set_system_setting

                        set_system_setting(
                            db, "gemini_api_key", {"api_key": encrypt_secret(decrypted)}
                        )
                        db.commit()
                    return decrypted
            except Exception:
                logger.warning(
                    "Fallo al descifrar gemini_api_key. Posible rotación de APP_SECRET_KEY."
                )
    env_val = (settings.GEMINI_API_KEY or "").strip()
    return env_val or None


def update_gemini_api_key(db: Session, api_key: str, user_id: int | None = None) -> None:
    """Actualiza la clave de API de Gemini de forma cifrada en la configuración del sistema."""
    from app.services.setup_service import set_system_setting

    set_system_setting(db, "gemini_api_key", {"api_key": encrypt_secret(api_key.strip())}, user_id)
    db.commit()


def invalidate_ai_context_cache() -> None:
    """Invalida la caché del agente para forzar refresco en el siguiente uso."""
    global _cached_instruction, _cached_at
    _cached_instruction = None
    _cached_at = None
    logger.info("Caché de contexto de IA invalidada.")


def get_ai_context(db: Session, force_refresh: bool = False) -> tuple[str, list[str], list[str]]:
    """Obtiene o compila el contexto y la instrucción del sistema del Agente IA."""
    global _cached_instruction, _cached_categories, _cached_accounts, _cached_users, _cached_at

    now = datetime.now(UTC)
    is_expired = _cached_at is None or (now - _cached_at) > timedelta(seconds=_CACHE_TTL_SECONDS)

    if not force_refresh and _cached_instruction is not None and not is_expired:
        return _cached_instruction, _cached_categories, _cached_accounts

    users = [u.display_name for u in db.scalars(select(User).where(User.active.is_(True))).all()]
    if not users:
        users = ["Persona 1", "Persona 2"]

    accounts = [
        a.name
        for a in db.scalars(
            select(Account)
            .where(Account.active.is_(True), Account.is_virtual.is_(False))
            .order_by(Account.name)
        ).all()
    ]
    if not accounts:
        accounts = ["Efectivo"]

    categories = [
        c.name
        for c in db.scalars(
            select(Category)
            .where(
                Category.active.is_(True),
                Category.merged_into_id.is_(None),
                Category.kind == "expense",
            )
            .order_by(Category.name)
        ).all()
    ]
    if not categories:
        categories = ["Varios para la casa"]

    instruction = build_system_instruction(categories, accounts, users)

    _cached_instruction = instruction
    _cached_categories = categories
    _cached_accounts = accounts
    _cached_users = users
    _cached_at = now

    logger.info(
        "Contexto de Agente IA compilado: %d categorías, %d cuentas.",
        len(categories),
        len(accounts),
    )
    return instruction, categories, accounts


def get_ai_context_status(db: Session) -> dict[str, Any]:
    """Informa el estado de sincronización del contexto del agente."""
    _, categories, accounts = get_ai_context(db)
    return {
        "status": "synchronized",
        "categories_count": len(categories),
        "accounts_count": len(accounts),
        "cached_at": _cached_at.isoformat() if _cached_at else None,
    }


def parse_raw_text_with_gemini(
    db: Session,
    raw_text: str,
    client: httpx.Client | None = None,
) -> list[AIDraftItem]:
    """Envía el texto desestructurado a Gemini con el contexto cacheado y extrae movimientos."""
    text_content = raw_text.strip()
    if not text_content:
        raise ValueError("El texto a analizar no puede estar vacío.")
    if len(text_content) > 10000:
        raise ValueError("El texto supera el límite máximo permitido (10.000 caracteres).")

    api_key = get_effective_gemini_api_key(db)
    if not api_key:
        raise ValueError(
            "La clave de Google Gemini no está configurada. "
            "Podés ingresarla en Ajustes o definir GEMINI_API_KEY en tu archivo .env "
            "para habilitar el Asistente IA con voz."
        )

    instruction, categories, accounts = get_ai_context(db)
    default_acc = accounts[0] if accounts else "Efectivo"
    default_cat = categories[0] if categories else "Varios para la casa"

    payload = {
        "system_instruction": {"parts": [{"text": instruction}]},
        "contents": [{"parts": [{"text": text_content}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0,
        },
    }

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{settings.GEMINI_MODEL}"
        ":generateContent"
    )
    headers = {"x-goog-api-key": api_key}

    should_close = False
    if client is None:
        client = httpx.Client(timeout=30)
        should_close = True

    try:
        response = client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise ValueError(
            "El servicio de IA demoró demasiado en responder (timeout). Reintentá en unos segundos."
        ) from exc
    except httpx.RequestError as exc:
        raise ValueError(f"Error de conexión con el servicio de IA: {exc}") from exc
    finally:
        if should_close:
            client.close()

    if response.status_code == 429:
        raise ValueError("Límite de solicitudes de IA alcanzado. Aguardá y reintentá.")
    if response.status_code >= 500:
        raise ValueError("El servicio de IA tiene alta demanda. Reintentá en instantes.")
    if response.status_code != 200:
        raise ValueError(
            f"Respuesta inesperada de la IA ({response.status_code}): {response.text[:200]}"
        )

    try:
        resp_data = response.json()
    except Exception as exc:
        raise ValueError("La respuesta de la IA no es un formato JSON válido.") from exc

    if not isinstance(resp_data, dict):
        raise ValueError("La respuesta de la IA no devolvió un objeto JSON esperado.")

    candidates = resp_data.get("candidates", [])
    if not candidates:
        raise ValueError("La IA no devolvió candidatos válidos para procesar.")

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason")
    if finish_reason == "MAX_TOKENS":
        raise ValueError(
            "El texto a procesar es demasiado extenso y la respuesta de la IA fue truncada. "
            "Probá dividiendo el texto."
        )
    if finish_reason and finish_reason != "STOP":
        raise ValueError(f"La IA interrumpió el análisis (motivo: {finish_reason}).")

    try:
        raw_llm_text = candidate["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise ValueError("La IA no devolvió una estructura de texto compatible.") from exc

    raw_items = parse_llm_json_response(raw_llm_text)
    if not raw_items:
        raise ValueError("No se detectaron movimientos contables en el texto provisto.")

    return [
        sanitize_draft_item(raw, categories, accounts, default_acc, default_cat)
        for raw in raw_items
    ]


def check_duplicates_for_draft_items(
    db: Session,
    items: list[AIDraftItem],
    fallback_date: date,
) -> list[tuple[AIDraftItem, str | None]]:
    """Coteja los movimientos borrador contra transacciones activas existentes y el lote actual."""
    if not items:
        return []

    amounts = [it.amount for it in items]
    min_date = fallback_date - timedelta(days=90)
    max_date = fallback_date + timedelta(days=90)
    rows = db.execute(
        select(
            Transaction.id,
            Transaction.date,
            Transaction.amount,
            Transaction.description,
            Account.name.label("account_name"),
        )
        .join(Account, Transaction.account_id == Account.id, isouter=True)
        .where(
            Transaction.status == "active",
            Transaction.amount.in_(amounts),
            Transaction.date >= min_date,
            Transaction.date <= max_date,
        )
    ).all()

    existing_records = [
        ExistingTxRecord(
            id=r.id,
            date=r.date.isoformat(),
            amount=r.amount,
            description=r.description,
            account_name=r.account_name,
        )
        for r in rows
    ]

    return check_item_duplicates_pure(items, existing_records, fallback_date.isoformat())


@atomic_service
def confirm_ai_draft_transactions(
    db: Session,
    items: list[dict[str, Any]],
    actor_user_id: int,
) -> list[int]:
    """Persiste en forma atómica y auditada los movimientos revisados y aprobados por el usuario."""
    if not items:
        raise ValueError("No hay movimientos para confirmar.")

    accounts_by_name = {
        a.name.lower(): a for a in db.scalars(select(Account).where(Account.active.is_(True))).all()
    }
    categories_by_name = {
        c.name.lower(): c
        for c in db.scalars(
            select(Category).where(Category.active.is_(True), Category.merged_into_id.is_(None))
        ).all()
    }
    users_by_name = {
        u.display_name.lower(): u
        for u in db.scalars(select(User).where(User.active.is_(True))).all()
    }
    for u in db.scalars(select(User).where(User.active.is_(True))).all():
        users_by_name[u.username.lower()] = u

    created_ids: list[int] = []

    for raw in items:
        desc = str(raw.get("description", "")).strip()
        if not desc:
            raise ValueError("Cada movimiento debe tener una descripción.")

        amount_val = raw.get("amount")
        if amount_val is None:
            raise ValueError("Cada movimiento debe tener un importe.")
        amount = Decimal(str(amount_val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount <= Decimal("0"):
            raise ValueError("Cada importe debe ser mayor a cero.")

        account: Account | None = None
        if raw.get("account_id") is not None:
            account = db.get(Account, int(raw["account_id"]))
        if account is None and raw.get("account_name"):
            acc_name = str(raw.get("account_name", "")).strip().lower()
            account = accounts_by_name.get(acc_name)
        if account is None or not account.active:
            target_acc = raw.get("account_name") or raw.get("account_id")
            raise ValueError(f"Cuenta desconocida o inactiva: '{target_acc}'.")

        category: Category | None = None
        if raw.get("category_id") is not None:
            category = db.get(Category, int(raw["category_id"]))
        if category is None and raw.get("category_name"):
            cat_name = str(raw.get("category_name", "")).strip().lower()
            category = categories_by_name.get(cat_name)
        if category is None or not category.active:
            target_cat = raw.get("category_name") or raw.get("category_id")
            raise ValueError(f"Categoría desconocida o inactiva: '{target_cat}'.")

        activity = str(raw.get("activity", "household")).strip().lower()
        if activity not in ("household", "personal", "business"):
            activity = "household"

        notes = str(raw.get("notes", "")).strip() or None
        full_desc = f"{desc} ({notes})" if notes and notes not in desc else desc

        charged_to_id: int | None = None
        if raw.get("charged_to_user_id") is not None:
            uid = int(raw["charged_to_user_id"]) if raw["charged_to_user_id"] != "" else None
            charged_to_id = uid
        elif raw.get("charged_to_user_name"):
            user_target_name = str(raw.get("charged_to_user_name", "")).strip().lower()
            charged_to = users_by_name.get(user_target_name)
            charged_to_id = charged_to.id if charged_to else None
        elif activity == "personal":
            charged_to_id = actor_user_id

        tx_date = today_ar()
        if raw.get("tx_date"):
            raw_date = raw["tx_date"]
            if isinstance(raw_date, date):
                tx_date = raw_date
            elif isinstance(raw_date, str) and raw_date.strip():
                tx_date = date.fromisoformat(raw_date.strip())

        kind = str(raw.get("kind", "expense")).strip().lower()
        if kind not in ("expense", "income"):
            kind = "expense"

        tx = create_transaction(
            db,
            kind=kind,
            amount=amount,
            account_id=account.id,
            category_id=category.id,
            description=full_desc,
            activity=activity,
            charged_to_user_id=charged_to_id,
            user_id=actor_user_id,
            tx_date=tx_date,
            commit=False,
            generated_by="ai",
        )
        created_ids.append(tx.id)

    commit_service(db)
    return created_ids
