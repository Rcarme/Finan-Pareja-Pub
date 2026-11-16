"""Tests unitarios para validaciones contables de dinero y moneda."""

from decimal import Decimal

import pytest

from app.domain.ownership import common_fund, validate_currency, validate_money


def test_validate_currency():
    assert validate_currency("ars") == "ARS"
    assert validate_currency("USD") == "USD"
    assert validate_currency(" ars ") == "ARS"

    with pytest.raises(ValueError, match="Moneda no soportada"):
        validate_currency("eur")

    with pytest.raises(ValueError, match="Moneda no soportada"):
        validate_currency("mxn")

    with pytest.raises(ValueError, match="Moneda no soportada"):
        validate_currency("US")

    with pytest.raises(ValueError, match="Moneda no soportada"):
        validate_currency("PESOS")


def test_validate_money():
    validate_money(Decimal("100.50"))
    validate_money(Decimal("0.00"))

    # Más de dos decimales
    with pytest.raises(ValueError, match="hasta dos decimales"):
        validate_money(Decimal("10.555"))

    # Número negativo con nonnegative=True
    with pytest.raises(ValueError, match="respetar el límite contable"):
        validate_money(Decimal("-50.00"), nonnegative=True)


def test_common_fund():
    physical = [Decimal("1000.00"), Decimal("500.00")]
    personal = [Decimal("300.00")]
    # Fondo común = 1500 - 300 = 1200
    assert common_fund(physical, personal) == Decimal("1200.00")
