from __future__ import annotations

from decimal import Decimal

import pytest

from high_vs_low.execution import (
    BookLevel,
    ExecutionImpactExceeded,
    ExecutionModelError,
    InsufficientLiquidity,
    parse_book_levels,
    simulate_historical_minute_fill,
    simulate_orderbook_fill,
)


def _book() -> tuple[list[BookLevel], list[BookLevel]]:
    bids = [
        BookLevel(Decimal("99"), Decimal("2")),
        BookLevel(Decimal("98"), Decimal("3")),
    ]
    asks = [
        BookLevel(Decimal("101"), Decimal("1")),
        BookLevel(Decimal("102"), Decimal("3")),
    ]
    return bids, asks


def test_parse_book_levels_sorts_executable_order() -> None:
    asks = parse_book_levels([["102", "1"], ["101", "2"]], "ask")
    bids = parse_book_levels([["98", "1"], ["99", "2"]], "bid")
    assert [level.price for level in asks] == [Decimal("101"), Decimal("102")]
    assert [level.price for level in bids] == [Decimal("99"), Decimal("98")]


def test_buy_consumes_asks_and_calculates_vwap_fee_and_shortfall() -> None:
    bids, asks = _book()
    fill = simulate_orderbook_fill(
        "buy",
        Decimal("2"),
        bids,
        asks,
        fee_rate=Decimal("0.001"),
        max_shortfall_bps=Decimal("200"),
    )
    assert fill.complete
    assert fill.filled_quantity == Decimal("2")
    assert fill.notional == Decimal("203")
    assert fill.average_price == Decimal("101.5")
    assert fill.reference_price == Decimal("100")
    assert fill.fee == Decimal("0.203")
    assert fill.shortfall == Decimal("3")
    assert fill.shortfall_bps == Decimal("150")


def test_sell_consumes_bids_and_calculates_vwap() -> None:
    bids, asks = _book()
    fill = simulate_orderbook_fill(
        "sell",
        Decimal("2"),
        bids,
        asks,
        max_shortfall_bps=Decimal("200"),
    )
    assert fill.complete
    assert fill.notional == Decimal("198")
    assert fill.average_price == Decimal("99")
    assert fill.shortfall == Decimal("2")
    assert fill.shortfall_bps == Decimal("100")


def test_insufficient_book_depth_rejects_by_default() -> None:
    bids, asks = _book()
    with pytest.raises(InsufficientLiquidity):
        simulate_orderbook_fill(
            "buy", Decimal("10"), bids, asks, max_shortfall_bps=Decimal("1000")
        )


def test_partial_fill_is_explicit() -> None:
    bids, asks = _book()
    fill = simulate_orderbook_fill(
        "buy",
        Decimal("10"),
        bids,
        asks,
        max_shortfall_bps=Decimal("1000"),
        allow_partial=True,
    )
    assert not fill.complete
    assert fill.requested_quantity == Decimal("10")
    assert fill.filled_quantity == Decimal("4")


def test_maximum_shortfall_rejects_fill() -> None:
    bids, asks = _book()
    with pytest.raises(ExecutionImpactExceeded):
        simulate_orderbook_fill(
            "buy", Decimal("2"), bids, asks, max_shortfall_bps=Decimal("50")
        )


def test_historical_minute_fill_applies_adverse_slippage_once() -> None:
    buy = simulate_historical_minute_fill(
        "buy",
        Decimal("2"),
        Decimal("100"),
        fee_rate=Decimal("0.001"),
        adverse_slippage_bps=Decimal("10"),
    )
    sell = simulate_historical_minute_fill(
        "sell",
        Decimal("2"),
        Decimal("100"),
        fee_rate=Decimal("0.001"),
        adverse_slippage_bps=Decimal("10"),
    )
    assert buy.average_price == Decimal("100.10")
    assert sell.average_price == Decimal("99.90")
    assert buy.shortfall == Decimal("0.20")
    assert sell.shortfall == Decimal("0.20")
    assert buy.fee == Decimal("0.20020")
    assert sell.fee == Decimal("0.19980")



def test_historical_sell_fill_rejects_zero_or_negative_price_after_slippage() -> None:
    with pytest.raises(ExecutionModelError, match="fill price must be finite and positive"):
        simulate_historical_minute_fill(
            "sell",
            Decimal("1"),
            Decimal("100"),
            adverse_slippage_bps=Decimal("10000"),
        )
    with pytest.raises(ExecutionModelError, match="fill price must be finite and positive"):
        simulate_historical_minute_fill(
            "sell",
            Decimal("1"),
            Decimal("100"),
            adverse_slippage_bps=Decimal("10001"),
        )


def test_book_level_rejects_nonpositive_or_nonfinite_values() -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        BookLevel(Decimal("0"), Decimal("1"))
    with pytest.raises(ValueError, match="finite and positive"):
        BookLevel(Decimal("1"), Decimal("-1"))


def test_crossed_book_and_invalid_inputs_are_rejected() -> None:
    with pytest.raises(ExecutionModelError, match="crossed"):
        simulate_orderbook_fill(
            "buy",
            Decimal("1"),
            [BookLevel(Decimal("101"), Decimal("1"))],
            [BookLevel(Decimal("100"), Decimal("1"))],
        )
    with pytest.raises(ExecutionModelError):
        parse_book_levels([], "ask")
    with pytest.raises(ExecutionModelError):
        simulate_historical_minute_fill(
            "buy", Decimal("-1"), Decimal("100")
        )
