"""Tests for the status-release invariant.

Marcus invariant (Simon decision 011b3fad): any status change to a
terminal state (DONE, BLOCKED) must release coordination state — the
agent's assignment slot, the persistence record, and the active lease —
independently of correctness outcomes (validation, worktree merge).

Without this invariant, the snake_game-v1 [2-agent]-run_0-2_agents
experiment showed cascading failure: a worktree merge conflict caused
``report_progress(status=completed)`` to return early before removing
the lease; the lease then expired 215 seconds later and triggered
recovery onto a fresh agent, who duplicated the work; both agents
eventually called ``report_blocker`` as an escape hatch but
``report_blocker`` didn't release the assignment either, leaving both
agents deadlocked.

These tests pin the new contract: terminal status flips release
coordination state. Correctness failures become feedback to the agent
(e.g. resolve a merge conflict and retry) but never reasons to keep
stale leases alive.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus


def _wire_release_lease(lease_manager) -> None:
    """Make a mocked lease manager honour release_lease().

    Production code now drops leases through
    ``AssignmentLeaseManager.release_lease`` rather than deleting from
    ``active_leases`` directly, so the fencing epoch stays behind the
    same lock every other mutation takes (ADR-0012 D11). A bare
    ``MagicMock`` would accept the call and leave the dict untouched,
    which makes these tests assert against a fiction.
    """

    async def _release(task_id, reason="released"):
        return lease_manager.active_leases.pop(task_id, None) is not None

    lease_manager.release_lease = AsyncMock(side_effect=_release)


pytestmark = pytest.mark.unit


def _make_task(task_id: str = "task-1") -> Task:
    """Build a minimal task fixture."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name="Test task",
        description="...",
        status=TaskStatus.IN_PROGRESS,
        priority=Priority.MEDIUM,
        assigned_to="agent-1",
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=2.0,
        dependencies=[],
        labels=[],
    )


def _make_state(task: Task) -> Any:
    """Build a fully-mocked Marcus state with one assigned task and lease."""
    state = MagicMock()
    state.kanban_client = AsyncMock()
    state.kanban_client.update_task = AsyncMock()
    state.kanban_client.add_comment = AsyncMock()
    state.kanban_client.get_task_by_id = AsyncMock(return_value=task)
    state.project_tasks = [task]

    # report_blocker awaits state.initialize_kanban() — must be async
    state.initialize_kanban = AsyncMock()

    # Agent registered and currently holding the task
    agent = MagicMock()
    agent.current_tasks = [task]
    state.agent_status = {"agent-1": agent}

    assignment = MagicMock()
    assignment.task_id = task.id
    state.agent_tasks = {"agent-1": assignment}

    state.assignment_persistence = AsyncMock()
    state.assignment_persistence.remove_assignment = AsyncMock()

    lease = MagicMock()
    lease.agent_id = "agent-1"
    state.lease_manager = MagicMock()
    state.lease_manager.active_leases = {task.id: lease}
    _wire_release_lease(state.lease_manager)
    # Issue #667 Fix 1: report_task_progress awaits extend_for_validation
    # on status=completed before the smoke gate runs. Mock as AsyncMock
    # so the await resolves cleanly in tests that exercise the completion
    # path.
    state.lease_manager.extend_for_validation = AsyncMock(return_value=None)

    state.ai_engine = AsyncMock()
    state.ai_engine.analyze_blocker = AsyncMock(return_value="Try X")

    state.project_registry = None
    state.code_analyzer = None
    state.provider = "sqlite"

    return state


