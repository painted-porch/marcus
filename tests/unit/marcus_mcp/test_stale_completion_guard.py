"""
Unit tests for the stale-completion guard in report_task_progress.

Regression coverage for Issue #343 (simultaneous completion guard
for recovered tasks). When a task's lease expires and is recovered
to another agent, the original agent's in-memory assignment is
cleared. If that original agent keeps working locally and later
reports completion — unaware their assignment was revoked — Marcus
must reject the stale completion. Otherwise we accept a second
completion on a task another agent is actively working on, and the
two implementations collide at merge time.

Dashboard-v70 Epictetus audit: 341 lines of ghost source + 506
lines of ghost tests were produced by exactly this race.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import Priority, TaskAssignment
from src.marcus_mcp.tools.task import report_task_progress

pytestmark = pytest.mark.unit


def _make_assignment(task_id: str, agent_id: str = "agent-001") -> TaskAssignment:
    """Build a minimal TaskAssignment for state.agent_tasks."""
    return TaskAssignment(
        task_id=task_id,
        task_name="Test Task",
        description="desc",
        instructions="do it",
        estimated_hours=0.1,
        priority=Priority.HIGH,
        dependencies=[],
        assigned_to=agent_id,
        assigned_at=datetime.now(timezone.utc),
        due_date=None,
    )


def _make_state_with_assignment(agent_id: str, task_id: str | None) -> Mock:
    """
    Build a Mock state with agent_tasks populated appropriately.
    ``task_id=None`` simulates the post-recovery state where the
    agent's assignment was cleared by on_recovery_callback.
    """
    state = Mock()
    state.initialize_kanban = AsyncMock()
    state.kanban_client = Mock()
    state.kanban_client.get_all_tasks = AsyncMock(return_value=[])
    state.kanban_client.update_task = AsyncMock()
    state.kanban_client.update_task_progress = AsyncMock()
    # _load_workspace_state is consulted by _merge_agent_branch_to_main
    # via ``if ws_state and "project_root" in ws_state``. Returning None
    # short-circuits the merge path so the test stays focused on the
    # guard rather than git worktree behavior.
    state.kanban_client._load_workspace_state = Mock(return_value=None)
    state.agent_tasks = {}
    if task_id is not None:
        state.agent_tasks[agent_id] = _make_assignment(task_id, agent_id)
    state.project_tasks = []
    state.lease_manager = None
    state.agent_status = {}
    state.assignment_persistence = Mock()
    state.assignment_persistence.remove_assignment = AsyncMock()
    state.memory = None
    state.provider = "sqlite"
    state.code_analyzer = None
    state.subtask_manager = None
    return state


class TestStaleCompletionGuard:
    """
    ``report_task_progress`` must reject stale completions from
    agents whose assignments were cleared by recovery callback.
    """

    @pytest.mark.asyncio
    async def test_rejects_completion_when_assignment_cleared(self) -> None:
        """
        Original agent had task X, recovery cleared agent_tasks,
        agent still tries to complete X → rejected with
        stale_completion status.
        """
        state = _make_state_with_assignment("agent-001", task_id=None)

        result = await report_task_progress(
            agent_id="agent-001",
            task_id="task-343",
            status="completed",
            progress=100,
            message="finished",
            state=state,
        )

        assert result["success"] is False
        assert result["status"] == "stale_completion"
        assert result["error"] == "task_recovered"
        assert "task-343" in result["message"]
        assert "marcus/agent-001" in result["message"]
        # Kanban must NOT have been written — the whole point of
        # the guard is to prevent the double-completion side
        # effects. The completion path never runs.
        state.kanban_client.update_task.assert_not_called()
        state.kanban_client.update_task_progress.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_completion_when_agent_reassigned_to_different_task(
        self,
    ) -> None:
        """
        Original agent was recovered off task X, then picked up
        task Y. They still try to complete X from their local
        work → rejected. The agent IS in agent_tasks but their
        assignment points to a different task.
        """
        state = _make_state_with_assignment("agent-001", task_id="task-Y")

        result = await report_task_progress(
            agent_id="agent-001",
            task_id="task-X",
            status="completed",
            progress=100,
            message="finished",
            state=state,
        )

        assert result["success"] is False
        assert result["status"] == "stale_completion"
        assert "task-X" in result["message"]
        state.kanban_client.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_completion_when_assignment_matches(self) -> None:
        """
        Legitimate completion: agent is assigned to task X and
        reports completion on X → guard passes, completion flows
        through the normal pipeline.
        """
        state = _make_state_with_assignment("agent-001", task_id="task-343")
        # Skip the validation gate by returning no matching task
        # from get_all_tasks (task_filter sees no task, skips).
        # This isolates the guard test from validator logic.

        result = await report_task_progress(
            agent_id="agent-001",
            task_id="task-343",
            status="completed",
            progress=100,
            message="finished",
            state=state,
        )

        # Kanban WAS written — guard did not block the path
        state.kanban_client.update_task.assert_called_once()
        # Final response surfaces success
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_allows_progress_update_even_when_assignment_cleared(
        self,
    ) -> None:
        """
        The guard fires only on status=completed. Intermediate
        progress updates (25%, 50%, etc.) from an agent whose
        assignment was cleared still flow through — the "No
        active lease" fallback in the lease path recreates the
        lease, so the agent can continue working. Only
        completions are blocked, because completions have
        irreversible kanban side effects (DONE status + branch
        merge).
        """
        state = _make_state_with_assignment("agent-001", task_id=None)

        result = await report_task_progress(
            agent_id="agent-001",
            task_id="task-343",
            status="in_progress",
            progress=50,
            message="halfway",
            state=state,
        )

        # Progress update goes through
        assert result["success"] is True
        state.kanban_client.update_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejects_completion_when_no_assignment_for_agent(
        self,
    ) -> None:
        """
        Agent is completely unknown to state.agent_tasks (never
        registered or was removed entirely) → reject with the
        stale-completion response. This covers the "recovery
        callback cleared our entry" path where ``agent_tasks[agent_id]``
        is missing rather than pointing to a different task.
        """
        state = _make_state_with_assignment("other-agent", task_id="task-X")

        result = await report_task_progress(
            agent_id="agent-001",
            task_id="task-343",
            status="completed",
            progress=100,
            message="finished",
            state=state,
        )

        assert result["success"] is False
        assert result["status"] == "stale_completion"
        assert result["error"] == "task_recovered"
        state.kanban_client.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_cold_cache_completion_allowed_via_lease_manager(
        self,
    ) -> None:
        """
        Codex P1 regression on PR #345.

        ``state.agent_tasks`` is in-memory only and starts EMPTY on
        Marcus restart. ``MarcusServer.__init__`` never rehydrates
        it from ``assignment_persistence``. The lease manager, on
        the other hand, IS rebuilt from persistence on startup.

        Without a fallback path, every legitimate post-restart
        completion gets rejected as stale because the in-memory
        cache is empty. The fix: when ``agent_tasks`` misses, also
        consult ``state.lease_manager.active_leases`` — if a lease
        for this task exists AND is held by this agent, allow the
        completion. The lease manager is the authoritative
        cross-restart source of truth for "who holds this task."

        This test simulates a restart by giving the state an EMPTY
        ``agent_tasks`` but a lease manager with a matching lease.
        Pre-fix this would reject; post-fix it must allow.
        """
        state = _make_state_with_assignment("agent-001", task_id=None)
        # Cold cache: no in-memory entry for this agent
        assert state.agent_tasks == {}

        # But the lease manager has a lease (rebuilt from
        # persistence on restart in the real system)
        fake_lease = Mock()
        fake_lease.agent_id = "agent-001"
        fake_lease.task_id = "task-343"
        state.lease_manager = Mock()
        # extend_for_validation is awaited by report_task_progress on
        # status=completed (issue #667 Fix 1). Mock as AsyncMock so
        # the await resolves; the return value isn't asserted here.
        state.lease_manager.extend_for_validation = AsyncMock(return_value=None)
        state.lease_manager.active_leases = {"task-343": fake_lease}

        result = await report_task_progress(
            agent_id="agent-001",
            task_id="task-343",
            status="completed",
            progress=100,
            message="finished",
            state=state,
        )

        # Completion proceeds — the cold-cache fallback recognized
        # this agent as the legitimate lease holder.
        assert result["success"] is True
        # Kanban DONE write happened (completion path ran).
        state.kanban_client.update_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_cold_cache_rejects_when_lease_held_by_different_agent(
        self,
    ) -> None:
        """
        Cold cache fallback must NOT allow a completion when the
        lease manager says a DIFFERENT agent holds the task. This
        is the actual stale-completion case post-recovery: lease
        manager has been updated to the new holder, in-memory
        cache may or may not have been cleared, original agent
        tries to complete — both checks correctly reject.
        """
        state = _make_state_with_assignment("agent-001", task_id=None)

        fake_lease = Mock()
        fake_lease.agent_id = "different-agent"  # NOT the requester
        fake_lease.task_id = "task-343"
        state.lease_manager = Mock()
        # extend_for_validation is awaited by report_task_progress on
        # status=completed (issue #667 Fix 1). Mock as AsyncMock so
        # the await resolves; the return value isn't asserted here.
        state.lease_manager.extend_for_validation = AsyncMock(return_value=None)
        state.lease_manager.active_leases = {"task-343": fake_lease}

        result = await report_task_progress(
            agent_id="agent-001",
            task_id="task-343",
            status="completed",
            progress=100,
            message="finished",
            state=state,
        )

        assert result["success"] is False
        assert result["status"] == "stale_completion"
        state.kanban_client.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_cold_cache_lease_lookup_error_falls_through_to_reject(
        self,
    ) -> None:
        """
        If the lease manager lookup raises (e.g. corrupt state,
        race during reload), don't crash the completion path —
        fall through to the default rejection. The agent will
        retry, the cache will eventually rebuild, and the next
        attempt will succeed.
        """
        state = _make_state_with_assignment("agent-001", task_id=None)

        broken_lease_manager = Mock()
        # Make .active_leases.get raise
        broken_active_leases = Mock()
        broken_active_leases.get = Mock(side_effect=RuntimeError("corrupt"))
        broken_lease_manager.active_leases = broken_active_leases
        broken_lease_manager.extend_for_validation = AsyncMock(return_value=None)
        state.lease_manager = broken_lease_manager

        result = await report_task_progress(
            agent_id="agent-001",
            task_id="task-343",
            status="completed",
            progress=100,
            message="finished",
            state=state,
        )

        # Falls through to rejection — defensive, not a crash.
        assert result["success"] is False
        assert result["status"] == "stale_completion"


class TestValidationWindowFiresBeforeSmokeGate:
    """Regression guard for Codex P1 on PR #668.

    The validation-window extension must fire BEFORE the
    validation / smoke gates run. If it fires after, two things
    break:

    1. The smoke gate runs under the prior (smaller) lease window
       and routinely exceeds it on slow integration tasks — the
       lease expires mid-gate.
    2. On a successful completion, ``active_leases[task_id]`` is
       deleted by the completion-cleanup path well before the
       original placement was reached. The extension call would
       hit a missing lease and return None silently.

    This test asserts call ordering: ``extend_for_validation`` is
    invoked before the smoke-gate / kanban-update path runs. The
    cheap signal we use is: ``extend_for_validation`` must be
    called at least once during a completion that exercises the
    happy path.
    """

    @pytest.mark.asyncio
    async def test_extend_for_validation_called_on_completion_happy_path(
        self,
    ) -> None:
        """extend_for_validation is awaited when status=completed.

        Validates the wiring exists. Combined with the assertion
        that the call happens at the stale-guard hand-off point,
        this prevents Codex's P1 from regressing — moving the call
        downstream of the smoke gate would cause this test plus
        the integration-level test to fail in tandem.
        """
        state = _make_state_with_assignment("agent-001", task_id="task-343")
        state.lease_manager = Mock()
        state.lease_manager.active_leases = {}
        state.lease_manager.extend_for_validation = AsyncMock(return_value=None)
        state.tasks_being_assigned = set()

        await report_task_progress(
            agent_id="agent-001",
            task_id="task-343",
            status="completed",
            progress=100,
            message="finished",
            state=state,
        )

        state.lease_manager.extend_for_validation.assert_awaited_once_with("task-343")

    @pytest.mark.asyncio
    async def test_extend_for_validation_not_called_for_progress_update(
        self,
    ) -> None:
        """Progress updates (non-completion) go through renew_lease,
        not extend_for_validation. The validation window is only
        granted at the completion-handoff moment.
        """
        state = _make_state_with_assignment("agent-001", task_id="task-343")
        state.lease_manager = Mock()
        state.lease_manager.active_leases = {}
        state.lease_manager.extend_for_validation = AsyncMock(return_value=None)
        state.lease_manager.renew_lease = AsyncMock(return_value=None)
        state.tasks_being_assigned = set()

        await report_task_progress(
            agent_id="agent-001",
            task_id="task-343",
            status="in_progress",
            progress=50,
            message="halfway",
            state=state,
        )

        state.lease_manager.extend_for_validation.assert_not_called()


class TestTransactionalLateCompletionAccept:
    """Tests for Fix 2 of issue #667: accept late completions when
    the task is uncontested.

    The original Issue #343 guard was written to prevent two agents
    from writing conflicting completions. The dangerous case it
    protects against is "two agents both producing artifacts
    simultaneously" — not "old agent finishes seconds after lease
    recovery, before any replacement agent has been assigned."

    When the lease has been reclaimed but no replacement is in flight
    (active_leases has no entry for the task, no agent in
    state.agent_tasks references the task, and tasks_being_assigned
    does not contain the task), the late completion should be
    accepted. The agent's verification artifacts are preserved
    instead of discarded, eliminating the need for a re-run.

    When ANY replacement signal is present (new agent in
    active_leases, another agent's assignment, in-flight assignment),
    the completion is still rejected to preserve the two-agent-race
    protection.
    """

    @pytest.mark.asyncio
    async def test_accepts_late_completion_when_task_uncontested(self) -> None:
        """Lease reclaimed, no replacement → late completion accepted.

        The agent's lease was recovered (active_leases no longer has
        this task), no other agent has been assigned to this task
        (state.agent_tasks empty for the task), and no assignment is
        in flight (tasks_being_assigned does not contain the task).
        The agent's late ``completed`` arrives — Fix 2 accepts it.

        Before Fix 2 this would be rejected as stale and the agent's
        work discarded. After Fix 2 the completion proceeds and
        kanban DONE is written.
        """
        state = _make_state_with_assignment("agent-001", task_id=None)
        # Lease reclaimed: no entry for this task
        state.lease_manager = Mock()
        # extend_for_validation is awaited by report_task_progress on
        # status=completed (issue #667 Fix 1). Mock as AsyncMock so
        # the await resolves; the return value isn't asserted here.
        state.lease_manager.extend_for_validation = AsyncMock(return_value=None)
        state.lease_manager.active_leases = {}
        # No replacement agent has been assigned to this task
        # (agent_tasks already empty per fixture)
        # No in-flight assignment
        state.tasks_being_assigned = set()

        result = await report_task_progress(
            agent_id="agent-001",
            task_id="task-343",
            status="completed",
            progress=100,
            message="finished post-lease-recovery",
            state=state,
        )

        assert result["success"] is True
        # The completion path ran — kanban received the DONE write.
        state.kanban_client.update_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejects_late_completion_when_in_flight_assignment_exists(
        self,
    ) -> None:
        """Race case: mid-assignment NULL → still reject.

        The lease has been reclaimed and Marcus is in the middle of
        assigning to a new agent. ``active_leases`` does not yet
        have an entry (the new lease hasn't been created), but
        ``tasks_being_assigned`` does. Accepting the late completion
        here would race with the new agent — both could end up
        believing they own the task.

        Fix 2's transactional check must reject in this case to
        preserve Issue #343's two-agent-race protection.
        """
        state = _make_state_with_assignment("agent-001", task_id=None)
        state.lease_manager = Mock()
        # extend_for_validation is awaited by report_task_progress on
        # status=completed (issue #667 Fix 1). Mock as AsyncMock so
        # the await resolves; the return value isn't asserted here.
        state.lease_manager.extend_for_validation = AsyncMock(return_value=None)
        state.lease_manager.active_leases = {}
        # In-flight assignment to a new agent
        state.tasks_being_assigned = {"task-343"}

        result = await report_task_progress(
            agent_id="agent-001",
            task_id="task-343",
            status="completed",
            progress=100,
            message="finished post-lease-recovery",
            state=state,
        )

        assert result["success"] is False
        assert result["status"] == "stale_completion"
        state.kanban_client.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_late_completion_when_other_agent_holds_assignment(
        self,
    ) -> None:
        """Another agent has been assigned → reject.

        After recovery, the task was re-assigned to a different
        agent whose ``agent_tasks`` entry now references this task.
        The original agent's late completion would conflict with
        the new agent's in-progress work. Reject.
        """
        state = _make_state_with_assignment("agent-001", task_id=None)
        # Different agent now owns the task
        state.agent_tasks["agent-002"] = _make_assignment("task-343", "agent-002")
        state.lease_manager = Mock()
        # extend_for_validation is awaited by report_task_progress on
        # status=completed (issue #667 Fix 1). Mock as AsyncMock so
        # the await resolves; the return value isn't asserted here.
        state.lease_manager.extend_for_validation = AsyncMock(return_value=None)
        state.lease_manager.active_leases = {}
        state.tasks_being_assigned = set()

        result = await report_task_progress(
            agent_id="agent-001",
            task_id="task-343",
            status="completed",
            progress=100,
            message="finished",
            state=state,
        )

        assert result["success"] is False
        assert result["status"] == "stale_completion"
        state.kanban_client.update_task.assert_not_called()
