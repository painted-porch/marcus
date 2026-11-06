"""
Unit tests for LinearKanban stamping Task.project_id (issue #842, review).

Codex P1, round 1 on PR #842: an issue carries no project id of its
own in the fields _linear_issue_to_task reads -- the client already
scopes its own queries by self.project_id (the configured project),
which is the single source of truth every K2 lookup assumes. Before
this fix, project_id was left None, silently breaking any
project_id-scoped filter (e.g. get_all_board_tasks) over tasks from
this provider.
"""

from unittest.mock import AsyncMock

import pytest

from src.integrations.providers.linear_kanban import LinearKanban

pytestmark = pytest.mark.unit


def _provider(
    project_id: str | None = "linear-project-123",
    registry_project_id: str | None = None,
) -> LinearKanban:
    caller = AsyncMock()
    return LinearKanban(
        {
            "mcp_function_caller": caller,
            "team_id": "team-1",
            "project_id": project_id,
            "registry_project_id": registry_project_id,
        }
    )


class TestLinearIssueToTaskStampsProjectId:
    """_linear_issue_to_task must stamp Task.project_id from the client."""

    def test_stamps_the_configured_project_id(self) -> None:
        provider = _provider(project_id="linear-project-123")
        issue = {
            "id": "issue-1",
            "title": "Check the claim",
            "state": {"name": "Todo", "type": "unstarted"},
        }

        task = provider._linear_issue_to_task(issue)

        assert task.project_id == "linear-project-123"

    def test_none_when_the_client_has_no_configured_project(self) -> None:
        """No regression when the integration is team-scoped only."""
        provider = _provider(project_id=None)
        issue = {
            "id": "issue-1",
            "title": "Check the claim",
            "state": {"name": "Todo", "type": "unstarted"},
        }

        task = provider._linear_issue_to_task(issue)

        assert task.project_id is None


class TestLinearIssueToTaskPrefersTheRegistryProjectId:
    """Issue #854: the real fix for #849/#853's Linear regression.

    When the client is constructed from a registered ProjectConfig,
    ``registry_project_id`` (carrying that config's own ``id``) must
    win over Linear's own ``project_id`` -- the two are different
    strings, unlike SQLite's GH-306 guarantee, and every K2-keyed
    lookup needs the registry one.
    """

    def test_the_registry_id_wins_over_linears_own(self) -> None:
        provider = _provider(
            project_id="linear-project-123", registry_project_id="uuid-registry-9"
        )
        issue = {
            "id": "issue-1",
            "title": "Check the claim",
            "state": {"name": "Todo", "type": "unstarted"},
        }

        task = provider._linear_issue_to_task(issue)

        assert task.project_id == "uuid-registry-9"

    def test_falls_back_without_a_registry_project_id(self) -> None:
        provider = _provider(project_id="linear-project-123")
        issue = {
            "id": "issue-1",
            "title": "Check the claim",
            "state": {"name": "Todo", "type": "unstarted"},
        }

        task = provider._linear_issue_to_task(issue)

        assert task.project_id == "linear-project-123"
