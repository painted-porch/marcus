"""
MCP Tool Registration and Handlers.

This module provides the tool registration and handling functionality
for the Marcus MCP server, organizing all tool definitions and handlers
in a centralized location.
"""

import json
import logging
import time
from contextlib import ExitStack
from typing import Any, Dict, List, Optional

import mcp.types as types

from src.cost_tracking.cost_recorder import PlannerContext, get_recorder
from src.logging.mcp_tool_logger import log_mcp_tool_response

from .audit import get_audit_logger
from .tools import (  # Agent tools; Task tools; Project tools; System tools; NLP tools
    add_feature,
    check_assignment_health,
    create_project,
    get_agent_status,
    get_project_status,
    get_task_context,
    list_registered_agents,
    log_artifact,
    ping,
    register_agent,
    report_blocker,
    report_task_progress,
    request_next_task,
    request_task_redo,
)
from .tools.analytics import (  # Analytics tools
    get_agent_metrics,
    get_project_metrics,
    get_system_metrics,
    get_task_metrics,
)
from .tools.audit_tools import (
    USAGE_REPORT_TOOL,
    get_usage_report,
)
from .tools.auth import (
    AUTHENTICATE_TOOL,
    authenticate,
    get_client_tools,
)
from .tools.board_health import (  # Board health tools
    check_board_health,
    check_task_dependencies,
)
from .tools.code_metrics import (  # Code metrics tools
    get_code_metrics,
    get_code_quality_metrics,
    get_code_review_metrics,
    get_repository_metrics,
)
from .tools.context import (  # Context tools
    log_decision,
)
from .tools.cost_tracking import (
    COST_SUMMARY_TOOL,
    get_cost_summary,
)

# Pattern learning tools disabled - only accessible via visualization UI API
# from .tools.pattern_learning import (  # Pattern learning tools
#     assess_project_quality,
#     get_pattern_recommendations,
#     get_project_patterns,
#     get_quality_trends,
#     get_similar_projects,
#     learn_from_completed_project,
# )
from .tools.predictions import (  # Prediction tools
    get_task_assignment_score,
    predict_blockage_probability,
    predict_cascade_effects,
    predict_completion_time,
    predict_task_outcome,
)
from .tools.project_management import (  # Project management tools
    add_project,
    get_current_project,
    list_projects,
    remove_project,
    switch_project,
    update_project,
)
from .tools.scheduling import (  # Scheduling tools
    get_optimal_agent_count,
)

logger = logging.getLogger(__name__)


def get_all_tool_definitions() -> Dict[str, types.Tool]:
    """
    Get all tool definitions as a mapping.

    Returns
    -------
        Dict mapping tool name to Tool definition
    """
    # Build complete tool map
    all_tools = {}

    # Get all tools from both agent and human definitions
    for tool in get_tool_definitions("agent"):
        all_tools[tool.name] = tool
    for tool in get_tool_definitions("human"):
        all_tools[tool.name] = tool

    # Add auth and audit tools
    all_tools["authenticate"] = AUTHENTICATE_TOOL
    all_tools["get_usage_report"] = USAGE_REPORT_TOOL
    all_tools["get_cost_summary"] = COST_SUMMARY_TOOL

    return all_tools


def get_all_tool_names() -> List[str]:
    """Get list of all available tool names."""
    return list(get_all_tool_definitions().keys())


