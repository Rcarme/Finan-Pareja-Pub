"""Script para inicializar la base de datos con datos de demostración ficticios y anónimos.

Uso:
    python scripts/seed_demo.py

IMPORTANTE:
- Solo se puede ejecutar en entornos de desarrollo o pruebas (ENVIRONMENT=development o test).
- Bloqueado estrictamente en producción para evitar generar usuarios de prueba reales.
- Requiere que las migraciones de Alembic ya se encuentren aplicadas (alembic upgrade head).
- Genera contraseñas aleatorias seguras para cada ejecución y las imprime en consola una única vez.
"""

import secrets
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

# Agregar directorio raíz al sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import inspect  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import get_engine, get_session_factory  # noqa: E402
from app.models.ledger import Account, Category, Transaction  # noqa: E402
from app.services.setup_service import initialize_instance, is_onboarded  # noqa: E402


def seed_demo_data() -> None:
    env = (settings.ENVIRONMENT or "").strip().lower()
    if env in {"production", "prod"}:
        print("❌ ERROR: seed_demo.py está terminantemente prohibido en entornos de producción.")
        print("   Por seguridad, no se permite crear datos de prueba en producción.")
        sys.exit(1)

    print(f"Iniciando carga de datos de demostración en entorno '{env}'...")
    engine = get_engine()

    # Verificar que el esquema de base de datos haya sido inicializado previamente por Alembic
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    required_tables = {"users", "accounts", "categories", "transactions", "settings"}
    if not required_tables.issubset(existing_tables):
        print("❌ ERROR: Las tablas de la base de datos no existen.")
        print("   Ejecutá las migraciones de Alembic antes de sembrar datos demo:")
        print("       alembic upgrade head")
        sys.exit(1)

    session_factory = get_session_factory()
    db = session_factory()

    try:
        user1_pw = f"Demo_{secrets.token_urlsafe(12)}!"
        user2_pw = f"Demo_{secrets.token_urlsafe(12)}!"

        if not is_onboarded(db):
            print("Instancia no inicializada. Creando usuarios y configuración inicial...")
            user1, user2 = initialize_instance(
                db,
                user1_username="alex",
                user1_display_name="Alex",
                user1_password=user1_pw,
                user2_username="sam",
                user2_display_name="Sam",
                user2_password=user2_pw,
                currency="ARS",
                split_pct=50,
                business_enabled=False,
                seed_defaults=True,
            )
            print("✅ Usuarios de demostración creados.")
        else:
            print("Instancia ya inicializada previamente. Agregando transacciones de muestra...")
            user1_pw = "(contraseña preexistente ya configurada)"
            user2_pw = "(contraseña preexistente ya configurada)"

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

        # Buscar categorías estándar ("Supermercado y Alimentos", "Servicios del Hogar")
        cat_super = db.query(Category).filter(Category.name.ilike("%supermercado%")).first()
        cat_servicios = db.query(Category).filter(Category.name.ilike("%servicios%")).first()

        if not cat_super or not cat_servicios:
            print("❌ ERROR: No se encontraron las categorías estándar requeridas.")
            print("   Asegurate de haber inicializado las categorías por defecto.")
            sys.exit(1)

        today = date.today()
        demo_transactions = [
            (
                today - timedelta(days=5),
                Decimal("15420.50"),
                "Compra semanal de alimentos",
                cat_super,
            ),
            (
                today - timedelta(days=2),
                Decimal("8500.00"),
                "Factura de electricidad",
                cat_servicios,
            ),
            (
                today - timedelta(days=1),
                Decimal("3200.00"),
                "Artículos de limpieza del hogar",
                cat_super,
            ),
        ]

        for tx_date, amount, desc, cat in demo_transactions:
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
        print("✅ Datos de demostración cargados exitosamente.")
        print("\n-----------------------------------------------------------")
        print("🔑 Credenciales de demostración generadas para esta sesión:")
        print(f"   Usuario 1: alex  | Contraseña: {user1_pw}")
        print(f"   Usuario 2: sam   | Contraseña: {user2_pw}")
        print("-----------------------------------------------------------")
        print("Guardá estas contraseñas si las necesitás para iniciar sesión.")

    finally:
        db.close()


if __name__ == "__main__":
    seed_demo_data()
