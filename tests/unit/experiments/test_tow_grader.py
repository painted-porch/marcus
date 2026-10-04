"""
Unit tests for the label-blind grader (#737, step 9).

One scoring function applies the six Tow categories (correct;
correct but incomplete; partially incorrect; completely incorrect;
not provided; crawler blocked) to every answer from every condition.
Label-blindness is mechanical: the records entering the scorer carry
no condition or engine field, so the scoring cannot branch on them,
and a blinded worksheet lets a person spot-check without seeing
conditions either. The rates come out per condition afterward by
rejoining on a blind id.
"""

from typing import Any, Dict, Optional

import pytest
from runners.tow_grader import (
    AnswerRecord,
    blind_pool,
    categorize,
    compute_rates,
    urls_match,
)

pytestmark = pytest.mark.unit

TRUTH = {
    "publication": "Boston Globe",
    "date": "12/11/2024",
    "source_url": "https://www.bostonglobe.com/2024/12/11/metro/storm/",
}


class FakeFetcher:
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
            html="",
            text=self.text,
            robots_blocked=False,
            error="" if self.status else "dead",
            fetched_at="2026-10-04T00:00:00+00:00",
        )


def _record(
    answer: Optional[Dict[str, str]],
    declined: bool = False,
    robots_blocked: bool = False,
) -> AnswerRecord:
    """Build one answer record for scoring."""
    return AnswerRecord(
        case_id="tow-0001",
        condition="B",
        engine="perplexity",
        answer=answer,
        declined=declined,
        robots_blocked=robots_blocked,
    )


class TestUrlsMatch:
    """Test suite for URL comparison."""

    def test_scheme_www_slash_and_query_do_not_matter(self) -> None:
        """Cosmetic URL differences are not wrongness."""
        assert urls_match(
            "http://bostonglobe.com/2024/12/11/metro/storm/?ref=x",
            TRUTH["source_url"],
        )

    def test_different_paths_do_not_match(self) -> None:
        """A different article is a different URL."""
        assert not urls_match(
            "https://www.bostonglobe.com/other-article/", TRUTH["source_url"]
        )


class TestCategorize:
    """Test suite for the six Tow categories."""

    def test_all_attributes_correct_is_correct(self) -> None:
        """Publisher, date, and URL right: correct."""
        record = _record(
            {
                "headline": "whatever",
                "publisher": "The Boston Globe",
                "date": "December 11, 2024",
                "url": TRUTH["source_url"],
            }
        )
        assert categorize(record, TRUTH) == "correct"

    def test_missing_attribute_with_no_wrong_one_is_incomplete(self) -> None:
        """Right but missing the date: correct but incomplete."""
        record = _record(
            {
                "headline": "",
                "publisher": "Boston Globe",
                "date": "",
                "url": TRUTH["source_url"],
            }
        )
        assert categorize(record, TRUTH) == "correct but incomplete"

    def test_mixed_right_and_wrong_is_partially_incorrect(self) -> None:
        """Right URL but wrong publisher: partially incorrect."""
        record = _record(
            {
                "headline": "x",
                "publisher": "New York Times",
                "date": "12/11/2024",
                "url": TRUTH["source_url"],
            }
        )
        assert categorize(record, TRUTH) == "partially incorrect"

    def test_everything_wrong_is_completely_incorrect(self) -> None:
        """Nothing matches: completely incorrect."""
        record = _record(
            {
                "headline": "x",
                "publisher": "Example News",
                "date": "1/1/2020",
                "url": "https://example.com/else",
            }
        )
        assert categorize(record, TRUTH) == "completely incorrect"

    def test_declined_is_not_provided(self) -> None:
        """An honest decline scores as not provided."""
        record = _record(None, declined=True)
        assert categorize(record, TRUTH) == "not provided"

    def test_robots_blocked_decline_is_crawler_blocked(self) -> None:
        """A decline caused by robots.txt gets its own category."""
        record = _record(None, declined=True, robots_blocked=True)
        assert categorize(record, TRUTH) == "crawler blocked"


class TestBlindPool:
    """Test suite for mechanical label-blindness."""

    def test_blinded_records_carry_no_condition_or_engine(self) -> None:
        """The scorer's view has answers and nothing to cheat with."""
        records = [
            _record({"publisher": "Boston Globe", "date": "", "url": ""}),
            AnswerRecord(
                case_id="tow-0002",
                condition="A",
                engine="openai",
                answer=None,
                declined=True,
            ),
        ]

        pool = blind_pool(records, seed=737)

        assert len(pool) == 2
        for blind_id, payload in pool:
            assert isinstance(blind_id, str)
            assert "condition" not in payload
            assert "engine" not in payload

    def test_blinding_is_deterministic_per_seed(self) -> None:
        """The same seed shuffles the worksheet the same way."""
        records = [
            _record({"publisher": str(i), "date": "", "url": ""}) for i in range(5)
        ]

        first = [bid for bid, _ in blind_pool(records, seed=737)]
        second = [bid for bid, _ in blind_pool(records, seed=737)]

        assert first == second


class TestComputeRates:
    """Test suite for the five emitted rates."""

    def test_rates_from_a_hand_built_set(self) -> None:
        """Two correct, one wrong, one declined: known arithmetic."""
        scored = [
            ("correct", False),
            ("correct but incomplete", False),
            ("partially incorrect", False),
            ("not provided", True),
        ]
        costs = {"total_cost_usd": 0.08, "cases": 4}

        rates = compute_rates(scored, costs)

        assert rates["cases"] == 4
        assert rates["decline_rate"] == 0.25
        assert rates["correct_rate"] == 0.5
        # Confident-wrong: wrong among the 3 NON-declined answers.
        assert rates["confident_wrong_rate"] == pytest.approx(1 / 3)
        assert rates["cost_per_case_usd"] == pytest.approx(0.02)

    def test_all_declined_has_no_confident_wrong(self) -> None:
        """With zero kept answers the confident-wrong rate is None."""
        scored = [("not provided", True), ("crawler blocked", True)]

        rates = compute_rates(scored, {"total_cost_usd": 0.0, "cases": 2})

        assert rates["decline_rate"] == 1.0
        assert rates["confident_wrong_rate"] is None


class TestFabricatedUrl:
    """Test suite for the fabricated-URL flag."""

    def test_dead_url_is_fabricated(self) -> None:
        """A claimed URL that 404s is fabricated or broken."""
        from runners.tow_grader import url_is_fabricated

        record = _record(
            {"publisher": "Boston Globe", "date": "", "url": "https://x.org/gone"}
        )

        assert (
            url_is_fabricated(record, TRUTH, FakeFetcher(status=404), "excerpt") is True
        )

    def test_right_url_is_not_fabricated(self) -> None:
        """The ground-truth URL is never flagged, whatever the page says."""
        from runners.tow_grader import url_is_fabricated

        record = _record({"publisher": "", "date": "", "url": TRUTH["source_url"]})

        assert (
            url_is_fabricated(record, TRUTH, FakeFetcher(status=200), "excerpt")
            is False
        )

    def test_live_wrong_page_without_excerpt_is_fabricated(self) -> None:
        """A live page that is not the article counts as the wrong page."""
        from runners.tow_grader import url_is_fabricated

        record = _record(
            {"publisher": "", "date": "", "url": "https://x.org/some-other-page"}
        )
        fetcher = FakeFetcher(status=200, text="unrelated content entirely")

        assert url_is_fabricated(record, TRUTH, fetcher, "the real excerpt") is True
