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
    check_report,
    check_slide_spec,
    extract_numbers,
    load_sources,
    required_consistency_claims,
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


class TestRequiredConsistencyClaims:
    """Cross-checks are derived from the data, not offered by the writer.

    A writer that simply never declares a cross-check would pass with
    the contradiction sitting in the staged files. The checks that
    catch a table disagreeing with its own summary are exactly the
    ones nobody thinks to ask for, so the harness derives them from
    the staged schema and the writer cannot omit what it does not
    author.
    """

    @pytest.mark.unit
    def test_totals_are_required_for_every_counted_table(self, sources):
        """Each table with a record count must total the summary."""
        required = required_consistency_claims(sources)
        sources_checked = {c.inputs["a"].split(":")[0] for c in required}
        assert "tiers.csv" in sources_checked
        assert "per_crop.csv" in sources_checked

    @pytest.mark.unit
    def test_percentage_columns_are_required_to_total_100(self, sources):
        """A rate column that does not sum to 100 is a contradiction."""
        required = required_consistency_claims(sources)
        assert any(c.value == 100 for c in required)

    @pytest.mark.unit
    def test_required_claims_are_marked_as_the_harness_own(self, sources):
        """Derived claims are distinguishable from the writer's."""
        for claim in required_consistency_claims(sources):
            assert claim.id.startswith("required:")
            assert claim.kind == "consistency"

    @pytest.mark.unit
    def test_a_source_without_counts_yields_no_claim(self, tmp_path):
        """Nothing is invented for a table that counts nothing."""
        d = tmp_path / "eval"
        d.mkdir()
        (d / "metrics.json").write_text(json.dumps({"n_records": 10}))
        (d / "notes.csv").write_text("crop,comment\nmaize,fine\n")
        assert required_consistency_claims(load_sources(d)) == []

    @pytest.mark.unit
    def test_no_summary_count_means_no_total_check(self, tmp_path):
        """Without a record count there is nothing to total against."""
        d = tmp_path / "eval"
        d.mkdir()
        (d / "metrics.json").write_text(json.dumps({"r2_overall": 0.5}))
        (d / "tiers.csv").write_text("tier,n_records\nA,5\nB,5\n")
        derived = required_consistency_claims(load_sources(d))
        assert all("n_records" not in (c.inputs.get("b") or "") for c in derived)


