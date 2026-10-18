"""
Subtask Assignment Logic for Marcus.

This module handles finding and assigning subtasks to agents,
integrating with the existing task assignment workflow.
"""

import logging
from typing import Any, Callable, List, Optional

from src.core.models import Task, TaskStatus
from src.marcus_mcp.coordinator.subtask_manager import Subtask, SubtaskManager

logger = logging.getLogger(__name__)


def _determine_task_type(task: Task) -> str:
    """
    Determine the task type (design/implementation/testing) from task name and labels.

    This logic mirrors the type detection in
    AIAnalysisEngine.generate_task_instructions() to ensure consistency.

    Parameters
    ----------
    task : Task
        The task to determine type for

    Returns
    -------
    str
        Task type: "design", "testing", or "implementation"
    """
    task_name_lower = task.name.lower()
    task_labels = getattr(task, "labels", []) or []

    # Check name and labels for explicit type indicators
    if "design" in task_name_lower or "type:design" in task_labels:
        return "design"
    elif "test" in task_name_lower or "type:testing" in task_labels:
        return "testing"
    else:
        return "implementation"


def _are_dependencies_satisfied(task: Task, all_tasks: List[Task]) -> bool:
    """
    Check if all HARD dependencies of a task are completed.

    Soft dependencies are skipped - agents can start work using design specs
    as contracts and integrate later.

    Parameters
    ----------
    task : Task
        The task to check dependencies for
    all_tasks : List[Task]
        All tasks in the project

    Returns
    -------
    bool
        True if all hard dependencies are satisfied (or no dependencies exist)

    Notes
    -----
    Dependency types:
    - "hard": Agent must wait for dependency to be DONE before starting
    - "soft": Agent can start using design specs, integrate when ready
    - Empty array: Defaults to all "hard" (migration behavior)
    """
    if not task.dependencies:
        return True

    # Get dependency types (default to all "hard" if not specified)
    dependency_types = task.dependency_types or []
    if not dependency_types and task.dependencies:
        dependency_types = ["hard"] * len(task.dependencies)
        logger.debug(
            f"Task '{task.name}' has no dependency_types - "
            f"defaulting all {len(task.dependencies)} dependencies to 'hard'"
        )

    # Create a mapping of task IDs to their status
    task_status_map = {t.id: t.status for t in all_tasks}

    # Check each dependency based on its type
    for idx, dep_id in enumerate(task.dependencies):
        # Get dependency type (default to "hard" if index out of range)
        dep_type = dependency_types[idx] if idx < len(dependency_types) else "hard"

        # Skip soft dependencies - agent can use design specs
        if dep_type == "soft":
            logger.debug(
                f"Task '{task.name}' has soft dependency '{dep_id}' - "
                "can start using design specs as contract"
            )
            continue

        # Check hard dependencies - must be DONE
        dep_status = task_status_map.get(dep_id)
        if dep_status != TaskStatus.DONE:
            logger.debug(
                f"Task '{task.name}' blocked by HARD dependency '{dep_id}' "
                f"(status: {dep_status})"
            )
            return False

    logger.debug(f"Task '{task.name}' - all hard dependencies satisfied")
    return True


