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

import asyncio
import itertools
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import Task, TaskStatus


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


_EPOCHS = itertools.count(1)


def _make_state(
    agent_id: str,
    task_id: str,
    all_agents: Optional[List[str]] = None,
) -> MagicMock:
    """Mock Marcus state with the agent holding the task.

    Parameters
    ----------
    agent_id : str
        The agent that holds the task and will report the blocker.
    task_id : str
        The task under test.
    all_agents : Optional[List[str]]
        The full pool of agents registered with Marcus (``state.agent_status``).
        Defaults to ``[agent_id]``, a lone-agent board. The repair decision
        no longer reads this pool (issue #769 is bounded by a total requeue
        budget instead), but the board shape is kept so the tests still
        describe the scenario they model.
    """
    state = MagicMock()
    state.initialize_kanban = AsyncMock()
    state.provider = "sqlite"

    task = _make_task(task_id, TaskStatus.IN_PROGRESS, assigned_to=agent_id)
    state.project_tasks = [task]
    state.kanban_client = MagicMock()
    state.kanban_client.get_task_by_id = AsyncMock(return_value=task)
    state.kanban_client.update_task = AsyncMock()
    state.kanban_client.add_comment = AsyncMock()

    pool = all_agents if all_agents is not None else [agent_id]
    agent_status = {}
    for aid in pool:
        member = MagicMock()
        member.name = aid
        # Only the reporting agent holds the task; the rest are idle and
        # therefore count as fresh candidates for a repair retry.
        member.current_tasks = [task] if aid == agent_id else []
        agent_status[aid] = member
    agent = agent_status[agent_id]
    state.agent_status = agent_status

    state.ai_engine = MagicMock()
    state.ai_engine.analyze_blocker = AsyncMock(return_value="Try pip install -e .")

    lease = MagicMock()
    lease.agent_id = agent_id
    # Every _make_state is a fresh claim, and create_lease issues a new
    # fencing epoch per claim; the terminal give-up is accepted once per epoch.
    lease.lease_epoch = next(_EPOCHS)
    state.lease_manager = MagicMock()
    state.lease_manager.active_leases = {task_id: lease}
    _wire_release_lease(state.lease_manager)

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


async def _terminal_blocker(
    state: MagicMock, agent_id: str, task_id: str
) -> Dict[str, Any]:
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
    """A first give-up returns the work to the board, it does not kill it.

    These model a multi-agent board (``all_agents`` names a second, idle
    agent). A lone-agent board behaves the same on the first give-up; how
    its loop is bounded (issue #769) is covered by ``TestBoundedRepairBudget``.
    """

    @pytest.mark.asyncio
    async def test_task_goes_back_to_todo_not_blocked(self) -> None:
        """The board must show TODO so a fresh agent can claim it."""
        from src.marcus_mcp.tools.task import clear_repair_attempts

        clear_repair_attempts("task-1")
        state = _make_state("agent-1", "task-1", all_agents=["agent-1", "agent-2"])

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
        state = _make_state("agent-1", "task-1", all_agents=["agent-1", "agent-2"])

        await _terminal_blocker(state, "agent-1", "task-1")

        task = state.project_tasks[0]
        assert task.recovery_info is not None
        assert task.recovery_info.recovery_reason == "blocker_repair"
        assert task.recovery_info.recovered_from_agent == "agent-1"
        instructions = task.recovery_info.instructions
        assert "cannot resolve the packaging layout" in instructions
        assert "agent-1" in instructions

    @pytest.mark.asyncio
    async def test_the_repair_requeue_is_tagged_implementation_gap(self) -> None:
        """#809: a blocker-repair requeue is always the agent giving up
        on execution, never a verifier disputing the requirement or the
        environment model -- so it is tagged accordingly, consistent
        with request_task_redo's default for the one repair this tool
        actually performs.
        """
        from src.marcus_mcp.tools.task import clear_repair_attempts

        clear_repair_attempts("task-1")
        state = _make_state("agent-1", "task-1", all_agents=["agent-1", "agent-2"])

        await _terminal_blocker(state, "agent-1", "task-1")

        task = state.project_tasks[0]
        assert task.recovery_info.defect_type == "implementation_gap"


