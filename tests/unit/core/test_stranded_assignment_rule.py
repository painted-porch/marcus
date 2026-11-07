"""
Unit tests for the stranded-assignment rule (issue #900).

A card is *stranded* when the board says ``TODO``, names an assignee, and
nothing in the kernel holds it: no live lease, no reservation while an
assignment is in flight, no persisted assignment row.
Every kernel path that resets a card to ``TODO`` also clears its assignee,
so this state is residue left by something outside those paths (an
external board edit, a best-effort adapter, a failed design-ghost
completion).
A card someone is working on is ``IN_PROGRESS`` with its assignee, written
in one update, so the rule never fires for live work.

The rule lives in ``src/core/task_claimability.py`` so the drain verdict,
the eligibility filter and the status tool cannot drift apart.
"""

from datetime import datetime, timezone
from typing import Optional

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.core.task_claimability import COORDINATOR_ASSIGNEE, is_stranded_assignment

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _task(status: TaskStatus, assigned_to: Optional[str]) -> Task:
    return Task(
        id="t-1",
        name="Build the thing",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=assigned_to,
        created_at=NOW,
        updated_at=NOW,
        due_date=None,
        estimated_hours=1.0,
        dependencies=[],
        labels=[],
    )


class TestIsStrandedAssignment:
    """TODO, named to someone, held by nothing: that and only that."""

    def test_todo_with_assignee_and_no_hold_is_stranded(self) -> None:
        """The defining case: the board names a holder the kernel does not know."""
        assert (
            is_stranded_assignment(_task(TaskStatus.TODO, "agent-gone"), set()) is True
        )

    def test_a_held_card_is_not_stranded(self) -> None:
        """A lease, a reservation or a persisted row means the kernel holds it."""
        assert (
            is_stranded_assignment(_task(TaskStatus.TODO, "agent-1"), {"t-1"}) is False
        )

    def test_todo_without_an_assignee_is_ordinary_backlog(self) -> None:
        """No assignee means nothing to reclaim; the card is plain TODO."""
        assert is_stranded_assignment(_task(TaskStatus.TODO, None), set()) is False
        assert is_stranded_assignment(_task(TaskStatus.TODO, ""), set()) is False

    def test_in_progress_with_assignee_is_never_stranded(self) -> None:
        """A card being worked is IN_PROGRESS; the rule never touches it, lease or not."""
        assert (
            is_stranded_assignment(_task(TaskStatus.IN_PROGRESS, "agent-1"), set())
            is False
        )

    @pytest.mark.parametrize(
        "status", [TaskStatus.DONE, TaskStatus.BLOCKED, TaskStatus.DECLINED]
    )
    def test_terminal_statuses_are_never_stranded(self, status: TaskStatus) -> None:
        """Settled cards keep their assignee as attribution, not as a hold."""
        assert is_stranded_assignment(_task(status, "agent-1"), set()) is False

    def test_a_coordinator_owned_card_is_stranded_too(self) -> None:
        """The rule is ownership-blind; whether to OFFER such a card is the caller's call."""
        assert COORDINATOR_ASSIGNEE == "Marcus"
        assert (
            is_stranded_assignment(_task(TaskStatus.TODO, COORDINATOR_ASSIGNEE), set())
            is True
        )
