"""
Unit tests for ADR-0012 D11 lease-epoch fencing (PRE-3 gate 2).

Background
----------
Marcus is migrating from *spawn-per-task* agents (one throwaway process per
task, killed afterwards) to long-lived **sessions** that register once and loop
pulling tasks. Under the old model, lease recovery *killed* the agent, so death
itself was the fencing token: a recovered task could never be finished twice.

Sessions remove that guarantee. Marcus is deliberately blind to the OS process,
so a session that trips the silence timeout while quietly doing deep work is
still alive. Marcus reassigns its card; the first session later reports done.
Two live completions, one card, no rule for who wins.

ADR-0012 D11 answers this with a monotonically-increasing ``lease_epoch``
stamped at claim and carried on every report. This module pins that contract
plus the three *additional* un-fenced ownership paths found by the PRE-3 gate-1
call-site inventory (D11 amendment, 2026-08-09).

What each test class covers
---------------------------
``TestLeaseEpochStamping``
    The epoch exists, starts at 1, and is unique per claim.
``TestLeaseEpochMonotonicity``
    The epoch never repeats for a task -- *including across release*, which is
    the property that actually fences a returning session. ``active_leases``
    holds only live leases and every release deletes the entry, so a naive
    "highest epoch in the dict" implementation silently reissues epoch 1.
``TestLeaseEpochPersistence``
    The epoch survives a Marcus restart. Without this, a restart resets the
    counter and defeats fencing entirely.
``TestRenewLeaseOwnership``
    ``renew_lease`` must not let a non-holder overwrite the holder's progress
    or extend its expiry.
``TestTouchLeaseScoping``
    The D3 heartbeat must be deterministic when an agent holds more than one
    lease (reachable post-restart, when the one-task-per-agent guard is
    skipped because registration was never rehydrated -- obligation O1).

Notes
-----
These tests are written before the implementation exists (TDD). They are
expected to fail on first run; that failure is the evidence they would catch
the defect.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.assignment_lease import AssignmentLease, AssignmentLeaseManager
from src.core.models import Priority, Task, TaskStatus

pytestmark = pytest.mark.unit


def _make_task(task_id: str = "task-123", estimated_hours: float = 4.0) -> Task:
    """
    Build a minimal Task suitable for lease creation.

    Parameters
    ----------
    task_id : str
        Identifier assigned to the task.
    estimated_hours : float
        Estimate used by the lease manager to size the initial lease.

    Returns
    -------
    Task
        A TODO task with no dependencies or labels.
    """
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="Fixture task",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        estimated_hours=estimated_hours,
        dependencies=[],
        labels=[],
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
    )


@pytest.fixture
def mock_kanban_client() -> Mock:
    """
    Create a mock kanban client.

    Returns
    -------
    Mock
        Client whose task-mutating methods are async no-ops.
    """
    client = Mock()
    client.update_task_status = AsyncMock()
    client.update_task = AsyncMock()
    return client


@pytest.fixture
def mock_persistence() -> Mock:
    """
    Create a mock assignment-persistence layer backed by a dict.

    The real persistence layer round-trips assignment dicts to disk. Tests that
    exercise restart behaviour need the values written by ``save_assignment``
    to be visible to a later ``load_assignments``, so this fixture records what
    was persisted rather than discarding it.

    Returns
    -------
    Mock
        Persistence double exposing ``saved`` -- the agent_id-keyed dict of
        assignment records that were written.
    """
    persistence = Mock()
    persistence.saved: dict = {}

    def _merge(
        agent_id: str, task_id: str, updates: dict, create_if_absent: bool = True
    ) -> None:
        """Mirror the real merge semantics.

        Same task keeps prior fields; a released or reassigned record is
        left untouched when creation is refused, so a stale write cannot
        resurrect it.
        """
        existing = persistence.saved.get(agent_id)
        if existing is not None and existing.get("task_id") == task_id:
            record = dict(existing)
        elif create_if_absent:
            record = {
                "assigned_at": datetime.now(timezone.utc).isoformat(),
                "task_data": {},
            }
        else:
            return
        record["task_id"] = task_id
        record.update(updates)
        persistence.saved[agent_id] = record

    async def _get_assignment(agent_id: str):
        # Returns None when absent, exactly like the real implementation.
        return persistence.saved.get(agent_id)

    async def _save_assignment(agent_id: str, task_id: str, task_data):
        _merge(agent_id, task_id, {"task_data": task_data})

    async def _update_assignment_fields(
        agent_id: str, task_id: str, fields: dict, *, create_if_absent: bool = True
    ):
        _merge(agent_id, task_id, fields, create_if_absent)

    persistence.get_assignment = AsyncMock(side_effect=_get_assignment)
    persistence.save_assignment = AsyncMock(side_effect=_save_assignment)
    persistence.update_assignment_fields = AsyncMock(
        side_effect=_update_assignment_fields
    )
    persistence.remove_assignment = AsyncMock()
    persistence.load_assignments = AsyncMock(return_value={})
    return persistence


@pytest.fixture
def lease_manager(mock_kanban_client: Mock, mock_persistence: Mock):
    """
    Create a lease manager wired to the mock kanban client and persistence.

    Returns
    -------
    AssignmentLeaseManager
        Manager in conservative mode (4-hour default lease).
    """
    return AssignmentLeaseManager(
        mock_kanban_client, mock_persistence, default_lease_hours=4.0
    )


class TestLeaseEpochStamping:
    """Every claim of a card carries a fencing token."""

    @pytest.mark.asyncio
    async def test_create_lease_stamps_an_epoch(self, lease_manager) -> None:
        """A newly created lease carries a positive lease_epoch."""
        lease = await lease_manager.create_lease("task-123", "agent-1", _make_task())

        assert lease.lease_epoch >= 1

    @pytest.mark.asyncio
    async def test_first_claim_starts_at_one(self, lease_manager) -> None:
        """The first ever claim of a card is epoch 1, not 0.

        Zero is reserved as "no epoch supplied", so a report that omits the
        epoch can never accidentally match a real one.
        """
        lease = await lease_manager.create_lease("task-123", "agent-1", _make_task())

        assert lease.lease_epoch == 1

    @pytest.mark.asyncio
    async def test_epochs_are_independent_per_task(self, lease_manager) -> None:
        """Two different cards each start their own epoch sequence."""
        first = await lease_manager.create_lease(
            "task-a", "agent-1", _make_task("task-a")
        )
        second = await lease_manager.create_lease(
            "task-b", "agent-2", _make_task("task-b")
        )

        assert first.lease_epoch == 1
        assert second.lease_epoch == 1


class TestLeaseEpochMonotonicity:
    """The epoch must never repeat for a card -- especially across release."""

    @pytest.mark.asyncio
    async def test_reclaim_after_release_gets_a_higher_epoch(
        self, lease_manager
    ) -> None:
        """Re-claiming a released card issues a strictly higher epoch.

        This is the property that actually fences a returning session. Recovery
        removes the lease from ``active_leases``; if the counter is derived
        from that dict, the replacement agent is handed epoch 1 again and the
        first session's stale report compares equal.
        """
        first = await lease_manager.create_lease("task-123", "agent-1", _make_task())
        first_epoch = first.lease_epoch

        await lease_manager.release_lease("task-123", reason="lease_expired")

        second = await lease_manager.create_lease("task-123", "agent-2", _make_task())

        assert second.lease_epoch > first_epoch

    @pytest.mark.asyncio
    async def test_epoch_increases_across_many_reclaims(self, lease_manager) -> None:
        """Epochs strictly increase over a sequence of claim/release cycles."""
        seen = []
        for index in range(5):
            lease = await lease_manager.create_lease(
                "task-123", f"agent-{index}", _make_task()
            )
            seen.append(lease.lease_epoch)
            await lease_manager.release_lease("task-123", reason="lease_expired")

        assert seen == sorted(set(seen)), f"epochs not strictly increasing: {seen}"
        assert len(set(seen)) == 5

    @pytest.mark.asyncio
    async def test_renewal_does_not_change_the_epoch(self, lease_manager) -> None:
        """Renewing a lease keeps the same epoch.

        The epoch identifies *the claim*, not the heartbeat. A renewal is the
        same agent continuing the same claim, so bumping it here would
        invalidate reports the holder has already issued.
        """
        lease = await lease_manager.create_lease("task-123", "agent-1", _make_task())
        original = lease.lease_epoch

        renewed = await lease_manager.renew_lease(
            "task-123", 50, "halfway", agent_id="agent-1"
        )

        assert renewed is not None
        assert renewed.lease_epoch == original


class TestLeaseEpochPersistence:
    """The fencing token must survive a Marcus restart."""

    @pytest.mark.asyncio
    async def test_persist_lease_writes_the_epoch(
        self, lease_manager, mock_persistence
    ) -> None:
        """The epoch is written to assignment persistence on claim."""
        await lease_manager.create_lease("task-123", "agent-1", _make_task())

        record = mock_persistence.saved.get("agent-1", {})
        assert record.get("lease_epoch") == 1

    @pytest.mark.asyncio
    async def test_load_active_leases_restores_the_epoch(
        self, lease_manager, mock_persistence
    ) -> None:
        """A rehydrated lease carries the epoch it was claimed under."""
        now = datetime.now(timezone.utc)
        mock_persistence.load_assignments = AsyncMock(
            return_value={
                "agent-1": {
                    "task_id": "task-123",
                    "assigned_at": now.isoformat(),
                    "lease_expires": (now + timedelta(hours=4)).isoformat(),
                    "lease_renewed_at": now.isoformat(),
                    "lease_epoch": 7,
                }
            }
        )

        await lease_manager.load_active_leases()

        assert lease_manager.active_leases["task-123"].lease_epoch == 7

    @pytest.mark.asyncio
    async def test_reclaim_after_restart_does_not_reuse_an_epoch(
        self, lease_manager, mock_persistence
    ) -> None:
        """A card re-claimed after restart gets an epoch above the rehydrated one.

        This is the restart-shaped version of the release bug: Marcus restarts,
        rehydrates a lease at epoch 7, that lease expires, and the replacement
        claim must not be issued epoch 1.
        """
        now = datetime.now(timezone.utc)
        mock_persistence.load_assignments = AsyncMock(
            return_value={
                "agent-1": {
                    "task_id": "task-123",
                    "assigned_at": now.isoformat(),
                    "lease_expires": (now + timedelta(hours=4)).isoformat(),
                    "lease_renewed_at": now.isoformat(),
                    "lease_epoch": 7,
                }
            }
        )
        await lease_manager.load_active_leases()
        await lease_manager.release_lease("task-123", reason="lease_expired")

        replacement = await lease_manager.create_lease(
            "task-123", "agent-2", _make_task()
        )

        assert replacement.lease_epoch > 7

    @pytest.mark.asyncio
    async def test_legacy_assignment_without_epoch_rehydrates_safely(
        self, lease_manager, mock_persistence
    ) -> None:
        """Assignments written before this field existed still load.

        Pre-migration rows have no ``lease_epoch`` key. They must rehydrate
        rather than raise, so an operator upgrading Marcus does not lose every
        in-flight lease.
        """
        now = datetime.now(timezone.utc)
        mock_persistence.load_assignments = AsyncMock(
            return_value={
                "agent-1": {
                    "task_id": "task-123",
                    "assigned_at": now.isoformat(),
                    "lease_expires": (now + timedelta(hours=4)).isoformat(),
                    "lease_renewed_at": now.isoformat(),
                }
            }
        )

        await lease_manager.load_active_leases()

        assert lease_manager.active_leases["task-123"].lease_epoch == 0


class TestRenewLeaseOwnership:
    """A non-holder must not be able to renew or overwrite a held lease."""

    @pytest.mark.asyncio
    async def test_holder_can_renew(self, lease_manager) -> None:
        """The agent holding the lease renews it normally."""
        await lease_manager.create_lease("task-123", "agent-1", _make_task())

        renewed = await lease_manager.renew_lease(
            "task-123", 50, "halfway", agent_id="agent-1"
        )

        assert renewed is not None
        assert renewed.progress_percentage == 50

    @pytest.mark.asyncio
    async def test_non_holder_renewal_is_refused(self, lease_manager) -> None:
        """An agent that does not hold the lease cannot renew it."""
        await lease_manager.create_lease("task-123", "agent-1", _make_task())

        result = await lease_manager.renew_lease(
            "task-123", 90, "I am not the holder", agent_id="agent-2"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_non_holder_cannot_overwrite_progress(self, lease_manager) -> None:
        """A refused renewal leaves the holder's self-reported progress intact.

        ``renew_lease`` currently takes no agent_id and looks the lease up by
        task alone, so a displaced agent's progress report overwrites the real
        holder's ``progress_percentage`` and ``last_progress_message``.
        """
        await lease_manager.create_lease("task-123", "agent-1", _make_task())
        await lease_manager.renew_lease(
            "task-123", 40, "holder at 40", agent_id="agent-1"
        )

        await lease_manager.renew_lease(
            "task-123", 95, "impostor at 95", agent_id="agent-2"
        )

        lease = lease_manager.active_leases["task-123"]
        assert lease.progress_percentage == 40
        assert lease.last_progress_message == "holder at 40"

    @pytest.mark.asyncio
    async def test_non_holder_cannot_extend_expiry(self, lease_manager) -> None:
        """A refused renewal does not push the lease's expiry forward.

        Otherwise a dead-but-chatty session keeps a card alive indefinitely
        while the real holder makes no progress.
        """
        await lease_manager.create_lease("task-123", "agent-1", _make_task())
        expiry_before = lease_manager.active_leases["task-123"].lease_expires

        await lease_manager.renew_lease("task-123", 95, "impostor", agent_id="agent-2")

        assert lease_manager.active_leases["task-123"].lease_expires == expiry_before

    @pytest.mark.asyncio
    async def test_renew_lease_requires_an_explicit_agent(self, lease_manager) -> None:
        """Omitting agent_id is a loud error, not a silent unfenced renewal.

        ``agent_id`` is keyword-only so that pre-migration positional calls --
        ``renew_lease(task_id, progress, message)`` -- raise TypeError instead
        of binding the progress integer to the agent parameter.
        """
        await lease_manager.create_lease("task-123", "agent-1", _make_task())

        with pytest.raises(TypeError):
            await lease_manager.renew_lease("task-123", 50, "no agent supplied")


class TestTouchLeaseScoping:
    """The D3 heartbeat must be deterministic when an agent holds two leases."""

    @pytest.mark.asyncio
    async def test_touch_lease_marks_the_named_task_alive(self, lease_manager) -> None:
        """Touching with an explicit task_id records liveness on that lease.

        The heartbeat stamps ``last_renewed`` and appends an update
        timestamp. It must never *shorten* the lease: in conservative
        mode (``default_lease_hours >= 1.0`` -- what both shipped configs
        set) the adaptive window is minutes while the lease runs for
        hours, so a naive assignment would cut a 4-hour lease down to the
        next few minutes on every MCP call the agent makes.
        """
        await lease_manager.create_lease("task-a", "agent-1", _make_task("task-a"))
        lease = lease_manager.active_leases["task-a"]
        expiry_before = lease.lease_expires
        renewed_before = lease.last_renewed
        updates_before = len(lease.update_timestamps)

        touched = await lease_manager.touch_lease("agent-1", task_id="task-a")

        assert touched is True
        assert lease.last_renewed >= renewed_before
        assert len(lease.update_timestamps) == updates_before + 1
        assert lease.lease_expires >= expiry_before

    @pytest.mark.asyncio
    async def test_touch_lease_does_not_extend_another_agents_lease(
        self, lease_manager
    ) -> None:
        """An agent's heartbeat never touches a lease it does not hold."""
        await lease_manager.create_lease("task-a", "agent-1", _make_task("task-a"))
        await lease_manager.create_lease("task-b", "agent-2", _make_task("task-b"))
        other_before = lease_manager.active_leases["task-b"].lease_expires

        await lease_manager.touch_lease("agent-1")

        assert lease_manager.active_leases["task-b"].lease_expires == other_before

    @pytest.mark.asyncio
    async def test_touch_lease_without_task_extends_all_held_leases(
        self, lease_manager
    ) -> None:
        """With no task named, every lease the agent holds is extended.

        An agent normally holds one task, but the one-task-per-agent guard is
        skipped when registration is missing -- the post-restart state
        described by obligation O1. In that state the current implementation
        extends an arbitrary "first match" and lets the other lease expire
        under a demonstrably live agent.
        """
        await lease_manager.create_lease("task-a", "agent-1", _make_task("task-a"))
        await lease_manager.create_lease("task-b", "agent-1", _make_task("task-b"))
        lease_a = lease_manager.active_leases["task-a"]
        lease_b = lease_manager.active_leases["task-b"]
        updates_a = len(lease_a.update_timestamps)
        updates_b = len(lease_b.update_timestamps)

        touched = await lease_manager.touch_lease("agent-1")

        assert touched is True
        assert len(lease_a.update_timestamps) == updates_a + 1
        assert len(lease_b.update_timestamps) == updates_b + 1

    @pytest.mark.asyncio
    async def test_touch_lease_for_unheld_task_is_refused(self, lease_manager) -> None:
        """Naming a task the agent does not hold does not extend it."""
        await lease_manager.create_lease("task-a", "agent-1", _make_task("task-a"))
        before = lease_manager.active_leases["task-a"].lease_expires

        touched = await lease_manager.touch_lease("agent-2", task_id="task-a")

        assert touched is False
        assert lease_manager.active_leases["task-a"].lease_expires == before