class TestRepairCeiling:
    """Independent agents agreeing is what makes a lane genuinely dead."""

    @pytest.mark.asyncio
    async def test_lane_dies_after_the_cap(self) -> None:
        """Past MAX_BLOCKER_REPAIR_ATTEMPTS the task is finally BLOCKED.

        A large pool keeps a fresh agent available at every step, so the
        CEILING (distinct give-ups > ``MAX_BLOCKER_REPAIR_ATTEMPTS``) — not
        the lone-agent floor (#769) — is what ends the lane here.
        """
        from src.marcus_mcp.tools.task import (
            MAX_BLOCKER_REPAIR_ATTEMPTS,
            clear_repair_attempts,
        )

        clear_repair_attempts("task-1")
        pool = [f"agent-{i}" for i in range(MAX_BLOCKER_REPAIR_ATTEMPTS)]
        pool.append("agent-final")

        for i in range(MAX_BLOCKER_REPAIR_ATTEMPTS):
            state = _make_state(f"agent-{i}", "task-1", all_agents=pool)
            result = await _terminal_blocker(state, f"agent-{i}", "task-1")
            assert result["requeued_for_repair"] is True

        state = _make_state("agent-final", "task-1", all_agents=pool)
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
        # A fresh agent must exist for a requeue to happen at all (#769).
        state = _make_state("agent-1", "task-1", all_agents=["agent-1", "agent-2"])
        state.file_lock_registry = MagicMock()
        state.file_lock_registry.release = AsyncMock(return_value=2)

        result = await _terminal_blocker(state, "agent-1", "task-1")

        assert result["requeued_for_repair"] is True
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
        # A fresh agent must exist for a requeue to happen at all (#769).
        state = _make_state("agent-1", "task-1", all_agents=["agent-1", "agent-2"])
        state.file_lock_registry = MagicMock()
        state.file_lock_registry.release = AsyncMock(side_effect=RuntimeError("boom"))

        result = await _terminal_blocker(state, "agent-1", "task-1")

        assert result["requeued_for_repair"] is True


