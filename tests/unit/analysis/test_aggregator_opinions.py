"""
Unit tests for how the history aggregator treats opinions (deliberation B1).

A project-level opinion has no task.  The aggregator's per-task history
must skip it rather than invent a task history keyed by ``None``; the
opinion is read through ``query_project_history(..., "opinions")``.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.analysis.aggregator import ProjectHistoryAggregator
from src.core.project_history import Decision

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


class TestAggregatorSkipsProjectLevelOpinions:
    """Test suite for task histories built from decisions and opinions."""

    def test_project_level_opinion_creates_no_task_history(
        self, tmp_path: Path
    ) -> None:
        """Only the task-scoped decision is attached to a task."""
        aggregator = ProjectHistoryAggregator(marcus_root=tmp_path)
        task_decision = Decision(
            decision_id="dec_1",
            task_id="t1",
            agent_id="agent-1",
            timestamp=NOW,
            what="chose postgres",
            why="acid",
            impact="data",
        )
        project_opinion = Decision(
            decision_id="dec_op",
            task_id=None,
            agent_id="agent-2",
            timestamp=NOW,
            what="the farm is flooded",
            why="water",
            impact="food",
            subject="farm.flooded",
            kind="assessment",
        )

        histories = aggregator._build_task_histories(
            [], [task_decision, project_opinion], [], [], []
        )

        assert [h.task_id for h in histories] == ["t1"]
        assert histories[0].decisions_made == [task_decision]
