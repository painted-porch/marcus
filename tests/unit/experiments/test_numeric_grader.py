"""Unit tests for the label-blind numeric-verifier grader.

Scoring must never see which condition a case came from, and the
rates it reports have to mean what their names say: a wrong figure
that shipped, a correct one that was declined, a trap that went
uncaught. These tests pin the scoring function's blindness and the
arithmetic behind each rate.
"""

import hashlib
import json

import pytest
from runners.numeric_grader import (
    CONDITION_A,
    CONDITION_B,
    CaseRecord,
    ClaimRecord,
    blind_pool,
    compute_rates,
    grade,
    load_condition_a,
    load_condition_b,
    load_condition_c,
    score_case,
)
from runners.numeric_writer import trusted_hashes_path


def _write_trusted_manifest(case_dir):
    """Write the harness-retained manifest matching case_dir/staged/ as-is.

    A hand-built workspace in a test has no manifest unless it
    writes one itself -- see ``runners.numeric_writer.
    trusted_hashes_path``. Tests about something other than staged
    integrity call this so the (now mandatory) integrity check does
    not short-circuit them.

    Parameters
    ----------
    case_dir : pathlib.Path
        The workspace whose current ``staged/`` contents to hash.
    """
    staged = case_dir / "staged"
    hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(staged.glob("*"))
        if p.is_file()
    }
    trusted_hashes_path(case_dir).write_text(json.dumps(hashes, indent=2))


def make_record(
    case_id, condition, claims, rounds=1, declined=False, verified_clean=True
):
    """Build a CaseRecord for a test.

    Returns
    -------
    CaseRecord
        The constructed record.
    """
    return CaseRecord(
        case_id=case_id,
        condition=condition,
        claims=claims,
        rounds=rounds,
        declined=declined,
        verified_clean=verified_clean,
    )


class TestScoreCaseIsBlind:
    """The scoring function never reads condition."""

    @pytest.mark.unit
    def test_identical_claims_score_identically_across_conditions(self):
        """Condition cannot change the verdict on the same evidence."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="verified",
                stated=0.9,
                resolved=0.9,
            ),
        ]
        a = score_case(make_record("case-1", CONDITION_A, claims), {})
        b = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert a["wrong_shipped"] == b["wrong_shipped"] == []
        assert a["false_declines"] == b["false_declines"] == []


class TestWrongShipped:
    """A verified claim whose stated value disagrees with the truth."""

    @pytest.mark.unit
    def test_verified_but_mismatched_is_wrong_shipped(self):
        """This is the case the whole trial exists to catch."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="verified",
                stated=23,
                resolved=22.1,
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert len(out["wrong_shipped"]) == 1

    @pytest.mark.unit
    def test_verified_and_matching_is_not_wrong_shipped(self):
        """A correct, passing claim is not an error."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="verified",
                stated=0.886,
                resolved=0.886,
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert out["wrong_shipped"] == []

    @pytest.mark.unit
    def test_failed_claim_is_never_wrong_shipped(self):
        """A rejected claim did not ship, whatever it stated."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="failed",
                stated=23,
                resolved=22.1,
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert out["wrong_shipped"] == []


