"""
Unit tests for filtering decisions by kind in the store (#880, #881 review).

Opinions, the project primer and the crew's decisions all live in the
``decisions`` collection, told apart by ``kind``. A reader that wants the
decisions alone (the Minecraft pull bundle, the planner's context) asks for
``kind="decision"``.

Filtering after the store has paged is not filtering: the store's limit is a
PAGE limit, so a Python filter over one page makes every older matching row
permanently invisible. Ten thousand opinions followed by one decision
returned nothing at any offset (Codex on PR #881). The predicate therefore
goes into the query, beside the project filter that is there for the same
reason (Codex P2 on PR #800), so the store pages over matching rows only.
"""

from pathlib import Path
from typing import Any, Optional

import pytest

from src.core.project_history import ProjectHistoryPersistence

pytestmark = pytest.mark.unit


def _row(
    decision_id: str, kind: Optional[str], project_id: str = "p1"
) -> dict[str, Any]:
    """A stored decision row; ``kind=None`` writes no ``kind`` field at all."""
    row = {
        "decision_id": decision_id,
        "task_id": None,
        "agent_id": "Eir",
        "timestamp": "2026-11-06T13:00:00+00:00",
        "what": "w",
        "why": "y",
        "impact": "i",
        "project_id": project_id,
        "subject": "wood.shortage",
    }
    if kind is not None:
        row["kind"] = kind
    return row


async def _store(
    tmp_path: Path, rows: list[dict[str, Any]]
) -> ProjectHistoryPersistence:
    """Write ``rows`` oldest first, so the LAST row is the newest."""
    history = ProjectHistoryPersistence(marcus_root=tmp_path)
    for index, row in enumerate(rows):
        await history._backend.store("decisions", f"dec_{index:05d}", row)
    return history


class TestTheKindFilterRunsInTheStore:
    """A matching row behind many newer non-matching rows is still found."""

    @pytest.mark.asyncio
    async def test_a_decision_behind_a_full_page_of_opinions_is_found(
        self, tmp_path: Path
    ) -> None:
        """The decision is written first, so 40 opinions sit in front of it.

        With the predicate in the store, a page of ten still returns it.
        """
        rows = [_row("dec-eir", "decision")]
        rows += [_row(f"op-{i}", "assessment") for i in range(40)]
        history = await _store(tmp_path, rows)

        found = await history.load_decisions("p1", limit=10, kind="decision")

        assert [d.decision_id for d in found] == ["dec-eir"]

    @pytest.mark.asyncio
    async def test_the_offset_pages_over_matching_rows_only(
        self, tmp_path: Path
    ) -> None:
        """Paging a filtered read skips decisions, never the opinions between."""
        rows: list[dict[str, Any]] = []
        for i in range(3):
            rows.append(_row(f"dec-{i}", "decision"))
            rows += [_row(f"op-{i}-{j}", "assessment") for j in range(5)]
        history = await _store(tmp_path, rows)

        page = await history.load_decisions("p1", limit=2, offset=1, kind="decision")

        # Newest first: dec-2, dec-1, dec-0; offset 1 starts at dec-1.
        assert [d.decision_id for d in page] == ["dec-1", "dec-0"]

    @pytest.mark.asyncio
    async def test_a_row_without_a_kind_is_a_decision(self, tmp_path: Path) -> None:
        """Rows written before ``kind`` existed load as decisions, as they do today.

        ``Decision.from_dict`` already defaults them to ``kind="decision"``, so
        the store filter must match a null or absent field too, or the filter
        would hide exactly the rows the unfiltered read calls decisions.
        """
        history = await _store(
            tmp_path, [_row("legacy", None), _row("op", "assessment")]
        )

        found = await history.load_decisions("p1", limit=10, kind="decision")

        assert [d.decision_id for d in found] == ["legacy"]

    @pytest.mark.asyncio
    async def test_the_primer_is_not_a_decision(self, tmp_path: Path) -> None:
        """``kind="objective"`` is the project primer, not something an agent chose."""
        history = await _store(
            tmp_path, [_row("primer", "objective"), _row("dec", "decision")]
        )

        found = await history.load_decisions("p1", limit=10, kind="decision")

        assert [d.decision_id for d in found] == ["dec"]

    @pytest.mark.asyncio
    async def test_assessments_can_be_asked_for_too(self, tmp_path: Path) -> None:
        """The filter is general: the opinions read can use the same path."""
        history = await _store(
            tmp_path, [_row("op", "assessment"), _row("dec", "decision")]
        )

        found = await history.load_decisions("p1", limit=10, kind="assessment")

        assert [d.decision_id for d in found] == ["op"]

    @pytest.mark.asyncio
    async def test_no_kind_loads_every_row(self, tmp_path: Path) -> None:
        """Without a kind the read is unchanged: opinions, primer and decisions."""
        history = await _store(
            tmp_path,
            [
                _row("op", "assessment"),
                _row("primer", "objective"),
                _row("dec", "decision"),
            ],
        )

        found = await history.load_decisions("p1", limit=10)

        assert {d.decision_id for d in found} == {"op", "primer", "dec"}

    @pytest.mark.asyncio
    async def test_another_projects_rows_are_still_excluded(
        self, tmp_path: Path
    ) -> None:
        """The kind filter is ANDed with the project filter, not instead of it."""
        history = await _store(
            tmp_path,
            [_row("mine", "decision"), _row("theirs", "decision", project_id="p2")],
        )

        found = await history.load_decisions("p1", limit=10, kind="decision")

        assert [d.decision_id for d in found] == ["mine"]


