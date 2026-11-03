"""
Unit tests for the unfinished-task list on ``get_project_status``.

A run could end looking successful while a deliverable was silently
missing: counts alone do not say which task never got done or why. The
experiment monitor used to name the unfinished tasks through
``get_experiment_status``; that monitor is gone (ADR-0012 D10), so the
board's own status tool carries the list now. It is published only when
the task fetch is corroborated by the metrics total, because providers
return ``[]`` on error rather than raising and an unreachable board must
never read as "nothing unfinished" (Codex P1 on PR #723).
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.project import get_project_status, unfinished_reason

pytestmark = pytest.mark.unit


def _task(
    task_id: str,
    name: str,
    status: TaskStatus,
    source_context: Optional[Dict[str, Any]] = None,
) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description=name,
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        labels=[],
        dependencies=[],
        source_context=source_context,
    )


def _state(tasks: List[Task], *, fetched: Optional[List[Task]] = None) -> MagicMock:
    """A server state whose board holds ``tasks``; ``fetched`` overrides the read."""
    state = MagicMock()
    state.project_state = MagicMock()
    state.project_state.project_name = "Test"
    state.agent_status = {}
    state.provider = "sqlite"
    active = MagicMock(id="p1", provider="sqlite")
    active.name = "Test"  # ``name`` is special to MagicMock; set it after
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
    kanban.get_all_tasks = AsyncMock(return_value=tasks if fetched is None else fetched)
    kanban.get_task_blockers = AsyncMock(return_value=[])
    state.persistence = MagicMock()
    state.persistence.retrieve = AsyncMock(return_value=None)
    state.kanban_client = kanban
    state.project_manager = MagicMock()
    state.project_manager.get_kanban_client = AsyncMock(return_value=kanban)
    state.project_tasks = tasks
    state.refresh_project_state = AsyncMock()
    return state


class TestUnfinishedTasksOnProjectStatus:
    """The status payload names what did not get built."""

    @pytest.mark.asyncio
    async def test_blocked_task_is_named_with_its_reason(self) -> None:
        """A blocked task appears with id, name, status, and the blocker text."""
        tasks = [
            _task("t1", "Setup", TaskStatus.DONE),
            _task(
                "t2",
                "Implement CLI Entry Point",
                TaskStatus.BLOCKED,
                {"blocker": "needs a paid API key"},
            ),
        ]

        result = await get_project_status(_state(tasks))

        unfinished = result["project"]["unfinished_tasks"]
        assert unfinished == [
            {
                "id": "t2",
                "name": "Implement CLI Entry Point",
                "status": "blocked",
                "reason": "needs a paid API key",
            }
        ]

    @pytest.mark.asyncio
    async def test_never_claimed_task_is_named(self) -> None:
        """A task still in TODO is reported as never claimed."""
        tasks = [_task("t1", "A", TaskStatus.DONE), _task("t2", "B", TaskStatus.TODO)]

        result = await get_project_status(_state(tasks))

        entry = result["project"]["unfinished_tasks"][0]
        assert entry["id"] == "t2"
        assert entry["status"] == "todo"
        assert entry["reason"] == "never claimed by an agent"

    @pytest.mark.asyncio
    async def test_complete_board_reports_an_empty_list(self) -> None:
        """All DONE: the list is present and empty, so consumers can say so."""
        tasks = [_task("t1", "A", TaskStatus.DONE), _task("t2", "B", TaskStatus.DONE)]

        result = await get_project_status(_state(tasks))

        assert result["project"]["unfinished_tasks"] == []

    @pytest.mark.asyncio
    async def test_fetch_contradicting_metrics_is_not_published(self) -> None:
        """An empty read against a non-empty board omits the field entirely."""
        tasks = [_task("t1", "A", TaskStatus.DONE), _task("t2", "B", TaskStatus.TODO)]

        result = await get_project_status(_state(tasks, fetched=[]))

        assert "unfinished_tasks" not in result["project"]
        assert result["project"]["total_tasks"] == 2

    @pytest.mark.asyncio
    async def test_fetch_failure_does_not_break_status(self) -> None:
        """A raising fetch leaves the counts intact and omits the list."""
        tasks = [_task("t1", "A", TaskStatus.DONE)]
        state = _state(tasks)
        state.kanban_client.get_all_tasks = AsyncMock(side_effect=RuntimeError("down"))

        result = await get_project_status(state)

        assert result["success"] is True
        assert result["project"]["completed"] == 1
        assert "unfinished_tasks" not in result["project"]

    @pytest.mark.asyncio
    async def test_provider_without_task_listing_omits_the_field(self) -> None:
        """A client with no get_all_tasks still answers with counts."""
        tasks = [_task("t1", "A", TaskStatus.TODO)]
        state = _state(tasks)
        del state.kanban_client.get_all_tasks

        result = await get_project_status(state)

        assert result["success"] is True
        assert "unfinished_tasks" not in result["project"]


class TestUnfinishedReason:
    """One line per task that says why it never reached DONE."""

    @pytest.mark.asyncio
    async def test_source_context_blocker_wins(self) -> None:
        """Blocker text on the task row is used as is."""
        task = _task("t", "X", TaskStatus.BLOCKED, {"blocker": "from source_context"})
        client = MagicMock()
        client.get_task_blockers = AsyncMock(return_value=[{"description": "store"}])

        assert await unfinished_reason(task, client) == "from source_context"

    @pytest.mark.asyncio
    async def test_blocker_store_is_consulted_when_row_is_silent(self) -> None:
        """The provider's blocker record explains a blocked task."""
        task = _task("t", "X", TaskStatus.BLOCKED)
        client = MagicMock()
        client.get_task_blockers = AsyncMock(
            return_value=[{"description": "needs a paid API key"}]
        )

        assert await unfinished_reason(task, client) == "needs a paid API key"

    @pytest.mark.asyncio
    async def test_most_recent_blocker_wins(self) -> None:
        """A task blocked twice reports why it is blocked now."""
        task = _task("t", "X", TaskStatus.BLOCKED)
        client = MagicMock()
        client.get_task_blockers = AsyncMock(
            return_value=[{"description": "first"}, {"description": "second"}]
        )

        assert await unfinished_reason(task, client) == "second"

    @pytest.mark.asyncio
    async def test_blocker_lookup_failure_degrades_to_fallback(self) -> None:
        """A failing store never drops the entry; the state is described instead."""
        task = _task("t", "X", TaskStatus.BLOCKED)
        client = MagicMock()
        client.get_task_blockers = AsyncMock(side_effect=RuntimeError("db"))

        assert await unfinished_reason(task, client) == (
            "blocked (no blocker text recorded)"
        )

    @pytest.mark.asyncio
    async def test_in_progress_and_todo_have_plain_descriptions(self) -> None:
        """Non-blocked states get a plain, never-empty description."""
        assert (
            await unfinished_reason(_task("t", "X", TaskStatus.IN_PROGRESS), None)
            == "still in progress when the run ended"
        )
        assert (
            await unfinished_reason(_task("t", "X", TaskStatus.TODO), None)
            == "never claimed by an agent"
        )


