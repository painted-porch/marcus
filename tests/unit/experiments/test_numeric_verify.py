"""Unit tests for the verifier's workspace check.

The verifier is handed a staged evaluation, a written report and the
claims that report declared. It must survive a writer that produced
nothing usable, and it must report coverage honestly, because a
report that declares five tidy claims and writes six numbers has not
been fully checked.
"""

import json

import pytest
from runners.numeric_verify import verify_workspace


@pytest.fixture
def workspace(tmp_path):
    """Build a workspace with a consistent staged evaluation.

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


class TestCoverage:
    """Coverage is the fraction of stated figures carrying a claim."""

    @pytest.mark.unit
    def test_full_coverage_is_reported(self, workspace):
        """Every figure declared gives complete coverage."""
        write(
            workspace,
            "The overall score was 0.9.",
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
            "The overall score was 0.9, and cotton reached 0.44.",
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
        """A writer cannot pass by omitting the contradiction."""
        (workspace / "staged" / "tiers.csv").write_text(
            "tier,n_records,rate_pct\nGood,60,60.0\nPoor,35,35.0\n"
        )
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
