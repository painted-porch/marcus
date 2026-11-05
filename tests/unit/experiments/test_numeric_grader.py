"""Unit tests for the label-blind numeric-verifier grader.

Scoring must never see which condition a case came from, and the
rates it reports have to mean what their names say: a wrong figure
that shipped, a correct one that was declined, a trap that went
uncaught. These tests pin the scoring function's blindness and the
arithmetic behind each rate.
"""

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
    load_condition_c,
    score_case,
)


def make_record(case_id, condition, claims, rounds=1, declined=False):
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
    def test_false_decline_rate_is_per_case_not_per_claim(self):
        """A rate is normalized by case count."""
        cases = [
            {
                "wrong_shipped": [],
                "false_declines": [1],
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
                "rounds": 1,
            },
        ]
        rates = compute_rates({CONDITION_B: cases})
        assert rates[CONDITION_B]["false_decline_rate"] == 0.5

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
        """A planted trap that produced a decline is caught."""
        cases = [
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [],
                "traps_present": ["dropped_tier_rows"],
                "declined": False,
                "verified_clean": False,
                "rounds": 1,
            },
            {
                "wrong_shipped": [],
                "false_declines": [],
                "coverage_gaps": [],
                "traps_present": ["dropped_tier_rows"],
                "declined": False,
                "verified_clean": True,
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
