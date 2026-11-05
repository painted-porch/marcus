"""
Unit tests for the K13 kernel invariant (ADR-0013): ``update_task``
leaves the caller's in-memory view consistent with what it just wrote.

The same staleness shape was found independently three times in one
day (#807, #816, #814): something wrote the board, and something else
in the SAME request read ``state.project_tasks`` expecting that write
to already be visible. ``TaskSyncingKanbanClient`` is the fix applied
once, at the ``state.kanban_client`` boundary, rather than swept per
call site.
"""

from datetime import datetime, timezone
from typing import Any, List
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.kanban_task_sync import TaskSyncingKanbanClient, sync_on_write

pytestmark = pytest.mark.unit


def _task(task_id: str, status: TaskStatus = TaskStatus.TODO, **kw: Any) -> Task:
    """Build a minimal Task."""
    now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=kw.get("assigned_to"),
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        inputs=kw.get("inputs", {}),
    )


class TestTheWriteSyncsTheMatchingInMemoryTask:
    """The core guarantee: a successful write is visible immediately."""

    @pytest.mark.asyncio
    async def test_status_and_assigned_to_are_synced(self) -> None:
        """The exact #814/#816 shape: status + assigned_to must land."""
        task = _task("t1")
        inner = Mock()
        inner.update_task = AsyncMock(return_value=None)
        wrapped = TaskSyncingKanbanClient(inner, lambda: [task])

        await wrapped.update_task(
            "t1", {"status": TaskStatus.IN_PROGRESS, "assigned_to": "agent-1"}
        )

        assert task.status == TaskStatus.IN_PROGRESS
        assert task.assigned_to == "agent-1"

    @pytest.mark.asyncio
    async def test_only_the_passed_keys_are_touched(self) -> None:
        """A field the caller did not mention is left alone."""
        task = _task("t1", assigned_to="someone")
        inner = Mock()
        inner.update_task = AsyncMock(return_value=None)
        wrapped = TaskSyncingKanbanClient(inner, lambda: [task])

        await wrapped.update_task("t1", {"status": TaskStatus.DONE})

        assert task.status == TaskStatus.DONE
        assert task.assigned_to == "someone"

    @pytest.mark.asyncio
    async def test_a_non_task_attribute_is_not_set(self) -> None:
        """Board-only keys (blocker, progress) are not Task attributes.

        A bare setattr would create a new, incorrect attribute rather
        than updating a real one -- several call sites pass these.
        """
        task = _task("t1")
        inner = Mock()
        inner.update_task = AsyncMock(return_value=None)
        wrapped = TaskSyncingKanbanClient(inner, lambda: [task])

        await wrapped.update_task(
            "t1", {"status": TaskStatus.BLOCKED, "blocker": "stuck", "progress": 40}
        )

        assert task.status == TaskStatus.BLOCKED
        assert not hasattr(task, "blocker")
        assert not hasattr(task, "progress")

    @pytest.mark.asyncio
    async def test_the_board_write_still_happens(self) -> None:
        """The real client's update_task is actually called."""
        task = _task("t1")
        inner = Mock()
        inner.update_task = AsyncMock(return_value=None)
        wrapped = TaskSyncingKanbanClient(inner, lambda: [task])

        await wrapped.update_task("t1", {"status": TaskStatus.DONE})

        inner.update_task.assert_awaited_once_with("t1", {"status": TaskStatus.DONE})

    @pytest.mark.asyncio
    async def test_the_boards_return_value_is_passed_through(self) -> None:
        """Callers that use update_task's return value still get it."""
        task = _task("t1")
        returned = _task("t1", status=TaskStatus.DONE)
        inner = Mock()
        inner.update_task = AsyncMock(return_value=returned)
        wrapped = TaskSyncingKanbanClient(inner, lambda: [task])

        result = await wrapped.update_task("t1", {"status": TaskStatus.DONE})

        assert result is returned


class TestItCoercesAStringStatusToTheEnum:
    """Found by this module's own sweep: one caller passed status as a
    plain string ("done") instead of TaskStatus.DONE. Fixed at the
    source, and the wrapper coerces defensively too, since the rest of
    the codebase compares task.status against TaskStatus members, not
    strings.
    """

    @pytest.mark.asyncio
    async def test_a_string_status_lands_as_the_matching_enum(self) -> None:
        task = _task("t1")
        inner = Mock()
        inner.update_task = AsyncMock(return_value=None)
        wrapped = TaskSyncingKanbanClient(inner, lambda: [task])

        await wrapped.update_task("t1", {"status": "done"})

        assert task.status == TaskStatus.DONE
        assert isinstance(task.status, TaskStatus)

    @pytest.mark.asyncio
    async def test_an_unrecognized_string_status_is_left_as_is(self) -> None:
        task = _task("t1")
        inner = Mock()
        inner.update_task = AsyncMock(return_value=None)
        wrapped = TaskSyncingKanbanClient(inner, lambda: [task])

        await wrapped.update_task("t1", {"status": "not_a_real_status"})

        assert task.status == "not_a_real_status"


