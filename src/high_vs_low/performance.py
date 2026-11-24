"""Auditable daily-equity and closed-trade performance analytics."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite, sqrt
from statistics import mean, stdev

DAY_MS = 86_400_000
TRADING_DAYS_PER_YEAR = 365


@dataclass(frozen=True)
class EquityPoint:
    timestamp_ms: int
    nav: Decimal


@dataclass(frozen=True)
class Metric:
    value: float | None
    reason: str | None
    observations: int


@dataclass(frozen=True)
class DrawdownPoint:
    timestamp_ms: int
    nav: Decimal
    high_watermark: Decimal
    drawdown: float


def _validate_equity(points: list[EquityPoint]) -> None:
    if len(points) < 2:
        raise ValueError("At least two equity observations are required")
    previous_timestamp: int | None = None
    for point in points:
        if (
            not isinstance(point.timestamp_ms, int)
            or isinstance(point.timestamp_ms, bool)
            or point.timestamp_ms <= 0
        ):
            raise ValueError("Equity timestamps must be positive integer UTC milliseconds")
        if (
            not isinstance(point.nav, Decimal)
            or not point.nav.is_finite()
            or point.nav <= 0
        ):
            raise ValueError("Equity NAV values must be finite and positive Decimals")
        if previous_timestamp is not None:
            gap = point.timestamp_ms - previous_timestamp
            if gap != DAY_MS:
                raise ValueError(
                    "Performance inputs must contain consecutive UTC daily NAV points; "
                    f"observed interval was {gap} ms"
                )
        previous_timestamp = point.timestamp_ms


def daily_returns(points: Iterable[EquityPoint]) -> list[float]:
    """Return simple net NAV returns between consecutive UTC daily marks."""
    ordered = list(points)
    _validate_equity(ordered)
    returns: list[float] = []
    for previous, current in zip(ordered, ordered[1:], strict=False):
        value = float(current.nav / previous.nav - Decimal("1"))
        if not isfinite(value):
            raise ValueError("Daily return calculation produced a non-finite value")
        returns.append(value)
    return returns


def drawdown_series(points: Iterable[EquityPoint]) -> list[DrawdownPoint]:
    ordered = list(points)
    _validate_equity(ordered)
    high = ordered[0].nav
    result: list[DrawdownPoint] = []
    for point in ordered:
        if point.nav > high:
            high = point.nav
        drawdown = float(point.nav / high - Decimal("1"))
        result.append(
            DrawdownPoint(
                timestamp_ms=point.timestamp_ms,
                nav=point.nav,
                high_watermark=high,
                drawdown=drawdown,
            )
        )
    return result


def _unavailable(reason: str, observations: int) -> Metric:
    return Metric(value=None, reason=reason, observations=observations)


def compute_performance_metrics(
    equity: Iterable[EquityPoint],
    closed_trade_net_pnls: Iterable[Decimal] = (),
) -> dict[str, Metric]:
    """Compute metrics with explicit sample/period gates and unavailable reasons.

    Inputs are consecutive daily UTC NAV marks. Fees and confirmed funding must
    already be reflected in NAV returns and in closed-trade net P&L.
    """
    points = list(equity)
    _validate_equity(points)
    returns = daily_returns(points)
    drawdowns = drawdown_series(points)
    observation_count = len(returns)
    elapsed_days = (points[-1].timestamp_ms - points[0].timestamp_ms) / DAY_MS
    total_return = float(points[-1].nav / points[0].nav - Decimal("1"))
    max_drawdown = max(0.0, -min(item.drawdown for item in drawdowns))
    if max_drawdown == 0:
        trough_index = 0
        recovery_index = None
        recovery_period_days = None
    else:
        trough_index = min(range(len(drawdowns)), key=lambda i: drawdowns[i].drawdown)
        peak_value = drawdowns[trough_index].high_watermark
        recovery_index = next(
            (
                i
                for i in range(trough_index + 1, len(points))
                if points[i].nav >= peak_value
            ),
            None,
        )
        recovery_period_days = (
            (points[recovery_index].timestamp_ms - points[trough_index].timestamp_ms) / DAY_MS
            if recovery_index is not None
            else None
        )

    # Maximum drawdown duration is the longest complete or still-open
    # high-watermark-to-recovery episode, not necessarily the episode with
    # the deepest percentage loss.
    maximum_drawdown_duration = 0.0
    current_drawdown_duration = 0.0
    high_watermark = points[0].nav
    high_watermark_timestamp = points[0].timestamp_ms
    active_drawdown = False
    for point in points[1:]:
        if not active_drawdown:
            if point.nav > high_watermark:
                high_watermark = point.nav
                high_watermark_timestamp = point.timestamp_ms
            elif point.nav < high_watermark:
                active_drawdown = True
        elif point.nav >= high_watermark:
            duration = (
                point.timestamp_ms - high_watermark_timestamp
            ) / DAY_MS
            maximum_drawdown_duration = max(maximum_drawdown_duration, duration)
            active_drawdown = False
            if point.nav > high_watermark:
                high_watermark = point.nav
            # A full recovery closes the old episode and starts the next
            # drawdown clock at this recovery timestamp, including an exact
            # touch of the prior high watermark.
            high_watermark_timestamp = point.timestamp_ms

    if active_drawdown:
        current_drawdown_duration = (
            points[-1].timestamp_ms - high_watermark_timestamp
        ) / DAY_MS
        maximum_drawdown_duration = max(
            maximum_drawdown_duration, current_drawdown_duration
        )

    metrics: dict[str, Metric] = {
        "total_return": Metric(total_return, None, observation_count),
        "maximum_drawdown": Metric(max_drawdown, None, len(drawdowns)),
        "maximum_drawdown_duration_days": Metric(
            maximum_drawdown_duration, None, len(drawdowns)
        ),
        "current_drawdown_duration_days": Metric(
            current_drawdown_duration, None, len(drawdowns)
        ),
        "recovery_period_days": (
            Metric(recovery_period_days, None, len(drawdowns))
            if recovery_period_days is not None
            else _unavailable(
                "No drawdown occurred"
                if max_drawdown == 0
                else "Maximum drawdown has not recovered by the latest observation",
                len(drawdowns),
            )
        ),
    }

    if elapsed_days >= TRADING_DAYS_PER_YEAR:
        cagr = (
            float(points[-1].nav / points[0].nav)
            ** (TRADING_DAYS_PER_YEAR / elapsed_days)
        ) - 1.0
        metrics["cagr"] = Metric(cagr, None, observation_count)
    else:
        metrics["cagr"] = _unavailable(
            "At least 365 elapsed calendar days are required for formal CAGR",
            observation_count,
        )

    if observation_count >= 30:
        volatility = stdev(returns) * sqrt(TRADING_DAYS_PER_YEAR)
        metrics["annualized_daily_volatility"] = Metric(
            volatility, None, observation_count
        )
        if volatility > 0:
            sharpe = mean(returns) / stdev(returns) * sqrt(TRADING_DAYS_PER_YEAR)
            metrics["sharpe"] = Metric(sharpe, None, observation_count)
        else:
            metrics["sharpe"] = _unavailable(
                "Daily return standard deviation is zero", observation_count
            )
    else:
        reason = "At least 30 daily returns are required"
        metrics["annualized_daily_volatility"] = _unavailable(reason, observation_count)
        metrics["sharpe"] = _unavailable(reason, observation_count)

    negative_returns = [value for value in returns if value < 0]
    if observation_count >= 20 and len(negative_returns) >= 5:
        downside_deviation = sqrt(
            sum(min(value, 0.0) ** 2 for value in returns) / observation_count
        )
        if downside_deviation > 0:
            sortino = mean(returns) / downside_deviation * sqrt(TRADING_DAYS_PER_YEAR)
            metrics["sortino"] = Metric(sortino, None, observation_count)
        else:
            metrics["sortino"] = _unavailable(
                "Downside deviation is zero", observation_count
            )
    else:
        metrics["sortino"] = _unavailable(
            "Requires at least 20 daily returns and 5 negative daily returns",
            observation_count,
        )

    cagr_metric = metrics["cagr"]
    if cagr_metric.value is not None and max_drawdown > 0:
        metrics["calmar"] = Metric(
            cagr_metric.value / max_drawdown, None, observation_count
        )
    else:
        reason = (
            "Maximum drawdown is zero"
            if max_drawdown == 0
            else "CAGR is unavailable for the observation period"
        )
        metrics["calmar"] = _unavailable(reason, observation_count)

    trades = list(closed_trade_net_pnls)
    if any(
        not isinstance(value, Decimal) or not value.is_finite()
        for value in trades
    ):
        raise ValueError("Closed-trade net P&L values must be finite Decimals")
    winners = [value for value in trades if value > 0]
    losers = [value for value in trades if value < 0]
    metrics["closed_trade_count"] = Metric(float(len(trades)), None, len(trades))
    if trades:
        metrics["win_rate"] = Metric(len(winners) / len(trades), None, len(trades))
        metrics["expectancy_net_pnl"] = Metric(
            float(sum(trades, Decimal("0")) / len(trades)), None, len(trades)
        )
    else:
        metrics["win_rate"] = _unavailable("No closed trades", 0)
        metrics["expectancy_net_pnl"] = _unavailable("No closed trades", 0)

    metrics["average_winner_net_pnl"] = (
        Metric(float(sum(winners, Decimal("0")) / len(winners)), None, len(winners))
        if winners
        else _unavailable("No winning closed trades", len(trades))
    )
    metrics["average_loser_net_pnl"] = (
        Metric(float(sum(losers, Decimal("0")) / len(losers)), None, len(losers))
        if losers
        else _unavailable("No losing closed trades", len(trades))
    )
    if losers:
        gross_profit = sum(winners, Decimal("0"))
        gross_loss = -sum(losers, Decimal("0"))
        metrics["profit_factor"] = Metric(
            float(gross_profit / gross_loss), None, len(trades)
        )
    else:
        metrics["profit_factor"] = _unavailable(
            "No losing closed trades; profit factor is undefined or uninformative",
            len(trades),
        )
    non_finite_metrics = [
        name for name, metric in metrics.items()
        if metric.value is not None and not isfinite(metric.value)
    ]
    if non_finite_metrics:
        raise ValueError(
            "Performance calculation produced non-finite metrics: "
            + ", ".join(sorted(non_finite_metrics))
        )
    return metrics
