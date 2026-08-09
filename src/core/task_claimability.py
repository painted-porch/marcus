"""
Dependency-gate claimability rules shared by selection and gridlock detection.

Issue #629: the terminal integration-verification task depends on every implementer
task. The availability filter used to require every dependency to be
strictly DONE, so a single BLOCKED upstream froze the integration task
forever and the project hung at "almost done" with no recovery path.
The gridlock detector duplicated the same strict rule, so it would also
mis-classify that state as gridlock.

This module is the single home for the rule so the selector
(``src/marcus_mcp/tools/task.py``) and the detector
(``src/core/gridlock_detector.py``) cannot drift apart. It lives in
``src/core`` because the detector must not import from the MCP tools
layer.
"""

from typing import Dict, List, Set, Tuple

from src.core.models import Task, TaskStatus

# Best-effort integration claim floor (issue #629). For the integration
# task ONLY, a dependency set is satisfiable when every upstream is
# SETTLED (DONE, or BLOCKED — a terminal state after #667/#651) and at
# least this fraction of them is DONE. Below the floor the project is
# too broken for integration to be meaningful; the circuit breaker /
# gridlock paths own that case.
BEST_EFFORT_MIN_DONE_FRACTION = 0.8

SETTLED_FOR_BEST_EFFORT = {TaskStatus.DONE, TaskStatus.BLOCKED}


def is_integration_task(task: Task) -> bool:
    """Detect whether a task is an integration verification task.

    Integration verification tasks are produced by
    ``IntegrationTaskGenerator.create_integration_task`` and carry the
    ``"type:integration"`` label as a stable type marker.

    Parameters
    ----------
    task : Task
        Task to inspect.

    Returns
    -------
    bool
        True if the task carries ``type:integration`` in its labels.
        Defensive: returns False if labels is missing or not a sequence.
    """
    labels = getattr(task, "labels", None)
    if not labels:
        return False
    try:
        return "type:integration" in labels
    except TypeError:
        return False


def deps_allow_claim(
    task: Task,
    resolved_deps: List[str],
    tasks_by_id: Dict[str, Task],
) -> Tuple[bool, List[Dict[str, str]]]:
    """Decide claimability of ``task`` given its dependency states (#629).

    Ordinary tasks keep the strict gate: every dependency must be DONE.
    Integration tasks (``is_integration_task``) claim best-effort: every
    dependency must be settled (DONE or BLOCKED) and the DONE fraction
    must be at least ``BEST_EFFORT_MIN_DONE_FRACTION``. TODO and
    IN_PROGRESS upstreams are live work — integration keeps waiting for
    them; an unknown dependency id cannot be proven settled and blocks
    the claim.

    Parameters
    ----------
    task : Task
        The candidate task being filtered for availability.
    resolved_deps : List[str]
        The task's dependency ids after slug resolution.
    tasks_by_id : Dict[str, Task]
        Project tasks indexed by id.

    Returns
    -------
    Tuple[bool, List[Dict[str, str]]]
        ``(claimable, degraded_upstreams)``. ``degraded_upstreams`` is
        non-empty only for a best-effort integration claim: one
        ``{"id", "name", "status"}`` record per non-DONE upstream, so the
        assignment can tell the agent exactly which lanes never finished
        (Invariant #2 — Marcus provides the state, the agent decides).
    """
    if not resolved_deps:
        return True, []

    dep_tasks = [tasks_by_id.get(dep_id) for dep_id in resolved_deps]

    if not is_integration_task(task):
        all_done = all(
            dep is not None and dep.status == TaskStatus.DONE for dep in dep_tasks
        )
        return all_done, []

    if any(
        dep is None or dep.status not in SETTLED_FOR_BEST_EFFORT for dep in dep_tasks
    ):
        return False, []

    done_count = sum(
        1 for dep in dep_tasks if dep is not None and dep.status == TaskStatus.DONE
    )
    if done_count / len(resolved_deps) < BEST_EFFORT_MIN_DONE_FRACTION:
        return False, []

    degraded = [
        {"id": dep.id, "name": dep.name, "status": dep.status.value}
        for dep in dep_tasks
        if dep is not None and dep.status != TaskStatus.DONE
    ]
    return True, degraded


def phase_satisfied_by(task: Task) -> Set[TaskStatus]:
    """Statuses that satisfy an earlier-phase requirement for ``task``.

    Task selection applies a second gate after dependencies: a task is
    phase-ineligible while an earlier phase in the same feature has no
    completed task. That gate counted only DONE, which reintroduced the
    #629 hang through a different door — a BLOCKED same-feature task kept
    the terminal integration task ineligible forever, while the gridlock
    detector (sharing ``deps_allow_claim``) reported no gridlock. The
    alarm went quiet and the run still hung (Codex P1 on PR #718).

    For the integration task, SETTLED (DONE or BLOCKED) satisfies the
    phase requirement, matching the dependency rule. Ordinary tasks keep
    the strict DONE rule: implementation must not start on a dead design.

    Parameters
    ----------
    task : Task
        The candidate task being phase-filtered.

    Returns
    -------
    Set[TaskStatus]
        Statuses that count as "this earlier phase is finished".
    """
    if is_integration_task(task):
        return set(SETTLED_FOR_BEST_EFFORT)
    return {TaskStatus.DONE}
