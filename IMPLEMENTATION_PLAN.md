# Implementation Plan

- [x] Initialize the independent repository and freeze Option A signal semantics.
- [x] Transfer public MEXC API, segmented candle retrieval, compressed data store, execution, accounting and state publisher.
- [x] Replace Q/B selection with point-in-time ATH/ATL distance ranking.
- [x] Add listing-date history provenance and independent 100 USDT account identity.
- [x] Add inverse-30-day-volatility shadow curve using same pairs and rebalance timestamps.
- [x] Add daily/five-minute paper workflow and status builder.
- [x] Add focused selector and inverse-volatility tests plus inherited infrastructure tests.
- [ ] Verify hosted CI and repair failures.
- [ ] Verify hosted MEXC public preflight and full-universe history capture.
- [ ] Verify recovery probe/read-back and fresh-checkout account initialization.
- [ ] Reconcile first two-sided simulated fill, fees, funding and NAV.
- [ ] Complete independent history-coverage and strategy red-team audit.

Code completion does not mark later evidence milestones complete.
