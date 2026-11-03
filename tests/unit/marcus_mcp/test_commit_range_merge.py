"""
Unit tests for per-card commit-range merging (issue #730).

Background
----------
Marcus decides *whether* an agent's work is accepted. Until this change
it did not control *which commits* reach ``main``:
``_merge_agent_branch_to_main`` merged the agent's whole branch.

That is harmless while one throwaway agent works one card, because the
branch holds exactly that card's commits. Under ADR-0012 D1 the worktree
and branch are scoped to the **session**, so one branch spans many cards.
A card whose completion was refused (by the D11 epoch fence, say) leaves
its commits on the branch, and the next accepted completion merges them
into ``main`` anyway. Nobody decided that.

The fix merges only ``baseline..HEAD`` for the card, where the baseline is
the session branch tip captured when the card was claimed. The same
baseline rescues the commit-verification gate, which otherwise passes
every later card in a session once any earlier commit is left unmerged.

These tests drive real git repositories in ``tmp_path`` rather than
mocking ``subprocess``. The defect is about git semantics (ranges,
merge commits, conflict rollback), so asserting against mocked git calls
would assert the mock, not the behaviour. No network or service is
involved. Precedent: ``test_merge_conflict_recovery.py``.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import Priority, TaskAssignment
from src.marcus_mcp.tools.task import (
    _baseline_commit_for_task,
    _capture_baseline_commit,
    _merge_agent_branch_to_main,
    _merge_card_range,
    _verify_agent_has_commits,
    _worktree_path_for_branch,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo`` and return its stdout.

    Parameters
    ----------
    repo : Path
        Directory to run the command in.
    *args : str
        Git arguments, e.g. ``"rev-parse", "HEAD"``.

    Returns
    -------
    str
        Stripped stdout of the command.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    """Create a git repo on ``main`` with one commit.

    Parameters
    ----------
    path : Path
        Directory to initialise (created if absent).

    Returns
    -------
    Path
        The repository path.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    _git(path, "config", "user.email", "marcus@example.com")
    _git(path, "config", "user.name", "Marcus Test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("base\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "initial commit")
    return path


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    """Write a file and commit it, returning the new commit sha.

    Parameters
    ----------
    repo : Path
        Repository (or worktree) to commit in.
    filename : str
        Path relative to ``repo``.
    content : str
        File contents to write.
    message : str
        Commit message.

    Returns
    -------
    str
        The sha of the new commit.
    """
    (repo / filename).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _add_worktree(repo: Path, path: Path, branch: str) -> Path:
    """Add a worktree at ``path`` on a new ``branch`` cut from main.

    Parameters
    ----------
    repo : Path
        The main repository.
    path : Path
        Where the worktree should live.
    branch : str
        Branch name to create.

    Returns
    -------
    Path
        The worktree path.
    """
    _git(repo, "worktree", "add", "-b", branch, str(path), "main")
    _git(path, "config", "user.email", "agent@example.com")
    _git(path, "config", "user.name", "Session Agent")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _make_state(project_root: Path) -> Mock:
    """Build a Mock state whose workspace points at ``project_root``.

    Parameters
    ----------
    project_root : Path
        The main repository path Marcus merges into.

    Returns
    -------
    Mock
        Minimal state double for the merge helper.
    """
    state = Mock()
    state.kanban_client = Mock()
    state.kanban_client._load_workspace_state = Mock(
        return_value={"project_root": str(project_root)}
    )
    return state


def _assignment(task_id: str, agent_id: str) -> TaskAssignment:
    """Build a minimal TaskAssignment.

    Parameters
    ----------
    task_id : str
        Task id for the assignment.
    agent_id : str
        Agent holding the assignment.

    Returns
    -------
    TaskAssignment
        Assignment with no baseline captured yet.
    """
    return TaskAssignment(
        task_id=task_id,
        task_name="Card",
        description="d",
        instructions="i",
        estimated_hours=1.0,
        priority=Priority.MEDIUM,
        dependencies=[],
        assigned_to=agent_id,
        assigned_at=datetime.now(timezone.utc),
        due_date=None,
    )


# ---------------------------------------------------------------------------
# A session branch spanning three cards, one of them refused
# ---------------------------------------------------------------------------


