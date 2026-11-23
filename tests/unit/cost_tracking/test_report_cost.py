"""
Unit tests for cost carried on a progress report (issue #742, via #939).

A worker that pays for its own model calls is the only party that knows
what a task cost. Before this, that number reached Marcus's cost ledger
(``token_events``) only through a batch import at the end of a run, so the
dashboard read $0.00 for the whole run. Now the worker puts its calls on the
report it already makes, ``report_task_progress(cost=[...])``, and the kernel
records them at once through ``ingest_usage_records``.

These tests drive ``record_report_cost`` against a real ``CostStore`` in a
temporary directory and pin the rules the contract states:

* the kernel stamps the agent, the task, the run and ``attribution="held"``;
  the worker supplies only what it knows about each call;
* a malformed record is refused by index with a reason, never silently;
* records are deduplicated by ``seq``, so resending is safe;
* nothing is recorded for a report the board refused, or for an agent with
  no project.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from src.cost_tracking.cost_store import CostStore, Run
from src.cost_tracking.report_cost import record_report_cost

pytestmark = pytest.mark.unit

_PROJECT = "0f3c2a9e1b7d4c55a6e8f90123456789"


def _call(seq: Any = 1, **overrides: Any) -> Dict[str, Any]:
    """Return one call record as a worker would send it."""
    record: Dict[str, Any] = {
        "seq": seq,
        "at": "2026-11-23T10:00:00Z",
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "input_tokens": 3012,
        "output_tokens": 410,
        "usd": 0.0382,
    }
    record.update(overrides)
    return record


@pytest.fixture
def store(tmp_path: Path) -> CostStore:
    """Return a cost store with one open run for the project."""
    from datetime import datetime, timezone

    cost_store = CostStore(db_path=tmp_path / "costs.db")
    cost_store.record_run(
        Run(run_id="run-1", project_id=_PROJECT, started_at=datetime.now(timezone.utc))
    )
    return cost_store


@pytest.fixture
def state(store: CostStore) -> SimpleNamespace:
    """Return server state with one agent registered to the project."""
    return SimpleNamespace(cost_store=store, agent_project_map={"finder-1": _PROJECT})


def _rows(store: CostStore) -> List[Dict[str, Any]]:
    """Read every token_events row back as a dict."""
    store.conn.row_factory = None
    cursor = store.conn.execute(
        "SELECT run_id, project_id, agent_id, task_id, attribution, basis,"
        " reported_by, input_tokens, output_tokens, reported_usd FROM token_events"
    )
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


class TestARecordedCall:
    """What the kernel stamps on a call it records."""

    def test_the_row_carries_the_kernels_stamps_and_the_workers_counts(
        self, state: SimpleNamespace, store: CostStore
    ) -> None:
        """Agent, task, run and attribution come from the kernel."""
        record_report_cost(state, "finder-1", "task-7", [_call()], accepted=True)

        assert _rows(store) == [
            {
                "run_id": "run-1",
                "project_id": _PROJECT,
                "agent_id": "finder-1",
                "task_id": "task-7",
                "attribution": "held",
                "basis": "attested",
                "reported_by": "agent",
                "input_tokens": 3012,
                "output_tokens": 410,
                "reported_usd": 0.0382,
            }
        ]

    def test_the_reply_acknowledges_the_batch(self, state: SimpleNamespace) -> None:
        """The worker learns how many calls were taken."""
        reply = record_report_cost(
            state, "finder-1", "task-7", [_call(1), _call(2)], accepted=True
        )

        assert (reply["recorded"], reply["accepted"], reply["already_present"]) == (
            True,
            2,
            0,
        )

    def test_a_resent_call_is_counted_once(
        self, state: SimpleNamespace, store: CostStore
    ) -> None:
        """Sending the same seq twice adds no second row."""
        record_report_cost(state, "finder-1", "task-7", [_call(1)], accepted=True)
        reply = record_report_cost(
            state, "finder-1", "task-7", [_call(1)], accepted=True
        )

        assert (reply["already_present"], len(_rows(store))) == (1, 1)

    def test_a_relay_may_say_it_relayed_the_vendors_usage_block(
        self, state: SimpleNamespace, store: CostStore
    ) -> None:
        """A harness can label its numbers as the vendor's, not the model's."""
        record_report_cost(
            state,
            "finder-1",
            "task-7",
            [_call(basis="vendor_usage_block", reported_by="harness")],
            accepted=True,
        )

        row = _rows(store)[0]
        assert (row["basis"], row["reported_by"]) == ("vendor_usage_block", "harness")

    def test_a_run_captured_before_the_report_wins_over_a_later_lookup(
        self, state: SimpleNamespace, store: CostStore
    ) -> None:
        """The run the report started in is the one its calls land in.

        Codex P2 on PR #1013: the last completion can drain the board and
        close the run before its cost is recorded, which filed the calls
        under ``unassigned`` and broke deduplication against calls already
        recorded under the real run.
        """
        store.close_latest_open_run_for_project(_PROJECT)

        reply = record_report_cost(
            state, "finder-1", "task-7", [_call()], accepted=True, run_id="run-1"
        )

        assert (reply["run_id"], _rows(store)[0]["run_id"]) == ("run-1", "run-1")

    def test_resolve_report_run_reads_the_open_run(
        self, state: SimpleNamespace
    ) -> None:
        """The capture is the same lookup the recorder would make."""
        from src.cost_tracking.report_cost import resolve_report_run

        assert resolve_report_run(state, "finder-1") == "run-1"

    def test_resolve_report_run_never_raises(self) -> None:
        """No project, no store or a broken store all give ``None``."""
        from unittest.mock import Mock

        from src.cost_tracking.report_cost import resolve_report_run

        broken = Mock()
        broken.get_latest_open_run_for_project.side_effect = RuntimeError("x")
        states = [
            SimpleNamespace(cost_store=broken, agent_project_map={"a": _PROJECT}),
            SimpleNamespace(cost_store=None, agent_project_map={"a": _PROJECT}),
            SimpleNamespace(cost_store=broken, agent_project_map={}),
        ]

        assert [resolve_report_run(s, "a") for s in states] == [None, None, None]

    def test_a_project_with_no_open_run_records_under_unassigned(
        self, tmp_path: Path
    ) -> None:
        """The same fallback the planner's own calls use."""
        store = CostStore(db_path=tmp_path / "costs.db")
        state = SimpleNamespace(
            cost_store=store, agent_project_map={"finder-1": _PROJECT}
        )

        reply = record_report_cost(
            state, "finder-1", "task-7", [_call()], accepted=True
        )

        assert reply["run_id"] == "unassigned"


