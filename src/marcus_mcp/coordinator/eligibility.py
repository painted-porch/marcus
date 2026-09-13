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

import logging
from datetime import datetime, timezone
from typing import Any, List, Tuple

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
    and appended to ``state.eligibility_refusals`` so the audit bundle
    (step 8) can show who was refused and why.

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
    eligible: List[Task] = []
    for task in tasks:
        ok, reason = is_eligible(agent, task, state)
        if ok:
            eligible.append(task)
            continue

        logger.info(
            "[eligibility] refused %s task %s (%s) to %s: %s",
            task.task_type,
            task.id,
            task.name,
            agent.worker_id,
            reason,
        )
        refusals = getattr(state, "eligibility_refusals", None)
        if not isinstance(refusals, list):
            refusals = []
            try:
                state.eligibility_refusals = refusals
            except AttributeError:
                # A read-only state still gets the log line above;
                # refusal recording is best-effort by design.
                continue
        refusals.append(
            {
                "agent_id": agent.worker_id,
                "task_id": task.id,
                "task_type": task.task_type,
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
    return eligible
