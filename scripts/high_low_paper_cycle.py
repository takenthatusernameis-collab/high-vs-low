#!/usr/bin/env python3
"""Daily-close and five-minute MEXC market-data and gated USDT forward-paper cycle.

Public market data can be collected and ranked without state-write authorization.
Portfolio transitions require the explicit PAPER_STATE_WRITE_ENABLED flag.
State writes are serialized, hash-chain checked and verified by exact remote
read-back. There are no live orders.
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from high_vs_low.event_journal import (
    build_event_batch,
    order_event_batches,
    verify_event_batch,
)
from high_vs_low.github_state import GitHubStatePublisher
from high_vs_low.indicators import wilder_atr
from high_vs_low.market_data_store import MarketDataStore
from high_vs_low.mexc_api import MEXCAPIError, MEXCForbiddenError, MEXCPublicClient
from high_vs_low.mexc_paper import (
    Candidate,
    MEXCRankings,
    rank_mexc_universe,
    simulate_mexc_fill,
    size_mexc_leg,
)
from high_vs_low.mexc_portfolio import (
    account_report,
    append_daily_equity,
    append_equity_snapshot,
    apply_confirmed_funding,
    close_paper_position,
    initial_portfolio_state,
    open_paper_position,
    portfolio_nav,
    reset_daily_risk_window,
    stop_is_triggered,
    update_trailing_stop,
)


DAY_MS = 86_400_000
BOOTSTRAP_DAYS = 3650  # fallback only; unproven history never qualifies as all-time
DEFAULT_BOOTSTRAP_BATCH_SIZE = 0
DAILY_REFRESH_BATCH_VERSION = 1
DAILY_REFRESH_CHECKPOINT_EVERY = 25
MAX_EXCHANGE_CLOCK_SKEW_MS = 5_000
MAX_SHORTFALL_BPS = Decimal("10")
STOP_ATR_MULTIPLE = Decimal("3")
INITIAL_CAPITAL = Decimal("100")
ACCOUNT_ID = "mexc-usdt-forward-high-low-100usdt-v1"
STRATEGY_ID = "high-low-ath-atl-cross-sectional-v1-100usdt"


class MEXCExecutionUnavailable(RuntimeError):
    """Public order-book data could not support a safe paper execution decision."""


def dec(value: object, default: str = "0") -> Decimal:
    try:
        parsed = Decimal(str(default if value is None else value))
    except Exception:
        return Decimal(default)
    return parsed if parsed.is_finite() else Decimal(default)


def _last_completed_daily_bar(server_ms: int) -> int:
    return (server_ms // DAY_MS) * DAY_MS - DAY_MS


def _daily_decision_is_due(
    history_coverage_pass: bool,
    latest_bar_ms: int,
    last_attempt_ms: int | None,
) -> bool:
    """Only make a candle decision eligible after its required history is complete."""
    return bool(
        history_coverage_pass
        and (last_attempt_ms is None or latest_bar_ms > int(last_attempt_ms))
    )



def _daily_refresh_due(manifest: dict[str, Any], latest_bar_ms: int) -> bool:
    """Continue refreshing the same daily bar until the full data-coverage gate passes."""
    if manifest.get("daily_refresh_batch_version") != DAILY_REFRESH_BATCH_VERSION:
        return True
    last_covered_bar = manifest.get("last_research_daily_bar_ms")
    return last_covered_bar is None or latest_bar_ms > int(last_covered_bar)


def _daily_refresh_candidates(
    instruments: list[dict[str, Any]],
    manifest_rows: dict[str, Any],
    latest_bar_ms: int,
) -> list[dict[str, Any]]:
    """Return instruments needing bootstrap or a current bar, oldest attempts first."""
    pending = []
    for instrument in instruments:
        record = manifest_rows[str(instrument["instrument_id"])]
        last_checked = record.get("last_checked_daily_bar_ms")
        last_candle = record.get("last_candle_timestamp_ms")
        latest_candle_missing = (
            last_candle is None or int(last_candle) < latest_bar_ms
        )
        if (
            not bool(record.get("bootstrap_complete"))
            or last_checked is None
            or int(last_checked) < latest_bar_ms
            or latest_candle_missing
        ):
            pending.append(instrument)

    # A failed/no-new-candle response increments bootstrap_attempts, rotating it
    # behind as-yet-unchecked instruments instead of starving the rest of the universe.
    pending.sort(key=lambda row: (
        int(
            manifest_rows[str(row["instrument_id"])].get(
                "bootstrap_attempts", 0
            )
        ),
        int(
            manifest_rows[str(row["instrument_id"])].get(
                "last_checked_daily_bar_ms"
            ) or 0
        ),
        int(
            manifest_rows[str(row["instrument_id"])].get(
                "first_seen_at_ms", 0
            )
        ),
        int(row["instrument_id"]),
    ))
    return pending


def _daily_refresh_complete(
    instruments: list[dict[str, Any]],
    manifest_rows: dict[str, Any],
    latest_bar_ms: int,
) -> bool:
    """Require every active instrument to be bootstrapped and checked through this bar."""
    return all(
        bool(manifest_rows[str(row["instrument_id"])].get("bootstrap_complete"))
        and int(
            manifest_rows[str(row["instrument_id"])].get(
                "last_checked_daily_bar_ms"
            ) or 0
        ) >= latest_bar_ms
        for row in instruments
    )


def _resolve_batch_size(requested_batch_size: int, available_count: int) -> int:
    """Resolve 0 to the full live universe; positive values remain an explicit cap."""
    if requested_batch_size < 0:
        raise ValueError("BOOTSTRAP_BATCH_SIZE cannot be negative")
    if available_count < 1:
        raise ValueError("The active perpetual universe is empty")
    return available_count if requested_batch_size == 0 else min(
        requested_batch_size, available_count
    )


def _cycle_status_requires_attention(status: str) -> bool:
    """Keep blocked decisions visible as failed workflow runs, not green monitoring."""
    return status.startswith((
        "BLOCKED_",
        "REBALANCE_BLOCKED:",
        "RETRY_BLOCKED_",
        "RETRY_EXECUTION_BLOCKED",
        "RETRY_NO_EXECUTABLE_PAIR",
        "EMERGENCY_FLATTEN_PENDING_RETRY",
        "DAILY_REFRESH_BLOCKED:",
        "DAILY_REFRESH_PENDING_NO_EXECUTABLE_PAIR",
        "UNAVAILABLE_",
    ))


def _history_row(instrument: dict[str, Any], candle: dict[str, Any]) -> dict[str, Any]:
    contract_size = dec(instrument["contract_size"])
    volume_contracts = dec(candle["volume_contracts"])
    usdt_eligible = bool(instrument.get("usdt_account_eligible"))
    quote_turnover = candle.get("turnover_quote", candle.get("turnover_usdt"))
    return {
        "instrument_id": int(instrument["instrument_id"]),
        "symbol": str(instrument["symbol"]),
        "category": str(instrument.get("category", "unclassified")),
        "quote_asset": str(instrument.get("quote_asset", "")),
        "settlement_asset": str(instrument.get("settlement_asset", "")),
        "timestamp_ms": int(candle["timestamp_ms"]),
        "open": str(candle["open"]),
        "high": str(candle["high"]),
        "low": str(candle["low"]),
        "close": str(candle["close"]),
        "volume": str(volume_contracts * contract_size) if usdt_eligible else None,
        "volume_contracts": str(volume_contracts),
        "turnover_quote": str(quote_turnover),
        "turnover_quote_asset": str(instrument.get("quote_asset", "")),
        "turnover_usdt": str(quote_turnover) if usdt_eligible else None,
    }


def _completed_candles(
    instrument: dict[str, Any],
    candles: list[dict[str, Any]],
    server_ms: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candle in candles:
        timestamp = int(candle["timestamp_ms"])
        if timestamp + DAY_MS > server_ms:
            continue
        values = [
            dec(candle[name])
            for name in ("open", "high", "low", "close")
        ]
        open_price, high, low, close = values
        if (
            min(values) <= 0
            or high < max(open_price, low, close)
            or low > min(open_price, high, close)
        ):
            continue
        rows.append(_history_row(instrument, candle))
    return rows


def _stale_or_missing_position_marks(
    positions: list[dict[str, Any]],
    ticker_by_id: dict[int, dict[str, Any]],
    now_ms: int,
    *,
    max_age_ms: int = 300_000,
) -> list[str]:
    """Return open-position symbols without a fresh, positive mark price."""
    missing: list[str] = []
    for position in positions:
        iid = int(position["instrument_id"])
        ticker = ticker_by_id.get(iid)
        if ticker is None:
            missing.append(str(position["symbol"]))
            continue
        age = now_ms - int(ticker.get("timestamp_ms", 0))
        mark = dec(ticker.get("mark_price"))
        if (
            age < -MAX_EXCHANGE_CLOCK_SKEW_MS
            or age > max_age_ms
            or mark <= 0
        ):
            missing.append(str(position["symbol"]))
    return sorted(set(missing))


def _portfolio_neutrality_metrics(
    state: dict[str, Any],
    ticker_by_id: dict[int, dict[str, Any]],
) -> dict[str, str]:
    """Measure portfolio dollar neutrality on the same marks used for NAV."""
    long_notional = Decimal("0")
    short_notional = Decimal("0")
    for position in state.get("positions", []):
        iid = int(position["instrument_id"])
        ticker = ticker_by_id.get(iid, {})
        mark = dec(ticker.get(
            "mark_price",
            position.get("last_mark_price", position.get("entry_price")),
        ))
        if mark <= 0:
            mark = dec(position.get("last_mark_price", position.get("entry_price")))
        if mark <= 0:
            raise MEXCExecutionUnavailable(
                f"Cannot verify neutrality for {position.get('symbol', iid)}: missing mark"
            )
        notional = dec(position["quantity"]) * mark
        if position["side"] == "long":
            long_notional += notional
        elif position["side"] == "short":
            short_notional += notional
        else:
            raise MEXCExecutionUnavailable("Portfolio has an invalid position side")

    nav = portfolio_nav(state, ticker_by_id)
    imbalance = abs(long_notional - short_notional)
    fraction = imbalance / nav if nav > 0 else Decimal("Infinity")
    return {
        "nav_usdt": str(nav),
        "long_notional_usdt": str(long_notional),
        "short_notional_usdt": str(short_notional),
        "imbalance_usdt": str(imbalance),
        "imbalance_nav_fraction": str(fraction),
        "gross_exposure_nav": str(
            (long_notional + short_notional) / nav if nav > 0 else Decimal("0")
        ),
    }


def _validate_two_sided_portfolio(
    state: dict[str, Any],
    ticker_by_id: dict[int, dict[str, Any]],
) -> dict[str, str]:
    """Require one positive long and one positive short; report, never gate, the gap."""
    metrics = _portfolio_neutrality_metrics(state, ticker_by_id)
    positions = state.get("positions", [])
    if positions and (
        len(positions) != 2
        or {str(row.get("side")) for row in positions} != {"long", "short"}
    ):
        raise MEXCExecutionUnavailable(
            "A rebuilt portfolio must contain exactly one long and one short"
        )
    if positions and (
        Decimal(metrics["long_notional_usdt"]) <= 0
        or Decimal(metrics["short_notional_usdt"]) <= 0
    ):
        raise MEXCExecutionUnavailable(
            "A rebuilt portfolio requires positive long and short notionals"
        )
    return metrics


def _ticker_maps(
    instruments: list[dict[str, Any]],
    ticker_rows: list[dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], list[int]]:
    ticker_by_symbol = {str(row["symbol"]): row for row in ticker_rows}
    mapped: dict[int, dict[str, Any]] = {}
    missing: list[int] = []
    for instrument in instruments:
        iid = int(instrument["instrument_id"])
        ticker = ticker_by_symbol.get(str(instrument["symbol"]))
        if ticker is None:
            missing.append(iid)
            continue
        mapped[iid] = ticker
    return mapped, missing


def _current_position_tickers(
    client: MEXCPublicClient,
    positions: list[dict[str, Any]],
    now_ms: int,
) -> tuple[dict[int, dict[str, Any]], list[str], list[str]]:
    """Get fresh held-leg marks from the server-timestamped all-market stream."""
    symbols = [str(position["symbol"]) for position in positions]
    try:
        ticker_rows = client.tickers()
    except MEXCAPIError as exc:
        detail = f"all-market ticker stream unavailable: {type(exc).__name__}: {str(exc)[:180]}"
        return {}, symbols, [detail]
    ticker_by_symbol = {str(row["symbol"]): row for row in ticker_rows}
    mapped: dict[int, dict[str, Any]] = {}
    missing: list[str] = []
    errors: list[str] = []
    for position in positions:
        symbol = str(position["symbol"])
        ticker = ticker_by_symbol.get(symbol)
        if ticker is None:
            missing.append(symbol)
            errors.append(f"{symbol}: absent from current all-market ticker push")
            continue
        timestamp_ms = int(ticker["timestamp_ms"])
        age_ms = now_ms - timestamp_ms
        mark = dec(ticker.get("mark_price"))
        if (
            age_ms < -MAX_EXCHANGE_CLOCK_SKEW_MS
            or age_ms > 300_000
            or mark <= 0
        ):
            missing.append(symbol)
            errors.append(
                f"{symbol}: ticker failed freshness gate "
                f"(timestamp_ms={timestamp_ms}, age_ms={age_ms}, mark_price={mark})"
            )
            continue
        mapped[int(position["instrument_id"])] = ticker
    return mapped, sorted(set(missing)), errors


def _ranking_report_rows(
    candidates: list[Any],
    instruments_by_key: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate ATH/ATL proximity candidates to human-readable rows."""
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        instrument = instruments_by_key.get(candidate.symbol)
        if instrument is None:
            continue
        row = {
            "symbol": str(instrument["symbol"]),
            "instrument_id": int(instrument["instrument_id"]),
            "category": str(instrument.get("category", "unclassified")),
            "distance_pct": str(candidate.distance_pct),
            "distance_metric": ("distance_below_all_time_high_pct" if candidate.direction == "long" else "distance_above_all_time_low_pct"),
            "direction": candidate.direction,
            "extreme_price": None if candidate.extreme_price is None else str(candidate.extreme_price),
            "completed_close": None if candidate.close_price is None else str(candidate.close_price),
            "extreme_timestamp_ms": candidate.extreme_timestamp_ms,
            "decision_bar_timestamp_ms": candidate.decision_timestamp_ms,
            "all_time_history_verified": bool(instrument.get("all_time_history_verified")),
            "history_observed_daily_gap_count": instrument.get("history_observed_daily_gap_count"),
        }
        rows.append(row)
    return rows


