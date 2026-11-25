from __future__ import annotations

import json
from pathlib import Path

import pytest

from high_vs_low.event_journal import build_event_batch
from scripts.high_low_paper_cycle import _load_state


ACCOUNT_ID = "mexc-usdt-forward-high-low-100usdt-v1"
STRATEGY_ID = "high-low-ath-atl-cross-sectional-v1-100usdt"
PROTOCOL = "PUBLISH_READBACK_THEN_FRESH_CHECKOUT"


def _write_probe(state_dir: Path, *, details: dict | None = None) -> dict:
    timestamp = 1_795_555_392_595
    event = {
        "event_id": "state-probe-1795555392595",
        "idempotency_key": "state-probe-1795555392595",
        "account_id": ACCOUNT_ID,
        "strategy_id": STRATEGY_ID,
        "occurred_at_ms": timestamp,
        "event_type": "paper_state_recovery_probe",
        "payload": {
            "details": details or {
                "protocol": PROTOCOL,
                "financial_state_created": False,
                "account_initialized": False,
                "orders_submitted": 0,
            }
        },
    }
    batch = build_event_batch(
        run_id="state-probe-1795555392595",
        created_at_ms=timestamp,
        events=[event],
        previous_batch_hash=None,
    )
    path = state_dir / "events" / "2026" / "11" / "state-probe.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(batch), encoding="utf-8")
    return batch


def test_fresh_checkout_recognizes_valid_nonfinancial_probe(tmp_path: Path) -> None:
    batch = _write_probe(tmp_path)

    portfolio, previous_hash, recovery_probe_verified = _load_state(tmp_path)

    assert portfolio is None
    assert previous_hash == batch["batch_hash"]
    assert recovery_probe_verified is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"protocol": "UNRECOGNIZED"},
        {"financial_state_created": True},
        {"account_initialized": True},
        {"orders_submitted": 1},
    ],
)
def test_probe_with_unsafe_or_incomplete_details_is_not_accepted(
    tmp_path: Path,
    overrides: dict,
) -> None:
    details = {
        "protocol": PROTOCOL,
        "financial_state_created": False,
        "account_initialized": False,
        "orders_submitted": 0,
        **overrides,
    }
    _write_probe(tmp_path, details=details)

    portfolio, previous_hash, recovery_probe_verified = _load_state(tmp_path)

    assert portfolio is None
    assert previous_hash is not None
    assert recovery_probe_verified is False


def test_workflow_continues_probe_only_run_from_distinct_fresh_checkout() -> None:
    workflow = Path(".github/workflows/high-low-paper-cycle.yml").read_text(
        encoding="utf-8"
    )

    assert "steps.cycle.outputs.mode == 'STATE_PERSISTENCE_PROBE'" in workflow
    assert "path: state-recovery" in workflow
    assert "PAPER_STATE_DIR: ../state-recovery" in workflow
    assert "Continue immediately after verified recovery probe" in workflow


def test_one_time_activation_is_path_scoped_not_a_general_push_trigger() -> None:
    workflow = Path(".github/workflows/high-low-paper-cycle.yml").read_text(
        encoding="utf-8"
    )

    assert "paths:" in workflow
    assert '- ".github/activations/high-low-first-run-once"' in workflow
    assert "contains(github.event.head_commit.message, '[HIGH_LOW_BOOTSTRAP_AFTER_PROBE]')" in workflow
