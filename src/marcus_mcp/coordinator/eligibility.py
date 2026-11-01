"""Eligibility rules the board applies before offering a task.

Issue #737: the board refuses to offer a task to a worker that is not
allowed to take it, so a claim is never checked by its author or by
its author's vendor, an approval can only be completed by a person,
and a person is offered approve tasks and nothing else.

Eligibility is environment design, not control: the board declines to
offer, and the worker still self-selects among what is offered, which
keeps the Bright Line Test satisfied (Invariant #1).

The rules, in full:

- A ``verify`` task is never offered to the author of the claim it
  depends on, nor to any worker whose vendor is the author's vendor.
  The author is found through ``task.dependencies`` to the DONE find
  task's ``assigned_to``; the vendor comes from the author's
  registration (``state.agent_status``).
- An ``approve`` task is offered only to a ``human`` principal.
- Every other task type is offered only to an ``agent`` principal.

Both assignment entry points (``_find_optimal_task_original_logic`` in
``src/marcus_mcp/tools/task.py`` and the coordinator's
``find_optimal_task_with_subtasks``) draw from
:func:`filter_eligible_tasks`, so a future third entry point has one
obvious function to call and refusals are logged in one place.
"""

import asyncio
import hashlib
import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from src.core.models import Task, TaskStatus, WorkerStatus

logger = logging.getLogger(__name__)

APPROVE_TYPE = "approve"
VERIFY_TYPE = "verify"


def _claim_authors(task: Task, state: Any) -> List[Tuple[str, str]]:
    """Find the authors of the claims a verify task depends on.

    Walks ``task.dependencies`` to every DONE ``find`` task and
    collects who completed it and that worker's registered vendor.

    Parameters
    ----------
    task : Task
        The verify task being offered.
    state : Any
        Marcus server state; ``project_tasks`` holds the board's tasks
        and ``agent_status`` the registered workers.

    Returns
    -------
    List[Tuple[str, str]]
        ``(author_id, author_vendor)`` pairs; the vendor is ``""``
        when the author never declared one or is no longer registered.
    """
    tasks_by_id = {t.id: t for t in getattr(state, "project_tasks", None) or []}
    agent_status = getattr(state, "agent_status", None) or {}

    authors: List[Tuple[str, str]] = []
    for dep_id in task.dependencies or []:
        dep = tasks_by_id.get(dep_id)
        if dep is None:
            continue
        if dep.task_type != "find" or dep.status != TaskStatus.DONE:
            continue
        if not dep.assigned_to:
            continue
        author = agent_status.get(dep.assigned_to)
        vendor = author.vendor if isinstance(author, WorkerStatus) else ""
        authors.append((dep.assigned_to, vendor))
    return authors


def is_eligible(agent: WorkerStatus, task: Task, state: Any) -> Tuple[bool, str]:
    """Decide whether the board may offer this task to this worker.

    Parameters
    ----------
    agent : WorkerStatus
        The worker asking for work, as registered.
    task : Task
        The candidate task.
    state : Any
        Marcus server state, used to find the claim's author for
        verify tasks.

    Returns
    -------
    Tuple[bool, str]
        ``(True, "")`` when the task may be offered; ``(False,
        reason)`` with a human-readable reason when it may not.
    """
    if task.task_type == APPROVE_TYPE:
        if agent.principal != "human":
            return False, "approve tasks may only be completed by a human principal"
        return True, ""

    if agent.principal != "agent":
        return (
            False,
            "this task type requires an agent principal; a human "
            "principal is offered approve tasks only",
        )

    if task.task_type == VERIFY_TYPE:
        for author_id, author_vendor in _claim_authors(task, state):
            if agent.worker_id == author_id:
                return False, "author of the claim this verify task checks"
            if author_vendor and agent.vendor and agent.vendor == author_vendor:
                return (
                    False,
                    f"same vendor as the claim's author ({author_vendor})",
                )

    return True, ""