class TestCheckReport:
    """The report-level check merges the harness's claims with the writer's."""

    @pytest.mark.unit
    def test_omitted_cross_check_still_fails(self, staged):
        """The whole point: a writer cannot skip the contradiction.

        The tier table is short of the record count. The writer
        declares only correct look-ups and never mentions it.
        """
        (staged / "tiers.csv").write_text(
            "tier,n_records,rate_pct\n"
            "Excellent,660,55.0\nGood,317,26.4\n"
            "Average,170,14.2\nPoor,48,4.0\n"
        )
        srcs = load_sources(staged)
        honest_lookup = Claim(
            id="c1",
            value=1200,
            label="records evaluated",
            source="metrics.json",
            key="n_records",
            kind="lookup",
        )
        results = check_report([honest_lookup], srcs, report_text="1200 records.")
        failed = [r for r in results if r.verdict == "failed"]
        assert failed, "the short tier table must be reported"
        assert any("required:" in r.claim_id for r in failed)

    @pytest.mark.unit
    def test_clean_data_passes(self, sources):
        """A consistent evaluation raises nothing on its own."""
        results = check_report([], sources)
        totals = [
            r
            for r in results
            if r.claim_id.startswith("required:") and "total" in r.claim_id
        ]
        assert totals
        assert all(r.verdict == "verified" for r in totals if "tiers" in r.claim_id)

    @pytest.mark.unit
    def test_writer_claims_are_kept(self, sources):
        """The writer's own claims are checked alongside."""
        c = Claim(
            id="w1",
            value=0.858,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        ids = {r.claim_id for r in check_report([c], sources)}
        assert "w1" in ids

    @pytest.mark.unit
    def test_rounded_percentages_are_tolerated(self, tmp_path):
        """Rates rounded to one decimal need not sum to exactly 100."""
        d = tmp_path / "eval"
        d.mkdir()
        (d / "metrics.json").write_text(json.dumps({"n_records": 3}))
        (d / "tiers.csv").write_text(
            "tier,n_records,rate_pct\nA,1,33.3\nB,1,33.3\nC,1,33.3\n"
        )
        results = check_report([], load_sources(d))
        pct = [r for r in results if "pct" in r.claim_id]
        assert pct and all(r.verdict == "verified" for r in pct)


class TestLabelMentionsSiblingsAsContext:
    """Naming its own row earns a label the right to mention others.

    A rehearsal produced the label "Q2 area quartile record count (Q3
    and Q4 also 381)". The value was Q2's and correct, and the
    parenthetical was useful context, but the first contradiction
    rule failed it for naming a sibling at all. That is a false
    decline, and false declines are the cost side of strictness.
    """

    @pytest.mark.unit
    def test_own_row_named_allows_a_parenthetical_sibling(self, sources):
        """The real rehearsal label must pass."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2 (cotton is lower)",
            source="per_crop.csv",
            key="crop=maize:r2",
            kind="lookup",
        )
        assert check_claims([c], sources)[0].verdict == "verified"

    @pytest.mark.unit
    def test_sibling_without_own_row_still_fails(self, sources):
        """The wrong-row error is still caught."""
        c = Claim(
            id="c1",
            value=0.886,
            label="cotton R2",
            source="per_crop.csv",
            key="crop=maize:r2",
            kind="lookup",
        )
        assert check_claims([c], sources)[0].verdict == "failed"

    @pytest.mark.unit
    def test_own_unit_named_allows_mentioning_another(self, sources):
        """The same allowance applies to units."""
        c = Claim(
            id="c1",
            value=0.585,
            label="cotton R2 in LB / ACRE, unlike the BU / ACRE crops",
            source="per_crop.csv",
            key="crop=cotton:r2",
            kind="lookup",
        )
        assert check_claims([c], sources)[0].verdict == "verified"


class TestPassingVerdictsShowTheirWorking:
    """A verdict that passes must show what it looked at.

    A bare pass cannot be argued with, and a wrong one is
    indistinguishable from a right one. Explaining only rejections
    leaves the half a report's author most wants to defend resting on
    trust, which is the shape this whole design exists to remove.
    """

    @pytest.mark.unit
    def test_passing_lookup_names_its_source(self, sources):
        """The reader can see which cell was read."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            source="per_crop.csv",
            key="crop=maize:r2",
            kind="lookup",
        )
        res = check_claims([c], sources)[0]
        assert res.verdict == "verified"
        assert "per_crop.csv" in res.detail
        assert "0.886" in res.detail

    @pytest.mark.unit
    def test_passing_derived_shows_its_inputs(self, sources):
        """The arithmetic is reproducible from the message alone."""
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
        res = check_claims([c], sources)[0]
        assert res.verdict == "verified"
        assert "817" in res.detail and "37" in res.detail

    @pytest.mark.unit
    def test_passing_consistency_shows_both_sides(self, sources):
        """A cross-check that passes names the figures it compared."""
        c = Claim(
            id="c1",
            value=True,
            label="tier total equals records",
            kind="consistency",
            expr="a == b",
            inputs={"a": "tiers.csv:sum:n_records", "b": "metrics.json:n_records"},
        )
        res = check_claims([c], sources)[0]
        assert res.verdict == "verified"
        assert "1200" in res.detail

    @pytest.mark.unit
    def test_tolerance_used_is_stated_when_one_was_declared(self, sources):
        """A pass that relied on tolerance says so."""
        c = Claim(
            id="c1",
            value=0.89,
            label="maize R2",
            source="per_crop.csv",
            key="crop=maize:r2",
            kind="lookup",
            tolerance={"type": "abs", "value": 0.01},
        )
        res = check_claims([c], sources)[0]
        assert res.verdict == "verified"
        assert "0.01" in res.detail

    @pytest.mark.unit
    def test_failure_messages_are_unchanged(self, sources):
        """Rejections keep the wording the retry handoff relies on."""
        c = Claim(
            id="c1",
            value=0.92,
            label="maize R2",
            source="per_crop.csv",
            key="crop=maize:r2",
            kind="lookup",
        )
        res = check_claims([c], sources)[0]
        assert res.verdict == "failed"
        assert res.detail == "maize R2: stated 0.92, source says 0.886"


