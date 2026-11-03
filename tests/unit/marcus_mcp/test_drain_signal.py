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

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
PROJECT = "proj-1"


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
        assert result["retry_after_seconds"] == 30
        assert result["drain"]["reason"] == "in_flight"

    async def test_unregistered_agent_never_gets_drained(self) -> None:
        """No project: no verdict; the usual retry applies."""
        from src.marcus_mcp.tools.task import request_next_task

        state = _request_state([_task("a", TaskStatus.DONE)])
        state.agent_project_map = {}

        with _no_task_patches():
            result = await request_next_task(agent_id="agent-1", state=state)

        assert "should_exit" not in result
        assert result["retry_after_seconds"] == 30

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
        assert result["retry_after_seconds"] == 30


class TestDrainClosesTheRun:
    """The board's verdict, not a tool call, closes the cost-store run."""

    @pytest.mark.asyncio
    async def test_complete_drain_closes_the_latest_open_run(self) -> None:
        """A drained board stamps ended_at and the final counts on the run."""
        from src.marcus_mcp.tools.task import request_next_task

        tasks = [_task("a", TaskStatus.DONE), _task("b", TaskStatus.DONE)]
        state = _request_state(tasks)
        store = Mock()
        store.close_latest_open_run_for_project = Mock(return_value="run-1")
        recorder = Mock(store=store)

        with (
            _no_task_patches(),
            patch(
                "src.cost_tracking.cost_recorder.get_recorder", return_value=recorder
            ),
        ):
            result = await request_next_task("agent-1", state)

        assert result["status"] == "BOARD_DRAINED"
        kwargs = store.close_latest_open_run_for_project.call_args.kwargs
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
