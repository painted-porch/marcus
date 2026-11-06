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


def _make_blocked_state(task: Task) -> Any:
    """State for a report_task_progress(status="blocked") call that

    reaches the function's full shared tail (update_task_progress,
    lease renewal, etc.), not just the early guards -- unlike
    ``_make_state`` (used by the ``report_blocker`` tests above),
    which never exercises that tail.
    """
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
    # The shared tail renews the lease for every non-"completed" status,
    # including "blocked" -- a bare MagicMock here isn't awaitable, and
    # it only matters for a test that runs the function all the way to
    # its normal return instead of stopping at the release block.
    state.lease_manager.renew_lease = AsyncMock(return_value=None)

    state.code_analyzer = None
    state.provider = "sqlite"
    state.subtask_manager = None
    state.memory = None
    state.context = None

    return state


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
        return _make_blocked_state(task)

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


class TestStaleBlockedReportDoesNotClearACurrentTask:
    """Issue #858, Codex P2: a stale or duplicate ``blocked`` report for
    a task the agent no longer holds must not clear whatever task the
    agent has moved on to.

    Sequence: agent blocks task A (releases A's hold, as fixed above),
    claims task B (current_tasks=[B]), then a retried or delayed
    ``blocked`` report for A arrives. A has no active lease and is
    already BLOCKED, so neither existing guard rejects it -- Guard 3
    is what catches it.
    """

    def _make_two_task_state(self, task_a: Task, task_b: Task) -> Any:
        state = MagicMock()
        state.kanban_client = AsyncMock()
        state.kanban_client.update_task = AsyncMock()
        state.kanban_client.add_comment = AsyncMock()
        state.kanban_client.get_task_by_id = AsyncMock(return_value=task_a)
        state.project_tasks = [task_a, task_b]

        state.initialize_kanban = AsyncMock()

        # Agent has already moved on: holds B, not A.
        agent = MagicMock()
        agent.current_tasks = [task_b]
        state.agent_status = {"agent-1": agent}

        assignment_b = MagicMock()
        assignment_b.task_id = task_b.id
        state.agent_tasks = {"agent-1": assignment_b}

        state.assignment_persistence = AsyncMock()
        state.assignment_persistence.remove_assignment = AsyncMock()

        lease_b = MagicMock()
        lease_b.agent_id = "agent-1"
        state.lease_manager = MagicMock()
        # A's lease was already released when A was first blocked; only
        # B's lease is active.
        state.lease_manager.active_leases = {task_b.id: lease_b}
        _wire_release_lease(state.lease_manager)

        state.code_analyzer = None
        state.provider = "sqlite"
        state.subtask_manager = None
        state.memory = None
        state.context = None

        return state

    @pytest.mark.asyncio
    async def test_a_stale_blocked_report_for_a_does_not_clear_bs_hold(self) -> None:
        from src.marcus_mcp.tools.task import report_task_progress

        task_a = _make_task("task-a")
        task_a.status = TaskStatus.BLOCKED  # already blocked from the first report
        task_b = _make_task("task-b")
        state = self._make_two_task_state(task_a, task_b)

        result = await report_task_progress(
            agent_id="agent-1",
            task_id="task-a",
            status="blocked",
            progress=30,
            message="Stale retry of the first blocked report",
            state=state,
        )

        assert result["success"] is False
        assert result["status"] == "not_current_assignment"
        assert state.agent_status["agent-1"].current_tasks == [task_b], (
            "A stale blocked report for A must not clear B from "
            "current_tasks -- B is what the agent actually holds now."
        )

    @pytest.mark.asyncio
    async def test_a_stale_blocked_report_for_a_does_not_clear_bs_agent_tasks(
        self,
    ) -> None:
        from src.marcus_mcp.tools.task import report_task_progress

        task_a = _make_task("task-a")
        task_a.status = TaskStatus.BLOCKED
        task_b = _make_task("task-b")
        state = self._make_two_task_state(task_a, task_b)

        await report_task_progress(
            agent_id="agent-1",
            task_id="task-a",
            status="blocked",
            progress=30,
            message="Stale retry",
            state=state,
        )

        assert "agent-1" in state.agent_tasks
        assert state.agent_tasks["agent-1"].task_id == "task-b"

    @pytest.mark.asyncio
    async def test_a_stale_blocked_report_for_a_does_not_touch_persistence(
        self,
    ) -> None:
        from src.marcus_mcp.tools.task import report_task_progress

        task_a = _make_task("task-a")
        task_a.status = TaskStatus.BLOCKED
        task_b = _make_task("task-b")
        state = self._make_two_task_state(task_a, task_b)

        await report_task_progress(
            agent_id="agent-1",
            task_id="task-a",
            status="blocked",
            progress=30,
            message="Stale retry",
            state=state,
        )

        state.assignment_persistence.remove_assignment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_stale_blocked_report_for_a_does_not_touch_bs_lease(self) -> None:
        from src.marcus_mcp.tools.task import report_task_progress

        task_a = _make_task("task-a")
        task_a.status = TaskStatus.BLOCKED
        task_b = _make_task("task-b")
        state = self._make_two_task_state(task_a, task_b)

        await report_task_progress(
            agent_id="agent-1",
            task_id="task-a",
            status="blocked",
            progress=30,
            message="Stale retry",
            state=state,
        )

        assert "task-b" in state.lease_manager.active_leases

    @pytest.mark.asyncio
    async def test_the_one_task_per_agent_guard_still_refuses_a_second_pull(
        self,
    ) -> None:
        """End-to-end within the mocked state: after the stale report,
        current_tasks is unchanged ([task_b]), so request_next_task's
        own guard (`if agent.current_tasks:`) is still truthy and would
        still refuse a second pull -- the exact bypass Codex named."""
        from src.marcus_mcp.tools.task import report_task_progress

        task_a = _make_task("task-a")
        task_a.status = TaskStatus.BLOCKED
        task_b = _make_task("task-b")
        state = self._make_two_task_state(task_a, task_b)

        await report_task_progress(
            agent_id="agent-1",
            task_id="task-a",
            status="blocked",
            progress=30,
            message="Stale retry",
            state=state,
        )

        assert bool(state.agent_status["agent-1"].current_tasks) is True


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