class _Session:
    """A main repo plus a session worktree, mirroring issue #730's example."""

    def __init__(self, tmp_path: Path, agent_id: str = "session_1") -> None:
        self.agent_id = agent_id
        self.repo = _init_repo(tmp_path / "implementation")
        self.worktree = _add_worktree(
            self.repo, tmp_path / "worktrees" / agent_id, f"marcus/{agent_id}"
        )
        self.state = _make_state(self.repo)


@pytest.fixture
def session(tmp_path: Path) -> _Session:
    """Provide a main repo with one session worktree."""
    return _Session(tmp_path)


class TestMergeByCommitRange:
    """``_merge_agent_branch_to_main`` must merge one card, not one branch."""

    @pytest.mark.asyncio
    async def test_refused_card_does_not_reach_main(self, session: _Session) -> None:
        """A rejected card's commits stay off main when a later card merges.

        This is issue #730's worked example: card A accepted, card B
        refused by the fence, card C accepted. Main must hold A and C.
        """
        # Arrange — card A, claimed at the branch point and accepted.
        baseline_a = _git(session.worktree, "rev-parse", "HEAD")
        _commit(session.worktree, "a.txt", "A\n", "card A")
        merged_a = await _merge_agent_branch_to_main(
            session.agent_id, "card-a", session.state, baseline_commit=baseline_a
        )
        assert merged_a is not None and merged_a["success"] is True

        # Card B — committed, then refused: Marcus never merges it.
        _commit(session.worktree, "b.txt", "B\n", "card B (refused)")

        # Card C — claimed after B, so its baseline is the branch tip now.
        baseline_c = _git(session.worktree, "rev-parse", "HEAD")
        _commit(session.worktree, "c.txt", "C\n", "card C")

        # Act
        merged_c = await _merge_agent_branch_to_main(
            session.agent_id, "card-c", session.state, baseline_commit=baseline_c
        )

        # Assert
        assert merged_c is not None and merged_c["success"] is True
        assert (session.repo / "a.txt").exists()
        assert (session.repo / "c.txt").exists()
        assert not (session.repo / "b.txt").exists()

    @pytest.mark.asyncio
    async def test_without_a_baseline_the_whole_branch_merges(
        self, session: _Session
    ) -> None:
        """No baseline falls back to the pre-#730 whole-branch merge.

        A deployment with no persisted baseline (an assignment claimed
        before this change, say) must still merge rather than silently
        drop the work.
        """
        # Arrange
        _commit(session.worktree, "b.txt", "B\n", "card B (refused)")
        _commit(session.worktree, "c.txt", "C\n", "card C")

        # Act
        result = await _merge_agent_branch_to_main(
            session.agent_id, "card-c", session.state, baseline_commit=None
        )

        # Assert
        assert result is not None and result["success"] is True
        assert (session.repo / "b.txt").exists()
        assert (session.repo / "c.txt").exists()

    @pytest.mark.asyncio
    async def test_empty_range_merges_nothing_and_reports_it(
        self, session: _Session
    ) -> None:
        """A card with no commits of its own leaves main untouched."""
        # Arrange
        _commit(session.worktree, "b.txt", "B\n", "card B (refused)")
        baseline = _git(session.worktree, "rev-parse", "HEAD")
        main_before = _git(session.repo, "rev-parse", "main")

        # Act
        result = await _merge_agent_branch_to_main(
            session.agent_id, "card-c", session.state, baseline_commit=baseline
        )

        # Assert
        assert result == {"success": True, "commits_merged": 0}
        assert _git(session.repo, "rev-parse", "main") == main_before
        assert not (session.repo / "b.txt").exists()

    @pytest.mark.asyncio
    async def test_conflict_leaves_main_exactly_as_it_was(
        self, session: _Session
    ) -> None:
        """A conflicting range aborts cleanly and reports merge_conflict."""
        # Arrange — main and the session edit the same line.
        baseline = _git(session.worktree, "rev-parse", "HEAD")
        _commit(session.worktree, "README.md", "session edit\n", "card C")
        _commit(session.repo, "README.md", "main edit\n", "main moved on")
        main_before = _git(session.repo, "rev-parse", "main")

        # Act
        result = await _merge_agent_branch_to_main(
            session.agent_id, "card-c", session.state, baseline_commit=baseline
        )

        # Assert
        assert result is not None
        assert result["success"] is False
        assert result["error"] == "merge_conflict"
        assert _git(session.repo, "rev-parse", "main") == main_before
        assert (session.repo / "README.md").read_text() == "main edit\n"
        # No half-finished cherry-pick left behind for the next merge.
        state_check = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"],
            cwd=session.repo,
            capture_output=True,
            text=True,
        )
        assert state_check.returncode != 0
        assert _git(session.repo, "status", "--porcelain") == ""

    @pytest.mark.asyncio
    async def test_range_carries_a_conflict_resolution_merge_commit(
        self, session: _Session
    ) -> None:
        """A merge commit inside the range contributes its resolution.

        Agents resolve a conflict by merging main into their branch. The
        resolution then lives only in that merge commit, so the range
        merge must apply it rather than skip it.
        """
        # Arrange — main moves, the session merges main in and resolves.
        _commit(session.repo, "shared.txt", "main version\n", "main writes shared")
        baseline = _git(session.worktree, "rev-parse", "HEAD")
        _commit(session.worktree, "shared.txt", "session version\n", "card C")
        merge_attempt = subprocess.run(
            ["git", "merge", "main", "--no-edit"],
            cwd=session.worktree,
            capture_output=True,
            text=True,
        )
        assert merge_attempt.returncode != 0, "expected a conflict to resolve"
        (session.worktree / "shared.txt").write_text("resolved version\n")
        _git(session.worktree, "add", "-A")
        _git(session.worktree, "commit", "--no-edit")

        # Act
        result = await _merge_agent_branch_to_main(
            session.agent_id, "card-c", session.state, baseline_commit=baseline
        )

        # Assert
        assert result is not None and result["success"] is True
        assert (session.repo / "shared.txt").read_text() == "resolved version\n"

    @pytest.mark.asyncio
    async def test_landing_commit_records_the_cards_provenance(
        self, session: _Session
    ) -> None:
        """One commit lands the card, and it names where the work came from.

        A card lands as a single commit so that intermediate states the
        agent later fixed are never reapplied. The granular commits stay
        on the session branch, so the landing commit has to carry the
        range and the shas that explain it.
        """
        # Arrange
        baseline = _git(session.worktree, "rev-parse", "HEAD")
        first = _commit(session.worktree, "one.txt", "1\n", "card C part one")
        second = _commit(session.worktree, "two.txt", "2\n", "card C part two")
        main_before = _git(session.repo, "rev-parse", "main")

        # Act
        result = await _merge_agent_branch_to_main(
            session.agent_id, "card-c", session.state, baseline_commit=baseline
        )

        # Assert
        assert result == {"success": True, "commits_merged": 2}
        assert (session.repo / "one.txt").exists()
        assert (session.repo / "two.txt").exists()
        # Exactly one new commit on main, and it is not a merge commit.
        new_commits = _git(session.repo, "rev-list", f"{main_before}..main").split()
        assert len(new_commits) == 1
        parents = _git(session.repo, "rev-list", "--parents", "-n", "1", "main")
        assert len(parents.split()) == 2
        message = _git(session.repo, "log", "-1", "--format=%B", "main")
        assert "card-c" in message
        assert f"marcus/{session.agent_id}" in message
        assert baseline[:8] in message
        assert first[:8] in message
        assert second[:8] in message
        assert "card C part one" in message
        assert "card C part two" in message

    @pytest.mark.asyncio
    async def test_unknown_baseline_falls_back_to_the_branch(
        self, session: _Session
    ) -> None:
        """A baseline git cannot resolve must not drop the agent's work."""
        # Arrange
        _commit(session.worktree, "c.txt", "C\n", "card C")

        # Act — 40 hex digits that are not a commit in this repo.
        result = await _merge_agent_branch_to_main(
            session.agent_id,
            "card-c",
            session.state,
            baseline_commit="0" * 40,
        )

        # Assert
        assert result is not None and result["success"] is True
        assert (session.repo / "c.txt").exists()

    @pytest.mark.asyncio
    async def test_no_branch_means_no_merge(self, session: _Session) -> None:
        """An agent with no session branch still returns None (no-op)."""
        result = await _merge_agent_branch_to_main(
            "agent_without_branch", "card-c", session.state, baseline_commit=None
        )
        assert result is None