def filter_eligible_tasks(
    agent: WorkerStatus, tasks: List[Task], state: Any
) -> List[Task]:
    """Filter candidate tasks to the ones this worker may be offered.

    The single choke point both assignment entry points draw from.
    Every refusal is logged with the worker, the task, and the reason,
    and recorded on ``state.eligibility_refusals`` so the audit bundle
    (step 8) can show who was refused and why.

    Workers poll roughly every 30 seconds, so the same refusal recurs
    constantly; entries are deduplicated by (agent, task, reason) with
    a count and first/last timestamps, which keeps the record bounded
    and the export legible. The first occurrence logs at INFO, repeats
    at DEBUG.

    The refused worker itself receives nothing: both entry points
    return the ordinary no-task result, so a refusal is
    indistinguishable from an empty board on the agent's side. The
    reason stays server-side by design, so an author cannot learn
    what to game.

    Parameters
    ----------
    agent : WorkerStatus
        The worker asking for work.
    tasks : List[Task]
        Candidate tasks, already filtered for status and dependencies
        by the caller.
    state : Any
        Marcus server state; grows an ``eligibility_refusals`` list on
        first refusal if it does not already have one.

    Returns
    -------
    List[Task]
        The tasks the worker is eligible for, in the caller's order.
    """
    # Judge only identities this module understands. Registration
    # always stores WorkerStatus; anything else (test doubles, legacy
    # callers reaching here untyped) passes through unchanged, the
    # same defensive posture as the vendor lookup in _claim_authors.
    # mypy sees the annotation and calls this branch unreachable; the
    # guard exists precisely for callers mypy cannot see.
    if not isinstance(agent, WorkerStatus):
        return list(tasks)  # type: ignore[unreachable]

    eligible: List[Task] = []
    for task in tasks:
        ok, reason = is_eligible(agent, task, state)
        if ok:
            eligible.append(task)
            continue
        _record_refusal(agent, task, reason, state)
    return eligible


# Refusals must survive a server restart (issue #740): the October 2026
# full run exported 200 bundles with empty refusal lists because the
# export ran after a restart. Each refusal is written through the
# persistence layer by ONE serialized writer per server state, so writes
# for a record never interleave (Codex on PR #757): the prior process's
# count is carried in exactly once, only after a successful read; repeats
# coalesce into the latest in-memory count; a failed write is retried a
# bounded number of times. Records live in a per-task collection so an
# export is an index lookup, not a scan. Both assignment entry points
# call the filter synchronously, so the write is scheduled on the running
# loop; a state without an awaitable persistence layer, or no running
# loop, keeps the in-memory path.
REFUSALS_COLLECTION = "eligibility_refusals"
# The first version (PR #757) kept every refusal in this one flat
# collection. It is read as a fallback and migrated on touch.
LEGACY_REFUSALS_COLLECTION = REFUSALS_COLLECTION
REFUSAL_WRITE_ATTEMPTS = 3
REFUSAL_RETRY_DELAY_SECONDS = 0.2


def refusals_collection(task_id: str) -> str:
    """Name the persistence collection holding one task's refusals.

    Parameters
    ----------
    task_id : str
        The task that was not offered.

    Returns
    -------
    str
        ``"eligibility_refusals:<task_id>"``; keyed per task so an
        audit export reads only the collections for its own tasks.
    """
    return f"{REFUSALS_COLLECTION}:{task_id}"


def refusal_key(agent_id: str, task_id: str, reason: str) -> str:
    """Build the persistence key for one (agent, task, reason) refusal.

    Parameters
    ----------
    agent_id : str
        The refused worker.
    task_id : str
        The task that was not offered.
    reason : str
        The human-readable reason from :func:`is_eligible`.

    Returns
    -------
    str
        ``"<task_id>:<agent_id>:<reason digest>"``; the same triple
        always maps to the same key, which is what makes repeats update
        rather than append.
    """
    digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:12]
    return f"{task_id}:{agent_id}:{digest}"


