"""
Seed a project with exactly the tasks an extension names, through MCP.

``create_project(options.tasks)`` with ``options.path = "direct"`` puts the
listed tasks on the board and runs no model decomposition. The two traps the
Minecraft runs found are enforced here: the criteria key is
``completion_criteria``, ``depends_on`` names tasks by name, and an extra key
on a spec breaks the call (six of seven promotions in Minecraft run 6).

No label from the ``build`` family goes on any task: those labels switch on
the software validation gate, which rejects a task with no source files
(ADR-0013 K3, issue #798).

Every seeded project also carries one extra done card, ``About: <name>``,
which Marcus creates itself; counts read back from the board are seeded + 1.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .client import MarcusMCPClient

#: The keys a task spec may carry on ``create_project`` and ``create_tasks``.
TASK_KEYS = frozenset(
    {
        "name",
        "description",
        "labels",
        "depends_on",
        "completion_criteria",
        "estimated_hours",
        "priority",
    }
)
#: Labels that switch on the software validation gate (K3); never on a board-only task.
FORBIDDEN_LABELS = frozenset({"build", "create", "implement", "develop", "feature"})
PRIORITIES = frozenset({"high", "medium", "low"})


@dataclass
class SeedResult:
    """What seeding produced."""

    project_id: str
    task_names: List[str]
    total_tasks: Optional[int]
    reply: Dict[str, Any] = field(default_factory=dict)


def validate_specs(specs: Sequence[Dict[str, Any]]) -> None:
    """
    Refuse a task list the board would mis-handle.

    Parameters
    ----------
    specs : Sequence[Dict[str, Any]]
        Task specs.

    Raises
    ------
    ValueError
        On an unknown key, a forbidden label, a bad priority, a duplicate
        name, or a ``depends_on`` that names no task in the list.
    """
    names = [str(s.get("name") or "") for s in specs]
    if len(set(names)) != len(names) or any(not n for n in names):
        raise ValueError("every task needs a distinct, non-empty name")
    for spec in specs:
        unknown = set(spec) - TASK_KEYS
        if unknown:
            raise ValueError(
                f"task {spec['name']!r}: unknown keys {sorted(unknown)};"
                f" allowed {sorted(TASK_KEYS)}"
            )
        bad = {
            str(label).lower() for label in spec.get("labels") or []
        } & FORBIDDEN_LABELS
        if bad:
            raise ValueError(
                f"task {spec['name']!r}: label(s) {sorted(bad)} switch on the"
                " software validation gate (K3)"
            )
        if "priority" in spec and spec["priority"] not in PRIORITIES:
            raise ValueError(
                f"task {spec['name']!r}: priority must be one of {sorted(PRIORITIES)}"
            )
        for dep in spec.get("depends_on") or []:
            if dep not in names:
                raise ValueError(
                    f"task {spec['name']!r}: depends_on {dep!r} names no task"
                    " in this list"
                )
        criteria = spec.get("completion_criteria")
        if criteria is not None and not isinstance(criteria, list):
            raise ValueError(
                f"task {spec['name']!r}: completion_criteria must be a list"
                " of sentences"
            )


def seed_project(
    client: MarcusMCPClient,
    *,
    name: str,
    description: str,
    tasks: Sequence[Dict[str, Any]],
    objective: Optional[str] = None,
    project_root: Optional[str] = None,
    wait_seconds: float = 120.0,
    sleep_seconds: float = 2.0,
) -> SeedResult:
    """
    Create a project holding exactly ``tasks``, and read back that it did.

    Parameters
    ----------
    client : MarcusMCPClient
        A connected client.
    name : str
        The project name. Never contains "test".
    description : str
        The project description.
    tasks : Sequence[Dict[str, Any]]
        Task specs, validated by :func:`validate_specs`.
    objective : Optional[str]
        A human-written objective every agent reads once at registration.
    project_root : Optional[str]
        Where artifacts are written; a directory that is deliberately not a repository.
    wait_seconds : float
        How long to wait for the board to show the tasks.
    sleep_seconds : float
        The poll interval while waiting.

    Returns
    -------
    SeedResult

    Raises
    ------
    ValueError
        On a bad task list or a project name containing "test".
    RuntimeError
        When the server returns no project id.
    """
    if "test" in name.lower():
        raise ValueError(
            "a project name containing 'test' changes Marcus's behaviour;"
            " pick another name"
        )
    validate_specs(tasks)
    options: Dict[str, Any] = {
        "provider": "sqlite",
        "complexity": "prototype",
        "path": "direct",
        "tasks": list(tasks),
    }
    if project_root:
        options["project_root"] = project_root
    if objective:
        options["objective"] = objective
    reply = client.call(
        "create_project",
        {"description": description, "project_name": name, "options": options},
    )
    project_id = reply.get("project_id")
    if not project_id:
        raise RuntimeError(f"create_project returned no project id: {reply}")
    total: Optional[int] = None
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        status = (client.call_tool("get_project_status") or {}).get("project") or {}
        if (status.get("total_tasks") or 0) >= len(tasks):
            total = int(status["total_tasks"])
            break
        time.sleep(sleep_seconds)
    return SeedResult(
        project_id=str(project_id),
        task_names=[str(t["name"]) for t in tasks],
        total_tasks=total,
        reply=reply,
    )
