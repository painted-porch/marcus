"""Resolve the project a kanban client instance is currently bound to.

Shared by ``AssignmentMonitor`` (``src/monitoring/assignment_monitor.py``,
issue #856 round 4) and ``AssignmentLeaseManager``
(``src/core/assignment_lease.py``, issue #859) -- both need to tell
"this assignment/lease belongs to a different project than the one
the kanban client is bound to right now" from "this assignment/lease
is genuinely gone", and both need the identical resolution rule to
agree with each other.
"""

from typing import Any, Optional


def bound_project_id(kanban_client: Any) -> Optional[str]:
    """Resolve the project scope the given kanban client is bound to.

    Mirrors the same precedence ``KanbanClient._card_to_task`` uses to
    stamp a task's own ``project_id`` (registry id wins when present;
    the provider's own id otherwise), so a stamped ``project_id`` can
    be compared against it directly. Works across providers: SQLite
    has no separate registry id (its own ``project_id`` already *is*
    the registry id, GH-306); Planka and Linear expose
    ``registry_project_id`` since #854.
    """
    return getattr(kanban_client, "registry_project_id", None) or getattr(
        kanban_client, "project_id", None
    )
