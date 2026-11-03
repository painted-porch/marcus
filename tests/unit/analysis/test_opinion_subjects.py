"""
Unit tests for grouping opinions by subject (deliberation layer, B2).

``group_opinions_by_subject`` turns the flat list of decisions into one
entry per subject.  The count that matters for corroboration is the
number of DISTINCT agents, not posts, so one agent repeating itself
cannot raise a subject's weight.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from src.analysis.query_api import (
    DECISION_PAGE_SIZE,
    ProjectHistoryQuery,
    group_opinions_by_subject,
)
from src.core.project_history import Decision

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def _opinion(
    decision_id: str,
    agent_id: str,
    subject: str,
    minutes: int = 0,
    task_id: str | None = None,
    kind: str = "assessment",
) -> Decision:
    """Build an opinion-shaped Decision."""
    return Decision(
        decision_id=decision_id,
        task_id=task_id,
        agent_id=agent_id,
        timestamp=T0 + timedelta(minutes=minutes),
        what=f"{subject} observed",
        why="seen in the world",
        impact="unknown",
        subject=subject,
        kind=kind,
        project_id="p1",
    )


class TestGroupOpinionsBySubject:
    """Test suite for the subject grouping."""

    def test_counts_distinct_agents_not_posts(self) -> None:
        """Three agents, one of them twice, is a count of three."""
        decisions = [
            _opinion("d1", "agent-2", "farm.flooded", 0),
            _opinion("d2", "agent-5", "farm.flooded", 1),
            _opinion("d3", "agent-7", "farm.flooded", 2),
            _opinion("d4", "agent-2", "farm.flooded", 3),
        ]

        [entry] = group_opinions_by_subject(decisions)

        assert entry["subject"] == "farm.flooded"
        assert entry["count"] == 3
        assert entry["opinion_count"] == 4
        assert entry["agents"] == ["agent-2", "agent-5", "agent-7"]

    def test_entries_are_ordered_by_count_then_recency(self) -> None:
        """The most corroborated subject comes first."""
        decisions = [
            _opinion("d1", "agent-2", "mobs.north", 0),
            _opinion("d2", "agent-5", "farm.flooded", 1),
            _opinion("d3", "agent-7", "farm.flooded", 2),
            _opinion("d4", "agent-9", "torches.missing", 9),
        ]

        entries = group_opinions_by_subject(decisions)

        assert [e["subject"] for e in entries] == [
            "farm.flooded",
            "torches.missing",
            "mobs.north",
        ]

    def test_architectural_decisions_are_not_opinions(self) -> None:
        """kind=decision rows (and rows with no subject) stay out of the list."""
        decisions = [
            _opinion("d1", "agent-2", "farm.flooded", 0),
            _opinion("d2", "agent-5", "schema.users", 1, kind="decision"),
            Decision(
                decision_id="d3",
                task_id="t1",
                agent_id="agent-7",
                timestamp=T0,
                what="chose postgres",
                why="acid",
                impact="data",
            ),
        ]

        entries = group_opinions_by_subject(decisions)

        assert [e["subject"] for e in entries] == ["farm.flooded"]

    def test_kind_filter_can_select_decisions_instead(self) -> None:
        """Asking for kind=decision groups the architectural decisions."""
        decisions = [
            _opinion("d1", "agent-2", "farm.flooded", 0),
            _opinion("d2", "agent-5", "schema.users", 1, kind="decision"),
        ]

        entries = group_opinions_by_subject(decisions, kind="decision")

        assert [e["subject"] for e in entries] == ["schema.users"]
        assert entries[0]["kind"] == "decision"

    def test_subject_filter_returns_only_that_subject(self) -> None:
        """A subject filter narrows to one entry."""
        decisions = [
            _opinion("d1", "agent-2", "farm.flooded", 0),
            _opinion("d2", "agent-5", "mobs.north", 1),
        ]

        entries = group_opinions_by_subject(decisions, subject="mobs.north")

        assert [e["subject"] for e in entries] == ["mobs.north"]

    def test_since_filter_drops_older_opinions(self) -> None:
        """Opinions before ``since`` do not count."""
        decisions = [
            _opinion("d1", "agent-2", "farm.flooded", 0),
            _opinion("d2", "agent-5", "farm.flooded", 30),
        ]

        [entry] = group_opinions_by_subject(
            decisions, since=(T0 + timedelta(minutes=10)).isoformat()
        )

        assert entry["count"] == 1
        assert entry["agents"] == ["agent-5"]

    def test_entry_carries_window_tasks_and_opinions_newest_first(self) -> None:
        """first_seen, last_seen, task_ids and the opinions themselves."""
        decisions = [
            _opinion("d1", "agent-2", "farm.flooded", 0, task_id="t1"),
            _opinion("d2", "agent-5", "farm.flooded", 5),
            _opinion("d3", "agent-7", "farm.flooded", 2, task_id="t2"),
        ]

        [entry] = group_opinions_by_subject(decisions)

        assert entry["first_seen"] == T0.isoformat()
        assert entry["last_seen"] == (T0 + timedelta(minutes=5)).isoformat()
        assert entry["task_ids"] == ["t1", "t2"]
        assert [o["decision_id"] for o in entry["opinions"]] == ["d2", "d3", "d1"]
        assert entry["opinions"][0]["what"] == "farm.flooded observed"

    def test_empty_input_is_an_empty_list(self) -> None:
        """No opinions means no entries, not an error."""
        assert group_opinions_by_subject([]) == []


class TestSummarizeSubjectsReadsEveryPage:
    """Test suite for the read behind ``query_project_history("opinions")``.

    ``load_decisions`` pages at ``DECISION_PAGE_SIZE`` rows.  Grouping only
    the first page would undercount corroboration on a large project and
    could drop subjects entirely (Codex P2 on PR #800), so the read must
    walk every page before it groups.
    """

    @pytest.mark.asyncio
    async def test_counts_opinions_beyond_the_first_page(self) -> None:
        """One more opinion than a page holds is still counted."""
        total = DECISION_PAGE_SIZE + 1
        rows = [
            _opinion(f"d{i}", f"agent-{i}", "farm.flooded", i) for i in range(total)
        ]

        async def load_decisions(
            project_id: str, limit: int = DECISION_PAGE_SIZE, offset: int = 0
        ) -> list[Decision]:
            return rows[offset : offset + limit]

        aggregator = Mock()
        aggregator.history_persistence.load_decisions = AsyncMock(
            side_effect=load_decisions
        )

        [entry] = await ProjectHistoryQuery(aggregator).summarize_subjects("p1")

        assert entry["count"] == total
        assert entry["opinion_count"] == total
        offsets = [
            call.kwargs["offset"]
            for call in aggregator.history_persistence.load_decisions.await_args_list
        ]
        assert offsets == [0, DECISION_PAGE_SIZE, 2 * DECISION_PAGE_SIZE]

    @pytest.mark.asyncio
    async def test_small_project_reads_one_page_and_stops(self) -> None:
        """A project under one page costs one data page plus the empty check.

        The loop stops on the first EMPTY page, not the first short one,
        because ``load_decisions`` skips malformed rows and can return a
        short page with more rows still behind it.
        """
        rows = [_opinion("d1", "agent-1", "farm.flooded", 0)]
        aggregator = Mock()
        aggregator.history_persistence.load_decisions = AsyncMock(
            side_effect=lambda project_id, limit, offset: rows[offset : offset + limit]
        )

        entries = await ProjectHistoryQuery(aggregator).summarize_subjects("p1")

        assert [e["count"] for e in entries] == [1]
        assert aggregator.history_persistence.load_decisions.await_count == 2