class TestCodexFindingWrongShippedOnlyCountsForReleasedReports:
    """A wrong figure in a case that never released never shipped.

    wrong_figures_shipped counted a verified-but-wrong claim even
    when another claim in the same case failed and the whole report
    was declined. numeric_slides.main() refuses to build a deck
    unless the overall report verifies, so that figure never reached
    anyone -- counting it in a metric named "shipped" overstates how
    many wrong figures a reader could actually have seen.

    Codex's exact example: a claim accepted at 0.9 against a true
    0.481 (materially wrong, verified), alongside a separately
    rejected claim that declines the case as a whole.
    """

    @pytest.mark.unit
    def test_a_wrong_verified_claim_in_a_declined_case_does_not_ship(self):
        """The exact payload from the finding."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="verified",
                stated=0.9,
                resolved=0.481,
            ),
            ClaimRecord(
                claim_id="c2",
                kind="declared",
                verdict="failed",
                stated=1,
                resolved=2,
                reason="value_mismatch",
            ),
        ]
        record = make_record("case-1", CONDITION_B, claims, verified_clean=False)
        out = score_case(record, {})
        assert out["wrong_shipped"] == []
        assert len(out["wrong_figures_in_rejected_reports"]) == 1
        assert out["wrong_figures_in_rejected_reports"][0].claim_id == "c1"

    @pytest.mark.unit
    def test_the_same_wrong_claim_in_a_released_case_still_ships(self):
        """The fix must not hide a wrong figure in a case that DID
        release -- only a declined case's figures are diagnostic-only."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="verified",
                stated=0.9,
                resolved=0.481,
            ),
        ]
        record = make_record("case-1", CONDITION_B, claims, verified_clean=True)
        out = score_case(record, {})
        assert len(out["wrong_shipped"]) == 1
        assert out["wrong_figures_in_rejected_reports"] == []

    @pytest.mark.unit
    def test_compute_rates_reports_both_metrics_separately(self):
        """The aggregate rate keeps the two counts distinct, not merged."""
        released = {
            "wrong_shipped": [1],
            "wrong_figures_in_rejected_reports": [],
            "false_declines": [],
            "coverage_gaps": [],
            "traps_present": [],
            "declined": False,
            "verified_clean": True,
            "rounds": 1,
        }
        declined = {
            "wrong_shipped": [],
            "wrong_figures_in_rejected_reports": [1],
            "false_declines": [],
            "coverage_gaps": [],
            "traps_present": [],
            "declined": True,
            "verified_clean": False,
            "rounds": 3,
        }
        rates = compute_rates({CONDITION_B: [released, declined]})
        assert rates[CONDITION_B]["wrong_figures_shipped"] == 1
        assert rates[CONDITION_B]["wrong_figures_in_rejected_reports"] == 1


class TestFalseDeclines:
    """A rejected claim whose stated value was in fact correct."""

    @pytest.mark.unit
    def test_failed_but_actually_correct_is_a_false_decline(self):
        """The cost side of strictness, counted honestly."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="failed",
                stated=0.886,
                resolved=0.886,
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert len(out["false_declines"]) == 1

    @pytest.mark.unit
    def test_correctly_failed_claim_is_not_a_false_decline(self):
        """A rejection that matches the truth is working as intended."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="failed",
                stated=0.99,
                resolved=0.886,
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert out["false_declines"] == []


class TestCoverageGaps:
    """Figures the checker found with no claim behind them."""

    @pytest.mark.unit
    def test_uncovered_claim_is_a_coverage_gap(self):
        """An unclaimed figure counts against coverage."""
        claims = [
            ClaimRecord(
                claim_id="uncovered:0.99",
                kind="coverage",
                verdict="uncovered",
                stated="0.99",
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert len(out["coverage_gaps"]) == 1


class TestRates:
    """The six rates, computed from scored cases."""

    @pytest.mark.unit
    def test_trap_catch_rate_needs_an_opportunity(self):
        """A trap never planted contributes no opportunity to miss."""
        cases = [
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [],
                "traps_present": [],
                "declined": False,
                "verified_clean": True,
                "rounds": 1,
            },
        ]
        rates = compute_rates({CONDITION_B: cases})
        assert rates[CONDITION_B]["trap_catch_rate"] == {}

    @pytest.mark.unit
    def test_caught_trap_counts_against_its_own_opportunity(self):
        """A planted trap whose own signal fired is caught."""
        cases = [
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [],
                "traps_present": ["dropped_tier_rows"],
                "declined": False,
                "verified_clean": False,
                "required_tiers_failed": True,
                "rounds": 1,
            },
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [],
                "traps_present": ["dropped_tier_rows"],
                "declined": False,
                "verified_clean": True,
                "required_tiers_failed": False,
                "rounds": 1,
            },
        ]
        rates = compute_rates({CONDITION_B: cases})
        assert rates[CONDITION_B]["trap_catch_rate"]["dropped_tier_rows"] == 0.5

    @pytest.mark.unit
    def test_mean_rounds_averages_across_cases(self):
        """Rounds spent is a per-case mean, not a total."""
        cases = [
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [],
                "traps_present": [],
                "declined": False,
                "verified_clean": True,
                "rounds": 1,
            },
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [],
                "traps_present": [],
                "declined": False,
                "verified_clean": True,
                "rounds": 3,
            },
        ]
        rates = compute_rates({CONDITION_B: cases})
        assert rates[CONDITION_B]["mean_rounds"] == 2.0

    @pytest.mark.unit
    def test_empty_condition_reports_zero_cases_not_an_error(self):
        """A condition with no records yet does not crash the grader."""
        rates = compute_rates({CONDITION_A: []})
        assert rates[CONDITION_A] == {"cases": 0}


