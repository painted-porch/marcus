"""
General persistence layer for Marcus systems.

Provides a unified storage interface for Events, Context, Memory, and other
systems that need persistent data. Supports multiple backends starting with
file-based storage and ready for database backends.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles

from src.core.context import Decision
from src.core.event_loop_utils import EventLoopLockManager
from src.core.events import Event
from src.core.paths import DATA_DIR_ENV

logger = logging.getLogger(__name__)


#: A JSON field name as it may appear in a ``where`` prefilter.
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _where_values(expected: Any) -> List[Any]:
    """Return the list of acceptable values behind one ``where`` entry."""
    if isinstance(expected, (list, tuple, set, frozenset)):
        return list(expected)
    return [expected]


def _matches_where(item: Dict[str, Any], where: Optional[Dict[str, Any]]) -> bool:
    """Apply a ``where`` prefilter in Python (the file backend, and tests).

    Each entry is ``field: value`` or ``field: [values]``; ``None`` in the
    values matches a field that is null or absent. Entries are ANDed.
    """
    if not where:
        return True
    for field, expected in where.items():
        values = _where_values(expected)
        actual = item.get(field)
        if actual is None:
            if not any(v is None for v in values):
                return False
        elif actual not in [v for v in values if v is not None]:
            return False
    return True


class PersistenceBackend:
    """Base class for persistence backends."""

    async def store(self, collection: str, key: str, data: Dict[str, Any]) -> None:
        """Store data in a collection."""
        raise NotImplementedError

    async def retrieve(self, collection: str, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve data from a collection."""
        raise NotImplementedError

    async def query(
        self,
        collection: str,
        filter_func: Optional[Any] = None,
        limit: int = 100,
        where: Optional[Dict[str, Any]] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query data from a collection, newest stored first.

        Parameters
        ----------
        collection : str
            Collection to read.
        filter_func : Optional[Any]
            Python predicate applied to every candidate row. The limit
            counts rows that pass it, so a match behind many newer
            non-matching rows is still found.
        limit : int
            Maximum number of matching rows to return.
        where : Optional[Dict[str, Any]]
            Prefilter on JSON fields applied before the limit: ``field:
            value`` or ``field: [values]``; ``None`` matches a null or
            absent field. Entries are ANDed. Backends that can push this
            into storage do, which is what keeps a per-project read
            cheap on a shared database.
        offset : int
            Number of matching rows to skip, newest first.
        """
        raise NotImplementedError

    async def delete(self, collection: str, key: str) -> None:
        """Delete data from a collection."""
        raise NotImplementedError

    async def clear_old(self, collection: str, days: int) -> int:
        """Clear data older than specified days."""
        raise NotImplementedError


class FilePersistence(PersistenceBackend):
    """File-based persistence using JSON files."""

    def __init__(self, storage_dir: Optional[Path] = None) -> None:
        """
        Initialize file-based persistence.

        Parameters
        ----------
        storage_dir : Optional[Path]
            Directory for the JSON collections. When omitted,
            ``$MARCUS_DATA_DIR/marcus_state`` if the override is set (issue
            #766: an isolated data directory must isolate the project
            registry too, as it already isolates the subtask store),
            otherwise the historical repo-root ``data/marcus_state``.
        """
        if storage_dir is None:
            override = os.environ.get(DATA_DIR_ENV)
            if override:
                storage_dir = Path(override) / "marcus_state"
            else:
                # Use absolute path to ensure it works regardless of cwd
                marcus_root = Path(__file__).parent.parent.parent
                storage_dir = marcus_root / "data" / "marcus_state"
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._lock_managers: Dict[str, EventLoopLockManager] = {}

    def _get_lock(self, collection: str) -> asyncio.Lock:
        """
        Get or create a lock for a collection.

        Ensures correct event loop binding.
        """
        if collection not in self._lock_managers:
            self._lock_managers[collection] = EventLoopLockManager()
        return self._lock_managers[collection].get_lock()

    def _get_collection_file(self, collection: str) -> Path:
        """Get the file path for a collection."""
        return self.storage_dir / f"{collection}.json"

    async def store(self, collection: str, key: str, data: Dict[str, Any]) -> None:
        """Store data in a collection."""
        lock = self._get_lock(collection)
        async with lock:
            # Load existing data
            file_path = self._get_collection_file(collection)
            existing_data: Dict[str, Any] = {}

            if file_path.exists():
                try:
                    async with aiofiles.open(file_path, "r") as f:
                        content = await f.read()
                        existing_data = json.loads(content) if content else {}
                except Exception as e:
                    logger.error(f"Error loading {collection}: {e}")

            # Update with new data
            existing_data[key] = {
                **data,
                "_stored_at": datetime.now(timezone.utc).isoformat(),
            }

            # Write back atomically
            temp_file = file_path.with_suffix(".tmp")
            try:
                async with aiofiles.open(temp_file, "w") as f:
                    await f.write(json.dumps(existing_data, indent=2, default=str))
                temp_file.replace(file_path)
            except Exception as e:
                logger.error(f"Error writing {collection}: {e}")
                if temp_file.exists():
                    temp_file.unlink()
                raise

    async def retrieve(self, collection: str, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve data from a collection."""
        lock = self._get_lock(collection)
        async with lock:
            file_path = self._get_collection_file(collection)

            if not file_path.exists():
                return None

            try:
                async with aiofiles.open(file_path, "r") as f:
                    content = await f.read()
                    data = json.loads(content) if content else {}
                    return data.get(key)
            except Exception as e:
                logger.error(f"Error reading {collection}: {e}")
                return None

    async def query(
        self,
        collection: str,
        filter_func: Optional[Any] = None,
        limit: int = 100,
        where: Optional[Dict[str, Any]] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query data from a collection."""
        lock = self._get_lock(collection)
        async with lock:
            file_path = self._get_collection_file(collection)

            if not file_path.exists():
                return []

            try:
                async with aiofiles.open(file_path, "r") as f:
                    content = await f.read()
                    data = json.loads(content) if content else {}

                # Convert to list of items
                items = [{"_key": k, **v} for k, v in data.items()]

                # Apply the prefilter and the predicate before any limit
                if where:
                    items = [item for item in items if _matches_where(item, where)]
                if filter_func:
                    items = [item for item in items if filter_func(item)]

                # Sort by stored time (newest first; key breaks ties), then page
                items.sort(
                    key=lambda x: (x.get("_stored_at", ""), x.get("_key", "")),
                    reverse=True,
                )

                return items[offset : offset + limit]

            except Exception as e:
                logger.error(f"Error querying {collection}: {e}")
                return []

    async def delete(self, collection: str, key: str) -> None:
        """Delete data from a collection."""
        lock = self._get_lock(collection)
        async with lock:
            file_path = self._get_collection_file(collection)

            if not file_path.exists():
                return

            try:
                async with aiofiles.open(file_path, "r") as f:
                    content = await f.read()
                    data = json.loads(content) if content else {}

                if key in data:
                    del data[key]

                    # Write back
                    temp_file = file_path.with_suffix(".tmp")
                    async with aiofiles.open(temp_file, "w") as f:
                        await f.write(json.dumps(data, indent=2, default=str))
                    temp_file.replace(file_path)

            except Exception as e:
                logger.error(f"Error deleting from {collection}: {e}")

    async def clear_old(self, collection: str, days: int) -> int:
        """Clear data older than specified days."""
        lock = self._get_lock(collection)
        async with lock:
            file_path = self._get_collection_file(collection)

            if not file_path.exists():
                return 0

            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            removed_count = 0

            try:
                async with aiofiles.open(file_path, "r") as f:
                    content = await f.read()
                    data = json.loads(content) if content else {}

                # Filter out old entries
                new_data = {}
                for key, value in data.items():
                    stored_at_str = value.get("_stored_at")
                    if stored_at_str:
                        stored_at = datetime.fromisoformat(stored_at_str)
                        if stored_at >= cutoff:
                            new_data[key] = value
                        else:
                            removed_count += 1
                    else:
                        # Keep entries without timestamp
                        new_data[key] = value

                # Write back if anything was removed
                if removed_count > 0:
                    temp_file = file_path.with_suffix(".tmp")
                    async with aiofiles.open(temp_file, "w") as f:
                        await f.write(json.dumps(new_data, indent=2, default=str))
                    temp_file.replace(file_path)

                return removed_count

            except Exception as e:
                logger.error(f"Error clearing old data from {collection}: {e}")
                return 0

    async def calculate_median_task_duration(self) -> float:
        """
        Calculate median task duration (fallback for file-based persistence).

        Loads task outcomes from file and calculates median in memory.
        Less efficient than SQL-based approach but works with file backend.

        Returns
        -------
        float
            Median task duration in hours. Returns 1.0 if no data available.
        """
        import statistics

        outcomes_data = await self.query("task_outcomes", limit=10000)

        if not outcomes_data:
            logger.debug("No task outcomes in file persistence")
            return 1.0

        # Filter successful tasks with actual_hours > 0
        durations = [
            o["actual_hours"]
            for o in outcomes_data
            if o.get("success") and o.get("actual_hours", 0) > 0
        ]

        if not durations:
            logger.debug("No successful task outcomes with duration")
            return 1.0

        median_value = float(statistics.median(durations))
        logger.debug(
            f"File-based median: {median_value:.2f} hours from {len(durations)} tasks"
        )
        return median_value


class SQLitePersistence(PersistenceBackend):
    """SQLite-based persistence for better performance and queries."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        """Initialize SQLite persistence."""
        self.db_path = db_path or Path("./data/marcus_state.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS persistence (
                    collection TEXT NOT NULL,
                    key TEXT NOT NULL,
                    data TEXT NOT NULL,
                    stored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (collection, key)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_stored_at
                ON persistence(stored_at)
            """)
            conn.commit()

    async def store(self, collection: str, key: str, data: Dict[str, Any]) -> None:
        """Store data in SQLite."""

        def _store() -> None:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO persistence (collection, key, data)
                    VALUES (?, ?, ?)
                """,
                    (collection, key, json.dumps(data, default=str)),
                )
                conn.commit()

        await asyncio.get_event_loop().run_in_executor(None, _store)

    async def retrieve(self, collection: str, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve data from SQLite."""

        def _retrieve() -> Optional[Dict[str, Any]]:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT data FROM persistence
                    WHERE collection = ? AND key = ?
                """,
                    (collection, key),
                )
                row = cursor.fetchone()
                return json.loads(row[0]) if row else None

        return await asyncio.get_event_loop().run_in_executor(None, _retrieve)

    @staticmethod
    def _where_sql(where: Optional[Dict[str, Any]]) -> tuple[str, List[Any]]:
        """Translate a ``where`` prefilter into SQL on the JSON ``data`` column.

        Returns the clause to append after ``WHERE collection = ?`` (empty
        when there is nothing to add) and its bound parameters. Field
        names are validated as identifiers; values are always bound.
        """
        if not where:
            return "", []
        clauses: List[str] = []
        params: List[Any] = []
        for field, expected in where.items():
            if not _FIELD_NAME.match(field):
                raise ValueError(f"invalid where field name: {field!r}")
            path = f"json_extract(data, '$.{field}')"
            values = _where_values(expected)
            non_null = [v for v in values if v is not None]
            parts: List[str] = []
            if non_null:
                parts.append(f"{path} IN ({', '.join('?' * len(non_null))})")
                params.extend(non_null)
            if any(v is None for v in values):
                parts.append(f"{path} IS NULL")
            # An empty list of values matches nothing.
            clauses.append("(" + " OR ".join(parts or ["0"]) + ")")
        return " AND " + " AND ".join(clauses), params

    async def query(
        self,
        collection: str,
        filter_func: Optional[Any] = None,
        limit: int = 100,
        where: Optional[Dict[str, Any]] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query data from SQLite.

        The ``where`` prefilter runs in SQL before any limit. A Python
        ``filter_func`` is applied by streaming the ordered rows, so the
        limit counts matches rather than candidates: the old
        ``LIMIT limit * 2`` prefetch returned an empty page whenever a
        match sat behind more than twice the limit in newer non-matching
        rows (Codex P2 on PR #800).
        """
        where_sql, where_params = self._where_sql(where)
        # The order is total (key breaks same-second ties) so that "the
        # newest N" is one well-defined set and paging never overlaps.
        base_sql = (
            "SELECT key, data, stored_at FROM persistence WHERE collection = ?"
            f"{where_sql} ORDER BY stored_at DESC, key DESC"
        )
        base_params: List[Any] = [collection, *where_params]

        def _load(row: Any) -> Dict[str, Any]:
            item: Dict[str, Any] = json.loads(row[1])
            item["_key"] = row[0]
            item["_stored_at"] = row[2]
            return item

        def _query() -> List[Dict[str, Any]]:
            with sqlite3.connect(self.db_path) as conn:
                if filter_func is None:
                    cursor = conn.execute(
                        base_sql + " LIMIT ? OFFSET ?", (*base_params, limit, offset)
                    )
                    return [_load(row) for row in cursor]

                items: List[Dict[str, Any]] = []
                skipped = 0
                for row in conn.execute(base_sql, base_params):
                    item = _load(row)
                    if not filter_func(item):
                        continue
                    if skipped < offset:
                        skipped += 1
                        continue
                    items.append(item)
                    if len(items) >= limit:
                        break
                return items

        return await asyncio.get_event_loop().run_in_executor(None, _query)

    async def delete(self, collection: str, key: str) -> None:
        """Delete data from SQLite."""

        def _delete() -> None:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    DELETE FROM persistence
                    WHERE collection = ? AND key = ?
                """,
                    (collection, key),
                )
                conn.commit()

        await asyncio.get_event_loop().run_in_executor(None, _delete)

    async def clear_old(self, collection: str, days: int) -> int:
        """Clear old data from SQLite."""

        def _clear() -> int:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    DELETE FROM persistence
                    WHERE collection = ? AND stored_at < ?
                """,
                    (collection, cutoff.isoformat()),
                )
                conn.commit()
                return cursor.rowcount

        return await asyncio.get_event_loop().run_in_executor(None, _clear)

    async def calculate_median_task_duration(self) -> float:
        """
        Calculate median task duration from all successful completed tasks.

        Uses SQL to efficiently compute median across all historical data.
        This is more scalable than loading all outcomes into memory.

        Returns
        -------
        float
            Median task duration in hours. Returns 1.0 if no data available.
        """

        def _calculate_median() -> float:
            with sqlite3.connect(self.db_path) as conn:
                # First, get count of successful tasks
                count_cursor = conn.execute("""
                    SELECT COUNT(*) FROM persistence
                    WHERE collection = 'task_outcomes'
                      AND json_extract(data, '$.success') = 1
                      AND CAST(json_extract(data, '$.actual_hours') AS REAL) > 0
                    """)
                count = count_cursor.fetchone()[0]

                if count == 0:
                    return 1.0  # Default fallback

                # Calculate median position
                # For odd count: middle element
                # For even count: average of two middle elements
                is_odd = count % 2 == 1
                middle_pos = count // 2

                if is_odd:
                    # Get single middle element
                    cursor = conn.execute(
                        """
                        SELECT CAST(json_extract(data, '$.actual_hours')
                                    AS REAL) as actual_hours
                        FROM persistence
                        WHERE collection = 'task_outcomes'
                          AND json_extract(data, '$.success') = 1
                          AND CAST(json_extract(data, '$.actual_hours')
                                   AS REAL) > 0
                        ORDER BY actual_hours
                        LIMIT 1 OFFSET ?
                        """,
                        (middle_pos,),
                    )
                    result = cursor.fetchone()
                    return float(result[0]) if result else 1.0
                else:
                    # Get two middle elements and average them
                    cursor = conn.execute(
                        """
                        SELECT CAST(json_extract(data, '$.actual_hours')
                                    AS REAL) as actual_hours
                        FROM persistence
                        WHERE collection = 'task_outcomes'
                          AND json_extract(data, '$.success') = 1
                          AND CAST(json_extract(data, '$.actual_hours')
                                   AS REAL) > 0
                        ORDER BY actual_hours
                        LIMIT 2 OFFSET ?
                        """,
                        (middle_pos - 1,),
                    )
                    results = cursor.fetchall()
                    if len(results) == 2:
                        return float((results[0][0] + results[1][0]) / 2.0)
                    elif len(results) == 1:
                        return float(results[0][0])
                    else:
                        return 1.0

        return await asyncio.get_event_loop().run_in_executor(None, _calculate_median)


