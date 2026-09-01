"""
Unit tests pinning the ``retry_after_seconds`` contract.

Background
----------
``calculate_retry_after_seconds`` computed a retry interval from the
blocking task's ETA, floored it at 30, and then *ceilinged* it at 30 —
so the ETA computation was dead and every caller received exactly 30.

That reads like a clamp bug. It is not. PR #177 ("reduce agent retry wait
times from 300s to 30s for better parallelization") deliberately made the
wait a constant: long waits left idle agents missing work as tasks became
available. The constant is the intent.

What was actually wrong is that the code did not say so. The docstring
still promised "caps at 5 minutes" and "max 300s", the ETA arithmetic
survived as dead code that read as though it did something, and nothing
pinned the intended value — so the next reader either "fixes" the clamp
and silently restores 300s behaviour, or leaves it and keeps propagating
a docstring that lies.

These tests pin the intent so a future change to it has to be deliberate.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import Priority, Task, TaskAssignment, TaskStatus

pytestmark = pytest.mark.unit


def _task(task_id: str, status: TaskStatus = TaskStatus.IN_PROGRESS) -> Task:
    """
    Build a task for the retry calculation.

    Returns
    -------
    Task
        A task with a long estimate, so any surviving ETA arithmetic
        would produce a value far above the intended constant.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="Fixture",
        status=status,
        priority=Priority.MEDIUM,
        estimated_hours=40.0,
        dependencies=[],
        labels=[],
        assigned_to="agent-1",
        created_at=now,
        updated_at=now,
        due_date=None,
    )


def _state_with_task_in_progress() -> Mock:
    """
    Build server state with one long-running IN_PROGRESS task.

    Returns
    -------
    Mock
        State suitable for ``calculate_retry_after_seconds``.
    """
    from datetime import datetime, timezone

    task = _task("task-1")
    assignment = Mock(spec=TaskAssignment)
    assignment.task_id = "task-1"
    assignment.assigned_at = datetime.now(timezone.utc)

    state = Mock()
    state.agent_tasks = {"agent-1": assignment}
    state.project_tasks = [task]
    state.agent_status = {"agent-1": Mock(), "agent-2": Mock()}
    state.memory = None
    return state


class TestRetryAfterIsAConstant:
    """The wait is a deliberate constant, not an ETA-derived value."""

    @pytest.mark.asyncio
    async def test_no_tasks_in_progress_returns_the_constant(self) -> None:
        """With nothing running, the agent is told to come back soon."""
        from src.marcus_mcp.tools.task import (
            RETRY_AFTER_SECONDS,
            calculate_retry_after_seconds,
        )

        state = Mock()
        state.agent_tasks = {}
        state.project_tasks = []

        result = await calculate_retry_after_seconds(state)

        assert result["retry_after_seconds"] == RETRY_AFTER_SECONDS

    @pytest.mark.asyncio
    async def test_a_long_running_task_does_not_lengthen_the_wait(self) -> None:
        """A 40-hour estimate must not produce a 40-hour-scaled wait.

        This is the assertion that would fail if someone "repaired" the
        clamp and restored ETA-proportional waits without revisiting the
        PR #177 decision.
        """
        from src.marcus_mcp.tools.task import (
            RETRY_AFTER_SECONDS,
            calculate_retry_after_seconds,
        )

        result = await calculate_retry_after_seconds(_state_with_task_in_progress())

        assert result["retry_after_seconds"] == RETRY_AFTER_SECONDS

    @pytest.mark.asyncio
    async def test_the_blocking_task_is_still_reported(self) -> None:
        """The ETA is still surfaced for humans, just not used for timing.

        Agents poll on a fixed cadence; the ETA is diagnostic.
        """
        result = None
        from src.marcus_mcp.tools.task import calculate_retry_after_seconds

        result = await calculate_retry_after_seconds(_state_with_task_in_progress())

        assert result["blocking_task"] is not None
        assert result["blocking_task"]["id"] == "task-1"
        assert result["blocking_task"]["eta_seconds"] > 0


class TestDocstringMatchesBehaviour:
    """The documented contract must not contradict the shipped one."""

    def test_docstring_does_not_promise_five_minutes(self) -> None:
        """The 300s promise was left behind by PR #177."""
        from src.marcus_mcp.tools.task import calculate_retry_after_seconds

        doc = calculate_retry_after_seconds.__doc__ or ""
        assert "5 minutes" not in doc
        assert "300s" not in doc
