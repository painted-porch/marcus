"""
Unit tests: the objective primer is readable by kind and never counted as an opinion.

``group_opinions_by_subject`` defaults to ``kind="assessment"``. A primer
written with ``kind="objective"`` must be excluded from that default read,
so a project's stated objective is never counted toward distinct-agent
corroboration, and must come back when a reader asks for
``kind="objective"`` explicitly.
"""

from datetime import datetime, timezone

import pytest

from src.analysis.query_api import group_opinions_by_subject
from src.core.project_history import Decision

pytestmark = pytest.mark.unit


def _decision(kind: str, subject: str, agent: str) -> Decision:
    return Decision(
        decision_id=f"d-{kind}-{agent}",
        agent_id=agent,
        task_id=None,
        what="text",
        why="why",
        impact="impact",
        timestamp=datetime(2026, 11, 5, tzinfo=timezone.utc),
        subject=subject,
        kind=kind,
    )


class TestObjectiveKind:
    def test_default_read_excludes_the_objective(self) -> None:
        decisions = [
            _decision("objective", "project.objective", "marcus"),
            _decision("assessment", "wood.stocked", "ash"),
        ]
        subjects = {e["subject"] for e in group_opinions_by_subject(decisions)}
        assert subjects == {"wood.stocked"}

    def test_objective_read_returns_only_the_objective(self) -> None:
        decisions = [
            _decision("objective", "project.objective", "marcus"),
            _decision("assessment", "wood.stocked", "ash"),
        ]
        entries = group_opinions_by_subject(decisions, kind="objective")
        assert [e["subject"] for e in entries] == ["project.objective"]
        assert entries[0]["kind"] == "objective"