def _pending_entry_signal(
    rankings: MEXCRankings,
    daily_bar_timestamp_ms: int,
) -> dict[str, Any] | None:
    """Persist the candle-stamped execution shortlist for five-minute retries."""
    def encode(candidates: list[Candidate]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for candidate in candidates[:10]:
            instrument = rankings.instruments_by_key.get(candidate.symbol)
            if instrument is None:
                continue
            rows.append({
                "candidate_key": candidate.symbol,
                "instrument_id": int(instrument["instrument_id"]),
                "distance_pct": str(candidate.distance_pct),
                "extreme_price": None if candidate.extreme_price is None else str(candidate.extreme_price),
                "extreme_timestamp_ms": candidate.extreme_timestamp_ms,
                "decision_timestamp_ms": candidate.decision_timestamp_ms,
                "direction": candidate.direction,
                "cycle_age": candidate.cycle_age,
                "semi_cycle_direction": candidate.semi_cycle_direction,
            })
        return rows

    longs = encode(rankings.executable_longs)
    shorts = encode(rankings.executable_shorts)
    if not longs or not shorts:
        return None
    return {
        "daily_bar_timestamp_ms": int(daily_bar_timestamp_ms),
        "attempt_count": 0,
        "last_attempt_ms": None,
        "executable_longs": longs,
        "executable_shorts": shorts,
    }


def _restore_pending_rankings(
    pending: dict[str, Any],
    manifest_instruments: dict[str, dict[str, Any]],
) -> MEXCRankings | None:
    """Rehydrate the frozen daily ranking without changing the frozen ATH/ATL signal."""
    instruments_by_key: dict[str, dict[str, Any]] = {}

    def decode(side: str) -> list[Candidate]:
        rows: list[Candidate] = []
        for item in pending.get(side, []):
            iid = int(item["instrument_id"])
            instrument = manifest_instruments.get(str(iid))
            if instrument is None:
                continue
            key = str(item["candidate_key"])
            instruments_by_key[key] = instrument
            b_value = item.get("extreme_price")
            rows.append(Candidate(
                symbol=key,
                distance_pct=float(item["distance_pct"]),
                extreme_price=None if b_value is None else float(b_value),
                direction=str(item["direction"]),
                cycle_age=(
                    None if item.get("cycle_age") is None else int(item["cycle_age"])
                ),
                semi_cycle_direction=item.get("semi_cycle_direction"),
                extreme_timestamp_ms=(
                    None if item.get("extreme_timestamp_ms") is None
                    else int(item["extreme_timestamp_ms"])
                ),
                decision_timestamp_ms=(
                    None if item.get("decision_timestamp_ms") is None
                    else int(item["decision_timestamp_ms"])
                ),
            ))
        return rows

    longs = decode("executable_longs")
    shorts = decode("executable_shorts")
    if not longs or not shorts:
        return None
    return MEXCRankings(
        research_longs=longs,
        research_shorts=shorts,
        executable_longs=longs,
        executable_shorts=shorts,
        instruments_by_key=instruments_by_key,
        excluded=[],
    )


def _minimum_order_size_diagnostics(
    instruments: list[dict[str, Any]],
    ticker_by_id: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Measure minVol × contractSize × live mid-price for USDT contracts."""
    notionals: list[Decimal] = []
    affordable = {"1": 0, "10": 0}
    evaluated = 0
    for instrument in instruments:
        if not instrument.get("usdt_account_eligible"):
            continue
        iid = int(instrument["instrument_id"])
        ticker = ticker_by_id.get(iid)
        if ticker is None:
            continue
        mid = dec(ticker.get("mid_price"))
        if mid <= 0:
            continue
        minimum_base = (
            dec(instrument.get("min_contracts"))
            * dec(instrument.get("contract_size"))
        )
        notionals.append(minimum_base * mid)
        evaluated += 1
        for capital in ("1", "10"):
            try:
                quantity = size_mexc_leg(instrument, Decimal(capital), mid)
            except Exception:
                quantity = Decimal("0")
            if quantity > 0:
                affordable[capital] += 1

    if not notionals:
        return {"status": "NO_CURRENT_USDT_TICKERS", "evaluated_contracts": 0}
    ordered = sorted(notionals)

    def percentile(fraction: float) -> str:
        index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
        return str(ordered[index])

    return {
        "status": "MEASURED_FROM_CURRENT_PUBLIC_TICKERS",
        "evaluated_contracts": evaluated,
        "minimum_order_notional_usdt": {
            "p10": percentile(0.10),
            "median": percentile(0.50),
            "p75": percentile(0.75),
            "p90": percentile(0.90),
            "maximum": str(ordered[-1]),
        },
        "contracts_satisfying_minimum_quantity_and_step": {
            "at_1_usdt_per_leg": affordable["1"],
            "at_10_usdt_per_leg": affordable["10"],
        },
        "contracts_with_minimum_notional_above_1_usdt": sum(
            value > Decimal("1") for value in notionals
        ),
        "contracts_with_minimum_notional_above_10_usdt": sum(
            value > Decimal("10") for value in notionals
        ),
        "scope_note": (
            "Minimum quantity and lot-step affordability only; turnover, book depth, "
            "fees and shortfall remain separate execution gates."
        ),
    }


def _snapshot_rows(
    instruments: list[dict[str, Any]],
    ticker_by_id: dict[int, dict[str, Any]],
    now_ms: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for instrument in instruments:
        iid = int(instrument["instrument_id"])
        ticker = ticker_by_id.get(iid)
        if ticker is None:
            continue
        rows.append({
            "instrument_id": iid,
            "symbol": str(instrument["symbol"]),
            "category": str(instrument.get("category", "unclassified")),
            "quote_asset": str(instrument.get("quote_asset", "")),
            "settlement_asset": str(instrument.get("settlement_asset", "")),
            "usdt_account_eligible": bool(instrument.get("usdt_account_eligible")),
            "last_price": str(ticker["last_price"]),
            "mark_price": str(ticker["mark_price"]),
            "index_price": str(ticker["index_price"]),
            "mid_price": str(ticker["mid_price"]),
            "bid_price": str(ticker["bid_price"]),
            "ask_price": str(ticker["ask_price"]),
            "funding_rate": str(ticker["funding_rate"]),
            "turnover_24h_quote": str(ticker["turnover_24h_quote"]),
            "turnover_24h_usdt": (
                str(ticker["turnover_24h_quote"])
                if instrument.get("usdt_account_eligible") else None
            ),
            "open_interest_contracts": str(ticker["open_interest_contracts"]),
            "timestamp_ms": int(ticker["timestamp_ms"]),
            "snapshot_timestamp_ms": int(now_ms),
        })
    return rows


def _bootstrap_or_increment(
    client: MEXCPublicClient,
    store: MarketDataStore,
    instruments: list[dict[str, Any]],
    server_ms: int,
    latest_completed_bar_ms: int,
    batch_size: int,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Refresh a bounded, fair batch of stale daily bars across the full universe."""
    manifest_rows = store.manifest["instruments"]
    existing_history = store.load_all_candles()
    pending = _daily_refresh_candidates(
        instruments, manifest_rows, latest_completed_bar_ms
    )
    selected_ids = {
        int(row["instrument_id"]) for row in pending[:batch_size]
    }
    completed_count = 0
    processed_count = 0
    api_failures: list[dict[str, Any]] = []
    all_candle_updates: list[dict[str, Any]] = []

    print(
        f"[mexc-history] selected {len(selected_ids)} stale instruments "
        f"from {len(instruments)} active contracts for daily bar "
        f"{latest_completed_bar_ms}",
        flush=True,
    )

    def flush_checkpoint() -> None:
        if all_candle_updates:
            store.append_candles(all_candle_updates)
            all_candle_updates.clear()
        store.save_manifest(server_ms)

    def checkpoint_if_due() -> None:
        if processed_count and processed_count % DAILY_REFRESH_CHECKPOINT_EVERY == 0:
            flush_checkpoint()
            remaining = sum(
                not bool(manifest_rows[str(row["instrument_id"])].get("bootstrap_complete"))
                for row in instruments
            )
            print(
                f"[mexc-history] checkpoint {processed_count}/{len(selected_ids)}; "
                f"new histories bootstrapped={completed_count}; "
                f"bootstrap histories remaining={remaining}; "
                f"API failures={len(api_failures)}",
                flush=True,
            )

    for instrument in instruments:
        iid = int(instrument["instrument_id"])
        if iid not in selected_ids:
            continue
        record = manifest_rows[str(iid)]
        is_bootstrap = not bool(record.get("bootstrap_complete"))
        last_timestamp = record.get("last_candle_timestamp_ms")
        last_checked = record.get("last_checked_daily_bar_ms")
        if is_bootstrap and last_timestamp is None and last_checked is None:
            listing_time = instrument.get("opening_time_ms") or instrument.get("create_time_ms")
            start_ms = (
                (int(listing_time) // DAY_MS) * DAY_MS
                if listing_time
                else max(1, server_ms - BOOTSTRAP_DAYS * DAY_MS)
            )
        elif last_timestamp is not None:
            start_ms = int(last_timestamp) + DAY_MS
        else:
            start_ms = int(last_checked or 0) + DAY_MS

        end_ms = min(server_ms, latest_completed_bar_ms + DAY_MS)
        if start_ms >= end_ms:
            record["last_checked_daily_bar_ms"] = latest_completed_bar_ms
            if is_bootstrap:
                store.mark_bootstrap_complete(
                    iid,
                    completed_at_ms=server_ms,
                    history_status="INSUFFICIENT_HISTORY",
                )
                record["bootstrap_attempts"] = int(
                    record.get("bootstrap_attempts", 0)
                ) + 1
                completed_count += 1
            processed_count += 1
            checkpoint_if_due()
            continue

        try:
            raw = client.candles(
                str(instrument["symbol"]),
                start_timestamp_ms=start_ms,
                end_timestamp_ms=end_ms,
                interval="Day1",
            )
            rows = _completed_candles(instrument, raw, server_ms)
            existing = existing_history.get(iid, [])
            existing_stamps = {int(row["timestamp_ms"]) for row in existing}
            all_candle_updates.extend(
                row for row in rows
                if int(row["timestamp_ms"]) not in existing_stamps
            )
            total_count = len(existing) + sum(
                int(row["timestamp_ms"]) not in existing_stamps for row in rows
            )
            all_rows = existing + [
                row for row in rows
                if int(row["timestamp_ms"]) not in existing_stamps
            ]
            listing_time = instrument.get("opening_time_ms") or instrument.get("create_time_ms")
            listing_day_ms = (int(listing_time) // DAY_MS) * DAY_MS if listing_time else None
            first_history_ms = min(
                (int(row["timestamp_ms"]) for row in all_rows), default=None
            )
            last_history_ms = max(
                (int(row["timestamp_ms"]) for row in all_rows), default=None
            )
            gap_count = sum(
                max(0, (int(right["timestamp_ms"]) - int(left["timestamp_ms"])) // DAY_MS - 1)
                for left, right in zip(
                    sorted(all_rows, key=lambda row: int(row["timestamp_ms"])),
                    sorted(all_rows, key=lambda row: int(row["timestamp_ms"]))[1:],
                )
            )
            record["history_origin_timestamp_ms"] = listing_day_ms
            record["history_first_candle_timestamp_ms"] = first_history_ms
            record["history_last_candle_timestamp_ms"] = last_history_ms
            record["history_observed_daily_gap_count"] = gap_count
            record["all_time_history_verified"] = bool(
                listing_day_ms is not None
                and total_count >= 35
                and first_history_ms is not None
                and first_history_ms == listing_day_ms
                and last_history_ms is not None
                and last_history_ms >= latest_completed_bar_ms
                and gap_count == 0
            )
            if is_bootstrap:
                status = "AVAILABLE" if total_count >= 35 else "INSUFFICIENT_HISTORY"
                store.mark_bootstrap_complete(
                    iid,
                    completed_at_ms=server_ms,
                    history_status=status,
                )
                completed_count += 1
            elif (
                record.get("history_status") == "INSUFFICIENT_HISTORY"
                and total_count >= 35
            ):
                record["history_status"] = "AVAILABLE"
            record["last_checked_daily_bar_ms"] = latest_completed_bar_ms
            record["bootstrap_attempts"] = int(
                record.get("bootstrap_attempts", 0)
            ) + 1
            record["last_bootstrap_error"] = None
        except MEXCForbiddenError as exc:
            record["bootstrap_attempts"] = int(
                record.get("bootstrap_attempts", 0)
            ) + 1
            record["last_bootstrap_error"] = (
                f"{type(exc).__name__}: {str(exc)[:250]}"
            )
            processed_count += 1
            flush_checkpoint()
            print(
                f"[mexc-history] stopped on HTTP 403 after "
                f"{processed_count}/{len(selected_ids)} selected instruments",
                flush=True,
            )
            raise MEXCForbiddenError(
                f"Stopping MEXC bootstrap at {instrument['symbol']}: {exc}"
            ) from exc
        except Exception as exc:
            record["bootstrap_attempts"] = int(
                record.get("bootstrap_attempts", 0)
            ) + 1
            record["last_bootstrap_error"] = (
                f"{type(exc).__name__}: {str(exc)[:250]}"
            )
            api_failures.append({
                "instrument_id": iid,
                "symbol": instrument["symbol"],
                "error": record["last_bootstrap_error"],
            })
        processed_count += 1
        checkpoint_if_due()

    if processed_count % DAILY_REFRESH_CHECKPOINT_EVERY:
        flush_checkpoint()
    remaining = sum(
        not bool(manifest_rows[str(row["instrument_id"])].get("bootstrap_complete"))
        for row in instruments
    )
    print(
        f"[mexc-history] batch finished: checked={processed_count}/"
        f"{len(selected_ids)}; new histories bootstrapped={completed_count}; "
        f"bootstrap histories remaining={remaining}; API failures={len(api_failures)}",
        flush=True,
    )
    return completed_count, remaining, api_failures

def _load_state(
    state_dir: Path,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Verify the persisted chain and report whether a prior-run recovery probe exists."""
    paths = sorted((state_dir / "events").glob("**/*.json"))
    batches: list[dict[str, Any]] = []
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not verify_event_batch(value):
            raise RuntimeError(f"Invalid immutable paper event batch: {path}")
        batches.append(value)
    batches = order_event_batches(batches)
    previous_hash: str | None = None
    portfolio: dict[str, Any] | None = None
    recovery_probe_verified = False
    for batch in batches:
        if batch.get("previous_batch_hash") != previous_hash:
            raise RuntimeError("Paper state branch contains a broken event-batch chain")
        previous_hash = str(batch["batch_hash"])
        for event in batch["events"]:
            payload = event.get("payload", {})
            details = payload.get("details", {}) if isinstance(payload, dict) else {}
            if (
                event.get("account_id") == ACCOUNT_ID
                and event.get("event_type") == "paper_state_recovery_probe"
                and isinstance(details, dict)
                and details.get("protocol") == "PUBLISH_READBACK_THEN_FRESH_CHECKOUT"
                and details.get("financial_state_created") is False
                and details.get("account_initialized") is False
                and details.get("orders_submitted") == 0
                and "portfolio_state" not in payload
            ):
                recovery_probe_verified = True
            if event.get("account_id") == ACCOUNT_ID:
                state = payload.get("portfolio_state") if isinstance(payload, dict) else None
                if isinstance(state, dict) and state.get("account_id") == ACCOUNT_ID:
                    portfolio = state
    return portfolio, previous_hash, recovery_probe_verified


def _publish_recovery_probe(
    now_ms: int,
    previous_hash: str | None,
) -> dict[str, Any]:
    """Append a non-financial probe; publisher returns only after remote read-back."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repository:
        raise RuntimeError("Missing GitHub publisher configuration for state probe")
    stamp = int(now_ms)
    key = f"state-probe-{stamp}"
    event = {
        "event_id": key,
        "idempotency_key": key,
        "account_id": ACCOUNT_ID,
        "strategy_id": STRATEGY_ID,
        "occurred_at_ms": stamp,
        "event_type": "paper_state_recovery_probe",
        "payload": {
            "details": {
                "protocol": "PUBLISH_READBACK_THEN_FRESH_CHECKOUT",
                "purpose": (
                    "Verify GitHub append-only publisher read-back "
                    "before financial initialization"
                ),
                "financial_state_created": False,
                "account_initialized": False,
                "orders_submitted": 0,
            }
        },
    }
    batch = build_event_batch(
        run_id=key,
        created_at_ms=stamp,
        events=[event],
        previous_batch_hash=previous_hash,
    )
    publisher = GitHubStatePublisher(repository, token, branch="state")
    result = publisher.publish_batch(batch)
    return {
        "path": result.path,
        "batch_hash": result.batch_hash,
        "created": result.created,
        "commit_sha": result.commit_sha,
        "remote_readback_verified": True,
        "fresh_checkout_recovery_required": True,
    }

def _publish_event(
    state: dict[str, Any],
    details: dict[str, Any],
    event_type: str,
    idempotency_key: str,
    now_ms: int,
    previous_hash: str | None,
) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    event = {
        "event_id": f"{event_type}-{idempotency_key}",
        "idempotency_key": idempotency_key,
        "account_id": ACCOUNT_ID,
        "strategy_id": STRATEGY_ID,
        "occurred_at_ms": int(now_ms),
        "event_type": event_type,
        "payload": {
            "portfolio_state": state,
            "details": details,
        },
    }
    batch = build_event_batch(
        run_id=idempotency_key[:120],
        created_at_ms=int(now_ms),
        events=[event],
        previous_batch_hash=previous_hash,
    )
    publisher = GitHubStatePublisher(repository, token, branch="state")
    result = publisher.publish_batch(batch)
    return {
        "path": result.path,
        "batch_hash": result.batch_hash,
        "created": result.created,
        "commit_sha": result.commit_sha,
    }


def _atr(history: list[dict[str, Any]]) -> Decimal | None:
    if len(history) < 14:
        return None
    values = wilder_atr(
        [float(row["high"]) for row in history],
        [float(row["low"]) for row in history],
        [float(row["close"]) for row in history],
        period=14,
    )
    return None if values[-1] is None else Decimal(str(values[-1]))


def _fill(
    client: MEXCPublicClient,
    instrument: dict[str, Any],
    side: str,
    quantity: Decimal,
    now_ms: int,
) -> tuple[Any, dict[str, Any]]:
    book = client.orderbook(str(instrument["symbol"]), depth=50)
    # The cycle-start clock can be several minutes old after universe backfill.
    # Validate depth against MEXC server time sampled after the book was fetched.
    book_reference_ms = client.server_time_ms()
    result = simulate_mexc_fill(
        book,
        instrument,
        side,
        quantity,
        book_reference_ms,
        max_shortfall_bps=MAX_SHORTFALL_BPS,
    )
    return result, book


def _try_pair(
    client: MEXCPublicClient,
    rankings: MEXCRankings,
    history: dict[int, list[dict[str, Any]]],
    tickers: dict[int, dict[str, Any]],
    state: dict[str, Any],
    now_ms: int,
    *,
    max_pairs: int = 100,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], tuple[str, str]] | None:
    """Choose an executable pair; size each leg independently to 1x NAV."""
    nav = portfolio_nav(state, tickers)
    if nav <= 0 or max_pairs < 1:
        return None

    attempted_pairs = 0
    execution_failures: list[str] = []
    fill_cache: dict[
        tuple[int, str, str], tuple[Any, dict[str, Any]] | Exception
    ] = {}

    def cached_fill(
        instrument: dict[str, Any],
        order_side: str,
        quantity: Decimal,
    ) -> tuple[Any, dict[str, Any]]:
        key = (int(instrument["instrument_id"]), order_side, str(quantity))
        if key not in fill_cache:
            try:
                fill_cache[key] = _fill(
                    client, instrument, order_side, quantity, now_ms
                )
            except Exception as exc:
                fill_cache[key] = exc
        result = fill_cache[key]
        if isinstance(result, Exception):
            raise result
        return result

    for long_candidate in rankings.executable_longs[:10]:
        if attempted_pairs >= max_pairs:
            break
        for short_candidate in rankings.executable_shorts[:10]:
            if attempted_pairs >= max_pairs:
                break
            attempted_pairs += 1
            if long_candidate.symbol == short_candidate.symbol:
                continue

            long_instrument = rankings.instruments_by_key[long_candidate.symbol]
            short_instrument = rankings.instruments_by_key[short_candidate.symbol]
            long_id = int(long_instrument["instrument_id"])
            short_id = int(short_instrument["instrument_id"])
            long_ticker = tickers.get(long_id)
            short_ticker = tickers.get(short_id)
            if long_ticker is None or short_ticker is None:
                continue

            long_mid = dec(long_ticker.get("mid_price"))
            short_mid = dec(short_ticker.get("mid_price"))
            long_mark = dec(long_ticker.get("mark_price")) or long_mid
            short_mark = dec(short_ticker.get("mark_price")) or short_mid
            if min(long_mid, short_mid, long_mark, short_mark) <= 0:
                continue

            # 50% NAV margin at 2x leverage gives about 1x NAV notional per leg.
            # Round independently down to venue lots; record the actual gap without
            # imposing a percentage-neutrality rejection threshold.
            targets = (
                (
                    long_instrument,
                    "long",
                    long_ticker,
                    size_mexc_leg(long_instrument, nav, long_mark),
                ),
                (
                    short_instrument,
                    "short",
                    short_ticker,
                    size_mexc_leg(short_instrument, nav, short_mark),
                ),
            )
            if any(quantity <= 0 for _, _, _, quantity in targets):
                continue

            proposed: list[dict[str, Any]] = []
            fills: list[dict[str, Any]] = []
            valid = True
            for instrument, side, ticker, quantity in targets:
                iid = int(instrument["instrument_id"])
                atr = _atr(history.get(iid, []))
                if atr is None:
                    valid = False
                    break
                order_side = "buy" if side == "long" else "sell"
                try:
                    simulated, book = cached_fill(instrument, order_side, quantity)
                except Exception as exc:
                    execution_failures.append(
                        f"{instrument['symbol']}: {type(exc).__name__}: {str(exc)[:120]}"
                    )
                    valid = False
                    break

                mark = dec(ticker.get("mark_price")) or dec(ticker.get("mid_price"))
                if mark <= 0:
                    valid = False
                    break
                mark_notional = quantity * mark
                proposed.append({
                    "instrument": instrument,
                    "ticker": ticker,
                    "side": side,
                    "atr": atr,
                    "fill": simulated,
                    "target_quantity": str(quantity),
                    "current_quantity": "0",
                    "quantity_delta": str(quantity),
                    "final_quantity": str(quantity),
                    "mark_price": str(mark),
                    "mark_notional_usdt": str(mark_notional),
                    "retained_position": False,
                })
                fills.append({
                    "instrument_id": iid,
                    "symbol": instrument["symbol"],
                    "side": side,
                    "action": "OPEN",
                    "quantity_base": str(simulated.filled_quantity),
                    "quantity_delta": str(quantity),
                    "entry_or_exit_price": str(simulated.average_price),
                    "notional_usdt": str(simulated.notional),
                    "mark_notional_usdt": str(mark_notional),
                    "fee_usdt": str(simulated.fee),
                    "shortfall_bps": str(simulated.shortfall_bps),
                    "book_timestamp_ms": book["timestamp_ms"],
                })

            if valid and len(proposed) == 2:
                long_notional = sum(
                    (
                        dec(row["mark_notional_usdt"])
                        for row in proposed if row["side"] == "long"
                    ),
                    Decimal("0"),
                )
                short_notional = sum(
                    (
                        dec(row["mark_notional_usdt"])
                        for row in proposed if row["side"] == "short"
                    ),
                    Decimal("0"),
                )
                imbalance = abs(long_notional - short_notional)
                imbalance_fraction = imbalance / nav
                for row in fills:
                    row["pair_imbalance_usdt"] = str(imbalance)
                    row["pair_imbalance_nav_fraction"] = str(imbalance_fraction)
                return proposed, fills, (
                    long_candidate.symbol,
                    short_candidate.symbol,
                )

    if execution_failures:
        raise MEXCExecutionUnavailable(
            "No candidate pair passed independent NAV sizing and public-depth checks; "
            f"{len(execution_failures)} fill attempt(s) failed; "
            f"last failure: {execution_failures[-1]}"
        )
    return None


def _apply_selected_pair(
    state: dict[str, Any],
    proposed: list[dict[str, Any]],
    now_ms: int,
) -> list[dict[str, Any]]:
    """Open a fresh two-leg portfolio; retained-position resizing is not supported."""
    if state.get("positions"):
        raise MEXCExecutionUnavailable(
            "Full portfolio refresh must flatten the prior book before opening"
        )
    trial = deepcopy(state)
    opened: list[dict[str, Any]] = []
    for row in proposed:
        instrument = row["instrument"]
        simulated = row.get("fill")
        if simulated is None:
            raise RuntimeError(
                f"Missing full entry fill for {instrument['symbol']}"
            )
        opened.append(open_paper_position(
            trial,
            instrument,
            row["ticker"],
            str(row["side"]),
            row["atr"],
            simulated,
            now_ms,
            stop_atr_multiple=STOP_ATR_MULTIPLE,
        ) | {"action": "OPENED"})
    state.clear()
    state.update(trial)
    return opened


def _retry_pending_pair(
    client: MEXCPublicClient,
    portfolio: dict[str, Any],
    pending_entry: dict[str, Any],
    rankings: MEXCRankings,
    history: dict[int, list[dict[str, Any]]],
    tickers: dict[int, dict[str, Any]],
    instruments_by_id: dict[int, dict[str, Any]],
    latest_bar_ms: int,
    server_ms: int,
) -> dict[str, Any]:
    """Retry the frozen daily signal by atomically flattening and rebuilding."""
    pending_entry["attempt_count"] = int(pending_entry.get("attempt_count", 0)) + 1
    pending_entry["last_attempt_ms"] = server_ms
    try:
        result = _rebuild_fresh_portfolio(
            client,
            portfolio,
            rankings,
            history,
            tickers,
            instruments_by_id,
            server_ms,
            max_pairs=5,
        )
        if result["status"] != "PAIR_REBUILT":
            return {
                "status": "RETRY_NO_EXECUTABLE_PAIR",
                "daily_bar_timestamp_ms": latest_bar_ms,
                "attempt_count": pending_entry["attempt_count"],
                "pairs_checked_this_run": 5,
            }

        trial = result["portfolio_state"]
        trial["pending_entry_signal"] = None
        execution_ms = int(result["execution_timestamp_ms"])
        risk_window_reset = reset_daily_risk_window(
            trial,
            execution_ms,
            portfolio_nav(trial, tickers),
            reference="daily_portfolio_full_refresh_retry",
        )
        portfolio.clear()
        portfolio.update(trial)
        return {
            "status": "RETRY_PAIR_REBUILT",
            "daily_bar_timestamp_ms": latest_bar_ms,
            "attempt_count": pending_entry["attempt_count"],
            "pair": list(result["pair"]),
            "candidate_fill_estimates": result["candidate_fill_estimates"],
            "close_actions": result["close_actions"],
            "open_actions": result["open_actions"],
            "fills": result["close_actions"] + result["open_actions"],
            "risk_window_reset": risk_window_reset,
            "portfolio_neutrality": result["portfolio_neutrality"],
        }
    except Exception as exc:
        return {
            "status": "RETRY_EXECUTION_BLOCKED",
            "daily_bar_timestamp_ms": latest_bar_ms,
            "attempt_count": pending_entry["attempt_count"],
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


def _close_all(
    client: MEXCPublicClient,
    state: dict[str, Any],
    instruments_by_id: dict[int, dict[str, Any]],
    now_ms: int,
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    trial = deepcopy(state)
    for position in list(trial.get("positions", [])):
        instrument = instruments_by_id.get(int(position["instrument_id"]))
        if instrument is None:
            raise RuntimeError(
                f"Cannot close missing MEXC instrument {position['instrument_id']}"
            )
        quantity = dec(position["quantity"])
        side = "sell" if position["side"] == "long" else "buy"
        simulated, _ = _fill(client, instrument, side, quantity, now_ms)
        details.append(
            close_paper_position(trial, position, simulated, now_ms) | {"action": "CLOSED"}
        )
    state.clear()
    state.update(trial)
    return details


def _rebuild_fresh_portfolio(
    client: MEXCPublicClient,
    state: dict[str, Any],
    rankings: MEXCRankings,
    history: dict[int, list[dict[str, Any]]],
    tickers: dict[int, dict[str, Any]],
    instruments_by_id: dict[int, dict[str, Any]],
    now_ms: int,
    *,
    max_pairs: int = 100,
) -> dict[str, Any]:
    """Build on a clone; caller commits only a complete two-leg daily refresh."""
    trial = deepcopy(state)
    close_actions = _close_all(
        client, trial, instruments_by_id, now_ms
    ) if trial.get("positions") else []
    pair = _try_pair(
        client, rankings, history, tickers, trial, now_ms, max_pairs=max_pairs
    )
    if pair is None:
        return {
            "status": "NO_EXECUTABLE_PAIR",
            "portfolio_state": trial,
            "close_actions": close_actions,
            "open_actions": [],
            "candidate_fill_estimates": [],
            "pair": None,
            "portfolio_neutrality": None,
            "execution_timestamp_ms": now_ms,
        }

    proposed, candidate_fill_estimates, pair_symbols = pair
    execution_ms = client.server_time_ms()
    open_actions = _apply_selected_pair(trial, proposed, execution_ms)
    neutrality = _validate_two_sided_portfolio(trial, tickers)
    if len(trial.get("positions", [])) != 2:
        raise MEXCExecutionUnavailable(
            "Completed refresh did not create exactly two paper positions"
        )
    return {
        "status": "PAIR_REBUILT",
        "portfolio_state": trial,
        "close_actions": close_actions,
        "open_actions": open_actions,
        "candidate_fill_estimates": candidate_fill_estimates,
        "pair": pair_symbols,
        "portfolio_neutrality": neutrality,
        "execution_timestamp_ms": execution_ms,
    }



def _process_funding(
    client: MEXCPublicClient,
    store: MarketDataStore,
    state: dict[str, Any],
    server_ms: int,
) -> tuple[list[dict[str, Any]], int]:
    applied: list[dict[str, Any]] = []
    unresolved = 0
    for position in state.get("positions", []):
        iid = int(position["instrument_id"])
        start_ms = int(position.get(
            "last_funding_timestamp_ms", position["entry_timestamp_ms"]
        )) + 1
        try:
            observations = client.funding_history(
                str(position["symbol"]),
                start_timestamp_ms=start_ms,
                end_timestamp_ms=server_ms + 1,
                page_size=1000,
            )
        except Exception:
            unresolved += 1
            continue
        funding_rows = [
            {
                "instrument_id": iid,
                "symbol": str(position["symbol"]),
                **row,
            }
            for row in observations
        ]
        if funding_rows:
            store.append_funding(funding_rows)
        for row in sorted(funding_rows, key=lambda item: int(item["timestamp_ms"])):
            funding_ts = int(row["timestamp_ms"])
            if funding_ts <= int(position["entry_timestamp_ms"]):
                position["last_funding_timestamp_ms"] = max(
                    int(position["last_funding_timestamp_ms"]), funding_ts
                )
                continue
            mark = store.find_snapshot_mark(iid, funding_ts, max_age_ms=300_000)
            if mark is None:
                unresolved += 1
                # Do not leapfrog an unmatched settlement; retry from this event next run.
                break
            event = apply_confirmed_funding(
                state,
                position,
                dec(row["funding_rate"]),
                dec(mark[1]),
                funding_ts,
            )
            event["snapshot_timestamp_ms"] = mark[0]
            applied.append(event)
    state["unresolved_funding_count"] = unresolved
    return applied, unresolved


def _process_stops(
    client: MEXCPublicClient,
    state: dict[str, Any],
    instruments_by_id: dict[int, dict[str, Any]],
    tickers: dict[int, dict[str, Any]],
    history: dict[int, list[dict[str, Any]]],
    latest_completed_bar_ms: int,
    now_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Update trailing stops and flatten the pair if either leg is stopped."""
    trailing: list[dict[str, Any]] = []
    triggered_symbols: list[str] = []
    for position in list(state.get("positions", [])):
        iid = int(position["instrument_id"])
        ticker = tickers.get(iid)
        instrument = instruments_by_id.get(iid)
        if ticker is None or instrument is None:
            continue
        last_price = dec(ticker.get("last_price"))
        if last_price <= 0:
            continue
        series = history.get(iid, [])
        last_trailing_bar = int(position.get("last_trailing_bar_ms", 0))
        if (
            latest_completed_bar_ms > last_trailing_bar
            and latest_completed_bar_ms > int(position["entry_timestamp_ms"])
        ):
            bar = next(
                (
                    row for row in series
                    if int(row["timestamp_ms"]) == latest_completed_bar_ms
                ),
                None,
            )
            atr = _atr(series)
            if bar is not None and atr is not None:
                stop = update_trailing_stop(
                    position,
                    dec(bar["high"]),
                    dec(bar["low"]),
                    atr,
                    multiple=STOP_ATR_MULTIPLE,
                )
                position["last_trailing_bar_ms"] = latest_completed_bar_ms
                trailing.append({
                    "instrument_id": iid,
                    "daily_bar_timestamp_ms": latest_completed_bar_ms,
                    "atr14": str(atr),
                    "stop_price": str(stop),
                })
        if stop_is_triggered(position, last_price):
            triggered_symbols.append(str(position["symbol"]))

    if not triggered_symbols:
        return [], trailing

    # A market-neutral strategy must never leave the opposite leg running alone.
    state["pending_entry_signal"] = None
    try:
        exits = _close_all(client, state, instruments_by_id, now_ms)
        for item in exits:
            item["pair_stop_triggered_by"] = triggered_symbols
        return exits, trailing
    except Exception as exc:
        state["emergency_liquidation_pending"] = True
        risk = state.setdefault("daily_risk", {})
        risk["flattened"] = False
        risk["flatten_attempts"] = int(risk.get("flatten_attempts", 0)) + 1
        risk["flatten_error"] = (
            f"Pair stop triggered by {triggered_symbols}; "
            f"{type(exc).__name__}: {str(exc)[:180]}"
        )
        return [], trailing


def _branch_files_missing(state_dir: Path) -> bool:
    return not any((state_dir / "events").glob("**/*.json"))





def _snapshot_only_cycle(
    *,
    client: MEXCPublicClient,
    store: MarketDataStore,
    portfolio: dict[str, Any] | None,
    writer_enabled: bool,
    writer_requested: bool,
    state_write_mode: str,
    paper_account_restored_from_state_branch: bool,
    previous_hash: str | None,
    latest_bar_ms: int,
    server_ms: int,
    market_data_dir: Path,
) -> int:
    """Five-minute portfolio valuation/risk path; no ordinary strategy decisions."""
    ticker_by_id: dict[int, dict[str, Any]] = {}
    position_instruments: list[dict[str, Any]] = []
    missing_symbols: list[str] = []
    position_ticker_errors: list[str] = []
    snapshot: dict[str, Any] | None = None
    state_event: dict[str, Any] | None = None
    portfolio_report: dict[str, Any] | None = None
    emergency_exits: list[dict[str, Any]] = []
    funding_events: list[dict[str, Any]] = []
    unresolved_funding = 0
    risk_status = "NOT_APPLICABLE_NO_INITIALIZED_PAPER_ACCOUNT"
    state_changed = False
    entry_retry_report: dict[str, Any] | None = None
    pair_stop_triggered_symbols: list[str] = []

    if portfolio is not None:
        positions = list(portfolio.get("positions", []))
        pending_entry = portfolio.get("pending_entry_signal")
        if (
            writer_enabled
            and isinstance(pending_entry, dict)
            and int(pending_entry.get("daily_bar_timestamp_ms", 0)) == latest_bar_ms
            and not portfolio.get("daily_risk", {}).get("stop_triggered")
            and not portfolio.get("emergency_liquidation_pending")
        ):
            retry_rankings = _restore_pending_rankings(
                pending_entry, store.manifest.get("instruments", {})
            )
            if retry_rankings is not None:
                retry_instruments_by_id = {
                    int(row["instrument_id"]): row
                    for row in retry_rankings.instruments_by_key.values()
                }
                for position in positions:
                    iid = int(position["instrument_id"])
                    instrument = store.manifest.get("instruments", {}).get(str(iid))
                    if instrument is not None:
                        retry_instruments_by_id[iid] = instrument
                retry_instruments = list(retry_instruments_by_id.values())
                retry_tickers, retry_missing = _ticker_maps(
                    retry_instruments, client.tickers()
                )
                retry_position_gaps = _stale_or_missing_position_marks(
                    positions, retry_tickers, server_ms
                )
                if retry_missing or retry_position_gaps:
                    pending_entry["attempt_count"] = (
                        int(pending_entry.get("attempt_count", 0)) + 1
                    )
                    pending_entry["last_attempt_ms"] = server_ms
                    entry_retry_report = {
                        "status": "RETRY_BLOCKED_MISSING_MARKET_DATA",
                        "missing_instrument_ids": retry_missing,
                        "missing_position_marks": retry_position_gaps,
                        "daily_bar_timestamp_ms": latest_bar_ms,
                        "attempt_count": pending_entry["attempt_count"],
                    }
                else:
                    retry_history = store.load_all_candles()
                    entry_retry_report = _retry_pending_pair(
                        client,
                        portfolio,
                        pending_entry,
                        retry_rankings,
                        retry_history,
                        retry_tickers,
                        retry_instruments_by_id,
                        latest_bar_ms,
                        server_ms,
                    )
                state_changed = True
            else:
                pending_entry["attempt_count"] = (
                    int(pending_entry.get("attempt_count", 0)) + 1
                )
                pending_entry["last_attempt_ms"] = server_ms
                entry_retry_report = {
                    "status": "RETRY_BLOCKED_INVALID_PENDING_SIGNAL",
                    "daily_bar_timestamp_ms": latest_bar_ms,
                    "attempt_count": pending_entry["attempt_count"],
                }
                state_changed = True
        elif (
            writer_enabled
            and isinstance(pending_entry, dict)
            and int(pending_entry.get("daily_bar_timestamp_ms", 0)) < latest_bar_ms
        ):
            portfolio["pending_entry_signal"] = None
            entry_retry_report = {
                "status": "PENDING_SIGNAL_EXPIRED_ON_NEW_DAILY_CANDLE",
                "daily_bar_timestamp_ms": latest_bar_ms,
            }
            state_changed = True

        if entry_retry_report and entry_retry_report.get("status") == "RETRY_PAIR_REBUILT":
            positions = list(portfolio.get("positions", []))

        if positions:
            # The REST ticker timestamp is last-trade time and may legitimately lag
            # for quiet contracts. Use the all-market WebSocket push timestamp,
            # which is the exchange server time, for the five-minute freshness gate.
            ticker_by_id, missing_symbols, position_ticker_errors = (
                _current_position_tickers(client, positions, server_ms)
            )
            for position in positions:
                iid = int(position["instrument_id"])
                if iid not in ticker_by_id:
                    continue
                instrument = store.manifest["instruments"].get(str(iid))
                if instrument is None:
                    missing_symbols.append(str(position["symbol"]))
                    position_ticker_errors.append(
                        f"{position['symbol']}: instrument metadata missing "
                        "from market-data manifest"
                    )
                else:
                    position_instruments.append(instrument)
            missing_symbols = sorted(set(missing_symbols))

        # Funding is a confirmed cash-flow event, not an ordinary strategy decision.
        if writer_enabled:
            funding_events, unresolved_funding = _process_funding(
                client, store, portfolio, server_ms
            )
            state_changed = bool(state_changed or funding_events)

        risk = portfolio.setdefault("daily_risk", {})
        pair_stop_triggered_symbols = [
            str(position["symbol"])
            for position in portfolio.get("positions", [])
            if (
                (ticker := ticker_by_id.get(int(position["instrument_id"]))) is not None
                and dec(ticker.get("last_price")) > 0
                and stop_is_triggered(position, dec(ticker.get("last_price")))
            )
        ]
        if pair_stop_triggered_symbols:
            # Flatten both legs atomically when either leg's ATR stop is hit.
            portfolio["emergency_liquidation_pending"] = True
            portfolio["pending_entry_signal"] = None
        pending_flatten = bool(
            risk.get("stop_triggered")
            or portfolio.get("emergency_liquidation_pending")
        )
        if missing_symbols:
            risk_status = "UNAVAILABLE_STALE_OR_MISSING_POSITION_MARK"
            # Never defer an already-triggered emergency exit merely because a
            # mark is unavailable. The closing order-book simulator is independent.
            if writer_enabled and pending_flatten and portfolio.get("positions"):
                portfolio["emergency_liquidation_pending"] = True
                try:
                    instruments_by_id = {
                        int(key): value
                        for key, value in store.manifest["instruments"].items()
                    }
                    emergency_exits.extend(
                        _close_all(client, portfolio, instruments_by_id, server_ms)
                    )
                    portfolio["daily_risk"]["flattened"] = True
                    portfolio["daily_risk"]["flatten_error"] = (None)
                    portfolio["emergency_liquidation_pending"] = False
                    risk_status = "EMERGENCY_BOOK_FLATTENED_WITH_MARK_GAP"
                except Exception as exc:
                    portfolio["daily_risk"]["flattened"] = False
                    portfolio["daily_risk"]["flatten_attempts"] = int(
                        portfolio["daily_risk"].get("flatten_attempts", 0)
                    ) + 1
                    portfolio["daily_risk"]["flatten_error"] = (
                        f"{type(exc).__name__}: {str(exc)[:200]}"
                    )
                    risk_status = "EMERGENCY_FLATTEN_PENDING_RETRY"
                state_changed = True
        elif not writer_enabled:
            risk_status = "READ_ONLY_ONLY_STATE_WRITE_GATE_CLOSED"
        else:
            nav = portfolio_nav(portfolio, ticker_by_id)
            long_notional = Decimal("0")
            short_notional = Decimal("0")
            for position in portfolio.get("positions", []):
                iid = int(position["instrument_id"])
                mark = dec(
                    ticker_by_id.get(iid, {}).get(
                        "mark_price", position.get("last_mark_price")
                    )
                )
                notional = dec(position["quantity"]) * mark
                if position["side"] == "long":
                    long_notional += notional
                else:
                    short_notional += notional

            snapshot = append_equity_snapshot(
                portfolio, server_ms, nav, long_notional, short_notional
            )
            state_changed = True
            risk = portfolio["daily_risk"]
            if risk.get("stop_triggered"):
                portfolio["emergency_liquidation_pending"] = bool(
                    portfolio.get("positions")
                )

            if (
                risk.get("stop_triggered")
                or portfolio.get("emergency_liquidation_pending")
            ) and portfolio.get("positions"):
                portfolio["emergency_liquidation_pending"] = True
                try:
                    instruments_by_id = {
                        int(key): value
                        for key, value in store.manifest["instruments"].items()
                    }
                    emergency_exits.extend(
                        _close_all(client, portfolio, instruments_by_id, server_ms)
                    )
                    portfolio["daily_risk"]["flattened"] = True
                    portfolio["daily_risk"]["flatten_error"] = (None)
                    portfolio["emergency_liquidation_pending"] = False
                    risk_status = "EMERGENCY_BOOK_FLATTENED"
                except Exception as exc:
                    portfolio["daily_risk"]["flattened"] = False
                    portfolio["daily_risk"]["flatten_attempts"] = int(
                        portfolio["daily_risk"].get("flatten_attempts", 0)
                    ) + 1
                    portfolio["daily_risk"]["flatten_error"] = (
                        f"{type(exc).__name__}: {str(exc)[:200]}"
                    )
                    risk_status = "EMERGENCY_FLATTEN_PENDING_RETRY"
            elif risk.get("stop_triggered"):
                portfolio["daily_risk"]["flattened"] = True
                portfolio["emergency_liquidation_pending"] = False
                risk_status = "EMERGENCY_STOP_ACTIVE_BOOK_ALREADY_FLAT"
            elif pair_stop_triggered_symbols:
                risk_status = "PAIR_STOP_FLATTENED"
            else:
                risk_status = "MONITORING_ONLY_NO_STRATEGY_DECISIONS"

        if writer_enabled and state_changed:
            details = {
                "provider": "mexc",
                "workflow_mode": "FIVE_MINUTE_PORTFOLIO_SNAPSHOT",
                "portfolio_snapshot": snapshot,
                "emergency_risk_status": risk_status,
                "funding_events": funding_events,
                "emergency_exits": emergency_exits,
                "unresolved_funding_count": unresolved_funding,
                "missing_position_marks": missing_symbols,
                "position_ticker_errors": position_ticker_errors,
                "ordinary_strategy_decisions": False,
                "daily_signal_execution_retry": entry_retry_report is not None,
                "entry_retry": entry_retry_report,
                "pair_stop_triggered_symbols": pair_stop_triggered_symbols,
                "live_orders_submitted": 0,
            }
            state_event = _publish_event(
                portfolio,
                details,
                (
                    "paper_entry_retry" if entry_retry_report is not None
                    else "paper_portfolio_snapshot"
                ),
                f"snapshot-{server_ms}",
                server_ms,
                previous_hash,
            )
            store.save_projection({
                "schema_version": 1,
                "portfolio_state": portfolio,
                "last_run_utc": datetime.fromtimestamp(
                    server_ms / 1000, tz=UTC
                ).isoformat(),
                "state_event": state_event,
            })

        if writer_enabled and not missing_symbols:
            try:
                portfolio_report = account_report(portfolio, ticker_by_id)
            except Exception as exc:
                portfolio_report = {
                    "status": "PORTFOLIO_REPORT_UNAVAILABLE",
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "currency": "USDT",
                    "position_count": len(portfolio.get("positions", [])),
                }
        elif not writer_enabled:
            try:
                portfolio_report = account_report(portfolio, ticker_by_id)
            except Exception:
                portfolio_report = None

    if position_instruments and ticker_by_id:
        store.save_snapshot(
            server_ms, _snapshot_rows(position_instruments, ticker_by_id, server_ms)
        )
    store.manifest["last_snapshot_ms"] = server_ms
    store.save_manifest(server_ms)
    store.prune_snapshots(server_ms)

    report = {
        "run_utc": datetime.fromtimestamp(server_ms / 1000, tz=UTC).isoformat(),
        "provider": "mexc",
        "mode": "PORTFOLIO_SNAPSHOT_ONLY",
        "workflow_mode": "FIVE_MINUTE_PORTFOLIO_SNAPSHOT",
        "latest_completed_daily_bar_ms": latest_bar_ms,
        "daily_decision_due": False,
        "research_refresh_performed": False,
        "extreme_rankings_recalculated": False,
        "ordinary_strategy_decisions": False,
        "paper_state_write_requested": writer_requested,
        "paper_simulation_enabled": writer_enabled,
        "paper_account_restored_from_state_branch": paper_account_restored_from_state_branch,
        "paper_account_recovery_status": (
            "RESTORED_FROM_VERIFIED_EVENT_CHAIN"
            if paper_account_restored_from_state_branch
            else "NOT_INITIALIZED"
        ),
        "state_write_mode": state_write_mode,
        "paper_decision_status": (
            risk_status if str(risk_status).startswith("NEUTRALITY_REBALANCE_BLOCKED")
            else entry_retry_report["status"] if entry_retry_report is not None
            else risk_status
        ),
        "entry_retry": entry_retry_report,
        "ticker_coverage": len(ticker_by_id),
        "missing_ticker_count": len(missing_symbols),
        "missing_position_marks": missing_symbols,
        "position_ticker_errors": position_ticker_errors,
        "active_cross_usdt_perpetual_count": "not refreshed in snapshot mode",
        "bootstrap_remaining": "not refreshed in snapshot mode",
        "history_coverage_pass": "not checked in snapshot mode",
        "missing_latest_bar_count": 0,
        "api_failure_count": 0,
        "research_long_top": [],
        "research_short_top": [],
        "execution_long_top": [],
        "execution_short_top": [],
        "excluded_or_unranked_sample": [],
        "portfolio": portfolio_report,
        "portfolio_snapshot": snapshot,
        "pair_stop_triggered_symbols": pair_stop_triggered_symbols,
        "funding_events": funding_events,
                "emergency_exits": emergency_exits,
        "unresolved_funding_count": unresolved_funding,
        "emergency_risk_status": risk_status,
        "state_event": state_event,
        "orders_submitted": 0,
        "credentials_used": False,
        "private_endpoints_used": False,
        "wallet_used": False,
    }
    report_dir = market_data_dir / "data" / "reports" / "mexc"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "latest_cycle.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    (report_dir / "latest_cycle.md").write_text(
        _markdown_report(report), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 1 if _cycle_status_requires_attention(str(report["paper_decision_status"])) else 0

def _paper_state_writer_requested() -> bool:
    """Require an explicit true flag; the scheduled workflow supplies the default."""
    return os.environ.get("PAPER_STATE_WRITE_ENABLED", "").strip().lower() == "true"


def _run() -> int:
    market_data_dir = Path(os.environ.get("MARKET_DATA_DIR", "market-data"))
    state_dir = Path(os.environ.get("PAPER_STATE_DIR", "state"))
    requested_batch_size = int(os.environ.get(
        "BOOTSTRAP_BATCH_SIZE", str(DEFAULT_BOOTSTRAP_BATCH_SIZE)
    ))
    if requested_batch_size < 0:
        raise ValueError("BOOTSTRAP_BATCH_SIZE cannot be negative")
    store = MarketDataStore(market_data_dir, provider="mexc")
    client = MEXCPublicClient()
    server_ms = client.server_time_ms()
    latest_bar_ms = _last_completed_daily_bar(server_ms)

    # Inspect the persistent state before any universe/history/ranking requests.
    previous_state, previous_hash, recovery_probe_verified = _load_state(state_dir)
    writer_requested = _paper_state_writer_requested()
    state_write_mode = (
        "UNPROTECTED_SINGLE_WRITER_APPEND_ONLY"
        if writer_requested
        else "PAPER_STATE_WRITES_DISABLED_BY_CONFIGURATION"
    )
    writer_enabled = writer_requested
    if writer_enabled and previous_state is None and not recovery_probe_verified:
        probe_result = _publish_recovery_probe(server_ms, previous_hash)
        store.manifest["last_state_recovery_probe_ms"] = server_ms
        store.save_manifest(server_ms)
        report = {
            "run_utc": datetime.now(UTC).isoformat(),
            "provider": "mexc",
            "mode": "STATE_PERSISTENCE_PROBE",
            "paper_decision_status": "PUBLISH_READBACK_VERIFIED_AWAITING_FRESH_CHECKOUT",
            "paper_state_write_requested": True,
            "paper_simulation_enabled": False,
            "paper_account_initialized": False,
            "initial_capital_usdt": str(INITIAL_CAPITAL),
            "state_write_mode": state_write_mode,
            "state_recovery_probe": probe_result,
            "orders_submitted": 0,
            "credentials_used": False,
            "private_endpoints_used": False,
            "wallet_used": False,
        }
        report_dir = market_data_dir / "data" / "reports" / "mexc"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "latest_cycle.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        (report_dir / "latest_cycle.md").write_text(
            "# MEXC State Persistence Probe\n\n"
            "- Status: publisher write/read-back succeeded\n"
            "- Financial account initialized: no\n"
            "- Orders submitted: 0\n"
            "- Next invocation must verify this event from a fresh state-branch checkout\n",
            encoding="utf-8",
        )
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0

    account_initialization_due = (
        previous_state is None and writer_enabled and recovery_probe_verified
    )
    portfolio = previous_state
    if account_initialization_due:
        portfolio = initial_portfolio_state(
            INITIAL_CAPITAL, account_id=ACCOUNT_ID, strategy_id=STRATEGY_ID
        )
    elif portfolio is not None and portfolio.get("currency") != "USDT":
        raise RuntimeError("Existing paper account currency is not USDT")

    research_due = _daily_refresh_due(store.manifest, latest_bar_ms)
    account_attempt_bar = None
    if portfolio is not None:
        account_attempt_bar = portfolio.get(
            "last_decision_attempt_daily_bar_ms",
            portfolio.get("last_processed_daily_bar_ms"),
        )
    account_due = bool(
        writer_enabled
        and portfolio is not None
        and (
            account_attempt_bar is None
            or latest_bar_ms > int(account_attempt_bar)
        )
    )
    if not research_due and not account_due:
        return _snapshot_only_cycle(
            client=client,
            store=store,
            portfolio=portfolio,
            writer_enabled=writer_enabled,
            writer_requested=writer_requested,
            state_write_mode=state_write_mode,
            paper_account_restored_from_state_branch=previous_state is not None,
            previous_hash=previous_hash,
            latest_bar_ms=latest_bar_ms,
            server_ms=server_ms,
            market_data_dir=market_data_dir,
        )

    instruments = client.instruments()
    batch_size = _resolve_batch_size(requested_batch_size, len(instruments))
    print(
        f"[mexc-history] resolved batch size={batch_size} for "
        f"{len(instruments)} active cross-margin perpetuals "
        f"(0 configuration means full universe)",
        flush=True,
    )
    instrument_by_id = {
        int(row["instrument_id"]): row for row in instruments
    }
    lifecycle_counts = store.sync_universe(instruments, server_ms)
    latest_bar_ms = _last_completed_daily_bar(server_ms)
    bootstrap_completed, bootstrap_remaining, api_failures = _bootstrap_or_increment(
        client,
        store,
        instruments,
        server_ms,
        latest_bar_ms,
        batch_size,
    )
    for instrument in instruments:
        record = store.manifest["instruments"][str(int(instrument["instrument_id"]))]
        instrument["all_time_history_verified"] = bool(
            record.get("all_time_history_verified")
        )
        instrument["history_status"] = record.get("history_status")
        instrument["history_origin_timestamp_ms"] = record.get(
            "history_origin_timestamp_ms"
        )
        instrument["history_first_candle_timestamp_ms"] = record.get(
            "history_first_candle_timestamp_ms"
        )
        instrument["history_last_candle_timestamp_ms"] = record.get(
            "history_last_candle_timestamp_ms"
        )
        instrument["history_observed_daily_gap_count"] = int(
            record.get("history_observed_daily_gap_count", 0)
        )

    # Backfill can take minutes. Refresh the exchange clock and bulk tickers after
    # that work so marks and order-sizing inputs are current when decisions begin.
    server_ms = client.server_time_ms()
    latest_bar_ms = _last_completed_daily_bar(server_ms)
    ticker_rows = client.tickers()
    ticker_by_id, missing_tickers = _ticker_maps(instruments, ticker_rows)
    snapshot_rows = _snapshot_rows(instruments, ticker_by_id, server_ms)
    snapshot_path = store.save_snapshot(server_ms, snapshot_rows)
    history = store.load_all_candles()
    missing_latest_bar = []
    for instrument in instruments:
        iid = int(instrument["instrument_id"])
        manifest_row = store.manifest["instruments"][str(iid)]
        if (
            manifest_row.get("history_status") == "AVAILABLE"
            and (
                not history.get(iid)
                or int(history[iid][-1]["timestamp_ms"]) < latest_bar_ms
            )
        ):
            missing_latest_bar.append(iid)

    daily_refresh_complete = _daily_refresh_complete(
        instruments, store.manifest["instruments"], latest_bar_ms
    )
    history_coverage_pass = (
        bootstrap_remaining == 0
        and daily_refresh_complete
        and not api_failures
        and not missing_tickers
        and not missing_latest_bar
    )
    # Rankings remain available as soon as any instrument has enough causal
    # daily data; incomplete coverage blocks portfolio decisions, not research output.
    rankings = rank_mexc_universe(
        instruments,
        history,
        ticker_by_id,
        INITIAL_CAPITAL,
        as_of_timestamp_ms=latest_bar_ms,
    )
    paper_decision_status = (
        "BLOCKED_STATE_WRITE_GATE"
        if not writer_enabled
        else "BLOCKED_INCOMPLETE_MARKET_DATA"
    )
    state_event: dict[str, Any] | None = None
    funding_events: list[dict[str, Any]] = []
    stop_exits: list[dict[str, Any]] = []
    trailing_updates: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    unresolved_funding = 0
    if writer_enabled and portfolio is not None:
        old_last_attempt = portfolio.get(
            "last_decision_attempt_daily_bar_ms",
            portfolio.get("last_processed_daily_bar_ms"),
        )
        new_daily_bar = _daily_decision_is_due(
            history_coverage_pass,
            latest_bar_ms,
            old_last_attempt,
        )
        state_changed = False
        daily_risk_snapshot: dict[str, Any] | None = None
        risk_window_reset: dict[str, Any] | None = None
        pair_neutrality_report: dict[str, str] | None = None
        position_mark_gaps: list[str] = []
        funding_events, unresolved_funding = _process_funding(
            client, store, portfolio, server_ms
        )
        position_mark_gaps = _stale_or_missing_position_marks(
            list(portfolio.get("positions", [])),
            ticker_by_id,
            server_ms,
        )
        if position_mark_gaps:
            state_changed = True
            paper_decision_status = "BLOCKED_MISSING_POSITION_MARK"
            emergency_pending = bool(
                portfolio.get("daily_risk", {}).get("stop_triggered")
                or portfolio.get("emergency_liquidation_pending")
            )
            if emergency_pending and portfolio.get("positions"):
                portfolio["emergency_liquidation_pending"] = True
                try:
                    stop_exits.extend(
                        _close_all(client, portfolio, instrument_by_id, server_ms)
                    )
                    portfolio["daily_risk"]["flattened"] = True
                    portfolio["daily_risk"]["flatten_error"] = None
                    portfolio["emergency_liquidation_pending"] = False
                    paper_decision_status = "EMERGENCY_BOOK_FLATTENED_WITH_MARK_GAP"
                except Exception as exc:
                    portfolio["daily_risk"]["flattened"] = False
                    portfolio["emergency_liquidation_pending"] = True
                    portfolio["daily_risk"]["flatten_attempts"] = int(
                        portfolio["daily_risk"].get("flatten_attempts", 0)
                    ) + 1
                    portfolio["daily_risk"]["flatten_error"] = (
                        f"{type(exc).__name__}: {str(exc)[:200]}"
                    )
                    paper_decision_status = "EMERGENCY_FLATTEN_PENDING_RETRY"
        else:
            nav_before_decisions = portfolio_nav(portfolio, ticker_by_id)
            long_notional_before = Decimal("0")
            short_notional_before = Decimal("0")
            for position in portfolio.get("positions", []):
                mark = dec(ticker_by_id.get(int(position["instrument_id"]), {}).get(
                    "mark_price", position.get("last_mark_price")
                ))
                notional = dec(position["quantity"]) * mark
                if position["side"] == "long":
                    long_notional_before += notional
                else:
                    short_notional_before += notional
            daily_risk_snapshot = append_equity_snapshot(
                portfolio, server_ms, nav_before_decisions,
                long_notional_before, short_notional_before,
            )
            state_changed = True
            if (
                portfolio["daily_risk"].get("stop_triggered")
                or portfolio.get("emergency_liquidation_pending")
            ) and portfolio.get("positions"):
                try:
                    stop_exits.extend(_close_all(
                        client, portfolio, instrument_by_id, server_ms
                    ))
                    portfolio["daily_risk"]["flattened"] = True
                    portfolio["daily_risk"]["flatten_error"] = None
                    portfolio["emergency_liquidation_pending"] = False
                    paper_decision_status = "EMERGENCY_BOOK_FLATTENED"
                except Exception as exc:
                    portfolio["daily_risk"]["flattened"] = False
                    portfolio["emergency_liquidation_pending"] = True
                    portfolio["daily_risk"]["flatten_attempts"] = int(
                        portfolio["daily_risk"].get("flatten_attempts", 0)
                    ) + 1
                    portfolio["daily_risk"]["flatten_error"] = (
                        f"{type(exc).__name__}: {str(exc)[:200]}"
                    )
                    paper_decision_status = "EMERGENCY_FLATTEN_PENDING_RETRY"
            elif portfolio.get("daily_risk", {}).get("stop_triggered"):
                portfolio["daily_risk"]["flattened"] = True
                portfolio["emergency_liquidation_pending"] = False
                paper_decision_status = "EMERGENCY_STOP_ACTIVE_BOOK_ALREADY_FLAT"
            else:
                if history_coverage_pass:
                    stop_exits, trailing_updates = _process_stops(
                        client,
                        portfolio,
                        instrument_by_id,
                        ticker_by_id,
                        history,
                        latest_bar_ms,
                        server_ms,
                    )
                    if stop_exits:
                        paper_decision_status = "PAIR_STOP_FLATTENED"
                        portfolio["last_decision_attempt_daily_bar_ms"] = latest_bar_ms
                        portfolio["last_processed_daily_bar_ms"] = latest_bar_ms
                    elif portfolio.get("emergency_liquidation_pending"):
                        paper_decision_status = "EMERGENCY_FLATTEN_PENDING_RETRY"
                state_changed = bool(
                    state_changed or funding_events or stop_exits or trailing_updates
                )
    
    
        refresh_close_actions: list[dict[str, Any]] = []
        refresh_open_actions: list[dict[str, Any]] = []
        if (
            new_daily_bar
            and not portfolio.get("daily_risk", {}).get("stop_triggered")
            and not portfolio.get("emergency_liquidation_pending")
            and not position_mark_gaps
            and not stop_exits
        ):
            portfolio["last_decision_attempt_daily_bar_ms"] = latest_bar_ms
            portfolio["pending_entry_signal"] = _pending_entry_signal(
                rankings, latest_bar_ms
            )
            try:
                refresh = _rebuild_fresh_portfolio(
                    client,
                    portfolio,
                    rankings,
                    history,
                    ticker_by_id,
                    instrument_by_id,
                    server_ms,
                )
                refresh_close_actions = refresh["close_actions"]
                refresh_open_actions = refresh["open_actions"]
                if refresh["status"] == "PAIR_REBUILT":
                    trial = refresh["portfolio_state"]
                    trial["pending_entry_signal"] = None
                    pair_neutrality_report = refresh["portfolio_neutrality"]
                    fills = refresh_close_actions + refresh_open_actions
                    post_nav = portfolio_nav(trial, ticker_by_id)
                    post_long_notional = Decimal("0")
                    post_short_notional = Decimal("0")
                    for position in trial.get("positions", []):
                        mark = dec(ticker_by_id.get(
                            int(position["instrument_id"]), {}
                        ).get("mark_price", position.get("last_mark_price")))
                        notional = dec(position["quantity"]) * mark
                        if position["side"] == "long":
                            post_long_notional += notional
                        else:
                            post_short_notional += notional
                    risk_window_reset = reset_daily_risk_window(
                        trial,
                        int(refresh["execution_timestamp_ms"]),
                        post_nav,
                        reference="daily_portfolio_full_refresh",
                    )
                    daily_risk_snapshot = append_equity_snapshot(
                        trial,
                        int(refresh["execution_timestamp_ms"]),
                        post_nav,
                        post_long_notional,
                        post_short_notional,
                    )
                    portfolio.clear()
                    portfolio.update(trial)
                    paper_decision_status = "PAIR_REBUILT"
                    state_changed = True
                elif portfolio.get("pending_entry_signal") is None:
                    # No executable daily shortlist: close the old book and stay in USDT.
                    trial = refresh["portfolio_state"]
                    portfolio.clear()
                    portfolio.update(trial)
                    fills = refresh_close_actions
                    paper_decision_status = "FLAT_NO_EXECUTABLE_PAIR"
                    state_changed = True
                else:
                    # Preserve the previous book while this candle's frozen shortlist is retried.
                    paper_decision_status = "DAILY_REFRESH_PENDING_NO_EXECUTABLE_PAIR"
                    state_changed = True
            except Exception as exc:
                # A failed simulated close/reopen cannot partially alter the persisted account.
                paper_decision_status = (
                    f"DAILY_REFRESH_BLOCKED:{type(exc).__name__}:{str(exc)[:180]}"
                )
                state_changed = True

            if paper_decision_status in {"PAIR_REBUILT", "FLAT_NO_EXECUTABLE_PAIR"}:
                nav = portfolio_nav(portfolio, ticker_by_id)
                append_daily_equity(portfolio, latest_bar_ms, nav)
                portfolio["last_processed_daily_bar_ms"] = latest_bar_ms
                state_changed = True
        if new_daily_bar and portfolio.get("daily_risk", {}).get("stop_triggered"):
            portfolio["last_decision_attempt_daily_bar_ms"] = latest_bar_ms
            nav = portfolio_nav(portfolio, ticker_by_id)
            append_daily_equity(portfolio, latest_bar_ms, nav)
            portfolio["last_processed_daily_bar_ms"] = latest_bar_ms
            state_changed = True
        if state_changed:
            report_details = {
                "provider": "mexc",
                "daily_bar_timestamp_ms": latest_bar_ms,
                "daily_decision_status": paper_decision_status,
                "fills": fills,
                "stop_exits": stop_exits,
                "trailing_stop_updates": trailing_updates,
                "funding_events": funding_events,
                "research_long_top": _ranking_report_rows(
                    rankings.research_longs[:10], rankings.instruments_by_key
                ),
                "research_short_top": _ranking_report_rows(
                    rankings.research_shorts[:10], rankings.instruments_by_key
                ),
                "execution_long_top": _ranking_report_rows(
                    rankings.executable_longs[:10],
                    rankings.instruments_by_key,
                ),
                "execution_short_top": _ranking_report_rows(
                    rankings.executable_shorts[:10],
                    rankings.instruments_by_key,
                ),
                "history_coverage_pass": history_coverage_pass,
                "missing_position_marks": position_mark_gaps,
                "unresolved_funding_count": unresolved_funding,
                "portfolio_daily_risk_snapshot": daily_risk_snapshot,
                "portfolio_daily_risk_reset": risk_window_reset,
                "portfolio_refresh_close_actions": refresh_close_actions,
                "portfolio_refresh_open_actions": refresh_open_actions,
                "portfolio_neutrality": pair_neutrality_report,
                "emergency_risk_status": (
                    paper_decision_status
                    if str(paper_decision_status).startswith("EMERGENCY_")
                    else "WITHIN_DAILY_LIMIT"
                ),
            }
            if account_initialization_due:
                report_details["account_initialization"] = {
                    "initial_capital_usdt": str(INITIAL_CAPITAL),
                    "starting_cash_usdt": str(INITIAL_CAPITAL),
                    "account_mode": "FORWARD_PAPER_ONLY",
                    "historical_equity_imported": False,
                    "historical_trades_imported": False,
                }
                key = f"account-init-{server_ms}"
                event_type = "paper_account_initialized"
            else:
                key = (
                    f"daily-{latest_bar_ms}"
                    if paper_decision_status in {
                        "PAIR_REBUILT", "FLAT_NO_EXECUTABLE_PAIR"
                    }
                    else f"risk-{server_ms}"
                )
                event_type = (
                    "daily_portfolio_decision"
                    if key.startswith("daily-") else "paper_risk_update"
                )
            state_event = _publish_event(
                portfolio,
                report_details,
                event_type,
                key,
                server_ms,
                previous_hash,
            )
            store.save_projection({
                "schema_version": 1,
                "portfolio_state": portfolio,
                "last_run_utc": datetime.fromtimestamp(
                    server_ms / 1000, tz=UTC
                ).isoformat(),
                "state_event": state_event,
            })
            store.manifest["last_processed_daily_bar_ms"] = (
                portfolio.get("last_processed_daily_bar_ms")
            )

    # A disabled paper-state writer never initializes a simulated account.
    portfolio_report = (
        account_report(portfolio, ticker_by_id)
        if writer_enabled and portfolio is not None
        else None
    )
    report = {
        "run_utc": datetime.now(UTC).isoformat(),
        "provider": "mexc",
        "mode": "DAILY_CLOSE_RESEARCH_AND_DECISION",
        "workflow_mode": "DAILY_CLOSE_RESEARCH_AND_DECISION",
        "daily_decision_due": True,
        "research_refresh_performed": True,
        "extreme_rankings_recalculated": True,
        "ordinary_strategy_decisions": bool(writer_enabled),
        "state_recovery_probe_verified_from_prior_checkout": recovery_probe_verified,
        "paper_account_restored_from_state_branch": previous_state is not None,
        "paper_account_recovery_status": (
            "RESTORED_FROM_VERIFIED_EVENT_CHAIN"
            if previous_state is not None
            else (
                "INITIALIZED_THIS_CYCLE"
                if account_initialization_due
                else "NOT_INITIALIZED"
            )
        ),
        "paper_account_initialized_this_cycle": account_initialization_due,
        "paper_account_initial_capital_usdt": (
            str(INITIAL_CAPITAL) if account_initialization_due else None
        ),
        "instrument_count_all": len(client.contract_details()),
        "active_cross_margin_perpetual_count": len(instruments),
        "active_cross_usdt_perpetual_count": sum(
            bool(row.get("usdt_account_eligible")) for row in instruments
        ),
        "asset_class_counts": {
            category: sum(1 for row in instruments if row["category"] == category)
            for category in sorted({str(row["category"]) for row in instruments})
        },
        "ticker_count": len(ticker_rows),
        "ticker_coverage": len(ticker_by_id),
        "missing_ticker_count": len(missing_tickers),
        "missing_ticker_sample": missing_tickers[:20],
        "lifecycle_counts": lifecycle_counts,
        "bootstrap_completed_this_run": bootstrap_completed,
        "bootstrap_remaining": bootstrap_remaining,
        "candle_rows_stored": sum(len(rows) for rows in history.values()),
        "latest_completed_daily_bar_ms": latest_bar_ms,
        "missing_latest_bar_count": len(missing_latest_bar),
        "missing_latest_bar_sample": missing_latest_bar[:20],
        "api_failure_count": len(api_failures),
        "api_failure_sample": api_failures[:20],
        "history_coverage_pass": history_coverage_pass,
        "daily_refresh_complete": daily_refresh_complete,
        "minimum_order_size_diagnostics": _minimum_order_size_diagnostics(
            instruments, ticker_by_id
        ),
        "initial_capital_usdt": str(INITIAL_CAPITAL),
        "account_id": ACCOUNT_ID,
        "research_long_top": _ranking_report_rows(
            rankings.research_longs[:20], rankings.instruments_by_key
        ),
        "research_short_top": _ranking_report_rows(
            rankings.research_shorts[:20], rankings.instruments_by_key
        ),
        "execution_long_top": _ranking_report_rows(
            rankings.executable_longs[:20],
            rankings.instruments_by_key,
        ),
        "execution_short_top": _ranking_report_rows(
            rankings.executable_shorts[:20],
            rankings.instruments_by_key,
        ),
        "excluded_or_unranked_sample": rankings.excluded[:50],
        "paper_state_write_requested": writer_requested,
        "state_write_mode": state_write_mode,
        "paper_simulation_enabled": writer_enabled,
        "paper_decision_status": paper_decision_status,
        "portfolio": portfolio_report,
        "state_event": state_event,
        "funding_events": funding_events,
        "unresolved_funding_count": unresolved_funding,
        "stop_exits": stop_exits,
        "trailing_stop_updates": trailing_updates,
        "snapshot_partition": str(snapshot_path),
        "orders_submitted": 0,
        "credentials_used": False,
        "private_endpoints_used": False,
        "wallet_used": False,
    }
    report_dir = market_data_dir / "data" / "reports" / "mexc"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "latest_cycle.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    (report_dir / "latest_cycle.md").write_text(
        _markdown_report(report),
        encoding="utf-8",
    )
    store.manifest["selected_provider"] = "mexc"
    if history_coverage_pass:
        store.manifest["daily_refresh_batch_version"] = DAILY_REFRESH_BATCH_VERSION
        store.manifest["last_research_daily_bar_ms"] = latest_bar_ms
        store.manifest["last_daily_workflow_bar_ms"] = latest_bar_ms
    store.manifest["last_processed_daily_bar_ms"] = (
        store.manifest.get("last_processed_daily_bar_ms")
    )
    store.manifest["bootstrap_complete"] = bootstrap_remaining == 0
    store.save_manifest(server_ms)
    store.prune_snapshots(server_ms)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 1 if _cycle_status_requires_attention(paper_decision_status) else 0


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# MEXC High-Low ATH/ATL Paper Research",
        "",
        f"- Run (UTC): {report['run_utc']}",
        f"- Active cross-margin USDT perpetuals: {report['active_cross_usdt_perpetual_count']}",
        f"- Bootstrap remaining: {report['bootstrap_remaining']}",
        f"- History coverage gate: {report['history_coverage_pass']}",
        f"- State write mode: {report['state_write_mode']}",
        f"- Paper simulation enabled: {report['paper_simulation_enabled']}",
        f"- Decision: {report['paper_decision_status']}",
        "",
        "## Research long ranking — closest to ATH",
        "",
        "| Symbol | Distance (%) | Extreme price |",
        "|---|---:|---:|",
    ]
    lines.extend(
        f"| {row['symbol']} | {row['distance_pct']} | {row['extreme_price']} |"
        for row in report["research_long_top"]
    )
    lines.extend(["", "## Research short ranking — closest to ATL", "", "| Symbol | Distance (%) | Extreme price |", "|---|---:|---:|"])
    lines.extend(
        f"| {row['symbol']} | {row['distance_pct']} | {row['extreme_price']} |"
        for row in report["research_short_top"]
    )
    lines.extend([
        "",
        "## Data completeness",
        "",
        f"- Ticker coverage: {report['ticker_coverage']}",
        f"- Missing latest daily bars: {report['missing_latest_bar_count']}",
        f"- API failures: {report['api_failure_count']}",
        f"- Excluded/unranked records sampled: {len(report['excluded_or_unranked_sample'])}",
        "",
        "## Portfolio accounting",
        "",
    ])
    portfolio = report.get("portfolio")
    if portfolio is None:
        lines.append(
            "Not initialized. Ranking and market-data collection do not imply a paper account. "
            "The fresh-checkout recovery probe must be verified and the explicit paper-state writer enabled."
        )
    else:
        lines.extend([
            f"- Currency: {portfolio['currency']}",
            f"- Cash: {portfolio['cash']}",
            f"- NAV: {portfolio['nav']}",
            f"- Realized gross P&L: {portfolio['realized_gross_pnl']}",
            f"- Unrealized P&L: {portfolio['unrealized_pnl']}",
            f"- Fees paid: {portfolio['fees_paid']}",
            f"- Confirmed funding: {portfolio['confirmed_funding']}",
            f"- Gross exposure: {portfolio['gross_exposure']}",
            f"- Net exposure: {portfolio['net_exposure']}",
            f"- Open positions: {portfolio['position_count']}",
            f"- Closed trades: {portfolio['closed_trade_count']}",
            f"- Performance report status: {portfolio['performance']['status']}",
        ])
        for name, metric in portfolio["performance"].get("metrics", {}).items():
            lines.append(
                f"- {name}: {metric['value'] if metric['value'] is not None else 'Unavailable'}"
                + (f" ({metric['reason']})" if metric.get("reason") else "")
            )
        equity_curve = portfolio.get("daily_equity_curve", [])
        lines.extend([
            "",
            "## Daily equity curve",
            "",
            "| UTC daily bar | NAV (USDT) |",
            "|---|---:|",
        ])
        lines.extend(
            f"| {datetime.fromtimestamp(row['timestamp_ms'] / 1000, tz=UTC).isoformat()} "
            f"| {row['nav']} |"
            for row in equity_curve[-30:]
        )
        if not equity_curve:
            lines.append("No persisted daily equity observations yet.")
    lines.extend([
        "",
        "No exchange orders were submitted. All execution records are simulated.",
        "",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        raise SystemExit(_run())
    except Exception as exc:
        print(json.dumps({
            "status": "FAILED",
            "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
            "provider": "mexc",
            "orders_submitted": 0,
            "credentials_used": False,
            "private_endpoints_used": False,
        }, indent=2))
        raise SystemExit(1) from exc
