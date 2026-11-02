"""Durable worker registrations (ADR-0012 obligation O1).

A worker registers once and then pulls chores for hours. Until this
store existed, ``agent_status`` and ``agent_project_map`` lived only in
memory, so a Marcus restart turned every live worker into an unknown
agent whose polls failed forever (the lease store, by contrast, was
always rehydrated). This module is the durable half of the fix: one
JSON file, ``registrations.json``, beside ``assignments.json``, holding
what ``register_agent`` was told plus ``registered_at`` and
``last_seen``.

Two things are deliberately **not** here:

- ``current_tasks``. Who holds what is the lease store's business; the
  server rebuilds a rehydrated worker's current task from the live
  leases so there is exactly one authority on ownership.
- Eviction policy. The store records ``last_seen``; the server decides
  when a silent registration has expired.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import aiofiles

from src.core.event_loop_utils import EventLoopLockManager
from src.core.models import WorkerStatus

logger = logging.getLogger(__name__)

REGISTRATIONS_FILE = "registrations.json"


def record_from_worker(
    worker: WorkerStatus,
    project_id: str,
    registered_at: datetime,
    last_seen: datetime,
) -> Dict[str, Any]:
    """Build the on-disk record for a registered worker.

    Parameters
    ----------
    worker : WorkerStatus
        The in-memory registration.
    project_id : str
        The project the worker registered for (``agent_project_map``).
    registered_at : datetime
        When the worker (last) registered, timezone-aware.
    last_seen : datetime
        When Marcus last heard from the worker, timezone-aware.

    Returns
    -------
    Dict[str, Any]
        A JSON-serializable record. ``current_tasks`` is left out on
        purpose; see the module docstring.
    """
    return {
        "agent_id": worker.worker_id,
        "name": worker.name,
        "role": worker.role,
        "email": worker.email,
        "skills": list(worker.skills or []),
        "capacity": worker.capacity,
        "availability": dict(worker.availability or {}),
        "performance_score": worker.performance_score,
        "completed_tasks_count": worker.completed_tasks_count,
        "vendor": worker.vendor,
        "principal": worker.principal,
        "project_id": project_id,
        "registered_at": registered_at.isoformat(),
        "last_seen": last_seen.isoformat(),
    }


def worker_from_record(record: Dict[str, Any]) -> WorkerStatus:
    """Rebuild the in-memory registration from an on-disk record.

    Parameters
    ----------
    record : Dict[str, Any]
        A record written by :func:`record_from_worker`.

    Returns
    -------
    WorkerStatus
        The registration, with ``current_tasks`` empty; the server
        attaches the live lease's task afterwards.
    """
    return WorkerStatus(
        worker_id=record["agent_id"],
        name=record.get("name", record["agent_id"]),
        role=record.get("role", ""),
        email=record.get("email"),
        current_tasks=[],
        completed_tasks_count=int(record.get("completed_tasks_count", 0)),
        capacity=int(record.get("capacity", 40)),
        skills=list(record.get("skills") or []),
        availability=dict(record.get("availability") or {}),
        performance_score=float(record.get("performance_score", 1.0)),
        vendor=record.get("vendor", "") or "",
        principal=record.get("principal", "agent") or "agent",
    )


def _parse_when(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp from a record, tolerating absence and naivety."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class RegistrationPersistence:
    """The roster file: every registration Marcus has accepted.

    Parameters
    ----------
    storage_dir : Path
        Directory holding ``registrations.json`` (created if absent). The
        server passes the assignment store's directory so a restart with
        the same data directory sees both leases and registrations.
    touch_write_interval_seconds : float
        Minimum spacing between disk writes caused by :meth:`touch` for
        one agent. Every tool call is a touch, so without debouncing a
        chatty worker would rewrite the roster on each call.
    """

    def __init__(
        self,
        storage_dir: Path,
        touch_write_interval_seconds: float = 60.0,
    ) -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.registrations_file = self.storage_dir / REGISTRATIONS_FILE
        self.touch_write_interval_seconds = touch_write_interval_seconds
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._loaded = False
        self._last_written_seen: Dict[str, datetime] = {}
        self._lock_manager = EventLoopLockManager()

    @property
    def lock(self) -> asyncio.Lock:
        """The lock for the current event loop."""
        return self._lock_manager.get_lock()

    async def load(self) -> Dict[str, Dict[str, Any]]:
        """Read the roster from disk into the cache and return a copy.

        Returns
        -------
        Dict[str, Dict[str, Any]]
            ``agent_id -> record``. A missing file is an empty roster; an
            unreadable one is logged loudly and treated as empty, and the
            next write replaces it.
        """
        async with self.lock:
            self._cache = {}
            self._last_written_seen = {}
            if self.registrations_file.exists():
                try:
                    async with aiofiles.open(
                        self.registrations_file, "r", encoding="utf-8"
                    ) as f:
                        content = await f.read()
                    data = json.loads(content) if content else {}
                    if isinstance(data, dict):
                        self._cache = {
                            str(k): v for k, v in data.items() if isinstance(v, dict)
                        }
                except (json.JSONDecodeError, OSError) as error:
                    logger.error(
                        f"Could not read worker registrations from "
                        f"{self.registrations_file}: {error}. Starting with an "
                        "empty roster; live workers must re-register."
                    )
            for agent_id, record in self._cache.items():
                seen = _parse_when(record.get("last_seen"))
                if seen is not None:
                    self._last_written_seen[agent_id] = seen
            self._loaded = True
            return {k: dict(v) for k, v in self._cache.items()}

    async def _ensure_loaded_locked(self) -> None:
        """Read the file once before the first write (caller holds the lock).

        Writing an unread cache would truncate the roster to the one
        record in memory, the same hazard ``task_epochs.json`` guards
        against in the assignment store.
        """
        if self._loaded:
            return
        if self.registrations_file.exists():
            try:
                async with aiofiles.open(
                    self.registrations_file, "r", encoding="utf-8"
                ) as f:
                    content = await f.read()
                data = json.loads(content) if content else {}
                if isinstance(data, dict):
                    self._cache = {
                        str(k): v for k, v in data.items() if isinstance(v, dict)
                    }
            except (json.JSONDecodeError, OSError) as error:
                logger.error(
                    f"Could not read worker registrations from "
                    f"{self.registrations_file} before writing: {error}. "
                    "The file will be replaced."
                )
        self._loaded = True

    async def save(self, record: Dict[str, Any]) -> None:
        """Insert or replace one worker's record and write the roster.

        Parameters
        ----------
        record : Dict[str, Any]
            A record from :func:`record_from_worker`; keyed by its
            ``agent_id``.
        """
        async with self.lock:
            await self._ensure_loaded_locked()
            agent_id = str(record["agent_id"])
            self._cache[agent_id] = dict(record)
            seen = _parse_when(record.get("last_seen"))
            if seen is not None:
                self._last_written_seen[agent_id] = seen
            await self._write()

    async def update_fields(self, agent_id: str, **fields: Any) -> None:
        """Merge fields into an existing record; unknown agents are ignored.

        Parameters
        ----------
        agent_id : str
            The worker whose record changes.
        **fields : Any
            Field values to set (for example ``completed_tasks_count``).
        """
        async with self.lock:
            await self._ensure_loaded_locked()
            record = self._cache.get(agent_id)
            if record is None:
                return
            record.update(fields)
            await self._write()

    async def remove(self, agent_id: str) -> None:
        """Drop one worker's record; a missing record is not an error.

        Parameters
        ----------
        agent_id : str
            The worker to forget.
        """
        async with self.lock:
            await self._ensure_loaded_locked()
            if self._cache.pop(agent_id, None) is None:
                return
            self._last_written_seen.pop(agent_id, None)
            await self._write()

    async def remove_if_stale(
        self, agent_id: str, cutoff: datetime
    ) -> Optional[Dict[str, Any]]:
        """Drop a record only if it is still stale once the lock is held.

        The eviction decision and the removal happen under one lock, so a
        re-registration that lands in between (its ``save`` refreshes
        ``last_seen``) wins and nothing is removed (Codex P2 on PR #772).

        Parameters
        ----------
        agent_id : str
            The worker whose record may be removed.
        cutoff : datetime
            Records whose ``last_seen`` is before this moment are stale.

        Returns
        -------
        Optional[Dict[str, Any]]
            The removed record, so a caller that loses a race after the
            removal can put it back with :meth:`save`; None when nothing
            was removed.
        """
        async with self.lock:
            await self._ensure_loaded_locked()
            record = self._cache.get(agent_id)
            if record is None:
                return None
            seen = _parse_when(record.get("last_seen"))
            if seen is not None and seen >= cutoff:
                return None
            removed = self._cache.pop(agent_id)
            self._last_written_seen.pop(agent_id, None)
            await self._write()
            return dict(removed)

    async def touch(self, agent_id: str, now: Optional[datetime] = None) -> bool:
        """Record that Marcus heard from a worker.

        Parameters
        ----------
        agent_id : str
            The worker that just made a tool call.
        now : Optional[datetime]
            The moment of the call (defaults to the current UTC time).

        Returns
        -------
        bool
            True when the roster was written to disk, False when the
            touch was absorbed by the debounce or the agent is unknown.
            In memory the latest touch always wins.
        """
        when = now or datetime.now(timezone.utc)
        async with self.lock:
            await self._ensure_loaded_locked()
            record = self._cache.get(agent_id)
            if record is None:
                return False
            record["last_seen"] = when.isoformat()
            last_written = self._last_written_seen.get(agent_id)
            if (
                last_written is not None
                and (when - last_written).total_seconds()
                < self.touch_write_interval_seconds
            ):
                return False
            self._last_written_seen[agent_id] = when
            await self._write()
            return True

    def last_seen(self, agent_id: str) -> Optional[datetime]:
        """Return when Marcus last heard from a worker, or None if unknown.

        Parameters
        ----------
        agent_id : str
            The worker in question.

        Returns
        -------
        Optional[datetime]
            The latest touch (written or not), timezone-aware.
        """
        record = self._cache.get(agent_id)
        if record is None:
            return None
        return _parse_when(record.get("last_seen"))

    def records(self) -> Dict[str, Dict[str, Any]]:
        """Return a copy of the cached roster.

        Returns
        -------
        Dict[str, Dict[str, Any]]
            ``agent_id -> record`` as last loaded or written.
        """
        return {k: dict(v) for k, v in self._cache.items()}

    async def _write(self) -> None:
        """Write the cache to disk atomically (caller holds the lock)."""
        temp_file = self.registrations_file.with_suffix(".json.tmp")
        try:
            async with aiofiles.open(temp_file, "w", encoding="utf-8") as f:
                await f.write(json.dumps(self._cache, indent=2))
            temp_file.replace(self.registrations_file)
        except Exception:
            if temp_file.exists():
                temp_file.unlink()
            raise
