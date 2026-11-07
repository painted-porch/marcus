"""
Unit tests for stranded assignments (issue #900).

A card is *stranded* when the board says ``TODO``, names an assignee, and
nothing in the kernel holds it: no live lease, no reservation, no persisted
assignment row.
The eligibility filter used to skip such a card forever ("assigned on the
board"), the drain verdict counted it as work in flight on the theory that
the reconciler would clear it, and the reconciler only touches
``IN_PROGRESS`` orphans and persisted rows.
So one stranded card made a board that could never drain, with a task that
could never be offered or finished, silently blocking its dependents.

The kernel's own definition of "held" is the lease, the reservation and the
persisted row; a ``TODO`` card's assignee with none of those is residue.
These tests pin the three parts of the fix: count it (verdict, frontier
reply, drained message), reclaim it (the filter offers it and the
assignment write records who held it), and leave coordinator-owned cards
counted but never offered so the design-ghost path (#619) is unchanged.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus, WorkerStatus
from src.core.task_claimability import COORDINATOR_ASSIGNEE
from src.marcus_mcp.coordinator.drain import (
    DrainVerdict,
    compute_board_drain,
    find_stranded_assignments,
)
from src.marcus_mcp.tools import task as task_module
from src.marcus_mcp.tools.task import (
    _find_optimal_task_original_logic,
    _frontier_for_agent,
    assignment_write_payload,
    reclaim_stamp,
    request_next_task,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
PROJECT = "proj-1"


def _task(
    task_id: str,
    status: TaskStatus = TaskStatus.TODO,
    *,
    deps: Optional[List[str]] = None,
    assigned_to: Optional[str] = None,
    project_id: Optional[str] = PROJECT,
    source_context: Optional[Dict[str, Any]] = None,
) -> Task:
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=assigned_to,
        created_at=NOW,
        updated_at=NOW,
        due_date=None,
        estimated_hours=1.0,
        dependencies=list(deps or []),
        labels=[],
        project_id=project_id,
        source_context=source_context,
    )


def _worker(worker_id: str) -> WorkerStatus:
    return WorkerStatus(
        worker_id=worker_id,
        name=worker_id,
        role="developer",
        email=None,
        current_tasks=[],
        completed_tasks_count=0,
        capacity=40,
        skills=[],
        availability={},
        performance_score=1.0,
    )


def _state(
    tasks: List[Task],
    *,
    leases: Optional[Dict[str, str]] = None,
    being_assigned: Optional[set] = None,
    persisted: Optional[set] = None,
    workers: Optional[List[WorkerStatus]] = None,
) -> Mock:
    """The fields the drain verdict, the frontier and the filter read."""
    state = Mock()
    state.project_tasks = tasks
    state.tasks_being_assigned = set(being_assigned or set())
    state.assignment_persistence = Mock()
    state.assignment_persistence.get_all_assigned_task_ids = AsyncMock(
        return_value=set(persisted or set())
    )
    state.lease_manager = Mock()
    state.lease_manager.active_leases = {}
    for task_id, agent_id in (leases or {}).items():
        lease = Mock()
        lease.agent_id = agent_id
        lease.task_id = task_id
        state.lease_manager.active_leases[task_id] = lease
    state.subtask_manager = None
    workers = workers or [_worker("agent-1")]
    state.agent_status = {w.worker_id: w for w in workers}
    state.agent_project_map = {w.worker_id: PROJECT for w in workers}
    state.agent_tasks = {}
    state.project_state = Mock()
    state.ai_engine = None
    state.assignment_lock = asyncio.Lock()
    state.eligibility_refusals = []
    return state


# ---------------------------------------------------------------------------
# The drain verdict
# ---------------------------------------------------------------------------


class TestVerdictCountsStrandedCards:
    """A stranded card is residue, counted as such, never as live work."""

    @pytest.mark.asyncio
    async def test_stranded_card_is_claimable_and_counted(self) -> None:
        """TODO + assignee + no hold, dependencies met: claimable, stranded=1."""
        state = _state([_task("a", assigned_to="agent-gone")])

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.tasks_stranded == 1
        assert verdict.stranded_task_ids == ["a"]
        assert verdict.tasks_in_flight == 0
        assert verdict.tasks_claimable == 1
        assert verdict.reason == "claimable"

    @pytest.mark.asyncio
    async def test_a_live_lease_means_held_not_stranded(self) -> None:
        """A TODO card under a live lease is held: in flight, exactly as before."""
        state = _state([_task("a", assigned_to="agent-1")], leases={"a": "agent-1"})

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.tasks_stranded == 0
        assert verdict.tasks_in_flight == 1

    @pytest.mark.asyncio
    async def test_a_persisted_assignment_means_held_not_stranded(self) -> None:
        """A persisted row survives a restart; it is a hold."""
        state = _state([_task("a", assigned_to="agent-1")], persisted={"a"})

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.tasks_stranded == 0
        assert verdict.tasks_in_flight == 1

    @pytest.mark.asyncio
    async def test_a_reservation_means_held_not_stranded(self) -> None:
        """A card mid-assignment is held by the reservation."""
        state = _state([_task("a", assigned_to="agent-1")], being_assigned={"a"})

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.tasks_stranded == 0
        assert verdict.tasks_in_flight == 1

    @pytest.mark.asyncio
    async def test_an_in_progress_card_without_a_lease_is_still_in_flight(self) -> None:
        """A card being worked is IN_PROGRESS; the stranded rule never touches it."""
        state = _state([_task("a", TaskStatus.IN_PROGRESS, assigned_to="agent-1")])

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.tasks_stranded == 0
        assert verdict.tasks_in_flight == 1

    @pytest.mark.asyncio
    async def test_stranded_card_with_unmet_dependencies_is_unclaimable(self) -> None:
        """Stranded is a diagnostic overlay; the dependency gate still decides claimability."""
        state = _state(
            [
                _task("up", TaskStatus.BLOCKED),
                _task("a", deps=["up"], assigned_to="agent-gone"),
            ]
        )

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.tasks_stranded == 1
        assert verdict.tasks_claimable == 0
        assert verdict.tasks_unclaimable == 1
        assert verdict.tasks_in_flight == 0

    @pytest.mark.asyncio
    async def test_coordinator_owned_card_is_counted_but_never_claimable(self) -> None:
        """A TODO design ghost (assigned to Marcus) is stranded and unclaimable, not in flight."""
        state = _state(
            [
                _task("done", TaskStatus.DONE),
                _task("ghost", assigned_to=COORDINATOR_ASSIGNEE),
            ]
        )

        verdict = await compute_board_drain(state, PROJECT)

        assert verdict.tasks_stranded == 1
        assert verdict.tasks_claimable == 0
        assert verdict.tasks_in_flight == 0
        assert verdict.tasks_unclaimable == 1
        # Nothing claimable and nothing in flight: the board drains instead
        # of hanging forever behind a card nobody will ever be offered.
        assert verdict.drained is True
        assert verdict.reason == "blocked"

    def test_verdict_serializes_the_new_fields(self) -> None:
        """The counts ride inside the MCP response as plain data."""
        verdict = DrainVerdict(
            drained=False,
            reason="claimable",
            tasks_total=2,
            tasks_done=0,
            tasks_blocked=0,
            tasks_claimable=2,
            tasks_in_flight=0,
            tasks_unclaimable=0,
            tasks_stranded=1,
            stranded_task_ids=["a"],
        )

        data = verdict.to_dict()

        assert data["tasks_stranded"] == 1
        assert data["stranded_task_ids"] == ["a"]

    def test_verdict_defaults_keep_positional_construction_working(self) -> None:
        """Callers that build the eight original fields positionally still work."""
        verdict = DrainVerdict(False, "empty", 0, 0, 0, 0, 0, 0)

        assert verdict.tasks_stranded == 0
        assert verdict.stranded_task_ids == []


class TestFindStrandedAssignments:
    """The shared lookup the status tool uses."""

    @pytest.mark.asyncio
    async def test_returns_the_stranded_cards_of_the_project(self) -> None:
        """Scoped like the verdict: this project's cards and legacy cards without one."""
        state = _state(
            [
                _task("a", assigned_to="agent-gone"),
                _task("b", assigned_to="agent-1"),
                _task("other", assigned_to="agent-x", project_id="proj-2"),
                _task("legacy", assigned_to="agent-y", project_id=None),
            ],
            persisted={"b"},
        )

        stranded = await find_stranded_assignments(state, PROJECT)

        assert [t.id for t in stranded] == ["a", "legacy"]

    @pytest.mark.asyncio
    async def test_no_project_means_the_whole_board(self) -> None:
        """The status tool has an active board, not always a project id."""
        state = _state(
            [
                _task("a", assigned_to="agent-gone"),
                _task("other", assigned_to="agent-x", project_id="proj-2"),
            ]
        )

        stranded = await find_stranded_assignments(state, None)

        assert [t.id for t in stranded] == ["a", "other"]


