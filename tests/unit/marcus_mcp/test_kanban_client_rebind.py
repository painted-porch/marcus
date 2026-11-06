"""
Unit tests: replacing ``MarcusServer.kanban_client`` rebinds every
long-lived service that captured a reference to it (issue #856 round 3).

``AssignmentMonitor`` (and its nested ``AssignmentReconciler``) and
``AssignmentLeaseManager`` each capture the kanban client passed to them
at construction into their own ``self.kanban_client`` attribute and
never see it change again. Round 1's always-fresh-client fix for
``create_project`` (a second project on the same provider now gets a
genuinely new client object instead of a mutated old one) made that
latent bug reachable: those three services -- built once per server
process -- kept talking to the OLD client. ``kanban_client`` is now a
property; its setter is the only writer and propagates a replacement to
every holder.

Issue #896 review: the setter now also wraps every assignment with the
K13 sync guarantee (``TaskSyncingKanbanClient``), so each rebound holder
receives the SAME wrapped object the server itself reads back, not the
raw pre-wrap argument a caller passed in. These tests therefore assert
through an actual task write that is expected to sync (``update_task``
followed by reading the in-memory ``Task``), not through object
identity against the raw mock -- an identity check would pass even
against a raw, unwrapped client that never synced anything.
"""

import asyncio
from datetime import datetime, timezone
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.server import MarcusServer

pytestmark = pytest.mark.unit


def _task(task_id: str) -> Task:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
    )


def _build_server(project_tasks: Optional[List[Task]] = None) -> MarcusServer:
    """A real MarcusServer instance, built via ``__new__`` rather than
    ``__init__``.

    ``MarcusServer()`` requires a large amount of config/filesystem
    mocking that is currently broken elsewhere in this test file
    (``TestKanbanInitialization``/``TestMarcusServerInitialization``
    are skipped with "Config singleton mocking needs refactoring").
    What's under test here is the ``kanban_client`` property's setter
    logic alone -- it only reads ``self.project_tasks`` and
    ``self.assignment_monitor``/``self.lease_manager`` via
    ``getattr(..., None)``, so a real instance with just those
    attributes set is sufficient and exercises the actual property
    descriptor on the real class.
    """
    server = MarcusServer.__new__(MarcusServer)
    server._kanban_client = None
    server.assignment_monitor = None
    server.lease_manager = None
    server.project_tasks = project_tasks if project_tasks is not None else []
    return server


def _raw_client() -> MagicMock:
    """A bare, unwrapped mock client -- exactly what every tool-level
    reassignment (``select_project``, ``add_project``, ``update_project``)
    passes to this setter."""
    client = MagicMock(name="raw-provider-client")
    client.update_task = AsyncMock(return_value=None)
    return client


class TestKanbanClientRebind:
    """Setting ``server.kanban_client`` propagates to every captured holder."""

    def test_assignment_monitor_sees_the_new_client(self) -> None:
        task = _task("t1")
        server = _build_server(project_tasks=[task])
        old_client = _raw_client()
        new_client = _raw_client()

        server.assignment_monitor = MagicMock()
        server.assignment_monitor.kanban_client = old_client
        server.assignment_monitor.reconciler = MagicMock()
        server.assignment_monitor.reconciler.kanban_client = old_client

        server.kanban_client = new_client

        asyncio.run(
            server.assignment_monitor.kanban_client.update_task(
                "t1", {"status": TaskStatus.BLOCKED}
            )
        )
        assert task.status == TaskStatus.BLOCKED, (
            "AssignmentMonitor kept talking to the old client after "
            "create_project built a fresh one for a second project -- "
            "this is exactly what made _check_for_reversions query the "
            "wrong board (#856 round 3). A write through the rebound "
            "reference must sync project_tasks the same way the "
            "server's own kanban_client does (#896 review)."
        )
        new_client.update_task.assert_awaited_once_with(
            "t1", {"status": TaskStatus.BLOCKED}
        )

    def test_assignment_monitors_reconciler_sees_the_new_client(self) -> None:
        task = _task("t1")
        server = _build_server(project_tasks=[task])
        old_client = _raw_client()
        new_client = _raw_client()

        server.assignment_monitor = MagicMock()
        server.assignment_monitor.kanban_client = old_client
        server.assignment_monitor.reconciler = MagicMock()
        server.assignment_monitor.reconciler.kanban_client = old_client

        server.kanban_client = new_client

        asyncio.run(
            server.assignment_monitor.reconciler.kanban_client.update_task(
                "t1", {"status": TaskStatus.BLOCKED}
            )
        )
        assert task.status == TaskStatus.BLOCKED
        new_client.update_task.assert_awaited_once_with(
            "t1", {"status": TaskStatus.BLOCKED}
        )

    def test_lease_manager_sees_the_new_client(self) -> None:
        task = _task("t1")
        server = _build_server(project_tasks=[task])
        old_client = _raw_client()
        new_client = _raw_client()

        server.lease_manager = MagicMock()
        server.lease_manager.kanban_client = old_client

        server.kanban_client = new_client

        asyncio.run(
            server.lease_manager.kanban_client.update_task(
                "t1", {"status": TaskStatus.BLOCKED}
            )
        )
        assert task.status == TaskStatus.BLOCKED, (
            "AssignmentLeaseManager kept talking to the old client -- "
            "lease recovery would check terminal status against the "
            "wrong board (#856 round 3). A write through the rebound "
            "reference must sync project_tasks too (#896 review)."
        )
        new_client.update_task.assert_awaited_once_with(
            "t1", {"status": TaskStatus.BLOCKED}
        )

    def test_server_itself_reads_back_the_new_client(self) -> None:
        task = _task("t1")
        server = _build_server(project_tasks=[task])
        new_client = _raw_client()

        server.kanban_client = new_client

        asyncio.run(
            server.kanban_client.update_task("t1", {"status": TaskStatus.BLOCKED})
        )
        assert task.status == TaskStatus.BLOCKED, (
            "server.kanban_client must sync a write to project_tasks "
            "(K13) -- a raw, unwrapped client passed straight through "
            "would never touch it (#896 review)."
        )
        new_client.update_task.assert_awaited_once_with(
            "t1", {"status": TaskStatus.BLOCKED}
        )

    def test_no_monitor_or_lease_manager_yet_does_not_raise(self) -> None:
        """Before _initialize_monitoring_systems has ever run (both are
        None, as at server construction), setting the client must be a
        no-op for the rebind, not an error."""
        server = _build_server()
        assert server.assignment_monitor is None
        assert server.lease_manager is None

        server.kanban_client = _raw_client()  # must not raise

        assert server.kanban_client is not None
