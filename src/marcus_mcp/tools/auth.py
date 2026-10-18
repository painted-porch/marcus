"""
Authentication and role management tools for Marcus MCP.

This module provides tools for client registration and role-based access control.
"""

from typing import Any, Dict, List, Optional

from mcp.types import Tool

from ..audit import get_audit_logger

# Define role-based tool access
ROLE_TOOLS = {
    "observer": [
        # Basic connectivity and authentication
        "ping",
        "authenticate",
        # Read-only project visibility
        "get_project_status",
        "get_current_project",
        "list_projects",
        # Read-only agent visibility
        "list_registered_agents",
        "get_agent_status",
        # Read-only board health monitoring
        "check_board_health",
        "check_task_dependencies",
        # Scheduling and resource planning
        "get_optimal_agent_count",  # Calculate optimal agent count using CPM
        # Analytics and monitoring (delegated to Cato)
        # Pipeline tools removed - functionality moved to Cato
        # Project management (PMs need this)
        "remove_project",  # Delete projects
        # System health monitoring
        "check_assignment_health",  # Debug assignments
        # Audit and usage analytics
        "get_usage_report",  # Usage statistics
        "get_cost_summary",  # Per-experiment / per-project cost (#409)
        # Prediction and AI intelligence tools
        "predict_completion_time",  # Project completion predictions
        "predict_task_outcome",  # Task success probability
        "predict_blockage_probability",  # Blockage risk analysis
        "predict_cascade_effects",  # Delay impact analysis
        "get_task_assignment_score",  # Agent-task fitness scoring
        # Analytics and metrics tools
        "get_system_metrics",  # System-wide performance metrics
        "get_agent_metrics",  # Individual agent performance
        "get_project_metrics",  # Project health and velocity
        "get_task_metrics",  # Aggregated task analytics
        # Code production metrics tools
        "get_code_metrics",  # Individual code production metrics
        "get_repository_metrics",  # Repository-wide statistics
        "get_code_review_metrics",  # Review activity and participation
        "get_code_quality_metrics",  # Static analysis and quality gates
    ],
    "developer": [
        # Everything observers have (read access)
        "ping",
        "register_client",
        "get_project_status",
        "get_current_project",
        "list_projects",
        "list_registered_agents",
        "get_agent_status",
        "check_board_health",
        "check_task_dependencies",
        # Scheduling and resource planning
        "get_optimal_agent_count",  # Calculate optimal agent count using CPM
        # Project creation and management
        "create_project",  # NLP project creation
        "add_feature",  # NLP feature addition
        "switch_project",  # Change active project
        "add_project",  # Add existing project
        "update_project",  # Modify project config
        # Task context (read-only)
        "get_task_context",  # View task details
    ],
    "agent": [
        # Basic connectivity
        "ping",
        "register_client",
        # Core agent workflow
        "register_agent",  # Register themselves
        "request_next_task",  # Get assignments
        "report_task_progress",  # Update progress
        "report_blocker",  # Report issues
        "request_task_redo",  # Send failed completed work back (issue #627)
        # Context and collaboration
        "get_task_context",  # Full task context
        "log_decision",  # Document choices
        "log_artifact",  # Store outputs
        # Project creation
        "create_project",  # NLP project creation
        # Scheduling and resource planning
        "get_optimal_agent_count",  # Calculate optimal agent count using CPM
        # Experiment tracking
        "start_experiment",  # Start live experiments
        "end_experiment",  # Stop and finalize experiments
        "get_experiment_status",  # Check experiment status
    ],
    "admin": [
        # Admins get all tools
        "*"
    ],
}

# Default tools available before registration
# These allow basic connectivity and authentication
DEFAULT_TOOLS = ["ping", "authenticate"]


