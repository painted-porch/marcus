"""
Unit tests for the ``give_opinion`` tool (deliberation layer, B1).

An opinion is a board marker: an agent's assessment of something in the
project that is not necessarily about the task it holds.  It is stored
as a ``Decision`` with ``kind="assessment"`` and a normalised ``subject``
slug so that several agents flagging the same thing can be counted
together.  A project-level opinion (no ``task_id``) gets its kanban
comment on the board's About card.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.context import give_opinion, normalize_subject

pytestmark = pytest.mark.unit


def _about_card() -> Task:
    """Build the board's About card the way create_project does."""
    now = datetime.now(timezone.utc)
    return Task(
        id="about_project",
        name="About: Wheat Farm",
        description="overview",
        status=TaskStatus.DONE,
        priority=Priority.LOW,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=0,
        labels=["documentation"],
        source_type="project_about",
    )


def _logged_decision(**overrides: Any) -> Mock:
    """Build the Decision the Context returns from log_decision."""
    decision = Mock()
    decision.decision_id = "dec_7_1.0"
    payload = {
        "decision_id": "dec_7_1.0",
        "task_id": None,
        "agent_id": "agent-2",
        "subject": "farm.flooded",
        "kind": "assessment",
        "what": "the farm is flooded",
        "why": "the north water source breached",
        "impact": "food supply",
    }
    payload.update(overrides)
    decision.to_dict = Mock(return_value=payload)
    return decision


def _make_state(with_about_card: bool = True) -> Mock:
    """Build a Mock state with a Context, a kanban client and a board."""
    state = Mock()
    state.context = Mock()
    state.context.log_decision = AsyncMock(return_value=_logged_decision())
    state.kanban_client = Mock()
    state.kanban_client.add_comment = AsyncMock(return_value={"success": True})
    state.project_tasks = [_about_card()] if with_about_card else []
    state.events = None
    return state