class TestReportBlockerReleasesCoordination:
    """``report_blocker`` must release the agent's assignment + lease.

    Issue #719: these tests use ``severity="high"`` because the invariant
    under test is "a terminal handoff releases coordination state".
    low/medium blockers are advisory — they deliberately keep the task
    with the agent (see ``tests/unit/mcp/test_advisory_blocker.py``), so
    the invariant does not apply to them.
    """

    @pytest.mark.asyncio
    async def test_report_blocker_clears_agent_tasks(self) -> None:
        """After report_blocker, agent_tasks no longer holds the task."""
        from src.marcus_mcp.tools.task import report_blocker

        task = _make_task()
        state = _make_state(task)

        await report_blocker(
            agent_id="agent-1",
            task_id=task.id,
            blocker_description="Stuck on dep",
            severity="high",
            state=state,
        )

        assert "agent-1" not in state.agent_tasks, (
            "report_blocker must clear the agent's assignment slot — a "
            "blocker means this agent cannot make progress on this task, "
            "so the slot must be freed for the next request_next_task."
        )

    @pytest.mark.asyncio
    async def test_report_blocker_removes_lease(self) -> None:
        """After report_blocker, the lease is no longer active."""
        from src.marcus_mcp.tools.task import report_blocker

        task = _make_task()
        state = _make_state(task)

        await report_blocker(
            agent_id="agent-1",
            task_id=task.id,
            blocker_description="Stuck on dep",
            severity="high",
            state=state,
        )

        assert task.id not in state.lease_manager.active_leases, (
            "report_blocker must remove the active lease — otherwise the "
            "lease keeps ticking, eventually expires, and triggers a "
            "spurious recovery cycle on a task that's already blocked."
        )

    @pytest.mark.asyncio
    async def test_report_blocker_clears_persistence(self) -> None:
        """After report_blocker, the assignment is removed from persistence."""
        from src.marcus_mcp.tools.task import report_blocker

        task = _make_task()
        state = _make_state(task)

        await report_blocker(
            agent_id="agent-1",
            task_id=task.id,
            blocker_description="Stuck on dep",
            severity="high",
            state=state,
        )

        state.assignment_persistence.remove_assignment.assert_awaited_once_with(
            "agent-1"
        )

    @pytest.mark.asyncio
    async def test_report_blocker_still_flips_status(self) -> None:
        """A board write still happens alongside the coordination release.

        The exact status depends on the repair rung — the first give-ups
        write TODO (requeued for a fresh agent), an exhausted repair budget
        writes BLOCKED. Both are pinned in
        ``tests/unit/mcp/test_blocker_repair_requeue.py``; what this test
        guards is that the board is updated at all, in the same call that
        releases coordination state.
        """
        from src.marcus_mcp.tools.task import clear_repair_attempts, report_blocker

        task = _make_task()
        # Repair counters are module-level; make this the task's FIRST give-up
        # whatever other tests in the session did with the same id.
        clear_repair_attempts(task.id)
        state = _make_state(task)

        await report_blocker(
            agent_id="agent-1",
            task_id=task.id,
            blocker_description="Stuck",
            severity="high",
            state=state,
        )

        update_call = state.kanban_client.update_task.call_args
        assert update_call is not None
        update_payload = update_call[0][1]
        # A first give-up always requeues for repair (TODO, assigned_to
        # cleared), whatever the agent pool looks like: #769 bounds the
        # total requeues and never kills a lane on the first give-up.
        # Lane death is pinned in tests/unit/mcp/test_blocker_repair_requeue.py.
        assert update_payload["status"] == TaskStatus.TODO
        assert update_payload["assigned_to"] is None


