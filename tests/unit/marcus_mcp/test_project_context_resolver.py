"""
Unit tests for ``_resolve_project_for_cost`` in marcus_mcp.handlers.

The resolver is what makes the cost dashboard's project axis actually
work. Marcus's identity is ``project_id`` (per CLAUDE.md GH-388 and
the session launcher), so every planner LLM call made while servicing an
MCP request needs to land in the right project bucket.
"""

from __future__ import annotations

from typing import Any

from src.marcus_mcp.handlers import _resolve_project_for_cost


class _State:
    """Lightweight stand-in for MarcusServer state."""

    def __init__(
        self,
        agent_project_map: dict[str, str] | None = None,
        selected_project_id: str | None = None,
    ) -> None:
        self.agent_project_map: dict[str, str] = agent_project_map or {}
        self.selected_project_id: str | None = selected_project_id


class TestResolveProjectForCost:
    """Resolver checks args → agent map → server state, in that order."""

    def test_explicit_project_id_wins(self) -> None:
        """An explicit project_id arg overrides everything else."""
        state = _State(
            agent_project_map={"a1": "from-map"},
            selected_project_id="from-state",
        )
        assert (
            _resolve_project_for_cost(
                {"project_id": "explicit", "agent_id": "a1"}, state
            )
            == "explicit"
        )

    def test_falls_back_to_agent_project_map(self) -> None:
        """When no project_id arg, look up agent_id in the map."""
        state = _State(agent_project_map={"agent_1": "p_via_agent"})
        assert (
            _resolve_project_for_cost({"agent_id": "agent_1"}, state) == "p_via_agent"
        )

    def test_falls_back_to_selected_project_id(self) -> None:
        """If neither args nor agent map have it, use the selected project."""
        state = _State(selected_project_id="p_selected")
        assert _resolve_project_for_cost({}, state) == "p_selected"

    def test_falls_back_to_current_project_id(self) -> None:
        """current_project_id is accepted as an alt attribute name."""
        state: Any = type("S", (), {})()
        state.current_project_id = "p_current"
        assert _resolve_project_for_cost({}, state) == "p_current"

    def test_returns_none_when_nothing_resolves(self) -> None:
        """Resolver returns None so the caller can fall back to 'unassigned'."""
        state = _State()
        assert _resolve_project_for_cost({}, state) is None

    def test_unknown_agent_falls_through(self) -> None:
        """agent_id not in the map → keep looking at selected_project_id."""
        state = _State(
            agent_project_map={"a1": "p_a1"},
            selected_project_id="p_selected",
        )
        assert _resolve_project_for_cost({"agent_id": "unknown"}, state) == "p_selected"


class TestCodexP1ProjectCreationGuard:
    """Regression: project-creation tools must not inherit the active project.

    Codex P1 on PR #503: ``create_project`` runs heavy LLM decomposition.
    Falling back to ``state.selected_project_id`` for it attributed that
    work to the previously-active project, silently corrupting its cost
    totals. The resolver now returns ``None`` for creation tools when
    no ``project_id`` arg is present, so events land in the visible
    ``'unassigned'`` bucket instead.
    """

    def test_create_project_with_active_state_returns_none(self) -> None:
        """create_project must NOT fall back to selected_project_id."""
        state = _State(selected_project_id="old_active")
        assert _resolve_project_for_cost({}, state, tool_name="create_project") is None

    def test_add_project_with_active_state_returns_none(self) -> None:
        """add_project must NOT fall back to selected_project_id either."""
        state = _State(selected_project_id="old_active")
        assert _resolve_project_for_cost({}, state, tool_name="add_project") is None

    def test_switch_project_with_active_state_returns_none(self) -> None:
        """switch_project must NOT fall back to selected_project_id."""
        state = _State(selected_project_id="old_active")
        assert _resolve_project_for_cost({}, state, tool_name="switch_project") is None

    def test_creation_tool_with_explicit_project_id_uses_it(self) -> None:
        """Explicit project_id in args wins even for creation tools."""
        state = _State(selected_project_id="old_active")
        assert (
            _resolve_project_for_cost(
                {"project_id": "p_target"}, state, tool_name="switch_project"
            )
            == "p_target"
        )

    def test_non_creation_tool_still_inherits_active_project(self) -> None:
        """Sanity: the guard only applies to the creation set."""
        state = _State(selected_project_id="p_active")
        assert (
            _resolve_project_for_cost({}, state, tool_name="report_blocker")
            == "p_active"
        )

    def test_tool_name_none_preserves_legacy_behavior(self) -> None:
        """Backward compat: callers that don't pass tool_name still fall back."""
        state = _State(selected_project_id="p_active")
        assert _resolve_project_for_cost({}, state) == "p_active"
