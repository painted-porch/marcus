"""
Unit tests for the drain signal (epic #706 Milestone A, launcher design piece 4).

Under the session model a worker registers once and pulls chores in a
loop. The only "go home" signal used to be ``should_exit`` from the
experiment monitor, which D10 deletes; without a replacement a drained
board leaves every worker polling forever. The replacement is a
board-computed verdict, decided by the server in ``request_next_task``:
**no claimable task and no in-flight work** in the agent's project.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.coordinator.drain import DrainVerdict, compute_board_drain
from src.marcus_mcp.tools.task import RETRY_AFTER_SECONDS, RETRY_JITTER_FRACTION

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
PROJECT = "proj-1"


def _in_retry_band(seconds: int) -> bool:
    """True when ``seconds`` is the jittered retry cadence (epic #706).

    The wait is ``RETRY_AFTER_SECONDS`` with a symmetric spread so workers
    do not poll in lockstep; it is never ETA-derived. See
    ``tests/unit/marcus_mcp/test_retry_jitter.py``.
    """
    spread = RETRY_AFTER_SECONDS * RETRY_JITTER_FRACTION
    return (
        round(RETRY_AFTER_SECONDS - spread)
        <= seconds
        <= round(RETRY_AFTER_SECONDS + spread)
    )


def _task(
    task_id: str,
    status: TaskStatus = TaskStatus.TODO,
    *,
    deps: Optional[List[str]] = None,
    assigned_to: Optional[str] = None,
    project_id: Optional[str] = PROJECT,
    is_subtask: bool = False,
    parent_task_id: Optional[str] = None,
    labels: Optional[List[str]] = None,
    name: Optional[str] = None,
) -> Task:
    return Task(
        id=task_id,
        name=name or f"Task {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        labels=labels or [],
        dependencies=deps or [],
        estimated_hours=1.0,
        assigned_to=assigned_to,
        created_at=NOW,
        updated_at=NOW,
        due_date=None,
        project_id=project_id,
        is_subtask=is_subtask,
        parent_task_id=parent_task_id,
    )


def _state(
    tasks: List[Task],
    *,
    leases: Optional[Dict[str, str]] = None,
    lease_manager: bool = True,
    being_assigned: Optional[set] = None,
    persisted: Optional[set] = None,
    subtask_manager: bool = False,
) -> Mock:
    state = Mock()
    state.project_tasks = tasks
    state.tasks_being_assigned = set(being_assigned or set())
    state.assignment_persistence = Mock()
    state.assignment_persistence.get_all_assigned_task_ids = AsyncMock(
        return_value=set(persisted or set())
    )
    if lease_manager:
        state.lease_manager = Mock()
        state.lease_manager.active_leases = {}
        for task_id, agent_id in (leases or {}).items():
            lease = Mock()
            lease.agent_id = agent_id
            lease.task_id = task_id
            state.lease_manager.active_leases[task_id] = lease
    else:
        state.lease_manager = None
    if subtask_manager:
        from src.marcus_mcp.coordinator.subtask_manager import SubtaskManager

        state.subtask_manager = Mock(spec=SubtaskManager)
        state.subtask_manager.has_subtasks = Mock(
            side_effect=lambda task_id, project_tasks=None: any(
                t.is_subtask and t.parent_task_id == task_id for t in tasks
            )
        )
    else:
        state.subtask_manager = None
    return state


class TestVerdictWhenWorkRemains:
    """Anything that could still produce work means: not drained."""

    @pytest.mark.asyncio
    async def test_claimable_task_is_not_drained(self) -> None:
        """A TODO task whose dependencies are done is claimable by someone."""
        state = _state([_task("a", TaskStatus.DONE), _task("b", deps=["a"])])

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is False
        assert verdict.reason == "claimable"
        assert verdict.tasks_claimable == 1

    @pytest.mark.asyncio
    async def test_live_lease_is_in_flight(self) -> None:
        """A task held under a live lease is work in flight."""
        state = _state(
            [_task("a", TaskStatus.IN_PROGRESS), _task("b", deps=["a"])],
            leases={"a": "agent-1"},
        )

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is False
        assert verdict.reason == "in_flight"
        assert verdict.tasks_in_flight == 1

    @pytest.mark.asyncio
    async def test_in_progress_without_lease_is_still_in_flight(self) -> None:
        """Recovery, not the drain signal, decides what happens to an orphan."""
        state = _state([_task("a", TaskStatus.IN_PROGRESS)])

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is False
        assert verdict.reason == "in_flight"

    @pytest.mark.asyncio
    async def test_task_being_assigned_is_in_flight(self) -> None:
        """Between claim and lease creation the task is reserved, not free."""
        state = _state([_task("a")], being_assigned={"a"})

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is False
        assert verdict.reason == "in_flight"

    @pytest.mark.asyncio
    async def test_persisted_assignment_is_in_flight(self) -> None:
        """The assignment store counts even before the lease table catches up."""
        state = _state([_task("a")], persisted={"a"})

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is False
        assert verdict.reason == "in_flight"

    @pytest.mark.asyncio
    async def test_todo_still_assigned_on_board_is_pending_recovery(self) -> None:
        """TODO with assigned_to set is an orphan the reconciler will clear."""
        state = _state([_task("a", assigned_to="agent-gone")])

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is False
        assert verdict.reason == "in_flight"

    @pytest.mark.asyncio
    async def test_empty_board_is_never_drained(self) -> None:
        """No tasks yet means the project is still being created, not finished."""
        state = _state([])

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is False
        assert verdict.reason == "empty"

    @pytest.mark.asyncio
    async def test_without_lease_manager_in_progress_counts_as_live(self) -> None:
        """Deployments with no lease system fall back to board status."""
        state = _state([_task("a", TaskStatus.IN_PROGRESS)], lease_manager=False)

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is False
        assert verdict.reason == "in_flight"

    @pytest.mark.asyncio
    async def test_subtask_with_parent_deps_met_is_claimable(self) -> None:
        """Subtasks are tasks in the unified graph and count when claimable."""
        tasks = [
            _task("up", TaskStatus.DONE),
            _task("p", deps=["up"]),
            _task("p.1", is_subtask=True, parent_task_id="p"),
        ]
        state = _state(tasks, subtask_manager=True)

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is False
        assert verdict.reason == "claimable"
        assert verdict.tasks_claimable == 1  # the subtask, not the parent


class TestVerdictWhenDrained:
    """Nothing claimable and nothing in flight: the workers can go home."""

    @pytest.mark.asyncio
    async def test_all_done_is_complete(self) -> None:
        """Every task DONE, no leases: drained, reason complete."""
        state = _state([_task("a", TaskStatus.DONE), _task("b", TaskStatus.DONE)])

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is True
        assert verdict.reason == "complete"
        assert verdict.tasks_done == 2 and verdict.tasks_total == 2

    @pytest.mark.asyncio
    async def test_dead_lane_with_dependents_is_blocked(self) -> None:
        """A BLOCKED upstream and a TODO that can never become claimable."""
        state = _state([_task("a", TaskStatus.BLOCKED), _task("b", deps=["a"])])

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is True
        assert verdict.reason == "blocked"
        assert verdict.tasks_blocked == 1
        assert verdict.tasks_unclaimable == 1

    @pytest.mark.asyncio
    async def test_lease_on_a_task_of_another_project_does_not_count(self) -> None:
        """Drain is per project; another project's live work is irrelevant."""
        tasks = [
            _task("a", TaskStatus.DONE),
            _task("x", TaskStatus.IN_PROGRESS, project_id="proj-2"),
        ]
        state = _state(tasks, leases={"x": "agent-9"})

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is True
        assert verdict.reason == "complete"
        assert verdict.tasks_total == 1

    @pytest.mark.asyncio
    async def test_legacy_tasks_without_project_id_belong_to_every_project(
        self,
    ) -> None:
        """Scoping matches the assignment filter: project_id None is shared."""
        state = _state(
            [_task("a", TaskStatus.DONE, project_id=None), _task("b", project_id=None)]
        )

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is False
        assert verdict.tasks_total == 2

    @pytest.mark.asyncio
    async def test_decomposed_parent_is_not_itself_claimable(self) -> None:
        """A parent with subtasks is never offered; only its subtasks are."""
        tasks = [
            _task("p"),
            _task("p.1", TaskStatus.DONE, is_subtask=True, parent_task_id="p"),
        ]
        state = _state(tasks, subtask_manager=True)

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.tasks_claimable == 0

    @pytest.mark.asyncio
    async def test_design_slug_dependency_resolves_like_assignment(self) -> None:
        """A dependency written as a design slug is resolved before gating."""
        tasks = [
            _task(
                "42",
                TaskStatus.DONE,
                name="Design productivity tools",
                labels=["design", "architecture", "productivity tools"],
            ),
            _task("b", deps=["design_productivity_tools"]),
        ]
        state = _state(tasks)

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is False
        assert verdict.reason == "claimable"

    @pytest.mark.asyncio
    async def test_subtask_soft_dependency_is_skipped_like_the_selector(
        self,
    ) -> None:
        """The subtask selector ignores soft dependencies; so must the verdict.

        A runnable child behind a soft dependency on a BLOCKED task is
        claimable by someone, so the board is not drained (Codex P2 on
        PR #773).
        """
        parent = _task("p")
        child = _task("p.1", is_subtask=True, parent_task_id="p", deps=["dead"])
        child.dependency_types = ["soft"]
        tasks = [_task("dead", TaskStatus.BLOCKED), parent, child]
        state = _state(tasks, subtask_manager=True)

        with patch(
            "src.marcus_mcp.coordinator.drain._subtasks_enabled", return_value=True
        ):
            verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is False
        assert verdict.reason == "claimable"

    @pytest.mark.asyncio
    async def test_subtask_hard_dependency_on_dead_lane_is_unclaimable(self) -> None:
        """A hard dependency on a BLOCKED task keeps the child unclaimable."""
        parent = _task("p")
        child = _task("p.1", is_subtask=True, parent_task_id="p", deps=["dead"])
        child.dependency_types = ["hard"]
        tasks = [_task("dead", TaskStatus.BLOCKED), parent, child]
        state = _state(tasks, subtask_manager=True)

        with patch(
            "src.marcus_mcp.coordinator.drain._subtasks_enabled", return_value=True
        ):
            verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is True
        assert verdict.reason == "blocked"

    @pytest.mark.asyncio
    async def test_in_progress_decomposed_parent_is_not_live_work(self) -> None:
        """The selector parks a parent in IN_PROGRESS; its children decide liveness.

        One child done, the other waiting on a dead lane, no leases: the
        parent sits IN_PROGRESS forever, and the board is drained, blocked
        (Codex P2 on PR #773).
        """
        parent = _task("p", TaskStatus.IN_PROGRESS)
        tasks = [
            _task("dead", TaskStatus.BLOCKED),
            parent,
            _task("p.1", TaskStatus.DONE, is_subtask=True, parent_task_id="p"),
            _task("p.2", is_subtask=True, parent_task_id="p", deps=["dead"]),
        ]
        state = _state(tasks, subtask_manager=True)

        with patch(
            "src.marcus_mcp.coordinator.drain._subtasks_enabled", return_value=True
        ):
            verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is True
        assert verdict.reason == "blocked"
        assert verdict.tasks_in_flight == 0

    @pytest.mark.asyncio
    async def test_phase_gate_blocks_implementation_behind_a_dead_design(
        self,
    ) -> None:
        """A BLOCKED design task and a dependency-free implementation task that share a feature.

        The assignment filter never offers the implementation while the
        design phase has no DONE task; the verdict must agree, or workers
        retry forever instead of going home (Codex P2 on PR #773).
        """
        tasks = [
            _task(
                "d",
                TaskStatus.BLOCKED,
                name="Design payments API",
                labels=["design", "payments"],
            ),
            _task(
                "i",
                name="Implement payments endpoint",
                labels=["implementation", "payments"],
            ),
        ]
        state = _state(tasks)

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is True
        assert verdict.reason == "blocked"
        assert verdict.tasks_unclaimable == 1

    @pytest.mark.asyncio
    async def test_a_just_assigned_implementation_task_still_blocks_its_phase(
        self,
    ) -> None:
        """Codex P2 on PR #814: a stale in-memory status undercounts correctly.

        ``request_next_task`` writes a task's new status and assignee to
        the board, but a prior version never synced that same task's own
        in-memory object in ``state.project_tasks`` -- it kept reading
        TODO/unassigned until the next ``refresh_project_state``. This
        reproduces the gap directly against the object both the gate and
        the drain verdict actually read: with the implementation task's
        in-memory status still TODO (what it looked like right after
        assignment, before the fix), the phase gate cannot see the
        implementation phase as live and wrongly offers the testing task.
        Setting the in-memory status to IN_PROGRESS -- the fix -- closes
        the gap, matching what ``request_next_task`` does the instant the
        board write for the assignment succeeds.
        """
        stale_impl = _task(
            "i",
            TaskStatus.TODO,
            assigned_to=None,
            name="Implement payments endpoint",
            labels=["implementation", "payments"],
        )
        done_impl = _task(
            "i-done",
            TaskStatus.DONE,
            name="Implement payments backend",
            labels=["implementation", "payments"],
        )
        testing = _task(
            "t",
            name="Test payments endpoint",
            labels=["testing", "payments"],
        )
        state = _state([stale_impl, done_impl, testing])

        from src.marcus_mcp.tools.task import apply_board_wide_gates

        scoped = [stale_impl, done_impl, testing]
        tasks_by_id = {t.id: t for t in scoped}

        allowed_while_stale = apply_board_wide_gates(
            list(scoped), scoped, tasks_by_id, state
        )
        assert testing in allowed_while_stale, (
            "the bug: the testing task is wrongly offered because the "
            "just-assigned implementation task still reads TODO in memory"
        )

        stale_impl.status = TaskStatus.IN_PROGRESS
        stale_impl.assigned_to = "agent-1"

        allowed_once_synced = apply_board_wide_gates(
            list(scoped), scoped, tasks_by_id, state
        )
        assert testing not in allowed_once_synced

        fixed_verdict = await compute_board_drain(state, PROJECT)
        assert fixed_verdict.tasks_claimable == 0
        assert fixed_verdict.tasks_in_flight == 1
        assert fixed_verdict.tasks_unclaimable == 1

    @pytest.mark.asyncio
    async def test_readme_alone_is_claimable_like_the_selector(self) -> None:
        """README is held back until 90% done unless it is the only task left."""
        tasks = [
            _task("a", TaskStatus.BLOCKED),
            _task("b", TaskStatus.BLOCKED),
            _task("r", name="Write README documentation", labels=["documentation"]),
        ]
        state = _state(tasks)

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.drained is False
        assert verdict.reason == "claimable"

    def test_verdict_serializes(self) -> None:
        """The verdict rides inside the MCP response as plain data."""
        verdict = DrainVerdict(
            drained=True,
            reason="complete",
            tasks_total=3,
            tasks_done=3,
            tasks_blocked=0,
            tasks_claimable=0,
            tasks_in_flight=0,
            tasks_unclaimable=0,
        )
        data = verdict.to_dict()
        assert data["drained"] is True and data["reason"] == "complete"
        assert data["tasks_total"] == 3


# ---------------------------------------------------------------------------
# request_next_task wiring
# ---------------------------------------------------------------------------


def _request_state(tasks: List[Task], **kwargs: Any) -> Mock:
    """A state that reaches the no-task path of request_next_task."""
    state = _state(tasks, **kwargs)
    state.ensure_lease_monitor_running = AsyncMock()
    state.ensure_registrations_loaded = AsyncMock()
    state.registration_persistence = None
    state.log_event = Mock()
    state.initialize_kanban = AsyncMock()
    state.refresh_project_state = AsyncMock()
    state.agent_status = {}
    state.agent_tasks = {}
    state.gridlock_detector = None
    state.agent_project_map = {"agent-1": PROJECT}
    state.kanban_client = Mock()
    state.project_state = None
    state.ai_engine = None
    state.provider = "sqlite"
    state.code_analyzer = None
    state.context = None
    state.assignment_lock = asyncio.Lock()
    return state


def _no_task_patches() -> Any:
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(
            "src.marcus_mcp.tools.task.find_optimal_task_for_agent",
            new=AsyncMock(return_value=None),
        )
    )
    stack.enter_context(
        patch(
            "src.marcus_mcp.tools.task.get_project_board_context",
            new=AsyncMock(return_value={}),
        )
    )
    stack.enter_context(patch("src.marcus_mcp.tools.task.conversation_logger"))
    stack.enter_context(patch("src.marcus_mcp.tools.task.log_agent_event"))
    stack.enter_context(patch("src.marcus_mcp.tools.task.log_thinking"))
    return stack


