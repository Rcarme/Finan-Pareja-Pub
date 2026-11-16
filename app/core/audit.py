"""Servicio centralizado y reutilizable de auditoría inmutable (RN-37, ARCHITECTURE.md § 3)."""

from contextvars import ContextVar
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import AuditLog

# Context variables para asociar request_id, ip y usuario actual en servicios transaccionales
current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)
current_request_id: ContextVar[str | None] = ContextVar("current_request_id", default=None)
current_client_ip: ContextVar[str | None] = ContextVar("current_client_ip", default=None)

AUDIT_ENTITY_GROUPS: dict[str, tuple[str, ...]] = {
    "Transaction": ("Transaction", "transaction", "transactions"),
    "Envelope": ("Envelope", "envelope", "envelopes"),
    "EnvelopeMovement": ("EnvelopeMovement", "envelope_movement", "envelope_movements"),
    "Obligation": ("Obligation", "obligation", "obligations"),
    "ObligationItem": ("ObligationItem", "obligation_item", "obligation_items"),
    "FxRate": ("FxRate", "fx_rate", "fx_rates"),
    "RentContract": ("RentContract", "rent_contract", "rent_contracts"),
    "RentAdjustment": ("RentAdjustment", "rent_adjustment", "rent_adjustments"),
    "RecurringTemplate": ("RecurringTemplate", "recurring_template", "recurring_templates"),
    "RecurringPending": ("RecurringPending", "recurring_pending", "recurring_pendings"),
    "Category": ("Category", "category", "categories"),
    "CardStatement": ("CardStatement", "card_statement", "card_statements"),
    "MerchantRule": ("MerchantRule", "merchant_rule", "merchant_rules"),
    "Account": ("Account", "account", "accounts"),
    "OwnershipEntry": ("OwnershipEntry", "ownership_entry", "ownership_entries"),
    "RetirementContribution": (
        "RetirementContribution",
        "retirement_contribution",
        "retirement_contributions",
    ),
    "Close": ("Close", "close", "closes"),
    "Period": ("Period", "period", "periods"),
    "Carryover": ("Carryover", "carryover", "carryovers"),
    "Setting": ("Setting", "setting", "settings"),
    "User": ("User", "user", "users"),
    "Session": ("Session", "session", "sessions"),
}

AUDIT_ACTION_GROUPS: dict[str, tuple[str, ...]] = {
    "INSERT": ("INSERT", "create", "CREATE", "allocate", "spend"),
    "UPDATE": ("UPDATE", "update"),
    "VOID": ("VOID", "void", "REVERT"),
    "CLOSE": ("CLOSE", "close"),
    "REOPEN": ("REOPEN", "reopen"),
    "RECONCILE": ("RECONCILE", "reconcile"),
    "LOGIN_SUCCESS": ("LOGIN_SUCCESS", "login_success", "login"),
    "LOGIN_FAILED": ("LOGIN_FAILED", "login_failed"),
    "LOGOUT": ("LOGOUT", "logout"),
    "PASSWORD_CHANGE": ("PASSWORD_CHANGE", "password_change"),
}

KNOWN_AUDIT_ENTITIES: list[str] = list(AUDIT_ENTITY_GROUPS.keys())
KNOWN_AUDIT_ACTIONS: list[str] = list(AUDIT_ACTION_GROUPS.keys())


def resolve_audit_entity_values(entity: str) -> list[str]:
    """Expande un nombre de entidad (canónico o alias) a todos los valores equivalentes (H-06)."""
    cleaned = entity.strip()
    for canonical, aliases in AUDIT_ENTITY_GROUPS.items():
        if cleaned == canonical or cleaned.lower() in {a.lower() for a in aliases}:
            return list(aliases)
    return [cleaned]


def resolve_audit_action_values(action: str) -> list[str]:
    """Expande un nombre de acción (canónico o alias) a todos los valores equivalentes (H-06)."""
    cleaned = action.strip()
    for canonical, aliases in AUDIT_ACTION_GROUPS.items():
        if cleaned == canonical or cleaned.lower() in {a.lower() for a in aliases}:
            return list(aliases)
    return [cleaned]


def record_audit(
    db: Session,
    entity: str,
    entity_id: int,
    action: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    user_id: int | None = None,
    request_id: str | None = None,
    ip: str | None = None,
) -> AuditLog:
    """Registra una operación en audit_log.

    Regla RN-37: Todo alta/edición/anulación/cierre/reapertura/cambio de configuración
    escribe una fila en audit_log (usuario, fecha/hora, entidad, acción, antes, después).
    """
    effective_user_id = user_id if user_id is not None else current_user_id.get()
    effective_request_id = request_id if request_id is not None else current_request_id.get()
    effective_ip = ip if ip is not None else current_client_ip.get()

    log_entry = AuditLog(
        user_id=effective_user_id,
        entity=entity,
        entity_id=entity_id,
        action=action,
        before=before,
        after=after,
        request_id=effective_request_id,
        ip=effective_ip,
    )
    db.add(log_entry)
    return log_entry
