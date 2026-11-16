"""Consumo informativo por atribución explícita; no cambia el reparto (Q34)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class SpendingPerson:
    user_id: int
    key: str
    name: str


@dataclass(frozen=True)
class SpendingSource:
    transaction_id: int
    date: date
    description: str
    kind: str
    amount: Decimal
    person_id: int | None
    category_id: int
    category_name: str


@dataclass(frozen=True)
class SpendingEntry(SpendingSource):
    group_key: str
    net_amount: Decimal


@dataclass(frozen=True)
class SpendingCategory:
    category_id: int
    category_name: str
    expense: Decimal
    refunds: Decimal
    net: Decimal
    count: int


@dataclass(frozen=True)
class SpendingBucket:
    key: str
    name: str
    expense: Decimal
    refunds: Decimal
    net: Decimal
    percentage: Decimal | None
    categories: tuple[SpendingCategory, ...]


@dataclass(frozen=True)
class SpendingShare:
    key: str
    name: str
    own_net: Decimal
    common_share: Decimal
    total: Decimal
    percentage: Decimal | None


@dataclass(frozen=True)
class SpendingReport:
    period: date
    people: tuple[SpendingPerson, SpendingPerson]
    buckets: tuple[SpendingBucket, ...]
    shares: tuple[SpendingShare, ...]
    entries: tuple[SpendingEntry, ...]
    total_expense: Decimal
    total_refunds: Decimal
    total_net: Decimal
    rounding_residual: Decimal
    percentages_available: bool


def is_spending_refund(
    kind: str, original_name: str, canonical_name: str, generated_by: str, account_type: str
) -> bool:
    refund = "reintegros de tarjeta"
    return kind == "income" and (
        original_name.strip().casefold() == refund
        or canonical_name.strip().casefold() == refund
        or (generated_by == "card_import" and account_type == "credit")
    )


def _totals(entries: Sequence[SpendingEntry]) -> tuple[Decimal, Decimal, Decimal]:
    expense = sum((e.amount for e in entries if e.kind == "expense"), Decimal("0.00"))
    refunds = sum((e.amount for e in entries if e.kind == "income"), Decimal("0.00"))
    return expense, refunds, expense - refunds


def build_spending_report(
    period: date, people: tuple[SpendingPerson, SpendingPerson], sources: Sequence[SpendingSource]
) -> SpendingReport:
    if (
        len(people) != 2
        or len({p.user_id for p in people}) != 2
        or any(p.user_id <= 0 for p in people)
    ):
        raise ValueError("El reporte requiere las dos personas de la pareja, sin duplicados.")
    identities = {p.user_id: p.key for p in people}
    entries = []
    for source in sources:
        if (
            not isinstance(source.amount, Decimal)
            or not source.amount.is_finite()
            or source.amount <= 0
            or source.amount != source.amount.quantize(Decimal("0.01"))
            or source.kind not in ("expense", "income")
        ):
            raise ValueError("El consumo debe tener importe decimal positivo y tipo válido.")
        if source.person_id is not None and source.person_id not in identities:
            raise ValueError("El movimiento está atribuido a una persona ajena a la pareja.")
        entries.append(
            SpendingEntry(
                source.transaction_id,
                source.date,
                source.description,
                source.kind,
                source.amount,
                source.person_id,
                source.category_id,
                source.category_name,
                identities[source.person_id] if source.person_id is not None else "common",
                source.amount if source.kind == "expense" else -source.amount,
            )
        )
    expense, refunds, net = _totals(entries)
    grouped = {
        key: [e for e in entries if e.group_key == key]
        for key in (people[0].key, people[1].key, "common")
    }
    totals = {key: _totals(rows) for key, rows in grouped.items()}
    percentages = net > 0 and all(row[2] >= 0 for row in totals.values())

    def percentage(value: Decimal) -> Decimal | None:
        return (
            (value * Decimal("100") / net).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if percentages
            else None
        )

    buckets = []
    for key, name in (
        (people[0].key, people[0].name),
        (people[1].key, people[1].name),
        ("common", "Los dos"),
    ):
        rows = grouped[key]
        categories = []
        for identity in sorted({e.category_id for e in rows}):
            matching = [e for e in rows if e.category_id == identity]
            cat_expense, cat_refunds, cat_net = _totals(matching)
            categories.append(
                SpendingCategory(
                    identity,
                    matching[0].category_name,
                    cat_expense,
                    cat_refunds,
                    cat_net,
                    len(matching),
                )
            )
        gross, returned, own = totals[key]
        buckets.append(
            SpendingBucket(key, name, gross, returned, own, percentage(own), tuple(categories))
        )
    common = totals["common"][2]
    half = (common / Decimal("2")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    shares = tuple(
        SpendingShare(
            person.key,
            person.name,
            totals[person.key][2],
            half,
            totals[person.key][2] + half,
            percentage(totals[person.key][2] + half),
        )
        for person in people
    )
    return SpendingReport(
        period.replace(day=1),
        people,
        tuple(buckets),
        shares,
        tuple(entries),
        expense,
        refunds,
        net,
        common - half * Decimal("2"),
        percentages,
    )
