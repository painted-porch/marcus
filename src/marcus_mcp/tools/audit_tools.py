"""
Audit and usage analytics tools for Marcus.

Provides tools for analyzing audit logs, generating usage reports,
and exporting a case's complete audit bundle (issue #737): every
task with its principal and timestamps, the evidence payloads,
the approvals, the eligibility refusals, identity changes, and the
cost rows. The bundle is a query over what the board already holds.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from mcp.types import Tool

from src.core.models import Task, TaskStatus, WorkerStatus

from ..audit import get_audit_logger


async def get_usage_report(
    days: int = 7,
    state: Any = None,
) -> Dict[str, Any]:
    """
    Generate a usage report from audit logs.

    Parameters
    ----------
    days : int
        Number of days to include in report (default: 7)
    state : Any
        Marcus server state

    Returns
    -------
    Dict[str, Any]
        Usage statistics and insights
    """
    audit_logger = get_audit_logger()

    # Calculate date range
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days)

    # Get usage statistics
    stats = await audit_logger.get_usage_stats(start_date, end_date)

    # Format report
    report = {
        "period": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "days": days,
        },
        "summary": {
            "total_events": stats["total_events"],
            "unique_clients": stats["unique_clients"],
            "errors": stats["errors"],
            "error_rate": (
                f"{(stats['errors'] / stats['total_events'] * 100):.1f}%"
                if stats["total_events"] > 0
                else "0%"
            ),
        },
        "by_client_type": stats["by_client_type"],
        "by_tool": stats["by_tool"],
        "by_event_type": stats["by_event_type"],
    }

    # Add insights
    insights = []

    # Most active client type
    if stats["by_client_type"]:
        most_active = max(stats["by_client_type"].items(), key=lambda x: x[1])
        insights.append(
            f"Most active client type: {most_active[0]} ({most_active[1]} events)"
        )

    # Most used tool
    if stats["by_tool"]:
        most_used = max(stats["by_tool"].items(), key=lambda x: x[1])
        insights.append(f"Most used tool: {most_used[0]} ({most_used[1]} calls)")

    # Error insights
    if stats["errors"] > 10:
        insights.append(f"High error count detected: {stats['errors']} errors")

    report["insights"] = insights

    return report


def _principal_of(agent_id: Optional[str], state: Any) -> Dict[str, str]:
    """Resolve who an agent id is on the record.

    Parameters
    ----------
    agent_id : Optional[str]
        The agent id from a task's ``assigned_to``.
    state : Any
        Marcus server state; ``agent_status`` holds registrations.

    Returns
    -------
    Dict[str, str]
        agent_id, principal, vendor; "unknown" when unregistered,
        which the bundle shows rather than hides.
    """
    worker = (getattr(state, "agent_status", None) or {}).get(agent_id or "")
    if isinstance(worker, WorkerStatus):
        return {
            "agent_id": agent_id or "",
            "principal": worker.principal,
            "vendor": worker.vendor,
        }
    return {"agent_id": agent_id or "", "principal": "unknown", "vendor": ""}


def _case_tasks(case_id: str, state: Any) -> List[Task]:
    """Find the case's tasks by their ``case:<id>`` label."""
    label = f"case:{case_id}"
    return [
        t
        for t in (getattr(state, "project_tasks", None) or [])
        if label in (t.labels or [])
    ]


async def export_case(case_id: str, state: Any) -> Dict[str, Any]:
    """Export one case's complete audit bundle (issue #737, step 8).

    The bundle is the third party's view of the case: who did what,
    what evidence came back (``raw`` included, this is the auditors'
    copy), who was refused and why, who approved, what it cost. It
    is assembled from live board state, so it runs against the
    server that worked the case; the refusal log and registrations
    are in-memory, which the pilot report states as a limit.

    Parameters
    ----------
    case_id : str
        The case to export (e.g. ``tow-0001``).
    state : Any
        Marcus server state instance.

    Returns
    -------
    Dict[str, Any]
        The bundle: case_id, summary, tasks, approvals, refusals,
        identity_changes, costs.
    """
    tasks = sorted(_case_tasks(case_id, state), key=lambda t: t.created_at)

    task_entries: List[Dict[str, Any]] = []
    approvals: List[Dict[str, Any]] = []
    answer: Optional[Dict[str, Any]] = None
    for task in tasks:
        evidence = (task.source_context or {}).get("evidence") or {}
        worked_by = _principal_of(task.assigned_to, state)
        task_entries.append(
            {
                "task_id": task.id,
                "name": task.name,
                "task_type": task.task_type,
                "status": task.status.value,
                "created_at": task.created_at.isoformat(),
                "updated_at": task.updated_at.isoformat(),
                "worked_by": worked_by,
                "dependencies": list(task.dependencies or []),
                "inputs": dict(task.inputs or {}),
                "output_schema": task.output_schema,
                "evidence": evidence,
            }
        )
        if task.task_type == "approve" and evidence:
            approvals.append(
                {
                    "task_id": task.id,
                    "decision": evidence.get("decision"),
                    "approved_by": evidence.get("approved_by"),
                    "decided_at": evidence.get("decided_at"),
                    "principal": worked_by["principal"],
                }
            )
        if task.task_type == "synthesize" and evidence.get("answer"):
            answer = evidence["answer"]

    task_ids = {t.id for t in tasks}
    involved_agents = {t.assigned_to for t in tasks if t.assigned_to}

    refusals = [
        r
        for r in (getattr(state, "eligibility_refusals", None) or [])
        if r.get("task_id") in task_ids
    ]
    identity_changes = [
        c
        for c in (getattr(state, "identity_changes", None) or [])
        if c.get("agent_id") in involved_agents
    ]

    costs: List[Dict[str, Any]] = []
    cost_store = getattr(state, "cost_store", None)
    if cost_store is not None and task_ids:
        from src.cost_tracking.worker_usage_ingester import events_for_tasks

        costs = events_for_tasks(cost_store, sorted(task_ids))

    if not tasks:
        summary: Dict[str, Any] = {"status": "not_found"}
    else:
        all_done = all(t.status == TaskStatus.DONE for t in tasks)
        summary = {
            "status": "closed" if all_done else "open",
            "opened_at": tasks[0].created_at.isoformat(),
            "closed_at": max(t.updated_at for t in tasks).isoformat(),
            "answer": answer,
        }

    return {
        "case_id": case_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "tasks": task_entries,
        "approvals": approvals,
        "refusals": refusals,
        "identity_changes": identity_changes,
        "costs": costs,
    }


EXPORT_CASE_TOOL = Tool(
    name="export_case",
    description=(
        "Export one case's complete audit bundle: tasks with "
        "principals and timestamps, evidence (raw included), "
        "approvals, eligibility refusals, identity changes, and "
        "cost rows (#737)"
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "case_id": {
                "type": "string",
                "description": "The case to export (e.g. tow-0001)",
            },
        },
        "required": ["case_id"],
    },
)


# Tool definition
USAGE_REPORT_TOOL = Tool(
    name="get_usage_report",
    description="Generate usage statistics and insights from audit logs",
    inputSchema={
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Number of days to include in report (default: 7)",
                "minimum": 1,
                "maximum": 365,
                "default": 7,
            },
        },
    },
)
