"""
Unit tests for the shutdown path draining refusal writes (issue #740).

Refusal writes are scheduled on the event loop; a shutdown that exits
the process without waiting for them loses the audit records the
persistence work exists to keep. The orderly cleanup must drain them
before it exits.
"""

from types import SimpleNamespace
from typing import Any, List
from unittest.mock import AsyncMock, patch

import pytest

from src.marcus_mcp.server import MarcusServer

pytestmark = pytest.mark.unit


def _bare_server() -> SimpleNamespace:
    """The attributes ``_cleanup_on_shutdown`` touches, nothing else."""
    return SimpleNamespace(
        _cleanup_done=False,
        tasks_being_assigned=set(),
        _active_operations=set(),
        assignment_monitor=None,
        lease_monitor=None,
        realtime_log=None,
        assignment_persistence=None,
    )


class TestShutdownDrainsRefusalWrites:
    """Test suite for the drain-before-exit guarantee."""

    @pytest.mark.asyncio
    async def test_cleanup_drains_refusal_writes_before_exit(self) -> None:
        """The drain is awaited, with the server state, before ``os._exit``."""
        order: List[str] = []
        server = _bare_server()

        async def fake_drain(state: Any, timeout: Any = None) -> None:
            assert state is server
            assert timeout is not None and timeout > 0
            order.append("drain")

        def fake_exit(code: int) -> None:
            order.append("exit")

        with (
            patch("src.marcus_mcp.server.drain_refusal_writes", side_effect=fake_drain),
            patch("src.marcus_mcp.server.os._exit", side_effect=fake_exit),
        ):
            await MarcusServer._cleanup_on_shutdown(server)  # type: ignore[arg-type]

        assert order == ["drain", "exit"]

    @pytest.mark.asyncio
    async def test_cleanup_still_exits_when_the_drain_fails(self) -> None:
        """A failing drain must not stop the process from exiting."""
        server = _bare_server()
        exited = AsyncMock()

        with (
            patch(
                "src.marcus_mcp.server.drain_refusal_writes",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "src.marcus_mcp.server.os._exit", side_effect=lambda code: exited(code)
            ),
        ):
            await MarcusServer._cleanup_on_shutdown(server)  # type: ignore[arg-type]

        exited.assert_called_once_with(0)
