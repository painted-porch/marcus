"""
Continuous monitoring for task assignment state changes.

This module monitors for task state reversions and handles them appropriately
to prevent workers from being stuck with reverted tasks.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.core.assignment_persistence import AssignmentPersistence
from src.core.assignment_reconciliation import AssignmentReconciler
from src.core.models import Task, TaskStatus
from src.integrations.kanban_interface import KanbanInterface

logger = logging.getLogger(__name__)


class AssignmentMonitor:
    """Monitors task assignments for state reversions and inconsistencies."""

    def __init__(
        self,
        persistence: AssignmentPersistence,
        kanban_client: KanbanInterface,
        check_interval: int = 30,  # seconds
    ):
        """
        Initialize the assignment monitor.

        Parameters
        ----------
        persistence : AssignmentPersistence
            Assignment persistence layer
        kanban_client : KanbanInterface
            Kanban board interface
        check_interval : int
            How often to check for reversions (seconds)
        """
        self.persistence = persistence
        self.kanban_client = kanban_client
        self.reconciler = AssignmentReconciler(persistence, kanban_client)
        self.check_interval = check_interval
        self._running = False
        self._monitor_task: Optional[asyncio.Task[None]] = None

        # Track task states to detect changes
        self._last_known_states: Dict[str, TaskStatus] = {}
        self._reversion_count: Dict[str, int] = (
            {}
        )  # Track how many times a task reverted

    async def start(self) -> None:
        """Start monitoring for assignment reversions."""
        if self._running:
            logger.warning("Assignment monitor already running")
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"Assignment monitor started (interval: {self.check_interval}s)")

    async def stop(self) -> None:
        """Stop the assignment monitor."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Assignment monitor stopped")

    async def _monitor_loop(self) -> None:
        """Run the main monitoring loop."""
        while self._running:
            try:
                await self._check_for_reversions()
                await asyncio.sleep(self.check_interval)
            except Exception as e:
                logger.error(f"Error in assignment monitor: {e}")
                await asyncio.sleep(self.check_interval)

    async def _check_for_reversions(self) -> None:
        """Check for task state reversions."""
        try:
            # Get current assignments from persistence
            assignments = await self.persistence.load_assignments()

            # Get current task states from kanban
            try:
                all_tasks = await self.kanban_client.get_all_tasks()
            except AttributeError as e:
                # Fallback: if get_all_tasks is not available, use available tasks only
                logger.warning(
                    f"get_all_tasks not available on {type(self.kanban_client)}: {e}"
                )
                logger.warning(
                    "Using get_available_tasks as fallback - "
                    "health check will be limited"
                )
                all_tasks = await self.kanban_client.get_available_tasks()

            task_map = {task.id: task for task in all_tasks}

            reversions_detected = []

            for worker_id, assignment in list(assignments.items()):
                task_id = assignment["task_id"]

                # Check if task still exists
                if task_id not in task_map:
                    logger.warning(f"Task {task_id} no longer exists in kanban")
                    await self._handle_missing_task(worker_id, task_id)
                    continue

                task = task_map[task_id]

                # Check for state reversion
                if await self._detect_reversion(task, worker_id):
                    reversions_detected.append(
                        {
                            "task_id": task_id,
                            "worker_id": worker_id,
                            "current_status": task.status,
                            "assigned_to": task.assigned_to,
                        }
                    )

                # Update last known state
                self._last_known_states[task_id] = task.status

            # Handle detected reversions
            if reversions_detected:
                logger.warning(f"Detected {len(reversions_detected)} task reversions")
                for reversion in reversions_detected:
                    await self._handle_reversion(reversion)

        except Exception as e:
            logger.error(f"Error checking for reversions: {e}")

    async def _detect_reversion(self, task: Task, worker_id: str) -> bool:
        """
        Detect if a task has been reverted.

        Returns
        -------
        bool
            True if the task was reverted, False otherwise
        """
        task_id = task.id

        # Case 1: Task went back to TODO
        if task.status == TaskStatus.TODO:
            logger.info(f"Task {task_id} reverted to TODO status")
            return True

        # Case 2: Task is IN_PROGRESS but assigned to different worker
        if task.status == TaskStatus.IN_PROGRESS and task.assigned_to != worker_id:
            logger.info(
                f"Task {task_id} reassigned from {worker_id} to {task.assigned_to}"
            )
            return True

        # Case 3: Task completed by someone else
        if task.status == TaskStatus.DONE and task.assigned_to != worker_id:
            logger.info(
                f"Task {task_id} completed by {task.assigned_to} instead of {worker_id}"
            )
            return True

        # Case 4: Task blocked but no longer assigned
        if task.status == TaskStatus.BLOCKED and not task.assigned_to:
            logger.info(f"Task {task_id} blocked and unassigned")
            return True

        # Case 5: Task declined but no longer assigned (ADR-0013 K5,
        # Codex round-6 sweep on PR #834 -- the same unassigned-terminal
        # shape Case 4 already covers for BLOCKED).
        if task.status == TaskStatus.DECLINED and not task.assigned_to:
            logger.info(f"Task {task_id} declined and unassigned")
            return True

        return False

    async def _handle_reversion(self, reversion: Dict[str, Any]) -> None:
        """Handle a detected task reversion."""
        task_id = reversion["task_id"]
        worker_id = reversion["worker_id"]

        # Track reversion count
        self._reversion_count[task_id] = self._reversion_count.get(task_id, 0) + 1

        # Remove assignment from persistence
        await self.persistence.remove_assignment(worker_id)

        # Log the reversion
        logger.warning(
            f"Handled reversion: Task {task_id} for worker {worker_id} "
            f"(status: {reversion['current_status']}, "
            f"assigned_to: {reversion['assigned_to']}, "
            f"reversion_count: {self._reversion_count[task_id]})"
        )

        # If task reverts too many times, flag it
        if self._reversion_count[task_id] >= 3:
            logger.error(
                f"Task {task_id} has reverted {self._reversion_count[task_id]} times! "
                "This task may have issues."
            )

    async def _handle_missing_task(self, worker_id: str, task_id: str) -> None:
        """Handle a task that no longer exists."""
        # Remove assignment
        await self.persistence.remove_assignment(worker_id)

        logger.warning(
            f"Removed assignment for missing task {task_id} from worker {worker_id}"
        )

    async def force_reconciliation(self) -> Dict[str, Any]:
        """Force a full reconciliation check."""
        logger.info("Forcing assignment reconciliation")
        results = await self.reconciler.reconcile_assignments()
        logger.info(f"Reconciliation results: {results}")
        # Convert ReconciliationResults to dict format
        return {
            "status": "completed",
            "fixed_assignments": getattr(results, "fixed_assignments", 0),
            "removed_assignments": getattr(results, "removed_assignments", 0),
            "errors": getattr(results, "errors", []),
            "details": str(results),
        }

    def get_monitoring_stats(self) -> Dict[str, Any]:
        """Get current monitoring statistics."""
        return {
            "monitoring": self._running,
            "check_interval": self.check_interval,
            "tracked_tasks": len(self._last_known_states),
            "reversion_counts": dict(self._reversion_count),
            "last_check": datetime.now(timezone.utc).isoformat(),
        }