class TestItDoesNotTouchTheWrongTask:
    """Only the task named by task_id is affected."""

    @pytest.mark.asyncio
    async def test_other_tasks_in_the_list_are_untouched(self) -> None:
        other = _task("other")
        task = _task("t1")
        inner = Mock()
        inner.update_task = AsyncMock(return_value=None)
        wrapped = TaskSyncingKanbanClient(inner, lambda: [other, task])

        await wrapped.update_task("t1", {"status": TaskStatus.DONE})

        assert other.status == TaskStatus.TODO
        assert task.status == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_a_task_id_with_no_in_memory_match_does_not_raise(self) -> None:
        """A reconciliation/audit card not tracked in project_tasks: no crash."""
        task = _task("t1")
        inner = Mock()
        inner.update_task = AsyncMock(return_value=None)
        wrapped = TaskSyncingKanbanClient(inner, lambda: [task])

        result = await wrapped.update_task("ghost", {"status": TaskStatus.DONE})

        assert result is None
        assert task.status == TaskStatus.TODO


class TestItDegradesRatherThanFailing:
    """A sync failure must never fail a write that already landed."""

    @pytest.mark.asyncio
    async def test_a_failed_board_write_is_not_synced_and_still_raises(self) -> None:
        """If the write itself fails, nothing is synced, and the caller
        sees the real exception -- "memory agrees with what landed,"
        not "memory agrees regardless of whether anything landed."""
        task = _task("t1")
        inner = Mock()
        inner.update_task = AsyncMock(side_effect=RuntimeError("board down"))
        wrapped = TaskSyncingKanbanClient(inner, lambda: [task])

        with pytest.raises(RuntimeError, match="board down"):
            await wrapped.update_task("t1", {"status": TaskStatus.DONE})

        assert task.status == TaskStatus.TODO

    @pytest.mark.asyncio
    async def test_a_broken_tasks_source_does_not_fail_the_write(self) -> None:
        """The write succeeded on the board; a bookkeeping miss must not
        undo that for the caller."""
        inner = Mock()
        inner.update_task = AsyncMock(return_value="ok")

        def _boom() -> List[Task]:
            raise RuntimeError("project_tasks unavailable")

        wrapped = TaskSyncingKanbanClient(inner, _boom)

        result = await wrapped.update_task("t1", {"status": TaskStatus.DONE})

        assert result == "ok"


class TestDelegationAndIdentity:
    """Everything else about the wrapper must be transparent."""

    @pytest.mark.asyncio
    async def test_other_methods_delegate_to_the_real_client(self) -> None:
        inner = Mock()
        inner.get_all_tasks = AsyncMock(return_value=["a", "b"])
        wrapped = TaskSyncingKanbanClient(inner, lambda: [])

        assert await wrapped.get_all_tasks() == ["a", "b"]

    def test_attribute_access_delegates(self) -> None:
        inner = Mock()
        inner.board_id = "board-123"
        wrapped = TaskSyncingKanbanClient(inner, lambda: [])

        assert wrapped.board_id == "board-123"

    def test_equality_compares_the_wrapped_client(self) -> None:
        """A caller that stashed the raw client and later compares it
        against state.kanban_client must still find them equal (common
        in tests that do ``assert server.kanban_client == mock_client``)."""
        inner = Mock()
        wrapped = TaskSyncingKanbanClient(inner, lambda: [])

        assert wrapped == inner
        assert inner == wrapped

    def test_truthiness_follows_the_wrapped_client(self) -> None:
        wrapped = TaskSyncingKanbanClient(Mock(), lambda: [])
        assert bool(wrapped) is True


class TestSyncOnWrite:
    """The factory applied at every production assignment site."""

    def test_none_passes_through(self) -> None:
        assert sync_on_write(None, lambda: []) is None

    def test_a_real_client_is_wrapped(self) -> None:
        inner = Mock()
        wrapped = sync_on_write(inner, lambda: [])

        assert isinstance(wrapped, TaskSyncingKanbanClient)
        assert wrapped == inner

    def test_an_already_wrapped_client_is_not_double_wrapped(self) -> None:
        inner = Mock()
        once = sync_on_write(inner, lambda: [])
        twice = sync_on_write(once, lambda: [])

        assert twice is once
