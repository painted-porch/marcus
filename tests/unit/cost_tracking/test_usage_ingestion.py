"""
Unit tests for the generic usage-ingestion API (ADR-0013 K8 consequence 3).

The shape here is the one agreed on issue #742 with the Minecraft
harness (the first non-Claude-Code consumer): one record per model
call, keyed by (run_id, agent_id, seq), with a nullable task_id and
an ``attribution`` of "held" / "between_tasks" / "controller", first-
class cache token counts, an optional vendor-reported ``usd`` and
``priced_by``, and a ``reported_by`` provenance tag distinguishing a
relay (harness/controller) from a worker's own self-report.
"""

from pathlib import Path

import pytest

from src.cost_tracking.cost_store import CostStore
from src.cost_tracking.usage_ingestion import (
    UsageRecord,
    ingest_usage_records,
    read_run_ledger,
)

pytestmark = pytest.mark.unit


def _record(**overrides: object) -> UsageRecord:
    """Build one agreed-shape usage record, overridable per test."""
    fields = {
        "run_id": "20261105T213906",
        "agent_id": "Bly_2139",
        "seq": "msg_011Cgm4KJfzf8B21huREKRY1",
        "at": "2026-11-06T04:39:35.028Z",
        "task_id": "1b0d549182604abba3876e04183eba23",
        "attribution": "held",
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "api": "anthropic-messages",
        "input_tokens": 10,
        "output_tokens": 210,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 11599,
        "reasoning_tokens": 106,
        "usd": 0.04667625,
        "basis": "vendor_usage_block",
        "priced_by": "pi@0.82.0",
        "reported_by": "harness",
    }
    fields.update(overrides)
    return UsageRecord(**fields)  # type: ignore[arg-type]


