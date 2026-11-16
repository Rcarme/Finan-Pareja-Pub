"""Convert reserved USD into actual received ARS in one audited transaction."""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db import acquire_advisory_lock
from app.core.transactions import atomic_service, commit_service
from app.domain.exchange import effective_exchange_rate
from app.models.ledger import Account
from app.models.ownership import OwnershipEntry
from app.services.ownership_service import adjust_ownership_opening, get_ownership_summary


@atomic_service
def convert_reserved_usd(
    db: Session,
    usd_account_id: int,
    ars_account_id: int,
    usd_amount: Decimal,
    ars_received: Decimal,
    period: date,
    reason: str,
    user_id: int,
) -> list[OwnershipEntry]:
    rate = effective_exchange_rate(usd_amount, ars_received)
    acquire_advisory_lock(db, 170170, shared=False)
    summary = get_ownership_summary(db)
    if summary is None:
        raise ValueError("Primero registrá la apertura del dinero.")
    source = db.get(Account, usd_account_id)
    target = db.get(Account, ars_account_id)
    if any(
        account is None or not account.active or account.is_virtual or account.type == "credit"
        for account in (source, target)
    ):
        raise ValueError("Elegí cuentas físicas activas con dinero.")
    assert source is not None
    assert target is not None
    if source.currency != "USD" or target.currency != "ARS":
        raise ValueError("La conversión requiere origen USD y destino ARS.")
    if usd_amount > summary.savings_usd or usd_amount > summary.account_balances.get(
        source.id, Decimal("0")
    ):
        raise ValueError("No alcanza el ahorro en dólares de esa cuenta.")
    previous = db.info.get("pending_operation")
    if previous is None:
        db.info["pending_operation"] = "currency_exchange"
    try:
        entries = adjust_ownership_opening(
            db,
            period,
            reason,
            user_id,
            account_id=source.id,
            physical_delta=-usd_amount,
            savings_delta=-usd_amount,
            currency="USD",
        )
        entries += adjust_ownership_opening(
            db,
            period,
            reason,
            user_id,
            account_id=target.id,
            physical_delta=ars_received,
            savings_delta=ars_received,
            currency="ARS",
        )
        record_audit(
            db,
            "OwnershipEntry",
            entries[0].id,
            "INSERT",
            after={
                "operation": "currency_exchange",
                "usd_amount": str(usd_amount),
                "ars_received": str(ars_received),
                "effective_rate": str(rate),
                "source_account_id": source.id,
                "target_account_id": target.id,
                "reason": reason,
            },
            user_id=user_id,
        )
    finally:
        if previous is None:
            db.info.pop("pending_operation", None)
    commit_service(db)
    return entries