class Persistence:
    """
    Main persistence interface for Marcus systems.

    Provides a unified way to store and retrieve data for Events,
    Context, Memory, and other systems.
    """

    def __init__(self, backend: Optional[PersistenceBackend] = None) -> None:
        """
        Initialize persistence layer.

        Parameters
        ----------
            backend
                Storage backend to use. Defaults to FilePersistence.
        """
        self.backend = backend or FilePersistence()

    # Event-specific methods
    async def store_event(self, event: Event) -> None:
        """Store an event."""
        await self.backend.store("events", event.event_id, event.to_dict())

    async def get_events(
        self,
        event_type: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 100,
    ) -> List[Event]:
        """Retrieve events with optional filtering."""

        def filter_func(item: Dict[str, Any]) -> bool:
            if event_type and item.get("event_type") != event_type:
                return False
            if source and item.get("source") != source:
                return False
            return True

        items = await self.backend.query("events", filter_func, limit)

        # Convert back to Event objects
        events = []
        for item in items:
            try:
                # Remove internal fields
                item.pop("_key", None)
                item.pop("_stored_at", None)

                # Reconstruct Event
                event = Event(
                    event_id=item["event_id"],
                    timestamp=datetime.fromisoformat(item["timestamp"]),
                    event_type=item["event_type"],
                    source=item["source"],
                    data=item["data"],
                    metadata=item.get("metadata"),
                )
                events.append(event)
            except Exception as e:
                logger.error(f"Error reconstructing event: {e}")

        return events

    # Context-specific methods
    async def store_decision(self, decision: Decision) -> None:
        """Store an architectural decision."""
        await self.backend.store("decisions", decision.decision_id, decision.to_dict())

    async def get_decisions(
        self,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Decision]:
        """Retrieve decisions with optional filtering."""

        def filter_func(item: Dict[str, Any]) -> bool:
            if task_id and item.get("task_id") != task_id:
                return False
            if agent_id and item.get("agent_id") != agent_id:
                return False
            return True

        items = await self.backend.query("decisions", filter_func, limit)

        # Convert back to Decision objects
        decisions = []
        for item in items:
            try:
                decision = Decision(
                    decision_id=item["decision_id"],
                    task_id=item.get("task_id"),
                    agent_id=item["agent_id"],
                    timestamp=datetime.fromisoformat(item["timestamp"]),
                    what=item["what"],
                    why=item["why"],
                    impact=item["impact"],
                    project_id=item.get("project_id"),
                    subject=item.get("subject"),
                    kind=item.get("kind", "decision"),
                )
                decisions.append(decision)
            except Exception as e:
                logger.error(f"Error reconstructing decision: {e}")

        return decisions

    # General methods
    async def store(self, collection: str, key: str, data: Dict[str, Any]) -> None:
        """Store arbitrary data in a collection."""
        await self.backend.store(collection, key, data)

    async def retrieve(self, collection: str, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve arbitrary data from a collection."""
        return await self.backend.retrieve(collection, key)

    async def query(
        self,
        collection: str,
        filter_func: Optional[Any] = None,
        limit: int = 100,
        where: Optional[Dict[str, Any]] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query a collection (see ``PersistenceBackend.query``)."""
        return await self.backend.query(
            collection, filter_func, limit, where=where, offset=offset
        )

    async def delete(self, collection: str, key: str) -> None:
        """Delete data from a collection."""
        await self.backend.delete(collection, key)

    async def cleanup(self, days: int = 30) -> Dict[str, int]:
        """Clean up old data from all collections."""
        collections = ["events", "decisions", "implementations", "patterns"]
        results = {}

        for collection in collections:
            count = await self.backend.clear_old(collection, days)
            if count > 0:
                results[collection] = count
                logger.info(f"Cleaned up {count} old items from {collection}")

        return results


class MemoryPersistence(PersistenceBackend):
    """In-memory persistence for testing and temporary storage."""

    def __init__(self) -> None:
        """Initialize memory persistence."""
        self.data: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._lock_managers: Dict[str, EventLoopLockManager] = {}

    def _get_lock(self, collection: str) -> asyncio.Lock:
        """
        Get or create a lock for a collection.

        Ensures correct event loop binding.
        """
        if collection not in self._lock_managers:
            self._lock_managers[collection] = EventLoopLockManager()
        return self._lock_managers[collection].get_lock()

    async def store(self, collection: str, key: str, data: Dict[str, Any]) -> None:
        """Store data in memory."""
        lock = self._get_lock(collection)
        async with lock:
            if collection not in self.data:
                self.data[collection] = {}

            self.data[collection][key] = {
                **data,
                "_stored_at": datetime.now(timezone.utc).isoformat(),
            }

    async def retrieve(self, collection: str, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve data from memory."""
        lock = self._get_lock(collection)
        async with lock:
            if collection not in self.data:
                return None

            return self.data[collection].get(key)

    async def query(
        self,
        collection: str,
        filter_func: Optional[Any] = None,
        limit: int = 100,
        where: Optional[Dict[str, Any]] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query data from memory."""
        lock = self._get_lock(collection)
        async with lock:
            if collection not in self.data:
                return []

            # Convert to list of items
            items = [{"_key": k, **v} for k, v in self.data[collection].items()]

            # Apply the prefilter and the predicate before any limit
            if where:
                items = [item for item in items if _matches_where(item, where)]
            if filter_func:
                items = [item for item in items if filter_func(item)]

            # Sort by stored time (newest first; key breaks ties), then page
            items.sort(
                key=lambda x: (x.get("_stored_at", ""), x.get("_key", "")),
                reverse=True,
            )

            return items[offset : offset + limit]

    async def delete(self, collection: str, key: str) -> None:
        """Delete data from memory."""
        lock = self._get_lock(collection)
        async with lock:
            if collection in self.data and key in self.data[collection]:
                del self.data[collection][key]

    async def clear_old(self, collection: str, days: int) -> int:
        """Clear old data from memory."""
        lock = self._get_lock(collection)
        async with lock:
            if collection not in self.data:
                return 0

            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            removed_count = 0

            # Create new dict with non-old entries
            new_data = {}
            for key, value in self.data[collection].items():
                stored_at_str = value.get("_stored_at")
                if stored_at_str:
                    stored_at = datetime.fromisoformat(stored_at_str)
                    if stored_at >= cutoff:
                        new_data[key] = value
                    else:
                        removed_count += 1
                else:
                    # Keep entries without timestamp
                    new_data[key] = value

            self.data[collection] = new_data
            return removed_count


#: Fields a ``task_metadata`` row is expected to carry. ``task_id`` is
#: required (it's the storage key); the rest are stored verbatim when
#: present so a caller that omits one simply gets ``None`` back on read,
#: matching how every existing writer of this collection already behaves.
TASK_METADATA_FIELDS = (
    "task_id",
    "name",
    "description",
    "priority",
    "estimated_hours",
    "labels",
    "dependencies",
    "project_id",
    "source_type",
    "created_at",
)


async def write_task_metadata(db_path: "str | Path", row: Dict[str, Any]) -> None:
    """Write one ``task_metadata`` row -- the sanctioned path (issue #741).

    Cato renders its task universe from the ``task_metadata``
    persistence collection. Tasks created through the NLP pipeline land
    there as a side effect of ``create_tasks_on_board``
    (``src/integrations/nlp_base.py``); anything that creates tasks by
    another route -- a directly-seeded board, a future intake adapter
    -- was previously left to open its own ``SQLitePersistence`` and
    write the collection by hand. This is that call, factored out so
    there is one place that owns the write.

    Parameters
    ----------
    db_path : str | Path
        Marcus's persistence database (normally
        ``<marcus root>/data/marcus.db``).
    row : Dict[str, Any]
        The metadata row, keyed by ``task_id``. Stored exactly as
        given; see ``TASK_METADATA_FIELDS`` for the fields Cato
        expects.

    Raises
    ------
    KeyError
        ``row`` has no ``task_id``.
    """
    if "task_id" not in row:
        raise KeyError("task_id")
    await write_task_metadata_rows(db_path, [row])


async def write_task_metadata_rows(
    db_path: "str | Path", rows: List[Dict[str, Any]]
) -> None:
    """Write several ``task_metadata`` rows; see ``write_task_metadata``.

    Parameters
    ----------
    db_path : str | Path
        Marcus's persistence database.
    rows : List[Dict[str, Any]]
        One metadata row per task, each keyed by its own ``task_id``.
    """
    store = Persistence(backend=SQLitePersistence(db_path=Path(db_path)))
    for row in rows:
        await store.store("task_metadata", str(row["task_id"]), row)
