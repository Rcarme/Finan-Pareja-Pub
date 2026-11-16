"""Preferencias de origen por usuario, separadas de la atribución del gasto."""

from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.transactions import atomic_service, commit_service
from app.models.auth import User
from app.models.close import Setting
from app.models.ledger import Account


def _usable(account: Account | None, user_id: int) -> bool:
    return (
        account is not None
        and account.active
        and account.currency == "ARS"
        and not account.is_virtual
        and account.owner_user_id in (None, user_id)
    )


def get_preferred_expense_account(db: Session, user_id: int) -> Account | None:
    preference = db.get(Setting, f"preferred_expense_account:{user_id}")
    if preference is None:
        return None
    account_id = preference.value.get("account_id")
    if not isinstance(account_id, int) or isinstance(account_id, bool):
        return None
    account = db.get(Account, account_id)
    return account if _usable(account, user_id) else None


@atomic_service
def set_preferred_expense_account(
    db: Session, user_id: int, account_id: int | None, actor_user_id: int
) -> None:
    user = db.get(User, user_id)
    if user is None or not user.active:
        raise ValueError("El usuario no está disponible.")
    if account_id is not None and not _usable(db.get(Account, account_id), user_id):
        raise ValueError("Elegí una cuenta propia o sin titular, activa y física en pesos.")
    key = f"preferred_expense_account:{user_id}"
    preference = db.get(Setting, key)
    before = preference.value if preference is not None else None
    value = {"account_id": account_id}
    if preference is None:
        preference = Setting(key=key, value=value, updated_by=actor_user_id)
        db.add(preference)
    else:
        preference.value = value
        preference.updated_by = actor_user_id
    record_audit(
        db,
        entity="Setting",
        entity_id=user_id,
        action="UPDATE" if before is not None else "INSERT",
        before={"key": key, "value": before} if before is not None else None,
        after={"key": key, "value": value},
        user_id=actor_user_id,
    )
    commit_service(db)
