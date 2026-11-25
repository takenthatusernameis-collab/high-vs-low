# Status — High-Low ATH/ATL Paper Research

As of 2026-11-24
Repository: takenthatusernameis-collab/high-vs-low
Branch: main
Mode: paper-only
Account: mexc-usdt-forward-high-low-100usdt-v1

## Implemented
Independent package, public MEXC adapter, segmented history retrieval, market-data manifest provenance, ATH/ATL ranker, simulated execution, fees/funding/NAV accounting, combined risk controls, append-only state publisher, daily/five-minute cycle, paper status generator and inverse-volatility shadow curve.

## Verified engineering evidence
- Hosted GitHub Actions CI: [run 45384439303](https://github.com/takenthatusernameis-collab/high-vs-low/actions/runs/45384439303), Ruff passed and 51 tests passed on main commit `b99a49f92a1dbbd8405414ac6a75774ebe42ec22`.

## First scheduled-run diagnosis

The first scheduled workflow run (`45490491168`, 2026-11-24 21:22 UTC) completed its **non-financial state persistence probe only**. It correctly reported `paper_account_initialized=false`, `paper_simulation_enabled=false`, and `orders_submitted=0`. The market-data manifest contained zero instruments because the probe path returns before universe discovery. The state branch contains the verified probe event. This run did not execute ATH/ATL ranking or simulated pair selection, so it does not indicate that the strategy found no eligible assets.

The workflow has now been modified to fetch the state branch again into a distinct fresh checkout and continue immediately after a valid probe, plus a path-scoped one-time activation for the already-published probe. Hosted CI for this recovery change is pending. No financial initialization, public-universe bootstrap, paper position or simulated fill is claimed until the follow-on run reports it.

## Evidence outstanding
- Read-only MEXC public preflight and current universe coverage.
- Listing-date history coverage and daily-gap audit.
- State recovery probe and account initialization.
- First two-sided simulated pair and accounting reconciliation.
- Forward sample-size gates before performance statistics.

No High-Low NAV, trade, return or profitability observation is claimed yet. Trend-Quality state and performance are not imported. No live orders, private endpoints, credentials, wallet or Testnet have been used.
