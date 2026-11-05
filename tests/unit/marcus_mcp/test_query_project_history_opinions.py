"""
Unit tests for the ``opinions`` query type of ``query_project_history``
(deliberation layer, B2).

The read agents and the planner act on: one entry per subject, with the
distinct-agent count, served through the same tool and pagination as
every other history query.
"""

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.marcus_mcp.tools.history import query_project_history

pytestmark = pytest.mark.unit


def _entries(n: int) -> list[dict[str, Any]]:
    """Build n subject entries in count order."""
    return [
        {
            "subject": f"subject.{i}",
            "kind": "assessment",
            "count": n - i,
            "opinion_count": n - i,
            "agents": [f"agent-{j}" for j in range(n - i)],
            "first_seen": "2026-11-03T12:00:00+00:00",
            "last_seen": "2026-11-03T12:05:00+00:00",
            "task_ids": [],
            "opinions": [],
        }
        for i in range(n)
    ]


class TestQueryProjectHistoryOpinions:
    """Test suite for the opinions branch of the history tool."""

    @pytest.mark.asyncio
    async def test_returns_subject_entries_with_pagination(self) -> None:
        """The branch forwards the filters and pages over subjects."""
        query_api = Mock()
        query_api.summarize_subjects = AsyncMock(return_value=_entries(3))

        with (
            patch("src.marcus_mcp.tools.history.ProjectHistoryAggregator"),
            patch(
                "src.marcus_mcp.tools.history.ProjectHistoryQuery",
                return_value=query_api,
            ),
        ):
            result = await query_project_history(
                "proj-1",
                "opinions",
                Mock(),
                subject="subject.1",
                since="2026-11-03T00:00:00+00:00",
                limit=2,
                offset=0,
            )

        assert result["success"] is True
        assert [e["subject"] for e in result["data"]] == ["subject.0", "subject.1"]
        assert result["count"] == 2
        assert result["total_subjects"] == 3
        assert result["pagination"] == {"limit": 2, "offset": 0}
        query_api.summarize_subjects.assert_awaited_once_with(
            "proj-1",
            subject="subject.1",
            kind="assessment",
            since="2026-11-03T00:00:00+00:00",
            window_minutes=None,
        )

    @pytest.mark.asyncio
    async def test_offset_skips_subjects(self) -> None:
        """Offset applies to subjects, the unit the caller pages over."""
        query_api = Mock()
        query_api.summarize_subjects = AsyncMock(return_value=_entries(3))

        with (
            patch("src.marcus_mcp.tools.history.ProjectHistoryAggregator"),
            patch(
                "src.marcus_mcp.tools.history.ProjectHistoryQuery",
                return_value=query_api,
            ),
        ):
            result = await query_project_history(
                "proj-1", "opinions", Mock(), limit=10, offset=2
            )

        assert [e["subject"] for e in result["data"]] == ["subject.2"]

    @pytest.mark.asyncio
    async def test_kind_filter_is_forwarded(self) -> None:
        """A caller may group architectural decisions instead."""
        query_api = Mock()
        query_api.summarize_subjects = AsyncMock(return_value=[])

        with (
            patch("src.marcus_mcp.tools.history.ProjectHistoryAggregator"),
            patch(
                "src.marcus_mcp.tools.history.ProjectHistoryQuery",
                return_value=query_api,
            ),
        ):
            result = await query_project_history(
                "proj-1", "opinions", Mock(), kind="decision"
            )

        assert result["success"] is True
        assert result["data"] == []
        assert query_api.summarize_subjects.await_args.kwargs["kind"] == "decision"
