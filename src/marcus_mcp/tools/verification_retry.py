"""Bounded verification retry and the declined outcome.

A verify task that finds a wrong figure has to do something about it.
Marcus re-opens the task that authored the claim, hands it the named
failures, and counts the round. When the rounds are used up the case
is declined: the failing claims are recorded and everything
downstream is blocked, so nothing ships on the strength of "we
tried".

Two failure modes shape this design.

Unbounded retry is the first. Given enough attempts something will
pass, and a loop that keeps going until it does has stopped measuring
correctness. The bound is the answer, and it is Marcus's to enforce
rather than the worker's to respect.

Proceeding anyway is the second. A loop that gives up after its last
round and carries on has converted "verified" into "we tried". A
declined case with named failures is actionable; a silently completed
one is not.

The retry also has to clear the previous round's claim off the verify
task. The evidence handoff lets a dependent's existing keys win, so a
verify task that kept its inputs would re-check the superseded claim
and pass. Clearing them is what makes a second round a real second
look.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.core.models import Task, TaskStatus

logger = logging.getLogger(__name__)

VERIFY_TASK_TYPE = "verify"
FIND_TASK_TYPE = "find"

#: How many times a claim author may be sent back before the case is
#: declined. Three matches the workflows this stands in for, and the
#: number is deliberately small: a report that needed three rounds is
#: a quality signal, not a success.
MAX_VERIFICATION_ROUNDS = 3

ROUND_KEY = "verification_round"
FAILED_CLAIMS_KEY = "failed_claims"
DECLINED_KEY = "verification_declined"


def verification_round(task: Task) -> int:
    """Read how many rounds a task has already been through.

    Parameters
    ----------
    task : Task
        The claim author's task.

    Returns
    -------
    int
        Completed rounds; zero before the first retry.
    """
    try:
        return int((task.inputs or {}).get(ROUND_KEY, 0))
    except (TypeError, ValueError):
        return 0


def _verdict_failed(evidence: Dict[str, Any]) -> bool:
    """Decide whether a verify task's evidence reports a failure.

    Parameters
    ----------
    evidence : Dict[str, Any]
        The verify task's schema-validated evidence.

    Returns
    -------
    bool
        True when the verdict is anything but a clean pass.
    """
    if not evidence:
        return False
    verdict = str(evidence.get("verdict", "")).strip().lower()
    if verdict in ("failed", "declined", "rejected"):
        return True
    return bool(evidence.get(FAILED_CLAIMS_KEY))


def _authors_of(verify_task: Task, tasks: List[Task]) -> List[Task]:
    """Find the tasks whose claims this verify task checked.

    Parameters
    ----------
    verify_task : Task
        The verify task that just closed.
    tasks : List[Task]
        Every task on the board.

    Returns
    -------
    List[Task]
        The find tasks it depends on.
    """
    deps = set(verify_task.dependencies or [])
    return [t for t in tasks if t.id in deps and t.task_type == FIND_TASK_TYPE]


def _dependents_of(task_ids: List[str], tasks: List[Task]) -> List[Task]:
    """Collect every task downstream of the given ones, transitively.

    Parameters
    ----------
    task_ids : List[str]
        Ids whose dependents are wanted.
    tasks : List[Task]
        Every task on the board.

    Returns
    -------
    List[Task]
        The transitive dependents.
    """
    found: Dict[str, Task] = {}
    frontier = set(task_ids)
    while frontier:
        nxt = set()
        for candidate in tasks:
            if candidate.id in found or candidate.id in task_ids:
                continue
            if frontier & set(candidate.dependencies or []):
                found[candidate.id] = candidate
                nxt.add(candidate.id)
        frontier = nxt
    return list(found.values())


async def _write(state: Any, task_id: str, data: Dict[str, Any]) -> None:
    """Persist a task update, tolerating a kanban failure.

    Parameters
    ----------
    state : Any
        Marcus server state holding ``kanban_client``.
    task_id : str
        Task to update.
    data : Dict[str, Any]
        Fields to write.
    """
    client = getattr(state, "kanban_client", None)
    if client is None:
        return
    try:
        await client.update_task(task_id, data)
    except Exception as err:  # noqa: BLE001 - memory copy still applies
        logger.warning(
            "Verification retry: kanban write failed for %s (%s); the "
            "in-memory change is applied and a refresh may lose it.",
            task_id,
            err,
        )


async def apply_verification_outcome(
    verify_task: Task,
    evidence: Dict[str, Any],
    state: Any,
    max_rounds: int = MAX_VERIFICATION_ROUNDS,
) -> Optional[Dict[str, Any]]:
    """Act on a verify task's verdict: retry the author, or decline.

    Parameters
    ----------
    verify_task : Task
        The verify task that just closed.
    evidence : Dict[str, Any]
        Its schema-validated evidence, carrying the verdict and any
        failed claims.
    state : Any
        Marcus server state with ``project_tasks`` and
        ``kanban_client``.
    max_rounds : int
        How many times the author may be sent back.

    Returns
    -------
    Optional[Dict[str, Any]]
        ``None`` when nothing is needed, otherwise a dict whose
        ``action`` is ``retry`` or ``declined``.
    """
    if verify_task.task_type != VERIFY_TASK_TYPE:
        return None
    if not _verdict_failed(evidence):
        return None

    tasks = list(getattr(state, "project_tasks", None) or [])
    authors = _authors_of(verify_task, tasks)
    if not authors:
        logger.warning(
            "Verification retry: verify task %s reported failures but "
            "depends on no find task; nothing to re-open.",
            verify_task.id,
        )
        return None

    failed = list(evidence.get(FAILED_CLAIMS_KEY) or [])
    rounds_done = max(verification_round(a) for a in authors)

    if rounds_done >= max_rounds:
        return await _decline(verify_task, authors, failed, rounds_done, tasks, state)

    next_round = rounds_done + 1
    for author in authors:
        author.inputs = {
            **(author.inputs or {}),
            ROUND_KEY: next_round,
            FAILED_CLAIMS_KEY: failed,
        }
        author.status = TaskStatus.TODO
        author.assigned_to = None
        await _write(
            state,
            author.id,
            {
                "status": TaskStatus.TODO.value,
                "assigned_to": None,
                "inputs": author.inputs,
            },
        )

    # The previous round's claim is cleared by the kernel itself, not
    # here: #803/#807 made the evidence handoff replace the whole set
    # of keys an upstream task contributed on its next close, rather
    # than merging over it. So when the author redoes the find task
    # and closes it again, its new evidence correctly supersedes the
    # keys from this round on every dependent, including a key this
    # round wrote that the next round omits. Verified live against a
    # real request_task_redo round-trip (two rounds, a key present in
    # round 1 and dropped in round 2 is gone from the dependent
    # afterward). This module used to strip those keys itself before
    # the fix existed; that workaround is removed now that the
    # guarantee is the kernel's.
    verify_task.status = TaskStatus.TODO
    verify_task.assigned_to = None
    await _write(
        state,
        verify_task.id,
        {
            "status": TaskStatus.TODO.value,
            "assigned_to": None,
            "inputs": verify_task.inputs,
        },
    )

    logger.info(
        "Verification retry: round %d of %d for %s; %d claim(s) failed.",
        next_round,
        max_rounds,
        ", ".join(a.id for a in authors),
        len(failed),
    )
    return {
        "action": "retry",
        "round": next_round,
        "max_rounds": max_rounds,
        "verify_task_id": verify_task.id,
        "author_task_ids": [a.id for a in authors],
        FAILED_CLAIMS_KEY: failed,
    }


async def _decline(
    verify_task: Task,
    authors: List[Task],
    failed: List[Dict[str, Any]],
    rounds: int,
    tasks: List[Task],
    state: Any,
) -> Dict[str, Any]:
    """Close the case as declined and hold everything downstream.

    Parameters
    ----------
    verify_task : Task
        The verify task that exhausted its rounds.
    authors : List[Task]
        The claim authors that were sent back.
    failed : List[Dict[str, Any]]
        The claims still failing.
    rounds : int
        Rounds already spent.
    tasks : List[Task]
        Every task on the board.
    state : Any
        Marcus server state.

    Returns
    -------
    Dict[str, Any]
        The declined outcome.
    """
    marker = {
        DECLINED_KEY: True,
        "declined_rounds": rounds,
        FAILED_CLAIMS_KEY: failed,
    }
    verify_task.inputs = {**(verify_task.inputs or {}), **marker}
    await _write(
        state,
        verify_task.id,
        {"inputs": verify_task.inputs, "status": verify_task.status.value},
    )

    held = _dependents_of([verify_task.id], tasks)
    for task in held:
        task.status = TaskStatus.BLOCKED
        task.inputs = {**(task.inputs or {}), DECLINED_KEY: True}
        await _write(
            state,
            task.id,
            {"status": TaskStatus.BLOCKED.value, "inputs": task.inputs},
        )

    logger.warning(
        "Verification declined: %s exhausted %d round(s) with %d claim(s) "
        "still failing; %d downstream task(s) held.",
        verify_task.id,
        rounds,
        len(failed),
        len(held),
    )
    return {
        "action": "declined",
        "rounds": rounds,
        "verify_task_id": verify_task.id,
        "author_task_ids": [a.id for a in authors],
        FAILED_CLAIMS_KEY: failed,
        "held_task_ids": [t.id for t in held],
    }
