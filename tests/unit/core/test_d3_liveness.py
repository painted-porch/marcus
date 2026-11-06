"""
Unit tests for ADR-0012 D3: two liveness dials instead of one lease.

The old lease conflated "is the agent alive?" with "how long may it hold
the card?", so detecting a dead worker was tied to how big and how chatty
its chore was. D3 splits them: a **silence timeout** (how long an agent
may make no MCP call before it is presumed dead) and a **budget ceiling**
(how long it may hold the chore at all). These tests pin that split.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from src.config.marcus_config import TaskLeaseSettings
from src.core.assignment_lease import AssignmentLease, AssignmentLeaseManager
from src.core.lease_invariants import (
    DEFAULT_CHECKPOINT_CADENCE_MINUTES,
    DEFAULT_SILENCE_TIMEOUT_MINUTES,
)
from src.core.models import Priority, Task, TaskStatus

pytestmark = pytest.mark.unit

MINUTE = timedelta(minutes=1)


def _task(
    task_id: str = "task-1",
    estimated_hours: float = 2.0,
    priority: Priority = Priority.MEDIUM,
    labels: Optional[List[str]] = None,
) -> Task:
    """Build a minimal task with the fields the budget reads."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name="chore",
        description="",
        status=TaskStatus.TODO,
        priority=priority,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=estimated_hours,
        dependencies=[],
        labels=labels or [],
    )


@pytest.fixture
def kanban() -> Mock:
    """A board that knows no tasks (no terminal-status short-circuit)."""
    client = Mock()
    client.get_task_by_id = AsyncMock(return_value=None)
    client.update_task = AsyncMock()
    client.update_task_status = AsyncMock()
    return client


@pytest.fixture
def persistence() -> AsyncMock:
    """Assignment persistence that records writes and loads nothing."""
    store = AsyncMock()
    store.load_assignments = AsyncMock(return_value={})
    store.load_task_epochs = AsyncMock(return_value={})
    store.get_assignment = AsyncMock(return_value=None)
    return store


@pytest.fixture
def manager(kanban: Mock, persistence: AsyncMock) -> AssignmentLeaseManager:
    """A manager on the shipped D3 defaults."""
    return AssignmentLeaseManager(kanban, persistence)


class TestTheTwoDials:
    """Test suite for the configuration surface D3 introduces and removes."""

    def test_defaults_are_the_adr_values(self, manager: AssignmentLeaseManager) -> None:
        """45 minutes of silence, 3x the estimate, on both the manager and the config."""
        assert (
            manager.silence_timeout_minutes == DEFAULT_SILENCE_TIMEOUT_MINUTES == 45.0
        )
        assert manager.budget_multiplier == 3.0
        settings = TaskLeaseSettings()
        assert settings.silence_timeout_minutes == 45.0
        assert settings.budget_multiplier == 3.0
        assert settings.checkpoint_cadence_minutes == DEFAULT_CHECKPOINT_CADENCE_MINUTES

    @pytest.mark.parametrize(
        "removed",
        [
            {"max_renewals": 10},
            {"stuck_task_threshold_renewals": 5},
            {"renewal_decay_factor": 0.9},
            {"silence_multiplier": 5.0},
        ],
    )
    def test_the_single_dial_knobs_are_gone(
        self, kanban: Mock, persistence: AsyncMock, removed: dict
    ) -> None:
        """The renewal caps and the cadence multiplier no longer exist on the manager."""
        with pytest.raises(TypeError):
            AssignmentLeaseManager(kanban, persistence, **removed)

    def test_legacy_config_keys_still_load(self) -> None:
        """Config files on disk that name the removed knobs must still parse."""
        settings = TaskLeaseSettings(
            max_renewals=10,
            stuck_threshold_renewals=5,
            renewal_decay_factor=0.9,
            silence_multiplier=1.5,
        )
        assert settings.silence_timeout_minutes == 45.0

    def test_silence_checkpoint_invariant_is_enforced_at_construction(
        self, kanban: Mock, persistence: AsyncMock
    ) -> None:
        """A timeout under twice the checkpoint cadence designs false recovery in."""
        with pytest.raises(ValueError, match="at least 2"):
            AssignmentLeaseManager(
                kanban,
                persistence,
                silence_timeout_minutes=30.0,
                checkpoint_cadence_minutes=20.0,
            )

    def test_from_settings_maps_the_new_fields_and_ignores_the_old(
        self, kanban: Mock, persistence: AsyncMock
    ) -> None:
        """The server builds the manager from settings through one function."""
        settings = TaskLeaseSettings(
            silence_timeout_minutes=60.0,
            budget_multiplier=2.0,
            max_renewals=3,
            silence_multiplier=1.5,
        )
        built = AssignmentLeaseManager.from_settings(kanban, persistence, settings)
        assert built.silence_timeout_minutes == 60.0
        assert built.budget_multiplier == 2.0
        assert built.max_lease_hours == settings.max_lease_hours

    def test_from_settings_accepts_a_project_dict_with_unknown_keys(
        self, kanban: Mock, persistence: AsyncMock
    ) -> None:
        """Per-project lease config arrives as a dict and may carry old keys."""
        built = AssignmentLeaseManager.from_settings(
            kanban,
            persistence,
            {
                "silence_timeout_minutes": 50,
                "stuck_threshold_renewals": 5,
                "unknown": 1,
            },
        )
        assert built.silence_timeout_minutes == 50.0


