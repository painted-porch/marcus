"""
Unit tests for repair-requeue on terminal blockers (the "middle rung").

Marcus had no step between "an agent gives up on a task" and "the task is
dead forever". A terminal blocker killed the lane on the spot, so the
work it was meant to produce simply never happened — and the only
downstream mitigation was best-effort integration (#629), which routes
around the hole rather than filling it.

This adds the missing rung: a terminally-blocked task goes back on the
board (TODO) carrying the blocker text and Marcus's suggestions, so a
FRESH agent attempts it with the previous agent's diagnostic in hand.
Only after MAX_BLOCKER_REPAIR_ATTEMPTS independent agents have failed is
the lane declared genuinely dead — two agents agreeing is real evidence;
one agent's bad afternoon is not.

The repair counter deliberately does NOT reset on recovery (unlike the
advisory-help budget), because it is the loop guard.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import Task, TaskStatus

pytestmark = pytest.mark.unit


def _make_task(task_id: str, status: TaskStatus, assigned_to: str = "") -> Task:
    """Create a minimal Task object for testing."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.status = status
    task.assigned_to = assigned_to
    task.name = f"Task {task_id}"
    task.labels = []
    task.dependencies = []
    task.recovery_info = None
    return task


def _make_state(agent_id: str, task_id: str) -> MagicMock:
    """Mock Marcus state with the agent holding the task."""
    state = MagicMock()
    state.initialize_kanban = AsyncMock()
    state.provider = "sqlite"

    task = _make_task(task_id, TaskStatus.IN_PROGRESS, assigned_to=agent_id)
    state.project_tasks = [task]
    state.kanban_client = MagicMock()
    state.kanban_client.get_task_by_id = AsyncMock(return_value=task)
    state.kanban_client.update_task = AsyncMock()
    state.kanban_client.add_comment = AsyncMock()

    agent = MagicMock()
    agent.name = agent_id
    agent.current_tasks = [task]
    state.agent_status = {agent_id: agent}

    state.ai_engine = MagicMock()
    state.ai_engine.analyze_blocker = AsyncMock(return_value="Try pip install -e .")

    lease = MagicMock()
    lease.agent_id = agent_id
    state.lease_manager = MagicMock()
    state.lease_manager.active_leases = {task_id: lease}

    assignment = MagicMock()
    assignment.task_id = task_id
    state.agent_tasks = {agent_id: assignment}

    state.assignment_persistence = AsyncMock()
    state.assignment_persistence.remove_assignment = AsyncMock()

    # #206 lock registry: awaitable by default so the release path is
    # exercised rather than swallowed as an error (tests that assert on
    # release override this with their own mock).
    state.file_lock_registry = MagicMock()
    state.file_lock_registry.release = AsyncMock(return_value=0)

    state.memory = None
    state.project_state = MagicMock()
    return state


async def _terminal_blocker(state: MagicMock, agent_id: str, task_id: str) -> dict:
    """Fire one terminal (high-severity) blocker."""
    from src.marcus_mcp.tools.task import report_blocker

    return await report_blocker(
        agent_id=agent_id,
        task_id=task_id,
        blocker_description="cannot resolve the packaging layout",
        severity="high",
        state=state,
        skip_ai_analysis=True,
    )