class TestARefusedRecord:
    """A record that cannot be taken is named, and the rest still land."""

    @pytest.mark.parametrize(
        "bad, reason",
        [
            (_call(seq=None), "seq"),
            (_call(model=""), "model"),
            (_call(at="yesterday"), "timestamp"),
            (_call(input_tokens=-1), "input_tokens"),
            (_call(basis="metered"), "basis"),
            (_call(reported_by="controller"), "reported_by"),
            (_call(task_id="other"), "task_id"),
            ("not a dict", "object"),
            # Codex P2 on PR #1013: an unhashable value once raised inside
            # the set-membership check and dropped the whole batch.
            (_call(basis=[]), "basis"),
            (_call(reported_by={}), "reported_by"),
            (_call(api=["x"]), "api"),
            (_call(priced_by={"t": 1}), "priced_by"),
        ],
    )
    def test_the_refusal_names_the_record_and_the_field(
        self, state: SimpleNamespace, bad: Any, reason: str
    ) -> None:
        """Index 1 is refused with a reason naming the field; index 0 lands."""
        reply = record_report_cost(
            state, "finder-1", "task-7", [_call(1), bad], accepted=True
        )

        assert (reply["accepted"], [r["index"] for r in reply["rejected"]]) == (1, [1])
        assert reason in reply["rejected"][0]["reason"]

    def test_a_cost_that_is_not_a_list_is_refused_whole(
        self, state: SimpleNamespace, store: CostStore
    ) -> None:
        """One dict instead of a list of calls is a shape error, not a call."""
        reply = record_report_cost(
            state, "finder-1", "task-7", _call(), accepted=True  # type: ignore[arg-type]
        )

        assert (reply["recorded"], len(_rows(store))) == (False, 0)


class TestNothingIsRecorded:
    """Cost rides on accepted work by a registered agent, nothing else."""

    def test_a_refused_report_records_nothing_and_says_to_resend(
        self, state: SimpleNamespace, store: CostStore
    ) -> None:
        """The same rule as the opinion marker: refused work records nothing."""
        reply = record_report_cost(
            state, "finder-1", "task-7", [_call()], accepted=False
        )

        assert (reply["recorded"], len(_rows(store))) == (False, 0)
        assert "again" in reply["reason"]

    def test_an_agent_with_no_project_records_nothing(
        self, state: SimpleNamespace, store: CostStore
    ) -> None:
        """No registration, no project to attribute the spend to."""
        reply = record_report_cost(
            state, "stranger", "task-7", [_call()], accepted=True
        )

        assert (reply["recorded"], len(_rows(store))) == (False, 0)

    def test_a_failing_cost_store_is_reported_never_raised(self) -> None:
        """A broken ledger never fails the progress report it rides on."""
        from unittest.mock import Mock

        broken = Mock()
        broken.get_latest_open_run_for_project.side_effect = RuntimeError("disk full")
        state = SimpleNamespace(
            cost_store=broken, agent_project_map={"finder-1": _PROJECT}
        )

        reply = record_report_cost(
            state, "finder-1", "task-7", [_call()], accepted=True
        )

        assert (reply["recorded"], "disk full" in reply["reason"]) == (False, True)

    def test_a_server_with_no_cost_store_records_nothing(self) -> None:
        """Said in the reply, never raised into the report."""
        state = SimpleNamespace(
            cost_store=None, agent_project_map={"finder-1": _PROJECT}
        )

        reply = record_report_cost(
            state, "finder-1", "task-7", [_call()], accepted=True
        )

        assert reply["recorded"] is False
