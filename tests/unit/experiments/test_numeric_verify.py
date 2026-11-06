"""Unit tests for the verifier's workspace check.

The verifier is handed a staged evaluation, a written report and the
claims that report declared. It must survive a writer that produced
nothing usable, and it must report coverage honestly, because a
report that declares five tidy claims and writes six numbers has not
been fully checked.
"""

import hashlib
import json

import pytest
from runners.numeric_verify import verify_workspace
from runners.numeric_writer import trusted_hashes_path


def _write_trusted_manifest(workspace):
    """Write the harness-retained manifest matching staged/ as it is now.

    The real integrity check trusts only this sibling file, never
    anything inside the workspace -- see
    ``runners.numeric_writer.trusted_hashes_path``. Tests that build a
    workspace by hand (not via ``build_workspace``) call this to get
    a passing-by-default manifest, the same way a real staged case
    always has one.

    Parameters
    ----------
    workspace : pathlib.Path
        The workspace whose current ``staged/`` contents to hash.
    """
    staged = workspace / "staged"
    hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(staged.glob("*"))
        if p.is_file()
    }
    trusted_hashes_path(workspace).write_text(json.dumps(hashes, indent=2))


@pytest.fixture
def workspace(tmp_path):
    """Build a workspace with a consistent staged evaluation.

    Carries a matching trusted hash manifest by default, same as a
    real workspace from ``build_workspace`` always does, so tests
    that are not specifically about staged integrity do not need to
    set one up themselves.

    Returns
    -------
    pathlib.Path
        The workspace directory.
    """
    ws = tmp_path / "case"
    staged = ws / "staged"
    staged.mkdir(parents=True)
    (staged / "metrics.json").write_text(
        json.dumps({"n_records": 100, "r2_overall": 0.9})
    )
    (staged / "per_crop.csv").write_text(
        "crop,n_records,r2,unit\n"
        "maize,80,0.91,BU / ACRE\n"
        "cotton,20,0.44,LB / ACRE\n"
    )
    (staged / "tiers.csv").write_text(
        "tier,n_records,rate_pct\nGood,60,60.0\nPoor,40,40.0\n"
    )
    _write_trusted_manifest(ws)
    return ws


def write(ws, report, claims):
    """Place a report and its claims into the workspace.

    Parameters
    ----------
    ws : pathlib.Path
        The workspace.
    report : str
        Report text.
    claims : list
        Claim dicts.
    """
    (ws / "report.md").write_text(report)
    (ws / "claims.json").write_text(json.dumps({"claims": claims}))


class TestMissingOutput:
    """A writer that produced nothing must not look like a pass."""

    @pytest.mark.unit
    def test_absent_claims_file_is_a_failure(self, workspace):
        """No claims file means nothing was checked."""
        (workspace / "report.md").write_text("The score was 0.9.")
        out = verify_workspace(workspace)
        assert out["verdict"] == "failed"
        assert "missing" in out["parse_error"]

    @pytest.mark.unit
    def test_malformed_claims_file_is_a_failure(self, workspace):
        """Unparseable claims are reported, not ignored."""
        (workspace / "report.md").write_text("The score was 0.9.")
        (workspace / "claims.json").write_text("{not json")
        out = verify_workspace(workspace)
        assert out["verdict"] == "failed"
        assert "could not be read" in out["parse_error"]

    @pytest.mark.unit
    def test_malformed_inputs_field_is_a_failure_not_a_crash(self, workspace):
        """A claim whose "inputs" is a list, not an object, fails gracefully.

        Claim.from_dict calls dict(raw.get("inputs") or {}); a list
        like ["a"] is valid JSON but dict(["a"]) raises ValueError,
        which previously escaped uncaught and aborted the whole
        verify_workspace (and load_condition_b) pass.
        """
        (workspace / "report.md").write_text("R2 was 0.9 [c1].")
        (workspace / "claims.json").write_text(
            json.dumps(
                {
                    "claims": [
                        {
                            "id": "c1",
                            "value": 0.9,
                            "label": "x",
                            "kind": "derived",
                            "expr": "a",
                            "inputs": ["a"],
                        }
                    ]
                }
            )
        )
        out = verify_workspace(workspace)
        assert out["verdict"] == "failed"
        assert "could not be read" in out["parse_error"]