class TestCodexFindingNegativeSigns:
    """P2: the figure-extraction regex dropped minus signs.

    A correct claim of -0.5 against a source value of -0.5 produced
    an uncovered 0.5 and failed verification; a positive 0.5 claim
    could cover an incorrect -0.5 in the prose and let a sign error
    pass. Both directions matter: the sign has to be part of the
    figure, not stripped before comparison.
    """

    @pytest.mark.unit
    def test_extract_numbers_keeps_a_leading_minus(self):
        """A negative figure in prose is extracted with its sign."""
        assert "-0.5" in extract_numbers("The correlation was -0.5.")

    @pytest.mark.unit
    def test_negative_claim_matching_negative_text_is_covered(self, sources):
        """A correct negative claim against correct negative prose passes."""
        c = Claim(
            id="c1",
            value=-0.5,
            label="bias",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
            tolerance={"type": "abs", "value": 1.0},
        )
        res = check_claims([c], sources, report_text="The bias was -0.5.")
        uncovered = [r for r in res if r.verdict == "uncovered"]
        assert uncovered == []

    @pytest.mark.unit
    def test_positive_claim_does_not_cover_a_negative_figure(self, sources):
        """A sign error in the text must not be hidden by a positive claim."""
        c = Claim(
            id="c1",
            value=0.5,
            label="bias",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
            tolerance={"type": "abs", "value": 1.0},
        )
        res = check_claims([c], sources, report_text="The bias was -0.5.")
        uncovered = [r for r in res if r.verdict == "uncovered"]
        assert len(uncovered) == 1
        assert "-0.5" in uncovered[0].detail


