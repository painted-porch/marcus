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
    _inferred_claim_unit,
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


class TestCodexFindingMalformedToleranceDoesNotCrash:
    """A malformed tolerance value crashed the whole checker.

    ``{"type": "abs", "value": None}`` or ``{"type": "abs", "value":
    "0.01%"}`` are syntactically valid claim JSON, but
    ``float(tolerance["value"])`` raises uncaught, escaping
    ``_check_one`` and aborting ``verify_workspace`` and the whole
    ``load_condition_b`` grading pass over one bad claim.
    """

    @pytest.mark.unit
    def test_null_tolerance_value_is_unresolvable_not_a_crash(self, sources):
        """A None tolerance amount fails gracefully."""
        c = Claim(
            id="c1",
            value=0.858,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
            tolerance={"type": "abs", "value": None},
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_non_numeric_tolerance_value_is_unresolvable_not_a_crash(self, sources):
        """A string tolerance amount fails gracefully."""
        c = Claim(
            id="c1",
            value=0.858,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
            tolerance={"type": "abs", "value": "0.01%"},
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"


class TestCodexFindingClaimShapeIsValidatedBeforeResolution:
    """Every claim field reachable from writer JSON is checked up front.

    Round 14's finding: ``{"inputs": {"a": null}}`` passes
    ``Claim.from_dict`` (an object-valued ``inputs`` is accepted), but
    ``resolve_address`` then calls ``address.split(":")`` on ``None``
    and raises an uncaught ``AttributeError``, aborting the whole
    ``check_claims`` pass over one claim. This is the fourth round
    where writer-supplied data reached a code path that assumed its
    shape (malformed tolerance values, non-object ``slides``, a
    constant-only ``expr``, now a null input address). Rather than
    patch each field's consumer again, ``_check_one`` now validates a
    claim's shape -- source/key for a lookup, expr and every input
    address for a derived/consistency claim, and the tolerance object
    -- in one place before attempting any resolution, so the next
    malformed field is caught here too.
    """

    @pytest.mark.unit
    def test_a_null_input_address_is_unresolvable_not_a_crash(self, sources):
        """The exact payload from the finding."""
        c = Claim(
            id="c1",
            value=1,
            label="x",
            kind="derived",
            expr="a",
            inputs={"a": None},
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_a_non_string_input_address_is_unresolvable_not_a_crash(self, sources):
        """A non-null but still non-string address is caught the same way."""
        c = Claim(
            id="c1",
            value=1,
            label="x",
            kind="derived",
            expr="a",
            inputs={"a": 42},
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_an_empty_string_input_address_is_unresolvable_not_a_crash(self, sources):
        """An empty address string is not a usable address either."""
        c = Claim(
            id="c1",
            value=1,
            label="x",
            kind="derived",
            expr="a",
            inputs={"a": ""},
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_a_non_string_expr_is_unresolvable_not_a_crash(self, sources):
        """An expr that is not a string at all (e.g. a number) is caught."""
        c = Claim(
            id="c1",
            value=1,
            label="x",
            kind="derived",
            expr=999,
            inputs={"a": "metrics.json:n_records"},
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_a_lookup_claim_with_no_source_is_unresolvable_not_a_crash(self, sources):
        """A lookup claim missing its source file is caught before the
        address is even built."""
        c = Claim(
            id="c1",
            value=1,
            label="x",
            kind="lookup",
            source=None,
            key="n_records",
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_a_non_object_tolerance_is_unresolvable_not_a_crash(self, sources):
        """A tolerance that is not an object at all (e.g. a bare string)."""
        c = Claim(
            id="c1",
            value=0.858,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
            tolerance="loose",
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_a_well_formed_claim_is_unaffected(self, sources):
        """The validation gate must not disturb an ordinary, valid claim."""
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
            unit="plain",
        )
        assert check_claims([c], sources)[0].verdict == "verified"


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
            unit="plain",
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
            unit="plain",
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
            unit="percent",
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

    @pytest.mark.unit
    def test_constant_only_expression_with_no_inputs_is_unresolvable(self, sources):
        """A derived claim must recompute from staged data, not restate
        itself.

        ``expr="999"`` with ``inputs={}`` evaluates to 999 regardless
        of anything staged, so a claim whose value is 999 would
        "verify" by matching an assertion with nothing behind it.
        """
        c = Claim(
            id="c1",
            value=999,
            label="fabricated",
            kind="derived",
            expr="999",
            inputs={},
        )
        assert check_claims([c], sources)[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_an_unused_decoy_input_does_not_rescue_a_constant_expression(self, sources):
        """Declaring an input the expression never references is the
        same bypass wearing a disguise."""
        c = Claim(
            id="c1",
            value=999,
            label="fabricated",
            kind="derived",
            expr="999",
            inputs={"a": "metrics.json:n_records"},
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
            unit="plain",
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
            unit="plain",
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
        report = "R2 was 0.858 [c1] and the hidden figure is 0.999."
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


class TestCodexFindingSlideCellClaimIdMustBeAString:
    """A non-string claim id crashed the slide checker.

    Round 15's finding: a cell with ``"claim": ["c1"]`` passes
    ``load_slide_spec``'s structural checks (the cells are objects
    inside valid lists -- nothing there inspects individual field
    values), then crashes ``accepted.get(claim_id)`` with an
    uncaught ``TypeError: unhashable type: 'list'``, in both the
    title-check pass and the main per-cell loop. Same class of bug as
    round 14 (a document the writer controls reaching an operation
    that assumes its shape), on the slide spec instead of the claims
    file.
    """

    @pytest.mark.unit
    def test_a_list_valued_claim_id_is_a_named_problem_not_a_crash(self):
        """The exact payload from the finding."""
        accepted = {
            "c1": CheckResult(claim_id="c1", verdict="verified", value=1, label="x")
        }
        spec = {"slides": [{"cells": [{"value": "1", "claim": ["c1"]}]}]}
        problems = check_slide_spec(spec, accepted, {"c1": "x"})
        assert problems

    @pytest.mark.unit
    def test_an_object_valued_claim_id_is_a_named_problem_not_a_crash(self):
        """A dict-valued claim id is just as unhashable."""
        accepted = {
            "c1": CheckResult(claim_id="c1", verdict="verified", value=1, label="x")
        }
        spec = {"slides": [{"cells": [{"value": "1", "claim": {"id": "c1"}}]}]}
        problems = check_slide_spec(spec, accepted, {"c1": "x"})
        assert problems

    @pytest.mark.unit
    def test_a_list_valued_claim_id_in_a_titled_slide_does_not_crash(self):
        """The title-check pass has its own, separate lookup over cells."""
        accepted = {
            "c1": CheckResult(claim_id="c1", verdict="verified", value=1, label="x")
        }
        spec = {
            "slides": [{"title": "Summary", "cells": [{"value": "1", "claim": ["c1"]}]}]
        }
        problems = check_slide_spec(spec, accepted, {"c1": "x"})
        assert problems

    @pytest.mark.unit
    def test_a_well_formed_claim_id_is_unaffected(self):
        """The fix must not disturb an ordinary, valid string claim id."""
        accepted = {
            "c1": CheckResult(claim_id="c1", verdict="verified", value=1, label="x")
        }
        spec = {"slides": [{"cells": [{"value": "1", "claim": "c1", "label": "x"}]}]}
        assert check_slide_spec(spec, accepted, {"c1": "x"}) == []


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
            unit="plain",
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
            unit="plain",
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
        res = check_claims([c], sources, report_text="The bias was -0.5 [c1].")
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
        res = check_report([c], sources, report_text="R2 was 0.858 [c1].")
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
        res = check_claims(
            [c], sources, report_text="Maize R2 exceeds 0.48 [c8] overall."
        )
        assert [r for r in res if r.verdict == "uncovered"] == []


class TestCodexFindingMultiplierSuffix:
    """A ratio written as "~23x" was invisible to the figure extractor.

    PROTOCOL.md's motivating example is exactly this shape: a stated
    ratio of "~23x" that recomputed to 22.1. The old regex's trailing
    negative lookahead treated the "x" as a disqualifying word
    character, so the number never matched at all and the ratio could
    ship with no coverage claim and no chance of being caught.
    """

    @pytest.mark.unit
    def test_integer_ratio_with_multiplier_suffix_is_extracted(self):
        """ "~23x" yields the figure 23, not nothing."""
        assert extract_numbers("Maize has ~23x as many records as cotton.") == ["23"]

    @pytest.mark.unit
    def test_decimal_ratio_with_multiplier_suffix_is_extracted_in_full(self):
        """ "22.1x" yields 22.1, not the truncated 22."""
        assert extract_numbers("The true ratio is 22.1x cotton's count.") == ["22.1"]

    @pytest.mark.unit
    def test_multiplier_suffix_does_not_swallow_a_following_word(self):
        """A digit run glued to real letters is still not a figure."""
        assert extract_numbers("See table 23xyz for details.") == []


class TestCodexFindingCommaGroupedDecimal:
    """A comma-grouped figure lost its fractional part on extraction.

    The comma-grouped alternative of the number regex had no optional
    decimal suffix, so "1,234.5" matched only "1,234" and the stated
    figure silently changed value before it ever reached the checker.
    """

    @pytest.mark.unit
    def test_comma_grouped_figure_keeps_its_decimal_part(self):
        """ "1,234.5" is extracted whole, not truncated to "1,234"."""
        assert extract_numbers("A value of 1,234.5 was recorded.") == ["1,234.5"]

    @pytest.mark.unit
    def test_comma_grouped_integer_is_still_extracted_without_a_decimal(self):
        """The common integer case is unaffected by the fix."""
        assert extract_numbers("1,234 records were evaluated.") == ["1,234"]


class TestCodexFindingSumAddressMissingColumn:
    """A malformed "sum" address crashed the checker instead of failing

    one claim.

    ``resolve_address`` read ``rest[1]`` for the column name without
    checking ``rest`` had a second element, so an address like
    "tiers.csv:sum" (no ":column" suffix) raised an uncaught
    ``IndexError`` instead of the ``KeyError`` every other malformed
    address produces. An uncaught exception from one claim's address
    takes down the whole run, not just that claim.
    """

    @pytest.mark.unit
    def test_sum_address_missing_a_column_is_unresolvable_not_a_crash(self, sources):
        """A sum address with no column name fails gracefully."""
        c = Claim(
            id="c1",
            value=1,
            label="broken aggregate",
            source="tiers.csv",
            key="sum",
            kind="lookup",
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"


class TestCodexFindingSumOfNonNumericColumnDoesNotCrash:
    """A "sum" address selecting a nonnumeric column crashed the checker.

    ``resolve_address``'s sum branch calls ``float(_coerce(row[column]))``
    unconditionally. ``_coerce`` returns a string unchanged when it
    cannot be parsed as a number, so summing a text column (e.g. the
    "crop" column, "maize"/"cotton"/...) raised an uncaught
    ``ValueError`` from ``float()`` instead of the ``KeyError`` every
    other malformed address produces, taking down the whole run over
    one bad claim.
    """

    @pytest.mark.unit
    def test_summing_a_text_column_is_unresolvable_not_a_crash(self, sources):
        """A sum address over a nonnumeric column fails gracefully."""
        c = Claim(
            id="c1",
            value=1,
            label="broken aggregate",
            source="per_crop.csv",
            key="sum:crop",
            kind="lookup",
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"


class TestCodexFindingBooleanNumericConfusion:
    """A boolean truth value could be matched by any nonzero number.

    ``_within_tolerance`` treated "either side is a bool" as "compare
    as booleans", so a consistency claim whose computed truth is
    ``True`` would accept a stated value of, say, ``42`` because
    ``bool(42) == bool(True)``. A boolean and a genuine number are
    different kinds of claim and must never be treated as equal.
    """

    @pytest.mark.unit
    def test_a_nonzero_number_does_not_satisfy_a_boolean_truth(self, sources):
        """A numeric claim value must not pass against a boolean truth."""
        required = [
            c
            for c in required_consistency_claims(sources)
            if c.id == "required:tiers-total"
        ]
        assert required, "fixture tiers.csv must balance against n_records"
        broken = Claim(
            id=required[0].id,
            value=42,
            label=required[0].label,
            kind=required[0].kind,
            expr=required[0].expr,
            inputs=required[0].inputs,
            unit=required[0].unit,
        )
        res = check_claims([broken], sources)
        assert len(res) == 1
        assert res[0].verdict == "failed"

    @pytest.mark.unit
    def test_matching_booleans_still_pass(self, sources):
        """The fix must not break a genuine boolean-to-boolean match."""
        required = [
            c
            for c in required_consistency_claims(sources)
            if c.id == "required:tiers-total"
        ]
        assert required
        res = check_claims(list(required), sources)
        assert [r for r in res if r.verdict == "failed"] == []


class TestCodexFindingLeadingDecimalFigures:
    """A figure written without a leading zero was invisible to extraction.

    ".99" and "-.5" both require a digit before the decimal point in
    the old regex, and the negative lookbehind blocked matching the
    fractional digits as their own figure, so neither pattern matched
    anything at all. A report could state such a figure with zero
    claim coverage and the checker would never notice.
    """

    @pytest.mark.unit
    def test_leading_decimal_without_a_zero_is_extracted(self):
        """ ".99" yields the figure 0.99's own written form."""
        assert extract_numbers("Accuracy is .99.") == [".99"]

    @pytest.mark.unit
    def test_negative_leading_decimal_is_extracted_with_its_sign(self):
        """ "-.5" keeps its sign, matching the existing negative-sign fix."""
        assert extract_numbers("The bias was -.5.") == ["-.5"]

    @pytest.mark.unit
    def test_an_uncovered_leading_decimal_figure_is_caught(self, sources):
        """End to end: a bare ".99" with no claim is a coverage gap."""
        res = check_claims([], sources, report_text="Accuracy is .99.")
        assert [r for r in res if r.verdict == "uncovered"] != []


class TestCodexFindingScientificNotation:
    """A figure in scientific notation was missed or split into fragments.

    The old regex had no exponent support, so "1e3" matched nothing
    at all (the trailing lookahead treated "e" as a disqualifying
    word character) and "1.2e-3" fragmented into the unrelated bare
    figures "1" and "3". Either direction lets a figure bypass claim
    coverage or reports a spurious one.
    """

    @pytest.mark.unit
    def test_integer_exponent_is_extracted_whole(self):
        """ "1e3" is extracted as one figure, not missed entirely."""
        assert extract_numbers("The evaluation has 1e3 records.") == ["1e3"]

    @pytest.mark.unit
    def test_negative_exponent_decimal_is_extracted_whole(self):
        """ "1.2e-3" is extracted whole, not fragmented into 1 and 3."""
        assert extract_numbers("Error rate is 1.2e-3 overall.") == ["1.2e-3"]

    @pytest.mark.unit
    def test_an_uncovered_scientific_figure_is_caught(self, sources):
        """End to end: a bare "1e3" with no claim is a coverage gap."""
        res = check_claims([], sources, report_text="The evaluation has 1e3 records.")
        assert [r for r in res if r.verdict == "uncovered"] != []


class TestCodexFindingIllTypedExpressionCrashesTheRun:
    """A type-mismatched derived expression crashed the whole checker.

    ``a / "b"`` (a string resolved where a number was expected) raised
    an uncaught TypeError from _safe_eval's arithmetic, escaping
    _check_one entirely. One malformed claim could abort
    verify_workspace and the whole load_condition_b grading pass
    instead of producing an unresolvable verdict for that one claim.
    """

    @pytest.mark.unit
    def test_ill_typed_expression_is_unresolvable_not_a_crash(self, staged):
        """A string operand in arithmetic fails gracefully, not fatally."""
        (staged / "labels.json").write_text('{"name": "maize"}')
        sources_with_string = load_sources(staged)
        c = Claim(
            id="c1",
            value=1,
            label="broken derived claim",
            kind="derived",
            expr="a / b",
            inputs={
                "a": "metrics.json:n_records",
                "b": "labels.json:name",
            },
        )
        res = check_claims([c], sources_with_string)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"


class TestCodexFindingSlideTitleFiguresAreChecked:
    """A number in a slide's title was never checked against anything.

    check_slide_spec only inspected ``cells``, so a slide titled
    "Accuracy is 99%" with otherwise valid, claim-backed cells still
    verified even though nothing anywhere claims 99. A figure outside
    the claim-backed cells could reach the deck through the title.
    """

    @pytest.mark.unit
    def test_title_figure_with_no_backing_claim_on_the_slide_fails(self):
        """An invented number in the title is caught, not ignored."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="verified", value=0.858, expected=0.858
            )
        }
        spec = {
            "slides": [
                {
                    "title": "Accuracy is 99%",
                    "cells": [{"value": "0.858", "claim": "c1", "label": "overall R2"}],
                }
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "overall R2"})

    @pytest.mark.unit
    def test_title_figure_matching_a_backed_cell_passes(self):
        """A title restating a figure already backed by a cell is fine."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="verified", value=0.858, expected=0.858
            )
        }
        spec = {
            "slides": [
                {
                    "title": "Overall R2 is 0.858",
                    "cells": [{"value": "0.858", "claim": "c1", "label": "overall R2"}],
                }
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "overall R2"}) == []

    @pytest.mark.unit
    def test_a_title_with_no_figures_at_all_is_unaffected(self):
        """A purely descriptive title is not required to carry a claim."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="verified", value=0.858, expected=0.858
            )
        }
        spec = {
            "slides": [
                {
                    "title": "Model Performance Summary",
                    "cells": [{"value": "0.858", "claim": "c1", "label": "overall R2"}],
                }
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "overall R2"}) == []


class TestMandatoryClaimTagBinding:
    """Coverage binds to the claim a figure is explicitly tagged with.

    #827: value-only coverage let a figure borrow any claim sharing
    its number, including the wrong one (a correct maize R2 covering
    a false claim about cotton's R2, if the two happen to share a
    value). WRITER_INSTRUCTIONS now requires every figure to carry a
    [cN] tag naming its own claim; coverage resolves that tag to a
    specific claim and checks the figure against only that claim's
    value, not the whole claimed-value set.
    """

    @pytest.mark.unit
    def test_untagged_figure_is_uncovered_even_with_a_matching_claim(self, sources):
        """A figure with no tag fails, even if some claim has its value."""
        c = Claim(
            id="c1",
            value=0.858,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        res = check_claims([c], sources, report_text="R2 was 0.858.")
        uncovered = [r for r in res if r.verdict == "uncovered"]
        assert len(uncovered) == 1
        assert "no claim tag" in uncovered[0].detail

    @pytest.mark.unit
    def test_tag_naming_a_nonexistent_claim_is_uncovered(self, sources):
        """A tag that doesn't resolve to any declared claim fails."""
        res = check_claims([], sources, report_text="R2 was 0.858 [c9].")
        uncovered = [r for r in res if r.verdict == "uncovered"]
        assert len(uncovered) == 1
        assert "c9" in uncovered[0].detail

    @pytest.mark.unit
    def test_wrong_number_under_the_right_tag_is_uncovered(self, sources):
        """A stated figure that disagrees with its own tagged claim is a
        coverage gap, not a distinct failure.

        This is the half of #827 that tag-binding alone closes: the
        figure's value must match the value of the specific claim it
        is tagged with, not any claim sharing that number. Classified
        as VERDICT_UNCOVERED, not VERDICT_FAILED: a figure with no
        valid claim behind it is exactly what "uncovered" means,
        whether that's because it carries no tag or because its tag
        points to a claim that doesn't actually support it. Coding it
        as a distinct FAILED result instead would corrupt
        numeric_verify.verify_workspace's coverage math and
        numeric_grader.score_case's coverage_gaps, both of which key
        on VERDICT_UNCOVERED (Codex finding on #828).

        Catching a number that is correct in VALUE but attributed to
        the wrong LABEL in the surrounding prose (the full
        maize/cotton example in #827) is the harder, separately
        tracked second half.
        """
        c = Claim(
            id="c1",
            value=0.585,
            label="cotton R2",
            source="per_crop.csv",
            key="crop=cotton:r2",
            kind="lookup",
        )
        res = check_claims([c], sources, report_text="Cotton's R2 is 0.886 [c1].")
        uncovered = [r for r in res if r.verdict == "uncovered"]
        assert len(uncovered) == 1
        assert uncovered[0].expected == 0.585

    @pytest.mark.unit
    def test_a_claim_value_elsewhere_does_not_rescue_a_mistagged_figure(self, sources):
        """A wrong figure cannot be excused by a different, correct claim
        happening to share the same tag's expected value elsewhere."""
        maize = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            source="per_crop.csv",
            key="crop=maize:r2",
            kind="lookup",
        )
        cotton = Claim(
            id="c2",
            value=0.585,
            label="cotton R2",
            source="per_crop.csv",
            key="crop=cotton:r2",
            kind="lookup",
        )
        report = "Maize R2 is 0.886 [c1]. Cotton's R2 is 0.886 [c2]."
        res = check_claims([maize, cotton], sources, report_text=report)
        uncovered = [
            r
            for r in res
            if r.verdict == "uncovered" and r.claim_id == "uncovered:0.886"
        ]
        assert len(uncovered) == 1
        assert uncovered[0].expected == 0.585

    @pytest.mark.unit
    def test_correctly_tagged_figure_is_covered(self, sources):
        """The ordinary case: a figure tagged with its own, matching claim."""
        c = Claim(
            id="c1",
            value=0.858,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        res = check_claims([c], sources, report_text="R2 was 0.858 [c1].")
        assert [r for r in res if r.verdict in ("uncovered", "failed")] == []

    @pytest.mark.unit
    def test_an_orphan_tag_with_no_preceding_number_is_ignored(self, sources):
        """A stray [cN] with no number before it binds to nothing."""
        c = Claim(
            id="c1",
            value=0.858,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        res = check_claims(
            [c],
            sources,
            report_text="See claim [c1] for the overall R2 of 0.858 [c1].",
        )
        assert [r for r in res if r.verdict in ("uncovered", "failed")] == []


class TestCodexFindingRoundingLeniencyIsBounded:
    """Coverage's rounding allowance accepted any precision, even zero.

    ``_figure_matches_claim_value`` tried rounding the claim's value
    to 0-3 decimal places and accepted any of them as "the same
    figure." ``round(0.481, 0)`` is ``0``, so a report tagging a bare
    ``0`` with a claim whose real value is ``0.481`` was accepted as
    covered -- a materially wrong figure (a 100% relative error, not
    rounding noise) slipping through because "0" happened to appear
    in the candidate set.
    """

    @pytest.mark.unit
    def test_rounding_a_fraction_down_to_zero_is_not_the_same_figure(self, tmp_path):
        """A bare 0 tagged with a claim of 0.481 is a value mismatch.

        Codex's exact example. 0.481 must round to 0 at zero decimal
        places (unlike the fixture's other R2 values, which round to
        1), so this uses its own minimal source rather than the
        shared fixture.
        """
        d = tmp_path / "eval"
        d.mkdir()
        (d / "metrics.json").write_text('{"r2_overall": 0.481}')
        tiny_sources = load_sources(d)
        c = Claim(
            id="c1",
            value=0.481,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        res = check_claims([c], tiny_sources, report_text="Overall R2 is 0 [c1].")
        bad = [r for r in res if r.verdict in ("uncovered", "failed")]
        assert len(bad) == 1

    @pytest.mark.unit
    def test_genuine_two_decimal_rounding_still_covered(self, sources):
        """The fix must not break the ordinary rounding case it allows."""
        c = Claim(
            id="c1",
            value=0.858,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        res = check_claims([c], sources, report_text="R2 was 0.86 [c1].")
        assert [r for r in res if r.verdict in ("uncovered", "failed")] == []

    @pytest.mark.unit
    def test_a_declared_tolerance_is_honored_for_a_rounded_restatement(self, sources):
        """A figure outside the generic rounding guess but inside the
        claim's own declared tolerance is still covered."""
        c = Claim(
            id="c1",
            value=0.858,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
            tolerance={"type": "abs", "value": 0.1},
        )
        res = check_claims([c], sources, report_text="R2 was about 0.8 [c1].")
        assert [r for r in res if r.verdict in ("uncovered", "failed")] == []

    @pytest.mark.unit
    def test_malformed_tolerance_in_the_coverage_path_does_not_crash(self, sources):
        """A null tolerance value reached via a tagged figure's explicit-
        tolerance check must fail that figure gracefully, not crash
        the whole pass.

        This is a different call path than _check_one's own claim
        resolution (already hardened): _figure_matches_claim_value
        calls _within_tolerance directly when a figure falls outside
        the generic rounding guess, with no exception handling.
        """
        c = Claim(
            id="c1",
            value=0.481,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
            tolerance={"type": "abs", "value": None},
        )
        res = check_claims([c], sources, report_text="Overall R2 is 0.6 [c1].")
        bad = [r for r in res if r.verdict in ("uncovered", "failed")]
        assert len(bad) >= 1


class TestCodexFindingArithmeticBlowupsDoNotCrashTheRun:
    """A derived claim with valid numeric operands can still blow up.

    ``_safe_eval`` only caught ``ValueError``, ``ZeroDivisionError``
    and ``TypeError``. ``a ** 1000`` with a well-formed float operand
    raises ``OverflowError`` instead (Python's float ``**`` raises
    when the result cannot be represented, where int ``**`` would
    just keep computing), which escaped the handler and would have
    aborted ``verify_workspace`` and the whole ``load_condition_b``
    grading pass over one malformed claim. The whole arithmetic
    family -- including exceptions a pathological expression can
    raise even though every individual operand resolved fine -- is
    now caught in one place.
    """

    @pytest.mark.unit
    def test_an_overflowing_power_is_unresolvable_not_a_crash(self, sources):
        """Codex's exact example: a ** 1000 with a well-formed float."""
        c = Claim(
            id="c1",
            value=1,
            label="blown-up power",
            kind="derived",
            expr="a ** 1000",
            inputs={"a": "metrics.json:mean_ape"},
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_a_derived_division_by_a_staged_zero_is_unresolvable(self, sources):
        """A staged value of zero as a denominator must not crash."""
        c = Claim(
            id="c1",
            value=1,
            label="division by a staged zero",
            kind="derived",
            expr="a / b",
            inputs={
                "a": "metrics.json:n_records",
                "b": "tiers.csv:tier=Missing:n_records",
            },
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_a_pathologically_nested_expression_is_unresolvable(self, sources):
        """Thousands of nested unary minuses must not blow the Python
        call stack out of this one claim's evaluation."""
        c = Claim(
            id="c1",
            value=1,
            label="pathologically nested",
            kind="derived",
            expr="-" * 5000 + "a",
            inputs={"a": "metrics.json:n_records"},
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_an_exponent_past_the_safety_cap_is_unresolvable(self, sources):
        """An exponent with no legitimate use in this trial is refused
        before arbitrary-precision integer math has a chance to hang
        or exhaust memory, rather than relying on an exception a huge
        int power may never actually raise."""
        c = Claim(
            id="c1",
            value=1,
            label="exponent past the safety cap",
            kind="derived",
            expr="a ** 100000000",
            inputs={"a": "metrics.json:n_records"},
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_a_memory_error_from_evaluation_is_unresolvable(self, sources, monkeypatch):
        """A pathological expression that exhausts memory during
        evaluation itself -- not merely a large exponent, which is
        caught earlier -- must still fail that one claim gracefully.

        Actually exhausting memory in a test would be slow and unsafe,
        so this simulates the exception _safe_eval's own documentation
        promises to raise under pathological input.
        """
        import runners.numeric_claims as numeric_claims_module

        def _raise_memory_error(node, names):
            raise MemoryError("simulated exhaustion")

        monkeypatch.setattr(numeric_claims_module, "_safe_eval", _raise_memory_error)
        c = Claim(
            id="c1",
            value=1,
            label="simulated memory exhaustion",
            kind="derived",
            expr="a + 1",
            inputs={"a": "metrics.json:n_records"},
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"


class TestCodexFindingUnicodeNumericForms:
    """A model can write a number's sign, digits, or grouping in
    several Unicode forms the ASCII-only extractor missed entirely.

    Codex's exact demonstration: "Overall R2 is -0.481 [c1]." written
    with the Unicode MINUS SIGN (U+2212), not the ASCII hyphen,
    against a claim and source value of 0.481. The old extractor
    never recognized U+2212 as part of a number at all, so the
    figure it saw was the unsigned "0.481" -- a match, full coverage,
    no mismatch -- even though the report states the opposite sign.
    The same extractor backs slide-title checking through
    ``extract_numbers``, so the same bug let a wrong-signed title
    figure through there too.

    Swept in the same change, since this is the first finding about
    the text layer rather than the JSON layer: every other Unicode
    form a model might write a number in, with a decision recorded
    for each.

    - U+2212 minus, U+FE63 small hyphen-minus, U+FF0D fullwidth
      hyphen-minus: always a sign, normalized to ASCII '-'.
    - U+2013 en dash, U+2014 em dash: normalized to '-' only when
      immediately before a digit, so "10-20" (prose range) still
      reads as two figures and is not swallowed as a negative one --
      the existing extractor's own word-boundary rule already
      guarantees this once the dash becomes a hyphen.
    - Fullwidth digits (U+FF10-U+FF19): normalized to ASCII digits,
      so a fullwidth-written figure's captured text is consistent
      with everything downstream that treats it as a plain string.
    - Thousands grouping with U+00A0 (no-break space) or U+202F
      (narrow no-break space) between digit groups: normalized to a
      comma, joining the groups the same way the existing
      comma-grouped pattern already does.
    - Per-mille (U+2030): already tokenizes correctly with no change,
      the same way '%' already does -- neither is a word character,
      so the existing word-boundary lookahead already excludes it
      from the figure.
    - Superscript digits (category "No", not "Nd"): deliberately NOT
      normalized to plain digits. Python's \\d already excludes them,
      and normalizing "10²" (ten squared) into the literal digit
      string "102" would silently fabricate a wrong figure -- worse
      than the status quo of not recognizing exponent notation at
      all, which this trial's writer does not use.
    - A European decimal comma ("0,481" meaning 0.481): explicitly
      NOT handled. This trial's writer prompt and staged data are
      English/US-formatted (period decimal, comma thousands), and
      distinguishing a decimal comma from the existing comma-grouped
      thousands pattern from text shape alone is not reliable --
      attempting it risks corrupting the comma-grouping case that is
      already in real use. A known, accepted limitation, not a bug
      this change fixes.
    """

    @pytest.mark.unit
    def test_unicode_minus_sign_is_extracted_with_its_sign(self):
        """Codex's own character: U+2212, not the ASCII hyphen."""
        assert extract_numbers("R2 is −0.481.") == ["-0.481"]

    @pytest.mark.unit
    def test_a_sign_flip_under_a_unicode_minus_is_caught_end_to_end(self, staged):
        """Codex's full sentence, against a real source and claim."""
        (staged / "metrics.json").write_text(
            json.dumps({"n_records": 1200, "r2_overall": 0.481})
        )
        sources = load_sources(staged)
        c = Claim(
            id="c1",
            value=0.481,
            label="overall R2",
            source="metrics.json",
            key="r2_overall",
            kind="lookup",
        )
        res = check_claims([c], sources, report_text="Overall R2 is −0.481 [c1].")
        bad = [r for r in res if r.verdict in ("failed", "uncovered")]
        assert bad != []

    @pytest.mark.unit
    def test_a_sign_flip_in_a_slide_title_is_caught(self):
        """The same bug through the slide-title path: a positive
        claim does not back a negative-signed title figure."""
        spec = {
            "slides": [
                {
                    "title": "Bias is −0.481",
                    "cells": [{"value": 0.481, "claim": "c1", "label": "bias"}],
                }
            ]
        }
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="verified", value=0.481, label="bias"
            )
        }
        problems = check_slide_spec(spec, accepted, {"c1": "bias"})
        assert any("title states -0.481" in p for p in problems)

    @pytest.mark.unit
    def test_small_hyphen_minus_is_normalized(self):
        """U+FE63, a compatibility form, is also always a sign."""
        assert extract_numbers("Bias is ﹣0.5.") == ["-0.5"]

    @pytest.mark.unit
    def test_fullwidth_hyphen_minus_is_normalized(self):
        """U+FF0D, a compatibility form, is also always a sign."""
        assert extract_numbers("Bias is －0.5.") == ["-0.5"]

    @pytest.mark.unit
    def test_en_dash_immediately_before_a_digit_is_a_minus_sign(self):
        """U+2013 before a digit, with a word boundary before it."""
        assert extract_numbers("Bias is –0.5.") == ["-0.5"]

    @pytest.mark.unit
    def test_em_dash_immediately_before_a_digit_is_a_minus_sign(self):
        """U+2014 before a digit, with a word boundary before it."""
        assert extract_numbers("Bias is —0.5.") == ["-0.5"]

    @pytest.mark.unit
    def test_an_en_dash_range_is_not_misread_as_a_negative_number(self):
        """ "10-20" (a prose range) must still read as two figures,
        not one figure and a sign error, because the dash in a range
        is never at a number's own word boundary."""
        assert extract_numbers("Values ranged 10–20 overall.") == [
            "10",
            "20",
        ]

    @pytest.mark.unit
    def test_fullwidth_digits_are_normalized_to_ascii(self):
        """A figure written entirely in fullwidth digits is still one
        recognizable, ASCII-stringed figure."""
        assert extract_numbers("Accuracy is ０.９９.") == ["0.99"]

    @pytest.mark.unit
    def test_a_nbsp_grouped_thousands_figure_is_joined(self):
        """U+00A0 between digit groups is a thousands separator, the
        same convention the existing comma-grouped pattern serves.

        Built with chr() rather than a literal character: a
        no-break space is invisible in a diff, so spelling out its
        codepoint is the only way a reviewer can tell what is
        actually there.
        """
        nbsp = chr(0x00A0)
        assert extract_numbers(f"Record count: 1{nbsp}200 was used.") == ["1,200"]

    @pytest.mark.unit
    def test_a_narrow_nbsp_grouped_thousands_figure_is_joined(self):
        """U+202F, the SI-recommended grouping space, is also joined."""
        narrow_nbsp = chr(0x202F)
        assert extract_numbers(f"Record count: 1{narrow_nbsp}200 was used.") == [
            "1,200"
        ]

    @pytest.mark.unit
    def test_a_multi_group_nbsp_thousands_figure_is_joined(self):
        """More than one grouping space in the same figure."""
        narrow_nbsp = chr(0x202F)
        text = f"Population: 1{narrow_nbsp}200{narrow_nbsp}000 people."
        assert extract_numbers(text) == ["1,200,000"]

    @pytest.mark.unit
    def test_per_mille_already_tokenizes_without_change(self):
        """Decision: no normalization needed, the same as '%' already."""
        assert extract_numbers("Rate is 95‰ overall.") == ["95"]

    @pytest.mark.unit
    def test_superscript_exponent_is_not_merged_into_the_base_figure(self):
        """Decision: reject, not normalize -- "10 squared" must not
        become the fabricated literal figure 102."""
        assert extract_numbers("The area is 10² units.") == ["10"]


class TestCodexFindingMechanicalFieldSweep:
    """Every field _malformed_claim_shape reads, fuzzed the same way.

    Round 21's finding (numeric_writer.collect_output's id field) was
    the second time a claims gate missed a field -- round 14 was the
    first, for this very function. To make the next one mechanical
    instead of another one-off patch, every field this gate reads --
    source, key, expr, inputs, and tolerance -- gets the same five
    shapes: None, a list, an object, a number, and an empty string.

    Fields: source, key (lookup); expr, inputs (derived/consistency);
    tolerance (either kind).
    """

    BAD_SHAPES = [None, ["x"], {"k": "v"}, 5, ""]

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_source", BAD_SHAPES, ids=repr)
    def test_a_malformed_lookup_source_is_unresolvable(self, sources, bad_source):
        """source."""
        c = Claim(
            id="c1",
            value=1,
            label="x",
            kind="lookup",
            source=bad_source,
            key="n_records",
        )
        assert check_claims([c], sources)[0].verdict == "unresolvable"

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_key", BAD_SHAPES, ids=repr)
    def test_a_malformed_lookup_key_is_unresolvable(self, sources, bad_key):
        """key."""
        c = Claim(
            id="c1",
            value=1,
            label="x",
            kind="lookup",
            source="metrics.json",
            key=bad_key,
        )
        assert check_claims([c], sources)[0].verdict == "unresolvable"

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_expr", BAD_SHAPES, ids=repr)
    def test_a_malformed_derived_expr_is_unresolvable(self, sources, bad_expr):
        """expr."""
        c = Claim(
            id="c1",
            value=1,
            label="x",
            kind="derived",
            expr=bad_expr,
            inputs={"a": "metrics.json:n_records"},
        )
        assert check_claims([c], sources)[0].verdict == "unresolvable"

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_unit", BAD_SHAPES, ids=repr)
    def test_a_malformed_derived_unit_is_unresolvable_not_a_crash(
        self, sources, bad_unit
    ):
        """unit. An unhashable shape (a list or an object) must be
        rejected the same way any other bad shape is, not raise a
        TypeError at a membership check and abort the whole pass."""
        c = Claim(
            id="c1",
            value=1,
            label="x",
            kind="derived",
            expr="a",
            inputs={"a": "metrics.json:n_records"},
            unit=bad_unit,
        )
        assert check_claims([c], sources)[0].verdict == "unresolvable"

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_inputs", [["x"], "a", 5], ids=repr)
    def test_a_malformed_inputs_field_itself_is_unresolvable(self, sources, bad_inputs):
        """inputs, as a whole, not just the addresses inside it.

        Defense-in-depth, not a reachable-today bug: Claim.from_dict
        always coerces this field to a dict or raises an exception
        the caller already catches, and the harness's own direct
        Claim(...) constructions (required_consistency_claims) always
        pass a well-formed dict literal. The gate should not depend
        on its only callers happening to be safe.
        """
        c = Claim(
            id="c1", value=1, label="x", kind="derived", expr="a", inputs=bad_inputs
        )
        assert check_claims([c], sources)[0].verdict == "unresolvable"

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_tolerance", [["x"], 5, ""], ids=repr)
    def test_a_malformed_tolerance_is_unresolvable(self, sources, bad_tolerance):
        """tolerance -- excluding None (explicitly means exact) and a
        well-formed object (the valid shape) from the bad-shape set."""
        c = Claim(
            id="c1",
            value=0.9,
            label="x",
            kind="lookup",
            source="metrics.json",
            key="r2_overall",
            tolerance=bad_tolerance,
        )
        assert check_claims([c], sources)[0].verdict == "unresolvable"

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_tolerance", [["x"], 5, ""], ids=repr)
    def test_a_malformed_tolerance_does_not_crash_check_report_end_to_end(
        self, sources, bad_tolerance
    ):
        """The same bad shape, driven through check_report (required
        claims plus report_text) rather than _check_one directly --
        this is where a malformed claim's tolerance previously slipped
        past the gate and crashed on a bare value's .get(). The
        figure must not already match the claim's value exactly, or
        the comparison short-circuits before ever reaching
        tolerance -- "0.91" against 0.9 forces the tolerance-aware
        branch, the one that actually calls tolerance.get()."""
        c = Claim(
            id="c1",
            value=0.9,
            label="x",
            kind="lookup",
            source="metrics.json",
            key="r2_overall",
            tolerance=bad_tolerance,
        )
        results = check_report([c], sources, report_text="R2 is 0.91 [c1].")
        bad = [r for r in results if r.claim_id in ("c1", "uncovered:0.91")]
        assert bad and all(
            r.verdict == "unresolvable" for r in bad if r.claim_id == "c1"
        )

    @pytest.mark.unit
    def test_tolerance_of_none_is_still_exact_not_malformed(self, sources):
        """None is a legitimate value for this field, not a bad shape."""
        c = Claim(
            id="c1",
            value=0.858,
            label="x",
            kind="lookup",
            source="metrics.json",
            key="r2_overall",
            tolerance=None,
        )
        assert check_claims([c], sources)[0].verdict == "verified"


class TestCodexFindingEmptyClaimLabelDisablesWrongRowProtection:
    """An omitted label let a right-number-wrong-row figure through.

    Claim.from_dict defaults a missing label to "". Report
    verification accepted such a claim, and check_slide_spec's own
    label comparison skipped both its checks whenever expected_label
    was falsy -- an empty label is exactly as falsy as a missing one.
    Codex's demonstration: a verified maize R2 displayed on a slide
    under "wheat R2," with a fully "verified" deck, because the
    claim backing it was never given a label to contradict.
    """

    @pytest.mark.unit
    def test_a_claim_with_no_label_is_unresolvable_not_verified(self, sources):
        """A report claim must carry a nonempty label to be accepted
        at all -- the same way every other field this gate checks is
        required before resolution is attempted."""
        c = Claim(
            id="c1",
            value=0.886,
            label="",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        assert check_claims([c], sources)[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_check_slide_spec_never_skips_for_a_falsy_expected_label(self):
        """Defense in depth: even if some future caller hands
        check_slide_spec an empty expected label directly, a mismatched
        cell label must still be caught, not waved through as "nothing
        to compare"."""
        accepted = {
            "c1": CheckResult(claim_id="c1", verdict="verified", value=0.481, label="")
        }
        spec = {
            "slides": [
                {"cells": [{"value": 0.481, "claim": "c1", "label": "wheat R2"}]}
            ]
        }
        problems = check_slide_spec(spec, accepted, {"c1": ""})
        assert problems

    @pytest.mark.unit
    def test_check_slide_spec_requires_a_cell_label_even_when_expected_is_empty(self):
        """The missing-label case must also not be waved through when
        expected_label itself happens to be empty."""
        accepted = {
            "c1": CheckResult(claim_id="c1", verdict="verified", value=0.481, label="")
        }
        spec = {"slides": [{"cells": [{"value": 0.481, "claim": "c1"}]}]}
        problems = check_slide_spec(spec, accepted, {"c1": ""})
        assert problems


class TestCodexFindingFiguresEmbeddedInALabelAreUnverified:
    """A figure hidden inside a cell's label text was never checked.

    A label such as "maize R2 (99% confidence)" matches the expected
    label verbatim (the "99" is just along for the ride), so a slide
    copying it passed even though nothing supports the 99. The title
    path already runs numeric coverage over its text; the cell-label
    path did not.
    """

    @pytest.mark.unit
    def test_an_unbacked_figure_inside_a_cell_label_is_caught(self):
        """Codex's example: a confidence figure with no claim behind
        it. The cell label matches the claim's own (verified-under)
        label verbatim -- the identity check passes cleanly -- so
        this pins specifically the embedded-figure coverage gap, not
        a label mismatch."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=0.886,
                label="maize R2 (99% confidence)",
            )
        }
        spec = {
            "slides": [
                {
                    "cells": [
                        {
                            "value": 0.886,
                            "claim": "c1",
                            "label": "maize R2 (99% confidence)",
                        }
                    ]
                }
            ]
        }
        problems = check_slide_spec(spec, accepted, {"c1": "maize R2 (99% confidence)"})
        assert any("99" in p for p in problems)

    @pytest.mark.unit
    def test_a_claim_backed_figure_inside_a_cell_label_still_passes(self):
        """The fix must not reject a label whose extra figure is
        itself backed by another accepted claim on the same slide."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=0.886,
                label="maize R2 (99% confidence)",
            ),
            "c2": CheckResult(
                claim_id="c2",
                verdict="verified",
                value=99,
                label="confidence",
                unit="percent",
            ),
        }
        spec = {
            "slides": [
                {
                    "cells": [
                        {
                            "value": 0.886,
                            "claim": "c1",
                            "label": "maize R2 (99% confidence)",
                        },
                        {"value": 99, "claim": "c2", "label": "confidence"},
                    ]
                }
            ]
        }
        labels = {"c1": "maize R2 (99% confidence)", "c2": "confidence"}
        assert check_slide_spec(spec, accepted, labels) == []


class TestCodexFindingDerivedClaimsGetRowLabelProtectionToo:
    """The wrong-row protection applied to lookups, but not derived claims.

    A derived claim with expr "a" and a single row-selected input is,
    for every practical purpose, a lookup wearing a different kind --
    the writer instructions require a rounded restatement to be a
    derived claim rather than a lookup, so this was not a rare edge
    case. Against a real maize R2 of 0.886, a derived claim reading
    crop=maize:r2, labelled "cotton R2," verified: the derived success
    path never ran label_contradiction at all.
    """

    @pytest.mark.unit
    def test_a_single_input_derived_claim_gets_the_lookup_label_check(self, sources):
        """Codex's scenario, generalized to this file's own fixture
        values: maize's real R2 claimed under cotton's label."""
        c = Claim(
            id="c1",
            value=0.886,
            label="cotton R2",
            kind="derived",
            expr="a",
            inputs={"a": "per_crop.csv:crop=maize:r2"},
            unit="fraction",
        )
        res = check_claims([c], sources)[0]
        assert res.verdict == "failed"
        assert res.reason == "label_mismatch"

    @pytest.mark.unit
    def test_a_correctly_labelled_single_input_derived_claim_still_passes(
        self, sources
    ):
        """The fix must not reject the ordinary, correctly labelled case."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="derived",
            expr="a",
            inputs={"a": "per_crop.csv:crop=maize:r2"},
            unit="fraction",
        )
        assert check_claims([c], sources)[0].verdict == "verified"

    @pytest.mark.unit
    def test_a_two_crop_ratio_naming_both_crops_is_not_over_rejected(self, sources):
        """A legitimate ratio label mentions both of its own rows --
        neither is a sibling being smuggled in under the other's
        claim, so this must still pass."""
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
            unit="plain",
        )
        assert check_claims([c], sources)[0].verdict == "verified"

    @pytest.mark.unit
    def test_a_two_input_sum_naming_a_sibling_outside_its_own_inputs_fails(
        self, sources
    ):
        """A sum labelled with a tier that is neither input's own row
        is exactly the contradiction this check exists to catch."""
        c = Claim(
            id="c1",
            value=977,
            label="Excellent and Average tiers combined",
            kind="derived",
            expr="a + b",
            inputs={
                "a": "tiers.csv:tier=Excellent:n_records",
                "b": "tiers.csv:tier=Good:n_records",
            },
            unit="count",
        )
        res = check_claims([c], sources)[0]
        assert res.verdict == "failed"
        assert res.reason == "label_mismatch"


class TestCodexFindingOverflowAfterEvaluationStillUnresolvable:
    """A result that evaluates fine can still overflow float conversion.

    "a ** 1000" with a as a plain JSON integer (1200, not a float)
    computes fine as a Python big int -- no exception at all, Python
    ints have no fixed size. The crash came later: _within_tolerance
    converts the result to float to compare it against the claim's
    stated value, and that conversion is what raises OverflowError,
    outside the exception handling wrapped around evaluation itself.
    Converting right after evaluation, inside the same handler, means
    an int result too large for float is caught at the same point an
    already-too-large float result already was (round 19).

    This round also tightened the exponent cap (1000 -> 100), so
    Codex's own literal payload is now caught earlier, by the cap
    itself, before evaluation ever runs. The conversion fix is
    isolated in its own test below with a multiplication chain,
    which the cap does not bound at all -- any operator can produce
    an oversized integer, not just **.
    """

    @pytest.mark.unit
    def test_codexs_literal_payload_is_now_caught_by_the_tightened_cap(self, sources):
        """The exact reported payload still ends up unresolvable, now
        via the cap rather than the conversion -- a cheaper rejection,
        before any 1000-digit number is even computed."""
        c = Claim(
            id="c1",
            value=1,
            label="oversized integer result",
            kind="derived",
            expr="a ** 1000",
            inputs={"a": "metrics.json:n_records"},
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_an_oversized_result_from_a_non_pow_operator_is_unresolvable(self, sources):
        """The exponent cap only bounds **. A long multiplication
        chain reaches the same oversized-integer outcome through a
        different operator entirely, isolating the conversion fix
        from the cap: 1200 multiplied by itself 100 times is far
        larger than any float can hold, and the cap never saw it."""
        c = Claim(
            id="c1",
            value=1,
            label="oversized product",
            kind="derived",
            expr=" * ".join(["a"] * 101),
            inputs={"a": "metrics.json:n_records"},
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_an_oversized_float_result_lands_on_the_same_verdict(self, sources):
        """The float-base variant: overflows during the exponentiation
        itself (already caught since round 19), not during a later
        conversion -- a different path to the same user-visible
        verdict, confirmed here so the two stay consistent."""
        c = Claim(
            id="c1",
            value=1,
            label="oversized float result",
            kind="derived",
            expr="a ** 1000",
            inputs={"a": "metrics.json:mean_ape"},
        )
        res = check_claims([c], sources)
        assert len(res) == 1
        assert res[0].verdict == "unresolvable"


class TestCodexFindingSlideCellPercentScaleIsRespected:
    """A percent-suffixed slide cell is a different number than the raw one.

    _coerce strips a trailing '%' without scaling, so a cell showing
    "0.481%" (really 0.00481) passed against a claim verified as the
    raw 0.481 -- the deck could publish a figure 100x smaller than
    what the claim actually says, under a literal that looks close
    enough to pass a naive string-insensitive comparison.
    """

    @pytest.mark.unit
    def test_a_correctly_converted_percentage_passes(self):
        """ "48.1%" is 0.481 -- the same number the claim verified,
        just rendered the other conventional way. unit is declared
        explicitly here since a hand-built CheckResult carries no
        addresses to infer it from."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=0.481,
                label="maize R2",
                unit="fraction",
            )
        }
        spec = {
            "slides": [
                {"cells": [{"value": "48.1%", "claim": "c1", "label": "maize R2"}]}
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "maize R2"}) == []

    @pytest.mark.unit
    def test_an_unscaled_percentage_is_caught(self):
        """Codex's exact scenario: "0.481%" is 0.00481, not 0.481, and
        must not pass just because stripping the '%' leaves the same
        digits."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="verified", value=0.481, label="maize R2"
            )
        }
        spec = {
            "slides": [
                {"cells": [{"value": "0.481%", "claim": "c1", "label": "maize R2"}]}
            ]
        }
        problems = check_slide_spec(spec, accepted, {"c1": "maize R2"})
        assert problems

    @pytest.mark.unit
    def test_a_plain_numeric_cell_is_unaffected(self):
        """The fix must not disturb a cell with no '%' in it at all."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="verified", value=0.481, label="maize R2"
            )
        }
        spec = {
            "slides": [
                {"cells": [{"value": 0.481, "claim": "c1", "label": "maize R2"}]}
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "maize R2"}) == []


class TestCodexFindingSlideTitleBindsToASpecificClaimsProvenance:
    """A title figure must not borrow legitimacy from an unrelated claim.

    "Wheat R2 is 0.481" passed because 0.481 happened to match some
    accepted cell's value on the slide -- membership, not binding.
    The title's own words were never checked against the specific
    claim's row provenance the way a cell's label already is. A
    maize claim correctly backing 0.481 does not make a title about
    wheat true.
    """

    @pytest.mark.unit
    def test_a_title_naming_a_sibling_crop_is_caught(self, sources):
        """Codex's exact scenario: the title's own words name a crop
        the backing claim does not read."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=0.886,
                label="maize R2",
                addresses=("per_crop.csv:crop=maize:r2",),
            )
        }
        spec = {
            "slides": [
                {
                    "title": "Cotton R2 is 0.886",
                    "cells": [{"value": 0.886, "claim": "c1", "label": "maize R2"}],
                }
            ]
        }
        problems = check_slide_spec(spec, accepted, {"c1": "maize R2"}, sources=sources)
        assert problems

    @pytest.mark.unit
    def test_a_title_naming_the_correct_crop_still_passes(self, sources):
        """The fix must not reject the ordinary, correctly worded case."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=0.886,
                label="maize R2",
                addresses=("per_crop.csv:crop=maize:r2",),
            )
        }
        spec = {
            "slides": [
                {
                    "title": "Maize R2 is 0.886",
                    "cells": [{"value": 0.886, "claim": "c1", "label": "maize R2"}],
                }
            ]
        }
        problems = check_slide_spec(spec, accepted, {"c1": "maize R2"}, sources=sources)
        assert problems == []

    @pytest.mark.unit
    def test_omitting_sources_preserves_the_old_membership_only_behavior(self):
        """A caller with no staged data to check against (every
        existing test in this file) must see unchanged behavior."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=0.886,
                label="maize R2",
                addresses=("per_crop.csv:crop=maize:r2",),
            )
        }
        spec = {
            "slides": [
                {
                    "title": "Cotton R2 is 0.886",
                    "cells": [{"value": 0.886, "claim": "c1", "label": "maize R2"}],
                }
            ]
        }
        problems = check_slide_spec(spec, accepted, {"c1": "maize R2"})
        assert problems == []


class TestIssue827Step2ProseLabelCheck:
    """A right number under a correct claim can still sit in a lying
    sentence -- #827's second half.

    Step 1 (mandatory [cN] tags, binding coverage to the specific
    claim tagged rather than any claim sharing the value) closed the
    half where a figure states the wrong number. This closes the
    other half: a figure that states the *right* number, under a
    claim whose own label is *also* correct, but inside a sentence
    that names a different row. "Cotton's R2 is 0.886 [c1]." where
    c1 correctly reads and labels maize's own 0.886 -- the claim is
    right, the sentence lies, and nothing before this read the
    sentence's own words at all.
    """

    @pytest.mark.unit
    def test_the_original_827_worked_example_is_caught(self, sources):
        """The exact example from the issue."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        res = check_claims([c], sources, report_text="Cotton's R2 is 0.886 [c1].")
        bad = [r for r in res if r.verdict == "uncovered"]
        assert len(bad) == 1
        assert "0.886" in bad[0].claim_id or bad[0].value == "0.886"

    @pytest.mark.unit
    def test_a_correctly_worded_sentence_still_passes(self, sources):
        """The fix must not disturb the ordinary, correctly worded case."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        res = check_claims([c], sources, report_text="Maize's R2 is 0.886 [c1].")
        assert [r for r in res if r.verdict == "uncovered"] == []

    @pytest.mark.unit
    def test_a_sentence_mentioning_a_sibling_as_context_still_passes(self, sources):
        """Naming the claim's own row earns the sentence the right to
        mention a sibling too, the same allowance a label already
        has -- a sentence is not punished for ordinary comparison."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        res = check_claims(
            [c],
            sources,
            report_text="Maize's R2 of 0.886 [c1] far outperforms cotton's.",
        )
        assert [r for r in res if r.verdict == "uncovered"] == []

    @pytest.mark.unit
    def test_the_check_applies_to_derived_claims_too(self, sources):
        """A single-input derived claim gets the same sentence check
        a lookup does, the same way label_contradiction's own
        derived-claim generalization works."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="derived",
            expr="a",
            inputs={"a": "per_crop.csv:crop=maize:r2"},
        )
        res = check_claims([c], sources, report_text="Cotton's R2 is 0.886 [c1].")
        bad = [r for r in res if r.verdict == "uncovered"]
        assert len(bad) == 1

    @pytest.mark.unit
    def test_only_the_sentence_with_the_tagged_figure_is_checked(self, sources):
        """A sibling word in a different sentence must not contaminate
        this one -- the check is scoped to one sentence, not the
        whole report."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "Cotton did poorly overall. Maize's R2 is 0.886 [c1]."
        res = check_claims([c], sources, report_text=text)
        assert [r for r in res if r.verdict == "uncovered"] == []


class TestCodexFindingPercentScaleInProseIsRespected:
    """The slide-cell percent fix did not reach the prose path.

    "Maize R2 is 0.481% [c1]." passed check_report against a staged
    maize R2 of 0.481, because _tag_figures dropped the '%' suffix
    entirely before the figure ever reached comparison -- the same
    scale confusion _percent_scaled already fixed for a slide cell,
    unfixed here.
    """

    @pytest.mark.unit
    def test_a_correctly_converted_percentage_in_prose_passes(self):
        """ "48.1% [c1]" is 0.481 -- the same number, the other way.
        The claim's own value is the plain 0.481 that matches the
        source; it is the report's own figure text that uses the
        percent form."""
        sources = {"metrics.json": {"r2_overall": 0.481}}
        c = Claim(
            id="c1",
            value=0.481,
            label="overall R2",
            kind="lookup",
            source="metrics.json",
            key="r2_overall",
        )
        res = check_claims([c], sources, report_text="Overall R2 is 48.1% [c1].")
        assert [r for r in res if r.verdict == "uncovered"] == []

    @pytest.mark.unit
    def test_an_unscaled_percentage_in_prose_is_caught(self):
        """Codex's exact scenario: "0.481%" is 0.00481, not 0.481, and
        must not pass just because stripping the '%' leaves the same
        digits as the real value."""
        sources = {"metrics.json": {"r2_overall": 0.481}}
        c = Claim(
            id="c1",
            value=0.481,
            label="overall R2",
            kind="lookup",
            source="metrics.json",
            key="r2_overall",
        )
        res = check_claims([c], sources, report_text="Overall R2 is 0.481% [c1].")
        bad = [r for r in res if r.verdict == "uncovered"]
        assert len(bad) == 1

    @pytest.mark.unit
    def test_extract_numbers_is_unaffected_by_the_percent_carry(self):
        """The fix is scoped to _tag_figures; extract_numbers's own
        contract (the bare digits, no '%') must not change -- other
        callers (coverage-gap detection, slide titles) depend on it."""
        text = "R2 was 0.858 across 1,200 records; median error 4.4%."
        assert set(extract_numbers(text)) >= {"0.858", "1,200", "4.4"}


class TestCodexFindingPercentUnitIsClaimAware:
    """One percent-scale rule, used by prose, cell and title alike.

    The earlier percent fixes (rounds 25 and 26) assumed every
    backing value is a fraction (0-1) and unconditionally divided a
    percent-suffixed figure by 100. numeric_stage.py's own tiers.csv
    writes rate_pct as 100 * n / total -- already on the 0-100
    scale -- so a correct report saying "Excellent tier share is 50%
    [c1]." against a lookup of 50.0 was flagged as a coverage gap: a
    real over-rejection that would have penalized correct reports and
    distorted the coverage metric in the paid run. The slide-title
    path had the opposite, older gap: it still used extract_numbers,
    which drops the '%' entirely, so "Maize R2 is 0.481%" passed
    against a real 0.481 with no scale check applied at all.

    _inferred_claim_unit decides fraction vs percent from the
    claim's own source column name (or an explicit declaration);
    _percent_aware_match is the one function all three surfaces now
    share for the scale decision itself.
    """

    PERCENT_SOURCES = {"tiers.csv": [{"tier": "Excellent", "rate_pct": "50.0"}]}
    FRACTION_SOURCES = {"metrics.json": {"r2_overall": 0.481}}

    # --- prose -------------------------------------------------------

    @pytest.mark.unit
    def test_prose_percent_unit_claim_matches_at_face_value(self):
        """ "50% [c1]" against a rate_pct lookup of 50.0 must pass --
        the percent sign is just how a percent-scale value is
        conventionally written, not a /100 instruction."""
        c = Claim(
            id="c1",
            value=50.0,
            label="Excellent tier share",
            kind="lookup",
            source="tiers.csv",
            key="tier=Excellent:rate_pct",
        )
        res = check_claims([c], self.PERCENT_SOURCES, report_text="Share is 50% [c1].")
        assert [r for r in res if r.verdict == "uncovered"] == []

    @pytest.mark.unit
    def test_prose_fraction_unit_claim_rejects_the_unscaled_literal(self):
        """ "0.481% [c1]" against an R2 lookup of 0.481 must still fail
        -- 0.481% is 0.00481, not 0.481."""
        c = Claim(
            id="c1",
            value=0.481,
            label="overall R2",
            kind="lookup",
            source="metrics.json",
            key="r2_overall",
        )
        res = check_claims(
            [c], self.FRACTION_SOURCES, report_text="Overall R2 is 0.481% [c1]."
        )
        assert len([r for r in res if r.verdict == "uncovered"]) == 1

    @pytest.mark.unit
    def test_prose_fraction_unit_claim_accepts_the_scaled_literal(self):
        """ "48.1% [c1]" against the same R2 lookup must pass."""
        c = Claim(
            id="c1",
            value=0.481,
            label="overall R2",
            kind="lookup",
            source="metrics.json",
            key="r2_overall",
        )
        res = check_claims(
            [c], self.FRACTION_SOURCES, report_text="Overall R2 is 48.1% [c1]."
        )
        assert [r for r in res if r.verdict == "uncovered"] == []

    # --- slide cell ----------------------------------------------------

    @pytest.mark.unit
    def test_slide_cell_percent_unit_claim_matches_at_face_value(self):
        """The same three cases, as a slide cell."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=50.0,
                label="Excellent tier share",
                addresses=("tiers.csv:tier=Excellent:rate_pct",),
                unit="percent",
            )
        }
        spec = {
            "slides": [
                {
                    "cells": [
                        {"value": "50%", "claim": "c1", "label": "Excellent tier share"}
                    ]
                }
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "Excellent tier share"}) == []

    @pytest.mark.unit
    def test_slide_cell_fraction_unit_claim_rejects_the_unscaled_literal(self):
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=0.481,
                label="overall R2",
                addresses=("metrics.json:r2_overall",),
                unit="fraction",
            )
        }
        spec = {
            "slides": [
                {"cells": [{"value": "0.481%", "claim": "c1", "label": "overall R2"}]}
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "overall R2"}) != []

    @pytest.mark.unit
    def test_slide_cell_fraction_unit_claim_accepts_the_scaled_literal(self):
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=0.481,
                label="overall R2",
                addresses=("metrics.json:r2_overall",),
                unit="fraction",
            )
        }
        spec = {
            "slides": [
                {"cells": [{"value": "48.1%", "claim": "c1", "label": "overall R2"}]}
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "overall R2"}) == []

    # --- slide title -----------------------------------------------------

    @pytest.mark.unit
    def test_slide_title_percent_unit_claim_matches_at_face_value(self, sources):
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=50.0,
                label="Excellent tier share",
                addresses=("tiers.csv:tier=Excellent:rate_pct",),
                unit="percent",
            )
        }
        spec = {
            "slides": [
                {
                    "title": "Excellent tier share is 50%",
                    "cells": [
                        {"value": 50.0, "claim": "c1", "label": "Excellent tier share"}
                    ],
                }
            ]
        }
        problems = check_slide_spec(
            spec, accepted, {"c1": "Excellent tier share"}, sources=sources
        )
        assert problems == []

    @pytest.mark.unit
    def test_slide_title_fraction_unit_claim_rejects_the_unscaled_literal(
        self, sources
    ):
        """Codex's exact scenario: the title path still used
        extract_numbers (no '%' at all) and skipped the scale check
        entirely."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=0.481,
                label="maize R2",
                addresses=("per_crop.csv:crop=maize:r2",),
                unit="fraction",
            )
        }
        spec = {
            "slides": [
                {
                    "title": "Maize R2 is 0.481%",
                    "cells": [{"value": 0.481, "claim": "c1", "label": "maize R2"}],
                }
            ]
        }
        problems = check_slide_spec(spec, accepted, {"c1": "maize R2"}, sources=sources)
        assert problems != []

    @pytest.mark.unit
    def test_slide_title_fraction_unit_claim_accepts_the_scaled_literal(self, sources):
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=0.481,
                label="maize R2",
                addresses=("per_crop.csv:crop=maize:r2",),
                unit="fraction",
            )
        }
        spec = {
            "slides": [
                {
                    "title": "Maize R2 is 48.1%",
                    "cells": [{"value": 0.481, "claim": "c1", "label": "maize R2"}],
                }
            ]
        }
        problems = check_slide_spec(spec, accepted, {"c1": "maize R2"}, sources=sources)
        assert problems == []

    # --- unit inference itself --------------------------------------------

    @pytest.mark.unit
    def test_unit_is_inferred_as_percent_from_the_rate_pct_column_name(self):
        c = Claim(
            id="c1",
            value=50.0,
            label="x",
            kind="lookup",
            source="tiers.csv",
            key="tier=Excellent:rate_pct",
        )
        assert _inferred_claim_unit(c) == "percent"

    @pytest.mark.unit
    def test_unit_is_inferred_as_fraction_from_an_ordinary_column_name(self):
        c = Claim(
            id="c1",
            value=0.481,
            label="x",
            kind="lookup",
            source="metrics.json",
            key="r2_overall",
        )
        assert _inferred_claim_unit(c) == "fraction"

    @pytest.mark.unit
    def test_an_explicit_unit_declaration_overrides_inference(self):
        """A claim can declare its own unit, overriding the column-name
        guess entirely -- useful when a report states a rate_pct
        value as a bare fraction instead of its staged percent scale."""
        c = Claim(
            id="c1",
            value=0.5,
            label="x",
            kind="lookup",
            source="tiers.csv",
            key="tier=Excellent:rate_pct",
            unit="fraction",
        )
        assert _inferred_claim_unit(c) == "fraction"

    @pytest.mark.unit
    def test_unit_cannot_be_determined_with_no_source_column_at_all(self):
        """A claim with no usable addressing (malformed, or a kind this
        function does not read inputs/source for) must not default
        to a guess."""
        c = Claim(id="c1", value=1, label="x", kind="lookup")
        assert _inferred_claim_unit(c) is None


class TestCodexFindingUnitsComeFromTheStagersOwnManifest:
    """The stager's own units manifest is ground truth for a claim's
    unit; the column-name heuristic is only a fallback for sources
    the manifest does not cover.

    fit_and_predict already scales APE by 100, and median_ape /
    mean_ape carry that same 0-100 scale in metrics.json, per_crop.csv
    and quartiles.csv -- but neither name ends in the _pct/percent
    suffix the heuristic looks for, so it guessed "fraction". A
    correct "4.4% [c1]" against a real median_ape of 4.4 was rejected,
    and the wrong "440% [c1]" passed; a slide cell had the same error.
    """

    UNITS = {"metrics.json": {"median_ape": "percent"}}

    @pytest.mark.unit
    def test_inferred_claim_unit_prefers_the_manifest_over_the_name_heuristic(self):
        c = Claim(
            id="c1",
            value=4.4,
            label="median APE",
            kind="lookup",
            source="metrics.json",
            key="median_ape",
        )
        assert _inferred_claim_unit(c) == "fraction"
        assert _inferred_claim_unit(c, units=self.UNITS) == "percent"

    @pytest.mark.unit
    def test_prose_percent_figure_passes_with_the_manifested_unit(self, sources):
        c = Claim(
            id="c1",
            value=4.4,
            label="median APE",
            kind="lookup",
            source="metrics.json",
            key="median_ape",
        )
        res = check_claims(
            [c], sources, report_text="Median APE is 4.4% [c1].", units=self.UNITS
        )
        assert [r for r in res if r.verdict == "uncovered"] == []

    @pytest.mark.unit
    def test_prose_the_unscaled_literal_is_still_caught(self, sources):
        c = Claim(
            id="c1",
            value=4.4,
            label="median APE",
            kind="lookup",
            source="metrics.json",
            key="median_ape",
        )
        res = check_claims(
            [c], sources, report_text="Median APE is 440% [c1].", units=self.UNITS
        )
        assert len([r for r in res if r.verdict == "uncovered"]) == 1

    @pytest.mark.unit
    def test_without_the_manifest_the_name_heuristic_still_over_rejects(self, sources):
        """Pins Codex's exact scenario: with no manifest at all,
        median_ape's name gives no percent hint, so the correct
        "4.4%" is wrongly rejected -- the reason a manifest is needed
        rather than a better name-matching heuristic."""
        c = Claim(
            id="c1",
            value=4.4,
            label="median APE",
            kind="lookup",
            source="metrics.json",
            key="median_ape",
        )
        res = check_claims([c], sources, report_text="Median APE is 4.4% [c1].")
        assert len([r for r in res if r.verdict == "uncovered"]) == 1

    @pytest.mark.unit
    def test_slide_cell_percent_figure_passes_with_the_manifested_unit(self, sources):
        c = Claim(
            id="c1",
            value=4.4,
            label="median APE",
            kind="lookup",
            source="metrics.json",
            key="median_ape",
        )
        accepted = {r.claim_id: r for r in check_claims([c], sources, units=self.UNITS)}
        spec = {
            "slides": [
                {"cells": [{"value": "4.4%", "claim": "c1", "label": "median APE"}]}
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "median APE"}) == []

    @pytest.mark.unit
    def test_slide_cell_the_unscaled_literal_is_still_caught(self, sources):
        c = Claim(
            id="c1",
            value=4.4,
            label="median APE",
            kind="lookup",
            source="metrics.json",
            key="median_ape",
        )
        accepted = {r.claim_id: r for r in check_claims([c], sources, units=self.UNITS)}
        spec = {
            "slides": [
                {"cells": [{"value": "440%", "claim": "c1", "label": "median APE"}]}
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "median APE"}) != []

    @pytest.mark.unit
    def test_rate_pct_still_resolves_via_the_manifest(self, sources):
        """A column the old name-suffix heuristic already recognized
        must resolve the same way when the manifest also declares it
        -- the manifest does not have to re-litigate what already
        worked."""
        c = Claim(
            id="c1",
            value=55.0,
            label="Excellent tier share",
            kind="lookup",
            source="tiers.csv",
            key="tier=Excellent:rate_pct",
        )
        units = {"tiers.csv": {"rate_pct": "percent"}}
        res = check_claims([c], sources, report_text="Share is 55% [c1].", units=units)
        assert [r for r in res if r.verdict == "uncovered"] == []

    @pytest.mark.unit
    def test_a_claim_with_no_addressing_still_cannot_be_determined_even_with_a_manifest(
        self,
    ):
        """A manifest is consulted per address a claim actually reads;
        a claim with no usable addressing at all has nothing for it
        to cover, and must stay undetermined exactly as it did with
        no manifest at all."""
        c = Claim(id="c1", value=1, label="x", kind="lookup")
        assert _inferred_claim_unit(c, units=self.UNITS) is None


class TestCodexFindingOrderedListMarkersDoNotDriftSentencePositions:
    """An ordered-list marker's own length, not just its presence,
    corrupted every later figure's sentence-check position.

    _tag_figures blanked a marker like "10. " down to a single
    space, shortening the text by three characters at that point.
    Every figure after it was then checked against a sentence
    extracted from a DIFFERENT, unmasked text (built independently
    in check_claims) at that same, now-wrong offset -- drifting
    further with every marker already passed, and specifically
    drifting backward into an earlier sentence since each marker is
    longer than the one space it was replaced with.
    """

    @pytest.mark.unit
    def test_a_ten_item_ordered_list_does_not_drift_the_last_entry(self, sources):
        """Codex's exact scenario: nine ordinary one-digit markers,
        then "10." (one character longer), with the tenth entry's own
        sentence correctly naming cotton, not maize."""
        lines = [f"{i}. Maize R2 is 0.886 [c1]." for i in range(1, 10)]
        lines.append("10. Cotton's R2 is 0.886 [c1].")
        text = "\n".join(lines)
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        res = check_claims([c], sources, report_text=text)
        bad = [r for r in res if r.verdict == "uncovered"]
        assert len(bad) == 1

    @pytest.mark.unit
    def test_markers_of_varying_width_do_not_drift_at_any_position(self, sources):
        """1. through 12. spans both one- and two-digit markers; a
        wrong-crop sentence placed at every position in the list must
        be caught regardless of how many markers came before it."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        for wrong_at in (1, 5, 9, 12):
            lines = []
            for i in range(1, 13):
                if i == wrong_at:
                    lines.append(f"{i}. Cotton's R2 is 0.886 [c1].")
                else:
                    lines.append(f"{i}. Maize R2 is 0.886 [c1].")
            text = "\n".join(lines)
            res = check_claims([c], sources, report_text=text)
            bad = [r for r in res if r.verdict == "uncovered"]
            assert (
                len(bad) == 1
            ), f"failed to catch the wrong sentence at item {wrong_at}"


class TestCodexFindingListAndTableRowsAreSentenceBoundaries:
    """A list item or table row with no terminal punctuation must not
    merge with its neighbor into one "sentence" for the prose
    label-contradiction check."""

    @pytest.mark.unit
    def test_a_two_item_markdown_list_does_not_merge_rows(self, sources):
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "- Maize R2: 0.886 [c1]\n- Cotton R2: 0.886 [c1]"
        res = check_claims([c], sources, report_text=text)
        bad = [r for r in res if r.verdict == "uncovered"]
        assert len(bad) == 1

    @pytest.mark.unit
    def test_a_two_row_markdown_table_does_not_merge_rows(self, sources):
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "| Crop | R2 |\n| Maize | 0.886 [c1] |\n| Cotton | 0.886 [c1] |"
        res = check_claims([c], sources, report_text=text)
        bad = [r for r in res if r.verdict == "uncovered"]
        assert len(bad) == 1

    @pytest.mark.unit
    def test_an_ordinary_two_sentence_paragraph_still_works(self, sources):
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "Cotton did poorly overall. Maize's R2 is 0.886 [c1]."
        res = check_claims([c], sources, report_text=text)
        assert [r for r in res if r.verdict == "uncovered"] == []


class TestCodexFindingSentenceBoundaryGrammar:
    """A period is not always a sentence boundary, and a sentence
    boundary is not always a bare period.

    Over-rejection: "vs." in "Maize (vs. cotton) has R2 0.886 [c1]."
    split the sentence at the abbreviation, so "cotton" looked like
    the sentence's row name and the correct maize claim was marked
    uncovered.

    Under-rejection: a boundary before a closing markdown or
    quotation mark was missed. "**Maize's R2 is 0.886 [c1].**
    Cotton's R2 is 0.886 [c1]." merged into one unit, so the maize
    mention satisfied the sibling-row allowance for the following,
    unrelated cotton sentence.
    """

    @pytest.mark.unit
    def test_vs_abbreviation_does_not_split_the_sentence(self, sources):
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "Maize (vs. cotton) has R2 0.886 [c1]."
        res = check_claims([c], sources, report_text=text)
        assert [r for r in res if r.verdict == "uncovered"] == []

    @pytest.mark.unit
    def test_eg_abbreviation_mid_sentence_does_not_split(self, sources):
        """ "e.g." is followed by a capitalized word, so the
        capital-letter heuristic alone would not catch it -- only the
        fixed abbreviation list does."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "Maize (e.g. Cotton) has R2 0.886 [c1]."
        res = check_claims([c], sources, report_text=text)
        assert [r for r in res if r.verdict == "uncovered"] == []

    @pytest.mark.unit
    def test_a_decimal_inside_a_sentence_does_not_split(self, sources):
        """A decimal point has no whitespace after it, so it was
        never a boundary candidate -- this pins that a sentence
        naming both a number and a claim stays one unit."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "Maize's R2 is 0.886 [c1], a strong result overall."
        res = check_claims([c], sources, report_text=text)
        assert [r for r in res if r.verdict == "uncovered"] == []

    @pytest.mark.unit
    def test_bold_markers_before_a_capital_do_split(self, sources):
        """Codex's exact under-rejection scenario: the closing bold
        marker sits between the period and the next sentence's
        capital letter, and must not hide the boundary."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "**Maize's R2 is 0.886 [c1].** Cotton's R2 is 0.886 [c1]."
        res = check_claims([c], sources, report_text=text)
        bad = [r for r in res if r.verdict == "uncovered"]
        assert len(bad) == 1

    @pytest.mark.unit
    def test_a_quoted_sentence_followed_by_another_sentence_splits_correctly(
        self, sources
    ):
        """A closing quotation mark, not just markdown emphasis, must
        also sit between the period and the next capital letter
        without hiding the boundary."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "\"Maize's R2 is 0.886 [c1].\" Cotton's R2 is 0.886 [c1]."
        res = check_claims([c], sources, report_text=text)
        bad = [r for r in res if r.verdict == "uncovered"]
        assert len(bad) == 1

    @pytest.mark.unit
    def test_an_unformatted_two_sentence_paragraph_still_works(self, sources):
        """The control: ordinary prose with no abbreviation, no
        decimal, and no closing markers must be unaffected."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "Cotton's R2 lagged behind. Maize's R2 is 0.886 [c1]."
        res = check_claims([c], sources, report_text=text)
        assert [r for r in res if r.verdict == "uncovered"] == []


class TestCodexFindingSegmentationSurvivesMaskingAndOpeningMarkers:
    r"""Round 4: two specific gaps in the round-3 segmenter, plus a
    matrix proving the fix holds across every combination this
    checker's own inputs can produce.

    Masking gap: an ordered-list marker ("1. ") is blanked to spaces
    before _LIST_OR_TABLE_LINE_RE ever runs, so its own \d+\.\s
    pattern never matches a numbered item's line start, and a
    numbered list merges into one sentence the same way an
    unformatted list once did.

    Opening-marker gap: the round-3 fix looked past a *closing*
    marker sitting between the period and the next sentence's
    capital letter, but not an *opening* marker sitting between the
    whitespace and that same capital letter.

    Round 5 (paragraph-break-or-heading gap): a blank-line paragraph
    break or a Markdown heading is not itself a boundary when the
    paragraph before it has no terminal punctuation -- "Maize R2:
    0.886 [c1]\n\nCotton R2: 0.886 [c1]" merges two paragraphs into
    one, and "# Maize\n\nCotton R2 is 0.886 [c1]." merges a heading
    into the paragraph after it, through the same gap.

    Every case below reuses the maize claim's own tag [c1] on a
    sentence about cotton -- a right number under the wrong row is
    exactly what the sibling-row check exists to catch, and it can
    only catch it when segmentation keeps the two rows apart. Naming
    both rows in one merged blob is not a violation (a sentence that
    names its own row earns the right to mention a sibling too), so
    a stray *untagged* cotton figure would never prove segmentation
    actually worked -- only a mistagged one does.
    """

    @pytest.mark.unit
    def test_an_ordered_list_still_segments_even_though_masking_runs_first(
        self, sources
    ):
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "1. Maize R2: 0.886 [c1]\n2. Cotton R2: 0.886 [c1]"
        res = check_claims([c], sources, report_text=text)
        bad = [r for r in res if r.verdict == "uncovered"]
        assert len(bad) == 1

    @pytest.mark.unit
    def test_an_opening_bold_marker_on_the_next_sentence_still_splits(self, sources):
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "Maize R2 is 0.886 [c1]. **Cotton R2 is 0.886 [c1].**"
        res = check_claims([c], sources, report_text=text)
        bad = [r for r in res if r.verdict == "uncovered"]
        assert len(bad) == 1

    @pytest.mark.unit
    def test_a_blank_line_paragraph_break_still_splits_with_no_punctuation(
        self, sources
    ):
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "Maize R2: 0.886 [c1]\n\nCotton R2: 0.886 [c1]"
        res = check_claims([c], sources, report_text=text)
        bad = [r for r in res if r.verdict == "uncovered"]
        assert len(bad) == 1

    @pytest.mark.unit
    def test_a_heading_still_splits_the_paragraph_after_it(self, sources):
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = "# Maize\n\nCotton R2 is 0.886 [c1]."
        res = check_claims([c], sources, report_text=text)
        bad = [r for r in res if r.verdict == "uncovered"]
        assert len(bad) == 1

    LINE_FORMS = ("none", "bullet", "ordered", "table", "paragraph", "heading")
    MARKERS = {
        "none": ("", ""),
        "bold": ("**", "**"),
        "italic": ("*", "*"),
        "quote": ('"', '"'),
        "paren": ("(", ")"),
    }
    TRAPS = {
        "none": "",
        "abbreviation": " (vs. cotton)",
        "decimal": ", within 2.0 of target,",
    }

    @staticmethod
    def _build_text(line_form, marker, trap):
        cls = TestCodexFindingSegmentationSurvivesMaskingAndOpeningMarkers
        open_m, close_m = cls.MARKERS[marker]
        trap_text = cls.TRAPS[trap]
        maize_core = f"Maize's R2{trap_text} is 0.886 [c1]."
        # Reuses [c1] on a cotton sentence: a right number under the
        # wrong row, caught only if this sentence is correctly kept
        # apart from maize's own.
        cotton_core = "Cotton's R2 is 0.886 [c1]."
        maize = f"{open_m}{maize_core}{close_m}"
        cotton = f"{open_m}{cotton_core}{close_m}"
        if line_form == "none":
            return f"{maize} {cotton}"
        if line_form == "bullet":
            return f"- {maize}\n- {cotton}"
        if line_form == "ordered":
            return f"1. {maize}\n2. {cotton}"
        if line_form == "table":
            return f"| Crop | R2 |\n| Maize | {maize} |\n| Cotton | {cotton} |"
        if line_form == "paragraph":
            return f"{maize}\n\n{cotton}"
        if line_form == "heading":
            return f"{maize}\n# Note\n{cotton}"
        raise ValueError(line_form)

    @pytest.mark.unit
    @pytest.mark.parametrize("line_form", LINE_FORMS)
    @pytest.mark.parametrize("marker", sorted(MARKERS))
    @pytest.mark.parametrize("trap", sorted(TRAPS))
    def test_cottons_mistagged_sentence_is_still_caught(
        self, sources, line_form, marker, trap
    ):
        """6 line forms x 5 marker styles x 3 in-sentence traps = 90
        cases. Every one must still catch cotton's sentence wrongly
        wearing the maize claim's tag -- regardless of how the two
        rows are formatted or what sits inside the maize sentence --
        and must never fail the maize claim itself."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        text = self._build_text(line_form, marker, trap)
        res = check_claims([c], sources, report_text=text)
        by_claim_id = {r.claim_id: r for r in res if r.claim_id == "c1"}
        assert by_claim_id["c1"].verdict == "verified"
        mistagged_cotton_catches = [
            r for r in res if r.verdict == "uncovered" and str(r.value) == "0.886"
        ]
        assert len(mistagged_cotton_catches) == 1, (
            f"expected cotton's mistagged sentence to be caught exactly "
            f"once; got {len(mistagged_cotton_catches)} in {text!r}"
        )


class TestCodexFindingDerivedUnitIsDeclaredNeverInferred:
    """A derived or consistency claim's result unit comes only from
    its own declaration, never from its inputs' addresses.

    Codex's scenario: maize count 60, total count 100, expr
    "100 * a / b" -- both inputs are counts, so the old
    address-based inference called the result "fraction" (neither
    input's column looked like a percent column), even though the
    expression's own *100 makes the result percent-scale. "60%
    [c1]" was wrongly rejected and "6000% [c1]" wrongly passed.

    units manifest could never settle this either -- it describes
    staged columns, not the arithmetic a writer chooses to apply to
    them. Only the writer who wrote the expression knows its own
    scale, so it must declare it.
    """

    PERCENT_SOURCES = {"metrics.json": {"maize_count": 60, "total_count": 100}}
    CLAIM_KWARGS = {
        "id": "c1",
        "value": 60,
        "label": "maize share of total",
        "kind": "derived",
        "expr": "100 * a / b",
        "inputs": {
            "a": "metrics.json:maize_count",
            "b": "metrics.json:total_count",
        },
    }

    @pytest.mark.unit
    def test_declared_percent_unit_accepts_the_percent_literal(self):
        c = Claim(**self.CLAIM_KWARGS, unit="percent")
        res = check_claims(
            [c], self.PERCENT_SOURCES, report_text="Maize share is 60% [c1]."
        )
        assert [r for r in res if r.verdict == "uncovered"] == []

    @pytest.mark.unit
    def test_declared_percent_unit_rejects_the_unscaled_literal(self):
        """Codex's exact over-rejection, inverted: with the unit
        correctly declared, the wrong "6000%" is now what fails,
        not the correct "60%"."""
        c = Claim(**self.CLAIM_KWARGS, unit="percent")
        res = check_claims(
            [c], self.PERCENT_SOURCES, report_text="Maize share is 6000% [c1]."
        )
        assert len([r for r in res if r.verdict == "uncovered"]) == 1

    @pytest.mark.unit
    def test_a_misdeclared_fraction_unit_fails_at_the_percent_check(self):
        """A claim whose own declaration says "fraction" is checked
        on that scale, even when the expression's real result is
        percent-scale -- the declaration is load-bearing, not
        decorative. "60%" scaled as a fraction is 0.6, which does
        not match this claim's value of 60."""
        c = Claim(**self.CLAIM_KWARGS, unit="fraction")
        res = check_claims(
            [c], self.PERCENT_SOURCES, report_text="Maize share is 60% [c1]."
        )
        assert len([r for r in res if r.verdict == "uncovered"]) == 1

    @pytest.mark.unit
    def test_a_derived_claim_with_no_declared_unit_is_rejected_by_the_gate(self):
        """No unit at all is a named problem through
        _malformed_claim_shape, the same gate every other missing
        field for a derived claim goes through -- not a silent
        fallback to guessing from the inputs."""
        c = Claim(**self.CLAIM_KWARGS)
        res = check_claims([c], self.PERCENT_SOURCES)
        assert res[0].verdict == "unresolvable"
        assert "unit" in res[0].detail.lower()

    @pytest.mark.unit
    def test_a_consistency_claim_with_no_declared_unit_is_rejected_too(self):
        c = Claim(
            id="c1",
            value=True,
            label="totals agree",
            kind="consistency",
            expr="a == b",
            inputs={"a": "metrics.json:maize_count", "b": "metrics.json:total_count"},
        )
        res = check_claims([c], self.PERCENT_SOURCES)
        assert res[0].verdict == "unresolvable"
        assert "unit" in res[0].detail.lower()

    @pytest.mark.unit
    def test_a_lookup_claim_still_needs_no_declared_unit(self, sources):
        """Lookups are unaffected -- their unit still comes from the
        manifest or the column-name heuristic, exactly as before."""
        c = Claim(
            id="c1",
            value=0.886,
            label="maize R2",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:r2",
        )
        assert check_claims([c], sources)[0].verdict == "verified"


class TestCodexFindingPercentConversionFormatsExactly:
    """A scaled percent figure must round-trip exactly, not through a
    formatted string that rounds to six significant digits.

    100 * 1 / 3 is 33.33333333333333..., a figure with far more
    digits than ":g" formatting keeps. The checker's own scaled float
    is exactly right; only the string built from it to hand to
    compare() was rounded away before the exact-match slide path
    ever saw it.
    """

    @pytest.mark.unit
    def test_a_repeating_percent_figure_matches_a_slide_cell_exactly(self):
        value = float("33.333333333333336")
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=value,
                label="maize share",
                addresses=(),
                unit="percent",
            )
        }
        spec = {
            "slides": [
                {
                    "cells": [
                        {
                            "value": "33.333333333333336%",
                            "claim": "c1",
                            "label": "maize share",
                        }
                    ]
                }
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "maize share"}) == []


class TestCodexFindingLookupUnitCannotOverrideTheManifest:
    """A lookup's declared unit must agree with the manifest, not override it.

    "Overall R2 is 0.481% [c1]." against a manifest-fraction
    r2_overall of 0.481 must fail -- 0.481% is 0.00481, not 0.481 --
    but declaring the claim's own unit "percent" let it take 0.481%
    at face value instead, wrongly verifying. The manifest is
    harness-authored ground truth; a lookup's own declaration is
    never trusted over it.
    """

    @pytest.mark.unit
    def test_a_declared_unit_disagreeing_with_the_manifest_is_rejected(self):
        sources = {"metrics.json": {"r2_overall": 0.481}}
        units = {"metrics.json": {"r2_overall": "fraction"}}
        c = Claim(
            id="c1",
            value=0.481,
            label="overall R2",
            kind="lookup",
            source="metrics.json",
            key="r2_overall",
            unit="percent",
        )
        res = check_claims([c], sources, units=units)
        assert res[0].verdict == "unresolvable"

    @pytest.mark.unit
    def test_a_declared_unit_agreeing_with_the_manifest_still_works(self):
        sources = {"metrics.json": {"r2_overall": 0.481}}
        units = {"metrics.json": {"r2_overall": "fraction"}}
        c = Claim(
            id="c1",
            value=0.481,
            label="overall R2",
            kind="lookup",
            source="metrics.json",
            key="r2_overall",
            unit="fraction",
        )
        res = check_claims(
            [c], sources, report_text="Overall R2 is 48.1% [c1].", units=units
        )
        assert [r for r in res if r.verdict == "uncovered"] == []

    @pytest.mark.unit
    def test_a_declared_unit_with_no_manifest_entry_still_applies(self):
        """Only when the manifest has no entry for this address at
        all does the declaration apply -- unaffected by this fix."""
        sources = {"metrics.json": {"maize_share": 50.0}}
        c = Claim(
            id="c1",
            value=50.0,
            label="maize share",
            kind="lookup",
            source="metrics.json",
            key="maize_share",
            unit="percent",
        )
        res = check_claims([c], sources, report_text="Maize share is 50% [c1].")
        assert [r for r in res if r.verdict == "uncovered"] == []


class TestCodexFindingUnusedDerivedInputIsRejected:
    """A derived claim cannot borrow a row from an input its own
    expression never reads.

    expr "a" with inputs {"a": maize R2, "b": wheat R2}, labelled
    "wheat R2", passed: the label/provenance check read from
    ``inputs`` as a whole, not from what the expression actually
    used, so the unused "b" supplied cover for a label the expression
    never touched. Rejecting a declared-but-unused input outright
    also catches the writer mistake this represents on its own.
    """

    @pytest.mark.unit
    def test_an_unused_input_is_rejected_even_when_it_matches_the_label(self, sources):
        c = Claim(
            id="c1",
            value=0.886,
            label="wheat R2",
            kind="derived",
            expr="a",
            inputs={
                "a": "per_crop.csv:crop=maize:r2",
                "b": "per_crop.csv:crop=cotton:r2",
            },
            unit="fraction",
        )
        res = check_claims([c], sources)[0]
        assert res.verdict == "unresolvable"
        assert "b" in res.detail

    @pytest.mark.unit
    def test_every_declared_input_actually_used_still_passes(self, sources):
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
            unit="plain",
        )
        assert check_claims([c], sources)[0].verdict == "verified"


class TestCodexFindingManifestCountAndPlainUnitsSurviveInference:
    """A manifest unit of "count" or "plain" was discarded on the
    way through _column_unit_from_manifest (which only ever returned
    "fraction" or "percent"), so a manifest-declared count column
    fell back to the column-name heuristic, which guessed "fraction"
    -- "Maize record count is 6000% [c1]." against an n_records of
    60 (the manifest says "count") verified, and so did the slide.
    """

    COUNT_SOURCES = {"metrics.json": {"n_records": 60}}
    COUNT_UNITS = {"metrics.json": {"n_records": "count"}}

    @pytest.mark.unit
    def test_a_percent_literal_against_a_count_claim_never_matches(self):
        c = Claim(
            id="c1",
            value=60,
            label="maize record count",
            kind="lookup",
            source="metrics.json",
            key="n_records",
        )
        res = check_claims(
            [c],
            self.COUNT_SOURCES,
            report_text="Maize record count is 6000% [c1].",
            units=self.COUNT_UNITS,
        )
        assert len([r for r in res if r.verdict == "uncovered"]) == 1

    @pytest.mark.unit
    def test_the_bare_count_figure_still_passes(self):
        c = Claim(
            id="c1",
            value=60,
            label="maize record count",
            kind="lookup",
            source="metrics.json",
            key="n_records",
        )
        res = check_claims(
            [c],
            self.COUNT_SOURCES,
            report_text="Maize record count is 60 [c1].",
            units=self.COUNT_UNITS,
        )
        assert [r for r in res if r.verdict == "uncovered"] == []


class TestCodexFindingLabelMustNameTheRightMetricToo:
    """The label check covered row identity and unit, but not which
    metric: a lookup of crop=maize:mean_ape labelled "maize median
    APE" verified "Maize median APE is 20 [c1]." against mean_ape
    20.0, and a slide would inherit the wrong heading. mean_ape and
    median_ape share a token ("ape"), so the fix cannot just be
    "mentioning your own column excuses mentioning another" the way
    the row check works -- it has to compare what distinguishes the
    two columns specifically.
    """

    PER_CROP_SOURCES = {
        "per_crop.csv": [
            {"crop": "maize", "mean_ape": 20.0, "median_ape": 15.0},
            {"crop": "cotton", "mean_ape": 10.0, "median_ape": 8.0},
        ]
    }

    @pytest.mark.unit
    def test_a_label_naming_the_sibling_metric_is_rejected(self):
        c = Claim(
            id="c1",
            value=20.0,
            label="maize median APE",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:mean_ape",
        )
        res = check_claims([c], self.PER_CROP_SOURCES)[0]
        assert res.verdict == "failed"
        assert res.reason == "label_mismatch"

    @pytest.mark.unit
    def test_a_label_naming_its_own_metric_still_passes(self):
        c = Claim(
            id="c1",
            value=20.0,
            label="maize mean APE",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:mean_ape",
        )
        assert check_claims([c], self.PER_CROP_SOURCES)[0].verdict == "verified"

    @pytest.mark.unit
    def test_a_label_with_no_metric_word_at_all_still_passes(self):
        """The rule detects contradiction, not absence -- a label
        that names only the row, not any metric, is left alone."""
        c = Claim(
            id="c1",
            value=20.0,
            label="maize",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:mean_ape",
        )
        assert check_claims([c], self.PER_CROP_SOURCES)[0].verdict == "verified"

    @pytest.mark.unit
    def test_a_single_input_derived_claim_gets_the_same_metric_check(self):
        """Round 23 gave the row check this scope first; the metric
        check gets it too, since expr "a" with one input is a lookup
        wearing a different kind."""
        c = Claim(
            id="c1",
            value=20.0,
            label="maize median APE",
            kind="derived",
            expr="a",
            inputs={"a": "per_crop.csv:crop=maize:mean_ape"},
            unit="percent",
        )
        res = check_claims([c], self.PER_CROP_SOURCES)[0]
        assert res.verdict == "failed"
        assert res.reason == "label_mismatch"


class TestCodexFindingMetricIdentityAppliesToProseAndSlideTitlesToo:
    """The metric-identity check ran on claim labels, not on the
    tagged sentence or a slide title -- the prose twin of round 31,
    the same shape round 26 was to round 23's row check.

    A correctly labelled claim ("maize mean APE" reading mean_ape 20)
    still let "Maize median APE is 20% [c1]." through, because the
    sentence the figure sits in never reached _metric_contradiction
    at all. Fixed with the same one entry point
    (_metric_contradiction_for_addresses) every surface now calls:
    the claim's own label, the tagged prose sentence, and a slide
    title.
    """

    PER_CROP_SOURCES = {
        "per_crop.csv": [
            {"crop": "maize", "mean_ape": 20.0, "median_ape": 15.0},
            {"crop": "cotton", "mean_ape": 10.0, "median_ape": 8.0},
        ]
    }

    @pytest.mark.unit
    def test_a_sentence_naming_the_sibling_metric_is_caught(self):
        """Codex's exact scenario: the claim's own label is correct
        ("maize mean APE"), but the sentence states a different
        metric name around the same figure."""
        c = Claim(
            id="c1",
            value=20.0,
            label="maize mean APE",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:mean_ape",
            unit="percent",
        )
        res = check_claims(
            [c],
            self.PER_CROP_SOURCES,
            report_text="Maize median APE is 20% [c1].",
        )
        assert len([r for r in res if r.verdict == "uncovered"]) == 1

    @pytest.mark.unit
    def test_a_sentence_naming_its_own_metric_still_passes(self):
        c = Claim(
            id="c1",
            value=20.0,
            label="maize mean APE",
            kind="lookup",
            source="per_crop.csv",
            key="crop=maize:mean_ape",
            unit="percent",
        )
        res = check_claims(
            [c],
            self.PER_CROP_SOURCES,
            report_text="Maize mean APE is 20% [c1].",
        )
        assert [r for r in res if r.verdict == "uncovered"] == []

    @pytest.mark.unit
    def test_a_slide_title_naming_the_sibling_metric_is_rejected(self):
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=20.0,
                label="maize mean APE",
                addresses=("per_crop.csv:crop=maize:mean_ape",),
                unit="percent",
            )
        }
        spec = {
            "slides": [
                {
                    "title": "Maize median APE 20%",
                    "cells": [
                        {"value": 20.0, "claim": "c1", "label": "maize mean APE"}
                    ],
                }
            ]
        }
        problems = check_slide_spec(
            spec, accepted, {"c1": "maize mean APE"}, sources=self.PER_CROP_SOURCES
        )
        assert problems

    @pytest.mark.unit
    def test_a_slide_title_naming_its_own_metric_still_passes(self):
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=20.0,
                label="maize mean APE",
                addresses=("per_crop.csv:crop=maize:mean_ape",),
                unit="percent",
            )
        }
        spec = {
            "slides": [
                {
                    "title": "Maize mean APE 20%",
                    "cells": [
                        {"value": 20.0, "claim": "c1", "label": "maize mean APE"}
                    ],
                }
            ]
        }
        problems = check_slide_spec(
            spec, accepted, {"c1": "maize mean APE"}, sources=self.PER_CROP_SOURCES
        )
        assert problems == []


