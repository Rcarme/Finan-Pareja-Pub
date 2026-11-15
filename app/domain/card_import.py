"""Normalización pura de CSV con mapeo explícito, sin IA ni conversión automática."""

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


@dataclass(frozen=True)
class ColumnMapping:
    date: str
    merchant: str
    amount: str
    currency: str = ""
    installments: str = ""


@dataclass(frozen=True)
class CardRow:
    date: date
    merchant: str
    amount: Decimal
    currency: str
    kind: str
    remaining_installments: int


def normalize_merchant(value: str) -> str:
    return " ".join(value.casefold().split())


def parse_statement(
    content: bytes,
    mapping: ColumnMapping,
    *,
    delimiter: str = ",",
    encoding: str = "utf-8-sig",
    date_format: str = "%d/%m/%Y",
    decimal_separator: str = ",",
    expense_positive: bool = True,
) -> list[CardRow]:
    if len(content) > 2_000_000 or not content:
        raise ValueError("El CSV debe contener datos y pesar como máximo 2 MB.")
    if delimiter not in (",", ";", "\t") or decimal_separator not in (",", "."):
        raise ValueError("Separador inválido.")
    try:
        text = content.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise ValueError("No se pudo leer el CSV con esa codificación.") from exc
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter, strict=True)
        required = [mapping.date, mapping.merchant, mapping.amount]
        required.extend(value for value in (mapping.currency, mapping.installments) if value)
        if not reader.fieldnames or not set(required) <= set(reader.fieldnames):
            raise ValueError("Las columnas indicadas no existen en el CSV.")
        result: list[CardRow] = []
        for index, values in enumerate(reader, 2):
            if index > 2001:
                raise ValueError("Máximo 2000 consumos por resumen.")
            try:
                row_date = datetime.strptime(values[mapping.date], date_format).date()
                merchant = values[mapping.merchant].strip()
                if not merchant or len(merchant) > 255:
                    raise ValueError("comercio vacío o demasiado largo")
                raw_amount = values[mapping.amount].strip().replace("$", "").replace(" ", "")
                thousands = "." if decimal_separator == "," else ","
                amount = Decimal(raw_amount.replace(thousands, "").replace(decimal_separator, "."))
                if (
                    not amount.is_finite()
                    or amount == 0
                    or abs(amount) > Decimal("999999999999.99")
                ):
                    raise ValueError("importe inválido")
                kind = "expense" if (amount > 0) == expense_positive else "income"
                currency = values[mapping.currency].strip().upper() if mapping.currency else "ARS"
                if currency not in ("ARS", "USD"):
                    raise ValueError("moneda inválida; use ARS o USD")
                remaining = 0
                installments = values[mapping.installments].strip() if mapping.installments else ""
                if installments:
                    current, total = map(int, installments.split("/"))
                    if not 1 <= current <= total <= 120:
                        raise ValueError("cuotas inválidas")
                    remaining = total - current
                result.append(
                    CardRow(
                        row_date,
                        merchant,
                        abs(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                        currency,
                        kind,
                        remaining,
                    )
                )
            except (ValueError, InvalidOperation, TypeError, AttributeError) as exc:
                raise ValueError(f"Fila {index}: {exc}") from exc
    except csv.Error as exc:
        raise ValueError("CSV inválido: revisá la estructura y el tamaño de los campos.") from exc
    if not result:
        raise ValueError("El CSV no contiene consumos.")
    return result
