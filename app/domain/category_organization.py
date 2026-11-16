"""Reglas puras de unificación visual sin modificar la imputación contable."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CategoryMergeConfiguration:
    name: str
    kind: str
    activity: str
    charged_to_user_id: int | None
    period_offset: int
    is_system: bool
    active: bool
    merged_into_id: int | None


def validate_category_merge(
    target: CategoryMergeConfiguration, sources: list[CategoryMergeConfiguration]
) -> None:
    protected = {
        "Ajuste de conciliación",
        "Sueldo",
        "Alquiler cobrado",
        "Compromiso mensual",
        "Retiro de actividad comercial",
    }
    for category in [target, *sources]:
        if not category.active or category.merged_into_id is not None:
            raise ValueError("Las categorías deben estar activas y sin unificaciones previas.")
        if category.is_system and category.name in protected:
            raise ValueError("Esta categoría tiene una función del sistema y no se puede unificar.")
        if (
            category.kind,
            category.activity,
            category.charged_to_user_id,
            category.period_offset,
        ) != (target.kind, target.activity, target.charged_to_user_id, target.period_offset):
            raise ValueError(
                "Las categorías deben tener el mismo tipo, actividad, cargo y período por defecto."
            )
