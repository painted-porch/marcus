"""MCP Tools Package.

This package contains all MCP tool implementations organized by domain:
- agent: Agent management (register, status, list)
- task: Task operations (request, progress, blockers)
- attachment: Design artifact sharing (upload, download, list)
- project: Project monitoring
- system: System health and diagnostics
- nlp: Natural language processing tools
"""

from .agent import get_agent_status, list_registered_agents, register_agent
from .attachment import log_artifact
from .context import get_task_context, log_decision
from .nlp import add_feature, create_project
from .project import get_project_status
from .system import check_assignment_health, ping
from .task import (
    report_blocker,
    report_task_progress,
    request_next_task,
    request_task_redo,
)

__all__ = [
    # Agent tools
    "register_agent",
    "get_agent_status",
    "list_registered_agents",
    # Task tools
    "request_next_task",
    "report_task_progress",
    "report_blocker",
    "request_task_redo",
    # Artifact tools
    "log_artifact",
    # Context tools
    "get_task_context",
    "log_decision",
    # Project tools
    "get_project_status",
    # System tools
    "ping",
    "check_assignment_health",
    # NLP tools
    "create_project",
    "add_feature",
]
