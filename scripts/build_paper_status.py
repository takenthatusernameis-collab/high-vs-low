#!/usr/bin/env python3
"""Build the compact active MEXC paper status and its two equity curves."""
from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

from high_vs_low.market_data_store import MarketDataStore
from high_vs_low.paper_status import render_paper_status, render_primary_trade_journal

DAY_MS = 86_400_000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("PAPER_STATE_DIR", "state")),
    )
    parser.add_argument(
        "--market-data-dir",
        type=Path,
        default=Path(os.environ.get("MARKET_DATA_DIR", "market-data")),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.environ.get("PAPER_STATUS_PATH", "PAPER_STATUS.md")),
    )
    parser.add_argument(
        "--journal-output",
        type=Path,
        default=Path(os.environ.get("PAPER_TRADE_JOURNAL_PATH", "PAPER_TRADE_JOURNAL.md")),
    )
    parser.add_argument(
        "--equal-svg-output",
        type=Path,
        default=Path(os.environ.get(
            "PAPER_EQUAL_EQUITY_SVG_PATH", "equal_exposure_equity_curve.svg"
        )),
    )
    parser.add_argument(
        "--alternative-svg-output",
        type=Path,
        default=Path(os.environ.get(
            "PAPER_ALTERNATIVE_EQUITY_SVG_PATH", "alternative_path_equity_curve.svg"
        )),
    )
    args = parser.parse_args()

    generated_at_ms = int(datetime.now(UTC).timestamp() * 1000)
    store = MarketDataStore(args.market_data_dir, provider="mexc")
    history = store.load_all_candles(
        start_timestamp_ms=max(0, generated_at_ms - 45 * DAY_MS)
    )
    environment = dict(os.environ)
    server_url = environment.get("PAPER_WORKFLOW_SERVER_URL", "https://github.com")
    repository = environment.get("PAPER_WORKFLOW_REPOSITORY", "")
    run_id = environment.get("PAPER_WORKFLOW_RUN_ID", "")
    metadata = {
        "server_url": server_url,
        "repository": repository,
        "run_id": run_id,
        "run_url": (
            f"{server_url}/{repository}/actions/runs/{run_id}"
            if repository and run_id else ""
        ),
        "run_number": environment.get("PAPER_WORKFLOW_RUN_NUMBER", ""),
        "run_attempt": environment.get("PAPER_WORKFLOW_RUN_ATTEMPT", ""),
        "outcome": environment.get("PAPER_WORKFLOW_OUTCOME", "not supplied"),
        "event_name": environment.get("GITHUB_EVENT_NAME", "workflow run"),
    }
    report = render_paper_status(
        args.state_dir,
        history,
        metadata,
        generated_at_ms=generated_at_ms,
        equal_svg_path=args.equal_svg_output,
        alternative_svg_path=args.alternative_svg_output,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    journal = render_primary_trade_journal(
        args.state_dir,
        generated_at_ms=generated_at_ms,
        workflow_metadata=metadata,
    )
    args.journal_output.parent.mkdir(parents=True, exist_ok=True)
    args.journal_output.write_text(journal, encoding="utf-8")
    print(
        f"Built compact paper status: {args.output}; journal: {args.journal_output}; "
        f"equal curve: {args.equal_svg_output}; "
        f"alternative curve: {args.alternative_svg_output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
