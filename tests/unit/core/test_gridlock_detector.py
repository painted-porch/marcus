"""
Unit tests for GridlockDetector.

Tests the gridlock detection logic, focusing on distinguishing
true gridlock (all tasks blocked, no progress possible) from
normal steady state (agents polling while tasks are in-progress).
"""

from datetime import datetime, timezone
from typing import List, Optional

import pytest

from src.core.gridlock_detector import GridlockDetector
from src.core.models import Priority, Task, TaskStatus


def _make_task(
    task_id: str,
    status: TaskStatus = TaskStatus.TODO,
    dependencies: Optional[List[str]] = None,
) -> Task:
    """
    Create a minimal task for testing.

    Parameters
    ----------
    task_id : str
        Task identifier
    status : TaskStatus
        Current task status
    dependencies : Optional[List[str]]
        List of dependency task IDs

    Returns
    -------
    Task
        Test task instance
    """
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="Test task",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=2.0,
        labels=[],
        dependencies=dependencies or [],
    )


@pytest.mark.unit
class TestGridlockDetection:
    """Test gridlock detection accuracy."""

    @pytest.fixture
    def detector(self) -> GridlockDetector:
        """Create a gridlock detector with default settings."""
        return GridlockDetector()

    def test_true_gridlock_all_blocked_nothing_in_progress(
        self, detector: GridlockDetector
    ) -> None:
        """Test gridlock detected: all TODO blocked, nothing in-progress."""
        tasks = [
            _make_task("done-1", TaskStatus.DONE),
            _make_task("blocked-1", TaskStatus.TODO, dependencies=["missing-1"]),
            _make_task("blocked-2", TaskStatus.TODO, dependencies=["missing-2"]),
        ]

        # Record enough requests to satisfy threshold
        for _ in range(3):
            detector.record_no_task_response("agent-1")

        result = detector.check_for_gridlock(tasks)

        assert result["is_gridlock"] is True
        assert result["severity"] == "critical"

    def test_no_gridlock_tasks_in_progress(self, detector: GridlockDetector) -> None:
        """Test NO gridlock when tasks are actively being worked on.

        This is the key false positive scenario: agents poll every 30s
        and get no task because all available work is in-progress.
        That's normal, not gridlock.
        """
        tasks = [
            _make_task("done-1", TaskStatus.DONE),
            _make_task("in-prog-1", TaskStatus.IN_PROGRESS),
            _make_task("in-prog-2", TaskStatus.IN_PROGRESS),
            _make_task("blocked-1", TaskStatus.TODO, dependencies=["in-prog-1"]),
            _make_task("blocked-2", TaskStatus.TODO, dependencies=["in-prog-2"]),
        ]

        # Simulate heavy polling - 10 requests from 3 agents
        for agent in ["agent-1", "agent-2", "agent-3"]:
            for _ in range(4):
                detector.record_no_task_response(agent)

        result = detector.check_for_gridlock(tasks)

        assert result["is_gridlock"] is False

    def test_no_gridlock_unblocked_todo_exists(
        self, detector: GridlockDetector
    ) -> None:
        """Test NO gridlock when at least one TODO task is unblocked."""
        tasks = [
            _make_task("unblocked-1", TaskStatus.TODO),  # No dependencies!
            _make_task("blocked-1", TaskStatus.TODO, dependencies=["missing-1"]),
        ]

        for _ in range(5):
            detector.record_no_task_response("agent-1")

        result = detector.check_for_gridlock(tasks)

        assert result["is_gridlock"] is False

    def test_no_gridlock_no_todo_tasks(self, detector: GridlockDetector) -> None:
        """Test NO gridlock when all tasks are done."""
        tasks = [
            _make_task("done-1", TaskStatus.DONE),
            _make_task("done-2", TaskStatus.DONE),
        ]

        for _ in range(5):
            detector.record_no_task_response("agent-1")

        result = detector.check_for_gridlock(tasks)

        assert result["is_gridlock"] is False

    def test_false_positive_single_agent_polling(
        self, detector: GridlockDetector
    ) -> None:
        """Test that one agent polling rapidly doesn't trigger gridlock.

        With 30s polling and a 5-min window, a single agent makes 10
        requests. The old logic treated this as 10 failed requests.
        The new logic should count distinct agents, not raw requests.
        """
        tasks = [
            _make_task("in-prog-1", TaskStatus.IN_PROGRESS),
            _make_task("blocked-1", TaskStatus.TODO, dependencies=["in-prog-1"]),
        ]

        # Single agent polls 10 times
        for _ in range(10):
            detector.record_no_task_response("agent-1")

        result = detector.check_for_gridlock(tasks)

        assert result["is_gridlock"] is False

    def test_distinct_agents_tracked_in_metrics(
        self, detector: GridlockDetector
    ) -> None:
        """Test that metrics report distinct agent count."""
        tasks = [
            _make_task("blocked-1", TaskStatus.TODO, dependencies=["missing"]),
        ]

        detector.record_no_task_response("agent-1")
        detector.record_no_task_response("agent-1")
        detector.record_no_task_response("agent-2")

        result = detector.check_for_gridlock(tasks)

        assert result["metrics"]["distinct_agents_requesting"] == 2

    def test_alert_cooldown_prevents_spam(self, detector: GridlockDetector) -> None:
        """Test that gridlock alerts respect cooldown period."""
        tasks = [
            _make_task("blocked-1", TaskStatus.TODO, dependencies=["missing"]),
        ]

        # First check: should alert
        for _ in range(3):
            detector.record_no_task_response("agent-1")
        result1 = detector.check_for_gridlock(tasks)

        # Second check immediately: gridlock still true, but no alert
        for _ in range(3):
            detector.record_no_task_response("agent-2")
        result2 = detector.check_for_gridlock(tasks)

        assert result1["is_gridlock"] is True
        assert result1["should_alert"] is True
        assert result2["is_gridlock"] is True
        assert result2["should_alert"] is False

    def test_gridlock_with_in_progress_but_zero_unblocked(
        self, detector: GridlockDetector
    ) -> None:
        """Test: 1 task in-progress but ALL TODO are blocked.

        This is borderline — work exists but if the in-progress task
        fails, we're deadlocked. Should NOT trigger gridlock since
        progress is possible through the in-progress task.
        """
        tasks = [
            _make_task("in-prog-1", TaskStatus.IN_PROGRESS),
            _make_task("blocked-1", TaskStatus.TODO, dependencies=["in-prog-1"]),
            _make_task("blocked-2", TaskStatus.TODO, dependencies=["in-prog-1"]),
        ]

        for _ in range(5):
            detector.record_no_task_response("agent-1")

        result = detector.check_for_gridlock(tasks)

        # Not gridlock - the in-progress task can still complete
        # and unblock the others
        assert result["is_gridlock"] is False