def public_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return a record without storage-private keys (those starting with ``_``).

    Parameters
    ----------
    record : Dict[str, Any]
        A refusal record, from memory or from a persistence query (which
        adds ``_key``).

    Returns
    -------
    Dict[str, Any]
        A copy holding only the audit fields.
    """
    return {k: v for k, v in record.items() if not k.startswith("_")}


class RefusalWriter:
    """One serialized writer of refusal records for one server state.

    Parameters
    ----------
    persistence : Any
        The server's persistence layer (awaitable ``store`` and
        ``retrieve``).

    Notes
    -----
    ``mark`` is synchronous and cheap: it notes that a record is dirty
    and starts the background task if it is not running. The task drains
    the dirty records in arrival order, one write at a time, so two
    writes for the same key can never race. The prior process's count is
    read and added to the in-memory record the first time a key is
    written in this process, and the key is remembered as carried only
    after that read succeeded, so a transient failure cannot disable the
    carry. A write that fails is retried up to ``REFUSAL_WRITE_ATTEMPTS``
    times; the in-memory record stands regardless.
    """

    def __init__(self, persistence: Any) -> None:
        self.persistence = persistence
        self._dirty: Dict[str, Dict[str, Any]] = {}
        self._carried: Set[str] = set()
        self._attempts: Dict[str, int] = {}
        self._task: Optional["asyncio.Task[None]"] = None

    def mark(self, record: Dict[str, Any]) -> None:
        """Note that ``record`` changed and make sure the writer is running.

        Parameters
        ----------
        record : Dict[str, Any]
            The in-memory refusal record (the same dict the server holds,
            so the write always sees the latest count).
        """
        key = refusal_key(record["agent_id"], record["task_id"], record["reason"])
        self._dirty[key] = record
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        """Write dirty records one at a time until none remain."""
        while self._dirty:
            key = next(iter(self._dirty))
            record = self._dirty.pop(key)
            try:
                await self._write(key, record)
                self._attempts.pop(key, None)
            except (
                Exception
            ) as error:  # noqa: BLE001 - persistence failures are retried, never raised
                attempts = self._attempts.get(key, 0) + 1
                if attempts >= REFUSAL_WRITE_ATTEMPTS:
                    self._attempts.pop(key, None)
                    logger.warning(
                        "[eligibility] refusal write %s gave up after %d attempts: %s",
                        key,
                        attempts,
                        error,
                    )
                    continue
                self._attempts[key] = attempts
                self._dirty.setdefault(key, record)
                logger.warning(
                    "[eligibility] refusal write for %s failed (attempt %d): %s",
                    key,
                    attempts,
                    error,
                )
                await asyncio.sleep(REFUSAL_RETRY_DELAY_SECONDS)

    async def _write(self, key: str, record: Dict[str, Any]) -> None:
        """Carry the prior count once, then store the record.

        The prior is read from the per-task collection, or from the first
        version's flat collection when the per-task one has nothing; a
        legacy row is deleted once the per-task record exists.
        """
        collection = refusals_collection(record["task_id"])
        legacy_row = None
        if key not in self._carried:
            prior = await self.persistence.retrieve(collection, key)
            if prior is None:
                legacy_row = await self.persistence.retrieve(
                    LEGACY_REFUSALS_COLLECTION, key
                )
                prior = legacy_row
            if prior:
                record["count"] = int(record["count"]) + int(prior.get("count") or 0)
                prior_first = prior.get("first_refused_at")
                if prior_first:
                    record["first_refused_at"] = min(
                        str(prior_first), str(record["first_refused_at"])
                    )
            self._carried.add(key)
        await self.persistence.store(collection, key, public_fields(record))
        if legacy_row is not None:
            delete = getattr(self.persistence, "delete", None)
            if inspect.iscoroutinefunction(delete):
                await delete(LEGACY_REFUSALS_COLLECTION, key)

    async def drain(self, timeout: Optional[float] = None) -> None:
        """Wait for the writer to finish, up to ``timeout`` seconds.

        Parameters
        ----------
        timeout : Optional[float]
            Seconds to wait; None waits until every dirty record is written.
        """
        task = self._task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "[eligibility] %d refusal write(s) still pending after %.1fs",
                len(self._dirty) + 1,
                timeout or 0.0,
            )


def _writer_for(state: Any) -> Optional[RefusalWriter]:
    """Return the state's refusal writer, creating it when persistence allows.

    Parameters
    ----------
    state : Any
        Marcus server state.

    Returns
    -------
    Optional[RefusalWriter]
        None when the state has no awaitable persistence layer.
    """
    persistence = getattr(state, "persistence", None)
    store = getattr(persistence, "store", None)
    retrieve = getattr(persistence, "retrieve", None)
    if not (
        inspect.iscoroutinefunction(store) and inspect.iscoroutinefunction(retrieve)
    ):
        return None
    writer = getattr(state, "_refusal_writer", None)
    if not isinstance(writer, RefusalWriter) or writer.persistence is not persistence:
        writer = RefusalWriter(persistence)
        try:
            state._refusal_writer = writer
        except AttributeError:
            return None
    return writer


def _schedule_refusal_write(state: Any, record: Dict[str, Any]) -> None:
    """Hand a changed refusal record to the state's writer, if it can run.

    Parameters
    ----------
    state : Any
        Marcus server state; needs an awaitable ``persistence`` and a
        running event loop, otherwise nothing is scheduled.
    record : Dict[str, Any]
        The in-memory refusal record.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    writer = _writer_for(state)
    if writer is not None:
        writer.mark(record)


