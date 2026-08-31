"""
End-to-end tests for D11 fencing through the real ``report_task_progress``.

Why this module exists
----------------------
The first cut of gate 2 was verified only against the fencing *helpers*
(``should_fence_stale_epoch``, ``open_reconciliation_card``) called
directly. Those tests would have passed with the fence never wired into
the completion path at all, and they hid two real defects:

* a lone agent with no rival claimant was fenced, because "no live lease"
  was treated the same as "superseded epoch"; and
* an agent whose lapsed lease was silently re-granted a new epoch was
  fenced on its own completion.

The fence has since been removed entirely — it contradicted Marcus's
documented false-positive recovery compensation. What remains is the
ownership hardening plus record-only observation of the collision. These
tests cover what actually ships.

Every test here drives the actual ``report_task_progress`` entry point
with a real ``AssignmentLeaseManager``, and asserts on what the agent
receives and what reaches the board.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.assignment_lease import AssignmentLeaseManager
from src.core.assignment_persistence import AssignmentPersistence
from src.core.models import Priority, Task, TaskAssignment, TaskStatus
from src.marcus_mcp.tools.task import report_task_progress

pytestmark = pytest.mark.unit


def _make_task(task_id: str = "task-1") -> Task:
    """
    Build a minimal TODO task.

    Parameters
    ----------
    task_id : str
        Identifier for the task.

    Returns
    -------
    Task
        A task with no dependencies or labels.
    """
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name="Build the API",
        description="Fixture task",
        status=TaskStatus.IN_PROGRESS,
        priority=Priority.MEDIUM,
        estimated_hours=4.0,
        dependencies=[],
        labels=[],
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
    )


def _make_assignment(task_id: str, agent_id: str) -> TaskAssignment:
    """
    Build a minimal TaskAssignment for ``state.agent_tasks``.

    Returns
    -------
    TaskAssignment
        Assignment linking the agent to the task.
    """
    return TaskAssignment(
        task_id=task_id,
        task_name="Build the API",
        description="Fixture task",
        instructions="do the thing",
        estimated_hours=4.0,
        priority=Priority.MEDIUM,
        dependencies=[],
        assigned_to=agent_id,
        assigned_at=datetime.now(timezone.utc),
        due_date=None,
    )


def _make_state(
    lease_manager: AssignmentLeaseManager,
    task: Task,
    agent_id: Optional[str] = None,
) -> Mock:
    """
    Build a Mock server state wired to a real lease manager.

    Parameters
    ----------
    lease_manager : AssignmentLeaseManager
        The real manager under test.
    task : Task
        The task being reported on; placed in ``project_tasks``.
    agent_id : Optional[str]
        When given, the agent is recorded as holding the task in
        ``agent_tasks``. Leave None to model recovery having cleared it.

    Returns
    -------
    Mock
        State object suitable for ``report_task_progress``.
    """
    state = Mock()
    state.initialize_kanban = AsyncMock()
    state.kanban_client = Mock()
    state.kanban_client.get_all_tasks = AsyncMock(return_value=[task])
    state.kanban_client.update_task = AsyncMock()
    state.kanban_client.update_task_progress = AsyncMock()
    state.kanban_client._load_workspace_state = Mock(return_value=None)

    created: List[Dict[str, Any]] = []

    async def _create_task(task_data: Dict[str, Any]):
        created.append(task_data)
        made = Mock()
        made.id = f"recon-{len(created)}"
        return made

    state.kanban_client.create_task = AsyncMock(side_effect=_create_task)
    state.created_cards = created

    state.agent_tasks = {}
    if agent_id is not None:
        state.agent_tasks[agent_id] = _make_assignment(task.id, agent_id)
    state.project_tasks = [task]
    state.lease_manager = lease_manager
    state.agent_status = {}
    state.tasks_being_assigned = set()
    state.assignment_persistence = Mock()
    state.assignment_persistence.remove_assignment = AsyncMock()
    state.memory = None
    state.provider = "sqlite"
    state.code_analyzer = None
    state.subtask_manager = None
    return state


@pytest.fixture
def manager(tmp_path) -> AssignmentLeaseManager:
    """
    Create a real lease manager over isolated persistence.

    Returns
    -------
    AssignmentLeaseManager
        Manager backed by a temp directory.
    """
    return AssignmentLeaseManager(
        Mock(), AssignmentPersistence(tmp_path / "assignments")
    )


class TestLoneAgentIsNotFenced:
    """No live lease means nobody else holds it — that is not a collision."""

    @pytest.mark.asyncio
    async def test_no_rival_claimant_is_not_fenced(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """A lone late completion after recovery is not fenced.

        Recovery releases the lease, so "no live lease" is the *usual*
        state at fence time. Treating it as a superseded epoch fenced
        agents that nobody was competing with, and silently reverted the
        #667 Fix 2 uncontested-accept.
        """
        task = _make_task()
        lease = await manager.create_lease(task.id, "agent-a")
        await manager.release_lease(task.id, reason="lease_expired")
        # Nobody re-claims. Agent A, alive all along, finishes.

        state = _make_state(manager, task, agent_id=None)

        result = await report_task_progress(
            agent_id="agent-a",
            task_id=task.id,
            status="completed",
            progress=100,
            message="finished the endpoint",
            state=state,
            lease_epoch=lease.lease_epoch,
        )

        assert result.get("status") != "stale_epoch"
        # No collision, so no card claiming there was one.
        assert len(state.created_cards) == 0

    @pytest.mark.asyncio
    async def test_no_false_reconciliation_card(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """A card must never assert two agents when there was one."""
        task = _make_task()
        lease = await manager.create_lease(task.id, "agent-a")
        await manager.release_lease(task.id, reason="lease_expired")

        state = _make_state(manager, task, agent_id=None)

        await report_task_progress(
            agent_id="agent-a",
            task_id=task.id,
            status="completed",
            progress=100,
            message="done",
            state=state,
            lease_epoch=lease.lease_epoch,
        )

        for card in state.created_cards:
            assert "Two agents" not in card["description"]


class TestHolderKeepsItsEpochAcrossALapse:
    """A lapsed-but-not-recovered lease is still the holder's."""

    @pytest.mark.asyncio
    async def test_progress_report_does_not_rotate_the_epoch(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """Reporting on a lapsed lease must not change the agent's epoch.

        The agent is only ever told its epoch once, by
        ``request_next_task``. If reporting progress silently mints a new
        one, the agent keeps sending the old value and is fenced on its
        own completion — punishing precisely the agents that follow the
        protocol.
        """
        task = _make_task()
        lease = await manager.create_lease(task.id, "agent-a")
        original_epoch = lease.lease_epoch

        # The lease lapses, but recovery has not run: still in active_leases.
        lease.lease_expires = datetime.now(timezone.utc) - timedelta(seconds=1)

        state = _make_state(manager, task, agent_id="agent-a")

        await report_task_progress(
            agent_id="agent-a",
            task_id=task.id,
            status="in_progress",
            progress=50,
            message="halfway",
            state=state,
            lease_epoch=original_epoch,
        )

        live = manager.active_leases.get(task.id)
        assert live is not None
        assert live.lease_epoch == original_epoch
        assert live.agent_id == "agent-a"

    @pytest.mark.asyncio
    async def test_own_completion_is_not_fenced_after_a_lapse(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """The full sequence that used to fence a compliant agent."""
        task = _make_task()
        lease = await manager.create_lease(task.id, "agent-a")
        original_epoch = lease.lease_epoch
        lease.lease_expires = datetime.now(timezone.utc) - timedelta(seconds=1)

        state = _make_state(manager, task, agent_id="agent-a")

        await report_task_progress(
            agent_id="agent-a",
            task_id=task.id,
            status="in_progress",
            progress=50,
            message="halfway",
            state=state,
            lease_epoch=original_epoch,
        )

        result = await report_task_progress(
            agent_id="agent-a",
            task_id=task.id,
            status="completed",
            progress=100,
            message="done",
            state=state,
            lease_epoch=original_epoch,
        )

        assert result.get("status") != "stale_epoch"
        assert len(state.created_cards) == 0


class TestLenientRollout:
    """An epoch-less agent behaves exactly as it did before D11."""

    @pytest.mark.asyncio
    async def test_epochless_completion_is_not_fenced(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """An un-migrated agent is never fenced on the epoch path."""
        task = _make_task()
        await manager.create_lease(task.id, "agent-a")
        await manager.release_lease(task.id, reason="lease_expired")
        await manager.create_lease(task.id, "agent-b")

        state = _make_state(manager, task, agent_id=None)

        result = await report_task_progress(
            agent_id="agent-a",
            task_id=task.id,
            status="completed",
            progress=100,
            message="done",
            state=state,
        )

        assert result.get("status") != "stale_epoch"


class TestReGrantOwnership:
    """The re-grant must not hand a live holder's card to anyone else."""

    @pytest.mark.asyncio
    async def test_epochless_non_holder_cannot_steal_the_lease(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """An un-migrated displaced agent must not take the card.

        Adding the holder check to ``renew_lease`` made the re-grant
        branch reachable for non-holders, and the only guard on it was
        the epoch fence — which deliberately passes epoch-less agents.
        So a displaced agent on an old prompt could overwrite the live
        holder's lease entirely. Leniency means "behaves as before", and
        before this change a non-holder could not take ownership.
        """
        task = _make_task()
        await manager.create_lease(task.id, "agent-a")
        await manager.release_lease(task.id, reason="lease_expired")
        lease_b = await manager.create_lease(task.id, "agent-b")

        state = _make_state(manager, task, agent_id="agent-b")

        await report_task_progress(
            agent_id="agent-a",
            task_id=task.id,
            status="in_progress",
            progress=50,
            message="still going",
            state=state,
        )

        live = manager.active_leases.get(task.id)
        assert live is not None
        assert live.agent_id == "agent-b"
        assert live.lease_epoch == lease_b.lease_epoch

    @pytest.mark.asyncio
    async def test_done_task_does_not_get_a_zombie_lease(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """A report on an already-DONE task must not recreate a lease.

        The guard compared a ``TaskStatus`` enum member against string
        literals. ``TaskStatus`` is a plain Enum, not a str-mixin, so the
        comparison was always False and the zombie-prevention branch had
        never executed.
        """
        task = _make_task()
        task.status = TaskStatus.DONE
        state = _make_state(manager, task, agent_id="agent-a")

        await report_task_progress(
            agent_id="agent-a",
            task_id=task.id,
            status="in_progress",
            progress=50,
            message="late report on finished work",
            state=state,
        )

        assert task.id not in manager.active_leases


class TestBoardOwnershipIsProtected:
    """A displaced agent must not take the card's name on the board."""

    @pytest.mark.asyncio
    async def test_displaced_agent_does_not_rewrite_assigned_to(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """The epoch fence guards completions; the board write is earlier.

        A session carrying a superseded token reported in_progress and
        set ``assigned_to`` to itself, visually taking the card from the
        live holder even though it would be refused at completion.
        """
        task = _make_task()
        await manager.create_lease(task.id, "agent-a")
        await manager.release_lease(task.id, reason="lease_expired")
        await manager.create_lease(task.id, "agent-b")

        state = _make_state(manager, task, agent_id="agent-b")

        await report_task_progress(
            agent_id="agent-a",
            task_id=task.id,
            status="in_progress",
            progress=50,
            message="still going",
            state=state,
        )

        for call in state.kanban_client.update_task.call_args_list:
            payload = call.args[1] if len(call.args) > 1 else {}
            assert payload.get("assigned_to") != "agent-a"

    @pytest.mark.asyncio
    async def test_the_live_holder_still_sets_assigned_to(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """The guard must not block the legitimate holder."""
        task = _make_task()
        await manager.create_lease(task.id, "agent-b")
        state = _make_state(manager, task, agent_id="agent-b")

        await report_task_progress(
            agent_id="agent-b",
            task_id=task.id,
            status="in_progress",
            progress=50,
            message="working",
            state=state,
        )

        assigned = [
            (call.args[1] if len(call.args) > 1 else {}).get("assigned_to")
            for call in state.kanban_client.update_task.call_args_list
        ]
        assert "agent-b" in assigned


class TestEpochIsRecordedNotEnforced:
    """The epoch is observed. No completion is refused for a stale one.

    Fencing was removed because Marcus already ships a documented
    compensation for false-positive recovery — the agent's next
    ``report_task_progress`` recreates the lease. That says "recovery may
    have been wrong"; a fence says "recovery was right". Both cannot be
    authoritative. Resolving that belongs with D3's liveness retune.
    """

    @pytest.mark.asyncio
    async def test_a_superseded_completion_is_still_accepted(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """A displaced agent's completion is NOT refused today."""
        task = _make_task()
        lease_a = await manager.create_lease(task.id, "agent-a")
        await manager.release_lease(task.id, reason="lease_expired")
        await manager.create_lease(task.id, "agent-b")

        state = _make_state(manager, task, agent_id=None)

        result = await report_task_progress(
            agent_id="agent-a",
            task_id=task.id,
            status="completed",
            progress=100,
            message="done",
            state=state,
            lease_epoch=lease_a.lease_epoch,
        )

        assert result.get("status") != "stale_epoch"
        # And no reconciliation card: that mechanism is gone.
        assert len(state.created_cards) == 0

    @pytest.mark.asyncio
    async def test_the_collision_is_observed(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """The superseded claim is detected so the rate is measurable.

        Nobody currently knows how often this fires. Under spawn-per-task
        it should be impossible, because recovery kills the agent — so a
        nonzero count is itself a finding.
        """
        from src.marcus_mcp.tools.task import _observe_epoch_collision

        task = _make_task()
        lease_a = await manager.create_lease(task.id, "agent-a")
        await manager.release_lease(task.id, reason="lease_expired")
        await manager.create_lease(task.id, "agent-b")

        state = _make_state(manager, task, agent_id=None)

        assert await _observe_epoch_collision(
            state, task.id, "agent-a", lease_a.lease_epoch
        )

    @pytest.mark.asyncio
    async def test_the_current_holder_is_not_a_collision(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """The newest claim must not be logged as a collision."""
        from src.marcus_mcp.tools.task import _observe_epoch_collision

        task = _make_task()
        await manager.create_lease(task.id, "agent-a")
        await manager.release_lease(task.id, reason="lease_expired")
        lease_b = await manager.create_lease(task.id, "agent-b")

        state = _make_state(manager, task, agent_id="agent-b")

        assert not await _observe_epoch_collision(
            state, task.id, "agent-b", lease_b.lease_epoch
        )

    @pytest.mark.asyncio
    async def test_an_epochless_report_is_never_a_collision(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """Un-migrated agents produce no noise."""
        from src.marcus_mcp.tools.task import _observe_epoch_collision

        task = _make_task()
        await manager.create_lease(task.id, "agent-a")
        state = _make_state(manager, task, agent_id="agent-a")

        assert not await _observe_epoch_collision(state, task.id, "agent-a", None)


class TestTokenIsNeverReissued:
    """Monotonicity is the property that makes an epoch a fencing token.

    An earlier attempt added ``preserve_epoch`` so a re-granted agent kept
    its token. That reissued a number already held, which let an agent
    that had never been issued an epoch present one and take a card. Every
    grant except a same-agent continuation advances the sequence.
    """

    @pytest.mark.asyncio
    async def test_a_new_claimant_always_advances_the_sequence(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """No caller can ask for an epoch that is already held."""
        task = _make_task()
        first = await manager.create_lease(task.id, "agent-a")
        await manager.release_lease(task.id, reason="lease_expired")

        second = await manager.create_lease(task.id, "agent-c")

        assert second.lease_epoch > first.lease_epoch

    @pytest.mark.asyncio
    async def test_same_agent_continuation_keeps_its_token(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """A continuation is not a new claim."""
        task = _make_task()
        first = await manager.create_lease(task.id, "agent-a")

        second = await manager.create_lease(task.id, "agent-a")

        assert second.lease_epoch == first.lease_epoch
