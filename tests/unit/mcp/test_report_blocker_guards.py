"""Unit tests for report_blocker and report_task_progress done-task guards.

Covers two bugs found in the snake_game-v10 [4-agent] run:
1. report_blocker reverts DONE tasks to BLOCKED (stale agent calls blocker
   after lease recovery and completion by new holder).
2. report_task_progress(status="blocked") same issue — no DONE guard.

Root cause: neither function checked the task's current status before
writing BLOCKED to the kanban DB. A task in DONE state must be immutable —
no agent (including one that previously held the lease) should be able to
revert it.

Both functions also must reject calls from agents who no longer hold the
lease, so lease-expired agents cannot corrupt completed work.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import Task, TaskStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(task_id: str, status: TaskStatus, assigned_to: str = "") -> Task:
    """Create a minimal Task object for testing."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.status = status
    task.assigned_to = assigned_to
    task.name = f"Task {task_id}"
    task.labels = []
    task.dependencies = []
    return task


def _make_state(
    agent_id: str,
    task_id: str,
    task_status: TaskStatus = TaskStatus.IN_PROGRESS,
    lease_holder: str | None = None,
    agent_task_id: str | None = None,
) -> MagicMock:
    """Create a minimal mock Marcus state for blocker tests."""
    state = MagicMock()
    state.initialize_kanban = AsyncMock()
    state.provider = "sqlite"

    # Task returned by kanban client
    task = _make_task(task_id, task_status)
    state.kanban_client = MagicMock()
    state.kanban_client.get_task_by_id = AsyncMock(return_value=task)
    state.kanban_client.update_task = AsyncMock()
    state.kanban_client.add_comment = AsyncMock()

    # Agent status
    agent = MagicMock()
    agent.name = agent_id
    state.agent_status = {agent_id: agent}

    # AI engine
    state.ai_engine = MagicMock()
    state.ai_engine.analyze_blocker = AsyncMock(return_value="AI suggestions")

    # Lease manager
    lease = MagicMock()
    lease.agent_id = lease_holder or agent_id
    state.lease_manager = MagicMock()
    state.lease_manager.active_leases = {task_id: lease} if lease_holder else {}

    # agent_tasks (in-memory assignment)
    assignment = MagicMock()
    assignment.task_id = agent_task_id or task_id
    state.agent_tasks = {agent_id: assignment} if agent_task_id else {}

    # assignment persistence (needed by report_blocker release path)
    state.assignment_persistence = AsyncMock()
    state.assignment_persistence.remove_assignment = AsyncMock()

    # misc
    state.memory = None
    state.project_state = MagicMock()

    return state


# ---------------------------------------------------------------------------
# report_blocker guards
# ---------------------------------------------------------------------------


class TestReportBlockerDoneTaskGuard:
    """report_blocker must reject attempts to block a DONE task."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_report_blocker_rejects_done_task(self) -> None:
        """Task in DONE state must not be reverted to BLOCKED."""
        from src.marcus_mcp.tools.task import report_blocker

        state = _make_state(
            agent_id="agent_stale",
            task_id="task-done",
            task_status=TaskStatus.DONE,
            lease_holder="agent_stale",
        )

        result = await report_blocker(
            agent_id="agent_stale",
            task_id="task-done",
            blocker_description="TERMINAL BLOCKER",
            severity="high",
            state=state,
            skip_ai_analysis=True,
        )

        assert result["success"] is False
        assert result["status"] == "task_already_complete"
        # Kanban must NOT be updated
        state.kanban_client.update_task.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_report_blocker_rejects_done_task_string_status(self) -> None:
        """Guard fires even if status stored as string 'done'."""
        from src.marcus_mcp.tools.task import report_blocker

        state = _make_state(
            agent_id="agent_stale",
            task_id="task-done",
            task_status=TaskStatus.DONE,
            lease_holder="agent_stale",
        )
        # Override to string representation
        state.kanban_client.get_task_by_id.return_value.status = "done"

        result = await report_blocker(
            agent_id="agent_stale",
            task_id="task-done",
            blocker_description="Should be rejected",
            severity="low",
            state=state,
            skip_ai_analysis=True,
        )

        assert result["success"] is False
        state.kanban_client.update_task.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_report_blocker_rejects_non_lease_holder(self) -> None:
        """Agent that no longer holds the lease cannot report a blocker."""
        from src.marcus_mcp.tools.task import report_blocker

        state = _make_state(
            agent_id="agent_stale",
            task_id="task-active",
            task_status=TaskStatus.IN_PROGRESS,
            lease_holder="agent_new_holder",  # different agent holds lease
        )

        result = await report_blocker(
            agent_id="agent_stale",
            task_id="task-active",
            blocker_description="Lease race condition",
            severity="high",
            state=state,
            skip_ai_analysis=True,
        )

        assert result["success"] is False
        assert result["status"] == "not_task_holder"
        state.kanban_client.update_task.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_report_blocker_allowed_for_lease_holder(self) -> None:
        """Legitimate blocker from the actual lease holder must succeed.

        Issue #719 changed what "succeed" writes: a ``high`` blocker is
        terminal and flips the board to BLOCKED (asserted here), while
        low/medium are advisory and deliberately leave the task with the
        agent — see ``tests/unit/mcp/test_advisory_blocker.py``. The guard
        behaviour under test (lease holder is not rejected) is unchanged.
        """
        from src.marcus_mcp.tools.task import report_blocker

        state = _make_state(
            agent_id="agent_holder",
            task_id="task-active",
            task_status=TaskStatus.IN_PROGRESS,
            lease_holder="agent_holder",
        )

        result = await report_blocker(
            agent_id="agent_holder",
            task_id="task-active",
            blocker_description="Real blocker",
            severity="high",
            state=state,
            skip_ai_analysis=True,
        )

        assert result["success"] is True
        state.kanban_client.update_task.assert_called_once()


# ---------------------------------------------------------------------------
# report_task_progress(status="blocked") guards
# ---------------------------------------------------------------------------


class TestReportTaskProgressBlockedGuard:
    """report_task_progress(status='blocked') must not revert DONE tasks."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_progress_blocked_rejects_done_task(self) -> None:
        """status='blocked' on a DONE task must be rejected."""
        from src.marcus_mcp.tools.task import report_task_progress

        state = _make_state(
            agent_id="agent_stale",
            task_id="task-done",
            task_status=TaskStatus.DONE,
            lease_holder="agent_stale",
            agent_task_id="task-done",
        )

        result = await report_task_progress(
            agent_id="agent_stale",
            task_id="task-done",
            status="blocked",
            progress=99,
            message="Cannot complete — stale_completion loop",
            state=state,
        )

        assert result["success"] is False
        assert result["status"] == "task_already_complete"
        state.kanban_client.update_task.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_progress_blocked_rejects_non_lease_holder(self) -> None:
        """status='blocked' from non-holder must be rejected."""
        from src.marcus_mcp.tools.task import report_task_progress

        state = _make_state(
            agent_id="agent_stale",
            task_id="task-active",
            task_status=TaskStatus.IN_PROGRESS,
            lease_holder="agent_new_holder",
            agent_task_id=None,
        )

        result = await report_task_progress(
            agent_id="agent_stale",
            task_id="task-active",
            status="blocked",
            progress=99,
            message="PERMANENTLY BLOCKED",
            state=state,
        )

        assert result["success"] is False
        assert result["status"] == "not_task_holder"
        state.kanban_client.update_task.assert_not_called()
