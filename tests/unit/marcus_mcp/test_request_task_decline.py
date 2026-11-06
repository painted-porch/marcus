"""
Unit tests for the request_task_decline MCP tool (ADR-0013 K5).

A verification task's holder needs a way to say "this cannot be
verified" that is distinct from DONE, from a worker-reported BLOCKED,
and from report_blocker's repair-rung requeue, which sends the task
back for another attempt on a budget separate from any redo bound --
silently defeating it for exactly this case. request_task_decline
marks the task DECLINED: terminal, and never claimable again.
"""

from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.task import request_task_decline

pytestmark = pytest.mark.unit


def _make_task(
    task_id: str = "verify-1",
    status: TaskStatus = TaskStatus.IN_PROGRESS,
    assigned_to: str = "verifier-1",
) -> Task:
    """Create a minimal Task in the given status."""
    now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        name="Verify the claim",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=assigned_to,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
    )


def _make_state(
    task: Task, caller_id: str = "verifier-1", holder_id: Optional[str] = "verifier-1"
) -> Mock:
    """Create a mock Marcus server state holding one task.

    ``holder_id`` seeds an active lease for the task, matching
    production: a lease is created when a task is claimed and exists
    for as long as it is IN_PROGRESS. Pass ``None`` to simulate a task
    with no active lease at all (an unclaimed TODO, or a lease that
    already expired with nothing recovering it).
    """
    state = Mock()
    state.project_tasks = [task]
    state.agent_status = {caller_id: Mock(current_tasks=[task])}
    state.agent_tasks = {caller_id: Mock()}
    state.kanban_client = Mock()
    state.kanban_client.update_task = AsyncMock()
    state.kanban_client.add_comment = AsyncMock()
    # Matches every real provider's happy path (sqlite always succeeds;
    # Planka/GitHub/Linear report True when they actually moved the
    # card). Tests exercising a provider that cannot represent a
    # status override this explicitly.
    state.kanban_client.move_task_to_column = AsyncMock(return_value=True)
    state.assignment_persistence = Mock()
    state.assignment_persistence.remove_assignment = AsyncMock()
    state.lease_manager = Mock()
    state.lease_manager.active_leases = {}
    if holder_id is not None:
        lease = Mock()
        lease.agent_id = holder_id
        state.lease_manager.active_leases[task.id] = lease
    state.lease_manager.release_lease = AsyncMock()
    return state


