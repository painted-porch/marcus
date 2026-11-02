"""
Unit tests for RegistrationPersistence (ADR-0012 obligation O1).

A worker registers once and then pulls chores for hours. ``agent_status``
and ``agent_project_map`` lived only in memory, so a Marcus restart made
every live worker an unknown agent whose polls failed forever. This store
is the durable half of the fix: one JSON file beside ``assignments.json``,
written atomically, holding what ``register_agent`` was told plus
``registered_at`` and ``last_seen``.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.models import WorkerStatus
from src.core.registration_persistence import (
    RegistrationPersistence,
    record_from_worker,
    worker_from_record,
)

pytestmark = pytest.mark.unit


def _worker(agent_id: str = "agent-a", **overrides: object) -> WorkerStatus:
    fields: dict = dict(
        worker_id=agent_id,
        name="Alice",
        role="Backend Developer",
        email=None,
        current_tasks=[],
        completed_tasks_count=3,
        capacity=40,
        skills=["python", "sql"],
        availability={"monday": True, "sunday": False},
        performance_score=1.0,
        vendor="anthropic",
        principal="agent",
    )
    fields.update(overrides)
    return WorkerStatus(**fields)


class TestRecordConversion:
    """WorkerStatus <-> on-disk record, minus the volatile parts."""

    def test_round_trip_preserves_identity_and_counters(self) -> None:
        """Everything register_agent was told survives a round trip."""
        when = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        record = record_from_worker(
            _worker(), project_id="proj-1", registered_at=when, last_seen=when
        )

        restored = worker_from_record(record)

        assert restored.worker_id == "agent-a"
        assert restored.name == "Alice"
        assert restored.role == "Backend Developer"
        assert restored.skills == ["python", "sql"]
        assert restored.vendor == "anthropic"
        assert restored.principal == "agent"
        assert restored.completed_tasks_count == 3
        assert restored.capacity == 40
        assert restored.availability == {"monday": True, "sunday": False}
        assert record["project_id"] == "proj-1"
        assert record["registered_at"] == when.isoformat()
        assert record["last_seen"] == when.isoformat()

    def test_current_tasks_are_not_persisted(self) -> None:
        """Who holds what is the lease store's business, not this file's."""
        record = record_from_worker(
            _worker(current_tasks=[object()]),  # type: ignore[list-item]
            project_id="proj-1",
            registered_at=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )

        assert "current_tasks" not in record
        assert worker_from_record(record).current_tasks == []

    def test_record_is_json_serializable(self) -> None:
        """The record goes straight to json.dumps."""
        record = record_from_worker(
            _worker(),
            project_id="proj-1",
            registered_at=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        json.dumps(record)


class TestSaveAndLoad:
    """The file is the roster that outlives the process."""

    @pytest.mark.asyncio
    async def test_save_then_load_in_a_new_instance(self, tmp_path: Path) -> None:
        """A second instance over the same directory sees the first's writes."""
        store = RegistrationPersistence(tmp_path)
        when = datetime.now(timezone.utc)
        await store.save(
            record_from_worker(
                _worker(), project_id="proj-1", registered_at=when, last_seen=when
            )
        )
        await store.save(
            record_from_worker(
                _worker("agent-b", name="Bob"),
                project_id="proj-2",
                registered_at=when,
                last_seen=when,
            )
        )

        loaded = await RegistrationPersistence(tmp_path).load()

        assert set(loaded) == {"agent-a", "agent-b"}
        assert loaded["agent-a"]["project_id"] == "proj-1"
        assert loaded["agent-b"]["name"] == "Bob"

    @pytest.mark.asyncio
    async def test_save_replaces_the_record_for_the_same_agent(
        self, tmp_path: Path
    ) -> None:
        """Re-registration overwrites; the roster never holds two rows per id."""
        store = RegistrationPersistence(tmp_path)
        when = datetime.now(timezone.utc)
        await store.save(
            record_from_worker(
                _worker(role="old"), project_id="p", registered_at=when, last_seen=when
            )
        )
        await store.save(
            record_from_worker(
                _worker(role="new"), project_id="p", registered_at=when, last_seen=when
            )
        )

        loaded = await RegistrationPersistence(tmp_path).load()

        assert len(loaded) == 1
        assert loaded["agent-a"]["role"] == "new"

    @pytest.mark.asyncio
    async def test_remove_drops_the_record(self, tmp_path: Path) -> None:
        """Eviction removes the row on disk, not just in memory."""
        store = RegistrationPersistence(tmp_path)
        when = datetime.now(timezone.utc)
        await store.save(
            record_from_worker(
                _worker(), project_id="p", registered_at=when, last_seen=when
            )
        )

        await store.remove("agent-a")
        await store.remove("never-registered")  # tolerated

        assert await RegistrationPersistence(tmp_path).load() == {}

    @pytest.mark.asyncio
    async def test_missing_file_loads_empty(self, tmp_path: Path) -> None:
        """A fresh data directory is an empty roster, not an error."""
        assert await RegistrationPersistence(tmp_path).load() == {}

    @pytest.mark.asyncio
    async def test_corrupt_file_loads_empty_and_is_not_overwritten_silently(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Garbage on disk yields an empty roster and a loud log line."""
        (tmp_path / "registrations.json").write_text("{not json", encoding="utf-8")

        loaded = await RegistrationPersistence(tmp_path).load()

        assert loaded == {}
        assert any("registrations" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_write_is_atomic(self, tmp_path: Path) -> None:
        """No temp file is left behind and the target is complete JSON."""
        store = RegistrationPersistence(tmp_path)
        when = datetime.now(timezone.utc)
        await store.save(
            record_from_worker(
                _worker(), project_id="p", registered_at=when, last_seen=when
            )
        )

        assert sorted(p.name for p in tmp_path.iterdir()) == ["registrations.json"]
        json.loads((tmp_path / "registrations.json").read_text(encoding="utf-8"))

    @pytest.mark.asyncio
    async def test_update_fields_merges_into_the_record(self, tmp_path: Path) -> None:
        """A counter bump touches one field and keeps the rest."""
        store = RegistrationPersistence(tmp_path)
        when = datetime.now(timezone.utc)
        await store.save(
            record_from_worker(
                _worker(), project_id="p", registered_at=when, last_seen=when
            )
        )

        await store.update_fields("agent-a", completed_tasks_count=4)
        await store.update_fields("unknown", completed_tasks_count=1)  # no-op

        loaded = await RegistrationPersistence(tmp_path).load()
        assert loaded["agent-a"]["completed_tasks_count"] == 4
        assert loaded["agent-a"]["role"] == "Backend Developer"
        assert "unknown" not in loaded


class TestTouch:
    """last_seen is the eviction clock; writes are debounced."""

    @pytest.mark.asyncio
    async def test_touch_updates_last_seen_and_debounces_disk_writes(
        self, tmp_path: Path
    ) -> None:
        """The first touch after the interval is written; the next is not."""
        store = RegistrationPersistence(tmp_path, touch_write_interval_seconds=60.0)
        t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        await store.save(
            record_from_worker(
                _worker(), project_id="p", registered_at=t0, last_seen=t0
            )
        )

        wrote_1 = await store.touch("agent-a", now=t0 + timedelta(seconds=90))
        wrote_2 = await store.touch("agent-a", now=t0 + timedelta(seconds=100))
        wrote_3 = await store.touch("agent-a", now=t0 + timedelta(seconds=200))

        assert (wrote_1, wrote_2, wrote_3) == (True, False, True)
        on_disk = await RegistrationPersistence(tmp_path).load()
        assert (
            on_disk["agent-a"]["last_seen"] == (t0 + timedelta(seconds=200)).isoformat()
        )
        # In memory the latest touch always wins, written or not.
        assert store.last_seen("agent-a") == t0 + timedelta(seconds=200)

    @pytest.mark.asyncio
    async def test_touch_of_unknown_agent_is_a_no_op(self, tmp_path: Path) -> None:
        """Touching an agent that never registered writes nothing."""
        store = RegistrationPersistence(tmp_path)

        assert await store.touch("ghost") is False
        assert store.last_seen("ghost") is None
        assert not (tmp_path / "registrations.json").exists()

    @pytest.mark.asyncio
    async def test_load_populates_last_seen(self, tmp_path: Path) -> None:
        """After a restart the eviction clock starts from what was on disk."""
        store = RegistrationPersistence(tmp_path)
        t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        await store.save(
            record_from_worker(
                _worker(), project_id="p", registered_at=t0, last_seen=t0
            )
        )

        fresh = RegistrationPersistence(tmp_path)
        await fresh.load()

        assert fresh.last_seen("agent-a") == t0
