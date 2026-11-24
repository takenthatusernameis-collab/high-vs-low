"""Pure paper execution models; never submits exchange orders."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
BPS_DENOMINATOR = Decimal("10000")


class ExecutionModelError(ValueError):
    """Raised when a requested paper fill violates execution assumptions."""


class InsufficientLiquidity(ExecutionModelError):
    """Raised when the visible book cannot fill the requested base quantity."""


class ExecutionImpactExceeded(ExecutionModelError):
    """Raised when simulated market impact exceeds the configured threshold."""


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if (
            not self.price.is_finite()
            or not self.quantity.is_finite()
            or self.price <= ZERO
            or self.quantity <= ZERO
        ):
            raise ExecutionModelError("book price and quantity must be finite and positive")


@dataclass(frozen=True)
class SimulatedFill:
    side: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal
    reference_price: Decimal
    notional: Decimal
    fee: Decimal
    shortfall: Decimal
    shortfall_bps: Decimal
    complete: bool
    levels_consumed: int


def _positive_decimal(value: object, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise ExecutionModelError(f"Invalid {field}: {value!r}") from exc
    if not result.is_finite() or result <= ZERO:
        raise ExecutionModelError(f"{field} must be finite and positive")
    return result


def parse_book_levels(rows: object, side: str) -> list[BookLevel]:
    """Parse Bybit [price, size] levels and sort in executable order."""
    if side not in ("bid", "ask"):
        raise ExecutionModelError("book side must be bid or ask")
    if not isinstance(rows, list):
        raise ExecutionModelError("order-book levels must be a list")
    levels: list[BookLevel] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            raise ExecutionModelError("each order-book level must contain price and quantity")
        levels.append(
            BookLevel(
                _positive_decimal(row[0], "book price"),
                _positive_decimal(row[1], "book quantity"),
            )
        )
    if not levels:
        raise ExecutionModelError("order-book side is empty")
    return sorted(levels, key=lambda level: level.price, reverse=(side == "bid"))


def simulate_orderbook_fill(
    side: str,
    quantity: Decimal,
    bids: list[BookLevel],
    asks: list[BookLevel],
    *,
    fee_rate: Decimal = Decimal("0.001"),
    max_shortfall_bps: Decimal = Decimal("10"),
    allow_partial: bool = False,
) -> SimulatedFill:
    """Consume asks for buys or bids for sells and return depth-weighted VWAP.

    Shortfall is measured against the mid-price and is already reflected in the
    average fill price; it is a reporting field, not an additional cash debit.
    """
    if side not in ("buy", "sell"):
        raise ExecutionModelError("side must be buy or sell")
    request = _positive_decimal(quantity, "quantity")
    if not fee_rate.is_finite() or fee_rate < ZERO:
        raise ExecutionModelError("fee_rate must be finite and non-negative")
    if not max_shortfall_bps.is_finite() or max_shortfall_bps < ZERO:
        raise ExecutionModelError("max_shortfall_bps must be finite and non-negative")
    if not bids or not asks:
        raise ExecutionModelError("both bid and ask sides are required")
    ordered_bids = sorted(bids, key=lambda level: level.price, reverse=True)
    ordered_asks = sorted(asks, key=lambda level: level.price)
    best_bid = ordered_bids[0].price
    best_ask = ordered_asks[0].price
    if best_bid >= best_ask:
        raise ExecutionModelError("crossed or locked order book is not executable")
    midpoint = (best_bid + best_ask) / Decimal("2")

    levels = ordered_asks if side == "buy" else ordered_bids
    remaining = request
    filled = ZERO
    notional = ZERO
    consumed = 0
    for level in levels:
        take = min(remaining, level.quantity)
        if take <= ZERO:
            continue
        filled += take
        notional += take * level.price
        remaining -= take
        consumed += 1
        if remaining == ZERO:
            break

    complete = remaining == ZERO
    if not complete and not allow_partial:
        raise InsufficientLiquidity(
            f"Requested {request} base units but visible depth filled only {filled}"
        )
    if filled == ZERO:
        raise InsufficientLiquidity("No executable quantity in visible order book")

    average_price = notional / filled
    if side == "buy":
        shortfall = max(ZERO, (average_price - midpoint) * filled)
        shortfall_bps = max(ZERO, (average_price / midpoint - Decimal("1")) * BPS_DENOMINATOR)
    else:
        shortfall = max(ZERO, (midpoint - average_price) * filled)
        shortfall_bps = max(ZERO, (Decimal("1") - average_price / midpoint) * BPS_DENOMINATOR)
    if shortfall_bps > max_shortfall_bps:
        raise ExecutionImpactExceeded(
            f"Modeled shortfall {shortfall_bps} bps exceeds limit {max_shortfall_bps} bps"
        )
    fee = notional * fee_rate
    return SimulatedFill(
        side=side,
        requested_quantity=request,
        filled_quantity=filled,
        average_price=average_price,
        reference_price=midpoint,
        notional=notional,
        fee=fee,
        shortfall=shortfall,
        shortfall_bps=shortfall_bps,
        complete=complete,
        levels_consumed=consumed,
    )


def simulate_historical_minute_fill(
    side: str,
    quantity: Decimal,
    reference_price: Decimal,
    *,
    fee_rate: Decimal = Decimal("0.001"),
    adverse_slippage_bps: Decimal = Decimal("10"),
) -> SimulatedFill:
    """Model historical next-minute execution without claiming historical depth."""
    if side not in ("buy", "sell"):
        raise ExecutionModelError("side must be buy or sell")
    request = _positive_decimal(quantity, "quantity")
    reference = _positive_decimal(reference_price, "reference_price")
    if not fee_rate.is_finite() or fee_rate < ZERO:
        raise ExecutionModelError("fee_rate must be finite and non-negative")
    if not adverse_slippage_bps.is_finite() or adverse_slippage_bps < ZERO:
        raise ExecutionModelError("adverse_slippage_bps must be finite and non-negative")
    direction = Decimal("1") if side == "buy" else Decimal("-1")
    fill_price = reference * (
        Decimal("1") + direction * adverse_slippage_bps / BPS_DENOMINATOR
    )
    if not fill_price.is_finite() or fill_price <= ZERO:
        raise ExecutionModelError(
            "modeled fill price must be finite and positive after adverse slippage"
        )
    notional = request * fill_price
    shortfall = request * abs(fill_price - reference)
    fee = notional * fee_rate
    return SimulatedFill(
        side=side,
        requested_quantity=request,
        filled_quantity=request,
        average_price=fill_price,
        reference_price=reference,
        notional=notional,
        fee=fee,
        shortfall=shortfall,
        shortfall_bps=adverse_slippage_bps,
        complete=True,
        levels_consumed=0,
    )
