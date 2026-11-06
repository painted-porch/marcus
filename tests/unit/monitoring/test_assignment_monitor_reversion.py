"""
Unit tests for AssignmentMonitor._detect_reversion.

ADR-0013 K5, Codex round-6 sweep on PR #834: the monitor already
detects an unassigned BLOCKED task as a reversion (Case 4), so a
worker that was silently unassigned does not stay stuck believing it
still holds the task. DECLINED is the same shape -- terminal and
unassigned after request_task_decline -- and needed the identical
case.
"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.monitoring.assignment_monitor import AssignmentMonitor

pytestmark = pytest.mark.unit


def _task(task_id: str, status: TaskStatus, assigned_to: str | None) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=assigned_to,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
    )


@pytest.fixture
def monitor() -> AssignmentMonitor:
    return AssignmentMonitor(persistence=Mock(), kanban_client=Mock())


class TestDetectReversion:
    """Test suite for the reversion cases _detect_reversion recognizes."""

    @pytest.mark.asyncio
    async def test_declined_and_unassigned_is_a_reversion(
        self, monitor: AssignmentMonitor
    ) -> None:
        task = _task("t1", TaskStatus.DECLINED, assigned_to=None)

        reverted = await monitor._detect_reversion(task, "agent-a")

        assert reverted is True

    @pytest.mark.asyncio
    async def test_declined_but_still_assigned_is_not_this_case(
        self, monitor: AssignmentMonitor
    ) -> None:
        """Guards against a false positive: this check is specifically
        about the unassigned shape, matching Case 4's own scoping."""
        task = _task("t1", TaskStatus.DECLINED, assigned_to="agent-a")

        reverted = await monitor._detect_reversion(task, "agent-a")

        assert reverted is False

    @pytest.mark.asyncio
    async def test_blocked_and_unassigned_is_still_a_reversion(
        self, monitor: AssignmentMonitor
    ) -> None:
        """Pre-existing Case 4, confirmed unchanged by this fix."""
        task = _task("t1", TaskStatus.BLOCKED, assigned_to=None)

        reverted = await monitor._detect_reversion(task, "agent-a")

        assert reverted is True
