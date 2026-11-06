"""
Unit tests for the Persistence layer
"""

import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.context import Decision
from src.core.events import Event
from src.core.persistence import (
    FilePersistence,
    Persistence,
    PersistenceBackend,
    SQLitePersistence,
    write_task_metadata,
    write_task_metadata_rows,
)

pytestmark = pytest.mark.unit


class TestFilePersistence:
    """Test suite for file-based persistence"""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for testing"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def file_persistence(self, temp_dir):
        """Create a FilePersistence instance for testing"""
        return FilePersistence(storage_dir=temp_dir)

    @pytest.mark.asyncio
    async def test_store_and_retrieve(self, file_persistence):
        """Test storing and retrieving data"""
        await file_persistence.store(
            "test_collection", "key1", {"data": "value1", "number": 42}
        )

        result = await file_persistence.retrieve("test_collection", "key1")

        assert result is not None
        assert result["data"] == "value1"
        assert result["number"] == 42
        assert "_stored_at" in result

    @pytest.mark.asyncio
    async def test_retrieve_nonexistent(self, file_persistence):
        """Test retrieving non-existent data"""
        result = await file_persistence.retrieve("test_collection", "nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_query(self, file_persistence):
        """Test querying data from a collection"""
        # Store multiple items
        for i in range(5):
            await file_persistence.store(
                "test_collection",
                f"key{i}",
                {"value": i, "type": "even" if i % 2 == 0 else "odd"},
            )

        # Query all
        all_items = await file_persistence.query("test_collection")
        assert len(all_items) == 5

        # Query with filter
        even_items = await file_persistence.query(
            "test_collection", filter_func=lambda x: x.get("type") == "even"
        )
        assert len(even_items) == 3  # 0, 2, 4

    @pytest.mark.asyncio
    async def test_delete(self, file_persistence):
        """Test deleting data"""
        await file_persistence.store("test_collection", "key1", {"data": "value1"})
        await file_persistence.store("test_collection", "key2", {"data": "value2"})

        await file_persistence.delete("test_collection", "key1")

        result1 = await file_persistence.retrieve("test_collection", "key1")
        result2 = await file_persistence.retrieve("test_collection", "key2")

        assert result1 is None
        assert result2 is not None

    @pytest.mark.asyncio
    async def test_clear_old(self, file_persistence):
        """Test clearing old data"""
        # Store data with different timestamps
        # We'll manually set _stored_at for testing
        file_path = file_persistence._get_collection_file("test_collection")

        old_date = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        recent_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

        data = {
            "old_key": {"data": "old", "_stored_at": old_date},
            "recent_key": {"data": "recent", "_stored_at": recent_date},
        }

        # Write directly for testing
        with open(file_path, "w") as f:
            json.dump(data, f)

        # Clear data older than 30 days
        removed = await file_persistence.clear_old("test_collection", 30)

        assert removed == 1

        # Check what remains
        remaining = await file_persistence.query("test_collection")
        assert len(remaining) == 1
        assert remaining[0]["data"] == "recent"

    @pytest.mark.asyncio
    async def test_concurrent_access(self, file_persistence):
        """Test concurrent access to same collection"""

        async def store_data(key, value):
            await file_persistence.store("concurrent", key, {"value": value})

        # Run multiple stores concurrently
        await asyncio.gather(
            store_data("key1", "value1"),
            store_data("key2", "value2"),
            store_data("key3", "value3"),
        )

        # Verify all data was stored
        items = await file_persistence.query("concurrent")
        assert len(items) == 3

    async def test_concurrent_writers_do_not_lose_each_others_entries(self, temp_dir):
        """Two independent FilePersistence instances racing on the same
        collection file must not drop each other's writes.

        ``_get_lock``'s asyncio.Lock is per-instance, so it does
        nothing when two separate ``FilePersistence`` objects point at
        the same directory -- e.g. two ``ProjectRegistry()``
        instances in one process, or two separate OS processes
        sharing a data directory (issue #883: a second project
        registered on an already-running server never appeared in
        the shared projects.json; the write path's own cross-process
        locking gap is the explanation that reproduces deterministically
        here. A plain asyncio.Lock on one instance can't close this --
        only an OS-level lock shared by file descriptor, not by Python
        object, can).
        """
        writer_a = FilePersistence(storage_dir=temp_dir)
        writer_b = FilePersistence(storage_dir=temp_dir)

        # Before the fix, two independent instances' asyncio.Lock
        # objects never contended with each other, so both writers'
        # read-modify-write cycles could interleave freely and the
        # loser's entry was silently overwritten. Running them
        # concurrently, with no artificial delay needed, is enough to
        # trigger that interleaving on an unlocked implementation.
        await asyncio.gather(
            writer_a.store("projects", "proj-1", {"name": "proj-1"}),
            writer_b.store("projects", "proj-2", {"name": "proj-2"}),
        )

        on_disk = json.loads((temp_dir / "projects.json").read_text())
        assert set(on_disk.keys()) == {"proj-1", "proj-2"}

    async def test_many_concurrent_writers_do_not_deadlock(self, temp_dir):
        """A blocking cross-process lock on the default executor can
        deadlock -- this must complete quickly instead.

        The first cut of #883's fix ran ``fcntl.flock(LOCK_EX)`` (a
        blocking call) in a worker thread via ``asyncio.to_thread``.
        That thread pool is the same default executor ``aiofiles``
        uses for its own file I/O. With enough concurrent writers,
        every worker thread can end up parked waiting on the lock
        while the thread actually holding it has no free thread left
        to run its own read/write or release the lock -- a real
        deadlock (Codex review on PR #885 reproduced it with 40
        independent writers). This test uses the same count,
        specifically more than the default executor's worker cap
        (``min(32, cpu_count + 4)``), so it cannot pass by accident on
        a machine with enough CPUs to avoid exhausting the pool.

        The fix never blocks a thread: it polls a non-blocking flock
        and backs off on the event loop instead. There is no
        acquisition timeout in the implementation (a bounded wait
        that gives up would silently reintroduce #883 under load), so
        correctness here is bounded by this test's own
        ``asyncio.wait_for`` instead -- a deadlock shows up as this
        test timing out, not as an exception.
        """
        writer_count = 40
        writers = [FilePersistence(storage_dir=temp_dir) for _ in range(writer_count)]

        async def store_all() -> None:
            await asyncio.gather(
                *(
                    writer.store("projects", f"proj-{i}", {"name": f"proj-{i}"})
                    for i, writer in enumerate(writers)
                )
            )

        await asyncio.wait_for(store_all(), timeout=10.0)

        on_disk = json.loads((temp_dir / "projects.json").read_text())
        assert set(on_disk.keys()) == {f"proj-{i}" for i in range(writer_count)}


class TestSQLitePersistence:
    """Test suite for SQLite-based persistence"""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = Path(tmp.name)
        yield db_path
        # Cleanup
        if db_path.exists():
            db_path.unlink()

    @pytest.fixture
    def sqlite_persistence(self, temp_db):
        """Create a SQLitePersistence instance for testing"""
        return SQLitePersistence(db_path=temp_db)

    @pytest.mark.asyncio
    async def test_store_and_retrieve(self, sqlite_persistence):
        """Test storing and retrieving data with SQLite"""
        await sqlite_persistence.store(
            "test_collection", "key1", {"data": "value1", "nested": {"key": "value"}}
        )

        result = await sqlite_persistence.retrieve("test_collection", "key1")

        assert result is not None
        assert result["data"] == "value1"
        assert result["nested"]["key"] == "value"

    @pytest.mark.asyncio
    async def test_query_with_limit(self, sqlite_persistence):
        """Test querying with limit"""
        import sqlite3
        from datetime import datetime, timedelta

        # Manually insert data with different timestamps to ensure proper ordering
        # This bypasses the store method to have full control over timestamps
        with sqlite3.connect(sqlite_persistence.db_path) as conn:
            for i in range(20):
                # Create timestamps that are clearly different (1 minute apart)
                timestamp = (
                    datetime.now(timezone.utc) + timedelta(minutes=i)
                ).isoformat()
                conn.execute(
                    "INSERT INTO persistence (collection, key, data, stored_at) VALUES (?, ?, ?, ?)",
                    ("test_collection", f"key{i}", f'{{"value": {i}}}', timestamp),
                )
            conn.commit()

        # Query with limit
        items = await sqlite_persistence.query("test_collection", limit=5)

        assert len(items) == 5
        # Should be ordered by stored_at DESC (newest first)
        # Debug: print the items to see ordering
        print(f"Items received: {[item['value'] for item in items]}")
        assert items[0]["value"] == 19

    @pytest.mark.asyncio
    async def test_clear_old_sqlite(self, sqlite_persistence):
        """Test clearing old data from SQLite"""
        # We need to manually insert old data for testing
        import sqlite3

        old_date = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        recent_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

        with sqlite3.connect(sqlite_persistence.db_path) as conn:
            conn.execute(
                """
                INSERT INTO persistence (collection, key, data, stored_at)
                VALUES (?, ?, ?, ?)
            """,
                ("test", "old_key", '{"data": "old"}', old_date),
            )

            conn.execute(
                """
                INSERT INTO persistence (collection, key, data, stored_at)
                VALUES (?, ?, ?, ?)
            """,
                ("test", "recent_key", '{"data": "recent"}', recent_date),
            )

            conn.commit()

        # Clear old data
        removed = await sqlite_persistence.clear_old("test", 30)

        assert removed == 1

        # Verify only recent data remains
        remaining = await sqlite_persistence.query("test")
        assert len(remaining) == 1
        assert remaining[0]["data"] == "recent"


class TestPersistence:
    """Test suite for main Persistence interface"""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock backend for testing"""
        backend = Mock(spec=PersistenceBackend)
        backend.store = AsyncMock()
        backend.retrieve = AsyncMock()
        backend.query = AsyncMock(return_value=[])
        backend.delete = AsyncMock()
        backend.clear_old = AsyncMock(return_value=0)
        return backend

    @pytest.fixture
    def persistence(self, mock_backend):
        """Create a Persistence instance with mock backend"""
        return Persistence(backend=mock_backend)

    @pytest.mark.asyncio
    async def test_store_event(self, persistence, mock_backend):
        """Test storing an event"""
        event = Event(
            event_id="evt_123",
            timestamp=datetime.now(timezone.utc),
            event_type="test_event",
            source="test",
            data={"key": "value"},
        )

        await persistence.store_event(event)

        mock_backend.store.assert_called_once_with("events", "evt_123", event.to_dict())

    @pytest.mark.asyncio
    async def test_get_events(self, persistence, mock_backend):
        """Test retrieving events"""
        # Mock backend response
        mock_backend.query.return_value = [
            {
                "event_id": "evt_1",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": "test_event",
                "source": "test",
                "data": {"key": "value"},
                "_key": "evt_1",
                "_stored_at": datetime.now(timezone.utc).isoformat(),
            }
        ]

        events = await persistence.get_events(event_type="test_event")

        assert len(events) == 1
        assert isinstance(events[0], Event)
        assert events[0].event_id == "evt_1"
        assert events[0].event_type == "test_event"

    @pytest.mark.asyncio
    async def test_store_decision(self, persistence, mock_backend):
        """Test storing a decision"""
        decision = Decision(
            decision_id="dec_123",
            task_id="task_1",
            agent_id="agent_1",
            timestamp=datetime.now(timezone.utc),
            what="Use PostgreSQL",
            why="Need ACID",
            impact="All services",
        )

        await persistence.store_decision(decision)

        mock_backend.store.assert_called_once_with(
            "decisions", "dec_123", decision.to_dict()
        )

    @pytest.mark.asyncio
    async def test_get_decisions(self, persistence, mock_backend):
        """Test retrieving decisions"""
        # Mock backend response
        mock_backend.query.return_value = [
            {
                "decision_id": "dec_1",
                "task_id": "task_1",
                "agent_id": "agent_1",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "what": "Use REST",
                "why": "Standard",
                "impact": "APIs",
            }
        ]

        decisions = await persistence.get_decisions(task_id="task_1")

        assert len(decisions) == 1
        assert isinstance(decisions[0], Decision)
        assert decisions[0].decision_id == "dec_1"
        assert decisions[0].what == "Use REST"

    @pytest.mark.asyncio
    async def test_cleanup(self, persistence, mock_backend):
        """Test cleanup of old data"""
        mock_backend.clear_old.side_effect = [5, 3, 0, 2]  # Different counts

        results = await persistence.cleanup(days=30)

        # Should have called clear_old for each collection
        assert mock_backend.clear_old.call_count == 4

        # Results should include non-zero counts
        assert results == {"events": 5, "decisions": 3, "patterns": 2}

    @pytest.mark.asyncio
    async def test_general_store_retrieve(self, persistence, mock_backend):
        """Test general store/retrieve methods"""
        await persistence.store("custom", "key1", {"data": "value"})

        mock_backend.store.assert_called_with("custom", "key1", {"data": "value"})

        mock_backend.retrieve.return_value = {"data": "value"}
        result = await persistence.retrieve("custom", "key1")

        assert result == {"data": "value"}
        mock_backend.retrieve.assert_called_with("custom", "key1")


class TestWriteTaskMetadata:
    """Issue #741 step 3: the sanctioned ``task_metadata`` write.

    Cato renders its task universe from this collection; before this
    existed, anything that created tasks outside the NLP pipeline (an
    experiment seeder, a future intake adapter) had to open its own
    ``SQLitePersistence`` and write the collection by hand.
    """

    @pytest.fixture
    def db_path(self, tmp_path: Path) -> Path:
        return tmp_path / "marcus.db"

    @pytest.mark.asyncio
    async def test_a_single_row_is_written_and_readable_back(
        self, db_path: Path
    ) -> None:
        await write_task_metadata(
            str(db_path),
            {
                "task_id": "task-1",
                "name": "Find the headline",
                "project_id": "tow-b-claude",
            },
        )

        store = Persistence(backend=SQLitePersistence(db_path=db_path))
        stored = await store.retrieve("task_metadata", "task-1")

        assert stored is not None
        assert stored["name"] == "Find the headline"
        assert stored["project_id"] == "tow-b-claude"

    @pytest.mark.asyncio
    async def test_a_row_without_a_task_id_is_rejected(self, db_path: Path) -> None:
        with pytest.raises(KeyError):
            await write_task_metadata(str(db_path), {"name": "no id here"})

    @pytest.mark.asyncio
    async def test_several_rows_are_each_written_under_their_own_id(
        self, db_path: Path
    ) -> None:
        rows = [
            {"task_id": "task-1", "name": "Find"},
            {"task_id": "task-2", "name": "Verify"},
        ]

        await write_task_metadata_rows(str(db_path), rows)

        store = Persistence(backend=SQLitePersistence(db_path=db_path))
        first = await store.retrieve("task_metadata", "task-1")
        second = await store.retrieve("task_metadata", "task-2")

        assert first is not None and first["name"] == "Find"
        assert second is not None and second["name"] == "Verify"
