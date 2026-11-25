# Refined Execution Directive — First Financial Paper Cycle

## Repository
`takenthatusernameis-collab/high-vs-low`, default branch `main`.

## Evidence-established problem
The first scheduled run (`45490491168`) successfully published and read back one non-financial state-recovery probe. Its report correctly states:
- `mode=STATE_PERSISTENCE_PROBE`
- `paper_account_initialized=false`
- `paper_simulation_enabled=false`
- `orders_submitted=0`

The market-data manifest has zero instruments and zero candles because this mode returns before universe discovery. The state branch contains the exact valid probe. GitHub Actions lists only one scheduled run after deployment, so waiting for a second cron event is not a reliable bootstrap mechanism.

This is not evidence that the ATH/ATL ranking returned no candidates. The strategy selection and execution path did not run.

## Final refined prompt

Repair the first-run recovery handoff so a successful non-financial probe automatically proceeds to the first financial paper cycle without relying on a later scheduled trigger.

1. Preserve the first-stage probe as a non-financial operation. It must publish its event and verify the exact remote read-back before any financial account is initialized.
2. Detect the explicit `STATE_PERSISTENCE_PROBE` report mode as a workflow output.
3. After the probe has been published, perform a separate, new checkout of the `state` branch into a distinct directory. Do not reuse the stale checkout from before the probe was written.
4. Run the existing paper cycle again against that fresh checkout. It must independently validate the hash chain and exact probe fields before initializing the dedicated 100 USDT account.
5. Continue the normal public-data bootstrap, listing-history capture, Option A ranking, two-sided simulated fill checks, account event publishing, market-data commit and status generation.
6. Add a path-scoped one-time activation marker to start the already-probed account now. Do not trigger the paper workflow on arbitrary code pushes.
7. Preserve the existing 30-minute recurring and daily-close schedules, one-writer concurrency lock, explicit manual confirmation, frozen strategy, separate account and shadow inverse-volatility path.
8. If data, eligibility, sizing, liquidity, book freshness or simulated fills block a pair, report the precise gate and retain zero positions. Never fabricate positions or relax controls to force an entry.

## Acceptance criteria

- A valid probe is recognized only after a fresh state checkout and full event-chain verification.
- Invalid protocol fields, financial-state flags or nonzero probe order count are rejected.
- A probe-only cycle is followed immediately by a second cycle using a distinct checkout.
- The one-time activation is path-scoped and requires its exact commit-message token; ordinary code pushes do not launch paper execution.
- The second cycle reports account initialization separately from successful pair entry.
- Market-data history coverage, per-side candidate rankings, exclusions, execution failures and any pair fill are visible in the durable report.
- Any pair entry requires two complete simulated fills; no one-leg account state is committed.
- No live orders, private endpoints, credentials, Testnet, wallet access or strategy changes are introduced.
- Ruff and the full hosted test suite pass before the one-time activation marker is added.