class TestCoverage:
    """Coverage is the fraction of stated figures carrying a claim."""

    @pytest.mark.unit
    def test_full_coverage_is_reported(self, workspace):
        """Every figure declared gives complete coverage."""
        write(
            workspace,
            "The overall score was 0.9 [c1].",
            [
                {
                    "id": "c1",
                    "value": 0.9,
                    "label": "overall R2",
                    "kind": "lookup",
                    "source": "metrics.json",
                    "key": "r2_overall",
                }
            ],
        )
        out = verify_workspace(workspace)
        assert out["summary"]["coverage"] == 1.0
        assert out["verdict"] == "verified"

    @pytest.mark.unit
    def test_an_undeclared_figure_lowers_coverage_and_fails(self, workspace):
        """A number with no claim is a failure of the report."""
        write(
            workspace,
            "The overall score was 0.9 [c1], and cotton reached 0.44.",
            [
                {
                    "id": "c1",
                    "value": 0.9,
                    "label": "overall R2",
                    "kind": "lookup",
                    "source": "metrics.json",
                    "key": "r2_overall",
                }
            ],
        )
        out = verify_workspace(workspace)
        assert out["summary"]["uncovered"] == 1
        assert out["summary"]["coverage"] < 1.0
        assert out["verdict"] == "failed"


class TestRequiredChecksRun:
    """The harness's own cross-checks run whatever the writer declared."""

    @pytest.mark.unit
    def test_short_table_fails_though_the_writer_was_silent(self, workspace):
        """A writer cannot pass by omitting the contradiction.

        This test is about the staged data's own tiers table being
        short, not about tampering, so the trusted manifest is
        refreshed to match the table this test stages -- otherwise
        the staged-integrity check (which has no way to tell "this is
        the test's real data" from "something edited staged/ after
        the manifest was written") would short-circuit first.
        """
        (workspace / "staged" / "tiers.csv").write_text(
            "tier,n_records,rate_pct\nGood,60,60.0\nPoor,35,35.0\n"
        )
        _write_trusted_manifest(workspace)
        write(workspace, "A report with no figures at all.", [])
        out = verify_workspace(workspace)
        assert out["verdict"] == "failed"
        assert any("required:" in c["claim_id"] for c in out["failed_claims"])


class TestRetryHandoff:
    """What a failed check hands back to the writer."""

    @pytest.mark.unit
    def test_failures_carry_an_id_and_a_reason(self, workspace):
        """The writer is told which claim failed and why."""
        write(
            workspace,
            "The overall score was 0.95.",
            [
                {
                    "id": "c1",
                    "value": 0.95,
                    "label": "overall R2",
                    "kind": "lookup",
                    "source": "metrics.json",
                    "key": "r2_overall",
                }
            ],
        )
        out = verify_workspace(workspace)
        failure = next(c for c in out["failed_claims"] if c["claim_id"] == "c1")
        assert "0.9" in failure["detail"]

    @pytest.mark.unit
    def test_claim_kinds_are_counted(self, workspace):
        """The summary records what kinds the writer used."""
        write(
            workspace,
            "Maize held 80 of 100 records, a share of 0.8.",
            [
                {
                    "id": "c1",
                    "value": 80,
                    "label": "maize records",
                    "kind": "lookup",
                    "source": "per_crop.csv",
                    "key": "crop=maize:n_records",
                },
                {
                    "id": "c2",
                    "value": 100,
                    "label": "records evaluated",
                    "kind": "lookup",
                    "source": "metrics.json",
                    "key": "n_records",
                },
                {
                    "id": "c3",
                    "value": 0.8,
                    "label": "maize share",
                    "kind": "derived",
                    "expr": "a / b",
                    "inputs": {
                        "a": "per_crop.csv:crop=maize:n_records",
                        "b": "metrics.json:n_records",
                    },
                },
            ],
        )
        out = verify_workspace(workspace)
        assert out["summary"]["kinds"]["lookup"] == 2
        assert out["summary"]["kinds"]["derived"] == 1


