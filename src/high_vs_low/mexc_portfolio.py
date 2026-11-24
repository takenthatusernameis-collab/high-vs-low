"""MEXC USDT forward-paper portfolio accounting.

All quantities are normalized to base-asset units and all cashflows are USDT.
This module is deterministic and never submits exchange orders.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .execution import SimulatedFill
from .performance import EquityPoint, compute_performance_metrics
from .portfolio import Position, funding_cashflow, portfolio_exposure

ZERO = Decimal("0")
ONE = Decimal("1")
DEFAULT_INITIAL_CAPITAL = Decimal("100")
DEFAULT_DAILY_DRAWDOWN_LIMIT = Decimal("0.25")
DEFAULT_LEVERAGE = Decimal("2")


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"Invalid {field}") from exc
    if not result.is_finite():
        raise ValueError(f"Non-finite {field}")
    return result


def initial_portfolio_state(
    initial_capital: Decimal = DEFAULT_INITIAL_CAPITAL,
    *,
    account_id: str = "mexc-usdt-forward-high-low-100usdt-v1",
    strategy_id: str = "high-low-ath-atl-cross-sectional-v1-100usdt",
) -> dict[str, Any]:
    if not initial_capital.is_finite() or initial_capital <= ZERO:
        raise ValueError("Initial capital must be finite and positive")
    return {
        "schema_version": 1,
        "account_id": account_id,
        "strategy_id": strategy_id,
        "currency": "USDT",
        "initial_capital": str(initial_capital),
        "cash": str(initial_capital),
        "realized_gross_pnl": "0",
        "fees_paid": "0",
        "confirmed_funding": "0",
        "positions": [],
        "closed_trade_net_pnls": [],
        "partial_realized_net_pnls": [],
        "daily_equity": [],
        "equity_curve": [],
        "daily_risk": {},
        "last_processed_daily_bar_ms": None,
        "last_snapshot_ms": None,
        "unresolved_funding_count": 0,
        "status": "INITIALIZED_PAPER_ONLY",
    }


def portfolio_nav(
    state: dict[str, Any],
    ticker_by_instrument_id: dict[int, dict[str, Any]],
) -> Decimal:
    unrealized = ZERO
    for position in state.get("positions", []):
        iid = int(position["instrument_id"])
        ticker = ticker_by_instrument_id.get(iid, {})
        mark = _decimal(
            ticker.get("mark_price", position.get("last_mark_price", position["entry_price"])),
            "mark price",
        )
        if mark <= ZERO:
            mark = _decimal(position["last_mark_price"], "last mark price")
        model = Position(
            symbol=str(position["symbol"]),
            side=str(position["side"]),
            quantity=_decimal(position["quantity"], "position quantity"),
            entry_price=_decimal(position["entry_price"], "entry price"),
        )
        unrealized += model.unrealized_pnl(mark)
        position["last_mark_price"] = str(mark)
    nav = (
        _decimal(state["cash"], "cash")
        + unrealized
    )
    if not nav.is_finite():
        raise ValueError("Portfolio NAV is non-finite")
    return nav


def open_paper_position(
    state: dict[str, Any],
    instrument: dict[str, Any],
    ticker: dict[str, Any],
    side: str,
    atr: Decimal,
    fill: SimulatedFill,
    timestamp_ms: int,
    *,
    stop_atr_multiple: Decimal = Decimal("3"),
    leverage: Decimal = DEFAULT_LEVERAGE,
) -> dict[str, Any]:
    if side not in {"long", "short"}:
        raise ValueError("Position side must be long or short")
    if fill.side != ("buy" if side == "long" else "sell") or not fill.complete:
        raise ValueError("Entry fill side or completeness is invalid")
    iid = int(instrument["instrument_id"])
    if any(int(row["instrument_id"]) == iid for row in state.get("positions", [])):
        raise ValueError("Instrument already has an open paper position")
    if atr <= ZERO or stop_atr_multiple <= ZERO or not leverage.is_finite() or leverage <= ZERO:
        raise ValueError("ATR, stop multiple and leverage must be positive")
    maximum_leverage = _decimal(instrument.get("max_leverage", leverage), "maximum leverage")
    if leverage > maximum_leverage:
        raise ValueError("Requested paper leverage exceeds instrument maximum")
    entry = fill.average_price
    stop = (
        entry - atr * stop_atr_multiple
        if side == "long"
        else entry + atr * stop_atr_multiple
    )
    if stop <= ZERO:
        raise ValueError("Initial stop price is non-positive")
    fee = fill.fee
    state["cash"] = str(_decimal(state["cash"], "cash") - fee)
    state["fees_paid"] = str(_decimal(state["fees_paid"], "fees") + fee)
    position = {
        "instrument_id": iid,
        "symbol": str(instrument["symbol"]),
        "category": str(instrument.get("category", "unclassified")),
        "side": side,
        "quantity": str(fill.filled_quantity),
        "entry_price": str(entry),
        "entry_timestamp_ms": int(timestamp_ms),
        "entry_fee": str(fee),
        "leverage": str(leverage),
        "initial_margin_usdt": str(fill.notional / leverage),
        "last_mark_price": str(ticker.get("mark_price", entry)),
        "stop_price": str(stop),
        "highest_since_entry": str(entry),
        "lowest_since_entry": str(entry),
        "last_funding_timestamp_ms": int(timestamp_ms),
        "unresolved_funding_count": 0,
    }
    state.setdefault("positions", []).append(position)
    return position



def increase_paper_position(
    state: dict[str, Any],
    position: dict[str, Any],
    instrument: dict[str, Any],
    ticker: dict[str, Any],
    atr: Decimal,
    fill: SimulatedFill,
    timestamp_ms: int,
    *,
    stop_atr_multiple: Decimal = Decimal("3"),
) -> dict[str, Any]:
    """Add only the required quantity while preserving the existing position."""
    expected_side = "buy" if position["side"] == "long" else "sell"
    if fill.side != expected_side or not fill.complete:
        raise ValueError("Rebalance add fill side or completeness is invalid")
    if int(position["instrument_id"]) != int(instrument["instrument_id"]):
        raise ValueError("Rebalance add instrument does not match position")
    if atr <= ZERO or stop_atr_multiple <= ZERO:
        raise ValueError("ATR and stop multiple must be positive")

    old_quantity = _decimal(position["quantity"], "existing position quantity")
    added_quantity = fill.filled_quantity
    if old_quantity <= ZERO or added_quantity <= ZERO:
        raise ValueError("Existing and added quantities must be positive")
    old_entry = _decimal(position["entry_price"], "existing entry price")
    new_quantity = old_quantity + added_quantity
    average_entry = (
        old_quantity * old_entry + added_quantity * fill.average_price
    ) / new_quantity
    leverage = _decimal(position["leverage"], "position leverage")
    fee = fill.fee
    state["cash"] = str(_decimal(state["cash"], "cash") - fee)
    state["fees_paid"] = str(_decimal(state["fees_paid"], "fees") + fee)
    position["quantity"] = str(new_quantity)
    position["entry_price"] = str(average_entry)
    position["entry_fee"] = str(
        _decimal(position.get("entry_fee", "0"), "entry fee") + fee
    )
    position["initial_margin_usdt"] = str(
        _decimal(position["initial_margin_usdt"], "initial margin")
        + fill.notional / leverage
    )
    current_mark = _decimal(
        ticker.get("mark_price", fill.average_price), "rebalance mark price"
    )
    position["last_mark_price"] = str(current_mark)
    candidate_stop = (
        average_entry - atr * stop_atr_multiple
        if position["side"] == "long"
        else average_entry + atr * stop_atr_multiple
    )
    if candidate_stop <= ZERO:
        raise ValueError("Rebalanced ATR stop is non-positive")
    old_stop = _decimal(position["stop_price"], "existing stop price")
    position["stop_price"] = str(
        max(old_stop, candidate_stop)
        if position["side"] == "long"
        else min(old_stop, candidate_stop)
    )
    position["highest_since_entry"] = str(max(
        _decimal(position.get("highest_since_entry", old_entry), "position high"),
        current_mark,
        fill.average_price,
    ))
    position["lowest_since_entry"] = str(min(
        _decimal(position.get("lowest_since_entry", old_entry), "position low"),
        current_mark,
        fill.average_price,
    ))
    # Prior funding has already been reconciled before the daily rebalance.
    # Advance the watermark so the enlarged quantity is not credited/debited
    # for settlements that occurred before this incremental fill.
    position["last_funding_timestamp_ms"] = max(
        int(position.get("last_funding_timestamp_ms", 0)), int(timestamp_ms)
    )
    position["last_rebalanced_at_ms"] = int(timestamp_ms)
    return {
        "instrument_id": int(position["instrument_id"]),
        "symbol": str(position["symbol"]),
        "side": str(position["side"]),
        "action": "INCREASED",
        "quantity_delta": str(added_quantity),
        "new_quantity": str(new_quantity),
        "average_entry_price": str(average_entry),
        "fill_price": str(fill.average_price),
        "notional_usdt": str(fill.notional),
        "fee_usdt": str(fee),
        "shortfall_bps": str(fill.shortfall_bps),
    }


def reduce_paper_position(
    state: dict[str, Any],
    position: dict[str, Any],
    fill: SimulatedFill,
    timestamp_ms: int,
) -> dict[str, Any]:
    """Realize only the requested slice; leave the rest of the position open."""
    expected_side = "sell" if position["side"] == "long" else "buy"
    if fill.side != expected_side or not fill.complete:
        raise ValueError("Rebalance reduction fill side or completeness is invalid")
    old_quantity = _decimal(position["quantity"], "existing position quantity")
    reduced_quantity = fill.filled_quantity
    if reduced_quantity <= ZERO or reduced_quantity > old_quantity:
        raise ValueError("Reduced quantity must be positive and no larger than position")
    if reduced_quantity == old_quantity:
        return close_paper_position(state, position, fill, timestamp_ms)

    model = Position(
        symbol=str(position["symbol"]),
        side=str(position["side"]),
        quantity=reduced_quantity,
        entry_price=_decimal(position["entry_price"], "entry price"),
    )
    realized = model.unrealized_pnl(fill.average_price)
    old_entry_fee = _decimal(position.get("entry_fee", "0"), "entry fee")
    allocated_entry_fee = old_entry_fee * reduced_quantity / old_quantity
    remaining_entry_fee = old_entry_fee - allocated_entry_fee
    exit_fee = fill.fee
    remaining_quantity = old_quantity - reduced_quantity
    leverage = _decimal(position["leverage"], "position leverage")

    state["cash"] = str(
        _decimal(state["cash"], "cash") + realized - exit_fee
    )
    state["realized_gross_pnl"] = str(
        _decimal(state["realized_gross_pnl"], "realized gross PnL") + realized
    )
    state["fees_paid"] = str(_decimal(state["fees_paid"], "fees") + exit_fee)
    partial_net = realized - allocated_entry_fee - exit_fee
    state.setdefault("partial_realized_net_pnls", []).append(str(partial_net))

    position["quantity"] = str(remaining_quantity)
    position["entry_fee"] = str(remaining_entry_fee)
    position["initial_margin_usdt"] = str(
        remaining_quantity
        * _decimal(position["entry_price"], "entry price")
        / leverage
    )
    position["last_funding_timestamp_ms"] = max(
        int(position.get("last_funding_timestamp_ms", 0)), int(timestamp_ms)
    )
    position["last_rebalanced_at_ms"] = int(timestamp_ms)
    return {
        "instrument_id": int(position["instrument_id"]),
        "symbol": str(position["symbol"]),
        "side": str(position["side"]),
        "action": "REDUCED",
        "quantity_delta": str(-reduced_quantity),
        "remaining_quantity": str(remaining_quantity),
        "entry_price": str(position["entry_price"]),
        "exit_price": str(fill.average_price),
        "realized_gross_pnl": str(realized),
        "allocated_entry_fee": str(allocated_entry_fee),
        "exit_fee": str(exit_fee),
        "partial_realized_net_pnl": str(partial_net),
        "shortfall_bps": str(fill.shortfall_bps),
    }

def close_paper_position(
    state: dict[str, Any],
    position: dict[str, Any],
    fill: SimulatedFill,
    timestamp_ms: int,
) -> dict[str, Any]:
    expected_side = "sell" if position["side"] == "long" else "buy"
    if fill.side != expected_side or not fill.complete:
        raise ValueError("Exit fill side or completeness is invalid")
    if fill.filled_quantity != _decimal(position["quantity"], "position quantity"):
        raise ValueError("Exit fill quantity does not close the full paper position")
    model = Position(
        symbol=str(position["symbol"]),
        side=str(position["side"]),
        quantity=_decimal(position["quantity"], "position quantity"),
        entry_price=_decimal(position["entry_price"], "entry price"),
    )
    realized = model.unrealized_pnl(fill.average_price)
    exit_fee = fill.fee
    entry_fee = _decimal(position.get("entry_fee", "0"), "entry fee")
    state["cash"] = str(_decimal(state["cash"], "cash") + realized - exit_fee)
    state["realized_gross_pnl"] = str(
        _decimal(state["realized_gross_pnl"], "realized gross PnL") + realized
    )
    state["fees_paid"] = str(_decimal(state["fees_paid"], "fees") + exit_fee)
    net_trade = realized - entry_fee - exit_fee
    state.setdefault("closed_trade_net_pnls", []).append(str(net_trade))
    state["positions"] = [
        row for row in state.get("positions", [])
        if int(row["instrument_id"]) != int(position["instrument_id"])
    ]
    return {
        "instrument_id": int(position["instrument_id"]),
        "symbol": str(position["symbol"]),
        "side": str(position["side"]),
        "quantity": str(fill.filled_quantity),
        "entry_price": str(position["entry_price"]),
        "exit_price": str(fill.average_price),
        "realized_gross_pnl": str(realized),
        "entry_fee": str(entry_fee),
        "exit_fee": str(exit_fee),
        "net_trade_pnl": str(net_trade),
        "exit_timestamp_ms": int(timestamp_ms),
        "shortfall_bps": str(fill.shortfall_bps),
    }


def apply_confirmed_funding(
    state: dict[str, Any],
    position: dict[str, Any],
    funding_rate: Decimal,
    settlement_mark: Decimal,
    funding_timestamp_ms: int,
) -> dict[str, Any]:
    cashflow = funding_cashflow(
        str(position["side"]),
        _decimal(position["quantity"], "position quantity"),
        settlement_mark,
        funding_rate,
    )
    state["cash"] = str(_decimal(state["cash"], "cash") + cashflow)
    state["confirmed_funding"] = str(
        _decimal(state["confirmed_funding"], "confirmed funding") + cashflow
    )
    position["last_funding_timestamp_ms"] = int(funding_timestamp_ms)
    return {
        "instrument_id": int(position["instrument_id"]),
        "funding_timestamp_ms": int(funding_timestamp_ms),
        "funding_rate": str(funding_rate),
        "settlement_mark_price": str(settlement_mark),
        "cashflow_usdt": str(cashflow),
    }


def update_trailing_stop(
    position: dict[str, Any],
    high: Decimal,
    low: Decimal,
    atr: Decimal,
    *,
    multiple: Decimal = Decimal("3"),
) -> Decimal:
    if min(high, low, atr, multiple) <= ZERO:
        raise ValueError("Trailing stop inputs must be positive")
    if high < low:
        raise ValueError("Daily high cannot be below daily low")
    if position["side"] == "long":
        extreme = max(_decimal(position["highest_since_entry"], "highest"), high)
        proposed = extreme - atr * multiple
        stop = max(_decimal(position["stop_price"], "stop"), proposed)
        position["highest_since_entry"] = str(extreme)
    elif position["side"] == "short":
        extreme = min(_decimal(position["lowest_since_entry"], "lowest"), low)
        proposed = extreme + atr * multiple
        stop = min(_decimal(position["stop_price"], "stop"), proposed)
        position["lowest_since_entry"] = str(extreme)
    else:
        raise ValueError("Position side must be long or short")
    if stop <= ZERO:
        raise ValueError("Trailing stop became non-positive")
    position["stop_price"] = str(stop)
    return stop


def stop_is_triggered(position: dict[str, Any], last_price: Decimal) -> bool:
    stop = _decimal(position["stop_price"], "stop price")
    if position["side"] == "long":
        return last_price <= stop
    if position["side"] == "short":
        return last_price >= stop
    raise ValueError("Position side must be long or short")


def append_daily_equity(
    state: dict[str, Any],
    timestamp_ms: int,
    nav: Decimal,
) -> None:
    if timestamp_ms <= 0 or nav <= ZERO or not nav.is_finite():
        raise ValueError("Invalid daily equity observation")
    curve = state.setdefault("daily_equity", [])
    if curve and int(curve[-1]["timestamp_ms"]) == int(timestamp_ms):
        curve[-1]["nav"] = str(nav)
        return
    if curve and int(curve[-1]["timestamp_ms"]) > int(timestamp_ms):
        raise ValueError("Daily equity timestamps cannot move backwards")
    curve.append({"timestamp_ms": int(timestamp_ms), "nav": str(nav)})



def append_equity_snapshot(
    state: dict[str, Any],
    timestamp_ms: int,
    nav: Decimal,
    long_notional: Decimal,
    short_notional: Decimal,
    *,
    drawdown_limit: Decimal = DEFAULT_DAILY_DRAWDOWN_LIMIT,
) -> dict[str, Any]:
    """Append a mark and update combined-book daily risk using prior-day close as baseline."""
    for value, field in (
        (nav, "nav"),
        (long_notional, "long_notional"),
        (short_notional, "short_notional"),
        (drawdown_limit, "drawdown_limit"),
    ):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"{field} must be a finite Decimal")
    if timestamp_ms <= 0 or long_notional < ZERO or short_notional < ZERO:
        raise ValueError("Invalid equity snapshot timestamp or exposure")
    if drawdown_limit <= ZERO or drawdown_limit >= ONE:
        raise ValueError("drawdown_limit must be in (0, 1)")

    curve = state.setdefault("equity_curve", [])
    if curve and int(curve[-1]["timestamp_ms"]) > int(timestamp_ms):
        raise ValueError("Equity-curve timestamps cannot move backwards")

    timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    day_key = timestamp.strftime("%Y-%m-%d")
    day_start_ms = int(
        timestamp.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        * 1000
    )
    risk = state.setdefault("daily_risk", {})
    if risk.get("utc_date") != day_key:
        # The last successful mark before UTC midnight is the daily opening
        # equity reference. This captures price moves during delayed/missed
        # first cron invocations instead of resetting the baseline to a later,
        # potentially already-damaged portfolio valuation.
        prior_nav = (
            _decimal(curve[-1]["nav"], "prior equity snapshot NAV")
            if curve and int(curve[-1]["timestamp_ms"]) < day_start_ms
            else nav
        )
        risk = {
            "utc_date": day_key,
            "starting_nav": str(prior_nav),
            "starting_timestamp_ms": day_start_ms,
            "starting_reference": (
                "previous_utc_day_last_snapshot"
                if curve and int(curve[-1]["timestamp_ms"]) < day_start_ms
                else "first_observation_of_utc_day"
            ),
            "calendar_day_starting_nav": str(prior_nav),
            "calendar_day_starting_reference": (
                "previous_utc_day_last_snapshot"
                if curve and int(curve[-1]["timestamp_ms"]) < day_start_ms
                else "first_observation_of_utc_day"
            ),
            "peak_nav": str(max(prior_nav, nav)),
            "peak_timestamp_ms": (
                int(timestamp_ms) if nav > prior_nav else day_start_ms
            ),
            "stop_triggered": False,
            "stop_triggered_at_ms": None,
            "stop_reason": None,
            "flattened": False,
            "flatten_attempts": 0,
        }
        state["daily_risk"] = risk
    else:
        peak = _decimal(risk.get("peak_nav", nav), "daily peak NAV")
        if nav > peak:
            risk["peak_nav"] = str(nav)
            risk["peak_timestamp_ms"] = int(timestamp_ms)

    risk_window_starting_nav = _decimal(
        risk["starting_nav"], "drawdown-window starting NAV"
    )
    calendar_starting_nav = _decimal(
        risk.get("calendar_day_starting_nav", risk["starting_nav"]),
        "calendar-day starting NAV",
    )
    peak_nav = _decimal(risk["peak_nav"], "daily peak NAV")
    if calendar_starting_nav <= ZERO or peak_nav <= ZERO:
        daily_return = Decimal("-1")
        daily_drawdown = Decimal("-1")
    else:
        # Calendar-day return remains anchored to UTC rollover even when a
        # successful daily rebalance restarts the drawdown risk window later.
        daily_return = nav / calendar_starting_nav - ONE
        daily_drawdown = nav / peak_nav - ONE

    newly_triggered = False
    if daily_drawdown <= -drawdown_limit and not bool(risk.get("stop_triggered")):
        risk["stop_triggered"] = True
        risk["stop_triggered_at_ms"] = int(timestamp_ms)
        risk["stop_reason"] = "COMBINED_PORTFOLIO_DAILY_DRAWDOWN_LIMIT"
        risk["flattened"] = False
        newly_triggered = True

    initial_capital = _decimal(state["initial_capital"], "initial capital")
    total_roi = nav / initial_capital - ONE if initial_capital > ZERO else ZERO
    gross_notional = long_notional + short_notional
    gross_exposure = gross_notional / nav if nav > ZERO else None
    snapshot = {
        "timestamp_ms": int(timestamp_ms),
        "utc_date": day_key,
        "nav": str(nav),
        "roi_pct": str(total_roi * Decimal("100")),
        "daily_return_pct": str(daily_return * Decimal("100")),
        "daily_drawdown_pct": str(daily_drawdown * Decimal("100")),
        "daily_starting_nav": str(calendar_starting_nav),
        "daily_starting_reference": risk.get(
            "calendar_day_starting_reference", risk["starting_reference"]
        ),
        "drawdown_window_starting_nav": str(risk_window_starting_nav),
        "drawdown_window_reference": risk["starting_reference"],
        "daily_peak_nav": str(peak_nav),
        "long_notional_usdt": str(long_notional),
        "short_notional_usdt": str(short_notional),
        "gross_exposure": None if gross_exposure is None else str(gross_exposure),
        "emergency_stop_triggered": bool(risk.get("stop_triggered")),
        "emergency_stop_newly_triggered": newly_triggered,
        "emergency_stop_flattened": bool(risk.get("flattened")),
    }

    if curve and int(curve[-1]["timestamp_ms"]) == int(timestamp_ms):
        curve[-1] = snapshot
    else:
        curve.append(snapshot)
    state["last_snapshot_ms"] = int(timestamp_ms)
    return snapshot


def reset_daily_risk_window(
    state: dict[str, Any],
    timestamp_ms: int,
    nav: Decimal,
    *,
    reference: str = "daily_portfolio_rebalance",
) -> dict[str, Any]:
    """Start a fresh daily drawdown window after a successful portfolio rebalance.

    An already-latched emergency stop is never cleared here: a triggered flatten
    must complete before a new trade/risk window can start.
    """
    if timestamp_ms <= 0 or not isinstance(nav, Decimal) or not nav.is_finite() or nav <= ZERO:
        raise ValueError("Risk-window reset requires a positive timestamp and NAV")
    risk = state.setdefault("daily_risk", {})
    if risk.get("stop_triggered") or state.get("emergency_liquidation_pending"):
        raise ValueError("Cannot reset a latched emergency drawdown/flatten state")
    timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    existing_calendar_nav = risk.get(
        "calendar_day_starting_nav", risk.get("starting_nav", nav)
    )
    existing_calendar_reference = risk.get(
        "calendar_day_starting_reference",
        risk.get("starting_reference", "first_observation_of_utc_day"),
    )
    risk.update({
        "utc_date": timestamp.strftime("%Y-%m-%d"),
        # Drawdown controls restart from the post-rebalance equity.
        "starting_nav": str(nav),
        "starting_timestamp_ms": int(timestamp_ms),
        "starting_reference": reference,
        "peak_nav": str(nav),
        "peak_timestamp_ms": int(timestamp_ms),
        # Daily-return reporting continues to reference the UTC-day open.
        "calendar_day_starting_nav": str(existing_calendar_nav),
        "calendar_day_starting_reference": existing_calendar_reference,
        "stop_triggered": False,
        "stop_triggered_at_ms": None,
        "stop_reason": None,
        "flattened": False,
        "flatten_attempts": 0,
        "risk_window_reset_at_ms": int(timestamp_ms),
        "risk_window_reset_reason": reference,
    })
    state["daily_risk"] = risk
    return dict(risk)

def lifetime_max_drawdown(state: dict[str, Any]) -> Decimal | None:
    """Peak-to-trough NAV drawdown over all stored portfolio snapshots."""
    points = state.get("equity_curve") or state.get("daily_equity", [])
    if not points:
        return None
    peak: Decimal | None = None
    maximum = ZERO
    for row in points:
        nav = _decimal(row["nav"], "equity-curve NAV")
        if peak is None or nav > peak:
            peak = nav
        if peak is not None and peak > ZERO:
            drawdown = nav / peak - ONE
            if drawdown < maximum:
                maximum = drawdown
    return maximum

def performance_summary(state: dict[str, Any]) -> dict[str, Any]:
    curve = state.get("daily_equity", [])
    points = [
        EquityPoint(
            timestamp_ms=int(row["timestamp_ms"]),
            nav=_decimal(row["nav"], "daily NAV"),
        )
        for row in curve
    ]
    if len(points) < 2:
        return {
            "status": "INSUFFICIENT_DAILY_EQUITY_HISTORY",
            "observations": len(points),
            "metrics": {},
        }
    try:
        metrics = compute_performance_metrics(
            points,
            [_decimal(value, "closed trade net PnL")
             for value in state.get("closed_trade_net_pnls", [])],
        )
    except ValueError as exc:
        return {
            "status": "METRICS_UNAVAILABLE",
            "observations": len(points),
            "reason": str(exc),
            "metrics": {},
        }
    return {
        "status": "AVAILABLE_WITH_SAMPLE_GATES",
        "observations": len(points),
        "metrics": {
            name: {
                "value": metric.value,
                "reason": metric.reason,
                "observations": metric.observations,
            }
            for name, metric in metrics.items()
        },
    }


def account_report(
    state: dict[str, Any],
    ticker_by_instrument_id: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    nav = portfolio_nav(state, ticker_by_instrument_id)
    long_notional = ZERO
    short_notional = ZERO
    long_margin = ZERO
    short_margin = ZERO
    for position in state.get("positions", []):
        mark = _decimal(
            ticker_by_instrument_id.get(int(position["instrument_id"]), {}).get(
                "mark_price", position["last_mark_price"]
            ),
            "position mark",
        )
        notional = _decimal(position["quantity"], "quantity") * mark
        leverage = _decimal(position.get("leverage", "1"), "position leverage")
        if leverage <= ZERO:
            raise ValueError("Position leverage must be positive")
        margin = notional / leverage
        if position["side"] == "long":
            long_notional += notional
            long_margin += margin
        else:
            short_notional += notional
            short_margin += margin
    exposure = portfolio_exposure(nav, long_notional, short_notional)
    return {
        "currency": "USDT",
        "initial_capital": state["initial_capital"],
        "cash": state["cash"],
        "nav": str(nav),
        "realized_gross_pnl": state["realized_gross_pnl"],
        "unrealized_pnl": str(nav - _decimal(state["cash"], "cash")),
        "fees_paid": state["fees_paid"],
        "confirmed_funding": state["confirmed_funding"],
        "gross_exposure": str(exposure["gross_exposure"]),
        "net_exposure": str(exposure["net_exposure"]),
        "long_notional_usdt": str(long_notional),
        "short_notional_usdt": str(short_notional),
        "long_margin_used_usdt": str(long_margin),
        "short_margin_used_usdt": str(short_margin),
        "total_margin_used_usdt": str(long_margin + short_margin),
        "effective_gross_leverage": str(exposure["gross_exposure"]),
        "roi_pct": str(
            (nav / _decimal(state["initial_capital"], "initial capital") - ONE)
            * Decimal("100")
        ),
        "lifetime_max_drawdown_pct": (
            None if lifetime_max_drawdown(state) is None
            else str(lifetime_max_drawdown(state) * Decimal("100"))
        ),
        "daily_risk": dict(state.get("daily_risk", {})),
        "equity_curve": list(state.get("equity_curve", [])),
        "dollar_neutrality_imbalance": str(
            exposure["dollar_neutrality_imbalance"]
        ),
        "position_count": len(state.get("positions", [])),
        "positions": state.get("positions", []),
        "closed_trade_count": len(state.get("closed_trade_net_pnls", [])),
        "partial_realized_net_pnl": str(sum(
            (_decimal(value, "partial realized net PnL")
             for value in state.get("partial_realized_net_pnls", [])),
            ZERO,
        )),
        "partial_realized_event_count": len(
            state.get("partial_realized_net_pnls", [])
        ),
        "daily_equity_curve": list(state.get("daily_equity", [])),
        "performance": performance_summary(state),
    }
