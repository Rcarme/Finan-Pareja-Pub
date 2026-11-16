"""Add date range and currency check constraints

Revision ID: 002_add_date_and_currency_constraints
Revises: 001_initial_schema
Create Date: 2026-11-16 18:50:00.000000

"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = "002_add_date_and_currency_constraints"
down_revision: Union[str, None] = "001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _get_existing_constraints(bind, table_name: str) -> set[str]:
    inspector = inspect(bind)
    try:
        return {
            c["name"]
            for c in inspector.get_check_constraints(table_name)
            if c.get("name")
        }
    except Exception:
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    def add_check_if_missing(table_name: str, constraint_name: str, condition: str) -> None:
        existing = _get_existing_constraints(bind, table_name)
        if constraint_name in existing:
            return
        if dialect == "sqlite":
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.create_check_constraint(constraint_name, condition)
        else:
            op.create_check_constraint(constraint_name, table_name, condition)

    def drop_check_if_present(table_name: str, constraint_name: str) -> None:
        existing = _get_existing_constraints(bind, table_name)
        if constraint_name not in existing:
            return
        if dialect == "sqlite":
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.drop_constraint(constraint_name, type_="check")
        else:
            op.drop_constraint(constraint_name, table_name, type_="check")

    # 1. Dropear restricciones redundantes legadas si existen
    drop_check_if_present("accounts", "chk_account_currency")
    drop_check_if_present("ownership_entries", "chk_ownership_currency")

    # 2. Agregar restricciones si no existen (en instalaciones nuevas 001 ya las crea vía Base.metadata)
    add_check_if_missing(
        "transactions",
        "chk_transaction_date_range",
        "date >= '2000-01-01' AND date <= '2100-12-31'",
    )
    add_check_if_missing(
        "periods",
        "chk_period_range",
        "period >= '2000-01-01' AND period <= '2100-12-31'",
    )
    add_check_if_missing(
        "accounts",
        "chk_account_currency_valid",
        "currency IN ('ARS', 'USD')",
    )
    add_check_if_missing(
        "ownership_entries",
        "chk_ownership_currency_valid",
        "currency IN ('ARS', 'USD')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    def drop_check_if_present(table_name: str, constraint_name: str) -> None:
        existing = _get_existing_constraints(bind, table_name)
        if constraint_name not in existing:
            return
        if dialect == "sqlite":
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.drop_constraint(constraint_name, type_="check")
        else:
            op.drop_constraint(constraint_name, table_name, type_="check")

    drop_check_if_present("ownership_entries", "chk_ownership_currency_valid")
    drop_check_if_present("accounts", "chk_account_currency_valid")
    drop_check_if_present("periods", "chk_period_range")
    drop_check_if_present("transactions", "chk_transaction_date_range")
