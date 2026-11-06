"""Unit tests for the numeric-verifier report writer.

collect_output is the only place that reads back whatever the writer
actually produced, so it has to survive a writer that wrote garbage,
not just a writer that wrote nothing.
"""

import json

import pytest
from runners.numeric_writer import build_workspace, collect_output


def write(ws, report=None, claims_raw=None):
    """Place a report and/or raw claims.json content into a workspace.

    Parameters
    ----------
    ws : pathlib.Path
        The workspace.
    report : Optional[str]
        report.md text; omitted means no report.md at all.
    claims_raw : Optional[str]
        Raw claims.json text, written as-is (not re-encoded), so a
        test can write a malformed or oddly-shaped payload directly.
    """
    if report is not None:
        (ws / "report.md").write_text(report)
    if claims_raw is not None:
        (ws / "claims.json").write_text(claims_raw)


class TestCodexFindingNonObjectClaimEntryDoesNotCrash:
    """A claims list containing a non-object entry crashed collect_output.

    The claims loop calls ``claim.get("id")`` on every entry
    unconditionally. ``{"claims": [null]}`` is valid JSON, so it
    passes the dict-or-list check, but a ``None`` entry has no
    ``.get`` method: an uncaught ``AttributeError`` escaped
    collect_output instead of being reported as a named problem, the
    same way every other malformed claims.json shape already is.
    """

    @pytest.mark.unit
    def test_a_null_claim_entry_is_a_named_problem_not_a_crash(self, tmp_path):
        """A null entry in the claims list fails gracefully."""
        write(tmp_path, report="x", claims_raw=json.dumps({"claims": [None]}))
        got = collect_output(tmp_path)
        assert any("claim" in p for p in got["problems"])

    @pytest.mark.unit
    def test_a_non_object_claim_entry_is_a_named_problem_not_a_crash(self, tmp_path):
        """A string entry in the claims list fails gracefully too."""
        write(tmp_path, report="x", claims_raw=json.dumps({"claims": ["c1"]}))
        got = collect_output(tmp_path)
        assert any("claim" in p for p in got["problems"])

    @pytest.mark.unit
    def test_well_formed_claims_are_unaffected(self, tmp_path):
        """The fix must not disturb ordinary, valid claims."""
        write(
            tmp_path,
            report="x",
            claims_raw=json.dumps(
                {"claims": [{"id": "c1", "value": 1, "label": "x", "kind": "lookup"}]}
            ),
        )
        got = collect_output(tmp_path)
        assert got["problems"] == []
        assert len(got["claims"]) == 1


class TestCodexFindingStaleSiblingTrapReachable:
    """The stale_sibling trap must actually be reachable by the writer.

    numeric_stage.stage_case plants a second eval_* directory beside
    the real one for this trap. build_workspace copied only
    staged_dir's own contents into staged/, so the planted sibling
    was never present anywhere in the writer's workspace -- the trap
    could not tempt a writer into anything, while the manifest and
    grader still counted it as a trap opportunity.
    """

    @pytest.mark.unit
    def test_a_planted_sibling_eval_dir_is_copied_into_the_workspace(self, tmp_path):
        """A sibling eval_* directory next to staged_dir is reachable."""
        case_dir = tmp_path / "case-000"
        current = case_dir / "eval_20261103_120000"
        stale = case_dir / "eval_20261102_120000"
        current.mkdir(parents=True)
        stale.mkdir(parents=True)
        (current / "metrics.json").write_text('{"r2_overall": 0.886}')
        (stale / "metrics.json").write_text('{"r2_overall": 0.844}')

        workspace = build_workspace(current, tmp_path / "out")

        assert (workspace / "staged" / "metrics.json").read_text() == (
            '{"r2_overall": 0.886}'
        )
        assert (workspace / "eval_20261102_120000" / "metrics.json").read_text() == (
            '{"r2_overall": 0.844}'
        )

    @pytest.mark.unit
    def test_no_sibling_means_no_extra_directory(self, tmp_path):
        """A case with no planted trap is unaffected."""
        case_dir = tmp_path / "case-001"
        current = case_dir / "eval_20261103_120000"
        current.mkdir(parents=True)
        (current / "metrics.json").write_text("{}")

        workspace = build_workspace(current, tmp_path / "out")

        assert sorted(p.name for p in workspace.iterdir()) == ["staged"]
