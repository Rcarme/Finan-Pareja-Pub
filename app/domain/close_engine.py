"""Motor puro de cálculo de cierre de mes y reparto 30/20/50 (RN-20 a RN-23, RN-40 a RN-42).

Lógica de dominio puro: sin I/O, sin base de datos, 100% Decimal, sin floats.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any, TypedDict

CENT = Decimal("0.01")


def floor_centavos(val: Decimal) -> Decimal:
    """Trunca un monto Decimal a dos decimales exactos (centavos) usando ROUND_FLOOR."""
    return val.quantize(CENT, rounding=ROUND_FLOOR)


@dataclass(frozen=True)
class CloseSplitConfig:
    """Configuración de porcentajes para el excedente E (RN-22, RN-23)."""

    savings_pct: Decimal = Decimal("30.00")
    buffer_pct: Decimal = Decimal("20.00")
    distribution_pct: Decimal = Decimal("50.00")

    def validate(self) -> None:
        """Valida que los porcentajes sumen exactamente 100% y sean no negativos."""
        if (
            self.savings_pct < Decimal("0.00")
            or self.buffer_pct < Decimal("0.00")
            or self.distribution_pct < Decimal("0.00")
        ):
            raise ValueError("Los porcentajes no pueden ser negativos.")
        total = self.savings_pct + self.buffer_pct + self.distribution_pct
        if total != Decimal("100.00"):
            raise ValueError(f"Los porcentajes deben sumar exactamente 100.00% (actual: {total}%).")


@dataclass(frozen=True)
class CloseCalculationInput:
    """Parámetros de entrada para el cálculo de cierre del período P (RN-20)."""

    period: date
    available: Decimal
    reinforcement_reservation: Decimal
    user_a_id: int
    user_b_id: int
    user_a_charges: Decimal = Decimal("0.00")
    user_b_charges: Decimal = Decimal("0.00")
    user_a_carryover: Decimal = Decimal("0.00")
    user_b_carryover: Decimal = Decimal("0.00")
    buffer_target: Decimal | None = None
    buffer_current_balance: Decimal = Decimal("0.00")
    split_config: CloseSplitConfig = CloseSplitConfig()
    split_mode: str = "equal"
    user_a_income: Decimal = Decimal("0.00")
    user_b_income: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class CloseCalculationResult:
    """Resultado inmutable del cálculo de cierre de mes (RN-20 a RN-24)."""

    period: date
    is_deficit: bool
    available: Decimal
    reinforcement_reservation: Decimal
    base: Decimal
    total_charges_a: Decimal
    total_charges_b: Decimal
    excedent: Decimal
    buffer_allocation: Decimal
    savings_allocation: Decimal
    distributable: Decimal
    share_each: Decimal
    retained: Decimal
    distribution_user_a: Decimal
    distribution_user_b: Decimal
    new_carryover_user_a: Decimal
    new_carryover_user_b: Decimal
    share_user_a: Decimal = Decimal("0.00")
    share_user_b: Decimal = Decimal("0.00")


class CloseSnapshotPayload(TypedDict):
    """Esquema único del snapshot persistido en Close.payload (C-02, RN-24)."""

    period: str
    user_a_id: int
    user_b_id: int
    available: str
    reinforcement_reservation: str
    base: str
    is_deficit: bool
    total_charges_a: str
    total_charges_b: str
    deducted_user_a: str
    deducted_user_b: str
    excedent: str
    buffer_allocation: str
    savings_allocation: str
    distributable: str
    share_each: str
    retained: str
    distribution_user_a: str
    distribution_user_b: str
    new_carryover_user_a: str
    new_carryover_user_b: str
    destination_accounts: dict[int, int]
    savings_destinations: dict[str, str] | None


@dataclass(frozen=True)
class CloseSnapshotMetrics:
    """Métricas extraídas de Close.payload para reportes mensuales y de cargos (C-02)."""

    savings_allocation: Decimal
    buffer_allocation: Decimal
    total_savings: Decimal
    distribution_user_a: Decimal
    distribution_user_b: Decimal
    total_distributed: Decimal
    user_deductions: dict[int, tuple[Decimal, Decimal]]


def build_close_snapshot_payload(
    result: CloseCalculationResult,
    user_a_id: int,
    user_b_id: int,
    destination_accounts: dict[int, int],
    savings_destinations: Mapping[int, Decimal] | None = None,
) -> CloseSnapshotPayload:
    """Construye el payload tipado de Close.payload desde el resultado de dominio (C-02)."""
    deducted_a = (
        Decimal("0.00")
        if result.is_deficit
        else max(Decimal("0.00"), min(result.share_each, result.total_charges_a))
    )
    deducted_b = (
        Decimal("0.00")
        if result.is_deficit
        else max(Decimal("0.00"), min(result.share_each, result.total_charges_b))
    )
    return {
        "period": result.period.isoformat(),
        "user_a_id": user_a_id,
        "user_b_id": user_b_id,
        "available": str(result.available),
        "reinforcement_reservation": str(result.reinforcement_reservation),
        "base": str(result.base),
        "is_deficit": result.is_deficit,
        "total_charges_a": str(result.total_charges_a),
        "total_charges_b": str(result.total_charges_b),
        "deducted_user_a": str(deducted_a),
        "deducted_user_b": str(deducted_b),
        "excedent": str(result.excedent),
        "buffer_allocation": str(result.buffer_allocation),
        "savings_allocation": str(result.savings_allocation),
        "distributable": str(result.distributable),
        "share_each": str(result.share_each),
        "retained": str(result.retained),
        "distribution_user_a": str(result.distribution_user_a),
        "distribution_user_b": str(result.distribution_user_b),
        "new_carryover_user_a": str(result.new_carryover_user_a),
        "new_carryover_user_b": str(result.new_carryover_user_b),
        "destination_accounts": destination_accounts,
        "savings_destinations": (
            {str(k): str(v) for k, v in savings_destinations.items()}
            if savings_destinations
            else None
        ),
    }


def extract_close_snapshot_metrics(payload: Mapping[str, Any]) -> CloseSnapshotMetrics:
    """Extrae las métricas contables desde Close.payload usando el esquema único (C-02)."""
    savings_alloc = Decimal(str(payload.get("savings_allocation", "0.00")))
    buffer_alloc = Decimal(str(payload.get("buffer_allocation", "0.00")))
    dist_a = Decimal(str(payload.get("distribution_user_a", "0.00")))
    dist_b = Decimal(str(payload.get("distribution_user_b", "0.00")))
    share_each = Decimal(str(payload.get("share_each", "0.00")))
    is_deficit = bool(payload.get("is_deficit", False))

    charges_a = Decimal(str(payload.get("total_charges_a", "0.00")))
    charges_b = Decimal(str(payload.get("total_charges_b", "0.00")))
    ded_a_raw = payload.get("deducted_user_a")
    ded_b_raw = payload.get("deducted_user_b")
    ded_a = (
        Decimal(str(ded_a_raw))
        if ded_a_raw is not None
        else (Decimal("0.00") if is_deficit else max(Decimal("0.00"), min(share_each, charges_a)))
    )
    ded_b = (
        Decimal(str(ded_b_raw))
        if ded_b_raw is not None
        else (Decimal("0.00") if is_deficit else max(Decimal("0.00"), min(share_each, charges_b)))
    )
    carry_a = Decimal(str(payload.get("new_carryover_user_a", "0.00")))
    carry_b = Decimal(str(payload.get("new_carryover_user_b", "0.00")))

    user_deductions: dict[int, tuple[Decimal, Decimal]] = {}
    uid_a = payload.get("user_a_id")
    uid_b = payload.get("user_b_id")
    if isinstance(uid_a, int):
        user_deductions[uid_a] = (ded_a, carry_a)
    if isinstance(uid_b, int):
        user_deductions[uid_b] = (ded_b, carry_b)

    return CloseSnapshotMetrics(
        savings_allocation=savings_alloc,
        buffer_allocation=buffer_alloc,
        total_savings=savings_alloc + buffer_alloc,
        distribution_user_a=dist_a,
        distribution_user_b=dist_b,
        total_distributed=dist_a + dist_b,
        user_deductions=user_deductions,
    )


def calculate_close(inp: CloseCalculationInput) -> CloseCalculationResult:
    """Calcula el cierre de mes y el reparto 30/20/50 según RN-20 a RN-23, RN-42.

    1. D = Disponible(P)
    2. R = Reserva de refuerzos (0 <= R <= max(D, 0))
    3. Base = D - R
    4. Cargos(u) = cargos(u) + arrastre(u)
    5. Si Base <= 0: déficit. Sin asignaciones ni reparto. Cargos positivos pasan a nuevo arrastre.
    6. Si Base > 0: E = Base + Cargos(A) + Cargos(B).
       - Si E <= 0 (créditos por reembolso superan el disponible común): no hay excedente común;
         la Base se reintegra a quien tenga saldo acreedor manteniendo Disponible(post) == retenido.
       - Si E > 0:
         * Colchón 20% limitado al tope; excedente se deriva a ahorro.
         * Ahorro 30% + excedente del tope.
         * A repartir = E - colchón - ahorro.
         * parte = floor_centavos(a_repartir / 2).
         * retenido = a_repartir - 2 * parte (0,00 o 0,01).
         * retiro(u) = max(0, parte - Cargos(u)).
         * nuevo_arrastre(u) = max(0, Cargos(u) - parte).
    """
    inp.split_config.validate()

    d = inp.available
    r = inp.reinforcement_reservation
    max_r = max(d, Decimal("0.00"))

    if r < Decimal("0.00") or r > max_r:
        raise ValueError(
            f"Reserva de refuerzos inválida: {r}. "
            f"Debe cumplir 0 <= R <= max(Disponible, 0) ({max_r})."
        )

    base = d - r
    charges_a = inp.user_a_charges + inp.user_a_carryover
    charges_b = inp.user_b_charges + inp.user_b_carryover

    # RN-21: Si Base <= 0, no hay reparto ni asignaciones; solo cargos positivos arrastran (RN-42)
    if base <= Decimal("0.00"):
        return CloseCalculationResult(
            period=inp.period,
            is_deficit=True,
            available=d,
            reinforcement_reservation=r,
            base=base,
            total_charges_a=charges_a,
            total_charges_b=charges_b,
            excedent=Decimal("0.00"),
            buffer_allocation=Decimal("0.00"),
            savings_allocation=Decimal("0.00"),
            distributable=Decimal("0.00"),
            share_each=Decimal("0.00"),
            retained=Decimal("0.00"),
            distribution_user_a=Decimal("0.00"),
            distribution_user_b=Decimal("0.00"),
            new_carryover_user_a=max(Decimal("0.00"), charges_a),
            new_carryover_user_b=max(Decimal("0.00"), charges_b),
        )

    # RN-22: Base > 0
    e = base + charges_a + charges_b

    # RN-42 (H-03): Si E <= 0 con Base > 0 (crédito de reembolsos supera el saldo común)
    if e <= Decimal("0.00"):
        credit_a = max(Decimal("0.00"), -charges_a)
        credit_b = max(Decimal("0.00"), -charges_b)
        total_credit = credit_a + credit_b
        distrib_a = min(credit_a, floor_centavos(base * credit_a / total_credit))
        distrib_b = min(credit_b, base - distrib_a)
        retained = base - distrib_a - distrib_b
        return CloseCalculationResult(
            period=inp.period,
            is_deficit=False,
            available=d,
            reinforcement_reservation=r,
            base=base,
            total_charges_a=charges_a,
            total_charges_b=charges_b,
            excedent=Decimal("0.00"),
            buffer_allocation=Decimal("0.00"),
            savings_allocation=Decimal("0.00"),
            distributable=Decimal("0.00"),
            share_each=Decimal("0.00"),
            retained=retained,
            distribution_user_a=distrib_a,
            distribution_user_b=distrib_b,
            new_carryover_user_a=max(Decimal("0.00"), charges_a),
            new_carryover_user_b=max(Decimal("0.00"), charges_b),
        )

    buffer_ratio = inp.split_config.buffer_pct / Decimal("100.00")
    savings_ratio = inp.split_config.savings_pct / Decimal("100.00")

    raw_buffer = floor_centavos(e * buffer_ratio)

    if inp.buffer_target is not None:
        space = max(Decimal("0.00"), inp.buffer_target - inp.buffer_current_balance)
        buffer_allocation = min(raw_buffer, space)
        buffer_overflow = raw_buffer - buffer_allocation
    else:
        buffer_allocation = raw_buffer
        buffer_overflow = Decimal("0.00")

    raw_savings = floor_centavos(e * savings_ratio)
    savings_allocation = raw_savings + buffer_overflow

    distributable = e - buffer_allocation - savings_allocation
    total_inc = max(Decimal("0.00"), inp.user_a_income) + max(Decimal("0.00"), inp.user_b_income)
    if inp.split_mode == "proportional_income" and total_inc > Decimal("0.00"):
        from decimal import ROUND_HALF_UP

        ratio_a = (max(Decimal("0.00"), inp.user_a_income) / total_inc).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        ratio_b = Decimal("1.0000") - ratio_a
        share_a = floor_centavos(distributable * ratio_a)
        share_b = floor_centavos(distributable * ratio_b)
        share_each = share_a
        retained = distributable - share_a - share_b
    else:
        share_each = floor_centavos(distributable / Decimal("2"))
        share_a = share_each
        share_b = share_each
        retained = distributable - (share_each * Decimal("2"))

    # Retiros y arrastres individuales
    net_a = share_a - charges_a
    if net_a >= Decimal("0.00"):
        distrib_a = net_a
        carryover_a = Decimal("0.00")
    else:
        distrib_a = Decimal("0.00")
        carryover_a = abs(net_a)

    net_b = share_b - charges_b
    if net_b >= Decimal("0.00"):
        distrib_b = net_b
        carryover_b = Decimal("0.00")
    else:
        distrib_b = Decimal("0.00")
        carryover_b = abs(net_b)

    return CloseCalculationResult(
        period=inp.period,
        is_deficit=False,
        available=d,
        reinforcement_reservation=r,
        base=base,
        total_charges_a=charges_a,
        total_charges_b=charges_b,
        excedent=e,
        buffer_allocation=buffer_allocation,
        savings_allocation=savings_allocation,
        distributable=distributable,
        share_each=share_each,
        retained=retained,
        distribution_user_a=distrib_a,
        distribution_user_b=distrib_b,
        new_carryover_user_a=carryover_a,
        new_carryover_user_b=carryover_b,
        share_user_a=share_a,
        share_user_b=share_b,
    )
