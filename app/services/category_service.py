"""Organización auditada de categorías; visibilidad independiente de la imputación."""

from collections.abc import Sequence

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.audit import record_audit
from app.core.transactions import ConcurrencyError, atomic_service, commit_service
from app.domain.category_organization import CategoryMergeConfiguration, validate_category_merge
from app.domain.ownership import validate_activity
from app.domain.reports import BALANCE_ADJUSTMENT_CATEGORY
from app.models.auth import User
from app.models.ledger import Category


def _invalidate_ai_cache() -> None:
    try:
        from app.services.ai_assistant_service import invalidate_ai_context_cache

        invalidate_ai_context_cache()
    except Exception:
        pass


def canonical_categories(db: Session) -> dict[int, Category]:
    """Alias de lectura: nunca cambia el movimiento ni el snapshot original."""
    categories = {
        category.id: category
        for category in db.scalars(select(Category).execution_options(populate_existing=True))
    }
    return {
        key: categories.get(category.merged_into_id, category)
        if category.merged_into_id is not None
        else category
        for key, category in categories.items()
    }


def canonical_category(db: Session, category_id: int) -> Category | None:
    category = db.scalar(
        select(Category).where(Category.id == category_id).execution_options(populate_existing=True)
    )
    if category is not None and category.merged_into_id is not None:
        return db.scalar(
            select(Category)
            .where(Category.id == category.merged_into_id)
            .execution_options(populate_existing=True)
        )
    return category


def alias_category_ids(db: Session, category_id: int) -> list[int]:
    categories = canonical_categories(db)
    category = categories.get(category_id)
    if category is None:
        return []
    return [key for key, candidate in categories.items() if candidate.id == category.id]


def list_categories(
    db: Session,
    user_id: int,
    *,
    show_all: bool = False,
    include_id: int | None = None,
    kind: str | None = None,
) -> list[Category]:
    if kind not in (None, "income", "expense"):
        raise ValueError("Tipo de categoría inválido.")
    eligible: ColumnElement[bool] = (
        Category.active.is_(True)
        & Category.merged_into_id.is_(None)
        & ~(Category.is_system.is_(True) & (Category.name == BALANCE_ADJUSTMENT_CATEGORY))
    )
    if not show_all:
        eligible &= or_(Category.owner_user_id.is_(None), Category.owner_user_id == user_id)
    if include_id is not None:
        eligible = or_(eligible, Category.id == include_id)
    statement = select(Category).where(eligible)
    if kind is not None:
        statement = statement.where(Category.kind == kind)
    return list(db.scalars(statement.order_by(Category.name, Category.id)))


