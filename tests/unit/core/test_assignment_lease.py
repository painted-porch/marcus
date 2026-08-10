"""
Unit tests for the assignment lease system.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.assignment_lease import (
    AssignmentLease,
    AssignmentLeaseManager,
    LeaseMonitor,
    LeaseStatus,
)
from src.core.models import Priority, RecoveryInfo, Task, TaskStatus

pytestmark = pytest.mark.unit


class TestAssignmentLease:
    """Test suite for AssignmentLease data class."""

    def test_lease_creation(self):
        """Test basic lease creation."""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=4)

        lease = AssignmentLease(
            task_id="task-123",
            agent_id="agent-001",
            assigned_at=now,
            lease_expires=expires,
            last_renewed=now,
        )

        assert lease.task_id == "task-123"
        assert lease.agent_id == "agent-001"
        assert not lease.is_expired
        assert lease.status == LeaseStatus.ACTIVE

    def test_lease_expiration(self):
        """Test lease expiration detection."""
        now = datetime.now(timezone.utc)

        # Create expired lease
        expired_lease = AssignmentLease(
            task_id="task-123",
            agent_id="agent-001",
            assigned_at=now - timedelta(hours=5),
            lease_expires=now - timedelta(hours=1),
            last_renewed=now - timedelta(hours=5),
        )

        assert expired_lease.is_expired
        assert expired_lease.status == LeaseStatus.EXPIRED

    def test_lease_expiring_soon(self):
        """Test detection of leases expiring soon."""
        now = datetime.now(timezone.utc)

        # Lease expiring in 30 minutes
        expiring_lease = AssignmentLease(
            task_id="task-123",
            agent_id="agent-001",
            assigned_at=now,
            lease_expires=now + timedelta(minutes=30),
            last_renewed=now,
        )

        assert expiring_lease.is_expiring_soon
        assert expiring_lease.status == LeaseStatus.EXPIRING_SOON
        assert not expiring_lease.is_expired

    def test_renewal_duration_calculation(self):
        """Test lease renewal duration calculation."""
        now = datetime.now(timezone.utc)
        base_lease = AssignmentLease(
            task_id="task-123",
            agent_id="agent-001",
            assigned_at=now,
            lease_expires=now + timedelta(hours=4),
            last_renewed=now,
        )

        # High progress - shorter renewal
        base_lease.progress_percentage = 80
        duration = base_lease.calculate_renewal_duration()
        assert duration == timedelta(hours=2.0)

        # Medium progress
        base_lease.progress_percentage = 60
        duration = base_lease.calculate_renewal_duration()
        assert duration == timedelta(hours=3.0)

        # Low progress with many renewals - stuck task
        base_lease.progress_percentage = 20
        base_lease.renewal_count = 3
        duration = base_lease.calculate_renewal_duration()
        assert duration == timedelta(hours=2.0)

        # Complex task
        base_lease.estimated_hours = 10
        base_lease.progress_percentage = 50
        base_lease.renewal_count = 1  # Reset renewal count
        duration = base_lease.calculate_renewal_duration()
        assert duration == timedelta(hours=6.0)  # 4 * 1.5


class TestAssignmentLeaseManager:
    """Test suite for AssignmentLeaseManager."""

    @pytest.fixture
    def mock_kanban_client(self):
        """Create mock kanban client."""
        client = Mock()
        client.update_task_status = AsyncMock()
        client.update_task = AsyncMock()
        return client

    @pytest.fixture
    def mock_persistence(self):
        """Create mock assignment persistence."""
        persistence = Mock()
        persistence.get_assignment = AsyncMock(
            return_value={
                "task_id": "task-123",
                "assigned_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        persistence.save_assignment = AsyncMock()
        persistence.update_assignment_fields = AsyncMock()
        persistence.remove_assignment = AsyncMock()
        persistence.load_assignments = AsyncMock(return_value={})
        return persistence

    @pytest.fixture
    def lease_manager(self, mock_kanban_client, mock_persistence):
        """Create lease manager instance."""
        return AssignmentLeaseManager(
            mock_kanban_client, mock_persistence, default_lease_hours=4.0
        )

    @pytest.mark.asyncio
    async def test_create_lease(self, lease_manager):
        """Test lease creation."""
        task = Task(
            id="task-123",
            name="Test Task",
            description="Test",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
            estimated_hours=5.0,
            dependencies=[],
            labels=[],
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
        )

        lease = await lease_manager.create_lease("task-123", "agent-001", task)

        assert lease.task_id == "task-123"
        assert lease.agent_id == "agent-001"
        assert lease.estimated_hours == 5.0
        assert not lease.is_expired
        assert "task-123" in lease_manager.active_leases

    @pytest.mark.asyncio
    async def test_renew_lease(self, lease_manager):
        """Test lease renewal."""
        # Create initial lease
        lease = await lease_manager.create_lease("task-123", "agent-001")

        initial_expiry = lease.lease_expires

        # Renew lease
        renewed_lease = await lease_manager.renew_lease("task-123", 50, "Halfway done")

        assert renewed_lease is not None
        assert renewed_lease.renewal_count == 1
        assert renewed_lease.progress_percentage == 50
        assert renewed_lease.lease_expires > initial_expiry

    @pytest.mark.asyncio
    async def test_renew_expired_lease_fails(self, lease_manager):
        """Test that expired leases cannot be renewed."""
        # Create expired lease
        lease = AssignmentLease(
            task_id="task-123",
            agent_id="agent-001",
            assigned_at=datetime.now(timezone.utc) - timedelta(hours=5),
            lease_expires=datetime.now(timezone.utc) - timedelta(hours=1),
            last_renewed=datetime.now(timezone.utc) - timedelta(hours=5),
        )
        lease_manager.active_leases["task-123"] = lease

        # Try to renew
        renewed_lease = await lease_manager.renew_lease("task-123", 50, "Progress")

        assert renewed_lease is None

    @pytest.mark.asyncio
    async def test_check_expired_leases(self, lease_manager):
        """Test detection of expired leases."""
        now = datetime.now(timezone.utc)

        # Add mix of leases
        active_lease = AssignmentLease(
            task_id="task-active",
            agent_id="agent-001",
            assigned_at=now,
            lease_expires=now + timedelta(hours=2),
            last_renewed=now,
        )

        expired_lease = AssignmentLease(
            task_id="task-expired",
            agent_id="agent-002",
            assigned_at=now - timedelta(hours=5),
            lease_expires=now - timedelta(hours=1),
            last_renewed=now - timedelta(hours=5),
        )

        lease_manager.active_leases = {
            "task-active": active_lease,
            "task-expired": expired_lease,
        }

        expired = await lease_manager.check_expired_leases()

        assert len(expired) == 1
        assert expired[0].task_id == "task-expired"

    @pytest.mark.asyncio
    async def test_recover_expired_lease(
        self, lease_manager, mock_kanban_client, mock_persistence
    ):
        """Test recovery of expired lease."""
        expired_lease = AssignmentLease(
            task_id="task-123",
            agent_id="agent-001",
            assigned_at=datetime.now(timezone.utc) - timedelta(hours=5),
            lease_expires=datetime.now(timezone.utc) - timedelta(hours=1),
            last_renewed=datetime.now(timezone.utc) - timedelta(hours=5),
            progress_percentage=30,
        )

        lease_manager.active_leases["task-123"] = expired_lease

        success = await lease_manager.recover_expired_lease(expired_lease)

        assert success
        assert "task-123" not in lease_manager.active_leases
        mock_persistence.remove_assignment.assert_called_once_with("agent-001")
        # Recovery uses update_task to atomically clear status + assigned_to
        mock_kanban_client.update_task.assert_called_once_with(
            "task-123", {"status": TaskStatus.TODO, "assigned_to": None}
        )

    @pytest.mark.asyncio
    async def test_get_expiring_leases(self, lease_manager):
        """Test getting leases that are expiring soon."""
        now = datetime.now(timezone.utc)

        # Add various leases
        lease_manager.active_leases = {
            "task-active": AssignmentLease(
                task_id="task-active",
                agent_id="agent-001",
                assigned_at=now,
                lease_expires=now + timedelta(hours=4),
                last_renewed=now,
            ),
            "task-expiring": AssignmentLease(
                task_id="task-expiring",
                agent_id="agent-002",
                assigned_at=now,
                lease_expires=now + timedelta(minutes=30),
                last_renewed=now,
            ),
            "task-expired": AssignmentLease(
                task_id="task-expired",
                agent_id="agent-003",
                assigned_at=now - timedelta(hours=5),
                lease_expires=now - timedelta(hours=1),
                last_renewed=now - timedelta(hours=5),
            ),
        }

        expiring = await lease_manager.get_expiring_leases()

        assert len(expiring) == 1
        assert expiring[0].task_id == "task-expiring"

    def test_lease_statistics(self, lease_manager):
        """Test lease statistics calculation."""
        now = datetime.now(timezone.utc)

        # Add various leases
        lease_manager.active_leases = {
            "task-1": AssignmentLease(
                task_id="task-1",
                agent_id="agent-001",
                assigned_at=now,
                lease_expires=now + timedelta(hours=4),
                last_renewed=now,
                renewal_count=2,
            ),
            "task-2": AssignmentLease(
                task_id="task-2",
                agent_id="agent-002",
                assigned_at=now,
                lease_expires=now + timedelta(minutes=30),
                last_renewed=now,
                renewal_count=8,
            ),
            "task-3": AssignmentLease(
                task_id="task-3",
                agent_id="agent-003",
                assigned_at=now - timedelta(hours=5),
                lease_expires=now - timedelta(hours=1),
                last_renewed=now - timedelta(hours=5),
                renewal_count=12,
            ),
        }

        stats = lease_manager.get_lease_statistics()

        assert stats["total_active"] == 3
        assert stats["expired"] == 1
        assert stats["expiring_soon"] == 1
        assert stats["high_renewal_count"] == 1  # Only task-3 has >= 10 renewals
        assert stats["average_renewal_count"] == (2 + 8 + 12) / 3


class TestLeaseMonitor:
    """Test suite for LeaseMonitor."""

    @pytest.fixture
    def mock_lease_manager(self):
        """Create mock lease manager."""
        manager = Mock()
        manager.active_leases = {}
        manager.load_active_leases = AsyncMock()
        manager.check_expired_leases = AsyncMock(return_value=[])
        manager.recover_expired_lease = AsyncMock(return_value=True)
        manager.get_lease_statistics = Mock(
            return_value={"total_active": 0, "expiring_soon": 0, "expired": 0}
        )
        manager.get_expiring_leases = AsyncMock(return_value=[])
        return manager

    @pytest.fixture
    def lease_monitor(self, mock_lease_manager):
        """Create lease monitor instance."""
        return LeaseMonitor(mock_lease_manager, check_interval_seconds=1)

    @pytest.mark.asyncio
    async def test_monitor_start_stop(self, lease_monitor):
        """Test starting and stopping the monitor."""
        await lease_monitor.start()
        assert lease_monitor._running
        assert lease_monitor._monitor_task is not None

        await lease_monitor.stop()
        assert not lease_monitor._running

    @pytest.mark.asyncio
    async def test_monitor_recovers_expired_leases(
        self, lease_monitor, mock_lease_manager
    ):
        """Test that monitor recovers expired leases with smart checks."""
        # Set up expired leases
        expired_lease = Mock()
        expired_lease.task_id = "task-123"
        mock_lease_manager.check_expired_leases.return_value = [expired_lease]

        # Mock smart recovery check to allow recovery
        mock_lease_manager.should_recover_expired_lease = AsyncMock(return_value=True)

        # Start monitor
        await lease_monitor.start()

        # Wait for at least one check cycle
        await asyncio.sleep(1.5)

        # Stop monitor
        await lease_monitor.stop()

        # Verify recovery was attempted
        mock_lease_manager.check_expired_leases.assert_called()
        mock_lease_manager.should_recover_expired_lease.assert_called_with(
            expired_lease
        )
        mock_lease_manager.recover_expired_lease.assert_called_with(expired_lease)


class TestNaiveDatetimeBackwardsCompatibility:
    """Test suite for backwards compatibility with naive datetime persistence."""

    @pytest.mark.asyncio
    async def test_load_active_leases_handles_naive_datetimes(self):
        """Test that load_active_leases normalizes naive datetimes to UTC."""
        from src.core.assignment_persistence import AssignmentPersistence

        # Arrange - create mock persistence with NAIVE datetime strings
        # (simulating old data saved with naive datetime.isoformat())
        mock_persistence = Mock(spec=AssignmentPersistence)

        # Create naive datetime (no timezone info)
        # Simulating old data by stripping timezone from a UTC datetime
        aware_now = datetime.now(timezone.utc)
        naive_now = aware_now.replace(tzinfo=None)  # Strip timezone
        naive_iso = naive_now.isoformat()  # No +00:00 suffix

        old_assignment = {
            "task_id": "task-123",
            "assigned_at": naive_iso,  # Naive datetime string
            "lease_expires": naive_iso,  # Naive datetime string
            "lease_renewed_at": naive_iso,  # Naive datetime string
            "renewal_count": 0,
            "progress_percentage": 0,
        }

        mock_persistence.load_assignments = AsyncMock(
            return_value={"agent-001": old_assignment}
        )

        # Create lease manager with mock persistence
        lease_manager = AssignmentLeaseManager(
            kanban_client=Mock(),
            assignment_persistence=mock_persistence,
        )

        # Act - load the old naive datetime assignments
        await lease_manager.load_active_leases()

        # Assert - lease should be created successfully
        assert len(lease_manager.active_leases) == 1
        lease = lease_manager.active_leases["task-123"]

        # All datetimes should be timezone-aware (UTC)
        assert lease.assigned_at.tzinfo is not None
        assert lease.assigned_at.tzinfo == timezone.utc
        assert lease.lease_expires.tzinfo is not None
        assert lease.lease_expires.tzinfo == timezone.utc
        assert lease.last_renewed.tzinfo is not None
        assert lease.last_renewed.tzinfo == timezone.utc

        # Should be able to call is_expired without TypeError
        _ = lease.is_expired  # This would raise TypeError before the fix

    @pytest.mark.asyncio
    async def test_load_active_leases_preserves_aware_datetimes(self):
        """Test that timezone-aware datetimes are preserved as-is."""
        from src.core.assignment_persistence import AssignmentPersistence

        # Arrange - create mock persistence with AWARE datetime strings
        mock_persistence = Mock(spec=AssignmentPersistence)

        aware_now = datetime.now(timezone.utc)
        aware_iso = aware_now.isoformat()  # Has +00:00 suffix

        new_assignment = {
            "task_id": "task-456",
            "assigned_at": aware_iso,
            "lease_expires": aware_iso,
            "lease_renewed_at": aware_iso,
            "renewal_count": 0,
            "progress_percentage": 0,
        }

        mock_persistence.load_assignments = AsyncMock(
            return_value={"agent-002": new_assignment}
        )

        lease_manager = AssignmentLeaseManager(
            kanban_client=Mock(),
            assignment_persistence=mock_persistence,
        )

        # Act
        await lease_manager.load_active_leases()

        # Assert
        assert len(lease_manager.active_leases) == 1
        lease = lease_manager.active_leases["task-456"]

        # All datetimes should be timezone-aware (UTC)
        assert lease.assigned_at.tzinfo == timezone.utc
        assert lease.lease_expires.tzinfo == timezone.utc
        assert lease.last_renewed.tzinfo == timezone.utc

    @pytest.mark.asyncio
    async def test_load_active_leases_mixed_naive_and_aware(self):
        """Test loading a mix of old naive and new aware datetimes."""
        from src.core.assignment_persistence import AssignmentPersistence

        mock_persistence = Mock(spec=AssignmentPersistence)

        # Old assignment with naive datetimes
        # Simulating old data by stripping timezone from a UTC datetime
        aware_now = datetime.now(timezone.utc)
        naive_now = aware_now.replace(tzinfo=None)  # Strip timezone
        old_assignment = {
            "task_id": "old-task",
            "assigned_at": naive_now.isoformat(),
            "lease_expires": naive_now.isoformat(),
            "lease_renewed_at": naive_now.isoformat(),
            "renewal_count": 0,
            "progress_percentage": 0,
        }

        # New assignment with aware datetimes
        aware_now = datetime.now(timezone.utc)
        new_assignment = {
            "task_id": "new-task",
            "assigned_at": aware_now.isoformat(),
            "lease_expires": aware_now.isoformat(),
            "lease_renewed_at": aware_now.isoformat(),
            "renewal_count": 0,
            "progress_percentage": 0,
        }

        mock_persistence.load_assignments = AsyncMock(
            return_value={
                "agent-old": old_assignment,
                "agent-new": new_assignment,
            }
        )

        lease_manager = AssignmentLeaseManager(
            kanban_client=Mock(),
            assignment_persistence=mock_persistence,
        )

        # Act
        await lease_manager.load_active_leases()

        # Assert - both leases loaded successfully
        assert len(lease_manager.active_leases) == 2

        old_lease = lease_manager.active_leases["old-task"]
        new_lease = lease_manager.active_leases["new-task"]

        # Both should be timezone-aware now
        assert old_lease.assigned_at.tzinfo == timezone.utc
        assert new_lease.assigned_at.tzinfo == timezone.utc

        # Both should be comparable without TypeError
        assert old_lease.time_remaining  # Computed successfully
        assert new_lease.time_remaining  # Computed successfully


class TestUpdateTimestampPersistence:
    """Test that update_timestamps survive persist/load cycle."""

    @pytest.mark.asyncio
    async def test_load_active_leases_restores_update_timestamps(self):
        """Test that update_timestamps are restored from persistence."""
        from src.core.assignment_persistence import AssignmentPersistence

        now = datetime.now(timezone.utc)
        timestamps = [
            (now - timedelta(seconds=180)).isoformat(),
            (now - timedelta(seconds=120)).isoformat(),
            (now - timedelta(seconds=60)).isoformat(),
        ]

        mock_persistence = Mock(spec=AssignmentPersistence)
        mock_persistence.load_assignments = AsyncMock(
            return_value={
                "agent-001": {
                    "task_id": "task-123",
                    "assigned_at": now.isoformat(),
                    "lease_expires": (now + timedelta(minutes=2)).isoformat(),
                    "lease_renewed_at": now.isoformat(),
                    "renewal_count": 3,
                    "progress_percentage": 50,
                    "update_timestamps": timestamps,
                }
            }
        )

        lease_manager = AssignmentLeaseManager(
            kanban_client=Mock(),
            assignment_persistence=mock_persistence,
        )
        await lease_manager.load_active_leases()

        lease = lease_manager.active_leases["task-123"]
        assert len(lease.update_timestamps) == 3
        assert all(ts.tzinfo is not None for ts in lease.update_timestamps)

    @pytest.mark.asyncio
    async def test_load_active_leases_handles_missing_timestamps(self):
        """Test that missing update_timestamps defaults to empty list."""
        from src.core.assignment_persistence import AssignmentPersistence

        now = datetime.now(timezone.utc)
        mock_persistence = Mock(spec=AssignmentPersistence)
        mock_persistence.load_assignments = AsyncMock(
            return_value={
                "agent-001": {
                    "task_id": "task-456",
                    "assigned_at": now.isoformat(),
                    "lease_expires": (now + timedelta(minutes=2)).isoformat(),
                    "lease_renewed_at": now.isoformat(),
                    "renewal_count": 0,
                    "progress_percentage": 0,
                }
            }
        )

        lease_manager = AssignmentLeaseManager(
            kanban_client=Mock(),
            assignment_persistence=mock_persistence,
        )
        await lease_manager.load_active_leases()

        lease = lease_manager.active_leases["task-456"]
        assert lease.update_timestamps == []

    @pytest.mark.asyncio
    async def test_persist_lease_saves_update_timestamps(self):
        """_persist_lease hands update_timestamps to the persistence layer.

        This previously asserted that ``_persist_lease`` mutated the dict
        returned by ``get_assignment``. It did -- but ``save_assignment``
        then replaced the cached record wholesale, so every field written
        here was discarded before reaching disk. The test passed while
        the behaviour it described did not work. Assert against the call
        that actually persists instead.
        """
        from src.core.assignment_persistence import AssignmentPersistence

        now = datetime.now(timezone.utc)
        mock_persistence = Mock(spec=AssignmentPersistence)
        mock_persistence.get_assignment = AsyncMock(
            return_value={
                "task_id": "task-789",
                "assigned_at": now.isoformat(),
            }
        )
        mock_persistence.update_assignment_fields = AsyncMock()

        lease_manager = AssignmentLeaseManager(
            kanban_client=Mock(),
            assignment_persistence=mock_persistence,
        )

        lease = AssignmentLease(
            task_id="task-789",
            agent_id="agent-001",
            assigned_at=now,
            lease_expires=now + timedelta(minutes=2),
            last_renewed=now,
            update_timestamps=[
                now - timedelta(seconds=60),
                now,
            ],
        )

        await lease_manager._persist_lease(lease)

        mock_persistence.update_assignment_fields.assert_called_once()
        agent_id, task_id, fields = (
            mock_persistence.update_assignment_fields.call_args.args
        )
        assert agent_id == "agent-001"
        assert task_id == "task-789"
        assert len(fields["update_timestamps"]) == 2


class TestRecoveryInfo:
    """Test suite for RecoveryInfo dataclass."""

    def test_recovery_info_creation(self):
        """Test basic RecoveryInfo creation."""
        now = datetime.now(timezone.utc)
        recovery_info = RecoveryInfo(
            recovered_at=now,
            recovered_from_agent="agent-001",
            previous_progress=50,
            time_spent_minutes=120.5,
            recovery_reason="lease_expired",
            instructions="Check git history for previous work",
        )

        assert recovery_info.recovered_from_agent == "agent-001"
        assert recovery_info.previous_progress == 50
        assert recovery_info.time_spent_minutes == 120.5
        assert recovery_info.recovery_reason == "lease_expired"
        assert "git history" in recovery_info.instructions

    def test_recovery_info_to_dict(self):
        """Test RecoveryInfo serialization to dictionary."""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=24)

        recovery_info = RecoveryInfo(
            recovered_at=now,
            recovered_from_agent="agent-001",
            previous_progress=50,
            time_spent_minutes=120.5,
            recovery_reason="lease_expired",
            instructions="Check git history",
            recovery_expires_at=expires,
        )

        data = recovery_info.to_dict()

        assert data["recovered_from_agent"] == "agent-001"
        assert data["previous_progress"] == 50
        assert data["time_spent_minutes"] == 120.5
        assert data["recovery_reason"] == "lease_expired"
        assert data["instructions"] == "Check git history"
        assert data["recovery_expires_at"] == expires.isoformat()

    def test_recovery_info_from_dict(self):
        """Test RecoveryInfo deserialization from dictionary."""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=24)

        data = {
            "recovered_at": now.isoformat(),
            "recovered_from_agent": "agent-001",
            "previous_progress": 50,
            "time_spent_minutes": 120.5,
            "recovery_reason": "lease_expired",
            "instructions": "Check git history",
            "recovery_expires_at": expires.isoformat(),
        }

        recovery_info = RecoveryInfo.from_dict(data)

        assert recovery_info.recovered_from_agent == "agent-001"
        assert recovery_info.previous_progress == 50
        assert recovery_info.time_spent_minutes == 120.5
        assert recovery_info.recovery_reason == "lease_expired"
        assert recovery_info.recovery_expires_at is not None

    def test_recovery_info_optional_expiration(self):
        """Test RecoveryInfo with no expiration."""
        now = datetime.now(timezone.utc)
        recovery_info = RecoveryInfo(
            recovered_at=now,
            recovered_from_agent="agent-001",
            previous_progress=50,
            time_spent_minutes=120.5,
            recovery_reason="lease_expired",
            instructions="Check git history",
        )

        assert recovery_info.recovery_expires_at is None

        data = recovery_info.to_dict()
        assert data["recovery_expires_at"] is None


class TestRecoveryHandoffDualWrite:
    """Test suite for recovery handoff dual-write (task model + Kanban)."""

    @pytest.fixture
    def mock_kanban_client(self):
        """Create mock kanban client with comment support."""
        client = Mock()
        client.update_task_status = AsyncMock()
        client.add_comment = AsyncMock()
        return client

    @pytest.fixture
    def mock_persistence(self):
        """Create mock assignment persistence."""
        persistence = Mock()
        persistence.get_assignment = AsyncMock(return_value=None)
        persistence.save_assignment = AsyncMock()
        persistence.update_assignment_fields = AsyncMock()
        persistence.remove_assignment = AsyncMock()
        persistence.load_assignments = AsyncMock(return_value={})
        return persistence

    @pytest.fixture
    def lease_manager(self, mock_kanban_client, mock_persistence):
        """Create lease manager instance."""
        return AssignmentLeaseManager(
            mock_kanban_client, mock_persistence, default_lease_hours=4.0
        )

    @pytest.fixture
    def mock_task(self):
        """Create a mock task for testing."""
        task = Task(
            id="task-123",
            name="Test Task",
            description="Test",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
            estimated_hours=5.0,
            dependencies=[],
            labels=[],
            assigned_to="agent-001",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
        )
        return task

    @pytest.mark.asyncio
    async def test_recover_expired_lease_creates_recovery_info(
        self, lease_manager, mock_task
    ):
        """Test that recovery populates task.recovery_info."""
        # Arrange
        expired_lease = AssignmentLease(
            task_id="task-123",
            agent_id="agent-001",
            assigned_at=datetime.now(timezone.utc) - timedelta(hours=2),
            lease_expires=datetime.now(timezone.utc) - timedelta(hours=1),
            last_renewed=datetime.now(timezone.utc) - timedelta(hours=2),
            progress_percentage=30,
            last_progress_message="Working on it",
        )

        # Mock the internal _find_task method to return our mock task
        with patch.object(lease_manager, "_find_task", return_value=mock_task):
            # Act
            success = await lease_manager.recover_expired_lease(expired_lease)

            # Assert
            assert success
            assert mock_task.recovery_info is not None
            assert mock_task.recovery_info.recovered_from_agent == "agent-001"
            assert mock_task.recovery_info.previous_progress == 30
            assert mock_task.recovery_info.recovery_reason == "lease_expired"
            assert mock_task.recovery_info.time_spent_minutes > 0

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_recover_expired_lease_injects_progress_message(
        self, lease_manager, mock_task
    ):
        """Test that last_progress_message is quoted in recovery instructions.

        Verifies the production code path in recover_expired_lease() so a
        regression in the string assembly would be caught here — not only
        by the test_recovery_handoff.py tests which hardcode instructions.
        Addresses Codex P2 from PR #395 review.
        """
        note = "Built NOAA solar algorithm.\nApp.tsx wiring still needed."
        expired_lease = AssignmentLease(
            task_id="task-123",
            agent_id="agent-001",
            assigned_at=datetime.now(timezone.utc) - timedelta(hours=2),
            lease_expires=datetime.now(timezone.utc) - timedelta(hours=1),
            last_renewed=datetime.now(timezone.utc) - timedelta(hours=2),
            progress_percentage=50,
            last_progress_message=note,
        )

        with patch.object(lease_manager, "_find_task", return_value=mock_task):
            await lease_manager.recover_expired_lease(expired_lease)

        instructions = mock_task.recovery_info.instructions
        # Every line of the progress note must be blockquoted
        assert "> Built NOAA solar algorithm." in instructions
        assert "> App.tsx wiring still needed." in instructions
        assert "last progress note" in instructions

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_recover_expired_lease_warns_when_no_progress_message(
        self, lease_manager, mock_task
    ):
        """Test that empty last_progress_message produces the no-notes warning.

        When an agent dies before its first progress report, the recovery note
        must tell the recovering agent to inspect git diff rather than assuming
        nothing was committed. Addresses Codex P2 from PR #395 review.
        """
        expired_lease = AssignmentLease(
            task_id="task-123",
            agent_id="agent-001",
            assigned_at=datetime.now(timezone.utc) - timedelta(hours=2),
            lease_expires=datetime.now(timezone.utc) - timedelta(hours=1),
            last_renewed=datetime.now(timezone.utc) - timedelta(hours=2),
            progress_percentage=0,
            last_progress_message="",
        )

        with patch.object(lease_manager, "_find_task", return_value=mock_task):
            await lease_manager.recover_expired_lease(expired_lease)

        instructions = mock_task.recovery_info.instructions
        assert "no progress notes" in instructions
        assert "git diff main" in instructions
        assert "Do NOT re-implement" in instructions

    @pytest.mark.asyncio
    async def test_recover_expired_lease_dual_writes_to_kanban(
        self, lease_manager, mock_kanban_client, mock_task
    ):
        """Test that recovery writes BOTH to task model AND Kanban comment."""
        # Arrange
        expired_lease = AssignmentLease(
            task_id="task-123",
            agent_id="agent-001",
            assigned_at=datetime.now(timezone.utc) - timedelta(hours=2),
            lease_expires=datetime.now(timezone.utc) - timedelta(hours=1),
            last_renewed=datetime.now(timezone.utc) - timedelta(hours=2),
            progress_percentage=30,
        )

        # Mock the internal _find_task method
        with patch.object(lease_manager, "_find_task", return_value=mock_task):
            # Act
            success = await lease_manager.recover_expired_lease(expired_lease)

            # Assert dual-write
            assert success

            # 1. Task model should have recovery_info
            assert mock_task.recovery_info is not None

            # 2. Kanban comment should be posted
            mock_kanban_client.add_comment.assert_called_once()
            comment_call = mock_kanban_client.add_comment.call_args
            assert comment_call[0][0] == "task-123"  # task_id
            comment_text = comment_call[0][1]
            assert "TASK RECOVERED FROM AGENT agent-001" in comment_text
            assert "30%" in comment_text

    @pytest.mark.asyncio
    async def test_recovery_continues_if_kanban_comment_fails(
        self, lease_manager, mock_kanban_client, mock_task
    ):
        """Test that recovery succeeds even if Kanban comment fails."""
        # Arrange
        expired_lease = AssignmentLease(
            task_id="task-123",
            agent_id="agent-001",
            assigned_at=datetime.now(timezone.utc) - timedelta(hours=2),
            lease_expires=datetime.now(timezone.utc) - timedelta(hours=1),
            last_renewed=datetime.now(timezone.utc) - timedelta(hours=2),
            progress_percentage=30,
        )

        # Make Kanban comment fail
        mock_kanban_client.add_comment.side_effect = Exception("Kanban unavailable")

        with patch.object(lease_manager, "_find_task", return_value=mock_task):
            # Act - should not raise exception
            success = await lease_manager.recover_expired_lease(expired_lease)

            # Assert - recovery still succeeds
            assert success
            assert mock_task.recovery_info is not None

    @pytest.mark.asyncio
    async def test_recovery_info_includes_24hour_expiration(
        self, lease_manager, mock_task
    ):
        """Test that recovery info includes 24-hour expiration."""
        # Arrange
        now = datetime.now(timezone.utc)
        expired_lease = AssignmentLease(
            task_id="task-123",
            agent_id="agent-001",
            assigned_at=now - timedelta(hours=2),
            lease_expires=now - timedelta(hours=1),
            last_renewed=now - timedelta(hours=2),
            progress_percentage=30,
        )

        with patch.object(lease_manager, "_find_task", return_value=mock_task):
            # Act
            await lease_manager.recover_expired_lease(expired_lease)

            # Assert
            assert mock_task.recovery_info is not None
            assert mock_task.recovery_info.recovery_expires_at is not None

            # Should expire in approximately 24 hours
            expiry_delta = mock_task.recovery_info.recovery_expires_at - datetime.now(
                timezone.utc
            )
            assert 23 <= expiry_delta.total_seconds() / 3600 <= 25  # 23-25 hours

    @pytest.mark.asyncio
    async def test_recovery_info_includes_git_instructions(
        self, lease_manager, mock_task
    ):
        """Test that recovery info includes helpful git instructions."""
        # Arrange
        expired_lease = AssignmentLease(
            task_id="task-123",
            agent_id="agent-001",
            assigned_at=datetime.now(timezone.utc) - timedelta(hours=2),
            lease_expires=datetime.now(timezone.utc) - timedelta(hours=1),
            last_renewed=datetime.now(timezone.utc) - timedelta(hours=2),
            progress_percentage=30,
        )

        with patch.object(lease_manager, "_find_task", return_value=mock_task):
            # Act
            await lease_manager.recover_expired_lease(expired_lease)

            # Assert
            assert mock_task.recovery_info is not None
            instructions = mock_task.recovery_info.instructions

            # Should include key guidance for worktree recovery
            assert "agent-001" in instructions
            assert "marcus/agent-001" in instructions  # Branch name
            assert "git merge" in instructions  # Merge dead agent's branch
            assert "30%" in instructions


class TestExpiredLeaseProgressCapture:
    """
    Regression coverage for Issue #342: ``renew_lease`` must
    capture the agent's latest progress value even when the lease
    itself cannot be renewed because it's already expired.

    The dashboard-v70 Epictetus audit documented the exact bug:
    agent reported 25% then 50%, but the 50% report arrived after
    the lease silently expired. Without this capture, the renewal
    path returned None and dropped the 50% value on the floor.
    When the monitor later recovered the lease, the recovery
    context showed 25% (or 0% after a false-positive recovery
    recreated the lease), causing the recovering agent to rebuild
    from scratch — 341 lines of ghost source + 506 lines of ghost
    tests.

    The fix: on the ``lease.is_expired`` path in ``renew_lease``,
    still mutate ``lease.progress_percentage`` to reflect the
    agent's latest self-reported value before returning None.
    Guarded with ``>`` so the snapshot never regresses.
    """

    @pytest.fixture
    def mock_kanban_client(self):
        client = Mock()
        client.update_task_status = AsyncMock()
        client.update_task = AsyncMock()
        client.add_comment = AsyncMock()
        return client

    @pytest.fixture
    def mock_persistence(self):
        persistence = Mock()
        persistence.get_assignment = AsyncMock(return_value=None)
        persistence.save_assignment = AsyncMock()
        persistence.update_assignment_fields = AsyncMock()
        persistence.remove_assignment = AsyncMock()
        persistence.load_assignments = AsyncMock(return_value={})
        return persistence

    @pytest.fixture
    def lease_manager(self, mock_kanban_client, mock_persistence):
        return AssignmentLeaseManager(
            mock_kanban_client, mock_persistence, default_lease_hours=4.0
        )

    @pytest.fixture
    def real_task(self):
        """
        Use a REAL ``Task`` dataclass, not a Mock. This is the
        anti-trap from the first attempt at this fix: Mock()
        allowed inventing a ``progress`` attribute that the real
        Task class doesn't have, so the fix looked right in test
        but was a no-op in production. Real Task objects here
        force the fix to work on fields that actually exist.
        """
        return Task(
            id="task-342",
            name="Stale Progress Task",
            description="Test",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
            estimated_hours=0.1,
            dependencies=[],
            labels=[],
            assigned_to="agent-001",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
        )

    async def _insert_expired_lease(
        self,
        manager: AssignmentLeaseManager,
        task_id: str,
        agent_id: str,
        initial_progress: int,
    ) -> AssignmentLease:
        """
        Insert an expired lease directly into ``active_leases`` so
        ``renew_lease`` has a target to operate on. Bypasses
        ``create_lease`` to avoid tripping on side effects.
        """
        now = datetime.now(timezone.utc)
        expired = AssignmentLease(
            task_id=task_id,
            agent_id=agent_id,
            assigned_at=now - timedelta(hours=2),
            lease_expires=now - timedelta(hours=1),
            last_renewed=now - timedelta(hours=2),
            progress_percentage=initial_progress,
        )
        async with manager.lease_lock:
            manager.active_leases[task_id] = expired
        return expired

    @pytest.mark.asyncio
    async def test_expired_lease_renewal_captures_higher_progress(self, lease_manager):
        """
        The bug scenario: lease expired at 25%, agent reports 50%
        via renew_lease. Renewal fails (lease is expired), but the
        50% value must land in ``lease.progress_percentage`` so
        the later recovery carries it forward.
        """
        expired = await self._insert_expired_lease(
            lease_manager, "task-342", "agent-001", initial_progress=25
        )

        result = await lease_manager.renew_lease(
            task_id="task-342", progress=50, message="halfway"
        )

        # Renewal still fails — lease can't be un-expired
        assert result is None
        # But the snapshot was updated
        assert expired.progress_percentage == 50
        assert expired.last_progress_message == "halfway"

    @pytest.mark.asyncio
    async def test_expired_lease_renewal_does_not_regress_progress(self, lease_manager):
        """
        Defensive: if a late report arrives out of order with a
        lower progress value, we must not regress the snapshot.
        Only strictly-higher values replace the stored progress.
        """
        expired = await self._insert_expired_lease(
            lease_manager, "task-342", "agent-001", initial_progress=50
        )

        result = await lease_manager.renew_lease(
            task_id="task-342",
            progress=30,
            message="out of order",
        )

        assert result is None
        assert expired.progress_percentage == 50  # preserved
        # last_progress_message also preserved because we only
        # update it when progress actually advances
        assert expired.last_progress_message != "out of order"

    @pytest.mark.asyncio
    async def test_recovery_uses_captured_progress_after_expired_update(
        self, lease_manager, real_task
    ):
        """
        End-to-end: agent reports 50% via renew_lease on an
        expired lease, then the monitor recovers the lease. The
        recovery context must show 50%, not the original 25%.
        This closes the dashboard-v70 loop.
        """
        # 1. Lease is expired at 25%
        expired = await self._insert_expired_lease(
            lease_manager, "task-342", "agent-001", initial_progress=25
        )

        # 2. Agent reports late progress — renewal fails, but
        #    capture fires.
        await lease_manager.renew_lease(
            task_id="task-342", progress=50, message="halfway"
        )
        assert expired.progress_percentage == 50

        # 3. Monitor recovers the lease. Recovery context must
        #    reflect the captured value, not the stale snapshot.
        with patch.object(lease_manager, "_find_task", return_value=real_task):
            success = await lease_manager.recover_expired_lease(expired)

        assert success
        assert real_task.recovery_info is not None
        assert real_task.recovery_info.previous_progress == 50
        assert "50%" in real_task.recovery_info.instructions
        # Lease history also tracks the corrected value
        assert lease_manager.lease_history[-1]["progress_at_recovery"] == 50

    @pytest.mark.asyncio
    async def test_expired_lease_renewal_still_returns_none(self, lease_manager):
        """
        The capture must not accidentally un-expire the lease.
        ``renew_lease`` still returns None on expired leases so
        the caller's "No active lease" fallback path triggers
        correctly. This test pins that contract explicitly so a
        future refactor doesn't silently start returning the
        lease object from the expired branch.
        """
        expired = await self._insert_expired_lease(
            lease_manager, "task-342", "agent-001", initial_progress=0
        )

        result = await lease_manager.renew_lease(
            task_id="task-342", progress=80, message="almost done"
        )

        assert result is None
        assert expired.is_expired  # lease remains expired
        assert expired.progress_percentage == 80  # but progress captured


class TestMergeConflictExtension:
    """
    Lease grants a one-shot extension when the agent's worktree has
    unresolved git merge conflicts.

    Regression for dashboard-v73: agent_unicorn_2 was actively
    resolving merge conflicts when its lease silently expired. The
    work was discarded and recovery handed the task to a fresh agent
    that re-did everything (and the same merge conflicts) from
    scratch. The fix grants up to ``MAX_MERGE_CONFLICT_EXTENSIONS``
    extensions of ``MERGE_CONFLICT_EXTENSION_SECONDS`` each when the
    worktree's git porcelain status reports unmerged paths.

    Truth-grounded: the worktree git state IS the source of truth.
    No new agent API surface, no agent self-declaration, agent
    cannot lie or forget.
    """

    @pytest.fixture
    def mock_kanban_client(self, tmp_path):
        client = Mock()
        client.update_task_status = AsyncMock()
        client.update_task = AsyncMock()
        # Workspace state points at a fake project_root so the
        # worktree convention (../worktrees/<agent_id>) lands inside
        # tmp_path. Tests that need the worktree to "exist" populate
        # it manually via Path.mkdir.
        impl_dir = tmp_path / "implementation"
        impl_dir.mkdir()
        client._load_workspace_state = Mock(
            return_value={"project_root": str(impl_dir)}
        )
        return client

    @pytest.fixture
    def mock_persistence(self):
        persistence = Mock()
        persistence.save_assignment = AsyncMock()
        persistence.update_assignment_fields = AsyncMock()
        persistence.remove_assignment = AsyncMock()
        persistence.load_assignments = AsyncMock(return_value={})
        # Default to returning an assignment dict so _persist_lease's
        # "if assignment:" branch fires when extensions are granted.
        # Tests that need a different shape override this directly.
        persistence.get_assignment = AsyncMock(
            return_value={
                "task_id": "task-build",
                "assigned_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return persistence

    @pytest.fixture
    def lease_manager(self, mock_kanban_client, mock_persistence):
        return AssignmentLeaseManager(
            mock_kanban_client,
            mock_persistence,
            default_lease_hours=4.0,
            grace_period_minutes=0.001,  # ~0s grace so tests don't wait
        )

    def _make_expired_lease(
        self, task_id: str = "task-build", agent_id: str = "agent-001"
    ) -> AssignmentLease:
        """Build a lease that is comfortably past its grace period."""
        now = datetime.now(timezone.utc)
        return AssignmentLease(
            task_id=task_id,
            agent_id=agent_id,
            assigned_at=now - timedelta(hours=1),
            lease_expires=now - timedelta(minutes=10),
            last_renewed=now - timedelta(minutes=15),
            progress_percentage=75,
        )

    def _make_worktree_with_conflict(self, kanban_client, agent_id: str) -> None:
        """Create the worktree dir on disk so _resolve_worktree_path finds it."""
        impl_dir = Path(kanban_client._load_workspace_state()["project_root"])
        worktree = impl_dir.parent / "worktrees" / agent_id
        worktree.mkdir(parents=True, exist_ok=True)

    @pytest.mark.asyncio
    async def test_extension_granted_when_worktree_has_unmerged_paths(
        self, lease_manager, mock_kanban_client
    ):
        """Worktree with unmerged paths → lease extended, not recovered."""
        lease = self._make_expired_lease()
        lease_manager.active_leases[lease.task_id] = lease
        self._make_worktree_with_conflict(mock_kanban_client, lease.agent_id)
        original_expiry = lease.lease_expires

        with patch.object(
            lease_manager,
            "_has_unresolved_conflicts",
            new_callable=AsyncMock,
            return_value=True,
        ):
            expired = await lease_manager.check_expired_leases()

        assert expired == []  # not in the recovery list
        assert lease.merge_conflict_extensions == 1
        assert lease.lease_expires > original_expiry
        # Lease is now in the future (extension applied)
        assert not lease.is_expired

    @pytest.mark.asyncio
    async def test_extension_not_granted_without_worktree(
        self, lease_manager, mock_kanban_client
    ):
        """No worktree on disk → no extension, lease recovered normally."""
        lease = self._make_expired_lease()
        lease_manager.active_leases[lease.task_id] = lease
        # Note: NOT creating the worktree dir

        expired = await lease_manager.check_expired_leases()

        assert lease in expired
        assert lease.merge_conflict_extensions == 0

    @pytest.mark.asyncio
    async def test_extension_not_granted_when_no_conflicts(
        self, lease_manager, mock_kanban_client
    ):
        """Worktree exists but no conflicts → no extension."""
        lease = self._make_expired_lease()
        lease_manager.active_leases[lease.task_id] = lease
        self._make_worktree_with_conflict(mock_kanban_client, lease.agent_id)

        with patch.object(
            lease_manager,
            "_has_unresolved_conflicts",
            new_callable=AsyncMock,
            return_value=False,
        ):
            expired = await lease_manager.check_expired_leases()

        assert lease in expired
        assert lease.merge_conflict_extensions == 0

    @pytest.mark.asyncio
    async def test_extension_capped_at_max(self, lease_manager, mock_kanban_client):
        """After MAX_MERGE_CONFLICT_EXTENSIONS, no further extensions."""
        from src.core.assignment_lease import MAX_MERGE_CONFLICT_EXTENSIONS

        lease = self._make_expired_lease()
        lease.merge_conflict_extensions = MAX_MERGE_CONFLICT_EXTENSIONS
        lease_manager.active_leases[lease.task_id] = lease
        self._make_worktree_with_conflict(mock_kanban_client, lease.agent_id)

        # Even with conflicts present, the cap stops further extensions
        with patch.object(
            lease_manager,
            "_has_unresolved_conflicts",
            new_callable=AsyncMock,
            return_value=True,
        ):
            expired = await lease_manager.check_expired_leases()

        assert lease in expired
        assert lease.merge_conflict_extensions == MAX_MERGE_CONFLICT_EXTENSIONS

    @pytest.mark.asyncio
    async def test_extension_increments_counter_each_grant(
        self, lease_manager, mock_kanban_client
    ):
        """Each successful extension increments the counter."""
        lease = self._make_expired_lease()
        lease_manager.active_leases[lease.task_id] = lease
        self._make_worktree_with_conflict(mock_kanban_client, lease.agent_id)

        with patch.object(
            lease_manager,
            "_has_unresolved_conflicts",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await lease_manager.check_expired_leases()
            assert lease.merge_conflict_extensions == 1

            # Force another expiry by rewinding lease_expires
            lease.lease_expires = datetime.now(timezone.utc) - timedelta(minutes=10)
            await lease_manager.check_expired_leases()
            assert lease.merge_conflict_extensions == 2

    @pytest.mark.asyncio
    async def test_extension_logged_to_lease_history(
        self, lease_manager, mock_kanban_client
    ):
        """Each extension appends a merge_conflict_extension event."""
        lease = self._make_expired_lease()
        lease_manager.active_leases[lease.task_id] = lease
        self._make_worktree_with_conflict(mock_kanban_client, lease.agent_id)

        with patch.object(
            lease_manager,
            "_has_unresolved_conflicts",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await lease_manager.check_expired_leases()

        events = [
            e
            for e in lease_manager.lease_history
            if e.get("event") == "merge_conflict_extension"
        ]
        assert len(events) == 1
        assert events[0]["task_id"] == lease.task_id
        assert events[0]["agent_id"] == lease.agent_id
        assert events[0]["extension_count"] == 1

    @pytest.mark.asyncio
    async def test_resolve_worktree_path_returns_none_without_workspace_state(
        self, lease_manager, mock_kanban_client
    ):
        """No workspace state → _resolve_worktree_path returns None."""
        mock_kanban_client._load_workspace_state = Mock(return_value=None)
        lease = self._make_expired_lease()
        result = lease_manager._resolve_worktree_path(lease)
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_worktree_path_returns_none_when_dir_missing(
        self, lease_manager, mock_kanban_client
    ):
        """Workspace state present but worktree dir doesn't exist → None."""
        lease = self._make_expired_lease()
        # Don't create the worktree dir
        result = lease_manager._resolve_worktree_path(lease)
        assert result is None

    @pytest.mark.asyncio
    async def test_has_unresolved_conflicts_real_git_clean_worktree(
        self, lease_manager, tmp_path
    ):
        """Real git invocation on a clean worktree returns False."""
        worktree = tmp_path / "clean"
        worktree.mkdir()
        # Initialize a real git repo
        proc = await asyncio.create_subprocess_exec(
            "git",
            "init",
            "-q",
            "-b",
            "main",
            str(worktree),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()

        result = await lease_manager._has_unresolved_conflicts(worktree)
        assert result is False

    @pytest.mark.asyncio
    async def test_has_unresolved_conflicts_real_git_with_conflict(
        self, lease_manager, tmp_path
    ):
        """
        Real git invocation on a worktree with an unmerged path returns True.

        Sets up a minimal git repo with an actual merge conflict by
        creating two divergent branches that touch the same line and
        then attempting (and failing) the merge. The resulting
        porcelain status has a UU line that the helper must detect.
        """
        worktree = tmp_path / "conflicted"
        worktree.mkdir()

        async def run(*args):
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(worktree),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode

        await run("init", "-q", "-b", "main")
        await run("config", "user.email", "test@test")
        await run("config", "user.name", "Test")
        (worktree / "f.txt").write_text("base\n")
        await run("add", "f.txt")
        await run("commit", "-q", "-m", "base")
        await run("checkout", "-q", "-b", "branch-a")
        (worktree / "f.txt").write_text("a\n")
        await run("commit", "-aq", "-m", "a")
        await run("checkout", "-q", "main")
        (worktree / "f.txt").write_text("b\n")
        await run("commit", "-aq", "-m", "b")
        # This merge will fail with a conflict, leaving UU in status
        await run("merge", "branch-a", "--no-edit")

        result = await lease_manager._has_unresolved_conflicts(worktree)
        assert result is True

    @pytest.mark.asyncio
    async def test_has_unresolved_conflicts_returns_false_on_non_git_dir(
        self, lease_manager, tmp_path
    ):
        """Non-git directory → defensive False, no exception."""
        worktree = tmp_path / "not_git"
        worktree.mkdir()
        result = await lease_manager._has_unresolved_conflicts(worktree)
        assert result is False

    @pytest.mark.asyncio
    async def test_extension_persists_lease_for_restart_safety(
        self, lease_manager, mock_kanban_client, mock_persistence
    ):
        """
        Codex P1 on PR #350: extension must call _persist_lease so a
        service restart during the 5-min extension window doesn't
        reload the old expiry and immediately recover the lease.
        """
        # Persistence has an existing assignment for this agent so
        # _persist_lease's "if assignment:" branch fires.
        mock_persistence.get_assignment = AsyncMock(
            return_value={
                "task_id": "task-build",
                "assigned_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        lease = self._make_expired_lease()
        lease_manager.active_leases[lease.task_id] = lease
        self._make_worktree_with_conflict(mock_kanban_client, lease.agent_id)

        with patch.object(
            lease_manager,
            "_has_unresolved_conflicts",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await lease_manager.check_expired_leases()

        # _persist_lease writes via update_assignment_fields, which merges
        # onto the record instead of replacing it. The previous
        # save_assignment path discarded merge_conflict_extensions before
        # it reached disk, so the restart-safety guarantee this test
        # describes did not actually hold.
        mock_persistence.update_assignment_fields.assert_called()
        persisted_counters = [
            call.args[2].get("merge_conflict_extensions")
            for call in mock_persistence.update_assignment_fields.call_args_list
        ]
        assert (
            1 in persisted_counters
        ), f"extension counter never persisted; saw {persisted_counters}"
        # The persisted fields must also carry the expiry, so a restart
        # rehydrates both the cap counter and the extended lease window.
        #
        # This previously asserted that the dict returned by
        # ``get_assignment`` had been mutated in place. That dict never
        # reached disk -- ``save_assignment`` replaced the cached record
        # wholesale -- so the assertion described a guarantee the code
        # did not provide.
        persisted_fields = [
            call.args[2]
            for call in mock_persistence.update_assignment_fields.call_args_list
        ]
        assert any("lease_expires" in fields for fields in persisted_fields)

    @pytest.mark.asyncio
    async def test_extension_does_not_hold_lock_during_git_probe(
        self, lease_manager, mock_kanban_client
    ):
        """
        Codex P2 on PR #350: git subprocess must not run while the
        lease lock is held, otherwise concurrent renew_lease calls
        starve.

        The test forces _has_unresolved_conflicts to acquire the
        lease_lock itself. If check_expired_leases were holding the
        lock during the probe, this would deadlock. Under the fix
        (probe outside lock, brief reacquire for mutation), the
        probe sees the lock as available and the test completes.
        """
        lease = self._make_expired_lease()
        lease_manager.active_leases[lease.task_id] = lease
        self._make_worktree_with_conflict(mock_kanban_client, lease.agent_id)

        async def probe_acquires_lock(_worktree):
            # If check_expired_leases is holding lease_lock, this hangs
            async with lease_manager.lease_lock:
                return True

        with patch.object(
            lease_manager,
            "_has_unresolved_conflicts",
            side_effect=probe_acquires_lock,
        ):
            # Bound the test so a regression manifests as a timeout
            # rather than an indefinite hang
            result = await asyncio.wait_for(
                lease_manager.check_expired_leases(), timeout=5.0
            )

        assert result == []  # extension was granted
        assert lease.merge_conflict_extensions == 1

    @pytest.mark.asyncio
    async def test_has_unresolved_conflicts_times_out_on_wedged_git(
        self, lease_manager, tmp_path, monkeypatch
    ):
        """
        Subprocess timeout (human review on PR #350): a wedged git
        process must not stall the lease loop indefinitely. The
        helper bounds git status with MERGE_CONFLICT_GIT_TIMEOUT_SECONDS
        and returns False on timeout.
        """
        from src.core import assignment_lease as al_mod

        # Drop the timeout to a sub-second value for the test
        monkeypatch.setattr(al_mod, "MERGE_CONFLICT_GIT_TIMEOUT_SECONDS", 0.1)

        worktree = tmp_path / "wedge"
        worktree.mkdir()

        # Patch create_subprocess_exec to return a process that
        # never completes communicate() — simulates a wedged git.
        class _HangingProcess:
            def __init__(self):
                self.returncode = None

            async def communicate(self):
                await asyncio.sleep(60)  # > timeout
                return b"", b""

            def kill(self):
                pass

            async def wait(self):
                return 0

        async def fake_create(*args, **kwargs):
            return _HangingProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

        # Should return False quickly, not hang
        result = await asyncio.wait_for(
            lease_manager._has_unresolved_conflicts(worktree), timeout=2.0
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_load_active_leases_restores_merge_conflict_extensions(
        self, lease_manager, mock_persistence
    ):
        """Codex P1 on PR #350: cap counter survives restart via persistence."""
        # Persistence returns an assignment with the new field set
        mock_persistence.load_assignments = AsyncMock(
            return_value={
                "agent-001": {
                    "task_id": "task-build",
                    "assigned_at": datetime.now(timezone.utc).isoformat(),
                    "lease_expires": datetime.now(timezone.utc).isoformat(),
                    "lease_renewed_at": datetime.now(timezone.utc).isoformat(),
                    "merge_conflict_extensions": 2,
                }
            }
        )

        await lease_manager.load_active_leases()
        loaded = lease_manager.active_leases["task-build"]
        assert loaded.merge_conflict_extensions == 2

    @pytest.mark.asyncio
    async def test_load_active_leases_defaults_extensions_to_zero_for_legacy(
        self, lease_manager, mock_persistence
    ):
        """Older persistence rows without the field must default to 0."""
        mock_persistence.load_assignments = AsyncMock(
            return_value={
                "agent-001": {
                    "task_id": "task-build",
                    "assigned_at": datetime.now(timezone.utc).isoformat(),
                    "lease_expires": datetime.now(timezone.utc).isoformat(),
                    "lease_renewed_at": datetime.now(timezone.utc).isoformat(),
                    # NOTE: no merge_conflict_extensions key
                }
            }
        )

        await lease_manager.load_active_leases()
        loaded = lease_manager.active_leases["task-build"]
        assert loaded.merge_conflict_extensions == 0

    @pytest.mark.asyncio
    async def test_extension_skipped_when_lease_renewed_during_git_probe(
        self, lease_manager, mock_kanban_client
    ):
        """P1 fix (PR #384): renew_lease wins race during git probe → no extension.

        If renew_lease advances the lease while _has_unresolved_conflicts is
        running, the lease is no longer expired when Phase 3 runs. The
        under-lock is_expired re-check must skip the extension so we don't
        burn a conflict-extension slot on an active lease.
        """
        lease = self._make_expired_lease()
        lease_manager.active_leases[lease.task_id] = lease
        self._make_worktree_with_conflict(mock_kanban_client, lease.agent_id)

        async def probe_then_renew(_worktree):
            # Simulate renew_lease winning while git probe runs.
            lease.lease_expires = datetime.now(timezone.utc) + timedelta(hours=1)
            return True  # conflicts still present, but lease already renewed

        with patch.object(
            lease_manager,
            "_has_unresolved_conflicts",
            new=probe_then_renew,
        ):
            expired = await lease_manager.check_expired_leases()

        # Lease is active again — should not be in expired list
        assert expired == []
        # Extension slot must NOT have been consumed
        assert lease.merge_conflict_extensions == 0

    @pytest.mark.asyncio
    async def test_extension_expiry_uses_grant_time_not_scan_time(
        self, lease_manager, mock_kanban_client
    ):
        """P2 fix (PR #384): new expiry is based on grant time, not scan start.

        check_expired_leases used to compute ``now`` once and pass it to every
        candidate's extension. If prior candidates' git probes took time, later
        candidates received a shorter-than-intended extension. The fix captures
        a fresh timestamp inside the lock at grant time.
        """
        lease = self._make_expired_lease()
        lease_manager.active_leases[lease.task_id] = lease
        self._make_worktree_with_conflict(mock_kanban_client, lease.agent_id)

        before_grant = datetime.now(timezone.utc)

        with patch.object(
            lease_manager,
            "_has_unresolved_conflicts",
            new_callable=AsyncMock,
            return_value=True,
        ):
            expired = await lease_manager.check_expired_leases()

        after_grant = datetime.now(timezone.utc)

        assert expired == []
        assert lease.merge_conflict_extensions == 1

        from src.core.assignment_lease import MERGE_CONFLICT_EXTENSION_SECONDS

        # New expiry must be at least (before_grant + extension) and no more
        # than (after_grant + extension) — i.e., based on a timestamp taken
        # during the grant, not before the git probe.
        expected_min = before_grant + timedelta(
            seconds=MERGE_CONFLICT_EXTENSION_SECONDS
        )
        expected_max = after_grant + timedelta(seconds=MERGE_CONFLICT_EXTENSION_SECONDS)
        assert expected_min <= lease.lease_expires <= expected_max


class TestSilenceMultiplierDefault:
    """Tests for the silence_multiplier default preventing aggressive lease thrashing."""

    def test_silence_multiplier_default_is_5x(self) -> None:
        """
        Verify silence_multiplier defaults to 5.0 (not 1.5).

        Dashboard-v98 post-mortem: the 1.5x default caused 8 stale
        completions when a 371s task was compared against a 66s median.
        5.0x gives ~330s headroom — enough for the slowest observed LLM
        tool-call chains (~4-6 min for code synthesis tasks).
        """
        manager = AssignmentLeaseManager(
            kanban_client=Mock(),
            assignment_persistence=Mock(),
        )
        assert manager.silence_multiplier == 5.0

    def test_silence_multiplier_custom_value_is_respected(self) -> None:
        """Custom silence_multiplier overrides the default."""
        manager = AssignmentLeaseManager(
            kanban_client=Mock(),
            assignment_persistence=Mock(),
            silence_multiplier=3.0,
        )
        assert manager.silence_multiplier == 3.0

    def test_silence_threshold_uses_5x_multiplier(self) -> None:
        """
        Recovery threshold = median_interval * silence_multiplier.

        With median=66s and multiplier=5.0, threshold is 330s — giving
        LLM-heavy tasks room to finish before lease recovery fires.
        """
        manager = AssignmentLeaseManager(
            kanban_client=Mock(),
            assignment_persistence=Mock(),
        )
        # Simulate a median of 66s (fast tasks set the baseline)
        median_seconds = 66.0
        threshold = median_seconds * manager.silence_multiplier
        assert threshold == pytest.approx(330.0)
        # 330s >> 99s (old 1.5x threshold) — no more thrashing on 371s tasks


class TestPhase4Timeout:
    """Phase 4 lease timeout must be 450s total to cover validation retries.

    When an agent reports >=75% progress and calls complete_task, the
    validator runs up to MAX_VALIDATION_RETRIES times. Each LLM call can
    take 60-120s. Phase 4 must give 450s total (420s lease + 30s grace)
    so touch_lease-extended validation has headroom without triggering
    premature lease recovery.
    """

    def _make_manager(self) -> AssignmentLeaseManager:
        """Construct a minimal lease manager."""
        return AssignmentLeaseManager(
            kanban_client=Mock(),
            assignment_persistence=Mock(),
            default_lease_hours=0.0667,  # aggressive mode
        )

    def test_phase4_lease_seconds_is_360(self) -> None:
        """Phase 4 (progress >=75%) must grant 360s lease duration."""
        manager = self._make_manager()
        lease_seconds, _ = manager.calculate_adaptive_timeout(
            progress=100, update_count=5, has_recent_activity=True
        )
        assert (
            lease_seconds == 360
        ), f"Phase 4 lease is {lease_seconds}s — expected 360s (450s total)"

    def test_phase4_grace_seconds_is_90(self) -> None:
        """Phase 4 grace period is 90s to cover tail-phase activities."""
        manager = self._make_manager()
        _, grace_seconds = manager.calculate_adaptive_timeout(
            progress=100, update_count=5, has_recent_activity=True
        )
        assert grace_seconds == 90

    def test_phase4_total_window_is_450(self) -> None:
        """Phase 4 total window (lease + grace) must be exactly 450s."""
        manager = self._make_manager()
        lease_s, grace_s = manager.calculate_adaptive_timeout(
            progress=76, update_count=3, has_recent_activity=True
        )
        assert lease_s + grace_s == 450

    def test_phase4_boundary_at_75_percent(self) -> None:
        """progress=75 must trigger Phase 4, not Phase 3."""
        manager = self._make_manager()
        lease_s, grace_s = manager.calculate_adaptive_timeout(
            progress=75, update_count=3, has_recent_activity=True
        )
        assert lease_s + grace_s == 450

    def test_phase3_below_75_is_360(self) -> None:
        """progress=74 stays in Phase 3 (360s total), not Phase 4."""
        manager = self._make_manager()
        lease_s, grace_s = manager.calculate_adaptive_timeout(
            progress=74, update_count=3, has_recent_activity=True
        )
        assert lease_s + grace_s == 360


class TestExtendForValidation:
    """Tests for Fix 1 of issue #667: validation-window extension on
    ``status=completed``.

    Today ``report_task_progress`` skips lease renewal when the agent
    reports completion (``src/marcus_mcp/tools/task.py:4480-4488``).
    The smoke gate and any rejection-triggered remediation then run
    under an expiring lease. ``extend_for_validation`` is the new
    entry point that grants a configurable window (default 600s) at
    completion time so the post-completion validation/remediation
    cycle has lease coverage.

    The window covers smoke-gate runtime + one round of remediation.
    Subsequent ``touch_lease`` activity from the agent during
    remediation continues to extend from the validation anchor.
    """

    @pytest.fixture
    def mock_kanban_client(self) -> Mock:
        """Create mock kanban client for lease persistence."""
        client = Mock()
        client.update_task = AsyncMock()
        return client

    @pytest.fixture
    def mock_persistence(self) -> Mock:
        """Create mock persistence layer that no-ops on save."""
        persistence = Mock()
        persistence.get_assignment = AsyncMock(
            return_value={
                "task_id": "task-validation",
                "assigned_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        persistence.save_assignment = AsyncMock()
        persistence.update_assignment_fields = AsyncMock()
        persistence.remove_assignment = AsyncMock()
        persistence.load_assignments = AsyncMock(return_value={})
        return persistence

    @pytest.fixture
    def lease_manager(
        self, mock_kanban_client: Mock, mock_persistence: Mock
    ) -> AssignmentLeaseManager:
        """Construct lease manager in aggressive mode (matches prod default)."""
        return AssignmentLeaseManager(
            mock_kanban_client,
            mock_persistence,
            default_lease_hours=0.0667,
        )

    @pytest.mark.asyncio
    async def test_extend_for_validation_extends_by_default_window(
        self, lease_manager: AssignmentLeaseManager
    ) -> None:
        """``extend_for_validation`` adds 600s to the existing lease.

        Default validation window is 600 seconds (10 minutes). The
        method must move ``lease_expires`` forward to ``now + 600s``
        regardless of where the prior expiry sat — completion is the
        anchor moment, not a continuation of the progressive curve.
        """
        await lease_manager.create_lease("task-validation", "agent-001")

        before = datetime.now(timezone.utc)
        result = await lease_manager.extend_for_validation("task-validation")
        after = datetime.now(timezone.utc)

        assert result is not None
        # Validation window extension lands at now + 600s (default).
        # Bound check accommodates the time elapsed during the call.
        min_expected = before + timedelta(seconds=600)
        max_expected = after + timedelta(seconds=601)
        assert min_expected <= result.lease_expires <= max_expected

    @pytest.mark.asyncio
    async def test_extend_for_validation_honors_custom_window(
        self, lease_manager: AssignmentLeaseManager
    ) -> None:
        """Caller-provided window override is respected.

        Allows callers to configure shorter or longer validation
        windows per task type without changing the default.
        """
        await lease_manager.create_lease("task-validation", "agent-001")

        before = datetime.now(timezone.utc)
        result = await lease_manager.extend_for_validation(
            "task-validation", window_seconds=300
        )
        after = datetime.now(timezone.utc)

        assert result is not None
        min_expected = before + timedelta(seconds=300)
        max_expected = after + timedelta(seconds=301)
        assert min_expected <= result.lease_expires <= max_expected

    @pytest.mark.asyncio
    async def test_extend_for_validation_does_not_regress_progress(
        self, lease_manager: AssignmentLeaseManager
    ) -> None:
        """Progress percentage stays at 100 after the validation extension.

        The agent has reported completion. Pushing ``progress_percentage``
        back to 0 (or to the renewal curve) would re-enter Phase 1's
        short window on the next ``touch_lease`` call and lose the
        validation coverage we just granted.
        """
        lease = await lease_manager.create_lease("task-validation", "agent-001")
        lease.progress_percentage = 100

        result = await lease_manager.extend_for_validation("task-validation")

        assert result is not None
        assert result.progress_percentage == 100

    @pytest.mark.asyncio
    async def test_extend_for_validation_returns_none_when_no_lease(
        self, lease_manager: AssignmentLeaseManager
    ) -> None:
        """Missing lease returns None, doesn't raise.

        The caller (``report_task_progress``) may invoke this for a
        task whose lease was already cleared (e.g. completion arrives
        for a task that finished moments ago). Defensive None preserves
        the caller's error-handling pattern matching ``renew_lease``.
        """
        result = await lease_manager.extend_for_validation("task-nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_extend_for_validation_returns_none_when_expired(
        self, lease_manager: AssignmentLeaseManager
    ) -> None:
        """Expired lease is not retroactively resurrected.

        If the lease already expired before completion arrived, the
        validation extension is too late to help — the late completion
        will be handled by Fix 2's transactional accept path. Mirrors
        the behavior of ``renew_lease`` on expired leases.
        """
        lease = await lease_manager.create_lease("task-validation", "agent-001")
        # Force expiry by moving lease_expires into the past
        lease.lease_expires = datetime.now(timezone.utc) - timedelta(seconds=60)

        result = await lease_manager.extend_for_validation("task-validation")
        assert result is None

    @pytest.mark.asyncio
    async def test_extend_for_validation_is_idempotent(
        self, lease_manager: AssignmentLeaseManager
    ) -> None:
        """Calling extend_for_validation twice doesn't stack windows.

        An agent may report ``status=completed`` more than once
        (e.g. retry after a transient failure). Each call should land
        on ``now + window_seconds``, not extend by ``window_seconds``
        on top of the prior extension. Otherwise a chatty agent could
        accumulate hours of lease.
        """
        await lease_manager.create_lease("task-validation", "agent-001")

        first = await lease_manager.extend_for_validation(
            "task-validation", window_seconds=600
        )
        # Tiny delay so the second call's anchor moves forward
        await asyncio.sleep(0.01)
        second = await lease_manager.extend_for_validation(
            "task-validation", window_seconds=600
        )

        assert first is not None and second is not None
        # The two expiries should differ by approximately the sleep,
        # not by 600 seconds. Generous bound to avoid flakes.
        delta = (second.lease_expires - first.lease_expires).total_seconds()
        assert 0 <= delta < 5


class TestSilentRecoveryCircuitBreaker:
    """Test suite for the silent-recovery circuit breaker (issue #667 Fix 3a).

    A "silent" recovery is a lease recovery where the agent never
    reported progress (``renewal_count == 0`` — the ``updates=0``
    signature from the snake_game runaway incident). After N consecutive
    silent recoveries of the same task, Marcus must mark the task
    BLOCKED instead of returning it to TODO, so the respawn/reclaim loop
    physically cannot continue and the failure becomes visible on the
    board.
    """

    @pytest.fixture
    def mock_kanban_client(self):
        """Create mock kanban client."""
        client = Mock()
        client.update_task = AsyncMock()
        client.add_comment = AsyncMock()
        client.move_task_to_column = AsyncMock(return_value=True)
        return client

    @pytest.fixture
    def mock_persistence(self):
        """Create mock assignment persistence."""
        persistence = Mock()
        persistence.get_assignment = AsyncMock(return_value=None)
        persistence.save_assignment = AsyncMock()
        persistence.update_assignment_fields = AsyncMock()
        persistence.remove_assignment = AsyncMock()
        persistence.load_assignments = AsyncMock(return_value={})
        return persistence

    @pytest.fixture
    def task(self):
        """Create the task under recovery, registered in task_list."""
        return Task(
            id="task-667",
            name="Integration verification",
            description="Terminal integration task",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
            estimated_hours=1.0,
            dependencies=[],
            labels=["type:integration"],
            assigned_to="agent-001",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
        )

    @pytest.fixture
    def lease_manager(self, mock_kanban_client, mock_persistence, task):
        """Create lease manager with the default breaker threshold (3)."""
        return AssignmentLeaseManager(
            mock_kanban_client,
            mock_persistence,
            default_lease_hours=1.0,
            task_list=[task],
        )

    def _expired_lease(self, agent_id: str, renewal_count: int = 0):
        """Build an already-expired lease for task-667.

        ``renewal_count=0`` models a SILENT failure (agent never
        reported progress); ``renewal_count>0`` models an agent that
        made progress but still expired.
        """
        now = datetime.now(timezone.utc)
        return AssignmentLease(
            task_id="task-667",
            agent_id=agent_id,
            assigned_at=now - timedelta(minutes=10),
            lease_expires=now - timedelta(minutes=1),
            last_renewed=now - timedelta(minutes=10),
            renewal_count=renewal_count,
        )

    @pytest.mark.asyncio
    async def test_silent_recovery_below_threshold_resets_to_todo(
        self, lease_manager, mock_kanban_client
    ):
        """First and second silent recoveries keep today's TODO reset."""
        # Arrange / Act — two silent recoveries
        await lease_manager.recover_expired_lease(self._expired_lease("agent-1"))
        await lease_manager.recover_expired_lease(self._expired_lease("agent-2"))

        # Assert — both board updates were TODO resets, never BLOCKED
        statuses = [
            call.args[1]["status"]
            for call in mock_kanban_client.update_task.await_args_list
        ]
        assert statuses == [TaskStatus.TODO, TaskStatus.TODO]

    @pytest.mark.asyncio
    async def test_third_consecutive_silent_recovery_marks_task_blocked(
        self, lease_manager, mock_kanban_client
    ):
        """The third consecutive silent recovery trips the breaker to BLOCKED."""
        # Arrange / Act — three consecutive silent recoveries
        for i in range(3):
            await lease_manager.recover_expired_lease(self._expired_lease(f"agent-{i}"))

        # Assert — final board update is BLOCKED with ownership cleared
        final_update = mock_kanban_client.update_task.await_args_list[-1]
        assert final_update.args[1]["status"] == TaskStatus.BLOCKED
        assert final_update.args[1]["assigned_to"] is None

    @pytest.mark.asyncio
    async def test_progressful_recovery_resets_silent_streak(
        self, lease_manager, mock_kanban_client
    ):
        """A recovery WITH progress breaks the silent streak (not the pattern).

        silent, silent, progressful, silent, silent → never reaches 3
        consecutive silent recoveries, so the task stays recoverable.
        A slow-but-alive agent must not trip the breaker (the #703
        lesson applied at the board level).
        """
        await lease_manager.recover_expired_lease(self._expired_lease("a1"))
        await lease_manager.recover_expired_lease(self._expired_lease("a2"))
        await lease_manager.recover_expired_lease(
            self._expired_lease("a3", renewal_count=2)
        )
        await lease_manager.recover_expired_lease(self._expired_lease("a4"))
        await lease_manager.recover_expired_lease(self._expired_lease("a5"))

        statuses = [
            call.args[1]["status"]
            for call in mock_kanban_client.update_task.await_args_list
        ]
        assert TaskStatus.BLOCKED not in statuses

    @pytest.mark.asyncio
    async def test_blocked_path_posts_explanatory_comment(
        self, lease_manager, mock_kanban_client
    ):
        """The BLOCKED transition posts a human-readable diagnostic comment."""
        for i in range(3):
            await lease_manager.recover_expired_lease(self._expired_lease(f"agent-{i}"))

        comment_bodies = [
            call.args[1] for call in mock_kanban_client.add_comment.await_args_list
        ]
        breaker_comments = [
            body
            for body in comment_bodies
            if "3 consecutive" in body and "no progress" in body
        ]
        assert len(breaker_comments) == 1

    @pytest.mark.asyncio
    async def test_blocked_path_still_cleans_coordination_state(
        self, lease_manager, mock_persistence
    ):
        """Tripping the breaker still releases lease + assignment + callback.

        The BLOCKED path must perform the same coordination cleanup as a
        normal recovery, or the server would leak in-memory agent state
        (the exact class of bug behind issue #485).
        """
        callback_calls = []
        lease_manager.on_recovery_callback = lambda agent_id, task_id: (
            callback_calls.append((agent_id, task_id))
        )

        for i in range(3):
            lease = self._expired_lease(f"agent-{i}")
            lease_manager.active_leases[lease.task_id] = lease
            await lease_manager.recover_expired_lease(lease)

        assert "task-667" not in lease_manager.active_leases
        assert mock_persistence.remove_assignment.await_count == 3
        assert ("agent-2", "task-667") in callback_calls

    @pytest.mark.asyncio
    async def test_threshold_is_configurable(
        self, mock_kanban_client, mock_persistence, task
    ):
        """max_silent_recoveries=1 blocks on the very first silent recovery."""
        manager = AssignmentLeaseManager(
            mock_kanban_client,
            mock_persistence,
            default_lease_hours=1.0,
            task_list=[task],
            max_silent_recoveries=1,
        )

        await manager.recover_expired_lease(self._expired_lease("agent-1"))

        final_update = mock_kanban_client.update_task.await_args_list[-1]
        assert final_update.args[1]["status"] == TaskStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_task_model_marked_blocked_in_task_list(self, lease_manager, task):
        """The in-memory task model (source of truth) flips to BLOCKED too."""
        for i in range(3):
            await lease_manager.recover_expired_lease(self._expired_lease(f"agent-{i}"))

        assert task.status == TaskStatus.BLOCKED
        assert task.assigned_to is None

    @pytest.mark.asyncio
    async def test_breaker_event_recorded_in_lease_history(self, lease_manager):
        """The breaker records an auditable lease-history event."""
        for i in range(3):
            await lease_manager.recover_expired_lease(self._expired_lease(f"agent-{i}"))

        events = [entry["event"] for entry in lease_manager.lease_history]
        assert "task_blocked_circuit_breaker" in events

    @pytest.mark.asyncio
    async def test_blocked_path_moves_task_to_blocked_column(
        self, lease_manager, mock_kanban_client
    ):
        """The breaker also calls move_task_to_column("blocked").

        Codex P2 on PR #707: GitHub- and Linear-backed boards do NOT
        persist status through ``update_task`` (GitHub only opens/closes
        the issue; Linear ignores status entirely) — their blocked-state
        mechanics live in ``move_task_to_column``. Without this call,
        the next project refresh re-reads the task as open/TODO on those
        providers and the loop the breaker exists to stop resumes.
        """
        for i in range(3):
            await lease_manager.recover_expired_lease(self._expired_lease(f"agent-{i}"))

        mock_kanban_client.move_task_to_column.assert_awaited_once_with(
            "task-667", "blocked"
        )

    @pytest.mark.asyncio
    async def test_blocked_path_tolerates_client_without_move_method(
        self, mock_persistence, task
    ):
        """A kanban client lacking move_task_to_column doesn't break the breaker.

        The status write via ``update_task`` must still land and the
        breaker must complete without raising.
        """
        client = Mock(spec=["update_task", "add_comment"])
        client.update_task = AsyncMock()
        client.add_comment = AsyncMock()
        manager = AssignmentLeaseManager(
            client,
            mock_persistence,
            default_lease_hours=1.0,
            task_list=[task],
        )

        result = False
        for i in range(3):
            result = await manager.recover_expired_lease(
                self._expired_lease(f"agent-{i}")
            )

        assert result is True
        final_update = client.update_task.await_args_list[-1]
        assert final_update.args[1]["status"] == TaskStatus.BLOCKED