async def authenticate(
    client_id: str,
    client_type: str,
    role: str,
    metadata: Optional[Dict[str, Any]] = None,
    state: Any = None,
) -> Dict[str, Any]:
    """
    Authenticate with Marcus and establish role-based access.

    This tool establishes a client's identity and determines which Marcus tools
    they can access based on their client_type. Authentication is required before
    using most Marcus tools and enables audit logging of all client actions.

    Note: Admin access is controlled at the deployment level. Anyone who can
    start Marcus effectively has admin access. The admin role here is for
    tracking and audit purposes only.

    Parameters
    ----------
    client_id : str
        Unique identifier for the client (e.g., "cato-001",
        "user-john", "agent-backend-01"). This should be consistent
        across sessions for the same client

    client_type : str
        Must be one of: "observer", "developer", "agent", "admin"
        - observer: Read-only access for monitoring/analytics (e.g., Cato, PMs)
        - developer: Can create/manage projects and features via NLP
        - agent: AI agents that execute tasks (register → request → progress → complete)
        - admin: Full access to all tools

    role : str
        Specific role within the client type for identification
        (e.g., "analytics", "frontend", "pm"). This is descriptive
        and helps with audit logs but doesn't affect permissions

    metadata : Optional[Dict[str, Any]]
        Additional client metadata such as:
        - version: Client version
        - capabilities: List of client capabilities
        - environment: Development/production/staging
        - team: Team name or identifier

    state : Any
        Marcus server state (automatically provided)

    Returns
    -------
    Dict[str, Any]
        {
            "success": True,
            "client_id": "your-client-id",
            "client_type": "observer|developer|agent|admin",
            "role": "your-role",
            "available_tools": ["tool1", "tool2", ...],  # List of tools you can now use
            "message": "Registration confirmation"
        }

    Examples
    --------
    # Authenticate Cato as an observer for analytics
    authenticate(
        client_id="cato-prod-001",
        client_type="observer",
        role="analytics",
        metadata={"version": "2.0", "environment": "production"}
    )

    # Authenticate an AI agent (they still need to call register_agent after this)
    authenticate(
        client_id="agent-backend-01",
        client_type="agent",
        role="backend_developer",
        metadata={"capabilities": ["python", "api", "database"]}
    )

    # Authenticate a developer using Claude
    authenticate(
        client_id="user-alice",
        client_type="developer",
        role="frontend_lead",
        metadata={"team": "ui-team"}
    )
    """
    # Store client registration
    if not hasattr(state, "_registered_clients"):
        state._registered_clients = {}

    client_info = {
        "client_id": client_id,
        "client_type": client_type,
        "role": role,
        "metadata": metadata or {},
        "registered_at": (
            state._get_timestamp() if hasattr(state, "_get_timestamp") else None
        ),
    }

    state._registered_clients[client_id] = client_info

    # Set current client ID for tracking
    state._current_client_id = client_id

    # Log the registration
    state.log_event(
        "client_registered",
        {
            "client_id": client_id,
            "client_type": client_type,
            "role": role,
        },
    )

    # Audit the registration
    audit_logger = get_audit_logger()
    await audit_logger.log_registration(
        client_id=client_id,
        client_type=client_type,
        role=role,
        metadata=metadata,
    )

    # Get available tools for this client type
    available_tools = ROLE_TOOLS.get(client_type, DEFAULT_TOOLS)

    return {
        "success": True,
        "client_id": client_id,
        "client_type": client_type,
        "role": role,
        "available_tools": available_tools,
        "message": (
            f"Client '{client_id}' authenticated as {client_type} "
            f"with role '{role}'"
        ),
    }


def get_client_tools(client_id: Optional[str], state: Any) -> List[str]:
    """
    Get list of tools available to a specific client.

    Parameters
    ----------
    client_id : Optional[str]
        Client identifier, None for unregistered clients
    state : Any
        Marcus server state

    Returns
    -------
    List[str]
        List of available tool names
    """
    # If no client_id, return default tools
    if not client_id:
        return DEFAULT_TOOLS

    # Check if client is registered
    if not hasattr(state, "_registered_clients"):
        return DEFAULT_TOOLS

    client_info = state._registered_clients.get(client_id)
    if not client_info:
        return DEFAULT_TOOLS

    # Get tools based on client type
    client_type = client_info.get("client_type", "user")
    tools = ROLE_TOOLS.get(client_type, DEFAULT_TOOLS)

    # If admin, return all tools
    if "*" in tools:
        # Return all tool names from the registry
        from ..handlers import get_all_tool_names

        return get_all_tool_names()

    return tools


def get_tool_definitions_for_client(client_id: Optional[str], state: Any) -> List[Tool]:
    """
    Get tool definitions filtered by client access.

    Parameters
    ----------
    client_id : Optional[str]
        Client identifier
    state : Any
        Marcus server state

    Returns
    -------
    List[Tool]
        List of tool definitions available to the client
    """
    from ..handlers import get_all_tool_definitions

    # Get allowed tools for this client
    allowed_tools = get_client_tools(client_id, state)

    # Get all tool definitions
    all_tools_map = get_all_tool_definitions()

    # Filter based on client access
    if "*" in allowed_tools:
        return list(all_tools_map.values())

    return [all_tools_map[name] for name in allowed_tools if name in all_tools_map]


# Tool definition for registration
AUTHENTICATE_TOOL = Tool(
    name="authenticate",
    description="Authenticate with Marcus and establish role-based tool access",
    inputSchema={
        "type": "object",
        "properties": {
            "client_id": {
                "type": "string",
                "description": (
                    "Unique client identifier " "(e.g., 'cato-001', 'user-john')"
                ),
            },
            "client_type": {
                "type": "string",
                "enum": ["observer", "developer", "agent", "admin"],
                "description": "Type of client determining tool access",
            },
            "role": {
                "type": "string",
                "description": (
                    "Specific role " "(e.g., 'analytics', 'developer', 'backend')"
                ),
            },
            "metadata": {
                "type": "object",
                "description": "Optional metadata about the client",
                "additionalProperties": True,
            },
        },
        "required": ["client_id", "client_type", "role"],
    },
)
