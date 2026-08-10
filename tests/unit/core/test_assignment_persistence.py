"""
Unit tests for AssignmentPersistence record merging and the lease round-trip.

Why this file exists
--------------------
``src/core/assignment_persistence.py`` had no dedicated test module. That gap
hid a real defect: ``save_assignment`` replaced a worker's cached record
wholesale on every call, so everything
``AssignmentLeaseManager._persist_lease`` wrote onto the record --
``lease_expires``, ``lease_renewed_at``, ``renewal_count``,
``progress_percentage``, ``update_timestamps``, ``merge_conflict_extensions``
-- was discarded before it reached disk, and ``assigned_at`` was reset to *now*
on every progress report.

Because ``load_active_leases`` defaults a missing ``lease_expires`` to *now*,
every lease rehydrated after a restart was born already expiring. The existing
tests passed throughout, because they asserted that ``_persist_lease`` mutated
the dict handed back by ``get_assignment`` -- which it did, on an object that
was then thrown away.

The defect was found while adding the ADR-0012 D11 ``lease_epoch``, which has
the same durability requirement: if the epoch does not survive a restart, the
fencing token silently stops fencing.

``TestLeaseRoundTrip`` is the regression test for the original bug: it drives
the real ``AssignmentPersistence`` (no mocks) through persist-then-rehydrate
and asserts the lease state actually survives.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core.assignment_lease import AssignmentLease, AssignmentLeaseManager
from src.core.assignment_persistence import AssignmentPersistence

pytestmark = pytest.mark.unit


@pytest.fixture
def persistence(tmp_path: Path) -> AssignmentPersistence:
    """
    Create a real AssignmentPersistence rooted in a temp directory.

    Parameters
    ----------
    tmp_path : Path
        pytest-provided isolated directory, so no test can reach the
        production ``data/`` tree (issue #724).

    Returns
    -------
    AssignmentPersistence
        Persistence layer backed by ``tmp_path/assignments``.
    """
    return AssignmentPersistence(storage_dir=tmp_path / "assignments")


class TestSaveAssignmentMerging:
    """save_assignment must not clobber fields other subsystems wrote."""

    @pytest.mark.asyncio
    async def test_preserves_lease_fields_for_the_same_task(
        self, persistence: AssignmentPersistence
    ) -> None:
        """Re-saving the same task keeps previously merged lease fields."""
        await persistence.update_assignment_fields(
            "agent-1", "task-1", {"renewal_count": 2, "progress_percentage": 30}
        )

        await persistence.save_assignment("agent-1", "task-1", {"name": "Build API"})

        record = await persistence.get_assignment("agent-1")
        assert record is not None
        assert record["renewal_count"] == 2
        assert record["progress_percentage"] == 30
        assert record["task_data"] == {"name": "Build API"}

    @pytest.mark.asyncio
    async def test_preserves_original_assigned_at_for_the_same_task(
        self, persistence: AssignmentPersistence
    ) -> None:
        """assigned_at is not reset on every save.

        It previously was, which corrupted every "time spent on task"
        calculation the moment an agent reported any progress.
        """
        await persistence.save_assignment("agent-1", "task-1", {"name": "Build API"})
        original = (await persistence.get_assignment("agent-1"))["assigned_at"]

        await persistence.save_assignment("agent-1", "task-1", {"name": "Build API v2"})

        assert (await persistence.get_assignment("agent-1"))["assigned_at"] == original

    @pytest.mark.asyncio
    async def test_new_task_drops_stale_lease_fields(
        self, persistence: AssignmentPersistence
    ) -> None:
        """Reassigning the worker to a different task starts a clean record."""
        await persistence.update_assignment_fields(
            "agent-1", "task-1", {"renewal_count": 2, "progress_percentage": 30}
        )

        await persistence.save_assignment("agent-1", "task-2", {"name": "Other work"})

        record = await persistence.get_assignment("agent-1")
        assert record is not None
        assert record["task_id"] == "task-2"
        assert "renewal_count" not in record
        assert "progress_percentage" not in record


class TestUpdateAssignmentFields:
    """The merge path used by the lease manager."""

    @pytest.mark.asyncio
    async def test_creates_the_record_when_absent(
        self, persistence: AssignmentPersistence
    ) -> None:
        """A lease is created before the assignment row exists.

        A method that skipped missing records would drop the D11 fencing
        token for every freshly-claimed task.
        """
        await persistence.update_assignment_fields(
            "agent-1", "task-1", {"renewal_count": 1}
        )

        record = await persistence.get_assignment("agent-1")
        assert record is not None
        assert record["task_id"] == "task-1"
        assert record["renewal_count"] == 1

    @pytest.mark.asyncio
    async def test_merges_onto_an_existing_record(
        self, persistence: AssignmentPersistence
    ) -> None:
        """Successive merges accumulate rather than replace."""
        await persistence.save_assignment("agent-1", "task-1", {"name": "Build API"})

        await persistence.update_assignment_fields(
            "agent-1", "task-1", {"renewal_count": 2}
        )
        await persistence.update_assignment_fields(
            "agent-1", "task-1", {"progress_percentage": 60}
        )

        record = await persistence.get_assignment("agent-1")
        assert record is not None
        assert record["renewal_count"] == 2
        assert record["progress_percentage"] == 60
        assert record["task_data"] == {"name": "Build API"}

    @pytest.mark.asyncio
    async def test_survives_a_reload_from_disk(
        self, persistence: AssignmentPersistence, tmp_path: Path
    ) -> None:
        """Merged fields are written to disk, not just the in-memory cache."""
        await persistence.update_assignment_fields(
            "agent-1", "task-1", {"renewal_count": 9}
        )

        reopened = AssignmentPersistence(storage_dir=tmp_path / "assignments")
        assignments = await reopened.load_assignments()

        assert assignments["agent-1"]["renewal_count"] == 9


class TestLeaseRoundTrip:
    """Regression test for the discarded-lease-state defect."""

    @pytest.mark.asyncio
    async def test_lease_state_survives_persist_then_rehydrate(
        self, tmp_path: Path
    ) -> None:
        """A persisted lease rehydrates with its epoch and expiry intact.

        Drives the real persistence layer end to end. Before the fix,
        ``lease_expires`` never reached disk, so ``load_active_leases``
        fell back to its current-time default and every rehydrated lease
        was born expiring -- and the D11 epoch was lost entirely.
        """
        storage = tmp_path / "assignments"
        expires_at = datetime.now(timezone.utc) + timedelta(hours=3)

        writer = AssignmentLeaseManager(Mock(), AssignmentPersistence(storage))
        lease = AssignmentLease(
            task_id="task-1",
            agent_id="agent-1",
            assigned_at=datetime.now(timezone.utc),
            lease_expires=expires_at,
            last_renewed=datetime.now(timezone.utc),
            renewal_count=3,
            progress_percentage=60,
            merge_conflict_extensions=1,
        )
        await writer._persist_lease(lease)

        reader = AssignmentLeaseManager(Mock(), AssignmentPersistence(storage))
        await reader.load_active_leases()

        rehydrated = reader.active_leases["task-1"]
        assert rehydrated.renewal_count == 3
        assert rehydrated.progress_percentage == 60
        assert rehydrated.merge_conflict_extensions == 1
        assert not rehydrated.is_expired
        assert abs((rehydrated.lease_expires - expires_at).total_seconds()) < 1
