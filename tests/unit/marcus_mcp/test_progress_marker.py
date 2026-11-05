"""
Unit tests for the marker an agent leaves while reporting progress.

In Grasse's stigmergy a termite does not decide to inform anyone: the
pellet it deposits IS the stimulus the next termite responds to. The
signal is a byproduct of the work and costs nothing extra. ``give_opinion``
as a separate call is the opposite: a civic act with its own cost, and in
30 agent-minutes of real runs agents made zero of them while writing the
same observations into their progress messages, where nothing could count
them.

So ``report_task_progress`` takes an optional ``subject``, and the
observation it already carries becomes an opinion on the board. One call,
no extra turn.
"""

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytestmark = pytest.mark.unit


async def _report(**kwargs: Any) -> Dict[str, Any]:
    """Call report_task_progress with the marker arguments under test."""
    from src.marcus_mcp.tools.task import report_task_progress

    state = Mock()
    base: Dict[str, Any] = {
        "agent_id": "agent-1",
        "task_id": "task-9",
        "status": "in_progress",
        "progress": 50,
        "message": "Trees near camp are already harvested; nearest logs 40 blocks east.",
        "state": state,
    }
    base.update(kwargs)
    return await report_task_progress(**base)


class TestProgressMarker:
    """Test suite for recording an opinion from a progress report."""

    @pytest.mark.asyncio
    async def test_a_subject_turns_the_report_into_a_counted_marker(self) -> None:
        """The observation already in the message becomes the opinion."""
        opinion = AsyncMock(return_value={"success": True, "subject": "wood.harvested"})
        with (
            patch(
                "src.marcus_mcp.tools.task._record_progress",
                new=AsyncMock(return_value={"success": True}),
            ),
            patch("src.marcus_mcp.tools.task.give_opinion", new=opinion),
        ):
            result = await _report(subject="wood.harvested")
        assert result["success"] is True
        opinion.assert_awaited_once()
        kwargs = opinion.await_args.kwargs
        assert kwargs["agent_id"] == "agent-1"
        assert kwargs["subject"] == "wood.harvested"
        assert kwargs["task_id"] == "task-9"
        assert "already harvested" in kwargs["what"], "the message is the observation"
        assert result["marker"]["subject"] == "wood.harvested"

    @pytest.mark.asyncio
    async def test_an_explicit_observation_wins_over_the_progress_message(self) -> None:
        """An agent can say the crew-relevant part separately."""
        opinion = AsyncMock(return_value={"success": True, "subject": "wood.harvested"})
        with (
            patch(
                "src.marcus_mcp.tools.task._record_progress",
                new=AsyncMock(return_value={"success": True}),
            ),
            patch("src.marcus_mcp.tools.task.give_opinion", new=opinion),
        ):
            await _report(
                subject="wood.harvested", observation="no logs within 40 blocks of camp"
            )
        assert opinion.await_args.kwargs["what"] == "no logs within 40 blocks of camp"

    @pytest.mark.asyncio
    async def test_no_subject_means_no_marker_and_no_extra_work(self) -> None:
        """Reporting progress without a subject behaves exactly as before."""
        opinion = AsyncMock()
        with (
            patch(
                "src.marcus_mcp.tools.task._record_progress",
                new=AsyncMock(return_value={"success": True}),
            ),
            patch("src.marcus_mcp.tools.task.give_opinion", new=opinion),
        ):
            result = await _report()
        opinion.assert_not_awaited()
        assert "marker" not in result

    @pytest.mark.asyncio
    async def test_a_failed_marker_never_fails_the_progress_report(self) -> None:
        """The work is the point; the marker rides along with it."""
        opinion = AsyncMock(return_value={"success": False, "error": "no board"})
        with (
            patch(
                "src.marcus_mcp.tools.task._record_progress",
                new=AsyncMock(return_value={"success": True}),
            ),
            patch("src.marcus_mcp.tools.task.give_opinion", new=opinion),
        ):
            result = await _report(subject="wood.harvested")
        assert result["success"] is True
        assert result["marker"]["success"] is False

    @pytest.mark.asyncio
    async def test_a_marker_that_raises_never_fails_the_progress_report(self) -> None:
        """A broken marker path is logged, not propagated."""
        opinion = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch(
                "src.marcus_mcp.tools.task._record_progress",
                new=AsyncMock(return_value={"success": True}),
            ),
            patch("src.marcus_mcp.tools.task.give_opinion", new=opinion),
        ):
            result = await _report(subject="wood.harvested")
        assert result["success"] is True
        assert result["marker"]["success"] is False

    @pytest.mark.asyncio
    async def test_a_rejected_report_leaves_no_marker(self) -> None:
        """Nothing is recorded for work the board did not accept."""
        opinion = AsyncMock()
        with (
            patch(
                "src.marcus_mcp.tools.task._record_progress",
                new=AsyncMock(
                    return_value={"success": False, "error": "not_task_holder"}
                ),
            ),
            patch("src.marcus_mcp.tools.task.give_opinion", new=opinion),
        ):
            result = await _report(subject="wood.harvested")
        assert result["success"] is False
        opinion.assert_not_awaited()
