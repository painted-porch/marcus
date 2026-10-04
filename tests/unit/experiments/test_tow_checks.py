"""
Unit tests for the verifier's checks and verdict (#737, step 7).

The verifier never calls a model. It checks three things about a
claim: the excerpt appears on the fetched page (normalized), the
claimed publisher matches the page's declared site name or domain,
and the page's published date is within one day of the claimed date
when the page declares one. The verdict rules are fixed before the
pilot: fetch failures and robots blocks are unverifiable, a fetched
page that fails a check is contradicted, everything else is
verified.
"""

from datetime import date
from typing import Optional

import pytest
from runners.tow_checks import (
    date_within_tolerance,
    decide_verdict,
    domain_matches_publisher,
    excerpt_on_page,
    normalize,
    parse_claim_date,
    published_date_from_html,
    site_name_from_html,
)
from runners.tow_fetch import FetchResult

pytestmark = pytest.mark.unit


def _fetch(
    status: Optional[int] = 200,
    html: str = "",
    text: str = "",
    robots_blocked: bool = False,
    final_url: str = "https://www.bostonglobe.com/2024/12/11/metro/storm/",
) -> FetchResult:
    """Build a FetchResult for verdict tests."""
    return FetchResult(
        url=final_url,
        final_url=final_url,
        status=status,
        html=html,
        text=text,
        robots_blocked=robots_blocked,
        error="",
        fetched_at="2026-10-04T00:00:00+00:00",
        from_cache=False,
    )


class TestNormalize:
    """Test suite for text normalization."""

    def test_curly_quotes_entities_case_and_whitespace(self) -> None:
        """Typographic variants collapse to one comparable form."""
        fancy = "It’s   a “Big” storm — really&amp;truly"
        plain = 'it\'s a "big" storm - really&truly'

        assert normalize(fancy) == normalize(plain)


class TestExcerptOnPage:
    """Test suite for the excerpt check."""

    def test_finds_excerpt_despite_markup_noise(self) -> None:
        """A normalized substring match survives quote and space drift."""
        excerpt = "Some good news is that this storm should make a lasting impact"
        page = "nav menu … Some  good news is that this storm\nshould make a lasting impact on the drought. footer"

        assert excerpt_on_page(excerpt, page) is True

    def test_absent_excerpt_is_not_found(self) -> None:
        """A page without the excerpt fails the check."""
        assert excerpt_on_page("this exact sentence", "entirely other text") is False


class TestSiteAndDates:
    """Test suite for page metadata extraction."""

    def test_site_name_prefers_og_meta(self) -> None:
        """og:site_name is the page's own declaration of its publisher."""
        html = (
            '<head><meta property="og:site_name" content="The Boston Globe"/>'
            "<title>Storm hits - The Boston Globe</title></head>"
        )
        assert site_name_from_html(html) == "The Boston Globe"

    def test_site_name_falls_back_to_title(self) -> None:
        """Without og:site_name the title is better than nothing."""
        html = "<head><title>Storm hits | Example News</title></head>"
        assert "Example News" in site_name_from_html(html)

    def test_published_date_from_meta(self) -> None:
        """article:published_time yields a date."""
        html = (
            '<meta property="article:published_time" '
            'content="2024-12-11T09:30:00-05:00"/>'
        )
        assert published_date_from_html(html) == date(2024, 12, 11)

    def test_published_date_from_json_ld(self) -> None:
        """datePublished inside JSON-LD yields a date."""
        html = '<script type="application/ld+json">{"datePublished": "2025-03-06T12:00:00Z"}</script>'
        assert published_date_from_html(html) == date(2025, 3, 6)

    def test_no_date_is_none(self) -> None:
        """A page without a declared date returns None, not a guess."""
        assert published_date_from_html("<p>hello</p>") is None

    def test_parse_claim_date_formats(self) -> None:
        """The three formats the dataset and engines use all parse."""
        assert parse_claim_date("12/11/2024") == date(2024, 12, 11)
        assert parse_claim_date("2025-03-06") == date(2025, 3, 6)
        assert parse_claim_date("March 6, 2025") == date(2025, 3, 6)

    def test_date_tolerance(self) -> None:
        """One day of drift passes; two days fail; missing is None."""
        d = date(2024, 12, 11)
        assert date_within_tolerance(d, date(2024, 12, 12)) is True
        assert date_within_tolerance(d, date(2024, 12, 13)) is False
        assert date_within_tolerance(None, d) is None
        assert date_within_tolerance(d, None) is None


class TestDomainMatch:
    """Test suite for the publisher-versus-domain check."""

    def test_matches_via_og_site_name(self) -> None:
        """The page saying 'The Boston Globe' settles it."""
        html = '<meta property="og:site_name" content="The Boston Globe"/>'
        assert (
            domain_matches_publisher(
                "Boston Globe", "https://www.bostonglobe.com/x", html
            )
            is True
        )

    def test_matches_via_domain_token_without_site_name(self) -> None:
        """A long publisher token inside the domain label counts."""
        assert (
            domain_matches_publisher(
                "The New York Times", "https://www.nytimes.com/2024/x", ""
            )
            is True
        )

    def test_wrong_domain_fails(self) -> None:
        """A claim citing the wrong outlet fails the check."""
        assert (
            domain_matches_publisher(
                "Boston Globe", "https://www.example-aggregator.com/x", ""
            )
            is False
        )


class TestVerdict:
    """Test suite for the verdict rules."""

    EXCERPT = "Some good news is that this storm should make a lasting impact"
    PAGE = (
        '<meta property="og:site_name" content="The Boston Globe"/>'
        '<meta property="article:published_time" content="2024-12-11T09:00:00-05:00"/>'
    )

    def _claim(self) -> dict:
        return {
            "publisher": "Boston Globe",
            "date": "12/11/2024",
            "url": "https://www.bostonglobe.com/2024/12/11/metro/storm/",
        }

    def test_robots_block_is_unverifiable(self) -> None:
        """A well-behaved fetcher refused entry declines honestly."""
        verdict, checks = decide_verdict(
            _fetch(status=None, robots_blocked=True), self.EXCERPT, self._claim()
        )
        assert verdict == "unverifiable"
        assert checks["robots_blocked"] is True

    def test_http_error_is_unverifiable(self) -> None:
        """A fabricated URL that 404s cannot be verified."""
        verdict, _ = decide_verdict(_fetch(status=404), self.EXCERPT, self._claim())
        assert verdict == "unverifiable"

    def test_missing_excerpt_is_contradicted(self) -> None:
        """A live page without the excerpt contradicts the claim."""
        verdict, checks = decide_verdict(
            _fetch(html=self.PAGE, text="totally different content"),
            self.EXCERPT,
            self._claim(),
        )
        assert verdict == "contradicted"
        assert checks["excerpt_found"] is False

    def test_all_checks_pass_is_verified(self) -> None:
        """Excerpt present, domain right, date within a day: verified."""
        verdict, checks = decide_verdict(
            _fetch(html=self.PAGE, text=f"intro {self.EXCERPT} outro"),
            self.EXCERPT,
            self._claim(),
        )
        assert verdict == "verified"
        assert checks["excerpt_found"] is True
        assert checks["domain_match"] is True
        assert checks["date_match"] is True
        assert checks["http_status"] == 200
