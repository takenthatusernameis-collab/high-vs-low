# Find Low vs. High — Market-Neutral Paper Strategy

An independent paper-only MEXC Futures research and forward-simulation system. It transfers the tested engineering architecture of the Trend-Quality project while replacing its signal with proximity to historical price extremes.

## Strategy — Option A

At each completed UTC daily candle, the long side ranks assets by ascending percentage distance below their historical all-time high (closest to ATH first). The short side ranks assets by ascending percentage distance above their historical all-time low (closest to ATL first).

Long distance = 100 × (historical ATH / completed close − 1).
Short distance = 100 × (completed close / historical ATL − 1).

No Trend-Quality, EMA, Q/B or momentum signal is used. “All-time” means earliest available MEXC Futures history from the perpetual contract's provider creation/opening timestamp. It is not spot-market ATH/ATL and does not include price action before the derivative existed. Instruments without listing-date history provenance, sufficient valid history or the current decision-day candle are excluded from active ranking.

## Independent portfolio paths

- Primary paper account: one long and one short, each targeting approximately 1× NAV USDT notional (50% NAV margin at 2× leverage), with independent lot rounding, simulated order-book fills, fees, slippage and funding.
- Inverse-volatility counterfactual: uses the exact same selected pair and rebalance timestamps. It allocates more gross notional to the lower-volatility leg based on 30 completed daily returns and applies the inherited volatility-matching overlay only when pair volatility is lower. This is a shadow path, not a second account or order stream.

Only the equal-notional path writes primary account state and drives risk controls. Both curves are reported separately.

## Operating policy

- Independent 100 USDT virtual account: mexc-usdt-forward-high-low-100usdt-v1.
- Strategy: high-low-ath-atl-cross-sectional-v1-100usdt.
- Research universe: all active MEXC linear perpetuals with per-instrument cross-margin capability and no asset-category restriction.
- Execution universe: USDT-quoted and USDT-settled contracts only.
- Decision: first complete-data cycle after each new UTC daily candle.
- Monitoring: 30-minute marks/risk and a 00:01 UTC daily-close backup.
- Public unauthenticated data only. No credentials, private endpoints, Testnet or live orders.

See IMPLEMENTATION_PROMPT.md, OPERATING_MODEL.md, MATHEMATICAL_REFERENCE.md, ASSUMPTIONS.md, DATA_DICTIONARY.md, RUNBOOK.md and FIRST_RUN_RECOVERY_DIRECTIVE.md for the operating contract. No profitability claim is made.