class AssignmentHealthChecker:
    """Performs periodic health checks on assignment system."""

    def __init__(
        self,
        persistence: AssignmentPersistence,
        kanban_client: KanbanInterface,
        monitor: AssignmentMonitor,
    ):
        self.persistence = persistence
        self.kanban_client = kanban_client
        self.monitor = monitor
        self.reconciler = AssignmentReconciler(persistence, kanban_client)

    async def check_assignment_health(self) -> Dict[str, Any]:
        """
        Comprehensive health check of assignment system.

        Returns
        -------
        Dict[str, Any]
            Dictionary with health status and any issues found
        """
        health: Dict[str, Any] = {
            "healthy": True,
            "issues": [],
            "metrics": {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        try:
            # Check persistence health
            persisted = await self.persistence.load_assignments()
            health["metrics"]["persisted_assignments"] = len(persisted)

            # Check kanban state
            try:
                tasks = await self.kanban_client.get_all_tasks()
            except AttributeError as e:
                # Fallback: if get_all_tasks is not available, use available tasks only
                logger.warning(
                    f"get_all_tasks not available on {type(self.kanban_client)}: {e}"
                )
                logger.warning(
                    "Using get_available_tasks as fallback - "
                    "health check will be limited"
                )
                tasks = await self.kanban_client.get_available_tasks()

            in_progress = [t for t in tasks if t.status == TaskStatus.IN_PROGRESS]
            health["metrics"]["in_progress_tasks"] = len(in_progress)

            # Check for mismatches
            persisted_task_ids = {a["task_id"] for a in list(persisted.values())}
            kanban_assigned_ids = {t.id for t in in_progress if t.assigned_to}

            # Tasks only in persistence
            orphaned_persisted = persisted_task_ids - kanban_assigned_ids
            if orphaned_persisted:
                health["healthy"] = False
                health["issues"].append(
                    {
                        "type": "orphaned_assignments",
                        "description": (
                            f"{len(orphaned_persisted)} tasks in persistence "
                            "but not assigned in kanban"
                        ),
                        "task_ids": list(orphaned_persisted),
                    }
                )

            # Tasks only in kanban
            orphaned_kanban = kanban_assigned_ids - persisted_task_ids
            if orphaned_kanban:
                health["healthy"] = False
                health["issues"].append(
                    {
                        "type": "untracked_assignments",
                        "description": (
                            f"{len(orphaned_kanban)} tasks assigned in kanban "
                            "but not tracked"
                        ),
                        "task_ids": list(orphaned_kanban),
                    }
                )

            # Check monitor status
            monitor_stats = self.monitor.get_monitoring_stats()
            health["metrics"]["monitor"] = monitor_stats

            if not monitor_stats["monitoring"]:
                health["issues"].append(
                    {
                        "type": "monitor_stopped",
                        "description": "Assignment monitor is not running",
                        "severity": "warning",
                    }
                )

            # Check for high reversion counts
            high_reversions = {
                task_id: count
                for task_id, count in monitor_stats["reversion_counts"].items()
                if count >= 3
            }
            if high_reversions:
                health["issues"].append(
                    {
                        "type": "high_reversions",
                        "description": "Tasks with high reversion counts",
                        "tasks": high_reversions,
                        "severity": "warning",
                    }
                )

        except Exception as e:
            health["healthy"] = False
            health["issues"].append(
                {
                    "type": "check_error",
                    "description": f"Error during health check: {str(e)}",
                    "severity": "error",
                }
            )

        return health
