"""
Unit tests: ``AssignmentMonitor._check_for_reversions`` scopes "missing"
to the assignment's own project (issue #856 round 4).

``AssignmentPersistence`` is the server-wide assignment store, not
scoped to one project, but ``self.kanban_client`` is bound to exactly
one project's board at a time (and, since round 1's always-fresh-client
fix for ``create_project``, a genuinely different client object each
time the active project changes). Before this fix, switching the
active project from A to B made the next reversion check delete A's
still-valid assignments, because they simply don't appear on B's
board. An assignment can only be "missing" relative to its OWN
project's board.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.monitoring.assignment_monitor import AssignmentMonitor

pytestmark = pytest.mark.unit


def _task(task_id: str, project_id: Optional[str] = None) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=TaskStatus.IN_PROGRESS,
        priority=Priority.MEDIUM,
        assigned_to="agent-a",
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        project_id=project_id,
    )


def _fake_persistence(assignments: Dict[str, Dict[str, Any]]) -> MagicMock:
    persistence = MagicMock()
    persistence.load_assignments = AsyncMock(return_value=assignments)
    persistence.remove_assignment = AsyncMock()
    return persistence


def _fake_client(tasks: List[Task], bound_project_id: Optional[str]) -> MagicMock:
    client = MagicMock()
    client.get_all_tasks = AsyncMock(return_value=tasks)
    client.registry_project_id = bound_project_id
    client.project_id = bound_project_id
    return client


class TestReversionCheckIsScopedToTheAssignmentsOwnProject:
    @pytest.mark.asyncio
    async def test_a_different_projects_assignment_is_left_alone(self) -> None:
        """Project A's assignment must survive a check run while the
        client is bound to project B -- it is not "missing", it is
        just not on THIS board."""
        assignments = {
            "agent-a": {
                "task_id": "task-on-a",
                "task_data": {"project_id": "project-A"},
            }
        }
        persistence = _fake_persistence(assignments)
        # Bound to B; B's board has no task-on-a at all.
        client = _fake_client([_task("task-on-b", "project-B")], "project-B")
        monitor = AssignmentMonitor(persistence=persistence, kanban_client=client)

        await monitor._check_for_reversions()

        persistence.remove_assignment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_genuinely_missing_task_on_the_bound_project_is_still_detected(
        self,
    ) -> None:
        """The fix must not blind the check to real reversions on the
        project it IS currently bound to."""
        assignments = {
            "agent-a": {
                "task_id": "task-on-b-deleted",
                "task_data": {"project_id": "project-B"},
            }
        }
        persistence = _fake_persistence(assignments)
        # Bound to B; B's board does NOT have task-on-b-deleted.
        client = _fake_client([_task("some-other-task", "project-B")], "project-B")
        monitor = AssignmentMonitor(persistence=persistence, kanban_client=client)

        await monitor._check_for_reversions()

        persistence.remove_assignment.assert_awaited_once_with("agent-a")

    @pytest.mark.asyncio
    async def test_an_assignment_with_no_recorded_project_id_keeps_old_behavior(
        self,
    ) -> None:
        """An assignment saved before this fix existed has no
        project_id in its task_data -- it must fall through to the
        pre-existing, unscoped check rather than being silently
        exempted forever."""
        assignments = {
            "agent-a": {
                "task_id": "pre-fix-assignment",
                "task_data": {"name": "no project_id recorded"},
            }
        }
        persistence = _fake_persistence(assignments)
        client = _fake_client([], "project-B")
        monitor = AssignmentMonitor(persistence=persistence, kanban_client=client)

        await monitor._check_for_reversions()

        persistence.remove_assignment.assert_awaited_once_with("agent-a")

    @pytest.mark.asyncio
    async def test_matching_project_id_still_goes_through_the_normal_check(
        self,
    ) -> None:
        """Same project as bound: existing behavior, task present ->
        no removal."""
        assignments = {
            "agent-a": {
                "task_id": "task-on-a",
                "task_data": {"project_id": "project-A"},
            }
        }
        persistence = _fake_persistence(assignments)
        client = _fake_client([_task("task-on-a", "project-A")], "project-A")
        monitor = AssignmentMonitor(persistence=persistence, kanban_client=client)

        await monitor._check_for_reversions()

        persistence.remove_assignment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_client_with_no_bound_project_id_keeps_old_behavior(self) -> None:
        """A kanban client exposing neither registry_project_id nor
        project_id (e.g. a bare test double) must not change behavior
        -- the scoping check is a no-op without something to scope by."""
        assignments = {
            "agent-a": {
                "task_id": "task-on-a",
                "task_data": {"project_id": "project-A"},
            }
        }
        persistence = _fake_persistence(assignments)
        client = MagicMock(spec=["get_all_tasks"])
        client.get_all_tasks = AsyncMock(return_value=[])
        monitor = AssignmentMonitor(persistence=persistence, kanban_client=client)

        await monitor._check_for_reversions()

        persistence.remove_assignment.assert_awaited_once_with("agent-a")
