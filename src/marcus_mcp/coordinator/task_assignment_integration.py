"""
Task Assignment Integration with Subtasks.

This module integrates subtask assignment into the existing
task assignment workflow without modifying core task.py logic.
"""

import logging
from typing import Any, Awaitable, Callable, Optional

from src.core.models import Task
from src.marcus_mcp.coordinator.subtask_assignment import (
    _determine_task_type,
    find_next_available_subtask,
)

logger = logging.getLogger(__name__)


def _build_eligibility_check(
    agent_id: str, state: Any
) -> Optional[Callable[[Task, Task], bool]]:
    """Build the eligibility check the subtask loop applies (issue #737).

    The returned callable evaluates BOTH the subtask and its parent
    through the shared rules in
    ``src.marcus_mcp.coordinator.eligibility``, refusing when either
    refuses, so a verify parent's subtask can never leak to the
    claim's author. Refusals are logged and recorded by the shared
    filter; the refused worker itself just sees the next candidate or
    the ordinary no-task response.

    Parameters
    ----------
    agent_id : str
        The requesting worker's id.
    state : Any
        Marcus server state; ``agent_status`` supplies the worker's
        registered identity.

    Returns
    -------
    Optional[Callable[[Task, Task], bool]]
        The check, or None when the worker has no registration to
        judge (existing behavior is then kept).
    """
    from src.core.models import WorkerStatus
    from src.marcus_mcp.coordinator.eligibility import filter_eligible_tasks

    agent = getattr(state, "agent_status", {}).get(agent_id)
    if not isinstance(agent, WorkerStatus):
        return None

    def _check(subtask: Task, parent_task: Task) -> bool:
        allowed_sub = filter_eligible_tasks(agent, [subtask], state)
        allowed_parent = filter_eligible_tasks(agent, [parent_task], state)
        return bool(allowed_sub) and bool(allowed_parent)

    return _check


async def find_optimal_task_with_subtasks(
    agent_id: str,
    state: Any,
    fallback_task_finder: Callable[[str, Any], Awaitable[Optional[Task]]],
) -> Optional[Task]:
    """
    Find optimal task for agent, prioritizing subtasks.

    Checks for available subtasks first, then falls back to
    regular task assignment if no subtasks available.

    Parameters
    ----------
    agent_id : str
        ID of the requesting agent
    state : Any
        Marcus server state instance
    fallback_task_finder : Callable[[str, Any], Awaitable[Optional[Task]]]
        Original task finder function to use if no subtasks

    Returns
    -------
    Optional[Task]
        Task or Subtask (converted to Task) for assignment
    """
    # Check if subtask feature is enabled
    from src.config.settings import Settings

    settings = Settings()
    if not settings.is_subtasks_enabled():
        logger.debug("Subtask feature is disabled, using fallback")
        return await fallback_task_finder(agent_id, state)

    # Check if subtask manager is available
    if not hasattr(state, "subtask_manager") or not state.subtask_manager:
        logger.debug("SubtaskManager not available, using fallback")
        return await fallback_task_finder(agent_id, state)

    # Get assigned task IDs
    assigned_task_ids = {a.task_id for a in state.agent_tasks.values()}
    persisted_assigned_ids = (
        await state.assignment_persistence.get_all_assigned_task_ids()
    )
    all_assigned_ids = (
        assigned_task_ids | persisted_assigned_ids | state.tasks_being_assigned
    )

    # Try to find available subtask (now returns Task directly from unified graph)
    subtask_task = find_next_available_subtask(
        agent_id,
        state.project_tasks,
        state.subtask_manager,
        all_assigned_ids,
        eligibility_check=_build_eligibility_check(agent_id, state),
    )

    if subtask_task:
        # CRITICAL: Reserve the task IMMEDIATELY to prevent race conditions
        # Multiple agents calling simultaneously must not get the same task
        state.tasks_being_assigned.add(subtask_task.id)
        logger.debug(f"Reserved subtask {subtask_task.id} for {agent_id}")

        # SIMPLIFIED: Subtask is already a Task object from unified graph!
        # Find parent task for context
        parent_task = next(
            (t for t in state.project_tasks if t.id == subtask_task.parent_task_id),
            None,
        )

        if parent_task:
            # Update parent task to IN_PROGRESS if it's still TODO
            # (first subtask being assigned)
            from src.core.models import TaskStatus

            logger.info(
                f"🔍 SUBTASK ASSIGNMENT - Parent task '{parent_task.name}' "
                f"found with status: {parent_task.status}"
            )

            if parent_task.status == TaskStatus.TODO:
                logger.info(
                    f"✅ Moving parent task '{parent_task.name}' to IN_PROGRESS "
                    f"(first subtask assignment)"
                )
                try:
                    await state.kanban_client.update_task(
                        parent_task.id,
                        {"status": TaskStatus.IN_PROGRESS},
                    )
                    # Update local state
                    parent_task.status = TaskStatus.IN_PROGRESS
                    logger.info(
                        f"✅ Successfully moved parent task '{parent_task.name}' "
                        f"to IN_PROGRESS"
                    )
                except Exception as e:
                    logger.error(
                        f"❌ Failed to update parent task status: {e}",
                        exc_info=True,
                    )
            else:
                logger.warning(
                    f"⚠️  Skipping parent task status update - "
                    f"'{parent_task.name}' status is {parent_task.status}, "
                    f"expected TODO"
                )

            # Add metadata to help identify parent task type
            # for instruction generation
            parent_task_type = _determine_task_type(parent_task)
            subtask_task._parent_task_type = parent_task_type  # type: ignore
            subtask_task._is_subtask = True  # type: ignore
            subtask_task._parent_task_name = parent_task.name  # type: ignore

            logger.info(
                f"Assigning subtask {subtask_task.name} "
                f"(parent: {parent_task.name}) to {agent_id}"
            )
            return subtask_task
        else:
            logger.warning(
                f"Parent task {subtask_task.parent_task_id} "
                f"not found for subtask {subtask_task.id}"
            )

    # No subtasks available, use fallback
    logger.debug("No available subtasks, using fallback task finder")
    return await fallback_task_finder(agent_id, state)
