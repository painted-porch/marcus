"""
Unit tests for the retry-interval jitter (epic #706 Milestone A).

Workers that register together were all told to wait the same 30 seconds,
so they polled in lockstep for the whole run: N requests in the same
second every half minute, and N-1 of them losing the file-lock race for
the task the first one just claimed. Every retry interval Marcus hands a
worker now carries a symmetric spread, so the pollers decorrelate after
one wait while the mean cadence stays at the constant PR #177 chose.
"""

from statistics import mean
from unittest.mock import Mock, patch

import pytest

from src.marcus_mcp.tools.task import (
    RETRY_AFTER_SECONDS,
    RETRY_JITTER_FRACTION,
    jittered_retry_seconds,
)

pytestmark = pytest.mark.unit

# Computed the way the helper does, so the float arithmetic matches exactly.
SPREAD = RETRY_AFTER_SECONDS * RETRY_JITTER_FRACTION
LOW = RETRY_AFTER_SECONDS - SPREAD
HIGH = RETRY_AFTER_SECONDS + SPREAD


class TestJitteredRetrySeconds:
    """The spread: bounded, centred on the constant, and never degenerate."""

    def test_every_draw_is_a_whole_number_inside_the_spread(self) -> None:
        """A worker is never told to wait outside the documented band."""
        draws = [jittered_retry_seconds() for _ in range(400)]
        assert all(isinstance(d, int) for d in draws)
        assert all(round(LOW) <= d <= round(HIGH) for d in draws), (
            f"draws outside [{round(LOW)}, {round(HIGH)}]: "
            f"{sorted(set(d for d in draws if not round(LOW) <= d <= round(HIGH)))}"
        )

    def test_the_mean_stays_at_the_constant(self) -> None:
        """The spread is symmetric: it does not lengthen the average wait."""
        draws = [jittered_retry_seconds() for _ in range(2000)]
        assert abs(mean(draws) - RETRY_AFTER_SECONDS) < 1.0

    def test_draws_are_not_all_the_same(self) -> None:
        """The point of the change: two workers get different waits."""
        draws = {jittered_retry_seconds() for _ in range(50)}
        assert len(draws) > 5

    def test_a_custom_base_is_spread_around_itself(self) -> None:
        """The helper spreads whatever interval it is given."""
        draws = [jittered_retry_seconds(300) for _ in range(200)]
        assert all(201 <= d <= 399 for d in draws)
        assert abs(mean(draws) - 300) < 10

    def test_a_tiny_base_never_yields_a_zero_wait(self) -> None:
        """A zero wait would turn the retry into a busy loop."""
        assert all(jittered_retry_seconds(1) >= 1 for _ in range(100))
        assert jittered_retry_seconds(0) == 1

    def test_the_draw_comes_from_the_system_random_source(self) -> None:
        """The helper uses the codebase's SystemRandom, as the other jitters do."""
        with patch("secrets.SystemRandom") as system_random:
            system_random.return_value.uniform.return_value = 23.4
            assert jittered_retry_seconds() == 23
        system_random.return_value.uniform.assert_called_once_with(LOW, HIGH)


class TestRetryResponsesCarryJitter:
    """Every retry interval the server hands an agent is jittered."""

    @pytest.mark.asyncio
    async def test_no_tasks_in_progress_path_is_jittered(self) -> None:
        """The simple path (nothing in flight) still spreads its answer."""
        from src.marcus_mcp.tools.task import calculate_retry_after_seconds

        state = Mock()
        state.agent_tasks = {}
        state.project_tasks = []
        state.agent_status = {}

        with patch(
            "src.marcus_mcp.tools.task.jittered_retry_seconds", return_value=27
        ) as jitter:
            result = await calculate_retry_after_seconds(state)

        assert result["retry_after_seconds"] == 27
        jitter.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_the_blocking_task_path_is_jittered_not_eta_derived(self) -> None:
        """A long ETA still yields the jittered constant, never a longer wait."""
        from datetime import datetime, timedelta, timezone

        from src.core.models import Priority, Task, TaskAssignment, TaskStatus
        from src.marcus_mcp.tools.task import calculate_retry_after_seconds

        now = datetime.now(timezone.utc)
        task = Task(
            id="task-1",
            name="A very long task",
            description="",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.MEDIUM,
            assigned_to="agent-1",
            created_at=now,
            updated_at=now,
            due_date=None,
            estimated_hours=40.0,
        )
        assignment = Mock(spec=TaskAssignment)
        assignment.task_id = "task-1"
        assignment.assigned_at = now - timedelta(minutes=1)
        state = Mock()
        state.agent_tasks = {"agent-1": assignment}
        state.project_tasks = [task]
        state.agent_status = {"agent-1": Mock()}
        state.memory = None

        with patch("src.marcus_mcp.tools.task.jittered_retry_seconds", return_value=35):
            result = await calculate_retry_after_seconds(state)

        assert result["retry_after_seconds"] == 35
        assert result["blocking_task"]["eta_seconds"] > 35

    def test_the_file_lock_race_retry_is_jittered(self) -> None:
        """Two agents that raced for one task must not come back together."""
        import inspect

        from src.marcus_mcp.tools.task import request_next_task

        source = inspect.getsource(request_next_task)
        race_at = source.index('"retry_reason": "file_lock_race"')
        race = source[race_at - 400 : race_at]
        assert "jittered_retry_seconds()" in race
        assert '"retry_after_seconds": 30' not in race
