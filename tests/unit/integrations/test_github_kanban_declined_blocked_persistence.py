"""
Unit tests for issue #835: GitHub's update_task forwards BLOCKED and
DECLINED, not just DONE.

Before this fix, update_task's only status handling was
``state = "closed" if status == DONE else "open"`` -- a BLOCKED or
DECLINED task stayed indistinguishable from a plain open issue. Both
now also get a status label, read back on the other side too.
"""

from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from src.core.models import TaskStatus
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


def _provider() -> tuple[GitHubKanban, AsyncMock, Dict[str, Any]]:
    """A provider whose fake transport tracks the issue's live label set."""
    issue_state: Dict[str, Any] = {"number": 7, "labels": [], "state": "open"}

    async def _call(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool == "github.get_issue":
            return {"issue": dict(issue_state)}
        if tool == "github.update_issue":
            if "state" in args:
                issue_state["state"] = args["state"]
            if "labels" in args:
                issue_state["labels"] = [{"name": n} for n in args["labels"]]
            return {"success": True, "issue": dict(issue_state)}
        return {"success": True}

    caller = AsyncMock(side_effect=_call)
    provider = _ConcreteGitHubKanban(
        {"mcp_function_caller": caller, "owner": "acme", "repo": "widgets"}
    )
    return provider, caller, issue_state


class TestUpdateTaskForwardsBlockedAndDeclined:
    """update_task must move the issue's label for a terminal-failure status."""

    @pytest.mark.asyncio
    async def test_blocked_sets_the_blocked_label_and_stays_open(self) -> None:
        provider, _caller, issue_state = _provider()

        task = await provider.update_task("7", {"status": TaskStatus.BLOCKED})

        assert issue_state["state"] == "open"
        assert issue_state["labels"] == [{"name": "blocked"}]
        assert task is not None
        assert task.status == TaskStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_declined_sets_the_declined_label_and_stays_open(self) -> None:
        provider, _caller, issue_state = _provider()

        task = await provider.update_task("7", {"status": TaskStatus.DECLINED})

        assert issue_state["state"] == "open"
        assert issue_state["labels"] == [{"name": "declined"}]
        assert task is not None
        assert task.status == TaskStatus.DECLINED

    @pytest.mark.asyncio
    async def test_done_still_closes_without_touching_labels(self) -> None:
        """Pre-existing DONE behavior, confirmed unchanged by this fix."""
        provider, caller, issue_state = _provider()

        task = await provider.update_task("7", {"status": TaskStatus.DONE})

        assert issue_state["state"] == "closed"
        assert task is not None
        assert task.status == TaskStatus.DONE
        # Only one update_issue call -- no extra label round-trip for DONE.
        update_calls = [
            call.args[0]
            for call in caller.await_args_list
            if call.args[0] == "github.update_issue"
        ]
        assert len(update_calls) == 1


class TestUpdateTaskRaisesWhenTheLabelMoveFails:
    """Codex P2, round 1 on PR #839.

    The issue-field update can succeed while the follow-up label move
    does not -- update_task must not then hand back a task whose
    status silently never changed, with no exception for a caller
    like report_blocker's lane-death path to roll back on.
    """

    @pytest.mark.asyncio
    async def test_a_rejected_label_update_raises(self) -> None:
        from src.core.error_framework import KanbanIntegrationError

        calls: List[str] = []

        async def _call(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
            calls.append(tool)
            if tool == "github.get_issue":
                return {"issue": {"number": 7, "labels": []}}
            if tool == "github.update_issue":
                # The issue-field update (state="open") succeeds; the
                # follow-up label write (the second update_issue call,
                # made by move_task_to_column's own _replace_status_label)
                # is rejected.
                if calls.count("github.update_issue") == 1:
                    return {"success": True, "issue": {"labels": []}}
                return {"success": False}
            return {"success": True}

        caller = AsyncMock(side_effect=_call)
        provider = _ConcreteGitHubKanban(
            {"mcp_function_caller": caller, "owner": "acme", "repo": "widgets"}
        )

        with pytest.raises(KanbanIntegrationError):
            await provider.update_task("7", {"status": TaskStatus.BLOCKED})
