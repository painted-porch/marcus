"""Seeded boards: build Task objects from an explicit task list.

``create_project`` normally turns a description into tasks with an LLM
decomposer. A seeded board skips that: the caller hands over the exact
tasks (one, two, three) and Marcus puts precisely those on the board, so a
live check of the coordination machinery takes minutes instead of a full
decomposition plus a dozen chores. Everything else ``create_project`` does
(registry, workspace state, ``project_info.json``, the About card, the cost
run row) happens as usual.

A seed spec is a list of mappings::

    - name: Implement add_one_plus_one
      description: A function returning 1 + 1 and a unit test for it.
      labels: [implement, python]
      depends_on: []
      completion_criteria:
        - add_one_plus_one() returns 2
      estimated_hours: 0.5
      priority: medium

Only ``name`` is required. ``depends_on`` names another seeded task in the
same call, by name, or -- issue #841, ADR-0013 K10 accepted narrowly
2026-11-06 -- an existing task already on the board, by name or by id,
scoped to the caller's project. The in-batch name is always tried first;
the board is only consulted for a name the batch does not have. This is
add-only: a dependency on an existing task declares a new edge into it and
never rewires or otherwise touches that task, so a leased task can be
depended on exactly as freely as a ``todo`` one.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.core.models import Priority, Task, TaskStatus

SEEDED_SOURCE_TYPE = "seeded"
_ALLOWED_KEYS = {
    "name",
    "description",
    "labels",
    "depends_on",
    "completion_criteria",
    "estimated_hours",
    "priority",
}


class SeedTaskError(ValueError):
    """A seed spec is malformed; the message names every problem found."""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "task"


def _resolve_against_board(
    dep: str, existing_tasks: List[Task]
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve one ``depends_on`` entry against the board's existing tasks.

    Called only for a name the current batch does not have. Tries an
    exact id match first (ids are unique by construction, so this can
    never be ambiguous), then an exact name match.

    Parameters
    ----------
    dep : str
        The dependency as named in the seed spec, already stripped.
    existing_tasks : List[Task]
        The caller's own project's existing board tasks (issue #841:
        the caller is responsible for scoping this list to its
        project before calling).

    Returns
    -------
    Tuple[Optional[str], Optional[str]]
        ``(resolved_id, None)`` on a clean match; ``(None, None)``
        when nothing on the board matches either way; ``(None,
        error)`` when the name matches more than one existing task
        and the caller must not guess which one was meant.
    """
    for task in existing_tasks:
        if task.id == dep:
            return dep, None
    name_matches = [task for task in existing_tasks if task.name == dep]
    if len(name_matches) > 1:
        ambiguous_ids = ", ".join(str(task.id) for task in name_matches)
        return None, (
            f"{dep!r} matches {len(name_matches)} existing board tasks by "
            f"name ({ambiguous_ids}); name the task by id instead"
        )
    if len(name_matches) == 1:
        return str(name_matches[0].id), None
    return None, None


def validate_seed_specs(
    specs: Any, existing_tasks: Optional[List[Task]] = None
) -> List[str]:
    """Return every problem with ``specs``, or an empty list when it is valid.

    Parameters
    ----------
    specs : Any
        The ``tasks`` option as received: must be a non-empty list of
        mappings with a unique, non-empty ``name`` each, ``depends_on``
        naming only other seeded tasks or an existing board task (by
        name or id), string lists for ``labels`` and
        ``completion_criteria``, a positive ``estimated_hours``, and a
        ``priority`` from :class:`Priority`.
    existing_tasks : Optional[List[Task]]
        The caller's project's existing board tasks, consulted only
        for a ``depends_on`` name the batch itself does not have
        (issue #841). ``None`` or empty keeps the pre-#841 behavior:
        every dependency must be in the same batch.

    Returns
    -------
    List[str]
        Human-readable problems, in order of appearance.
    """
    errors: List[str] = []
    if not isinstance(specs, list) or not specs:
        return ["tasks must be a non-empty list of task mappings"]
    names: List[str] = []
    for index, spec in enumerate(specs):
        where = f"tasks[{index}]"
        if not isinstance(spec, dict):
            errors.append(f"{where} must be a mapping")
            continue
        unknown = sorted(set(spec) - _ALLOWED_KEYS)
        if unknown:
            errors.append(f"{where} has unknown keys: {', '.join(unknown)}")
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{where} needs a non-empty name")
        else:
            if name.strip() in names:
                errors.append(f"{where} repeats the name {name.strip()!r}")
            names.append(name.strip())
        for key in ("labels", "completion_criteria", "depends_on"):
            value = spec.get(key, [])
            if not isinstance(value, list) or not all(
                isinstance(v, str) and v.strip() for v in value
            ):
                errors.append(f"{where}.{key} must be a list of non-empty strings")
        hours = spec.get("estimated_hours", 1.0)
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
            errors.append(f"{where}.estimated_hours must be a positive number")
        priority = spec.get("priority", "medium")
        if not isinstance(priority, str) or priority not in {p.value for p in Priority}:
            errors.append(
                f"{where}.priority must be one of "
                f"{', '.join(p.value for p in Priority)}"
            )
    edges: Dict[str, List[str]] = {}
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            continue
        deps = spec.get("depends_on", []) or []
        if not isinstance(deps, list):
            continue  # already reported by the list-type check above
        own = str(spec.get("name", "")).strip()
        for dep in deps:
            if not isinstance(dep, str):
                continue  # already reported
            dep_name = dep.strip()
            if dep_name == own:
                errors.append(f"tasks[{index}] depends on itself")
            elif dep_name in names:
                # In-batch: only these edges can form a cycle -- a new
                # task's edge into an existing board task cannot, since
                # that task's own dependencies are already fixed.
                edges.setdefault(own, []).append(dep_name)
            else:
                resolved_id, board_error = _resolve_against_board(
                    dep_name, existing_tasks or []
                )
                if board_error:
                    errors.append(f"tasks[{index}].depends_on {dep!r}: {board_error}")
                elif resolved_id is None:
                    errors.append(
                        f"tasks[{index}].depends_on names an unknown task "
                        f"{dep!r}: not in this list and not on the board"
                    )
    cycle = _find_cycle(edges)
    if cycle:
        errors.append("tasks form a dependency cycle: " + " -> ".join(cycle))
    return errors


