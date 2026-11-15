"""Catálogos cerrados y presentación informativa, sin efectos contables."""

from collections.abc import Mapping
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType

from app.domain.emoji_catalog import EMOJI_ICONS

ICONS: Mapping[str, str] = MappingProxyType(
    {
        "receipt": "Comprobante",
        "cart": "Compras",
        "home": "Hogar",
        "car": "Transporte",
        "food": "Comida",
        "health": "Salud",
        "education": "Educación",
        "gift": "Regalos",
        "wallet": "Dinero",
        "work": "Trabajo",
        "music": "Música",
        "scissors": "Tijeras",
        "dye": "Pomo de tintura",
        "other": "Otros",
    }
)
COLORS: Mapping[str, str] = MappingProxyType(
    {
        "blue": "#38bdf8",
        "green": "#22c55e",
        "amber": "#f59e0b",
        "rose": "#fb7185",
        "violet": "#a78bfa",
        "teal": "#2dd4bf",
        "slate": "#94a3b8",
    }
)

RECOMMENDED_EMOJIS: Mapping[str, str] = MappingProxyType(
    {
        "🚗": "Auto",
        "💼": "Negocio / Emprendimiento",
        "🐱": "Mascotas",
        "⛽": "Combustible",
        "🛒": "Supermercado",
        "🏠": "Hogar",
        "🍔": "Comida",
        "💡": "Servicios",
        "💊": "Salud",
        "🎁": "Regalos",
        "👔": "Trabajo",
        "💵": "Dinero",
        "🍕": "Pizza / Delivery",
        "🥩": "Carnicería",
        "🥦": "Verdulería",
        "🥖": "Panadería",
        "🥐": "Facturas / Merienda",
        "☕": "Cafetería",
        "🧉": "Mate / Yerba",
        "🍺": "Cerveza / Bar",
        "🍷": "Vino / Bebidas",
        "🍦": "Heladería",
        "🍽️": "Restaurante",
        "🧹": "Limpieza",
        "🧺": "Lavandería",
        "🛋️": "Muebles / Deco",
        "🔧": "Ferretería / Arreglos",
        "🪴": "Plantas / Jardín",
        "📱": "Celular / Internet",
        "💻": "Tecnología",
        "📺": "Streaming / TV",
        "🔑": "Alquiler / Expensas",
        "📦": "Envíos / Compras online",
        "💇‍♀️": "Corte / Peinado",
        "🧴": "Insumos / Cremas",
        "💅": "Estética / Uñas",
        "💄": "Cosmética",
        "🪒": "Barbería",
        "👕": "Ropa",
        "👟": "Calzado",
        "👜": "Accesorios",
        "🚌": "Colectivo / SUBE",
        "🚕": "Taxi / Uber",
        "🛞": "Taller / Gomero",
        "🅿️": "Estacionamiento / Peaje",
        "✈️": "Viajes / Vuelos",
        "🏖️": "Vacaciones",
        "🏨": "Alojamiento",
        "🏥": "Médico / Clínica",
        "🦷": "Odontólogo",
        "👓": "Óptica",
        "🏋️": "Gimnasio",
        "⚽": "Deportes",
        "🐶": "Perro / Veterinaria",
        "🐾": "Alimento mascotas",
        "🎵": "Música / Recitales",
        "🎬": "Cine / Salidas",
        "🎮": "Juegos",
        "📚": "Libros / Estudio",
        "🎓": "Cursos / Educación",
        "🎉": "Eventos / Fiestas",
        "🎨": "Arte / Hobbies",
        "💳": "Tarjeta",
        "🏦": "Banco",
        "🧾": "Impuestos / Monotributo",
        "🛡️": "Seguros / Retiro",
        "💰": "Ahorro / Sueldo",
        "📈": "Inversiones",
        "🏢": "Departamento",
        "🤝": "Reintegros",
        "⭐": "Destacado",
        "❓": "Varios",
    }
)


def validate_appearance(icon: str, color: str) -> None:
    if icon not in ICONS and icon not in EMOJI_ICONS:
        raise ValueError("Elegí un icono SVG del catálogo o un único emoji de 1 a 4 codepoints.")
    if color not in COLORS:
        raise ValueError("El color no pertenece al catálogo permitido.")


def date_window(window: str, today: date) -> tuple[date, date]:
    """Rango informativo por fecha; el final exclusivo incluye el domingo completo."""
    if window == "day":
        return today, today + timedelta(days=1)
    if window == "week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)
    if window == "month":
        start = today.replace(day=1)
        end = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        return start, end
    if window == "year":
        return date(today.year, 1, 1), date(today.year + 1, 1, 1)
    raise ValueError("El rango informativo debe ser day, week, month o year.")


def display_percentage(amount: Decimal, total: Decimal) -> Decimal:
    """Porcentaje visual; no participa del cierre ni modifica el reparto."""
    if not amount.is_finite() or not total.is_finite() or amount < 0 or total < 0:
        raise ValueError("Los importes del porcentaje deben ser finitos y no negativos.")
    if total == 0:
        return Decimal("0.00")
    return (amount * Decimal("100") / total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
