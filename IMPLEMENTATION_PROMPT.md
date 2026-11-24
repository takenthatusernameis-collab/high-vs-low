# Master Implementation Directive — High-Low ATH/ATL v1

Repository: takenthatusernameis-collab/high-vs-low
Target branch: main
Account: mexc-usdt-forward-high-low-100usdt-v1
Strategy: high-low-ath-atl-cross-sectional-v1-100usdt

## Frozen signal
At decision time t, use only completed UTC daily candles with timestamp <= t.
ATH_i(t) is the maximum daily high through t. ATL_i(t) is the minimum daily low through t.
d_ATH_i(t) = 100 × (ATH_i(t) / close_i,t − 1).
d_ATL_i(t) = 100 × (close_i,t / ATL_i(t) − 1).
Longs sort by ascending d_ATH (closest to ATH first). Shorts sort by ascending d_ATL (closest to ATL first). Use deterministic ties and distinct instruments. There is no Q, EMA, trend-quality, momentum or signal blending. Do not invert these directions.

## Universe and history
Discover every active MEXC linear perpetual with per-instrument cross-margin capability across all asset categories. Keep all settlements in research; only USDT/USDT instruments enter the USDT paper ledger. Do not impose an arbitrary category or fixed asset-count cap.

Capture daily candles from provider creation/opening timestamp. Segment history requests to no more than 2,000 bars. Persist per-instrument cursors, UTC-date partitions and earliest/latest observed timestamps. Report observed daily gaps and listing-date provenance. A rolling fallback must never be labeled all-time. Contracts without confirmed origin, sufficient valid history or the latest completed decision candle are excluded from active ranking.

## Primary portfolio
Independent 100 USDT virtual seed; one long and one short. Each leg targets approximately 1× NAV notional via 50% NAV margin at 2× leverage. Respect contract size, lot increments, min/max volume and max leverage. Round down; never round up to venue minimum. Require complete two-sided simulated fills before committing a new pair. Model visible book depth, taker fees, slippage, funding at actual settlement times and every close/open cost. Daily decision once per completed candle. Five-minute monitoring is not a new signal decision.

## Inverse-volatility shadow
Reconstruct the exact primary selected pair, sides and rebalance timestamps. Estimate trailing 30 common completed-day returns available at each rebalance. Allocate more gross notional to the lower-volatility leg. Apply the inherited volatility-matching overlay only when estimated alternative pair volatility is lower than equal exposure. This path is shadow-only: no separate orders, account, state, event stream or risk controls; it must not affect primary NAV.

## Safety and durability
Public unauthenticated market data only. No live orders, Testnet, credentials, private endpoints or wallet methods. Source code on main, market data on market-data, paper events on state. Use one non-cancelling single-writer lock. Verify the whole parent-hash chain; reject forks, duplicates and conflicting content; append immutable event files; advance refs fast-forward only; verify exact remote read-back. First invocation publishes a non-financial recovery probe; a later fresh checkout verifies it before account initialization. Never import Trend-Quality trades, balances, events or performance.

## Evidence
Separate API preflight, history coverage, engineering tests, recovery, account initialization, simulated fills and forward performance. Missing metrics remain unavailable. Never claim profitability from tests or partial data.
