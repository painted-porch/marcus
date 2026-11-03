"""
Unit tests for the single-endpoint core agent tool surface (#737).

The default HTTP endpoint registers a small set of agent tools on the
FastMCP instance. A verify worker reads the claim it must check
through ``get_task_context`` (the board-written ``inputs``), so that
tool must be on the core surface; without it, HTTP workers cannot see
their inputs and the independence invariant cannot be exercised at
all. This test pins the registration.
"""

from typing import Any

import pytest

from src.marcus_mcp.server import MarcusServer

pytestmark = pytest.mark.unit


class TestCoreToolSurface:
    """Test suite for the core FastMCP tool registration."""

    @pytest.mark.asyncio
    async def test_core_surface_includes_the_agent_loop_tools(self) -> None:
        """The default endpoint carries the pull-loop and context tools."""
        from mcp.server.fastmcp import FastMCP

        server: Any = MarcusServer.__new__(MarcusServer)
        server._fastmcp = FastMCP("test-marcus")

        server._register_fastmcp_tools()
        tools = {t.name for t in await server._fastmcp.list_tools()}

        for required in (
            "register_agent",
            "request_next_task",
            "report_task_progress",
            "get_task_context",
            # #737 step 8: the operator exports audit bundles over the
            # same endpoint in single-endpoint deployments.
            "export_case",
        ):
            assert required in tools, f"{required} missing from core surface"

    @pytest.mark.asyncio
    async def test_all_tools_surface_serves_every_endpoint_group(self) -> None:
        """``--all-tools`` is the union of the endpoint groups (#795).

        The hand-copied list it used to register had drifted and lost
        ``log_decision`` and ``log_artifact``, so agents on that server
        could not record provenance.  The surface now resolves through
        the same registration path as the endpoints.
        """
        from mcp.server.fastmcp import FastMCP

        from src.marcus_mcp.tool_groups import TOOL_GROUPS

        server: Any = MarcusServer.__new__(MarcusServer)
        server._fastmcp = FastMCP("test-marcus")

        server._register_fastmcp_tools()
        tools = {t.name for t in await server._fastmcp.list_tools()}

        assert TOOL_GROUPS["agent"] <= tools
        assert {"log_decision", "log_artifact"} <= tools
        # A human-only tool proves the surface is the union, not just
        # the agent group under another name.
        assert "create_tasks" in tools

    @pytest.mark.asyncio
    async def test_agent_endpoint_serves_give_opinion(self) -> None:
        """The agent endpoint registers the opinion marker (deliberation B1)."""
        from mcp.server.fastmcp import FastMCP

        server: Any = MarcusServer.__new__(MarcusServer)
        app = FastMCP("test-marcus-agent")

        server._register_endpoint_tools(app, "agent")
        tools = {t.name: t for t in await app.list_tools()}

        assert "give_opinion" in tools
        required = set(tools["give_opinion"].inputSchema.get("required", []))
        assert required == {"agent_id", "subject", "what"}
