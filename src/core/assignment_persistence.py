"""
Assignment persistence layer for Marcus.

This module provides persistent storage for task assignments to prevent
duplicate assignments across Marcus restarts and multiple instances.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import aiofiles

from src.core.event_loop_utils import EventLoopLockManager

logger = logging.getLogger(__name__)


class AssignmentPersistence:
    """Handles persistent storage of task assignments."""

    def __init__(self, storage_dir: Optional[Path] = None):
        """
        Initialize the assignment persistence layer.

        Parameters
        ----------
            storage_dir
                Directory for storing assignment data.
                        Defaults to ./data/assignments/
        """
        if storage_dir is None:
            # Use absolute path to ensure it works regardless of working directory
            marcus_root = Path(__file__).parent.parent.parent
            storage_dir = marcus_root / "data" / "assignments"
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        self.assignments_file = self.storage_dir / "assignments.json"
        self.lock_file = self.storage_dir / ".assignments.lock"

        # In-memory cache
        self._assignments_cache: Dict[str, Dict[str, Any]] = {}
        self._lock_manager = EventLoopLockManager()

    @property
    def lock(self) -> asyncio.Lock:
        """Get lock for the current event loop."""
        return self._lock_manager.get_lock()

    async def save_assignment(
        self, worker_id: str, task_id: str, task_data: Dict[str, Any]
    ) -> None:
        """
        Save a task assignment persistently.

        Parameters
        ----------
            worker_id
                ID of the worker assigned to the task.
            task_id
                ID of the task being assigned.
            task_data
                Additional task information to store.

        Notes
        -----
        When the worker is already assigned to **this same task**, fields
        written onto the record by other subsystems are preserved and
        ``assigned_at`` keeps its original value.

        This method previously replaced the record wholesale on every
        call, which silently discarded everything
        ``AssignmentLeaseManager._persist_lease`` had just written —
        ``lease_expires``, ``lease_renewed_at``, ``renewal_count``,
        ``progress_percentage``, ``update_timestamps``,
        ``merge_conflict_extensions`` — and reset ``assigned_at`` to now
        on every progress report. Because ``load_active_leases`` defaults
        a missing ``lease_expires`` to *now*, every lease rehydrated
        after a restart was born already expiring. Found while adding the
        ADR-0012 D11 fencing token, which has the same durability
        requirement.

        A *different* task_id means a genuinely new assignment, so the
        record is rebuilt and ``assigned_at`` is refreshed — otherwise a
        reassigned worker would inherit the previous task's start time.
        """
        async with self.lock:
            existing = self._assignments_cache.get(worker_id)
            if existing is not None and existing.get("task_id") == task_id:
                record: Dict[str, Any] = dict(existing)
            else:
                record = {"assigned_at": datetime.now(timezone.utc).isoformat()}

            record["task_id"] = task_id
            record["task_data"] = task_data
            record.setdefault("assigned_at", datetime.now(timezone.utc).isoformat())

            # Update cache
            self._assignments_cache[worker_id] = record

            # Persist to disk
            await self._write_assignments()

    async def update_assignment_fields(
        self, worker_id: str, task_id: str, fields: Dict[str, Any]
    ) -> None:
        """
        Merge extra fields onto a worker's assignment record.

        Used by :class:`~src.core.assignment_lease.AssignmentLeaseManager`
        to durably record lease state (expiry, renewal count, progress,
        and the ADR-0012 D11 ``lease_epoch``) alongside the assignment.

        The record is **created when absent**. A lease is created before
        the assignment row exists, so a method that skipped missing
        records would silently drop the fencing token for every
        freshly-claimed task.

        Parameters
        ----------
            worker_id
                ID of the worker whose record should be updated.
            task_id
                Task the fields describe. A mismatch against the stored
                record means a new assignment, so stale lease fields from
                the previous task are dropped rather than carried over.
            fields
                Key/value pairs merged onto the record.
        """
        async with self.lock:
            existing = self._assignments_cache.get(worker_id)
            if existing is not None and existing.get("task_id") == task_id:
                record: Dict[str, Any] = dict(existing)
            else:
                record = {
                    "assigned_at": datetime.now(timezone.utc).isoformat(),
                    "task_data": {},
                }

            record["task_id"] = task_id
            record.update(fields)

            self._assignments_cache[worker_id] = record
            await self._write_assignments()

    async def remove_assignment(self, worker_id: str) -> None:
        """
        Remove a task assignment (e.g., when task is completed).

        Parameters
        ----------
            worker_id
                ID of the worker to remove assignment for.
        """
        async with self.lock:
            if worker_id in self._assignments_cache:
                del self._assignments_cache[worker_id]
                await self._write_assignments()

    async def get_assignment(self, worker_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the current assignment for a worker.

        Parameters
        ----------
            worker_id
                ID of the worker.

        Returns
        -------
            Assignment data or None if no assignment exists
        """
        async with self.lock:
            return self._assignments_cache.get(worker_id)

    async def get_all_assigned_task_ids(self) -> set[str]:
        """
        Get all currently assigned task IDs.

        Returns
        -------
            Set of task IDs that are currently assigned
        """
        async with self.lock:
            return {
                assignment["task_id"] for assignment in self._assignments_cache.values()
            }

    async def load_assignments(self) -> Dict[str, Dict[str, Any]]:
        """
        Load assignments from persistent storage.

        Returns
        -------
            Dictionary of worker_id -> assignment data
        """
        async with self.lock:
            if not self.assignments_file.exists():
                return {}

            try:
                async with aiofiles.open(self.assignments_file, "r") as f:
                    content = await f.read()
                    self._assignments_cache = json.loads(content) if content else {}
                    return self._assignments_cache
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading assignments: {e}")
                # Return empty dict on error to allow recovery
                return {}

    async def _write_assignments(self) -> None:
        """Write assignments to disk atomically."""
        temp_file = self.assignments_file.with_suffix(".tmp")

        try:
            async with aiofiles.open(temp_file, "w") as f:
                await f.write(json.dumps(self._assignments_cache, indent=2))

            # Atomic rename
            temp_file.replace(self.assignments_file)

        except Exception as e:
            logger.error(f"Error writing assignments: {e}")
            if temp_file.exists():
                temp_file.unlink()
            raise

    async def is_task_assigned(self, task_id: str) -> bool:
        """
        Check if a task is currently assigned to any worker.

        Parameters
        ----------
            task_id
                ID of the task to check.

        Returns
        -------
            True if the task is assigned, False otherwise
        """
        async with self.lock:
            for assignment in self._assignments_cache.values():
                if assignment["task_id"] == task_id:
                    return True
            return False

    async def get_worker_for_task(self, task_id: str) -> Optional[str]:
        """
        Get the worker ID assigned to a specific task.

        Parameters
        ----------
            task_id
                ID of the task.

        Returns
        -------
            Worker ID or None if task is not assigned
        """
        async with self.lock:
            for worker_id, assignment in self._assignments_cache.items():
                if assignment["task_id"] == task_id:
                    return worker_id
            return None

    async def cleanup(self) -> None:
        """Clean up any resources and persist final state."""
        try:
            # Persist any cached data one final time
            if self._assignments_cache:
                await self._write_assignments()

            logger.info("Assignment persistence cleanup completed")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
