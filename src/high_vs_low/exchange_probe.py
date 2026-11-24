"""Pure eligibility parsing for the ordered public perpetual-exchange qualification.

This module deliberately separates public technical metadata from jurisdiction/account
eligibility. It never sends authenticated requests or orders.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

PRIMARY_EXCHANGE_ORDER = ("htx", "mexc", "binance", "weex", "bitget")
FALLBACK_EXCHANGE_ORDER = ("okx", "kraken")


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def classify_asset_class(*values: Any) -> str:
    """Return an explicit provider category; do not guess from ticker suffixes."""
    text = " ".join(_text(value) for value in values if value is not None)
    explicit = (
        ("crypto", (
            "crypto", "digital asset", "digital_asset", "trade-zone-mainly",
            "layer1", "layer 1", "layer2", "layer 2", "pow", "proof of work",
            "defi", "meme", "nft", "web3", "gaming", "depin", "infrastructure",
            "0fees",
        )),
        ("equity", ("equity", "stock", "share", "etf")),
        ("index", ("index", "indices")),
        ("commodity", ("commodity", "commodities", "energy", "oil", "gas")),
        ("precious_metal", ("precious metal", "gold", "silver", "copper")),
        ("fx", ("forex", "foreign exchange", "fx")),
    )
    for category, markers in explicit:
        if any(marker in text for marker in markers):
            return category
    return "unclassified"


def htx_cross_perpetual(row: Mapping[str, Any]) -> bool:
    """HTX swap record: cross/all margin, active swap and no delivery."""
    return (
        _text(row.get("contract_type")) == "swap"
        and _text(row.get("business_type")) == "swap"
        and _text(row.get("support_margin_mode")) in {"cross", "all"}
        and int(row.get("contract_status", -1)) == 1
        and not str(row.get("delivery_time") or "").strip()
    )


def mexc_cross_perpetual(row: Mapping[str, Any]) -> bool:
    """MEXC: futureType=1 perpetual; positionOpenType 2/3 includes cross."""
    try:
        return (
            int(row.get("futureType", 0)) == 1
            and int(row.get("positionOpenType", 0)) in {2, 3}
            and int(row.get("state", 0)) == 0
            and row.get("apiAllowed") is not False
        )
    except (TypeError, ValueError):
        return False


def binance_perpetual(row: Mapping[str, Any]) -> bool:
    """USD-M perpetuals; cross mode availability is documented separately."""
    return (
        _text(row.get("status")) == "trading"
        and _text(row.get("contractType")) == "perpetual"
        and _text(row.get("quoteAsset")) in {"usdt", "usdc"}
        and bool(row.get("symbol"))
    )


def weex_perpetual(row: Mapping[str, Any], api_symbols: set[str]) -> bool:
    """WEEX V3 PERPETUAL or TRADIFI_PERPETUAL exposed for API trading."""
    return (
        str(row.get("symbol") or "") in api_symbols
        and _text(row.get("contractType")) in {"perpetual", "tradifi_perpetual"}
        and bool(row.get("marginAsset"))
        and bool(row.get("baseAsset"))
        and bool(row.get("quoteAsset"))
    )


def bitget_cross_perpetual(row: Mapping[str, Any]) -> bool:
    """Bitget USDT futures contract; status/margin mode is product-type scoped."""
    status = _text(row.get("symbolStatus"))
    return (
        bool(row.get("symbol"))
        and _text(row.get("quoteCoin")) == "usdt"
        and status in {"normal", "listed", "trading"}
        and bool(row.get("baseCoin"))
    )


def missing_fields(row: Mapping[str, Any], required: Sequence[str]) -> list[str]:
    """Report absent/empty contract fields without coercing unknown precision."""
    return [
        field for field in required
        if row.get(field) is None or str(row.get(field)).strip() == ""
    ]


def group_asset_classes(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Count instruments by explicit asset-class metadata, preserving unknowns."""
    counts: dict[str, int] = {}
    for row in rows:
        category = classify_asset_class(
            row.get("underlyingType"),
            row.get("underlyingSubType"),
            row.get("category"),
            row.get("conceptPlate"),
            row.get("assetClass"),
            row.get("displayNameEn"),
            row.get("displaySymbol"),
        )
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))
