"""Importación contable revisada y aprendizaje de reglas locales por tarjeta."""

import hashlib
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.transactions import atomic_service, commit_service
from app.domain.card_import import ColumnMapping, normalize_merchant, parse_statement
from app.models.auth import User
from app.models.cards import CardStatement, MerchantRule
from app.models.ledger import Account
from app.services.category_service import canonical_category
from app.services.ledger_service import create_transaction


def _amount(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Importe inválido.") from exc
    if not amount.is_finite() or amount <= 0 or amount > Decimal("999999999999.99"):
        raise ValueError("Importe inválido.")
    rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        raise ValueError("Importe inválido.")
    return rounded


def _lock_card(db: Session, account_id: int) -> Account:
    account = db.execute(
        select(Account)
        .where(Account.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if account is None or not account.active or account.type != "credit":
        raise ValueError("Elegí una cuenta activa de tipo tarjeta.")
    return account


@atomic_service
def preview_statement(
    db: Session,
    account_id: int,
    owner_user_id: int,
    content: bytes,
    mapping: ColumnMapping,
    user_id: int,
    *,
    delimiter: str = ";",
    encoding: str = "utf-8-sig",
    date_format: str = "%d/%m/%Y",
    decimal_separator: str = ",",
    expense_positive: bool = True,
) -> CardStatement:
    # Todos los escritores adquieren el lock contable antes de bloquear filas.
    db.execute(text("SELECT pg_advisory_xact_lock_shared(170170)"))
    _lock_card(db, account_id)
    if db.get(User, owner_user_id) is None:
        raise ValueError("Titular inválido.")
    source_hash = hashlib.sha256(content).hexdigest()
    existing = db.execute(
        select(CardStatement)
        .where(CardStatement.account_id == account_id, CardStatement.source_hash == source_hash)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if existing is not None and existing.status != "preview":
        return existing
    parsed = parse_statement(
        content,
        mapping,
        delimiter=delimiter,
        encoding=encoding,
        date_format=date_format,
        decimal_separator=decimal_separator,
        expense_positive=expense_positive,
    )
    rules = {
        rule.merchant: rule
        for rule in db.scalars(select(MerchantRule).where(MerchantRule.account_id == account_id))
    }
    previous = db.scalars(
        select(CardStatement).where(
            CardStatement.account_id == account_id, CardStatement.source_hash != source_hash
        )
    ).all()
    seen = {
        (row["date"], row["merchant"], row["amount"], row["currency"])
        for statement in previous
        for row in statement.rows
    }
    rows: list[dict[str, Any]] = []
    for row in parsed:
        rule = rules.get(normalize_merchant(row.merchant))
        category = canonical_category(db, rule.category_id) if rule else None
        valid_rule = (
            rule is not None
            and category is not None
            and category.active
            and category.kind == row.kind
        )
        rows.append(
            {
                "date": row.date.isoformat(),
                "merchant": row.merchant,
                "amount": str(row.amount),
                "currency": row.currency,
                "kind": row.kind,
                "remaining_installments": row.remaining_installments,
                "category_id": category.id if valid_rule and category else None,
                "charged_to_user_id": rule.charged_to_user_id if valid_rule and rule else None,
                "suggested": valid_rule,
                "transaction_id": None,
                "possible_duplicate": (
                    row.date.isoformat(),
                    row.merchant,
                    str(row.amount),
                    row.currency,
                )
                in seen,
            }
        )
    before = None
    if existing is None:
        statement = CardStatement(
            account_id=account_id,
            owner_user_id=owner_user_id,
            source_hash=source_hash,
            rows=rows,
            status="preview",
        )
        db.add(statement)
    else:
        statement = existing
        before = {"rows": statement.rows, "owner_user_id": statement.owner_user_id}
        if statement.rows == rows and statement.owner_user_id == owner_user_id:
            return statement
        statement.rows = rows
        statement.owner_user_id = owner_user_id
    db.flush()
    record_audit(
        db,
        "CardStatement",
        statement.id,
        "UPDATE" if existing is not None else "INSERT",
        before,
        {"account_id": account_id, "source_hash": source_hash, "rows": rows},
        user_id,
    )
    commit_service(db)
    return statement


@atomic_service
def confirm_statement(
    db: Session, statement_id: int, decisions: list[dict[str, Any]], user_id: int
) -> CardStatement:
    db.execute(text("SELECT pg_advisory_xact_lock_shared(170170)"))
    account_id = db.scalar(select(CardStatement.account_id).where(CardStatement.id == statement_id))
    if account_id is None:
        raise ValueError("Resumen inexistente.")
    # Previsualización y confirmación serializan por cuenta antes del resumen.
    _lock_card(db, account_id)
    statement = db.execute(
        select(CardStatement)
        .where(CardStatement.id == statement_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if len(decisions) != len(statement.rows):
        raise ValueError("Revisá todas las filas del resumen.")
    before = {"status": statement.status, "rows": statement.rows}
    rows = [dict(row) for row in statement.rows]
    for index, (row, decision) in enumerate(zip(rows, decisions, strict=True)):
        if row.get("transaction_id") is not None:
            continue
        if not decision.get("selected"):
            row["review_status"] = "pending_settlement" if row["currency"] == "USD" else "discarded"
            continue
        category_id = int(decision["category_id"])
        kind = str(decision.get("kind", row["kind"]))
        category = canonical_category(db, category_id)
        if (
            kind not in ("expense", "income")
            or category is None
            or not category.active
            or category.kind != kind
        ):
            raise ValueError(f"Fila {index + 1}: categoría incompatible con gasto/reintegro.")
        category_id = category.id
        charged = decision.get("charged_to_user_id")
        charged_id = int(charged) if charged else None
        if charged_id is not None and db.get(User, charged_id) is None:
            raise ValueError("Persona inválida.")
        amount = _amount(decision.get("amount", row["amount"]))
        usd_amount = None
        fx_rate = None
        if row["currency"] == "USD":
            usd_amount = amount
            settled = decision.get("settled_ars")
            if not settled:
                raise ValueError("Los consumos USD requieren el importe real liquidado en pesos.")
            amount = _amount(settled)
            fx_rate = (amount / usd_amount).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if fx_rate > Decimal("9999999999.9999"):
                raise ValueError("La cotización efectiva supera el límite contable.")
        tx_date = date.fromisoformat(str(decision.get("date", row["date"])))
        period = date.fromisoformat(str(decision.get("period", row["date"]))).replace(day=1)
        transaction = create_transaction(
            db,
            kind=kind,
            amount=amount,
            account_id=statement.account_id,
            tx_date=tx_date,
            period=period,
            description=row["merchant"],
            user_id=user_id,
            category_id=category_id,
            charged_to_user_id=charged_id,
            generated_by="card_import",
            idempotency_key=f"card:{statement.id}:{index}",
            usd_amount=usd_amount,
            fx_rate_effective=fx_rate,
            use_category_default=False,
            commit=False,
        )
        row.update(
            transaction_id=transaction.id,
            category_id=category_id,
            charged_to_user_id=charged_id,
            kind=kind,
            posted_amount=str(amount),
            amount=str(usd_amount if usd_amount is not None else amount),
            date=tx_date.isoformat(),
            review_status="posted",
        )
        if decision.get("learn"):
            merchant = normalize_merchant(row["merchant"])
            rule = db.scalar(
                select(MerchantRule).where(
                    MerchantRule.account_id == statement.account_id,
                    MerchantRule.merchant == merchant,
                )
            )
            old = (
                {"category_id": rule.category_id, "charged_to_user_id": rule.charged_to_user_id}
                if rule
                else None
            )
            action = "UPDATE" if rule else "INSERT"
            if rule is None:
                rule = MerchantRule(
                    account_id=statement.account_id,
                    merchant=merchant,
                    category_id=category_id,
                    charged_to_user_id=charged_id,
                )
                db.add(rule)
            else:
                rule.category_id = category_id
                rule.charged_to_user_id = charged_id
            db.flush()
            record_audit(
                db,
                "MerchantRule",
                rule.id,
                action,
                old,
                {
                    "account_id": statement.account_id,
                    "merchant": merchant,
                    "category_id": category_id,
                    "charged_to_user_id": charged_id,
                },
                user_id,
            )
    statement.rows = rows
    statement.status = "confirmed"
    db.flush()
    record_audit(
        db,
        "CardStatement",
        statement.id,
        "UPDATE",
        before,
        {"status": statement.status, "rows": rows},
        user_id,
    )
    commit_service(db)
    return statement
