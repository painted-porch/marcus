"""
Unit tests for ``stranded_assignments`` on ``get_project_status`` (issue #900).

A card that is ``TODO``, names an assignee, and is held by nothing in the
kernel can neither be offered nor completed, and it blocks its dependents.
Nothing used to report it; someone had to read the eligibility filter's
source and compare it with raw board state by hand.
The status tool now carries the count and the ids, computed by the same
rule the drain verdict and the eligibility filter use.
"""

from datetime import datetime, timezone
from typing import List, Optional, Set
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.project import get_project_status

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _task(task_id: str, status: TaskStatus, assigned_to: Optional[str] = None) -> Task:
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=assigned_to,
        created_at=NOW,
        updated_at=NOW,
        due_date=None,
        estimated_hours=1.0,
        labels=[],
        dependencies=[],
        project_id="p1",
    )


def _state(tasks: List[Task], *, persisted: Optional[Set[str]] = None) -> MagicMock:
    """A server state whose active board holds ``tasks`` and holds nothing else."""
    state = MagicMock()
    state.project_state = MagicMock()
    state.project_state.project_name = "Test"
    state.agent_status = {}
    state.provider = "sqlite"
    active = MagicMock(id="p1", provider="sqlite")
    active.name = "Test"
    state.project_registry = MagicMock()
    state.project_registry.get_active_project = AsyncMock(return_value=active)
    kanban = MagicMock()
    kanban.get_project_metrics = AsyncMock(
        return_value={
            "total_tasks": len(tasks),
            "completed_tasks": sum(1 for t in tasks if t.status == TaskStatus.DONE),
            "in_progress_tasks": sum(
                1 for t in tasks if t.status == TaskStatus.IN_PROGRESS
            ),
            "blocked_tasks": sum(1 for t in tasks if t.status == TaskStatus.BLOCKED),
        }
    )
    kanban.get_all_tasks = AsyncMock(return_value=tasks)
    kanban.get_task_blockers = AsyncMock(return_value=[])
    state.persistence = MagicMock()
    state.persistence.retrieve = AsyncMock(return_value=None)
    state.kanban_client = kanban
    state.project_manager = MagicMock()
    state.project_manager.get_kanban_client = AsyncMock(return_value=kanban)
    state.project_tasks = tasks
    state.refresh_project_state = AsyncMock()
    # What the kernel holds: nothing leased, nothing mid-assignment,
    # only what ``persisted`` names.
    state.lease_manager = MagicMock()
    state.lease_manager.active_leases = {}
    state.tasks_being_assigned = set()
    state.assignment_persistence = MagicMock()
    state.assignment_persistence.get_all_assigned_task_ids = AsyncMock(
        return_value=set(persisted or set())
    )
    return state


class TestStrandedAssignmentsOnProjectStatus:
    """The status payload names cards the kernel can neither offer nor finish."""

    @pytest.mark.asyncio
    async def test_a_stranded_card_is_counted_and_named(self) -> None:
        """TODO + assignee + no hold shows up with its id."""
        state = _state(
            [
                _task("t-1", TaskStatus.DONE, assigned_to="agent-a"),
                _task("t-2", TaskStatus.TODO, assigned_to="agent-gone"),
                _task("t-3", TaskStatus.TODO),
            ]
        )

        result = await get_project_status(state)

        assert result["success"] is True
        assert result["project"]["stranded_assignments"] == {
            "count": 1,
            "task_ids": ["t-2"],
        }

    @pytest.mark.asyncio
    async def test_a_held_card_is_not_stranded(self) -> None:
        """A persisted assignment row means the kernel holds it; nothing to report."""
        state = _state(
            [_task("t-2", TaskStatus.TODO, assigned_to="agent-1")], persisted={"t-2"}
        )

        result = await get_project_status(state)

        assert result["project"]["stranded_assignments"] == {"count": 0, "task_ids": []}

    @pytest.mark.asyncio
    async def test_in_progress_cards_are_never_reported(self) -> None:
        """A card being worked is IN_PROGRESS with its assignee; that is not residue."""
        state = _state([_task("t-2", TaskStatus.IN_PROGRESS, assigned_to="agent-1")])

        result = await get_project_status(state)

        assert result["project"]["stranded_assignments"]["count"] == 0

    @pytest.mark.asyncio
    async def test_a_computation_failure_does_not_break_the_status(self) -> None:
        """Observational only: the field is omitted, the rest of the status stands."""
        state = _state([_task("t-2", TaskStatus.TODO, assigned_to="agent-gone")])
        with patch(
            "src.marcus_mcp.tools.project.find_stranded_assignments",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await get_project_status(state)

        assert result["success"] is True
        assert "stranded_assignments" not in result["project"]
