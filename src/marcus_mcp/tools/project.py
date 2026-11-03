"""Project Monitoring Tools for Marcus MCP.

This module contains tools for monitoring project progress and metrics:
- get_project_status: Get comprehensive project metrics and status, including
  the list of tasks that have not reached DONE and why, and the count of
  observed lease-epoch collisions (ADR-0012 D11, record-only)
"""

import logging
from typing import Any, Dict, List, Optional

from src.core.models import Task, TaskStatus
from src.logging.conversation_logger import conversation_logger
from src.marcus_mcp.utils import serialize_for_mcp

logger = logging.getLogger(__name__)


async def unfinished_reason(task: Task, kanban_client: Any = None) -> str:
    """Explain, in one line, why a task never reached DONE.

    The blocker text is not on the task row for every provider: Marcus's
    ``report_blocker`` sends ``update_task(task_id, {"blocker": text})`` and
    the SQLite provider inserts that into a separate ``blockers`` table,
    while task hydration reads only the tasks row. So the provider's blocker
    store is consulted when the row carries no text (Codex P1 on PR #723).

    Parameters
    ----------
    task : Task
        A task that is not DONE.
    kanban_client : Any, optional
        Provider, consulted for its blocker record when the task row carries
        no blocker text. Duck-typed: providers without ``get_task_blockers``
        fall through to the plain description.

    Returns
    -------
    str
        The recorded blocker text when one can be found, otherwise a plain
        description of the state the task was left in. Never empty.
    """
    blocker = (task.source_context or {}).get("blocker")
    if blocker:
        return str(blocker)
    if task.status == TaskStatus.BLOCKED and kanban_client is not None:
        getter = getattr(kanban_client, "get_task_blockers", None)
        if getter is not None:
            try:
                records = await getter(task.id)
                if records:
                    # The most recent blocker is the operative one.
                    latest = records[-1]
                    text = (
                        latest.get("description")
                        if isinstance(latest, dict)
                        else getattr(latest, "description", None)
                    )
                    if text:
                        return str(text)
            except Exception as e:  # noqa: BLE001 - never drop the entry
                logger.warning(
                    "unfinished_reason: blocker lookup failed for %s: %s", task.id, e
                )
    if task.status == TaskStatus.BLOCKED:
        return "blocked (no blocker text recorded)"
    if task.status == TaskStatus.IN_PROGRESS:
        return "still in progress when the run ended"
    return "never claimed by an agent"


async def list_unfinished_tasks(
    kanban_client: Any, expected_total: int
) -> Optional[List[Dict[str, Any]]]:
    """Return the tasks that are not DONE, or None when the read is untrustworthy.

    Providers swallow fetch errors and return ``[]`` rather than raising, so
    an unreachable board would otherwise publish "nothing unfinished". The
    list is published only when the fetch agrees with the metrics total.

    Parameters
    ----------
    kanban_client : Any
        The project's provider; needs ``get_all_tasks``.
    expected_total : int
        The task count the provider's metrics reported.

    Returns
    -------
    Optional[List[Dict[str, Any]]]
        ``{"id", "name", "status", "reason"}`` per unfinished task, or None
        when the provider cannot list tasks or the read is not corroborated.
    """
    getter = getattr(kanban_client, "get_all_tasks", None)
    if getter is None:
        return None
    try:
        all_tasks = await getter()
    except Exception as e:  # noqa: BLE001 - status must never fail on this
        logger.warning("get_project_status: unfinished-task fetch failed: %s", e)
        return None
    if not all_tasks or len(all_tasks) != expected_total:
        logger.warning(
            "get_project_status: task fetch returned %d task(s) but metrics "
            "report %d; omitting unfinished_tasks",
            len(all_tasks or []),
            expected_total,
        )
        return None
    return [
        {
            "id": t.id,
            "name": t.name,
            "status": t.status.value,
            "reason": await unfinished_reason(t, kanban_client),
        }
        for t in all_tasks
        if t.status != TaskStatus.DONE
    ]