class TestCodexFindingSelectorColumnIsNotAMetric:
    """The row-selector column itself ("tier" in "tier=Good:n_records")
    must never be read as a sibling metric name.

    Found while regression-testing the prose/title extension above:
    "Good tier has 5 [c1]." against tiers.csv's own n_records column
    was flagged as naming the sibling metric "tier", because the
    metric check's column loop did not exclude the selector column
    the address already identifies by value, not by metric. An
    ordinary sentence naming its own row this way is not a
    contradiction.
    """

    TIER_SOURCES = {"tiers.csv": [{"tier": "Good", "n_records": 5}]}

    @pytest.mark.unit
    def test_a_sentence_naming_its_own_row_is_not_read_as_a_metric_name(self):
        c = Claim(
            id="c1",
            value=5,
            label="good tier count",
            kind="lookup",
            source="tiers.csv",
            key="tier=Good:n_records",
        )
        res = check_claims([c], self.TIER_SOURCES, report_text="Good tier has 5 [c1].")
        assert [r for r in res if r.verdict == "uncovered"] == []

    @pytest.mark.unit
    def test_a_label_naming_its_own_row_is_not_read_as_a_metric_name(self):
        c = Claim(
            id="c1",
            value=5,
            label="good tier count",
            kind="lookup",
            source="tiers.csv",
            key="tier=Good:n_records",
        )
        assert check_claims([c], self.TIER_SOURCES)[0].verdict == "verified"