async def drain_refusal_writes(state: Any, timeout: Optional[float] = None) -> None:
    """Wait for the state's pending refusal writes to finish.

    Parameters
    ----------
    state : Any
        Marcus server state. Called by the server's orderly shutdown and
        by tests, so no refusal is lost between the offer and the write.
    timeout : Optional[float]
        Seconds to wait; None waits for every pending write.
    """
    writer = getattr(state, "_refusal_writer", None)
    if isinstance(writer, RefusalWriter):
        await writer.drain(timeout)


def _record_refusal(agent: WorkerStatus, task: Task, reason: str, state: Any) -> None:
    """Log one refusal and dedup it into ``state.eligibility_refusals``.

    Parameters
    ----------
    agent : WorkerStatus
        The refused worker.
    task : Task
        The task that was not offered.
    reason : str
        The human-readable reason from :func:`is_eligible`.
    state : Any
        Marcus server state holding the refusal record.
    """
    refusals = getattr(state, "eligibility_refusals", None)
    if not isinstance(refusals, list):
        refusals = []
        try:
            state.eligibility_refusals = refusals
        except AttributeError:
            # A read-only state still gets the log line below;
            # refusal recording is best-effort by design.
            refusals = None

    now_iso = datetime.now(timezone.utc).isoformat()
    existing = None
    if refusals is not None:
        existing = next(
            (
                r
                for r in refusals
                if r["agent_id"] == agent.worker_id
                and r["task_id"] == task.id
                and r["reason"] == reason
            ),
            None,
        )

    log = logger.debug if existing else logger.info
    log(
        "[eligibility] refused %s task %s (%s) to %s: %s",
        task.task_type,
        task.id,
        task.name,
        agent.worker_id,
        reason,
    )

    if refusals is None:
        return
    if existing:
        existing["count"] += 1
        existing["last_refused_at"] = now_iso
        _schedule_refusal_write(state, existing)
        return
    record = {
        "agent_id": agent.worker_id,
        "task_id": task.id,
        "task_type": task.task_type,
        "reason": reason,
        "count": 1,
        "first_refused_at": now_iso,
        "last_refused_at": now_iso,
    }
    refusals.append(record)
    _schedule_refusal_write(state, record)
