"""
Unit tests for LiveExperimentMonitor.get_status.

Covers the v73 fix: get_status must expose ground-truth task counts
from the kanban backend (total_tasks, completed_tasks, etc.) in
addition to the legacy in-monitor running tallies. Consumers
(monitor agent, downstream tooling, future MCP wrappers) rely on
the kanban-truth block to make "is the project done" decisions —
the legacy task_assignments/task_completions counters are running
event tallies that do NOT represent project totals and were the
root of the v73 premature-exit cascade.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.experiments.live_experiment_monitor import LiveExperimentMonitor

pytestmark = pytest.mark.unit


def _make_monitor(kanban_client: Any = None) -> LiveExperimentMonitor:
    """Build a LiveExperimentMonitor with a mock MLflow experiment."""
    monitor = LiveExperimentMonitor.__new__(LiveExperimentMonitor)
    monitor.experiment_name = "test_exp"
    monitor.board_id = "board-1"
    monitor.project_id = "proj-1"
    monitor.tracking_interval = 30
    monitor.kanban_client = kanban_client
    monitor.mlflow_experiment = MagicMock()
    monitor.is_running = True
    monitor.monitor_task = None
    monitor.run_name = "run-1"
    monitor.run_dir = None
    monitor.registered_agents = {}
    monitor.task_assignments = {}
    monitor.task_completions = {}
    monitor.blockers_reported = 0
    monitor.artifacts_created = 0
    monitor.decisions_logged = 0
    monitor.context_requests = 0
    monitor.progress_updates = 0
    return monitor


class TestProgressHeartbeat:
    """record_progress + progress_updates surface the report_task_progress
    heartbeat (PR #704 review).

    The spawn-thrash detector reads ``progress_updates`` so a claimed
    agent reporting 25/50/75% counts as forward progress before any task
    completes — closing the claim->first-artifact gap that could
    otherwise fast-fail a healthy run.
    """

    def test_record_progress_increments_counter(self) -> None:
        """Each report_task_progress call bumps the counter."""
        monitor = _make_monitor()
        assert monitor.progress_updates == 0
        monitor.record_progress(agent_id="a1", task_id="t1", progress=25)
        monitor.record_progress(agent_id="a1", task_id="t1", progress=50)
        assert monitor.progress_updates == 2

    @pytest.mark.asyncio
    async def test_get_status_exposes_progress_updates(self) -> None:
        """get_status surfaces progress_updates for the runner to read."""
        monitor = _make_monitor()
        monitor.record_progress(agent_id="a1", task_id="t1", progress=75)
        status = await monitor.get_status()
        assert status["progress_updates"] == 1

    def test_record_context_request_increments_counter(self) -> None:
        """record_context_request (now wired into get_task_context) counts."""
        monitor = _make_monitor()
        assert monitor.context_requests == 0
        monitor.record_context_request(agent_id="a1", task_id="t1")
        assert monitor.context_requests == 1


class TestGetStatusKanbanTruth:
    """get_status must expose kanban-truth task counts."""

    @pytest.mark.asyncio
    async def test_get_status_includes_kanban_truth_fields(self) -> None:
        """When kanban_client is wired, get_status must include all 5 truth fields."""
        kanban_client = MagicMock()
        kanban_client.get_project_metrics = AsyncMock(
            return_value={
                "total_tasks": 6,
                "completed_tasks": 4,
                "in_progress_tasks": 1,
                "backlog_tasks": 1,
                "blocked_tasks": 0,
            }
        )
        monitor = _make_monitor(kanban_client=kanban_client)

        status = await monitor.get_status()

        # Ground-truth fields — what consumers MUST use for done-checks
        assert status["total_tasks"] == 6
        assert status["completed_tasks"] == 4
        assert status["in_progress_tasks"] == 1
        assert status["backlog_tasks"] == 1
        assert status["blocked_tasks"] == 0

    @pytest.mark.asyncio
    async def test_get_status_preserves_legacy_running_counters(self) -> None:
        """Legacy task_assignments/task_completions must still be present.

        They are misleading as "project totals" but mlflow + summary
        rendering still consume them as running tallies.
        """
        monitor = _make_monitor()
        monitor.task_assignments = {"t1": "agent-1", "t2": "agent-2"}
        monitor.task_completions = {"t1": 100.0}

        status = await monitor.get_status()

        assert status["task_assignments"] == 2
        assert status["task_completions"] == 1
        assert status["is_running"] is True
        assert status["run_name"] == "run-1"

    @pytest.mark.asyncio
    async def test_get_status_omits_kanban_fields_when_no_client(self) -> None:
        """No kanban client → no truth fields, but call still succeeds."""
        monitor = _make_monitor(kanban_client=None)

        status = await monitor.get_status()

        # Legacy fields present
        assert status["is_running"] is True
        assert status["task_assignments"] == 0
        # Truth fields absent
        assert "total_tasks" not in status
        assert "completed_tasks" not in status

    @pytest.mark.asyncio
    async def test_get_status_handles_kanban_failure_gracefully(self) -> None:
        """Kanban fetch failure → log warning, return legacy fields only."""
        kanban_client = MagicMock()
        kanban_client.get_project_metrics = AsyncMock(
            side_effect=RuntimeError("kanban down")
        )
        monitor = _make_monitor(kanban_client=kanban_client)

        status = await monitor.get_status()

        # Must not raise; legacy fields still present
        assert status["is_running"] is True
        # Truth fields absent because fetch failed
        assert "total_tasks" not in status

    @pytest.mark.asyncio
    async def test_get_status_v73_regression_total_tasks_distinct_from_assignments(
        self,
    ) -> None:
        """
        REGRESSION for dashboard-v73.

        v73 had 6 project tasks but only 2 had ever been assigned to
        agents (Build + Integrate; the other 4 were either pre-baked
        DONE, gated on dependencies, or about to be created later).
        The legacy task_assignments counter showed 2; consumers
        misread it as "total tasks = 2" and concluded the project
        was done.

        After the fix, get_status must expose total_tasks=6 from the
        kanban backend so consumers have an unambiguous denominator.
        """
        kanban_client = MagicMock()
        kanban_client.get_project_metrics = AsyncMock(
            return_value={
                "total_tasks": 6,
                "completed_tasks": 2,
                "in_progress_tasks": 0,
                "backlog_tasks": 4,
                "blocked_tasks": 0,
            }
        )
        monitor = _make_monitor(kanban_client=kanban_client)
        # Simulate the v73 state: 2 assignments observed, 2 completions observed
        monitor.task_assignments = {"build-id": "agent-1", "integrate-id": "agent-2"}
        monitor.task_completions = {"build-id": 100.0, "integrate-id": 200.0}

        status = await monitor.get_status()

        # The legacy tallies still report 2/2 — that's NOT a bug, that's
        # what they semantically are. The fix is exposing the kanban
        # truth alongside them.
        assert status["task_assignments"] == 2
        assert status["task_completions"] == 2
        # The truth: 6 total, 2 done, 4 still pending. Consumers
        # making done-checks now have an unambiguous denominator.
        assert status["total_tasks"] == 6
        assert status["completed_tasks"] == 2
        assert status["backlog_tasks"] == 4
        # Therefore: completed_tasks != total_tasks → project NOT done
        assert status["completed_tasks"] != status["total_tasks"]

    @pytest.mark.asyncio
    async def test_get_status_is_awaitable(self) -> None:
        """get_status must be async (was sync before v73 fix)."""
        import inspect

        monitor = _make_monitor()
        assert inspect.iscoroutinefunction(monitor.get_status)


class TestGetExperimentStatusLifecycle:
    """The MCP wrapper distinguishes startup, active, and finished states.

    Codex P1 on PR #349: workers wait on project_info.json which
    the creator writes BEFORE calling start_experiment. There's a
    real window where get_experiment_status returns "no monitor"
    and a worker reading is_running=False would exit. The fix is
    to expose experiment_started so consumers can distinguish
    "not started yet" from "started and ended."
    """

    @pytest.mark.asyncio
    async def test_status_returns_not_started_when_no_monitor(self) -> None:
        """Startup window: monitor is None → experiment_started=False."""
        from src.marcus_mcp.tools import experiments as experiments_module

        # Patch get_active_monitor to return None (startup window)
        original = experiments_module.get_active_monitor
        experiments_module.get_active_monitor = lambda: None  # type: ignore[assignment]
        try:
            status = await experiments_module.get_experiment_status()
        finally:
            experiments_module.get_active_monitor = original  # type: ignore[assignment]

        assert status["experiment_started"] is False
        assert status["is_running"] is False
        # The two-flag combination unambiguously says "not started"
        # (vs "started=True, is_running=False" which means "ended")

    @pytest.mark.asyncio
    async def test_status_returns_started_when_monitor_active(self) -> None:
        """Active state: monitor exists → experiment_started=True."""
        from src.marcus_mcp.tools import experiments as experiments_module

        kanban_client = MagicMock()
        kanban_client.get_project_metrics = AsyncMock(
            return_value={
                "total_tasks": 6,
                "completed_tasks": 2,
                "in_progress_tasks": 2,
                "backlog_tasks": 2,
                "blocked_tasks": 0,
            }
        )
        monitor = _make_monitor(kanban_client=kanban_client)

        original = experiments_module.get_active_monitor
        experiments_module.get_active_monitor = (  # type: ignore[assignment]
            lambda: monitor
        )
        try:
            status = await experiments_module.get_experiment_status()
        finally:
            experiments_module.get_active_monitor = original  # type: ignore[assignment]

        assert status["experiment_started"] is True
        assert status["is_running"] is True
        assert status["total_tasks"] == 6


class TestCompletionFormulaAlignment:
    """The documented completion formula must match _check_completion.

    Codex P2 on PR #349: prior PR docstrings said
    `completed_tasks == total_tasks AND in_progress_tasks == 0`,
    but the runtime check at LiveExperimentMonitor._check_completion
    uses `(completed + blocked) == total AND in_progress == 0`.
    Blocked tasks count toward "done" because the project shouldn't
    stall waiting for them. Consumers following the wrong formula
    would think work is incomplete even after Marcus finishes.
    """

    @pytest.mark.asyncio
    async def test_completion_with_blocked_tasks_matches_runtime(self) -> None:
        """When blocked + completed == total, the project IS done."""
        # Build a state where: total=5, completed=3, blocked=2,
        # in_progress=0. Per the runtime formula, this is "done"
        # because (3+2)==5 and in_progress==0. Consumers using the
        # documented formula must reach the same verdict.
        kanban_client = MagicMock()
        kanban_client.get_project_metrics = AsyncMock(
            return_value={
                "total_tasks": 5,
                "completed_tasks": 3,
                "in_progress_tasks": 0,
                "backlog_tasks": 0,
                "blocked_tasks": 2,
            }
        )
        monitor = _make_monitor(kanban_client=kanban_client)

        status = await monitor.get_status()

        completed = status["completed_tasks"]
        blocked = status["blocked_tasks"]
        total = status["total_tasks"]
        in_progress = status["in_progress_tasks"]

        # The documented formula
        project_done = in_progress == 0 and (completed + blocked) == total
        assert project_done is True, (
            "Documented formula must match runtime: blocked tasks "
            "count toward done. Codex P2 on PR #349."
        )

        # The OLD/wrong formula would say not done
        wrong_formula = completed == total and in_progress == 0
        assert wrong_formula is False, (
            "Sanity check: the OLD formula would (incorrectly) say "
            "not done in this state — that's the bug we're guarding "
            "against."
        )


class TestStopReportsBlockedAccurately:
    """``stop()`` must report ``success=False`` when blockers remain.

    Marcus's completion math counts BLOCKED + DONE as terminal so a
    run never stalls on a blocked task. But that's a coordination
    decision, not a quality signal. Reporting a run as
    ``success: true`` when tasks remain blocked masks failures
    (snake_game-v1 cascade — task ended BLOCKED with the actual work
    committed but a deadlock preventing completion report; experiment
    flagged success=true, sweeping the bug under the rug).
    """

    @pytest.mark.asyncio
    async def test_stop_returns_success_false_when_tasks_blocked(self) -> None:
        """A run that ends with blocked tasks must report success=False."""
        kanban_client = MagicMock()
        kanban_client.get_project_metrics = AsyncMock(
            return_value={
                "total_tasks": 13,
                "completed_tasks": 12,
                "blocked_tasks": 1,
                "in_progress_tasks": 0,
            }
        )
        monitor = _make_monitor(kanban_client=kanban_client)

        result = await monitor.stop()

        assert result["success"] is False, (
            "Run with blockers at stop time must report success=False — "
            "blocked counts as terminal for coordination but not for "
            "experiment quality."
        )
        assert result["blocked_tasks_at_stop"] == 1
        # Run still completes (we didn't stall)
        assert result["run_name"] == "run-1"

    @pytest.mark.asyncio
    async def test_stop_returns_success_true_when_all_done(self) -> None:
        """Clean run with zero blockers reports success=True."""
        kanban_client = MagicMock()
        kanban_client.get_project_metrics = AsyncMock(
            return_value={
                "total_tasks": 13,
                "completed_tasks": 13,
                "blocked_tasks": 0,
                "in_progress_tasks": 0,
            }
        )
        monitor = _make_monitor(kanban_client=kanban_client)

        result = await monitor.stop()

        assert result["success"] is True
        assert result["blocked_tasks_at_stop"] == 0

    @pytest.mark.asyncio
    async def test_stop_handles_metrics_failure_gracefully(self) -> None:
        """A failed metrics read shouldn't crash stop()."""
        kanban_client = MagicMock()
        kanban_client.get_project_metrics = AsyncMock(
            side_effect=RuntimeError("DB unavailable")
        )
        monitor = _make_monitor(kanban_client=kanban_client)

        result = await monitor.stop()

        # When we can't read the count, default to success=True
        # (no evidence of blockers) so a transient DB hiccup doesn't
        # falsely mark a clean run as a failure.
        assert result["success"] is True
        assert result["blocked_tasks_at_stop"] == 0


class TestLogProjectStateMetricsUsesWiredClient:
    """_monitor_loop metric logging must use the experiment's kanban client.

    Regression: the monitoring loop previously built its own
    ProjectMonitor, which hardcodes a Planka KanbanClient. A SQLite-backed
    experiment would then issue calls against a (stale) Planka board and
    flood the log with getBoardSummary errors. Metric logging must instead
    read from the kanban client wired into the monitor.
    """

    @pytest.mark.asyncio
    async def test_log_project_state_uses_kanban_client_metrics(self) -> None:
        """Metrics are read from the wired client and forwarded to MLflow."""
        kanban_client = MagicMock()
        kanban_client.get_project_metrics = AsyncMock(
            return_value={
                "total_tasks": 10,
                "completed_tasks": 4,
                "in_progress_tasks": 2,
                "blocked_tasks": 1,
            }
        )
        monitor = _make_monitor(kanban_client=kanban_client)

        await monitor._log_project_state_metrics(step=3)

        kanban_client.get_project_metrics.assert_awaited_once()
        monitor.mlflow_experiment.log_project_state.assert_called_once()
        kwargs = monitor.mlflow_experiment.log_project_state.call_args.kwargs
        assert kwargs["total_tasks"] == 10
        assert kwargs["completed_tasks"] == 4
        assert kwargs["in_progress_tasks"] == 2
        assert kwargs["blocked_tasks"] == 1
        assert kwargs["progress_percent"] == pytest.approx(40.0)
        assert kwargs["step"] == 3

    @pytest.mark.asyncio
    async def test_log_project_state_logs_active_agents(self) -> None:
        """Active-agent count is logged alongside project state."""
        kanban_client = MagicMock()
        kanban_client.get_project_metrics = AsyncMock(
            return_value={"total_tasks": 1, "completed_tasks": 0}
        )
        monitor = _make_monitor(kanban_client=kanban_client)
        monitor.registered_agents = {"a1": {}, "a2": {}}

        await monitor._log_project_state_metrics(step=0)

        monitor.mlflow_experiment.log_metric.assert_called_once_with(
            "active_agents", 2, step=0
        )

    @pytest.mark.asyncio
    async def test_log_project_state_noop_without_kanban_client(self) -> None:
        """No kanban client → nothing logged, no error raised."""
        monitor = _make_monitor(kanban_client=None)

        await monitor._log_project_state_metrics(step=0)

        monitor.mlflow_experiment.log_project_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_log_project_state_handles_zero_tasks(self) -> None:
        """Empty board → progress_percent is 0.0, no ZeroDivisionError."""
        kanban_client = MagicMock()
        kanban_client.get_project_metrics = AsyncMock(
            return_value={"total_tasks": 0, "completed_tasks": 0}
        )
        monitor = _make_monitor(kanban_client=kanban_client)

        await monitor._log_project_state_metrics(step=0)

        kwargs = monitor.mlflow_experiment.log_project_state.call_args.kwargs
        assert kwargs["progress_percent"] == 0.0


class TestComputeVelocity:
    """Velocity is the per-minute task-completion rate between samples."""

    def test_velocity_first_sample_counts_from_zero(self) -> None:
        """First sample: velocity reflects all completed tasks so far."""
        monitor = _make_monitor()
        monitor.tracking_interval = 60  # 1 minute

        velocity = monitor._compute_velocity(completed=3)

        assert velocity == pytest.approx(3.0)

    def test_velocity_uses_delta_between_samples(self) -> None:
        """Subsequent samples count only newly-completed tasks."""
        monitor = _make_monitor()
        monitor.tracking_interval = 60

        monitor._compute_velocity(completed=3)
        velocity = monitor._compute_velocity(completed=5)

        assert velocity == pytest.approx(2.0)

    def test_velocity_normalized_to_per_minute(self) -> None:
        """A 30s interval doubles the per-minute rate."""
        monitor = _make_monitor()
        monitor.tracking_interval = 30  # half a minute

        velocity = monitor._compute_velocity(completed=2)

        assert velocity == pytest.approx(4.0)

    def test_velocity_never_negative(self) -> None:
        """A dropping completed count (e.g. board reset) yields 0, not negative."""
        monitor = _make_monitor()
        monitor.tracking_interval = 60

        monitor._compute_velocity(completed=5)
        velocity = monitor._compute_velocity(completed=2)

        assert velocity == 0.0
