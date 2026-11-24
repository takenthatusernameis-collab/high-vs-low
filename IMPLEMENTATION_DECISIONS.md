# Implementation Decisions

## 2026-11-24
- Option A: long closest to historical ATH; short closest to historical ATL.
- Both percentage-distance lists sort ascending with deterministic ties.
- Only completed UTC daily candles enter the signal.
- Q/B and all Trend-Quality selection mathematics are removed from this project's ranker.
- Dedicated 100 USDT account, event chain and performance series.
- Primary equal-notional sizing: 1× NAV notional per leg at 2× leverage.
- Parallel inverse-30-completed-day-volatility shadow path reuses selected pairs/timestamps and never issues orders or influences primary risk.
- Retrieve earliest available provider listing-date history; rolling 365-day history is never labeled all-time.