class TestRangeMergeFailureModes:
    """The replay has to fail safely, not loudly or silently."""

    def test_identical_content_is_not_an_error(self, session: _Session) -> None:
        """A card whose change is already on main lands as a no-op.

        Two sessions can arrive at byte-identical content. The merge
        leaves main alone and still reports the card as merged, because
        the card's work is present.
        """
        # Arrange — main and the session write the same file content.
        baseline = _git(session.worktree, "rev-parse", "HEAD")
        _commit(session.worktree, "same.txt", "same\n", "card C")
        _commit(session.repo, "same.txt", "same\n", "another session landed it")
        main_before = _git(session.repo, "rev-parse", "main")
        card_commits = _git(
            session.repo,
            "rev-list",
            "--reverse",
            f"marcus/{session.agent_id}",
            "--not",
            baseline,
            "main",
        ).split()

        # Act
        result = _merge_card_range(
            repo=session.repo,
            branch=f"marcus/{session.agent_id}",
            agent_id=session.agent_id,
            task_id="card-c",
            baseline_commit=baseline,
            card_commits=card_commits,
            pre_merge_sha=main_before,
        )

        # Assert
        assert result["success"] is True
        assert result["net_change"] is False
        assert _git(session.repo, "rev-parse", "main") == main_before
        assert _git(session.repo, "status", "--porcelain") == ""

    def test_a_git_level_failure_never_widens_the_merge(
        self, session: _Session
    ) -> None:
        """When git cannot do the range merge, nothing is merged at all.

        Falling back to a whole-branch merge here would reintroduce the
        defect issue #730 exists to close, so the card fails instead and
        says it needs an operator.
        """
        # Arrange
        baseline = _git(session.worktree, "rev-parse", "HEAD")
        _commit(session.worktree, "c.txt", "C\n", "card C")
        main_before = _git(session.repo, "rev-parse", "main")
        card_commits = _git(
            session.repo,
            "rev-list",
            "--reverse",
            f"marcus/{session.agent_id}",
            "--not",
            baseline,
            "main",
        ).split()

        # Act — a base sha that is not an object in this repository.
        result = _merge_card_range(
            repo=session.repo,
            branch=f"marcus/{session.agent_id}",
            agent_id=session.agent_id,
            task_id="card-c",
            baseline_commit="0" * 40,
            card_commits=card_commits,
            pre_merge_sha=main_before,
        )

        # Assert
        assert result["success"] is False
        assert result["error"] == "range_merge_failed"
        assert "operator" in result["message"]
        assert _git(session.repo, "rev-parse", "main") == main_before
        assert not (session.repo / "c.txt").exists()


