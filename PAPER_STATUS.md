# ACTIVE PAPER STATUS — HIGH-LOW $100 USDT

**🟢 ACTIVE · PAPER ONLY · NO LIVE ORDERS**

**Correct workflow:** [Open active workflow](https://github.com/takenthatusernameis-collab/high-vs-low/actions/workflows/high-low-paper-cycle.yml)

- Account: `mexc-usdt-forward-high-low-100usdt-v1`
- Strategy generation: `high-low-ath-atl-cross-sectional-v1-100usdt`
- Starting capital: **100.00000000 USDT**
- Account state: **INITIALIZED — FORWARD PAPER**
- Last report build: 2026-11-26 10:00:56 UTC
- Latest workflow run: [45797224206](https://github.com/takenthatusernameis-collab/high-vs-low/actions/runs/45797224206) · run #10, attempt 1 · success · workflow_dispatch
- Latest processed daily candle: 2026-11-25 00:00:00 UTC
- Latest account event: 2026-11-26 10:00:39 UTC
- Primary execution journal: [PAPER_TRADE_JOURNAL.md](PAPER_TRADE_JOURNAL.md)

## Current performance

| Metric | Value |
|---|---:|
| Initial capital | 100.00000000 USDT |
| **NAV** | **96.97698978 USDT** |
| Total return | -3.023% |
| Cash | 97.10018978 USDT |
| Unrealized P&L | -0.12320000 USDT |
| Fees paid | 0.12011022 USDT |
| Confirmed funding | +0.00000000 USDT |
| Gross exposure | 1.995× NAV |
| Net exposure | 0.011× NAV |
| Current drawdown from risk-window peak | -0.058% |
| Lifetime / cross-session maximum drawdown | -5.931% |
| Worst risk-session maximum drawdown | -4.701% |
| Sharpe ratio | Unavailable — At least 30 daily returns are required |
| Sortino ratio | Unavailable — Requires at least 20 daily returns and 5 negative daily returns |
| Annualized daily volatility | Unavailable — At least 30 daily returns are required |

### Maximum drawdown by risk session

| Session start (UTC) | Starting NAV | Latest NAV | Max drawdown | Marks |
|---|---:|---:|---:|---:|
| 2026-11-25 00:00:00 UTC | 100.00000000 | 100.00000000 | +0.000% | 1 |
| 2026-11-25 03:29:24 UTC | 100.03187864 | 98.03257864 | -4.701% | 4 |
| 2026-11-26 00:00:00 UTC | 97.17007864 | 97.17007864 | +0.000% | 1 |
| 2026-11-26 06:18:57 UTC | 97.03328978 | 96.97698978 | -0.274% | 5 |

## Open positions

| Symbol | Side | Quantity | Entry price | Mark | Notional (USDT) |
|---|---|---:|---:|---:|---:|
| DISSTOCK_USDT | long | 0.76 | 126.42 | 126.57 | 96.193200 |
| MITSUBISHISTOCK_USDT | short | 5.93 | 16.36 | 16.4 | 97.252000 |

## Primary path — equal exposure

The live forward-paper strategy sizes the long and short legs independently toward equal USDT notional (approximately 1× NAV per leg before lot rounding).

![Equal-exposure strategy equity curve](equal_exposure_equity_curve.svg)

## Alternative path — volatility-weighted sizing

This chart is a **visual-only counterfactual**, not an executed account. It reuses the primary strategy’s exact selected symbols, sides and rebalance timestamps; only the notional split changes. No alternative trade journal is created.

At each rebalance, the quieter leg receives the larger share using inverse 30-completed-day realized volatility. The leverage overlay is the ratio of equal-exposure pair volatility to the inverse-volatility pair’s base estimated volatility. It increases modeled gross exposure only when the alternative has lower estimated volatility. Estimates use only data available at that rebalance.

- Latest long / short allocation: **64.7% / 35.3%** of alternative gross notional.
- Latest volatility-matching leverage multiplier: **1.151×**.
- Estimated base volatility reduction before leverage: **+13.09%**.
- Volatility observations used: **30**; status: `RISK_MATCHED_INVERSE_VOLATILITY`.

![Inverse-volatility alternative equity curve](alternative_path_equity_curve.svg)

**Model limitations:** the counterfactual scales observed primary entry/exit fees and funding by shadow notional. It does not model additional nonlinear market impact, depth constraints, liquidation limits or exchange leverage caps for larger hypothetical exposure. It is not an executable leverage recommendation.

## Next workflow action

The next daily decision requires a newer completed UTC candle and a passing data-coverage gate.

The daily-candle watermark is not advanced by this report. Only the paper workflow can consume a completed candle after full-history and execution gates pass. A blocked or incomplete run remains visible.

## Historical separation

The v3 $50 USDT generation and its preliminary snapshots are archived and excluded from this account’s NAV, returns, equity curves and metrics. The immutable event ledger remains intact.

## Alternative-path reconstruction notes

- No reconstruction warnings.

This is the compact live status page. The former detailed v3 report is retained under `archive/2026-11-18/` for audit purposes only.