class TestFirstTerminalBlockerRequeues:
    """A first give-up returns the work to the board, it does not kill it."""

    @pytest.mark.asyncio
    async def test_task_goes_back_to_todo_not_blocked(self) -> None:
        """The board must show TODO so a fresh agent can claim it."""
        from src.marcus_mcp.tools.task import clear_repair_attempts

        clear_repair_attempts("task-1")
        state = _make_state("agent-1", "task-1")

        result = await _terminal_blocker(state, "agent-1", "task-1")

        assert result["success"] is True
        assert result["requeued_for_repair"] is True
        payload = state.kanban_client.update_task.await_args.args[1]
        assert payload["status"] == TaskStatus.TODO
        assert payload["assigned_to"] is None

    @pytest.mark.asyncio
    async def test_reporting_agent_is_released(self) -> None:
        """The agent that gave up is freed either way."""
        from src.marcus_mcp.tools.task import clear_repair_attempts

        clear_repair_attempts("task-1")
        state = _make_state("agent-1", "task-1")

        await _terminal_blocker(state, "agent-1", "task-1")

        assert "agent-1" not in state.agent_tasks
        assert "task-1" not in state.lease_manager.active_leases

    @pytest.mark.asyncio
    async def test_next_agent_receives_the_diagnostic(self) -> None:
        """RecoveryInfo must carry why the previous agent gave up."""
        from src.marcus_mcp.tools.task import clear_repair_attempts

        clear_repair_attempts("task-1")
        state = _make_state("agent-1", "task-1")

        await _terminal_blocker(state, "agent-1", "task-1")

        task = state.project_tasks[0]
        assert task.recovery_info is not None
        assert task.recovery_info.recovery_reason == "blocker_repair"
        assert task.recovery_info.recovered_from_agent == "agent-1"
        instructions = task.recovery_info.instructions
        assert "cannot resolve the packaging layout" in instructions
        assert "agent-1" in instructions


class TestRepairCeiling:
    """Independent agents agreeing is what makes a lane genuinely dead."""

    @pytest.mark.asyncio
    async def test_lane_dies_after_the_cap(self) -> None:
        """Past MAX_BLOCKER_REPAIR_ATTEMPTS the task is finally BLOCKED."""
        from src.marcus_mcp.tools.task import (
            MAX_BLOCKER_REPAIR_ATTEMPTS,
            clear_repair_attempts,
        )

        clear_repair_attempts("task-1")

        for i in range(MAX_BLOCKER_REPAIR_ATTEMPTS):
            state = _make_state(f"agent-{i}", "task-1")
            result = await _terminal_blocker(state, f"agent-{i}", "task-1")
            assert result["requeued_for_repair"] is True

        state = _make_state("agent-final", "task-1")
        final = await _terminal_blocker(state, "agent-final", "task-1")

        assert final["requeued_for_repair"] is False
        payload = state.kanban_client.update_task.await_args.args[1]
        assert payload["status"] == TaskStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_repair_counter_survives_recovery_clear(self) -> None:
        """The loop guard must NOT be reset by lease recovery.

        ``clear_validation_retry`` resets the advisory-help budget so a
        fresh agent is not penalised — but if it also reset the repair
        counter, a task could ping-pong between TODO and blocked forever.
        """
        from src.marcus_mcp.tools.task import (
            _record_repair_attempt,
            clear_repair_attempts,
            clear_validation_retry,
        )

        clear_repair_attempts("task-loop")
        assert _record_repair_attempt("task-loop", "agent-1") == 1
        clear_validation_retry("task-loop")
        assert _record_repair_attempt("task-loop", "agent-2") == 2

    @pytest.mark.asyncio
    async def test_counter_is_per_task(self) -> None:
        """One task's failures must not doom another."""
        from src.marcus_mcp.tools.task import (
            _record_repair_attempt,
            clear_repair_attempts,
        )

        clear_repair_attempts("task-a")
        clear_repair_attempts("task-b")
        _record_repair_attempt("task-a", "agent-1")
        _record_repair_attempt("task-a", "agent-2")
        assert _record_repair_attempt("task-b", "agent-1") == 1


class TestAdvisoryUnaffected:
    """Advisory blockers (#719) must not touch the repair path."""

    @pytest.mark.asyncio
    async def test_advisory_blocker_does_not_requeue(self) -> None:
        """A medium blocker keeps the task with the agent — no requeue."""
        from src.marcus_mcp.tools.task import (
            clear_blocker_attempts,
            clear_repair_attempts,
            report_blocker,
        )

        clear_blocker_attempts("task-1")
        clear_repair_attempts("task-1")
        state = _make_state("agent-1", "task-1")

        result = await report_blocker(
            agent_id="agent-1",
            task_id="task-1",
            blocker_description="need advice",
            severity="medium",
            state=state,
            skip_ai_analysis=True,
        )

        assert result["advisory"] is True
        assert result.get("requeued_for_repair") is None
        assert "agent-1" in state.agent_tasks


