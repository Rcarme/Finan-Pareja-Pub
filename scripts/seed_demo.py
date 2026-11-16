"""Script para inicializar la base de datos con datos de demostración ficticios y anónimos.

Uso:
    python scripts/seed_demo.py

Crea dos usuarios de demostración (alex / sam) con categorías de prueba,
cuentas bancarias genéricas y transacciones de muestra para evaluar la plataforma.
"""

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

# Agregar directorio raíz al sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import get_engine, get_session_factory  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.ledger import Account, Category, Transaction  # noqa: E402
from app.services.setup_service import initialize_instance, is_onboarded  # noqa: E402


def seed_demo_data() -> None:
    print("Iniciando carga de datos de demostración...")
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    session_factory = get_session_factory()
    db = session_factory()

    try:
        if not is_onboarded(db):
            print("Instancia no inicializada. Creando usuarios y configuración inicial...")
            user1, user2 = initialize_instance(
                db,
                user1_username="alex",
                user1_display_name="Alex",
                user1_password="DemoPassword1234!",
                user2_username="sam",
                user2_display_name="Sam",
                user2_password="DemoPassword5678!",
                currency="ARS",
                split_pct=50,
                business_enabled=False,
                seed_defaults=True,
            )
            print(f"Usuarios creados: {user1.username} y {user2.username}")
        else:
            print("Instancia ya inicializada. Generando transacciones adicionales demo...")

        # Obtener o crear cuenta bancaria demo
        account = db.query(Account).filter_by(name="Cuenta Corriente Compartida").first()
        if not account:
            account = Account(
                name="Cuenta Corriente Compartida",
                type="bank",
                currency="ARS",
                active=True,
            )
            db.add(account)
            db.commit()
            db.refresh(account)

        # Buscar categorías estándar
        cat_super = db.query(Category).filter_by(name="Supermercado").first()
        cat_servicios = db.query(Category).filter_by(name="Servicios").first()

        today = date.today()
        demo_transactions = [
            (
                today - timedelta(days=5),
                Decimal("15420.50"),
                "Compra semanal de alimentos",
                cat_super,
            ),
            (today - timedelta(days=2), Decimal("8500.00"), "Factura de luz", cat_servicios),
            (today - timedelta(days=1), Decimal("3200.00"), "Artículos de limpieza", cat_super),
        ]

        for tx_date, amount, desc, cat in demo_transactions:
            if cat:
                tx = Transaction(
                    account_id=account.id,
                    category_id=cat.id,
                    amount=amount,
                    type="expense",
                    date=tx_date,
                    description=desc,
                )
                db.add(tx)

        db.commit()
        print("Datos de demostración cargados exitosamente.")
        print("Credenciales demo:")
        print("  Usuario 1: alex / DemoPassword1234!")
        print("  Usuario 2: sam  / DemoPassword5678!")

    finally:
        db.close()


if __name__ == "__main__":
    seed_demo_data()
