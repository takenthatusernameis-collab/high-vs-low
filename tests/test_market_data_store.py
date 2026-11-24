from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime

import pytest

from high_vs_low.market_data_store import MarketDataStore, MarketDataStoreError


def candle(instrument_id: int, timestamp_ms: int, close: str = "10") -> dict[str, object]:
    return {
        "instrument_id": instrument_id,
        "symbol": f"ASSET{instrument_id}-PERP",
        "category": "equity",
        "timestamp_ms": timestamp_ms,
        "open": close,
        "high": "11",
        "low": "9",
        "close": close,
        "volume": "100",
        "trades": 10,
    }


def test_append_and_load_candles_from_daily_partitions(tmp_path) -> None:
    store = MarketDataStore(tmp_path)
    first = 1_700_006_400_000
    second = first + 86_400_000
    assert store.append_candles([candle(1, first), candle(2, first, "20")]) == 2
    assert store.append_candles([candle(1, second, "11")]) == 1
    loaded = store.load_all_candles()
    assert [row["close"] for row in loaded[1]] == ["10", "11"]
    assert loaded[2][0]["close"] == "20"
    assert len(list((tmp_path / "data" / "candles").glob("*.jsonl.gz"))) == 2


def test_hourly_snapshots_append_as_concatenated_gzip_members(tmp_path) -> None:
    store = MarketDataStore(tmp_path)
    timestamp = 1_700_006_400_000
    one = store.save_snapshot(timestamp, [{"instrument_id": 1, "last_price": "10"}])
    two = store.save_snapshot(timestamp + 60_000, [{"instrument_id": 1, "last_price": "11"}])
    assert one == two
    with gzip.open(one, "rt", encoding="utf-8") as source:
        rows = [json.loads(line) for line in source]
    assert [row["last_price"] for row in rows] == ["10", "11"]


def test_universe_sync_preserves_first_seen_and_requires_two_misses(tmp_path) -> None:
    store = MarketDataStore(tmp_path)
    row = {
        "instrument_id": 3,
        "category": "pre_ipo",
        "symbol": "NEW-PERP",
        "isolated_only": False,
        "close_only": False,
        "settlement": None,
    }
    counts = store.sync_universe([row], 1_700_000_000_000)
    assert counts == {"active": 1}
    store.manifest["instruments"]["3"]["last_candle_timestamp_ms"] = 123
    counts = store.sync_universe([], 1_700_000_100_000)
    assert store.manifest["instruments"]["3"]["status"] == "active"
    counts = store.sync_universe([], 1_700_000_200_000)
    assert store.manifest["instruments"]["3"]["status"] == "missing_from_feed"
    store.sync_universe([row], 1_700_000_300_000)
    assert store.manifest["instruments"]["3"]["status"] == "active"
    assert store.manifest["instruments"]["3"]["last_candle_timestamp_ms"] == 123


def test_close_only_and_settled_lifecycle_are_preserved_in_manifest(tmp_path) -> None:
    store = MarketDataStore(tmp_path)
    base = {
        "instrument_id": 1,
        "category": "commodity",
        "symbol": "GOLD-PERP",
        "isolated_only": False,
        "close_only": True,
        "settlement": None,
    }
    store.sync_universe([base], 1_700_000_000_000)
    assert store.manifest["instruments"]["1"]["status"] == "close_only"
    settled = {**base, "close_only": True, "settlement": {"price": "2000"}}
    store.sync_universe([settled], 1_700_000_100_000)
    assert store.manifest["instruments"]["1"]["status"] == "settled"


def test_manifest_is_atomically_saved_and_reloadable(tmp_path) -> None:
    store = MarketDataStore(tmp_path)
    store.manifest["last_processed_daily_bar_ms"] = 1_700_000_000_000
    store.save_manifest(1_700_000_100_000)
    restored = MarketDataStore(tmp_path)
    assert restored.manifest["last_processed_daily_bar_ms"] == 1_700_000_000_000
    assert restored.manifest["updated_at_ms"] == 1_700_000_100_000


def test_snapshot_retention_prunes_only_old_partitions(tmp_path) -> None:
    store = MarketDataStore(tmp_path)
    now = int(datetime(2026, 11, 16, tzinfo=UTC).timestamp() * 1000)
    old = now - 120 * 86_400_000
    recent = now - 1 * 86_400_000
    old_path = store.save_snapshot(old, [{"instrument_id": 1, "last_price": "10"}])
    recent_path = store.save_snapshot(recent, [{"instrument_id": 1, "last_price": "11"}])
    assert store.prune_snapshots(now, retain_days=90) == 1
    assert not old_path.exists()
    assert recent_path.exists()



def test_candle_append_does_not_claim_bootstrap_complete_until_verified(tmp_path) -> None:
    store = MarketDataStore(tmp_path, provider="mexc")
    store.sync_universe([{
        "instrument_id": 9,
        "category": "unclassified",
        "symbol": "NEW_USDT",
        "isolated_only": False,
        "close_only": False,
        "settlement": None,
    }], 1_700_000_000_000)
    store.append_candles([candle(9, 1_700_006_400_000)])
    assert store.manifest["instruments"]["9"]["bootstrap_complete"] is False
    store.mark_bootstrap_complete(
        9,
        completed_at_ms=1_700_100_000_000,
        history_status="INSUFFICIENT_HISTORY",
    )
    assert store.manifest["instruments"]["9"]["bootstrap_complete"] is True
    assert store.manifest["instruments"]["9"]["history_status"] == "INSUFFICIENT_HISTORY"


def test_market_data_store_refuses_to_mix_exchange_histories(tmp_path) -> None:
    mexc_store = MarketDataStore(tmp_path, provider="mexc")
    mexc_store.save_manifest(1_700_000_000_000)
    with pytest.raises(MarketDataStoreError, match="refusing to mix exchange histories"):
        MarketDataStore(tmp_path, provider="polymarket")


def test_funding_events_are_partitioned_deduplicated_and_reloadable(tmp_path) -> None:
    store = MarketDataStore(tmp_path, provider="mexc")
    store.sync_universe([{
        "instrument_id": 21,
        "category": "crypto",
        "symbol": "BTC_USDT",
        "isolated_only": False,
        "close_only": False,
        "settlement": None,
    }], 1_700_000_000_000)
    funding = {
        "instrument_id": 21,
        "symbol": "BTC_USDT",
        "timestamp_ms": 1_700_000_000_000,
        "funding_rate": "-0.0001",
    }
    assert store.append_funding([funding, funding]) == 1
    assert store.load_funding(21) == [funding]
    assert store.manifest["instruments"]["21"]["last_funding_timestamp_ms"] == 1_700_000_000_000
