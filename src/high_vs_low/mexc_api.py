"""Read-only public MEXC USDT perpetual market-data adapter.

The client intentionally has no authentication, private-account, position, or order
methods. Contract sizes and trading increments are preserved so the paper simulator
can convert contract quantities correctly.
"""
from __future__ import annotations

import importlib
import json
import math
import threading
import time
import urllib.error
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .exchange_probe import classify_asset_class

DEFAULT_BASE_URL = "https://api.mexc.com"
DEFAULT_WEBSOCKET_URL = "wss://contract.mexc.com/edge"
DEFAULT_MINIMUM_INTERVAL_SECONDS = 1.5
USER_AGENT = "high-low-ath-atl-paper-research/1.0"
DAY_SECONDS = 86_400
MAX_KLINE_ROWS = 2_000
MAX_FUNDING_PAGE_SIZE = 1_000

Transport = Callable[[str, Mapping[str, Any]], tuple[int, Any]]
WebSocketConnector = Callable[[str, float], Any]


class MEXCAPIError(RuntimeError):
    """Raised for invalid public responses or failed API requests."""


class MEXCForbiddenError(MEXCAPIError):
    """Raised when MEXC or an upstream edge denies a public API request."""


def _http_error_message(
    path: str,
    status: int,
    *,
    detail: str | None = None,
    headers: Mapping[str, Any] | None = None,
    body: str | None = None,
) -> str:
    """Expose safe endpoint diagnostics without including query strings or secrets."""
    parts = [f"MEXC returned HTTP {status} for {path}"]
    if headers is not None:
        server = headers.get("Server")
        content_type = headers.get("Content-Type")
        retry_after = headers.get("Retry-After")
        if server:
            parts.append(f"server={str(server)[:80]}")
        if content_type:
            parts.append(f"content_type={str(content_type)[:100]}")
        if retry_after:
            parts.append(f"retry_after={str(retry_after)[:40]}")
    if detail:
        parts.append(f"response={ ' '.join(str(detail).split())[:220] }")
    if body:
        parts.append(f"body={ ' '.join(str(body).split())[:220] }")
    return "; ".join(parts)


