"""Unit tests for the slide worker.

The slide worker replaces re-auditing numbers on a deck with a
mapping problem: it is given only the claims that already verified,
and the check confirms every slide figure traces back to one of
them. These tests pin what reaches the slide workspace and how a
finished deck is scored.
"""

import json

import pytest
from runners.numeric_claims import CheckResult
from runners.numeric_slides import (
    accepted_claims,
    build_slide_workspace,
    load_slide_spec,
    verify_slide_workspace,
)


def outcome_with(results):
    """Build a verify_workspace-shaped outcome from a list of results.

    Parameters
    ----------
    results : list
        CheckResult objects.

    Returns
    -------
    dict
        An outcome dict with just the ``results`` key populated.
    """
    return {"results": results}


class TestAcceptedClaims:
    """Only verified, writer-declared claims reach the slide worker."""

    @pytest.mark.unit
    def test_verified_claim_is_included(self):
        """A passing claim is kept."""
        r = CheckResult(
            claim_id="c1", verdict="verified", value=0.886, label="maize R2"
        )
        assert "c1" in accepted_claims(outcome_with([r]))

    @pytest.mark.unit
    def test_failed_claim_is_excluded(self):
        """A rejected claim cannot reach the deck."""
        r = CheckResult(claim_id="c1", verdict="failed", value=0.99, label="maize R2")
        assert accepted_claims(outcome_with([r])) == {}

    @pytest.mark.unit
    def test_uncovered_figure_is_excluded(self):
        """A figure with no claim behind it is not a usable claim."""
        r = CheckResult(claim_id="uncovered:0.99", verdict="uncovered", value="0.99")
        assert accepted_claims(outcome_with([r])) == {}

    @pytest.mark.unit
    def test_required_harness_claims_are_excluded(self):
        """A passing cross-check is not itself a reportable figure."""
        r = CheckResult(
            claim_id="required:tiers-total",
            verdict="verified",
            value=True,
            label="tiers total",
        )
        assert accepted_claims(outcome_with([r])) == {}


class TestSlideWorkspace:
    """The workspace holds only what the slide worker is allowed to use."""

    @pytest.mark.unit
    def test_accepted_claims_file_lists_id_value_label(self, tmp_path):
        """Each entry carries exactly what the worker needs."""
        r = CheckResult(
            claim_id="c1", verdict="verified", value=0.886, label="maize R2"
        )
        ws = build_slide_workspace(outcome_with([r]), tmp_path / "out")
        payload = json.loads((ws / "accepted_claims.json").read_text())
        assert payload == [{"id": "c1", "value": 0.886, "label": "maize R2"}]

    @pytest.mark.unit
    def test_rejected_claims_do_not_appear_in_the_file(self, tmp_path):
        """A failed claim leaves no trace in what the worker reads."""
        good = CheckResult(claim_id="c1", verdict="verified", value=1, label="a")
        bad = CheckResult(claim_id="c2", verdict="failed", value=2, label="b")
        ws = build_slide_workspace(outcome_with([good, bad]), tmp_path / "out")
        payload = json.loads((ws / "accepted_claims.json").read_text())
        assert [p["id"] for p in payload] == ["c1"]

    @pytest.mark.unit
    def test_workspace_is_cleared_on_rebuild(self, tmp_path):
        """A rerun does not inherit a stale previous deck."""
        out = tmp_path / "out"
        out.mkdir()
        (out / "slides.json").write_text('{"slides": []}')
        build_slide_workspace(outcome_with([]), out)
        assert not (out / "slides.json").exists()


class TestSlideVerification:
    """Scoring a finished deck against the accepted set."""

    @pytest.mark.unit
    def test_clean_deck_verifies(self, tmp_path):
        """A deck using only accepted claims, correctly, passes."""
        accepted = {
            "c1": CheckResult(
                claim_id="c1", verdict="verified", value=0.886, label="maize R2"
            )
        }
        spec = {
            "slides": [
                {"cells": [{"value": 0.886, "claim": "c1", "label": "maize R2"}]}
            ]
        }
        (tmp_path / "slides.json").write_text(json.dumps(spec))
        out = verify_slide_workspace(tmp_path, accepted)
        assert out["verdict"] == "verified"
        assert out["problems"] == []
        assert out["cells_total"] == 1

    @pytest.mark.unit
    def test_missing_slides_file_fails_with_a_named_problem(self, tmp_path):
        """A worker that produced nothing is a failure, not a pass."""
        out = verify_slide_workspace(tmp_path, {})
        assert out["verdict"] == "failed"
        assert any("missing" in p for p in out["problems"])

    @pytest.mark.unit
    def test_invented_figure_with_no_claim_fails(self, tmp_path):
        """A number with no claim id is caught."""
        spec = {"slides": [{"cells": [{"value": 0.99, "label": "x"}]}]}
        (tmp_path / "slides.json").write_text(json.dumps(spec))
        out = verify_slide_workspace(tmp_path, {})
        assert out["verdict"] == "failed"
        assert out["problems"]

    @pytest.mark.unit
    def test_accepted_available_is_reported(self, tmp_path):
        """The scorer records how many claims the worker could draw on."""
        accepted = {
            "c1": CheckResult(claim_id="c1", verdict="verified", value=1, label="a"),
            "c2": CheckResult(claim_id="c2", verdict="verified", value=2, label="b"),
        }
        (tmp_path / "slides.json").write_text('{"slides": []}')
        out = verify_slide_workspace(tmp_path, accepted)
        assert out["accepted_available"] == 2


class TestLoadSlideSpec:
    """Reading back whatever the worker actually wrote."""

    @pytest.mark.unit
    def test_malformed_json_is_reported_not_raised(self, tmp_path):
        """A broken file does not crash the scorer."""
        (tmp_path / "slides.json").write_text("{not json")
        spec = load_slide_spec(tmp_path)
        assert spec["slides"] == []
        assert "_problem" in spec

    @pytest.mark.unit
    def test_missing_slides_key_is_reported(self, tmp_path):
        """A file with the wrong shape is caught, not silently emptied."""
        (tmp_path / "slides.json").write_text('{"decks": []}')
        spec = load_slide_spec(tmp_path)
        assert "_problem" in spec
