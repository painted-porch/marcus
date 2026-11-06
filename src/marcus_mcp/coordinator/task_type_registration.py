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

from typing import Any, Dict, List, Optional

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


def project_declares_phase_order(state: Any, project_id: Optional[str]) -> bool:
    """Return whether a project has opted into ADR-0013 K1/K3 phase order.

    Issue #852: phase order (design before implementation before
    testing before documentation) is a software-project convention,
    not a kernel default. A board with no such declaration must never
    have a task held because Marcus *inferred* a phase order the
    project never declared -- this is the check
    ``apply_board_wide_gates`` (``src/marcus_mcp/tools/task.py``) makes
    before running any phase-based filtering at all.

    Parameters
    ----------
    state : Any
        Marcus server state, passed through to the registration
        lookup.
    project_id : Optional[str]
        The candidate task's own project id (``task.project_id``).
        ``None`` falls back to the server's active project.

    Returns
    -------
    bool
        ``True`` only when the resolved project's
        ``enforce_phase_order`` is explicitly ``True``. ``False`` for
        no project registry, no resolvable project, or a project that
        has never set the field -- the same "absent means off" default
        every other K2 declaration in this module already follows.
    """
    registry = getattr(state, "project_registry", None)
    if not isinstance(registry, ProjectRegistry):
        return False
    project = (
        registry.get_project_sync(project_id)
        if project_id
        else registry.get_active_project_sync()
    )
    if project is None:
        return False
    return getattr(project, "enforce_phase_order", False) is True


def injected_checks_for_task_type(
    task_type: str, state: Any, project_id: Optional[str]
) -> List[Dict[str, str]]:
    """Return a task type's ADR-0013 K4 injected checks, if any.

    Issue #847: K4's recommendation is a kernel mechanism letting a
    verification extension require a check the worker did not
    author, with injection itself a per-type policy choice. Reads
    through the same registration lookup every other K2-keyed field
    uses, so a candidate task's own project is always checked first.

    Parameters
    ----------
    task_type : str
        The candidate task's type.
    state : Any
        Marcus server state, passed through to the registration
        lookup.
    project_id : Optional[str]
        The candidate task's own project id (``task.project_id``).

    Returns
    -------
    List[Dict[str, str]]
        Each check as declared (``name``, and ``description`` when
        given) -- never a callable or a command. Empty when
        ``checks_injectable`` is not explicitly ``True``, matching
        the validation ``ProjectRegistry.add_project`` already
        enforces at registration: a project cannot declare checks
        without the flag, and a project with the flag but no checks
        has simply declared nothing.
    """
    registration = task_type_registration(task_type, state, project_id)
    if not registration or registration.get("checks_injectable") is not True:
        return []
    return list(registration.get("injected_checks") or [])