class TestRequestTaskDeclineHappyPath:
    """A task's current holder can decline it, terminally."""

    @pytest.mark.asyncio
    async def test_the_task_is_marked_declined(self) -> None:
        task = _make_task()
        state = _make_state(task)

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            result = await request_task_decline(
                agent_id="verifier-1",
                task_id="verify-1",
                reason="the summary count doesn't match the bucket table",
                state=state,
            )

        assert result["success"] is True
        assert result["status"] == "declined"
        assert task.status == TaskStatus.DECLINED
        assert task.assigned_to is None

    @pytest.mark.asyncio
    async def test_the_board_write_carries_the_same_status(self) -> None:
        task = _make_task()
        state = _make_state(task)

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            await request_task_decline(
                agent_id="verifier-1",
                task_id="verify-1",
                reason="wrong",
                state=state,
            )

        state.kanban_client.update_task.assert_awaited_once_with(
            "verify-1", {"status": TaskStatus.DECLINED, "assigned_to": None}
        )

    @pytest.mark.asyncio
    async def test_coordination_state_is_released(self) -> None:
        task = _make_task()
        state = _make_state(task)

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            await request_task_decline(
                agent_id="verifier-1",
                task_id="verify-1",
                reason="wrong",
                state=state,
            )

        assert "verifier-1" not in state.agent_tasks
        state.assignment_persistence.remove_assignment.assert_awaited_once_with(
            "verifier-1"
        )

    @pytest.mark.asyncio
    async def test_emits_task_declined_event(self) -> None:
        task = _make_task()
        state = _make_state(task)

        with patch("src.marcus_mcp.tools.task.log_agent_event") as mock_event:
            await request_task_decline(
                agent_id="verifier-1",
                task_id="verify-1",
                reason="wrong",
                state=state,
            )

        mock_event.assert_called_once()
        event_name, payload = mock_event.call_args[0]
        assert event_name == "task_declined"
        assert payload["task_id"] == "verify-1"
        assert payload["declined_by"] == "verifier-1"
        assert payload["reason"] == "wrong"

    @pytest.mark.asyncio
    async def test_releases_file_locks_the_task_held(self) -> None:
        """A decline is terminal the same way report_blocker's
        lane-death path is, and must release #206 file locks the same
        way (Codex P1 on PR #834) -- otherwise another task declaring
        those files stays blocked by a holder that will never finish.
        """
        task = _make_task()
        state = _make_state(task)
        state.file_lock_registry = Mock()
        state.file_lock_registry.release = AsyncMock(return_value=2)

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            await request_task_decline(
                agent_id="verifier-1",
                task_id="verify-1",
                reason="wrong",
                state=state,
            )

        state.file_lock_registry.release.assert_awaited_once_with("verify-1")

    @pytest.mark.asyncio
    async def test_persists_through_the_subtask_manager_with_no_kanban_row(
        self, tmp_path
    ) -> None:
        """Codex P1, round 3 on PR #834: the board write cannot
        substitute for this. SubtaskManager's own store is what
        actually persists subtask state -- a migrated subtask may have
        no kanban row at all -- and is what request_next_task's own
        subtask path reads back on the next poll."""
        from src.marcus_mcp.coordinator.subtask_manager import (
            Subtask,
            SubtaskManager,
        )

        task = _make_task(task_id="sub-1")
        task.is_subtask = True
        task.parent_task_id = "parent-1"
        state = _make_state(task)

        manager = SubtaskManager(state_file=tmp_path / "subtasks_test.json")
        manager.subtasks["sub-1"] = Subtask(
            id="sub-1",
            parent_task_id="parent-1",
            name="Verify the claim",
            description="",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.MEDIUM,
            assigned_to="verifier-1",
            created_at=datetime.now(timezone.utc),
            estimated_hours=1.0,
        )
        state.subtask_manager = manager
        # No kanban_client write can be trusted to carry this -- the
        # real regression report_blocker's lane-death comment names
        # (GitHub, Linear) don't persist BLOCKED through update_task
        # either, which is exactly why this path must not depend on it.
        state.kanban_client.update_task = AsyncMock(side_effect=RuntimeError("down"))

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            result = await request_task_decline(
                agent_id="verifier-1",
                task_id="sub-1",
                reason="wrong",
                state=state,
            )

        assert result["success"] is True
        assert manager.subtasks["sub-1"].status == TaskStatus.DECLINED

    @pytest.mark.asyncio
    async def test_survives_a_kanban_write_failure(self) -> None:
        """A board write failure must not fail the decline -- the task
        model is the source of truth, matching request_task_redo."""
        task = _make_task()
        state = _make_state(task)
        state.kanban_client.update_task = AsyncMock(side_effect=RuntimeError("down"))
        state.kanban_client.add_comment = AsyncMock(side_effect=RuntimeError("down"))

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            result = await request_task_decline(
                agent_id="verifier-1",
                task_id="verify-1",
                reason="wrong",
                state=state,
            )

        assert result["success"] is True
        assert task.status == TaskStatus.DECLINED


class TestRequestTaskDeclineSurfacesUnpersistedBoardWrites:
    """Codex P1, round 4 on PR #834.

    github_kanban.py/linear_kanban.py's ``move_task_to_column`` used to
    report success for a column it has no mapping for at all, so the
    caller believed the board agreed with the in-memory DECLINED model
    when it did not. The provider fix (returning False instead of a
    silent True) is covered by each provider's own tests; this covers
    the caller side -- the reply must say so.
    """

    @pytest.mark.asyncio
    async def test_persisted_is_true_on_the_happy_path(self) -> None:
        task = _make_task()
        state = _make_state(task)

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            result = await request_task_decline(
                agent_id="verifier-1",
                task_id="verify-1",
                reason="wrong",
                state=state,
            )

        assert result["persisted"] is True
        assert "provider" not in result

    @pytest.mark.asyncio
    async def test_a_provider_with_no_mapping_reports_unpersisted(self) -> None:
        """move_task_to_column returning False (the provider fix) must
        reach the agent, naming the provider, not be swallowed."""
        task = _make_task()
        state = _make_state(task)
        state.kanban_client.move_task_to_column = AsyncMock(return_value=False)
        state.kanban_client.provider = Mock(value="github")

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            result = await request_task_decline(
                agent_id="verifier-1",
                task_id="verify-1",
                reason="wrong",
                state=state,
            )

        assert result["success"] is True
        assert task.status == TaskStatus.DECLINED
        assert result["persisted"] is False
        assert result["provider"] == "github"
        assert "github" in result["message"]

    @pytest.mark.asyncio
    async def test_a_move_task_to_column_exception_also_reports_unpersisted(
        self,
    ) -> None:
        task = _make_task()
        state = _make_state(task)
        state.kanban_client.move_task_to_column = AsyncMock(
            side_effect=RuntimeError("down")
        )
        state.kanban_client.provider = Mock(value="linear")

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            result = await request_task_decline(
                agent_id="verifier-1",
                task_id="verify-1",
                reason="wrong",
                state=state,
            )

        assert result["persisted"] is False
        assert result["provider"] == "linear"

    @pytest.mark.asyncio
    async def test_an_update_task_failure_also_reports_unpersisted(self) -> None:
        task = _make_task()
        state = _make_state(task)
        state.kanban_client.update_task = AsyncMock(side_effect=RuntimeError("down"))

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            result = await request_task_decline(
                agent_id="verifier-1",
                task_id="verify-1",
                reason="wrong",
                state=state,
            )

        assert result["persisted"] is False


