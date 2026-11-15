"""Tipos de filtros: un selector vacío representa Todos, nunca un ID inválido."""

from typing import Annotated, TypeAlias

from pydantic import BeforeValidator


def empty_identifier_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


OptionalIdentifier: TypeAlias = Annotated[int | None, BeforeValidator(empty_identifier_to_none)]


def parse_checkbox(value: object) -> bool:
    """Accept only the explicit HTML checkbox value, including direct route defaults."""
    if not isinstance(value, str) or value == "":
        return False
    if value != "1":
        raise ValueError("Confirmación inválida: marcá la casilla para continuar.")
    return True
