"""Unit tests for the slide worker.

The slide worker replaces re-auditing numbers on a deck with a
mapping problem: it is given only the claims that already verified,
and the check confirms every slide figure traces back to one of
them. These tests pin what reaches the slide workspace and how a
finished deck is scored.
"""

import hashlib
import json
from unittest.mock import patch

import pytest
from runners.numeric_claims import CheckResult
from runners.numeric_slides import (
    accepted_claims,
    build_slide_workspace,
    load_slide_spec,
    main,
    verify_slide_workspace,
)
from runners.numeric_writer import trusted_hashes_path


def _write_trusted_manifest(workspace):
    """Write the harness-retained manifest matching workspace/staged/.

    See ``runners.numeric_writer.trusted_hashes_path``: a hand-built
    workspace in a test has no manifest unless it writes one itself.
    """
    staged = workspace / "staged"
    hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(staged.glob("*"))
        if p.is_file()
    }
    trusted_hashes_path(workspace).write_text(json.dumps(hashes, indent=2))


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

    @pytest.mark.unit
    def test_null_slides_value_is_reported_not_a_crash(self, tmp_path):
        """A null "slides" value is caught before it reaches verification."""
        (tmp_path / "slides.json").write_text('{"slides": null}')
        spec = load_slide_spec(tmp_path)
        assert spec["slides"] == []
        assert "_problem" in spec

    @pytest.mark.unit
    def test_a_null_slide_in_the_list_is_reported_not_a_crash(self, tmp_path):
        """A null entry inside an otherwise valid slides list is caught."""
        (tmp_path / "slides.json").write_text('{"slides": [null]}')
        spec = load_slide_spec(tmp_path)
        assert spec["slides"] == []
        assert "_problem" in spec

    @pytest.mark.unit
    def test_non_list_cells_is_reported_not_a_crash(self, tmp_path):
        """A slide whose cells is not a list is caught before verification."""
        (tmp_path / "slides.json").write_text('{"slides": [{"cells": null}]}')
        spec = load_slide_spec(tmp_path)
        assert spec["slides"] == []
        assert "_problem" in spec

    @pytest.mark.unit
    def test_well_formed_spec_passes_through_unchanged(self, tmp_path):
        """The fix must not disturb an ordinary, valid slide spec."""
        (tmp_path / "slides.json").write_text(
            '{"slides": [{"title": "x", "cells": [{"value": "1", "claim": "c1"}]}]}'
        )
        spec = load_slide_spec(tmp_path)
        assert "_problem" not in spec
        assert len(spec["slides"]) == 1


class TestCodexFindingSlideGenerationRequiresAVerifiedReport:
    """A rejected report must not still produce a successful deck.

    main() extracted individually verified claims and built a deck
    from them without ever checking the overall report verdict. A
    report that fails a harness-required consistency check (H4's
    guarantee is specifically about this: "a deck assembled only from
    accepted claims cannot carry a figure the report verifier
    rejected") still has individually-correct claims to build a
    technically-mapping-clean deck from, which let a declined report
    produce an apparently successful slide run.

    Codex's exact example: metrics.json says 10 records, tiers.csv's
    counts add up to 9. The required cross-table check fails the
    report, but the tier count of 9 is still individually verified
    and would still map cleanly onto a slide.
    """

    @pytest.mark.unit
    def test_a_report_failing_a_required_check_refuses_slide_generation(self, tmp_path):
        """The 10-vs-9 example: slide generation must refuse, not build."""
        ws = tmp_path / "case"
        staged = ws / "staged"
        staged.mkdir(parents=True)
        (staged / "metrics.json").write_text(json.dumps({"n_records": 10}))
        (staged / "tiers.csv").write_text("tier,n_records\nGood,5\nPoor,4\n")
        (ws / "report.md").write_text("Good tier has 5 [c1].")
        (ws / "claims.json").write_text(
            json.dumps(
                {
                    "claims": [
                        {
                            "id": "c1",
                            "value": 5,
                            "label": "good tier count",
                            "kind": "lookup",
                            "source": "tiers.csv",
                            "key": "tier=Good:n_records",
                        }
                    ]
                }
            )
        )

        with patch("runners.numeric_slides.run_slide_worker") as mock_run:
            code = main(
                [
                    "--verify-workspace",
                    str(ws),
                    "--out-dir",
                    str(tmp_path / "slides"),
                ]
            )

        assert code != 0
        mock_run.assert_not_called()
        assert not (tmp_path / "slides").exists()

    @pytest.mark.unit
    def test_a_cleanly_verified_report_still_proceeds(self, tmp_path):
        """The fix must not block an ordinary, fully-verified report."""
        ws = tmp_path / "case"
        staged = ws / "staged"
        staged.mkdir(parents=True)
        (staged / "metrics.json").write_text(json.dumps({"n_records": 9}))
        (staged / "tiers.csv").write_text("tier,n_records\nGood,5\nPoor,4\n")
        _write_trusted_manifest(ws)
        (ws / "report.md").write_text("Good tier has 5 [c1].")
        (ws / "claims.json").write_text(
            json.dumps(
                {
                    "claims": [
                        {
                            "id": "c1",
                            "value": 5,
                            "label": "good tier count",
                            "kind": "lookup",
                            "source": "tiers.csv",
                            "key": "tier=Good:n_records",
                        }
                    ]
                }
            )
        )

        with patch("runners.numeric_slides.run_slide_worker") as mock_run:
            mock_run.return_value = {
                "returncode": 0,
                "seconds": 0.1,
                "stdout_tail": "",
                "stderr_tail": "",
            }
            main(
                [
                    "--verify-workspace",
                    str(ws),
                    "--out-dir",
                    str(tmp_path / "slides"),
                ]
            )

        mock_run.assert_called_once()


class TestCodexFindingSlidesTopLevelContainerShapeIsMechanical:
    """slides.json's own top-level shape, swept the same way as claims.json.

    Already safe (the existing ``isinstance(spec, dict)`` check
    catches all of these), pinned here so the whole mechanical sweep
    -- claims.json, slides.json, self_check.json -- lives as one
    documented set rather than scattered findings.
    """

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "bad_top_level", [None, 42, "a string", [1, 2, 3]], ids=repr
    )
    def test_a_non_object_top_level_document_is_reported_not_a_crash(
        self, tmp_path, bad_top_level
    ):
        """None, a number, a string, or a bare list at the top level
        -- none of these are the required object-with-"slides" shape."""
        (tmp_path / "slides.json").write_text(json.dumps(bad_top_level))
        spec = load_slide_spec(tmp_path)
        assert spec["slides"] == []
        assert "_problem" in spec