def find_next_available_subtask(
    agent_id: str,
    project_tasks: List[Task],
    subtask_manager: SubtaskManager,
    assigned_task_ids: set[str],
    eligibility_check: Optional[Callable[[Task, Task], bool]] = None,
) -> Optional[Task]:
    """
    Find the next available subtask for an agent using unified graph.

    SIMPLIFIED: Since subtasks are now Task objects in project_tasks with
    is_subtask=True, we can directly filter and check dependencies.

    Parameters
    ----------
    agent_id : str
        ID of the requesting agent
    project_tasks : List[Task]
        All tasks in the project (including subtasks)
    subtask_manager : SubtaskManager
        Manager tracking all subtasks (for legacy compatibility)
    assigned_task_ids : set[str]
        IDs of tasks/subtasks already assigned
    eligibility_check : Optional[Callable[[Task, Task], bool]]
        Issue #737: called with ``(subtask, parent_task)`` after all
        other filters; a False means this worker may not be offered
        the candidate (e.g. a verify parent's subtask offered to the
        claim's author) and the loop moves on. None (the default)
        keeps the pre-#737 behavior for existing callers.

    Returns
    -------
    Optional[Task]
        Next available subtask Task or None
    """
    # Filter to only subtasks (Task objects with is_subtask=True)
    subtasks = [t for t in project_tasks if t.is_subtask]

    # Sort by subtask_index FIRST for maximum parallelism across parents
    # This ensures we process 1.1, 2.1, 3.1 (parallel) before 1.2, 2.2, 3.2
    subtasks = sorted(
        subtasks, key=lambda t: (t.subtask_index or 0, t.parent_task_id or "")
    )

    for subtask in subtasks:
        # Skip if already assigned
        if subtask.id in assigned_task_ids:
            continue

        # Skip if already complete
        if subtask.status == TaskStatus.DONE:
            continue

        # Find parent task
        parent_task = next(
            (t for t in project_tasks if t.id == subtask.parent_task_id), None
        )

        if not parent_task:
            logger.warning(
                f"Parent task {subtask.parent_task_id} not found "
                f"for subtask {subtask.id}"
            )
            continue

        # Skip if parent task is DONE
        if parent_task.status == TaskStatus.DONE:
            continue

        # Check if parent task dependencies are satisfied
        # Subtasks should only be available after parent's dependencies complete
        if not _are_dependencies_satisfied(parent_task, project_tasks):
            logger.debug(
                f"Skipping subtask '{subtask.name}' - "
                f"parent '{parent_task.name}' dependencies not satisfied"
            )
            continue

        # Check if subtask's own dependencies are satisfied
        if not _are_dependencies_satisfied(subtask, project_tasks):
            logger.debug(
                f"Skipping subtask '{subtask.name}' - "
                "subtask dependencies not satisfied"
            )
            continue

        # Issue #737: eligibility is the LAST filter, after every
        # other guard, so a refusal here means the candidate would
        # otherwise have been offered. The check sees the subtask AND
        # its parent, because a verify parent's subtask must never
        # reach the claim's author even when the subtask itself
        # carries no dependency on the find task.
        if eligibility_check is not None and not eligibility_check(
            subtask, parent_task
        ):
            continue

        # Found an available subtask!
        logger.info(
            f"Found available subtask {subtask.name} " f"(parent: {parent_task.name})"
        )
        return subtask

    logger.debug("No available subtasks found")
    return None


def convert_subtask_to_task(subtask: Subtask, parent_task: Task) -> Task:
    """
    Convert a legacy Subtask to a Task object for assignment.

    LEGACY COMPATIBILITY: This function is only needed for backwards compatibility
    with code that still uses Subtask objects. With unified storage, subtasks are
    already Task objects and don't need conversion.

    Parameters
    ----------
    subtask : Subtask
        The legacy subtask to convert
    parent_task : Task
        The parent task this subtask belongs to

    Returns
    -------
    Task
        Task object representing the subtask with parent task type metadata
    """
    # Determine parent task type
    parent_task_type = _determine_task_type(parent_task)

    # Create a Task object from the legacy Subtask
    task = Task(
        id=subtask.id,
        name=subtask.name,
        description=subtask.description,
        status=subtask.status,
        priority=subtask.priority,
        assigned_to=subtask.assigned_to,
        created_at=subtask.created_at,
        updated_at=subtask.created_at,  # Use created_at as updated_at
        due_date=parent_task.due_date,  # Inherit from parent
        estimated_hours=subtask.estimated_hours,
        dependencies=subtask.dependencies,
        dependency_types=subtask.dependency_types,
        labels=parent_task.labels,  # Inherit labels from parent
        project_id=parent_task.project_id,
        project_name=parent_task.project_name,
    )

    # Add parent task type as metadata
    # This ensures subtasks inherit the correct instruction type
    task._parent_task_type = parent_task_type  # type: ignore[attr-defined]

    logger.debug(
        f"Converted legacy subtask '{subtask.name}' "
        f"with parent task type: {parent_task_type}"
    )

    return task


