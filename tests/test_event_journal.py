from __future__ import annotations

import json

import pytest

from high_vs_low.event_journal import (
    EventJournalError,
    build_event_batch,
    order_event_batches,
    read_batch,
    verify_event_batch,
    write_batch_atomic,
)


def _event(event_id: str = "evt-1") -> dict[str, object]:
    return {
        "event_id": event_id,
        "idempotency_key": f"idem-{event_id}",
        "account_id": "forward-paper",
        "strategy_id": "q-baseline",
        "occurred_at_ms": 1794787200000,
        "event_type": "ACCOUNT_INITIALIZED",
        "payload": {"initial_capital": "100.00", "currency": "USDT"},
    }


def test_batch_hash_and_event_chain_verify() -> None:
    batch = build_event_batch("run-1", 1794787201000, [_event()])
    assert verify_event_batch(batch)
    batch["events"][0]["payload"]["initial_capital"] = "999.00"  # type: ignore[index]
    assert not verify_event_batch(batch)



def test_event_batches_are_restored_by_hash_links_not_exchange_timestamps() -> None:
    first = build_event_batch("root-batch", 1_800_000_000_200, [_event("root")])
    second = build_event_batch(
        "child-batch",
        1_800_000_000_100,
        [_event("child")],
        previous_batch_hash=str(first["batch_hash"]),
    )

    restored = order_event_batches([second, first])

    assert [row["run_id"] for row in restored] == ["root-batch", "child-batch"]


def test_local_event_batch_ordering_rejects_forks() -> None:
    root = build_event_batch("root-fork-test", 1_800_000_000_000, [_event("root-fork")])
    left = build_event_batch(
        "left-fork-test",
        1_800_000_000_001,
        [_event("left-fork")],
        previous_batch_hash=str(root["batch_hash"]),
    )
    right = build_event_batch(
        "right-fork-test",
        1_800_000_000_002,
        [_event("right-fork")],
        previous_batch_hash=str(root["batch_hash"]),
    )

    with pytest.raises(EventJournalError, match="forks"):
        order_event_batches([root, right, left])


def test_binary_float_rejected_for_financial_ledger() -> None:
    event = _event()
    event["payload"] = {"amount": 1.25}
    with pytest.raises(EventJournalError, match="Binary float"):
        build_event_batch("run-2", 1794787201000, [event])



def test_nested_tuple_cannot_bypass_binary_float_rejection() -> None:
    event = _event()
    event["payload"] = {"items": ({"amount": 1.25},)}
    with pytest.raises(EventJournalError, match="Binary float"):
        build_event_batch("run-tuple-float", 1794787201000, [event])


def test_event_schema_rejects_unprojected_extra_fields() -> None:
    event = _event()
    event["extra_metadata"] = {"source": "untracked"}
    with pytest.raises(EventJournalError, match="unsupported keys"):
        build_event_batch("run-extra-event", 1794787201000, [event])


def test_canonical_json_rejects_non_string_object_keys() -> None:
    from high_vs_low.event_journal import canonical_json

    with pytest.raises(EventJournalError, match="keys must be strings"):
        canonical_json({"payload": {1: "not-a-string-key"}})


def test_verifier_rejects_extra_batch_or_event_fields() -> None:
    batch = build_event_batch("run-exact-schema", 1794787201000, [_event()])
    batch["unexpected"] = "extra field"
    assert not verify_event_batch(batch)
    batch = build_event_batch("run-exact-event-schema", 1794787201000, [_event()])
    batch["events"][0]["unexpected"] = "extra field"
    assert not verify_event_batch(batch)


def test_duplicate_event_ids_and_idempotency_keys_rejected() -> None:
    with pytest.raises(EventJournalError, match="Duplicate"):
        build_event_batch("run-3", 1794787201000, [_event(), _event("evt-1")])


def test_atomic_write_and_identical_retry_are_idempotent(tmp_path) -> None:
    batch = build_event_batch("run-4", 1794787201000, [_event()])
    path = write_batch_atomic(tmp_path, batch)
    assert read_batch(path) == batch
    assert write_batch_atomic(tmp_path, batch) == path
    assert json.loads(path.read_text())["batch_hash"] == batch["batch_hash"]


def test_run_id_collision_with_different_payload_is_rejected(tmp_path) -> None:
    first = build_event_batch("run-5", 1794787201000, [_event()])
    second = build_event_batch("run-5", 1794787201001, [_event()])
    write_batch_atomic(tmp_path, first)
    with pytest.raises(EventJournalError, match="different event content"):
        write_batch_atomic(tmp_path, second)
