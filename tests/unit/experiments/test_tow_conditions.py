"""
Unit tests for the Condition A and C runners' pure parts (#737, step 9).

Condition A is one engine request per excerpt with no board; its
runner is paid-gated and only the row shape is tested. Condition C
is the same verifier applied to A's answers as a post-hoc filter
with no board, sharing the fetch cache so both conditions see
identical observations. No test touches the network or any engine.
"""

from typing import Any, Optional

import pytest
from runners.tow_condition_a import build_a_row
from runners.tow_condition_c import filter_answer
from runners.tow_engines import EngineAnswer

pytestmark = pytest.mark.unit

EXCERPT = "Some good news is that this storm should make a lasting impact"


class FakeCache:
    """Scripted stand-in for the shared fetch cache."""

    def __init__(self, status: Optional[int] = 200, text: str = "") -> None:
        self.status = status
        self.text = text

    def fetch(self, url: str) -> Any:
        from runners.tow_fetch import FetchResult

        return FetchResult(
            url=url,
            final_url=url,
            status=self.status,
            html="<html></html>",
            text=self.text,
            robots_blocked=False,
            error="",
            fetched_at="2026-10-04T00:00:00+00:00",
        )


class TestBuildARow:
    """Test suite for the Condition A record shape."""

    def test_row_carries_claim_usage_and_identity(self) -> None:
        """The grader's A loader reads exactly these fields."""
        answer = EngineAnswer(
            headline="H",
            publisher="P",
            date="12/11/2024",
            url="https://example.org/a",
            raw="RAW",
            model="sonar",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.00015,
        )

        row = build_a_row("tow-0001", "perplexity", answer)

        assert row["case_id"] == "tow-0001"
        assert row["engine"] == "perplexity"
        assert row["url"] == "https://example.org/a"
        assert row["raw"] == "RAW"
        assert row["cost_usd"] == 0.00015
        assert "ts" in row


class TestFilterAnswer:
    """Test suite for the Condition C keep-or-drop filter."""

    def _a_row(self, url: str = "https://example.org/a") -> dict:
        return {
            "case_id": "tow-0001",
            "engine": "perplexity",
            "headline": "H",
            "publisher": "Example",
            "date": "12/11/2024",
            "url": url,
            "raw": "RAW",
            "cost_usd": 0.0001,
        }

    def test_verified_answer_is_kept_without_raw(self) -> None:
        """A page carrying the excerpt keeps the answer, raw stripped."""
        cache = FakeCache(status=200, text=f"intro {EXCERPT} outro")

        result = filter_answer(self._a_row(), EXCERPT, cache)

        assert result["kept"] is True
        assert result["verdict"] == "verified"
        assert result["answer"]["url"] == "https://example.org/a"
        assert "raw" not in result["answer"]

    def test_contradicted_answer_is_dropped(self) -> None:
        """A live page without the excerpt drops the answer."""
        cache = FakeCache(status=200, text="unrelated page")

        result = filter_answer(self._a_row(), EXCERPT, cache)

        assert result["kept"] is False
        assert result["verdict"] == "contradicted"
        assert result["answer"] is None

    def test_dead_url_is_dropped_as_unverifiable(self) -> None:
        """A broken URL cannot be kept."""
        cache = FakeCache(status=404)

        result = filter_answer(self._a_row(), EXCERPT, cache)

        assert result["kept"] is False
        assert result["verdict"] == "unverifiable"

    def test_cost_rides_along_for_the_grader(self) -> None:
        """C's cost is A's cost; the filter itself is free."""
        cache = FakeCache(status=200, text=EXCERPT)

        result = filter_answer(self._a_row(), EXCERPT, cache)

        assert result["cost_usd"] == 0.0001