def get_tool_definitions(role: str = "agent") -> List[types.Tool]:
    """
    Return list of available tool definitions for MCP based on role.

    Args:
        role: User role - "agent" for coding agents, "human" for full access

    Returns
    -------
        List of Tool objects with schemas for Marcus tools based on role
    """
    # Core agent tools available to all coding agents
    agent_tools = [
        # Agent Management Tools
        types.Tool(
            name="register_agent",
            description="Register a new agent with the Marcus system",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Unique agent identifier",
                    },
                    "name": {"type": "string", "description": "Agent's display name"},
                    "role": {
                        "type": "string",
                        "description": "Agent's role (e.g., 'Backend Developer')",
                    },
                    "skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of agent's skills",
                        "default": [],
                    },
                    "project_id": {
                        "type": "string",
                        "description": (
                            "Project this agent is working on (required). "
                            "Scopes task assignment to prevent cross-experiment "
                            "task theft (GH-388)."
                        ),
                    },
                },
                "required": ["agent_id", "name", "role", "project_id"],
            },
        ),
        types.Tool(
            name="get_agent_status",
            description="Get status and current assignment for an agent",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent to check status for",
                    }
                },
                "required": ["agent_id"],
            },
        ),
        # Task Management Tools
        types.Tool(
            name="request_next_task",
            description="Request the next optimal task assignment for an agent",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent requesting the task",
                    }
                },
                "required": ["agent_id"],
            },
        ),
        types.Tool(
            name="report_task_progress",
            description=(
                "Report progress on a task. For integration verification "
                "tasks, you MUST declare start_command when marking the "
                "task complete — Marcus runs the declared command as a "
                "subprocess and rejects the completion if it fails."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent reporting progress",
                    },
                    "task_id": {"type": "string", "description": "Task being updated"},
                    "status": {
                        "type": "string",
                        "description": "Task status: in_progress, completed, blocked",
                    },
                    "progress": {
                        "type": "integer",
                        "description": "Progress percentage (0-100)",
                        "default": 0,
                    },
                    "message": {
                        "type": "string",
                        "description": "Progress message",
                        "default": "",
                    },
                    "start_command": {
                        "type": "string",
                        "description": (
                            "Shell command Marcus runs to verify the "
                            "deliverable starts. REQUIRED for integration "
                            "verification tasks (type:integration label) "
                            "when status='completed'; ignored on other "
                            "tasks. Examples: 'npm run build', "
                            "'python -m mypackage --help', "
                            "'tsc --noEmit', 'uvicorn main:app --port 8000'."
                        ),
                    },
                    "readiness_probe": {
                        "type": "string",
                        "description": (
                            "Optional shell command Marcus polls to detect "
                            "when a long-running server is ready. Pair with "
                            "start_command for servers. When provided, "
                            "Marcus starts the command in the background, "
                            "polls this probe every 1s for up to 15s, and "
                            "passes when the probe returns exit 0. Example: "
                            "'curl -f http://localhost:8000/health'."
                        ),
                    },
                    "lease_epoch": {
                        "type": "integer",
                        "description": (
                            "The lease_epoch returned by request_next_task "
                            "for THIS task. Pass it unchanged on every "
                            "report. It proves you still hold the task: if "
                            "Marcus concluded you had died and reassigned "
                            "the task, your epoch is superseded and the "
                            "report is preserved for reconciliation rather "
                            "than applied as a completion. Omit only if you "
                            "were not given one."
                        ),
                    },
                },
                "required": ["agent_id", "task_id", "status"],
            },
        ),
        types.Tool(
            name="report_blocker",
            description="Report a blocker on a task",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent reporting the blocker",
                    },
                    "task_id": {"type": "string", "description": "Blocked task ID"},
                    "blocker_description": {
                        "type": "string",
                        "description": "Description of the blocker",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": (
                            "Blocker severity, which decides what happens to "
                            "the task (issue #719). 'low'/'medium' = ADVISORY: "
                            "you get suggestions and KEEP the task (it stays "
                            "in progress and assigned to you) — this is the "
                            "normal way to ask for help. 'high' = TERMINAL: "
                            "you hand the task back, it is marked BLOCKED and "
                            "nobody else picks it up — use only when the task "
                            "genuinely cannot be completed by anyone."
                        ),
                        "default": "medium",
                    },
                },
                "required": ["agent_id", "task_id", "blocker_description"],
            },
        ),
        types.Tool(
            name="request_task_redo",
            description=(
                "Send a completed (DONE) task back to the board for a fresh "
                "agent to redo. Use when you find another agent's completed "
                "work is substantively wrong (wrong response shape, missing "
                "behavior, logic bug) — instead of rewriting their code "
                "yourself. Marcus resets the task to TODO with your "
                "diagnostic; the next agent claims it normally. Capped at 3 "
                "redos per task, after which fix in place."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Your agent ID (the redo requester)",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "ID of the DONE task to send back",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Diagnostic for the next agent: what is wrong "
                            "and how you observed it (e.g. the contract "
                            "clause the output violates)"
                        ),
                    },
                },
                "required": ["agent_id", "task_id", "reason"],
            },
        ),
        # Project Monitoring Tools
        types.Tool(
            name="get_project_status",
            description="Get current project status and metrics",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        # System Health Tools
        types.Tool(
            name="ping",
            description=(
                "Check Marcus status and connectivity with special "
                "diagnostic commands.\n\n"
                "Special commands:\n"
                "- 'health': Get detailed system health including lease "
                "statistics and assignment state\n"
                "- 'cleanup': Force cleanup of stuck task assignments "
                "(safe recovery operation)\n"
                "- 'reset': Clear ALL assignment state - WARNING: "
                "use only when system is stuck!\n\n"
                "Examples:\n"
                '- ping("hello") - Simple connectivity check\n'
                '- ping("health") - Full system health report\n'
                '- ping("cleanup") - Clean stuck assignments after '
                "interruption\n"
                '- ping("reset") - Complete assignment reset'
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "echo": {
                        "type": "string",
                        "description": (
                            "Message to echo or command: " "'health'|'cleanup'|'reset'"
                        ),
                        "default": "",
                    }
                },
                "required": [],
            },
        ),
        # Context Tools (for agents to log decisions)
        types.Tool(
            name="log_decision",
            description=(
                "Log an architectural decision that might affect " "other tasks"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent making the decision",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Current task ID",
                    },
                    "decision": {
                        "type": "string",
                        "description": (
                            "Decision description. Format: "
                            "'I chose X because Y. This affects Z.'"
                        ),
                    },
                },
                "required": ["agent_id", "task_id", "decision"],
            },
        ),
        types.Tool(
            name="get_task_context",
            description=(
                "Get the full context for a specific task including "
                "dependencies, decisions, and artifacts stored in "
                "the repository"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID to get context for",
                    },
                    "project_root": {
                        "type": "string",
                        "description": (
                            "Absolute path to the project root "
                            "directory. "
                            "Used to discover artifacts created in "
                            "the project workspace. "
                            "All agents working on this project "
                            "should pass the same path "
                            "to see each other's artifacts."
                        ),
                    },
                },
                "required": ["task_id"],
            },
        ),
        types.Tool(
            name="log_artifact",
            description=(
                "Store an artifact with smart location management. "
                "Artifacts are automatically stored in organized "
                "directories based on type "
                "(e.g., API specs → docs/api/, designs → docs/design/). "
                "You can optionally override the location for "
                "special cases."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The current task ID"},
                    "filename": {
                        "type": "string",
                        "description": "Name for the artifact file",
                    },
                    "content": {
                        "type": "string",
                        "description": "The artifact content to store",
                    },
                    "artifact_type": {
                        "type": "string",
                        "description": "Type of artifact",
                        "enum": [
                            "specification",
                            "api",
                            "design",
                            "architecture",
                            "documentation",
                            "reference",
                            "temporary",
                        ],
                    },
                    "project_root": {
                        "type": "string",
                        "description": (
                            "Absolute path to the project root "
                            "directory where artifacts should be "
                            "created. "
                            "All agents working on this project "
                            "should pass the same path. "
                            "Artifacts will be created relative to "
                            "this path based on their type "
                            "(e.g., an 'api' artifact will go in "
                            "{project_root}/docs/api/). "
                            "Typically this is os.getcwd() when the "
                            "agent is running from the project root."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description of the artifact",
                        "default": "",
                    },
                    "location": {
                        "type": "string",
                        "description": (
                            "Optional override for storage location " "(relative path)"
                        ),
                        "default": None,
                    },
                },
                "required": [
                    "task_id",
                    "filename",
                    "content",
                    "artifact_type",
                    "project_root",
                ],
            },
        ),
        # Natural Language Tools (also available to agents)
        types.Tool(
            name="create_project",
            description=(
                "Create a complete project from natural language "
                "description. "
                "Automatically generates tasks, assigns priorities, "
                "and creates "
                "kanban board structure based on project complexity "
                "and deployment needs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": (
                            "Natural language description of what "
                            "you want to build. "
                            "Be specific about features and "
                            "functionality. "
                            "Example: 'Create a todo app with user "
                            "authentication, "
                            "task categories, and email reminders'"
                        ),
                    },
                    "project_name": {
                        "type": "string",
                        "description": (
                            "A short, memorable name for your "
                            "project. "
                            "This will be used as the kanban board "
                            "title. "
                            "Example: 'TodoMaster' or "
                            "'Task Tracker Pro'"
                        ),
                    },
                    "options": {
                        "type": "object",
                        "description": (
                            "Optional configuration to control "
                            "project scope and complexity. "
                            "All fields are optional - sensible "
                            "defaults will be used."
                        ),
                        "properties": {
                            "complexity": {
                                "type": "string",
                                "description": (
                                    "Project complexity level "
                                    "(default: 'standard'). "
                                    "- 'prototype': Quick MVP with "
                                    "minimal features (3-8 tasks) "
                                    "- 'standard': Full-featured "
                                    "project (10-20 tasks) "
                                    "- 'enterprise': Production-ready "
                                    "with all features (25+ tasks)"
                                ),
                                "enum": ["prototype", "standard", "enterprise"],
                                "default": "standard",
                            },
                            "deployment": {
                                "type": "string",
                                "description": (
                                    "Deployment scope "
                                    "(default: 'none'). "
                                    "- 'none': Local development only, "
                                    "no deployment tasks "
                                    "- 'internal': Include staging/team "
                                    "deployment tasks "
                                    "- 'production': Full production "
                                    "deployment with monitoring"
                                ),
                                "enum": ["none", "internal", "production"],
                                "default": "none",
                            },
                            "team_size": {
                                "type": "integer",
                                "description": (
                                    "Number of developers (1-20). "
                                    "Defaults based on complexity: "
                                    "prototype=1, standard=3, "
                                    "enterprise=5. "
                                    "Affects task parallelization "
                                    "and estimates."
                                ),
                                "minimum": 1,
                                "maximum": 20,
                            },
                            "tech_stack": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Technologies/frameworks to use. "
                                    "Example: ['Python', 'React', "
                                    "'PostgreSQL', 'Docker']. "
                                    "Helps generate appropriate setup "
                                    "and configuration tasks."
                                ),
                            },
                            "deadline": {
                                "type": "string",
                                "format": "date",
                                "description": (
                                    "Project deadline in ISO format "
                                    "(YYYY-MM-DD). "
                                    "Example: '2024-12-31'. "
                                    "Used to assess timeline risks and "
                                    "adjust priorities."
                                ),
                            },
                        },
                    },
                },
                "required": ["description", "project_name"],
            },
        ),
        # Scheduling and Planning Tools (also available to agents)
        types.Tool(
            name="get_optimal_agent_count",
            description=(
                "Calculate optimal number of agents using Critical Path "
                "Method (CPM) analysis.\n\n"
                "Analyzes the unified dependency graph (including parent "
                "tasks and subtasks) to determine the optimal agent count "
                "for maximum efficiency.\n\n"
                "Returns:\n"
                "- optimal_agents: Recommended number of agents\n"
                "- critical_path_hours: Duration of longest dependency "
                "chain\n"
                "- max_parallelism: Maximum tasks that can run "
                "simultaneously\n"
                "- efficiency_gain: Percentage improvement vs single agent\n"
                "- estimated_completion_hours: Expected completion time\n\n"
                "Optionally includes detailed parallel opportunities showing "
                "when multiple tasks can be worked on simultaneously."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_details": {
                        "type": "boolean",
                        "description": (
                            "Include detailed parallel opportunities "
                            "analysis (shows time points where multiple "
                            "tasks can run in parallel)"
                        ),
                        "default": False,
                    },
                },
                "required": [],
            },
        ),
    ]

    # If role is "agent", return only agent tools
    if role == "agent":
        return agent_tools

    # For "human" role, include all tools including pipeline enhancements
    human_tools = agent_tools + [
        # Administrative Tools (human only)
        types.Tool(
            name="list_registered_agents",
            description="List all registered agents",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="check_assignment_health",
            description="Check the health of the assignment tracking system",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="check_board_health",
            description=(
                "Analyze overall board health and detect systemic "
                "issues.\n\n"
                "Detects:\n"
                "- Skill mismatches: Tasks no agent can handle\n"
                "- Circular dependencies: Task cycles that block "
                "progress\n"
                "- Bottlenecks: Tasks blocking many others\n"
                "- Chain blocks: Long sequential dependency chains\n"
                "- Stale tasks: In-progress tasks not updated "
                "recently\n"
                "- Workload issues: Overloaded or idle agents\n\n"
                "Returns health score (0-100) with detailed issue "
                "analysis and recommendations.\n\n"
                "Usage: check_board_health()"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="check_task_dependencies",
            description=(
                "Check dependencies for a specific task and analyze "
                "its position in the workflow.\n\n"
                "Shows:\n"
                "- What this task depends on (upstream dependencies)\n"
                "- What depends on this task (downstream impact)\n"
                "- Whether task is part of circular dependencies\n"
                "- If task is a bottleneck (blocking 3+ tasks)\n"
                "- Recommended completion order\n\n"
                "Helps identify critical path tasks and dependency "
                "issues.\n\n"
                'Usage: check_task_dependencies("task-123")'
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "ID of the task to analyze (e.g., 'task-123')",
                    },
                },
                "required": ["task_id"],
            },
        ),
        # Project Management Tools (human only)
        types.Tool(
            name="list_projects",
            description="List all available projects",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter projects by tags",
                    },
                    "provider": {
                        "type": "string",
                        "description": "Filter by provider (planka, linear, github)",
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="switch_project",
            description="Switch to a different project",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "ID of project to switch to",
                    },
                    "project_name": {
                        "type": "string",
                        "description": "Name of project (alternative to ID)",
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_current_project",
            description="Get the currently active project",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="add_project",
            description="Add a new project configuration",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Project name"},
                    "provider": {
                        "type": "string",
                        "description": "Provider type (planka, linear, github)",
                        "enum": ["planka", "linear", "github"],
                    },
                    "config": {
                        "type": "object",
                        "description": "Provider-specific configuration",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Project tags",
                        "default": [],
                    },
                    "make_active": {
                        "type": "boolean",
                        "description": "Switch to this project after creation",
                        "default": True,
                    },
                },
                "required": ["name", "provider"],
            },
        ),
        types.Tool(
            name="remove_project",
            description="Remove a project from the registry",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "ID of project to remove",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "Confirm deletion",
                        "default": False,
                    },
                },
                "required": ["project_id"],
            },
        ),
        types.Tool(
            name="update_project",
            description="Update project configuration",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "ID of project to update",
                    },
                    "name": {"type": "string", "description": "New project name"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New project tags",
                    },
                    "config": {
                        "type": "object",
                        "description": "Updated provider configuration",
                    },
                },
                "required": ["project_id"],
            },
        ),
        # Natural Language Tools (also available to humans)
        types.Tool(
            name="create_project",
            description=(
                "Create a complete project from natural language "
                "description. "
                "Automatically generates tasks, assigns priorities, "
                "and creates "
                "kanban board structure based on project complexity "
                "and deployment needs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": (
                            "Natural language description of what "
                            "you want to build. "
                            "Be specific about features and "
                            "functionality. "
                            "Example: 'Create a todo app with user "
                            "authentication, "
                            "task categories, and email reminders'"
                        ),
                    },
                    "project_name": {
                        "type": "string",
                        "description": (
                            "A short, memorable name for your "
                            "project. "
                            "This will be used as the kanban board "
                            "title. "
                            "Example: 'TodoMaster' or "
                            "'Task Tracker Pro'"
                        ),
                    },
                    "options": {
                        "type": "object",
                        "description": (
                            "Optional configuration to control "
                            "project scope and complexity. "
                            "All fields are optional - sensible "
                            "defaults will be used."
                        ),
                        "properties": {
                            "complexity": {
                                "type": "string",
                                "description": (
                                    "Project complexity level "
                                    "(default: 'standard'). "
                                    "- 'prototype': Quick MVP with "
                                    "minimal features (3-8 tasks) "
                                    "- 'standard': Full-featured "
                                    "project (10-20 tasks) "
                                    "- 'enterprise': Production-ready "
                                    "with all features (25+ tasks)"
                                ),
                                "enum": ["prototype", "standard", "enterprise"],
                                "default": "standard",
                            },
                            "deployment": {
                                "type": "string",
                                "description": (
                                    "Deployment scope "
                                    "(default: 'none'). "
                                    "- 'none': Local development only, "
                                    "no deployment tasks "
                                    "- 'internal': Include staging/team "
                                    "deployment tasks "
                                    "- 'production': Full production "
                                    "deployment with monitoring"
                                ),
                                "enum": ["none", "internal", "production"],
                                "default": "none",
                            },
                            "team_size": {
                                "type": "integer",
                                "description": (
                                    "Number of developers (1-20). "
                                    "Defaults based on complexity: "
                                    "prototype=1, standard=3, "
                                    "enterprise=5. "
                                    "Affects task parallelization "
                                    "and estimates."
                                ),
                                "minimum": 1,
                                "maximum": 20,
                            },
                            "tech_stack": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Technologies/frameworks to use. "
                                    "Example: ['Python', 'React', "
                                    "'PostgreSQL', 'Docker']. "
                                    "Helps generate appropriate setup "
                                    "and configuration tasks."
                                ),
                            },
                            "deadline": {
                                "type": "string",
                                "format": "date",
                                "description": (
                                    "Project deadline in ISO format "
                                    "(YYYY-MM-DD). "
                                    "Example: '2024-12-31'. "
                                    "Used to assess timeline risks and "
                                    "adjust priorities."
                                ),
                            },
                        },
                    },
                },
                "required": ["description", "project_name"],
            },
        ),
        types.Tool(
            name="add_feature",
            description="Add a feature to existing project using natural language",
            inputSchema={
                "type": "object",
                "properties": {
                    "feature_description": {
                        "type": "string",
                        "description": (
                            "Natural language description of the feature to add"
                        ),
                    },
                    "integration_point": {
                        "type": "string",
                        "description": "How to integrate the feature",
                        "enum": [
                            "auto_detect",
                            "after_current",
                            "parallel",
                            "new_phase",
                        ],
                        "default": "auto_detect",
                    },
                },
                "required": ["feature_description"],
            },
        ),
        # Scheduling and Planning Tools
        types.Tool(
            name="get_optimal_agent_count",
            description=(
                "Calculate optimal number of agents using Critical Path "
                "Method (CPM) analysis.\n\n"
                "Analyzes the unified dependency graph (including parent "
                "tasks and subtasks) to determine the optimal agent count "
                "for maximum efficiency.\n\n"
                "Returns:\n"
                "- optimal_agents: Recommended number of agents\n"
                "- critical_path_hours: Duration of longest dependency "
                "chain\n"
                "- max_parallelism: Maximum tasks that can run "
                "simultaneously\n"
                "- efficiency_gain: Percentage improvement vs single agent\n"
                "- estimated_completion_hours: Expected completion time\n\n"
                "Optionally includes detailed parallel opportunities showing "
                "when multiple tasks can be worked on simultaneously."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_details": {
                        "type": "boolean",
                        "description": (
                            "Include detailed parallel opportunities "
                            "analysis (shows time points where multiple "
                            "tasks can run in parallel)"
                        ),
                        "default": False,
                    },
                },
                "required": [],
            },
        ),
        # Pattern Learning Tools removed - only accessible via visualization UI API
        # Audit and analytics tools
        USAGE_REPORT_TOOL,
        # Cost tracking dashboard (#409)
        COST_SUMMARY_TOOL,
    ]

    return human_tools


