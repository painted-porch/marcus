"""
Unit tests for the weekly GitHub traffic snapshot tool.

GitHub's traffic API keeps only a rolling 14 days of views and
clones, so weekly snapshots overlap and the same day arrives in
two consecutive runs.  The merge must dedupe by day and never let
a mid-day undercount overwrite a later, larger count.
"""

import pytest
from metrics.snapshot_traffic import merge_daily

pytestmark = pytest.mark.unit


class TestMergeDaily:
    """Test suite for merging overlapping daily traffic rows."""

    def test_new_days_are_appended_in_timestamp_order(self) -> None:
        """Days unseen by earlier snapshots are added, sorted by day."""
        existing = [{"timestamp": "2026-10-12T00:00:00Z", "count": 5, "uniques": 3}]
        new = [
            {"timestamp": "2026-10-14T00:00:00Z", "count": 9, "uniques": 4},
            {"timestamp": "2026-10-13T00:00:00Z", "count": 2, "uniques": 1},
        ]

        merged = merge_daily(existing, new)

        assert [r["timestamp"] for r in merged] == [
            "2026-10-12T00:00:00Z",
            "2026-10-13T00:00:00Z",
            "2026-10-14T00:00:00Z",
        ]

    def test_overlapping_day_keeps_the_larger_count(self) -> None:
        """A later snapshot of the same day wins only if it counted more."""
        existing = [{"timestamp": "2026-10-12T00:00:00Z", "count": 50, "uniques": 20}]
        new = [{"timestamp": "2026-10-12T00:00:00Z", "count": 7, "uniques": 30}]

        merged = merge_daily(existing, new)

        assert merged == [
            {"timestamp": "2026-10-12T00:00:00Z", "count": 50, "uniques": 30}
        ]

    def test_empty_existing_returns_sorted_new_rows(self) -> None:
        """First-ever snapshot merges against nothing."""
        new = [
            {"timestamp": "2026-10-14T00:00:00Z", "count": 1, "uniques": 1},
            {"timestamp": "2026-10-13T00:00:00Z", "count": 4, "uniques": 2},
        ]

        merged = merge_daily([], new)

        assert [r["count"] for r in merged] == [4, 1]
