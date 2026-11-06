"""Shared lookup for a project's ADR-0013 K2 task-type registration.

Issue #830 found the same gap twice in one review round: a hardcoded
set of literal task-type names standing in for "what a project's K2
registration actually says" -- first in eligibility.py's self-check
exclusion (fixed, PR #836), then again in task.py's typed-evidence
gate and evidence-recorded set (the second, still-open half, reopened
after #836 closed only the first). Both need the identical lookup --
resolve the candidate task's own project first, falling back to the
server's active project only for a legacy task with no ``project_id``
-- so it lives here once rather than drifting between two copies.
"""

from typing import Any, Dict, Optional

from src.core.project_registry import ProjectRegistry


def task_type_registration(
    task_type: str, state: Any, project_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Return a project's ADR-0013 K2 registration for a task type.

    Parameters
    ----------
    task_type : str
        The task type to look up.
    state : Any
        Marcus server state. Only a real ``ProjectRegistry`` is
        trusted -- a bare ``Mock`` auto-creates a ``project_registry``
        attribute that would otherwise look present without ever
        being one.
    project_id : Optional[str]
        The candidate task's own project id (``task.project_id``).
        ``None`` falls back to the server's active project.

    Returns
    -------
    Optional[Dict[str, Any]]
        The registration dict, or ``None`` when there is no project
        registry, no resolvable project, or no registration for this
        type.
    """
    registry = getattr(state, "project_registry", None)
    if not isinstance(registry, ProjectRegistry):
        return None
    project = (
        registry.get_project_sync(project_id)
        if project_id
        else registry.get_active_project_sync()
    )
    if project is None:
        return None
    return project.task_type_registrations.get(task_type)
