"""
Live Experiment Monitor for Real Marcus Experiments.

This module provides real-time experiment tracking for actual Marcus
operations, monitoring real agents working on real boards and logging
all metrics to MLflow.

Usage
-----
Use the MCP tools to control experiments:
- start_experiment: Begin tracking a real experiment
- end_experiment: Stop tracking and finalize results
- get_experiment_status: Check current experiment status

Example via MCP
---------------
# Start an experiment
start_experiment(
    experiment_name="production-test",
    board_id="abc123",
    project_id="xyz789"
)

# Agents work on real tasks...
# Monitor automatically tracks everything

# End the experiment
end_experiment()
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.experiments import MarcusExperiment

logger = logging.getLogger(__name__)


class LiveExperimentMonitor:
    """
    Monitor and track real Marcus experiments in real-time.

    This class runs in the background during actual Marcus operations,
    tracking agent registrations, task assignments, completions, and
    all other activities, logging everything to MLflow.

    Parameters
    ----------
    experiment_name : str
        Name for the MLflow experiment
    board_id : str
        Board ID to monitor
    project_id : str
        Project ID to monitor
    tracking_interval : int
        How often to log metrics (seconds), default 30
    """

    def __init__(
        self,
        experiment_name: str,
        board_id: str,
        project_id: str,
        tracking_interval: int = 30,
        kanban_client: Any = None,
    ):
        """Initialize live experiment monitor."""
        self.experiment_name = experiment_name
        self.board_id = board_id
        self.project_id = project_id
        self.tracking_interval = tracking_interval
        self.kanban_client = kanban_client

        # MLflow experiment
        self.mlflow_experiment = MarcusExperiment(
            experiment_name=experiment_name, tracking_uri="./mlruns"
        )

        # Monitoring state
        self.is_running = False
        self.was_started = False  # True once start() has been called
        self.monitor_task: Optional[asyncio.Task[None]] = None
        self.run_name: Optional[str] = None

        # Run directory for experiment_complete.json signal
        self.run_dir: Optional[Path] = None

        # Tracked metrics
        self.registered_agents: Dict[str, Dict[str, Any]] = {}
        self.task_assignments: Dict[str, str] = {}  # task_id -> agent_id
        self.task_completions: Dict[str, float] = {}  # task_id -> completion_time
        self.blockers_reported = 0
        self.artifacts_created = 0
        self.decisions_logged = 0
        self.context_requests = 0
        self.progress_updates = 0

        # Previous completed-task count, used to derive per-interval velocity
        self._last_completed_count = 0

        logger.info(
            f"Initialized LiveExperimentMonitor for {experiment_name} "
            f"(board: {board_id})"
        )

    async def start(
        self,
        run_name: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Start the live experiment monitoring.

        Parameters
        ----------
        run_name : str, optional
            Name for this run (auto-generated if not provided)
        params : Dict[str, Any], optional
            Experiment parameters to log
        tags : Dict[str, str], optional
            Tags to add to the run

        Returns
        -------
        Dict[str, Any]
            Status information including run_id
        """
        if self.is_running:
            return {
                "success": False,
                "error": "Experiment already running",
                "run_id": self.run_name,
            }

        # Generate run name if not provided
        if run_name is None:
            run_name = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        self.run_name = run_name

        # Prepare default parameters
        if params is None:
            params = {}

        params.update(
            {
                "board_id": self.board_id,
                "project_id": self.project_id,
                "tracking_interval": self.tracking_interval,
                "experiment_type": "live_monitoring",
            }
        )

        # Start MLflow run
        self.mlflow_experiment.start_run(run_name=run_name, params=params, tags=tags)

        # Start background monitoring
        self.was_started = True
        self.is_running = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())

        logger.info(f"Started live experiment: {run_name}")

        return {
            "success": True,
            "run_name": run_name,
            "experiment_name": self.experiment_name,
            "board_id": self.board_id,
            "message": "Live experiment monitoring started",
        }

    async def stop(self) -> Dict[str, Any]:
        """
        Stop the live experiment monitoring.

        The ``success`` flag in the result reflects whether all tasks
        actually completed cleanly. If any tasks are still BLOCKED on
        the board at stop time, ``success`` is set to ``False`` so
        downstream consumers (Posidonius, Cato, MLflow) can distinguish
        a clean run from one that ended with unresolved blockers.

        Marcus completion math counts BLOCKED + DONE as terminal so the
        run doesn't stall forever waiting on a blocker, but a run that
        ends with active blockers is not a clean success — it's a
        partial run that needs human attention. Reporting it as
        ``success: true`` masked real failures (Simon decision
        011b3fad — snake_game-v1 cascade).

        Returns
        -------
        Dict[str, Any]
            Final statistics and status. ``success`` is ``True`` only
            when no blockers remain unresolved at stop time.
        """
        if not self.is_running:
            return {"success": False, "error": "No experiment currently running"}

        self.is_running = False

        # Cancel monitoring task
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass

        # Compute blocked-task count for the success flag
        blocked_at_stop = 0
        if self.kanban_client:
            try:
                metrics = await self.kanban_client.get_project_metrics()
                blocked_at_stop = int(metrics.get("blocked_tasks", 0))
            except Exception as e:
                logger.warning(f"Could not read blocked-task count at stop: {e}")

        # Log final metrics
        final_metrics: Dict[str, float] = {
            "total_registered_agents": float(len(self.registered_agents)),
            "total_task_assignments": float(len(self.task_assignments)),
            "total_task_completions": float(len(self.task_completions)),
            "total_blockers": float(self.blockers_reported),
            "blocked_tasks_at_stop": float(blocked_at_stop),
            "total_artifacts": float(self.artifacts_created),
            "total_decisions": float(self.decisions_logged),
            "total_context_requests": float(self.context_requests),
            "total_progress_updates": float(self.progress_updates),
        }

        # Generate summary
        summary = self._generate_summary()

        # End MLflow run
        self.mlflow_experiment.end_run(final_metrics=final_metrics, summary=summary)

        success = blocked_at_stop == 0
        if not success:
            logger.warning(
                f"Experiment {self.run_name} ended with {blocked_at_stop} "
                f"blocked task(s); reporting success=False."
            )

        logger.info(f"Stopped live experiment: {self.run_name}")

        return {
            "success": success,
            "run_name": self.run_name,
            "final_metrics": final_metrics,
            "summary": summary,
            "blocked_tasks_at_stop": blocked_at_stop,
        }

    async def _monitor_loop(self) -> None:
        """Background monitoring loop."""
        step = 0

        try:
            while self.is_running:
                await asyncio.sleep(self.tracking_interval)

                # MLflow project-state logging. Isolated so a failure here
                # (e.g. the kanban provider is briefly unreachable) does not
                # skip the completion check below.
                try:
                    await self._log_project_state_metrics(step)
                    step += 1
                except Exception as e:
                    logger.error(f"Error logging metrics in monitoring loop: {e}")

                # Deterministic completion check from kanban DB. Runs every
                # iteration regardless of MLflow failures — otherwise an
                # unreachable provider leaves is_running stuck True after the
                # run actually finished.
                try:
                    if await self._check_completion():
                        logger.info(
                            "Auto-ending experiment: all tasks "
                            "complete (detected by kanban metrics)"
                        )
                        break
                except Exception as e:
                    logger.error(f"Error checking completion: {e}")

            # If we broke out of the loop due to completion,
            # auto-stop and write the completion signal
            if self.is_running:
                result = await self.stop()
                self._write_completion_file(result)

        except Exception as e:
            logger.error(f"Failed to initialize monitoring: {e}")

    async def _log_project_state_metrics(self, step: int) -> None:
        """Log project-state metrics to MLflow using the wired kanban client.

        Reads task counts from ``self.kanban_client`` — the kanban provider
        chosen for this experiment (SQLite, Planka, etc.). This is provider
        agnostic: it never instantiates a provider-specific monitor, so a
        SQLite-backed experiment does not accidentally issue calls against a
        Planka board.

        Parameters
        ----------
        step : int
            Monotonic step index for the MLflow metric time series.
        """
        if not self.kanban_client:
            return

        metrics = await self.kanban_client.get_project_metrics()
        total = int(metrics.get("total_tasks", 0))
        completed = int(metrics.get("completed_tasks", 0))
        in_progress = int(metrics.get("in_progress_tasks", 0))
        blocked = int(metrics.get("blocked_tasks", 0))
        progress_percent = (completed / total * 100.0) if total else 0.0
        velocity = self._compute_velocity(completed)

        self.mlflow_experiment.log_project_state(
            total_tasks=total,
            completed_tasks=completed,
            in_progress_tasks=in_progress,
            blocked_tasks=blocked,
            progress_percent=progress_percent,
            velocity=velocity,
            step=step,
        )
        self.mlflow_experiment.log_metric(
            "active_agents", len(self.registered_agents), step=step
        )

        logger.debug(
            f"Logged metrics at step {step}: "
            f"velocity={velocity:.2f}, "
            f"agents={len(self.registered_agents)}"
        )

    def _compute_velocity(self, completed: int) -> float:
        """Compute task-completion velocity since the previous sample.

        Velocity is the number of tasks that moved to DONE since the last
        monitoring sample, normalized to tasks-per-minute using
        ``tracking_interval``.

        Parameters
        ----------
        completed : int
            Current total of completed tasks reported by the kanban client.

        Returns
        -------
        float
            Tasks completed per minute over the last tracking interval.
        """
        last = getattr(self, "_last_completed_count", 0)
        self._last_completed_count = completed
        delta = max(0, completed - last)
        interval_minutes = self.tracking_interval / 60.0
        return delta / interval_minutes if interval_minutes > 0 else 0.0

    async def _check_completion(self) -> bool:
        """Check if all tasks are done using kanban DB metrics.

        Returns
        -------
        bool
            True if experiment is complete.
        """
        if not self.kanban_client:
            return False

        try:
            metrics = await self.kanban_client.get_project_metrics()
            total = metrics.get("total_tasks", 0)
            completed = metrics.get("completed_tasks", 0)
            blocked = metrics.get("blocked_tasks", 0)
            in_progress = metrics.get("in_progress_tasks", 0)

            if total == 0:
                return False

            is_done = in_progress == 0 and (completed + blocked) == total

            if is_done:
                logger.info(
                    f"Completion check: {completed}/{total} done, "
                    f"{blocked} blocked, {in_progress} in_progress "
                    f"→ COMPLETE"
                )
            return bool(is_done)

        except Exception as e:
            logger.warning(f"Completion check failed: {e}")
            return False

    def _write_completion_file(self, result: Dict[str, Any]) -> None:
        """Write experiment_complete.json to the run directory.

        Parameters
        ----------
        result : Dict[str, Any]
            Result from self.stop() with final metrics.
        """
        if not self.run_dir:
            logger.warning("Cannot write experiment_complete.json: " "run_dir not set")
            return

        import json

        try:
            completion_file = self.run_dir / "experiment_complete.json"
            completion_data = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "run_name": result.get("run_name"),
                "final_metrics": result.get("final_metrics", {}),
                "success": result.get("success", True),
            }
            with open(completion_file, "w") as f:
                json.dump(completion_data, f, indent=2)

            logger.info(f"Wrote experiment_complete.json to " f"{completion_file}")
        except Exception as e:
            logger.warning(f"Failed to write experiment_complete.json: {e}")

    def record_agent_registration(
        self,
        agent_id: str,
        name: str,
        role: str,
        skills: List[str],
    ) -> None:
        """
        Record an agent registration.

        Parameters
        ----------
        agent_id : str
            Unique agent identifier
        name : str
            Agent name
        role : str
            Agent role
        skills : list
            Agent skills
        """
        self.registered_agents[agent_id] = {
            "name": name,
            "role": role,
            "skills": skills,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "tasks_completed": 0,
        }

        # Log to MLflow
        self.mlflow_experiment.log_param(f"agent_{agent_id}_skills", ",".join(skills))

        logger.info(f"Recorded agent registration: {agent_id} ({name})")

    def record_task_assignment(
        self,
        task_id: str,
        agent_id: str,
    ) -> None:
        """Record a task assignment."""
        self.task_assignments[task_id] = agent_id
        logger.debug(f"Recorded task assignment: {task_id} -> {agent_id}")

    def record_task_completion(
        self,
        task_id: str,
        agent_id: str,
        duration_seconds: Optional[float] = None,
    ) -> None:
        """Record a task completion."""
        completion_time = datetime.now(timezone.utc).timestamp()
        self.task_completions[task_id] = completion_time

        # Update agent stats
        if agent_id in self.registered_agents:
            self.registered_agents[agent_id]["tasks_completed"] += 1

        # Log to MLflow
        if duration_seconds:
            self.mlflow_experiment.log_task_completion(
                task_id=task_id, duration_seconds=duration_seconds, agent_id=agent_id
            )

        logger.info(f"Recorded task completion: {task_id} by {agent_id}")

    def record_blocker(
        self,
        agent_id: str,
        task_id: str,
        description: str,
        severity: str = "medium",
    ) -> None:
        """Record a blocker."""
        self.blockers_reported += 1

        self.mlflow_experiment.log_blocker(
            agent_id=agent_id,
            task_id=task_id,
            blocker_description=description,
            severity=severity,
        )

        logger.info(f"Recorded blocker: {agent_id} on {task_id}")

    def record_artifact(
        self,
        task_id: str,
        artifact_type: str,
        filename: str,
        description: str = "",
    ) -> None:
        """Record an artifact creation."""
        self.artifacts_created += 1

        self.mlflow_experiment.log_artifact_event(
            task_id=task_id,
            artifact_type=artifact_type,
            filename=filename,
            description=description,
        )

        logger.info(f"Recorded artifact: {filename} ({artifact_type})")

    def record_decision(
        self,
        agent_id: str,
        task_id: str,
        decision: str,
    ) -> None:
        """Record a decision."""
        self.decisions_logged += 1

        self.mlflow_experiment.log_decision(
            agent_id=agent_id, task_id=task_id, decision=decision
        )

        logger.info(f"Recorded decision: {agent_id} on {task_id}")

    def record_context_request(
        self,
        agent_id: str,
        task_id: str,
        context_type: str = "task_context",
    ) -> None:
        """Record a context request."""
        self.context_requests += 1

        self.mlflow_experiment.log_context_request(
            agent_id=agent_id, task_id=task_id, context_type=context_type
        )

        logger.debug(f"Recorded context request: {agent_id} for {task_id}")

    def record_progress(
        self,
        agent_id: str,
        task_id: str,
        progress: int = 0,
        status: str = "in_progress",
    ) -> None:
        """Record a task-progress heartbeat (a report_task_progress call).

        Counts every progress report — including intermediate 25/50/75%
        updates — as a sign the agent is alive and working its claimed
        task. The runner's spawn-thrash detector reads this (via
        ``progress_updates`` in :meth:`get_status`) so it does not tear
        down a healthy-but-slow run that is advancing without a
        completion yet.
        """
        self.progress_updates += 1

        logger.debug(
            f"Recorded progress update: {agent_id} on {task_id} "
            f"({progress}% {status})"
        )

    async def get_status(self) -> Dict[str, Any]:
        """
        Get current experiment status.

        Includes both the legacy in-monitor counters
        (``task_assignments`` / ``task_completions`` — running tallies
        of events the monitor has *observed* during this run) and
        ground-truth project totals queried directly from the kanban
        backend (``total_tasks``, ``completed_tasks``, etc.).

        Consumers deciding whether the project is done MUST use
        ``completed_tasks`` / ``total_tasks`` from the kanban truth
        block, not the running tallies. The running tallies grow as
        events fire and never represent project totals — they will
        always *appear* equal to themselves once both initial agent
        assignments complete, even if many more tasks remain in the
        project (lease recovery cases, dependent tasks unblocking
        later, etc.). v73 surfaced this hazard: agents reading the
        running tallies as a denominator concluded "all work done"
        with 4 tasks still pending.

        Returns
        -------
        Dict[str, Any]
            Current status and metrics. Always includes the legacy
            counters; includes the kanban-truth task counts when a
            kanban client is wired and reachable.
        """
        status: Dict[str, Any] = {
            "is_running": self.is_running,
            "run_name": self.run_name,
            "experiment_name": self.experiment_name,
            "board_id": self.board_id,
            "registered_agents": len(self.registered_agents),
            # Legacy in-monitor running tallies. NOT project totals.
            # See docstring — do not use as a denominator for "done".
            "task_assignments": len(self.task_assignments),
            "task_completions": len(self.task_completions),
            "blockers_reported": self.blockers_reported,
            "artifacts_created": self.artifacts_created,
            "decisions_logged": self.decisions_logged,
            "context_requests": self.context_requests,
            "progress_updates": self.progress_updates,
        }

        # Ground truth from the kanban backend. This is what
        # consumers should use to decide whether the project is done.
        if self.kanban_client is not None:
            try:
                metrics = await self.kanban_client.get_project_metrics()
                status["total_tasks"] = metrics.get("total_tasks", 0)
                status["completed_tasks"] = metrics.get("completed_tasks", 0)
                status["in_progress_tasks"] = metrics.get("in_progress_tasks", 0)
                status["backlog_tasks"] = metrics.get("backlog_tasks", 0)
                status["blocked_tasks"] = metrics.get("blocked_tasks", 0)
            except Exception as e:
                logger.warning(f"get_status: kanban metrics fetch failed: {e}")

        return status

    def _generate_summary(self) -> str:
        """Generate experiment summary."""
        duration = 0.0
        if self.registered_agents:
            registered_at_str = list(self.registered_agents.values())[0][
                "registered_at"
            ]
            registered_at = datetime.fromisoformat(registered_at_str)
            # Ensure timezone-aware
            if registered_at.tzinfo is None:
                registered_at = registered_at.replace(tzinfo=timezone.utc)
            duration = (datetime.now(timezone.utc) - registered_at).total_seconds()

        active_agents = len(
            [a for a in self.registered_agents.values() if a["tasks_completed"] > 0]
        )
        completion_rate = (
            len(self.task_completions) / max(len(self.task_assignments), 1) * 100
        )
        summary = f"""
Live Experiment Summary
=======================

Experiment: {self.experiment_name}
Run: {self.run_name}
Board: {self.board_id}
Duration: {duration:.0f} seconds

Agent Metrics:
- Total Registered: {len(self.registered_agents)}
- Active Agents: {active_agents}

Task Metrics:
- Total Assignments: {len(self.task_assignments)}
- Total Completions: {len(self.task_completions)}
- Completion Rate: {completion_rate:.1f}%

Condition Metrics:
- Blockers Reported: {self.blockers_reported}
- Artifacts Created: {self.artifacts_created}
- Decisions Logged: {self.decisions_logged}
- Context Requests: {self.context_requests}
- Progress Updates: {self.progress_updates}

Top Agents by Completions:
"""
        # Add top agents
        sorted_agents = sorted(
            self.registered_agents.items(),
            key=lambda x: x[1]["tasks_completed"],
            reverse=True,
        )[:5]

        for agent_id, info in sorted_agents:
            summary += (
                f"- {info['name']} ({agent_id}): {info['tasks_completed']} tasks\n"
            )

        return summary.strip()


# Global instance for MCP tools to use
_active_monitor: Optional[LiveExperimentMonitor] = None


def get_active_monitor() -> Optional[LiveExperimentMonitor]:
    """Get the currently active experiment monitor."""
    return _active_monitor


def set_active_monitor(monitor: Optional[LiveExperimentMonitor]) -> None:
    """Set the active experiment monitor."""
    global _active_monitor
    _active_monitor = monitor
