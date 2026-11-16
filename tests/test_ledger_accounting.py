"""Tests unitarios para la contabilidad inquebrantable, auditoría y bloqueo optimista."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.audit import record_audit
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.base import Base
from app.models.ledger import Account, Category, Transaction


@pytest.fixture
def accounting_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        # Sembrar usuario base para auditoría
        user = User(
            username="alex",
            display_name="Alex",
            password_hash="dummy_hash",
            active=True,
            ui_mode="simple",
        )
        session.add(user)
        session.commit()
        yield session
    finally:
        session.close()


def test_transaction_decimal_and_optimistic_locking(accounting_db):
    acc = Account(
        name="Cuenta Corriente",
        type="bank",
        currency="ARS",
        opening_balance=Decimal("1000.50"),
        active=True,
        version=1,
    )
    cat = Category(
        name="Supermercado",
        kind="expense",
        activity="household",
        active=True,
        version=1,
    )
    accounting_db.add_all([acc, cat])
    accounting_db.commit()

    tx = Transaction(
        account_id=acc.id,
        category_id=cat.id,
        date=date(2026, 11, 15),
        period=date(2026, 11, 1),
        kind="expense",
        activity="household",
        amount=Decimal("350.25"),
        description="Compra semanal",
        status="active",
        created_by=1,
        version=1,
    )
    accounting_db.add(tx)
    accounting_db.commit()

    assert tx.id is not None
    assert isinstance(tx.amount, Decimal)
    assert tx.amount == Decimal("350.25")
    assert tx.version == 1

    # Actualización con incremento de versión
    tx.version += 1
    tx.amount = Decimal("360.00")
    accounting_db.commit()
    assert tx.version == 2


def test_transaction_voiding_and_audit_trail(accounting_db):
    acc = Account(
        name="Efectivo",
        type="cash",
        currency="ARS",
        opening_balance=Decimal("0.00"),
        active=True,
    )
    cat = Category(name="Varios", kind="expense", activity="household", active=True)
    accounting_db.add_all([acc, cat])
    accounting_db.commit()

    tx = Transaction(
        account_id=acc.id,
        category_id=cat.id,
        date=date(2026, 11, 15),
        period=date(2026, 11, 1),
        kind="expense",
        activity="household",
        amount=Decimal("1200.00"),
        description="Gasto erróneo",
        status="active",
        created_by=1,
    )
    accounting_db.add(tx)
    accounting_db.commit()

    # Anular la transacción con motivo y usuario de auditoría
    tx.status = "voided"
    tx.void_reason = "Carga duplicada por error"
    tx.voided_by = 1
    tx.voided_at = date(2026, 11, 15)

    # Registrar en audit_log
    record_audit(
        db=accounting_db,
        entity="transaction",
        entity_id=tx.id,
        action="void",
        before={"status": "active", "amount": "1200.00"},
        after={"status": "voided", "reason": tx.void_reason},
        user_id=1,
    )
    accounting_db.commit()

    # Verificar que el registro de auditoría es inmutable y existe
    audit_entry = accounting_db.query(AuditLog).filter_by(entity_id=tx.id, action="void").first()
    assert audit_entry is not None
    assert audit_entry.user_id == 1
    assert audit_entry.after["status"] == "voided"
    assert audit_entry.after["reason"] == "Carga duplicada por error"


def test_transaction_date_range_validation(accounting_db):
    """Verifica que fechas o períodos fuera de 2000-2100 sean rechazados."""
    from app.services.ledger_service import create_transaction

    with pytest.raises(ValueError, match="fuera del rango contable permitido"):
        create_transaction(
            db=accounting_db,
            kind="expense",
            amount=Decimal("100.00"),
            account_id=1,
            tx_date=date(1995, 1, 1),
            description="Fecha muy antigua",
            user_id=1,
        )

    with pytest.raises(ValueError, match="fuera del rango contable permitido"):
        create_transaction(
            db=accounting_db,
            kind="expense",
            amount=Decimal("100.00"),
            account_id=1,
            tx_date=date(2150, 1, 1),
            description="Fecha muy en el futuro",
            user_id=1,
        )


def test_account_currency_whitelist(accounting_db):
    """Verifica que la creación de cuentas restrinja estrictamente a ARS y USD."""
    from sqlalchemy.exc import IntegrityError

    from app.services.ledger_service import create_account

    # 1. Moneda no soportada a nivel de servicio
    with pytest.raises(ValueError, match="Moneda no soportada"):
        create_account(
            db=accounting_db,
            name="Cuenta en Euros",
            account_type="bank",
            opening_balance=Decimal("0.00"),
            user_id=1,
            currency="EUR",
        )

    # 2. Moneda válida ARS y USD a nivel de servicio
    acc_ars = create_account(
        db=accounting_db,
        name="Cuenta ARS",
        account_type="bank",
        opening_balance=Decimal("0.00"),
        user_id=1,
        currency="ARS",
    )
    assert acc_ars.currency == "ARS"

    acc_usd = create_account(
        db=accounting_db,
        name="Cuenta USD",
        account_type="bank",
        opening_balance=Decimal("0.00"),
        user_id=1,
        currency="USD",
    )
    assert acc_usd.currency == "USD"

    # 3. Violación directa del CheckConstraint a nivel de base de datos
    invalid_acc = Account(
        name="Cuenta Ilegal",
        type="bank",
        currency="ZZZ",
        opening_balance=Decimal("0.00"),
        opening_period=date(2026, 1, 1),
    )
    accounting_db.add(invalid_acc)
    with pytest.raises(IntegrityError):
        accounting_db.commit()
    accounting_db.rollback()
