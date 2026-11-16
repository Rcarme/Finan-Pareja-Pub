"""Actual currency conversion, independent from informative reference quotes."""

from decimal import Decimal

from app.domain.obligations import calculate_effective_fx_rate


def effective_exchange_rate(usd_amount: object, ars_received: object) -> Decimal:
    for value in (usd_amount, ars_received):
        if (
            not isinstance(value, Decimal)
            or not value.is_finite()
            or value <= 0
            or value > Decimal("999999999999.99")
            or value != value.quantize(Decimal("0.01"))
        ):
            raise ValueError(
                "El importe debe ser Decimal positivo, finito y de hasta dos decimales."
            )
    assert isinstance(usd_amount, Decimal)
    assert isinstance(ars_received, Decimal)
    return calculate_effective_fx_rate(ars_received, usd_amount)