# Tools that create or switch projects must NOT inherit
# ``state.selected_project_id`` as a fallback — their LLM work
# (notably create_project's heavy decomposition pass) belongs to the
# new/target project, not the one that happened to be active when the
# request arrived. Codex P1 on PR #503 caught the original miss:
# without this guard, decomposition cost was attributed to the
# previously-active project, silently corrupting that project's totals
# on every subsequent ``create_project`` call.
#
# We still honor an explicit ``project_id`` arg for these tools (which
# is how ``add_project`` / ``switch_project`` / ``update_project``
# identify their target). ``create_project`` has no project_id at
# request time, so its events land in the ``'unassigned'`` bucket —
# visible in the dashboard rather than silently mis-attributed.
_PROJECT_CREATION_TOOLS = frozenset(
    {
        "create_project",
        "add_project",
        "switch_project",
        "update_project",
    }
)


def _resolve_project_name_for_cost(
    project_id: Optional[str],
    state: Any,
) -> Optional[str]:
    """Best-effort lookup of the human-readable name for a project_id.

    Used to snapshot ``(project_id, name)`` into ``project_names`` at
    PlannerContext push so the dashboard can still render the right
    label after a project is deleted from Marcus's registry. Tries the
    in-memory project_manager / project_registry cache; never raises.

    Returns ``None`` when nothing resolves — the cost row still gets
    written with the id, the dashboard just falls back to its existing
    name resolution chain (projects.json, then truncated id).
    """
    if not project_id:
        return None
    # 1. project_manager.active_project_name (matches active project)
    pm = getattr(state, "project_manager", None)
    if pm is not None:
        active_id = getattr(pm, "active_project_id", None)
        active_name = getattr(pm, "active_project_name", None)
        if active_id == project_id and active_name:
            return str(active_name)
    # 2. project_registry cache (sync attribute access; we don't await
    #    inside a hot path).
    # TODO: ProjectRegistry should expose a sync ``get_cached_project``
    # so we're not poking at ``_cache`` directly — leaky abstraction
    # caught in Kaia's review on PR #515. Refactor when registry
    # internals next move.
    registry = getattr(state, "project_registry", None)
    if registry is not None:
        cache = getattr(registry, "_cache", None)
        if isinstance(cache, dict):
            cfg = cache.get(project_id)
            if cfg is not None and getattr(cfg, "name", None):
                return str(cfg.name)
    return None


