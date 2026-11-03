"""
Unit tests for commit verification in report_task_progress.

When an agent reports a task complete, Marcus must verify that the agent
actually committed code on their worktree branch. This prevents false
completions where an agent reports done without doing any implementation
work (as observed in dashboard-v88: agent_unicorn_3 reported two tasks
complete with zero commits).

Fix: _verify_agent_has_commits checks git log main..marcus/{agent_id}
before accepting completion. If the branch exists but has no commits,
the completion is rejected with a descriptive error.
"""

import subprocess
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, TaskAssignment
from src.marcus_mcp.tools.task import _verify_agent_has_commits, report_task_progress

pytestmark = pytest.mark.unit


def _make_assignment(task_id: str, agent_id: str = "agent_unicorn_3") -> TaskAssignment:
    """Build a minimal TaskAssignment for state.agent_tasks."""
    return TaskAssignment(
        task_id=task_id,
        task_name="Implement Animated Digital Clock with Live Seconds",
        description="Build animated flip clock component",
        instructions="Implement the AnimatedClock component",
        estimated_hours=2.0,
        priority=Priority.HIGH,
        dependencies=[],
        assigned_to=agent_id,
        assigned_at=datetime.now(timezone.utc),
        due_date=None,
    )


def _make_state(
    agent_id: str = "agent_unicorn_3",
    task_id: str = "b27a116b",
    project_root: str = "/tmp/test-repo",  # nosec B108
) -> Mock:
    """Build a Mock state with workspace state returning project_root."""
    state = Mock()
    state.initialize_kanban = AsyncMock()
    state.kanban_client = Mock()
    state.kanban_client.get_all_tasks = AsyncMock(return_value=[])
    state.kanban_client.update_task = AsyncMock()
    state.kanban_client.update_task_progress = AsyncMock()
    state.kanban_client._load_workspace_state = Mock(
        return_value={"project_root": project_root}
    )
    state.agent_tasks = {agent_id: _make_assignment(task_id, agent_id)}
    state.project_tasks = []
    state.lease_manager = None
    state.agent_status = {
        agent_id: Mock(skills=[], current_tasks=[], completed_tasks_count=0)
    }
    state.assignment_persistence = Mock()
    state.assignment_persistence.remove_assignment = AsyncMock()
    state.memory = None
    state.provider = "sqlite"
    state.code_analyzer = None
    state.subtask_manager = None
    return state


