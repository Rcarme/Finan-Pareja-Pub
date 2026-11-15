"""Servicio de exportación de movimientos por período en CSV y XLSX (SPEC.md §6).

Genera streams de bytes conforme a estándares RFC 4180 (CSV UTF-8 con BOM)
y OpenXML (XLSX nativo con zipfile, sin dependencias externas).
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.reports import build_minimal_xlsx, generate_csv_content
from app.models.auth import User
from app.models.ledger import Account, Transaction
from app.services.category_service import canonical_categories


def _get_export_data(
    db: Session,
    period: date,
) -> tuple[list[str], list[list[Any]]]:
    """Obtiene cabeceras y filas formateadas para los movimientos activos de un período."""
    norm_period = date(period.year, period.month, 1)

    stmt = (
        select(Transaction)
        .where(
            Transaction.period == norm_period,
            Transaction.status == "active",
        )
        .order_by(Transaction.date.asc(), Transaction.id.asc())
    )
    transactions = list(db.execute(stmt).scalars().all())

    headers = [
        "ID",
        "Fecha",
        "Período",
        "Tipo",
        "Cuenta",
        "Categoría",
        "Monto ARS",
        "Monto USD",
        "Cotización",
        "A cargo de",
        "Nota",
        "Creado por",
    ]

    users_map = {
        u.id: u.display_name or u.username for u in db.execute(select(User)).scalars().all()
    }
    accounts_map = {a.id: a.name for a in db.execute(select(Account)).scalars().all()}
    categories_map = {key: category.name for key, category in canonical_categories(db).items()}

    rows: list[list[Any]] = []
    for tx in transactions:
        kind_label = {
            "income": "Ingreso",
            "expense": "Gasto",
            "transfer": "Transferencia",
            "distribution": "Retiro",
        }[tx.kind]
        acc_name = accounts_map.get(tx.account_id, str(tx.account_id))
        cat_name = categories_map.get(tx.category_id, "") if tx.category_id else ""
        recipient_id = getattr(tx, "recipient_user_id", None)
        activity = getattr(tx, "activity", "household")
        if tx.kind == "transfer":
            charged_label = "Transferencia"
        elif tx.kind == "distribution":
            charged_label = users_map.get(recipient_id, "Retiro") if recipient_id else "Retiro"
        elif activity == "business":
            charged_label = "Negocio"
        else:
            charged_label = (
                users_map.get(tx.charged_to_user_id, "") if tx.charged_to_user_id else "Común"
            )
        creator_name = users_map.get(tx.created_by, str(tx.created_by))

        rows.append(
            [
                tx.id,
                tx.date,
                tx.period,
                kind_label,
                acc_name,
                cat_name,
                tx.amount,
                tx.usd_amount or Decimal("0.00"),
                tx.fx_rate_effective or Decimal("0.00"),
                charged_label,
                tx.description or "",
                creator_name,
            ]
        )

    return headers, rows


def export_period_transactions_csv(db: Session, period: date) -> bytes:
    """Genera archivo CSV de movimientos para el período especificado."""
    headers, rows = _get_export_data(db, period)
    return generate_csv_content(headers, rows)


def export_period_transactions_xlsx(db: Session, period: date) -> bytes:
    """Genera archivo XLSX de movimientos para el período especificado."""
    headers, rows = _get_export_data(db, period)
    sheet_name = f"Movimientos_{period.strftime('%Y_%m')}"
    return build_minimal_xlsx(headers, rows, sheet_name=sheet_name)