def _resolve_project_for_cost(
    arguments: Dict[str, Any],
    state: Any,
    tool_name: Optional[str] = None,
) -> Optional[str]:
    """Pick the most specific project_id available for cost attribution.

    Marcus's identity (per CLAUDE.md GH-388 and spawn_agents.py) is
    ``project_id``, so every planner LLM call made while servicing an
    MCP request should be tagged with the project that request belongs
    to. This helper checks, in order:

    1. ``project_id`` explicitly in the tool arguments.
    2. ``agent_id`` → ``state.agent_project_map`` (set by register_agent).
    3. ``state.selected_project_id`` (the active project on the server).
       **Skipped for tools in :data:`_PROJECT_CREATION_TOOLS`** so that
       project-creation work isn't mis-attributed to the previously
       active project.

    Returns ``None`` when none of those resolve, in which case the
    recorder falls back to its ``'unassigned'`` bucket — visible in
    the dashboard as a separate row so the gap is observable.
    """
    pid = arguments.get("project_id")
    if pid:
        return str(pid)
    agent_id = arguments.get("agent_id")
    if agent_id:
        mapped = getattr(state, "agent_project_map", {}).get(agent_id)
        if mapped:
            return str(mapped)
    if tool_name in _PROJECT_CREATION_TOOLS:
        return None
    selected = getattr(state, "selected_project_id", None) or getattr(
        state, "current_project_id", None
    )
    if selected:
        return str(selected)
    return None


