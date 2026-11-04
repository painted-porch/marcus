"""
Unit tests for recency in the opinions read (deliberation layer).

A pheromone evaporates. Grouping opinions by subject with no notion of
age means a hazard reported an hour ago weighs exactly as much as one
reported a minute ago, and a priority signal built on that cannot tell
"three agents think this now" from "three agents thought it at different
times for different reasons". So the grouped entry carries a windowed
count beside the all-time one, and the window is the caller's choice:
the kernel reports both and holds no policy about which matters.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from src.analysis.query_api import group_opinions_by_subject
from src.core.project_history import Decision

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def _opinion(
    agent: str,
    subject: str,
    minutes_ago: int,
    task_id: Optional[str] = None,
) -> Decision:
    """Build one opinion that was posted `minutes_ago`."""
    return Decision(
        decision_id=f"{agent}-{subject}-{minutes_ago}",
        task_id=task_id,
        agent_id=agent,
        timestamp=NOW - timedelta(minutes=minutes_ago),
        what=f"{subject} seen by {agent}",
        why="",
        impact="",
        subject=subject,
        kind="assessment",
    )


class TestOpinionRecency:
    """Test suite for the windowed count on a grouped subject."""

    def test_all_time_counts_are_unchanged_when_no_window_is_asked_for(self) -> None:
        """The existing reading of count and agents does not move."""
        decisions = [
            _opinion("a", "wood.scarce", 120),
            _opinion("b", "wood.scarce", 90),
            _opinion("c", "wood.scarce", 2),
        ]
        entry = group_opinions_by_subject(decisions)[0]
        assert entry["count"] == 3
        assert entry["agents"] == ["a", "b", "c"]
        assert "recent_count" not in entry

    def test_a_window_reports_who_still_thinks_so(self) -> None:
        """Only agents inside the window count toward the recent figure."""
        decisions = [
            _opinion("a", "wood.scarce", 120),
            _opinion("b", "wood.scarce", 90),
            _opinion("c", "wood.scarce", 2),
            _opinion("d", "wood.scarce", 20),
        ]
        entry = group_opinions_by_subject(decisions, window_minutes=30, now=NOW)[0]
        assert entry["count"] == 4, "the all-time count is still reported"
        assert entry["recent_count"] == 2, "only c and d are inside 30 minutes"
        assert entry["recent_agents"] == ["c", "d"]
        assert entry["window_minutes"] == 30

    def test_a_subject_nobody_has_mentioned_lately_reports_zero_not_absence(
        self,
    ) -> None:
        """A faded subject stays visible with a recent count of zero."""
        decisions = [
            _opinion("a", "farm.flooded", 300),
            _opinion("b", "farm.flooded", 280),
        ]
        entry = group_opinions_by_subject(decisions, window_minutes=30, now=NOW)[0]
        assert entry["count"] == 2
        assert entry["recent_count"] == 0
        assert entry["recent_agents"] == []

    def test_recency_orders_the_read_when_a_window_is_given(self) -> None:
        """A subject two agents raised just now outranks an older three."""
        decisions = [
            _opinion("a", "old.news", 200),
            _opinion("b", "old.news", 190),
            _opinion("c", "old.news", 180),
            _opinion("d", "happening.now", 3),
            _opinion("e", "happening.now", 1),
        ]
        entries = group_opinions_by_subject(decisions, window_minutes=30, now=NOW)
        assert [e["subject"] for e in entries] == ["happening.now", "old.news"]
        assert entries[0]["recent_count"] == 2
        assert entries[1]["recent_count"] == 0

    def test_an_agent_repeating_itself_does_not_raise_the_recent_count(self) -> None:
        """Distinct agents, inside the window as well as over all time."""
        decisions = [
            _opinion("a", "mobs.north", 5),
            _opinion("a", "mobs.north", 4),
            _opinion("a", "mobs.north", 1),
        ]
        entry = group_opinions_by_subject(decisions, window_minutes=30, now=NOW)[0]
        assert entry["recent_count"] == 1
        assert entry["opinion_count"] == 3

    def test_a_window_of_zero_or_less_is_treated_as_no_window(self) -> None:
        """Nonsense windows fall back to the unwindowed read."""
        decisions = [_opinion("a", "s", 1)]
        for window in (0, -5):
            entry = group_opinions_by_subject(
                decisions, window_minutes=window, now=NOW
            )[0]
            assert "recent_count" not in entry, f"window {window}"

    def test_naive_timestamps_are_read_as_utc(self) -> None:
        """A decision stored without a timezone is not silently dropped."""
        decision = _opinion("a", "s", 1)
        decision.timestamp = decision.timestamp.replace(tzinfo=None)
        entry = group_opinions_by_subject([decision], window_minutes=30, now=NOW)[0]
        assert entry["recent_count"] == 1
