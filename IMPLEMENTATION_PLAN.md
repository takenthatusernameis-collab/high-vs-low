# Implementation Plan

- [x] Initialize the independent repository and freeze Option A signal semantics.
- [x] Transfer public MEXC API, segmented candle retrieval, compressed data store, execution, accounting and state publisher.
- [x] Replace Q/B selection with point-in-time ATH/ATL distance ranking.
- [x] Add listing-date history provenance and independent 100 USDT account identity.
- [x] Add inverse-30-day-volatility shadow curve using same pairs and rebalance timestamps.
- [x] Add daily/five-minute paper workflow and status builder.
- [x] Add focused selector and inverse-volatility tests plus inherited infrastructure tests.
- [x] Verify hosted CI and repair failures (Ruff passed; 51 tests passed in run 45384439303).
- [x] Add immediate same-workflow continuation after a verified fresh-checkout state probe.
- [x] Add a path-scoped one-time activation path for the already-published probe; ordinary pushes are not paper triggers.
- [x] Add recovery-probe validation and workflow handoff regression tests (hosted CI pending).
- [x] Implement immediate same-workflow continuation after a verified state probe and a path-scoped one-time activation trigger (58 hosted tests passed).
- [ ] Verify hosted MEXC public preflight and full-universe history capture.
- [ ] Verify recovery probe/read-back and fresh-checkout account initialization.
- [ ] Reconcile first two-sided simulated fill, fees, funding and NAV.
- [ ] Complete independent history-coverage and strategy red-team audit.

Code completion does not mark later evidence milestones complete.