class TestDistinctAgentsNotRawCalls:
    """The distinct-agent ceiling counts independent agents, not tool calls."""

    @pytest.mark.asyncio
    async def test_stale_duplicate_without_a_lease_is_refused_and_counts_nothing(
        self,
    ) -> None:
        """A retry after a lost response arrives with no live lease.

        The requeue released the lease, so the duplicate is refused and
        consumes neither the ceiling nor the budget; a lane is never killed
        by a give-up nobody made.
        """
        from src.marcus_mcp.tools.task import (
            _blocker_repair_agents,
            _blocker_repair_requeues,
            clear_repair_attempts,
            report_blocker,
        )

        clear_repair_attempts("task-1")
        state = _make_state("agent-1", "task-1")
        first = await _terminal_blocker(state, "agent-1", "task-1")
        assert first["requeued_for_repair"] is True

        stale = _make_state("agent-1", "task-1")
        stale.lease_manager.active_leases = {}  # the requeue released it
        result = await report_blocker(
            agent_id="agent-1",
            task_id="task-1",
            blocker_description="same report, retried",
            severity="high",
            state=stale,
            skip_ai_analysis=True,
        )

        assert result["success"] is False
        assert result["status"] == "lease_recovered"
        assert _blocker_repair_agents["task-1"] == {"agent-1"}
        assert _blocker_repair_requeues["task-1"] == 1
        stale.kanban_client.update_task.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_concurrent_duplicates_on_one_lease_spend_the_budget_once(
        self,
    ) -> None:
        """Overlapping copies of the same give-up count as one.

        All of them pass the live-lease check (the lease is released only
        after the board write), so acceptance is keyed on the lease epoch:
        the first copy takes the mark before its first await, the rest are
        refused and consume neither bound (Codex P2 on PR #770).
        """
        from src.marcus_mcp.tools.task import (
            MAX_BLOCKER_REPAIR_REQUEUES,
            _blocker_repair_requeues,
            clear_repair_attempts,
        )

        clear_repair_attempts("task-1")
        state = _make_state("agent-1", "task-1")

        async def _board_write_yields(*args: Any, **kwargs: Any) -> None:
            # A real board write suspends; a bare AsyncMock does not, and
            # without a suspension the first report would run to completion
            # (lease released) before any duplicate started, hiding the race.
            await asyncio.sleep(0)

        state.kanban_client.update_task = AsyncMock(side_effect=_board_write_yields)

        results = await asyncio.gather(
            *[
                _terminal_blocker(state, "agent-1", "task-1")
                for _ in range(MAX_BLOCKER_REPAIR_REQUEUES + 1)
            ]
        )

        accepted = [r for r in results if r.get("requeued_for_repair") is True]
        refused = [r for r in results if r.get("duplicate") is True]
        assert len(accepted) == 1
        assert len(refused) == MAX_BLOCKER_REPAIR_REQUEUES
        assert all(r["status"] == "lease_recovered" for r in refused)
        assert _blocker_repair_requeues["task-1"] == 1

    @pytest.mark.asyncio
    async def test_failed_board_write_rolls_back_acceptance(self) -> None:
        """A handoff that never reached the board is not a give-up.

        When update_task raises, the lease is still held, so the agent's
        retry must be accepted (not refused as a duplicate) and the counters
        must show nothing for the failed attempt (Codex P2 on PR #770).
        """
        from src.marcus_mcp.tools.task import (
            _blocker_repair_agents,
            _blocker_repair_consumed_epoch,
            _blocker_repair_requeues,
            clear_repair_attempts,
        )

        clear_repair_attempts("task-1")
        state = _make_state("agent-1", "task-1")
        task_obj = state.project_tasks[0]
        state.kanban_client.update_task = AsyncMock(
            side_effect=RuntimeError("provider down")
        )

        failed = await _terminal_blocker(state, "agent-1", "task-1")

        assert failed["success"] is False
        assert "requeued_for_repair" not in failed
        assert "task-1" not in _blocker_repair_consumed_epoch
        assert "task-1" not in _blocker_repair_agents
        assert "task-1" not in _blocker_repair_requeues
        assert task_obj.status == TaskStatus.IN_PROGRESS
        assert task_obj.assigned_to == "agent-1"
        assert "task-1" in state.lease_manager.active_leases

        state.kanban_client.update_task = AsyncMock()
        retried = await _terminal_blocker(state, "agent-1", "task-1")

        assert retried["requeued_for_repair"] is True
        assert _blocker_repair_requeues["task-1"] == 1

    @pytest.mark.asyncio
    async def test_failed_lane_death_write_rolls_back_acceptance(self) -> None:
        """Same rollback on the lane-death branch: the retry still ends the lane."""
        from src.marcus_mcp.tools.task import (
            MAX_BLOCKER_REPAIR_REQUEUES,
            _blocker_repair_consumed_epoch,
            clear_repair_attempts,
        )

        clear_repair_attempts("task-1")
        for _ in range(MAX_BLOCKER_REPAIR_REQUEUES):
            await _terminal_blocker(
                _make_state("agent-1", "task-1"), "agent-1", "task-1"
            )
        state = _make_state("agent-1", "task-1")
        state.subtask_manager = MagicMock()
        state.subtask_manager.subtasks = {"task-1": MagicMock()}
        state.kanban_client.update_task = AsyncMock(
            side_effect=RuntimeError("provider down")
        )

        failed = await _terminal_blocker(state, "agent-1", "task-1")

        assert failed["success"] is False
        assert "task-1" not in _blocker_repair_consumed_epoch
        assert state.project_tasks[0].status == TaskStatus.IN_PROGRESS
        # The subtask store persists on write, so nothing may reach it
        # until the board write has succeeded.
        state.subtask_manager.update_subtask_status.assert_not_called()

        state.kanban_client.update_task = AsyncMock()
        final = await _terminal_blocker(state, "agent-1", "task-1")

        assert final["requeued_for_repair"] is False
        payload = state.kanban_client.update_task.await_args.args[1]
        assert payload["status"] == TaskStatus.BLOCKED
        state.subtask_manager.update_subtask_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_distinct_agents_each_consume_one(self) -> None:
        """Independent give-ups are what exhaust the ceiling."""
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


