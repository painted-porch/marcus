"""
Unit tests for human-only approval (#737, step 5).

An approve task closes only when ``report_task_progress`` is called by
a principal of type human; any other caller receives a structured
rejection. No new task status exists: dependents wait through the
existing dependency resolution, so the synthesize task is not offered
until the approval closes.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, List, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import Priority, Task, TaskStatus, WorkerStatus
from src.marcus_mcp.tools import task as task_module

pytestmark = pytest.mark.unit

PROJECT_ID = "tow-pilot"


def _make_task(
    task_id: str,
    task_type: str = "approve",
    status: TaskStatus = TaskStatus.TODO,
    dependencies: Optional[List[str]] = None,
) -> Task:
    """Build a minimal Task for approval tests."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        dependencies=dependencies or [],
        labels=[],
        project_id=PROJECT_ID,
        task_type=task_type,
    )


def _make_worker(worker_id: str, principal: str = "agent") -> WorkerStatus:
    """Build a minimal WorkerStatus."""
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
        principal=principal,
    )


def _make_state(tasks: List[Task], workers: List[WorkerStatus]) -> Mock:
    """Build a Mock server state rich enough for path 1 assignment."""
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


class TestApprovalGate:
    """Test suite for _apply_approval_gate."""

    def test_agent_principal_cannot_close_an_approve_task(self) -> None:
        """An agent calling completed on an approve task is refused."""
        approve = _make_task("approve-1")
        state = _make_state([approve], [_make_worker("verifier-gem-1")])

        response = task_module._apply_approval_gate(approve, "verifier-gem-1", state)

        assert response is not None
        assert response["success"] is False
        assert response["error"] == "approval_requires_human"
        assert "human" in response["blocker"]

    def test_human_principal_can_close_an_approve_task(self) -> None:
        """A human caller passes the gate; the completion proceeds."""
        approve = _make_task("approve-1")
        state = _make_state([approve], [_make_worker("larry", "human")])

        response = task_module._apply_approval_gate(approve, "larry", state)

        assert response is None

    def test_unregistered_caller_cannot_close_an_approve_task(self) -> None:
        """A caller with no registration is refused, not trusted."""
        approve = _make_task("approve-1")
        state = _make_state([approve], [])

        response = task_module._apply_approval_gate(approve, "ghost", state)

        assert response is not None
        assert response["error"] == "approval_requires_human"

    def test_non_approve_tasks_are_not_gated(self) -> None:
        """The gate only guards the approve type."""
        find = _make_task("find-1", task_type="find")
        state = _make_state([find], [_make_worker("finder-pplx-1")])

        response = task_module._apply_approval_gate(find, "finder-pplx-1", state)

        assert response is None


class TestDependentsWaitForApproval:
    """Test suite for the synthesize task held behind the approval."""

    @pytest.mark.asyncio
    async def test_synthesize_is_not_offered_while_approval_is_open(
        self,
    ) -> None:
        """Dependency resolution holds the synthesize task, no new status."""
        approve = _make_task("approve-1", status=TaskStatus.TODO)
        synthesize = _make_task(
            "synth-1", task_type="synthesize", dependencies=["approve-1"]
        )
        state = _make_state([approve, synthesize], [_make_worker("verifier-gem-1")])

        result = await task_module._find_optimal_task_original_logic(
            agent_id="verifier-gem-1", state=state
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_synthesize_is_offered_after_approval_closes(self) -> None:
        """Once the approve task is DONE, the synthesize task flows."""
        approve = _make_task("approve-1", status=TaskStatus.DONE)
        synthesize = _make_task(
            "synth-1", task_type="synthesize", dependencies=["approve-1"]
        )
        state = _make_state([approve, synthesize], [_make_worker("verifier-gem-1")])

        result = await task_module._find_optimal_task_original_logic(
            agent_id="verifier-gem-1", state=state
        )

        assert result is not None
        assert result.id == synthesize.id