class TestRequestTaskDeclineRejections:
    """Every validation failure returns success=False without mutating the task."""

    @pytest.mark.asyncio
    async def test_empty_reason_is_rejected(self) -> None:
        task = _make_task()
        state = _make_state(task)

        result = await request_task_decline(
            agent_id="verifier-1",
            task_id="verify-1",
            reason="   ",
            state=state,
        )

        assert result["success"] is False
        assert result["error"] == "reason_required"
        assert task.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_unknown_task_is_rejected(self) -> None:
        state = _make_state(_make_task())

        result = await request_task_decline(
            agent_id="verifier-1",
            task_id="no-such-task",
            reason="wrong",
            state=state,
        )

        assert result["success"] is False
        assert result["error"] == "task_not_found"

    @pytest.mark.asyncio
    async def test_a_non_holder_is_rejected(self) -> None:
        """A lease-expired agent cannot decline work the recovery
        holder has since taken over (mirrors report_blocker Guard 2)."""
        task = _make_task(assigned_to="verifier-1")
        state = _make_state(task)
        lease = Mock()
        lease.agent_id = "verifier-2"
        state.lease_manager.active_leases["verify-1"] = lease

        result = await request_task_decline(
            agent_id="verifier-1",
            task_id="verify-1",
            reason="wrong",
            state=state,
        )

        assert result["success"] is False
        assert result["error"] == "not_task_holder"
        assert task.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_an_unclaimed_todo_task_is_rejected(self) -> None:
        """A TODO task has no holder at all -- declining it is nonsense,
        not just unauthorized (Codex P1 on PR #834)."""
        task = _make_task(status=TaskStatus.TODO, assigned_to=None)
        state = _make_state(task, holder_id=None)

        result = await request_task_decline(
            agent_id="verifier-1",
            task_id="verify-1",
            reason="wrong",
            state=state,
        )

        assert result["success"] is False
        assert result["error"] == "task_not_held"
        assert task.status == TaskStatus.TODO

    @pytest.mark.asyncio
    async def test_a_done_task_is_rejected(self) -> None:
        """A settled DONE task must never be overwritten by a decline
        (Codex P1 on PR #834)."""
        task = _make_task(status=TaskStatus.DONE)
        state = _make_state(task)

        result = await request_task_decline(
            agent_id="verifier-1",
            task_id="verify-1",
            reason="wrong",
            state=state,
        )

        assert result["success"] is False
        assert result["error"] == "task_not_held"
        assert task.status == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_no_active_lease_at_all_is_rejected_not_a_pass_through(
        self,
    ) -> None:
        """A lease manager with no lease recorded for this task must
        refuse, not fall back to task.assigned_to (Codex P1 on PR
        #834) -- a lease miss means no one currently holds the task."""
        task = _make_task(assigned_to="verifier-1")
        state = _make_state(task, holder_id=None)

        result = await request_task_decline(
            agent_id="verifier-1",
            task_id="verify-1",
            reason="wrong",
            state=state,
        )

        assert result["success"] is False
        assert result["error"] == "not_task_holder"
        assert task.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_the_holder_succeeds_with_no_lease_manager_configured(
        self,
    ) -> None:
        """Without a lease manager at all, the recorded assignee may
        still decline -- the fallback path Codex's fix preserves."""
        task = _make_task(assigned_to="verifier-1")
        state = _make_state(task)
        state.lease_manager = None

        with patch("src.marcus_mcp.tools.task.log_agent_event"):
            result = await request_task_decline(
                agent_id="verifier-1",
                task_id="verify-1",
                reason="wrong",
                state=state,
            )

        assert result["success"] is True
        assert task.status == TaskStatus.DECLINED

    @pytest.mark.asyncio
    async def test_a_non_assignee_is_rejected_with_no_lease_manager(self) -> None:
        task = _make_task(assigned_to="verifier-1")
        state = _make_state(task)
        state.lease_manager = None

        result = await request_task_decline(
            agent_id="verifier-2",
            task_id="verify-1",
            reason="wrong",
            state=state,
        )

        assert result["success"] is False
        assert result["error"] == "not_task_holder"
        assert task.status == TaskStatus.IN_PROGRESS
