"""
Unit tests for ``give_opinion`` on the stdio tool surface (deliberation B1).

The stdio server advertises tools from hand-written schemas in
``handlers.py``; the HTTP endpoints derive theirs from the function
signature.  Both must agree on what is required, or an agent on one
transport is told a different contract from an agent on the other.
"""

import pytest

from src.marcus_mcp.agent_server import AGENT_ALLOWED_TOOLS
from src.marcus_mcp.handlers import get_all_tool_definitions, get_tool_definitions
from src.marcus_mcp.tools.auth import ROLE_TOOLS

pytestmark = pytest.mark.unit


class TestGiveOpinionStdioAllowlists:
    """Test suite for the stdio-side allowlists (Codex P2 on PR #800).

    An authenticated stdio client of type ``agent`` is filtered through
    ``ROLE_TOOLS["agent"]`` before dispatch, and the restricted
    ``AgentMarcusServer`` has its own list; a tool missing from either is
    advertised in the schema and refused on call.
    """

    def test_authenticated_agent_role_allows_give_opinion(self) -> None:
        """The auth-layer allowlist for agents includes the tool."""
        assert "give_opinion" in ROLE_TOOLS["agent"]

    def test_restricted_agent_server_allows_give_opinion(self) -> None:
        """The restricted agent server's allowlist includes the tool."""
        assert "give_opinion" in AGENT_ALLOWED_TOOLS


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
