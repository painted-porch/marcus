"""
Unit tests for eligibility at assignment (issue #737, step 3).

Eligibility is the rule the board applies before offering a task to a
worker.  The rules: a verify task is never offered to the author of the
claim it depends on, nor to any worker from the author's vendor; an
approve task is offered only to a human principal; every other task
type is offered only to an agent principal, so a person is offered
approve tasks and nothing else.  These tests cover the pure module
only; wiring into the two assignment entry points is reviewed and
tested separately.
"""

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional
from unittest.mock import Mock

import pytest

from src.core.models import Priority, Task, TaskStatus, WorkerStatus
from src.marcus_mcp.coordinator.eligibility import (
    filter_eligible_tasks,
    is_eligible,
)

pytestmark = pytest.mark.unit


def _make_task(
    task_id: str,
    task_type: str = "implement",
    dependencies: Optional[List[str]] = None,
    status: TaskStatus = TaskStatus.TODO,
    assigned_to: Optional[str] = None,
) -> Task:
    """Build a minimal Task for eligibility checks."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=assigned_to,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        dependencies=dependencies or [],
        task_type=task_type,
    )


def _make_worker(
    worker_id: str,
    vendor: str = "",
    principal: str = "agent",
) -> WorkerStatus:
    """Build a minimal WorkerStatus for eligibility checks."""
    return WorkerStatus(
        worker_id=worker_id,
        name=worker_id,
        role="worker",
        email=None,
        current_tasks=[],
        completed_tasks_count=0,
        capacity=40,
        skills=[],
        availability={},
        vendor=vendor,
        principal=principal,
    )


def _make_state(tasks: List[Task], workers: List[WorkerStatus]) -> Mock:
    """Build a Mock server state holding tasks and registered workers."""
    state = Mock()
    state.project_tasks = tasks
    state.agent_status = {w.worker_id: w for w in workers}
    return state


def _tow_case() -> tuple[Task, Task, Mock]:
    """Build the worked case from the design: closed find, open verify.

    finder-pplx-1 (vendor perplexity) authored the claim; the verify
    task depends on the closed find task.
    """
    find = _make_task(
        "find-1",
        task_type="find",
        status=TaskStatus.DONE,
        assigned_to="finder-pplx-1",
    )
    verify = _make_task("verify-1", task_type="verify", dependencies=["find-1"])
    workers = [
        _make_worker("finder-pplx-1", vendor="perplexity"),
        _make_worker("verifier-pplx-2", vendor="perplexity"),
        _make_worker("verifier-gem-1", vendor="google"),
    ]
    state = _make_state([find, verify], workers)
    return find, verify, state


class TestVerifyEligibility:
    """Test suite for the verify-task rules: never author, never author's vendor."""

    def test_author_is_refused_its_own_verify_task(self) -> None:
        """The claim's author is never offered the dependent verify task."""
        _, verify, state = _tow_case()
        author = state.agent_status["finder-pplx-1"]

        eligible, reason = is_eligible(author, verify, state)

        assert eligible is False
        assert "author" in reason

    def test_same_vendor_worker_is_refused(self) -> None:
        """A worker from the author's vendor is never offered the verify task."""
        _, verify, state = _tow_case()
        same_vendor = state.agent_status["verifier-pplx-2"]

        eligible, reason = is_eligible(same_vendor, verify, state)

        assert eligible is False
        assert "vendor" in reason

    def test_different_vendor_worker_is_offered(self) -> None:
        """A worker from a different vendor is eligible for the verify task."""
        _, verify, state = _tow_case()
        different_vendor = state.agent_status["verifier-gem-1"]

        eligible, reason = is_eligible(different_vendor, verify, state)

        assert eligible is True
        assert reason == ""

    def test_verify_without_done_find_dependency_passes(self) -> None:
        """With no closed find dependency there is no author to protect.

        Dependency resolution holds the verify task until the find task
        closes, so this only matters for hand-seeded or unusual boards;
        eligibility must not crash or refuse everyone.
        """
        verify = _make_task("verify-1", task_type="verify", dependencies=["find-1"])
        worker = _make_worker("verifier-gem-1", vendor="google")
        state = _make_state([verify], [worker])

        eligible, reason = is_eligible(worker, verify, state)

        assert eligible is True

    def test_empty_vendor_does_not_match_empty_vendor(self) -> None:
        """Two workers with no declared vendor are not 'the same vendor'.

        The author-refusal still applies; the vendor rule must not
        collapse every vendorless worker into one blocked pool.
        """
        find = _make_task(
            "find-1",
            task_type="find",
            status=TaskStatus.DONE,
            assigned_to="finder-a",
        )
        verify = _make_task("verify-1", task_type="verify", dependencies=["find-1"])
        author = _make_worker("finder-a", vendor="")
        other = _make_worker("verifier-b", vendor="")
        state = _make_state([find, verify], [author, other])

        eligible, _ = is_eligible(other, verify, state)

        assert eligible is True


