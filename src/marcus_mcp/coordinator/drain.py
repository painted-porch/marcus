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
it. Skills, eligibility and file locks are therefore not consulted here,
but the gates that no choice of worker can satisfy are: the dependency
gate (strict for ordinary tasks, hard-only for subtasks, exactly as the
two selectors apply them) and the board-wide README and phase gates,
through the very function the assignment filter uses.
In flight is judged conservatively: a live lease, an ``IN_PROGRESS``
card, a task being assigned right now, a persisted assignment, or a
``TODO`` card that still names an assignee (an orphan the reconciler
will clear) all mean "wait, then ask again". This leans on ADR-0012 D3:
a dead worker's lease is recovered within the silence timeout, so its
task returns to claimable and the drain never fires over abandoned work.

One ``BLOCKED`` card is not a dead lane: a completion whose branch failed
to merge into ``main`` is marked ``BLOCKED`` with a ``merge_conflict``
record (#651) while its own agent is told to resolve the conflict and
re-report. That agent holds no lease any more (the lease was released on
completion), so the card looks abandoned to the rules above. It is in
flight for as long as the record is younger than the silence timeout, the
same dial D3 uses to decide a quiet worker is dead; a live run drained
early on exactly this card the first time it happened.
"""

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from src.core.lease_invariants import DEFAULT_SILENCE_TIMEOUT_MINUTES
from src.core.models import Task, TaskStatus
from src.core.task_claimability import (
    build_slug_to_id,
    deps_allow_claim,
    resolve_dependency_ids,
)
from src.marcus_mcp.coordinator.subtask_assignment import (
    _are_dependencies_satisfied as hard_dependencies_satisfied,
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


def _subtasks_enabled(state: Any) -> bool:
    """Whether the subtask selector is in play, mirroring the assignment path."""
    if getattr(state, "subtask_manager", None) is None:
        return False
    try:
        from src.config.settings import Settings

        return bool(Settings().is_subtasks_enabled())
    except Exception:  # noqa: BLE001 - unreadable settings: assume the default
        return True


def _board_wide_gates(
    candidates: List[Task],
    scoped: List[Task],
    tasks_by_id: Dict[str, Task],
    state: Any,
    project_id: str,
) -> List[Task]:
    """Apply the assignment filter's board-wide gates (README, phases)."""
    if not candidates:
        return []
    # Imported here: tools.task imports this module.
    from src.marcus_mcp.tools.task import apply_board_wide_gates

    return apply_board_wide_gates(
        list(candidates), scoped, tasks_by_id, state, project_id=project_id
    )


def _is_decomposed_parent(task: Task, state: Any, scoped: List[Task]) -> bool:
    manager = getattr(state, "subtask_manager", None)
    if manager is None:
        return False
    try:
        return bool(manager.has_subtasks(task.id, scoped))
    except Exception:  # noqa: BLE001 - a broken manager must not break the verdict
        return any(t.is_subtask and t.parent_task_id == task.id for t in scoped)


def _conflict_resolution_in_flight(task: Task, lease_manager: Any) -> bool:
    """Return True when a BLOCKED card is a merge conflict its agent is still fixing.

    The merge-failure path stamps ``source_context["merge_conflict"]`` with
    ``blocked_at`` and tells the completing agent to resolve and re-report.
    Until the silence timeout has passed since that stamp, the card counts
    as work in flight; after it, the agent is presumed gone and the card
    is a dead lane like any other BLOCKED task.

    Parameters
    ----------
    task : Task
        A task whose status is BLOCKED.
    lease_manager : Any
        The server's lease manager, read for ``silence_timeout_minutes``;
        the D3 default applies when it is absent.

    Returns
    -------
    bool
        True when the conflict record is present, well-formed, and fresh.
    """
    record = (task.source_context or {}).get("merge_conflict")
    if not isinstance(record, dict):
        return False
    stamped = _parse_timestamp(record.get("blocked_at"))
    if stamped is None:
        return False
    minutes = getattr(lease_manager, "silence_timeout_minutes", None)
    window = timedelta(minutes=DEFAULT_SILENCE_TIMEOUT_MINUTES)
    if isinstance(minutes, (int, float)) and minutes > 0:
        window = timedelta(minutes=float(minutes))
    return datetime.now(timezone.utc) - stamped < window


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 stamp into an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    subtask_path = _subtasks_enabled(state)

    def _ordinary_deps_satisfied(task: Task) -> bool:
        resolved = resolve_dependency_ids(task, slug_to_id)
        claimable, _ = deps_allow_claim(task, resolved, tasks_by_id)
        return claimable

    done = blocked = in_flight = unclaimable = 0
    candidates: List[Task] = []  # ordinary tasks that still face the board gates
    subtask_claimable = 0
    for task in scoped:
        if task.status == TaskStatus.DONE:
            done += 1
            continue
        if task.status == TaskStatus.BLOCKED:
            if _conflict_resolution_in_flight(task, lease_manager):
                in_flight += 1
            else:
                blocked += 1
            continue
        if task.status == TaskStatus.DECLINED:
            # ADR-0013 K5: terminal, like BLOCKED, but never a merge
            # conflict a worker is mid-resolving -- a verifier declined
            # it; nobody is coming back to it the way a lease holder
            # might return to a conflict. Counted as blocked, never
            # in-flight, so the drain verdict agrees with
            # deps_allow_claim's own strict gate that this can never
            # become claimable again.
            blocked += 1
            continue
        # A decomposed parent is a container: the selector moves it to
        # IN_PROGRESS when its first child is claimed and completes it when
        # the last child is done, so its own status says nothing about live
        # work. Its liveness is its children's (Codex P2 on PR #773).
        if _is_decomposed_parent(task, state, scoped):
            unclaimable += 1
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
        # Only TODO remains: TaskStatus is exactly TODO, IN_PROGRESS, DONE,
        # BLOCKED and DECLINED, and the other four are handled above.
        if task.is_subtask and task.parent_task_id and subtask_path:
            # The subtask selector's rules: parent not DONE, the parent's and
            # the subtask's HARD dependencies done, soft ones skipped. No
            # slug resolution and no phase gate there, so none here.
            parent = tasks_by_id.get(task.parent_task_id)
            if parent is None or parent.status == TaskStatus.DONE:
                unclaimable += 1
                continue
            if not hard_dependencies_satisfied(parent, scoped):
                unclaimable += 1
                continue
            if not hard_dependencies_satisfied(task, scoped):
                unclaimable += 1
                continue
            subtask_claimable += 1
            continue
        if not _ordinary_deps_satisfied(task):
            unclaimable += 1
            continue
        if task.is_subtask and task.parent_task_id:
            parent = tasks_by_id.get(task.parent_task_id)
            if parent is not None:
                parent_deps = resolve_dependency_ids(parent, slug_to_id)
                if not all(dep in done_ids for dep in parent_deps):
                    unclaimable += 1
                    continue
        candidates.append(task)

    gated = _board_wide_gates(candidates, scoped, tasks_by_id, state, project_id)
    claimable = subtask_claimable + len(gated)
    unclaimable += len(candidates) - len(gated)

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