def _find_cycle(edges: Dict[str, List[str]]) -> List[str]:
    """Return one dependency cycle as a list of names, or an empty list.

    The board's graph auto-fixer does not run for seeded tasks, so a cycle
    must be caught here; on the board it would leave every task in the
    cycle unclaimable forever.
    """
    state: Dict[str, int] = {}
    stack: List[str] = []

    def visit(node: str) -> List[str]:
        state[node] = 1
        stack.append(node)
        for nxt in edges.get(node, []):
            if state.get(nxt, 0) == 1:
                return stack[stack.index(nxt) :] + [nxt]
            if state.get(nxt, 0) == 0:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return []

    for start in list(edges):
        if state.get(start, 0) == 0:
            found = visit(start)
            if found:
                return found
    return []


def build_seeded_tasks(
    specs: List[Dict[str, Any]],
    project_name: str,
    existing_tasks: Optional[List[Task]] = None,
) -> List[Task]:
    """Turn validated seed specs into Task objects ready for the board.

    Parameters
    ----------
    specs : List[Dict[str, Any]]
        Seed specs; see :func:`validate_seed_specs`.
    project_name : str
        Stamped on each task's ``project_name``.
    existing_tasks : Optional[List[Task]]
        The caller's project's existing board tasks, for resolving a
        ``depends_on`` entry the batch itself does not name (issue
        #841). Passed straight through to :func:`validate_seed_specs`.

    Returns
    -------
    List[Task]
        TODO tasks with synthetic ids ``seed-<n>-<slug>``. A
        ``depends_on`` naming another task in this batch resolves to
        that synthetic id; one naming an existing board task resolves
        to that task's real, already-assigned id -- no rewrite is
        needed because it was never synthetic. ``source_type`` is
        ``"seeded"``, which is how the rest of the pipeline knows to
        leave them exactly as given.

    Raises
    ------
    SeedTaskError
        When the specs are malformed; every problem is in the message.
    """
    errors = validate_seed_specs(specs, existing_tasks)
    if errors:
        raise SeedTaskError("; ".join(errors))
    existing_tasks = existing_tasks or []
    now = datetime.now(timezone.utc)
    ids = {
        spec["name"].strip(): f"seed-{i + 1}-{_slug(spec['name'])}"
        for i, spec in enumerate(specs)
    }

    def _resolve(dep: str) -> str:
        dep_name = dep.strip()
        if dep_name in ids:
            return ids[dep_name]
        # validate_seed_specs above already guaranteed this resolves
        # cleanly against the board when it is not in-batch.
        resolved_id, _ = _resolve_against_board(dep_name, existing_tasks)
        assert resolved_id is not None
        return resolved_id

    tasks: List[Task] = []
    for spec in specs:
        name = spec["name"].strip()
        tasks.append(
            Task(
                id=ids[name],
                name=name,
                description=str(spec.get("description") or name),
                status=TaskStatus.TODO,
                priority=Priority(spec.get("priority", "medium")),
                assigned_to=None,
                created_at=now,
                updated_at=now,
                due_date=None,
                estimated_hours=float(spec.get("estimated_hours", 1.0)),
                dependencies=[_resolve(d) for d in spec.get("depends_on", []) or []],
                labels=list(spec.get("labels", []) or []),
                project_name=project_name,
                source_type=SEEDED_SOURCE_TYPE,
                source_context={"seeded": True},
                completion_criteria=list(spec.get("completion_criteria", []) or [])
                or None,
            )
        )
    return tasks