class TestReportTaskProgressBlockedReleasesCoordination:
    """``report_task_progress(status="blocked")`` must release the
    agent's assignment + lease, the same as ``report_blocker``'s
    terminal path already does.

    Issue #851: a task reported BLOCKED through ``report_task_progress``
    (as opposed to ``report_blocker``) stayed in the agent's
    ``current_tasks``, so every later ``request_next_task`` call from
    that agent was refused with "You already have a task assigned" —
    a message that was no longer true, since the task had already been
    disclaimed. The board showed BLOCKED with no useful way for the
    agent to get new work until its run ended and something else
    cleaned up the stale assignment.
    """

    def _make_blocked_state(self, task: Task) -> Any:
        state = MagicMock()
        state.kanban_client = AsyncMock()
        state.kanban_client.update_task = AsyncMock()
        state.kanban_client.update_task_progress = AsyncMock()
        state.kanban_client.add_comment = AsyncMock()
        state.kanban_client.get_task_by_id = AsyncMock(return_value=task)
        state.kanban_client.get_all_tasks = AsyncMock(return_value=[task])
        state.kanban_client._load_workspace_state = MagicMock(return_value=None)
        state.project_tasks = [task]

        state.initialize_kanban = AsyncMock()
        state.refresh_project_state = AsyncMock()

        agent = MagicMock()
        agent.current_tasks = [task]
        state.agent_status = {"agent-1": agent}

        assignment = MagicMock()
        assignment.task_id = task.id
        assignment.assigned_at = datetime.now(timezone.utc)
        state.agent_tasks = {"agent-1": assignment}

        state.assignment_persistence = AsyncMock()
        state.assignment_persistence.remove_assignment = AsyncMock()

        lease = MagicMock()
        lease.agent_id = "agent-1"
        state.lease_manager = MagicMock()
        state.lease_manager.active_leases = {task.id: lease}
        _wire_release_lease(state.lease_manager)
        state.lease_manager.extend_for_validation = AsyncMock(return_value=None)

        state.code_analyzer = None
        state.provider = "sqlite"
        state.subtask_manager = None
        state.memory = None
        state.context = None

        return state

    @pytest.mark.asyncio
    async def test_blocked_clears_agent_current_tasks(self) -> None:
        """After a blocked report, the agent's current_tasks is empty —
        a subsequent request_next_task must not be refused on its
        account."""
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = self._make_blocked_state(task)

        await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=50,
            message="Stuck on a dependency",
            state=state,
        )

        assert state.agent_status["agent-1"].current_tasks == [], (
            "report_task_progress(blocked) must clear current_tasks — "
            "otherwise request_next_task's one-task-per-agent check "
            "refuses every later call with a now-false message."
        )

    @pytest.mark.asyncio
    async def test_blocked_clears_agent_tasks_assignment(self) -> None:
        """After a blocked report, agent_tasks no longer holds the task."""
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = self._make_blocked_state(task)

        await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=50,
            message="Stuck",
            state=state,
        )

        assert "agent-1" not in state.agent_tasks

    @pytest.mark.asyncio
    async def test_blocked_clears_persistence(self) -> None:
        """After a blocked report, the assignment is removed from persistence."""
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = self._make_blocked_state(task)

        await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=50,
            message="Stuck",
            state=state,
        )

        state.assignment_persistence.remove_assignment.assert_awaited_once_with(
            "agent-1"
        )

    @pytest.mark.asyncio
    async def test_blocked_removes_lease(self) -> None:
        """After a blocked report, the lease is no longer active."""
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = self._make_blocked_state(task)

        await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=50,
            message="Stuck",
            state=state,
        )

        assert task.id not in state.lease_manager.active_leases

    @pytest.mark.asyncio
    async def test_blocked_board_write_unassigns_the_card(self) -> None:
        """The kanban update for a blocked task clears assigned_to — the
        card must not still show the agent that just disclaimed it."""
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = self._make_blocked_state(task)

        await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=50,
            message="Stuck",
            state=state,
        )

        update_call = state.kanban_client.update_task.call_args
        assert update_call is not None
        update_payload = update_call[0][1]
        assert update_payload["status"] == TaskStatus.BLOCKED
        assert update_payload["assigned_to"] is None

    @pytest.mark.asyncio
    async def test_a_new_task_request_is_no_longer_refused_after_blocking(
        self,
    ) -> None:
        """End-to-end within the mocked state: request_next_task's
        one-task-per-agent guard reads agent.current_tasks directly, so
        once report_task_progress(blocked) clears it, that guard no
        longer fires for this agent."""
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = self._make_blocked_state(task)

        await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=50,
            message="Stuck",
            state=state,
        )

        # This is exactly the condition request_next_task's one-task-per-
        # agent guard checks (src/marcus_mcp/tools/task.py, "if
        # agent.current_tasks:") — it must now be falsy.
        assert not state.agent_status["agent-1"].current_tasks