class TestNormalizeSubject:
    """Test suite for the subject slug normaliser."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("farm.flooded", "farm.flooded"),
            ("Farm Flooded", "farm.flooded"),
            ("farm_flooded", "farm.flooded"),
            ("  schema.users  ", "schema.users"),
            ("..farm--flooded..", "farm.flooded"),
            ("FARM / Flooded!!", "farm.flooded"),
            ("", ""),
            ("!!!", ""),
        ],
    )
    def test_spellings_collapse_to_one_canonical_slug(
        self, raw: str, expected: str
    ) -> None:
        """Different spellings of one subject count as one subject."""
        assert normalize_subject(raw) == expected


class TestGiveOpinion:
    """Test suite for the give_opinion tool."""

    @pytest.mark.asyncio
    async def test_project_level_opinion_is_stored_as_an_assessment(self) -> None:
        """A project-level opinion writes a Decision with kind assessment."""
        state = _make_state()

        with patch(
            "src.marcus_mcp.tools.context._persist_decision_to_history",
            new_callable=AsyncMock,
        ):
            result = await give_opinion(
                agent_id="agent-2",
                subject="Farm Flooded",
                what="the farm is flooded",
                why="the north water source breached",
                impact="food supply",
                state=state,
            )

        assert result["success"] is True
        assert result["decision_id"] == "dec_7_1.0"
        assert result["subject"] == "farm.flooded"
        assert result["kind"] == "assessment"
        state.context.log_decision.assert_awaited_once_with(
            agent_id="agent-2",
            task_id=None,
            what="the farm is flooded",
            why="the north water source breached",
            impact="food supply",
            subject="farm.flooded",
            kind="assessment",
        )

    @pytest.mark.asyncio
    async def test_project_level_opinion_comments_on_the_about_card(self) -> None:
        """With no task, the kanban comment lands on the About card."""
        state = _make_state()

        with patch(
            "src.marcus_mcp.tools.context._persist_decision_to_history",
            new_callable=AsyncMock,
        ):
            await give_opinion(
                agent_id="agent-2",
                subject="farm.flooded",
                what="the farm is flooded",
                why="water",
                impact="food",
                state=state,
            )

        state.kanban_client.add_comment.assert_awaited_once()
        target, comment = state.kanban_client.add_comment.await_args.args
        assert target == "about_project"
        assert "OPINION" in comment
        assert "farm.flooded" in comment
        assert "agent-2" in comment

    @pytest.mark.asyncio
    async def test_task_scoped_opinion_comments_on_the_task(self) -> None:
        """With a task_id, the comment lands on that task."""
        state = _make_state()
        state.context.log_decision = AsyncMock(
            return_value=_logged_decision(task_id="task-9")
        )

        with patch(
            "src.marcus_mcp.tools.context._persist_decision_to_history",
            new_callable=AsyncMock,
        ):
            result = await give_opinion(
                agent_id="agent-2",
                subject="farm.flooded",
                what="the farm is flooded",
                why="water",
                impact="food",
                state=state,
                task_id="task-9",
            )

        assert result["success"] is True
        target, _ = state.kanban_client.add_comment.await_args.args
        assert target == "task-9"
        assert state.context.log_decision.await_args.kwargs["task_id"] == "task-9"

    @pytest.mark.asyncio
    async def test_no_about_card_and_no_task_skips_the_comment(self) -> None:
        """A board without an About card still records the opinion."""
        state = _make_state(with_about_card=False)

        with patch(
            "src.marcus_mcp.tools.context._persist_decision_to_history",
            new_callable=AsyncMock,
        ):
            result = await give_opinion(
                agent_id="agent-2",
                subject="farm.flooded",
                what="the farm is flooded",
                why="water",
                impact="food",
                state=state,
            )

        assert result["success"] is True
        state.kanban_client.add_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_subject_is_refused(self) -> None:
        """A subject that normalises to nothing cannot be counted, so refuse."""
        state = _make_state()

        result = await give_opinion(
            agent_id="agent-2",
            subject="!!!",
            what="the farm is flooded",
            why="water",
            impact="food",
            state=state,
        )

        assert result["success"] is False
        assert "subject" in result["error"]
        state.context.log_decision.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_system_disabled_is_an_error(self) -> None:
        """Without the Context system there is nowhere to record it."""
        state = _make_state()
        state.context = None

        result = await give_opinion(
            agent_id="agent-2",
            subject="farm.flooded",
            what="the farm is flooded",
            why="water",
            impact="food",
            state=state,
        )

        assert result["success"] is False
        assert "Context" in result["error"]

    @pytest.mark.asyncio
    async def test_opinion_is_published_and_persisted_to_history(self) -> None:
        """The opinion reaches the event bus and the project history."""
        state = _make_state()
        state.events = Mock()
        state.events.publish_nowait = AsyncMock()

        with patch(
            "src.marcus_mcp.tools.context._persist_decision_to_history",
            new_callable=AsyncMock,
        ) as persist:
            await give_opinion(
                agent_id="agent-2",
                subject="farm.flooded",
                what="the farm is flooded",
                why="water",
                impact="food",
                state=state,
            )

        event_type, source, payload = state.events.publish_nowait.await_args.args
        assert event_type == "opinion_logged"
        assert source == "agent-2"
        assert payload["subject"] == "farm.flooded"
        persist.assert_awaited_once()
        assert persist.await_args.args[0].decision_id == "dec_7_1.0"

    @pytest.mark.asyncio
    async def test_missing_why_and_impact_get_placeholders(self) -> None:
        """why and impact are optional; the record never stores empty strings."""
        state = _make_state()

        with patch(
            "src.marcus_mcp.tools.context._persist_decision_to_history",
            new_callable=AsyncMock,
        ):
            await give_opinion(
                agent_id="agent-2",
                subject="farm.flooded",
                what="the farm is flooded",
                state=state,
            )

        kwargs = state.context.log_decision.await_args.kwargs
        assert kwargs["why"] == "Not specified"
        assert kwargs["impact"] == "Not specified"
