# High-Low ATH/ATL — Operating Model

## Account
Independent 100 USDT forward-paper ledger: mexc-usdt-forward-high-low-100usdt-v1. It shares no state, events, balance or performance with Trend-Quality. Public MEXC Futures data only; no live orders, Testnet, credentials, private endpoints or wallet.

## Scheduler
The GitHub Actions workflow runs every five minutes and at 00:01 UTC. The first invocation seeing a new completed UTC daily candle performs data refresh, ranking and one portfolio decision. The candle watermark makes it idempotent. Other invocations mark open positions, process confirmed funding, check combined risk and retry only a previously frozen pending daily shortlist. They do not recalculate rankings or resize/rebalance ordinary holdings.

## Universe
Research includes every active MEXC linear perpetual with per-instrument cross-margin capability across categories and settlements. Preserve unknown categories. Executable positions are limited to quoteCoin=USDT and settleCoin=USDT. Never mix alternate collateral into USDT NAV.

## All-time history
For each active contract, choose provider openingTime where available, otherwise createTime; floor to UTC day and fetch all completed Day1 candles through the latest completed candle. Split long ranges into requests of at most 2,000 bars. Persist origin, first/last candle, history status, daily gaps and watermarks. “All-time” means earliest available MEXC Futures perpetual history from that instrument's listing/opening date, not spot history or pre-listing prices.

A rolling fallback may be captured for diagnostics but cannot qualify as all-time. Missing origin metadata, backfill failure, invalid OHLC, insufficient history or no decision-day candle keeps an instrument out of active ranking. Report gaps separately; API access is not proof of complete tick-level coverage.

## Ranking
For each completed daily close, ATH is the maximum daily high and ATL the minimum daily low through that candle. Longs sort ascending by percent below ATH; shorts sort ascending by percent above ATL. Research ranks retain non-USDT contracts. Execution lists separately enforce USDT/USDT settlement, 30-day median turnover cap, fresh two-sided order book, impact and venue lot limits.

## Sizing, costs and risk
Each primary leg targets 1× NAV notional via 50% NAV margin at 2× leverage. Quantities are rounded down independently. Model depth-weighted fills, taker fees, slippage, confirmed settlement funding and all close/open costs.

Retain the engineering reference's 3× ATR trailing stop and 25% combined portfolio daily drawdown latch. A stop on either leg closes both. Five-minute monitoring is sampled, not continuous. Missing marks do not create fabricated NAV. Emergency flatten remains latched until flat.

## Inverse-volatility alternative
The shadow path reuses primary symbols, sides and rebalance timestamps. At each rebalance it estimates the prior 30 common completed daily returns, weights the quieter leg more heavily and applies the volatility-matching overlay only if estimated pair volatility falls. Scale observed primary fees/funding by shadow notional. No separate decisions, fills, event journal or risk controls. Clearly disclose nonlinear impact, depth and leverage-cap limitations.

## State
Source code on main, public data on market-data, paper events on state. All event writers use one non-cancelling concurrency group. Verify full hash chain, reject forks/duplicates/conflicts, append immutable files, fast-forward only and verify exact remote read-back. Public state is permanent; never store secrets.

## Evidence
Report public API checks, history coverage, tests, state recovery, account initialization, simulated fills and forward observations independently. Performance statistics remain unavailable until sample gates pass.
