from __future__ import annotations

import decimal
import unittest
from importlib import import_module

_performance = import_module("high_vs_low.performance")
DAY_MS = _performance.DAY_MS
EquityPoint = _performance.EquityPoint
compute_performance_metrics = _performance.compute_performance_metrics
daily_returns = _performance.daily_returns
drawdown_series = _performance.drawdown_series


BASE_TIME = 1794787200000
CHECKS = unittest.TestCase()


def _equity(values: list[str], start_ms: int = BASE_TIME) -> list[EquityPoint]:
    return [
        EquityPoint(start_ms + index * DAY_MS, decimal.Decimal(value))
        for index, value in enumerate(values)
    ]


def test_daily_returns_and_drawdown_series() -> None:
    points = _equity(["100", "110", "99", "105", "120"])
    returns = daily_returns(points)
    CHECKS.assertAlmostEqual(returns[0], 0.10, places=10)
    CHECKS.assertAlmostEqual(returns[1], -0.10, places=10)
    series = drawdown_series(points)
    expected = [0.0, 0.0, -0.10, 105 / 110 - 1, 0.0]
    for actual, target in zip(
        [item.drawdown for item in series], expected, strict=True
    ):
        CHECKS.assertAlmostEqual(actual, target, places=10)
    assert series[2].high_watermark == decimal.Decimal("110")


def test_drawdown_duration_and_recovery_period() -> None:
    metrics = compute_performance_metrics(
        _equity(["100", "110", "99", "105", "120"])
    )
    CHECKS.assertAlmostEqual(metrics["maximum_drawdown"].value or 0.0, 0.10, places=10)
    CHECKS.assertAlmostEqual(
        metrics["maximum_drawdown_duration_days"].value or 0.0, 3.0, places=10
    )
    CHECKS.assertAlmostEqual(
        metrics["recovery_period_days"].value or 0.0, 2.0, places=10
    )



def test_maximum_drawdown_duration_uses_longest_episode_not_deepest_episode() -> None:
    # The first episode is shallower (5%) but lasts 5 days. The second is
    # deeper (11%) but lasts only 3 days. The metric must report 5 days.
    metrics = compute_performance_metrics(
        _equity(["100", "95", "96", "97", "98", "100", "90", "89", "100"])
    )
    CHECKS.assertAlmostEqual(
        metrics["maximum_drawdown"].value or 0.0, 0.11, places=10
    )
    CHECKS.assertAlmostEqual(
        metrics["maximum_drawdown_duration_days"].value or 0.0, 5.0, places=10
    )
    CHECKS.assertAlmostEqual(
        metrics["recovery_period_days"].value or 0.0, 1.0, places=10
    )



def test_recovered_high_resets_clock_for_next_drawdown_episode() -> None:
    # The second drawdown starts only after the account has fully recovered
    # to the previous peak. Its duration must not be measured from the first
    # episode's original peak date.
    metrics = compute_performance_metrics(
        _equity(["100", "90", "100", "95", "100"])
    )
    CHECKS.assertAlmostEqual(
        metrics["maximum_drawdown_duration_days"].value or 0.0, 2.0, places=10
    )
    CHECKS.assertAlmostEqual(
        metrics["recovery_period_days"].value or 0.0, 1.0, places=10
    )


def test_monotonic_equity_has_no_drawdown_or_recovery_duration() -> None:
    metrics = compute_performance_metrics(_equity(["100", "101", "102"]))
    assert metrics["maximum_drawdown"].value == 0.0
    assert metrics["maximum_drawdown_duration_days"].value == 0.0
    assert metrics["recovery_period_days"].value is None
    assert metrics["recovery_period_days"].reason == "No drawdown occurred"



def test_performance_rejects_nonfinite_nav_and_trade_pnl() -> None:
    with CHECKS.assertRaisesRegex(ValueError, "finite and positive Decimals"):
        daily_returns(
            [
                EquityPoint(BASE_TIME, decimal.Decimal("100")),
                EquityPoint(BASE_TIME + DAY_MS, decimal.Decimal("Infinity")),
            ]
        )
    with CHECKS.assertRaisesRegex(ValueError, "finite and positive Decimals"):
        daily_returns(
            [
                EquityPoint(BASE_TIME, decimal.Decimal("100")),
                EquityPoint(BASE_TIME + DAY_MS, decimal.Decimal("NaN")),
            ]
        )
    with CHECKS.assertRaisesRegex(ValueError, "positive integer UTC milliseconds"):
        daily_returns(
            [
                EquityPoint(True, decimal.Decimal("100")),
                EquityPoint(True + DAY_MS, decimal.Decimal("101")),
            ]
        )
    with CHECKS.assertRaisesRegex(ValueError, "finite Decimals"):
        compute_performance_metrics(
            _equity(["100", "101"]),
            [decimal.Decimal("Infinity")],
        )


