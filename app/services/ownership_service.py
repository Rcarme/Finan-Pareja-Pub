"""Audited opening and money ownership, independently of physical accounts (F18)."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.audit import record_audit
from app.core.transactions import atomic_service, commit_service
from app.domain.ownership import common_fund, personal_balance
from app.domain.ownership import validate_money as _money
from app.models.auth import User
from app.models.close import Setting
from app.models.envelopes import Envelope, EnvelopeMovement
from app.models.ledger import Account, Transaction
from app.models.ownership import OwnershipEntry


@dataclass(frozen=True)
class OwnershipSummary:
    opening_date: date
    account_balances: dict[int, Decimal]
    personal_by_user: dict[int, Decimal]
    common_fund: Decimal
    savings_ars: Decimal
    savings_usd: Decimal
    estimated_accounts: frozenset[int]
    debt_usd_by_account: dict[int, Decimal]


RESOLUTION_KEY = "ownership_pending_resolutions"


def resolved_opening_charge_ids(db: Session, opening: Setting) -> set[int]:
    marker = db.scalar(
        select(Setting)
        .where(Setting.key == RESOLUTION_KEY)
        .execution_options(populate_existing=True)
    )
    if marker is None:
        return set()
    if (
        marker.value.get("opening_date") != opening.value["date"]
        or marker.value.get("checkpoint") != opening.value["max_transaction_id"]
    ):
        raise ValueError("La resolución de pendientes no coincide con la foto de apertura.")
    return set(marker.value["resolved_ids"])


def ownership_enabled(db: Session, period_cutoff: date | None = None) -> bool:
    opening = db.get(Setting, "ownership_opening")
    return isinstance(opening, Setting) and (
        period_cutoff is None
        or period_cutoff >= date.fromisoformat(opening.value["date"]).replace(day=1)
    )


def personal_charge_conditions(
    db: Session, period: date, user_id: int | None = None, kind: str | None = None
) -> list[ColumnElement[bool]]:
    """Use the same pending charges in the close preview and its explanatory report."""
    normalized = period.replace(day=1)
    conditions = [
        Transaction.period == normalized,
        Transaction.status == "active",
        Transaction.activity == "household",
        Transaction.kind.in_(("income", "expense")),
        Transaction.charged_to_user_id.is_not(None),
    ]
    if user_id is not None:
        conditions.append(Transaction.charged_to_user_id == user_id)
    if kind is not None:
        conditions.append(Transaction.kind == kind)
    opening = db.get(Setting, "ownership_opening") if ownership_enabled(db, normalized) else None
    if opening is not None:
        resolved = resolved_opening_charge_ids(db, opening)
        pending = (
            opening.value.get("pending_personal_transaction_ids", [])
            if normalized == date.fromisoformat(opening.value["date"]).replace(day=1)
            else []
        )
        pending = [tx_id for tx_id in pending if tx_id not in resolved]
        conditions[0] = or_(Transaction.period == normalized, Transaction.id.in_(pending))
        conditions.append(Transaction.id.not_in(resolved))
        conditions.append(
            or_(
                Transaction.id > opening.value["max_transaction_id"],
                Transaction.date > date.fromisoformat(opening.value["date"]),
                Transaction.id.in_(pending),
            )
        )
    return conditions


@atomic_service
def activate_ownership(
    db: Session,
    opening_date: date,
    account_balances: dict[int, Decimal],
    personal_balances: dict[int, Decimal],
    savings_ars: Decimal,
    savings_usd: Decimal,
    user_id: int,
    reason: str,
    estimated_accounts: set[int] | None = None,
    token: str | None = None,
    pending_personal_transaction_ids: set[int] | None = None,
    debt_usd_by_account: dict[int, Decimal] | None = None,
) -> Setting:
    from app.services.ledger_service import check_period_open

    db.execute(text("SELECT pg_advisory_xact_lock(170170)"))
    for amount in (
        list(account_balances.values())
        + list(personal_balances.values())
        + [savings_ars, savings_usd]
        + list((debt_usd_by_account or {}).values())
    ):
        _money(amount)
    requested: dict[str, Any] = {
        "date": opening_date.isoformat(),
        "account_balances": {
            str(key): format(amount, ".2f") for key, amount in account_balances.items()
        },
        "personal_balances": {
            str(key): format(amount, ".2f") for key, amount in personal_balances.items()
        },
        "savings_ars": format(savings_ars, ".2f"),
        "savings_usd": format(savings_usd, ".2f"),
        "debt_usd_by_account": {
            str(key): format(amount, ".2f") for key, amount in (debt_usd_by_account or {}).items()
        },
        "pending_personal_transaction_ids": sorted(pending_personal_transaction_ids or set()),
        "estimated_accounts": sorted(estimated_accounts or set()),
        "reason": reason.strip(),
        "token": token,
    }
    existing = db.get(Setting, "ownership_opening")
    if existing is not None:
        if token and existing.value.get("token") == token:
            if (
                any(existing.value.get(key) != value for key, value in requested.items())
                or existing.updated_by != user_id
            ):
                raise ValueError(
                    "El mismo identificador de apertura no puede reutilizarse con otros datos."
                )
            return existing
        raise ValueError(
            "La apertura ya existe; las correcciones deben quedar auditadas por separado."
        )
    check_period_open(db, opening_date)
    if not reason.strip():
        raise ValueError("Indicá el motivo de la apertura.")
    accounts = list(
        db.scalars(select(Account).where(Account.active.is_(True), Account.is_virtual.is_(False)))
    )
    if set(account_balances) != {account.id for account in accounts}:
        raise ValueError("La apertura requiere el saldo de todas las cuentas reales activas.")
    users = {user.id for user in db.scalars(select(User).where(User.active.is_(True)))}
    if set(personal_balances) != users:
        raise ValueError("Indicá el saldo personal de cada integrante activo.")
    estimated = estimated_accounts or set()
    if not estimated.issubset(account_balances):
        raise ValueError("Una estimación debe corresponder a una cuenta incluida.")
    for account in accounts:
        _money(account_balances[account.id], nonnegative=account.type != "credit")
    for amount in personal_balances.values():
        _money(amount, nonnegative=True)
    _money(savings_ars, nonnegative=True)
    _money(savings_usd, nonnegative=True)
    # USD balances are native. ARS transactions can never be interpreted as USD.
    ars_assets = sum(
        (account_balances[a.id] for a in accounts if a.currency == "ARS" and a.type != "credit"),
        Decimal("0"),
    )
    if sum(personal_balances.values(), Decimal("0")) + savings_ars > ars_assets:
        raise ValueError(
            "La plata personal y el ahorro requieren dinero real disponible en las cuentas."
        )
    usd_assets = sum(
        (account_balances[a.id] for a in accounts if a.currency == "USD" and a.type != "credit"),
        Decimal("0"),
    )
    if savings_usd > usd_assets:
        raise ValueError("El ahorro en dólares requiere una cuenta real en USD con ese saldo.")
    usd_debt = debt_usd_by_account or {}
    credit_ids = {a.id for a in accounts if a.type == "credit"}
    if not set(usd_debt).issubset(credit_ids):
        raise ValueError("La deuda en dólares debe corresponder a una tarjeta real.")
    for amount in usd_debt.values():
        _money(amount, nonnegative=True)
    pending_ids = pending_personal_transaction_ids or set()
    pending = list(db.scalars(select(Transaction).where(Transaction.id.in_(pending_ids))))
    if len(pending) != len(pending_ids) or any(
        tx.status != "active"
        or tx.kind != "expense"
        or tx.charged_to_user_id is None
        or tx.activity != "household"
        or tx.date > opening_date
        or tx.period > opening_date.replace(day=1)
        for tx in pending
    ):
        raise ValueError(
            "Los cargos iniciales pendientes deben ser gastos personales activos del hogar."
        )
    payload = dict(requested)
    payload["max_transaction_id"] = db.scalar(select(func.max(Transaction.id))) or 0
    payload["max_envelope_movement_id"] = db.scalar(select(func.max(EnvelopeMovement.id))) or 0
    opening = Setting(key="ownership_opening", value=payload, updated_by=user_id)
    db.add(opening)
    db.flush()
    record_audit(db, "Setting", 0, "INSERT", after=payload, user_id=user_id)
    commit_service(db)
    return opening


def get_ownership_summary(
    db: Session, period_cutoff: date | None = None
) -> OwnershipSummary | None:
    if not ownership_enabled(db, period_cutoff):
        return None
    opening = db.get(Setting, "ownership_opening")
    assert opening is not None
    value = opening.value
    accounts = {
        a.id: a
        for a in db.scalars(
            select(Account)
            .where(Account.is_virtual.is_(False))
            .execution_options(populate_existing=True)
        )
    }
    balances = {
        int(key): Decimal(str(amount))
        for key, amount in value["account_balances"].items()
        if int(key) in accounts
    }
    for account_id in accounts:
        balances.setdefault(account_id, Decimal("0"))
    stmt = select(Transaction).where(
        Transaction.status == "active",
        or_(
            Transaction.id > int(value["max_transaction_id"]),
            Transaction.date > date.fromisoformat(value["date"]),
        ),
        Transaction.internal_distribution.is_(False),
    )
    if period_cutoff is not None:
        stmt = stmt.where(Transaction.period <= period_cutoff)
    personal_spending: dict[int, Decimal] = {}
    for tx in db.scalars(stmt.execution_options(populate_existing=True)):
        source = accounts.get(tx.account_id)
        if source is None or source.currency != "ARS":
            raise ValueError(
                "Un movimiento posterior a la apertura requiere una cuenta real en pesos."
            )
        balances[source.id] += tx.amount if tx.kind == "income" else -tx.amount
        if tx.kind == "transfer" and tx.to_account_id in accounts:
            balances[tx.to_account_id] += tx.amount
        if (
            tx.activity == "personal"
            and tx.kind in {"income", "expense"}
            and tx.charged_to_user_id is not None
        ):
            personal_spending[tx.charged_to_user_id] = personal_spending.get(
                tx.charged_to_user_id, Decimal("0")
            ) + (tx.amount if tx.kind == "expense" else -tx.amount)
    entry_stmt = select(OwnershipEntry)
    if period_cutoff is not None:
        entry_stmt = entry_stmt.where(OwnershipEntry.period <= period_cutoff)
    entries = list(db.scalars(entry_stmt.execution_options(populate_existing=True)))
    usd_debt = {
        int(key): Decimal(str(amount))
        for key, amount in value.get("debt_usd_by_account", {}).items()
    }
    savings = {"ARS": Decimal(str(value["savings_ars"])), "USD": Decimal(str(value["savings_usd"]))}
    for entry in entries:
        if entry.bucket == "physical" and entry.account_id in accounts:
            account = accounts[entry.account_id]
            if entry.currency == account.currency:
                balances[account.id] += entry.amount
            elif entry.currency == "USD" and account.type == "credit":
                usd_debt[account.id] = usd_debt.get(account.id, Decimal("0")) - entry.amount
            else:
                raise ValueError(
                    "Una corrección de apertura tiene una moneda incompatible con su cuenta."
                )
        elif entry.bucket == "savings":
            savings[entry.currency] += entry.amount
    personals = {
        int(uid): personal_balance(
            Decimal(str(amount)),
            [
                entry.amount
                for entry in entries
                if entry.bucket == "personal"
                and entry.user_id == int(uid)
                and entry.currency == "ARS"
            ],
            personal_spending.get(int(uid), Decimal("0")),
        )
        for uid, amount in value["personal_balances"].items()
    }
    return OwnershipSummary(
        opening_date=date.fromisoformat(value["date"]),
        account_balances=balances,
        personal_by_user=personals,
        common_fund=common_fund(
            [
                balance
                for account_id, balance in balances.items()
                if accounts[account_id].currency == "ARS"
            ],
            personals.values(),
        ),
        savings_ars=savings["ARS"],
        savings_usd=savings["USD"],
        debt_usd_by_account=usd_debt,
        estimated_accounts=frozenset(value["estimated_accounts"]),
    )


def record_personal_allocation(
    db: Session, recipient_user_id: int, amount: Decimal, period: date, close_id: int, user_id: int
) -> OwnershipEntry:
    entry = OwnershipEntry(
        user_id=recipient_user_id,
        amount=amount,
        currency="ARS",
        period=period,
        close_id=close_id,
        reason="Asignación interna del cierre",
        created_by=user_id,
    )
    db.add(entry)
    db.flush()
    record_audit(
        db,
        "OwnershipEntry",
        entry.id,
        "INSERT",
        after={"user_id": recipient_user_id, "amount": str(amount), "close_id": close_id},
        user_id=user_id,
    )
    return entry


def reverse_close_allocations(db: Session, close_id: int, user_id: int, reason: str) -> None:
    entries = list(
        db.scalars(
            select(OwnershipEntry).where(
                OwnershipEntry.close_id == close_id, OwnershipEntry.reversal_of.is_(None)
            )
        )
    )
    for entry in entries:
        reversal = OwnershipEntry(
            user_id=entry.user_id,
            account_id=entry.account_id,
            bucket=entry.bucket,
            amount=-entry.amount,
            currency=entry.currency,
            period=entry.period,
            close_id=close_id,
            reversal_of=entry.id,
            reason=reason,
            created_by=user_id,
        )
        db.add(reversal)
        db.flush()
        record_audit(
            db,
            "OwnershipEntry",
            reversal.id,
            "INSERT",
            after={"reversal_of": entry.id, "amount": str(reversal.amount)},
            user_id=user_id,
        )


def _backing_gap(
    db: Session, summary: OwnershipSummary, accounts: list[Account], currency: str
) -> Decimal:
    from app.services.envelope_service import get_envelope_balance

    assets = [
        summary.account_balances[a.id]
        for a in accounts
        if a.currency == currency and a.type != "credit"
    ]
    claims = [summary.savings_usd]
    if currency == "ARS":
        reserves = sum(
            (
                get_envelope_balance(db, env.id)
                for env in db.scalars(select(Envelope).where(Envelope.active.is_(True)))
            ),
            Decimal("0"),
        )
        claims = [*summary.personal_by_user.values(), reserves]
    return common_fund(assets, claims)


@atomic_service
def adjust_ownership_opening(
    db: Session,
    period: date,
    reason: str,
    user_id: int,
    account_id: int | None = None,
    personal_user_id: int | None = None,
    physical_delta: Decimal = Decimal("0"),
    personal_delta: Decimal = Decimal("0"),
    savings_delta: Decimal = Decimal("0"),
    currency: str = "ARS",
) -> list[OwnershipEntry]:
    """Correct a provisional photo using signed entries, never a new income."""
    from app.domain.ownership import validate_currency
    from app.services.ledger_service import check_period_open

    validate_currency(currency)
    db.execute(text("SELECT pg_advisory_xact_lock(170170)"))
    summary = get_ownership_summary(db)
    if summary is None:
        raise ValueError("Primero registrá la apertura.")
    normalized = period.replace(day=1)
    if normalized < summary.opening_date.replace(day=1):
        raise ValueError("La corrección no puede preceder la apertura.")
    check_period_open(db, normalized)
    if not reason.strip():
        raise ValueError("Indicá el motivo de la corrección.")
    for amount in (physical_delta, personal_delta, savings_delta):
        _money(amount)
    if physical_delta:
        account = db.get(Account, account_id)
        if account is None or account.is_virtual or not account.active:
            raise ValueError("Elegí una cuenta real activa.")
        if account.currency != currency and not (account.type == "credit" and currency == "USD"):
            raise ValueError("La moneda no coincide con la cuenta.")
        if account.type != "credit" and summary.account_balances[account.id] + physical_delta < 0:
            raise ValueError("El saldo corregido de dinero no puede ser negativo.")
    if personal_delta and (currency != "ARS" or personal_user_id not in summary.personal_by_user):
        raise ValueError("La corrección personal requiere un integrante y pesos.")
    current_savings = summary.savings_ars if currency == "ARS" else summary.savings_usd
    if current_savings + savings_delta < 0:
        raise ValueError("El ahorro corregido no puede ser negativo.")
    if currency == "ARS" and savings_delta < 0:
        from app.services.envelope_service import validate_envelope_deductions

        envelope = db.scalar(select(Envelope).where(Envelope.name == "Ahorro común"))
        assert envelope is not None
        try:
            validate_envelope_deductions(db, envelope.id, {normalized: -savings_delta})
        except ValueError as error:
            raise ValueError(
                "El ahorro corregido no puede quedar negativo ni por debajo de lo ya utilizado "
                "en ningún corte posterior."
            ) from error
    accounts = list(db.scalars(select(Account).where(Account.is_virtual.is_(False))))
    before_gap_ars = _backing_gap(db, summary, accounts, "ARS")
    before_gap_usd = _backing_gap(db, summary, accounts, "USD")
    entries = []
    for bucket, amount in [
        ("physical", physical_delta),
        ("personal", personal_delta),
        ("savings", savings_delta),
    ]:
        if not amount:
            continue
        entry = OwnershipEntry(
            bucket=bucket,
            account_id=account_id if bucket == "physical" else None,
            user_id=personal_user_id if bucket == "personal" else None,
            amount=amount,
            currency=currency,
            period=normalized,
            reason=reason.strip(),
            created_by=user_id,
        )
        db.add(entry)
        db.flush()
        record_audit(
            db,
            "OwnershipEntry",
            entry.id,
            "INSERT",
            after={
                "bucket": bucket,
                "amount": str(amount),
                "currency": currency,
                "account_id": entry.account_id,
                "user_id": entry.user_id,
                "reason": entry.reason,
            },
            user_id=user_id,
        )
        entries.append(entry)
    if not entries:
        raise ValueError("La corrección debe cambiar al menos un importe.")
    revised = get_ownership_summary(db)
    assert revised is not None
    if _backing_gap(db, revised, accounts, "ARS") < min(
        before_gap_ars, Decimal("0")
    ) or _backing_gap(db, revised, accounts, "USD") < min(before_gap_usd, Decimal("0")):
        raise ValueError(
            "La corrección asigna más dinero personal o ahorro que el dinero real existente."
        )
    commit_service(db)
    return entries
