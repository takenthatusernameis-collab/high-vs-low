"""Pure Decimal-based portfolio sizing and linear-perpetual accounting."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

ZERO = Decimal("0")
ONE = Decimal("1")


def _require_finite_decimal(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")


@dataclass(frozen=True)
class Position:
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("symbol must be a non-empty string")
        if self.side not in ("long", "short"):
            raise ValueError("side must be long or short")
        _require_finite_decimal(self.quantity, "quantity")
        _require_finite_decimal(self.entry_price, "entry_price")
        if self.quantity <= ZERO or self.entry_price <= ZERO:
            raise ValueError("quantity and entry_price must be positive")

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        _require_finite_decimal(mark_price, "mark_price")
        if mark_price <= ZERO:
            raise ValueError("mark_price must be positive")
        sign = ONE if self.side == "long" else Decimal("-1")
        result = sign * self.quantity * (mark_price - self.entry_price)
        _require_finite_decimal(result, "unrealized P&L")
        return result


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    _require_finite_decimal(value, "value")
    _require_finite_decimal(step, "step")
    if value < ZERO or step <= ZERO:
        raise ValueError("value must be non-negative and step positive")
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    result = units * step
    _require_finite_decimal(result, "rounded quantity")
    return result


def target_quantity(
    nav: Decimal,
    allocation_fraction: Decimal,
    price: Decimal,
    qty_step: Decimal,
    min_qty: Decimal,
    min_notional: Decimal,
    max_market_qty: Decimal | None = None,
) -> Decimal:
    """Size a leg and round down; never increase target exposure to meet minima."""
    for value, field in (
        (nav, "nav"),
        (allocation_fraction, "allocation_fraction"),
        (price, "price"),
        (qty_step, "qty_step"),
        (min_qty, "min_qty"),
        (min_notional, "min_notional"),
    ):
        _require_finite_decimal(value, field)
    if max_market_qty is not None:
        _require_finite_decimal(max_market_qty, "max_market_qty")
        if max_market_qty <= ZERO:
            raise ValueError("max_market_qty must be positive")
    if nav <= ZERO or price <= ZERO:
        raise ValueError("nav and price must be positive")
    if allocation_fraction <= ZERO or allocation_fraction > ONE:
        raise ValueError("allocation_fraction must be in (0, 1]")
    if min_qty < ZERO or min_notional < ZERO:
        raise ValueError("minimum constraints must be non-negative")
    raw_quantity = nav * allocation_fraction / price
    _require_finite_decimal(raw_quantity, "raw quantity")
    quantity = floor_to_step(raw_quantity, qty_step)
    if max_market_qty is not None and quantity > max_market_qty:
        quantity = floor_to_step(max_market_qty, qty_step)
    if quantity < min_qty or quantity * price < min_notional:
        return ZERO
    return quantity


def funding_cashflow(
    side: str,
    quantity: Decimal,
    mark_price: Decimal,
    funding_rate: Decimal,
) -> Decimal:
    if side not in ("long", "short"):
        raise ValueError("side must be long or short")
    for value, field in (
        (quantity, "quantity"),
        (mark_price, "mark_price"),
        (funding_rate, "funding_rate"),
    ):
        _require_finite_decimal(value, field)
    if quantity <= ZERO or mark_price <= ZERO:
        raise ValueError("quantity and mark_price must be positive")
    sign = ONE if side == "long" else Decimal("-1")
    result = -sign * quantity * mark_price * funding_rate
    _require_finite_decimal(result, "funding cashflow")
    return result


def portfolio_exposure(
    nav: Decimal,
    long_notional: Decimal,
    short_notional: Decimal,
) -> dict[str, Decimal]:
    for value, field in (
        (nav, "nav"),
        (long_notional, "long_notional"),
        (short_notional, "short_notional"),
    ):
        _require_finite_decimal(value, field)
    if nav <= ZERO:
        raise ValueError("nav must be positive")
    if long_notional < ZERO or short_notional < ZERO:
        raise ValueError("notionals must be non-negative magnitudes")
    gross = (long_notional + short_notional) / nav
    net = (long_notional - short_notional) / nav
    imbalance = abs(long_notional - short_notional) / nav
    for value, field in (
        (gross, "gross exposure"),
        (net, "net exposure"),
        (imbalance, "neutrality imbalance"),
    ):
        _require_finite_decimal(value, field)
    return {
        "gross_exposure": gross,
        "net_exposure": net,
        "dollar_neutrality_imbalance": imbalance,
    }


def calculate_nav(
    initial_capital: Decimal,
    realized_gross_pnl: Decimal,
    unrealized_pnl: Decimal,
    fees: Decimal,
    confirmed_funding: Decimal,
    adjustments: Decimal = ZERO,
) -> Decimal:
    """NAV = initial + realized gross + unrealized - fees + funding + adjustments."""
    for value, field in (
        (initial_capital, "initial_capital"),
        (realized_gross_pnl, "realized_gross_pnl"),
        (unrealized_pnl, "unrealized_pnl"),
        (fees, "fees"),
        (confirmed_funding, "confirmed_funding"),
        (adjustments, "adjustments"),
    ):
        _require_finite_decimal(value, field)
    if initial_capital <= ZERO:
        raise ValueError("initial_capital must be positive")
    if fees < ZERO:
        raise ValueError("fees must be non-negative")
    result = (
        initial_capital
        + realized_gross_pnl
        + unrealized_pnl
        - fees
        + confirmed_funding
        + adjustments
    )
    _require_finite_decimal(result, "NAV")
    return result
