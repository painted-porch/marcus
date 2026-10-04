"""
Unit tests for eligibility wiring in both assignment entry points (#737, step 3).

The rules live in one module (``src/marcus_mcp/coordinator/eligibility.py``);
these tests prove each entry point actually calls them, because a rule
that lands in only one entry point does not exist (the M2 lesson from
issue #706).  Path 1 is ``_find_optimal_task_original_logic`` in
``src/marcus_mcp/tools/task.py``; path 2 is the subtask loop in
``src/marcus_mcp/coordinator/subtask_assignment.py`` fed by
``find_optimal_task_with_subtasks``.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, List, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import Priority, Task, TaskStatus, WorkerStatus
from src.marcus_mcp.coordinator.subtask_assignment import (
    find_next_available_subtask,
)
from src.marcus_mcp.coordinator.task_assignment_integration import (
    _build_eligibility_check,
)
from src.marcus_mcp.tools import task as task_module

pytestmark = pytest.mark.unit

PROJECT_ID = "tow-pilot"


def _make_task(
    task_id: str,
    name: str,
    task_type: str = "implement",
    status: TaskStatus = TaskStatus.TODO,
    assigned_to: Optional[str] = None,
    dependencies: Optional[List[str]] = None,
    is_subtask: bool = False,
    parent_task_id: Optional[str] = None,
    subtask_index: Optional[int] = None,
) -> Task:
    """Build a minimal Task for wiring tests (no labels, neutral name)."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=assigned_to,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        dependencies=dependencies or [],
        labels=[],
        project_id=PROJECT_ID,
        is_subtask=is_subtask,
        parent_task_id=parent_task_id,
        subtask_index=subtask_index,
        task_type=task_type,
    )


def _make_worker(
    worker_id: str,
    vendor: str = "",
    principal: str = "agent",
) -> WorkerStatus:
    """Build a minimal WorkerStatus for wiring tests."""
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
    """Build a Mock server state rich enough to run the full path 1 flow."""
    state = Mock()
    state.project_tasks = tasks
    state.agent_status = {w.worker_id: w for w in workers}
    state.agent_project_map = {w.worker_id: PROJECT_ID for w in workers}
    state.agent_tasks = {}
    state.tasks_being_assigned = set()
    state.project_state = Mock()
    state.ai_engine = None
    state.subtask_manager = None
    state.assignment_lock = asyncio.Lock()
    state.assignment_persistence = Mock()
    state.assignment_persistence.get_all_assigned_task_ids = AsyncMock(
        return_value=set()
    )
    state.eligibility_refusals = []
    return state


def _tow_board() -> tuple[Mock, Task]:
    """One closed find task by finder-pplx-1 and its open verify task."""
    find = _make_task(
        "find-1",
        "Locate source for excerpt 1",
        task_type="find",
        status=TaskStatus.DONE,
        assigned_to="finder-pplx-1",
    )
    verify = _make_task(
        "verify-1",
        "Cross-check claim for excerpt 1",
        task_type="verify",
        dependencies=["find-1"],
    )
    workers = [
        _make_worker("finder-pplx-1", vendor="perplexity"),
        _make_worker("verifier-gem-1", vendor="google"),
        _make_worker("larry", principal="human"),
    ]
    state = _make_state([find, verify], workers)
    return state, verify