class TestCodexFindingMissingReportIsAFailure:
    """P2: a missing or empty report.md was never reported as a problem.

    report_path.read_text() if exists else "" produced an empty
    string, which was then passed to check_report as None, disabling
    coverage checks entirely rather than failing loudly. A workspace
    with clean staged data and a valid (or empty) claims list could
    receive a verified verdict and coverage of 1.0 despite containing
    no report at all. numeric_grader.load_condition_b calls this
    verifier directly, so this would have scored an incomplete run as
    clean.
    """

    @pytest.mark.unit
    def test_missing_report_file_is_a_failure(self, workspace):
        """No report.md at all must not verify."""
        (workspace / "claims.json").write_text(json.dumps({"claims": []}))
        out = verify_workspace(workspace)
        assert out["verdict"] == "failed"
        assert out.get("parse_error")

    @pytest.mark.unit
    def test_empty_report_file_is_a_failure(self, workspace):
        """An empty report.md is not a clean, coverage-complete report."""
        (workspace / "report.md").write_text("")
        (workspace / "claims.json").write_text(json.dumps({"claims": []}))
        out = verify_workspace(workspace)
        assert out["verdict"] == "failed"
        assert out.get("parse_error")


class TestCodexFindingStagedIntegrity:
    """A writer could modify its own staged/ copy and have it trusted.

    verify_workspace read staged/ unconditionally, with no check that
    it still matched what numeric_stage.py originally wrote. A writer
    that edited its workspace's staged/ copy -- by mistake or to make
    a wrong claim look right -- would have that edit treated as
    ground truth, and load_condition_b does not catch it either since
    it calls this same function. The ``workspace`` fixture writes a
    matching trusted manifest (the one build_workspace always writes
    for a real case) by default, so these tests are about what
    happens when that manifest and the staged data disagree, not
    about setting a manifest up.

    Round 17 corrected this fix itself: the manifest originally lived
    *inside* the workspace (``staged/HASHES.json``), which a writer
    could edit right along with the data it was supposed to guard.
    The real manifest now lives outside the workspace entirely (see
    ``trusted_hashes_path``), so nothing written inside the workspace
    -- including a regenerated copy of the old in-workspace file --
    can affect what this check trusts.
    """

    @pytest.mark.unit
    def test_clean_staged_files_pass_through_unaffected(self, workspace):
        """A manifest that matches every file changes nothing."""
        write(
            workspace,
            "The overall score was 0.9 [c1].",
            [
                {
                    "id": "c1",
                    "value": 0.9,
                    "label": "overall R2",
                    "kind": "lookup",
                    "source": "metrics.json",
                    "key": "r2_overall",
                }
            ],
        )
        out = verify_workspace(workspace)
        assert out["verdict"] == "verified"
        assert not out.get("parse_error")

    @pytest.mark.unit
    def test_a_modified_staged_file_is_caught_not_trusted(self, workspace):
        """A writer editing its own staged/ copy must not pass as truth."""
        (workspace / "staged" / "metrics.json").write_text(
            json.dumps({"n_records": 100, "r2_overall": 0.99})
        )
        write(
            workspace,
            "The overall score was 0.99.",
            [
                {
                    "id": "c1",
                    "value": 0.99,
                    "label": "overall R2",
                    "kind": "lookup",
                    "source": "metrics.json",
                    "key": "r2_overall",
                }
            ],
        )
        out = verify_workspace(workspace)
        assert out["verdict"] == "failed"
        assert "metrics.json" in out["parse_error"]

    @pytest.mark.unit
    def test_a_staged_file_deleted_after_staging_is_caught(self, workspace):
        """A staged file missing from a manifested set is a failure."""
        (workspace / "staged" / "metrics.json").unlink()
        write(workspace, "The overall score was 0.9.", [])
        out = verify_workspace(workspace)
        assert out["verdict"] == "failed"
        assert "metrics.json" in out["parse_error"]

    @pytest.mark.unit
    def test_a_missing_trusted_manifest_is_a_hard_failure(self, workspace):
        """Codex's first demonstration: change a staged value, declare
        the matching claim, delete the manifest -- the old design let
        this verify; a missing manifest is now always a failure, not
        "nothing to check"."""
        (workspace / "staged" / "metrics.json").write_text(
            json.dumps({"n_records": 100, "r2_overall": 0.999})
        )
        trusted_hashes_path(workspace).unlink()
        write(
            workspace,
            "The overall score was 0.999 [c1].",
            [
                {
                    "id": "c1",
                    "value": 0.999,
                    "label": "overall R2",
                    "kind": "lookup",
                    "source": "metrics.json",
                    "key": "r2_overall",
                }
            ],
        )
        out = verify_workspace(workspace)
        assert out["verdict"] == "failed"
        assert "manifest" in out["parse_error"]

    @pytest.mark.unit
    def test_a_regenerated_in_workspace_manifest_has_no_effect(self, workspace):
        """Codex's second demonstration: regenerate the manifest after
        the edit. Writing a new staged/HASHES.json that matches the
        tampered data -- the old design's trust point -- must not
        rescue the claim, because the real manifest outside the
        workspace still reflects the original, untampered value."""
        (workspace / "staged" / "metrics.json").write_text(
            json.dumps({"n_records": 100, "r2_overall": 0.999})
        )
        tampered_hashes = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((workspace / "staged").glob("*"))
            if p.is_file()
        }
        (workspace / "staged" / "HASHES.json").write_text(
            json.dumps(tampered_hashes, indent=2)
        )
        write(
            workspace,
            "The overall score was 0.999 [c1].",
            [
                {
                    "id": "c1",
                    "value": 0.999,
                    "label": "overall R2",
                    "kind": "lookup",
                    "source": "metrics.json",
                    "key": "r2_overall",
                }
            ],
        )
        out = verify_workspace(workspace)
        assert out["verdict"] == "failed"
        assert "metrics.json" in out["parse_error"]