class TestBlindPoolAndRejoin:
    """The shuffle hides condition from scoring; the rejoin restores it."""

    @pytest.mark.unit
    def test_shuffle_is_deterministic_under_one_seed(self):
        """A rerun with the same seed pools identically."""
        records = [make_record(f"case-{i}", CONDITION_B, []) for i in range(10)]
        a = [r.case_id for _, r in blind_pool(records, seed=1)]
        b = [r.case_id for _, r in blind_pool(records, seed=1)]
        assert a == b

    @pytest.mark.unit
    def test_grade_rejoins_every_case_to_its_own_condition(self):
        """The final rates are attributed to the condition that produced them."""
        a_claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="verified",
                stated=99,
                resolved=1,
            ),
        ]
        b_claims = [
            ClaimRecord(
                claim_id="c1", kind="declared", verdict="verified", stated=1, resolved=1
            ),
        ]
        records = [
            make_record("case-1", CONDITION_A, a_claims),
            make_record("case-1", CONDITION_B, b_claims),
        ]
        result = grade(records, {}, seed=5)
        assert result["rates"][CONDITION_A]["wrong_figures_shipped"] == 1
        assert result["rates"][CONDITION_B]["wrong_figures_shipped"] == 0


class TestUnbuiltConditions:
    """Conditions A and C fail loudly rather than returning empty results."""

    @pytest.mark.unit
    def test_condition_a_raises_not_implemented(self, tmp_path):
        """An empty Condition A must not look like a clean run."""
        with pytest.raises(NotImplementedError):
            load_condition_a(tmp_path)

    @pytest.mark.unit
    def test_condition_c_raises_not_implemented(self, tmp_path):
        """Condition C depends on Condition A's output."""
        with pytest.raises(NotImplementedError):
            load_condition_c(tmp_path)


class TestTrapNameNormalization:
    """The staging manifest's traps are dicts, not bare strings."""

    @pytest.mark.unit
    def test_dict_shaped_traps_are_extracted(self):
        """score_case must read the manifest's actual shape."""
        claims = [
            ClaimRecord(
                claim_id="c1", kind="declared", verdict="verified", stated=1, resolved=1
            ),
        ]
        truth = {"traps": [{"name": "dropped_tier_rows"}]}
        out = score_case(make_record("case-1", CONDITION_B, claims), truth)
        assert out["traps_present"] == ["dropped_tier_rows"]

    @pytest.mark.unit
    def test_no_traps_gives_an_empty_list(self):
        """A clean case has no trap opportunities."""
        out = score_case(make_record("case-1", CONDITION_B, []), {})
        assert out["traps_present"] == []


class TestMaterialityThreshold:
    """Ordinary rounding is not a wrong figure shipped.

    A checker that honors a declared tolerance leaves stated and
    resolved not bit-identical by design. Grading on exact equality
    would flag every rounded figure as an error; this is the
    regression a real rehearsal run surfaced.
    """

    @pytest.mark.unit
    def test_rounded_percentage_is_not_wrong_shipped(self):
        """94.0 stated against 93.97... resolved is ordinary rounding."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="verified",
                stated=94.0,
                resolved=93.97018970189703,
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert out["wrong_shipped"] == []

    @pytest.mark.unit
    def test_tiny_floating_point_noise_is_not_wrong_shipped(self):
        """6.3 against 6.300000000000001 is not an error."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="verified",
                stated=6.3,
                resolved=6.300000000000001,
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert out["wrong_shipped"] == []

    @pytest.mark.unit
    def test_rounded_up_ratio_is_still_wrong_shipped(self):
        """23 against 22.1 is the real defect, not rounding noise."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="verified",
                stated=23,
                resolved=22.1,
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert len(out["wrong_shipped"]) == 1

    @pytest.mark.unit
    def test_rounding_tolerant_reject_is_still_a_false_decline(self):
        """A rejection of a merely-rounded value is false, not correct."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="failed",
                stated=94.0,
                resolved=93.97018970189703,
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert len(out["false_declines"]) == 1