class TestStaleEpochDetection:
    """The manager can tell a current claim from a superseded one."""

    @pytest.mark.asyncio
    async def test_current_epoch_is_accepted(self, lease_manager) -> None:
        """A report carrying the live epoch is recognised as current."""
        lease = await lease_manager.create_lease("task-123", "agent-1", _make_task())

        assert (
            await lease_manager.is_epoch_current("task-123", lease.lease_epoch) is True
        )

    @pytest.mark.asyncio
    async def test_superseded_epoch_is_rejected(self, lease_manager) -> None:
        """A report from a displaced session is recognised as stale."""
        first = await lease_manager.create_lease("task-123", "agent-1", _make_task())
        await lease_manager.release_lease("task-123", reason="lease_expired")
        await lease_manager.create_lease("task-123", "agent-2", _make_task())

        assert (
            await lease_manager.is_epoch_current("task-123", first.lease_epoch) is False
        )

    @pytest.mark.asyncio
    async def test_missing_epoch_is_not_treated_as_current(self, lease_manager) -> None:
        """A report with no epoch (0) never compares equal to a real claim.

        Agent prompts and MCP signatures roll out gradually; an un-migrated
        agent must not be silently granted holder status.
        """
        await lease_manager.create_lease("task-123", "agent-1", _make_task())

        assert await lease_manager.is_epoch_current("task-123", 0) is False


