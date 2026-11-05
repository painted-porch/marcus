"""
Unit tests for the HTTP wrapper of ``request_task_redo``.

Same failure class as ``test_history_tool_forwards_filters.py``: the
wrapper in ``MarcusServer._register_endpoint_tools`` names its own
parameter list, independent of the real implementation's signature in
``src.marcus_mcp.tools.task``. Issue #809 added a ``defect_type``
parameter to the implementation, and the live check for it (not a unit
test — nothing here would have caught it) found the wrapper's own
hardcoded signature never names it, so every explicit ``defect_type``
an agent sends is silently dropped before the implementation ever sees
it.
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

    impl = AsyncMock(return_value={"success": True})
    with patch("src.marcus_mcp.tools.task.request_task_redo", impl):
        await app.call_tool("request_task_redo", arguments)
    return impl


class TestRequestTaskRedoWrapperForwardsDefectType:
    """Test suite for defect_type forwarding in the HTTP wrapper."""

    @pytest.mark.asyncio
    async def test_an_explicit_defect_type_reaches_the_implementation(self) -> None:
        """A caller's defect_type must not be dropped by the wrapper."""
        impl = await _call_wrapper(
            {
                "agent_id": "verifier",
                "task_id": "task-1",
                "reason": "the criteria never asked for this",
                "defect_type": "requirement_gap",
            }
        )

        kwargs = impl.await_args.kwargs
        assert kwargs["defect_type"] == "requirement_gap"

    @pytest.mark.asyncio
    async def test_an_omitted_defect_type_reaches_the_implementation_as_none(
        self,
    ) -> None:
        """Omitting it must forward None, not silently vanish as a kwarg
        the implementation never receives at all — the implementation's
        own default only applies if it actually gets called with None."""
        impl = await _call_wrapper(
            {
                "agent_id": "verifier",
                "task_id": "task-1",
                "reason": "wrong shape",
            }
        )

        kwargs = impl.await_args.kwargs
        assert kwargs.get("defect_type") is None