async def check_and_complete_parent_task(
    parent_task_id: str,
    subtask_manager: SubtaskManager,
    kanban_client: Any,
    state: Any = None,
    completing_agent_id: str = "",
) -> bool:
    """
    Check if all subtasks are complete and auto-complete parent task.

    CRITICAL: Also rolls up subtask artifacts and decisions to parent task
    so that dependent tasks can see the work done by subtasks.

    Parameters
    ----------
    parent_task_id : str
        ID of the parent task
    subtask_manager : SubtaskManager
        Manager tracking all subtasks
    kanban_client : Any
        Kanban client for updating task status
    state : Any, optional
        Marcus server state for accessing artifacts and context
    completing_agent_id : str, optional
        ID of the agent that completed the final subtask. When provided,
        a ``task_assignment`` event is emitted so Cato's DAG/SwimLanes
        can attribute the parent completion to a real agent.

    Returns
    -------
    bool
        True if parent was auto-completed
    """
    # Check if parent is complete using unified storage
    project_tasks = state.project_tasks if state else None
    if subtask_manager.is_parent_complete(parent_task_id, project_tasks):
        logger.info(
            f"All subtasks complete for {parent_task_id} " "- auto-completing parent"
        )

        # CRITICAL FIX: Roll up subtask artifacts and decisions to parent
        # This ensures dependent tasks can see what subtasks produced
        if state:
            await _rollup_subtask_artifacts_to_parent(
                parent_task_id, subtask_manager, state
            )
            await _rollup_subtask_decisions_to_parent(
                parent_task_id, subtask_manager, state
            )

        # Update parent task to DONE
        await kanban_client.update_task(
            parent_task_id,
            {
                "status": TaskStatus.DONE,
                "progress": 100,
            },
        )

        # Add completion comment (query from unified storage)
        subtasks = subtask_manager.get_subtasks(parent_task_id, project_tasks)
        completion_comment = (
            f"✅ **Auto-completed**: All {len(subtasks)} "
            "subtasks completed\n\n"
            "Completed subtasks:\n"
        )
        for subtask in subtasks:
            completion_comment += f"- {subtask.name}\n"

        await kanban_client.add_comment(parent_task_id, completion_comment)

        # Emit attribution event so Cato DAG/SwimLanes can link the parent
        # task to the agent that drove it to completion (Bug 3 fix).
        # Without this, the parent appears as a ghost completion with no
        # agent record — invisible in experiment analytics.
        #
        # DELIBERATELY NOT WRITING task_outcomes HERE — see contract below.
        #
        # Auto-completion contract (do not change without an ADR):
        #
        #   Parents-with-subtasks are coordination containers. The work data
        #   (started_at, completed_at, actual_hours, agent_id) lives on the
        #   subtask outcomes — which are real per-agent records written via
        #   Memory.record_task_completion when each subtask finishes.
        #
        #   Writing a synthetic parent outcome here would corrupt agent
        #   learning:
        #     * Memory._update_agent_profile (memory.py) ticks
        #       total_tasks + skill_success_rates EMA per outcome row;
        #       a parent row attributed to completing_agent_id would
        #       double-count their work.
        #     * analysis.aggregator._build_agent_histories sums
        #       outcome.actual_hours into total_hours, also double-counting.
        #     * Scheduler tests (test_scheduler_prototype_mode.py) already
        #       assert "parent with subtasks is a container — should be
        #       excluded" from CPM. Outcome accounting must match that.
        #
        #   Contrast with About/design auto-completion in nlp_tools.py:
        #   those write task_outcomes with agent_id="system" or "Marcus",
        #   synthetic IDs that don't correspond to a real worker and so
        #   don't pollute learning. Those tasks are LEAVES with no
        #   subtasks — the outcome write is the only place their data
        #   lives. Subtask-driven parents are DERIVATIVE — the data
        #   already lives on the subtasks.
        #
        #   Downstream observers (Cato) aggregate subtask data for parent
        #   display rather than relying on a parent outcome row. See
        #   cato_src/core/aggregator._apply_subtask_rollup.
        if completing_agent_id and state and hasattr(state, "log_event"):
            state.log_event(
                "task_assignment",
                {
                    "agent_id": completing_agent_id,
                    "task_id": parent_task_id,
                    "source": "auto_complete",
                },
            )

        return True

    return False


