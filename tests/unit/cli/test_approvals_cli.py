"""
Unit tests for the approvals CLI (#737, step 5).

The smallest human interface: a command that registers the reviewer
as a human principal, pulls open approve tasks through the same MCP
tool path agents use, and completes one with a decision recorded as
evidence. These tests cover the pure pieces; the tool path itself is
covered by the approval-gate and eligibility tests.
"""

from datetime import datetime

import pytest

from src.cli.approvals import _decision_evidence, build_parser

pytestmark = pytest.mark.unit


class TestBuildParser:
    """Test suite for the CLI argument surface."""

    def test_defaults_point_at_the_local_marcus(self) -> None:
        """The CLI defaults to the local HTTP endpoint and reviewer id."""
        parser = build_parser()

        args = parser.parse_args(["--project", "tow-pilot"])

        assert args.url == "http://localhost:4298/mcp"
        assert args.reviewer == "reviewer"
        assert args.project == "tow-pilot"
        assert args.decide is None

    def test_project_is_required(self) -> None:
        """Registration needs a project_id (GH-388), so the CLI does too."""
        parser = build_parser()

        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_scripted_decision_accepts_only_the_two_outcomes(self) -> None:
        """--decide takes approved or rejected, nothing else."""
        parser = build_parser()

        approved = parser.parse_args(["--project", "p", "--decide", "approved"])
        with pytest.raises(SystemExit):
            parser.parse_args(["--project", "p", "--decide", "maybe"])

        assert approved.decide == "approved"


class TestDecisionEvidence:
    """Test suite for the evidence payload an approval submits."""

    def test_evidence_carries_decision_reviewer_and_timestamp(self) -> None:
        """The approval decision is typed evidence on the record."""
        payload = _decision_evidence("larry", "approved")

        assert payload["decision"] == "approved"
        assert payload["approved_by"] == "larry"
        datetime.fromisoformat(payload["decided_at"])

    def test_rejected_decision_is_recorded_the_same_way(self) -> None:
        """A rejection closes the approve task with the decision kept."""
        payload = _decision_evidence("larry", "rejected")

        assert payload["decision"] == "rejected"

    def test_unknown_decision_raises(self) -> None:
        """Only the two decisions exist; anything else is a programming error."""
        with pytest.raises(ValueError):
            _decision_evidence("larry", "shrug")