class TestBoundedRepairBudget:
    """Issue #769: a lone persistent agent must not be re-offered a task forever.

    The distinct-agent ceiling cannot be reached when the pool is smaller
    than it. A second, agent-blind bound ends the loop: a task is re-offered
    at most ``MAX_BLOCKER_REPAIR_REQUEUES`` times after give-ups, whoever
    gave up. The bound deliberately reads no snapshot of who is registered:
    the spawn-per-task runner also shows one agent at give-up time, and a
    snapshot rule killed lanes the next spawned agent would have repaired
    (ultrareview on PR #770).
    """

    @pytest.mark.asyncio
    async def test_lone_agent_first_give_up_still_requeues(self) -> None:
        """One agent on the board: the first give-up is NOT lane death.

        This is the spawn-per-task runner's shape too; killing here would
        remove the #722 repair rung.
        """
        from src.marcus_mcp.tools.task import clear_repair_attempts

        clear_repair_attempts("task-1")
        state = _make_state("agent-1", "task-1")

        result = await _terminal_blocker(state, "agent-1", "task-1")

        assert result["requeued_for_repair"] is True
        payload = state.kanban_client.update_task.await_args.args[1]
        assert payload["status"] == TaskStatus.TODO

    @pytest.mark.asyncio
    async def test_lone_agent_loop_ends_at_the_budget(self) -> None:
        """A lone agent that keeps claiming and giving up is stopped after the budget."""
        from src.marcus_mcp.tools.task import (
            MAX_BLOCKER_REPAIR_REQUEUES,
            clear_repair_attempts,
        )

        clear_repair_attempts("task-1")
        for _ in range(MAX_BLOCKER_REPAIR_REQUEUES):
            state = _make_state("agent-1", "task-1")  # re-claimed: a live lease again
            result = await _terminal_blocker(state, "agent-1", "task-1")
            assert result["requeued_for_repair"] is True

        state = _make_state("agent-1", "task-1")
        final = await _terminal_blocker(state, "agent-1", "task-1")

        assert final["requeued_for_repair"] is False
        payload = state.kanban_client.update_task.await_args.args[1]
        assert payload["status"] == TaskStatus.BLOCKED
        assert "handed back" in final["message"]

    @pytest.mark.asyncio
    async def test_two_agents_ping_pong_ends_at_the_budget(self) -> None:
        """Two agents under the ceiling can no longer ping-pong a task forever."""
        from src.marcus_mcp.tools.task import (
            MAX_BLOCKER_REPAIR_REQUEUES,
            clear_repair_attempts,
        )

        clear_repair_attempts("task-1")
        agents = ["agent-a", "agent-b"]
        outcomes = []
        for i in range(MAX_BLOCKER_REPAIR_REQUEUES + 1):
            who = agents[i % 2]
            state = _make_state(who, "task-1")
            result = await _terminal_blocker(state, who, "task-1")
            outcomes.append(result["requeued_for_repair"])

        assert outcomes == [True] * MAX_BLOCKER_REPAIR_REQUEUES + [False]

    @pytest.mark.asyncio
    async def test_lane_death_write_is_complete(self) -> None:
        """Dead means dead everywhere: memory, subtask store, card, provider column."""
        from src.marcus_mcp.tools.task import (
            MAX_BLOCKER_REPAIR_REQUEUES,
            clear_repair_attempts,
        )

        clear_repair_attempts("task-1")
        for _ in range(MAX_BLOCKER_REPAIR_REQUEUES):
            await _terminal_blocker(
                _make_state("agent-1", "task-1"), "agent-1", "task-1"
            )
        state = _make_state("agent-1", "task-1")
        task_obj = state.project_tasks[0]
        state.subtask_manager = MagicMock()
        state.subtask_manager.subtasks = {"task-1": MagicMock()}
        state.kanban_client.move_task_to_column = AsyncMock()

        await _terminal_blocker(state, "agent-1", "task-1")

        assert task_obj.status == TaskStatus.BLOCKED
        assert task_obj.assigned_to is None
        state.subtask_manager.update_subtask_status.assert_called_once()
        assert (
            state.subtask_manager.update_subtask_status.call_args.args[1]
            == TaskStatus.BLOCKED
        )
        payload = state.kanban_client.update_task.await_args.args[1]
        assert payload["status"] == TaskStatus.BLOCKED
        assert payload["assigned_to"] is None
        state.kanban_client.move_task_to_column.assert_awaited_once_with(
            "task-1", "blocked"
        )

    @pytest.mark.asyncio
    async def test_lane_death_tolerates_a_provider_without_blocked_column(
        self,
    ) -> None:
        """A provider lacking move_task_to_column still gets the card write."""
        from src.marcus_mcp.tools.task import (
            MAX_BLOCKER_REPAIR_REQUEUES,
            clear_repair_attempts,
        )

        clear_repair_attempts("task-1")
        for _ in range(MAX_BLOCKER_REPAIR_REQUEUES):
            await _terminal_blocker(
                _make_state("agent-1", "task-1"), "agent-1", "task-1"
            )
        state = _make_state("agent-1", "task-1")
        del state.kanban_client.move_task_to_column

        final = await _terminal_blocker(state, "agent-1", "task-1")

        assert final["requeued_for_repair"] is False
        payload = state.kanban_client.update_task.await_args.args[1]
        assert payload["status"] == TaskStatus.BLOCKED