class TestPrincipalEligibility:
    """Test suite for the principal rules: approve is human-only, the rest agent-only."""

    def test_agent_is_refused_an_approve_task(self) -> None:
        """An agent principal is never offered an approve task."""
        approve = _make_task("approve-1", task_type="approve")
        agent = _make_worker("verifier-gem-1", vendor="google", principal="agent")
        state = _make_state([approve], [agent])

        eligible, reason = is_eligible(agent, approve, state)

        assert eligible is False
        assert "human" in reason

    def test_human_is_offered_an_approve_task(self) -> None:
        """A human principal is eligible for an approve task."""
        approve = _make_task("approve-1", task_type="approve")
        human = _make_worker("larry", principal="human")
        state = _make_state([approve], [human])

        eligible, reason = is_eligible(human, approve, state)

        assert eligible is True
        assert reason == ""

    @pytest.mark.parametrize("task_type", ["find", "verify", "synthesize", "implement"])
    def test_human_is_refused_every_other_task_type(self, task_type: str) -> None:
        """A human principal is offered approve tasks and nothing else."""
        task = _make_task("t-1", task_type=task_type)
        human = _make_worker("larry", principal="human")
        state = _make_state([task], [human])

        eligible, reason = is_eligible(human, task, state)

        assert eligible is False
        assert "agent" in reason

    @pytest.mark.parametrize("task_type", ["find", "synthesize", "implement"])
    def test_agent_passes_non_verify_non_approve_types(self, task_type: str) -> None:
        """An agent principal is eligible for the non-gated task types."""
        task = _make_task("t-1", task_type=task_type)
        agent = _make_worker("finder-pplx-1", vendor="perplexity")
        state = _make_state([task], [agent])

        eligible, reason = is_eligible(agent, task, state)

        assert eligible is True
        assert reason == ""


class TestFilterEligibleTasks:
    """Test suite for the shared filter both assignment entry points draw from."""

    def test_filter_removes_ineligible_and_keeps_eligible(self) -> None:
        """The filter drops the tasks the worker may not take."""
        find_done, verify, state = _tow_case()
        implement = _make_task("impl-1", task_type="implement")
        state.project_tasks = [find_done, verify, implement]
        author = state.agent_status["finder-pplx-1"]

        result = filter_eligible_tasks(author, [verify, implement], state)

        assert result == [implement]

    def test_filter_logs_each_refusal_with_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Every refusal is logged with the worker, task, and reason."""
        _, verify, state = _tow_case()
        author = state.agent_status["finder-pplx-1"]

        with caplog.at_level(logging.INFO):
            filter_eligible_tasks(author, [verify], state)

        assert any(
            "finder-pplx-1" in r.message
            and "verify-1" in r.message
            and "author" in r.message
            for r in caplog.records
        )

    def test_filter_records_refusals_on_state_for_the_audit_bundle(self) -> None:
        """Refusals are kept on state so export_case (step 8) can show them."""
        _, verify, state = _tow_case()
        state.eligibility_refusals = []
        same_vendor = state.agent_status["verifier-pplx-2"]

        filter_eligible_tasks(same_vendor, [verify], state)

        assert len(state.eligibility_refusals) == 1
        refusal = state.eligibility_refusals[0]
        assert refusal["agent_id"] == "verifier-pplx-2"
        assert refusal["task_id"] == "verify-1"
        assert "vendor" in refusal["reason"]
        assert refusal["count"] == 1
        assert "first_refused_at" in refusal
        assert "last_refused_at" in refusal

    def test_repeated_refusal_dedups_into_one_entry_with_count(self) -> None:
        """Polling workers refuse the same way every ~30s; dedup bounds state.

        The same (agent, task, reason) triple updates the existing
        entry's count and last_refused_at instead of appending, so an
        hour of polling is one row, not ~120.
        """
        _, verify, state = _tow_case()
        state.eligibility_refusals = []
        author = state.agent_status["finder-pplx-1"]

        filter_eligible_tasks(author, [verify], state)
        filter_eligible_tasks(author, [verify], state)

        assert len(state.eligibility_refusals) == 1
        refusal = state.eligibility_refusals[0]
        assert refusal["count"] == 2
        assert refusal["last_refused_at"] >= refusal["first_refused_at"]

    def test_refusals_by_different_agents_stay_separate_entries(self) -> None:
        """Dedup keys on (agent, task, reason); other agents get own rows."""
        _, verify, state = _tow_case()
        state.eligibility_refusals = []
        author = state.agent_status["finder-pplx-1"]
        same_vendor = state.agent_status["verifier-pplx-2"]

        filter_eligible_tasks(author, [verify], state)
        filter_eligible_tasks(same_vendor, [verify], state)

        assert len(state.eligibility_refusals) == 2

    def test_filter_passes_through_for_a_non_worker_status_agent(self) -> None:
        """A non-WorkerStatus identity is not judged, only passed through.

        Registration always stores WorkerStatus; test doubles and
        legacy callers must keep their pre-#737 behavior instead of
        being refused on attributes that are not real.
        """
        verify = _make_task("verify-1", task_type="verify")
        state = _make_state([verify], [])

        result = filter_eligible_tasks(Mock(), [verify], state)

        assert result == [verify]

    def test_filter_passes_everything_for_an_unregistered_worker(self) -> None:
        """A worker object with plain defaults filters like any agent.

        The filter never raises on missing state entries; eligibility
        is a refusal to offer, not an exception path.
        """
        implement = _make_task("impl-1", task_type="implement")
        worker = _make_worker("unknown-agent")
        state = _make_state([implement], [])

        result = filter_eligible_tasks(worker, [implement], state)

        assert result == [implement]
