"""Immutable, hash-chained per-run event batches.

This module handles local atomic batch files. Publishing a completed batch to
the dedicated Git state branch is a separate workflow responsibility.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class EventJournalError(RuntimeError):
    pass


def _validate_json_value(value: Any, path: str = "event") -> None:
    """Reject values that cannot be represented unambiguously in canonical JSON."""
    if isinstance(value, float):
        raise EventJournalError(f"Binary float is not allowed in durable events: {path}")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise EventJournalError(f"JSON object keys must be strings: {path}")
            _validate_json_value(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_json_value(child, f"{path}[{index}]")
    elif value is None or isinstance(value, (str, int, bool)):
        return
    else:
        raise EventJournalError(
            f"Unsupported durable JSON value {type(value).__name__}: {path}"
        )


def canonical_json(value: Any) -> str:
    _validate_json_value(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _event_hash(event_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(event_without_hash).encode("utf-8")).hexdigest()


def _is_positive_timestamp(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def build_event_batch(
    run_id: str,
    created_at_ms: int,
    events: Iterable[dict[str, Any]],
    previous_batch_hash: str | None = None,
) -> dict[str, Any]:
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", run_id):
        raise EventJournalError("run_id contains unsupported characters")
    if not _is_positive_timestamp(created_at_ms):
        raise EventJournalError("created_at_ms must be a positive integer")
    if previous_batch_hash is not None and (
        not isinstance(previous_batch_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", previous_batch_hash)
    ):
        raise EventJournalError("previous_batch_hash must be a SHA-256 hex digest or null")

    required = {
        "event_id",
        "idempotency_key",
        "account_id",
        "strategy_id",
        "occurred_at_ms",
        "event_type",
        "payload",
    }
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    chained: list[dict[str, Any]] = []
    previous_event_hash = previous_batch_hash
    for original in events:
        if not isinstance(original, dict):
            raise EventJournalError("Each event must be a JSON object")
        event = dict(original)
        missing = required - event.keys()
        unexpected = event.keys() - required
        if missing:
            raise EventJournalError(f"Event missing required keys: {sorted(missing)}")
        if unexpected:
            raise EventJournalError(
                f"Event contains unsupported keys: {sorted(str(key) for key in unexpected)}"
            )
        for field in (
            "event_id",
            "idempotency_key",
            "account_id",
            "strategy_id",
            "event_type",
        ):
            if not isinstance(event[field], str) or not event[field]:
                raise EventJournalError(f"{field} must be a non-empty string")
        if not _is_positive_timestamp(event["occurred_at_ms"]):
            raise EventJournalError("occurred_at_ms must be a positive integer")
        if not isinstance(event["payload"], dict):
            raise EventJournalError("event payload must be a JSON object")

        event_id = event["event_id"]
        idempotency_key = event["idempotency_key"]
        if event_id in seen_ids:
            raise EventJournalError("Duplicate event_id")
        if idempotency_key in seen_keys:
            raise EventJournalError("Duplicate idempotency_key")
        seen_ids.add(event_id)
        seen_keys.add(idempotency_key)
        event["previous_event_hash"] = previous_event_hash
        event["event_hash"] = _event_hash(event)
        previous_event_hash = event["event_hash"]
        chained.append(event)

    batch: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at_ms": created_at_ms,
        "previous_batch_hash": previous_batch_hash,
        "events": chained,
    }
    batch["batch_hash"] = hashlib.sha256(canonical_json(batch).encode("utf-8")).hexdigest()
    return batch


def verify_event_batch(batch: dict[str, Any]) -> bool:
    try:
        expected_batch_keys = {
            "schema_version",
            "run_id",
            "created_at_ms",
            "previous_batch_hash",
            "events",
            "batch_hash",
        }
        if not isinstance(batch, dict) or set(batch) != expected_batch_keys:
            return False
        if batch.get("schema_version") != 1:
            return False
        run_id = batch.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", run_id):
            return False
        if not _is_positive_timestamp(batch.get("created_at_ms")):
            return False
        expected_previous = batch.get("previous_batch_hash")
        if expected_previous is not None and (
            not isinstance(expected_previous, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_previous)
        ):
            return False
        events = batch.get("events")
        if not isinstance(events, list):
            return False
        event_required = {
            "event_id",
            "idempotency_key",
            "account_id",
            "strategy_id",
            "occurred_at_ms",
            "event_type",
            "payload",
            "previous_event_hash",
            "event_hash",
        }
        event_ids: set[str] = set()
        idempotency: set[str] = set()
        prior = expected_previous
        for event in events:
            if not isinstance(event, dict) or set(event) != event_required:
                return False
            for field in (
                "event_id",
                "idempotency_key",
                "account_id",
                "strategy_id",
                "event_type",
                "event_hash",
            ):
                if not isinstance(event[field], str) or not event[field]:
                    return False
            if not _is_positive_timestamp(event["occurred_at_ms"]):
                return False
            if not isinstance(event["payload"], dict):
                return False
            if event["previous_event_hash"] != prior:
                return False
            if event["event_id"] in event_ids or event["idempotency_key"] in idempotency:
                return False
            event_ids.add(event["event_id"])
            idempotency.add(event["idempotency_key"])
            event_hash = event["event_hash"]
            without_hash = dict(event)
            without_hash.pop("event_hash", None)
            if _event_hash(without_hash) != event_hash:
                return False
            prior = event_hash

        without_batch_hash = dict(batch)
        actual_hash = without_batch_hash.pop("batch_hash", None)
        if not isinstance(actual_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", actual_hash):
            return False
        calculated = hashlib.sha256(
            canonical_json(without_batch_hash).encode("utf-8")
        ).hexdigest()
        return calculated == actual_hash
    except (EventJournalError, TypeError, ValueError, KeyError):
        return False



def order_event_batches(batches: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return verified batches in parent-hash order, independent of venue clocks."""
    values = list(batches)
    if not values:
        return []

    by_hash: dict[str, dict[str, Any]] = {}
    seen_run_ids: set[str] = set()
    seen_event_ids: set[str] = set()
    seen_idempotency_keys: set[str] = set()
    for batch in values:
        if not verify_event_batch(batch):
            raise EventJournalError("Invalid event batch in local state chain")
        batch_hash = str(batch["batch_hash"])
        if batch_hash in by_hash:
            raise EventJournalError("Duplicate batch hash in local state chain")
        run_id = str(batch["run_id"])
        if run_id in seen_run_ids:
            raise EventJournalError(f"Duplicate run_id in local state chain: {run_id}")
        seen_run_ids.add(run_id)
        for event in batch["events"]:
            event_id = str(event["event_id"])
            idempotency_key = str(event["idempotency_key"])
            if event_id in seen_event_ids:
                raise EventJournalError(f"Duplicate event_id in local state chain: {event_id}")
            if idempotency_key in seen_idempotency_keys:
                raise EventJournalError(
                    f"Duplicate idempotency_key in local state chain: {idempotency_key}"
                )
            seen_event_ids.add(event_id)
            seen_idempotency_keys.add(idempotency_key)
        by_hash[batch_hash] = batch

    roots = [
        batch for batch in by_hash.values()
        if batch.get("previous_batch_hash") is None
    ]
    if len(roots) != 1:
        raise EventJournalError(
            f"Local state chain must have exactly one genesis batch; found {len(roots)}"
        )

    children: dict[str, list[str]] = {}
    for batch_hash, batch in by_hash.items():
        previous = batch.get("previous_batch_hash")
        if previous is None:
            continue
        if previous not in by_hash:
            raise EventJournalError(
                f"Orphan event batch in local state chain: {batch_hash}"
            )
        children.setdefault(str(previous), []).append(batch_hash)

    ordered: list[dict[str, Any]] = []
    visited: set[str] = set()
    current_hash = str(roots[0]["batch_hash"])
    while True:
        if current_hash in visited:
            raise EventJournalError("Local state chain contains a cycle")
        visited.add(current_hash)
        ordered.append(by_hash[current_hash])
        next_hashes = children.get(current_hash, [])
        if len(next_hashes) > 1:
            raise EventJournalError(
                f"Local state chain forks after batch {current_hash}"
            )
        if not next_hashes:
            break
        current_hash = next_hashes[0]

    if len(visited) != len(by_hash):
        raise EventJournalError("Local state chain contains disconnected batches")
    return ordered



def write_batch_atomic(root: str | Path, batch: dict[str, Any]) -> Path:
    """Atomically create a new batch file; never overwrite another run."""
    if not verify_event_batch(batch):
        raise EventJournalError("Refusing to persist an invalid event batch")
    created_at_ms = int(batch["created_at_ms"])
    date = datetime.fromtimestamp(created_at_ms / 1000, tz=UTC)
    directory = Path(root) / "events" / date.strftime("%Y") / date.strftime("%m")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{batch['run_id']}.json"
    payload = canonical_json(batch) + "\n"

    if destination.exists():
        if destination.read_text(encoding="utf-8") == payload:
            return destination
        raise EventJournalError("run_id already exists with different event content")

    fd, temporary_name = tempfile.mkstemp(prefix=".event-batch-", dir=directory)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, destination)
        except FileExistsError:
            if destination.read_text(encoding="utf-8") != payload:
                raise EventJournalError("Concurrent run_id collision") from None
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination


def read_batch(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not verify_event_batch(data):
        raise EventJournalError(f"Event batch integrity check failed: {path}")
    return data
