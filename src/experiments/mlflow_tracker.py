"""
MLflow Experiment Tracking for Marcus.

This module provides comprehensive experiment tracking using MLflow,
allowing systematic testing of different conditions and configurations.

Key Features
------------
- Track experiment configurations (agent count, complexity, etc.)
- Log all Marcus metrics (velocity, task times, etc.)
- Record experiment conditions (blockers, artifacts, decisions)
- Compare runs with different parameters
- Generate performance reports

Usage Example
-------------
>>> from src.experiments.mlflow_tracker import MarcusExperiment
>>>
>>> experiment = MarcusExperiment(
...     experiment_name="50-agent-swarm-test",
...     tracking_uri="./mlruns"
... )
>>>
>>> with experiment.start_run(
...     run_name="twitter-clone-enterprise",
...     params={
...         "num_agents": 50,
...         "complexity": "enterprise",
...         "enable_blockers": True,
...         "enable_artifacts": True,
...         "enable_decisions": True
...     }
... ):
...     # Your experiment code here
...     experiment.log_metric("velocity", 12.5)
...     experiment.log_task_completion(task_id, duration_seconds)
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import mlflow
from mlflow.tracking import MlflowClient

logger = logging.getLogger(__name__)


class MarcusExperiment:
    """
    MLflow experiment tracker for Marcus.

    Provides comprehensive tracking of Marcus experiments including
    configurations, metrics, and various experimental conditions.

    Parameters
    ----------
    experiment_name : str
        Name of the MLflow experiment
    tracking_uri : str, optional
        MLflow tracking URI (default: "./mlruns")
    """

    def __init__(
        self,
        experiment_name: str,
        tracking_uri: str = "./mlruns",
    ):
        """Initialize MLflow experiment tracker."""
        self.experiment_name = experiment_name
        self.tracking_uri = tracking_uri

        # Set up MLflow
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(experiment_name)

        self.client = MlflowClient()
        self.current_run_id: Optional[str] = None

        # Tracking state
        self.task_times: List[float] = []
        self.blocker_count = 0
        self.artifact_count = 0
        self.decision_count = 0
        self.context_requests = 0

        logger.info(f"Initialized MLflow experiment: {experiment_name}")

    def start_run(
        self,
        run_name: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> mlflow.ActiveRun:
        """
        Start a new MLflow run.

        Parameters
        ----------
        run_name : str, optional
            Name for this run
        params : Dict[str, Any], optional
            Experiment parameters to log
        tags : Dict[str, str], optional
            Tags to add to the run

        Returns
        -------
        mlflow.ActiveRun
            Active MLflow run context manager
        """
        run = mlflow.start_run(run_name=run_name)
        self.current_run_id = run.info.run_id

        # Log parameters
        if params:
            mlflow.log_params(params)

        # Log tags
        if tags:
            mlflow.set_tags(tags)

        # Log experiment start time
        mlflow.log_param("start_time", datetime.now(timezone.utc).isoformat())

        # Reset tracking state
        self.task_times = []
        self.blocker_count = 0
        self.artifact_count = 0
        self.decision_count = 0
        self.context_requests = 0

        logger.info(f"Started MLflow run: {run_name or self.current_run_id}")
        return run

    def log_metric(self, key: str, value: float, step: Optional[int] = None) -> None:
        """
        Log a metric to MLflow.

        Parameters
        ----------
        key : str
            Metric name
        value : float
            Metric value
        step : int, optional
            Step number for time-series metrics
        """
        mlflow.log_metric(key, value, step=step)

    def log_metrics(
        self, metrics: Dict[str, float], step: Optional[int] = None
    ) -> None:
        """
        Log multiple metrics at once.

        Parameters
        ----------
        metrics : Dict[str, float]
            Dictionary of metric names and values
        step : int, optional
            Step number for time-series metrics
        """
        mlflow.log_metrics(metrics, step=step)

    def log_param(self, key: str, value: Any) -> None:
        """Log a parameter to MLflow."""
        mlflow.log_param(key, value)

    def log_params(self, params: Dict[str, Any]) -> None:
        """Log multiple parameters at once."""
        mlflow.log_params(params)

    def log_task_completion(
        self,
        task_id: str,
        duration_seconds: float,
        estimated_hours: Optional[float] = None,
        actual_hours: Optional[float] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        """
        Log task completion metrics.

        Parameters
        ----------
        task_id : str
            Unique task identifier
        duration_seconds : float
            Time taken to complete the task in seconds
        estimated_hours : float, optional
            Originally estimated hours
        actual_hours : float, optional
            Actual hours spent
        agent_id : str, optional
            ID of the agent who completed the task
        """
        self.task_times.append(duration_seconds)

        # Log individual task metrics
        mlflow.log_metric(f"task_duration_{task_id}", duration_seconds)

        if estimated_hours and actual_hours:
            accuracy = min(estimated_hours, actual_hours) / max(
                estimated_hours, actual_hours
            )
            mlflow.log_metric(f"estimation_accuracy_{task_id}", accuracy)

        # Log aggregate metrics
        avg_duration = sum(self.task_times) / len(self.task_times)
        mlflow.log_metric("avg_task_duration", avg_duration)
        mlflow.log_metric("total_tasks_completed", len(self.task_times))

    def log_blocker(
        self,
        agent_id: str,
        task_id: str,
        blocker_description: str,
        severity: str = "medium",
    ) -> None:
        """
        Log a blocker event.

        Parameters
        ----------
        agent_id : str
            Agent reporting the blocker
        task_id : str
            Task that is blocked
        blocker_description : str
            Description of the blocker
        severity : str
            Blocker severity (low, medium, high)
        """
        self.blocker_count += 1

        # Log blocker details as artifact
        blocker_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "task_id": task_id,
            "description": blocker_description,
            "severity": severity,
        }

        # Save to temp file and log as artifact
        blocker_file = f"blocker_{self.blocker_count}.json"
        with open(blocker_file, "w") as f:
            json.dump(blocker_data, f, indent=2)

        mlflow.log_artifact(blocker_file)
        os.remove(blocker_file)

        # Log metrics
        mlflow.log_metric("total_blockers", self.blocker_count)
        mlflow.log_metric(f"blocker_{severity}", 1)

        logger.info(f"Logged blocker: {agent_id} on {task_id} ({severity})")

    def log_artifact_event(
        self,
        task_id: str,
        artifact_type: str,
        filename: str,
        description: str = "",
    ) -> None:
        """
        Log an artifact creation event.

        Parameters
        ----------
        task_id : str
            Task that created the artifact
        artifact_type : str
            Type of artifact (specification, api, design, etc.)
        filename : str
            Name of the artifact file
        description : str, optional
            Description of the artifact
        """
        self.artifact_count += 1

        artifact_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "type": artifact_type,
            "filename": filename,
            "description": description,
        }

        # Save artifact metadata
        artifact_file = f"artifact_log_{self.artifact_count}.json"
        with open(artifact_file, "w") as f:
            json.dump(artifact_data, f, indent=2)

        mlflow.log_artifact(artifact_file)
        os.remove(artifact_file)

        # Log metrics
        mlflow.log_metric("total_artifacts", self.artifact_count)
        mlflow.log_metric(f"artifacts_{artifact_type}", 1)

        logger.info(f"Logged artifact: {filename} ({artifact_type})")

    def log_decision(
        self,
        agent_id: str,
        task_id: str,
        decision: str,
    ) -> None:
        """
        Log an architectural or technical decision.

        Parameters
        ----------
        agent_id : str
            Agent making the decision
        task_id : str
            Task context for the decision
        decision : str
            Description of the decision
        """
        self.decision_count += 1

        decision_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "task_id": task_id,
            "decision": decision,
        }

        # Save decision log
        decision_file = f"decision_{self.decision_count}.json"
        with open(decision_file, "w") as f:
            json.dump(decision_data, f, indent=2)

        mlflow.log_artifact(decision_file)
        os.remove(decision_file)

        # Log metrics
        mlflow.log_metric("total_decisions", self.decision_count)

        logger.info(f"Logged decision: {agent_id} on {task_id}")

    def log_context_request(
        self,
        agent_id: str,
        task_id: str,
        context_type: str = "task_context",
    ) -> None:
        """
        Log a context request event.

        Parameters
        ----------
        agent_id : str
            Agent requesting context
        task_id : str
            Task for which context was requested
        context_type : str
            Type of context requested
        """
        self.context_requests += 1

        # Log metrics
        mlflow.log_metric("total_context_requests", self.context_requests)
        mlflow.log_metric(f"context_{context_type}", 1)

        logger.info(f"Logged context request: {agent_id} for {task_id}")

    def log_velocity(self, velocity: float, step: Optional[int] = None) -> None:
        """
        Log team velocity metric.

        Parameters
        ----------
        velocity : float
            Tasks completed per time period
        step : int, optional
            Time step for tracking velocity over time
        """
        mlflow.log_metric("velocity", velocity, step=step)

    def log_project_state(
        self,
        total_tasks: int,
        completed_tasks: int,
        in_progress_tasks: int,
        blocked_tasks: int,
        progress_percent: float,
        velocity: float,
        step: Optional[int] = None,
    ) -> None:
        """
        Log complete project state.

        Parameters
        ----------
        total_tasks : int
            Total number of tasks
        completed_tasks : int
            Number of completed tasks
        in_progress_tasks : int
            Number of in-progress tasks
        blocked_tasks : int
            Number of blocked tasks
        progress_percent : float
            Overall progress percentage
        velocity : float
            Current velocity
        step : int, optional
            Time step for tracking over time
        """
        metrics = {
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "in_progress_tasks": in_progress_tasks,
            "blocked_tasks": blocked_tasks,
            "progress_percent": progress_percent,
            "velocity": velocity,
        }

        mlflow.log_metrics(metrics, step=step)

    def log_agent_metrics(
        self,
        agent_id: str,
        tasks_completed: int,
        avg_task_duration: float,
        success_rate: float,
    ) -> None:
        """
        Log metrics for a specific agent.

        Parameters
        ----------
        agent_id : str
            Agent identifier
        tasks_completed : int
            Number of tasks completed by this agent
        avg_task_duration : float
            Average task duration for this agent
        success_rate : float
            Success rate (0.0 to 1.0)
        """
        mlflow.log_metric(f"agent_{agent_id}_tasks_completed", tasks_completed)
        mlflow.log_metric(f"agent_{agent_id}_avg_duration", avg_task_duration)
        mlflow.log_metric(f"agent_{agent_id}_success_rate", success_rate)

    def end_run(
        self,
        final_metrics: Optional[Dict[str, float]] = None,
        summary: Optional[str] = None,
    ) -> None:
        """
        End the current MLflow run.

        Parameters
        ----------
        final_metrics : Dict[str, float], optional
            Final metrics to log before ending
        summary : str, optional
            Summary text to save as artifact
        """
        if final_metrics:
            mlflow.log_metrics(final_metrics)

        # Log end time
        mlflow.log_param("end_time", datetime.now(timezone.utc).isoformat())

        # Log condition counts
        mlflow.log_metric("final_blocker_count", self.blocker_count)
        mlflow.log_metric("final_artifact_count", self.artifact_count)
        mlflow.log_metric("final_decision_count", self.decision_count)
        mlflow.log_metric("final_context_requests", self.context_requests)

        # Save summary if provided
        if summary:
            summary_file = "experiment_summary.txt"
            with open(summary_file, "w") as f:
                f.write(summary)
            mlflow.log_artifact(summary_file)
            os.remove(summary_file)

        mlflow.end_run()
        logger.info(f"Ended MLflow run: {self.current_run_id}")

    def compare_runs(
        self,
        run_ids: Optional[List[str]] = None,
        metric_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Compare multiple experiment runs.

        Parameters
        ----------
        run_ids : List[str], optional
            List of run IDs to compare (if None, uses all runs in experiment)
        metric_names : List[str], optional
            Specific metrics to compare

        Returns
        -------
        Dict[str, Any]
            Comparison data with metrics for each run
        """
        if run_ids is None:
            # Get all runs from this experiment
            experiment = self.client.get_experiment_by_name(self.experiment_name)
            if experiment is None:
                logger.warning(f"Experiment '{self.experiment_name}' not found")
                return {}
            # experiment_ids is list[str]. A bare string is iterable, so
            # passing one made mlflow treat it as a sequence of
            # single-character experiment ids.
            runs = self.client.search_runs([experiment.experiment_id])
            run_ids = [run.info.run_id for run in runs]

        comparison = {}

        for run_id in run_ids:
            run = self.client.get_run(run_id)
            comparison[run_id] = {
                "params": run.data.params,
                "metrics": run.data.metrics,
                "start_time": run.info.start_time,
                "end_time": run.info.end_time,
            }

        return comparison

    def generate_report(
        self,
        output_file: str = "experiment_report.json",
    ) -> None:
        """
        Generate a comprehensive experiment report.

        Parameters
        ----------
        output_file : str
            Path to save the report
        """
        comparison = self.compare_runs()

        report = {
            "experiment_name": self.experiment_name,
            "tracking_uri": self.tracking_uri,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "runs": comparison,
        }

        with open(output_file, "w") as f:
            json.dump(report, f, indent=2)

        logger.info(f"Generated experiment report: {output_file}")

    def log_code_quality(
        self,
        test_coverage: float,
        linting_errors: int,
        type_errors: int,
        cyclomatic_complexity: Optional[float] = None,
        step: Optional[int] = None,
    ) -> None:
        """
        Log code quality metrics.

        Parameters
        ----------
        test_coverage : float
            Test coverage percentage (0-100)
        linting_errors : int
            Number of linting errors detected
        type_errors : int
            Number of type checking errors (e.g., mypy errors)
        cyclomatic_complexity : float, optional
            Average cyclomatic complexity across codebase
        step : int, optional
            Step number for time-series tracking
        """
        metrics = {
            "test_coverage": test_coverage,
            "linting_errors": linting_errors,
            "type_errors": type_errors,
        }

        if cyclomatic_complexity is not None:
            metrics["cyclomatic_complexity"] = cyclomatic_complexity

        mlflow.log_metrics(metrics, step=step)
        logger.info(
            f"Logged code quality: coverage={test_coverage}%, "
            f"lint={linting_errors}, types={type_errors}"
        )

    def log_dependency_metrics(
        self,
        critical_path_length: int,
        avg_dependency_depth: float,
        parallelizable_fraction: float,
        max_parallel_tasks: int,
        step: Optional[int] = None,
    ) -> None:
        """
        Log task dependency structure metrics.

        Parameters
        ----------
        critical_path_length : int
            Length of the longest dependency chain
        avg_dependency_depth : float
            Average depth of dependency chains
        parallelizable_fraction : float
            Fraction of tasks that can run in parallel (0.0-1.0)
        max_parallel_tasks : int
            Maximum number of tasks that can run simultaneously
        step : int, optional
            Step number for time-series tracking
        """
        metrics = {
            "critical_path_length": critical_path_length,
            "avg_dependency_depth": avg_dependency_depth,
            "parallelizable_fraction": parallelizable_fraction,
            "max_parallel_tasks": max_parallel_tasks,
        }

        mlflow.log_metrics(metrics, step=step)
        logger.info(
            f"Logged dependency metrics: critical_path={critical_path_length}, "
            f"parallel_fraction={parallelizable_fraction:.2%}"
        )

    def log_agent_idle_time(
        self, agent_id: str, idle_seconds: float, step: Optional[int] = None
    ) -> None:
        """
        Log time an agent spent waiting for tasks.

        Parameters
        ----------
        agent_id : str
            Agent identifier
        idle_seconds : float
            Time spent idle in seconds
        step : int, optional
            Step number for time-series tracking
        """
        mlflow.log_metric(f"agent_{agent_id}_idle_time", idle_seconds, step=step)
        logger.info(f"Logged idle time: {agent_id} idle for {idle_seconds}s")

    def log_coordination_overhead(
        self,
        context_requests: int,
        blockers: int,
        avg_blocker_resolution_time: float,
        merge_conflicts: Optional[int] = None,
        step: Optional[int] = None,
    ) -> None:
        """
        Log coordination overhead metrics for multi-agent systems.

        Parameters
        ----------
        context_requests : int
            Total number of context requests made
        blockers : int
            Total number of blockers reported
        avg_blocker_resolution_time : float
            Average time to resolve blockers in seconds
        merge_conflicts : int, optional
            Number of git merge conflicts encountered
        step : int, optional
            Step number for time-series tracking
        """
        metrics = {
            "coordination_context_requests": context_requests,
            "coordination_blockers": blockers,
            "coordination_avg_blocker_resolution": avg_blocker_resolution_time,
        }

        if merge_conflicts is not None:
            metrics["coordination_merge_conflicts"] = merge_conflicts

        mlflow.log_metrics(metrics, step=step)
        logger.info(
            f"Logged coordination overhead: {context_requests} context requests, "
            f"{blockers} blockers"
        )

    def log_resource_usage(
        self,
        total_tokens: int,
        api_cost_usd: float,
        tokens_per_task: Optional[float] = None,
        cost_per_task: Optional[float] = None,
        step: Optional[int] = None,
    ) -> None:
        """
        Log resource usage and cost metrics.

        Parameters
        ----------
        total_tokens : int
            Total tokens consumed
        api_cost_usd : float
            Total API cost in USD
        tokens_per_task : float, optional
            Average tokens per completed task
        cost_per_task : float, optional
            Average cost per completed task in USD
        step : int, optional
            Step number for time-series tracking
        """
        metrics = {
            "total_tokens": float(total_tokens),
            "api_cost_usd": api_cost_usd,
        }

        if tokens_per_task is not None:
            metrics["tokens_per_task"] = tokens_per_task

        if cost_per_task is not None:
            metrics["cost_per_task"] = cost_per_task

        mlflow.log_metrics(metrics, step=step)
        logger.info(
            f"Logged resource usage: {total_tokens} tokens, ${api_cost_usd:.2f}"
        )

    def log_throughput(
        self,
        tasks_completed: int,
        elapsed_hours: float,
        tasks_per_hour: Optional[float] = None,
        step: Optional[int] = None,
    ) -> None:
        """
        Log throughput metrics.

        Parameters
        ----------
        tasks_completed : int
            Number of tasks completed
        elapsed_hours : float
            Total elapsed time in hours
        tasks_per_hour : float, optional
            Calculated throughput (if not provided, will be calculated)
        step : int, optional
            Step number for time-series tracking
        """
        if tasks_per_hour is None and elapsed_hours > 0:
            tasks_per_hour = tasks_completed / elapsed_hours

        metrics = {
            "throughput_tasks_completed": float(tasks_completed),
            "throughput_elapsed_hours": elapsed_hours,
        }

        if tasks_per_hour is not None:
            metrics["throughput_tasks_per_hour"] = tasks_per_hour

        mlflow.log_metrics(metrics, step=step)
        logger.info(
            f"Logged throughput: {tasks_completed} tasks in {elapsed_hours:.2f}h "
            f"({tasks_per_hour:.2f} tasks/hour)"
        )

    def log_parallel_efficiency(
        self,
        num_agents: int,
        single_agent_time: float,
        multi_agent_time: float,
        speedup_factor: Optional[float] = None,
        efficiency: Optional[float] = None,
    ) -> None:
        """
        Log parallel efficiency metrics comparing multi-agent to single-agent.

        Parameters
        ----------
        num_agents : int
            Number of agents in multi-agent configuration
        single_agent_time : float
            Time taken by single agent in hours
        multi_agent_time : float
            Time taken by multi-agent system in hours
        speedup_factor : float, optional
            Speedup factor (if not provided, will be calculated)
        efficiency : float, optional
            Parallel efficiency (if not provided, will be calculated)
        """
        if speedup_factor is None and multi_agent_time > 0:
            speedup_factor = single_agent_time / multi_agent_time

        if efficiency is None and speedup_factor is not None and num_agents > 0:
            efficiency = speedup_factor / num_agents

        metrics = {
            "parallel_num_agents": float(num_agents),
            "parallel_single_agent_time": single_agent_time,
            "parallel_multi_agent_time": multi_agent_time,
        }

        if speedup_factor is not None:
            metrics["parallel_speedup_factor"] = speedup_factor

        if efficiency is not None:
            metrics["parallel_efficiency"] = efficiency

        mlflow.log_metrics(metrics)

        if speedup_factor is not None and efficiency is not None:
            logger.info(
                f"Logged parallel efficiency: {num_agents} agents, "
                f"{speedup_factor:.2f}x speedup, {efficiency:.2%} efficiency"
            )
        else:
            logger.info(
                f"Logged parallel efficiency: {num_agents} agents "
                "(speedup/efficiency not calculated)"
            )