# ---------------------------------------------------------------------------
# The frontier reply (#814) carries the count on every assignment
# ---------------------------------------------------------------------------


class TestFrontierCarriesTheStrandedCount:
    """Measurement belongs to the kernel (ADR-0013 K9): the count rides on every reply."""

    @pytest.mark.asyncio
    async def test_stranded_count_is_reported_beside_the_frontier(self) -> None:
        """One stranded, one plain claimable: claimable=2 (both offerable), stranded=1."""
        state = _state([_task("a", assigned_to="agent-gone"), _task("b")])

        frontier = await _frontier_for_agent("agent-1", state)

        assert frontier == {
            "tasks_claimable": 2,
            "tasks_in_flight": 0,
            "tasks_stranded": 1,
        }


# ---------------------------------------------------------------------------
# The eligibility filter reclaims what nothing holds
# ---------------------------------------------------------------------------


class TestFilterReclaimsStrandedCards:
    """``_find_optimal_task_original_logic`` offers a stranded card; it skips held ones."""

    @pytest.mark.asyncio
    async def test_a_stranded_card_is_offered(self) -> None:
        """The only card on the board names a vanished agent; the caller gets it."""
        stranded = _task("a", assigned_to="agent-gone")
        state = _state([stranded])

        result = await _find_optimal_task_original_logic("agent-1", state)

        assert result is stranded

    @pytest.mark.asyncio
    async def test_a_persisted_assignment_is_still_skipped(self) -> None:
        """The persisted row is a hold the filter honours, exactly as before."""
        state = _state([_task("a", assigned_to="agent-2")], persisted={"a"})

        result = await _find_optimal_task_original_logic("agent-1", state)

        assert result is None

    @pytest.mark.asyncio
    async def test_a_reserved_card_is_still_skipped(self) -> None:
        """A card mid-assignment to someone else is not offered twice."""
        state = _state([_task("a", assigned_to="agent-2")], being_assigned={"a"})

        result = await _find_optimal_task_original_logic("agent-1", state)

        assert result is None

    @pytest.mark.asyncio
    async def test_a_coordinator_owned_card_is_never_offered(self) -> None:
        """A TODO design ghost stays Marcus's; agents are not handed design work (#619 unchanged)."""
        state = _state([_task("ghost", assigned_to=COORDINATOR_ASSIGNEE)])

        result = await _find_optimal_task_original_logic("agent-1", state)

        assert result is None

    @pytest.mark.asyncio
    async def test_reclaiming_is_warned_once_per_card_not_once_per_poll(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Agents poll every 30 seconds; the warning names the card and its stale assignee once."""
        caplog.set_level(logging.WARNING, logger="src.marcus_mcp.tools.task")
        state = _state([_task("a", assigned_to="agent-gone")])

        await _find_optimal_task_original_logic("agent-1", state)
        await _find_optimal_task_original_logic("agent-1", state)

        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "stranded" in r.getMessage().lower()
        ]
        assert len(warnings) == 1
        assert "a" in warnings[0] and "agent-gone" in warnings[0]


# ---------------------------------------------------------------------------
# The assignment write keeps who was holding the card
# ---------------------------------------------------------------------------


class TestReclaimStamp:
    """The record carries the whole transition, not just the new name."""

    def test_a_stranded_card_gets_a_stamp_merged_into_its_record(self) -> None:
        """previous assignee, who reclaimed it, when; existing record keys survive."""
        task = _task(
            "a", assigned_to="agent-gone", source_context={"contract_file": "x.md"}
        )

        stamped = reclaim_stamp(task, "agent-1", NOW)

        assert stamped == {
            "contract_file": "x.md",
            "stranded_assignment": {
                "previous_assignee": "agent-gone",
                "reclaimed_by": "agent-1",
                "reclaimed_at": NOW.isoformat(),
            },
        }

    def test_an_ordinary_card_gets_no_stamp(self) -> None:
        """No assignee means nothing was reclaimed."""
        assert reclaim_stamp(_task("a"), "agent-1", NOW) is None

    def test_a_card_already_named_to_the_claimant_gets_no_stamp(self) -> None:
        """Re-offering a card to its own recorded assignee is not a reclaim."""
        assert reclaim_stamp(_task("a", assigned_to="agent-1"), "agent-1", NOW) is None

    def test_a_card_that_is_not_todo_gets_no_stamp(self) -> None:
        """Only a TODO card can be stranded; anything else keeps its record untouched."""
        task = _task("a", TaskStatus.IN_PROGRESS, assigned_to="agent-gone")
        assert reclaim_stamp(task, "agent-1", NOW) is None


class TestAssignmentWriteCarriesTheStamp:
    """The stamp rides in the same update that replaces the assignee, never a second write."""

    def test_a_stranded_card_is_assigned_and_stamped_in_one_payload(self) -> None:
        """New status, new assignee and the reclaim record are one dict for one write."""
        task = _task("a", assigned_to="agent-gone", source_context={"k": "v"})

        payload = assignment_write_payload(task, "agent-1", NOW)

        assert payload["status"] == TaskStatus.IN_PROGRESS
        assert payload["assigned_to"] == "agent-1"
        assert payload["source_context"]["k"] == "v"
        assert payload["source_context"]["stranded_assignment"] == {
            "previous_assignee": "agent-gone",
            "reclaimed_by": "agent-1",
            "reclaimed_at": NOW.isoformat(),
        }

    def test_an_ordinary_assignment_writes_exactly_what_it_always_did(self) -> None:
        """No stranded card, no record write: the payload is the historical two keys."""
        payload = assignment_write_payload(_task("a"), "agent-1", NOW)

        assert payload == {"status": TaskStatus.IN_PROGRESS, "assigned_to": "agent-1"}


# ---------------------------------------------------------------------------
# The drained message names the stranded cards and the remedy
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
    state.gridlock_detector = None
    state.kanban_client = Mock()
    state.project_state = None
    state.provider = "sqlite"
    state.code_analyzer = None
    state.context = None
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


class TestDrainedMessageNamesStrandedCards:
    """An operator reading the go-home message learns what is stuck and what to do."""

    @pytest.mark.asyncio
    async def test_drained_behind_a_coordinator_owned_card_names_it(self) -> None:
        """Everything else done, one TODO ghost: drained, and the message says why."""
        state = _request_state(
            [
                _task("done", TaskStatus.DONE),
                _task("ghost", assigned_to=COORDINATOR_ASSIGNEE),
            ]
        )
        with _no_task_patches():
            result = await request_next_task(agent_id="agent-1", state=state)

        assert result["status"] == "BOARD_DRAINED"
        assert result["drain"]["tasks_stranded"] == 1
        assert result["drain"]["stranded_task_ids"] == ["ghost"]
        message = result["message"]
        assert "ghost" in message
        assert COORDINATOR_ASSIGNEE in message
        assert "unassign_task" in message