class TestStaleCompletedReportDoesNotClearACurrentTask:
    """Issue #858, Codex P2: the same rule the ``blocked`` branch's
    Guard 3 follows, applied to ``completed`` -- it had the identical
    gap (no check that the reported task matches what the agent
    currently holds before releasing coordination state)."""

    @pytest.mark.asyncio
    async def test_a_stale_completed_report_for_a_does_not_clear_bs_hold(
        self,
    ) -> None:
        from src.marcus_mcp.tools.task import report_task_progress

        task_a = _make_task("task-a")
        task_b = _make_task("task-b")

        state = MagicMock()
        state.kanban_client = AsyncMock()
        state.kanban_client.update_task = AsyncMock()
        state.kanban_client.get_task_by_id = AsyncMock(return_value=task_a)
        state.project_tasks = [task_a, task_b]
        state.initialize_kanban = AsyncMock()

        # Agent has moved on: holds B, not A.
        agent = MagicMock()
        agent.current_tasks = [task_b]
        state.agent_status = {"agent-1": agent}

        assignment_b = MagicMock()
        assignment_b.task_id = "task-b"
        state.agent_tasks = {"agent-1": assignment_b}

        state.assignment_persistence = AsyncMock()
        state.assignment_persistence.remove_assignment = AsyncMock()

        lease_b = MagicMock()
        state.lease_manager = MagicMock()
        state.lease_manager.active_leases = {"task-b": lease_b}
        _wire_release_lease(state.lease_manager)

        state.code_analyzer = None
        state.provider = "sqlite"
        state.subtask_manager = None
        state.memory = None
        state.context = None

        result = await report_task_progress(
            agent_id="agent-1",
            task_id="task-a",
            status="completed",
            progress=100,
            message="Stale/duplicate completion for a task already moved past",
            state=state,
        )

        # Rejected by the existing stale-completion gate (issue #343 /
        # #667 Fix 2) -- the "uncontested late completion" acceptance
        # now also requires that the agent has NOT moved on to a
        # different current assignment, so it correctly falls through
        # to the pre-existing stale_completion rejection instead of
        # accepting and wiping B's hold.
        assert result["success"] is False
        assert result["status"] == "stale_completion"
        assert state.agent_status["agent-1"].current_tasks == [task_b]
        assert "agent-1" in state.agent_tasks
        assert state.agent_tasks["agent-1"].task_id == "task-b"
        state.assignment_persistence.remove_assignment.assert_not_awaited()
        assert "task-b" in state.lease_manager.active_leases


