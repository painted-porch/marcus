"""Unit tests for merge-conflict recovery (#651 follow-up, Option A).

Background
----------
PR #653 marked tasks BLOCKED on merge failure (closed the
kanban/filesystem divergence) but didn't recover them.  verify-snake-8
(test71, 2026-05-25) hit a single content conflict that left task
``f76f890d`` BLOCKED; the runner then spawned 49 ephemeral agents
chasing the unclaimed downstream task before the 20-minute stall
watchdog gave up.  Roughly $25-50 of wasted spawn cost from one
recoverable conflict.

This module implements **auto-recovery via rebase**: when a task is
BLOCKED with ``source_context.merge_conflict``, Marcus reads the
agent_id, locates the worktree (still on disk under
``<experiment_dir>/worktrees/<agent_id>``), runs ``git rebase main``
in the worktree, and on success fast-forwards main to the rebased
branch and transitions the task to DONE.

Most "conflicts" in DAG-based parallel work are stale-base false
conflicts: another agent merged main while this agent's worktree
was on the old base; the actual code changes don't overlap.
Rebase resolves these mechanically.  Real content conflicts
(where two agents' changes overlap on the same lines) abort the
rebase; the task stays BLOCKED for human intervention.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import Priority, Task, TaskStatus

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_blocked_task(
    *,
    task_id: str = "task_recovered",
    agent_id: str = "agent_X",
    branch: str | None = None,
    name: str = "Implement feature",
) -> Task:
    """Build a Task in BLOCKED state with merge_conflict source_context.

    Mirrors what ``_apply_merge_failure_to_update_data`` stamps onto
    a task when its merge fails.
    """
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description="...",
        status=TaskStatus.BLOCKED,
        priority=Priority.HIGH,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        source_context={
            "merge_conflict": {
                "agent_id": agent_id,
                "branch": branch or f"marcus/{agent_id}",
                "conflict_stderr": "merge failed: ...",
                "blocked_at": now.isoformat(),
            }
        },
    )


def _setup_recoverable_repo(tmp_path: Path) -> Path:
    """Build a tmp git project where rebase resolves cleanly.

    Layout::

        tmp_path/
          implementation/   (main repo on 'main')
            base.txt        (committed on main)
            mainonly.txt    (committed on main AFTER agent branched)
          worktrees/
            agent_X/        (worktree on 'marcus/agent_X')
              base.txt
              agent_X.txt   (committed on the agent's branch)

    The agent's branch was created from an OLDER main (before
    ``mainonly.txt`` was added).  Rebase should cleanly replay the
    agent's commit on top of current main.
    """
    impl = tmp_path / "implementation"
    impl.mkdir()
    wtree_parent = tmp_path / "worktrees"
    wtree_parent.mkdir()

    def git(cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    # Initialize repo, identity, base commit on main.
    git(impl, "init", "--initial-branch=main")
    git(impl, "config", "user.email", "test@marcus.test")
    git(impl, "config", "user.name", "Test")
    (impl / "base.txt").write_text("base\n")
    git(impl, "add", ".")
    git(impl, "commit", "-m", "scaffold")

    # Create agent branch from this point — BEFORE the next main commit.
    git(impl, "branch", "marcus/agent_X")

    # Advance main with a new file (different file from agent's work).
    (impl / "mainonly.txt").write_text("from main\n")
    git(impl, "add", "mainonly.txt")
    git(impl, "commit", "-m", "main advances after agent branched")

    # Create the agent's worktree from their branch.
    wt = wtree_parent / "agent_X"
    git(impl, "worktree", "add", str(wt), "marcus/agent_X")

    # Agent commits their own file on the branch (different from mainonly.txt).
    (wt / "agent_X.txt").write_text("agent X work\n")
    git(wt, "add", "agent_X.txt")
    git(wt, "commit", "-m", "feat(agent_X): add file")

    return impl


def _dirty_worktree_index(impl: Path, agent_id: str = "agent_X") -> None:
    """Leave the agent's worktree with a dirty index and working tree.

    Reproduces the state that wedges recovery in bug #700: a failed
    integration merge (or gate-test side effects) leaves *uncommitted*
    content in the worktree.  ``git rebase`` refuses to start on a
    non-clean index/working tree, so the stale-base rebase that would
    otherwise recover the task fails immediately.

    Parameters
    ----------
    impl : Path
        The main repository directory (``.../implementation``).
    agent_id : str
        Agent whose worktree (``.../worktrees/<agent_id>``) to dirty.

    Notes
    -----
    Creates BOTH failure flavours seen in the issue logs:

    * a **staged** change (new file ``git add``-ed) — triggers
      ``error: cannot rebase: Your index contains uncommitted changes``
    * an **unstaged** change (edit to a tracked file) — triggers
      ``error: cannot rebase: You have unstaged changes``

    The agent's real work stays safely committed on its branch, so a
    correct recovery discards this cruft and still replays the commit.
    """
    wt = impl.parent / "worktrees" / agent_id

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=wt,
            check=True,
            capture_output=True,
            text=True,
        )

    # Unstaged change to a tracked file.
    (wt / "base.txt").write_text("base\nlocally scribbled by a gate test\n")
    # Staged (index) change — new file added but never committed.
    (wt / "leftover.txt").write_text("half-merged cruft\n")
    git("add", "leftover.txt")


def _setup_unrecoverable_repo(tmp_path: Path) -> Path:
    """Build a tmp git project where rebase has a real content conflict.

    Both main and the agent's branch modify the SAME LINE of
    ``conflict.txt``; rebase fails and must abort.
    """
    impl = tmp_path / "implementation"
    impl.mkdir()
    wtree_parent = tmp_path / "worktrees"
    wtree_parent.mkdir()

    def git(cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    git(impl, "init", "--initial-branch=main")
    git(impl, "config", "user.email", "test@marcus.test")
    git(impl, "config", "user.name", "Test")
    (impl / "conflict.txt").write_text("original line\n")
    git(impl, "add", ".")
    git(impl, "commit", "-m", "scaffold")

    git(impl, "branch", "marcus/agent_X")

    # Main modifies the contested file.
    (impl / "conflict.txt").write_text("main's version\n")
    git(impl, "add", "conflict.txt")
    git(impl, "commit", "-m", "main modifies conflict.txt")

    wt = wtree_parent / "agent_X"
    git(impl, "worktree", "add", str(wt), "marcus/agent_X")

    # Agent also modifies the contested file (same line) on their branch.
    (wt / "conflict.txt").write_text("agent's version\n")
    git(wt, "add", "conflict.txt")
    git(wt, "commit", "-m", "feat(agent_X): modify conflict.txt")

    return impl


def _make_state(impl: Path, *, blocked_task: Task) -> MagicMock:
    """Marcus state mock with the inputs the recovery helper reads."""
    state = MagicMock()
    state.kanban_client = MagicMock()
    state.kanban_client._load_workspace_state = MagicMock(
        return_value={"project_root": str(impl)}
    )
    state.kanban_client.update_task = AsyncMock(return_value={"success": True})
    state.project_tasks = [blocked_task]
    return state


# ---------------------------------------------------------------------------
# _attempt_merge_recovery — happy path
# ---------------------------------------------------------------------------


class TestAttemptMergeRecoveryHappyPath:
    """Stale-base false-conflict recoveries (the common case)."""

    @pytest.mark.asyncio
    async def test_rebase_succeeds_merges_branch_returns_success(
        self, tmp_path
    ) -> None:
        """When the rebase is clean, the branch lands in main and the task
        recovers.

        Verifies the end-to-end path: rebase succeeds, fast-forward
        merge lands the agent's commit in main, ``kanban_client.update_task``
        is called with status=DONE, and the helper returns a success dict.
        """
        from src.marcus_mcp.tools.task import _attempt_merge_recovery

        impl = _setup_recoverable_repo(tmp_path)
        task = _make_blocked_task()
        state = _make_state(impl, blocked_task=task)

        result = await _attempt_merge_recovery(task=task, state=state)

        # Helper reports success
        assert result is not None
        assert result["success"] is True
        assert result["task_id"] == task.id

        # The agent's file is now in main (rebase replayed cleanly,
        # fast-forward merge succeeded).
        assert (impl / "agent_X.txt").exists()
        assert (impl / "agent_X.txt").read_text() == "agent X work\n"
        # And main's own file is still there.
        assert (impl / "mainonly.txt").exists()

        # The task was transitioned to DONE on the kanban.
        state.kanban_client.update_task.assert_awaited_once()
        kanban_call = state.kanban_client.update_task.call_args
        update_data = (
            kanban_call.args[1]
            if len(kanban_call.args) > 1
            else kanban_call.kwargs["update_data"]
        )
        assert update_data["status"] == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_recovery_clears_merge_conflict_from_source_context(
        self, tmp_path
    ) -> None:
        """On success the ``source_context.merge_conflict`` record is removed.

        Otherwise the task would look like it still has a merge
        conflict even after recovery — downstream tooling (recovery
        sweepers, dashboards) would misclassify it.
        """
        from src.marcus_mcp.tools.task import _attempt_merge_recovery

        impl = _setup_recoverable_repo(tmp_path)
        task = _make_blocked_task()
        state = _make_state(impl, blocked_task=task)

        await _attempt_merge_recovery(task=task, state=state)

        # Inspect the update_data passed to update_task.
        kanban_call = state.kanban_client.update_task.call_args
        update_data = (
            kanban_call.args[1]
            if len(kanban_call.args) > 1
            else kanban_call.kwargs["update_data"]
        )
        # source_context still exists (other fields may have been set)
        # but the merge_conflict entry must be gone.
        ctx = update_data.get("source_context", {})
        assert "merge_conflict" not in ctx


# ---------------------------------------------------------------------------
# _attempt_merge_recovery — dirty worktree index (bug #700)
# ---------------------------------------------------------------------------


class TestAttemptMergeRecoveryDirtyWorktreeIndex:
    """A dirty worktree index must not permanently wedge recovery (#700).

    A failed integration merge or gate-test side effects can leave the
    agent's worktree with uncommitted changes.  Before the fix, the
    recovery path ran ``git rebase main`` straight into that dirty state
    and git aborted with "Your index contains uncommitted changes" /
    "You have unstaged changes", so the task stayed BLOCKED forever even
    though it was a mechanically-recoverable stale-base conflict.
    """

    @pytest.mark.asyncio
    async def test_dirty_worktree_is_cleaned_and_recovery_succeeds(
        self, tmp_path
    ) -> None:
        """Recovery cleans uncommitted cruft, then rebases and lands the work.

        This is a stale-base false conflict (recoverable), but the
        worktree carries both a staged and an unstaged change on entry.
        The fix discards that uncommitted state before rebasing; the
        agent's committed work is untouched and replays onto main.
        """
        from src.marcus_mcp.tools.task import _attempt_merge_recovery

        impl = _setup_recoverable_repo(tmp_path)
        _dirty_worktree_index(impl)
        task = _make_blocked_task()
        state = _make_state(impl, blocked_task=task)

        result = await _attempt_merge_recovery(task=task, state=state)

        # Recovery succeeded despite the dirty index on entry.
        assert result is not None
        assert result["success"] is True

        # The agent's committed work landed in main (rebase replayed it).
        assert (impl / "agent_X.txt").exists()
        assert (impl / "agent_X.txt").read_text() == "agent X work\n"
        # Main's own file survives.
        assert (impl / "mainonly.txt").exists()

        # The uncommitted cruft was discarded, not carried into main.
        assert not (impl / "leftover.txt").exists()

        # Task transitioned to DONE.
        state.kanban_client.update_task.assert_awaited_once()
        kanban_call = state.kanban_client.update_task.call_args
        update_data = (
            kanban_call.args[1]
            if len(kanban_call.args) > 1
            else kanban_call.kwargs["update_data"]
        )
        assert update_data["status"] == TaskStatus.DONE


# ---------------------------------------------------------------------------
# _attempt_merge_recovery — failure path
# ---------------------------------------------------------------------------


class TestAttemptMergeRecoveryRealConflict:
    """When rebase has real content conflicts, leave the task BLOCKED."""

    @pytest.mark.asyncio
    async def test_real_content_conflict_aborts_rebase_returns_none(
        self, tmp_path
    ) -> None:
        """The rebase aborts on overlapping line changes; the task stays
        BLOCKED so a human (or future MCP recovery tool) can resolve.

        Critical: the worktree must NOT be left in a half-rebase state.
        ``git rebase --abort`` cleans it up regardless of how this
        function exits.
        """
        from src.marcus_mcp.tools.task import _attempt_merge_recovery

        impl = _setup_unrecoverable_repo(tmp_path)
        task = _make_blocked_task()
        state = _make_state(impl, blocked_task=task)

        result = await _attempt_merge_recovery(task=task, state=state)

        # Helper reports recovery failed (None or success=False).
        assert result is None or result.get("success") is False

        # No kanban transition to DONE — task stays BLOCKED.
        if state.kanban_client.update_task.called:
            kanban_call = state.kanban_client.update_task.call_args
            update_data = (
                kanban_call.args[1]
                if len(kanban_call.args) > 1
                else kanban_call.kwargs.get("update_data", {})
            )
            assert update_data.get("status") != TaskStatus.DONE

        # Worktree is back to a clean state (no rebase in progress).
        wt = tmp_path / "worktrees" / "agent_X"
        rebase_dir = wt / ".git" / "rebase-merge"
        rebase_apply = wt / ".git" / "rebase-apply"
        # Neither rebase-state directory should remain.
        assert not rebase_dir.exists()
        assert not rebase_apply.exists()


# ---------------------------------------------------------------------------
# _attempt_merge_recovery — edge cases (defensive)
# ---------------------------------------------------------------------------


class TestAttemptMergeRecoveryDefensive:
    """No-op gracefully when inputs are missing or malformed."""

    @pytest.mark.asyncio
    async def test_no_merge_conflict_in_source_context_returns_none(
        self,
    ) -> None:
        """Task without merge_conflict source_context is a no-op.

        Defensive: the helper might be called speculatively on any
        BLOCKED task; only those marked by ``_apply_merge_failure_to_
        update_data`` should be attempted.
        """
        from src.marcus_mcp.tools.task import _attempt_merge_recovery

        now = datetime.now(timezone.utc)
        task = Task(
            id="task_X",
            name="X",
            description="...",
            status=TaskStatus.BLOCKED,
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=now,
            updated_at=now,
            due_date=None,
            estimated_hours=1.0,
            source_context=None,  # No merge_conflict record
        )
        state = MagicMock()
        state.kanban_client = MagicMock()

        result = await _attempt_merge_recovery(task=task, state=state)

        assert result is None
        state.kanban_client.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_worktree_returns_none(self, tmp_path) -> None:
        """If the worktree directory was cleaned up, recovery is impossible.

        Could happen across Marcus restarts or if the experiment runner
        teardown removed the worktrees.  Return None so the task stays
        BLOCKED; a human can investigate.
        """
        from src.marcus_mcp.tools.task import _attempt_merge_recovery

        # Repo exists but no worktree directory for agent_X.
        impl = tmp_path / "implementation"
        impl.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch=main"],
            cwd=impl,
            check=True,
            capture_output=True,
        )

        task = _make_blocked_task()
        state = _make_state(impl, blocked_task=task)

        result = await _attempt_merge_recovery(task=task, state=state)

        assert result is None
