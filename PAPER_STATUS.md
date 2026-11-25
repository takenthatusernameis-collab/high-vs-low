# ACTIVE PAPER STATUS — HIGH-LOW $100 USDT

**🟢 ACTIVE · PAPER ONLY · NO LIVE ORDERS**

**Correct workflow:** [Open active workflow](https://github.com/takenthatusernameis-collab/high-vs-low/actions/workflows/high-low-paper-cycle.yml)

- Account: `mexc-usdt-forward-high-low-100usdt-v1`
- Strategy generation: `high-low-ath-atl-cross-sectional-v1-100usdt`
- Starting capital: **100.00000000 USDT**
- Account state: **INITIALIZED — FORWARD PAPER**
- Last report build: 2026-11-25 07:42:44 UTC
- Latest workflow run: [45570223334](https://github.com/takenthatusernameis-collab/high-vs-low/actions/runs/45570223334) · run #3, attempt 1 · success · schedule
- Latest processed daily candle: 2026-11-24 00:00:00 UTC
- Latest account event: 2026-11-25 07:42:33 UTC
- Primary execution journal: [PAPER_TRADE_JOURNAL.md](PAPER_TRADE_JOURNAL.md)

## Current performance

| Metric | Value |
|---|---:|
| Initial capital | 100.00000000 USDT |
| **NAV** | **98.56757864 USDT** |
| Total return | -1.432% |
| Cash | 99.96017864 USDT |
| Unrealized P&L | -1.39260000 USDT |
| Fees paid | 0.03982136 USDT |
| Confirmed funding | +0.00000000 USDT |
| Gross exposure | 2.029× NAV |
| Net exposure | 0.022× NAV |
| Current drawdown from risk-window peak | -1.464% |
| Lifetime / cross-session maximum drawdown | -1.464% |
| Worst risk-session maximum drawdown | -1.464% |
| Sharpe ratio | Unavailable — sample gate not met |
| Sortino ratio | Unavailable — sample gate not met |
| Annualized daily volatility | Unavailable — sample gate not met |

### Maximum drawdown by risk session

| Session start (UTC) | Starting NAV | Latest NAV | Max drawdown | Marks |
|---|---:|---:|---:|---:|
| 2026-11-25 00:00:00 UTC | 100.00000000 | 100.00000000 | +0.000% | 1 |
| 2026-11-25 03:29:24 UTC | 100.03187864 | 98.56757864 | -1.464% | 2 |

## Open positions

| Symbol | Side | Quantity | Entry price | Mark | Notional (USDT) |
|---|---|---:|---:|---:|---:|
| DISSTOCK_USDT | long | 0.81 | 122.4 | 122.06 | 98.868600 |
| HYPER_USDT | short | 2660 | 0.03758 | 0.038 | 101.080000 |

## Primary path — equal exposure

The live forward-paper strategy sizes the long and short legs independently toward equal USDT notional (approximately 1× NAV per leg before lot rounding).

![Equal-exposure strategy equity curve](equal_exposure_equity_curve.svg)

## Alternative path — volatility-weighted sizing

This chart is a **visual-only counterfactual**, not an executed account. It reuses the primary strategy’s exact selected symbols, sides and rebalance timestamps; only the notional split changes. No alternative trade journal is created.

At each rebalance, the quieter leg receives the larger share using inverse 30-completed-day realized volatility. The leverage overlay is the ratio of equal-exposure pair volatility to the inverse-volatility pair’s base estimated volatility. It increases modeled gross exposure only when the alternative has lower estimated volatility. Estimates use only data available at that rebalance.

- Latest long / short allocation: **74.1% / 25.9%** of alternative gross notional.
- Latest volatility-matching leverage multiplier: **1.489×**.
- Estimated base volatility reduction before leverage: **+32.86%**.
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
