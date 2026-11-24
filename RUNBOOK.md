# Runbook

## Local development
python -m pip install -e ".[dev]"
ruff check .
pytest
python -m high_vs_low.cli --public-preflight

The preflight checks public contract discovery only. It does not initialize an account or submit orders.

## Workflow
GitHub Actions runs every five minutes and at 00:01 UTC. Manual dispatch requires the literal confirmation I_CONFIRM_HIGH_LOW_100_USDT_PAPER_ONLY. It uses main, market-data and state branches. The first enabled state write publishes only a non-financial recovery probe; a later fresh checkout verifies the probe before financial initialization.

## History
Capture all available completed daily candles from provider opening/creation timestamp to the latest completed UTC candle. Requests are segmented at no more than 2,000 bars. Persist UTC-date partitions and per-instrument cursors. Do not label a rolling fallback as all-time.

## Safety and recovery
No exchange order capability is implemented. If either leg hits the inherited 3× ATR stop, flatten both simulated legs. Five-minute monitoring is sampled. Missing marks do not create NAV. Funding without a settlement-time mark remains unresolved rather than being set to zero or a stale price.
