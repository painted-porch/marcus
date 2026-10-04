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
