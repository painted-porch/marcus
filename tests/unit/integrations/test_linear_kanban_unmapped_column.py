"""
Unit tests for LinearKanban.move_task_to_column on an unmapped column.

Codex P1, round 4 on PR #834: for a column with no entry in
``state_map`` (e.g. "declined" -- Linear status persistence for that
status is #835, out of scope here), the function fell back to sending
Linear the literal column name as a status, which Linear does not
recognize either. Whether that particular call happens to fail is an
accident of what the mock (or Linear) does with an unrecognized
status string; this fix makes the refusal explicit and stops the call
from being made at all.
"""

from unittest.mock import AsyncMock

import pytest

from src.integrations.providers.linear_kanban import LinearKanban

pytestmark = pytest.mark.unit


def _provider(caller: AsyncMock) -> LinearKanban:
    return LinearKanban({"mcp_function_caller": caller, "team_id": "team-1"})


class TestMoveTaskToColumnRejectsUnmappedColumns:
    """A column this provider cannot represent returns False, not a guess."""

    @pytest.mark.asyncio
    async def test_an_unmapped_column_returns_false(self) -> None:
        caller = AsyncMock(return_value={"success": True})
        provider = _provider(caller)

        ok = await provider.move_task_to_column("issue-1", "declined")

        assert ok is False

    @pytest.mark.asyncio
    async def test_an_unmapped_column_makes_no_network_call(self) -> None:
        caller = AsyncMock(return_value={"success": True})
        provider = _provider(caller)

        await provider.move_task_to_column("issue-1", "declined")

        caller.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_known_column_is_unaffected(self) -> None:
        """Confirms this fix didn't widen the refusal to real columns."""
        caller = AsyncMock(return_value={"success": True})
        provider = _provider(caller)

        ok = await provider.move_task_to_column("issue-1", "blocked")

        assert ok is True
        caller.assert_awaited_once_with(
            "linear.update_issue", {"issueId": "issue-1", "status": "Blocked"}
        )
