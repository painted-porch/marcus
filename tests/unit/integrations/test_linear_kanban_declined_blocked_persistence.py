"""
Unit tests for issue #835: Linear's update_task forwards BLOCKED and
DECLINED, not just silently dropping status entirely.

Before this fix, update_task never looked at "status" in updates at
all -- DONE and IN_PROGRESS get their own move through
update_task_progress, but BLOCKED and DECLINED had no path onto the
board whatsoever.
"""

from unittest.mock import AsyncMock

import pytest

from src.core.models import TaskStatus
from src.integrations.providers.linear_kanban import LinearKanban

pytestmark = pytest.mark.unit


def _provider(caller: AsyncMock) -> LinearKanban:
    return LinearKanban({"mcp_function_caller": caller, "team_id": "team-1"})


def _issue(state_name: str, state_type: str) -> dict:
    return {
        "id": "issue-1",
        "title": "Check the claim",
        "state": {"name": state_name, "type": state_type},
    }


class TestUpdateTaskForwardsBlockedAndDeclined:
    """update_task must send Linear a status for a terminal-failure one."""

    @pytest.mark.asyncio
    async def test_blocked_is_sent_as_a_status_update(self) -> None:
        caller = AsyncMock(
            return_value={"success": True, "issue": _issue("Blocked", "started")}
        )
        provider = _provider(caller)

        task = await provider.update_task("issue-1", {"status": TaskStatus.BLOCKED})

        caller.assert_awaited_once_with(
            "linear.update_issue", {"issueId": "issue-1", "status": "Blocked"}
        )
        assert task is not None
        assert task.status == TaskStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_declined_is_sent_as_a_status_update(self) -> None:
        caller = AsyncMock(
            return_value={"success": True, "issue": _issue("Declined", "canceled")}
        )
        provider = _provider(caller)

        task = await provider.update_task("issue-1", {"status": TaskStatus.DECLINED})

        caller.assert_awaited_once_with(
            "linear.update_issue", {"issueId": "issue-1", "status": "Declined"}
        )
        assert task is not None
        assert task.status == TaskStatus.DECLINED

    @pytest.mark.asyncio
    async def test_other_fields_still_update_without_touching_status(self) -> None:
        """Pre-existing behavior (name/description/priority, no status
        key at all), confirmed unchanged by this fix."""
        caller = AsyncMock(
            return_value={"success": True, "issue": _issue("Todo", "unstarted")}
        )
        provider = _provider(caller)

        await provider.update_task("issue-1", {"name": "New title"})

        caller.assert_awaited_once_with(
            "linear.update_issue", {"issueId": "issue-1", "title": "New title"}
        )


class TestReadBackChecksTheStateNameFirst:
    """A custom state's type alone cannot distinguish Blocked/Declined
    from an ordinary state sharing its category."""

    @pytest.mark.asyncio
    async def test_a_blocked_state_reads_back_as_blocked(self) -> None:
        caller = AsyncMock(
            return_value={"success": True, "issue": _issue("Blocked", "started")}
        )
        provider = _provider(caller)

        task = await provider.update_task("issue-1", {"name": "noop"})

        assert task is not None
        assert task.status == TaskStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_a_declined_state_reads_back_as_declined(self) -> None:
        caller = AsyncMock(
            return_value={"success": True, "issue": _issue("Declined", "canceled")}
        )
        provider = _provider(caller)

        task = await provider.update_task("issue-1", {"name": "noop"})

        assert task is not None
        assert task.status == TaskStatus.DECLINED

    @pytest.mark.asyncio
    async def test_an_ordinary_canceled_state_still_reads_back_as_done(
        self,
    ) -> None:
        """Pre-existing behavior, confirmed unchanged by this fix: a
        state named something other than "Declined" in the canceled
        category still falls back to the type map."""
        caller = AsyncMock(
            return_value={"success": True, "issue": _issue("Canceled", "canceled")}
        )
        provider = _provider(caller)

        task = await provider.update_task("issue-1", {"name": "noop"})

        assert task is not None
        assert task.status == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_a_state_named_unblocked_does_not_read_back_as_blocked(
        self,
    ) -> None:
        """Codex P2, round 1 on PR #839: the old substring match made
        "blocked" in "unblocked" a false match, misreading an
        unstarted issue as BLOCKED -- and since task selection only
        considers TODO, that work became unclaimable."""
        caller = AsyncMock(
            return_value={"success": True, "issue": _issue("Unblocked", "unstarted")}
        )
        provider = _provider(caller)

        task = await provider.update_task("issue-1", {"name": "noop"})

        assert task is not None
        assert task.status == TaskStatus.TODO