class TestCodexFindingLabelRejectionNotAFalseDecline:
    """P2: a correctly rejected label error was counted as a false decline.

    maize R2 0.886 labelled as cotton fails the label check (the
    checker working as intended) but had equal stated/resolved
    numeric values, so the materiality check alone classified it as
    a correct figure wrongly declined. That inverts the meaning of
    the false-decline rate: it would have penalized the checker for
    catching the exact wrong-row error it exists to catch.
    """

    @pytest.mark.unit
    def test_label_mismatch_rejection_is_not_a_false_decline(self):
        """Right number, wrong label: a real catch, not a false decline."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="failed",
                stated=0.886,
                resolved=0.886,
                reason="label_mismatch",
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert out["false_declines"] == []

    @pytest.mark.unit
    def test_value_mismatch_rejection_of_a_correct_figure_is_still_a_false_decline(
        self,
    ):
        """The original case this predicate exists for still works."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="failed",
                stated=0.886,
                resolved=0.886,
                reason="value_mismatch",
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert len(out["false_declines"]) == 1


class TestCodexFindingMaterialityScalesToTheMetric:
    """A flat 0.1 absolute allowance swallowed real errors in small metrics.

    0.1 is calibrated to percentage-scale figures (roughly stated as
    whole numbers near 0-100). Applied to a fractional metric like
    R2 (bounded 0-1), it is enormous: a stated 0.58 against a real
    0.481 is a ~20% relative error, the shape of a genuine wrong
    figure, but the flat allowance treated it as ordinary rounding.
    score_case incorrectly excluded it from wrong_shipped, and when
    a checker correctly rejected it, incorrectly counted that
    rejection as a false decline.
    """

    @pytest.mark.unit
    def test_a_materially_wrong_r2_is_not_hidden_by_the_flat_allowance(self):
        """0.58 stated against a real 0.481 is a wrong figure shipped."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="verified",
                stated=0.58,
                resolved=0.481,
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert len(out["wrong_shipped"]) == 1

    @pytest.mark.unit
    def test_rejecting_that_wrong_r2_is_not_a_false_decline(self):
        """Correctly rejecting 0.58-vs-0.481 must not count against the checker."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="failed",
                stated=0.58,
                resolved=0.481,
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert out["false_declines"] == []

    @pytest.mark.unit
    def test_ordinary_rounding_on_a_small_metric_is_still_tolerated(self):
        """A genuinely rounded R2 (0.481 stated as 0.48) is not an error."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="verified",
                stated=0.48,
                resolved=0.481,
            ),
        ]
        out = score_case(make_record("case-1", CONDITION_B, claims), {})
        assert out["wrong_shipped"] == []


class TestCodexFindingIncompleteCasesAreNotSilentlyDropped:
    """A writer that produced no claims.json vanished from grading.

    load_condition_b skipped any case directory lacking claims.json,
    so an unsuccessful writer session disappeared from the case count
    and every rate's denominator instead of contributing its failure
    -- biasing every reported rate toward successful runs. A case
    directory is identified by having a staged/ subdirectory (which
    build_workspace always creates), not by whether the writer
    finished.
    """

    @pytest.mark.unit
    def test_a_case_with_no_claims_json_is_still_loaded_as_a_failure(self, tmp_path):
        """An incomplete writer session counts, not vanishes."""
        case_dir = tmp_path / "case-000"
        (case_dir / "staged").mkdir(parents=True)
        records = load_condition_b(tmp_path)
        assert len(records) == 1
        assert records[0].case_id == "case-000"
        assert records[0].verified_clean is False

    @pytest.mark.unit
    def test_a_case_with_no_staged_dir_at_all_is_not_a_case(self, tmp_path):
        """A directory that was never a workspace is correctly excluded."""
        (tmp_path / "not_a_case").mkdir()
        records = load_condition_b(tmp_path)
        assert records == []

    @pytest.mark.unit
    def test_a_complete_case_still_loads_normally(self, tmp_path):
        """The fix must not change behavior for a case that did finish."""
        case_dir = tmp_path / "case-001"
        staged = case_dir / "staged"
        staged.mkdir(parents=True)
        (staged / "metrics.json").write_text('{"n_records": 10, "r2_overall": 0.9}')
        _write_trusted_manifest(case_dir)
        (case_dir / "report.md").write_text("R2 was 0.9 [c1].")
        (case_dir / "claims.json").write_text(
            '{"claims": [{"id": "c1", "value": 0.9, "label": "R2", '
            '"kind": "lookup", "source": "metrics.json", "key": "r2_overall"}]}'
        )
        records = load_condition_b(tmp_path)
        assert len(records) == 1
        assert records[0].verified_clean is True


class TestCodexFindingTrapCatchIsAttributedNotBlanket:
    """A trap is caught by its own signal, not "the case failed somehow."

    The old predicate credited every planted trap as caught whenever
    the case's overall verdict was non-clean, for any reason. A case
    containing only staged/ (no report, no claims at all) reported a
    100% catch rate for every trap present, though the writer never
    created an opportunity for any of them to be caught. Likewise,
    one harness-side catch (dropped_tier_rows) credited every other
    trap present in the same case, whether or not it had anything to
    do with that catch.
    """

    @pytest.mark.unit
    def test_score_case_reports_no_declared_mismatches_for_an_empty_case(self):
        """A case with no declared claims has nothing to attribute."""
        record = make_record("case-000", CONDITION_B, claims=[])
        out = score_case(record, {"traps": ["stale_sibling", "rounding_ratio"]})
        assert out["declared_value_mismatches"] == 0
        assert out["required_tiers_failed"] is False

    @pytest.mark.unit
    def test_score_case_counts_a_declared_value_mismatch(self):
        """A declared claim that turned out wrong is the writer-dependent
        trap signal."""
        claims = [
            ClaimRecord(
                claim_id="c1",
                kind="declared",
                verdict="failed",
                stated=23.0,
                resolved=22.1,
                reason="value_mismatch",
            ),
        ]
        record = make_record("case-000", CONDITION_B, claims=claims)
        out = score_case(record, {"traps": ["rounding_ratio"]})
        assert out["declared_value_mismatches"] == 1

    @pytest.mark.unit
    def test_score_case_detects_a_failed_tiers_required_check(self):
        """A failed required tiers check is the harness-side trap signal."""
        claims = [
            ClaimRecord(
                claim_id="required:tiers-total",
                kind="harness",
                verdict="failed",
                stated=True,
                resolved=False,
            ),
        ]
        record = make_record("case-000", CONDITION_B, claims=claims)
        out = score_case(record, {"traps": ["dropped_tier_rows"]})
        assert out["required_tiers_failed"] is True

    @pytest.mark.unit
    def test_an_empty_case_credits_no_writer_dependent_trap(self):
        """Codex's exact example: a staged/-only case with no report or
        claims must not report a 100% catch rate for a writer-dependent
        trap."""
        cases = [
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [],
                "traps_present": ["stale_sibling", "rounding_ratio"],
                "declined": False,
                "verified_clean": False,
                "required_tiers_failed": False,
                "declared_value_mismatches": 0,
                "rounds": 1,
            },
        ]
        rates = compute_rates({CONDITION_B: cases})
        assert rates[CONDITION_B]["trap_catch_rate"]["stale_sibling"] == 0.0
        assert rates[CONDITION_B]["trap_catch_rate"]["rounding_ratio"] == 0.0

    @pytest.mark.unit
    def test_a_dropped_tier_rows_catch_does_not_credit_other_traps(self):
        """Catching one trap must not inflate the catch rate of an
        unrelated trap present in the same case."""
        cases = [
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [],
                "traps_present": ["dropped_tier_rows", "rounding_ratio"],
                "declined": False,
                "verified_clean": False,
                "required_tiers_failed": True,
                "declared_value_mismatches": 0,
                "rounds": 1,
            },
        ]
        rates = compute_rates({CONDITION_B: cases})
        assert rates[CONDITION_B]["trap_catch_rate"]["dropped_tier_rows"] == 1.0
        assert rates[CONDITION_B]["trap_catch_rate"]["rounding_ratio"] == 0.0


class TestCodexFindingRatesUseFigureLevelDenominators:
    """A per-figure or per-claim count divided by case count is not a rate.

    coverage_gap_rate divided its count of uncovered figures by the
    number of CASES, giving an average gap count per report rather
    than the preregistered figure-coverage rate. Codex's example: one
    report with 40 figures and 2 uncovered ones reported 2.0 instead
    of 0.05, and adding more correctly-covered figures could never
    improve the metric, so reports of different lengths were not
    comparable on coverage at all.

    false_decline_rate had the identical bug: false_declines is a
    count of individual claims, also divided by the number of cases.
    Both are now summed across the whole condition and divided by
    the matching total -- figures_total (from the verifier's own
    summary) for coverage, and claims actually checked (declared or
    harness claims, excluding coverage gaps, which were never
    checked against a resolved value) for false declines.
    """

    @pytest.mark.unit
    def test_coverage_gap_rate_is_uncovered_over_total_figures(self):
        """Codex's exact numbers: 40 figures, 2 uncovered, expect 0.05."""
        cases = [
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [1, 2],
                "figures_total": 40,
                "claims_checked": 38,
                "traps_present": [],
                "declined": False,
                "verified_clean": True,
                "rounds": 1,
            },
        ]
        rates = compute_rates({CONDITION_B: cases})
        assert rates[CONDITION_B]["coverage_gap_rate"] == 0.05

    @pytest.mark.unit
    def test_coverage_gap_rate_is_figure_weighted_not_case_averaged(self):
        """Two reports of different lengths must not average their rates.

        A 40-figure report with 2 gaps and a 10-figure report with no
        gaps must combine to 2 gaps over 50 figures (0.04), not the
        mean of each report's own rate ((0.05 + 0.0) / 2 = 0.025).
        """
        cases = [
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [1, 2],
                "figures_total": 40,
                "claims_checked": 38,
                "traps_present": [],
                "declined": False,
                "verified_clean": True,
                "rounds": 1,
            },
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [],
                "figures_total": 10,
                "claims_checked": 10,
                "traps_present": [],
                "declined": False,
                "verified_clean": True,
                "rounds": 1,
            },
        ]
        rates = compute_rates({CONDITION_B: cases})
        assert rates[CONDITION_B]["coverage_gap_rate"] == 0.04

    @pytest.mark.unit
    def test_false_decline_rate_is_declines_over_claims_checked(self):
        """A per-claim count must be a fraction of claims, not of cases."""
        cases = [
            {
                "wrong_shipped": [],
                "false_declines": [1],
                "coverage_gaps": [],
                "figures_total": 10,
                "claims_checked": 10,
                "traps_present": [],
                "declined": False,
                "verified_clean": True,
                "rounds": 1,
            },
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [],
                "figures_total": 2,
                "claims_checked": 2,
                "traps_present": [],
                "declined": False,
                "verified_clean": True,
                "rounds": 1,
            },
        ]
        rates = compute_rates({CONDITION_B: cases})
        assert rates[CONDITION_B]["false_decline_rate"] == round(1 / 12, 3)

    @pytest.mark.unit
    def test_rates_with_no_figures_or_claims_do_not_divide_by_zero(self):
        """An empty-but-present condition's rates must not crash or NaN."""
        cases = [
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [],
                "figures_total": 0,
                "claims_checked": 0,
                "traps_present": [],
                "declined": False,
                "verified_clean": True,
                "rounds": 1,
            },
        ]
        rates = compute_rates({CONDITION_B: cases})
        assert rates[CONDITION_B]["coverage_gap_rate"] == 0.0
        assert rates[CONDITION_B]["false_decline_rate"] == 0.0

    @pytest.mark.unit
    def test_load_condition_b_carries_figures_total_from_the_verifier(self, tmp_path):
        """The verifier's own figure count, not the grader's guess."""
        case_dir = tmp_path / "case-001"
        staged = case_dir / "staged"
        staged.mkdir(parents=True)
        (staged / "metrics.json").write_text('{"n_records": 10, "r2_overall": 0.9}')
        _write_trusted_manifest(case_dir)
        (case_dir / "report.md").write_text("R2 was 0.9 [c1].")
        (case_dir / "claims.json").write_text(
            '{"claims": [{"id": "c1", "value": 0.9, "label": "R2", '
            '"kind": "lookup", "source": "metrics.json", "key": "r2_overall"}]}'
        )
        records = load_condition_b(tmp_path)
        assert records[0].figures_total == 1
