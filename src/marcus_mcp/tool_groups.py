"""
Tool group definitions for multi-endpoint Marcus server.

This module defines which tools are available on each endpoint
to provide role-based access without dynamic tool switching.
"""

from typing import Dict, Set

# Define tool groups for each endpoint
TOOL_GROUPS: Dict[str, Set[str]] = {
    "human": {
        # Essential tools for human developers using Claude Code
        "ping",
        "authenticate",
        "get_project_status",
        "list_projects",
        "select_project",
        "discover_planka_projects",  # Auto-discover projects from Planka
        "sync_projects",  # Sync projects from Planka into Marcus
        "switch_project",
        "get_current_project",
        "create_project",  # NLP project creation
        "create_tasks",  # Create tasks on existing project/board
        "add_feature",  # NLP feature addition
        "get_usage_report",
        "unassign_task",  # Manually unassign stuck tasks
        # Scheduling
        "get_optimal_agent_count",  # Calculate optimal agent count using CPM
        "get_desired_agent_count",  # Live layered-spawning signal (#595 Fix 3)
        # Diagnostics
        "diagnose_project",  # Diagnose project issues
        "capture_stall_snapshot",  # Capture snapshot when development stalls
        "replay_snapshot_conversations",  # Replay conversations from snapshot
        # Experiment tracking
        "start_experiment",
        "end_experiment",
        "get_experiment_status",
        # Project history queries
        "query_project_history",
        "list_project_history_files",
    },
    "agent": {
        # Core workflow tools for autonomous agents
        "ping",
        "register_agent",
        "request_next_task",
        "report_task_progress",
        "report_blocker",
        "get_task_context",
        "log_decision",
        "log_artifact",
        # Project creation
        "create_project",  # NLP project creation for autonomous agents
        # Scheduling
        "get_optimal_agent_count",  # Calculate optimal agent count using CPM
        "get_desired_agent_count",  # Live layered-spawning signal (#595 Fix 3)
        # Experiment tracking
        "start_experiment",
        "end_experiment",
        "get_experiment_status",
        # Project history queries
        "query_project_history",
        "list_project_history_files",
    },
    "analytics": {
        # All tools for comprehensive analytics (Cato)
        # This includes all tools from all groups plus analytics-specific ones
        "ping",
        "authenticate",
        "get_usage_report",
        # Project management
        "get_project_status",
        "list_projects",
        "select_project",
        "discover_planka_projects",
        "sync_projects",
        "switch_project",
        "get_current_project",
        "add_project",
        "remove_project",
        "update_project",
        # Agent management
        "register_agent",
        "get_agent_status",
        "list_registered_agents",
        # Task management
        "request_next_task",
        "report_task_progress",
        "report_blocker",
        "request_task_redo",
        "unassign_task",
        "get_task_context",
        "get_all_board_tasks",
        "check_task_dependencies",
        # Context and artifacts
        "log_decision",
        "log_artifact",
        # NLP tools
        "create_project",
        "create_tasks",
        "add_feature",
        # Scheduling
        "get_optimal_agent_count",  # Calculate optimal agent count using CPM
        "get_desired_agent_count",  # Live layered-spawning signal (#595 Fix 3)
        # System health
        "check_assignment_health",
        "check_board_health",
        # Pipeline analysis tools
        "pipeline_replay_start",
        "pipeline_replay_forward",
        "pipeline_replay_backward",
        "pipeline_replay_jump",
        "what_if_start",
        "what_if_simulate",
        "what_if_compare",
        "pipeline_compare",
        "pipeline_report",
        "pipeline_monitor_dashboard",
        "pipeline_monitor_flow",
        "pipeline_predict_risk",
        "pipeline_recommendations",
        "pipeline_find_similar",
        # Prediction and AI intelligence tools
        "predict_completion_time",
        "predict_task_outcome",
        "predict_blockage_probability",
        "predict_cascade_effects",
        "get_task_assignment_score",
        # Analytics and metrics tools
        "get_system_metrics",
        "get_agent_metrics",
        "get_project_metrics",
        "get_task_metrics",
        # Code production metrics tools
        "get_code_metrics",
        "get_repository_metrics",
        "get_code_review_metrics",
        "get_code_quality_metrics",
        # Diagnostics
        "diagnose_project",
        "capture_stall_snapshot",
        "replay_snapshot_conversations",
        # Project history queries
        "query_project_history",
        "list_project_history_files",
    },
}


def get_tools_for_endpoint(endpoint_type: str) -> Set[str]:
    """
    Get the set of tool names available for a specific endpoint type.

    Parameters
    ----------
    endpoint_type : str
        The type of endpoint ('human', 'agent', or 'analytics')

    Returns
    -------
    Set[str]
        Set of tool names available for this endpoint
    """
    return TOOL_GROUPS.get(endpoint_type, set())


def is_tool_allowed(endpoint_type: str, tool_name: str) -> bool:
    """
    Check if a tool is allowed for a specific endpoint type.

    Parameters
    ----------
    endpoint_type : str
        The type of endpoint ('human', 'agent', or 'analytics')
    tool_name : str
        The name of the tool to check

    Returns
    -------
    bool
        True if the tool is allowed for this endpoint
    """
    allowed_tools = get_tools_for_endpoint(endpoint_type)
    return tool_name in allowed_tools
