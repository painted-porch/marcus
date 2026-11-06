"""Unit tests for ``MarcusServer.kanban_client``'s setter wrapping every
assignment with the K13 sync guarantee (issue #896 review).

Every tool-level reassignment of ``server.kanban_client`` after startup
(``select_project``, ``add_project``, ``update_project``, and others in
``src/marcus_mcp/tools/project_management.py``) writes the raw client
``project_manager.get_kanban_client()`` returns. Only this module's own
startup code (``MarcusServer.initialize``) remembered to wrap that raw
client with ``sync_on_write`` before assigning it. Every tool-level
reassignment silently stripped the wrapper back off: after any project
switch, a task write stopped syncing ``state.project_tasks`` (ADR-0013
K13) -- a task could be marked blocked on the board while the cache
still read in progress.

Fixed once, structurally, inside the setter itself, so every current
and future caller -- not just the ones a reviewer happens to look at --
gets the wrapping for free. These tests assert through an actual task
write that is expected to sync, not through the client's identity or
type, because an identity check (``isinstance(client, ...)``) would
pass even against a raw, unwrapped provider that happens to already
satisfy some other check.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.kanban_task_sync import TaskSyncingKanbanClient
from src.marcus_mcp.server import MarcusServer

pytestmark = pytest.mark.unit


def _task(task_id: str, status: TaskStatus = TaskStatus.TODO) -> Task:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
    )


class _FakeServerState:
    """Just enough state for the real setter to run against.

    The setter reads ``self.project_tasks`` and rebinds
    ``self.assignment_monitor``/``self.lease_manager`` if present --
    everything the real ``MarcusServer.kanban_client`` property touches.
    """

    def __init__(self, project_tasks: list[Task]) -> None:
        self.project_tasks = project_tasks
        self._kanban_client: Any = None
        self.assignment_monitor = None
        self.lease_manager = None


def test_assigning_a_raw_client_through_the_setter_still_syncs_task_writes() -> None:
    """The exact shape every tool-level reassignment uses:
    ``server.kanban_client = <raw client from get_kanban_client()>``.

    A write through the result must land on the in-memory task
    immediately (K13), proving the setter wrapped it -- a raw,
    unwrapped client's ``update_task`` would never touch
    ``project_tasks`` at all. Asserts BOTH halves of the wrapper's
    own contract, not just the sync: the board write itself must
    still happen too (a wrapper that updated the cache and silently
    swallowed the board write would pass a sync-only assertion and
    break every real write Marcus makes).
    """
    task = _task("t1")
    fake = _FakeServerState(project_tasks=[task])

    raw_client = Mock(name="raw-provider-client")
    raw_client.update_task = AsyncMock(return_value=None)

    MarcusServer.kanban_client.fset(fake, raw_client)  # type: ignore[attr-defined]

    client = MarcusServer.kanban_client.fget(fake)  # type: ignore[attr-defined]

    import asyncio

    asyncio.run(client.update_task("t1", {"status": TaskStatus.BLOCKED}))

    assert task.status == TaskStatus.BLOCKED
    raw_client.update_task.assert_awaited_once_with(
        "t1", {"status": TaskStatus.BLOCKED}
    )


def test_assigning_an_already_wrapped_client_is_not_double_wrapped() -> None:
    """``sync_on_write`` is idempotent; the setter must not stack a
    second wrapper around one the caller already wrapped (this module's
    own startup code does exactly that before assigning)."""
    task = _task("t2")
    fake = _FakeServerState(project_tasks=[task])

    raw_client = Mock(name="raw-provider-client")
    raw_client.update_task = AsyncMock(return_value=None)
    already_wrapped = TaskSyncingKanbanClient(raw_client, lambda: fake.project_tasks)

    MarcusServer.kanban_client.fset(fake, already_wrapped)  # type: ignore[attr-defined]

    assert MarcusServer.kanban_client.fget(fake) is already_wrapped  # type: ignore[attr-defined]


def test_assigning_none_passes_through_unchanged() -> None:
    """Clearing the client (e.g. delete_project) must not crash or
    wrap ``None``."""
    fake = _FakeServerState(project_tasks=[])

    MarcusServer.kanban_client.fset(fake, None)  # type: ignore[attr-defined]

    assert MarcusServer.kanban_client.fget(fake) is None  # type: ignore[attr-defined]
