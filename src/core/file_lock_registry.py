"""
File-level write-lock registry (issue #206 MVP, v0.3.9).

The registry tracks which file paths each in-progress task is
authorized to write to. Marcus's ``request_next_task`` uses it as a
pre-assignment filter: a task whose declared files conflict with a
currently-held lock is skipped, so the agent that requested work is
offered a different task instead. When a task ends (DONE or BLOCKED)
its locks are released and the files become claimable again.

Background — why a separate registry instead of reusing the lease
system in ``src/core/assignment_lease.py``:

- The lease system arbitrates at the TASK level — "no two agents on
  the same task." That is a different invariant from "no two agents
  writing to the same file across two different tasks", which is what
  this registry enforces. The two systems are complementary.
- The lease system is concerned with timing (lease expiry, renewal,
  escalation). The registry is concerned with topology (which file is
  owned by which task right now). Conflating them would couple two
  independent concerns.

Architectural lineage — *Beyond Text-Passing* (Coeva, ICLR 2026
MALGAI workshop) names the concept ``Baton`` ("the right to perform a
privileged operation on shared state"). Marcus calls it a file lock
for consistency with the existing lease vocabulary, but the
semantics are the same: exactly one holder per substrate region,
reads are free, every state change has an accountable agent.

Scope notes (full design at ``docs/design/206-file-lock-registry-mvp.md``):

- In-memory; one registry per Marcus server. Restart loses state.
  Persistence is V2 work tied to federation (#206 v0.7.0).
- Single-process; concurrency is asyncio-only. No inter-process
  coordination yet.
- Substrate region is the file path (no directory or schema
  granularity yet).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileLockHolder:
    """Record of who currently owns the write authority for one file.

    Frozen so the registry's view of "who holds X" cannot be mutated
    by callers — only via the explicit ``try_acquire`` / ``release``
    methods. That keeps the asyncio.Lock the only place state
    changes.

    Attributes
    ----------
    task_id : str
        The in-progress task that holds the lock. Released when the
        task moves to DONE or BLOCKED.
    agent_id : str
        The agent currently working that task. Used for telemetry and
        the rejection message a competing caller receives.
    acquired_at : datetime
        UTC timestamp of acquisition. Used by future lock-leak
        detection (V2) and audit trail.
    """

    task_id: str
    agent_id: str
    acquired_at: datetime


@dataclass
class AcquireResult:
    """Structured return shape from :meth:`FileLockRegistry.try_acquire`.

    On success ``blocker`` and ``blocker_file`` are None. On failure
    they identify which lock blocked the acquisition so the caller
    can log the contention and tell the agent which task/file was in
    the way.

    Attributes
    ----------
    success : bool
        True when ALL requested files were acquired atomically.
    blocker : FileLockHolder, optional
        The holder of the file that blocked acquisition. Populated
        only on failure.
    blocker_file : str, optional
        The specific file path that was already held. Populated only
        on failure.
    """

    success: bool
    blocker: Optional[FileLockHolder] = None
    blocker_file: Optional[str] = None


class FileLockRegistry:
    """In-process file-level lock registry for #206 MVP.

    Tracks which file paths are write-authorized by which in-progress
    task. Marcus's ``request_next_task`` filters out tasks whose
    declared files conflict with current holders.

    Concurrency model
    -----------------
    All mutating operations (``try_acquire``, ``release``) take an
    ``asyncio.Lock`` so two coroutines cannot both see a file as free
    and both insert holders. Read-only methods (``held_by``,
    ``any_held``, ``snapshot``) are safe to call without the lock
    because Python's GIL makes dict reads atomic — the worst case is
    that the reader sees a slightly-stale view, which is fine for
    telemetry. Any decision that ACTS on a read (e.g., assigning a
    task) must be paired with a subsequent ``try_acquire`` to
    commit the claim atomically.

    Lifetime
    --------
    One registry per Marcus server instance. State is in-memory and
    lost on restart — acceptable for MVP given Marcus restarts are
    rare; on restart the next ``request_next_task`` re-acquires for
    any still-in-progress task that needs it.
    """

    def __init__(self) -> None:
        # Map of (project_id, file_path) -> FileLockHolder. A file
        # appears here iff exactly one task currently holds it. Keying
        # by (project_id, path) instead of bare path scopes locks per
        # project — two concurrently-running projects' tasks targeting
        # the same relative path (e.g. ``src/main.py``) don't collide.
        # Kaia's #658 review flagged this as the foot-gun to fix before
        # federation (#206 v0.7.0) makes it a refactor instead of a
        # tweak. Callers that omit ``project_id`` get the empty-string
        # namespace, which is fine for single-project tests but unsafe
        # in production — the wiring in task.py always passes the
        # agent's registered project_id.
        self._holders: Dict[Tuple[str, str], FileLockHolder] = {}
        # Serializes mutating operations. Read-only methods (held_by,
        # any_held, snapshot) skip the lock — see class docstring.
        self._lock: asyncio.Lock = asyncio.Lock()

    async def try_acquire(
        self,
        task_id: str,
        agent_id: str,
        files: List[str],
        project_id: str = "",
    ) -> AcquireResult:
        """Atomically claim every file in ``files`` for ``task_id``.

        Acquisition is all-or-nothing: if any requested file is already
        held by another task in the SAME project, no file from this
        request is claimed and the registry state is unchanged. Locks
        in OTHER projects are invisible — two projects with tasks
        targeting the same relative path do not collide.

        Parameters
        ----------
        task_id : str
            Task that will own the locks. Multiple files acquired in
            one call all share this owner; release with the same
            ``task_id`` frees them in one shot. Task IDs are assumed
            globally unique across projects (Marcus assigns them
            from a single source), so release does not need a
            ``project_id``.
        agent_id : str
            Agent currently working ``task_id``. Stored on each
            holder for telemetry and audit.
        files : list of str
            File paths to acquire. Empty list returns a success
            result without taking the lock (fast path for tasks
            with no declared files).
        project_id : str, optional
            Project namespace. Empty string is the default namespace —
            safe for single-project tests but unsafe in production
            where two projects might run concurrently. The wiring in
            ``task.py`` always passes the agent's registered
            project_id from ``state.agent_project_map``.

        Returns
        -------
        AcquireResult
            ``success=True`` on full acquisition. On contention
            ``success=False`` with ``blocker`` and ``blocker_file``
            populated.
        """
        if not files:
            # Empty-list fast path. A task with no declared_files
            # (feature-based path or legacy task) acquires nothing
            # and must not be blocked from assignment.
            return AcquireResult(success=True)

        async with self._lock:
            # Conflict scan first — must read every requested file
            # before mutating any of them, to preserve the
            # all-or-nothing invariant.
            for file_path in files:
                existing = self._holders.get((project_id, file_path))
                if existing is not None and existing.task_id != task_id:
                    logger.debug(
                        "FileLockRegistry: task %s blocked on %s "
                        "(project %s, held by task %s / agent %s)",
                        task_id,
                        file_path,
                        project_id or "<default>",
                        existing.task_id,
                        existing.agent_id,
                    )
                    return AcquireResult(
                        success=False,
                        blocker=existing,
                        blocker_file=file_path,
                    )

            # No conflicts — claim all files atomically.
            now = datetime.now(timezone.utc)
            holder = FileLockHolder(
                task_id=task_id,
                agent_id=agent_id,
                acquired_at=now,
            )
            for file_path in files:
                self._holders[(project_id, file_path)] = holder

            logger.info(
                "FileLockRegistry: task %s acquired %d file(s) " "in project %s: %s",
                task_id,
                len(files),
                project_id or "<default>",
                files,
            )
            return AcquireResult(success=True)

    async def release(self, task_id: str) -> int:
        """Release every file currently held by ``task_id``.

        Idempotent: releasing a task that holds no locks returns 0
        and does not raise. Both the DONE-path and the BLOCKED-path
        of ``report_task_progress`` call this; if a third path also
        fires it remains a no-op.

        Release does not take a ``project_id`` because task IDs are
        globally unique across projects in Marcus — iterating to find
        every key whose holder matches ``task_id`` is correct
        regardless of which project the locks were acquired in.

        Parameters
        ----------
        task_id : str
            Task whose locks to release.

        Returns
        -------
        int
            Count of files released (0 if the task held none).
        """
        async with self._lock:
            to_release = [
                key
                for key, holder in self._holders.items()
                if holder.task_id == task_id
            ]
            for key in to_release:
                del self._holders[key]

            if to_release:
                logger.info(
                    "FileLockRegistry: task %s released %d file(s): %s",
                    task_id,
                    len(to_release),
                    [path for _proj, path in to_release],
                )
            return len(to_release)

    def held_by(self, file_path: str, project_id: str = "") -> Optional[FileLockHolder]:
        """Return the current holder of ``(project_id, file_path)`` or ``None``.

        Read-only; safe to call without acquiring the asyncio.Lock.
        Any caller that ACTS on the answer (e.g., assigns a task)
        must follow up with ``try_acquire`` to commit the decision
        atomically — between this read and the act, a competing
        coroutine could claim the file.

        Parameters
        ----------
        file_path : str
            File path within the project namespace.
        project_id : str, optional
            Project namespace. Default empty string matches the
            default namespace used by ``try_acquire`` when no
            ``project_id`` is passed.
        """
        return self._holders.get((project_id, file_path))

    def any_held(self, files: List[str], project_id: str = "") -> bool:
        """Report whether any path in ``files`` is held in ``project_id``.

        Used by ``request_next_task`` as a pre-filter before the more
        expensive ``try_acquire`` call: if any declared file is held,
        the task is skipped without contending for the lock. Returns
        True iff at least one path in ``files`` has a current holder
        within the given project namespace.
        """
        if not files:
            return False
        return any((project_id, path) in self._holders for path in files)

    def snapshot(self) -> Dict[Tuple[str, str], FileLockHolder]:
        """Return an independent copy of the (project_id, path) -> holder map.

        For telemetry and debugging. Mutating the returned dict does
        not affect registry state — the registry returns a shallow
        copy and ``FileLockHolder`` itself is frozen.
        """
        return dict(self._holders)
