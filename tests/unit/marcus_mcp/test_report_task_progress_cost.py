"""
Unit tests: ``report_task_progress`` carries an optional ``cost`` list (#742).

The recording rules live in ``src/cost_tracking/report_cost.py`` and are
tested in ``tests/unit/cost_tracking/test_report_cost.py``. These tests pin
the wiring: that both transports advertise and forward the argument, that a
report the board refused is passed on as refused, and that a report with no
cost has no ``cost`` key in its reply.
"""

from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytestmark = pytest.mark.unit

_CALL = {
    "seq": 1,
    "at": "2026-11-23T10:00:00Z",
    "provider": "anthropic",
    "model": "claude-haiku-4-5",
    "input_tokens": 10,
    "output_tokens": 5,
}


async def _report(progress_result: Dict[str, Any], **extra: Any) -> Any:
    """Call the real ``report_task_progress`` with the inner report patched.

    Parameters
    ----------
    progress_result : Dict[str, Any]
        What the board's own progress handling returns.
    **extra : Any
        Further keyword arguments for the tool.

    Returns
    -------
    Any
        ``(reply, recorder)``: the tool's reply and the patched recorder.
    """
    from src.marcus_mcp.tools import task as task_tools

    recorder = Mock(return_value={"recorded": True, "accepted": 1})
    with (
        patch.object(
            task_tools, "_record_progress", new=AsyncMock(return_value=progress_result)
        ),
        patch.object(task_tools, "record_report_cost", new=recorder),
    ):
        reply = await task_tools.report_task_progress(
            agent_id="finder-1",
            task_id="task-7",
            status="completed",
            progress=100,
            message="done",
            state=Mock(),
            **extra,
        )
    return reply, recorder


class TestTheToolRecordsCost:
    """The tool hands its cost to the recorder and returns the receipt."""

    @pytest.mark.asyncio
    async def test_an_accepted_report_passes_its_cost_as_accepted(self) -> None:
        """The recorder is told the board took the report."""
        reply, recorder = await _report({"success": True}, cost=[_CALL])

        assert recorder.call_args.kwargs["accepted"] is True
        assert reply["cost"] == {"recorded": True, "accepted": 1}

    @pytest.mark.asyncio
    async def test_a_refused_report_passes_its_cost_as_refused(self) -> None:
        """The recorder is told the board did not take the report."""
        _reply, recorder = await _report(
            {"success": False, "error": "evidence missing"}, cost=[_CALL]
        )

        assert recorder.call_args.kwargs["accepted"] is False

    @pytest.mark.asyncio
    async def test_a_report_with_no_cost_has_no_cost_key(self) -> None:
        """Existing callers see exactly the reply they saw before."""
        reply, recorder = await _report({"success": True})

        assert ("cost" in reply, recorder.called) == (False, False)


class TestBothTransportsCarryCost:
    """HTTP and stdio advertise and forward the same argument."""

    @pytest.mark.asyncio
    async def test_the_http_wrapper_forwards_cost(self) -> None:
        """What an HTTP client sends is what the tool receives."""
        from mcp.server.fastmcp import FastMCP

        from src.marcus_mcp.server import MarcusServer

        server: Any = MarcusServer.__new__(MarcusServer)
        server._fastmcp = FastMCP("cost-test")
        server._register_fastmcp_tools()
        impl = AsyncMock(return_value={"success": True})
        with patch("src.marcus_mcp.tools.task.report_task_progress", impl):
            await server._fastmcp.call_tool(
                "report_task_progress",
                {
                    "agent_id": "finder-1",
                    "task_id": "task-7",
                    "status": "completed",
                    "cost": [_CALL],
                },
            )

        assert impl.await_args.kwargs["cost"] == [_CALL]

    def test_the_stdio_schema_advertises_cost_as_an_optional_list(self) -> None:
        """stdio clients are told the argument exists and is a list."""
        from src.marcus_mcp.handlers import get_all_tool_definitions

        schema = get_all_tool_definitions()["report_task_progress"].inputSchema

        assert (
            schema["properties"]["cost"]["type"],
            "cost" in schema["required"],
        ) == ("array", False)

    @pytest.mark.asyncio
    async def test_the_stdio_dispatcher_forwards_cost(self) -> None:
        """What a stdio client sends is what the tool receives."""
        from src.marcus_mcp import handlers

        state = Mock()
        state._current_client_id = "finder-1"
        state._registered_clients = {"finder-1": {"client_type": "agent"}}
        impl = AsyncMock(return_value={"success": True})
        with (
            patch.object(handlers, "report_task_progress", new=impl),
            patch.object(handlers, "rehydrate_on_tool_call", new=AsyncMock()),
            patch.object(handlers, "get_audit_logger", return_value=AsyncMock()),
            patch.object(handlers, "touch_on_tool_call", new=AsyncMock()),
        ):
            await handlers.handle_tool_call(
                "report_task_progress",
                {
                    "agent_id": "finder-1",
                    "task_id": "task-7",
                    "status": "completed",
                    "progress": 100,
                    "message": "done",
                    "cost": [_CALL],
                },
                state,
            )

        assert impl.await_args.kwargs["cost"] == [_CALL]
