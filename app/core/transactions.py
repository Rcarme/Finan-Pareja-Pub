"""Límites de escritura compartidos por servicios y formularios (RN-24)."""

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar, cast

from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

F = TypeVar("F", bound=Callable[..., Any])


class ConcurrencyError(Exception):
    """Una escritura intentó usar una versión que ya cambió (RN-10)."""


def commit_service(db: Session) -> None:
    """El formulario confirma efecto y resultado juntos; servicios directos confirman solos."""
    if db.info.get("pending_operation") is not None:
        db.flush()
    else:
        db.commit()


def atomic_service(function: F) -> F:
    """Una excepción descarta también los flush de los servicios internos."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        db: Session = args[0] if args else kwargs["db"]
        try:
            return function(*args, **kwargs)
        except StaleDataError as exc:
            db.rollback()
            raise ConcurrencyError(
                "El movimiento cambió mientras lo editabas. Recargá y revisá los cambios."
            ) from exc
        except Exception:
            db.rollback()
            raise

    return cast(F, wrapped)