class TestCodexFindingMistaggedFiguresDoNotCorruptCoverage:
    """A mistagged figure must count against coverage, not around it.

    #828's own fix added a value-mismatch-under-tag result, but it
    was coded as VERDICT_FAILED. verify_workspace's coverage and
    claims_required math only subtracted VERDICT_UNCOVERED results,
    so a report with exactly this problem could still report 100%
    coverage and a wrong claims_required count. Reclassified as
    VERDICT_UNCOVERED (a figure with no valid claim behind it is
    exactly what "uncovered" means), this now reads correctly with
    no change needed in this file.
    """

    @pytest.mark.unit
    def test_a_mistagged_figure_lowers_reported_coverage(self, workspace):
        """R2 is 0.8 [c1], backed by a verified claim of 0.9, is not
        100% coverage."""
        write(
            workspace,
            "R2 is 0.8 [c1].",
            [
                {
                    "id": "c1",
                    "value": 0.9,
                    "label": "overall R2",
                    "kind": "lookup",
                    "source": "metrics.json",
                    "key": "r2_overall",
                }
            ],
        )
        out = verify_workspace(workspace)
        assert out["summary"]["coverage"] < 1.0
        assert out["summary"]["uncovered"] == 1

    @pytest.mark.unit
    def test_a_mistagged_figure_does_not_inflate_claims_required(self, workspace):
        """A mistagged figure must not be miscounted as a harness-required
        claim -- claims_required must equal the genuine required count,
        whether or not a mistag is also present in the same report."""
        claims = [
            {
                "id": "c1",
                "value": 0.9,
                "label": "overall R2",
                "kind": "lookup",
                "source": "metrics.json",
                "key": "r2_overall",
            }
        ]
        write(workspace, "R2 is 0.9 [c1].", claims)
        clean_required = verify_workspace(workspace)["summary"]["claims_required"]

        write(workspace, "R2 is 0.8 [c1].", claims)
        out = verify_workspace(workspace)
        assert out["summary"]["claims_required"] == clean_required