def _lock_categories(db: Session, ids: list[int]) -> dict[int, Category]:
    # El mismo orden global del libro evita que una carga atraviese una unificación.
    db.execute(text("SELECT pg_advisory_xact_lock(170170)"))
    return {
        category.id: category
        for category in db.scalars(
            select(Category)
            .where(Category.id.in_(ids))
            .order_by(Category.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }


def _check_version(category: Category, expected_version: int | None) -> None:
    if category.version != expected_version:
        raise ConcurrencyError("La categoría cambió. Recargá y revisá los cambios.")


@atomic_service
def set_category_owner(
    db: Session,
    category_id: int,
    owner_user_id: int | None,
    actor_id: int,
    expected_version: int,
    *,
    activity: str | None = None,
) -> Category:
    category = _lock_categories(db, [category_id]).get(category_id)
    if category is None or category.merged_into_id is not None:
        raise ValueError("La categoría no está disponible.")
    _check_version(category, expected_version)
    if owner_user_id is not None:
        owner = db.get(User, owner_user_id)
        if owner is None or not owner.active:
            raise ValueError("La persona debe existir y estar activa.")
    resolved_activity = validate_activity(activity) if activity is not None else None
    before: dict[str, int | str | None] = {
        "owner_user_id": category.owner_user_id,
        "version": category.version,
    }
    after: dict[str, int | str | None] = {
        "owner_user_id": owner_user_id,
    }
    if resolved_activity is not None and resolved_activity != category.activity:
        before["activity"] = category.activity
        category.activity = resolved_activity
        after["activity"] = resolved_activity
        if resolved_activity == "business" and category.default_charged_to_user_id is not None:
            before["default_charged_to_user_id"] = category.default_charged_to_user_id
            category.default_charged_to_user_id = None
            after["default_charged_to_user_id"] = None
    category.owner_user_id = owner_user_id
    category.version += 1
    after["version"] = category.version
    record_audit(
        db,
        "Category",
        category.id,
        "UPDATE",
        before=before,
        after=after,
        user_id=actor_id,
    )
    commit_service(db)
    _invalidate_ai_cache()
    return category


@atomic_service
def make_household_category_common(
    db: Session, category_id: int, actor_id: int, expected_version: int
) -> Category:
    """Configura futuras cargas comunes sin reatribuir movimientos históricos."""
    actor = db.get(User, actor_id)
    if actor is None or not actor.active:
        raise ValueError("El actor debe existir y estar activo.")
    category = _lock_categories(db, [category_id]).get(category_id)
    if (
        category is None
        or not category.active
        or category.merged_into_id is not None
        or category.kind != "expense"
        or category.activity != "household"
    ):
        raise ValueError("Se requiere una categoría activa de gasto del hogar sin unificar.")
    _check_version(category, expected_version)
    if category.owner_user_id is None and category.default_charged_to_user_id is None:
        return category
    before = {
        "owner_user_id": category.owner_user_id,
        "default_charged_to_user_id": category.default_charged_to_user_id,
        "version": category.version,
    }
    category.owner_user_id = None
    category.default_charged_to_user_id = None
    category.version += 1
    record_audit(
        db,
        "Category",
        category.id,
        "UPDATE",
        before=before,
        after={
            "owner_user_id": None,
            "default_charged_to_user_id": None,
            "version": category.version,
        },
        user_id=actor_id,
    )
    commit_service(db)
    _invalidate_ai_cache()
    return category


@atomic_service
def configure_business_categories(
    db: Session,
    actor_id: int,
    names: Sequence[str] = (
        "Ingreso secundario",
        "Insumos de trabajo",
        "Equipamiento de trabajo",
        "Herramientas de trabajo",
        "Impuestos de la pelu",
    ),
) -> list[Category]:
    """Asigna de forma idempotente y auditada la actividad 'business' a las categorías del local."""
    categories = list(
        db.scalars(
            select(Category)
            .where(
                Category.active.is_(True),
                Category.merged_into_id.is_(None),
                Category.name.in_(list(names)),
            )
            .order_by(Category.id)
            .execution_options(populate_existing=True)
        )
    )
    updated: list[Category] = []
    for category in categories:
        if category.activity != "business":
            updated.append(
                set_category_owner(
                    db,
                    category.id,
                    category.owner_user_id,
                    actor_id,
                    category.version,
                    activity="business",
                )
            )
    commit_service(db)
    _invalidate_ai_cache()
    return updated


@atomic_service
def merge_categories(
    db: Session,
    target_id: int,
    source_ids: list[int],
    actor_id: int,
    expected_versions: dict[int, int],
) -> Category:
    if not source_ids or target_id in source_ids or len(source_ids) != len(set(source_ids)):
        raise ValueError("Elegí categorías de origen distintas del destino.")
    ids = [target_id, *source_ids]
    categories = _lock_categories(db, ids)
    if len(categories) != len(ids):
        raise ValueError("Una categoría no existe.")
    target = categories[target_id]
    for category in categories.values():
        _check_version(category, expected_versions.get(category.id))

    def configuration(category: Category) -> CategoryMergeConfiguration:
        return CategoryMergeConfiguration(
            category.name,
            category.kind,
            category.activity,
            category.default_charged_to_user_id,
            category.default_period_offset,
            category.is_system,
            category.active,
            category.merged_into_id,
        )

    validate_category_merge(
        configuration(target), [configuration(categories[source_id]) for source_id in source_ids]
    )
    # Rechazamos cadenas: el destino puede tener alias, pero no puede pasar a ser otro alias.
    if (
        db.scalar(select(Category.id).where(Category.merged_into_id.in_(source_ids)).limit(1))
        is not None
    ):
        raise ValueError("No se puede unificar una categoría que ya tiene alias históricos.")
    for source_id in source_ids:
        source = categories[source_id]
        before = {
            "active": source.active,
            "merged_into_id": source.merged_into_id,
            "version": source.version,
        }
        source.active = False
        source.merged_into_id = target.id
        source.version += 1
        record_audit(
            db,
            "Category",
            source.id,
            "UPDATE",
            before=before,
            after={"active": False, "merged_into_id": target.id, "version": source.version},
            user_id=actor_id,
        )
    target.version += 1
    record_audit(
        db,
        "Category",
        target.id,
        "UPDATE",
        before={"version": target.version - 1},
        after={"version": target.version, "merged_source_ids": source_ids},
        user_id=actor_id,
    )
    commit_service(db)
    _invalidate_ai_cache()
    return target
