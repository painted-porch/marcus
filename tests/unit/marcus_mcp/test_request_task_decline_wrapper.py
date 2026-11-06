"""
Unit tests for the HTTP wrapper of ``request_task_decline`` (ADR-0013 K5).

Same failure class as ``test_request_task_redo_wrapper_forwards_defect_type.py``:
the wrapper in ``MarcusServer._register_endpoint_tools`` names its own
parameter list, independent of the real implementation's signature in
``src.marcus_mcp.tools.task``. Issue #809 taught this session that a new
tool's HTTP-mode dispatch must be verified directly, not inferred from the
implementation working in isolation.
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

    impl = AsyncMock(return_value={"success": True, "status": "declined"})
    with patch("src.marcus_mcp.tools.task.request_task_decline", impl):
        await app.call_tool("request_task_decline", arguments)
    return impl


class TestRequestTaskDeclineWrapper:
    """Test suite for the request_task_decline HTTP wrapper."""

    @pytest.mark.asyncio
    async def test_the_reason_reaches_the_implementation(self) -> None:
        impl = await _call_wrapper(
            {
                "agent_id": "verifier-1",
                "task_id": "verify-1",
                "reason": "the summary count doesn't match the bucket table",
            }
        )

        kwargs = impl.await_args.kwargs
        assert kwargs["agent_id"] == "verifier-1"
        assert kwargs["task_id"] == "verify-1"
        assert kwargs["reason"] == "the summary count doesn't match the bucket table"
