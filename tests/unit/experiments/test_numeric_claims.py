"""Unit tests for the numeric claim checker (numeric-verifier POC).

The checker is the piece that replaces a model-based numeric audit
with code. These tests pin the three claim kinds, the tolerance
rules, the coverage requirement, and the slide mapping check.
"""

import json

import pytest
from runners.numeric_claims import (
    CheckResult,
    Claim,
    check_claims,
    check_slide_spec,
    extract_numbers,
    load_sources,
)


@pytest.fixture
def staged(tmp_path):
    """Create a minimal staged evaluation directory.

    Returns
    -------
    pathlib.Path
        Directory holding metrics.json, per_crop.csv and tiers.csv.
    """
    d = tmp_path / "eval_20261103_120000"
    d.mkdir()
    (d / "metrics.json").write_text(
        json.dumps(
            {
                "n_records": 1200,
                "r2_overall": 0.858,
                "median_ape": 4.4,
                "mean_ape": 23.1,
            }
        )
    )
    (d / "per_crop.csv").write_text(
        "crop,n_records,r2,median_ape,unit\n"
        "maize,817,0.886,3.6,BU / ACRE\n"
        "cotton,37,0.585,6.5,LB / ACRE\n"
    )
    (d / "tiers.csv").write_text(
        "tier,n_records,rate_pct\n"
        "Excellent,660,55.0\n"
        "Good,317,26.4\n"
        "Average,170,14.2\n"
        "Poor,53,4.4\n"
    )
    return d


@pytest.fixture
def sources(staged):
    """Load the staged directory into the checker's source map.

    Returns
    -------
    Dict[str, Any]
        Mapping of file name to parsed contents.
    """
    return load_sources(staged)


