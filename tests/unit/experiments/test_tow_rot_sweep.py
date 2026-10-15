"""
Unit tests for the link-rot and crawler-block sweep (#737, step 10).

Before any model call, the sweep fetches the pilot's ground-truth
URLs with the verifier's own fetcher and robots policy, and records
what even a perfect finder could verify. The decline rate is later
reported raw and net of this baseline, and the 50 percent no-go
threshold is read net of it, so dataset rot cannot be mistaken for
a verifier or record failure.
"""

from typing import Optional

import pytest
from runners.tow_fetch import FetchResult
from runners.tow_rot_sweep import classify_fetch, summarize

pytestmark = pytest.mark.unit

EXCERPT = "Some good news is that this storm should make a lasting impact"


def _fetch(
    status: Optional[int],
    text: str = "",
    robots_blocked: bool = False,
) -> FetchResult:
    """Build one fetch outcome."""
    return FetchResult(
        url="https://example.org/a",
        final_url="https://example.org/a",
        status=status,
        html="<html></html>" if status == 200 else "",
        text=text,
        robots_blocked=robots_blocked,
        error="" if status else "dead",
        fetched_at="2026-10-04T00:00:00+00:00",
    )


class TestClassifyFetch:
    """Test suite for the per-URL classification."""

    def test_robots_block_is_crawler_blocked(self) -> None:
        """A polite fetcher refused entry: crawler_blocked."""
        assert (
            classify_fetch(_fetch(None, robots_blocked=True), EXCERPT)
            == "crawler_blocked"
        )

    def test_error_status_is_broken(self) -> None:
        """A dead or erroring URL: broken."""
        assert classify_fetch(_fetch(404), EXCERPT) == "broken"
        assert classify_fetch(_fetch(None), EXCERPT) == "broken"

    def test_bot_wall_statuses_are_blocked_by_server(self) -> None:
        """401, 403, and 429 are the server refusing bots, not rot.

        The real sweep found whole publishers answering 403 to an
        honest fetcher while their robots.txt permits crawling; that
        is crawler blocking in effect and must not be reported as a
        dead link.
        """
        assert classify_fetch(_fetch(403), EXCERPT) == "blocked_by_server"
        assert classify_fetch(_fetch(401), EXCERPT) == "blocked_by_server"
        assert classify_fetch(_fetch(429), EXCERPT) == "blocked_by_server"

    def test_live_page_without_excerpt_is_excerpt_missing(self) -> None:
        """Fetched fine but the excerpt is not in the text."""
        assert (
            classify_fetch(_fetch(200, text="other content"), EXCERPT)
            == "excerpt_missing"
        )

    def test_live_page_with_excerpt_is_verifiable(self) -> None:
        """The happy case: a perfect finder could verify this one."""
        assert (
            classify_fetch(_fetch(200, text=f"intro {EXCERPT} outro"), EXCERPT)
            == "verifiable"
        )


class TestSummarize:
    """Test suite for the baseline arithmetic."""

    def test_counts_and_rates(self) -> None:
        """Known mix: known rates, known ceiling."""
        classes = [
            "verifiable",
            "verifiable",
            "crawler_blocked",
            "blocked_by_server",
            "broken",
            "excerpt_missing",
        ]

        summary = summarize(classes)

        assert summary["total"] == 6
        assert summary["counts"]["verifiable"] == 2
        assert summary["counts"]["blocked_by_server"] == 1
        assert summary["max_verifiable_rate"] == pytest.approx(2 / 6)
        assert summary["baseline_unverifiable_rate"] == pytest.approx(4 / 6)