class TestProjectStatusEdges:
    """The error branch and the refresh helper the status tool leans on."""

    @pytest.mark.asyncio
    async def test_no_project_state_is_an_error_not_a_crash(self) -> None:
        """Without a project state the tool says so instead of raising."""
        state = _state([])
        state.project_state = None

        result = await get_project_status(state)

        assert result["success"] is False
        assert "No project state" in result["error"]

    @pytest.mark.asyncio
    async def test_refresh_project_state_stores_tasks_and_logs_counts(self) -> None:
        """The refresh helper stores the board's tasks and logs the counts."""
        from src.marcus_mcp.tools.project import refresh_project_state

        tasks = [
            _task("t1", "A", TaskStatus.DONE),
            _task("t2", "B", TaskStatus.IN_PROGRESS),
            _task("t3", "C", TaskStatus.BLOCKED),
            _task("t4", "D", TaskStatus.TODO),
        ]
        state = MagicMock()
        state.kanban_client.get_available_tasks = AsyncMock(return_value=tasks)
        state.monitor = None
        state.log_event = MagicMock()

        await refresh_project_state(state)

        assert state.project_tasks == tasks
        counts = state.log_event.call_args.args[1]
        assert counts == {
            "total_tasks": 4,
            "completed": 1,
            "in_progress": 1,
            "todo": 1,
            "blocked": 1,
        }

    @pytest.mark.asyncio
    async def test_refresh_project_state_logs_and_reraises(self) -> None:
        """A provider failure is logged as an event and propagated."""
        from src.marcus_mcp.tools.project import refresh_project_state

        state = MagicMock()
        state.kanban_client.get_available_tasks = AsyncMock(
            side_effect=RuntimeError("board down")
        )
        state.log_event = MagicMock()

        with pytest.raises(RuntimeError):
            await refresh_project_state(state)

        assert state.log_event.call_args.args[0] == "refresh_project_state_error"


class TestEpochCollisionsReported:
    """The status payload carries the record-only collision count (D11)."""

    @pytest.mark.asyncio
    async def test_a_clean_project_reports_zero(self) -> None:
        """No observations means zero, present rather than absent."""
        result = await get_project_status(_state([_task("t1", "A", TaskStatus.DONE)]))

        assert result["project"]["epoch_collisions"] == 0

    @pytest.mark.asyncio
    async def test_this_boards_observations_are_counted(self) -> None:
        """The count sums the roll-up rows of this board's tasks only."""
        state = _state(
            [_task("t1", "A", TaskStatus.DONE), _task("t2", "B", TaskStatus.TODO)]
        )
        rows = {"t1": {"count": 2}, "t9": {"count": 7}}
        state.persistence.retrieve = AsyncMock(
            side_effect=lambda collection, key: rows.get(key)
        )

        result = await get_project_status(state)

        assert result["project"]["epoch_collisions"] == 2

    @pytest.mark.asyncio
    async def test_a_store_failure_does_not_break_the_status(self) -> None:
        """The count degrades to zero; the rest of the payload is intact."""
        state = _state([_task("t1", "A", TaskStatus.DONE)])
        state.persistence.retrieve = AsyncMock(side_effect=RuntimeError("locked"))

        result = await get_project_status(state)

        assert result["success"] is True
        assert result["project"]["epoch_collisions"] == 0
