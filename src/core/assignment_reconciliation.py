"""
Assignment reconciliation for Marcus.

This module handles reconciling persisted assignments with the actual
kanban board state on startup or after connectivity issues.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, TypedDict

from src.core.assignment_persistence import AssignmentPersistence
from src.core.models import TaskStatus
from src.integrations.kanban_interface import KanbanInterface

logger = logging.getLogger(__name__)


class ReconciliationResults(TypedDict):
    """Type definition for reconciliation results."""

    assignments_verified: int
    assignments_restored: int
    assignments_removed: int
    orphaned_tasks: List[Dict[str, Any]]
    errors: List[str]


class AssignmentHealth(TypedDict, total=False):
    """Type definition for assignment health status."""

    persisted_count: int
    kanban_assigned_count: int
    mismatches: List[Dict[str, Any]]
    healthy: bool
    error: str  # Optional field when there's an error


class AssignmentReconciler:
    """Reconciles persisted assignments with kanban board state."""

    def __init__(
        self, persistence: AssignmentPersistence, kanban_client: KanbanInterface
    ):
        """
        Initialize the reconciler.

        Parameters
        ----------
            persistence
                Assignment persistence layer.
            kanban_client
                Kanban board interface.
        """
        self.persistence = persistence
        self.kanban_client = kanban_client

    async def reconcile_assignments(self) -> ReconciliationResults:
        """
        Reconcile persisted assignments with kanban board state.

        Returns
        -------
            Dictionary with reconciliation results
        """
        results: ReconciliationResults = {
            "assignments_verified": 0,
            "assignments_restored": 0,
            "assignments_removed": 0,
            "orphaned_tasks": [],
            "errors": [],
        }

        try:
            # Get all persisted assignments
            persisted = await self.persistence.load_assignments()

            # Get all tasks from kanban
            all_tasks = await self.kanban_client.get_all_tasks()
            task_map = {task.id: task for task in all_tasks}

            # Check each persisted assignment
            for worker_id, assignment in list(persisted.items()):
                task_id = assignment["task_id"]

                if task_id not in task_map:
                    # Task no longer exists in kanban
                    logger.warning(
                        f"Task {task_id} no longer exists, removing assignment"
                    )
                    await self.persistence.remove_assignment(worker_id)
                    results["assignments_removed"] += 1
                    continue

                task = task_map[task_id]

                # Check if task is still assigned to this worker
                if (
                    task.status == TaskStatus.IN_PROGRESS
                    and task.assigned_to == worker_id
                ):
                    # Assignment is valid
                    results["assignments_verified"] += 1
                elif task.status == TaskStatus.DONE:
                    # Task was completed, remove assignment
                    logger.info(f"Task {task_id} is completed, removing assignment")
                    await self.persistence.remove_assignment(worker_id)
                    results["assignments_removed"] += 1
                elif task.status == TaskStatus.TODO:
                    # Task was reset to TODO, remove assignment
                    logger.warning(
                        f"Task {task_id} is back in TODO, removing assignment"
                    )
                    await self.persistence.remove_assignment(worker_id)
                    results["assignments_removed"] += 1
                else:
                    # Task is assigned to someone else or in different state
                    logger.warning(
                        f"Task {task_id} state mismatch - "
                        f"status: {task.status}, assigned_to: {task.assigned_to}"
                    )
                    await self.persistence.remove_assignment(worker_id)
                    results["assignments_removed"] += 1

            # Find orphaned IN_PROGRESS tasks (assigned in kanban but not persisted)
            persisted_task_ids = {a["task_id"] for a in list(persisted.values())}

            for task in all_tasks:
                if (
                    task.status == TaskStatus.IN_PROGRESS
                    and task.assigned_to
                    and task.id not in persisted_task_ids
                ):
                    # This task is assigned in kanban but not persisted
                    logger.info(
                        f"Found orphaned task {task.id} assigned to {task.assigned_to}"
                    )
                    results["orphaned_tasks"].append(
                        {
                            "task_id": task.id,
                            "task_name": task.name,
                            "assigned_to": task.assigned_to,
                        }
                    )

                    # Restore to persistence
                    await self.persistence.save_assignment(
                        task.assigned_to,
                        task.id,
                        {
                            "name": task.name,
                            "priority": (
                                task.priority.value if task.priority else "medium"
                            ),
                            "estimated_hours": task.estimated_hours,
                            "restored_at": datetime.now(timezone.utc).isoformat(),
                            # Issue #856 round 4: lets the assignment
                            # monitor's reversion check scope "missing"
                            # to this assignment's own project.
                            "project_id": getattr(task, "project_id", None),
                        },
                    )
                    results["assignments_restored"] += 1

        except Exception as e:
            logger.error(f"Error during reconciliation: {e}")
            results["errors"].append(str(e))

        return results

    async def get_assignment_health(self) -> AssignmentHealth:
        """
        Get health status of assignment tracking.

        Returns
        -------
            Dictionary with health metrics
        """
        health: AssignmentHealth = {
            "persisted_count": 0,
            "kanban_assigned_count": 0,
            "mismatches": [],
            "healthy": True,
        }

        try:
            # Get persisted assignments
            persisted = await self.persistence.load_assignments()
            health["persisted_count"] = len(persisted)

            # Get kanban assignments
            all_tasks = await self.kanban_client.get_all_tasks()
            kanban_assigned = [
                t
                for t in all_tasks
                if t.status == TaskStatus.IN_PROGRESS and t.assigned_to
            ]
            health["kanban_assigned_count"] = len(kanban_assigned)

            # Check for mismatches
            persisted_task_ids = {a["task_id"] for a in list(persisted.values())}
            kanban_task_ids = {t.id for t in kanban_assigned}

            # Tasks in persistence but not kanban
            only_persisted = persisted_task_ids - kanban_task_ids
            if only_persisted:
                health["mismatches"].append(
                    {"type": "only_in_persistence", "task_ids": list(only_persisted)}
                )

            # Tasks in kanban but not persistence
            only_kanban = kanban_task_ids - persisted_task_ids
            if only_kanban:
                health["mismatches"].append(
                    {"type": "only_in_kanban", "task_ids": list(only_kanban)}
                )

            health["healthy"] = len(health["mismatches"]) == 0

        except Exception as e:
            health["healthy"] = False
            health["error"] = str(e)

        return health
