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
from citation_verifier.grader import (
    AnswerRecord,
    blind_pool,
    categorize,
    categorize_v2,
    compute_rates,
    grade,
    syndication_label,
    urls_match,
)

pytestmark = pytest.mark.unit

TRUTH = {
    "publication": "Harbor Gazette",
    "date": "03/14/2024",
    "source_url": "https://www.harborgazette.example/2024/03/14/local/ferry/",
}


class FakeFetcher:
    """Scripted stand-in for the shared fetch cache."""

    def __init__(self, status: Optional[int] = 200, text: str = "") -> None:
        self.status = status
        self.text = text

    def fetch(self, url: str) -> Any:
        from citation_verifier.fetch import FetchResult

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
            "http://harborgazette.example/2024/03/14/local/ferry/?ref=x",
            TRUTH["source_url"],
        )

    def test_different_paths_do_not_match(self) -> None:
        """A different article is a different URL."""
        assert not urls_match(
            "https://www.harborgazette.example/other-article/", TRUTH["source_url"]
        )


class TestCategorize:
    """Test suite for the six Tow categories."""

    def test_all_attributes_correct_is_correct(self) -> None:
        """Publisher, date, and URL right: correct."""
        record = _record(
            {
                "headline": "whatever",
                "publisher": "The Harbor Gazette",
                "date": "March 14, 2024",
                "url": TRUTH["source_url"],
            }
        )
        assert categorize(record, TRUTH) == "correct"

    def test_missing_attribute_with_no_wrong_one_is_incomplete(self) -> None:
        """Right but missing the date: correct but incomplete."""
        record = _record(
            {
                "headline": "",
                "publisher": "Harbor Gazette",
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
                "date": "03/14/2024",
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


class TestCategorizeV2:
    """Test suite for categorize_v2, the opt-in republication rule.

    Decision recorded on the Marcus Program board 2026-11-05
    (OPTION B): verifier v2 counts a CONFIRMED known-network
    republication as a correct citation; the published full-200
    result is unchanged. This function is strictly additive --
    categorize() itself is never modified, so the default scoring
    path that reproduces the published numbers is untouched. Only a
    ``known_republication:*`` label is in scope; "moved" and
    "dated_snapshot" are URL-structure artifacts the board decision
    explicitly treats as a separate question and left alone here.
    """

    def test_known_republication_is_promoted_to_correct(self) -> None:
        """A confirmed sister-site match becomes correct under v2."""
        record = _record(
            {
                "publisher": "South Dakota Searchlight",
                "date": "",
                "url": "https://southdakotasearchlight.com/2024/05/24/radiation/",
            }
        )
        truth = {
            "publication": "Missouri Independent",
            "date": "",
            "source_url": "https://missouriindependent.com/2024/05/29/radiation/",
        }

        assert categorize(record, truth) == "completely incorrect"
        assert categorize_v2(record, truth) == "correct"

    def test_moved_url_is_not_promoted(self) -> None:
        """A moved/re-sectioned URL stays as categorize() scored it.

        The board decision treats moved/dated-snapshot as a separate,
        smaller URL-structure question, explicitly out of scope for
        this rule.
        """
        record = _record(
            {
                "publisher": "San Francisco Chronicle",
                "date": "",
                "url": (
                    "https://www.sfchronicle.example/bayarea/article/"
                    "a-moved-section-article-slug"
                ),
            }
        )
        truth = {
            "publication": "San Francisco Chronicle",
            "date": "",
            "source_url": (
                "https://www.sfchronicle.example/health/article/"
                "a-moved-section-article-slug"
            ),
        }

        v1 = categorize(record, truth)
        assert categorize_v2(record, truth) == v1

    def test_dated_snapshot_is_not_promoted(self) -> None:
        """A dated-snapshot citation stays as categorize() scored it."""
        record = _record(
            {
                "publisher": "TechCrunch",
                "date": "",
                "url": (
                    "https://techcrunch.example/2024/08/17/"
                    "an-ongoing-running-story-timeline/"
                ),
            }
        )
        truth = {
            "publication": "TechCrunch",
            "date": "",
            "source_url": (
                "https://techcrunch.example/2025/01/27/"
                "an-ongoing-running-story-timeline/"
            ),
        }

        v1 = categorize(record, truth)
        assert categorize_v2(record, truth) == v1

    def test_genuine_fabrication_is_not_promoted(self) -> None:
        """An unrelated outlet with no known relationship stays wrong."""
        record = _record(
            {"publisher": "Example News", "date": "", "url": "https://example.com/else"}
        )

        assert categorize(record, TRUTH) == categorize_v2(record, TRUTH)

    def test_network_match_with_a_wrong_date_is_not_promoted(self) -> None:
        """Codex P2 on PR #833: a known network pairing is not proof by itself.

        A publisher-name match on the known-network table only
        confirms the TWO OUTLETS have a documented relationship; it
        says nothing about whether THIS specific cited article is
        the right one. An engine could cite a real sister-site
        outlet while naming a wrong, unrelated article -- the date
        field is the one independently-checkable signal that catches
        that: a genuine republication of the same event does not
        carry a contradicted date. If the date is present and wrong,
        v2 must not promote; it stays exactly as v1 scored it.
        """
        record = _record(
            {
                "publisher": "South Dakota Searchlight",
                "date": "1990-01-01",
                "url": "https://example.com/nonexistent-unrelated-article",
            }
        )
        truth = {
            "publication": "Missouri Independent",
            "date": "2024-05-29",
            "source_url": "https://missouriindependent.com/2024/05/29/radiation/",
        }

        v1 = categorize(record, truth)
        assert v1 in ("partially incorrect", "completely incorrect")
        assert categorize_v2(record, truth) == v1

    def test_network_match_with_no_claimed_date_is_still_promoted(self) -> None:
        """A missing date is not a contradicted date.

        Many engines simply omit the date field; that is not
        evidence of a wrong article, so it must not block the
        promotion the way an explicitly wrong date does.
        """
        record = _record(
            {
                "publisher": "South Dakota Searchlight",
                "date": "",
                "url": "https://southdakotasearchlight.com/2024/05/24/radiation/",
            }
        )
        truth = {
            "publication": "Missouri Independent",
            "date": "2024-05-29",
            "source_url": "https://missouriindependent.com/2024/05/29/radiation/",
        }

        assert categorize_v2(record, truth) == "correct"

    def test_correct_and_declined_records_are_unaffected(self) -> None:
        """v2 cannot change an answer that v1 already scored correct or declined."""
        correct_record = _record(
            {
                "publisher": "The Harbor Gazette",
                "date": "March 14, 2024",
                "url": TRUTH["source_url"],
            }
        )
        declined_record = _record(None, declined=True)

        assert categorize_v2(correct_record, TRUTH) == "correct"
        assert categorize_v2(declined_record, TRUTH) == "not provided"

    def test_categorize_itself_is_never_called_with_different_results(self) -> None:
        """categorize() is unmodified: v1 and v2 must agree on every
        non-republication case, pinning that v2 is additive, not a
        rewrite of the base scoring rule.
        """
        cases = [
            (
                _record(
                    {
                        "publisher": "New York Times",
                        "date": "03/14/2024",
                        "url": TRUTH["source_url"],
                    }
                ),
                TRUTH,
            ),
            (_record(None, declined=True, robots_blocked=True), TRUTH),
        ]
        for record, truth in cases:
            assert categorize(record, truth) == categorize_v2(record, truth)


class TestBlindPool:
    """Test suite for mechanical label-blindness."""

    def test_blinded_records_carry_no_condition_or_engine(self) -> None:
        """The scorer's view has answers and nothing to cheat with."""
        records = [
            _record({"publisher": "Harbor Gazette", "date": "", "url": ""}),
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


class TestSyndicationLabel:
    """Test suite for the diagnostic syndication label (#745).

    Additive only: this never changes what categorize() returns, so
    none of the full-200 run's published numbers move because of
    this PR. It tells a wrong-delivered answer apart by WHY it is
    wrong, built from the 17-case inspection in
    docs/design/research/tow-full-200-readout-2026-10-16.md.
    """

    def test_known_network_match_labels_as_republication(self) -> None:
        """tow-0052: South Dakota Searchlight republishing Missouri Independent."""
        record = _record(
            {
                "publisher": "South Dakota Searchlight",
                "date": "",
                "url": "https://southdakotasearchlight.com/2024/05/24/radiation/",
            }
        )
        truth = {
            "publication": "Missouri Independent",
            "date": "",
            "source_url": "https://missouriindependent.com/2024/05/29/radiation/",
        }

        assert syndication_label(record, truth) == "known_republication:states_newsroom"

    def test_moved_url_same_outlet_labels_as_moved(self) -> None:
        """A same-outlet, re-sectioned URL labels as moved.

        (Real-world anchor: tow-0105, San Francisco Chronicle -- the
        slug matched, only the section path changed; the dataset's
        own URL never appears in this test, per the project's
        standing rule.)
        """
        record = _record(
            {
                "publisher": "San Francisco Chronicle",
                "date": "",
                "url": (
                    "https://www.sfchronicle.example/bayarea/article/"
                    "a-moved-section-article-slug"
                ),
            }
        )
        truth = {
            "publication": "San Francisco Chronicle",
            "date": "",
            "source_url": (
                "https://www.sfchronicle.example/health/article/"
                "a-moved-section-article-slug"
            ),
        }

        assert syndication_label(record, truth) == "moved"

    def test_dated_snapshot_labels_as_dated_snapshot(self) -> None:
        """An older dated URL of the same running story labels as a snapshot.

        (Real-world anchor: tow-0129, TechCrunch's "timeline" article
        pattern -- same slug, different date-in-path; the dataset's
        own URL never appears in this test, per the project's
        standing rule.)
        """
        record = _record(
            {
                "publisher": "TechCrunch",
                "date": "",
                "url": (
                    "https://techcrunch.example/2024/08/17/"
                    "an-ongoing-running-story-timeline/"
                ),
            }
        )
        truth = {
            "publication": "TechCrunch",
            "date": "",
            "source_url": (
                "https://techcrunch.example/2025/01/27/"
                "an-ongoing-running-story-timeline/"
            ),
        }

        assert syndication_label(record, truth) == "dated_snapshot"

    def test_genuine_mismatch_has_no_label(self) -> None:
        """A real fabrication has no network match and no slug relation."""
        record = _record(
            {
                "publisher": "Example News",
                "date": "",
                "url": "https://example.com/else",
            }
        )
        assert syndication_label(record, TRUTH) is None

    def test_declined_record_has_no_label(self) -> None:
        """A decline has no claimed answer to compare, so no label."""
        record = _record(None, declined=True)
        assert syndication_label(record, TRUTH) is None

    def test_correct_record_has_no_label(self) -> None:
        """A correct answer needs no syndication explanation."""
        record = _record(
            {
                "publisher": "The Harbor Gazette",
                "date": "March 14, 2024",
                "url": TRUTH["source_url"],
            }
        )
        assert syndication_label(record, TRUTH) is None

    def test_network_match_takes_priority_over_slug(self) -> None:
        """When both signals could apply, the network match is reported.

        A known-network match is the stronger, independently
        documented claim; slug comparison is a same-host heuristic
        and should not override it.
        """
        record = _record(
            {
                "publisher": "Kanebridge News",
                "date": "",
                "url": "https://kanebridgenews.com/a-killer-golf-swing/",
            }
        )
        truth = {
            "publication": "The Wall Street Journal",
            "date": "",
            "source_url": "https://www.wsj.com/lifestyle/careers/golf-business",
        }

        assert (
            syndication_label(record, truth)
            == "known_republication:wsj_dow_jones_syndication"
        )


class TestGradeWorksheetCarriesSyndicationLabel:
    """Test suite for wiring syndication_label into grade()'s output (#745).

    Codex P2 on PR #826: the helper existed but nothing exposed it to
    a grading consumer. The worksheet gains one additive column; the
    published rates (what compute_rates and categorize produce) must
    not move, since that is the whole point of keeping this
    diagnostic-only.
    """

    def test_worksheet_row_carries_the_syndication_label(self) -> None:
        """A known republication's worksheet row names the network."""
        records = [
            AnswerRecord(
                case_id="tow-0001",
                condition="B",
                engine="perplexity",
                answer={
                    "publisher": "South Dakota Searchlight",
                    "date": "",
                    "url": "https://southdakotasearchlight.com/2024/05/24/radiation/",
                },
                declined=False,
            )
        ]
        truths = {
            "tow-0001": {
                "publication": "Missouri Independent",
                "date": "",
                "source_url": "https://missouriindependent.com/2024/05/29/radiation/",
            }
        }

        results = grade(records, truths, excerpts={}, fetcher=None, seed=1)

        row = results["worksheet"][0]
        assert row["syndication"] == "known_republication:states_newsroom"
        assert row["category"] == "completely incorrect"

    def test_correct_answer_has_no_syndication_label(self) -> None:
        """A correct answer's worksheet row carries no label."""
        records = [
            AnswerRecord(
                case_id="tow-0001",
                condition="B",
                engine="perplexity",
                answer={
                    "publisher": "Harbor Gazette",
                    "date": "03/14/2024",
                    "url": TRUTH["source_url"],
                },
                declined=False,
            )
        ]
        truths = {"tow-0001": TRUTH}

        results = grade(records, truths, excerpts={}, fetcher=None, seed=1)

        assert results["worksheet"][0]["syndication"] is None

    def test_syndication_label_never_changes_the_published_rates(self) -> None:
        """Adding the label does not move categorize()'s own scoring.

        Same case, same inputs, scored with and without the label
        reachable (there is no toggle -- the label is attached beside
        category, never substituted for it); the category and the
        rates built from it must be identical either way.
        """
        answer = {
            "publisher": "South Dakota Searchlight",
            "date": "",
            "url": "https://southdakotasearchlight.com/2024/05/24/radiation/",
        }
        truth = {
            "publication": "Missouri Independent",
            "date": "",
            "source_url": "https://missouriindependent.com/2024/05/29/radiation/",
        }
        record = AnswerRecord(
            case_id="tow-0001", condition="B", engine="perplexity", answer=answer
        )

        direct_category = categorize(record, truth)
        results = grade(
            [record], {"tow-0001": truth}, excerpts={}, fetcher=None, seed=1
        )

        assert results["worksheet"][0]["category"] == direct_category
        assert results["conditions"]["B"]["confident_wrong_rate"] == 1.0


class TestGradeScoringVersion:
    """Test suite for grade()'s opt-in scoring_version parameter.

    Board decision (2026-11-05, OPTION B): the republication rule
    applies in verifier v2 only, NEVER in the code path that
    reproduces the published full-200 numbers. grade()'s default
    (``scoring_version="v1"``, unspecified) must be byte-for-byte
    identical to its behavior before this change existed, so re-
    running the grader on the published bundles with no new
    arguments reproduces the published result exactly. Passing
    ``scoring_version="v2"`` explicitly is the only way to reach the
    new rule.
    """

    def _known_republication_case(self) -> tuple:
        """One case that v1 scores wrong and v2 would score correct."""
        record = AnswerRecord(
            case_id="tow-0001",
            condition="B",
            engine="perplexity",
            answer={
                "publisher": "South Dakota Searchlight",
                "date": "",
                "url": "https://southdakotasearchlight.com/2024/05/24/radiation/",
            },
        )
        truth = {
            "publication": "Missouri Independent",
            "date": "",
            "source_url": "https://missouriindependent.com/2024/05/29/radiation/",
        }
        return record, truth

    def test_default_scoring_version_is_v1(self) -> None:
        """Calling grade() with no scoring_version argument scores as v1."""
        record, truth = self._known_republication_case()

        results = grade(
            [record], {"tow-0001": truth}, excerpts={}, fetcher=None, seed=1
        )

        assert results["worksheet"][0]["category"] == "completely incorrect"
        assert results["conditions"]["B"]["correct_rate"] == 0.0

    def test_explicit_v1_matches_the_default(self) -> None:
        """Explicit v1 and the omitted default produce identical results."""
        record, truth = self._known_republication_case()

        default_results = grade(
            [record], {"tow-0001": truth}, excerpts={}, fetcher=None, seed=1
        )
        explicit_v1_results = grade(
            [record],
            {"tow-0001": truth},
            excerpts={},
            fetcher=None,
            seed=1,
            scoring_version="v1",
        )

        assert default_results == explicit_v1_results

    def test_v2_scores_the_known_republication_as_correct(self) -> None:
        """Explicitly requesting v2 applies the board-decided rule."""
        record, truth = self._known_republication_case()

        results = grade(
            [record],
            {"tow-0001": truth},
            excerpts={},
            fetcher=None,
            seed=1,
            scoring_version="v2",
        )

        assert results["worksheet"][0]["category"] == "correct"
        assert results["conditions"]["B"]["correct_rate"] == 1.0

    def test_v2_does_not_affect_moved_or_dated_snapshot_cases(self) -> None:
        """v2 leaves the URL-structure-only labels exactly as v1 scored them."""
        record = AnswerRecord(
            case_id="tow-0001",
            condition="B",
            engine="perplexity",
            answer={
                "publisher": "San Francisco Chronicle",
                "date": "",
                "url": (
                    "https://www.sfchronicle.example/bayarea/article/"
                    "a-moved-section-article-slug"
                ),
            },
        )
        truth = {
            "publication": "San Francisco Chronicle",
            "date": "",
            "source_url": (
                "https://www.sfchronicle.example/health/article/"
                "a-moved-section-article-slug"
            ),
        }

        v1_results = grade(
            [record], {"tow-0001": truth}, excerpts={}, fetcher=None, seed=1
        )
        v2_results = grade(
            [record],
            {"tow-0001": truth},
            excerpts={},
            fetcher=None,
            seed=1,
            scoring_version="v2",
        )

        assert (
            v1_results["worksheet"][0]["category"]
            == v2_results["worksheet"][0]["category"]
        )

    def test_syndication_column_is_present_under_both_versions(self) -> None:
        """The diagnostic column from #745 survives v2 unchanged."""
        record, truth = self._known_republication_case()

        results = grade(
            [record],
            {"tow-0001": truth},
            excerpts={},
            fetcher=None,
            seed=1,
            scoring_version="v2",
        )

        assert (
            results["worksheet"][0]["syndication"]
            == "known_republication:states_newsroom"
        )


class TestFabricatedUrl:
    """Test suite for the fabricated-URL flag."""

    def test_dead_url_is_fabricated(self) -> None:
        """A claimed URL that 404s is fabricated or broken."""
        from citation_verifier.grader import url_is_fabricated

        record = _record(
            {"publisher": "Harbor Gazette", "date": "", "url": "https://x.org/gone"}
        )

        assert (
            url_is_fabricated(record, TRUTH, FakeFetcher(status=404), "excerpt") is True
        )

    def test_right_url_is_not_fabricated(self) -> None:
        """The ground-truth URL is never flagged, whatever the page says."""
        from citation_verifier.grader import url_is_fabricated

        record = _record({"publisher": "", "date": "", "url": TRUTH["source_url"]})

        assert (
            url_is_fabricated(record, TRUTH, FakeFetcher(status=200), "excerpt")
            is False
        )

    def test_live_wrong_page_without_excerpt_is_fabricated(self) -> None:
        """A live page that is not the article counts as the wrong page."""
        from citation_verifier.grader import url_is_fabricated

        record = _record(
            {"publisher": "", "date": "", "url": "https://x.org/some-other-page"}
        )
        fetcher = FakeFetcher(status=200, text="unrelated content entirely")

        assert url_is_fabricated(record, TRUTH, fetcher, "the real excerpt") is True
