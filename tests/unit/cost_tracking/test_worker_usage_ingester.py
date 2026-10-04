"""
Unit tests for non-git worker cost ingestion (#737, step 8).

The existing worker ingester attributes Claude-session costs through
the ``worktrees/<agent_id>`` path shape. The Tow workers are plain
pull-loop processes with no worktree, so their per-call usage JSONL
(written by the finder) is ingested directly into ``token_events``
keyed by ``agent_id`` and ``task_id``. Ingestion is idempotent: each
usage row gets a deterministic ``request_id``, and the store's
partial unique index makes re-ingestion a no-op.
"""

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from src.cost_tracking.cost_store import CostStore
from src.cost_tracking.worker_usage_ingester import (
    events_for_tasks,
    ingest_usage_log,
)

pytestmark = pytest.mark.unit


def _usage_row(**overrides: Any) -> Dict[str, Any]:
    """Build one usage JSONL row as the finder writes it."""
    row = {
        "ts": "2026-10-04T12:00:00+00:00",
        "agent_id": "finder-pplx-1",
        "engine": "perplexity",
        "model": "sonar",
        "case_id": "tow-0001",
        "task_id": "task-find-1",
        "prompt_tokens": 120,
        "completion_tokens": 80,
        "cost_usd": 0.0002,
    }
    row.update(overrides)
    return row


def _write_log(path: Path, rows: list) -> Path:
    """Write usage rows as JSONL."""
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


class TestIngestUsageLog:
    """Test suite for the usage JSONL ingestion."""

    def test_rows_land_keyed_by_agent_and_task(self, tmp_path: Path) -> None:
        """Each usage row becomes a token_events row with no worktree."""
        store = CostStore(db_path=tmp_path / "costs.db")
        log = _write_log(
            tmp_path / "usage.jsonl",
            [_usage_row(), _usage_row(task_id="task-find-2", case_id="tow-0002")],
        )

        count = ingest_usage_log(store, log, run_id="tow-pilot-1", project_id="tow-b")

        assert count == 2
        rows = events_for_tasks(store, ["task-find-1"])
        assert len(rows) == 1
        row = rows[0]
        assert row["agent_id"] == "finder-pplx-1"
        assert row["provider"] == "perplexity"
        assert row["input_tokens"] == 120
        assert row["output_tokens"] == 80

    def test_reingestion_is_idempotent(self, tmp_path: Path) -> None:
        """Running the ingester twice adds nothing the second time."""
        store = CostStore(db_path=tmp_path / "costs.db")
        log = _write_log(tmp_path / "usage.jsonl", [_usage_row()])

        first = ingest_usage_log(store, log, run_id="r", project_id="p")
        second = ingest_usage_log(store, log, run_id="r", project_id="p")

        assert first == 1
        assert second == 0
        assert len(events_for_tasks(store, ["task-find-1"])) == 1

    def test_malformed_lines_are_skipped_not_fatal(self, tmp_path: Path) -> None:
        """A corrupt line cannot take down the ingestion of the rest."""
        store = CostStore(db_path=tmp_path / "costs.db")
        log = tmp_path / "usage.jsonl"
        log.write_text(
            json.dumps(_usage_row()) + "\nNOT JSON AT ALL\n",
            encoding="utf-8",
        )

        count = ingest_usage_log(store, log, run_id="r", project_id="p")

        assert count == 1

    def test_events_for_tasks_filters_by_task_ids(self, tmp_path: Path) -> None:
        """The export queries exactly the case's task ids."""
        store = CostStore(db_path=tmp_path / "costs.db")
        log = _write_log(
            tmp_path / "usage.jsonl",
            [_usage_row(), _usage_row(task_id="other-task")],
        )
        ingest_usage_log(store, log, run_id="r", project_id="p")

        rows = events_for_tasks(store, ["task-find-1", "missing-task"])

        assert {r["task_id"] for r in rows} == {"task-find-1"}