class TestSilenceCheckpointInvariant:
    """ADR-0012 D11 part 3: pin the two dials apart.

    D3's silence timeout must be at least twice D8's checkpoint cadence.
    Otherwise false recovery is not a rare accident — it is designed in:
    an agent obeying the checkpoint obligation exactly can still be
    declared dead between two checkpoints, and parts 1 and 2 of D11 are
    left carrying load they were never meant to carry alone.
    """

    def test_defaults_satisfy_the_invariant(self) -> None:
        """The shipped defaults hold the >= 2x relationship."""
        from src.core.lease_invariants import (
            DEFAULT_CHECKPOINT_CADENCE_MINUTES,
            DEFAULT_SILENCE_TIMEOUT_MINUTES,
            validate_silence_checkpoint_invariant,
        )

        assert DEFAULT_SILENCE_TIMEOUT_MINUTES >= 2 * (
            DEFAULT_CHECKPOINT_CADENCE_MINUTES
        )
        validate_silence_checkpoint_invariant(
            DEFAULT_SILENCE_TIMEOUT_MINUTES, DEFAULT_CHECKPOINT_CADENCE_MINUTES
        )

    def test_exactly_two_times_is_allowed(self) -> None:
        """The bound is inclusive: 2x is the documented minimum."""
        from src.core.lease_invariants import validate_silence_checkpoint_invariant

        validate_silence_checkpoint_invariant(40.0, 20.0)

    def test_too_tight_a_timeout_is_rejected(self) -> None:
        """A config that designs false recovery in must not start."""
        from src.core.lease_invariants import validate_silence_checkpoint_invariant

        with pytest.raises(ValueError, match="at least 2"):
            validate_silence_checkpoint_invariant(30.0, 20.0)

    def test_error_names_both_values_and_the_fix(self) -> None:
        """An operator must be able to act on the message."""
        from src.core.lease_invariants import validate_silence_checkpoint_invariant

        with pytest.raises(ValueError) as excinfo:
            validate_silence_checkpoint_invariant(25.0, 20.0)

        text = str(excinfo.value)
        assert "25" in text
        assert "20" in text
        assert "40" in text

    def test_non_positive_cadence_is_rejected(self) -> None:
        """A zero or negative cadence makes the invariant meaningless."""
        from src.core.lease_invariants import validate_silence_checkpoint_invariant

        with pytest.raises(ValueError, match="positive"):
            validate_silence_checkpoint_invariant(45.0, 0.0)
