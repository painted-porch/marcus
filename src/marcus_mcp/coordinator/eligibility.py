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
from typing import Any, Dict, List, Tuple

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
# export ran after a restart. Each first-seen refusal is written through
# the persistence layer under a deterministic key; repeats update the
# same record; a fresh process resumes the count from it. The write is
# scheduled on the running loop because both assignment entry points
# call the filter synchronously, and it is best-effort: a state without
# a persistence layer, or no running loop, keeps today's in-memory path.
REFUSALS_COLLECTION = "eligibility_refusals"


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


async def _persist_refusal(persistence: Any, record: Dict[str, Any]) -> None:
    """Write one refusal through persistence, continuing any prior count.

    Parameters
    ----------
    persistence : Any
        The server's persistence layer (``store`` and ``retrieve``).
    record : Dict[str, Any]
        The in-memory refusal record; its ``count`` and
        ``first_refused_at`` are raised to the persisted values when a
        prior process already recorded this refusal, so memory and disk
        agree afterwards.
    """
    key = refusal_key(record["agent_id"], record["task_id"], record["reason"])
    # Carry a prior process's count into this record exactly once. The
    # flag is set before the first await so concurrent writes for the
    # same record (one per poll) cannot each add the prior count.
    carry = not record.get("_carried")
    record["_carried"] = True
    prior = await persistence.retrieve(REFUSALS_COLLECTION, key) if carry else None
    if prior:
        record["count"] = int(record["count"]) + int(prior.get("count", 0))
        record["first_refused_at"] = min(
            str(prior.get("first_refused_at", record["first_refused_at"])),
            record["first_refused_at"],
        )
    await persistence.store(
        REFUSALS_COLLECTION,
        key,
        {k: v for k, v in record.items() if not str(k).startswith("_")},
    )


def _schedule_refusal_write(state: Any, record: Dict[str, Any]) -> None:
    """Schedule the persistence write for a refusal, if it can be done.

    Parameters
    ----------
    state : Any
        Marcus server state; needs an awaitable ``persistence`` and a
        running event loop, otherwise nothing is scheduled.
    record : Dict[str, Any]
        The in-memory refusal record.
    """
    persistence = getattr(state, "persistence", None)
    store = getattr(persistence, "store", None)
    retrieve = getattr(persistence, "retrieve", None)
    if not (
        inspect.iscoroutinefunction(store) and inspect.iscoroutinefunction(retrieve)
    ):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    pending = getattr(state, "_refusal_writes", None)
    if not isinstance(pending, set):
        pending = set()
        try:
            state._refusal_writes = pending
        except AttributeError:
            pass
    write = loop.create_task(_persist_refusal(persistence, record))
    pending.add(write)
    write.add_done_callback(pending.discard)
    write.add_done_callback(_report_write_failure)


def _report_write_failure(write: "asyncio.Task[None]") -> None:
    """Log a failed refusal write; the in-memory record still stands."""
    if write.cancelled():
        return
    error = write.exception()
    if error is not None:
        logger.warning("[eligibility] refusal write failed: %s", error)


async def drain_refusal_writes(state: Any) -> None:
    """Wait for every scheduled refusal write on ``state`` to finish.

    Parameters
    ----------
    state : Any
        Marcus server state. Used by tests and by an orderly shutdown so
        no refusal is lost between the offer and the write.
    """
    pending = getattr(state, "_refusal_writes", None)
    if isinstance(pending, set) and pending:
        await asyncio.gather(*list(pending), return_exceptions=True)


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