class TestLiveness:
    """Test suite for the first dial: is the worker alive?"""

    @pytest.mark.asyncio
    async def test_expiry_is_the_silence_timeout_whatever_the_estimate(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """A quiet worker on a tiny or a huge chore gets the same 45 minutes."""
        before = datetime.now(timezone.utc)
        small = await manager.create_lease(
            "t-small", "agent-a", _task("t-small", estimated_hours=0.1)
        )
        large = await manager.create_lease(
            "t-large", "agent-b", _task("t-large", estimated_hours=40.0)
        )
        for lease in (small, large):
            assert (
                timedelta(minutes=44)
                < lease.lease_expires - before
                < timedelta(minutes=46)
            )

    @pytest.mark.asyncio
    async def test_a_heartbeat_moves_expiry_to_now_plus_the_timeout(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """touch_lease resets the silence clock; it does not shorten it."""
        lease = await manager.create_lease("t1", "agent-a", _task("t1"))
        lease.lease_expires = (
            datetime.now(timezone.utc) + 5 * MINUTE
        )  # 40 minutes of silence so far

        assert await manager.touch_lease("agent-a", "t1") is True

        remaining = lease.lease_expires - datetime.now(timezone.utc)
        assert timedelta(minutes=44) < remaining < timedelta(minutes=46)

    @pytest.mark.asyncio
    async def test_a_progress_report_moves_expiry_to_now_plus_the_timeout(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """renew_lease is the same clock reset, with progress recorded."""
        lease = await manager.create_lease("t1", "agent-a", _task("t1"))
        lease.lease_expires = datetime.now(timezone.utc) + 5 * MINUTE

        renewed = await manager.renew_lease("t1", 40, "halfway", agent_id="agent-a")

        assert renewed is not None
        remaining = renewed.lease_expires - datetime.now(timezone.utc)
        assert timedelta(minutes=44) < remaining < timedelta(minutes=46)
        assert renewed.progress_percentage == 40

    @pytest.mark.asyncio
    async def test_silence_past_the_timeout_is_death_with_no_cadence_math(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """An expired lease is recovered even when the old cadence rule would have waited.

        Under the single dial, a slow-but-regular reporter (median gap 10
        minutes) got median x 5 = 50 minutes of patience, longer than the
        timeout itself. Under D3 the timeout is the whole decision.
        """
        lease = await manager.create_lease("t1", "agent-a", _task("t1"))
        now = datetime.now(timezone.utc)
        lease.update_timestamps = [
            now - 70 * MINUTE,
            now - 60 * MINUTE,
            now - 50 * MINUTE,
        ]
        lease.lease_expires = now - 5 * MINUTE

        assert await manager.should_recover_expired_lease(lease) is True

    @pytest.mark.asyncio
    async def test_terminal_task_is_still_never_recovered(
        self, manager: AssignmentLeaseManager, kanban: Mock
    ) -> None:
        """The defense-in-depth guard against recovering finished work stays."""
        lease = await manager.create_lease("t1", "agent-a", _task("t1"))
        lease.lease_expires = datetime.now(timezone.utc) - 5 * MINUTE
        done = _task("t1")
        done.status = TaskStatus.DONE
        kanban.get_task_by_id = AsyncMock(return_value=done)

        assert await manager.should_recover_expired_lease(lease) is False

    @pytest.mark.asyncio
    async def test_blocked_task_is_never_recovered(
        self, manager: AssignmentLeaseManager, kanban: Mock
    ) -> None:
        """BLOCKED is terminal for recovery purposes too."""
        lease = await manager.create_lease("t1", "agent-a", _task("t1"))
        lease.lease_expires = datetime.now(timezone.utc) - 5 * MINUTE
        blocked = _task("t1")
        blocked.status = TaskStatus.BLOCKED
        kanban.get_task_by_id = AsyncMock(return_value=blocked)

        assert await manager.should_recover_expired_lease(lease) is False

    @pytest.mark.asyncio
    async def test_declined_task_is_never_recovered(
        self, manager: AssignmentLeaseManager, kanban: Mock
    ) -> None:
        """DECLINED is terminal for recovery purposes too (ADR-0013
        K5, Codex round-6 sweep on PR #834) -- a lease that expires
        around the same time as a decline must not hand the declined
        task to a fresh agent."""
        lease = await manager.create_lease("t1", "agent-a", _task("t1"))
        lease.lease_expires = datetime.now(timezone.utc) - 5 * MINUTE
        declined = _task("t1")
        declined.status = TaskStatus.DECLINED
        kanban.get_task_by_id = AsyncMock(return_value=declined)

        assert await manager.should_recover_expired_lease(lease) is False

    @pytest.mark.asyncio
    async def test_terminal_skip_increments_the_observability_counter(
        self, manager: AssignmentLeaseManager, kanban: Mock
    ) -> None:
        """Every terminal skip is counted: it marks a stale lease somewhere upstream."""
        lease = await manager.create_lease("t1", "agent-a", _task("t1"))
        lease.lease_expires = datetime.now(timezone.utc) - 5 * MINUTE
        done = _task("t1")
        done.status = TaskStatus.DONE
        kanban.get_task_by_id = AsyncMock(return_value=done)

        await manager.should_recover_expired_lease(lease)
        await manager.should_recover_expired_lease(lease)

        assert manager.recoveries_skipped_terminal_status == 2

    @pytest.mark.asyncio
    async def test_renewals_are_unlimited_and_quiet(
        self, manager: AssignmentLeaseManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A healthy long chore renews as often as it likes with no 'consider escalation'."""
        await manager.create_lease("t1", "agent-a", _task("t1", estimated_hours=40.0))
        with caplog.at_level(logging.WARNING):
            for i in range(50):
                assert await manager.renew_lease(
                    "t1", min(99, i * 2), "", agent_id="agent-a"
                )
        assert "renewed" not in " ".join(
            r.getMessage().lower()
            for r in caplog.records
            if r.levelno >= logging.WARNING
        )


class TestBudgetCeiling:
    """Test suite for the second dial: how long may it hold the chore?"""

    @pytest.mark.asyncio
    async def test_budget_is_three_times_the_estimate(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """A two-hour estimate buys six hours of budget."""
        lease = await manager.create_lease(
            "t1", "agent-a", _task("t1", estimated_hours=2.0)
        )
        assert lease.budget_deadline is not None
        assert (
            abs((lease.budget_deadline - lease.assigned_at) - timedelta(hours=6))
            < MINUTE
        )

    @pytest.mark.asyncio
    async def test_priority_and_complexity_scale_the_budget(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """The existing multipliers now shape the ceiling, not the heartbeat."""
        urgent = await manager.create_lease(
            "t-urgent",
            "a",
            _task("t-urgent", estimated_hours=2.0, priority=Priority.HIGH),
        )
        complex_ = await manager.create_lease(
            "t-complex",
            "b",
            _task("t-complex", estimated_hours=2.0, labels=["complex"]),
        )
        assert (
            abs((urgent.budget_deadline - urgent.assigned_at) - timedelta(hours=4.5))
            < MINUTE
        )
        assert (
            abs((complex_.budget_deadline - complex_.assigned_at) - timedelta(hours=9))
            < MINUTE
        )

    @pytest.mark.asyncio
    async def test_budget_never_undercuts_the_silence_timeout(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """A tiny estimate still gets one full silence window."""
        lease = await manager.create_lease(
            "t1", "agent-a", _task("t1", estimated_hours=0.05)
        )
        assert (
            abs((lease.budget_deadline - lease.assigned_at) - timedelta(minutes=45))
            < MINUTE
        )

    @pytest.mark.asyncio
    async def test_budget_is_capped_by_max_lease_hours(
        self, kanban: Mock, persistence: AsyncMock
    ) -> None:
        """max_lease_hours is the absolute ceiling on any chore."""
        capped = AssignmentLeaseManager(kanban, persistence, max_lease_hours=24.0)
        lease = await capped.create_lease(
            "t1", "agent-a", _task("t1", estimated_hours=40.0)
        )
        assert (
            abs((lease.budget_deadline - lease.assigned_at) - timedelta(hours=24))
            < MINUTE
        )

    @pytest.mark.asyncio
    async def test_no_estimate_falls_back_to_the_default_hours(
        self, kanban: Mock, persistence: AsyncMock
    ) -> None:
        """A task without an estimate budgets from default_lease_hours."""
        built = AssignmentLeaseManager(kanban, persistence, default_lease_hours=4.0)
        task = _task("t1")
        task.estimated_hours = None  # type: ignore[assignment]
        lease = await built.create_lease("t1", "agent-a", task)
        assert (
            abs((lease.budget_deadline - lease.assigned_at) - timedelta(hours=12))
            < MINUTE
        )

    @pytest.mark.asyncio
    async def test_a_heartbeat_cannot_extend_past_the_budget(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """Near the ceiling, a touch extends only up to the ceiling."""
        lease = await manager.create_lease(
            "t1", "agent-a", _task("t1", estimated_hours=0.05)
        )
        now = datetime.now(timezone.utc)
        lease.budget_deadline = now + 10 * MINUTE
        lease.lease_expires = now + 5 * MINUTE

        await manager.touch_lease("agent-a", "t1")

        assert lease.lease_expires == lease.budget_deadline

    @pytest.mark.asyncio
    async def test_a_chatty_zombie_is_reclaimed_at_the_budget(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """Once the budget is spent, heartbeats no longer keep the chore."""
        lease = await manager.create_lease(
            "t1", "agent-a", _task("t1", estimated_hours=0.05)
        )
        now = datetime.now(timezone.utc)
        lease.budget_deadline = now - 5 * MINUTE
        lease.lease_expires = now - 5 * MINUTE

        assert await manager.touch_lease("agent-a", "t1") is False
        expired = await manager.check_expired_leases()

        assert [x.task_id for x in expired] == ["t1"]
        assert await manager.should_recover_expired_lease(lease) is True


class TestTheCeilingIsHard:
    """Test suite for Codex's finding on PR #767: no path may outlive the budget.

    Renewals and heartbeats were capped at the budget, but the validation
    window after a completion report was not, so an agent whose completion
    kept being rejected could buy ten more minutes each time, forever.
    Budget exhaustion is now an expiry condition in its own right.
    """

    @pytest.mark.asyncio
    async def test_validation_window_cannot_extend_past_the_budget(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """A completion near the ceiling gets only what is left of the budget."""
        lease = await manager.create_lease(
            "t1", "agent-a", _task("t1", estimated_hours=0.05)
        )
        now = datetime.now(timezone.utc)
        lease.budget_deadline = now + 5 * MINUTE
        lease.lease_expires = now + 2 * MINUTE

        extended = await manager.extend_for_validation("t1", window_seconds=600)

        assert extended is not None
        assert extended.lease_expires == lease.budget_deadline

    @pytest.mark.asyncio
    async def test_budget_exhaustion_expires_the_lease_whatever_lease_expires_says(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """An expiry pushed past the ceiling by any path still ends at the ceiling."""
        lease = await manager.create_lease(
            "t1", "agent-a", _task("t1", estimated_hours=0.05)
        )
        now = datetime.now(timezone.utc)
        lease.lease_expires = (
            now + 10 * MINUTE
        )  # as if a validation window had been granted
        lease.budget_deadline = now - 2 * MINUTE

        assert lease.is_expired is True
        assert lease.time_remaining < timedelta(0)
        expired = await manager.check_expired_leases()
        assert [x.task_id for x in expired] == ["t1"]
        assert await manager.should_recover_expired_lease(lease) is True

    @pytest.mark.asyncio
    async def test_repeated_rejected_completions_do_not_outlive_the_budget(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """Once the budget is spent, a further completion report gets no window."""
        lease = await manager.create_lease(
            "t1", "agent-a", _task("t1", estimated_hours=0.05)
        )
        now = datetime.now(timezone.utc)
        lease.budget_deadline = now - 1 * MINUTE
        lease.lease_expires = (
            now + 9 * MINUTE
        )  # a window granted just before the ceiling

        assert await manager.extend_for_validation("t1", window_seconds=600) is None
        assert await manager.touch_lease("agent-a", "t1") is False
        assert await manager.renew_lease("t1", 90, "", agent_id="agent-a") is None


class TestParkForOperator:
    """The one exception to the hard ceiling: time Marcus itself is broken.

    The smoke gate parks a task it cannot evaluate because of a Marcus-side
    misconfiguration; the agent re-reports until an operator acts. That
    wait must not spend the agent's budget (Codex P2 on PR #775).
    """

    @pytest.mark.asyncio
    async def test_park_moves_the_budget_past_the_window(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """A lease near its ceiling is held for the whole window."""
        lease = await manager.create_lease(
            "t1", "agent-a", _task("t1", estimated_hours=0.05)
        )
        now = datetime.now(timezone.utc)
        lease.budget_deadline = now + 1 * MINUTE
        lease.lease_expires = now + 1 * MINUTE

        parked = await manager.park_for_operator("t1", window_seconds=600)

        assert parked is not None
        assert parked.lease_expires >= now + 9 * MINUTE
        assert parked.budget_deadline is not None
        assert parked.budget_deadline >= parked.lease_expires
        assert parked.effective_expiry >= now + 9 * MINUTE

    @pytest.mark.asyncio
    async def test_repeated_parks_keep_the_lease_alive_without_stacking(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """Each re-report re-anchors on now; the window never accumulates."""
        lease = await manager.create_lease(
            "t1", "agent-a", _task("t1", estimated_hours=0.05)
        )
        first = await manager.park_for_operator("t1", window_seconds=600)
        assert first is not None
        first_expiry = first.lease_expires

        second = await manager.park_for_operator("t1", window_seconds=600)

        assert second is not None
        assert second.lease_expires - first_expiry < 1 * MINUTE
        assert lease.budget_deadline is not None
        assert lease.budget_deadline >= second.lease_expires

    @pytest.mark.asyncio
    async def test_a_generous_budget_is_left_alone(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """Parking only ever moves the ceiling later, never earlier."""
        lease = await manager.create_lease(
            "t1", "agent-a", _task("t1", estimated_hours=4.0)
        )
        before = lease.budget_deadline

        await manager.park_for_operator("t1", window_seconds=600)

        assert lease.budget_deadline == before

    @pytest.mark.asyncio
    async def test_an_expired_lease_cannot_be_parked(
        self, manager: AssignmentLeaseManager
    ) -> None:
        """Recovery owns an expired lease; parking does not resurrect it."""
        lease = await manager.create_lease(
            "t1", "agent-a", _task("t1", estimated_hours=0.05)
        )
        lease.budget_deadline = datetime.now(timezone.utc) - 1 * MINUTE

        assert await manager.park_for_operator("t1", window_seconds=600) is None
        assert await manager.park_for_operator("missing", window_seconds=600) is None

    @pytest.mark.asyncio
    async def test_park_is_persisted(
        self, manager: AssignmentLeaseManager, persistence: AsyncMock
    ) -> None:
        """The moved ceiling survives a restart like any other lease field."""
        await manager.create_lease("t1", "agent-a", _task("t1", estimated_hours=0.05))
        persistence.update_assignment_fields.reset_mock()

        await manager.park_for_operator("t1", window_seconds=600)

        assert persistence.update_assignment_fields.await_count >= 1


class TestPersistence:
    """Test suite for the ceiling surviving a restart."""

    @pytest.mark.asyncio
    async def test_budget_deadline_is_persisted(
        self, manager: AssignmentLeaseManager, persistence: AsyncMock
    ) -> None:
        """The ceiling is written with the lease."""
        lease = await manager.create_lease("t1", "agent-a", _task("t1"))
        fields = persistence.update_assignment_fields.call_args.args[2]
        assert fields["budget_deadline"] == lease.budget_deadline.isoformat()

    @pytest.mark.asyncio
    async def test_budget_deadline_is_rehydrated(
        self, kanban: Mock, persistence: AsyncMock
    ) -> None:
        """A restarted server restores the ceiling it had set."""
        assigned = datetime.now(timezone.utc) - 30 * MINUTE
        deadline = assigned + timedelta(hours=6)
        persistence.load_assignments = AsyncMock(
            return_value={
                "agent-a": {
                    "task_id": "t1",
                    "assigned_at": assigned.isoformat(),
                    "lease_expires": (assigned + 45 * MINUTE).isoformat(),
                    "budget_deadline": deadline.isoformat(),
                }
            }
        )
        built = AssignmentLeaseManager(kanban, persistence)
        await built.load_active_leases()
        assert built.active_leases["t1"].budget_deadline == deadline

    @pytest.mark.asyncio
    async def test_legacy_record_without_a_ceiling_has_none(
        self, kanban: Mock, persistence: AsyncMock
    ) -> None:
        """Rows written before D3 carry no ceiling; liveness alone governs them."""
        assigned = datetime.now(timezone.utc) - 30 * MINUTE
        persistence.load_assignments = AsyncMock(
            return_value={
                "agent-a": {
                    "task_id": "t1",
                    "assigned_at": assigned.isoformat(),
                    "lease_expires": (assigned + 45 * MINUTE).isoformat(),
                }
            }
        )
        built = AssignmentLeaseManager(kanban, persistence)
        await built.load_active_leases()
        assert built.active_leases["t1"].budget_deadline is None
