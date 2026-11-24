"""ATH/ATL cross-sectional selection and MEXC paper-execution helpers."""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any
from .execution import BookLevel, SimulatedFill, simulate_orderbook_fill
from .portfolio import target_quantity

MIN_HISTORY_FOR_RANKING = 35
MAX_BOOK_AGE_MS = 60_000
MAX_SHORTFALL_BPS = Decimal("10")
DEFAULT_MAX_TURNOVER_SHARE = Decimal("0.001")

@dataclass(frozen=True)
class Candidate:
    symbol: str
    distance_pct: float
    extreme_price: float | None
    direction: str
    cycle_age: int | None = None
    semi_cycle_direction: str | None = None
    close_price: float | None = None
    extreme_timestamp_ms: int | None = None
    decision_timestamp_ms: int | None = None

@dataclass(frozen=True)
class MEXCRankings:
    research_longs: list[Candidate]
    research_shorts: list[Candidate]
    executable_longs: list[Candidate]
    executable_shorts: list[Candidate]
    instruments_by_key: dict[str, dict[str, Any]]
    excluded: list[str]

def _dec(value: object, field: str, *, positive: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"Invalid {field}") from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise ValueError(f"Invalid {field}")
    return parsed

def rank_mexc_universe(
    instruments: Sequence[dict[str, Any]],
    histories: Mapping[int, Sequence[dict[str, Any]]],
    tickers: Mapping[int, dict[str, Any]],
    nav: Decimal,
    *,
    as_of_timestamp_ms: int,
    minimum_history: int = MIN_HISTORY_FOR_RANKING,
    max_turnover_share: Decimal = DEFAULT_MAX_TURNOVER_SHARE,
) -> MEXCRankings:
    """Rank closest-to-ATH longs and closest-to-ATL shorts using completed bars only.

    A smaller distance percentage is stronger on either side.
    """
    if not nav.is_finite() or nav <= 0:
        raise ValueError("nav must be a finite positive Decimal")
    if as_of_timestamp_ms <= 0 or minimum_history < 1 or max_turnover_share <= 0:
        raise ValueError("Invalid as-of timestamp or execution policy")
    long_rows: list[Candidate] = []
    short_rows: list[Candidate] = []
    instruments_by_key: dict[str, dict[str, Any]] = {}
    executable_keys: set[str] = set()
    excluded: list[str] = []
    for source in instruments:
        iid = int(source["instrument_id"])
        symbol = str(source["symbol"])
        key = f"mexc:{iid}"
        instrument = dict(source)
        if not bool(instrument.get("all_time_history_verified")):
            excluded.append(f"{symbol}:ALL_TIME_HISTORY_NOT_VERIFIED")
            continue
        origin_value = instrument.get("history_origin_timestamp_ms")
        if origin_value is None:
            excluded.append(f"{symbol}:MISSING_HISTORY_ORIGIN")
            continue
        origin_timestamp_ms = int(origin_value)
        rows = sorted((
            row for row in histories.get(iid, [])
            if origin_timestamp_ms <= int(row.get("timestamp_ms", 0)) <= as_of_timestamp_ms
        ), key=lambda row: int(row["timestamp_ms"]))
        if len(rows) < minimum_history:
            excluded.append(f"{symbol}:INSUFFICIENT_HISTORY:{len(rows)}")
            continue
        ticker = tickers.get(iid)
        if ticker is None:
            excluded.append(f"{symbol}:MISSING_OR_INVALID_TICKER")
            continue
        try:
            if _dec(ticker.get("last_price"), "ticker last price", positive=True) <= 0:
                raise ValueError("invalid ticker")
        except Exception:
            excluded.append(f"{symbol}:MISSING_OR_INVALID_TICKER")
            continue
        if int(rows[-1]["timestamp_ms"]) != as_of_timestamp_ms:
            excluded.append(f"{symbol}:MISSING_DECISION_CANDLE")
            continue
        try:
            close = _dec(rows[-1].get("close"), "completed close", positive=True)
            ath = max(_dec(row.get("high"), "daily high", positive=True) for row in rows)
            atl = min(_dec(row.get("low"), "daily low", positive=True) for row in rows)
        except Exception:
            excluded.append(f"{symbol}:INVALID_OHLC_HISTORY")
            continue
        if ath < close or atl > close:
            excluded.append(f"{symbol}:INVALID_HISTORICAL_EXTREME_RELATION")
            continue
        distance_ath = (ath / close - Decimal("1")) * Decimal("100")
        distance_atl = (close / atl - Decimal("1")) * Decimal("100")
        if min(distance_ath, distance_atl) < 0:
            excluded.append(f"{symbol}:NEGATIVE_EXTREME_DISTANCE")
            continue
        instruments_by_key[key] = instrument
        ath_row = max(
            rows,
            key=lambda row: (
                _dec(row.get("high"), "daily high", positive=True),
                -int(row["timestamp_ms"]),
            ),
        )
        atl_row = min(
            rows,
            key=lambda row: (
                _dec(row.get("low"), "daily low", positive=True),
                int(row["timestamp_ms"]),
            ),
        )
        long_rows.append(Candidate(
            symbol=key,
            distance_pct=float(distance_ath),
            extreme_price=float(ath),
            direction="long",
            close_price=float(close),
            extreme_timestamp_ms=int(ath_row["timestamp_ms"]),
            decision_timestamp_ms=as_of_timestamp_ms,
        ))
        short_rows.append(Candidate(
            symbol=key,
            distance_pct=float(distance_atl),
            extreme_price=float(atl),
            direction="short",
            close_price=float(close),
            extreme_timestamp_ms=int(atl_row["timestamp_ms"]),
            decision_timestamp_ms=as_of_timestamp_ms,
        ))
        if not instrument.get("usdt_account_eligible"):
            excluded.append(f"{symbol}:NON_USDT_SETTLEMENT_RESEARCH_ONLY")
            continue
        turnovers: list[Decimal] = []
        for row in rows[-30:]:
            if row.get("turnover_usdt") is None:
                continue
            turnover = _dec(row["turnover_usdt"], "daily USDT turnover")
            if turnover > 0:
                turnovers.append(turnover)
        if not turnovers:
            excluded.append(f"{symbol}:NO_TURNOVER_HISTORY")
            continue
        median_turnover = Decimal(str(median(turnovers)))
        if nav <= median_turnover * max_turnover_share:
            executable_keys.add(key)
        else:
            excluded.append(f"{symbol}:TURNOVER_CAP")
    long_rows.sort(key=lambda item: (item.distance_pct, item.symbol))
    short_rows.sort(key=lambda item: (item.distance_pct, item.symbol))
    return MEXCRankings(
        research_longs=long_rows,
        research_shorts=short_rows,
        executable_longs=[row for row in long_rows if row.symbol in executable_keys],
        executable_shorts=[row for row in short_rows if row.symbol in executable_keys],
        instruments_by_key=instruments_by_key,
        excluded=excluded,
    )