def test_formal_cagr_is_withheld_before_365_elapsed_days() -> None:
    metrics = compute_performance_metrics(_equity(["100", "101"]))
    assert metrics["cagr"].value is None
    assert "365" in str(metrics["cagr"].reason)


def test_cagr_is_available_after_one_year() -> None:
    points = _equity(["100"] + ["110"] * 365)
    metrics = compute_performance_metrics(points)
    CHECKS.assertAlmostEqual(metrics["cagr"].value or 0.0, 0.10, places=10)
    assert metrics["calmar"].value is None


def test_sharpe_requires_30_returns_and_nonzero_volatility() -> None:
    points = [EquityPoint(BASE_TIME, decimal.Decimal("100"))]
    nav = decimal.Decimal("100")
    for index in range(30):
        rate = decimal.Decimal("0.001") if index % 2 == 0 else decimal.Decimal("0.002")
        nav *= decimal.Decimal("1") + rate
        points.append(EquityPoint(BASE_TIME + (index + 1) * DAY_MS, nav))
    metrics = compute_performance_metrics(points)
    assert metrics["sharpe"].value is not None
    assert metrics["annualized_daily_volatility"].value is not None


def test_sortino_requires_20_returns_and_five_negative_returns() -> None:
    points = [EquityPoint(BASE_TIME, decimal.Decimal("100"))]
    nav = decimal.Decimal("100")
    rates = [decimal.Decimal("0.002")] * 20 + [decimal.Decimal("-0.001")] * 5
    for index, rate in enumerate(rates):
        nav *= decimal.Decimal("1") + rate
        points.append(EquityPoint(BASE_TIME + (index + 1) * DAY_MS, nav))
    metrics = compute_performance_metrics(points)
    assert metrics["sortino"].value is not None


def test_sortino_withholds_for_insufficient_negative_returns() -> None:
    points = [EquityPoint(BASE_TIME, decimal.Decimal("100"))]
    nav = decimal.Decimal("100")
    for index in range(25):
        rate = decimal.Decimal("-0.001") if index < 4 else decimal.Decimal("0.001")
        nav *= decimal.Decimal("1") + rate
        points.append(EquityPoint(BASE_TIME + (index + 1) * DAY_MS, nav))
    metrics = compute_performance_metrics(points)
    assert metrics["sortino"].value is None


def test_trade_statistics_use_net_closed_trade_pnl() -> None:
    metrics = compute_performance_metrics(
        _equity(["100", "101", "102"]),
        [decimal.Decimal("10"), decimal.Decimal("-5"), decimal.Decimal("0"), decimal.Decimal("-5")],
    )
    assert metrics["closed_trade_count"].value == 4
    CHECKS.assertAlmostEqual(metrics["win_rate"].value or 0.0, 0.25, places=10)
    CHECKS.assertAlmostEqual(
        metrics["average_winner_net_pnl"].value or 0.0, 10.0, places=10
    )
    CHECKS.assertAlmostEqual(
        metrics["average_loser_net_pnl"].value or 0.0, -5.0, places=10
    )
    CHECKS.assertAlmostEqual(metrics["profit_factor"].value or 0.0, 1.0, places=10)
    CHECKS.assertAlmostEqual(
        metrics["expectancy_net_pnl"].value or 0.0, 0.0, places=10
    )


def test_profit_factor_is_not_claimed_without_losing_trades() -> None:
    metrics = compute_performance_metrics(
        _equity(["100", "101", "102"]),
        [decimal.Decimal("1"), decimal.Decimal("2")],
    )
    assert metrics["profit_factor"].value is None
    assert "No losing" in str(metrics["profit_factor"].reason)


def test_daily_metrics_reject_missing_dates_and_nonpositive_nav() -> None:
    with CHECKS.assertRaisesRegex(ValueError, "consecutive UTC daily"):
        daily_returns(
            [
                EquityPoint(BASE_TIME, decimal.Decimal("100")),
                EquityPoint(BASE_TIME + 2 * DAY_MS, decimal.Decimal("101")),
            ]
        )
    with CHECKS.assertRaisesRegex(ValueError, "positive"):
        daily_returns(_equity(["100", "0"]))