class TestRequestNextTaskDrain:
    """The verdict is returned by request_next_task, decided by the server."""

    @pytest.mark.asyncio
    async def test_drained_board_returns_board_drained_with_should_exit(
        self,
    ) -> None:
        """All done, nothing live: the worker is told to go home."""
        from src.marcus_mcp.tools.task import request_next_task

        state = _request_state([_task("a", TaskStatus.DONE)])

        with _no_task_patches():
            result = await request_next_task(agent_id="agent-1", state=state)

        assert result["success"] is False
        assert result["status"] == "BOARD_DRAINED"
        assert result["should_exit"] is True
        assert result["drain"]["reason"] == "complete"
        assert "exit" in result["message"].lower()
        assert "retry_after_seconds" not in result

    @pytest.mark.asyncio
    async def test_blocked_board_says_so_in_the_message(self) -> None:
        """A drained-but-unfinished board names the unfinished work."""
        from src.marcus_mcp.tools.task import request_next_task

        state = _request_state([_task("a", TaskStatus.BLOCKED), _task("b", deps=["a"])])

        with _no_task_patches():
            result = await request_next_task(agent_id="agent-1", state=state)

        assert result["status"] == "BOARD_DRAINED"
        assert result["drain"]["reason"] == "blocked"
        assert result["drain"]["tasks_blocked"] == 1
        assert (
            "cannot" in result["message"].lower()
            or "blocked" in result["message"].lower()
        )

    @pytest.mark.asyncio
    async def test_work_in_flight_still_returns_retry(self) -> None:
        """A live lease elsewhere means wait, then ask again."""
        from src.marcus_mcp.tools.task import request_next_task

        state = _request_state(
            [_task("a", TaskStatus.IN_PROGRESS), _task("b", deps=["a"])],
            leases={"a": "agent-2"},
        )

        with _no_task_patches():
            result = await request_next_task(agent_id="agent-1", state=state)

        assert result["success"] is False
        assert "should_exit" not in result
        assert _in_retry_band(result["retry_after_seconds"])
        assert result["drain"]["reason"] == "in_flight"

    async def test_unregistered_agent_never_gets_drained(self) -> None:
        """No project: no verdict; the usual retry applies."""
        from src.marcus_mcp.tools.task import request_next_task

        state = _request_state([_task("a", TaskStatus.DONE)])
        state.agent_project_map = {}

        with _no_task_patches():
            result = await request_next_task(agent_id="agent-1", state=state)

        assert "should_exit" not in result
        assert _in_retry_band(result["retry_after_seconds"])

    @pytest.mark.asyncio
    async def test_drain_computation_failure_falls_back_to_retry(self) -> None:
        """A bug in the verdict must never strand or eject a worker."""
        from src.marcus_mcp.tools.task import request_next_task

        state = _request_state([_task("a", TaskStatus.DONE)])

        with (
            _no_task_patches(),
            patch(
                "src.marcus_mcp.tools.task.compute_board_drain",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            result = await request_next_task(agent_id="agent-1", state=state)

        assert "should_exit" not in result
        assert _in_retry_band(result["retry_after_seconds"])


class TestDrainClosesTheRun:
    """The board's verdict, not a tool call, closes the cost-store run."""

    @pytest.mark.asyncio
    async def test_complete_drain_closes_the_latest_open_run(self) -> None:
        """A drained board stamps ended_at and the final counts on the run."""
        from src.marcus_mcp.tools.task import request_next_task

        tasks = [_task("a", TaskStatus.DONE), _task("b", TaskStatus.DONE)]
        state = _request_state(tasks)
        store = Mock()
        store.close_current_run_for_project = Mock(return_value="run-1")
        recorder = Mock(store=store)

        with (
            _no_task_patches(),
            patch(
                "src.cost_tracking.cost_recorder.get_recorder", return_value=recorder
            ),
        ):
            result = await request_next_task("agent-1", state)

        assert result["status"] == "BOARD_DRAINED"
        kwargs = store.close_current_run_for_project.call_args.kwargs
        assert kwargs["project_id"] == PROJECT
        assert kwargs["total_tasks"] == 2
        assert kwargs["completed_tasks"] == 2
        assert kwargs["blocked_tasks"] == 0
        assert kwargs["ended_at"] is not None

    @pytest.mark.asyncio
    async def test_cost_store_failure_does_not_block_the_drain(self) -> None:
        """Bookkeeping errors are logged; the worker still gets BOARD_DRAINED."""
        from src.marcus_mcp.tools.task import request_next_task

        tasks = [_task("a", TaskStatus.DONE)]
        state = _request_state(tasks)

        with (
            _no_task_patches(),
            patch(
                "src.cost_tracking.cost_recorder.get_recorder",
                side_effect=RuntimeError("no store"),
            ),
        ):
            result = await request_next_task("agent-1", state)

        assert result["status"] == "BOARD_DRAINED"
        assert result["should_exit"] is True

    @pytest.mark.asyncio
    async def test_cost_summary_fires_once_when_a_run_closes(self) -> None:
        """The project_cost_summary event rides on the real close, flattened."""
        from src.marcus_mcp.tools.task import request_next_task

        tasks = [_task("a", TaskStatus.DONE)]
        state = _request_state(tasks)
        store = Mock()
        store.close_current_run_for_project = Mock(return_value="run-1")
        recorder = Mock(store=store)
        aggregator = Mock()
        aggregator.project_summary = Mock(
            return_value={
                "summary": {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cache_read_tokens": 50,
                    "cache_creation_tokens": 5,
                    "total_cost_usd": 0.1234,
                },
                "by_task": [{"task_id": "a"}],
            }
        )

        with (
            _no_task_patches(),
            patch(
                "src.cost_tracking.cost_recorder.get_recorder", return_value=recorder
            ),
            patch(
                "src.cost_tracking.cost_aggregator.CostAggregator",
                return_value=aggregator,
            ),
            patch("src.telemetry.events.fire_project_cost_summary") as fire,
        ):
            await request_next_task("agent-1", state)

        fire.assert_called_once_with(
            {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_tokens": 50,
                "cache_creation_tokens": 5,
                "cost_usd_cents": 12,
                "task_count": 1,
            }
        )

    @pytest.mark.asyncio
    async def test_repeated_drain_does_not_close_or_fire_again(self) -> None:
        """A second worker's drain finds the run already closed: no close, no event."""
        from src.marcus_mcp.tools.task import request_next_task

        tasks = [_task("a", TaskStatus.DONE)]
        state = _request_state(tasks)
        store = Mock()
        store.close_current_run_for_project = Mock(return_value=None)
        recorder = Mock(store=store)

        with (
            _no_task_patches(),
            patch(
                "src.cost_tracking.cost_recorder.get_recorder", return_value=recorder
            ),
            patch("src.telemetry.events.fire_project_cost_summary") as fire,
        ):
            result = await request_next_task("agent-1", state)

        assert result["status"] == "BOARD_DRAINED"
        fire.assert_not_called()


def _conflicted(task_id: str, minutes_ago: float, stamp: Any = None) -> Task:
    """A BLOCKED task carrying a merge_conflict record stamped ``minutes_ago``."""
    from datetime import timedelta

    task = _task(task_id, TaskStatus.BLOCKED)
    when = (
        stamp
        if stamp is not None
        else (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    )
    task.source_context = {
        "merge_conflict": {
            "agent_id": "agent-1",
            "branch": "marcus/agent-1",
            "blocked_at": when,
        }
    }
    return task


class TestMergeConflictIsInFlight:
    """A merge conflict being resolved by its agent is not a dead lane.

    The completion path marks the card BLOCKED (#651) and tells the agent
    to resolve and re-report; the lease is already released, so nothing
    else says the card is alive. A live run drained on this card while the
    agent was mid-resolution, sending the other session home early.
    """

    @pytest.mark.asyncio
    async def test_fresh_conflict_counts_as_in_flight(self) -> None:
        """Stamped a minute ago: wait, do not drain."""
        tasks = [_task("a", TaskStatus.DONE), _conflicted("b", minutes_ago=1)]
        verdict = await compute_board_drain(_state(tasks), PROJECT)
        assert verdict.drained is False
        assert verdict.reason == "in_flight"
        assert verdict.tasks_in_flight == 1
        assert verdict.tasks_blocked == 0

    @pytest.mark.asyncio
    async def test_conflict_older_than_the_silence_timeout_is_blocked(self) -> None:
        """Past the D3 dial the agent is presumed gone: a dead lane."""
        tasks = [_task("a", TaskStatus.DONE), _conflicted("b", minutes_ago=46)]
        verdict = await compute_board_drain(_state(tasks), PROJECT)
        assert verdict.drained is True
        assert verdict.reason == "blocked"
        assert verdict.tasks_blocked == 1

    @pytest.mark.asyncio
    async def test_window_follows_the_lease_managers_silence_timeout(self) -> None:
        """A shorter configured timeout shortens the grace window."""
        tasks = [_task("a", TaskStatus.DONE), _conflicted("b", minutes_ago=10)]
        state = _state(tasks)
        state.lease_manager.silence_timeout_minutes = 5.0
        verdict = await compute_board_drain(state, PROJECT)
        assert verdict.drained is True and verdict.reason == "blocked"

    @pytest.mark.asyncio
    async def test_blocked_without_a_conflict_record_is_still_blocked(self) -> None:
        """An ordinary BLOCKED card (terminal blocker) is unchanged."""
        tasks = [_task("a", TaskStatus.DONE), _task("b", TaskStatus.BLOCKED)]
        verdict = await compute_board_drain(_state(tasks), PROJECT)
        assert verdict.drained is True and verdict.reason == "blocked"

    @pytest.mark.asyncio
    async def test_malformed_stamp_is_treated_as_blocked(self) -> None:
        """A record without a readable time cannot keep the board alive forever."""
        tasks = [_task("a", TaskStatus.DONE), _conflicted("b", 0, stamp="not-a-time")]
        verdict = await compute_board_drain(_state(tasks), PROJECT)
        assert verdict.drained is True and verdict.reason == "blocked"