def base_quantity_rules(instrument: Mapping[str, Any], reference_price: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    contract_size = _dec(instrument["contract_size"], "contract_size", positive=True)
    step_contracts = _dec(instrument["quantity_step_contracts"], "quantity_step_contracts", positive=True)
    minimum_contracts = _dec(instrument["min_contracts"], "min_contracts", positive=True)
    maximum_contracts = _dec(instrument["max_contracts"], "max_contracts", positive=True)
    if reference_price <= 0 or maximum_contracts < minimum_contracts:
        raise ValueError("Invalid MEXC contract sizing bounds")
    return step_contracts*contract_size, minimum_contracts*contract_size, maximum_contracts*contract_size, contract_size

def size_mexc_leg(instrument: Mapping[str, Any], nav: Decimal, reference_price: Decimal, *, allocation_fraction: Decimal = Decimal("0.5"), leverage: Decimal = Decimal("2")) -> Decimal:
    """Size to 1x NAV notional per leg (50% NAV margin at 2x), rounded down."""
    if not leverage.is_finite() or leverage <= 0:
        raise ValueError("leverage must be finite and positive")
    notional_fraction = allocation_fraction * leverage
    if notional_fraction <= 0 or notional_fraction > 1:
        raise ValueError("allocation_fraction × leverage must be in (0, 1]")
    step, minimum, maximum, _ = base_quantity_rules(instrument, reference_price)
    return target_quantity(nav, notional_fraction, reference_price, step, minimum, Decimal("0"), maximum)

def mexc_book_levels(raw_levels: Sequence[Mapping[str, Any]], contract_size: Decimal) -> list[BookLevel]:
    if not contract_size.is_finite() or contract_size <= 0:
        raise ValueError("contract_size must be positive")
    levels = [BookLevel(_dec(row.get("price"), "book price", positive=True), _dec(row.get("quantity_contracts"), "book quantity", positive=True)*contract_size) for row in raw_levels]
    if not levels:
        raise ValueError("MEXC order-book side is empty")
    return levels

def simulate_mexc_fill(raw_book: Mapping[str, Any], instrument: Mapping[str, Any], side: str, base_quantity: Decimal, now_ms: int, *, max_shortfall_bps: Decimal = MAX_SHORTFALL_BPS) -> SimulatedFill:
    timestamp = int(raw_book["timestamp_ms"])
    age = now_ms - timestamp
    if age < 0 or age > MAX_BOOK_AGE_MS:
        raise ValueError(f"Stale or future MEXC order book ({age} ms)")
    contract_size = _dec(instrument["contract_size"], "contract_size", positive=True)
    bids = mexc_book_levels(raw_book["bids"], contract_size)
    asks = mexc_book_levels(raw_book["asks"], contract_size)
    fee_rate = _dec(instrument["taker_fee_rate"], "taker_fee_rate")
    return simulate_orderbook_fill(side, base_quantity, bids, asks, fee_rate=fee_rate, max_shortfall_bps=max_shortfall_bps, allow_partial=False)
