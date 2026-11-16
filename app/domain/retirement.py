"""Aportes externos registrados: información bruta, nunca saldo de una póliza."""

import datetime as dt
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RetirementEntry:
    transaction_id: int | None
    date: dt.date
    period: dt.date
    description: str
    amount: Decimal
    account_name: str
    category_name: str
    person_id: int | None
    user_name: str | None
    policy: str = "unassigned"
    reference: str = ""


@dataclass(frozen=True)
class RetirementContributionInput:
    policy: str
    date: dt.date
    amount: Decimal
    source_account_id: int
    reference: str
    source_transaction_id: int | None = None


POLICIES = {
    "san_cristobal": "San Cristóbal",
    "cnp_1": "CNP 0001-019",
    "cnp_2": "CNP 0001-024",
    "unassigned": "Sin asignar a póliza",
}


def validate_retirement_contributions(
    entries: Sequence[RetirementContributionInput],
    today: dt.date,
) -> tuple[RetirementContributionInput, ...]:
    if not 1 <= len(entries) <= 1000:
        raise ValueError("Se requieren entre 1 y 1000 aportes explícitos.")
    references = set()
    for entry in entries:
        if entry.policy not in POLICIES or entry.policy == "unassigned":
            raise ValueError("La póliza no pertenece al catálogo confirmado.")
        if (
            not isinstance(entry.amount, Decimal)
            or not entry.amount.is_finite()
            or not Decimal("0") < entry.amount <= Decimal("999999999999.99")
            or entry.amount != entry.amount.quantize(Decimal("0.01"))
        ):
            raise ValueError("El aporte requiere un importe positivo decimal a centavos.")
        if type(entry.date) is not dt.date or entry.date > today:
            raise ValueError("La fecha debe ser real y no futura.")
        if (
            type(entry.source_account_id) is not int
            or entry.source_account_id <= 0
            or (
                entry.source_transaction_id is not None
                and (
                    type(entry.source_transaction_id) is not int or entry.source_transaction_id <= 0
                )
            )
        ):
            raise ValueError("La cuenta y el movimiento requieren identificadores válidos.")
        if (
            not isinstance(entry.reference, str)
            or not entry.reference.strip()
            or entry.reference != entry.reference.strip()
            or len(entry.reference) > 120
            or entry.reference in references
        ):
            raise ValueError("La referencia debe ser única, explícita y de hasta 120 caracteres.")
        references.add(entry.reference)
    return tuple(entries)


@dataclass(frozen=True)
class RetirementPolicy:
    key: str
    name: str
    total: Decimal
    common: Decimal
    personal: Decimal
    count: int


def retirement_breakdown_matches(total: Decimal, amounts: Sequence[Decimal]) -> bool:
    return bool(amounts) and sum(amounts, Decimal("0.00")) == total


@dataclass(frozen=True)
class RetirementMonth:
    period: dt.date
    total: Decimal
    common: Decimal
    personal: Decimal
    count: int


@dataclass(frozen=True)
class RetirementReport:
    total_registered: Decimal
    common_registered: Decimal
    personal_registered: Decimal
    record_count: int
    month_count: int
    first_date: dt.date | None
    last_date: dt.date | None
    months: tuple[RetirementMonth, ...]
    entries: tuple[RetirementEntry, ...]
    selected_period: dt.date | None
    month_total: Decimal
    month_common: Decimal
    month_personal: Decimal
    month_count_records: int
    policies: tuple[RetirementPolicy, ...] = ()
    allocation_warning_count: int = 0
    month_ledger_total: Decimal = Decimal("0.00")


def is_retirement_category(original_name: str, canonical_name: str) -> bool:
    return any(
        name.strip().casefold() == "seguro de retiro" for name in (original_name, canonical_name)
    )


def is_retirement_contribution(original_name: str, canonical_name: str, description: str) -> bool:
    """Solo aseguradoras confirmadas y el agrupado revisado; categoría no basta."""
    normalized = "".join(
        char
        for char in unicodedata.normalize("NFKD", description.casefold())
        if not unicodedata.combining(char)
    )
    normalized = " ".join(normalized.split())
    if re.search(r"\b(auto|automotor|zurich)\b", normalized):
        return False
    if re.search(r"\bcnp\b|\bsan\s*cristobal\b", normalized):
        return True
    return normalized == "seguro de retiro * 3" and is_retirement_category(
        original_name, canonical_name
    )


def _totals(entries: Sequence[RetirementEntry]) -> tuple[Decimal, Decimal, Decimal]:
    common = sum((e.amount for e in entries if e.person_id is None), Decimal("0.00"))
    personal = sum((e.amount for e in entries if e.person_id is not None), Decimal("0.00"))
    return common + personal, common, personal


def build_retirement_report(
    entries: Sequence[RetirementEntry], period: dt.date | None = None
) -> RetirementReport:
    for entry in entries:
        if (
            not isinstance(entry.amount, Decimal)
            or not entry.amount.is_finite()
            or not Decimal("0") < entry.amount <= Decimal("999999999999.99")
            or entry.amount != entry.amount.quantize(Decimal("0.01"))
        ):
            raise ValueError(
                "El aporte registrado debe ser un importe decimal positivo a centavos."
            )
    ordered = tuple(
        sorted(entries, key=lambda e: (e.date, e.transaction_id or 0, e.reference), reverse=True)
    )
    periods = sorted({e.period.replace(day=1) for e in entries}, reverse=True)
    months = []
    for month in periods:
        matching = [e for e in entries if e.period.replace(day=1) == month]
        total, common, personal = _totals(matching)
        months.append(RetirementMonth(month, total, common, personal, len(matching)))
    selected = period.replace(day=1) if period else None
    selected_rows = [e for e in entries if e.period.replace(day=1) == selected]
    month_total, month_common, month_personal = _totals(selected_rows)
    total, common, personal = _totals(entries)
    policies = []
    for key, name in POLICIES.items():
        matching = [e for e in entries if e.policy == key]
        policy_total, policy_common, policy_personal = _totals(matching)
        policies.append(
            RetirementPolicy(key, name, policy_total, policy_common, policy_personal, len(matching))
        )
    return RetirementReport(
        total,
        common,
        personal,
        len(entries),
        len(periods),
        min((e.date for e in entries), default=None),
        max((e.date for e in entries), default=None),
        tuple(months),
        ordered,
        selected,
        month_total,
        month_common,
        month_personal,
        len(selected_rows),
        tuple(policies),
        0,
        sum(
            (entry.amount for entry in selected_rows if entry.transaction_id is not None),
            Decimal("0.00"),
        ),
    )