class TestCodexFindingEmbeddedCellLabelFiguresArePercentAware:
    """A figure embedded in a slide cell's label was checked with
    extract_numbers, which strips any '%' before this check ever
    sees it -- the embedded-figure check compared bare digits at
    face value, never scaling a percent-suffixed figure the way the
    title path already does.

    "maize R2 (48.1%)" over a fraction-unit claim of 0.481 should
    pass (48.1% is 0.481, the same figure on the percent-scale
    wording a report routinely uses); "maize R2 (0.481%)" over the
    same claim should fail (0.481% is 0.00481, not 0.481).
    """

    @pytest.mark.unit
    def test_a_correctly_scaled_embedded_percentage_passes(self):
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=0.481,
                label="maize R2 (48.1%)",
                addresses=("per_crop.csv:crop=maize:r2",),
                unit="fraction",
            )
        }
        spec = {
            "slides": [
                {
                    "cells": [
                        {
                            "value": 0.481,
                            "claim": "c1",
                            "label": "maize R2 (48.1%)",
                        }
                    ]
                }
            ]
        }
        assert check_slide_spec(spec, accepted, {"c1": "maize R2 (48.1%)"}) == []

    @pytest.mark.unit
    def test_an_unscaled_embedded_percentage_is_caught(self):
        accepted = {
            "c1": CheckResult(
                claim_id="c1",
                verdict="verified",
                value=0.481,
                label="maize R2 (0.481%)",
                addresses=("per_crop.csv:crop=maize:r2",),
                unit="fraction",
            )
        }
        spec = {
            "slides": [
                {
                    "cells": [
                        {
                            "value": 0.481,
                            "claim": "c1",
                            "label": "maize R2 (0.481%)",
                        }
                    ]
                }
            ]
        }
        problems = check_slide_spec(spec, accepted, {"c1": "maize R2 (0.481%)"})
        assert problems


