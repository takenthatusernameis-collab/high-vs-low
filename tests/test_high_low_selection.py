from __future__ import annotations

from decimal import Decimal

from high_vs_low.mexc_paper import rank_mexc_universe


DAY = 86_400_000
AS_OF = 35 * DAY


def instrument(iid: int, symbol: str) -> dict:
    return {
        "instrument_id": iid,
        "symbol": symbol,
        "usdt_account_eligible": True,
        "all_time_history_verified": True,
    }


def history(iid: int, close: str, high: str, low: str) -> list[dict]:
    return [
        {
            "instrument_id": iid,
            "timestamp_ms": (index + 1) * DAY,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "turnover_usdt": "100000000",
        }
        for index in range(35)
    ]


def test_option_a_long_closest_to_ath_short_closest_to_atl() -> None:
    instruments = [instrument(1, "NEAR_ATH"), instrument(2, "NEAR_ATL")]
    histories = {
        1: history(1, "99", "100", "80"),
        2: history(2, "11", "20", "10"),
    }
    tickers = {
        1: {"last_price": "99", "mid_price": "99"},
        2: {"last_price": "11", "mid_price": "11"},
    }
    ranks = rank_mexc_universe(instruments, histories, tickers, Decimal("100"), as_of_timestamp_ms=AS_OF)
    assert ranks.research_longs[0].symbol == "mexc:1"
    assert ranks.research_shorts[0].symbol == "mexc:2"
    assert ranks.research_longs[0].distance_pct == 100.0 / 99.0 - 1.0
    assert ranks.research_shorts[0].distance_pct == 10.0
    assert ranks.executable_longs[0].symbol == "mexc:1"
    assert ranks.executable_shorts[0].symbol == "mexc:2"


def test_rank_uses_only_candles_at_or_before_decision_timestamp() -> None:
    item = instrument(4, "CAUSAL")
    rows = history(4, "99", "100", "80")
    rows.append({
        "instrument_id": 4, "timestamp_ms": 36 * DAY,
        "open": "90", "high": "10000", "low": "0.01", "close": "90",
        "turnover_usdt": "100000000",
    })
    result = rank_mexc_universe(
        [item], {4: rows}, {4: {"last_price": "99", "mid_price": "99"}},
        Decimal("100"), as_of_timestamp_ms=AS_OF,
    )
    assert result.research_longs[0].distance_pct == 100.0 * (100.0 / 99.0 - 1.0)
    assert result.research_shorts[0].distance_pct == 100.0 * (99.0 / 80.0 - 1.0)


def test_incomplete_listing_history_is_not_ranked_as_all_time() -> None:
    item = instrument(8, "UNVERIFIED")
    item["all_time_history_verified"] = False
    result = rank_mexc_universe(
        [item], {8: history(8, "99", "100", "80")},
        {8: {"last_price": "99", "mid_price": "99"}},
        Decimal("100"), as_of_timestamp_ms=AS_OF,
    )
    assert not result.research_longs
    assert "ALL_TIME_HISTORY_NOT_VERIFIED" in result.excluded[0]


def test_missing_decision_day_bar_is_excluded() -> None:
    rows = history(9, "99", "100", "80")
    rows.append({
        "instrument_id": 9, "timestamp_ms": 36 * DAY,
        "open": "99", "high": "100", "low": "80", "close": "99",
        "turnover_usdt": "100000000",
    })
    rows.pop()
    result = rank_mexc_universe(
        [instrument(9, "MISSING_BAR")], {9: rows},
        {9: {"last_price": "99", "mid_price": "99"}},
        Decimal("100"), as_of_timestamp_ms=36 * DAY,
    )
    assert not result.research_longs
    assert "MISSING_DECISION_CANDLE" in result.excluded[0]