def _retry_after_seconds(value: object) -> float | None:
    """Parse Retry-After as seconds or an HTTP date."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - datetime.now(UTC)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _retry_delay_seconds(
    status: int,
    attempt: int,
    retry_after: float | None = None,
) -> float:
    """Use long rate-limit cooldowns and exponential backoff for transient failures."""
    base = 60.0 if status == 429 else 3.0
    return max(base * (2**attempt), retry_after or 0.0)


def _decimal(value: object, field: str, *, allow_zero: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MEXCAPIError(f"Invalid decimal field {field}") from exc
    if not result.is_finite() or result < 0 or (result == 0 and not allow_zero):
        raise MEXCAPIError(f"Non-positive or non-finite decimal field {field}")
    return result


def _signed_decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MEXCAPIError(f"Invalid decimal field {field}") from exc
    if not result.is_finite():
        raise MEXCAPIError(f"Non-finite decimal field {field}")
    return result


def _optional_decimal(value: object, field: str) -> Decimal:
    """Represent absent non-negative market fields as zero, not a whole-call failure."""
    if value is None or value == "":
        return Decimal("0")
    return _decimal(value, field, allow_zero=True)


def _optional_signed_decimal(value: object, field: str) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return _signed_decimal(value, field)


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise MEXCAPIError(f"Invalid integer field {field}")
    try:
        parsed = int(value)  # MEXC publishes integer identifiers and precision fields.
    except (TypeError, ValueError) as exc:
        raise MEXCAPIError(f"Invalid integer field {field}") from exc
    if parsed < minimum:
        raise MEXCAPIError(f"Out-of-range integer field {field}")
    return parsed


def _payload_rows(payload: Any, field: str = "data") -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise MEXCAPIError("MEXC response is not a JSON object")
    value = payload.get(field)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def _normalize_ticker_row(
    row: Mapping[str, Any],
    *,
    timestamp_fallback_ms: int | None = None,
) -> dict[str, Any]:
    """Normalize REST and all-market WebSocket ticker rows to one stable schema."""
    symbol = str(row.get("symbol", "")).strip()
    if not symbol:
        raise MEXCAPIError("MEXC ticker row has no symbol")
    bid = _optional_decimal(row.get("bid1"), f"{symbol}.bid1")
    ask = _optional_decimal(row.get("ask1"), f"{symbol}.ask1")
    last = _optional_decimal(row.get("lastPrice"), f"{symbol}.lastPrice")
    fair = _optional_decimal(row.get("fairPrice"), f"{symbol}.fairPrice")
    index = _optional_decimal(row.get("indexPrice"), f"{symbol}.indexPrice")
    usable_mark = fair if fair > 0 else last
    mid = (bid + ask) / Decimal("2") if bid > 0 and ask > bid else usable_mark
    # The all-market WS envelope timestamp is the exchange server clock.
    # Prefer it even if a ticker row also contains an older last-trade timestamp.
    timestamp = (
        timestamp_fallback_ms
        if timestamp_fallback_ms is not None
        else row.get("timestamp")
    )
    timestamp_ms = _integer(timestamp, f"{symbol}.timestamp", minimum=1)
    return {
        "symbol": symbol,
        "last_price": str(last),
        "mark_price": str(usable_mark),
        "index_price": str(index),
        "bid_price": str(bid),
        "ask_price": str(ask),
        "mid_price": str(mid),
        "funding_rate": str(_optional_signed_decimal(
            row.get("fundingRate"), f"{symbol}.fundingRate"
        )),
        "volume_24h_contracts": str(_optional_decimal(
            row.get("volume24"), f"{symbol}.volume24"
        )),
        "turnover_24h_quote": str(_optional_decimal(
            row.get("amount24"), f"{symbol}.amount24"
        )),
        "open_interest_contracts": str(_optional_decimal(
            row.get("holdVol"), f"{symbol}.holdVol"
        )),
        "change_24h": str(_optional_signed_decimal(
            row.get("riseFallRate"), f"{symbol}.riseFallRate"
        )),
        "timestamp_ms": timestamp_ms,
        "next_funding_timestamp_ms": (
            None if row.get("nextSettleTime") in (None, "", 0, "0")
            else _integer(row.get("nextSettleTime"), f"{symbol}.nextSettleTime")
        ),
    }


def classify_mexc_instrument(row: Mapping[str, Any]) -> str:
    """Classify where provider labels permit; unknown labels remain valid metadata."""
    category = classify_asset_class(
        row.get("underlyingType"),
        row.get("underlyingSubType"),
        row.get("category"),
        row.get("conceptPlate"),
        row.get("assetClass"),
        row.get("displayNameEn"),
        row.get("baseCoinName"),
    )
    return category if category != "unclassified" else "unclassified"


def is_active_cross_perpetual(row: Mapping[str, Any]) -> bool:
    """Check cross-margin capability and live linear-perpetual state per instrument."""
    try:
        return (
            _integer(row.get("futureType"), "futureType") == 1
            and _integer(row.get("positionOpenType"), "positionOpenType") in {2, 3}
            and _integer(row.get("state"), "state") == 0
            and row.get("apiAllowed") is not False
            and bool(str(row.get("symbol", "")).strip())
        )
    except MEXCAPIError:
        return False


def is_active_cross_usdt_perpetual(row: Mapping[str, Any]) -> bool:
    """Return True when the active cross-margin perpetual settles and quotes in USDT."""
    return (
        is_active_cross_perpetual(row)
        and str(row.get("quoteCoin", "")).upper() == "USDT"
        and str(row.get("settleCoin", "")).upper() == "USDT"
    )


def normalize_instrument(row: Mapping[str, Any]) -> dict[str, Any]:
    """Convert an active MEXC detail record to the exchange-neutral data contract."""
    instrument_id = _integer(row.get("id", row.get("contractId")), "id", minimum=1)
    symbol = str(row.get("symbol", "")).strip()
    if not symbol:
        raise MEXCAPIError("Instrument symbol is missing")
    contract_size = _decimal(row.get("contractSize"), "contractSize")
    price_tick = _decimal(row.get("priceUnit"), "priceUnit")
    quantity_step = _decimal(row.get("volUnit"), "volUnit")
    min_contracts = _decimal(row.get("minVol"), "minVol", allow_zero=True)
    max_contracts = _decimal(row.get("maxVol"), "maxVol")
    if quantity_step <= 0 or max_contracts < min_contracts:
        raise MEXCAPIError(f"Invalid contract size rules for {symbol}")
    taker_fee = _decimal(row.get("takerFeeRate"), "takerFeeRate", allow_zero=True)
    maker_fee = _decimal(row.get("makerFeeRate"), "makerFeeRate", allow_zero=True)
    create_time = row.get("createTime")
    create_time_ms = None if create_time in (None, "", 0, "0") else _integer(
        create_time, "createTime", minimum=1
    )
    opening_time = row.get("openingTime")
    opening_time_ms = None if opening_time in (None, "", 0, "0") else _integer(
        opening_time, "openingTime", minimum=1
    )
    category = classify_mexc_instrument(row)
    position_open_type = _integer(
        row.get("positionOpenType"), "positionOpenType"
    )
    return {
        "instrument_id": instrument_id,
        "symbol": symbol,
        "category": category,
        "raw_category_labels": row.get("conceptPlate", []),
        "base_asset": str(row.get("baseCoin", "")),
        "quote_asset": str(row.get("quoteCoin", "")),
        "settlement_asset": str(row.get("settleCoin", "")),
        "contract_size": str(contract_size),
        "price_tick": str(price_tick),
        "quantity_step_contracts": str(quantity_step),
        "min_contracts": str(min_contracts),
        "max_contracts": str(max_contracts),
        "min_quantity_base": (
            str(min_contracts * contract_size)
            if str(row.get("quoteCoin", "")).upper() == "USDT"
            and str(row.get("settleCoin", "")).upper() == "USDT"
            else None
        ),
        "max_quantity_base": (
            str(max_contracts * contract_size)
            if str(row.get("quoteCoin", "")).upper() == "USDT"
            and str(row.get("settleCoin", "")).upper() == "USDT"
            else None
        ),
        "usdt_account_eligible": (
            str(row.get("quoteCoin", "")).upper() == "USDT"
            and str(row.get("settleCoin", "")).upper() == "USDT"
        ),
        "taker_fee_rate": str(taker_fee),
        "maker_fee_rate": str(maker_fee),
        "max_leverage": _integer(row.get("maxLeverage"), "maxLeverage", minimum=1),
        "position_open_type": position_open_type,
        "isolated_only": position_open_type == 1,
        "cross_margin_available": position_open_type in {2, 3},
        "settlement": None,
        "state": _integer(row.get("state"), "state"),
        "api_allowed": row.get("apiAllowed") is not False,
        "create_time_ms": create_time_ms,
        "opening_time_ms": opening_time_ms,
        "close_only": False,
        "raw_display_name": row.get("displayNameEn"),
    }


class MEXCPublicClient:
    """Rate-limited, retrying MEXC contract-v1 public client."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_seconds: float = 12.0,
        minimum_interval_seconds: float = DEFAULT_MINIMUM_INTERVAL_SECONDS,
        retries: int = 3,
        transport: Transport | None = None,
        websocket_connector: WebSocketConnector | None = None,
    ) -> None:
        if base_url.rstrip("/") != DEFAULT_BASE_URL:
            raise ValueError("Only the current official MEXC Futures API host is allowed")
        if timeout_seconds <= 0 or minimum_interval_seconds < 0 or retries < 0:
            raise ValueError("Invalid timeout, rate interval or retry setting")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.minimum_interval_seconds = minimum_interval_seconds
        self.retries = retries
        self.transport = transport
        self.websocket_connector = websocket_connector
        self.websocket_url = DEFAULT_WEBSOCKET_URL
        self._rate_lock = threading.Lock()
        self._last_request_at = 0.0
        self._contract_details_cache: list[dict[str, Any]] | None = None

    def _throttle(self) -> None:
        with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_at
            delay = self.minimum_interval_seconds - elapsed
            if delay > 0:
                time.sleep(delay)
            self._last_request_at = time.monotonic()

    def _request(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not path.startswith("/api/v1/contract/") or path.startswith("//"):
            raise MEXCAPIError("Only public MEXC /api/v1/contract endpoints are permitted")
        query = {key: value for key, value in (params or {}).items() if value is not None}
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            retry_delay: float | None = None
            self._throttle()
            try:
                if self.transport is not None:
                    status, payload = self.transport(path, query)
                else:
                    url = self.base_url + path
                    if query:
                        url += "?" + urlencode(query)
                    request = Request(
                        url,
                        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                        method="GET",
                    )
                    with urlopen(request, timeout=self.timeout_seconds) as response:
                        status = int(response.status)
                        payload = json.loads(response.read(8_000_000).decode("utf-8"))

                if status == 429 or status >= 500:
                    last_error = MEXCAPIError(
                        f"Retryable MEXC HTTP status {status} for {path}"
                    )
                    retry_delay = _retry_delay_seconds(status, attempt)
                elif status != 200:
                    detail = None
                    if isinstance(payload, dict):
                        response_code = payload.get("code")
                        response_message = payload.get("message") or payload.get("msg")
                        if response_code is not None or response_message:
                            detail = (
                                f"code={response_code!r}; "
                                f"message={str(response_message or '')[:160]}"
                            )
                    message = _http_error_message(path, status, detail=detail)
                    if status == 403:
                        raise MEXCForbiddenError(message)
                    raise MEXCAPIError(message)
                elif not isinstance(payload, dict):
                    raise MEXCAPIError("MEXC response is not a JSON object")
                elif payload.get("success") is not True or str(payload.get("code", "0")) != "0":
                    response_message = (
                        f"MEXC API rejected request: code={payload.get('code')!r} "
                        f"message={str(payload.get('message', ''))[:200]}"
                    )
                    if str(payload.get("code")) == "510":
                        last_error = MEXCAPIError(response_message)
                        retry_delay = _retry_delay_seconds(429, attempt)
                    else:
                        raise MEXCAPIError(response_message)
                else:
                    return payload
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    try:
                        body = exc.read(512).decode("utf-8", errors="replace")
                    except OSError:
                        body = None
                    message = _http_error_message(
                        path,
                        exc.code,
                        headers=exc.headers,
                        body=body,
                    )
                    if exc.code == 403:
                        raise MEXCForbiddenError(message) from exc
                    raise MEXCAPIError(message) from exc
                last_error = exc
                retry_after_header = (
                    exc.headers.get("Retry-After") if exc.headers is not None else None
                )
                retry_delay = _retry_delay_seconds(
                    exc.code,
                    attempt,
                    _retry_after_seconds(retry_after_header),
                )
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
                retry_delay = _retry_delay_seconds(0, attempt)

            if attempt < self.retries:
                time.sleep(
                    retry_delay if retry_delay is not None
                    else _retry_delay_seconds(0, attempt)
                )
        raise MEXCAPIError(f"MEXC request failed after retries: {last_error}") from last_error

    def server_time_ms(self) -> int:
        payload = self._request("/api/v1/contract/ping")
        return _integer(payload.get("data"), "server time", minimum=1)

    def contract_details(self) -> list[dict[str, Any]]:
        payload = self._request("/api/v1/contract/detail")
        rows = _payload_rows(payload)
        if not rows:
            raise MEXCAPIError("MEXC contract detail returned no instruments")
        return rows

    def instruments(self, *, usdt_only: bool = False) -> list[dict[str, Any]]:
        """Return every active cross-margin linear perpetual, optionally USDT-only.

        The research universe uses all settlements. The single-currency USDT paper
        account uses the narrower USDT-only execution subset.
        """
        output: list[dict[str, Any]] = []
        for row in self.contract_details():
            if not is_active_cross_perpetual(row):
                continue
            normalized = normalize_instrument(row)
            if usdt_only and not normalized["usdt_account_eligible"]:
                continue
            output.append(normalized)
        if not output:
            scope = "USDT-settled " if usdt_only else ""
            raise MEXCAPIError(
                f"No active cross-margin {scope}perpetuals were discovered"
            )
        return sorted(output, key=lambda row: int(row["instrument_id"]))

    def ticker(self, symbol: str) -> dict[str, Any]:
        """Fetch one contract's current ticker from the documented REST endpoint."""
        normalized_symbol = str(symbol).strip()
        if (
            not normalized_symbol
            or "/" in normalized_symbol
            or "?" in normalized_symbol
            or "#" in normalized_symbol
        ):
            raise ValueError("Invalid MEXC ticker symbol")
        payload = self._request(
            "/api/v1/contract/ticker", {"symbol": normalized_symbol}
        )
        rows = _payload_rows(payload)
        if len(rows) != 1 or str(rows[0].get("symbol", "")).strip() != normalized_symbol:
            raise MEXCAPIError(
                f"MEXC single-contract ticker response did not match {normalized_symbol}"
            )
        return _normalize_ticker_row(rows[0])

    def tickers(self) -> list[dict[str, Any]]:
        """Subscribe once to MEXC's public all-contract ticker stream."""
        connector = self.websocket_connector
        if connector is None:
            try:
                websocket_module = importlib.import_module("websocket")
                connector = websocket_module.create_connection
            except (ImportError, AttributeError) as exc:
                raise MEXCAPIError(
                    "The websocket-client package is required for MEXC all-market tickers"
                ) from exc

        connection: Any | None = None
        try:
            connection = connector(self.websocket_url, self.timeout_seconds)
            connection.send(json.dumps({
                "method": "sub.tickers",
                "param": {},
                "gzip": False,
            }))
            # Ignore subscription acknowledgements and heartbeat frames. The first
            # push.tickers payload contains all current contracts in one message.
            for _ in range(20):
                raw_message = connection.recv()
                if isinstance(raw_message, bytes):
                    raw_message = raw_message.decode("utf-8")
                message = json.loads(raw_message)
                if not isinstance(message, dict):
                    continue
                channel = str(message.get("channel", ""))
                if channel == "rs.error":
                    raise MEXCAPIError(
                        "MEXC rejected all-market ticker subscription: "
                        f"{str(message.get('data', message))[:180]}"
                    )
                if channel != "push.tickers":
                    continue
                timestamp_ms = _integer(message.get("ts"), "ticker stream timestamp", minimum=1)
                rows = message.get("data")
                if not isinstance(rows, list) or not rows:
                    raise MEXCAPIError("MEXC all-market ticker push contained no rows")
                output = [
                    _normalize_ticker_row(row, timestamp_fallback_ms=timestamp_ms)
                    for row in rows
                    if isinstance(row, dict)
                ]
                if not output:
                    raise MEXCAPIError("MEXC all-market ticker push contained no valid rows")
                return output
            raise MEXCAPIError("MEXC all-market ticker stream sent no ticker push")
        except MEXCAPIError:
            raise
        except Exception as exc:
            raise MEXCAPIError(
                "MEXC all-market ticker WebSocket failed: "
                f"{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def orderbook(self, symbol: str, *, depth: int = 20) -> dict[str, Any]:
        if not symbol or depth < 1 or depth > 1_000:
            raise ValueError("Invalid symbol or depth")
        payload = self._request(f"/api/v1/contract/depth/{symbol}", {"limit": depth})
        rows = _payload_rows(payload)
        if not rows:
            raise MEXCAPIError(f"MEXC order book is empty for {symbol}")
        raw = rows[0]
        levels: dict[str, list[dict[str, str]]] = {}
        for side in ("bids", "asks"):
            raw_levels = raw.get(side)
            if not isinstance(raw_levels, list):
                raise MEXCAPIError(f"MEXC {side} depth is malformed for {symbol}")
            parsed = []
            for level in raw_levels:
                if not isinstance(level, list) or len(level) < 2:
                    continue
                price = _decimal(level[0], f"{symbol}.{side}.price")
                quantity = _decimal(level[1], f"{symbol}.{side}.quantity", allow_zero=True)
                if quantity > 0:
                    parsed.append({"price": str(price), "quantity_contracts": str(quantity)})
            levels[side] = parsed
        if not levels["bids"] or not levels["asks"]:
            raise MEXCAPIError(f"MEXC order book has an empty side for {symbol}")
        return {
            **levels,
            "timestamp_ms": _integer(raw.get("timestamp"), f"{symbol}.book_timestamp", minimum=1),
            "version": raw.get("version"),
        }

    def candles(self, symbol: str, *, start_timestamp_ms: int, end_timestamp_ms: int, interval: str = "Day1") -> list[dict[str, Any]]:
        """Fetch large history windows as explicit segments of at most 2,000 bars."""
        if not symbol or start_timestamp_ms <= 0 or end_timestamp_ms <= start_timestamp_ms:
            raise ValueError("Invalid candle symbol or time window")
        seconds_per_bar = {"Min1":60,"Min5":300,"Min15":900,"Min30":1800,"Min60":3600,
            "Hour4":14400,"Hour8":28800,"Day1":DAY_SECONDS,"Week1":604800,"Month1":2592000}.get(interval)
        if seconds_per_bar is None:
            raise ValueError("Unsupported MEXC candle interval")
        cursor = start_timestamp_ms // 1000
        end_seconds = end_timestamp_ms // 1000
        output: dict[int, dict[str, Any]] = {}
        while cursor < end_seconds:
            segment_end = min(end_seconds, cursor + seconds_per_bar * MAX_KLINE_ROWS)
            payload = self._request(f"/api/v1/contract/kline/{symbol}",
                {"interval":interval,"start":cursor,"end":segment_end})
            data = payload.get("data")
            if not isinstance(data, dict):
                raise MEXCAPIError(f"MEXC kline response malformed for {symbol}")
            arrays = {name:data.get(name) for name in ("time","open","high","low","close","vol","amount")}
            if any(not isinstance(value,list) for value in arrays.values()):
                raise MEXCAPIError(f"MEXC kline arrays incomplete for {symbol}")
            count=len(arrays["time"] or [])
            if any(len(value or []) != count for value in arrays.values()):
                raise MEXCAPIError(f"MEXC kline arrays have unequal lengths for {symbol}")
            for i in range(count):
                stamp_seconds=_integer(arrays["time"][i],"kline time",minimum=1)
                stamp_ms=stamp_seconds*1000
                if not start_timestamp_ms <= stamp_ms < end_timestamp_ms or not cursor <= stamp_seconds < segment_end:
                    continue
                output[stamp_ms]={"timestamp_ms":stamp_ms,
                    "open":str(_decimal(arrays["open"][i],"kline open")),
                    "high":str(_decimal(arrays["high"][i],"kline high")),
                    "low":str(_decimal(arrays["low"][i],"kline low")),
                    "close":str(_decimal(arrays["close"][i],"kline close")),
                    "volume_contracts":str(_decimal(arrays["vol"][i],"kline volume",allow_zero=True)),
                    "turnover_quote":str(_decimal(arrays["amount"][i],"kline turnover",allow_zero=True))}
            next_cursor=segment_end
            if count >= MAX_KLINE_ROWS:
                latest=max(_integer(value,"kline time",minimum=1) for value in arrays["time"])
                next_cursor=max(cursor+seconds_per_bar,latest+seconds_per_bar)
            if next_cursor <= cursor:
                raise MEXCAPIError(f"MEXC kline pagination stalled for {symbol}")
            cursor=next_cursor
        return [output[key] for key in sorted(output)]

    def funding_history(
        self,
        symbol: str,
        *,
        start_timestamp_ms: int,
        end_timestamp_ms: int,
        page_size: int = MAX_FUNDING_PAGE_SIZE,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        if not symbol or start_timestamp_ms < 0 or end_timestamp_ms <= start_timestamp_ms:
            raise ValueError("Invalid funding symbol or time window")
        if not 1 <= page_size <= MAX_FUNDING_PAGE_SIZE or max_pages < 1:
            raise ValueError("Invalid funding pagination settings")
        output: dict[int, dict[str, Any]] = {}
        for page_num in range(1, max_pages + 1):
            payload = self._request(
                "/api/v1/contract/funding_rate/history",
                {"symbol": symbol, "page_num": page_num, "page_size": page_size},
            )
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("resultList"), list):
                raise MEXCAPIError(f"MEXC funding history is malformed for {symbol}")
            records = [row for row in data["resultList"] if isinstance(row, dict)]
            if not records:
                break
            for row in records:
                stamp = _integer(row.get("settleTime"), "funding settleTime", minimum=1)
                if start_timestamp_ms <= stamp < end_timestamp_ms:
                    output[stamp] = {
                        "symbol": symbol,
                        "timestamp_ms": stamp,
                        "funding_rate": str(_signed_decimal(
                            row.get("fundingRate"), "fundingRate"
                        )),
                    }
            oldest = min(
                _integer(row.get("settleTime"), "funding settleTime", minimum=1)
                for row in records
            )
            if oldest < start_timestamp_ms or len(records) < page_size:
                break
            if page_num == max_pages:
                raise MEXCAPIError(f"MEXC funding pagination limit reached for {symbol}")
        return [output[key] for key in sorted(output)]
