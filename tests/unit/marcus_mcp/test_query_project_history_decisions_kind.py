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


class TestTheFilterRunsBeforePagination:
    """Codex on PR #881: filtering a page is not filtering.

    ``get_project_history`` paginates in the store, so filtering the page it
    returns means ``kind="decision"`` with ``limit=10`` filters ten
    already-chosen rows. On a board busy with opinions the newest ten rows
    are assessments and the caller gets an empty list although decisions
    exist, which is worse than a visible failure: the planner and every
    agent read this with a limit, so the record would look like it works
    and show nobody anything.
    """

    @staticmethod
    def _paginating_api(rows: list[Any]) -> Mock:
        """A query API that paginates in the store, as the real one does."""
        api = Mock()

        async def _history(
            project_id: str,
            decision_limit: int = 10000,
            decision_offset: int = 0,
            **_: Any,
        ) -> Any:
            page = rows[decision_offset : decision_offset + decision_limit]
            return SimpleNamespace(decisions=page)

        api.get_project_history = AsyncMock(side_effect=_history)
        return api

    @pytest.mark.asyncio
    async def test_a_filtered_page_finds_decisions_behind_newer_opinions(self) -> None:
        """Twelve opinions, then two decisions: a page of ten finds both."""
        rows = [_row(f"op-{i}", "assessment") for i in range(12)]
        rows += [_row("dec-eir", "decision"), _row("dec-cyn", "decision")]
        api = self._paginating_api(rows)

        p1, p2 = _patched(api)
        with p1, p2:
            result = await query_project_history(
                "proj-1", "decisions", Mock(), kind="decision", limit=10
            )

        assert [d["decision_id"] for d in result["data"]] == ["dec-eir", "dec-cyn"]
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_the_offset_counts_matching_rows_only(self) -> None:
        """Paging a filtered read skips decisions, never opinions.

        Otherwise an offset counts non-matching rows and a consumer pages
        straight past the records the filter exists to expose.
        """
        rows = [
            _row("dec-1", "decision"),
            _row("op-1", "assessment"),
            _row("dec-2", "decision"),
            _row("op-2", "assessment"),
            _row("dec-3", "decision"),
        ]
        api = self._paginating_api(rows)

        p1, p2 = _patched(api)
        with p1, p2:
            result = await query_project_history(
                "proj-1", "decisions", Mock(), kind="decision", limit=2, offset=1
            )

        assert [d["decision_id"] for d in result["data"]] == ["dec-2", "dec-3"]

    @pytest.mark.asyncio
    async def test_an_unfiltered_read_still_paginates_in_the_store(self) -> None:
        """Without a kind nothing changes: the store pages, as today."""
        rows = [_row(f"r-{i}", "assessment") for i in range(5)]
        api = self._paginating_api(rows)

        p1, p2 = _patched(api)
        with p1, p2:
            await query_project_history(
                "proj-1", "decisions", Mock(), limit=2, offset=1
            )

        kwargs = api.get_project_history.await_args.kwargs
        assert kwargs["decision_limit"] == 2
        assert kwargs["decision_offset"] == 1


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
