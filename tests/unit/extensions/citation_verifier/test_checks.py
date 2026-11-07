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
from typing import List, Optional

import pytest
from citation_verifier.checks import (
    MIN_EXCERPT_COVERAGE,
    MIN_EXCERPT_RUN,
    classify_slug_relationship,
    date_within_tolerance,
    decide_verdict,
    domain_matches_publisher,
    excerpt_on_page,
    known_republication_network,
    match_excerpt,
    normalize,
    parse_claim_date,
    published_date_from_html,
    site_name_from_html,
)
from citation_verifier.fetch import FetchResult

pytestmark = pytest.mark.unit


def _fetch(
    status: Optional[int] = 200,
    html: str = "",
    text: str = "",
    robots_blocked: bool = False,
    final_url: str = "https://www.harborgazette.example/2024/03/14/local/ferry/",
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
        excerpt = (
            "The harbor council voted to keep the winter ferry running on weekends"
        )
        page = "nav menu … The  harbor council voted to keep the winter ferry\nrunning on weekends through March. footer"

        assert excerpt_on_page(excerpt, page) is True

    def test_absent_excerpt_is_not_found(self) -> None:
        """A page without the excerpt fails the check."""
        assert excerpt_on_page("this exact sentence", "entirely other text") is False


class TestMatchExcerpt:
    """Test suite for the ordered-alignment excerpt match (#752).

    The exact-substring check declined correct pages whenever one word
    in a long excerpt differed from the page (a dash, a dropped word, an
    inline link). The decline audit measured a 10% false-decline rate
    from exactly this. The replacement scores how much of the excerpt
    appears on the page, in order, and requires a contiguous run so a
    page that merely quotes a phrase still fails.
    """

    EXCERPT = (
        "The harbor council voted on Tuesday night to keep the winter ferry "
        "running on weekends after residents of the outer islands told the "
        "board that the weekday-only schedule had left them stranded twice "
        "in January"
    )

    def test_exact_text_scores_full_coverage(self) -> None:
        """A verbatim page scores 1.0 with the whole excerpt as one run."""
        result = match_excerpt(self.EXCERPT, f"header {self.EXCERPT} footer")

        assert result.found is True
        assert result.coverage == 1.0
        assert result.longest_run == result.excerpt_words

    def test_one_dropped_word_still_matches(self) -> None:
        """A page missing one mid-sentence word is the same sentence."""
        page = self.EXCERPT.replace("on Tuesday night ", "on Tuesday ")

        result = match_excerpt(self.EXCERPT, page)

        assert result.found is True
        assert result.coverage >= MIN_EXCERPT_COVERAGE

    def test_inline_link_text_mid_sentence_still_matches(self) -> None:
        """Markup noise inside the sentence (an inline link) does not break it."""
        page = self.EXCERPT.replace(
            "winter ferry", "winter ferry (see our earlier coverage) ferry"
        )

        result = match_excerpt(self.EXCERPT, page)

        assert result.found is True

    def test_truncated_tail_within_tolerance_matches(self) -> None:
        """A page that ends a little early still carries the sentence."""
        words = self.EXCERPT.split()
        page = " ".join(words[: int(len(words) * 0.9)])

        result = match_excerpt(self.EXCERPT, page)

        assert result.found is True

    def test_short_quoted_phrase_is_not_enough(self) -> None:
        """A page quoting six words of a forty-word excerpt is not the source."""
        page = "As one report put it, the winter ferry running on weekends matters."

        result = match_excerpt(self.EXCERPT, page)

        assert result.found is False
        assert result.coverage < MIN_EXCERPT_COVERAGE

    def test_same_words_out_of_order_do_not_match(self) -> None:
        """Coverage is in-order alignment, not a bag of words."""
        words = self.EXCERPT.split()
        page = " ".join(reversed(words))

        result = match_excerpt(self.EXCERPT, page)

        assert result.found is False

    def test_half_the_sentence_is_not_enough(self) -> None:
        """Half the excerpt, even contiguous, stays below the coverage floor."""
        words = self.EXCERPT.split()
        page = " ".join(words[: len(words) // 2])

        result = match_excerpt(self.EXCERPT, page)

        assert result.found is False

    def test_short_excerpt_needs_its_whole_length(self) -> None:
        """An excerpt shorter than the run floor must appear in full."""
        assert match_excerpt(
            "ferry runs on weekends", "the ferry runs on weekends now"
        ).found
        assert not match_excerpt(
            "ferry runs on weekends", "the ferry runs on weekdays"
        ).found

    def test_excerpt_deep_inside_a_long_page_is_found(self) -> None:
        """A long page with the sentence near the end is still matched."""
        filler = " ".join(f"word{i} filler text and more" for i in range(20000))
        page = f"{filler} {self.EXCERPT} {filler[:5000]}"

        result = match_excerpt(self.EXCERPT, page)

        assert result.found is True
        assert result.coverage == 1.0

    def test_passing_window_beats_a_higher_coverage_failing_window(self) -> None:
        """Codex finding on PR #756: keep a passing region over a better-scoring failing one.

        A decoy region carries every excerpt word with a stray token every
        six words (coverage 1.0, run 6, fails the run floor); another region
        carries the first ninety percent verbatim (passes). The page passes.
        """
        words = self.EXCERPT.split()
        decoy_words: List[str] = []
        for i, w in enumerate(words):
            decoy_words.append(w)
            if i % 6 == 5:
                decoy_words.append("stray")
        decoy = " ".join(decoy_words)
        passing = " ".join(words[: int(len(words) * 0.9)])
        filler = " ".join(f"filler{i} words here" for i in range(2000))
        page = f"{filler} {decoy} {filler} {passing} {filler}"

        result = match_excerpt(self.EXCERPT, page)

        assert result.found is True
        assert result.longest_run >= MIN_EXCERPT_RUN

    def test_scattered_words_outside_the_quoted_run_do_not_count(self) -> None:
        """Coverage is measured near the quoted run, not across the whole page.

        A short page quoting the first three quarters of the excerpt, with the
        remaining words sprinkled hundreds of words later, is a partial quote
        and must not pass on the strength of the sprinkles.
        """
        words = self.EXCERPT.split()
        cut = int(len(words) * 0.75)
        quoted = " ".join(words[:cut])
        rest = words[cut:]
        sprinkled = " ".join(
            f"{w} " + " ".join(f"other{i}{k}" for k in range(40))
            for i, w in enumerate(rest)
        )
        page = (
            f"intro text. {quoted}. "
            + " ".join(f"unrelated{i}" for i in range(500))
            + f" {sprinkled}"
        )

        result = match_excerpt(self.EXCERPT, page)

        assert result.found is False

    def test_edge_punctuation_does_not_cost_a_word(self) -> None:
        """Punctuation glued to a word is not a different word.

        The old substring check accepted 'weekends now.' for 'weekends now';
        the alignment must too, or short excerpts regress.
        """
        assert match_excerpt(
            "ferry runs on weekends now", "the ferry runs on weekends now."
        ).found
        assert match_excerpt(
            "ferry runs on weekends now", 'she said "ferry runs on weekends now" twice'
        ).found
        assert (
            match_excerpt(
                "ferry runs on weekends now", "the ferry runs on weekends now"
            ).coverage
            == 1.0
        )

    def test_hyphen_and_dash_variants_match(self) -> None:
        """'weekday-only' and 'weekday only' are the same words."""
        page = self.EXCERPT.replace("weekday-only", "weekday only")

        result = match_excerpt(self.EXCERPT, page)

        assert result.coverage == 1.0
        assert result.found is True

    def test_non_latin_letters_are_words_too(self) -> None:
        """Codex finding on PR #759: word extraction must keep Unicode letters.

        A non-Latin excerpt must match an identical page, and a mixed-script
        excerpt whose non-Latin part differs must not be scored as a match.
        """
        assert (
            match_excerpt(
                "部长说 不同意 这个 计划", "昨天 部长说 不同意 这个 计划 。"
            ).found
            is True
        )
        assert (
            match_excerpt("The minister said 不同意", "The minister said 同意").found
            is False
        )
        assert (
            match_excerpt(
                "café müde naïve façade", "the café müde naïve façade"
            ).coverage
            == 1.0
        )

    def test_run_floor_is_eight_words(self) -> None:
        """The contiguous-run floor is the documented constant."""
        assert MIN_EXCERPT_RUN == 8

    def test_empty_inputs_are_not_found(self) -> None:
        """No excerpt or no page text never verifies."""
        assert match_excerpt("", "anything").found is False
        assert match_excerpt(self.EXCERPT, "").found is False


class TestDeclineReasons:
    """Test suite for the recorded decline reason (#752).

    Every verdict now carries ``decline_reason`` so a readout can say
    why a case was withheld: a publisher that blocks fetchers and a
    dead link are both declines, but they indict different parties.
    """

    EXCERPT = "The harbor council voted to keep the winter ferry running on weekends"
    PAGE = (
        '<meta property="og:site_name" content="The Harbor Gazette"/>'
        '<meta property="article:published_time" content="2024-03-14T09:00:00-05:00"/>'
    )
    CLAIM = {
        "publisher": "Harbor Gazette",
        "date": "03/14/2024",
        "url": "https://www.harborgazette.example/2024/03/14/local/ferry/",
    }

    def test_robots_block_reason(self) -> None:
        """A robots refusal is recorded as such."""
        _, checks = decide_verdict(
            _fetch(status=None, robots_blocked=True), self.EXCERPT, self.CLAIM
        )
        assert checks["decline_reason"] == "robots_blocked"

    def test_grounding_redirect_wrapper_is_an_opaque_redirect(self) -> None:
        """A search-grounding redirect the fetcher may not follow is named.

        The wrapper host disallows its redirect path for every agent,
        so an honest fetcher cannot resolve it; the decline is the
        engine's citation format, not a bad source.
        """
        wrapper = (
            "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AbCdEf"
        )
        fetch = FetchResult(
            url=wrapper,
            final_url=wrapper,
            status=None,
            html="",
            text="",
            robots_blocked=True,
            error="",
            fetched_at="2026-10-04T00:00:00+00:00",
        )
        verdict, checks = decide_verdict(fetch, self.EXCERPT, self.CLAIM)
        assert verdict == "unverifiable"
        assert checks["decline_reason"] == "opaque_redirect"

    def test_forbidden_is_publisher_blocked(self) -> None:
        """HTTP 403 means the publisher refused the fetcher."""
        _, checks = decide_verdict(_fetch(status=403), self.EXCERPT, self.CLAIM)
        assert checks["decline_reason"] == "publisher_blocked"

    def test_not_found_is_a_dead_link(self) -> None:
        """HTTP 404 and 410 are dead links."""
        _, checks = decide_verdict(_fetch(status=404), self.EXCERPT, self.CLAIM)
        assert checks["decline_reason"] == "dead_link"
        _, checks = decide_verdict(_fetch(status=410), self.EXCERPT, self.CLAIM)
        assert checks["decline_reason"] == "dead_link"

    def test_other_http_errors_are_http_error(self) -> None:
        """A 400 or 500 is an HTTP error without a more specific name."""
        _, checks = decide_verdict(_fetch(status=400), self.EXCERPT, self.CLAIM)
        assert checks["decline_reason"] == "http_error"

    def test_network_failure_is_fetch_error(self) -> None:
        """No response at all is a fetch error."""
        fetch = FetchResult(
            url="https://www.harborgazette.example/x",
            final_url="https://www.harborgazette.example/x",
            status=None,
            html="",
            text="",
            robots_blocked=False,
            error="timed out",
            fetched_at="2026-10-04T00:00:00+00:00",
        )
        _, checks = decide_verdict(fetch, self.EXCERPT, self.CLAIM)
        assert checks["decline_reason"] == "fetch_error"

    def test_no_url_claimed_is_its_own_reason(self) -> None:
        """An engine that cited nothing is not a fetch error."""
        fetch = FetchResult(
            url="",
            final_url="",
            status=None,
            html="",
            text="",
            robots_blocked=False,
            error="no URL claimed",
            fetched_at="2026-10-04T00:00:00+00:00",
        )
        _, checks = decide_verdict(fetch, self.EXCERPT, self.CLAIM)
        assert checks["decline_reason"] == "no_url"

    def test_address_blocked_is_its_own_reason(self) -> None:
        """A URL the fetcher refused on policy (file://, loopback) is named."""
        fetch = FetchResult(
            url="file:///etc/passwd",
            final_url="file:///etc/passwd",
            status=None,
            html="",
            text="",
            robots_blocked=False,
            error="blocked: scheme 'file' is not http(s)",
            fetched_at="2026-10-04T00:00:00+00:00",
        )
        _, checks = decide_verdict(fetch, self.EXCERPT, self.CLAIM)
        assert checks["decline_reason"] == "address_blocked"

    def test_empty_body_is_empty_page(self) -> None:
        """A 2xx with no body cannot be checked and says so."""
        _, checks = decide_verdict(
            _fetch(status=200, html=""), self.EXCERPT, self.CLAIM
        )
        assert checks["decline_reason"] == "empty_page"

    def test_robots_block_after_leaving_the_wrapper_is_robots_blocked(self) -> None:
        """A publisher's robots refusal behind a wrapper is the publisher's, not the wrapper's."""
        fetch = FetchResult(
            url="https://vertexaisearch.cloud.google.com/grounding-api-redirect/AbCdEf",
            final_url="https://www.harborgazette.example/2024/03/14/local/ferry/",
            status=None,
            html="",
            text="",
            robots_blocked=True,
            error="",
            fetched_at="2026-10-04T00:00:00+00:00",
        )
        _, checks = decide_verdict(fetch, self.EXCERPT, self.CLAIM)
        assert checks["decline_reason"] == "robots_blocked"

    def test_missing_excerpt_records_coverage(self) -> None:
        """A live page without the sentence says so, with the scores."""
        verdict, checks = decide_verdict(
            _fetch(html=self.PAGE, text="totally different content"),
            self.EXCERPT,
            self.CLAIM,
        )
        assert verdict == "contradicted"
        assert checks["decline_reason"] == "excerpt_missing"
        assert checks["excerpt_coverage"] < MIN_EXCERPT_COVERAGE
        assert "excerpt_longest_run" in checks

    def test_near_verbatim_excerpt_verifies(self) -> None:
        """A page with one word of drift in the sentence now verifies."""
        drifted = self.EXCERPT.replace("to keep the", "to keep its")
        verdict, checks = decide_verdict(
            _fetch(
                html=self.PAGE,
                text=f"intro {drifted} outro",
                final_url=self.CLAIM["url"],
            ),
            self.EXCERPT,
            self.CLAIM,
        )
        assert verdict == "verified"
        assert checks["excerpt_found"] is True
        assert checks["decline_reason"] is None

    def test_publisher_mismatch_reason(self) -> None:
        """Right sentence on the wrong publisher's page."""
        _, checks = decide_verdict(
            _fetch(
                html='<meta property="og:site_name" content="Other Outlet"/>',
                text=f"intro {self.EXCERPT} outro",
                final_url="https://www.otheroutlet.example/a",
            ),
            self.EXCERPT,
            self.CLAIM,
        )
        assert checks["decline_reason"] == "publisher_mismatch"

    def test_date_mismatch_reason(self) -> None:
        """Right sentence, right publisher, wrong date."""
        _, checks = decide_verdict(
            _fetch(
                html=self.PAGE,
                text=f"intro {self.EXCERPT} outro",
                final_url=self.CLAIM["url"],
            ),
            self.EXCERPT,
            {**self.CLAIM, "date": "01/02/2023"},
        )
        assert checks["decline_reason"] == "date_mismatch"

    def test_syndicated_copy_reason(self) -> None:
        """A page whose canonical lives elsewhere is a syndicated copy."""
        page = (
            '<link rel="canonical" href="https://www.original.example/story/"/>'
            + self.PAGE
        )
        _, checks = decide_verdict(
            _fetch(
                html=page,
                text=f"intro {self.EXCERPT} outro",
                final_url=self.CLAIM["url"],
            ),
            self.EXCERPT,
            self.CLAIM,
        )
        assert checks["decline_reason"] == "syndicated_copy"


class TestSiteAndDates:
    """Test suite for page metadata extraction."""

    def test_site_name_prefers_og_meta(self) -> None:
        """og:site_name is the page's own declaration of its publisher."""
        html = (
            '<head><meta property="og:site_name" content="The Harbor Gazette"/>'
            "<title>Ferry vote - The Harbor Gazette</title></head>"
        )
        assert site_name_from_html(html) == "The Harbor Gazette"

    def test_site_name_falls_back_to_title(self) -> None:
        """Without og:site_name the title is better than nothing."""
        html = "<head><title>Ferry vote | Example News</title></head>"
        assert "Example News" in site_name_from_html(html)

    def test_published_date_from_meta(self) -> None:
        """article:published_time yields a date."""
        html = (
            '<meta property="article:published_time" '
            'content="2024-03-14T09:30:00-05:00"/>'
        )
        assert published_date_from_html(html) == date(2024, 3, 14)

    def test_published_date_from_json_ld(self) -> None:
        """datePublished inside JSON-LD yields a date."""
        html = '<script type="application/ld+json">{"datePublished": "2025-03-06T12:00:00Z"}</script>'
        assert published_date_from_html(html) == date(2025, 3, 6)

    def test_no_date_is_none(self) -> None:
        """A page without a declared date returns None, not a guess."""
        assert published_date_from_html("<p>hello</p>") is None

    def test_parse_claim_date_formats(self) -> None:
        """The three formats the dataset and engines use all parse."""
        assert parse_claim_date("03/14/2024") == date(2024, 3, 14)
        assert parse_claim_date("2025-03-06") == date(2025, 3, 6)
        assert parse_claim_date("March 6, 2025") == date(2025, 3, 6)

    def test_date_tolerance(self) -> None:
        """One day of drift passes; two days fail; missing is None."""
        d = date(2024, 3, 14)
        assert date_within_tolerance(d, date(2024, 3, 15)) is True
        assert date_within_tolerance(d, date(2024, 3, 16)) is False
        assert date_within_tolerance(None, d) is None
        assert date_within_tolerance(d, None) is None


class TestCanonicalUrl:
    """Test suite for the canonical-URL check (the pilot's gate-2 fix).

    The 2026-10-05 pilot's only verified-but-wrong cases were
    syndicated copies (Yahoo carrying the LA Times, KCUR carrying
    Missouri Independent, CT Mirror carrying ProPublica): the excerpt
    genuinely appears, but the page is a republication, not the
    source. Most republished pages declare their original through
    ``rel=canonical`` or ``og:url``; a canonical pointing off-domain
    means the page verifies the content but not the attribution.
    """

    def test_reads_rel_canonical_link(self) -> None:
        """The link tag is the primary signal."""
        from citation_verifier.checks import canonical_url_from_html

        html = '<link rel="canonical" href="https://www.latimes.com/environment/story/2022-07-31/original"/>'
        assert (
            canonical_url_from_html(html, "https://www.yahoo.com/news/copy")
            == "https://www.latimes.com/environment/story/2022-07-31/original"
        )

    def test_falls_back_to_og_url(self) -> None:
        """og:url stands in when no link tag exists."""
        from citation_verifier.checks import canonical_url_from_html

        html = '<meta property="og:url" content="https://missouriindependent.com/2024/04/29/original/"/>'
        assert (
            canonical_url_from_html(html, "https://www.kcur.org/copy")
            == "https://missouriindependent.com/2024/04/29/original/"
        )

    def test_relative_canonical_resolves_against_the_page(self) -> None:
        """A relative href is same-site by construction."""
        from citation_verifier.checks import canonical_url_from_html

        html = '<link rel="canonical" href="/2024/03/14/local/ferry/"/>'
        assert (
            canonical_url_from_html(html, "https://www.harborgazette.example/some/page")
            == "https://www.harborgazette.example/2024/03/14/local/ferry/"
        )

    def test_absent_canonical_is_empty(self) -> None:
        """No declaration, no signal."""
        from citation_verifier.checks import canonical_url_from_html

        assert canonical_url_from_html("<p>hello</p>", "https://x.org/a") == ""


class TestSyndicationVerdict:
    """Test suite for the verdict with the canonical check wired in."""

    EXCERPT = "California banned the sale of kangaroo leather years ago"
    SYNDICATED_PAGE = (
        '<link rel="canonical" href="https://www.latimes.com/environment/story/2022-07-31/original"/>'
        '<meta property="og:site_name" content="Yahoo News"/>'
    )

    def _claim(self) -> dict:
        return {
            "publisher": "Yahoo News",
            "date": "",
            "url": "https://www.yahoo.com/news/california-banned-kangaroo",
        }

    def test_offdomain_canonical_contradicts_despite_excerpt(self) -> None:
        """A syndicated copy verifies the content, not the attribution."""
        verdict, checks = decide_verdict(
            _fetch(
                html=self.SYNDICATED_PAGE,
                text=f"intro {self.EXCERPT} outro",
                final_url="https://www.yahoo.com/news/california-banned-kangaroo",
            ),
            self.EXCERPT,
            self._claim(),
        )

        assert verdict == "contradicted"
        assert checks["syndicated_copy"] is True
        assert "latimes.com" in checks["canonical_url"]

    def test_same_domain_canonical_still_verifies(self) -> None:
        """A normal self-canonical page is untouched by the check."""
        page = (
            '<link rel="canonical" href="https://www.harborgazette.example/2024/03/14/local/ferry/"/>'
            '<meta property="og:site_name" content="The Harbor Gazette"/>'
            '<meta property="article:published_time" content="2024-03-14T09:00:00-05:00"/>'
        )
        excerpt = "The harbor council voted to keep the winter ferry"
        verdict, checks = decide_verdict(
            _fetch(
                html=page,
                text=f"intro {excerpt} outro",
                final_url="https://www.harborgazette.example/2024/03/14/local/ferry/",
            ),
            excerpt,
            {
                "publisher": "Harbor Gazette",
                "date": "03/14/2024",
                "url": "https://www.harborgazette.example/2024/03/14/local/ferry/",
            },
        )

        assert verdict == "verified"
        assert checks["syndicated_copy"] is False


class TestKnownRepublicationNetwork:
    """Test suite for the static sister-site lookup (#745).

    Built from the full-200 run's 17 wrong-delivered cases
    (docs/design/research/tow-full-200-readout-2026-10-16.md): most
    were faithful republications the grader penalized for not
    matching Tow's single canonical outlet. A verify task's publisher
    check should not penalize a known, documented republication
    partner the same as a fabrication.

    Tests use the real publisher names the production table is keyed
    on -- organization names are public knowledge, not dataset text;
    the dataset's own excerpts and article URLs never appear here or
    anywhere else in the repo, per the project's standing rule.
    """

    def test_states_newsroom_sister_site_matches(self) -> None:
        """South Dakota Searchlight republishing Missouri Independent."""
        assert (
            known_republication_network(
                "South Dakota Searchlight", "Missouri Independent"
            )
            == "states_newsroom"
        )

    def test_npr_affiliate_content_partnership_matches(self) -> None:
        """KBIA and KCUR carry Missouri Independent's statehouse coverage
        under an explicit content-sharing partnership (both are
        University of Missouri NPR member stations); not independently
        fetch-verified, missouriindependent.com blocks robots.txt.
        """
        assert (
            known_republication_network("KBIA", "Missouri Independent")
            == "npr_affiliate_partnership"
        )
        assert (
            known_republication_network("KCUR", "Missouri Independent")
            == "npr_affiliate_partnership"
        )

    def test_mississippi_today_cc_license_matches(self) -> None:
        """Mississippi Today publishes under a Creative Commons license
        local papers may republish; not independently fetch-verified,
        mississippitoday.org blocks robots.txt.
        """
        assert (
            known_republication_network("Meridian Star", "Mississippi Today")
            == "mississippi_today_cc"
        )

    def test_wsj_dow_jones_syndication_matches(self) -> None:
        """Confirmed by direct fetch (2026-11-05): the Kanebridge News
        page carries a "Copyright 2020, Dow Jones & Company" notice and
        a WSJ logo badge on the article itself, which is licensed WSJ
        syndication, not Kanebridge's own reporting.
        """
        assert (
            known_republication_network("Kanebridge News", "The Wall Street Journal")
            == "wsj_dow_jones_syndication"
        )

    def test_unrelated_publishers_return_none(self) -> None:
        """A genuinely unrelated outlet is not in any known network."""
        assert known_republication_network("Random Blog", "The New York Times") is None

    def test_unconfirmed_pairs_are_not_in_the_table(self) -> None:
        """Plausible-but-unverified pairs are deliberately excluded.

        Spokesman-Review/Seattle Times is plausible (regional wire
        pickup) but spokesman.com disallows crawlers entirely
        (``Disallow: /``), so it was never confirmed and does not
        belong in a table whose entries are either documented policy
        or a direct observed fetch.
        """
        assert (
            known_republication_network("The Spokesman-Review", "Seattle Times") is None
        )

    def test_matching_is_order_independent(self) -> None:
        """Claimed and truth may appear in either argument position."""
        assert known_republication_network(
            "Missouri Independent", "South Dakota Searchlight"
        ) == known_republication_network(
            "South Dakota Searchlight", "Missouri Independent"
        )

    def test_empty_strings_return_none(self) -> None:
        """No publisher name on either side cannot match anything."""
        assert known_republication_network("", "Missouri Independent") is None
        assert known_republication_network("South Dakota Searchlight", "") is None


class TestSlugRelationship:
    """Test suite for comparing claimed and truth URLs by slug (#745).

    #745's own title: detect syndication without relying on
    ``rel=canonical``, which many smaller and local outlets never
    set. Comparing the URL slug (not just the host) distinguishes a
    moved/re-sectioned copy of the same article from a citation to a
    different, dated snapshot of an updated story.

    Fixtures use the invented ``harborgazette.example`` domain and
    invented article slugs already established elsewhere in this
    file -- the real-world case each pattern is drawn from is named
    in the docstring, but the actual dataset URL never appears here,
    per the project's standing rule (the leak scanner enforces this
    on every commit).
    """

    def test_identical_url_is_identical(self) -> None:
        """Byte-identical URLs."""
        url = "https://www.harborgazette.example/history/article/original-piece"
        assert classify_slug_relationship(url, url) == "identical"

    def test_same_slug_different_section_is_moved(self) -> None:
        """Same article, re-sectioned.

        (Real-world anchor: tow-0105, San Francisco Chronicle -- same
        slug, section path changed from one category to another.)
        """
        claimed = (
            "https://www.harborgazette.example/weather/article/"
            "coastal-storm-advisory-issued"
        )
        truth = (
            "https://www.harborgazette.example/local/article/"
            "coastal-storm-advisory-issued"
        )
        assert classify_slug_relationship(claimed, truth) == "moved"

    def test_same_slug_different_date_path_is_dated_snapshot(self) -> None:
        """An older dated URL of the same running story.

        (Real-world anchor: tow-0129, TechCrunch's "timeline" article
        pattern -- the slug matches but the date-in-path segment
        differs, which is a near-miss worth flagging, not a moved
        page.)
        """
        claimed = (
            "https://www.harborgazette.example/2024/08/17/"
            "ongoing-harbor-inquiry-timeline/"
        )
        truth = (
            "https://www.harborgazette.example/2025/01/27/"
            "ongoing-harbor-inquiry-timeline/"
        )
        assert classify_slug_relationship(claimed, truth) == "dated_snapshot"

    def test_different_slug_is_different(self) -> None:
        """A genuinely different article, even on the right domain."""
        claimed = "https://www.harborgazette.example/2025/01/27/different-story/"
        truth = "https://www.harborgazette.example/2024/05/29/original-story/"
        assert classify_slug_relationship(claimed, truth) == "different"

    def test_different_host_same_slug_is_different(self) -> None:
        """A matching slug on a different host is not a host-relative claim.

        This function compares slug relationships on ONE site; a
        cross-host match is the known-network lookup's job, not this
        function's.
        """
        claimed = "https://sister-bureau.example/2024/05/24/shared-story/"
        truth = "https://www.harborgazette.example/2024/05/29/shared-story/"
        assert classify_slug_relationship(claimed, truth) == "different"

    def test_empty_urls_are_different(self) -> None:
        """Missing URLs never classify as identical, moved, or a snapshot."""
        assert classify_slug_relationship("", "https://example.com/x/") == "different"
        assert classify_slug_relationship("https://example.com/x/", "") == "different"


class TestDomainMatch:
    """Test suite for the publisher-versus-domain check."""

    def test_matches_via_og_site_name(self) -> None:
        """The page saying 'The Harbor Gazette' settles it."""
        html = '<meta property="og:site_name" content="The Harbor Gazette"/>'
        assert (
            domain_matches_publisher(
                "Harbor Gazette", "https://www.harborgazette.example/x", html
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
                "Harbor Gazette", "https://www.example-aggregator.com/x", ""
            )
            is False
        )


class TestVerdict:
    """Test suite for the verdict rules."""

    EXCERPT = "The harbor council voted to keep the winter ferry running on weekends"
    PAGE = (
        '<meta property="og:site_name" content="The Harbor Gazette"/>'
        '<meta property="article:published_time" content="2024-03-14T09:00:00-05:00"/>'
    )

    def _claim(self) -> dict:
        return {
            "publisher": "Harbor Gazette",
            "date": "03/14/2024",
            "url": "https://www.harborgazette.example/2024/03/14/local/ferry/",
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