class TestCodexFindingRequiredClaimsDontGrantCoverage:
    """P2: a harness-injected claim silently covered a writer's bare figure.

    A required consistency claim (value 100, from a rate_pct column
    totalling 100) landed in the same coverage set as writer-declared
    claims. A report saying "accuracy is 100%" with zero declared
    claims passed coverage because the harness's own check happened
    to also be the number 100 -- the writer gets credit for a claim
    it never made.
    """

    @pytest.mark.unit
    def test_bare_figure_matching_a_required_claims_value_is_uncovered(self, sources):
        """The writer must declare its own claim; the harness's doesn't count."""
        res = check_report([], sources, report_text="Prediction accuracy is 100%.")
        uncovered = [r for r in res if r.verdict == "uncovered"]
        assert any("100" in r.detail for r in uncovered)

    @pytest.mark.unit
    def test_writer_declared_claim_still_grants_coverage(self, sources):
        """The fix must not break ordinary, honestly declared coverage."""
        c = Claim(
            id="c1",
            value=0.858,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        res = check_report([c], sources, report_text="R2 was 0.858.")
        uncovered = [r for r in res if r.verdict == "uncovered"]
        assert uncovered == []


class TestCodexFindingSlideLabelRequired:
    """P2: a slide cell with no label skipped label validation entirely.

    A cell carrying maize's verified value and claim id, titled
    "Cotton performance" but with no cell-level label, was accepted:
    the check only compared labels when both sides had one. A correct
    number can end up presented as the wrong crop's result.
    """

    @pytest.mark.unit
    def test_missing_cell_label_is_a_problem_not_skipped(self):
        """A slide cell must state which claim's label it is showing."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="verified", value=0.886, label="maize R2"
            )
        }
        spec = {"slides": [{"cells": [{"value": "0.886", "claim": "c1"}]}]}
        problems = check_slide_spec(spec, accepted, {"c1": "maize R2"})
        assert problems

    @pytest.mark.unit
    def test_empty_cell_label_is_a_problem_not_skipped(self):
        """An empty-string label is not a pass by omission either."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="verified", value=0.886, label="maize R2"
            )
        }
        spec = {"slides": [{"cells": [{"value": "0.886", "claim": "c1", "label": ""}]}]}
        problems = check_slide_spec(spec, accepted, {"c1": "maize R2"})
        assert problems

    @pytest.mark.unit
    def test_matching_label_still_passes(self):
        """The fix must not break the ordinary, correctly labelled case."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="verified", value=0.886, label="maize R2"
            )
        }
        spec = {
            "slides": [
                {"cells": [{"value": "0.886", "claim": "c1", "label": "maize R2"}]}
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "maize R2"}) == []


class TestHaikuSmokeFindingOrderedListMarkers:
    """A markdown ordered-list marker was read as a data figure.

    Found analyzing the unauthorized Haiku smoke case: a Conclusions
    section's "1. Adding confidence intervals..." produced an
    uncovered figure "1", and likewise for 2, 3, 4. This affects any
    writer on any model that uses an ordered list, so it was corrupting
    the coverage measure for every condition, not a Haiku-specific gap.
    """

    @pytest.mark.unit
    def test_ordered_list_markers_are_not_figures(self):
        """A numbered recommendation list contributes no bare figures."""
        text = (
            "Next steps:\n"
            "1. Adding confidence intervals.\n"
            "2. Implementing crop-specific variants.\n"
            "3. Weighting training data.\n"
            "4. Collecting more records.\n"
        )
        assert extract_numbers(text) == []

    @pytest.mark.unit
    def test_a_real_figure_at_the_start_of_a_sentence_is_still_extracted(self):
        """The fix must not blind the extractor to an ordinary figure."""
        assert extract_numbers("5 fields were excluded.") == ["5"]

    @pytest.mark.unit
    def test_a_decimal_after_a_list_marker_is_still_extracted(self):
        """Only the bare integer marker is excluded, not a real value
        that happens to follow one on the same line."""
        text = "1. R2 was 0.886 for this crop."
        assert extract_numbers(text) == ["0.886"]

    @pytest.mark.unit
    def test_claims_covering_the_list_items_report_no_coverage_gap(self, sources):
        """End to end: the Haiku smoke case's exact pattern now passes."""
        text = (
            "To reduce the poor-tier rate, consider:\n"
            "1. Adding confidence intervals.\n"
            "2. Implementing crop-specific variants.\n"
        )
        res = check_claims([], sources, report_text=text)
        assert [r for r in res if r.verdict == "uncovered"] == []


class TestHaikuSmokeFindingRoundedRestatement:
    """A claim restated at coarser rounding was not recognized as covered.

    claims.json declared maize R2 as 0.481 (c8); the report's
    Conclusions section restated it as "0.48", a correct rounding to
    two decimal places, and the coverage check did not recognize the
    two as the same figure.
    """

    @pytest.mark.unit
    def test_a_claim_restated_rounded_to_two_places_is_covered(self, sources):
        """0.481 restated as 0.48 must not count as an uncovered figure."""
        c = Claim(
            id="c8",
            value=0.481,
            label="maize R2",
            source="per_crop.csv",
            key="crop=maize:r2",
            kind="lookup",
        )
        res = check_claims([c], sources, report_text="Maize R2 exceeds 0.48 overall.")
        assert [r for r in res if r.verdict == "uncovered"] == []
