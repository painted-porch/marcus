"""
Unit tests for reporting frontier width on every ``request_next_task``
reply, not only the one with no work to hand out (#804).

``compute_board_drain`` used to run only in the no-task branch of
``request_next_task``, so the count of claimable tasks — the measure of
whether a plan is actually parallel — was discarded exactly when the
board is healthy and nobody starves. A consumer could only ever see the
frontier collapse, never see it wide.

``_frontier_for_agent`` is the extracted, independently testable half of
the fix: both call sites (the assignment-success branch and, unchanged,
the no-work branch) go through ``compute_board_drain`` as the single
rule, so the two numbers can never diverge. These tests exercise the
helper directly rather than the much larger ``request_next_task``, which
needs AI-engine, lease and kanban mocking far beyond what this change
touches.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.task import _frontier_for_agent

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
PROJECT = "proj-1"


def _task(
    task_id: str,
    status: TaskStatus = TaskStatus.TODO,
    *,
    deps: Optional[List[str]] = None,
    assigned_to: Optional[str] = None,
    project_id: Optional[str] = PROJECT,
) -> Task:
    """Build a task with the fields ``compute_board_drain`` reads.

    Parameters
    ----------
    task_id : str
        The task's id.
    status : TaskStatus
        Its board status.
    deps : Optional[List[str]]
        Ids this task depends on.
    assigned_to : Optional[str]
        An agent name, if the card names one.
    project_id : Optional[str]
        The project this task belongs to; ``None`` is the legacy,
        shared case ``compute_board_drain`` also scopes in.

    Returns
    -------
    Task
        A task suitable for ``compute_board_drain``.
    """
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        labels=[],
        dependencies=deps or [],
        estimated_hours=1.0,
        assigned_to=assigned_to,
        created_at=NOW,
        updated_at=NOW,
        due_date=None,
        project_id=project_id,
    )


def _state(
    tasks: List[Task],
    agent_project_map: Optional[Dict[str, str]] = None,
) -> Mock:
    """Assemble the server state ``_frontier_for_agent`` and
    ``compute_board_drain`` read.

    Parameters
    ----------
    tasks : List[Task]
        The project's tasks.
    agent_project_map : Optional[Dict[str, str]]
        Agent-to-project registrations.

    Returns
    -------
    Mock
        A state object with the fields both functions touch.
    """
    state = Mock()
    state.project_tasks = tasks
    state.agent_project_map = dict(agent_project_map or {})
    state.tasks_being_assigned = set()
    state.lease_manager = None
    # A bare Mock() auto-vivifies any attribute as a new, truthy Mock,
    # so leaving this unset would make compute_board_drain's
    # ``_is_decomposed_parent`` treat every task as a decomposed
    # parent (every task "has subtasks", since ``manager.has_subtasks``
    # also auto-vivifies truthy) and exclude it from claimable.
    # ``tests/unit/marcus_mcp/test_drain_signal.py`` sets this the same
    # way for the same reason.
    state.subtask_manager = None
    state.assignment_persistence = Mock()
    state.assignment_persistence.get_all_assigned_task_ids = AsyncMock(
        return_value=set()
    )
    return state


class TestFrontierReflectsTheRealBoard:
    """The count is the board's, not a guess."""

    @pytest.mark.asyncio
    async def test_a_wide_frontier_is_reported(self) -> None:
        """Five independent TODO tasks: five claimable."""
        tasks = [_task(f"t{i}") for i in range(5)]
        state = _state(tasks, {"alice": PROJECT})

        frontier = await _frontier_for_agent("alice", state)

        assert frontier == {"tasks_claimable": 5, "tasks_in_flight": 0}

    @pytest.mark.asyncio
    async def test_a_narrowing_frontier_is_reported_each_time(self) -> None:
        """The series a run would actually see: 5, then 3, then 1.

        This is the worked example in #804: the number that was
        invisible on every reply except the last.
        """
        all_tasks = [_task(f"t{i}") for i in range(5)]
        state = _state(all_tasks, {"alice": PROJECT})
        assert (await _frontier_for_agent("alice", state))["tasks_claimable"] == 5

        for t in all_tasks[:2]:
            t.status = TaskStatus.DONE
        assert (await _frontier_for_agent("alice", state))["tasks_claimable"] == 3

        for t in all_tasks[2:4]:
            t.status = TaskStatus.DONE
        assert (await _frontier_for_agent("alice", state))["tasks_claimable"] == 1

    @pytest.mark.asyncio
    async def test_in_flight_work_is_counted_separately(self) -> None:
        """A reserved task is in flight, not claimable, and both show."""
        tasks = [_task("t1"), _task("t2", assigned_to="bob")]
        state = _state(tasks, {"alice": PROJECT})
        state.tasks_being_assigned = {"t2"}

        frontier = await _frontier_for_agent("alice", state)

        assert frontier == {"tasks_claimable": 1, "tasks_in_flight": 1}


