"""
Unit tests for the HTTP wrapper of ``query_project_history``.

The wrapper names every filter it accepts, so a filter the implementation
understands but the wrapper does not name is silently dropped.  The
live check for the opinions read (deliberation B2) found exactly that:
``subject`` reached the server and was ignored.  These tests drive the
wrapper through FastMCP, the way an agent does, and check what reaches
the implementation.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.marcus_mcp.server import MarcusServer

pytestmark = pytest.mark.unit


async def _call_wrapper(arguments: dict[str, Any]) -> AsyncMock:
    """Register the agent tools on a fresh app, call the wrapper, return the impl."""
    from mcp.server.fastmcp import FastMCP

    server: Any = MarcusServer.__new__(MarcusServer)
    app = FastMCP("test-marcus-agent")
    server._register_endpoint_tools(app, "agent")

    impl = AsyncMock(return_value={"success": True, "data": []})
    with patch("src.marcus_mcp.tools.history.query_project_history", impl):
        await app.call_tool("query_project_history", arguments)
    return impl


class TestHistoryToolForwardsFilters:
    """Test suite for filter forwarding in the HTTP wrapper."""

    @pytest.mark.asyncio
    async def test_opinion_filters_reach_the_implementation(self) -> None:
        """subject, kind and since are forwarded for the opinions read."""
        impl = await _call_wrapper(
            {
                "project_id": "p1",
                "query_type": "opinions",
                "subject": "mobs.north",
                "kind": "assessment",
                "since": "2026-11-03T00:00:00+00:00",
            }
        )

        kwargs = impl.await_args.kwargs
        assert kwargs["query_type"] == "opinions"
        assert kwargs["subject"] == "mobs.north"
        assert kwargs["kind"] == "assessment"
        assert kwargs["since"] == "2026-11-03T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_pagination_reaches_the_implementation(self) -> None:
        """limit and offset, documented on the tool, are forwarded."""
        impl = await _call_wrapper(
            {"project_id": "p1", "query_type": "decisions", "limit": 5, "offset": 2}
        )

        kwargs = impl.await_args.kwargs
        assert kwargs["limit"] == 5
        assert kwargs["offset"] == 2

    @pytest.mark.asyncio
    async def test_unset_filters_are_not_sent_as_none(self) -> None:
        """A filter the caller did not set is absent, not None."""
        impl = await _call_wrapper({"project_id": "p1", "query_type": "summary"})

        kwargs = impl.await_args.kwargs
        for name in ("subject", "kind", "since", "limit", "offset", "status"):
            assert name not in kwargs
