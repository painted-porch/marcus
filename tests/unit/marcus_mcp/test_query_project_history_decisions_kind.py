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
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.marcus_mcp.tools.history import query_project_history

pytestmark = pytest.mark.unit


def _row(decision_id: str, kind: Optional[str]) -> Any:
    """Build a decision-like row with a ``kind`` and a ``to_dict``.

    ``kind=None`` builds a row with no ``kind`` attribute, the shape a row
    written before the field existed loads as.
    """
    data = {"decision_id": decision_id, "kind": kind, "subject": "wood.shortage"}
    if kind is None:
        return SimpleNamespace(to_dict=lambda: {"decision_id": decision_id})
    return SimpleNamespace(kind=kind, to_dict=lambda: dict(data))


def _mixed() -> list[Any]:
    """One decision, one opinion, one primer, newest first."""
    return [
        _row("dec-eir", "decision"),
        _row("op-bly", "assessment"),
        _row("primer", "objective"),
    ]


def _query_api(rows: Optional[list[Any]] = None) -> Mock:
    """A query API that filters and pages the way the real store does.

    ``get_project_history`` honours ``decision_kind`` in the query, as the
    store does since #881, and ``kind="decision"`` also matches a row with
    no kind at all. The unpaginated reads (by task, by agent, by affected
    task) return everything and are filtered by the tool.
    """
    api = Mock()
    all_rows = _mixed() if rows is None else rows

    async def _history(
        project_id: str,
        decision_limit: int = 10000,
        decision_offset: int = 0,
        decision_kind: Optional[str] = None,
        **_: Any,
    ) -> Any:
        matching = [
            r
            for r in all_rows
            if decision_kind is None or getattr(r, "kind", "decision") == decision_kind
        ]
        page = matching[decision_offset : decision_offset + decision_limit]
        return SimpleNamespace(decisions=page)

    api.get_project_history = AsyncMock(side_effect=_history)
    api.find_decisions_by_agent = AsyncMock(return_value=list(all_rows))
    api.find_decisions_by_task = AsyncMock(return_value=list(all_rows))
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
        """Rows written before ``kind`` existed are decisions (the default).

        Driven through the by-agent read, which returns its whole matching
        set and is filtered here; the same rule in the store has its own
        test in ``tests/unit/core/test_load_decisions_kind.py``.
        """
        api = _query_api([_row("old", None), _row("op", "assessment")])
        p1, p2 = _patched(api)
        with p1, p2:
            result = await query_project_history(
                "proj-1", "decisions", Mock(), agent_id="Eir", kind="decision"
            )

        assert [d["decision_id"] for d in result["data"]] == ["old"]


class TestTheStoreDoesTheFiltering:
    """Codex on PR #881: filtering a page is not filtering.

    ``limit`` is a PAGE limit, so a Python filter over the page the store
    returns makes every older matching row permanently invisible: ten
    thousand opinions in front of one decision returned nothing at any
    offset. The planner and every agent read this with a limit, so the
    record would have looked like it worked and shown nobody anything.

    The predicate therefore goes to the store, which is what these tests
    pin at the tool's edge; the store's own behaviour is tested in
    ``tests/unit/core/test_load_decisions_kind.py``.
    """

    @pytest.mark.asyncio
    async def test_the_kind_is_handed_to_the_store_with_a_plain_page(self) -> None:
        """No scan cap and no post-filter: the limit and offset pass straight through."""
        api = _query_api()
        p1, p2 = _patched(api)
        with p1, p2:
            await query_project_history(
                "proj-1", "decisions", Mock(), kind="decision", limit=10, offset=20
            )

        kwargs = api.get_project_history.await_args.kwargs
        assert kwargs["decision_kind"] == "decision"
        assert kwargs["decision_limit"] == 10
        assert kwargs["decision_offset"] == 20

    @pytest.mark.asyncio
    async def test_a_page_of_ten_finds_decisions_behind_twelve_opinions(self) -> None:
        """The regression, end to end through the tool against a filtering store."""
        rows = [_row(f"op-{i}", "assessment") for i in range(12)]
        rows += [_row("dec-eir", "decision"), _row("dec-cyn", "decision")]
        api = _query_api(rows)

        p1, p2 = _patched(api)
        with p1, p2:
            result = await query_project_history(
                "proj-1", "decisions", Mock(), kind="decision", limit=10
            )

        assert [d["decision_id"] for d in result["data"]] == ["dec-eir", "dec-cyn"]
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_the_offset_counts_matching_rows_only(self) -> None:
        """Paging a filtered read skips decisions, never the opinions between."""
        rows = [
            _row("dec-1", "decision"),
            _row("op-1", "assessment"),
            _row("dec-2", "decision"),
            _row("op-2", "assessment"),
            _row("dec-3", "decision"),
        ]
        api = _query_api(rows)

        p1, p2 = _patched(api)
        with p1, p2:
            result = await query_project_history(
                "proj-1", "decisions", Mock(), kind="decision", limit=2, offset=1
            )

        assert [d["decision_id"] for d in result["data"]] == ["dec-2", "dec-3"]

    @pytest.mark.asyncio
    async def test_an_unfiltered_read_asks_for_no_kind(self) -> None:
        """Without a kind nothing changes: the store pages every row, as today."""
        api = _query_api([_row(f"r-{i}", "assessment") for i in range(5)])

        p1, p2 = _patched(api)
        with p1, p2:
            await query_project_history(
                "proj-1", "decisions", Mock(), limit=2, offset=1
            )

        kwargs = api.get_project_history.await_args.kwargs
        assert kwargs["decision_limit"] == 2
        assert kwargs["decision_offset"] == 1
        assert kwargs["decision_kind"] is None


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