class TestBaselineCapture:
    """The baseline must come from the session worktree, not from main."""

    def test_baseline_is_the_session_branch_tip(self, session: _Session) -> None:
        """Capture reads HEAD in the worktree holding the agent's branch.

        Main's HEAD moves whenever another session merges, so reading it
        would hand the merge a baseline that is not on the agent's branch.
        """
        # Arrange
        _commit(session.worktree, "earlier.txt", "x\n", "earlier card")
        _commit(session.repo, "unrelated.txt", "y\n", "another session merged")
        branch_tip = _git(session.worktree, "rev-parse", "HEAD")
        assignment = _assignment("card-c", session.agent_id)

        # Act
        _capture_baseline_commit(
            assignment, str(session.repo), agent_id=session.agent_id
        )

        # Assert
        assert assignment.baseline_commit == branch_tip
        assert assignment.baseline_commit != _git(session.repo, "rev-parse", "main")

    def test_baseline_falls_back_to_the_repo_head(self, session: _Session) -> None:
        """With no worktree for the agent, the repo HEAD is the baseline."""
        # Arrange
        assignment = _assignment("card-c", "agent_without_worktree")

        # Act
        _capture_baseline_commit(
            assignment, str(session.repo), agent_id="agent_without_worktree"
        )

        # Assert
        assert assignment.baseline_commit == _git(session.repo, "rev-parse", "HEAD")

    def test_worktree_lookup_finds_the_branch(self, session: _Session) -> None:
        """``_worktree_path_for_branch`` resolves a checked-out branch."""
        found = _worktree_path_for_branch(
            str(session.repo), f"marcus/{session.agent_id}"
        )
        assert found is not None
        assert Path(found).resolve() == session.worktree.resolve()

    def test_worktree_lookup_misses_unknown_branch(self, session: _Session) -> None:
        """An unknown branch resolves to None rather than raising."""
        assert _worktree_path_for_branch(str(session.repo), "marcus/nobody") is None