async def handle_tool_call(
    name: str, arguments: Optional[Dict[str, Any]], state: Any
) -> List[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """
    Handle tool calls by routing to appropriate tool functions.

    Args:
        name: Name of the tool to call
        arguments: Tool arguments
        state: Marcus server state instance

    Returns
    -------
        List of MCP content objects with tool results
    """
    if arguments is None:
        arguments = {}

    # Track timing
    start_time = time.time()

    # Get client info if available
    client_id = None
    client_type = None
    if hasattr(state, "_current_client_id"):
        client_id = state._current_client_id
    if hasattr(state, "_registered_clients") and client_id:
        client_info = state._registered_clients.get(client_id, {})
        client_type = client_info.get("client_type")

    # Get audit logger
    audit_logger = get_audit_logger()

    # Check access control
    allowed_tools = get_client_tools(client_id, state)
    if name not in allowed_tools and "*" not in allowed_tools:
        # Audit access denied
        duration_ms = (time.time() - start_time) * 1000
        await audit_logger.log_access_denied(
            client_id=client_id,
            client_type=client_type,
            tool_name=name,
            reason=(
                f"Tool '{name}' not allowed for client type "
                f"'{client_type or 'unregistered'}'"
            ),
            duration_ms=duration_ms,
        )

        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": (
                            f"Access denied: Tool '{name}' not "
                            f"available for your client type"
                        ),
                        "client_type": client_type or "unregistered",
                        "allowed_tools": allowed_tools,
                    },
                    indent=2,
                ),
            )
        ]

    # Push a PlannerContext for the duration of this tool call so any
    # planner-side LLM calls Marcus makes while servicing it get tagged
    # with the right project_id (the dashboard's join key). If no
    # project resolves, the recorder falls back to 'unassigned' and the
    # gap shows up in the dashboard's unassigned bucket. (#409)
    _cost_project_id = _resolve_project_for_cost(arguments, state, tool_name=name)
    # Resolve a human-readable name to snapshot alongside the id so the
    # cost dashboard renders the right label even after the project is
    # later deleted from the registry. Best-effort lookup; None falls
    # through and we still record the id.
    _cost_project_name = _resolve_project_name_for_cost(_cost_project_id, state)
    _cost_stack = ExitStack()
    if _cost_project_id is not None:
        _cost_stack.enter_context(
            get_recorder().planner_context(
                PlannerContext(
                    run_id="unassigned",
                    project_id=_cost_project_id,
                    project_name=_cost_project_name,
                )
            )
        )

    try:
        # Initialize result variable with proper type
        result: Any = None

        # Authentication tools (special handling)
        if name == "authenticate":
            client_id_arg = arguments.get("client_id")
            client_type_arg = arguments.get("client_type")
            role_arg = arguments.get("role")

            # Validate required arguments
            if not all([client_id_arg, client_type_arg, role_arg]):
                return [
                    types.TextContent(
                        type="text",
                        text=(
                            '{"success": false, "error": "Missing '
                            "required arguments: client_id, "
                            'client_type, role"}'
                        ),
                    )
                ]

            result = await authenticate(
                client_id=str(client_id_arg),
                client_type=str(client_type_arg),
                role=str(role_arg),
                metadata=arguments.get("metadata"),
                state=state,
            )
            # Client has been registered, update tracking
            client_id = arguments.get("client_id")
            client_type = arguments.get("client_type")

        # Agent management tools
        elif name == "register_agent":
            agent_id = arguments.get("agent_id") if arguments else None
            agent_name = arguments.get("name") if arguments else None
            role = arguments.get("role") if arguments else None

            if not agent_id or not agent_name or not role:
                result = {"error": "agent_id, name, and role are required"}
            else:
                result = await register_agent(
                    agent_id=agent_id,
                    name=agent_name,
                    role=role,
                    skills=arguments.get("skills", []),
                    state=state,
                    project_id=arguments.get("project_id", ""),
                )

        elif name == "get_agent_status":
            agent_id = arguments.get("agent_id") if arguments else None
            if not agent_id:
                result = {"error": "agent_id is required"}
            else:
                result = await get_agent_status(agent_id=agent_id, state=state)

        elif name == "list_registered_agents":
            result = await list_registered_agents(state=state)

        # Task management tools
        elif name == "request_next_task":
            agent_id = arguments.get("agent_id") if arguments else None
            if not agent_id:
                result = {"error": "agent_id is required"}
            else:
                result = await request_next_task(agent_id=agent_id, state=state)

        elif name == "report_task_progress":
            agent_id = arguments.get("agent_id") if arguments else None
            task_id = arguments.get("task_id") if arguments else None
            status = arguments.get("status") if arguments else None

            if not agent_id or not task_id or not status:
                result = {"error": "agent_id, task_id, and status are required"}
            else:
                result = await report_task_progress(
                    agent_id=agent_id,
                    task_id=task_id,
                    status=status,
                    progress=arguments.get("progress", 0),
                    message=arguments.get("message", ""),
                    state=state,
                    start_command=arguments.get("start_command"),
                    readiness_probe=arguments.get("readiness_probe"),
                    lease_epoch=arguments.get("lease_epoch"),
                )

        elif name == "report_blocker":
            agent_id = arguments.get("agent_id") if arguments else None
            task_id = arguments.get("task_id") if arguments else None
            blocker_description = (
                arguments.get("blocker_description") if arguments else None
            )

            if not agent_id or not task_id or not blocker_description:
                result = {
                    "error": (
                        "agent_id, task_id, and " "blocker_description are required"
                    )
                }
            else:
                result = await report_blocker(
                    agent_id=agent_id,
                    task_id=task_id,
                    blocker_description=blocker_description,
                    severity=arguments.get("severity", "medium"),
                    state=state,
                )

        elif name == "request_task_redo":
            agent_id = arguments.get("agent_id") if arguments else None
            task_id = arguments.get("task_id") if arguments else None
            reason = arguments.get("reason") if arguments else None

            if not agent_id or not task_id or not reason:
                result = {"error": "agent_id, task_id, and reason are required"}
            else:
                result = await request_task_redo(
                    agent_id=agent_id,
                    task_id=task_id,
                    reason=reason,
                    state=state,
                )

        # Project monitoring tools
        elif name == "get_project_status":
            result = await get_project_status(state=state)

        # System health tools
        elif name == "ping":
            result = await ping(echo=arguments.get("echo", ""), state=state)

        elif name == "check_assignment_health":
            result = await check_assignment_health(state=state)

        elif name == "get_usage_report":
            days = arguments.get("days", 7) if arguments else 7
            result = await get_usage_report(days=days, state=state)

        elif name == "get_cost_summary":
            args = arguments or {}
            result = await get_cost_summary(
                run_id=args.get("run_id"),
                project_id=args.get("project_id"),
                state=state,
            )

        elif name == "check_board_health":
            result = await check_board_health(state=state)

        elif name == "check_task_dependencies":
            task_id = arguments.get("task_id") if arguments else None
            if not task_id:
                result = {"error": "task_id is required"}
            else:
                result = await check_task_dependencies(task_id=task_id, state=state)

        # Natural language tools
        elif name == "create_project":
            # Log tool call start
            state.log_event(
                "mcp_tool_call_start",
                {
                    "tool": "create_project",
                    "project_name": arguments.get("project_name", "unknown"),
                },
            )

            description = arguments.get("description") if arguments else None
            project_name = arguments.get("project_name") if arguments else None

            if not description or not project_name:
                result = {"error": "description and project_name are required"}
            else:
                # Cost attribution (placeholder push + rebind) lives
                # inside ``nlp.create_project`` so every entry point —
                # this legacy stdio handler, FastMCP HTTP, or direct
                # Python callers — gets the same behavior. See the
                # docstring on ``create_project`` for the two-phase
                # design.
                result = await create_project(
                    description=description,
                    project_name=project_name,
                    options=arguments.get("options"),
                    state=state,
                )

            # Log tool call complete
            state.log_event(
                "mcp_tool_call_complete",
                {
                    "tool": "create_project",
                    "project_name": arguments.get("project_name", "unknown"),
                    "success": (
                        result.get("success", False)
                        if isinstance(result, dict)
                        else False
                    ),
                },
            )

        elif name == "add_feature":
            feature_description = (
                arguments.get("feature_description") if arguments else None
            )

            if not feature_description:
                result = {"error": "feature_description is required"}
            else:
                result = await add_feature(
                    feature_description=feature_description,
                    integration_point=arguments.get("integration_point", "auto_detect"),
                    state=state,
                )

        # Context Tools
        elif name == "log_decision":
            agent_id = arguments.get("agent_id") if arguments else None
            task_id = arguments.get("task_id") if arguments else None
            decision = arguments.get("decision") if arguments else None

            if not agent_id or not task_id or not decision:
                result = {"error": "agent_id, task_id, and decision are required"}
            else:
                result = await log_decision(
                    agent_id=agent_id,
                    task_id=task_id,
                    decision=decision,
                    state=state,
                )

        elif name == "get_task_context":
            task_id = arguments.get("task_id") if arguments else None

            if not task_id:
                result = {"error": "task_id is required"}
            else:
                result = await get_task_context(task_id=task_id, state=state)

        elif name == "log_artifact":
            task_id = arguments.get("task_id") if arguments else None
            filename = arguments.get("filename") if arguments else None
            content = arguments.get("content") if arguments else None
            artifact_type = arguments.get("artifact_type") if arguments else None

            if not task_id or not filename or not content or not artifact_type:
                result = {
                    "error": (
                        "task_id, filename, content, and " "artifact_type are required"
                    )
                }
            else:
                result = await log_artifact(
                    task_id=task_id,
                    filename=filename,
                    content=content,
                    artifact_type=artifact_type,
                    project_root=arguments.get("project_root"),
                    description=arguments.get("description", ""),
                    location=arguments.get("location"),  # Optional override
                    state=state,
                )

        # Project Management Tools
        elif name == "list_projects":
            result = await list_projects(state, arguments)
        elif name == "switch_project":
            result = await switch_project(state, arguments)
        elif name == "get_current_project":
            result = await get_current_project(state, arguments)
        elif name == "add_project":
            result = await add_project(state, arguments)
        elif name == "remove_project":
            result = await remove_project(state, arguments)
        elif name == "update_project":
            result = await update_project(state, arguments)

        # Prediction and AI intelligence tools
        elif name == "predict_completion_time":
            result = await predict_completion_time(
                project_id=arguments.get("project_id"),
                include_confidence=arguments.get("include_confidence", True),
                state=state,
            )

        elif name == "predict_task_outcome":
            task_id = arguments.get("task_id") if arguments else None
            if not task_id:
                result = {"error": "task_id is required"}
            else:
                result = await predict_task_outcome(
                    task_id=task_id,
                    agent_id=arguments.get("agent_id"),
                    state=state,
                )

        elif name == "predict_blockage_probability":
            task_id = arguments.get("task_id") if arguments else None
            if not task_id:
                result = {"error": "task_id is required"}
            else:
                result = await predict_blockage_probability(
                    task_id=task_id,
                    include_mitigation=arguments.get("include_mitigation", True),
                    state=state,
                )

        elif name == "predict_cascade_effects":
            task_id = arguments.get("task_id") if arguments else None
            if not task_id:
                result = {"error": "task_id is required"}
            else:
                result = await predict_cascade_effects(
                    task_id=task_id,
                    delay_days=arguments.get("delay_days", 1),
                    state=state,
                )

        elif name == "get_task_assignment_score":
            task_id = arguments.get("task_id") if arguments else None
            agent_id = arguments.get("agent_id") if arguments else None
            if not task_id or not agent_id:
                result = {"error": "task_id and agent_id are required"}
            else:
                result = await get_task_assignment_score(
                    task_id=task_id,
                    agent_id=agent_id,
                    state=state,
                )

        # Analytics and metrics tools
        elif name == "get_system_metrics":
            result = await get_system_metrics(
                time_window=arguments.get("time_window", "1h"),
                state=state,
            )

        elif name == "get_agent_metrics":
            agent_id = arguments.get("agent_id") if arguments else None
            if not agent_id:
                result = {"error": "agent_id is required"}
            else:
                result = await get_agent_metrics(
                    agent_id=agent_id,
                    time_window=arguments.get("time_window", "7d"),
                    state=state,
                )

        elif name == "get_project_metrics":
            result = await get_project_metrics(
                project_id=arguments.get("project_id"),
                time_window=arguments.get("time_window", "7d"),
                state=state,
            )

        elif name == "get_task_metrics":
            result = await get_task_metrics(
                time_window=arguments.get("time_window", "30d"),
                group_by=arguments.get("group_by", "status"),
                state=state,
            )

        # Code production metrics tools
        elif name == "get_code_metrics":
            agent_id = arguments.get("agent_id") if arguments else None
            if not agent_id:
                result = {"error": "agent_id is required"}
            else:
                result = await get_code_metrics(
                    agent_id=agent_id,
                    start_date=arguments.get("start_date"),
                    end_date=arguments.get("end_date"),
                    state=state,
                )

        elif name == "get_repository_metrics":
            repository = arguments.get("repository") if arguments else None
            if not repository:
                result = {"error": "repository is required"}
            else:
                result = await get_repository_metrics(
                    repository=repository,
                    time_window=arguments.get("time_window", "7d"),
                    state=state,
                )

        elif name == "get_code_review_metrics":
            result = await get_code_review_metrics(
                agent_id=arguments.get("agent_id"),
                time_window=arguments.get("time_window", "7d"),
                state=state,
            )

        elif name == "get_code_quality_metrics":
            repository = arguments.get("repository") if arguments else None
            if not repository:
                result = {"error": "repository is required"}
            else:
                result = await get_code_quality_metrics(
                    repository=repository,
                    branch=arguments.get("branch", "main"),
                    state=state,
                )

        # Scheduling and Planning Tools
        elif name == "get_optimal_agent_count":
            result = await get_optimal_agent_count(
                include_details=arguments.get("include_details", False),
                state=state,
            )

        # Pattern Learning Tools removed - only accessible via visualization UI API
        elif name in [
            "get_similar_projects",
            "get_project_patterns",
            "assess_project_quality",
            "get_pattern_recommendations",
            "learn_from_completed_project",
            "get_quality_trends",
        ]:
            result = {
                "error": (
                    f"Pattern learning tool '{name}' is not available through MCP. "
                    "Please use the visualization UI API endpoints instead."
                ),
                "suggestion": (
                    "Pattern learning tools are now only accessible through "
                    "the web UI at http://localhost:8080"
                ),
            }

        else:
            result = {"error": f"Unknown tool: {name}"}

        # Log response creation
        state.log_event(
            "mcp_creating_response",
            {
                "tool": name,
                "has_result": result is not None,
                "result_type": type(result).__name__ if result else "None",
            },
        )

        # Touch lease on any agent tool activity (proves agent is alive)
        agent_id = arguments.get("agent_id") if arguments else None
        if agent_id and hasattr(state, "lease_manager") and state.lease_manager:
            await state.lease_manager.touch_lease(agent_id)

        response: List[
            types.TextContent | types.ImageContent | types.EmbeddedResource
        ] = [types.TextContent(type="text", text=json.dumps(result, indent=2))]

        # Log MCP tool response (especially failures) for diagnostics
        if isinstance(result, dict):
            log_mcp_tool_response(
                tool_name=name,
                arguments=arguments,
                response=result,
            )

        # Ensure stdio buffer is flushed for immediate response delivery
        import sys

        sys.stdout.flush()
        sys.stderr.flush()

        # Log response return
        state.log_event(
            "mcp_returning_response",
            {
                "tool": name,
                "response_length": (
                    len(response[0].text)
                    if response and isinstance(response[0], types.TextContent)
                    else 0
                ),
            },
        )

        # Audit successful tool call
        duration_ms = (time.time() - start_time) * 1000
        await audit_logger.log_tool_call(
            client_id=client_id,
            client_type=client_type,
            tool_name=name,
            arguments=arguments,
            result=result,
            duration_ms=duration_ms,
            success=True,
        )

        return response

    except Exception as e:
        # Audit failed tool call
        duration_ms = (time.time() - start_time) * 1000
        await audit_logger.log_tool_call(
            client_id=client_id,
            client_type=client_type,
            tool_name=name,
            arguments=arguments,
            result=None,
            duration_ms=duration_ms,
            success=False,
            error=str(e),
        )

        error_response: List[
            types.TextContent | types.ImageContent | types.EmbeddedResource
        ] = [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {"error": f"Tool execution failed: {str(e)}", "tool": name},
                    indent=2,
                ),
            )
        ]

        # Ensure stdio buffer is flushed for immediate error delivery
        import sys

        sys.stdout.flush()
        sys.stderr.flush()

        return error_response
    finally:
        # Pop the PlannerContext pushed at the top of this call so events
        # made by subsequent handlers don't inherit a stale project_id.
        _cost_stack.close()
