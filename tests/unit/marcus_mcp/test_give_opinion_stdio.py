"""
Unit tests for ``give_opinion`` on the stdio tool surface (deliberation B1).

The stdio server advertises tools from hand-written schemas in
``handlers.py``; the HTTP endpoints derive theirs from the function
signature.  Both must agree on what is required, or an agent on one
transport is told a different contract from an agent on the other.
"""

import pytest

from src.marcus_mcp.handlers import get_all_tool_definitions, get_tool_definitions

pytestmark = pytest.mark.unit


class TestGiveOpinionStdioDefinition:
    """Test suite for the stdio schema of give_opinion."""

    def test_agent_role_advertises_give_opinion(self) -> None:
        """Coding agents on stdio see the tool."""
        names = {tool.name for tool in get_tool_definitions("agent")}

        assert "give_opinion" in names

    def test_required_fields_match_the_http_surface(self) -> None:
        """Required: agent_id, subject, what; why, impact, task_id optional."""
        tool = get_all_tool_definitions()["give_opinion"]

        assert set(tool.inputSchema["required"]) == {"agent_id", "subject", "what"}
        assert set(tool.inputSchema["properties"]) == {
            "agent_id",
            "subject",
            "what",
            "why",
            "impact",
            "task_id",
        }
