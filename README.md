# High vs Low — MEXC Forward Paper Research

A standalone, paper-only cross-sectional futures strategy. It ranks eligible contracts by proximity to verified historical price extremes:

- **Long:** smallest percentage distance below the historical all-time high (closest to ATH).
- **Short:** smallest percentage distance above the historical all-time low (closest to ATL).

This repository is an independent strategy generation. It does not import, share, or combine state with the trend-quality paper account.

## Status

Initial engineering foundation. No live orders, exchange credentials, private endpoints, or exchange-side stops. A passing CI suite is engineering evidence, not trading evidence. The paper account must not be activated until the read-only preflight, full-history coverage, state persistence, and execution gates pass.

## Strategy specification

For each eligible instrument at completed UTC daily close:

`ath_distance_pct = 100 * (historical_ath / close - 1)`

`atl_distance_pct = 100 * (close / historical_atl - 1)`

Long candidates are sorted by ascending `ath_distance_pct`; short candidates by ascending `atl_distance_pct`. The selected long and short must be distinct instruments. Missing, invalid, stale, or incomplete all-time history is ineligible. The strategy uses the candle close and extrema available as of that decision timestamp; future candles must never leak into the historical ranking.

The initial default is one long and one short, equal target USDT notional, with independent account state. The implementation exposes explicit configuration for universe eligibility and portfolio limits; it does not silently impose a fixed asset-count cap.

## Engineering roadmap

1. Read-only MEXC public market-data adapter and contract discovery.
2. Resumable earliest-available daily-candle history capture with per-symbol coverage manifests.
3. Point-in-time ATH/ATL feature calculation and deterministic ranking.
4. Public order-book execution simulation, fees, slippage, funding, lot rounding and minimum-size checks.
5. Independent paper ledger, idempotent event journal, recovery and single-writer persistence.
6. Daily decision workflow plus monitoring/risk workflow.
7. Coverage, readiness, and performance reporting with clear evidence separation.

See [docs/STRATEGY.md](docs/STRATEGY.md), [docs/OPERATING_MODEL.md](docs/OPERATING_MODEL.md), and [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

## Local development

Python 3.11+. Install with `pip install -e ".[dev]"`, then run `pytest` and `python -m high_vs_low.cli --help`.

All data ingestion is public/read-only. No command submits an exchange order.
