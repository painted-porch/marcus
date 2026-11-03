"""
Unit tests: the agent endpoint serves ``get_project_status``.

The worker contract (``prompts/Agent_prompt.md``) lists ``get_project_status``
as a tool every worker may call, and the redo guidance tells a worker to poll
it until a redone task is DONE. The session launcher also reads it for the
end-of-run report. Yet the agent endpoint's tool group and the ``agent``
role allowlist omitted it, so every such call was refused: a live run ended
with "could not verify completion" because the launcher got nothing back.
"""

import pytest

from src.marcus_mcp.tool_groups import get_tools_for_endpoint
from src.marcus_mcp.tools.auth import ROLE_TOOLS

pytestmark = pytest.mark.unit


class TestAgentEndpointServesProjectStatus:
    """Read-only board status is part of the worker contract."""

    def test_agent_tool_group_includes_get_project_status(self) -> None:
        """The agent endpoint registers the tool."""
        assert "get_project_status" in get_tools_for_endpoint("agent")

    def test_agent_role_may_call_get_project_status(self) -> None:
        """The ``agent`` role allowlist admits it."""
        assert "get_project_status" in ROLE_TOOLS["agent"]

    def test_agent_endpoint_also_serves_get_agent_status(self) -> None:
        """The contract names a worker's own read-only status tool too."""
        assert "get_agent_status" in get_tools_for_endpoint("agent")
        assert "get_agent_status" in ROLE_TOOLS["agent"]

    def test_every_tool_the_contract_names_is_served_to_agents(self) -> None:
        """The contract's tool list and the agent endpoint must agree."""
        from pathlib import Path

        repo = Path(__file__).resolve().parents[3]
        text = (repo / "prompts" / "Agent_prompt.md").read_text()
        allow = text[text.index("You can only use these Marcus tools") :]
        allow = allow[: allow.index("- You CANNOT")]
        named = {
            tok.strip(" ,\n")
            for tok in allow.replace(":", " ").split()
            if tok.strip(" ,\n").islower() and "_" in tok
        }
        served = get_tools_for_endpoint("agent")
        missing = sorted(t for t in named if t not in served)
        assert (
            missing == []
        ), f"contract names tools the agent endpoint lacks: {missing}"