class TestCompletionReleasesLeaseEvenOnMergeFailure:
    """``report_progress(status=completed)`` must release the lease even when
    the worktree merge subsequently fails.

    The snake_game-v1 cascade was:
      1. Agent reports completion → board status flipped to DONE
      2. Worktree merge hits a conflict and returns success=False
      3. Old code: report_progress returns early before lease removal
      4. Lease ticks down, expires 215s later, recovery reassigns the
         already-completed task to a fresh agent
      5. Both agents end up deadlocked, calling report_blocker as escape

    The decoupling fix (Simon decision 011b3fad): release coordination
    state independently of merge correctness. Merge failure becomes
    feedback in the response, not a coordination state pin.
    """

    def _make_completion_state(self, task: Task) -> Any:
        """State for ``report_task_progress`` calls."""
        state = MagicMock()
        state.kanban_client = AsyncMock()
        state.kanban_client.update_task = AsyncMock()
        state.kanban_client.update_task_progress = AsyncMock()
        state.kanban_client.add_comment = AsyncMock()
        state.kanban_client.get_task_by_id = AsyncMock(return_value=task)
        state.kanban_client.get_all_tasks = AsyncMock(return_value=[task])
        state.kanban_client._load_workspace_state = MagicMock(return_value=None)
        state.project_tasks = [task]

        state.initialize_kanban = AsyncMock()
        state.refresh_project_state = AsyncMock()

        agent = MagicMock()
        agent.current_tasks = [task]
        agent.completed_tasks_count = 0
        state.agent_status = {"agent-1": agent}

        assignment = MagicMock()
        assignment.task_id = task.id
        state.agent_tasks = {"agent-1": assignment}

        state.assignment_persistence = AsyncMock()
        state.assignment_persistence.remove_assignment = AsyncMock()

        state.lease_manager = MagicMock()
        state.lease_manager.active_leases = {task.id: MagicMock()}
        _wire_release_lease(state.lease_manager)
        state.lease_manager.renew_lease = AsyncMock(return_value=None)
        # Issue #667 Fix 1: report_task_progress awaits this on completion.
        state.lease_manager.extend_for_validation = AsyncMock(return_value=None)

        state.code_analyzer = None
        state.provider = "sqlite"
        # Disable optional paths the completion code skips when None
        state.subtask_manager = None
        state.memory = None
        state.context = None

        return state

    @pytest.mark.asyncio
    async def test_lease_released_on_completion_when_merge_fails(self) -> None:
        """A failed merge does NOT keep the lease alive."""
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = self._make_completion_state(task)

        # Patch _merge_agent_branch_to_main to return a merge failure
        with patch(
            "src.marcus_mcp.tools.task._merge_agent_branch_to_main",
            new=AsyncMock(return_value={"success": False, "conflict": "x"}),
        ):
            result = await report_task_progress(
                agent_id="agent-1",
                task_id=task.id,
                status="completed",
                progress=100,
                message="done",
                state=state,
            )

        # Result reflects merge failure (feedback to agent)
        assert result.get("success") is False
        # But lease MUST be gone — coordination state is released
        # regardless of correctness outcome
        assert task.id not in state.lease_manager.active_leases, (
            "Merge failure must not keep the lease ticking. The board "
            "already says DONE and the lease will otherwise expire and "
            "trigger spurious recovery on a completed task."
        )
        # Assignment released too
        assert "agent-1" not in state.agent_tasks

    @pytest.mark.asyncio
    async def test_lease_released_on_completion_when_merge_succeeds(self) -> None:
        """Merge success path still releases the lease (regression baseline)."""
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = self._make_completion_state(task)

        with patch(
            "src.marcus_mcp.tools.task._merge_agent_branch_to_main",
            new=AsyncMock(return_value={"success": True}),
        ):
            await report_task_progress(
                agent_id="agent-1",
                task_id=task.id,
                status="completed",
                progress=100,
                message="done",
                state=state,
            )

        assert task.id not in state.lease_manager.active_leases
        assert "agent-1" not in state.agent_tasks

    @pytest.mark.asyncio
    async def test_completed_tasks_count_incremented_after_merge_not_before(
        self,
    ) -> None:
        """completed_tasks_count must be 1 after a failed merge (P2).

        The count tracks agent work output, not git outcomes.  It must
        increment exactly once regardless of whether the merge succeeded
        or failed — and it must NOT be incremented before we know the
        merge result (Codex review P2).
        """
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = self._make_completion_state(task)
        agent = state.agent_status["agent-1"]
        assert agent.completed_tasks_count == 0  # precondition

        with patch(
            "src.marcus_mcp.tools.task._merge_agent_branch_to_main",
            new=AsyncMock(return_value={"success": False, "conflict": "x"}),
        ):
            await report_task_progress(
                agent_id="agent-1",
                task_id=task.id,
                status="completed",
                progress=100,
                message="done",
                state=state,
            )

        assert agent.completed_tasks_count == 1, (
            "completed_tasks_count must be 1 after a failed merge — "
            "the agent completed the work even though the git merge failed."
        )
