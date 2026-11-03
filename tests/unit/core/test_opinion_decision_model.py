"""
Unit tests for the opinion fields on the Decision models (deliberation B1).

Both ``Decision`` dataclasses (the Context system's and the project
history's) gain an optional ``task_id``, a ``subject`` slug and a
``kind`` (``decision`` or ``assessment``).  Rows written before these
fields existed must still load, with the defaults of an architectural
decision.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.context import Context
from src.core.context import Decision as ContextDecision
from src.core.persistence import Persistence
from src.core.project_history import Decision as HistoryDecision

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


class TestHistoryDecisionOpinionFields:
    """Test suite for the project-history Decision."""

    def test_legacy_row_loads_as_an_architectural_decision(self) -> None:
        """A row without the new fields is a task-scoped decision."""
        row = {
            "decision_id": "dec_1",
            "task_id": "task_001",
            "agent_id": "agent_001",
            "timestamp": "2025-11-08T12:00:00+00:00",
            "what": "Chose PostgreSQL",
            "why": "ACID",
            "impact": "data models",
        }

        decision = HistoryDecision.from_dict(row)

        assert decision.task_id == "task_001"
        assert decision.subject is None
        assert decision.kind == "decision"

    def test_project_level_opinion_round_trips(self) -> None:
        """An opinion with no task keeps subject, kind and task_id=None."""
        decision = HistoryDecision(
            decision_id="dec_op",
            task_id=None,
            agent_id="agent-2",
            timestamp=NOW,
            what="the farm is flooded",
            why="water",
            impact="food",
            subject="farm.flooded",
            kind="assessment",
            project_id="p1",
        )

        restored = HistoryDecision.from_dict(decision.to_dict())

        assert restored == decision
        assert decision.to_dict()["task_id"] is None
        assert decision.to_dict()["subject"] == "farm.flooded"
        assert decision.to_dict()["kind"] == "assessment"


class TestContextDecisionOpinionFields:
    """Test suite for the Context system's Decision and log_decision."""

    def test_default_decision_is_task_scoped_with_no_subject(self) -> None:
        """Existing callers get the old shape plus the two new defaults."""
        decision = ContextDecision(
            decision_id="dec_1",
            task_id="t1",
            agent_id="a1",
            timestamp=NOW,
            what="chose postgres",
            why="acid",
            impact="data",
        )

        payload = decision.to_dict()

        assert payload["kind"] == "decision"
        assert payload["subject"] is None

    @pytest.mark.asyncio
    async def test_log_decision_records_a_project_level_opinion(self) -> None:
        """task_id may be None, and subject/kind are stored and persisted."""
        persistence = Mock()
        persistence.store = AsyncMock()
        context = Context(persistence=persistence, project_id="p1")

        decision = await context.log_decision(
            agent_id="agent-2",
            task_id=None,
            what="the farm is flooded",
            why="water",
            impact="food",
            subject="farm.flooded",
            kind="assessment",
        )

        assert decision.task_id is None
        assert decision.subject == "farm.flooded"
        assert decision.kind == "assessment"
        assert decision.project_id == "p1"
        assert context.decisions == [decision]
        collection, key, data = persistence.store.await_args.args
        assert collection == "decisions"
        assert key == decision.decision_id
        assert data["subject"] == "farm.flooded"
        assert data["kind"] == "assessment"
        assert data["task_id"] is None

    @pytest.mark.asyncio
    async def test_log_decision_project_id_override_wins(self) -> None:
        """A caller that knows the agent's project can stamp it explicitly."""
        context = Context(project_id="p-active")

        decision = await context.log_decision(
            agent_id="agent-2",
            task_id=None,
            what="w",
            why="y",
            impact="i",
            subject="farm.flooded",
            kind="assessment",
            project_id="p-agent",
        )

        assert decision.project_id == "p-agent"

    @pytest.mark.asyncio
    async def test_log_decision_keeps_the_old_call_shape(self) -> None:
        """Existing task-scoped callers are unchanged."""
        context = Context()

        decision = await context.log_decision(
            agent_id="a1", task_id="t1", what="w", why="y", impact="i"
        )

        assert decision.task_id == "t1"
        assert decision.kind == "decision"
        assert decision.subject is None


class TestPersistenceGetDecisions:
    """Test suite for reading opinion rows back through Persistence."""

    @pytest.mark.asyncio
    async def test_get_decisions_tolerates_opinion_rows(self) -> None:
        """A project-level opinion row is reconstructed, not dropped."""
        backend = Mock()
        backend.query = AsyncMock(
            return_value=[
                {
                    "decision_id": "dec_op",
                    "task_id": None,
                    "agent_id": "agent-2",
                    "timestamp": NOW.isoformat(),
                    "what": "the farm is flooded",
                    "why": "water",
                    "impact": "food",
                    "subject": "farm.flooded",
                    "kind": "assessment",
                }
            ]
        )
        persistence = Persistence(backend=backend)

        [decision] = await persistence.get_decisions()

        assert decision.task_id is None
        assert decision.subject == "farm.flooded"
        assert decision.kind == "assessment"
