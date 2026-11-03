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
from typing import Any, Dict, List, Optional

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
        # ADR-0012 D11 epoch high-water marks, keyed by TASK rather than
        # agent so they survive the assignment removal that recovery
        # performs.
        self.task_epochs_file = self.storage_dir / "task_epochs.json"
        # Issue #730 baselines, keyed by TASK and agent so they survive the
        # assignment removal that a completion performs. The merge of a
        # task's commit range needs the baseline again on a retry, and by
        # then the assignment row is gone.
        self.task_baselines_file = self.storage_dir / "task_baselines.json"
        # ADR-0012 D11 refused completions, keyed by TASK. Kept in
        # Marcus's own state rather than on the board: only the sqlite
        # provider round-trips ``source_context``, and a board refresh
        # replaces cached task objects, so a board-only record is lost
        # on Planka, GitHub and Linear (Codex P2 on PR #794).
        self.refused_attempts_file = self.storage_dir / "refused_attempts.json"

        # In-memory cache
        self._assignments_cache: Dict[str, Dict[str, Any]] = {}
        self._task_epochs_cache: Dict[str, int] = {}
        # Whether task_epochs.json has been read into the cache. A write
        # that publishes an unread cache truncates the file to whatever
        # single mark happens to be in memory, destroying every other
        # task's high-water mark and re-opening the fence for all of them.
        self._task_epochs_loaded: bool = False
        self._task_baselines_cache: Dict[str, Dict[str, str]] = {}
        self._task_baselines_loaded: bool = False
        self._refused_attempts_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._refused_attempts_loaded: bool = False
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
        self,
        worker_id: str,
        task_id: str,
        fields: Dict[str, Any],
        *,
        create_if_absent: bool = True,
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
            create_if_absent
                When True (default) a missing record is created — required
                for a freshly-claimed lease, since ``create_lease`` runs
                before the assignment row exists.

                Callers pass **False** when the write may be racing a
                removal. A renewal or merge-conflict extension already in
                flight when a completion, recovery, or unassignment calls
                ``remove_assignment`` would otherwise resurrect the
                released row, leaving a TODO task falsely assigned on disk
                and rehydrating a stale lease after a restart. With False,
                a missing record — or a record now pointing at a different
                task — is left untouched.
        """
        async with self.lock:
            existing = self._assignments_cache.get(worker_id)
            if existing is not None and existing.get("task_id") == task_id:
                record: Dict[str, Any] = dict(existing)
            elif create_if_absent:
                record = {
                    "assigned_at": datetime.now(timezone.utc).isoformat(),
                    "task_data": {},
                }
            else:
                # Released or reassigned: never resurrect or clobber.
                return

            record["task_id"] = task_id
            record.update(fields)

            self._assignments_cache[worker_id] = record
            await self._write_assignments()

    async def record_task_epoch(self, task_id: str, lease_epoch: int) -> None:
        """
        Record the highest ADR-0012 D11 epoch ever issued for a task.

        Stored in ``task_epochs.json``, **deliberately separate from the
        assignment rows**. Assignments are keyed by agent and are deleted
        by ``remove_assignment`` on recovery — and a recovered task is
        the only kind that ever has a displaced agent. Seeding the
        high-water mark from assignment rows therefore lost it for
        exactly the tasks that need it, so after a restart the next
        claimant was reissued epoch 1 and the displaced agent's stale
        token compared equal. The fence failed open in precisely the
        scenario it exists for.

        The value is monotonic: a lower epoch never overwrites a higher
        one.

        Parameters
        ----------
            task_id
                The task whose epoch is being recorded.
            lease_epoch
                The epoch just issued for it.
        """
        async with self.lock:
            # Read-before-write. ``create_lease`` can run before anything
            # calls ``load_task_epochs``, so an unguarded write would
            # publish a cache holding only this one mark.
            self._ensure_task_epochs_loaded()

            current = self._task_epochs_cache.get(task_id, 0)
            if lease_epoch <= current:
                return
            self._task_epochs_cache[task_id] = lease_epoch
            await self._write_task_epochs()

    async def load_task_epochs(self) -> Dict[str, int]:
        """
        Load the per-task epoch high-water marks from disk.

        Returns
        -------
            Mapping of task_id to the highest epoch ever issued for it.
            Empty when the store does not exist yet.
        """
        async with self.lock:
            if not self.task_epochs_file.exists():
                self._task_epochs_loaded = True
                return {}
            try:
                async with aiofiles.open(self.task_epochs_file, "r") as f:
                    content = await f.read()
                    self._task_epochs_cache = json.loads(content) if content else {}
                    self._task_epochs_loaded = True
                    return dict(self._task_epochs_cache)
            except (json.JSONDecodeError, IOError) as e:
                # A corrupt or unreadable store must not stop startup, but
                # losing it silently would re-open the fence. Log loudly.
                logger.error(
                    "Failed to load task epoch high-water marks from "
                    f"{self.task_epochs_file}: {e}. D11 fencing will "
                    "reissue epochs from 1 for previously-recovered "
                    "tasks until the store is rewritten."
                )
                return {}

    def _ensure_task_epochs_loaded(self) -> None:
        """Read the epoch store into the cache once. Caller holds the lock.

        Synchronous on purpose: it is called from inside the lock on the
        write path, and the file is small. Failure leaves the cache empty
        but marks it loaded, so a corrupt store degrades to "reissue from
        the marks we can see" rather than looping on every write.
        """
        if self._task_epochs_loaded:
            return
        self._task_epochs_loaded = True
        if not self.task_epochs_file.exists():
            return
        try:
            content = self.task_epochs_file.read_text()
            loaded = json.loads(content) if content else {}
            if isinstance(loaded, dict):
                # Existing marks win over anything already in memory, then
                # in-memory values are re-applied by the caller's update.
                merged = dict(loaded)
                merged.update(self._task_epochs_cache)
                self._task_epochs_cache = merged
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.error(
                f"Failed to read task epoch high-water marks from "
                f"{self.task_epochs_file}: {e}. D11 fencing may reissue "
                "epochs for previously-recovered tasks."
            )

    async def _write_task_epochs(self) -> None:
        """Persist the epoch high-water marks. Caller holds the lock.

        Written via temp file + atomic rename, matching
        ``_write_assignments``. An in-place write that is interrupted
        leaves a truncated or empty file, and an empty epoch store means
        every previously-recovered task reissues from epoch 1 — the fence
        fails open across the whole board.
        """
        tmp_path = self.task_epochs_file.with_suffix(".json.tmp")
        async with aiofiles.open(tmp_path, "w") as f:
            await f.write(json.dumps(self._task_epochs_cache, indent=2))
        tmp_path.replace(self.task_epochs_file)

    async def record_task_baseline(
        self, task_id: str, worker_id: str, baseline_commit: str
    ) -> None:
        """
        Record the commit a worker's task started from (issue #730).

        Stored in ``task_baselines.json``, **deliberately separate from
        the assignment rows**, for the same reason the epoch marks are:
        a completion deletes the assignment row before the merge runs, so
        a worker that is sent back to resolve a merge conflict and then
        reports again would find no baseline and Marcus would fall back
        to merging its whole branch — carrying work from a task whose
        completion it had refused.

        Keyed by task and then by worker, so two workers that held the
        same task (a recovery) each keep their own starting point.

        Parameters
        ----------
            task_id
                The task the baseline belongs to.
            worker_id
                The worker that claimed it.
            baseline_commit
                The worker's branch tip at claim time.
        """
        async with self.lock:
            self._ensure_task_baselines_loaded()
            for_task = dict(self._task_baselines_cache.get(task_id) or {})
            for_task[worker_id] = baseline_commit
            self._task_baselines_cache[task_id] = for_task
            await self._write_task_baselines()

    async def get_task_baseline(self, task_id: str, worker_id: str) -> Optional[str]:
        """
        Read the commit a worker's task started from (issue #730).

        Parameters
        ----------
            task_id
                The task to look up.
            worker_id
                The worker whose starting point is wanted.

        Returns
        -------
            The baseline commit, or None when none was recorded.
        """
        async with self.lock:
            self._ensure_task_baselines_loaded()
            baseline = (self._task_baselines_cache.get(task_id) or {}).get(worker_id)
            return str(baseline) if baseline else None

    def _ensure_task_baselines_loaded(self) -> None:
        """Read the baseline store into the cache once. Caller holds the lock.

        Synchronous for the same reason as the epoch store's loader: it
        runs inside the lock on the write path and the file is small. A
        corrupt store degrades to "no baseline", which merges the whole
        branch — logged loudly, because that is the behaviour issue #730
        exists to avoid.
        """
        if self._task_baselines_loaded:
            return
        self._task_baselines_loaded = True
        if not self.task_baselines_file.exists():
            return
        try:
            content = self.task_baselines_file.read_text()
            loaded = json.loads(content) if content else {}
            if isinstance(loaded, dict):
                merged: Dict[str, Dict[str, str]] = {
                    task: dict(agents)
                    for task, agents in loaded.items()
                    if isinstance(agents, dict)
                }
                for task, agents in self._task_baselines_cache.items():
                    merged.setdefault(task, {}).update(agents)
                self._task_baselines_cache = merged
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.error(
                f"Failed to read task baselines from "
                f"{self.task_baselines_file}: {e}. A completion that needs "
                "its baseline will merge the whole branch instead of the "
                "task's commit range (issue #730)."
            )

    async def _write_task_baselines(self) -> None:
        """Persist the baselines. Caller holds the lock.

        Temp file plus atomic rename, matching the other two stores: an
        interrupted in-place write leaves a truncated file, and an empty
        baseline store silently widens every later merge.
        """
        tmp_path = self.task_baselines_file.with_suffix(".json.tmp")
        async with aiofiles.open(tmp_path, "w") as f:
            await f.write(json.dumps(self._task_baselines_cache, indent=2))
        tmp_path.replace(self.task_baselines_file)

    async def record_refused_attempt(
        self, task_id: str, attempt: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Record a completion the ADR-0012 D11 fence refused.

        Kept here, in Marcus's own state, because the board cannot be
        relied on to hold it: only the sqlite provider round-trips
        ``source_context``, and ``refresh_project_state`` replaces
        cached task objects with freshly hydrated ones, so a board-only
        record disappears on the next refresh. The reconciliation card
        is opened from this record when a completion is finally
        accepted, which can be much later.

        Deduplicated by ``(agent_id, lease_epoch)``, so a displaced
        session repeating its report adds nothing while a second
        displaced session is kept.

        Parameters
        ----------
            task_id
                The contested task.
            attempt
                The refusal: agent id, branch, lease epoch, timestamp.

        Returns
        -------
            Every refused attempt recorded for this task, oldest first.
        """
        async with self.lock:
            self._ensure_refused_attempts_loaded()
            recorded = [dict(a) for a in self._refused_attempts_cache.get(task_id, [])]
            key = (attempt.get("agent_id"), attempt.get("lease_epoch"))
            if not any(
                (a.get("agent_id"), a.get("lease_epoch")) == key for a in recorded
            ):
                recorded.append(dict(attempt))
            self._refused_attempts_cache[task_id] = recorded
            await self._write_refused_attempts()
            return [dict(a) for a in recorded]

    async def mark_refused_attempts_reconciled(
        self, task_id: str, card_id: str, attempts: List[Dict[str, Any]]
    ) -> None:
        """
        Note which refused completions a reconciliation card covers.

        Without this, a displaced session repeating its report after its
        card has been completed opens a second card for the same
        refusal, re-litigating a settled decision and possibly replacing
        its outcome. A genuinely new refusal is still uncovered, so it
        still gets a card.

        Parameters
        ----------
            task_id
                The contested task.
            card_id
                The reconciliation card now covering these attempts.
            attempts
                The attempts the card names, matched on agent and epoch.
        """
        covered = {(a.get("agent_id"), a.get("lease_epoch")) for a in attempts}
        async with self.lock:
            self._ensure_refused_attempts_loaded()
            stored = self._refused_attempts_cache.get(task_id)
            if not stored:
                return
            for record in stored:
                if (record.get("agent_id"), record.get("lease_epoch")) in covered:
                    record["reconciled_by"] = card_id
            await self._write_refused_attempts()

    async def get_refused_attempts(self, task_id: str) -> List[Dict[str, Any]]:
        """
        Read the completions the fence refused for a task.

        Parameters
        ----------
            task_id
                The task to look up.

        Returns
        -------
            Every refused attempt, oldest first; empty when there were
            none.
        """
        async with self.lock:
            self._ensure_refused_attempts_loaded()
            return [dict(a) for a in self._refused_attempts_cache.get(task_id, [])]

    def _ensure_refused_attempts_loaded(self) -> None:
        """Read the refusal store into the cache once. Caller holds the lock.

        Synchronous for the same reason as the other two stores: it runs
        inside the lock on the write path and the file is small. A
        corrupt store degrades to "no refusals", which means a
        reconciliation card is not opened — logged loudly, because the
        refused work would then sit on its branch unmentioned.
        """
        if self._refused_attempts_loaded:
            return
        self._refused_attempts_loaded = True
        if not self.refused_attempts_file.exists():
            return
        try:
            content = self.refused_attempts_file.read_text()
            loaded = json.loads(content) if content else {}
            if isinstance(loaded, dict):
                merged: Dict[str, List[Dict[str, Any]]] = {
                    task: [dict(a) for a in attempts]
                    for task, attempts in loaded.items()
                    if isinstance(attempts, list)
                }
                for task, attempts in self._refused_attempts_cache.items():
                    merged.setdefault(task, []).extend(attempts)
                self._refused_attempts_cache = merged
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.error(
                f"Failed to read refused completions from "
                f"{self.refused_attempts_file}: {e}. A contested task may "
                "complete without a reconciliation card, leaving the "
                "refused work on its branch unmentioned (ADR-0012 D11)."
            )

    async def _write_refused_attempts(self) -> None:
        """Persist the refusals. Caller holds the lock.

        Temp file plus atomic rename, matching the other stores.
        """
        tmp_path = self.refused_attempts_file.with_suffix(".json.tmp")
        async with aiofiles.open(tmp_path, "w") as f:
            await f.write(json.dumps(self._refused_attempts_cache, indent=2))
        tmp_path.replace(self.refused_attempts_file)

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