class TestIngestUsageRecords:
    """Test suite for ingest_usage_records."""

    def test_a_held_record_lands_with_its_task_id(self, tmp_path: Path) -> None:
        """An attribution="held" record keeps the task_id it was given."""
        store = CostStore(db_path=tmp_path / "costs.db")

        ack = ingest_usage_records(store, "project-A", [_record()])

        assert ack.accepted == 1
        assert ack.already_present == 0
        assert ack.rejected == []
        row = store.conn.execute(
            "SELECT task_id, agent_role, cache_read_tokens, cache_creation_tokens, "
            "attribution, reported_by, basis, priced_by, reported_usd, reasoning_tokens "
            "FROM token_events WHERE run_id = ?",
            ("20261105T213906",),
        ).fetchone()
        assert row[0] == "1b0d549182604abba3876e04183eba23"
        assert row[1] == "worker"
        assert row[2] == 0
        assert row[3] == 11599
        assert row[4] == "held"
        assert row[5] == "harness"
        assert row[6] == "vendor_usage_block"
        assert row[7] == "pi@0.82.0"
        assert row[8] == pytest.approx(0.04667625)
        assert row[9] == 106

    def test_between_tasks_record_forces_task_id_to_none(self, tmp_path: Path) -> None:
        """A between_tasks record must never be attributed to a task,
        even if a caller mistakenly supplies one (Minecraft's own
        stated rule: real spend happens between tasks and must not be
        silently folded into whichever task_id happens to be passed)."""
        store = CostStore(db_path=tmp_path / "costs.db")

        ingest_usage_records(
            store,
            "project-A",
            [_record(attribution="between_tasks", task_id="should-be-dropped")],
        )

        row = store.conn.execute(
            "SELECT task_id FROM token_events WHERE run_id = ?",
            ("20261105T213906",),
        ).fetchone()
        assert row[0] is None

    def test_controller_record_is_agent_role_planner_with_no_task(
        self, tmp_path: Path
    ) -> None:
        """The planner's own ledger rows are controller spend, never a
        worker's and never a task's."""
        store = CostStore(db_path=tmp_path / "costs.db")

        ingest_usage_records(
            store,
            "project-A",
            [
                _record(
                    agent_id="planner_2139",
                    attribution="controller",
                    task_id="should-be-dropped",
                    usd=None,
                    priced_by=None,
                    reported_by="controller",
                )
            ],
        )

        row = store.conn.execute(
            "SELECT task_id, agent_role, reported_by FROM token_events WHERE run_id = ?",
            ("20261105T213906",),
        ).fetchone()
        assert row[0] is None
        assert row[1] == "planner"
        assert row[2] == "controller"

    def test_replaying_the_same_batch_is_idempotent(self, tmp_path: Path) -> None:
        """Re-posting the exact same records a second time (a crash
        and replay) must not double-count: same (run_id, agent_id,
        seq) is the same call."""
        store = CostStore(db_path=tmp_path / "costs.db")
        records = [_record(), _record(seq="msg_second")]

        first = ingest_usage_records(store, "project-A", records)
        second = ingest_usage_records(store, "project-A", records)

        assert first.accepted == 2
        assert first.already_present == 0
        assert second.accepted == 0
        assert second.already_present == 2
        count = store.conn.execute(
            "SELECT COUNT(*) FROM token_events WHERE run_id = ?",
            ("20261105T213906",),
        ).fetchone()[0]
        assert count == 2

    def test_an_invalid_attribution_is_rejected_not_inserted(
        self, tmp_path: Path
    ) -> None:
        """A record with an attribution outside the agreed vocabulary
        is rejected with a reason, not silently coerced or inserted."""
        store = CostStore(db_path=tmp_path / "costs.db")

        ack = ingest_usage_records(
            store, "project-A", [_record(attribution="made_up_value")]
        )

        assert ack.accepted == 0
        assert ack.already_present == 0
        assert len(ack.rejected) == 1
        assert ack.rejected[0].index == 0
        assert "made_up_value" in ack.rejected[0].reason
        count = store.conn.execute("SELECT COUNT(*) FROM token_events").fetchone()[0]
        assert count == 0

    def test_a_mixed_batch_reports_each_outcome_separately(
        self, tmp_path: Path
    ) -> None:
        """One batch ack must distinguish accepted, already-present,
        and rejected counts, per record, not just a pass/fail total."""
        store = CostStore(db_path=tmp_path / "costs.db")
        ingest_usage_records(store, "project-A", [_record(seq="already-here")])

        ack = ingest_usage_records(
            store,
            "project-A",
            [
                _record(seq="already-here"),
                _record(seq="brand-new"),
                _record(seq="bad-one", reported_by="model"),
            ],
        )

        assert ack.accepted == 1
        assert ack.already_present == 1
        assert len(ack.rejected) == 1
        assert ack.rejected[0].index == 2

    def test_usd_is_optional(self, tmp_path: Path) -> None:
        """A record with no vendor-reported price (Minecraft's planner
        ledger has none) is still accepted; the kernel's own
        v_event_cost view prices it from token counts."""
        store = CostStore(db_path=tmp_path / "costs.db")

        ack = ingest_usage_records(
            store,
            "project-A",
            [_record(usd=None, priced_by=None, basis="attested")],
        )

        assert ack.accepted == 1
        row = store.conn.execute(
            "SELECT reported_usd FROM token_events WHERE run_id = ?",
            ("20261105T213906",),
        ).fetchone()
        assert row[0] is None