class TestRequeueReleasesFileLocks:
    """Codex P1: a requeued task must not keep holding its own file locks.

    Tasks declare the files they write (#206) and hold locks on them while
    in progress. The availability filter skips any TODO task whose declared
    files are locked — with no exemption for locks the task itself holds.
    So a requeue that leaves the locks in place makes the repair task
    permanently unclaimable (and can starve siblings sharing those files),
    which is the exact opposite of this feature's purpose.
    """

    @pytest.mark.asyncio
    async def test_locks_released_on_requeue(self) -> None:
        """The requeue path releases locks like lease recovery does."""
        from src.marcus_mcp.tools.task import clear_repair_attempts

        clear_repair_attempts("task-1")
        state = _make_state("agent-1", "task-1")
        state.file_lock_registry = MagicMock()
        state.file_lock_registry.release = AsyncMock(return_value=2)

        await _terminal_blocker(state, "agent-1", "task-1")

        state.file_lock_registry.release.assert_awaited_once_with("task-1")

    @pytest.mark.asyncio
    async def test_locks_released_when_lane_dies(self) -> None:
        """The terminal path releases them too — a dead lane holds nothing."""
        from src.marcus_mcp.tools.task import (
            MAX_BLOCKER_REPAIR_ATTEMPTS,
            clear_repair_attempts,
        )

        clear_repair_attempts("task-1")
        for i in range(MAX_BLOCKER_REPAIR_ATTEMPTS + 1):
            state = _make_state(f"agent-{i}", "task-1")
            state.file_lock_registry = MagicMock()
            state.file_lock_registry.release = AsyncMock(return_value=0)
            await _terminal_blocker(state, f"agent-{i}", "task-1")

        state.file_lock_registry.release.assert_awaited_once_with("task-1")

    @pytest.mark.asyncio
    async def test_release_failure_never_breaks_the_requeue(self) -> None:
        """A lock-registry error must not lose the repair handoff."""
        from src.marcus_mcp.tools.task import clear_repair_attempts

        clear_repair_attempts("task-1")
        state = _make_state("agent-1", "task-1")
        state.file_lock_registry = MagicMock()
        state.file_lock_registry.release = AsyncMock(side_effect=RuntimeError("boom"))

        result = await _terminal_blocker(state, "agent-1", "task-1")

        assert result["requeued_for_repair"] is True


class TestDistinctAgentsNotRawCalls:
    """Codex P1: the budget counts independent agents, not tool calls.

    After the first requeue removes the lease, the ownership guard only
    rejects a lease held by *someone else* — so a stale agent retrying
    after a lost response could burn the remaining attempts and mark a
    lane dead that no fresh agent ever tried.
    """

    @pytest.mark.asyncio
    async def test_same_agent_repeating_does_not_consume_attempts(self) -> None:
        """Five calls from one agent = one agent having given up."""
        from src.marcus_mcp.tools.task import clear_repair_attempts, report_blocker

        clear_repair_attempts("task-1")

        for _ in range(5):
            state = _make_state("agent-1", "task-1")
            result = await report_blocker(
                agent_id="agent-1",
                task_id="task-1",
                blocker_description="same agent retrying",
                severity="high",
                state=state,
                skip_ai_analysis=True,
            )
            assert (
                result["requeued_for_repair"] is True
            ), "a single agent must never be able to declare the lane dead"

    @pytest.mark.asyncio
    async def test_distinct_agents_each_consume_one(self) -> None:
        """Independent give-ups are what exhaust the budget."""
        from src.marcus_mcp.tools.task import (
            MAX_BLOCKER_REPAIR_ATTEMPTS,
            _record_repair_attempt,
            clear_repair_attempts,
        )

        clear_repair_attempts("task-d")
        assert _record_repair_attempt("task-d", "agent-a") == 1
        assert _record_repair_attempt("task-d", "agent-a") == 1
        assert _record_repair_attempt("task-d", "agent-b") == 2
        assert MAX_BLOCKER_REPAIR_ATTEMPTS >= 2
