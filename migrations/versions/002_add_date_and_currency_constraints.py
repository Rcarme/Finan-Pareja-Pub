"""Add date range and currency check constraints

Revision ID: 002_add_date_and_currency_constraints
Revises: 001_initial_schema
Create Date: 2026-11-16 18:50:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002_add_date_and_currency_constraints"
down_revision: Union[str, None] = "001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Restricción de rango de fechas en transacciones (2000 - 2100)
    op.create_check_constraint(
        "chk_transaction_date_range",
        "transactions",
        "date >= '2000-01-01' AND date <= '2100-12-31'",
    )

    # 2. Restricción de rango de fechas en períodos (2000 - 2100)
    op.create_check_constraint(
        "chk_period_range",
        "periods",
        "period >= '2000-01-01' AND period <= '2100-12-31'",
    )

    # 3. Restricción de moneda en cuentas (ARS y USD exclusivamente)
    op.create_check_constraint(
        "chk_account_currency_valid",
        "accounts",
        "currency IN ('ARS', 'USD')",
    )

    # 4. Restricción de moneda en entradas de asignación personal (ARS y USD)
    op.create_check_constraint(
        "chk_ownership_currency_valid",
        "ownership_entries",
        "currency IN ('ARS', 'USD')",
    )


def downgrade() -> None:
    op.drop_constraint("chk_ownership_currency_valid", "ownership_entries", type_="check")
    op.drop_constraint("chk_account_currency_valid", "accounts", type_="check")
    op.drop_constraint("chk_period_range", "periods", type_="check")
    op.drop_constraint("chk_transaction_date_range", "transactions", type_="check")