async def _rollup_subtask_artifacts_to_parent(
    parent_task_id: str, subtask_manager: SubtaskManager, state: Any
) -> None:
    """
    Roll up all subtask artifacts to the parent task.

    This ensures that when Task 2 depends on Task 1, and both have subtasks,
    Task 2's subtasks can see the artifacts produced by Task 1's subtasks.

    Parameters
    ----------
    parent_task_id : str
        ID of the parent task
    subtask_manager : SubtaskManager
        Manager tracking all subtasks
    state : Any
        Marcus server state for accessing task_artifacts
    """
    if not hasattr(state, "task_artifacts"):
        return

    # Query subtasks from unified storage
    subtasks = subtask_manager.get_subtasks(parent_task_id, state.project_tasks)
    if not subtasks:
        return

    # Initialize parent's artifact list if not exists
    if parent_task_id not in state.task_artifacts:
        state.task_artifacts[parent_task_id] = []

    # Collect artifacts from all subtasks
    rollup_count = 0
    for subtask in subtasks:
        if subtask.id in state.task_artifacts:
            for artifact in state.task_artifacts[subtask.id]:
                # Add subtask context to artifact
                rollup_artifact = artifact.copy()
                rollup_artifact["from_subtask"] = subtask.id
                rollup_artifact["from_subtask_name"] = subtask.name
                rollup_artifact["description"] = (
                    f"[From subtask: {subtask.name}] "
                    f"{rollup_artifact.get('description', '')}"
                )

                # Add to parent's artifacts
                state.task_artifacts[parent_task_id].append(rollup_artifact)
                rollup_count += 1

    if rollup_count > 0:
        logger.info(
            f"Rolled up {rollup_count} artifacts from {len(subtasks)} subtasks "
            f"to parent task {parent_task_id}"
        )


async def _rollup_subtask_decisions_to_parent(
    parent_task_id: str, subtask_manager: SubtaskManager, state: Any
) -> None:
    """
    Roll up all subtask decisions to the parent task.

    This ensures that when Task 2 depends on Task 1, and both have subtasks,
    Task 2's subtasks can see the decisions made by Task 1's subtasks.

    Parameters
    ----------
    parent_task_id : str
        ID of the parent task
    subtask_manager : SubtaskManager
        Manager tracking all subtasks
    state : Any
        Marcus server state for accessing context.decisions
    """
    if not hasattr(state, "context") or not state.context:
        return

    # Query subtasks from unified storage
    subtasks = subtask_manager.get_subtasks(parent_task_id, state.project_tasks)
    if not subtasks:
        return

    # Collect decisions from all subtasks and re-log them for parent
    rollup_count = 0
    for subtask in subtasks:
        # Find decisions made during this subtask
        subtask_decisions = [
            d for d in state.context.decisions if d.task_id == subtask.id
        ]

        for decision in subtask_decisions:
            # Re-log the decision with parent task ID
            await state.context.log_decision(
                agent_id=decision.agent_id,
                task_id=parent_task_id,
                what=f"[From subtask: {subtask.name}] {decision.what}",
                why=decision.why,
                impact=decision.impact,
            )
            rollup_count += 1

    if rollup_count > 0:
        logger.info(
            f"Rolled up {rollup_count} decisions from {len(subtasks)} subtasks "
            f"to parent task {parent_task_id}"
        )


