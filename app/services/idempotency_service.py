"""Mecanismo reutilizable de idempotencia para formularios (F05.2 N7).

Tabla `idempotency_keys` con clave UNIQUE por ruta y referencia al resultado.
Flujo por ruta: `begin_operation` (reserva) → trabajo → `finish_operation`.
Un doble envío con el mismo token devuelve el resultado original sin duplicar.
Lo reutilizará el cierre de mes en F06.
"""

import hashlib
import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.idempotency import IdempotencyKey


def operation_payload(values: Mapping[str, object]) -> dict[str, object]:
    """Campos recibidos, sin sesión, conexiones ni secretos del formulario."""
    excluded = {"csrf_token", "idempotency_token", "idempotency_key"}
    return {
        key: value
        for key, value in values.items()
        if key not in excluded and isinstance(value, (str, int, bool, type(None)))
    }


class DuplicateCompletedError(Exception):
    """El token ya completó una operación: lleva el registro original."""

    def __init__(self, record: IdempotencyKey) -> None:
        super().__init__("Operación ya registrada con este token.")
        self.record = record


class OperationInProgressError(Exception):
    """El token tiene una operación en curso (reintento demasiado pronto)."""


def new_idempotency_token() -> str:
    """Genera un token único para un formulario recién renderizado."""
    return secrets.token_hex(16)


def begin_operation(
    db: Session,
    *,
    token: str | None,
    route: str,
    user_id: int,
    ttl_seconds: int = 300,
    payload: Mapping[str, object] | None = None,
) -> IdempotencyKey | None:
    """Reserva el token o devuelve el resultado original.

    - Sin token: devuelve None (sin protección, comportamiento anterior).
    - Token completado: lanza `DuplicateCompletedError` con el registro.
    - Token pendiente vigente: lanza `OperationInProgressError`.
    - Token pendiente expirado o nuevo: reserva y devuelve el registro.
    """
    if not token or not token.strip():
        return None
    key = token.strip()
    request_hash = (
        hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        if payload is not None
        else None
    )

    existing = db.execute(
        select(IdempotencyKey).where(IdempotencyKey.key == key)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.route != route or existing.created_by != user_id:
            raise ValueError(
                "El token pertenece a otro usuario u operación. Recargá el formulario."
            )
        if existing.request_hash != request_hash:
            raise ValueError("El token se usó con otros datos. Recargá el formulario.")
        if existing.status == "completed":
            raise DuplicateCompletedError(existing)
        age = datetime.now(UTC) - existing.created_at
        if age < timedelta(seconds=existing.expires_after_seconds):
            raise OperationInProgressError(
                "Esta operación ya está en curso; esperá unos segundos y recargá."
            )
        db.delete(existing)
        db.flush()

    record = IdempotencyKey(
        key=key,
        route=route,
        request_hash=request_hash,
        status="pending",
        created_by=user_id,
        expires_after_seconds=ttl_seconds,
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        # Carrera: otro request reservó primero; devolver su resultado.
        db.rollback()
        winner = db.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == key)
        ).scalar_one_or_none()
        if winner is not None and (
            winner.route != route
            or winner.created_by != user_id
            or winner.request_hash != request_hash
        ):
            raise ValueError("El token se usó con otros datos, usuario u operación.") from None
        if winner is not None and winner.status == "completed":
            raise DuplicateCompletedError(winner) from None
        raise OperationInProgressError(
            "Esta operación ya está en curso; esperá unos segundos y recargá."
        ) from None
    db.commit()
    db.refresh(record)
    db.info["pending_operation"] = record.id
    return record


def finish_operation(
    db: Session,
    record: IdempotencyKey,
    *,
    result_entity: str,
    result_id: int,
) -> None:
    """Marca la reserva como completada con referencia al resultado."""
    record.status = "completed"
    record.result_entity = result_entity
    record.result_id = result_id
    db.info.pop("pending_operation", None)
    db.commit()


def abort_operation(db: Session, record: IdempotencyKey) -> None:
    """Libera la reserva cuando el trabajo falla (el reintento usa el mismo token)."""
    identity = inspect(record).identity
    record_id = db.info.pop("pending_operation", None)
    if record_id is None and identity is not None:
        record_id = identity[0]
    db.rollback()
    reserved = db.get(IdempotencyKey, record_id)
    if reserved is not None and reserved.status == "pending":
        db.delete(reserved)
        db.commit()