@pytest.mark.unit
class TestGridlockDiagnosis:
    """Test the diagnostic output quality."""

    def test_diagnosis_includes_blocked_task_details(self) -> None:
        """Test that gridlock diagnosis lists blocked tasks."""
        detector = GridlockDetector()
        tasks = [
            _make_task("blocked-1", TaskStatus.TODO, dependencies=["missing"]),
        ]

        for _ in range(3):
            detector.record_no_task_response("agent-1")

        result = detector.check_for_gridlock(tasks)

        assert (
            "blocked-1" in result["diagnosis"].lower()
            or "Task blocked-1" in result["diagnosis"]
        )

    def test_normal_diagnosis_when_no_gridlock(self) -> None:
        """Test clean diagnosis when no gridlock."""
        detector = GridlockDetector()
        tasks = [
            _make_task("todo-1", TaskStatus.TODO),
        ]

        result = detector.check_for_gridlock(tasks)

        assert "No gridlock" in result["diagnosis"]


@pytest.mark.unit
class TestBestEffortClaimAlignment:
    """Issue #629: the detector must share the selector's claimability rule.

    The selector now lets the integration task claim best-effort over
    settled-but-BLOCKED upstreams (>=80% done). A detector that keeps
    the strict all-DONE rule would declare GRIDLOCK on a state that is
    actually about to progress — the exact state the #629 fix creates.
    """

    @pytest.fixture
    def detector(self) -> GridlockDetector:
        return GridlockDetector()

    def _integration_over_degraded_board(self) -> List[Task]:
        """6 done + 1 blocked + integration TODO depending on all 7."""
        tasks = [_make_task(f"d{i}", TaskStatus.DONE) for i in range(6)]
        tasks.append(_make_task("d6", TaskStatus.BLOCKED))
        integration = _make_task(
            "int-1", TaskStatus.TODO, dependencies=[f"d{i}" for i in range(7)]
        )
        integration.labels = ["integration", "verification", "type:integration"]
        tasks.append(integration)
        return tasks

    def test_claimable_integration_task_is_not_gridlock(
        self, detector: GridlockDetector
    ) -> None:
        """Best-effort-claimable integration waiting for pickup != gridlock."""
        tasks = self._integration_over_degraded_board()
        for _ in range(3):
            detector.record_no_task_response("agent-1")

        result = detector.check_for_gridlock(tasks)

        assert result["is_gridlock"] is False

    def test_ordinary_task_behind_blocked_dep_is_still_gridlock(
        self, detector: GridlockDetector
    ) -> None:
        """A non-integration TODO behind a BLOCKED dep stays gridlocked."""
        tasks = [
            _make_task("done-1", TaskStatus.DONE),
            _make_task("blocked-1", TaskStatus.BLOCKED),
            _make_task("waiting-1", TaskStatus.TODO, dependencies=["blocked-1"]),
        ]
        for _ in range(3):
            detector.record_no_task_response("agent-1")

        result = detector.check_for_gridlock(tasks)

        assert result["is_gridlock"] is True

    def test_integration_below_done_floor_is_still_gridlock(
        self, detector: GridlockDetector
    ) -> None:
        """1 done + 1 blocked (50% < 80%): integration unclaimable -> gridlock."""
        tasks = [
            _make_task("d0", TaskStatus.DONE),
            _make_task("d1", TaskStatus.BLOCKED),
        ]
        integration = _make_task("int-1", TaskStatus.TODO, dependencies=["d0", "d1"])
        integration.labels = ["integration", "verification", "type:integration"]
        tasks.append(integration)
        for _ in range(3):
            detector.record_no_task_response("agent-1")

        result = detector.check_for_gridlock(tasks)

        assert result["is_gridlock"] is True