class TestRejectsMalformedInputByName:
    """Codex review (round 1 on #867): reject malformed input instead
    of defaulting it. A bad timestamp must not silently stamp "now"
    and still count as accepted, and a bad token count must not
    silently become 0 or crash the SQL layer -- both reject the
    record, by name, so a caller can see exactly what was wrong."""

    def test_empty_at_is_rejected(self, tmp_path: Path) -> None:
        """An empty timestamp must not fall through to the store's
        own "now" default and still be counted as accepted -- a
        replayed ledger with a bad timestamp would be recorded at the
        wrong time."""
        store = CostStore(db_path=tmp_path / "costs.db")

        ack = ingest_usage_records(store, "project-A", [_record(at="")])

        assert ack.accepted == 0
        assert len(ack.rejected) == 1
        assert "timestamp" in ack.rejected[0].reason
        count = store.conn.execute("SELECT COUNT(*) FROM token_events").fetchone()[0]
        assert count == 0

    def test_unparseable_at_is_rejected(self, tmp_path: Path) -> None:
        """A timestamp that doesn't parse as ISO-8601 is rejected the
        same way an empty one is."""
        store = CostStore(db_path=tmp_path / "costs.db")

        ack = ingest_usage_records(store, "project-A", [_record(at="not-a-timestamp")])

        assert ack.accepted == 0
        assert len(ack.rejected) == 1
        assert "timestamp" in ack.rejected[0].reason

    @pytest.mark.parametrize(
        "field,bad_value",
        [
            ("input_tokens", -1),
            ("input_tokens", True),
            ("input_tokens", 1.5),
            ("input_tokens", None),
            ("output_tokens", -1),
            ("output_tokens", True),
            ("output_tokens", 1.5),
            ("output_tokens", None),
            ("cache_read_tokens", -1),
            ("cache_read_tokens", True),
            ("cache_read_tokens", 1.5),
            ("cache_read_tokens", None),
            ("cache_creation_tokens", -1),
            ("cache_creation_tokens", True),
            ("cache_creation_tokens", 1.5),
            ("cache_creation_tokens", None),
            ("reasoning_tokens", -1),
            ("reasoning_tokens", True),
            ("reasoning_tokens", 1.5),
        ],
    )
    def test_a_bad_token_count_is_rejected(
        self, tmp_path: Path, field: str, bad_value: object
    ) -> None:
        """Every token-count field must be a non-negative int -- not a
        bool (Python's bool is an int subclass, so True/False would
        otherwise silently pass as 1/0), not a float, and (for the
        four required counts) never None."""
        store = CostStore(db_path=tmp_path / "costs.db")

        ack = ingest_usage_records(store, "project-A", [_record(**{field: bad_value})])

        assert ack.accepted == 0
        assert len(ack.rejected) == 1
        assert field in ack.rejected[0].reason
        count = store.conn.execute("SELECT COUNT(*) FROM token_events").fetchone()[0]
        assert count == 0

    @pytest.mark.parametrize(
        "bad_usd",
        [-0.01, float("nan"), float("inf"), float("-inf"), True],
    )
    def test_a_bad_usd_is_rejected(self, tmp_path: Path, bad_usd: object) -> None:
        """usd, when present, must be a finite non-negative float."""
        store = CostStore(db_path=tmp_path / "costs.db")

        ack = ingest_usage_records(store, "project-A", [_record(usd=bad_usd)])

        assert ack.accepted == 0
        assert len(ack.rejected) == 1
        assert "usd" in ack.rejected[0].reason

    def test_usd_none_is_still_fine(self, tmp_path: Path) -> None:
        """None stays a legitimate "no vendor price" value, not a
        rejection -- only a present-but-invalid usd is rejected."""
        store = CostStore(db_path=tmp_path / "costs.db")

        ack = ingest_usage_records(store, "project-A", [_record(usd=None)])

        assert ack.accepted == 1
        assert ack.rejected == []


class TestHeldWithNoTaskIdYet:
    """A harness that has not yet attributed a call to a task (board-
    call-log attribution runs after the fact) still has a legitimate
    "held" record with no task_id -- distinct from "between_tasks" or
    "controller", which force task_id to None regardless of input."""

    def test_held_with_no_task_id_is_accepted_and_only_in_by_agent(
        self, tmp_path: Path
    ) -> None:
        store = CostStore(db_path=tmp_path / "costs.db")

        ack = ingest_usage_records(
            store, "project-A", [_record(attribution="held", task_id=None)]
        )

        assert ack.accepted == 1
        row = store.conn.execute(
            "SELECT task_id, attribution FROM token_events WHERE run_id = ?",
            ("20261105T213906",),
        ).fetchone()
        assert row[0] is None
        assert row[1] == "held"

        ledger = read_run_ledger(store, "20261105T213906")
        assert ledger["by_agent"]["Bly_2139"]["calls"] == 1
        assert ledger["by_task"] == {}


class TestReadRunLedger:
    """Test suite for read_run_ledger."""

    def test_groups_by_agent_and_by_task(self, tmp_path: Path) -> None:
        """A run's ledger reads back grouped both ways, so measure.py
        and Cato can read one source instead of re-pricing transcripts."""
        store = CostStore(db_path=tmp_path / "costs.db")
        ingest_usage_records(
            store,
            "project-A",
            [
                _record(seq="1", agent_id="Bly_2139", task_id="task-1"),
                _record(seq="2", agent_id="Bly_2139", task_id="task-1"),
                _record(
                    seq="3",
                    agent_id="Bly_2139",
                    attribution="between_tasks",
                    task_id=None,
                ),
                _record(seq="4", agent_id="Ash_2139", task_id="task-2"),
            ],
        )

        ledger = read_run_ledger(store, "20261105T213906")

        assert ledger["by_agent"]["Bly_2139"]["calls"] == 3
        assert ledger["by_agent"]["Ash_2139"]["calls"] == 1
        assert ledger["by_task"]["task-1"]["calls"] == 2
        assert ledger["by_task"]["task-2"]["calls"] == 1
        assert "between_tasks" not in ledger["by_task"]
        assert None not in ledger["by_task"]
