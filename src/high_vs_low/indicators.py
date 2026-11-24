"""Pure, deterministic indicator calculations.

No network, filesystem, or trading side effects are allowed in this module.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite, sqrt
from typing import Literal

NoiseMethod = Literal["absolute", "rms"]


@dataclass(frozen=True)
class QObservation:
    index: int
    direction: str | None
    cycle_start_index: int | None
    cycle_age: int | None
    fast_ema: float | None
    slow_ema: float | None
    cpc: float | None
    trend: float | None
    noise: float | None
    q: float | None
    b: float | None


def _validate_period(period: int, name: str) -> None:
    if not isinstance(period, int) or isinstance(period, bool) or period < 1:
        raise ValueError(f"{name} must be a positive integer")


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """EMA seeded with the SMA of the first complete period."""
    _validate_period(period, "period")
    if any(not isfinite(float(v)) for v in values):
        raise ValueError("EMA input contains a non-finite value")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(float(v) for v in values[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    previous = seed
    for i in range(period, len(values)):
        previous = alpha * float(values[i]) + (1.0 - alpha) * previous
        out[i] = previous
    return out


def _complete_mean(values: Sequence[float | None], end: int, period: int) -> float | None:
    start = end - period + 1
    if start < 0:
        return None
    window = values[start : end + 1]
    if len(window) != period or any(v is None for v in window):
        return None
    return sum(float(v) for v in window) / period


def compute_q_series(
    closes: Sequence[float],
    *,
    fast_period: int = 7,
    slow_period: int = 15,
    trend_period: int = 5,
    noise_period: int = 10,
    noise_method: NoiseMethod = "absolute",
    correction_factor: float = 1.0,
) -> list[QObservation]:
    """Calculate Q/B from completed daily closes using causal EMA crossovers.

    The pre-first-crossover segment is left-censored and deliberately excluded.
    CPC on the crossover candle includes close[t] - close[t-1].
    """
    _validate_period(fast_period, "fast_period")
    _validate_period(slow_period, "slow_period")
    _validate_period(trend_period, "trend_period")
    _validate_period(noise_period, "noise_period")
    if fast_period >= slow_period:
        raise ValueError("fast_period must be less than slow_period")
    if noise_period <= trend_period:
        raise ValueError("noise_period must be greater than trend_period")
    if noise_method not in ("absolute", "rms"):
        raise ValueError("noise_method must be absolute or rms")
    if not isfinite(correction_factor) or correction_factor <= 0:
        raise ValueError("correction_factor must be finite and positive")
    if any(not isfinite(float(v)) or float(v) <= 0 for v in closes):
        raise ValueError("closes must contain only finite positive prices")

    fast = ema(closes, fast_period)
    slow = ema(closes, slow_period)
    n = len(closes)
    cpc_values: list[float | None] = [None] * n
    trend_values: list[float | None] = [None] * n
    residual_values: list[float | None] = [None] * n
    noise_values: list[float | None] = [None] * n
    directions: list[str | None] = [None] * n
    cycle_starts: list[int | None] = [None] * n

    active_direction: str | None = None
    cycle_start: int | None = None
    prior_cpc: float | None = None

    for i in range(n):
        crossed_up = (
            i > 0
            and fast[i] is not None
            and slow[i] is not None
            and fast[i - 1] is not None
            and slow[i - 1] is not None
            and fast[i] > slow[i]
            and fast[i - 1] <= slow[i - 1]
        )
        crossed_down = (
            i > 0
            and fast[i] is not None
            and slow[i] is not None
            and fast[i - 1] is not None
            and slow[i - 1] is not None
            and fast[i] < slow[i]
            and fast[i - 1] >= slow[i - 1]
        )
        if crossed_up:
            active_direction = "bullish"
            cycle_start = i
            prior_cpc = None
        elif crossed_down:
            active_direction = "bearish"
            cycle_start = i
            prior_cpc = None

        if cycle_start is None or active_direction is None or i == 0:
            continue

        delta = float(closes[i]) - float(closes[i - 1])
        current_cpc = delta if i == cycle_start else (
            None if prior_cpc is None else prior_cpc + delta
        )
        if current_cpc is None:
            continue

        cpc_values[i] = current_cpc
        directions[i] = active_direction
        cycle_starts[i] = cycle_start
        prior_cpc = current_cpc

        if i - cycle_start + 1 < trend_period:
            continue
        trend = _complete_mean(cpc_values, i, trend_period)
        trend_values[i] = trend
        if trend is None:
            continue

        residual_values[i] = abs(current_cpc - trend)
        if i - cycle_start + 1 < trend_period + noise_period - 1:
            continue

        if noise_method == "absolute":
            noise = _complete_mean(residual_values, i, noise_period)
        else:
            square_residuals = [
                None if value is None else value * value
                for value in residual_values
            ]
            mean_square = _complete_mean(square_residuals, i, noise_period)
            noise = None if mean_square is None else sqrt(max(0.0, mean_square))
        noise_values[i] = noise

    out: list[QObservation] = []
    for i in range(n):
        trend = trend_values[i]
        noise = noise_values[i]
        q_value: float | None = None
        b_value: float | None = None
        if trend is not None and noise is not None:
            denominator = abs(trend) + noise
            if denominator > 0:
                b_value = abs(trend) / denominator * 100.0
            # A scale-relative floor avoids treating floating-point dust as signal.
            noise_floor = abs(float(closes[i])) * 1e-12
            if noise > noise_floor:
                candidate_q = (trend / noise) * correction_factor
                if isfinite(candidate_q):
                    q_value = candidate_q
        start = cycle_starts[i]
        out.append(
            QObservation(
                index=i,
                direction=directions[i],
                cycle_start_index=start,
                cycle_age=None if start is None else i - start + 1,
                fast_ema=fast[i],
                slow_ema=slow[i],
                cpc=cpc_values[i],
                trend=trend,
                noise=noise,
                q=q_value,
                b=b_value,
            )
        )
    return out


def wilder_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float | None]:
    """Wilder ATR with first true range equal to high minus low."""
    _validate_period(period, "period")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows and closes must have equal lengths")
    size = len(closes)
    for high, low, close in zip(highs, lows, closes, strict=True):
        if not all(isfinite(float(v)) for v in (high, low, close)):
            raise ValueError("OHLC input contains non-finite values")
        if (
            float(low) <= 0
            or float(high) <= 0
            or float(close) <= 0
            or float(high) < float(low)
            or float(close) < float(low)
            or float(close) > float(high)
        ):
            raise ValueError("invalid OHLC values")
    true_ranges: list[float] = []
    for i, (high, low) in enumerate(zip(highs, lows, strict=True)):
        if i == 0:
            true_ranges.append(float(high) - float(low))
        else:
            previous_close = float(closes[i - 1])
            true_ranges.append(
                max(
                    float(high) - float(low),
                    abs(float(high) - previous_close),
                    abs(float(low) - previous_close),
                )
            )
    out: list[float | None] = [None] * size
    if size < period:
        return out
    previous = sum(true_ranges[:period]) / period
    out[period - 1] = previous
    for i in range(period, size):
        previous = ((period - 1) * previous + true_ranges[i]) / period
        out[i] = previous
    return out