class TestBaselineLookup:
    """The baseline must survive a Marcus restart."""

    @pytest.mark.asyncio
    async def test_reads_the_in_memory_assignment_first(self) -> None:
        """The live assignment is authoritative while it exists."""
        # Arrange
        assignment = _assignment("card-c", "session_1")
        assignment.baseline_commit = "abc1234"
        state = Mock()
        state.agent_tasks = {"session_1": assignment}
        state.assignment_persistence = Mock()
        state.assignment_persistence.get_assignment = AsyncMock(return_value=None)

        # Act
        baseline = await _baseline_commit_for_task(state, "session_1", "card-c")

        # Assert
        assert baseline == "abc1234"

    @pytest.mark.asyncio
    async def test_falls_back_to_the_persisted_record(self) -> None:
        """After a restart the baseline comes off disk."""
        # Arrange
        state = Mock()
        state.agent_tasks = {}
        state.assignment_persistence = Mock()
        state.assignment_persistence.get_assignment = AsyncMock(
            return_value={
                "task_id": "card-c",
                "task_data": {"baseline_commit": "def5678"},
            }
        )

        # Act
        baseline = await _baseline_commit_for_task(state, "session_1", "card-c")

        # Assert
        assert baseline == "def5678"

    @pytest.mark.asyncio
    async def test_a_different_task_is_not_trusted(self) -> None:
        """A record for another card must not supply this card's baseline."""
        # Arrange
        state = Mock()
        state.agent_tasks = {}
        state.assignment_persistence = Mock()
        state.assignment_persistence.get_assignment = AsyncMock(
            return_value={
                "task_id": "card-b",
                "task_data": {"baseline_commit": "def5678"},
            }
        )

        # Act
        baseline = await _baseline_commit_for_task(state, "session_1", "card-c")

        # Assert
        assert baseline is None

    @pytest.mark.asyncio
    async def test_missing_everything_returns_none(self) -> None:
        """No assignment anywhere degrades to the whole-branch merge."""
        state = Mock(spec=[])
        assert await _baseline_commit_for_task(state, "session_1", "card-c") is None


class TestCommitGateScoping:
    """The commit gate must ask about the card, not about the branch."""

    def test_gate_fires_when_the_card_added_no_commits(self, session: _Session) -> None:
        """A session that wrote nothing for this card fails the gate.

        Without the baseline the branch is ahead of main from an earlier
        card, so the gate passed every later card for free — the vacuity
        the PRE-3 gate-1 inventory found.
        """
        # Arrange — an earlier card's commit is on the branch, unmerged.
        _commit(session.worktree, "earlier.txt", "x\n", "earlier card")
        baseline = _git(session.worktree, "rev-parse", "HEAD")

        # Act
        scoped = _verify_agent_has_commits(
            session.agent_id, str(session.repo), baseline_commit=baseline
        )
        unscoped = _verify_agent_has_commits(session.agent_id, str(session.repo))

        # Assert
        assert scoped is False
        assert unscoped is True, "documents the pre-#730 vacuity"

    def test_gate_passes_when_the_card_has_commits(self, session: _Session) -> None:
        """Commits after the baseline satisfy the gate."""
        # Arrange
        baseline = _git(session.worktree, "rev-parse", "HEAD")
        _commit(session.worktree, "c.txt", "C\n", "card C")

        # Act / Assert
        assert (
            _verify_agent_has_commits(
                session.agent_id, str(session.repo), baseline_commit=baseline
            )
            is True
        )

    def test_gate_falls_back_when_the_baseline_is_unknown(
        self, session: _Session
    ) -> None:
        """An unresolvable baseline reverts to the branch-level check."""
        # Arrange
        _commit(session.worktree, "c.txt", "C\n", "card C")

        # Act / Assert
        assert (
            _verify_agent_has_commits(
                session.agent_id, str(session.repo), baseline_commit="0" * 40
            )
            is True
        )

    def test_gate_skips_when_there_is_no_branch(self, session: _Session) -> None:
        """No session branch means the check cannot apply."""
        assert (
            _verify_agent_has_commits("agent_without_branch", str(session.repo)) is None
        )
