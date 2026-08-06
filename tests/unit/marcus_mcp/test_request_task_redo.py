"""
Unit tests for the request_task_redo MCP tool (issue #627).

request_task_redo lets an integration-verification agent send a
completed (DONE) task back to the board for a fresh implementer to
redo, instead of rewriting a sibling agent's implementation in place
(Invariant #2) or abandoning the project. Marcus resets the task to
TODO, populates RecoveryInfo with the redo diagnostic, and the next
agent claims it through the normal request_next_task cycle
(Invariant #1).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.core.models import Priority, RecoveryInfo, Task, TaskStatus
from src.marcus_mcp.tools.task import request_task_redo

pytestmark = pytest.mark.unit


def _make_task(
    task_id: str = "task-impl-1",
    status: TaskStatus = TaskStatus.DONE,
    assigned_to: str = "agent_unicorn_1_3",
    recovery_info: RecoveryInfo | None = None,
) -> Task:
    """Create a minimal Task in the given status."""
    now = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        name="Implement Task Management",
        description="Build the task management lane",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=assigned_to,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=4.0,
        recovery_info=recovery_info,
    )


def _make_state(task: Task, caller_id: str = "agent_integration") -> Mock:
    """Create a mock Marcus server state holding one task and an active caller."""
    state = Mock()
    state.project_tasks = [task]
    state.agent_tasks = {caller_id: Mock()}
    state.initialize_kanban = AsyncMock()
    state.kanban_client = Mock()
    state.kanban_client.update_task = AsyncMock()
    state.kanban_client.add_comment = AsyncMock()
    state.kanban_client._load_workspace_state = MagicMock(
        return_value={"project_root": "/srv/marcus/proj/repo"}
    )
    return state


class TestRequestTaskRedoHappyPath:
    """Happy path: a DONE task is sent back to the board with recovery info."""

    @pytest.mark.asyncio
    async def test_done_task_is_reset_to_todo_with_recovery_info(self) -> None:
        """A valid redo resets the task to TODO and populates RecoveryInfo."""
        task = _make_task()
        state = _make_state(task)

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            result = await request_task_redo(
                agent_id="agent_integration",
                task_id="task-impl-1",
                reason="API response missing required 'data.task' wrapper",
                state=state,
            )

        assert result["success"] is True
        assert result["task_id"] == "task-impl-1"
        assert task.status == TaskStatus.TODO
        assert task.assigned_to is None
        assert task.recovery_info is not None
        assert task.recovery_info.redo_reason == (
            "API response missing required 'data.task' wrapper"
        )
        assert task.recovery_info.requested_by == "agent_integration"
        assert task.recovery_info.recovered_from_agent == "agent_unicorn_1_3"
        assert task.recovery_info.redo_count == 1

    @pytest.mark.asyncio
    async def test_redo_boosts_priority_and_updates_board(self) -> None:
        """The redone task is boosted to URGENT and dual-written to kanban."""
        task = _make_task()
        state = _make_state(task)

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            await request_task_redo(
                agent_id="agent_integration",
                task_id="task-impl-1",
                reason="wrong shape",
                state=state,
            )

        assert task.priority == Priority.URGENT
        state.kanban_client.update_task.assert_awaited_once_with(
            "task-impl-1", {"status": TaskStatus.TODO, "assigned_to": None}
        )
        state.kanban_client.add_comment.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_redo_resolves_previous_worktree_and_branch(self) -> None:
        """RecoveryInfo points at the previous attempt's branch and worktree."""
        task = _make_task(assigned_to="agent_unicorn_1_3")
        state = _make_state(task)

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            await request_task_redo(
                agent_id="agent_integration",
                task_id="task-impl-1",
                reason="wrong shape",
                state=state,
            )

        info = task.recovery_info
        assert info is not None
        assert info.previous_agent_branch == "marcus/agent_unicorn_1_3"
        assert (
            info.previous_worktree_path
            == "/srv/marcus/proj/worktrees/agent_unicorn_1_3"
        )

    @pytest.mark.asyncio
    async def test_redo_emits_task_redo_requested_event(self) -> None:
        """The redo emits a task_redo_requested event for observability."""
        task = _make_task()
        state = _make_state(task)

        with patch("src.marcus_mcp.tools.task.log_agent_event") as mock_event:
            await request_task_redo(
                agent_id="agent_integration",
                task_id="task-impl-1",
                reason="wrong shape",
                state=state,
            )

        mock_event.assert_called_once()
        event_name, payload = mock_event.call_args[0]
        assert event_name == "task_redo_requested"
        assert payload["task_id"] == "task-impl-1"
        assert payload["requested_by"] == "agent_integration"
        assert payload["redo_count"] == 1

    @pytest.mark.asyncio
    async def test_redo_survives_workspace_state_failure(self) -> None:
        """Worktree resolution is best-effort; a workspace-state error
        leaves previous_worktree_path unset without failing the redo."""
        task = _make_task()
        state = _make_state(task)
        state.kanban_client._load_workspace_state = MagicMock(
            side_effect=RuntimeError("no workspace state")
        )

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            result = await request_task_redo(
                agent_id="agent_integration",
                task_id="task-impl-1",
                reason="wrong shape",
                state=state,
            )

        assert result["success"] is True
        assert task.recovery_info is not None
        assert task.recovery_info.previous_worktree_path is None

    @pytest.mark.asyncio
    async def test_redo_survives_kanban_failure(self) -> None:
        """A kanban write failure must not fail the redo (model is truth)."""
        task = _make_task()
        state = _make_state(task)
        state.kanban_client.update_task = AsyncMock(side_effect=RuntimeError("down"))
        state.kanban_client.add_comment = AsyncMock(side_effect=RuntimeError("down"))

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            result = await request_task_redo(
                agent_id="agent_integration",
                task_id="task-impl-1",
                reason="wrong shape",
                state=state,
            )

        assert result["success"] is True
        assert task.status == TaskStatus.TODO

    @pytest.mark.asyncio
    async def test_second_redo_increments_count(self) -> None:
        """A task redone once before gets redo_count=2 on the next redo."""
        first_redo = RecoveryInfo(
            recovered_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            recovered_from_agent="agent_unicorn_1_3",
            previous_progress=100,
            time_spent_minutes=0.0,
            recovery_reason="redo_requested",
            instructions="fix it",
            redo_count=1,
        )
        task = _make_task(assigned_to="agent_unicorn_2_8", recovery_info=first_redo)
        state = _make_state(task)

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            result = await request_task_redo(
                agent_id="agent_integration",
                task_id="task-impl-1",
                reason="still the wrong shape",
                state=state,
            )

        assert result["success"] is True
        assert result["redo_count"] == 2
        assert task.recovery_info is not None
        assert task.recovery_info.redo_count == 2
        assert task.recovery_info.recovered_from_agent == "agent_unicorn_2_8"


