"""
Unit tests for the heartbeat on any tool call over HTTP (issue #765).

Marcus never asks agents for explicit heartbeats: any MCP tool call that
carries an ``agent_id`` proves the agent alive and extends its lease.
That hook lived only in the stdio dispatcher; HTTP mode serves tools
through FastMCP registrations that never reach it, so over HTTP only a
progress report extended a lease. These tests pin one hook for both
transports.
"""

import sys
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.marcus_mcp.heartbeat import HeartbeatFastMCP, touch_on_tool_call
from src.marcus_mcp.server import MarcusServer

pytestmark = pytest.mark.unit


def _state(with_lease_manager: bool = True) -> Mock:
    """A server state with (or without) a lease manager whose touch is recorded."""
    state = Mock()
    if with_lease_manager:
        state.lease_manager = Mock()
        state.lease_manager.touch_lease = AsyncMock(return_value=True)
    else:
        state.lease_manager = None
    return state


class TestTouchOnToolCall:
    """Test suite for the shared helper."""

    @pytest.mark.asyncio
    async def test_agent_id_in_arguments_touches_the_lease(self) -> None:
        """An agent_id argument is a heartbeat."""
        state = _state()
        await touch_on_tool_call({"agent_id": "agent-a", "x": 1}, state)
        state.lease_manager.touch_lease.assert_awaited_once_with("agent-a")

    @pytest.mark.asyncio
    async def test_no_agent_id_is_not_a_heartbeat(self) -> None:
        """Calls without an agent_id (ping, project status) touch nothing."""
        state = _state()
        await touch_on_tool_call({"echo": "hi"}, state)
        await touch_on_tool_call(None, state)
        state.lease_manager.touch_lease.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_lease_manager_is_tolerated(self) -> None:
        """A deployment without a lease manager just skips the heartbeat."""
        await touch_on_tool_call(
            {"agent_id": "agent-a"}, _state(with_lease_manager=False)
        )

    @pytest.mark.asyncio
    async def test_a_failing_touch_never_fails_the_tool_call(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The heartbeat is best-effort; the agent still gets its tool result."""
        state = _state()
        state.lease_manager.touch_lease = AsyncMock(side_effect=RuntimeError("boom"))
        await touch_on_tool_call({"agent_id": "agent-a"}, state)
        assert "heartbeat" in caplog.text.lower()


class TestHeartbeatFastMCP:
    """Test suite for the HTTP transport's hook."""

    def _app(self, state: Mock) -> HeartbeatFastMCP:
        app = HeartbeatFastMCP("probe", instructions="test", state=state)

        @app.tool()  # type: ignore[misc]
        async def probe(agent_id: str = "") -> Dict[str, Any]:
            """A tool that happens to take an agent_id."""
            return {"ok": True, "agent_id": agent_id}

        @app.tool()  # type: ignore[misc]
        async def ping(echo: str = "") -> Dict[str, Any]:
            """A tool with no agent identity."""
            return {"pong": echo}

        return app

    @pytest.mark.asyncio
    async def test_every_tool_call_with_an_agent_id_touches_the_lease(self) -> None:
        """Codex-proof version of the ADR promise: any call is a heartbeat."""
        state = _state()
        app = self._app(state)

        result = await app.call_tool("probe", {"agent_id": "agent-q"})

        state.lease_manager.touch_lease.assert_awaited_once_with("agent-q")
        assert result  # the tool's own result still comes back

    @pytest.mark.asyncio
    async def test_a_tool_without_agent_id_does_not_touch(self) -> None:
        """ping stays anonymous."""
        state = _state()
        app = self._app(state)

        await app.call_tool("ping", {"echo": "x"})

        state.lease_manager.touch_lease.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_tool_that_raises_does_not_touch(self) -> None:
        """A failed call is not proof of useful life; the error propagates unchanged."""
        state = _state()
        app = HeartbeatFastMCP("probe", instructions="test", state=state)

        @app.tool()  # type: ignore[misc]
        async def explode(agent_id: str = "") -> Dict[str, Any]:
            """Always fails."""
            raise ValueError("nope")

        with pytest.raises(Exception):
            await app.call_tool("explode", {"agent_id": "agent-q"})
        state.lease_manager.touch_lease.assert_not_awaited()


class TestServerBuildsTheHeartbeatApp:
    """Test suite for both FastMCP construction sites using the hook."""

    def test_all_tools_app_is_a_heartbeat_app_bound_to_the_server(self) -> None:
        """--all-tools mode."""
        fake = Mock()
        fake._fastmcp = None
        fake._register_fastmcp_tools = Mock()
        with patch.object(sys, "argv", ["server", "--http", "--all-tools"]):
            app = MarcusServer._create_fastmcp(fake)  # type: ignore[arg-type]
        assert isinstance(app, HeartbeatFastMCP)
        assert app.marcus_state is fake

    def test_agent_app_is_a_heartbeat_app_bound_to_the_server(self) -> None:
        """Default agent mode."""
        fake = Mock()
        fake._fastmcp = None
        fake._register_endpoint_tools = Mock()
        with patch.object(sys, "argv", ["server", "--http"]):
            app = MarcusServer._create_fastmcp(fake)  # type: ignore[arg-type]
        assert isinstance(app, HeartbeatFastMCP)
        assert app.marcus_state is fake

    def test_endpoint_apps_are_heartbeat_apps(self) -> None:
        """Multi-endpoint mode."""
        fake = Mock()
        fake._endpoint_apps = {}
        fake._register_endpoint_tools = Mock()
        app = MarcusServer._create_endpoint_app(fake, "agent")  # type: ignore[arg-type]
        assert isinstance(app, HeartbeatFastMCP)
        assert app.marcus_state is fake
