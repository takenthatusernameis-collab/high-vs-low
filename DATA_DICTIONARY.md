# Data Dictionary

## Instrument manifest
- instrument_id: stable MEXC contract identifier; symbol is display-only.
- create_time_ms / opening_time_ms: provider listing/opening metadata.
- history_origin_timestamp_ms: first UTC day requested.
- history_first_candle_timestamp_ms / history_last_candle_timestamp_ms: stored provider-history bounds.
- all_time_history_verified: listing-origin capture completed with sufficient bars and reached the decision candle; not proof of zero intraday gaps.
- history_observed_daily_gap_count: missing UTC daily timestamps observed between stored bars.
- history_status: available/insufficient/error state.

## Candle rows
- timestamp_ms: UTC daily candle open timestamp.
- open/high/low/close: daily prices.
- volume_contracts: venue contract count.
- turnover_quote and turnover_quote_asset: quote turnover and currency.
- turnover_usdt: populated only for homogeneous USDT/USDT execution contracts.

## Ranking rows
- distance_pct: percentage below ATH for long or above ATL for short.
- distance_metric: explicit ATH or ATL distance definition.
- extreme_price: observed ATH daily high or ATL daily low.
- completed_close: close used for the rank.
- decision_bar_timestamp_ms: completed candle watermark.