class TestOriginalLogicPathEnforcesEligibility:
    """Regression tests for path 1 (_find_optimal_task_original_logic)."""

    @pytest.mark.asyncio
    async def test_author_gets_silent_no_task_not_its_own_verify_task(
        self,
    ) -> None:
        """The author polling sees plain 'no task', never a reason.

        The return type is Optional[Task], so a refusal is
        indistinguishable from an empty board to the agent; the
        reason lives only in the server-side refusal log.
        """
        state, _ = _tow_board()

        result = await task_module._find_optimal_task_original_logic(
            agent_id="finder-pplx-1", state=state
        )

        assert result is None
        assert len(state.eligibility_refusals) == 1
        assert "author" in state.eligibility_refusals[0]["reason"]

    @pytest.mark.asyncio
    async def test_different_vendor_verifier_is_offered_the_verify_task(
        self,
    ) -> None:
        """Positive control: the filter does not over-block path 1."""
        state, verify = _tow_board()

        result = await task_module._find_optimal_task_original_logic(
            agent_id="verifier-gem-1", state=state
        )

        assert result is not None
        assert result.id == verify.id

    @pytest.mark.asyncio
    async def test_human_is_not_offered_a_find_task_on_path_1(self) -> None:
        """A human principal polling never receives a non-approve task."""
        find = _make_task("find-2", "Locate source for excerpt 2", task_type="find")
        state = _make_state([find], [_make_worker("larry", principal="human")])

        result = await task_module._find_optimal_task_original_logic(
            agent_id="larry", state=state
        )

        assert result is None
        assert len(state.eligibility_refusals) == 1


class TestSubtaskPathEnforcesEligibility:
    """Regression tests for path 2 (the coordinator's subtask loop)."""

    def _subtask_board(self) -> tuple[List[Task], Task, Task]:
        """A verify parent task with one claimable subtask."""
        find = _make_task(
            "find-1",
            "Locate source for excerpt 1",
            task_type="find",
            status=TaskStatus.DONE,
            assigned_to="finder-pplx-1",
        )
        parent = _make_task(
            "verify-1",
            "Cross-check claim for excerpt 1",
            task_type="verify",
            dependencies=["find-1"],
        )
        sub = _make_task(
            "verify-1-sub-1",
            "Fetch the claimed URL",
            task_type="verify",
            is_subtask=True,
            parent_task_id="verify-1",
            subtask_index=1,
        )
        return [find, parent, sub], parent, sub

    def test_refused_candidate_is_skipped_in_the_subtask_loop(self) -> None:
        """A candidate the eligibility check refuses is never returned."""
        tasks, _, _ = self._subtask_board()

        result = find_next_available_subtask(
            agent_id="finder-pplx-1",
            project_tasks=tasks,
            subtask_manager=Mock(),
            assigned_task_ids=set(),
            eligibility_check=lambda subtask, parent: False,
        )

        assert result is None

    def test_allowed_candidate_still_flows_through(self) -> None:
        """Positive control: an accepting check changes nothing."""
        tasks, _, sub = self._subtask_board()

        result = find_next_available_subtask(
            agent_id="verifier-gem-1",
            project_tasks=tasks,
            subtask_manager=Mock(),
            assigned_task_ids=set(),
            eligibility_check=lambda subtask, parent: True,
        )

        assert result is not None
        assert result.id == sub.id

    def test_omitting_the_check_keeps_legacy_behavior(self) -> None:
        """Existing callers that pass no check are untouched."""
        tasks, _, sub = self._subtask_board()

        result = find_next_available_subtask(
            agent_id="anyone",
            project_tasks=tasks,
            subtask_manager=Mock(),
            assigned_task_ids=set(),
        )

        assert result is not None
        assert result.id == sub.id

    def test_built_check_refuses_the_author_via_the_parent(self) -> None:
        """The integration glue evaluates subtask AND parent rules.

        A verify parent's subtask must never reach the claim's author,
        even though the subtask itself carries no dependency on the
        find task.
        """
        tasks, parent, sub = self._subtask_board()
        state = _make_state(
            tasks,
            [
                _make_worker("finder-pplx-1", vendor="perplexity"),
                _make_worker("verifier-gem-1", vendor="google"),
            ],
        )

        author_check = _build_eligibility_check("finder-pplx-1", state)
        other_check = _build_eligibility_check("verifier-gem-1", state)

        assert author_check is not None
        assert other_check is not None
        assert author_check(sub, parent) is False
        assert other_check(sub, parent) is True

    def test_built_check_is_none_for_unregistered_agent(self) -> None:
        """No WorkerStatus means no check; existing behavior is kept."""
        tasks, _, _ = self._subtask_board()
        state = _make_state(tasks, [])

        assert _build_eligibility_check("ghost", state) is None
