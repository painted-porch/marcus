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

Only ``name`` is required. ``depends_on`` names other seeded tasks; the
builder resolves names to the synthetic ids the board creator later remaps
to kanban ids by name.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List

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


def validate_seed_specs(specs: Any) -> List[str]:
    """Return every problem with ``specs``, or an empty list when it is valid.

    Parameters
    ----------
    specs : Any
        The ``tasks`` option as received: must be a non-empty list of
        mappings with a unique, non-empty ``name`` each, ``depends_on``
        naming only other seeded tasks, string lists for ``labels`` and
        ``completion_criteria``, a positive ``estimated_hours``, and a
        ``priority`` from :class:`Priority`.

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
        if priority not in {p.value for p in Priority}:
            errors.append(
                f"{where}.priority must be one of "
                f"{', '.join(p.value for p in Priority)}"
            )
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            continue
        for dep in spec.get("depends_on", []) or []:
            if isinstance(dep, str) and dep.strip() not in names:
                errors.append(
                    f"tasks[{index}].depends_on names an unknown task {dep!r}"
                )
            elif (
                isinstance(dep, str)
                and dep.strip() == str(spec.get("name", "")).strip()
            ):
                errors.append(f"tasks[{index}] depends on itself")
    return errors


def build_seeded_tasks(specs: List[Dict[str, Any]], project_name: str) -> List[Task]:
    """Turn validated seed specs into Task objects ready for the board.

    Parameters
    ----------
    specs : List[Dict[str, Any]]
        Seed specs; see :func:`validate_seed_specs`.
    project_name : str
        Stamped on each task's ``project_name``.

    Returns
    -------
    List[Task]
        TODO tasks with synthetic ids ``seed-<n>-<slug>`` and dependencies
        resolved from names to those ids. ``source_type`` is
        ``"seeded"``, which is how the rest of the pipeline knows to leave
        them exactly as given.

    Raises
    ------
    SeedTaskError
        When the specs are malformed; every problem is in the message.
    """
    errors = validate_seed_specs(specs)
    if errors:
        raise SeedTaskError("; ".join(errors))
    now = datetime.now(timezone.utc)
    ids = {
        spec["name"].strip(): f"seed-{i + 1}-{_slug(spec['name'])}"
        for i, spec in enumerate(specs)
    }
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
                dependencies=[ids[d.strip()] for d in spec.get("depends_on", []) or []],
                labels=list(spec.get("labels", []) or []),
                project_name=project_name,
                source_type=SEEDED_SOURCE_TYPE,
                source_context={"seeded": True},
                completion_criteria=list(spec.get("completion_criteria", []) or [])
                or None,
            )
        )
    return tasks
