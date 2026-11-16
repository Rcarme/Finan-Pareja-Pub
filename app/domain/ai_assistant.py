"""Lógica de dominio pura para el asistente de carga inteligente con IA.

Modelos de datos inmutables y funciones puras sin I/O (RN-01, RN-10, RN-37).
Todo importe monetario se maneja como Decimal con ROUND_HALF_UP a dos decimales.
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class AIDraftItem:
    """Borrador estructurado de un movimiento propuesto por la IA."""

    description: str
    amount: Decimal
    category_name: str
    account_name: str
    activity: str = "household"
    notes: str | None = None
    charged_to_user_name: str | None = None
    tx_date: str | None = None
    duplicate_warning: str | None = None

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("La descripción del movimiento no puede estar vacía.")
        if (
            not isinstance(self.amount, Decimal)
            or not self.amount.is_finite()
            or self.amount <= Decimal("0")
        ):
            raise ValueError("El monto debe ser un Decimal positivo y finito.")
        if self.activity not in ("household", "personal"):
            raise ValueError("La actividad debe ser 'household' o 'personal'.")


@dataclass(frozen=True)
class ExistingTxRecord:
    """Representación liviana e inmutable de un movimiento contable para chequear duplicados."""

    id: int
    date: str  # YYYY-MM-DD
    amount: Decimal
    description: str
    account_name: str | None = None


def format_money_ar_pure(amount: Decimal) -> str:
    """Formatea dinero al estándar argentino '$ 1.234.567,89' de forma pura sin dependencias."""
    s = f"{amount:.2f}"
    int_part, dec_part = s.split(".")
    res: list[str] = []
    for i, ch in enumerate(reversed(int_part)):
        if i > 0 and i % 3 == 0:
            res.append(".")
        res.append(ch)
    return "$" + " " + "".join(reversed(res)) + "," + dec_part


def format_date_ar_pure(date_iso: str) -> str:
    """Convierte 'YYYY-MM-DD' a 'DD/MM/YYYY'."""
    parts = date_iso.split("-")
    if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    return date_iso


def _normalize_text(text: str) -> str:
    """Normaliza un texto eliminando puntuación y mayúsculas para comparar similitud."""
    return re.sub(r"[^\w\s]", "", (text or "").lower()).strip()


def check_item_duplicates_pure(
    items: list[AIDraftItem],
    existing_records: list[ExistingTxRecord],
    fallback_date: str,
) -> list[tuple[AIDraftItem, str | None]]:
    """Evalúa duplicados contra movimientos existentes y dentro del lote."""
    results: list[tuple[AIDraftItem, str | None]] = []

    for i, item in enumerate(items):
        item_d = item.tx_date or fallback_date
        item_desc_norm = _normalize_text(item.description)
        warning: str | None = None

        # 1. Comparar contra base de datos existente (mismo importe y fecha)
        for ex in existing_records:
            if ex.amount == item.amount and ex.date == item_d:
                acc_txt = f" en {ex.account_name}" if ex.account_name else ""
                ex_desc_norm = _normalize_text(ex.description)
                same_desc = (
                    item_desc_norm == ex_desc_norm
                    or item_desc_norm in ex_desc_norm
                    or ex_desc_norm in item_desc_norm
                )
                if same_desc:
                    warning = (
                        "Duplicado exacto: ya existe un gasto de "
                        f"{format_money_ar_pure(ex.amount)} el {format_date_ar_pure(ex.date)} "
                        f"con el mismo concepto ('{ex.description}'{acc_txt})."
                    )
                else:
                    warning = (
                        f"Mismo importe y fecha: ya existe un gasto por "
                        f"{format_money_ar_pure(ex.amount)} el {format_date_ar_pure(ex.date)} "
                        f"('{ex.description}'{acc_txt}). Verificá si está duplicado."
                    )
                break

        # 2. Si no hay duplicado en BD, comparar contra ítems previos en este mismo lote
        if not warning:
            for prev_idx, prev_item in enumerate(items[:i]):
                prev_d = prev_item.tx_date or fallback_date
                if prev_item.amount == item.amount and prev_d == item_d:
                    prev_desc_norm = _normalize_text(prev_item.description)
                    same_desc = (
                        item_desc_norm == prev_desc_norm
                        or item_desc_norm in prev_desc_norm
                        or prev_desc_norm in item_desc_norm
                    )
                    if same_desc:
                        warning = (
                            f"Ítem duplicado en este texto: coincide en concepto, importe "
                            f"({format_money_ar_pure(item.amount)}) y fecha con el Ítem "
                            f"{prev_idx + 1} ('{prev_item.description}')."
                        )
                    else:
                        warning = (
                            f"Posible ítem repetido en este texto: coincide en importe "
                            f"({format_money_ar_pure(item.amount)}) y fecha "
                            f"({format_date_ar_pure(item_d)}) con el Ítem {prev_idx + 1} "
                            f"('{prev_item.description}')."
                        )
                    break

        results.append((item, warning))

    return results


def parse_llm_json_response(raw_text: str) -> list[dict[str, Any]]:
    """Limpia bloques de código markdown y parsea el array JSON de la IA."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"La respuesta de la IA no es un JSON válido: {exc}") from exc

    if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
        data = data["items"]

    if not isinstance(data, list):
        raise ValueError("Se esperaba una lista de movimientos en el JSON de la IA.")

    return [item for item in data if isinstance(item, dict)]