class TestItUsesTheAgentsOwnProject:
    """The #805 lesson, applied here: the active project is not the source."""

    @pytest.mark.asyncio
    async def test_scoped_to_the_callers_project_not_another_ones(self) -> None:
        """A task in a different project must not inflate the count."""
        tasks = [
            _task("a1", project_id="proj-a"),
            _task("b1", project_id="proj-b"),
            _task("b2", project_id="proj-b"),
        ]
        state = _state(tasks, {"alice": "proj-a"})

        frontier = await _frontier_for_agent("alice", state)

        assert frontier == {"tasks_claimable": 1, "tasks_in_flight": 0}

    @pytest.mark.asyncio
    async def test_an_unregistered_agent_gets_no_frontier(self) -> None:
        """No registration, no project to scope the count to — None, not 0.

        Zero would be a lie: it would claim the board is drained for
        this agent when the truth is this agent is not known to any
        project at all.
        """
        state = _state([_task("t1")], agent_project_map={})

        assert await _frontier_for_agent("stranger", state) is None

    @pytest.mark.asyncio
    async def test_a_state_with_no_registration_map_at_all_is_handled(self) -> None:
        """Older callers/tests may pass a state with no agent_project_map."""
        state = Mock(spec=["project_tasks", "tasks_being_assigned"])
        state.project_tasks = [_task("t1")]
        state.tasks_being_assigned = set()

        assert await _frontier_for_agent("alice", state) is None


class TestItDegradesRatherThanBlockingAnAssignment:
    """Observational: a failure here must never cost an agent its task."""

    @pytest.mark.asyncio
    async def test_a_drain_computation_failure_returns_none(self) -> None:
        """The caller (request_next_task) must still be able to proceed."""
        state = _state([_task("t1")], {"alice": PROJECT})
        state.assignment_persistence.get_all_assigned_task_ids = AsyncMock(
            side_effect=RuntimeError("store unavailable")
        )

        # compute_board_drain itself degrades a persistence failure to
        # "unknown, not idle" rather than raising, so simulate a harder
        # failure to prove the helper's own try/except is load-bearing.
        import src.marcus_mcp.tools.task as task_module

        original = task_module.compute_board_drain

        async def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("drain computation exploded")

        task_module.compute_board_drain = _boom  # type: ignore[assignment]
        try:
            result = await _frontier_for_agent("alice", state)
        finally:
            task_module.compute_board_drain = original  # type: ignore[assignment]

        assert result is None


class TestItMatchesTheSameRuleTheNoWorkPathUses:
    """One rule, not two, per the issue's own verification step 4."""

    @pytest.mark.asyncio
    async def test_the_same_numbers_a_direct_drain_call_would_give(self) -> None:
        """``_frontier_for_agent`` is not a second implementation."""
        from src.marcus_mcp.coordinator.drain import compute_board_drain

        tasks = [_task("t1"), _task("t2", status=TaskStatus.DONE)]
        state = _state(tasks, {"alice": PROJECT})

        frontier = await _frontier_for_agent("alice", state)
        verdict = await compute_board_drain(state, PROJECT)

        assert frontier == {
            "tasks_claimable": verdict.tasks_claimable,
            "tasks_in_flight": verdict.tasks_in_flight,
        }