class TestBlockedBoardWriteFailureLeavesCoordinationStateIntact:
    """Issue #858, Codex P2 round 2: the board write for a blocked
    report must happen BEFORE coordination state is released, so a
    transient provider failure leaves the assignment and lease intact
    for a retry, rather than releasing first and discovering the write
    failed afterward -- the same ordering ``report_blocker``'s
    terminal give-up path already uses.
    """

    @pytest.mark.asyncio
    async def test_a_failed_board_write_does_not_clear_current_tasks(self) -> None:
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = _make_blocked_state(task)
        state.kanban_client.update_task = AsyncMock(
            side_effect=RuntimeError("provider timeout")
        )

        result = await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=30,
            message="Stuck",
            state=state,
        )

        assert result["success"] is False
        assert result["status"] == "board_write_failed"
        assert state.agent_status["agent-1"].current_tasks == [task], (
            "A failed board write must not release the agent's hold -- "
            "otherwise the retry of this same report would be wrongly "
            "rejected by Guard 3 as a stale report, with no lease left "
            "for the monitor to recover either."
        )

    @pytest.mark.asyncio
    async def test_a_failed_board_write_does_not_delete_the_agent_tasks_entry(
        self,
    ) -> None:
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = _make_blocked_state(task)
        state.kanban_client.update_task = AsyncMock(
            side_effect=RuntimeError("provider timeout")
        )

        await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=30,
            message="Stuck",
            state=state,
        )

        assert "agent-1" in state.agent_tasks
        assert state.agent_tasks["agent-1"].task_id == task.id

    @pytest.mark.asyncio
    async def test_a_failed_board_write_does_not_touch_persistence(self) -> None:
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = _make_blocked_state(task)
        state.kanban_client.update_task = AsyncMock(
            side_effect=RuntimeError("provider timeout")
        )

        await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=30,
            message="Stuck",
            state=state,
        )

        state.assignment_persistence.remove_assignment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_board_write_does_not_release_the_lease(self) -> None:
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = _make_blocked_state(task)
        state.kanban_client.update_task = AsyncMock(
            side_effect=RuntimeError("provider timeout")
        )

        await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=30,
            message="Stuck",
            state=state,
        )

        assert task.id in state.lease_manager.active_leases

    @pytest.mark.asyncio
    async def test_a_retry_after_the_board_write_succeeds_releases_normally(
        self,
    ) -> None:
        """The retry an agent is told to make: same report, this time
        the board write succeeds -- the hold must release exactly as
        the happy path already does."""
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = _make_blocked_state(task)
        state.kanban_client.update_task = AsyncMock(
            side_effect=[RuntimeError("provider timeout"), None]
        )

        first = await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=30,
            message="Stuck",
            state=state,
        )
        assert first["success"] is False

        second = await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=30,
            message="Stuck (retry)",
            state=state,
        )

        assert second["success"] is True
        assert state.agent_status["agent-1"].current_tasks == []
        assert "agent-1" not in state.agent_tasks
        assert task.id not in state.lease_manager.active_leases


class TestGuard3ColdCacheFallback:
    """Issue #858, Codex P2 round 3: Guard 3 must not reject a
    legitimate blocked report from a worker whose ``current_tasks``
    is empty only because of a cold in-memory cache after a server
    restart -- the same cold-cache fallback the "completed" branch's
    own stale-completion guard already uses.
    """

    @pytest.mark.asyncio
    async def test_a_live_lease_holder_with_empty_current_tasks_is_accepted(
        self,
    ) -> None:
        """worker_from_record restores current_tasks=[]; the lease
        manager, rebuilt from persistence, still confirms this agent
        holds the task. The blocked report must go through."""
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = _make_blocked_state(task)
        # Simulate the post-restart cold cache: current_tasks empty,
        # even though the lease manager (rebuilt from persistence)
        # still correctly shows this agent as the live holder.
        state.agent_status["agent-1"].current_tasks = []

        result = await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=30,
            message="Stuck after a restart",
            state=state,
        )

        assert result["success"] is True
        assert result.get("status") != "not_current_assignment"

    @pytest.mark.asyncio
    async def test_a_lease_held_by_a_different_agent_is_still_rejected(
        self,
    ) -> None:
        """The fallback must not become a bypass: if the lease is held
        by someone else, Guard 3 must still reject."""
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = _make_blocked_state(task)
        state.agent_status["agent-1"].current_tasks = []
        state.lease_manager.active_leases[task.id].agent_id = "agent-2"

        result = await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=30,
            message="Stuck after a restart",
            state=state,
        )

        assert result["success"] is False
        assert result["status"] == "not_task_holder"

    @pytest.mark.asyncio
    async def test_no_lease_at_all_and_empty_current_tasks_is_still_rejected(
        self,
    ) -> None:
        """Neither signal confirms the agent holds the task -- the
        genuinely stale case must still be rejected."""
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task()
        state = _make_blocked_state(task)
        state.agent_status["agent-1"].current_tasks = []
        state.lease_manager.active_leases = {}

        result = await report_task_progress(
            agent_id="agent-1",
            task_id=task.id,
            status="blocked",
            progress=30,
            message="Stuck",
            state=state,
        )

        assert result["success"] is False
        assert result["status"] == "not_current_assignment"
