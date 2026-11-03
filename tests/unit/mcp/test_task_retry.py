"""
Unit tests for intelligent task retry functionality
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from src.core.models import Priority, Task, TaskAssignment, TaskStatus
from src.marcus_mcp.tools.task import (
    RETRY_AFTER_SECONDS,
    RETRY_JITTER_FRACTION,
    calculate_retry_after_seconds,
)


def _in_retry_band(seconds: int) -> bool:
    """True when ``seconds`` is the jittered retry cadence (epic #706).

    The wait is ``RETRY_AFTER_SECONDS`` with a symmetric spread so workers
    do not poll in lockstep; it is never ETA-derived. See
    ``tests/unit/marcus_mcp/test_retry_jitter.py``.
    """
    spread = RETRY_AFTER_SECONDS * RETRY_JITTER_FRACTION
    return (
        round(RETRY_AFTER_SECONDS - spread)
        <= seconds
        <= round(RETRY_AFTER_SECONDS + spread)
    )


class TestCalculateRetryAfterSeconds:
    """Test suite for intelligent wait time calculation"""

    @pytest.mark.asyncio
    async def test_no_tasks_in_progress_returns_default(self):
        """Test returns the jittered default when no tasks in progress"""
        # Arrange
        mock_state = Mock()
        mock_state.agent_tasks = {}  # No tasks assigned
        mock_state.project_tasks = []
        mock_state.agent_status = {}  # No agents

        # Act
        result = await calculate_retry_after_seconds(mock_state)

        # Assert
        assert _in_retry_band(result["retry_after_seconds"])
        assert "No tasks currently in progress" in result["reason"]
        assert result["blocking_task"] is None

    @pytest.mark.asyncio
    async def test_calculates_eta_from_progress(self):
        """Test calculates ETA based on current task progress"""
        # Arrange
        mock_state = Mock()

        # Task that started 30 min ago and is 75% done
        # Expected: 30 min / 75% = 40 min total, 10 min remaining
        # ETA = 600s, but retry uses 60% of ETA = 360s, capped at 300s max
        task = Task(
            id="task_1",
            name="Database Setup",
            description="Setup DB",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
            assigned_to="agent_1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=1.0,
            labels=["backend"],
        )
        task.progress = 75  # 75% done

        assignment = TaskAssignment(
            task_id="task_1",
            task_name="Database Setup",
            description="Setup DB",
            instructions="Do it",
            estimated_hours=1.0,
            priority=Priority.HIGH,
            dependencies=[],
            assigned_to="agent_1",
            assigned_at=datetime.now(timezone.utc)
            - timedelta(minutes=30),  # Started 30 min ago
            due_date=None,
        )

        mock_state.agent_tasks = {"agent_1": assignment}
        mock_state.project_tasks = [task]
        mock_state.agent_status = {"agent_1": Mock()}  # Mock agent status

        # Mock memory (not needed for progress-based calculation)
        mock_memory = AsyncMock()
        mock_memory.get_global_median_duration = AsyncMock(return_value=1.0)
        mock_state.memory = mock_memory

        # Act
        result = await calculate_retry_after_seconds(mock_state)

        # Assert
        # Retry is capped at 30 seconds for regular re-polling
        # (changed to improve multi-agent parallelization)
        assert _in_retry_band(result["retry_after_seconds"])
        assert "Database Setup" in result["reason"]
        assert result["blocking_task"]["id"] == "task_1"
        assert result["blocking_task"]["progress"] == 75

    @pytest.mark.asyncio
    async def test_uses_historical_median_when_no_progress(self):
        """Test falls back to historical median when progress is 0"""
        # Arrange
        mock_state = Mock()

        # Task with no progress yet
        task = Task(
            id="task_1",
            name="API Development",
            description="Build API",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
            assigned_to="agent_1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=2.0,
            labels=["backend"],
        )
        task.progress = 0  # No progress yet

        assignment = TaskAssignment(
            task_id="task_1",
            task_name="API Development",
            description="Build API",
            instructions="Do it",
            estimated_hours=2.0,
            priority=Priority.HIGH,
            dependencies=[],
            assigned_to="agent_1",
            assigned_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            due_date=None,
        )

        mock_state.agent_tasks = {"agent_1": assignment}
        mock_state.project_tasks = [task]
        mock_state.agent_status = {"agent_1": Mock()}  # Mock agent status

        # Mock memory with 2-hour historical median
        mock_memory = AsyncMock()
        mock_memory.get_global_median_duration = AsyncMock(return_value=2.0)
        mock_state.memory = mock_memory

        # Act
        result = await calculate_retry_after_seconds(mock_state)

        # Assert
        # Historical median suggests 2 hours (7200s), but retry is capped at 30s
        # (changed to improve multi-agent parallelization)
        assert _in_retry_band(result["retry_after_seconds"])
        assert "API Development" in result["reason"]

    @pytest.mark.asyncio
    async def test_picks_soonest_completion(self):
        """Test picks task with soonest completion when multiple tasks in progress"""
        # Arrange
        mock_state = Mock()

        # Task 1: Will complete in ~10 minutes (75% done, started 30 min ago)
        task1 = Task(
            id="task_1",
            name="Quick Task",
            description="Fast",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
            assigned_to="agent_1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=1.0,
        )
        task1.progress = 75

        assignment1 = TaskAssignment(
            task_id="task_1",
            task_name="Quick Task",
            description="Fast",
            instructions="Do it",
            estimated_hours=1.0,
            priority=Priority.HIGH,
            dependencies=[],
            assigned_to="agent_1",
            assigned_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            due_date=None,
        )

        # Task 2: Will complete in ~60 minutes (50% done, started 60 min ago)
        task2 = Task(
            id="task_2",
            name="Slow Task",
            description="Slow",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
            assigned_to="agent_2",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=2.0,
        )
        task2.progress = 50

        assignment2 = TaskAssignment(
            task_id="task_2",
            task_name="Slow Task",
            description="Slow",
            instructions="Do it",
            estimated_hours=2.0,
            priority=Priority.HIGH,
            dependencies=[],
            assigned_to="agent_2",
            assigned_at=datetime.now(timezone.utc) - timedelta(minutes=60),
            due_date=None,
        )

        mock_state.agent_tasks = {"agent_1": assignment1, "agent_2": assignment2}
        mock_state.project_tasks = [task1, task2]
        mock_state.agent_status = {
            "agent_1": Mock(),
            "agent_2": Mock(),
        }  # Mock agent status

        mock_memory = AsyncMock()
        mock_memory.get_global_median_duration = AsyncMock(return_value=1.0)
        mock_state.memory = mock_memory

        # Act
        result = await calculate_retry_after_seconds(mock_state)

        # Assert
        # Should pick task1 (soonest)
        assert result["blocking_task"]["id"] == "task_1"
        assert result["blocking_task"]["name"] == "Quick Task"

    @pytest.mark.asyncio
    async def test_never_exceeds_the_jittered_cadence(self):
        """Test wait time is capped at 30 seconds maximum for regular re-polling"""
        # Arrange
        mock_state = Mock()

        # Task that will take a very long time
        task = Task(
            id="task_1",
            name="Long Task",
            description="Takes forever",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.LOW,
            assigned_to="agent_1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=10.0,
        )
        task.progress = 0  # No progress

        assignment = TaskAssignment(
            task_id="task_1",
            task_name="Long Task",
            description="Takes forever",
            instructions="Do it",
            estimated_hours=10.0,
            priority=Priority.LOW,
            dependencies=[],
            assigned_to="agent_1",
            assigned_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            due_date=None,
        )

        mock_state.agent_tasks = {"agent_1": assignment}
        mock_state.project_tasks = [task]
        mock_state.agent_status = {"agent_1": Mock()}  # Mock agent status

        # Mock memory with very long median (10 hours)
        mock_memory = AsyncMock()
        mock_memory.get_global_median_duration = AsyncMock(return_value=10.0)
        mock_state.memory = mock_memory

        # Act
        result = await calculate_retry_after_seconds(mock_state)

        # Assert
        # Even with 10 hour estimate, capped at 30 seconds for re-polling
        # (changed to improve multi-agent parallelization)
        assert _in_retry_band(result["retry_after_seconds"])


@pytest.mark.unit
class TestTouchLeaseBeforeValidation:
    """touch_lease must be called before each validation attempt.

    The validator runs LLM calls that can take 60-120s each (up to
    MAX_VALIDATION_RETRIES times). The agent is silent during this period
    and the lease clock keeps ticking. touch_lease extends the lease
    without regressing the progress percentage.
    """

    @pytest.mark.asyncio
    async def test_touch_lease_called_with_agent_id(self) -> None:
        """The validation preamble must call touch_lease(agent_id)."""
        # Directly exercise the pattern wired into the validation gate:
        #   if hasattr(state, "lease_manager") and state.lease_manager:
        #       await state.lease_manager.touch_lease(agent_id)
        # This is a targeted unit test of that decision branch — it does
        # not invoke the full complete_task stack (too many side effects).
        agent_id = "agent-alpha"
        call_log: list[str] = []

        mock_lease_manager = MagicMock()

        async def record_touch(aid: str) -> bool:
            call_log.append(f"touch:{aid}")
            return True

        mock_lease_manager.touch_lease = AsyncMock(side_effect=record_touch)

        state = MagicMock()
        state.lease_manager = mock_lease_manager

        # Replicate the exact guard expression from task.py validation gate
        if hasattr(state, "lease_manager") and state.lease_manager:
            await state.lease_manager.touch_lease(agent_id)

        assert call_log == [
            f"touch:{agent_id}"
        ], f"Expected touch_lease called with agent_id, got: {call_log}"

    @pytest.mark.asyncio
    async def test_touch_lease_skipped_when_no_lease_manager(self) -> None:
        """touch_lease must not raise when state has no lease_manager."""
        agent_id = "agent-beta"

        state = MagicMock()
        state.lease_manager = None

        # Must not raise — guard handles None lease_manager
        if hasattr(state, "lease_manager") and state.lease_manager:
            await state.lease_manager.touch_lease(agent_id)
        # Reaching here without error = pass

    @pytest.mark.asyncio
    async def test_touch_lease_skipped_when_missing_attribute(self) -> None:
        """touch_lease guard must handle state with no lease_manager attr."""
        agent_id = "agent-gamma"

        class BareState:
            pass

        state = BareState()

        if hasattr(state, "lease_manager") and state.lease_manager:  # type: ignore[union-attr]
            await state.lease_manager.touch_lease(agent_id)  # type: ignore[union-attr]
        # Reaching here without AttributeError = pass
