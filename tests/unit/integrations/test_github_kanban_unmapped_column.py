"""
Unit tests for GitHubKanban.move_task_to_column on an unmapped column.

Codex P1, round 4 on PR #834: the function's fallback was a bare
``return True`` reached for ANY column it has no mapping for. A
caller trusting that return value to mean the board agrees with its
own state was lied to. This covers the fix-worthy part: no network
call happens, and the return value is False, for any column this
provider's move logic does not recognize.

"declined" used to be exactly that unmapped example; #835 mapped it
(a label, symmetric with "blocked"), so this file now also covers
that it round-trips like every other known column.
"""

from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from src.integrations.providers.github_kanban import GitHubKanban

pytestmark = pytest.mark.unit


class _ConcreteGitHubKanban(GitHubKanban):
    """See test_github_kanban_status_labels.py for why this stub exists."""

    async def get_all_tasks(self) -> List[Any]:
        return []

    async def upload_attachment(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {}

    async def get_attachments(self, task_id: str) -> Dict[str, Any]:
        return {}

    async def download_attachment(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {}


class TestMoveTaskToColumnRejectsUnmappedColumns:
    """A column this provider cannot represent returns False, not True."""

    @pytest.mark.asyncio
    async def test_an_unmapped_column_returns_false(self) -> None:
        caller = AsyncMock()
        provider = _ConcreteGitHubKanban(
            {"mcp_function_caller": caller, "owner": "acme", "repo": "widgets"}
        )

        ok = await provider.move_task_to_column("7", "frobnicate")

        assert ok is False

    @pytest.mark.asyncio
    async def test_an_unmapped_column_makes_no_network_call(self) -> None:
        caller = AsyncMock()
        provider = _ConcreteGitHubKanban(
            {"mcp_function_caller": caller, "owner": "acme", "repo": "widgets"}
        )

        await provider.move_task_to_column("7", "frobnicate")

        caller.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_known_columns_are_unaffected(self) -> None:
        """Confirms this fix didn't widen the refusal to real columns."""
        caller = AsyncMock(return_value={"success": True, "issue": {"labels": []}})
        provider = _ConcreteGitHubKanban(
            {"mcp_function_caller": caller, "owner": "acme", "repo": "widgets"}
        )

        assert await provider.move_task_to_column("7", "done") is True


class TestMoveTaskToColumnDeclined:
    """ "declined" (#835) is mapped: a label, symmetric with "blocked"."""

    @pytest.mark.asyncio
    async def test_declined_sets_a_declined_label(self) -> None:
        async def _call(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
            if tool == "github.get_issue":
                return {"issue": {"number": 7, "labels": []}}
            return {"success": True, "issue": {"labels": [{"name": "declined"}]}}

        caller = AsyncMock(side_effect=_call)
        provider = _ConcreteGitHubKanban(
            {"mcp_function_caller": caller, "owner": "acme", "repo": "widgets"}
        )

        ok = await provider.move_task_to_column("7", "declined")

        assert ok is True
        update_calls = [
            call.args[1]
            for call in caller.await_args_list
            if call.args[0] == "github.update_issue"
        ]
        (update,) = update_calls
        assert update["labels"] == ["declined"]
