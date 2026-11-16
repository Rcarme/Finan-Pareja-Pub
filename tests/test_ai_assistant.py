"""Tests unitarios para la lógica del Asistente IA."""

from decimal import Decimal

from app.domain.ai_assistant import (
    AIDraftItem,
    ExistingTxRecord,
    build_system_instruction,
    check_item_duplicates_pure,
    parse_llm_json_response,
)


def test_build_system_instruction_dynamic():
    instruction = build_system_instruction(
        categories=["Supermercado", "Servicios"],
        accounts=["Banco", "Efectivo"],
        users=["Alex", "Sam"],
    )
    assert "Alex, Sam" in instruction
    assert "Supermercado" in instruction
    assert "Banco" in instruction


def test_duplicate_detection_pure():
    existing = [
        ExistingTxRecord(
            id=1,
            date="2026-11-10",
            amount=Decimal("4500.00"),
            description="Supermercado Central",
            account_name="Banco",
        )
    ]

    # Mismo monto y misma fecha: debe advertir duplicado
    draft1 = AIDraftItem(
        description="Compra super",
        amount=Decimal("4500.00"),
        category_name="Supermercado",
        account_name="Banco",
        tx_date="2026-11-10",
    )
    # Distinto monto: no debe advertir
    draft2 = AIDraftItem(
        description="Farmacia",
        amount=Decimal("1200.00"),
        category_name="Salud",
        account_name="Banco",
        tx_date="2026-11-10",
    )

    results = check_item_duplicates_pure([draft1, draft2], existing, "2026-11-10")
    assert results[0][1] is not None
    assert "ya existe un gasto" in results[0][1].lower()
    assert results[1][1] is None

    # Duplicado exacto de concepto y monto
    draft_exact = AIDraftItem(
        description="Supermercado Central",
        amount=Decimal("4500.00"),
        category_name="Supermercado",
        account_name="Banco",
        tx_date="2026-11-10",
    )
    res_exact = check_item_duplicates_pure([draft_exact], existing, "2026-11-10")
    assert "Duplicado exacto" in res_exact[0][1]


def test_parse_llm_json_response():
    raw_markdown = """```json
    [
        {
            "description": "Leche y pan",
            "amount": 1500.50,
            "category": "Supermercado",
            "account": "Efectivo",
            "activity": "household",
            "date": "2026-11-14"
        }
    ]
    ```"""
    parsed = parse_llm_json_response(raw_markdown)
    assert len(parsed) == 1
    assert parsed[0]["description"] == "Leche y pan"
    assert parsed[0]["amount"] == 1500.50

    from app.domain.ai_assistant import sanitize_draft_item

    draft = sanitize_draft_item(
        parsed[0],
        valid_categories=["Servicios", "Farmacia"],
        valid_accounts=["Banco", "Mercado Pago"],
        default_category="Servicios",
        default_account="Banco",
    )
    # Categoría 'Supermercado' no existe en valid_categories -> no auto-asignar silenciosamente
    assert draft.category_name is None
    assert draft.category_warning is not None
    assert "Supermercado" in draft.category_warning
    # Cuenta 'Efectivo' no existe en valid_accounts -> debe alertar
    assert draft.account_warning is not None
    assert "Efectivo" in draft.account_warning


def test_get_effective_gemini_api_key_with_rotated_key():
    from app.core.db import get_engine, get_session_factory
    from app.core.security import encrypt_secret
    from app.models.base import Base
    from app.models.close import Setting
    from app.services.ai_assistant_service import get_effective_gemini_api_key

    Base.metadata.create_all(bind=get_engine())
    session = get_session_factory()()
    try:
        old_encrypted = encrypt_secret(
            "my-gemini-key-val", key_material="old-master-secret-123456789012"
        )
        setting = session.query(Setting).filter(Setting.key == "gemini_api_key").first()
        if not setting:
            setting = Setting(key="gemini_api_key", value={"api_key": old_encrypted})
            session.add(setting)
        else:
            setting.value = {"api_key": old_encrypted}
        session.commit()

        # No debe lanzar InvalidToken ni excepción; debe degradar graciosamente a None o env var
        effective = get_effective_gemini_api_key(session)
        assert effective is None or isinstance(effective, str)
    finally:
        session.close()


def test_get_effective_gemini_api_key_migrates_legacy_secret_on_read():
    """Verifica que un secreto legado (XOR) se migre a Fernet v2 (HKDF) al leerse."""
    import base64
    import hashlib

    from app.core.config import settings
    from app.core.db import get_engine, get_session_factory
    from app.models.base import Base
    from app.models.close import Setting
    from app.services.ai_assistant_service import get_effective_gemini_api_key

    Base.metadata.create_all(bind=get_engine())
    session = get_session_factory()()
    try:
        # Generar manualmente un token XOR legacy
        key_str = settings.APP_SECRET_KEY
        legacy_key = hashlib.sha256(key_str.encode("utf-8")).digest()
        raw_secret = b"my-special-gemini-key-999"
        xor_bytes = bytes(b ^ legacy_key[i % len(legacy_key)] for i, b in enumerate(raw_secret))
        legacy_token = "enc:" + base64.b64encode(xor_bytes).decode("ascii")

        setting = session.query(Setting).filter(Setting.key == "gemini_api_key").first()
        if not setting:
            setting = Setting(key="gemini_api_key", value={"api_key": legacy_token})
            session.add(setting)
        else:
            setting.value = {"api_key": legacy_token}
        session.commit()

        # Al leer, debe devolver la clave descifrada
        effective = get_effective_gemini_api_key(session)
        assert effective == "my-special-gemini-key-999"

        # Y en la base de datos debe haberse migrado automáticamente a enc:v2:
        session.refresh(setting)
        new_val = setting.value["api_key"]
        assert new_val.startswith("enc:v2:")
    finally:
        session.close()
