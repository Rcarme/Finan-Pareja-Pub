"""Pure domain logic for ledger, accounts and funds (RN-06, RN-08, RN-18)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class MovementItem:
    """Represents a financial movement for pure domain calculations."""

    kind: str
    amount: Decimal
    account_id: int
    to_account_id: int | None = None
    recipient_user_id: int | None = None
    status: str = "active"
    period: date | None = None


def compute_account_balance(
    account_id: int,
    opening_balance: Decimal,
    movements: Sequence[MovementItem],
) -> Decimal:
    """Compute current account balance considering opening balance and active movements (RN-08)."""
    balance = opening_balance
    for m in movements:
        if m.status != "active":
            continue

        if m.account_id == account_id:
            if m.kind == "income":
                balance += m.amount
            elif m.kind in ("expense", "transfer", "distribution"):
                balance -= m.amount

        if m.kind == "transfer" and m.to_account_id == account_id:
            balance += m.amount

    return balance


def compute_total_fund(
    accounts_opening: Mapping[int, Decimal],
    movements: Sequence[MovementItem],
    period_cutoff: date | None = None,
) -> Decimal:
    """Compute total common fund up to period_cutoff by imputation period (RN-06, RN-18)."""
    total = sum(accounts_opening.values(), Decimal("0.00"))
    for m in movements:
        if m.status != "active":
            continue

        if period_cutoff is not None and m.period is not None and m.period > period_cutoff:
            continue

        if m.kind == "income":
            total += m.amount
        elif m.kind in ("expense", "distribution"):
            total -= m.amount
        elif m.kind == "transfer":
            # Transfers between internal accounts do not affect total fund (RN-06)
            pass

    return total


def validate_opening_omission(
    kind: str, tx_date: date, opening_date: date | None, confirmed: bool
) -> None:
    """An omission is an explicitly acknowledged expense predating the photo."""
    if confirmed and (kind != "expense" or opening_date is None or tx_date >= opening_date):
        raise ValueError("La omisión de apertura requiere un gasto anterior a la foto inicial.")


def resolve_omission_period(period: date, opening_date: date, explicit: bool) -> date:
    """Preserve the real date without charging a period absorbed by the opening."""
    first_period = opening_date.replace(day=1)
    if period < first_period:
        if explicit:
            raise ValueError("El gasto omitido debe imputarse al mes de apertura o uno posterior.")
        return first_period
    return period