class TestLookupClaims:
    """A lookup claim reads one value from one staged file."""

    @pytest.mark.unit
    def test_matching_lookup_passes(self, sources):
        """A lookup equal to its source value is verified."""
        c = Claim(
            id="c1",
            value=0.858,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        res = check_claims([c], sources)
        assert res[0].verdict == "verified"

    @pytest.mark.unit
    def test_wrong_lookup_fails_and_reports_expected(self, sources):
        """A lookup off its source value is declined with the truth."""
        c = Claim(
            id="c1",
            value=0.92,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        res = check_claims([c], sources)
        assert res[0].verdict == "failed"
        assert res[0].expected == 0.858

    @pytest.mark.unit
    def test_csv_cell_lookup_by_row_and_column(self, sources):
        """A lookup addresses a CSV cell as row:column."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            source="per_crop.csv",
            key="crop=maize:r2",
            kind="lookup",
        )
        assert check_claims([c], sources)[0].verdict == "verified"

    @pytest.mark.unit
    def test_missing_key_is_unresolvable_not_verified(self, sources):
        """A key absent from the source cannot be verified."""
        c = Claim(
            id="c1",
            value=1.0,
            label="nope",
            source="metrics.json",
            key="no_such_key",
            kind="lookup",
        )
        assert check_claims([c], sources)[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_stale_source_file_is_unresolvable(self, sources):
        """A claim naming a file not in the staged set is unresolvable."""
        c = Claim(
            id="c1",
            value=0.8,
            label="x",
            source="other_eval/metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        assert check_claims([c], sources)[0].verdict == "unresolvable"


class TestTolerance:
    """Tolerance is declared per claim, never decided by the checker."""

    @pytest.mark.unit
    def test_value_inside_declared_tolerance_passes(self, sources):
        """A rounded value inside tolerance is verified."""
        c = Claim(
            id="c1",
            value=0.86,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
            tolerance={"type": "abs", "value": 0.005},
        )
        assert check_claims([c], sources)[0].verdict == "verified"

    @pytest.mark.unit
    def test_value_outside_declared_tolerance_fails(self, sources):
        """A value beyond the declared tolerance is declined."""
        c = Claim(
            id="c1",
            value=0.87,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
            tolerance={"type": "abs", "value": 0.005},
        )
        assert check_claims([c], sources)[0].verdict == "failed"

    @pytest.mark.unit
    def test_relative_tolerance_scales_with_magnitude(self, sources):
        """A relative tolerance is applied as a fraction of truth."""
        c = Claim(
            id="c1",
            value=1180,
            label="record count",
            source="metrics.json",
            key="n_records",
            kind="lookup",
            tolerance={"type": "rel", "value": 0.02},
        )
        assert check_claims([c], sources)[0].verdict == "verified"

    @pytest.mark.unit
    def test_default_tolerance_is_exact_match(self, sources):
        """With no declared tolerance the comparison is exact."""
        c = Claim(
            id="c1",
            value=0.8581,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        assert check_claims([c], sources)[0].verdict == "failed"


class TestDerivedClaims:
    """A derived claim is recomputed, never taken on trust."""

    @pytest.mark.unit
    def test_correct_ratio_passes(self, sources):
        """A ratio matching its recomputation is verified."""
        c = Claim(
            id="c1",
            value=22.08,
            label="maize to cotton ratio",
            kind="derived",
            expr="a / b",
            inputs={
                "a": "per_crop.csv:crop=maize:n_records",
                "b": "per_crop.csv:crop=cotton:n_records",
            },
            tolerance={"type": "abs", "value": 0.01},
        )
        assert check_claims([c], sources)[0].verdict == "verified"

    @pytest.mark.unit
    def test_rounded_up_ratio_fails_recomputation(self, sources):
        """The ~23x case: 817/37 is 22.08, so 23 is declined."""
        c = Claim(
            id="c1",
            value=23.0,
            label="maize to cotton ratio",
            kind="derived",
            expr="a / b",
            inputs={
                "a": "per_crop.csv:crop=maize:n_records",
                "b": "per_crop.csv:crop=cotton:n_records",
            },
            tolerance={"type": "abs", "value": 0.5},
        )
        res = check_claims([c], sources)[0]
        assert res.verdict == "failed"
        assert res.expected == pytest.approx(22.08, abs=0.01)

    @pytest.mark.unit
    def test_sum_of_rounded_parts_fails(self, sources):
        """The ~18.7 case: parts recompute to 18.6, not 18.7."""
        c = Claim(
            id="c1",
            value=18.7,
            label="average plus poor share",
            kind="derived",
            expr="a + b",
            inputs={
                "a": "tiers.csv:tier=Average:rate_pct",
                "b": "tiers.csv:tier=Poor:rate_pct",
            },
            tolerance={"type": "abs", "value": 0.05},
        )
        assert check_claims([c], sources)[0].verdict == "failed"

    @pytest.mark.unit
    def test_expression_is_sandboxed(self, sources):
        """A derived expression cannot reach outside arithmetic."""
        c = Claim(
            id="c1",
            value=0,
            label="bad",
            kind="derived",
            expr="__import__('os').system('echo hi')",
            inputs={},
        )
        assert check_claims([c], sources)[0].verdict == "unresolvable"


class TestConsistencyClaims:
    """A consistency claim asserts a relation between figures."""

    @pytest.mark.unit
    def test_tier_total_matching_record_count_passes(self, sources):
        """When the tier counts sum to the record count, it is verified."""
        c = Claim(
            id="c1",
            value=True,
            label="tier total equals records",
            kind="consistency",
            expr="a == b",
            inputs={"a": "tiers.csv:sum:n_records", "b": "metrics.json:n_records"},
        )
        assert check_claims([c], sources)[0].verdict == "verified"

    @pytest.mark.unit
    def test_tier_total_short_of_record_count_fails(self, tmp_path, staged):
        """The 14,253 case: a short tier total is declined."""
        (staged / "tiers.csv").write_text(
            "tier,n_records,rate_pct\n"
            "Excellent,660,55.0\nGood,317,26.4\n"
            "Average,170,14.2\nPoor,48,4.0\n"
        )
        srcs = load_sources(staged)
        c = Claim(
            id="c1",
            value=True,
            label="tier total equals records",
            kind="consistency",
            expr="a == b",
            inputs={"a": "tiers.csv:sum:n_records", "b": "metrics.json:n_records"},
        )
        res = check_claims([c], srcs)[0]
        assert res.verdict == "failed"
        assert "1195" in str(res.detail)


class TestCoverage:
    """Every number in the report text needs a claim behind it."""

    @pytest.mark.unit
    def test_extract_numbers_finds_figures_in_prose(self):
        """Numbers are pulled from prose including percentages."""
        text = "R2 was 0.858 across 1,200 records; median error 4.4%."
        assert set(extract_numbers(text)) >= {"0.858", "1,200", "4.4"}

    @pytest.mark.unit
    def test_extract_numbers_ignores_claim_ids(self):
        """Claim identifiers are not mistaken for figures."""
        assert "1" not in extract_numbers("See [c1] and [c12].")

    @pytest.mark.unit
    def test_uncovered_number_is_reported(self, sources):
        """A figure with no claim behind it is a coverage failure."""
        c = Claim(
            id="c1",
            value=0.858,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        report = "R2 was 0.858 and the hidden figure is 0.999."
        res = check_claims([c], sources, report_text=report)
        uncovered = [r for r in res if r.verdict == "uncovered"]
        assert len(uncovered) == 1
        assert "0.999" in uncovered[0].detail


class TestSlideMapping:
    """Slide figures map to accepted claims instead of being re-audited."""

    @pytest.mark.unit
    def test_slide_value_matching_accepted_claim_passes(self):
        """A slide cell carrying an accepted claim id is verified."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="verified", value=0.858, expected=0.858
            )
        }
        spec = {
            "slides": [
                {"cells": [{"value": "0.858", "claim": "c1", "label": "overall R2"}]}
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "overall R2"}) == []

    @pytest.mark.unit
    def test_slide_value_not_matching_its_claim_fails(self):
        """A reworded number is caught by the mapping check."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="verified", value=0.858, expected=0.858
            )
        }
        spec = {
            "slides": [
                {"cells": [{"value": "0.86", "claim": "c1", "label": "overall R2"}]}
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "overall R2"})

    @pytest.mark.unit
    def test_slide_number_without_claim_fails(self):
        """A figure invented at slide time has no claim and fails."""
        spec = {"slides": [{"cells": [{"value": "0.91", "label": "x"}]}]}
        assert check_slide_spec(spec, {}, {})

    @pytest.mark.unit
    def test_declined_claim_cannot_reach_a_slide(self):
        """A claim that failed verification is not in the accepted set."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="failed", value=0.92, expected=0.858
            )
        }
        spec = {
            "slides": [
                {"cells": [{"value": "0.92", "claim": "c1", "label": "overall R2"}]}
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "overall R2"})

    @pytest.mark.unit
    def test_label_mismatch_catches_right_number_wrong_row(self):
        """A correct value under the wrong label is caught."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="verified", value=0.886, expected=0.886
            )
        }
        spec = {
            "slides": [
                {"cells": [{"value": "0.886", "claim": "c1", "label": "soybeans R2"}]}
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "maize R2"})


class TestLabelConsistency:
    """A claim's label must not contradict the data it reads.

    The label is the writer's own words, so nothing stops it naming a
    different crop or a different unit from the row the claim reads.
    A correct number under the wrong heading is still wrong, and a
    mislabelled claim passes every later check because the deck is
    checked against the label rather than against the source.

    The rule detects contradiction, not absence. A label that names a
    sibling row is caught; a label that names nothing recognizable is
    left alone, because report prose uses synonyms the data does not
    (a report says corn where the data says maize).
    """

    @pytest.mark.unit
    def test_label_naming_a_sibling_row_fails(self, sources):
        """Maize's value labelled as cotton is a wrong-row error."""
        c = Claim(
            id="c1",
            value=0.886,
            label="cotton R2",
            source="per_crop.csv",
            key="crop=maize:r2",
            kind="lookup",
        )
        res = check_claims([c], sources)[0]
        assert res.verdict == "failed"
        assert "cotton" in res.detail.lower()

    @pytest.mark.unit
    def test_label_naming_its_own_row_passes(self, sources):
        """The matching label is accepted."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            source="per_crop.csv",
            key="crop=maize:r2",
            kind="lookup",
        )
        assert check_claims([c], sources)[0].verdict == "verified"

    @pytest.mark.unit
    def test_label_using_a_synonym_is_not_failed(self, sources):
        """A report may say corn where the data says maize."""
        c = Claim(
            id="c1",
            value=0.886,
            label="corn R2",
            source="per_crop.csv",
            key="crop=maize:r2",
            kind="lookup",
        )
        assert check_claims([c], sources)[0].verdict == "verified"

    @pytest.mark.unit
    def test_wrong_unit_in_label_fails(self, sources):
        """Cotton is reported in pounds, so bushels is a contradiction."""
        c = Claim(
            id="c1",
            value=0.585,
            label="cotton R2 in BU / ACRE",
            source="per_crop.csv",
            key="crop=cotton:r2",
            kind="lookup",
        )
        res = check_claims([c], sources)[0]
        assert res.verdict == "failed"
        assert "unit" in res.detail.lower()

    @pytest.mark.unit
    def test_right_unit_in_label_passes(self, sources):
        """The row's own unit is accepted."""
        c = Claim(
            id="c1",
            value=0.585,
            label="cotton R2 in LB / ACRE",
            source="per_crop.csv",
            key="crop=cotton:r2",
            kind="lookup",
        )
        assert check_claims([c], sources)[0].verdict == "verified"

    @pytest.mark.unit
    def test_source_without_a_unit_column_is_not_checked(self, sources):
        """A JSON metric has no unit to contradict."""
        c = Claim(
            id="c1",
            value=0.858,
            label="overall R2 in BU / ACRE",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        assert check_claims([c], sources)[0].verdict == "verified"

    @pytest.mark.unit
    def test_wrong_value_is_reported_before_the_label(self, sources):
        """A wrong number is the more useful message of the two."""
        c = Claim(
            id="c1",
            value=0.99,
            label="cotton R2",
            source="per_crop.csv",
            key="crop=maize:r2",
            kind="lookup",
        )
        res = check_claims([c], sources)[0]
        assert res.verdict == "failed"
        assert "0.886" in res.detail
