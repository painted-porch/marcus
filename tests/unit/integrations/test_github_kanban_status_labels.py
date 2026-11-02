"""
Unit tests for GitHubKanban.move_task_to_column label handling.

Codex P2 on PR #770: the blocked branch sent ``update_issue`` with
``labels: ["blocked"]``, which replaces the issue's whole label set and so
deleted priority, domain, and user-managed labels every time a lane died.
Moving between status columns must replace only the status label.
"""

from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock

import pytest

from src.integrations.providers.github_kanban import GitHubKanban


class _ConcreteGitHubKanban(GitHubKanban):
    """GitHubKanban with the attachment and listing methods it never implemented.

    The production class is abstract (it lacks ``get_all_tasks`` and the
    three attachment methods), so it cannot be instantiated directly; this
    stub only adds no-op versions of those so ``move_task_to_column`` can be
    exercised.
    """

    async def get_all_tasks(self) -> List[Any]:
        return []

    async def upload_attachment(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {}

    async def get_attachments(self, task_id: str) -> Dict[str, Any]:
        return {}

    async def download_attachment(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {}


def _issue_with_labels(labels: List[str]) -> Dict[str, Any]:
    return {"issue": {"number": 7, "labels": [{"name": name} for name in labels]}}


def _provider(existing_labels: List[str]) -> Tuple[GitHubKanban, AsyncMock]:
    """Build a provider whose MCP caller serves one issue and records writes."""

    async def _call(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool == "github.get_issue":
            return _issue_with_labels(existing_labels)
        return {"success": True}

    caller = AsyncMock(side_effect=_call)
    provider = _ConcreteGitHubKanban(
        {"mcp_function_caller": caller, "owner": "acme", "repo": "widgets"}
    )
    return provider, caller


def _update_calls(caller: AsyncMock) -> List[Dict[str, Any]]:
    return [
        call.args[1]
        for call in caller.await_args_list
        if call.args[0] == "github.update_issue"
    ]


@pytest.mark.unit
class TestMoveTaskToColumnPreservesLabels:
    """Status moves replace the status label and keep everything else."""

    @pytest.mark.asyncio
    async def test_blocked_keeps_unrelated_labels(self) -> None:
        """Priority, domain and user labels survive a move to blocked."""
        provider, caller = _provider(["priority/high", "domain/api", "in-progress"])

        ok = await provider.move_task_to_column("7", "blocked")

        assert ok is True
        (update,) = _update_calls(caller)
        assert update["issue_number"] == 7
        assert sorted(update["labels"]) == ["blocked", "domain/api", "priority/high"]

    @pytest.mark.asyncio
    async def test_in_progress_clears_blocked_labels(self) -> None:
        """Moving back to in progress drops blocked and blocked/<severity>."""
        provider, caller = _provider(["blocked", "blocked/high", "priority/low"])

        await provider.move_task_to_column("7", "in progress")

        (update,) = _update_calls(caller)
        assert sorted(update["labels"]) == ["in-progress", "priority/low"]

    @pytest.mark.asyncio
    async def test_blocked_is_idempotent(self) -> None:
        """A second move to blocked does not duplicate the label."""
        provider, caller = _provider(["blocked", "priority/high"])

        await provider.move_task_to_column("7", "blocked")

        (update,) = _update_calls(caller)
        assert sorted(update["labels"]) == ["blocked", "priority/high"]

    @pytest.mark.asyncio
    async def test_unreadable_issue_still_sets_status_label(self) -> None:
        """If the issue cannot be read, the status label is still applied alone."""

        async def _call(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
            if tool == "github.get_issue":
                raise RuntimeError("boom")
            return {"success": True}

        caller = AsyncMock(side_effect=_call)
        provider = _ConcreteGitHubKanban(
            {"mcp_function_caller": caller, "owner": "acme", "repo": "widgets"}
        )

        ok = await provider.move_task_to_column("7", "blocked")

        assert ok is True
        (update,) = _update_calls(caller)
        assert update["labels"] == ["blocked"]