class TestCodexFindingRowCountAddressIsIntrinsicallyCountValued:
    """A row-count address ("per_record.csv:count:") has no column
    at all, so unit inference fell through to the fraction default
    even with a manifest present -- "Evaluated record count is 6000%
    [c1]." verified against a true count of 60.
    """

    COUNT_SOURCES = {
        "per_record.csv": [{"r2": 0.1}] * 60,
        "metrics.json": {"n_records": 60},
    }

    @pytest.mark.unit
    def test_a_percent_literal_against_a_row_count_claim_never_matches(self):
        c = Claim(
            id="c1",
            value=60,
            label="evaluated record count",
            kind="lookup",
            source="per_record.csv",
            key="count:",
        )
        res = check_claims(
            [c],
            self.COUNT_SOURCES,
            report_text="Evaluated record count is 6000% [c1].",
        )
        assert len([r for r in res if r.verdict == "uncovered"]) == 1

    @pytest.mark.unit
    def test_the_bare_row_count_figure_still_passes(self):
        c = Claim(
            id="c1",
            value=60,
            label="evaluated record count",
            kind="lookup",
            source="per_record.csv",
            key="count:",
        )
        res = check_claims(
            [c],
            self.COUNT_SOURCES,
            report_text="Evaluated record count is 60 [c1].",
        )
        assert [r for r in res if r.verdict == "uncovered"] == []
