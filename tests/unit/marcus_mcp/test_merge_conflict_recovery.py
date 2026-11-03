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
from typing import Any, Optional
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


def _git(cwd: Path, *args: str) -> None:
    """Run git in ``cwd`` and raise on failure.

    Parameters
    ----------
    cwd : Path
        Directory to run in.
    *args : str
        Git arguments.
    """
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_out(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` and return stripped stdout.

    Parameters
    ----------
    cwd : Path
        Directory to run in.
    *args : str
        Git arguments.

    Returns
    -------
    str
        Stripped stdout.
    """
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    ).stdout.strip()


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


def _setup_untracked_collision_repo(tmp_path: Path) -> Path:
    """Recoverable stale-base repo whose worktree has a colliding untracked file.

    Like :func:`_setup_recoverable_repo` (the agent's commit and main's
    commit touch different files, so the rebase is mechanically clean),
    but ``main`` also gained a *committed* ``lock.txt`` after the agent
    branched, and the worktree holds an *untracked* ``lock.txt`` in the
    way.  A plain ``git reset --hard`` leaves the untracked file behind,
    so ``git rebase main`` aborts with "untracked working tree files
    would be overwritten by checkout" — the P2 failure mode.  Setting the
    untracked file aside (stash ``--include-untracked``) lets the rebase
    proceed.
    """
    impl = _setup_recoverable_repo(tmp_path)

    def git(cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    # main gains a committed lock.txt (agent's branch does not have it).
    (impl / "lock.txt").write_text("generated on main\n")
    git(impl, "add", "lock.txt")
    git(impl, "commit", "-m", "main adds generated lock.txt")

    # The worktree holds an untracked lock.txt with different content.
    wt = impl.parent / "worktrees" / "agent_X"
    (wt / "lock.txt").write_text("stale generated copy from a gate run\n")

    return impl


def _make_state(
    impl: Path,
    *,
    blocked_task: Task,
    baseline: Optional[str] = None,
    storage: Optional[Path] = None,
) -> MagicMock:
    """Marcus state mock with the inputs the recovery helper reads.

    Parameters
    ----------
    impl : Path
        The implementation repository, reported as ``project_root``.
    blocked_task : Task
        The BLOCKED task the sweep will pick up.
    baseline : Optional[str]
        When given, recorded in a real ``AssignmentPersistence`` as the
        task's claim-time baseline. Recovery refuses without one, so a
        test that expects a landing has to supply it.
    storage : Optional[Path]
        Where that store lives. Required when ``baseline`` is given.

    Returns
    -------
    MagicMock
        The state double.
    """
    state = MagicMock()
    state.kanban_client = MagicMock()
    state.kanban_client._load_workspace_state = MagicMock(
        return_value={"project_root": str(impl)}
    )
    state.kanban_client.update_task = AsyncMock(return_value={"success": True})
    state.kanban_client.get_all_tasks = AsyncMock(return_value=[blocked_task])
    state.project_tasks = [blocked_task]
    state.agent_tasks = {}
    if baseline is not None:
        from src.core.assignment_persistence import AssignmentPersistence

        assert storage is not None, "a baseline needs somewhere to live"
        state.assignment_persistence = AssignmentPersistence(storage_dir=storage)
        state.pending_baseline = (baseline, blocked_task.id)
    else:
        state.assignment_persistence = None
    return state


async def _with_baseline(state: MagicMock, agent_id: str = "agent_X") -> None:
    """Write the recorded baseline into the state's store.

    Separate from ``_make_state`` because the store's writes are async.

    Parameters
    ----------
    state : MagicMock
        A state built with ``baseline=``.
    agent_id : str
        The agent that claimed the task.
    """
    baseline, task_id = state.pending_baseline
    await state.assignment_persistence.record_task_baseline(task_id, agent_id, baseline)


# ---------------------------------------------------------------------------
# _attempt_merge_recovery — happy path
# ---------------------------------------------------------------------------


class TestRecoveryLandsOnlyThisTask:
    """Issue #796: the rescue must not merge the whole session branch."""

    @pytest.mark.asyncio
    async def test_a_refused_task_on_the_branch_stays_off_main(self, tmp_path) -> None:
        """The headline case, which the old rebase-and-fast-forward failed.

        The branch carries a task Marcus refused and then this task's
        own work. Rescuing this task must land its work and leave the
        refused commit's file off main.
        """
        from src.marcus_mcp.tools.task import _attempt_merge_recovery

        impl = _setup_recoverable_repo(tmp_path)
        wt = impl.parent / "worktrees" / "agent_X"

        # A refused task's commit, then this task's baseline, then its work.
        _git(wt, "rm", "-q", "agent_X.txt")
        (wt / "refused.txt").write_text("work Marcus refused\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", "refused task")
        baseline = _git_out(wt, "rev-parse", "HEAD")
        (wt / "mine.txt").write_text("this task's work\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", "this task")

        task = _make_blocked_task()
        state = _make_state(
            impl,
            blocked_task=task,
            baseline=baseline,
            storage=tmp_path / "assignments",
        )
        await _with_baseline(state)

        result = await _attempt_merge_recovery(task=task, state=state)

        assert result is not None and result["success"] is True
        assert result["method"] == "task_scoped_landing"
        assert (impl / "mine.txt").exists(), "this task's work did not land"
        assert not (impl / "refused.txt").exists(), "a refused task reached main"
        # main's own file is untouched.
        assert (impl / "mainonly.txt").exists()

    @pytest.mark.asyncio
    async def test_without_a_baseline_the_task_stays_blocked(self, tmp_path) -> None:
        """No baseline means the landing could only be widened, so it stops.

        On the completion path a missing baseline falls back to merging
        the whole branch, because an agent is waiting and its work should
        not be dropped. Here nobody is waiting, and widening is the
        defect this path exists to stop.
        """
        from src.marcus_mcp.tools.task import _attempt_merge_recovery

        impl = _setup_recoverable_repo(tmp_path)
        before = _git_out(impl, "rev-parse", "main")
        task = _make_blocked_task()
        state = _make_state(impl, blocked_task=task)

        result = await _attempt_merge_recovery(task=task, state=state)

        assert result is None
        assert _git_out(impl, "rev-parse", "main") == before
        assert not (impl / "agent_X.txt").exists()

    @pytest.mark.asyncio
    async def test_recovery_marks_the_task_done_and_clears_the_conflict(
        self, tmp_path
    ) -> None:
        """On success the board stops saying BLOCKED and loses the record."""
        from src.marcus_mcp.tools.task import _attempt_merge_recovery

        impl = _setup_recoverable_repo(tmp_path)
        wt = impl.parent / "worktrees" / "agent_X"
        baseline = _git_out(wt, "rev-parse", "HEAD~1")
        task = _make_blocked_task()
        state = _make_state(
            impl,
            blocked_task=task,
            baseline=baseline,
            storage=tmp_path / "assignments",
        )
        await _with_baseline(state)

        result = await _attempt_merge_recovery(task=task, state=state)

        assert result is not None and result["success"] is True
        assert (impl / "agent_X.txt").read_text() == "agent X work\n"
        state.kanban_client.update_task.assert_awaited()
        call = state.kanban_client.update_task.call_args
        update_data = call.args[1] if len(call.args) > 1 else call.kwargs["update_data"]
        assert update_data["status"] == TaskStatus.DONE
        assert "merge_conflict" not in (update_data.get("source_context") or {})

    @pytest.mark.asyncio
    async def test_the_agents_worktree_is_left_exactly_as_it_was(
        self, tmp_path
    ) -> None:
        """The sweep no longer stashes, resets or rebases anything.

        It used to do all three to make room for a rebase, which meant a
        sweep running on someone else's poll could disturb an agent's
        uncommitted work. There is nothing left to disturb.
        """
        from src.marcus_mcp.tools.task import _attempt_merge_recovery

        impl = _setup_recoverable_repo(tmp_path)
        wt = impl.parent / "worktrees" / "agent_X"
        baseline = _git_out(wt, "rev-parse", "HEAD~1")
        # Uncommitted and untracked work the agent still has in hand.
        (wt / "agent_X.txt").write_text("agent X work, edited but not committed\n")
        (wt / "scratch.log").write_text("a gate run left this\n")
        head_before = _git_out(wt, "rev-parse", "HEAD")
        status_before = _git_out(wt, "status", "--porcelain")

        task = _make_blocked_task()
        state = _make_state(
            impl,
            blocked_task=task,
            baseline=baseline,
            storage=tmp_path / "assignments",
        )
        await _with_baseline(state)

        await _attempt_merge_recovery(task=task, state=state)

        assert _git_out(wt, "rev-parse", "HEAD") == head_before
        assert _git_out(wt, "status", "--porcelain") == status_before
        assert (wt / "scratch.log").exists()
        assert (
            wt / "agent_X.txt"
        ).read_text() == "agent X work, edited but not committed\n"
        assert _git_out(wt, "stash", "list") == "", "the sweep stashed something"

    @pytest.mark.asyncio
    async def test_a_still_conflicting_task_stays_blocked(self, tmp_path) -> None:
        """A genuine unresolved conflict is the honest BLOCKED case."""
        from src.marcus_mcp.tools.task import _attempt_merge_recovery

        impl = _setup_recoverable_repo(tmp_path)
        wt = impl.parent / "worktrees" / "agent_X"
        baseline = _git_out(wt, "rev-parse", "HEAD")
        # Both sides change the same line after the baseline.
        (wt / "base.txt").write_text("the agent's line\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", "agent edits base.txt")
        (impl / "base.txt").write_text("main's line\n")
        _git(impl, "add", "-A")
        _git(impl, "commit", "-m", "main edits base.txt")
        before = _git_out(impl, "rev-parse", "main")

        task = _make_blocked_task()
        state = _make_state(
            impl,
            blocked_task=task,
            baseline=baseline,
            storage=tmp_path / "assignments",
        )
        await _with_baseline(state)

        result = await _attempt_merge_recovery(task=task, state=state)

        assert result is None
        assert _git_out(impl, "rev-parse", "main") == before
        assert (impl / "base.txt").read_text() == "main's line\n"
        assert _git_out(impl, "status", "--porcelain") == ""

    @pytest.mark.asyncio
    async def test_manual_resolution_in_progress_is_not_disturbed(
        self, tmp_path
    ) -> None:
        """A worktree mid-merge is left alone.

        The blocker message tells the agent to resolve the conflict in
        that worktree, and this sweep runs on any other agent's no-task
        poll. A branch tip that is still the commit which failed would
        only fail again, so retrying there is churn at best.
        """
        from src.marcus_mcp.tools.task import _attempt_merge_recovery

        impl = _setup_recoverable_repo(tmp_path)
        wt = impl.parent / "worktrees" / "agent_X"
        baseline = _git_out(wt, "rev-parse", "HEAD~1")
        # Put the worktree mid-merge, as a resolving agent would.
        (wt / "base.txt").write_text("agent's side\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", "agent edits base.txt")
        (impl / "base.txt").write_text("main's side\n")
        _git(impl, "add", "-A")
        _git(impl, "commit", "-m", "main edits base.txt")
        subprocess.run(
            ["git", "merge", "main", "--no-edit"], cwd=wt, capture_output=True
        )
        assert _git_out(wt, "rev-parse", "--verify", "--quiet", "MERGE_HEAD")

        task = _make_blocked_task()
        state = _make_state(
            impl,
            blocked_task=task,
            baseline=baseline,
            storage=tmp_path / "assignments",
        )
        await _with_baseline(state)

        result = await _attempt_merge_recovery(task=task, state=state)

        assert result is None
        # The resolution in progress is untouched.
        assert _git_out(wt, "rev-parse", "--verify", "--quiet", "MERGE_HEAD")

    @pytest.mark.asyncio
    async def test_recovery_opens_a_pending_reconciliation_card(self, tmp_path) -> None:
        """A displaced session's refusal is reconciled after this path too.

        This path marks the task DONE without going through
        ``report_task_progress``, so a refusal recorded while the
        replacement was conflicted would otherwise never get its card
        (Codex P2 on PR #794).
        """
        from src.marcus_mcp.tools.task import _attempt_merge_recovery

        impl = _setup_recoverable_repo(tmp_path)
        wt = impl.parent / "worktrees" / "agent_X"
        baseline = _git_out(wt, "rev-parse", "HEAD~1")
        task = _make_blocked_task()
        state = _make_state(
            impl,
            blocked_task=task,
            baseline=baseline,
            storage=tmp_path / "assignments",
        )
        await _with_baseline(state)
        created: list = []

        async def _create_task(task_data):
            created.append(task_data)
            card = _make_blocked_task()
            card.id = "recon_card"
            card.name = task_data["name"]
            card.description = task_data["description"]
            return card

        state.kanban_client.create_task = AsyncMock(side_effect=_create_task)
        await state.assignment_persistence.record_refused_attempt(
            task.id,
            {
                "agent_id": "displaced_session",
                "branch": "marcus/displaced_session",
                "lease_epoch": 1,
                "refused_at": "2026-11-03T00:00:00+00:00",
            },
        )

        result = await _attempt_merge_recovery(task=task, state=state)

        assert result is not None and result["success"] is True
        assert len(created) == 1, "the recovery left the refusal unreconciled"
        assert "marcus/displaced_session" in created[0]["description"]


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
