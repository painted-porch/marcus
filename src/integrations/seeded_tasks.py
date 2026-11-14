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
same call, by name or by its caller-supplied ``id``, or -- issue #841,
ADR-0013 K10 accepted narrowly 2026-11-06 -- an existing task already on
the board, by name or by id, scoped to the caller's project. The in-batch
name is always tried first; the board is only consulted for a name the
batch does not have. This is add-only: a dependency on an existing task
declares a new edge into it and never rewires or otherwise touches that
task, so a leased task can be depended on exactly as freely as a ``todo``
one.

Issue #911 (ADR-0013 K2, the MCP-only write path for extensions): a spec
may also carry the three fields a typed chain needs and could not send
over MCP before::

    - name: Find the source of tow-0001
      id: tow-0001-find
      task_type: find
      inputs: {case_id: tow-0001, query: "..."}

``task_type`` selects which kernel rules apply to the card (the typed
evidence gate, offer-time eligibility); ``inputs`` is what the board hands
the worker that pulls it; ``id`` is the card's final id, so a seeder can
find its cards again without reading the board back. The SQLite provider
honours all three at creation (issue #844); the gap was only this schema.
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
    # Issue #911: the typed-chain fields.
    "task_type",
    "inputs",
    "id",
}
#: The characters a caller-supplied ``id`` may use; the same rule
#: ``SQLiteKanban._resolve_task_id`` enforces, so a bad id is refused
#: here with every other problem instead of failing mid-creation.
_ID_CHARSET = re.compile(r"^[A-Za-z0-9_-]+$")


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
        naming only other seeded tasks (by name or caller-supplied
        ``id``) or an existing board task (by name or id), string
        lists for ``labels`` and ``completion_criteria``, a positive
        ``estimated_hours``, a ``priority`` from :class:`Priority`,
        and -- issue #911 -- an optional non-empty string
        ``task_type``, a mapping ``inputs``, and a string ``id`` over
        ``[A-Za-z0-9_-]`` that is unique in the batch and not already
        a board task's id.
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
    ids: List[str] = []
    existing_ids = {task.id for task in existing_tasks or []}
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
        if "task_type" in spec:
            task_type = spec["task_type"]
            if not isinstance(task_type, str) or not task_type.strip():
                errors.append(f"{where}.task_type must be a non-empty string")
        if "inputs" in spec and not isinstance(spec["inputs"], dict):
            errors.append(f"{where}.inputs must be a mapping")
        if "id" in spec:
            task_id = spec["id"]
            if not isinstance(task_id, str) or not _ID_CHARSET.match(task_id):
                errors.append(f"{where}.id must match [A-Za-z0-9_-]")
            elif task_id in ids:
                errors.append(f"{where} repeats the id {task_id!r}")
            elif task_id in existing_ids:
                errors.append(f"{where}.id {task_id!r} already identifies a board task")
            else:
                ids.append(task_id)
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
            if dep_name == own or dep_name == spec.get("id"):
                errors.append(f"tasks[{index}] depends on itself")
            elif dep_name in names or dep_name in ids:
                # In-batch, by name or by caller-supplied id (#911):
                # only these edges can form a cycle -- a new task's edge
                # into an existing board task cannot, since that task's
                # own dependencies are already fixed. The cycle check
                # keys on names, so an id edge is recorded under the
                # name it belongs to.
                edges.setdefault(own, []).append(
                    dep_name if dep_name in names else _name_for_id(specs, dep_name)
                )
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


def _name_for_id(specs: List[Dict[str, Any]], task_id: str) -> str:
    """Return the stripped name of the in-batch spec whose ``id`` is ``task_id``."""
    for spec in specs:
        if isinstance(spec, dict) and spec.get("id") == task_id:
            return str(spec.get("name", "")).strip()
    return task_id


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
        TODO tasks with synthetic ids ``seed-<n>-<slug>``, or the
        caller's own ``id`` where the spec gave one (issue #911; the
        board honours it, and ``source_context["caller_id"]`` tells
        the creation path to pass it through). A ``depends_on``
        naming another task in this batch, by name or by id, resolves
        to that task's id; one naming an existing board task resolves
        to that task's real, already-assigned id -- no rewrite is
        needed because it was never synthetic. ``task_type`` and
        ``inputs`` are carried as given (defaults ``"implement"`` and
        ``{}``). ``source_type`` is ``"seeded"``, which is how the rest
        of the pipeline knows to leave them exactly as given.

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
    # Caller-supplied ids are taken as given; a generated id is allocated
    # outside that set (Codex P2 on PR #919: a caller id spelled like
    # another card's would-be synthetic id gave both cards one id, and
    # the post-creation dependency remap then turned the second card's
    # edge into a self-dependency).
    caller_ids = {str(spec["id"]) for spec in specs if spec.get("id")}
    ids: Dict[str, str] = {}
    for i, spec in enumerate(specs):
        if spec.get("id"):
            ids[spec["name"].strip()] = str(spec["id"])
            continue
        candidate = f"seed-{i + 1}-{_slug(spec['name'])}"
        bump = 0
        while candidate in caller_ids:
            bump += 1
            candidate = f"seed-{i + 1}-{bump}-{_slug(spec['name'])}"
        ids[spec["name"].strip()] = candidate

    def _resolve(dep: str) -> str:
        dep_name = dep.strip()
        if dep_name in ids:
            return ids[dep_name]
        if dep_name in caller_ids:
            return dep_name
        # validate_seed_specs above already guaranteed this resolves
        # cleanly against the board when it is not in-batch.
        resolved_id, _ = _resolve_against_board(dep_name, existing_tasks)
        assert resolved_id is not None
        return resolved_id

    tasks: List[Task] = []
    for spec in specs:
        name = spec["name"].strip()
        source_context: Dict[str, Any] = {"seeded": True}
        if spec.get("id"):
            source_context["caller_id"] = True
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
                source_context=source_context,
                completion_criteria=list(spec.get("completion_criteria", []) or [])
                or None,
                task_type=str(spec.get("task_type") or "implement").strip(),
                inputs=dict(spec.get("inputs") or {}),
            )
        )
    return tasks
