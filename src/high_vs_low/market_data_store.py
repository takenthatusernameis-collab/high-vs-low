"""Compact, incremental public market-data persistence for GitHub Actions.

Daily candles are partitioned by UTC date; timestamped ticker observations are
gzip-compressed into UTC-hour partitions. The manifest is atomic and records
instrument lifecycle and per-instrument candle watermarks.
"""
from __future__ import annotations

import gzip
import json
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SNAPSHOT_RETENTION_DAYS = 90


class MarketDataStoreError(RuntimeError):
    pass


class MarketDataStore:
    def __init__(self, root: str | Path, *, provider: str = "polymarket") -> None:
        if not provider or not provider.strip():
            raise ValueError("provider must be a non-empty name")
        self.provider = provider.strip().lower()
        self.root = Path(root)
        self.data_dir = self.root / "data"
        self.candles_dir = self.data_dir / "candles"
        self.funding_dir = self.data_dir / "funding"
        self.snapshots_dir = self.data_dir / "snapshots"
        self.manifest_path = self.data_dir / "manifest.json"
        self.candles_dir.mkdir(parents=True, exist_ok=True)
        self.funding_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = self._load_manifest()
        stored_provider = self.manifest.get("provider")
        if stored_provider and str(stored_provider).lower() != self.provider:
            raise MarketDataStoreError(
                f"Market-data store belongs to {stored_provider}, not {self.provider}; "
                "refusing to mix exchange histories"
            )
        self.manifest["provider"] = self.provider

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {
                "schema_version": SCHEMA_VERSION,
                "provider": self.provider,
                "created_at_ms": None,
                "updated_at_ms": None,
                "last_snapshot_ms": None,
                "last_processed_daily_bar_ms": None,
                "bootstrap_complete": False,
                "instruments": {},
            }
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MarketDataStoreError(f"Invalid market-data manifest: {exc}") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != SCHEMA_VERSION
            or not isinstance(value.get("instruments"), dict)
        ):
            raise MarketDataStoreError("Unsupported or malformed market-data manifest")
        return value

    def save_manifest(self, now_ms: int) -> None:
        self.manifest["updated_at_ms"] = int(now_ms)
        if self.manifest["created_at_ms"] is None:
            self.manifest["created_at_ms"] = int(now_ms)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.manifest_path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(self.manifest, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, self.manifest_path)

    def sync_universe(
        self, instruments: list[dict[str, Any]], now_ms: int
    ) -> dict[str, int]:
        """Refresh metadata and lifecycle without treating one miss as a delisting."""
        current_ids: set[str] = set()
        counts = defaultdict(int)
        stored = self.manifest["instruments"]
        for row in instruments:
            instrument_id = str(row["instrument_id"])
            current_ids.add(instrument_id)
            previous = stored.get(instrument_id, {})
            if row.get("settlement") is not None:
                status = "settled"
            elif row.get("isolated_only") is True:
                status = "isolated_only"
            elif row.get("close_only") is True:
                status = "close_only"
            else:
                status = "active"
            first_seen = previous.get("first_seen_at_ms", int(now_ms))
            provider_origin = row.get("opening_time_ms") or row.get("create_time_ms")
            origin_day_ms = (
                (int(provider_origin) // 86_400_000) * 86_400_000
                if provider_origin else None
            )
            origin_unchanged = previous.get("history_origin_timestamp_ms") == origin_day_ms
            stored[instrument_id] = {
                **row,
                "instrument_id": int(row["instrument_id"]),
                "first_seen_at_ms": int(first_seen),
                "last_seen_at_ms": int(now_ms),
                "missing_from_feed_count": 0,
                "status": status,
                "last_candle_timestamp_ms": previous.get("last_candle_timestamp_ms") if origin_unchanged else None,
                "bootstrap_complete": bool(previous.get("bootstrap_complete", False)) and origin_unchanged,
                "bootstrap_attempts": int(previous.get("bootstrap_attempts", 0)),
                "last_bootstrap_error": previous.get("last_bootstrap_error") if origin_unchanged else None,
                "history_status": previous.get("history_status") if origin_unchanged else None,
                "last_checked_daily_bar_ms": previous.get("last_checked_daily_bar_ms") if origin_unchanged else None,
                "last_funding_timestamp_ms": previous.get("last_funding_timestamp_ms"),
                "all_time_history_verified": bool(previous.get("all_time_history_verified", False)) and origin_unchanged,
                "history_origin_timestamp_ms": origin_day_ms,
                "history_first_candle_timestamp_ms": previous.get("history_first_candle_timestamp_ms") if origin_unchanged else None,
                "history_last_candle_timestamp_ms": previous.get("history_last_candle_timestamp_ms") if origin_unchanged else None,
                "history_observed_daily_gap_count": int(previous.get("history_observed_daily_gap_count", 0)) if origin_unchanged else 0,
            }
            counts[status] += 1
        for instrument_id, previous in stored.items():
            if instrument_id in current_ids:
                continue
            previous["missing_from_feed_count"] = int(
                previous.get("missing_from_feed_count", 0)
            ) + 1
            if previous["missing_from_feed_count"] >= 2:
                previous["status"] = "missing_from_feed"
            counts[str(previous.get("status", "unknown"))] += 1
        return dict(sorted(counts.items()))

    def append_candles(self, rows: list[dict[str, Any]]) -> int:
        """Append completed daily candle rows to one gzip partition per UTC date."""
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        seen: set[tuple[int, int]] = set()
        for row in rows:
            instrument_id = row.get("instrument_id")
            timestamp = row.get("timestamp_ms")
            if (
                isinstance(instrument_id, bool)
                or not isinstance(instrument_id, int)
                or isinstance(timestamp, bool)
                or not isinstance(timestamp, int)
                or timestamp <= 0
            ):
                raise MarketDataStoreError("Candle row has invalid instrument or timestamp")
            key = (instrument_id, timestamp)
            if key in seen:
                continue
            seen.add(key)
            day = datetime.fromtimestamp(timestamp / 1000, tz=UTC).strftime("%Y-%m-%d")
            grouped[day].append(row)
        for day, day_rows in grouped.items():
            path = self.candles_dir / f"{day}.jsonl.gz"
            block = "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in sorted(
                    day_rows,
                    key=lambda item: (item["instrument_id"], item["timestamp_ms"]),
                )
            ).encode("utf-8")
            with path.open("ab") as handle:
                handle.write(gzip.compress(block, mtime=0))
        for row in rows:
            instrument = self.manifest["instruments"].get(str(row["instrument_id"]))
            if instrument is not None:
                latest = instrument.get("last_candle_timestamp_ms")
                timestamp = int(row["timestamp_ms"])
                if latest is None or timestamp > int(latest):
                    instrument["last_candle_timestamp_ms"] = timestamp
        return sum(len(items) for items in grouped.values())


    def mark_bootstrap_complete(
        self,
        instrument_id: int,
        *,
        completed_at_ms: int,
        history_status: str,
    ) -> None:
        """Mark bootstrap only after the caller verifies the requested history window."""
        instrument = self.manifest["instruments"].get(str(int(instrument_id)))
        if instrument is None:
            raise MarketDataStoreError(f"Unknown instrument {instrument_id}")
        if history_status not in {
            "AVAILABLE", "INSUFFICIENT_HISTORY", "NO_TRADES_IN_WINDOW"
        }:
            raise ValueError("Unsupported history_status")
        instrument["bootstrap_complete"] = True
        instrument["bootstrap_completed_at_ms"] = int(completed_at_ms)
        instrument["history_status"] = history_status

    def append_funding(self, rows: list[dict[str, Any]]) -> int:
        """Append deduplicated settlement observations in daily gzip partitions."""
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        seen: set[tuple[int, int]] = set()
        for row in rows:
            instrument_id = row.get("instrument_id")
            timestamp = row.get("timestamp_ms")
            if (
                isinstance(instrument_id, bool)
                or not isinstance(instrument_id, int)
                or isinstance(timestamp, bool)
                or not isinstance(timestamp, int)
                or timestamp <= 0
            ):
                raise MarketDataStoreError("Funding row has invalid instrument or timestamp")
            key = (instrument_id, timestamp)
            if key in seen:
                continue
            seen.add(key)
            day = datetime.fromtimestamp(timestamp / 1000, tz=UTC).strftime("%Y-%m-%d")
            grouped[day].append(row)
        for day, day_rows in grouped.items():
            path = self.funding_dir / f"{day}.jsonl.gz"
            block = "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in sorted(
                    day_rows,
                    key=lambda item: (item["instrument_id"], item["timestamp_ms"]),
                )
            ).encode("utf-8")
            with path.open("ab") as handle:
                handle.write(gzip.compress(block, mtime=0))
        for row in rows:
            instrument = self.manifest["instruments"].get(str(row["instrument_id"]))
            if instrument is not None:
                latest = instrument.get("last_funding_timestamp_ms")
                timestamp = int(row["timestamp_ms"])
                if latest is None or timestamp > int(latest):
                    instrument["last_funding_timestamp_ms"] = timestamp
        return sum(len(items) for items in grouped.values())

    def load_funding(
        self,
        instrument_id: int,
        *,
        start_timestamp_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Load one instrument's funding events, deduplicated by settlement time."""
        result: dict[int, dict[str, Any]] = {}
        for path in sorted(self.funding_dir.glob("*.jsonl.gz")):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as source:
                    for line in source:
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        if int(row.get("instrument_id", -1)) != int(instrument_id):
                            continue
                        timestamp = int(row["timestamp_ms"])
                        if start_timestamp_ms is not None and timestamp < start_timestamp_ms:
                            continue
                        result[timestamp] = row
            except (
                OSError, EOFError, json.JSONDecodeError, KeyError, TypeError, ValueError
            ) as exc:
                raise MarketDataStoreError(f"Invalid funding partition {path}: {exc}") from exc
        return [result[timestamp] for timestamp in sorted(result)]

    def save_snapshot(self, snapshot_ms: int, rows: list[dict[str, Any]]) -> Path:
        stamp = datetime.fromtimestamp(snapshot_ms / 1000, tz=UTC)
        directory = self.snapshots_dir / stamp.strftime("%Y-%m-%d")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{stamp:%H}.jsonl.gz"
        block = "".join(
            json.dumps(
                {"snapshot_timestamp_ms": int(snapshot_ms), **row},
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            for row in sorted(rows, key=lambda item: int(item["instrument_id"]))
        ).encode("utf-8")
        with path.open("ab") as handle:
            handle.write(gzip.compress(block, mtime=0))
        self.manifest["last_snapshot_ms"] = int(snapshot_ms)
        return path

    def load_all_candles(
        self, *, start_timestamp_ms: int | None = None
    ) -> dict[int, list[dict[str, Any]]]:
        """Read the stored series in one pass for efficient cross-sectional recalculation."""
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for path in sorted(self.candles_dir.glob("*.jsonl.gz")):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as source:
                    for line in source:
                        if not line.strip():
                            continue
                        item = json.loads(line)
                        instrument_id = int(item["instrument_id"])
                        timestamp = int(item["timestamp_ms"])
                        if start_timestamp_ms is not None and timestamp < start_timestamp_ms:
                            continue
                        grouped[instrument_id].append(item)
            except (
                OSError, EOFError, json.JSONDecodeError, KeyError, TypeError, ValueError
            ) as exc:
                raise MarketDataStoreError(
                    f"Invalid candle partition {path}: {exc}"
                ) from exc
        result: dict[int, list[dict[str, Any]]] = {}
        for instrument_id, rows in grouped.items():
            by_timestamp = {
                int(row["timestamp_ms"]): row
                for row in rows
            }
            result[instrument_id] = [
                by_timestamp[timestamp] for timestamp in sorted(by_timestamp)
            ]
        return result

    def load_candles(
        self,
        instrument_id: int,
        *,
        start_timestamp_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.load_all_candles(
            start_timestamp_ms=start_timestamp_ms
        ).get(int(instrument_id), [])

    def find_snapshot_mark(
        self,
        instrument_id: int,
        target_timestamp_ms: int,
        *,
        max_age_ms: int = 300_000,
    ) -> tuple[int, str] | None:
        """Return the nearest stored mark at/before a funding settlement timestamp."""
        start_ms = max(0, target_timestamp_ms - max_age_ms)
        start_dt = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
        end_dt = datetime.fromtimestamp(target_timestamp_ms / 1000, tz=UTC)
        current = start_dt.replace(minute=0, second=0, microsecond=0)
        best: tuple[int, str] | None = None
        while current <= end_dt:
            path = (
                self.snapshots_dir
                / current.strftime("%Y-%m-%d")
                / f"{current:%H}.jsonl.gz"
            )
            if path.exists():
                try:
                    with gzip.open(path, "rt", encoding="utf-8") as source:
                        for line in source:
                            if not line.strip():
                                continue
                            row = json.loads(line)
                            if int(row.get("instrument_id", -1)) != int(instrument_id):
                                continue
                            timestamp = int(row.get("snapshot_timestamp_ms", 0))
                            mark = row.get("mark_price")
                            if (
                                start_ms <= timestamp <= target_timestamp_ms
                                and isinstance(mark, str)
                                and Decimal(mark).is_finite()
                                and Decimal(mark) > 0
                                and timestamp > (0 if best is None else best[0])
                            ):
                                best = (timestamp, mark)
                except (
                    OSError, EOFError, json.JSONDecodeError, KeyError, TypeError, ValueError
                ) as exc:
                    raise MarketDataStoreError(
                        f"Invalid snapshot partition {path}: {exc}"
                    ) from exc
            current += timedelta(hours=1)
        return best

    def load_projection(self) -> dict[str, Any] | None:
        path = self.data_dir / "paper_projection.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MarketDataStoreError(f"Invalid paper projection: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise MarketDataStoreError("Unsupported or malformed paper projection")
        return value

    def save_projection(self, projection: dict[str, Any]) -> None:
        path = self.data_dir / "paper_projection.json"
        temp = path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(projection, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)

    def prune_snapshots(self, now_ms: int, retain_days: int = SNAPSHOT_RETENTION_DAYS) -> int:
        if retain_days < 1:
            raise ValueError("retain_days must be positive")
        cutoff = datetime.fromtimestamp(now_ms / 1000, tz=UTC) - timedelta(days=retain_days)
        deleted = 0
        for path in self.snapshots_dir.glob("*/*.jsonl.gz"):
            try:
                day = datetime.strptime(path.parent.name, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            if day < cutoff:
                path.unlink(missing_ok=True)
                deleted += 1
        for directory in self.snapshots_dir.iterdir():
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        return deleted
