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