async def get_project_status(state: Any) -> Any:
    """
    Get current project status and metrics.

    Provides comprehensive project overview including:
    - Task completion statistics
    - Worker availability metrics
    - Kanban provider information
    - Active project context (for agents)

    Parameters
    ----------
    state : Any
        Marcus server state instance

    Returns
    -------
    Dict[str, Any]
        Dict with project metrics and status
    """
    try:
        # Get active project context
        active_project = None
        if hasattr(state, "project_registry") and state.project_registry:
            active_project = await state.project_registry.get_active_project()

        # Get kanban client (either from project manager or legacy)
        if hasattr(state, "project_manager") and state.project_manager:
            kanban_client = await state.project_manager.get_kanban_client()
            if not kanban_client:
                return {
                    "success": False,
                    "error": (
                        "No active project. Use switch_project to " "select a project."
                    ),
                }
            state.kanban_client = kanban_client
        else:
            # Legacy mode - Initialize kanban if needed
            await state.initialize_kanban()

            # Double-check that initialization worked
            if not hasattr(state, "kanban_client") or state.kanban_client is None:
                return {
                    "success": False,
                    "error": (
                        "Failed to initialize kanban client. Check your "
                        "kanban configuration."
                    ),
                }

        # Refresh state - use the server's own refresh method
        try:
            await state.refresh_project_state()
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to refresh project state: {str(e)}",
            }

        if state.project_state:
            # Get task counts from kanban DB (source of truth).
            # This is scoped by project_id and reflects the actual
            # board state, including tasks created as done (About).
            kanban_metrics = None
            if (
                hasattr(state, "kanban_client")
                and state.kanban_client
                and hasattr(state.kanban_client, "get_project_metrics")
            ):
                try:
                    kanban_metrics = await state.kanban_client.get_project_metrics()
                except Exception as e:
                    logger.warning(
                        f"Failed to get kanban metrics, "
                        f"falling back to in-memory: {e}"
                    )

            # Validate all required fields before trusting metrics.
            # Some providers (e.g. Linear) return incomplete data.
            _required = {
                "total_tasks",
                "completed_tasks",
                "in_progress_tasks",
                "blocked_tasks",
            }
            if kanban_metrics and _required.issubset(kanban_metrics):
                total_tasks = kanban_metrics["total_tasks"]
                completed = kanban_metrics["completed_tasks"]
                in_progress = kanban_metrics["in_progress_tasks"]
                blocked = kanban_metrics["blocked_tasks"]
            else:
                if kanban_metrics:
                    logger.warning(
                        "Kanban metrics incomplete "
                        f"(missing {_required - set(kanban_metrics)}), "
                        "falling back to in-memory"
                    )
                # Fallback to in-memory state
                total_tasks = len(state.project_tasks)
                completed = len(
                    [t for t in state.project_tasks if t.status == TaskStatus.DONE]
                )
                in_progress = len(
                    [
                        t
                        for t in state.project_tasks
                        if t.status == TaskStatus.IN_PROGRESS
                    ]
                )
                blocked = len(
                    [t for t in state.project_tasks if t.status == TaskStatus.BLOCKED]
                )

            # Worker metrics - create snapshot to avoid dictionary
            # mutation during iteration
            active_workers = len(
                [
                    w
                    for w in list(state.agent_status.values())
                    if len(w.current_tasks) > 0
                ]
            )

            # D11 record-only collision counting (ADR-0012): how often a
            # completion arrived under a superseded epoch during this
            # project. With D3's liveness dials this should be rare, and the
            # number is what sizes the fence in Milestone B, so it is
            # reported here rather than left in the log.
            from src.marcus_mcp.tools.task import count_epoch_collisions

            epoch_collisions = await count_epoch_collisions(
                state, active_project.id if active_project else None
            )

            unfinished = None
            if hasattr(state, "kanban_client") and state.kanban_client:
                unfinished = await list_unfinished_tasks(
                    state.kanban_client, int(total_tasks or 0)
                )

            response: Dict[str, Any] = {
                "success": True,
                "project": {
                    "id": active_project.id if active_project else None,
                    "name": (
                        active_project.name
                        if active_project
                        else state.project_state.project_name
                    ),
                    "provider": (
                        active_project.provider if active_project else state.provider
                    ),
                    "total_tasks": total_tasks,
                    "completed": completed,
                    "in_progress": in_progress,
                    "blocked": blocked,
                    "completion_percentage": (
                        (completed / total_tasks * 100) if total_tasks > 0 else 0
                    ),
                    "epoch_collisions": epoch_collisions,
                },
                "workers": {
                    "total": len(state.agent_status),
                    "active": active_workers,
                    "available": len(state.agent_status) - active_workers,
                },
            }
            if unfinished is not None:
                response["project"]["unfinished_tasks"] = unfinished
            return serialize_for_mcp(response)
        else:
            return {
                "success": False,
                "error": (
                    "No project state available. This might mean no "
                    "tasks exist on the kanban board or the board is "
                    "not accessible."
                ),
            }

    except Exception as e:
        return {"success": False, "error": str(e)}


# Helper function
async def refresh_project_state(state: Any) -> None:
    """
    Refresh the current project state from kanban.

    Updates the internal project state with latest data from
    the connected kanban provider.

    Parameters
    ----------
    state : Any
        Marcus server state instance
    """
    try:
        # Log refresh attempt
        state.log_event(
            "refresh_project_state_start",
            {"kanban_client_exists": state.kanban_client is not None},
        )

        tasks = await state.kanban_client.get_available_tasks()

        # Log tasks retrieved
        state.log_event(
            "refresh_project_state_tasks",
            {"task_count": len(tasks), "task_names": [t.name for t in tasks]},
        )

        # Store tasks separately
        state.project_tasks = tasks

        # Calculate task statistics
        completed = len([t for t in tasks if t.status == TaskStatus.DONE])
        in_progress = len([t for t in tasks if t.status == TaskStatus.IN_PROGRESS])
        todo = len([t for t in tasks if t.status == TaskStatus.TODO])
        blocked = len([t for t in tasks if t.status == TaskStatus.BLOCKED])

        # Get project state from monitor
        if state.monitor:
            state.project_state = await state.monitor.get_project_state()

        # Log state update
        state.log_event(
            "refresh_project_state_complete",
            {
                "total_tasks": len(tasks),
                "completed": completed,
                "in_progress": in_progress,
                "todo": todo,
                "blocked": blocked,
            },
        )

    except Exception as e:
        # Log error using log_pm_thinking instead
        conversation_logger.log_pm_thinking(f"Failed to refresh project state: {e}")
        state.log_event("refresh_project_state_error", {"error": str(e)})
        raise