class TestTheFilterReachesTheStoredQuery:
    """The predicate must be pushed down, not applied in Python afterwards."""

    @pytest.mark.asyncio
    async def test_the_backend_receives_the_kind_in_its_where_clause(
        self, tmp_path: Path
    ) -> None:
        """The backend sees ``kind`` so SQLite can filter before its limit."""
        seen: list[dict[str, Any]] = []
        history = await _store(tmp_path, [_row("dec", "decision")])
        real_query = history._backend.query

        async def _spy(collection: str, *args: Any, **kwargs: Any) -> Any:
            if kwargs.get("where"):
                seen.append(dict(kwargs["where"]))
            return await real_query(collection, *args, **kwargs)

        history._backend.query = _spy  # type: ignore[method-assign]

        await history.load_decisions("p1", limit=5, kind="decision")

        assert seen, "the read went through a where clause"
        assert any("kind" in where for where in seen)

    @pytest.mark.asyncio
    async def test_the_sql_filters_on_the_kind_field(self, tmp_path: Path) -> None:
        """The clause reaches SQL as a json_extract test, so the page is
        matching rows.

        Issue #890 round 2: the clause folds a missing ``kind`` onto
        the literal ``"decision"`` via ``COALESCE`` rather than an
        ``OR ... IS NULL`` branch, so this is a single equality the
        new index can seek on -- a disjunction over an indexed
        expression cannot be. Correctness for the legacy (no ``kind``
        field at all) row is covered separately by
        ``tests/unit/core/test_persistence_decisions_index.py``.
        """
        from src.core.persistence import SQLitePersistence

        backend = SQLitePersistence(tmp_path / "marcus.db")
        clause, params = backend._where_sql({"kind": ["decision", None]})

        assert "json_extract(data, '$.kind')" in clause
        assert "COALESCE(" in clause
        assert "decision" in params

    @pytest.mark.asyncio
    async def test_the_filtered_page_is_not_cut_from_an_unfiltered_page(
        self, tmp_path: Path
    ) -> None:
        """The regression itself: one decision behind more rows than a page holds.

        Fifteen opinions in front of one decision, with a page size of ten.
        Filtering an already-paginated page returns nothing here; filtering in
        the store returns the decision.
        """
        rows = [_row("deep-decision", "decision")]
        rows += [_row(f"op-{i}", "assessment") for i in range(15)]
        history = await _store(tmp_path, rows)

        found = await history.load_decisions("p1", limit=10, kind="decision")

        assert [d.decision_id for d in found] == ["deep-decision"]


class TestTheToolPassesTheKindDown:
    """``query_project_history`` must hand the predicate to the store."""

    @pytest.mark.asyncio
    async def test_the_decisions_query_asks_the_store_to_filter(self) -> None:
        """No Python post-filter and no scan cap: the store does the work."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch

        api = Mock()
        api.get_project_history = AsyncMock(return_value=SimpleNamespace(decisions=[]))

        with (
            patch("src.marcus_mcp.tools.history.ProjectHistoryAggregator"),
            patch("src.marcus_mcp.tools.history.ProjectHistoryQuery", return_value=api),
        ):
            from src.marcus_mcp.tools.history import query_project_history

            await query_project_history(
                "p1", "decisions", Mock(), kind="decision", limit=10, offset=20
            )

        kwargs = api.get_project_history.await_args.kwargs
        assert kwargs["decision_kind"] == "decision"
        assert kwargs["decision_limit"] == 10, "the tool keeps its plain limit"
        assert kwargs["decision_offset"] == 20, "and its plain offset"


class TestTheCacheKnowsAboutTheFilter:
    """A filtered read must not be served an unfiltered cached history.

    The aggregator cached on ``project_id`` alone, so it already served one
    page's history for another page's request. With a kind filter that
    staleness would hand a caller opinions labelled as decisions, which is the
    silent wrongness this whole change exists to avoid, so the key now carries
    every parameter that changes the result.
    """

    @pytest.mark.asyncio
    async def test_a_filtered_read_is_not_served_the_unfiltered_history(
        self, tmp_path: Path
    ) -> None:
        """Read everything, then read decisions: the second is not the first."""
        from src.analysis.aggregator import ProjectHistoryAggregator

        history = await _store(
            tmp_path, [_row("op", "assessment"), _row("dec", "decision")]
        )
        aggregator = ProjectHistoryAggregator(marcus_root=tmp_path)
        aggregator.history_persistence = history

        everything = await aggregator.aggregate_project(
            "p1", include_conversations=False
        )
        decisions_only = await aggregator.aggregate_project(
            "p1", include_conversations=False, decision_kind="decision"
        )

        assert {d.decision_id for d in everything.decisions} == {"op", "dec"}
        assert [d.decision_id for d in decisions_only.decisions] == ["dec"]

    @pytest.mark.asyncio
    async def test_two_pages_are_not_served_each_others_history(
        self, tmp_path: Path
    ) -> None:
        """The same staleness, for the pagination that was already there."""
        from src.analysis.aggregator import ProjectHistoryAggregator

        history = await _store(tmp_path, [_row(f"d-{i}", "decision") for i in range(3)])
        aggregator = ProjectHistoryAggregator(marcus_root=tmp_path)
        aggregator.history_persistence = history

        first = await aggregator.aggregate_project(
            "p1", include_conversations=False, decision_limit=1, decision_offset=0
        )
        second = await aggregator.aggregate_project(
            "p1", include_conversations=False, decision_limit=1, decision_offset=1
        )

        assert [d.decision_id for d in first.decisions] == ["d-2"]
        assert [d.decision_id for d in second.decisions] == ["d-1"]


def test_the_scan_cap_is_gone() -> None:
    """The workaround is removed, not left beside the real fix."""
    import src.marcus_mcp.tools.history as history_tool

    source = Path(history_tool.__file__).read_text()

    assert "_DECISION_SCAN_CAP" not in source
