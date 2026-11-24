"""Compact active MEXC paper status and visual-only inverse-volatility alternative path."""
from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .event_journal import order_event_batches, verify_event_batch
from .mexc_portfolio import performance_summary

ACCOUNT_ID = "mexc-usdt-forward-high-low-100usdt-v1"
STRATEGY_ID = "high-low-ath-atl-cross-sectional-v1-100usdt"
INITIAL_CAPITAL = Decimal("100")
DAY_MS = 86_400_000
VOLATILITY_LOOKBACK = 30
ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")


def _dec(value: object, default: str = "0") -> Decimal:
    try:
        parsed = Decimal(str(default if value is None else value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return parsed if parsed.is_finite() else Decimal(default)


def _utc(stamp: object) -> str:
    try:
        value = int(stamp)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    return datetime.fromtimestamp(value / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _load_events(state_dir: Path) -> list[dict[str, Any]]:
    paths = sorted((state_dir / "events").glob("**/*.json"))
    batches: list[dict[str, Any]] = []
    for path in paths:
        try:
            batch = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read immutable event batch {path}: {exc}") from exc
        if not isinstance(batch, dict) or not verify_event_batch(batch):
            raise ValueError(f"Invalid immutable event batch: {path}")
        batches.append(batch)
    return [event for batch in order_event_batches(batches) for event in batch["events"]]


def _stddev(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        return ZERO
    mean = sum(values, ZERO) / Decimal(len(values))
    variance = sum(((value - mean) ** 2 for value in values), ZERO)
    return (variance / Decimal(len(values) - 1)).sqrt()


def _pair_returns(
    long_id: int,
    short_id: int,
    history: dict[int, list[dict[str, Any]]],
    cutoff_ms: int,
) -> list[tuple[Decimal, Decimal]]:
    def closes(instrument_id: int) -> dict[int, Decimal]:
        output: dict[int, Decimal] = {}
        for row in history.get(instrument_id, []):
            stamp = int(row.get("timestamp_ms", 0))
            close = _dec(row.get("close"))
            if 0 < stamp <= cutoff_ms and close > ZERO:
                output[stamp] = close
        return output

    long_closes = closes(long_id)
    short_closes = closes(short_id)
    common = sorted(set(long_closes) & set(short_closes))[-(VOLATILITY_LOOKBACK + 1):]
    if len(common) < VOLATILITY_LOOKBACK + 1:
        return []
    return [
        (
            long_closes[current] / long_closes[previous] - ONE,
            short_closes[current] / short_closes[previous] - ONE,
        )
        for previous, current in zip(common, common[1:], strict=False)
    ][-VOLATILITY_LOOKBACK:]


def _risk_profile(
    long_id: int,
    short_id: int,
    history: dict[int, list[dict[str, Any]]],
    cutoff_ms: int,
) -> dict[str, Decimal | str | int]:
    returns = _pair_returns(long_id, short_id, history, cutoff_ms)
    if len(returns) < VOLATILITY_LOOKBACK:
        return {
            "long_weight": Decimal("0.5"),
            "short_weight": Decimal("0.5"),
            "leverage_multiplier": ONE,
            "equal_exposure_volatility": ZERO,
            "alternative_base_volatility": ZERO,
            "estimated_volatility_reduction": ZERO,
            "observations": len(returns),
            "status": "INSUFFICIENT_30_DAY_HISTORY_EQUAL_WEIGHTS_FALLBACK",
        }

    long_returns = [row[0] for row in returns]
    short_returns = [row[1] for row in returns]
    long_vol = _stddev(long_returns)
    short_vol = _stddev(short_returns)
    if long_vol <= ZERO or short_vol <= ZERO:
        long_weight = short_weight = Decimal("0.5")
    else:
        long_weight = short_vol / (long_vol + short_vol)
        short_weight = ONE - long_weight

    equal_returns = [long_return - short_return for long_return, short_return in returns]
    alternative_returns = [
        TWO * (long_weight * long_return - short_weight * short_return)
        for long_return, short_return in returns
    ]
    equal_vol = _stddev(equal_returns)
    alternative_vol = _stddev(alternative_returns)
    multiplier = (
        max(ONE, equal_vol / alternative_vol)
        if equal_vol > ZERO and alternative_vol > ZERO
        else ONE
    )
    reduction = (
        max(ZERO, ONE - alternative_vol / equal_vol)
        if equal_vol > ZERO
        else ZERO
    )
    return {
        "long_weight": long_weight,
        "short_weight": short_weight,
        "leverage_multiplier": multiplier,
        "equal_exposure_volatility": equal_vol,
        "alternative_base_volatility": alternative_vol,
        "estimated_volatility_reduction": reduction,
        "observations": len(returns),
        "status": "RISK_MATCHED_INVERSE_VOLATILITY",
    }


def _close_actions(details: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for field in ("portfolio_refresh_close_actions", "stop_exits", "emergency_exits"):
        value = details.get(field, [])
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
    retry = details.get("entry_retry")
    if isinstance(retry, dict) and isinstance(retry.get("fills"), list):
        rows.extend(item for item in retry["fills"] if isinstance(item, dict))
    if not rows and isinstance(details.get("fills"), list):
        rows.extend(item for item in details["fills"] if isinstance(item, dict))
    return {
        int(row["instrument_id"]): row for row in rows
        if row.get("instrument_id") is not None and row.get("action") == "CLOSED"
    }


def _shadow_nav(cash: Decimal, held: dict[tuple[int, str], dict[str, Any]]) -> Decimal:
    unrealized = ZERO
    for position in held.values():
        entry = _dec(position["entry_price"])
        mark = _dec(position["last_mark_price"], str(entry))
        quantity = _dec(position["quantity"])
        unrealized += (
            (mark - entry) * quantity
            if position["side"] == "long"
            else (entry - mark) * quantity
        )
    return cash + unrealized


def _build_alternative_curve(
    events: list[dict[str, Any]],
    history: dict[int, list[dict[str, Any]]],
    initial_capital: Decimal,
    generated_at_ms: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    cash = initial_capital
    held: dict[tuple[int, str], dict[str, Any]] = {}
    points: list[dict[str, Any]] = []
    latest_profile: dict[str, Any] = {
        "status": "AWAITING_FIRST_PAIR",
        "long_weight": Decimal("0.5"),
        "short_weight": Decimal("0.5"),
        "leverage_multiplier": ONE,
        "estimated_volatility_reduction": ZERO,
        "observations": 0,
    }
    warnings: list[str] = []

    def append_point(stamp: int) -> None:
        row = {"timestamp_ms": int(stamp), "nav": str(_shadow_nav(cash, held))}
        if points and int(points[-1]["timestamp_ms"]) == int(stamp):
            points[-1] = row
        elif not points or int(points[-1]["timestamp_ms"]) < int(stamp):
            points.append(row)

    for event in events:
        payload = event.get("payload", {})
        state = payload.get("portfolio_state") if isinstance(payload, dict) else None
        if not isinstance(state, dict):
            continue
        details = payload.get("details", {})
        details = details if isinstance(details, dict) else {}
        stamp = int(event.get("occurred_at_ms", 0))
        current_positions = {
            (int(row["instrument_id"]), str(row["side"])): row
            for row in state.get("positions", [])
            if isinstance(row, dict) and row.get("instrument_id") is not None
        }
        closes = _close_actions(details)
        if not points:
            append_point(max(1, stamp - 1))

        # Apply funding before closing any shadow lots at this event.
        funding_rows = details.get("funding_events", [])
        if isinstance(funding_rows, list):
            for item in funding_rows:
                if not isinstance(item, dict) or item.get("instrument_id") is None:
                    continue
                iid = int(item["instrument_id"])
                for key, shadow in held.items():
                    if key[0] == iid:
                        main_quantity = _dec(shadow.get("main_quantity"))
                        if main_quantity > ZERO:
                            cash += (
                                _dec(item.get("cashflow_usdt"))
                                * _dec(shadow.get("quantity"))
                                / main_quantity
                            )

        # Preserve existing lots between primary snapshots; close/reopen them
        # only when the primary account changes its entry or quantity.
        for key, shadow in list(held.items()):
            current = current_positions.get(key)
            same_entry = (
                current is not None
                and int(current.get("entry_timestamp_ms", 0))
                == int(shadow.get("entry_timestamp_ms", 0))
            )
            same_quantity = (
                same_entry and current is not None
                and _dec(current.get("quantity")) == _dec(shadow.get("main_quantity"))
            )
            if same_quantity and current is not None:
                shadow["last_mark_price"] = str(
                    _dec(current.get("last_mark_price"), str(shadow["last_mark_price"]))
                )
                continue

            action = closes.get(key[0], {})
            exit_price = _dec(
                action.get("exit_price", action.get("fill_price")),
                str(shadow["last_mark_price"]),
            )
            entry_price = _dec(shadow["entry_price"])
            quantity = _dec(shadow["quantity"])
            gross = (
                (exit_price - entry_price) * quantity
                if shadow["side"] == "long"
                else (entry_price - exit_price) * quantity
            )
            main_quantity = _dec(
                action.get("quantity"), str(shadow.get("main_quantity", "0"))
            )
            main_fee = _dec(action.get("exit_fee", action.get("fee_usdt")))
            exit_fee = main_fee * quantity / main_quantity if main_quantity > ZERO else ZERO
            cash += gross - exit_fee
            if not action:
                warnings.append(
                    f"Missing explicit close fill for {shadow['symbol']}; last observed mark used"
                )
            del held[key]

        new_pair = (
            len(current_positions) == 2
            and (
                not held
                or any(
                    key not in held
                    or int(current.get("entry_timestamp_ms", 0))
                    != int(held[key].get("entry_timestamp_ms", 0))
                    or _dec(current.get("quantity")) != _dec(held[key].get("main_quantity"))
                    for key, current in current_positions.items()
                )
            )
        )
        if new_pair:
            long_items = [row for row in current_positions.values() if row.get("side") == "long"]
            short_items = [row for row in current_positions.values() if row.get("side") == "short"]
            if len(long_items) == 1 and len(short_items) == 1:
                cutoff = int(
                    state.get("last_processed_daily_bar_ms")
                    or details.get("daily_bar_timestamp_ms")
                    or stamp
                )
                profile = _risk_profile(
                    int(long_items[0]["instrument_id"]),
                    int(short_items[0]["instrument_id"]),
                    history,
                    cutoff,
                )
                latest_profile = dict(profile)
                nav_before_open = max(ZERO, cash)
                for position in (long_items[0], short_items[0]):
                    side = str(position["side"])
                    weight = _dec(profile["long_weight" if side == "long" else "short_weight"])
                    entry = _dec(position.get("entry_price"))
                    main_quantity = _dec(position.get("quantity"))
                    if entry <= ZERO or main_quantity <= ZERO:
                        warnings.append(
                            f"Invalid primary entry for "
                            f"{position.get('symbol', 'unknown')}"
                        )
                        continue
                    notional = (
                        nav_before_open * TWO
                        * _dec(profile["leverage_multiplier"], "1") * weight
                    )
                    quantity = notional / entry
                    entry_fee = _dec(position.get("entry_fee")) * quantity / main_quantity
                    cash -= entry_fee
                    key = (int(position["instrument_id"]), side)
                    held[key] = {
                        "instrument_id": key[0],
                        "symbol": str(position.get("symbol", "")),
                        "side": side,
                        "quantity": str(quantity),
                        "main_quantity": str(main_quantity),
                        "entry_price": str(entry),
                        "last_mark_price": str(
                            _dec(position.get("last_mark_price"), str(entry))
                        ),
                        "entry_timestamp_ms": int(position.get("entry_timestamp_ms", stamp)),
                    }
            else:
                warnings.append("Alternative path skipped a malformed non-two-sided pair")
        append_point(stamp)

    if not points:
        points.append({"timestamp_ms": generated_at_ms, "nav": str(initial_capital)})
    return points, latest_profile, sorted(set(warnings))


def _equity_svg(
    points: list[dict[str, Any]],
    initial: Decimal,
    title: str,
    description: str,
    stroke: str,
) -> str:
    width, height = 1200, 480
    left, right, top, bottom = 110, 55, 92, 92
    plot_right, plot_bottom = width - right, height - bottom
    plot_width, plot_height = plot_right - left, plot_bottom - top
    values = [_dec(row.get("nav")) for row in points]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="chart-title chart-desc">',
        f'<title id="chart-title">{html.escape(title)}</title>',
        f'<desc id="chart-desc">{html.escape(description)}</desc>',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<g font-family="Arial, sans-serif" fill="#222">',
        f'<text x="{width / 2}" y="34" font-size="25" font-weight="bold" '
        f'text-anchor="middle">{html.escape(title)}</text>',
        f'<text x="{width / 2}" y="61" font-size="16" fill="#666" '
        f'text-anchor="middle">{html.escape(description)}</text>',
    ]
    if not values:
        parts.append(
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            'font-size="20" fill="#666">Awaiting first paper equity observation</text>'
        )
        return "".join(parts + ["</g></svg>"])

    reference = initial if initial > ZERO else values[0]
    low = min(values + [reference])
    high = max(values + [reference])
    pad = max((high - low) * Decimal("0.12"), abs(reference) * Decimal("0.002"), Decimal("0.01"))
    low -= pad
    high += pad
    if high <= low:
        high = low + ONE
    first, last = int(points[0]["timestamp_ms"]), int(points[-1]["timestamp_ms"])
    elapsed = max(0, last - first)

    def x_pos(stamp: int) -> float:
        return left + plot_width / 2 if elapsed == 0 else (
            left + (stamp - first) / elapsed * plot_width
        )

    def y_pos(value: Decimal) -> float:
        return plot_bottom - float((value - low) / (high - low)) * plot_height

    for index in range(5):
        fraction = index / 4
        y = top + fraction * plot_height
        tick = high - (high - low) * Decimal(str(fraction))
        parts.extend([
            f'<line x1="{left}" y1="{y:.2f}" x2="{plot_right}" y2="{y:.2f}" '
            'stroke="#e5e7eb" stroke-width="1"/>',
            f'<text x="{left - 12}" y="{y + 5:.2f}" text-anchor="end" '
            f'font-size="15" fill="#555">{html.escape(format(tick, ",.3f"))}</text>',
        ])
    parts.extend([
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{plot_bottom}" stroke="#666"/>',
        (
            f'<line x1="{left}" y1="{plot_bottom}" x2="{plot_right}" '
            f'y2="{plot_bottom}" stroke="#666"/>'
        ),
    ])
    reference_y = y_pos(reference)
    reference_label_y = min(plot_bottom - 10, max(top + 18, reference_y - 9))
    parts.extend([
        f'<line x1="{left}" y1="{reference_y:.2f}" x2="{plot_right}" y2="{reference_y:.2f}" '
        'stroke="#a66b12" stroke-width="1.6" stroke-dasharray="7 5"/>',
        f'<text x="{plot_right - 8}" y="{reference_label_y:.2f}" text-anchor="end" '
        'font-size="14" fill="#8a560e">Initial capital: '
        f'{html.escape(format(reference, ",.2f"))} USDT</text>',
    ])

    plotted = list(zip(points, values, strict=True))
    if len(plotted) > 1200:
        step = max(1, len(plotted) // 1100)
        keep = {0, len(plotted) - 1, *range(0, len(plotted), step)}
        plotted = [row for index, row in enumerate(plotted) if index in keep]
    coords = [(x_pos(int(row["timestamp_ms"])), y_pos(value)) for row, value in plotted]
    polyline = " ".join(f"{x:.2f},{y:.2f}" for x, y in coords)
    parts.extend([
        f'<polygon points="{polyline} {coords[-1][0]:.2f},{plot_bottom} '
        f'{coords[0][0]:.2f},{plot_bottom}" fill="{stroke}" opacity="0.10"/>',
        f'<polyline points="{polyline}" fill="none" stroke="{stroke}" stroke-width="3.2" '
        'stroke-linejoin="round" stroke-linecap="round"/>',
        f'<circle cx="{coords[0][0]:.2f}" cy="{coords[0][1]:.2f}" r="4.5" fill="{stroke}"/>',
        f'<circle cx="{coords[-1][0]:.2f}" cy="{coords[-1][1]:.2f}" r="4.5" fill="{stroke}"/>',
        f'<text x="{left}" y="84" font-size="16" fill="#333">Start: {values[0]:,.4f} USDT</text>',
        f'<text x="{plot_right}" y="84" text-anchor="end" font-size="16" '
        f'fill="#333">Latest: {values[-1]:,.4f} USDT</text>',
    ])

    # At most three labels; the midpoint is omitted if it crowds either endpoint.
    label_indices = [0] if len(points) == 1 else [0, len(points) - 1]
    if len(points) > 2 and elapsed > 0:
        middle = len(points) // 2
        middle_x = x_pos(int(points[middle]["timestamp_ms"]))
        if middle_x - left >= 210 and plot_right - middle_x >= 210:
            label_indices.insert(1, middle)
    for slot, index in enumerate(label_indices):
        dt = datetime.fromtimestamp(int(points[index]["timestamp_ms"]) / 1000, tz=UTC)
        x = x_pos(int(points[index]["timestamp_ms"]))
        if len(label_indices) == 1:
            x, anchor = left + plot_width / 2, "middle"
        elif slot == 0:
            x, anchor = left + 2, "start"
        elif slot == len(label_indices) - 1:
            x, anchor = plot_right - 2, "end"
        else:
            anchor = "middle"
        parts.extend([
            f'<text x="{x:.2f}" y="{height - 48}" text-anchor="{anchor}" '
            f'font-size="16" font-weight="bold" fill="#444">{dt:%Y-%m-%d}</text>',
            f'<text x="{x:.2f}" y="{height - 25}" text-anchor="{anchor}" '
            f'font-size="14" fill="#555">{dt:%H:%M:%S} UTC</text>',
        ])
    parts.extend([
        f'<text x="{plot_right}" y="{height - 5}" text-anchor="end" '
        f'font-size="13" fill="#777">{len(points):,} observations · paper-only model</text>',
        "</g></svg>",
    ])
    return "".join(parts)


def _nav_and_exposure(state: dict[str, Any]) -> dict[str, Decimal]:
    cash = _dec(state.get("cash"))
    unrealized = ZERO
    long_notional = ZERO
    short_notional = ZERO
    for position in state.get("positions", []):
        quantity = _dec(position.get("quantity"))
        entry = _dec(position.get("entry_price"))
        mark = _dec(position.get("last_mark_price"), str(entry))
        if position.get("side") == "long":
            unrealized += (mark - entry) * quantity
            long_notional += quantity * mark
        else:
            unrealized += (entry - mark) * quantity
            short_notional += quantity * mark
    nav = cash + unrealized
    return {
        "cash": cash, "unrealized": unrealized, "nav": nav,
        "long_notional": long_notional, "short_notional": short_notional,
        "gross_exposure": (long_notional + short_notional) / nav if nav > ZERO else ZERO,
        "net_exposure": abs(long_notional - short_notional) / nav if nav > ZERO else ZERO,
    }


def _metric(metrics: dict[str, Any], key: str, percent: bool = False) -> str:
    item = metrics.get(key, {})
    value = item.get("value") if isinstance(item, dict) else None
    if value is None:
        reason = item.get("reason") if isinstance(item, dict) else None
        return f"Unavailable — {reason or 'sample gate not met'}"
    number = Decimal(str(value))
    return f"{number * 100:+.2f}%" if percent else f"{number:.3f}"




def _drawdown_summary(
    events: list[dict[str, Any]],
    initial_capital: Decimal,
) -> tuple[Decimal | None, Decimal | None, list[dict[str, Any]]]:
    """Calculate lifetime and per-risk-session drawdown from persisted NAV marks."""
    points: dict[int, dict[str, Any]] = {}
    session_by_stamp: dict[int, int] = {}
    baselines: dict[int, Decimal] = {}
    for event in events:
        payload = event.get("payload", {})
        state = payload.get("portfolio_state", {}) if isinstance(payload, dict) else {}
        if not isinstance(state, dict):
            continue
        risk = state.get("daily_risk", {})
        risk = risk if isinstance(risk, dict) else {}
        try:
            session_start = int(risk.get("starting_timestamp_ms") or 0)
        except (TypeError, ValueError):
            session_start = 0
        if session_start > 0 and risk.get("starting_nav") is not None:
            baselines[session_start] = _dec(risk["starting_nav"])
        for row in state.get("equity_curve", []) or []:
            if not isinstance(row, dict) or row.get("timestamp_ms") is None:
                continue
            stamp = int(row["timestamp_ms"])
            points[stamp] = dict(row)
            if session_start > 0 and stamp >= session_start:
                session_by_stamp[stamp] = session_start
            elif stamp not in session_by_stamp:
                day_start = int(
                    datetime.fromtimestamp(stamp / 1000, tz=UTC)
                    .replace(hour=0, minute=0, second=0, microsecond=0)
                    .timestamp() * 1000
                )
                session_by_stamp[stamp] = day_start
                baselines.setdefault(day_start, _dec(row.get("nav")))

    if not points:
        return None, None, []

    peak = initial_capital
    lifetime_mdd = ZERO
    for stamp in sorted(points):
        nav = _dec(points[stamp].get("nav"))
        if nav > peak:
            peak = nav
        if peak > ZERO:
            lifetime_mdd = min(lifetime_mdd, (nav - peak) / peak)

    grouped: dict[int, list[dict[str, Any]]] = {}
    for stamp in sorted(points):
        grouped.setdefault(session_by_stamp.get(stamp, 0), []).append(points[stamp])

    sessions: list[dict[str, Any]] = []
    for start, rows in sorted(grouped.items()):
        baseline = baselines.get(start, _dec(rows[0].get("nav")))
        session_peak = baseline
        worst = ZERO
        for row in rows:
            nav = _dec(row.get("nav"))
            if nav > session_peak:
                session_peak = nav
            if session_peak > ZERO:
                worst = min(worst, (nav - session_peak) / session_peak)
        sessions.append({
            "start_timestamp_ms": start,
            "observations": len(rows),
            "starting_nav": baseline,
            "latest_nav": _dec(rows[-1].get("nav")),
            "max_drawdown": worst,
        })
    worst_session = min((row["max_drawdown"] for row in sessions), default=None)
    return lifetime_mdd, worst_session, sessions


def render_primary_trade_journal(
    state_dir: Path,
    *,
    generated_at_ms: int,
    workflow_metadata: dict[str, str] | None = None,
) -> str:
    """Render timestamped fills and funding for the High-Low primary account only."""
    events = [
        event for event in _load_events(Path(state_dir))
        if event.get("account_id") == ACCOUNT_ID
        and event.get("strategy_id") == STRATEGY_ID
    ]
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()

    def add_fill(
        item: dict[str, Any],
        event: dict[str, Any],
        fallback_action: str | None = None,
    ) -> None:
        action = str(item.get("action") or fallback_action or "").upper()
        if action not in {"OPENED", "INCREASED", "REDUCED", "CLOSED"}:
            return
        instrument_id = str(item.get("instrument_id", ""))
        symbol = str(item.get("symbol", instrument_id or "—"))
        quantity = item.get("quantity", item.get("filled_quantity", ""))
        price = item.get(
            "entry_price",
            item.get("exit_price", item.get("average_price", item.get("fill_price", ""))),
        )
        stamp = (
            item.get("timestamp_ms")
            or item.get("fill_timestamp_ms")
            or item.get("entry_timestamp_ms")
            or item.get("exit_timestamp_ms")
            or event.get("occurred_at_ms")
        )
        signature = (
            str(event.get("event_id", "")),
            action,
            instrument_id,
            str(quantity),
            str(price),
        )
        if signature in seen:
            return
        seen.add(signature)
        rows.append({
            "timestamp_ms": int(stamp or 0),
            "symbol": symbol,
            "side": str(item.get("side", "—")),
            "action": action,
            "quantity": str(quantity or "—"),
            "price": str(price or "—"),
            "fee": str(item.get(
                "fee",
                item.get("entry_fee", item.get("exit_fee", item.get("fee_usdt", "0"))),
            )),
            "realized_net_pnl": str(item.get(
                "realized_net_pnl",
                item.get("net_pnl", item.get("net_pnl_usdt", "—")),
            )),
            "funding": "—",
            "event_id": str(event.get("event_id", "—")),
        })

    for event in events:
        payload = event.get("payload", {})
        details = payload.get("details", {}) if isinstance(payload, dict) else {}
        details = details if isinstance(details, dict) else {}
        for field, fallback in (
            ("fills", None),
            ("portfolio_refresh_close_actions", "CLOSED"),
            ("portfolio_refresh_open_actions", "OPENED"),
            ("stop_exits", "CLOSED"),
            ("emergency_exits", "CLOSED"),
        ):
            values = details.get(field, [])
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, dict):
                        add_fill(item, event, fallback)

        retry = details.get("entry_retry")
        if isinstance(retry, dict) and isinstance(retry.get("fills"), list):
            for item in retry["fills"]:
                if isinstance(item, dict):
                    add_fill(item, event)

        funding_values = details.get("funding_events", [])
        if isinstance(funding_values, list):
            for item in funding_values:
                if not isinstance(item, dict):
                    continue
                stamp = (
                    item.get("timestamp_ms")
                    or item.get("settle_time_ms")
                    or event.get("occurred_at_ms")
                )
                rows.append({
                    "timestamp_ms": int(stamp or 0),
                    "symbol": str(item.get("symbol", item.get("instrument_id", "—"))),
                    "side": str(item.get("side", "—")),
                    "action": "FUNDING",
                    "quantity": str(item.get("quantity", "—")),
                    "price": str(item.get("mark_price", "—")),
                    "fee": "—",
                    "realized_net_pnl": "—",
                    "funding": str(item.get(
                        "cashflow_usdt",
                        item.get("cashflow", item.get("amount_usdt", "—")),
                    )),
                    "event_id": str(event.get("event_id", "—")),
                })

    rows.sort(key=lambda row: (row["timestamp_ms"], row["event_id"], row["action"]))
    metadata = workflow_metadata or {}
    run_id = metadata.get("run_id", "")
    run_url = metadata.get("run_url", "")
    run_link = f"[{run_id}]({run_url})" if run_id and run_url else (run_id or "Not yet run")
    lines = [
        "# Primary High-Low Paper Trade Journal — MEXC $100 USDT",
        "",
        (
            "**Primary equal-exposure account only.** This journal is reconstructed from "
            "the verified, append-only High-Low event chain. It contains no alternative-path "
            "trades or shadow executions."
        ),
        "",
        f"- Account: `{ACCOUNT_ID}`",
        f"- Strategy generation: `{STRATEGY_ID}`",
        "- Starting capital: **100.00000000 USDT**",
        f"- Generated: {_utc(generated_at_ms)}",
        f"- Latest workflow: {run_link}",
        f"- Recorded primary events: **{len(rows)}**",
        "",
        (
            "| Timestamp (UTC) | Symbol | Side | Action | Quantity | Price | "
            "Fee (USDT) | Realized net P&L (USDT) | Funding (USDT) | Event ID |"
        ),
        "|---|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    if rows:
        for row in rows:
            lines.append(
                f"| {_utc(row['timestamp_ms'])} | {row['symbol']} | {row['side']} "
                f"| {row['action']} | {row['quantity']} | {row['price']} "
                f"| {row['fee']} | {row['realized_net_pnl']} | {row['funding']} "
                f"| `{row['event_id']}` |"
            )
    else:
        lines.append(
            "| No primary fills or funding events recorded yet | — | — | — | — | "
            "— | — | — | — | — |"
        )
    lines.extend([
        "",
        (
            "This journal covers only the primary paper account. The inverse-volatility "
            "alternative path has no separate trade journal, positions or event stream."
        ),
        "",
    ])
    return "\n".join(lines)

def render_paper_status(
    state_dir: Path,
    history: dict[int, list[dict[str, Any]]],
    workflow_metadata: dict[str, str] | None = None,
    *,
    generated_at_ms: int,
    equal_svg_path: Path,
    alternative_svg_path: Path,
) -> str:
    events = [
        event for event in _load_events(Path(state_dir))
        if event.get("account_id") == ACCOUNT_ID
        and event.get("strategy_id") == STRATEGY_ID
    ]
    state_events = [
        event for event in events
        if isinstance(event.get("payload", {}).get("portfolio_state"), dict)
    ]
    metadata = workflow_metadata or {}
    repo = metadata.get("repository", "")
    server_url = metadata.get("server_url", "https://github.com")
    workflow_url = f"{server_url}/{repo}/actions/workflows/high-low-paper-cycle.yml" if repo else ""
    run_id, run_url = metadata.get("run_id", ""), metadata.get("run_url", "")
    run_link = f"[{run_id}]({run_url})" if run_id and run_url else (run_id or "Not yet run")

    if state_events:
        latest_event = state_events[-1]
        state = latest_event["payload"]["portfolio_state"]
        initial = _dec(state.get("initial_capital"), str(INITIAL_CAPITAL))
        if initial != INITIAL_CAPITAL:
            raise ValueError("Active paper account does not have the configured 100 USDT seed")
        equity_by_stamp: dict[int, dict[str, Any]] = {}
        for event in state_events:
            rows = event.get("payload", {}).get("portfolio_state", {}).get("equity_curve", [])
            for row in rows:
                if isinstance(row, dict) and row.get("timestamp_ms") is not None:
                    equity_by_stamp[int(row["timestamp_ms"])] = dict(row)
        equal_points = [
            {"timestamp_ms": stamp, "nav": str(row.get("nav", initial))}
            for stamp, row in sorted(equity_by_stamp.items())
        ] or [{"timestamp_ms": generated_at_ms, "nav": str(initial)}]
        alternative_points, profile, warnings = _build_alternative_curve(
            events, history, initial, generated_at_ms
        )
        snapshot = _nav_and_exposure(state)
        lifetime_mdd, worst_session_mdd, session_drawdowns = _drawdown_summary(
            events, initial
        )
        metrics = performance_summary(state).get("metrics", {})
        positions = list(state.get("positions", []))
        risk = state.get("daily_risk", {})
        peak = _dec(risk.get("peak_nav"), str(snapshot["nav"]))
        current_dd = snapshot["nav"] / peak - ONE if peak > ZERO else ZERO
        account_state = "INITIALIZED — FORWARD PAPER"
        latest_bar = state.get("last_processed_daily_bar_ms")
        latest_state_time = latest_event.get("occurred_at_ms")
        fees_paid = _dec(state.get("fees_paid"))
        confirmed_funding = _dec(state.get("confirmed_funding"))
    else:
        state = None
        initial = INITIAL_CAPITAL
        equal_points = [{"timestamp_ms": generated_at_ms, "nav": str(initial)}]
        alternative_points = list(equal_points)
        profile = {
            "status": "AWAITING_FIRST_PAIR",
            "long_weight": Decimal("0.5"),
            "short_weight": Decimal("0.5"),
            "leverage_multiplier": ONE,
            "estimated_volatility_reduction": ZERO,
            "observations": 0,
        }
        warnings = []
        snapshot = {
            "cash": initial, "unrealized": ZERO, "nav": initial,
            "long_notional": ZERO, "short_notional": ZERO,
            "gross_exposure": ZERO, "net_exposure": ZERO,
        }
        lifetime_mdd = None
        worst_session_mdd = None
        session_drawdowns = []
        metrics, positions, current_dd = {}, [], ZERO
        latest_bar, latest_state_time = None, None
        fees_paid, confirmed_funding = ZERO, ZERO
        account_state = "READY — WAITING FOR FIRST FINANCIAL PAPER EVENT"

    equal_svg_path.parent.mkdir(parents=True, exist_ok=True)
    alternative_svg_path.parent.mkdir(parents=True, exist_ok=True)
    equal_svg_path.write_text(
        _equity_svg(
            equal_points, initial, "High-Low Equal-Notional Strategy — Equity Curve",
            (
                "Primary paper strategy. Both legs target equal USDT notional; "
                "original fills and fees."
            ),
            "#2563eb",
        ),
        encoding="utf-8",
    )
    alternative_svg_path.write_text(
        _equity_svg(
            alternative_points, initial, "Alternative Path — Inverse-Volatility Sizing",
            (
                "Visual-only replay of the same selected pairs and rebalance times; "
                "no separate trade journal."
            ),
            "#7253a6",
        ),
        encoding="utf-8",
    )

    position_rows = [
        "| Symbol | Side | Quantity | Entry price | Mark | Notional (USDT) |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in positions:
        quantity = _dec(row.get("quantity"))
        mark = _dec(row.get("last_mark_price"), str(row.get("entry_price", "0")))
        position_rows.append(
            f"| {row.get('symbol', '—')} | {row.get('side', '—')} | {quantity} "
            f"| {_dec(row.get('entry_price'))} | {mark} | {quantity * mark:,.6f} |"
        )
    if not positions:
        position_rows.append("| No open positions yet | — | — | — | — | — |")

    long_weight = _dec(profile.get("long_weight"))
    short_weight = _dec(profile.get("short_weight"))
    multiplier = _dec(profile.get("leverage_multiplier"), "1")
    reduction = _dec(profile.get("estimated_volatility_reduction"))
    status = (
        f"{metadata.get('outcome', 'not supplied')} · {metadata.get('event_name', 'workflow run')}"
    )
    workflow_link = (
        f"[Open active workflow]({workflow_url})"
        if workflow_url
        else "Open the High-Low paper workflow in GitHub Actions"
    )
    current_bar = _utc(latest_bar)
    next_step = (
        "The next eligible cycle can process the latest genuine completed UTC daily candle; "
        "the new account has no consumed-candle watermark."
        if latest_bar is None and state is None else
        (
            "The next daily decision requires a newer completed UTC candle "
            "and a passing data-coverage gate."
        )
    )
    warning_text = (
        "\n".join(f"- {item}" for item in warnings)
        if warnings
        else "- No reconstruction warnings."
    )

    session_rows = [
        (
            f"| {_utc(row['start_timestamp_ms'])} | {row['starting_nav']:,.8f} "
            f"| {row['latest_nav']:,.8f} | {row['max_drawdown'] * 100:+.3f}% "
            f"| {row['observations']} |"
        )
        for row in session_drawdowns
    ] or ["| No risk sessions observed | — | — | — | 0 |"]
    lifetime_mdd_text = (
        "Unavailable — no account observations yet"
        if lifetime_mdd is None
        else f"{lifetime_mdd * 100:+.3f}%"
    )
    worst_session_mdd_text = (
        "Unavailable — no account observations yet"
        if worst_session_mdd is None
        else f"{worst_session_mdd * 100:+.3f}%"
    )
    lines = [
        "# ACTIVE PAPER STATUS — HIGH-LOW $100 USDT",
        "",
        "**🟢 ACTIVE · PAPER ONLY · NO LIVE ORDERS**",
        "",
        f"**Correct workflow:** {workflow_link}",
        "",
        f"- Account: `{ACCOUNT_ID}`",
        f"- Strategy generation: `{STRATEGY_ID}`",
        "- Starting capital: **100.00000000 USDT**",
        f"- Account state: **{account_state}**",
        f"- Last report build: {_utc(generated_at_ms)}",
        (
            f"- Latest workflow run: {run_link} · run "
            f"#{metadata.get('run_number', '—')}, "
            f"attempt {metadata.get('run_attempt', '—')} · {status}"
        ),
        f"- Latest processed daily candle: {current_bar}",
        f"- Latest account event: {_utc(latest_state_time)}",
        "- Primary execution journal: [PAPER_TRADE_JOURNAL.md](PAPER_TRADE_JOURNAL.md)",
        "",
        "## Current performance",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Initial capital | {initial:,.8f} USDT |",
        f"| **NAV** | **{snapshot['nav']:,.8f} USDT** |",
        f"| Total return | {(snapshot['nav'] / initial - ONE) * 100:+.3f}% |",
        f"| Cash | {snapshot['cash']:,.8f} USDT |",
        f"| Unrealized P&L | {snapshot['unrealized']:+,.8f} USDT |",
        f"| Fees paid | {fees_paid:,.8f} USDT |",
        f"| Confirmed funding | {confirmed_funding:+,.8f} USDT |",
        f"| Gross exposure | {snapshot['gross_exposure']:.3f}× NAV |",
        f"| Net exposure | {snapshot['net_exposure']:.3f}× NAV |",
        f"| Current drawdown from risk-window peak | {current_dd * 100:+.3f}% |",
        f"| Lifetime / cross-session maximum drawdown | {lifetime_mdd_text} |",
        f"| Worst risk-session maximum drawdown | {worst_session_mdd_text} |",
        f"| Sharpe ratio | {_metric(metrics, 'sharpe')} |",
        f"| Sortino ratio | {_metric(metrics, 'sortino')} |",
        (
            f"| Annualized daily volatility | "
            f"{_metric(metrics, 'annualized_daily_volatility', True)} |"
        ),
        "",
        "### Maximum drawdown by risk session",
        "",
        "| Session start (UTC) | Starting NAV | Latest NAV | Max drawdown | Marks |",
        "|---|---:|---:|---:|---:|",
        *session_rows,
        "",
        "## Open positions",
        "",
        *position_rows,
        "",
        "## Primary path — equal exposure",
        "",
        (
            "The live forward-paper strategy sizes the long and short legs "
            "independently toward equal USDT notional (approximately 1× NAV "
            "per leg before lot rounding)."
        ),
        "",
        "![Equal-exposure strategy equity curve](equal_exposure_equity_curve.svg)",
        "",
        "## Alternative path — volatility-weighted sizing",
        "",
        (
            "This chart is a **visual-only counterfactual**, not an executed account. "
            "It reuses the primary strategy’s exact selected symbols, sides and "
            "rebalance timestamps; only the notional split changes. No alternative "
            "trade journal is created."
        ),
        "",
        (
            "At each rebalance, the quieter leg receives the larger share using "
            "inverse 30-completed-day realized volatility. The leverage overlay is "
            "the ratio of equal-exposure pair volatility to the inverse-volatility "
            "pair’s base estimated volatility. It increases modeled gross exposure "
            "only when the alternative has lower estimated volatility. Estimates "
            "use only data available at that rebalance."
        ),
        "",
        (
            f"- Latest long / short allocation: **{long_weight * 100:.1f}% / "
            f"{short_weight * 100:.1f}%** of alternative gross notional."
        ),
        f"- Latest volatility-matching leverage multiplier: **{multiplier:.3f}×**.",
        f"- Estimated base volatility reduction before leverage: **{reduction * 100:+.2f}%**.",
        (
            f"- Volatility observations used: **{int(profile.get('observations', 0))}**; "
            f"status: `{profile.get('status', 'unavailable')}`."
        ),
        "",
        "![Inverse-volatility alternative equity curve](alternative_path_equity_curve.svg)",
        "",
        (
            "**Model limitations:** the counterfactual scales observed primary "
            "entry/exit fees and funding by shadow notional. It does not model "
            "additional nonlinear market impact, depth constraints, liquidation "
            "limits or exchange leverage caps for larger hypothetical exposure. "
            "It is not an executable leverage recommendation."
        ),
        "",
        "## Next workflow action",
        "",
        next_step,
        "",
        (
            "The daily-candle watermark is not advanced by this report. Only the "
            "paper workflow can consume a completed candle after full-history and "
            "execution gates pass. A blocked or incomplete run remains visible."
        ),
        "",
        "## Historical separation",
        "",
        (
            "The v3 $50 USDT generation and its preliminary snapshots are "
            "archived and excluded from this account’s NAV, returns, equity curves "
            "and metrics. The immutable event ledger remains intact."
        ),
        "",
        "## Alternative-path reconstruction notes",
        "",
        warning_text,
        "",
        (
            "This is the compact live status page. The former detailed v3 report "
            "is retained under `archive/2026-11-18/` for audit purposes only."
        ),
        "",
    ]
    return "\n".join(lines)
