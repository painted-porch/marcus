"""
Unit tests for GitHubKanban.move_task_to_column on an unmapped column.

Codex P1, round 4 on PR #834: the function's fallback was a bare
``return True`` reached for ANY column it has no mapping for (e.g.
"declined" -- GitHub status persistence for that status is #835, out
of scope here). A caller trusting that return value to mean the board
agrees with its own state was lied to. This covers only the
fix-worthy part: no network call happens, and the return value is
False, for any column this provider's move logic does not recognize.
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

        ok = await provider.move_task_to_column("7", "declined")

        assert ok is False

    @pytest.mark.asyncio
    async def test_an_unmapped_column_makes_no_network_call(self) -> None:
        caller = AsyncMock()
        provider = _ConcreteGitHubKanban(
            {"mcp_function_caller": caller, "owner": "acme", "repo": "widgets"}
        )

        await provider.move_task_to_column("7", "declined")

        caller.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_known_columns_are_unaffected(self) -> None:
        """Confirms this fix didn't widen the refusal to real columns."""
        caller = AsyncMock(return_value={"success": True, "issue": {"labels": []}})
        provider = _ConcreteGitHubKanban(
            {"mcp_function_caller": caller, "owner": "acme", "repo": "widgets"}
        )

        assert await provider.move_task_to_column("7", "done") is True