class TestVerifyAgentHasCommits:
    """Unit tests for _verify_agent_has_commits helper."""

    def test_returns_true_when_branch_has_commits(self, tmp_path: Any) -> None:
        """Branch with commits ahead of main → verification passes."""
        branch = "marcus/agent_unicorn_3"
        commits_output = "abc1234 Implement AnimatedClock component\n"

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout=".git"),  # git rev-parse
                Mock(returncode=0, stdout=branch),  # git branch --list
                Mock(returncode=0, stdout=commits_output),  # git log main..branch
            ]
            result = _verify_agent_has_commits(
                agent_id="agent_unicorn_3",
                project_root=str(tmp_path),
            )

        assert result is True

    def test_returns_false_when_branch_has_no_commits(self, tmp_path: Any) -> None:
        """Branch exists but no commits ahead of main → verification fails."""
        branch = "marcus/agent_unicorn_3"

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout=".git"),  # git rev-parse
                Mock(returncode=0, stdout=branch),  # branch exists
                Mock(returncode=0, stdout=""),  # no commits on branch
            ]
            result = _verify_agent_has_commits(
                agent_id="agent_unicorn_3",
                project_root=str(tmp_path),
            )

        assert result is False

    def test_returns_none_when_no_worktree_branch(self, tmp_path: Any) -> None:
        """No marcus/agent branch exists → skip check (agent worked on main)."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout=".git"),  # git rev-parse
                Mock(returncode=0, stdout=""),  # branch not found
            ]
            result = _verify_agent_has_commits(
                agent_id="agent_unicorn_3",
                project_root=str(tmp_path),
            )

        assert result is None

    def test_returns_none_when_not_a_git_repo(self, tmp_path: Any) -> None:
        """Not a git repo → skip check gracefully."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stdout="")
            result = _verify_agent_has_commits(
                agent_id="agent_unicorn_3",
                project_root=str(tmp_path),
            )

        assert result is None

    def test_returns_none_on_subprocess_exception(self, tmp_path: Any) -> None:
        """Subprocess error → skip check (non-fatal)."""
        with patch("subprocess.run", side_effect=OSError("git not found")):
            result = _verify_agent_has_commits(
                agent_id="agent_unicorn_3",
                project_root=str(tmp_path),
            )

        assert result is None

    def test_checks_correct_branch_name(self, tmp_path: Any) -> None:
        """Branch name must follow marcus/{agent_id} convention."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout=".git"),  # git rev-parse
                Mock(returncode=0, stdout=""),  # branch not found → early exit
            ]
            _verify_agent_has_commits(
                agent_id="agent_unicorn_3",
                project_root=str(tmp_path),
            )

        # Second call is git branch --list marcus/agent_unicorn_3
        second_call = mock_run.call_args_list[1]
        cmd = second_call[0][0]
        assert "marcus/agent_unicorn_3" in cmd

    def test_uses_main_double_dot_syntax(self, tmp_path: Any) -> None:
        """Commit check uses main..branch to count only new commits."""
        branch = "marcus/agent_unicorn_3"

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout=".git"),  # git rev-parse
                Mock(returncode=0, stdout=branch),  # branch exists
                Mock(returncode=0, stdout="abc1234 some work"),  # git log
            ]
            _verify_agent_has_commits(
                agent_id="agent_unicorn_3",
                project_root=str(tmp_path),
            )

        third_call = mock_run.call_args_list[2]
        cmd = third_call[0][0]
        assert "main..marcus/agent_unicorn_3" in cmd


class TestReportProgressRejectsNoCommits:
    """
    report_task_progress must reject completions from agents
    whose worktree branch has zero commits ahead of main.
    """

    @pytest.mark.asyncio
    async def test_rejects_completion_with_no_commits(self) -> None:
        """
        Agent reports task done; branch exists but has no commits.
        Marcus must reject and instruct agent to commit first.
        """
        state = _make_state()

        with patch(
            "src.marcus_mcp.tools.task._verify_agent_has_commits",
            return_value=False,  # branch exists but zero commits
        ):
            result = await report_task_progress(
                agent_id="agent_unicorn_3",
                task_id="b27a116b",
                status="completed",
                progress=100,
                message="AnimatedClock implemented",
                state=state,
            )

        assert result["success"] is False
        assert result["status"] == "no_commits"
        assert "commit" in result["message"].lower()
        state.kanban_client.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_completion_with_commits(self) -> None:
        """Agent has commits on branch → completion accepted normally."""
        state = _make_state()
        state.kanban_client.update_task = AsyncMock(return_value=None)

        with (
            patch(
                "src.marcus_mcp.tools.task._verify_agent_has_commits",
                return_value=True,  # branch has commits
            ),
            patch(
                "src.marcus_mcp.tools.task._merge_agent_branch_to_main",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
            patch(
                "src.marcus_mcp.tools.task._validate_task_completion",
                new_callable=AsyncMock,
                return_value=Mock(passed=True, issues=[]),
            ),
        ):
            result = await report_task_progress(
                agent_id="agent_unicorn_3",
                task_id="b27a116b",
                status="completed",
                progress=100,
                message="AnimatedClock implemented",
                state=state,
            )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_skips_commit_check_when_no_worktree(self) -> None:
        """
        No worktree branch → skip commit check (agent worked on main).
        Completion is not rejected for this reason.
        """
        state = _make_state()
        state.kanban_client.update_task = AsyncMock(return_value=None)

        with (
            patch(
                "src.marcus_mcp.tools.task._verify_agent_has_commits",
                return_value=None,  # no worktree branch → skip
            ),
            patch(
                "src.marcus_mcp.tools.task._merge_agent_branch_to_main",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
            patch(
                "src.marcus_mcp.tools.task._validate_task_completion",
                new_callable=AsyncMock,
                return_value=Mock(passed=True, issues=[]),
            ),
        ):
            result = await report_task_progress(
                agent_id="agent_unicorn_3",
                task_id="b27a116b",
                status="completed",
                progress=100,
                message="done",
                state=state,
            )

        # Should NOT reject for missing commits when no worktree exists
        assert result.get("status") != "no_commits"

    @pytest.mark.asyncio
    async def test_commit_check_not_applied_to_in_progress_updates(self) -> None:
        """Commit check only fires on completion, not in_progress updates."""
        state = _make_state()
        state.kanban_client.update_task = AsyncMock(return_value=None)
        state.kanban_client.update_task_progress = AsyncMock(return_value=None)

        with patch(
            "src.marcus_mcp.tools.task._verify_agent_has_commits"
        ) as mock_verify:
            await report_task_progress(
                agent_id="agent_unicorn_3",
                task_id="b27a116b",
                status="in_progress",
                progress=50,
                message="halfway",
                state=state,
            )

        mock_verify.assert_not_called()


@pytest.mark.unit
class TestReconciliationCardSkipsTheGate:
    """A reconciliation card's deliverable is a decision, not a commit."""

    @pytest.mark.asyncio
    async def test_a_reconciliation_card_completes_without_commits(self) -> None:
        """Keeping the work already on main changes no files.

        That outcome is explicitly permitted by the card, so the commit
        gate must not stand in its way (Codex P2 on PR #794).
        """
        from src.core.models import Task, TaskStatus
        from src.marcus_mcp.tools.task import report_task_progress

        agent_id = "reconciler"
        task_id = "recon_card"
        state = _make_state(agent_id=agent_id, task_id=task_id)
        now = datetime.now(timezone.utc)
        card = Task(
            id=task_id,
            name="Reconcile two completions of: something",
            description="<!-- marcus:reconciles task contested -->",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
            assigned_to=agent_id,
            created_at=now,
            updated_at=now,
            due_date=None,
            estimated_hours=1.0,
            labels=["reconciliation"],
            dependencies=[],
        )
        state.project_tasks = [card]
        state.kanban_client.get_all_tasks = AsyncMock(return_value=[card])

        with patch("src.marcus_mcp.tools.task._verify_agent_has_commits") as gate:
            gate.return_value = False
            result = await report_task_progress(
                agent_id=agent_id,
                task_id=task_id,
                status="completed",
                progress=100,
                message="kept what was on main; decision logged",
                state=state,
            )

        assert result.get("status") != "no_commits"
        gate.assert_not_called()