class TestRequestTaskRedoRejections:
    """Every validation failure returns success=False without mutating the task."""

    @pytest.mark.asyncio
    async def test_empty_reason_is_rejected(self) -> None:
        """A blank reason is rejected — the next agent needs a diagnostic."""
        task = _make_task()
        state = _make_state(task)

        result = await request_task_redo(
            agent_id="agent_integration",
            task_id="task-impl-1",
            reason="   ",
            state=state,
        )

        assert result["success"] is False
        assert result["error"] == "reason_required"
        assert task.status == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_unknown_task_is_rejected(self) -> None:
        """A task_id not in project_tasks returns task_not_found."""
        state = _make_state(_make_task())

        result = await request_task_redo(
            agent_id="agent_integration",
            task_id="no-such-task",
            reason="wrong shape",
            state=state,
        )

        assert result["success"] is False
        assert result["error"] == "task_not_found"

    @pytest.mark.asyncio
    async def test_non_done_task_is_rejected(self) -> None:
        """Only DONE tasks can be redone; in-flight work uses report_blocker."""
        task = _make_task(status=TaskStatus.IN_PROGRESS)
        state = _make_state(task)

        result = await request_task_redo(
            agent_id="agent_integration",
            task_id="task-impl-1",
            reason="wrong shape",
            state=state,
        )

        assert result["success"] is False
        assert result["error"] == "task_not_done"
        assert task.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_inactive_caller_is_rejected(self) -> None:
        """A caller with no claimed task cannot request a redo."""
        task = _make_task()
        state = _make_state(task, caller_id="someone_else")

        result = await request_task_redo(
            agent_id="agent_integration",
            task_id="task-impl-1",
            reason="wrong shape",
            state=state,
        )

        assert result["success"] is False
        assert result["error"] == "caller_not_active"
        assert task.status == TaskStatus.DONE


class TestRequestTaskRedoCap:
    """The redo ceiling: after 3 redos the tool refuses (issue #627 spec)."""

    @pytest.mark.asyncio
    async def test_fourth_redo_is_rejected(self) -> None:
        """redo_count=3 means the cap is reached; the 4th request fails."""
        capped = RecoveryInfo(
            recovered_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            recovered_from_agent="agent_unicorn_3_1",
            previous_progress=100,
            time_spent_minutes=0.0,
            recovery_reason="redo_requested",
            instructions="fix it",
            redo_count=3,
        )
        task = _make_task(recovery_info=capped)
        state = _make_state(task)

        result = await request_task_redo(
            agent_id="agent_integration",
            task_id="task-impl-1",
            reason="still wrong",
            state=state,
        )

        assert result["success"] is False
        assert result["error"] == "max_redo_count_exceeded"
        assert result["redo_count"] == 3
        assert task.status == TaskStatus.DONE
        assert task.recovery_info is capped


class TestRedoEndpointExposure:
    """Tier-1 regression (PR #712 review): the tool must reach agents.

    The live-server check found request_task_redo registered in the
    ANALYTICS tool group instead of AGENT — the agent HTTP endpoint
    (what spawned workers connect to) never exposed it, and no unit
    test asserted endpoint membership. These pin it to the right
    surface: agents can call it; the observational analytics endpoint
    cannot mutate task state.
    """

    def test_agent_endpoint_exposes_request_task_redo(self) -> None:
        """Spawned workers connect to the agent endpoint — redo must be there."""
        from src.marcus_mcp.tool_groups import get_tools_for_endpoint

        assert "request_task_redo" in get_tools_for_endpoint("agent")

    def test_analytics_endpoint_does_not_expose_request_task_redo(self) -> None:
        """Analytics is observational — it must not mutate task state."""
        from src.marcus_mcp.tool_groups import get_tools_for_endpoint

        assert "request_task_redo" not in get_tools_for_endpoint("analytics")
