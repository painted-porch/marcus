"""
Unit tests for get_all_board_tasks (issue #840).

The tool used to construct a PlankaKanban provider directly with an
empty config, hardwiring it to Planka regardless of the server's
real provider -- a kernel/extension boundary violation (ADR-0013
K1). On a SQLite board (or any non-Planka provider) it failed to
connect and returned success: false with an empty list.
"""

from datetime import datetime, timezone
from typing import Any, List, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.task import get_all_board_tasks

pytestmark = pytest.mark.unit


def _task(task_id: str, project_id: Optional[str]) -> Task:
    now = datetime.now(timezone.utc)
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
        project_id=project_id,
    )


def _make_state(tasks: List[Task]) -> Mock:
    state = Mock()
    state.initialize_kanban = AsyncMock()
    state.kanban_client = Mock()
    state.kanban_client.get_all_tasks = AsyncMock(return_value=tasks)
    return state


class TestGetAllBoardTasksReadsThroughTheConfiguredProvider:
    """get_all_board_tasks must use state.kanban_client, not Planka."""

    @pytest.mark.asyncio
    async def test_returns_tasks_from_a_non_planka_provider(self) -> None:
        """A fake SQLite-shaped state.kanban_client yields success,
        not the Planka-only failure this tool used to have."""
        tasks = [_task("t1", "proj-a"), _task("t2", "proj-a")]
        state = _make_state(tasks)

        result = await get_all_board_tasks(board_id=None, project_id="", state=state)

        assert result["success"] is True
        assert result["count"] == 2
        assert {t["id"] for t in result["tasks"]} == {"t1", "t2"}

    @pytest.mark.asyncio
    async def test_filters_to_the_given_project_id(self) -> None:
        tasks = [
            _task("t1", "proj-a"),
            _task("t2", "proj-a"),
            _task("t3", "proj-b"),
        ]
        state = _make_state(tasks)

        result = await get_all_board_tasks(
            board_id=None, project_id="proj-a", state=state
        )

        assert result["success"] is True
        assert result["count"] == 2
        assert {t["id"] for t in result["tasks"]} == {"t1", "t2"}

    @pytest.mark.asyncio
    async def test_each_task_carries_name_status_and_assigned_to(self) -> None:
        state = _make_state([_task("t1", "proj-a")])

        result = await get_all_board_tasks(board_id=None, project_id="", state=state)

        (task,) = result["tasks"]
        assert task["name"] == "Task t1"
        assert "status" in task
        assert "assigned_to" in task

    @pytest.mark.asyncio
    async def test_never_imports_or_constructs_planka(self) -> None:
        """The whole point of the fix: no Planka provider involved."""
        state = _make_state([])

        result = await get_all_board_tasks(board_id=None, project_id="", state=state)

        assert result["success"] is True
        assert result["count"] == 0
        state.kanban_client.get_all_tasks.assert_awaited_once()


class TestProjectIdFilterAgainstRealProviderShapes:
    """Codex P1, round 1 on PR #842.

    The new project_id filter discarded every task and returned a
    successful *empty* board on Planka and Linear, because those
    providers' card/issue-to-Task conversion never populated
    Task.project_id -- it stayed None, contrary to the docstring. The
    live check (SQLite) could not catch this: SQLite stamps
    project_id. Fixed at the provider level (KanbanClient._card_to_task,
    LinearKanban._linear_issue_to_task) rather than here; this covers
    the caller-side consequence directly, with tasks shaped the way
    those two providers actually produced them before that fix.
    """

    @pytest.mark.asyncio
    async def test_a_none_project_id_is_excluded_by_an_explicit_filter(self) -> None:
        """Without the provider-level fix, every Planka/Linear task
        has project_id=None, so any project_id filter wiped the whole
        board -- this is that failure mode, still correct behavior
        for a task that genuinely carries no project id."""
        tasks = [_task("t1", project_id=None), _task("t2", project_id=None)]
        state = _make_state(tasks)

        result = await get_all_board_tasks(
            board_id=None, project_id="proj-a", state=state
        )

        assert result["success"] is True
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_no_filter_still_returns_project_id_none_tasks(self) -> None:
        """The empty-board regression specifically: omitting the
        filter must still return every task regardless of its
        project_id."""
        tasks = [_task("t1", project_id=None), _task("t2", project_id=None)]
        state = _make_state(tasks)

        result = await get_all_board_tasks(board_id=None, project_id="", state=state)

        assert result["success"] is True
        assert result["count"] == 2


def test_no_direct_planka_construction_in_task_module() -> None:
    """grep-shaped regression guard (issue #840's own verification
    step 4): no tool in task.py constructs a provider directly."""
    import inspect

    from src.marcus_mcp.tools import task as task_module

    source = inspect.getsource(task_module)
    assert "PlankaKanban(" not in source