def sanitize_draft_item(
    raw: dict[str, Any],
    valid_categories: list[str],
    valid_accounts: list[str],
    default_account: str,
    default_category: str,
) -> AIDraftItem:
    """Valida y convierte un diccionario crudo de la IA en un AIDraftItem estricto."""
    raw_desc = str(raw.get("description", "")).strip()
    description = raw_desc[:200] if raw_desc else "Gasto sin descripción"

    raw_amount = raw.get("amount")
    try:
        if raw_amount is None:
            raise ValueError("Monto ausente")
        amount = Decimal(str(raw_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount <= Decimal("0") or not amount.is_finite():
            raise ValueError("Monto no positivo")
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Importe inválido '{raw_amount}': debe ser un número positivo.") from exc

    cat_candidate = str(raw.get("category", "")).strip()
    matched_cat = next(
        (c for c in valid_categories if c.lower() == cat_candidate.lower()),
        default_category,
    )

    acc_candidate = str(raw.get("account", "")).strip()
    matched_acc = next(
        (a for a in valid_accounts if a.lower() == acc_candidate.lower()),
        default_account,
    )

    activity = str(raw.get("activity", "household")).strip().lower()
    if activity not in ("household", "personal"):
        activity = "household"

    notes = str(raw.get("notes", "")).strip() or None
    user_name = str(raw.get("charged_to_user_name", "")).strip() or None

    raw_date = str(raw.get("date") or raw.get("tx_date") or "").strip()
    tx_date: str | None = None
    if raw_date and raw_date.lower() not in ("null", "none"):
        # Match YYYY-MM-DD
        m_iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw_date)
        if m_iso:
            y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
            try:
                candidate = date(y, m, d)
                tx_date = candidate.isoformat()
            except ValueError:
                tx_date = None
        else:
            # Match DD/MM/YYYY or DD-MM-YYYY
            m_ar = re.search(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", raw_date)
            if m_ar:
                d, m, y = int(m_ar.group(1)), int(m_ar.group(2)), int(m_ar.group(3))
                try:
                    candidate = date(y, m, d)
                    tx_date = candidate.isoformat()
                except ValueError:
                    tx_date = None

    return AIDraftItem(
        description=description,
        amount=amount,
        category_name=matched_cat,
        account_name=matched_acc,
        activity=activity,
        notes=notes,
        charged_to_user_name=user_name,
        tx_date=tx_date,
    )


def build_system_instruction(
    categories: list[str],
    accounts: list[str],
    users: list[str],
) -> str:
    """Genera la instrucción de sistema fija para el Agente Contable con IA."""
    cat_list = "\n".join(f"  - {c}" for c in sorted(categories))
    acc_list = "\n".join(f"  - {a}" for a in sorted(accounts))
    user_list = ", ".join(users)

    return (
        f"Eres el Agente Contable Inteligente de la app 'Tandem' para una pareja "
        f"({user_list}).\n"
        "Tu misión es interpretar textos desestructurados (comprobantes de compra, tickets, "
        "mensajes) y extraer cada gasto como un movimiento contable individual listo para "
        "ingresar.\n\n"
        f"CUENTAS DISPONIBLES EN EL SISTEMA:\n{acc_list}\n\n"
        f"CATEGORÍAS DISPONIBLES EN EL SISTEMA:\n{cat_list}\n\n"
        "REGLAS DE INTERPRETACIÓN:\n"
        "1. Divide las compras compuestas en ítems individuales si tienen destinatarios o "
        "categorías distintas.\n"
        "2. Si es gasto compartido del hogar, la actividad es 'household'. Si es personal de "
        "uno, 'personal'.\n"
        "3. Mapea la cuenta mencionada a la cuenta disponible más exacta "
        "(ej: 'tarjeta de crédito' -> 'Tarjeta compartida').\n"
        "4. Mapea a la categoría más adecuada del catálogo provisto.\n"
        "5. Los importes numéricos deben ser números positivos limpios sin símbolos.\n"
        "6. Si el usuario aclara destinatarios (ej: 'regalo de cumpleaños'), inclúyelo en la "
        "descripción y en 'notes'.\n"
        "7. Si el texto contiene una fecha explícita de compra o ticket (ej: '08/11/2026', "
        "'10 de octubre', 'Fecha 08/11/2026'), extráela y conviértela a formato 'YYYY-MM-DD' en el "
        "campo 'date'. Si no se menciona ninguna fecha, usa null.\n\n"
        "Responde ÚNICAMENTE con un array JSON de objetos con el siguiente esquema:\n"
        "[\n"
        "  {\n"
        '    "description": "Nombre claro del producto o gasto (ej: Compra de víveres)",\n'
        '    "amount": 2500.00,\n'
        '    "date": "YYYY-MM-DD o null si no se especifica",\n'
        '    "category": "Nombre exacto de una categoría válida",\n'
        '    "account": "Nombre exacto de una cuenta válida",\n'
        '    "activity": "household" o "personal",\n'
        '    "notes": "Detalles adicionales o destinatario",\n'
        '    "charged_to_user_name": "Nombre de usuario si es particular o vacío"\n'
        "  }\n"
        "]\n"
    )
