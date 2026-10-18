"""
Unit tests for ``_carry_forward_active_subtasks`` (issue #667, Option 2).

When ``refresh_project_state`` rebuilds Marcus's in-memory task list, it
must carry forward only the subtasks whose parent is still on the board.
Orphaned subtasks (parent gone, or left over from another project in a
long-lived server process) are dropped from the in-memory working set so
they stop bloating memory and churning the dependency-inference cache
signature on the ``request_next_task`` hot path.

This helper trims the in-memory list ONLY — the durable record in
``data/marcus_state/subtasks.json`` (which Cato reads directly as its
subtask data source) is never touched. See issue #672 for the proper
fix (move subtask storage into the database).
"""

from datetime import datetime, timezone
from typing import Optional

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.server import _carry_forward_active_subtasks

pytestmark = pytest.mark.unit


def _task(
    task_id: str, *, is_subtask: bool = False, parent_task_id: Optional[str] = None
) -> Task:
    """Build a minimal Task, optionally flagged as a subtask."""
    return Task(
        id=task_id,
        name=task_id,
        description="",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        estimated_hours=1.0,
        dependencies=[],
        labels=[],
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        is_subtask=is_subtask,
        parent_task_id=parent_task_id,
    )


class TestCarryForwardActiveSubtasks:
    """Only subtasks with a live parent survive; the file is never touched."""

    def test_keeps_subtasks_whose_parent_is_on_the_board(self) -> None:
        """A subtask whose parent id is in ``parent_ids`` is carried forward."""
        sub = _task("p1_sub_1", is_subtask=True, parent_task_id="p1")
        kept, dropped = _carry_forward_active_subtasks([sub], {"p1"})
        assert kept == [sub]
        assert dropped == 0

    def test_drops_orphaned_subtasks_whose_parent_is_gone(self) -> None:
        """A subtask whose parent is no longer on the board is dropped (counted)."""
        live = _task("p1_sub_1", is_subtask=True, parent_task_id="p1")
        orphan = _task("old_sub_1", is_subtask=True, parent_task_id="old-parent")
        kept, dropped = _carry_forward_active_subtasks([live, orphan], {"p1"})
        assert kept == [live]
        assert dropped == 1

    def test_ignores_non_subtasks(self) -> None:
        """Parents (non-subtasks) are not returned — the caller re-adds them."""
        parent = _task("p1", is_subtask=False)
        sub = _task("p1_sub_1", is_subtask=True, parent_task_id="p1")
        kept, dropped = _carry_forward_active_subtasks([parent, sub], {"p1"})
        assert kept == [sub]
        assert dropped == 0

    def test_handles_none_task_list(self) -> None:
        """A ``None`` task list yields no kept subtasks and no drops."""
        kept, dropped = _carry_forward_active_subtasks(None, {"p1"})
        assert kept == []
        assert dropped == 0

    def test_empty_parent_set_preserves_all_subtasks(self) -> None:
        """A transient empty board fetch must NOT orphan-GC subtasks.

        When ``get_all_tasks`` returns ``[]`` on error (the Planka provider
        does this), ``parent_ids`` is empty. Treating that as "every parent
        is gone" would drop every subtask, and the one-time migration guard
        would never re-add them -- stranding agents until restart. An empty
        parent set must therefore preserve all subtasks (Codex P1, PR #673).
        """
        s1 = _task("p1_sub_1", is_subtask=True, parent_task_id="p1")
        s2 = _task("p2_sub_1", is_subtask=True, parent_task_id="p2")
        kept, dropped = _carry_forward_active_subtasks([s1, s2], set())
        assert kept == [s1, s2]
        assert dropped == 0