async def update_subtask_progress_in_parent(
    parent_task_id: str,
    subtask_id: str,
    subtask_manager: SubtaskManager,
    kanban_client: Any,
) -> None:
    """
    Update parent task progress based on subtask completion.

    Also checks off the corresponding checklist item in Planka.

    Parameters
    ----------
    parent_task_id : str
        ID of the parent task
    subtask_id : str
        ID of the completed subtask
    subtask_manager : SubtaskManager
        Manager tracking all subtasks
    kanban_client : Any
        Kanban client for updating task status
    """
    # Get the completed subtask
    subtask = subtask_manager.subtasks.get(subtask_id)
    if not subtask:
        logger.warning(f"Subtask {subtask_id} not found in manager")
        return

    # Mark checklist item as complete in Planka
    await _mark_checklist_item_complete(parent_task_id, subtask.name)

    # Calculate parent task progress (query from unified storage if available)
    # Note: This function is called from task.py where state might not be passed
    # For now, use legacy storage (None) but should be updated to pass project_tasks
    progress = subtask_manager.get_completion_percentage(parent_task_id, None)

    # Update parent task progress
    await kanban_client.update_task_progress(
        parent_task_id,
        {
            "progress": int(progress),
            "status": "in_progress",
            "message": f"Subtask completed: {subtask_id}",
        },
    )

    # Add progress comment (using legacy storage for now)
    subtasks = subtask_manager.get_subtasks(parent_task_id, None)
    completed = sum(1 for s in subtasks if s.status == TaskStatus.DONE)

    progress_comment = (
        f"📊 **Progress Update**: {completed}/{len(subtasks)} "
        f"subtasks completed ({progress:.0f}%)"
    )
    await kanban_client.add_comment(parent_task_id, progress_comment)


async def _mark_checklist_item_complete(parent_card_id: str, subtask_name: str) -> None:
    """
    Mark a checklist item (Planka task) as complete.

    Parameters
    ----------
    parent_card_id : str
        ID of the parent card in Planka
    subtask_name : str
        Name of the subtask to mark complete
    """
    try:
        import json

        # Use local path for kanban-mcp
        import os

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.types import TextContent

        kanban_mcp_path = os.path.expanduser("~/dev/kanban-mcp/dist/index.js")
        server_params = StdioServerParameters(
            command="node",
            args=[kanban_mcp_path],
            env=os.environ.copy(),
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Get all checklist items for the card
                result = await session.call_tool(
                    "mcp_kanban_task_manager",
                    {"action": "get_all", "cardId": parent_card_id},
                )

                if not result or not hasattr(result, "content") or not result.content:
                    logger.warning(
                        f"No checklist items found for card {parent_card_id}"
                    )
                    return

                content = result.content[0]
                if isinstance(content, TextContent):
                    tasks_text = str(content.text)
                    checklist_data = json.loads(tasks_text)
                    checklist_items = (
                        checklist_data
                        if isinstance(checklist_data, list)
                        else checklist_data.get("items", [])
                    )

                    # Find the matching checklist item
                    for item in checklist_items:
                        if item.get("name") == subtask_name:
                            task_id = item.get("id")
                            if task_id and not item.get("isCompleted"):
                                # Mark as complete
                                await session.call_tool(
                                    "mcp_kanban_task_manager",
                                    {
                                        "action": "update",
                                        "id": task_id,
                                        "isCompleted": True,
                                    },
                                )
                                logger.info(
                                    f"✅ Checked off checklist item '{subtask_name}' "
                                    f"on card {parent_card_id}"
                                )
                            break

    except Exception as e:
        logger.error(
            f"Error marking checklist item complete for '{subtask_name}': {e}",
            exc_info=True,
        )
