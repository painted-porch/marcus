"""
Unit tests for the ``kind`` filter on the ``decisions`` query (#880).

Opinions are stored as decision rows with ``kind="assessment"``, so the
``decisions`` query returned opinions mixed in with decisions and a reader
that wanted only the crew's decisions (the Minecraft harness's pull
bundle, the planner's context) had to filter client-side. The query now
honours the ``kind`` filter the tool already names for the opinions read;
with no ``kind`` the behaviour is unchanged.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.marcus_mcp.tools.history import query_project_history

pytestmark = pytest.mark.unit


def _row(decision_id: str, kind: str) -> Any:
    """Build a decision-like row with a ``kind`` and a ``to_dict``."""
    data = {"decision_id": decision_id, "kind": kind, "subject": "wood.shortage"}
    return SimpleNamespace(kind=kind, to_dict=lambda: dict(data))


def _mixed() -> list[Any]:
    """One decision, one opinion, one primer, newest first."""
    return [
        _row("dec-eir", "decision"),
        _row("op-bly", "assessment"),
        _row("primer", "objective"),
    ]


def _query_api() -> Mock:
    """A query API whose every decisions read returns the mixed rows."""
    api = Mock()
    api.get_project_history = AsyncMock(
        return_value=SimpleNamespace(decisions=_mixed())
    )
    api.find_decisions_by_agent = AsyncMock(return_value=_mixed())
    api.find_decisions_by_task = AsyncMock(return_value=_mixed())
    return api


def _patched(api: Mock) -> Any:
    """Patch the tool's aggregator and query API with ``api``."""
    return (
        patch("src.marcus_mcp.tools.history.ProjectHistoryAggregator"),
        patch("src.marcus_mcp.tools.history.ProjectHistoryQuery", return_value=api),
    )


class TestDecisionsQueryKindFilter:
    """Test suite for the ``kind`` filter on the decisions branch."""

    @pytest.mark.asyncio
    async def test_kind_decision_returns_decisions_only(self) -> None:
        """``kind="decision"`` leaves the opinions and the primer out."""
        api = _query_api()
        p1, p2 = _patched(api)
        with p1, p2:
            result = await query_project_history(
                "proj-1", "decisions", Mock(), kind="decision"
            )

        assert result["success"] is True
        assert [d["decision_id"] for d in result["data"]] == ["dec-eir"]
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_no_kind_keeps_todays_behaviour(self) -> None:
        """Without ``kind`` every row comes back, as before."""
        api = _query_api()
        p1, p2 = _patched(api)
        with p1, p2:
            result = await query_project_history("proj-1", "decisions", Mock())

        assert [d["decision_id"] for d in result["data"]] == [
            "dec-eir",
            "op-bly",
            "primer",
        ]
        assert result["count"] == 3

    @pytest.mark.asyncio
    async def test_kind_combines_with_the_agent_filter(self) -> None:
        """``agent_id`` picks the read; ``kind`` still narrows it."""
        api = _query_api()
        p1, p2 = _patched(api)
        with p1, p2:
            result = await query_project_history(
                "proj-1", "decisions", Mock(), agent_id="Eir", kind="decision"
            )

        api.find_decisions_by_agent.assert_awaited_once_with("proj-1", "Eir")
        assert [d["decision_id"] for d in result["data"]] == ["dec-eir"]

    @pytest.mark.asyncio
    async def test_a_row_without_a_kind_counts_as_a_decision(self) -> None:
        """Rows written before ``kind`` existed are decisions (the default)."""
        api = _query_api()
        legacy = SimpleNamespace(to_dict=lambda: {"decision_id": "old"})
        api.get_project_history = AsyncMock(
            return_value=SimpleNamespace(decisions=[legacy, _row("op", "assessment")])
        )
        p1, p2 = _patched(api)
        with p1, p2:
            result = await query_project_history(
                "proj-1", "decisions", Mock(), kind="decision"
            )

        assert [d["decision_id"] for d in result["data"]] == ["old"]


class TestTheWrapperForwardsKindForDecisions:
    """The HTTP wrapper already names ``kind``; it must reach this branch too."""

    @pytest.mark.asyncio
    async def test_kind_reaches_the_implementation(self) -> None:
        """``kind`` is forwarded for a decisions read, not only for opinions."""
        from mcp.server.fastmcp import FastMCP

        from src.marcus_mcp.server import MarcusServer

        server: Any = MarcusServer.__new__(MarcusServer)
        app = FastMCP("test-marcus-agent")
        server._register_endpoint_tools(app, "agent")

        impl = AsyncMock(return_value={"success": True, "data": []})
        with patch("src.marcus_mcp.tools.history.query_project_history", impl):
            await app.call_tool(
                "query_project_history",
                {"project_id": "p1", "query_type": "decisions", "kind": "decision"},
            )

        kwargs = impl.await_args.kwargs
        assert kwargs["query_type"] == "decisions"
        assert kwargs["kind"] == "decision"
