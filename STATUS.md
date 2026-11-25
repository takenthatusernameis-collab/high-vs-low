# Status — High-Low ATH/ATL Paper Research

As of 2026-11-24
Repository: takenthatusernameis-collab/high-vs-low
Branch: main
Mode: paper-only
Account: mexc-usdt-forward-high-low-100usdt-v1

## Implemented
Independent package, public MEXC adapter, segmented history retrieval, market-data manifest provenance, ATH/ATL ranker, simulated execution, fees/funding/NAV accounting, combined risk controls, append-only state publisher, daily/30-minute cycle, paper status generator and inverse-volatility shadow curve.

## Verified engineering evidence
- Hosted GitHub Actions CI: [run 45536468339](https://github.com/takenthatusernameis-collab/high-vs-low/actions/runs/45536468339), Ruff passed and 58 tests passed on main commit `de30332937b7685c515e5366a5ebff3ea70e6d95`.

## First scheduled-run diagnosis

The first scheduled workflow run (`45490491168`, 2026-11-24 21:22 UTC) completed its **non-financial state persistence probe only**. It correctly reported `paper_account_initialized=false`, `paper_simulation_enabled=false`, and `orders_submitted=0`. The market-data manifest contained zero instruments because the probe path returns before universe discovery. The state branch contains the verified probe event. This run did not execute ATH/ATL ranking or simulated pair selection, so it does not indicate that the strategy found no eligible assets.

The workflow now fetches the state branch again into a distinct fresh checkout and continues immediately after a valid probe. The path-scoped one-time marker was committed as `693ddb32c0d3adf85694085a9abe7176ccdf2024` and triggered [activation run 45536591520](https://github.com/takenthatusernameis-collab/high-vs-low/actions/runs/45536591520), currently executing the full first data/research cycle. The recovery change passed hosted CI (58 tests). Account initialization and any simulated positions remain unverified until that run publishes its report.

## Evidence outstanding
- Read-only MEXC public preflight and current universe coverage.
- Listing-date history coverage and daily-gap audit.
- State recovery probe and account initialization.
- First two-sided simulated pair and accounting reconciliation.
- Forward sample-size gates before performance statistics.

No High-Low NAV, trade, return or profitability observation is claimed yet. Trend-Quality state and performance are not imported. No live orders, private endpoints, credentials, wallet or Testnet have been used.
