"""The drain signal: a board-computed "go home" for workers (epic #706).

Under the session model a worker registers once and pulls chores in a
loop. The only "you may exit" signal used to be ``should_exit`` from the
experiment monitor, which ADR-0012 D10 deletes; without a replacement a
drained board leaves every worker polling until someone kills it. The
replacement is decided by the server, not by a model remembering to
call a tool: a project is **drained** when it has **no claimable task and
no work in flight**.

Claimable is judged board-wide, not per agent: a task another worker
could take keeps the board alive even if the caller is not eligible for
it. Skills, eligibility and file locks are therefore not consulted here.
In flight is judged conservatively: a live lease, an ``IN_PROGRESS``
card, a task being assigned right now, a persisted assignment, or a
``TODO`` card that still names an assignee (an orphan the reconciler
will clear) all mean "wait, then ask again". This leans on ADR-0012 D3:
a dead worker's lease is recovered within the silence timeout, so its
task returns to claimable and the drain never fires over abandoned work.
"""

import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Set

from src.core.models import Task, TaskStatus
from src.core.task_claimability import (
    build_slug_to_id,
    deps_allow_claim,
    resolve_dependency_ids,
)

logger = logging.getLogger(__name__)


@dataclass
class DrainVerdict:
    """What the board says about a project's remaining work.

    Parameters
    ----------
    drained : bool
        True when nothing is claimable and nothing is in flight.
    reason : str
        ``complete`` (every task DONE) or ``blocked`` (unfinished work that
        can never become claimable) when drained; ``claimable``,
        ``in_flight`` or ``empty`` when not.
    tasks_total : int
        Tasks in the project.
    tasks_done, tasks_blocked, tasks_claimable, tasks_in_flight : int
        Tasks in each state.
    tasks_unclaimable : int
        TODO work whose dependency gate cannot pass, or a decomposed parent.
    """

    drained: bool
    reason: str
    tasks_total: int
    tasks_done: int
    tasks_blocked: int
    tasks_claimable: int
    tasks_in_flight: int
    tasks_unclaimable: int

    def to_dict(self) -> Dict[str, Any]:
        """Return the verdict as plain data for an MCP response."""
        return asdict(self)


def _scope(tasks: List[Task], project_id: str) -> List[Task]:
    """Mirror the assignment filter's scoping: legacy tasks (no project) are shared."""
    return [t for t in tasks if t.project_id is None or t.project_id == project_id]


def _is_decomposed_parent(task: Task, state: Any, scoped: List[Task]) -> bool:
    manager = getattr(state, "subtask_manager", None)
    if manager is None:
        return False
    try:
        return bool(manager.has_subtasks(task.id, scoped))
    except Exception:  # noqa: BLE001 - a broken manager must not break the verdict
        return any(t.is_subtask and t.parent_task_id == task.id for t in scoped)


async def compute_board_drain(state: Any, project_id: str) -> DrainVerdict:
    """Decide whether ``project_id`` has run out of work.

    Parameters
    ----------
    state : Any
        Marcus server state (``project_tasks``, ``lease_manager``,
        ``tasks_being_assigned``, ``assignment_persistence``,
        ``subtask_manager``).
    project_id : str
        The calling agent's registered project.

    Returns
    -------
    DrainVerdict
        See the class; ``drained`` is the signal, the rest is the evidence.
    """
    scoped = _scope(list(getattr(state, "project_tasks", None) or []), project_id)
    if not scoped:
        return DrainVerdict(False, "empty", 0, 0, 0, 0, 0, 0)

    scoped_ids = {t.id for t in scoped}
    tasks_by_id = {t.id: t for t in scoped}

    leased: Set[str] = set()
    lease_manager = getattr(state, "lease_manager", None)
    if lease_manager is not None:
        for task_id in getattr(lease_manager, "active_leases", None) or {}:
            if task_id in scoped_ids:
                leased.add(task_id)

    reserved: Set[str] = set(getattr(state, "tasks_being_assigned", None) or set())
    persisted: Set[str] = set()
    persistence = getattr(state, "assignment_persistence", None)
    if persistence is not None:
        try:
            persisted = set(await persistence.get_all_assigned_task_ids())
        except Exception as error:  # noqa: BLE001 - treat as unknown, not as idle
            logger.warning(f"[drain] could not read persisted assignments: {error}")

    slug_to_id = build_slug_to_id(scoped)
    done_ids = {t.id for t in scoped if t.status == TaskStatus.DONE}

    def _deps_satisfied(task: Task) -> bool:
        resolved = resolve_dependency_ids(task, slug_to_id)
        claimable, _ = deps_allow_claim(task, resolved, tasks_by_id)
        return claimable

    done = blocked = claimable = in_flight = unclaimable = 0
    for task in scoped:
        if task.status == TaskStatus.DONE:
            done += 1
            continue
        if (
            task.id in leased
            or task.id in reserved
            or task.id in persisted
            or task.status == TaskStatus.IN_PROGRESS
            or (task.status == TaskStatus.TODO and bool(task.assigned_to))
        ):
            in_flight += 1
            continue
        if task.status == TaskStatus.BLOCKED:
            blocked += 1
            continue
        # Only TODO remains: TaskStatus is exactly TODO, IN_PROGRESS, DONE,
        # BLOCKED, and the first three are handled above.
        if _is_decomposed_parent(task, state, scoped):
            unclaimable += 1
            continue
        if not _deps_satisfied(task):
            unclaimable += 1
            continue
        if task.is_subtask and task.parent_task_id:
            parent = tasks_by_id.get(task.parent_task_id)
            if parent is not None:
                parent_deps = resolve_dependency_ids(parent, slug_to_id)
                if not all(dep in done_ids for dep in parent_deps):
                    unclaimable += 1
                    continue
        claimable += 1

    total = len(scoped)
    if in_flight:
        reason, drained = "in_flight", False
    elif claimable:
        reason, drained = "claimable", False
    elif done == total:
        reason, drained = "complete", True
    else:
        reason, drained = "blocked", True
    return DrainVerdict(
        drained=drained,
        reason=reason,
        tasks_total=total,
        tasks_done=done,
        tasks_blocked=blocked,
        tasks_claimable=claimable,
        tasks_in_flight=in_flight,
        tasks_unclaimable=unclaimable,
    )
