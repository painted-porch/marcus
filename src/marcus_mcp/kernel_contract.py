"""
The kernel contract's tool index: which served tools an extension may rely on.

Marcus is being split into a kernel (the board and its rules) and extensions
(separate programs that use the board for one purpose). The door between them
is written down in ``docs/architecture/kernel-contract.md``; this module is the
machine-readable copy of that document's tool list, so tests can hold the code
and the document to each other (issue #939, Track A1).

Every tool the HTTP server registers with ``--all-tools`` is in exactly one
tier:

``contract``
    Part of the door. Its arguments and reply shape are specified in
    ``kernel-contract.md`` and a change to either bumps
    :data:`KERNEL_CONTRACT_VERSION`.
``operator``
    Kernel code that a person operating a server uses (switching projects,
    health checks, diagnostics, board analytics). Served, but outside the
    door: it may change in any release without a version bump, and an
    extension must not depend on it.
``leaving_with_track_c``
    Software-building tools, which move to ``painted-porch/marcus-software``
    with the extraction (epic #924). Outside the door for the same reason.

``tests/unit/marcus_mcp/test_kernel_contract_surface.py`` fails when a
registered tool is in no tier, when the tiers overlap, or when the document
and this module disagree about the version or the contract tools.
"""

from typing import Dict, FrozenSet

#: The version of the contract this kernel implements.
#:
#: Semantic versioning, applied to the door. MAJOR: a contract tool, argument
#: or reply field is removed or renamed, an optional argument becomes
#: required, or a field's meaning changes. MINOR: a contract tool, an optional
#: argument or a reply field is added. PATCH: the specification's wording is
#: corrected without changing behaviour. A kernel release states the version
#: it implements.
KERNEL_CONTRACT_VERSION = "1.0.0"

CONTRACT_TOOLS: FrozenSet[str] = frozenset(
    {
        # Connectivity.
        "ping",
        # Registration and the pull loop (Agent Invariant 1).
        "register_agent",
        "get_agent_status",
        "request_next_task",
        "report_task_progress",
        "report_blocker",
        "request_task_redo",
        "request_task_decline",
        # Reading the board and writing to it.
        "get_task_context",
        "get_project_status",
        "log_decision",
        "log_artifact",
        "give_opinion",
        # Seeding (ADR-0013 K2, the MCP-only write path).
        "create_project",
        "create_tasks",
        "add_project",
        # The record.
        "export_case",
        "query_project_history",
        "get_cost_summary",
        # Measurement the kernel owns (ADR-0013 K9).
        "get_desired_agent_count",
    }
)

OPERATOR_TOOLS: FrozenSet[str] = frozenset(
    {
        "authenticate",
        # Project administration.
        "list_projects",
        "select_project",
        "switch_project",
        "get_current_project",
        "remove_project",
        "update_project",
        "sync_projects",
        "discover_planka_projects",
        # Board administration and health.
        "unassign_task",
        "list_registered_agents",
        "get_all_board_tasks",
        "check_task_dependencies",
        "check_assignment_health",
        "check_board_health",
        "get_usage_report",
        "get_optimal_agent_count",
        "list_project_history_files",
        # Diagnostics.
        "diagnose_project",
        "capture_stall_snapshot",
        "replay_snapshot_conversations",
        # Board analytics; issue #948 decides where their packages end up.
        "predict_completion_time",
        "predict_task_outcome",
        "predict_blockage_probability",
        "predict_cascade_effects",
        "get_task_assignment_score",
        "get_system_metrics",
        "get_agent_metrics",
        "get_project_metrics",
        "get_task_metrics",
    }
)

LEAVING_WITH_TRACK_C: FrozenSet[str] = frozenset(
    {
        "add_feature",
        "get_code_metrics",
        "get_repository_metrics",
        "get_code_review_metrics",
        "get_code_quality_metrics",
    }
)

#: Tier name to the tools in it.
TIERS: Dict[str, FrozenSet[str]] = {
    "contract": CONTRACT_TOOLS,
    "operator": OPERATOR_TOOLS,
    "leaving_with_track_c": LEAVING_WITH_TRACK_C,
}


def tier_of(tool_name: str) -> str:
    """Return the tier a served tool belongs to.

    Parameters
    ----------
    tool_name : str
        A tool the HTTP server registers.

    Returns
    -------
    str
        ``"contract"``, ``"operator"`` or ``"leaving_with_track_c"``.

    Raises
    ------
    KeyError
        When the tool is in no tier, which means it is not a served tool.
    """
    for tier, names in TIERS.items():
        if tool_name in names:
            return tier
    raise KeyError(f"{tool_name!r} is not a served kernel tool")
