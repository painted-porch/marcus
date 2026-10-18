"""
Unit tests for FileLockRegistry (#206 MVP, Phase 1).

The registry tracks which file paths each in-progress task is authorized
to write to. Marcus's ``request_next_task`` filters out tasks whose
declared files conflict with current holders, preventing two agents
from independently editing the same shared file (e.g., ``package.json``,
``src/lib/db.ts``) and then failing on merge to ``main``.

These tests cover the pure registry primitive — no decomposer or
task-assignment integration. See ``docs/design/206-file-lock-registry-mvp.md``
for the full design and the bright-line analysis.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from src.core.file_lock_registry import (
    AcquireResult,
    FileLockHolder,
    FileLockRegistry,
)

pytestmark = pytest.mark.unit


class TestFileLockRegistryAcquire:
    """``try_acquire`` is the all-or-nothing claim primitive."""

    @pytest.mark.asyncio
    async def test_acquire_succeeds_on_empty_registry(self) -> None:
        """A first-time claim on a clean registry must succeed."""
        registry = FileLockRegistry()
        result = await registry.try_acquire(
            task_id="task-1",
            agent_id="agent-A",
            files=["src/foo.py"],
        )
        assert result.success is True
        assert result.blocker is None
        assert registry.held_by("src/foo.py") is not None

    @pytest.mark.asyncio
    async def test_acquire_succeeds_when_no_files_conflict(self) -> None:
        """Two tasks claiming disjoint files both succeed."""
        registry = FileLockRegistry()
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])
        result = await registry.try_acquire(
            "task-2", "agent-B", ["src/bar.py", "src/baz.py"]
        )
        assert result.success is True
        assert result.blocker is None

    @pytest.mark.asyncio
    async def test_acquire_is_all_or_nothing_on_conflict(self) -> None:
        """If any requested file is held, NO files from the request are claimed.

        This is the core invariant. Partial acquisition would leave some
        files held by a task that wasn't actually assigned — a leak.
        """
        registry = FileLockRegistry()
        # task-1 holds src/foo.py
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])

        # task-2 wants src/foo.py AND src/bar.py — foo.py is held, so
        # the WHOLE acquisition must fail and bar.py must remain free.
        result = await registry.try_acquire(
            "task-2", "agent-B", ["src/foo.py", "src/bar.py"]
        )
        assert result.success is False
        assert registry.held_by("src/bar.py") is None

    @pytest.mark.asyncio
    async def test_acquire_returns_blocker_info_on_failure(self) -> None:
        """On failure, result names the holder and the specific file.

        The caller (request_next_task) uses this for logging and for
        the rejection message the agent receives.
        """
        registry = FileLockRegistry()
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])

        result = await registry.try_acquire("task-2", "agent-B", ["src/foo.py"])
        assert result.success is False
        assert result.blocker is not None
        assert result.blocker.task_id == "task-1"
        assert result.blocker.agent_id == "agent-A"
        assert result.blocker_file == "src/foo.py"

    @pytest.mark.asyncio
    async def test_acquire_empty_files_is_noop_success(self) -> None:
        """A task with no declared files acquires nothing — and succeeds.

        Many existing tasks have no declared_files (feature-based path,
        legacy tasks). Those must not be blocked from assignment.
        """
        registry = FileLockRegistry()
        result = await registry.try_acquire("task-1", "agent-A", [])
        assert result.success is True
        assert len(registry.snapshot()) == 0


class TestFileLockRegistryRelease:
    """``release`` returns files to the available pool when a task ends."""

    @pytest.mark.asyncio
    async def test_release_frees_all_files_held_by_task(self) -> None:
        """Releasing a task drops every file it held."""
        registry = FileLockRegistry()
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py", "src/bar.py"])
        count = await registry.release("task-1")
        assert count == 2
        assert registry.held_by("src/foo.py") is None
        assert registry.held_by("src/bar.py") is None

    @pytest.mark.asyncio
    async def test_release_is_idempotent(self) -> None:
        """Releasing a task with no held locks returns 0 and does not raise.

        Both the DONE-path and the BLOCKED-path of report_task_progress
        call release; a task that hit only one path must not blow up
        if the other path also fires later.
        """
        registry = FileLockRegistry()
        assert await registry.release("nonexistent") == 0
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])
        await registry.release("task-1")
        # Second release on same task — no-op, no exception.
        assert await registry.release("task-1") == 0

    @pytest.mark.asyncio
    async def test_release_only_affects_target_task(self) -> None:
        """Releasing task A must not touch task B's locks."""
        registry = FileLockRegistry()
        await registry.try_acquire("task-A", "agent-1", ["src/foo.py"])
        await registry.try_acquire("task-B", "agent-2", ["src/bar.py"])
        await registry.release("task-A")
        assert registry.held_by("src/foo.py") is None
        assert registry.held_by("src/bar.py") is not None

    @pytest.mark.asyncio
    async def test_released_file_can_be_reacquired(self) -> None:
        """The whole point: after release, another task can claim the file."""
        registry = FileLockRegistry()
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])
        await registry.release("task-1")
        result = await registry.try_acquire("task-2", "agent-B", ["src/foo.py"])
        assert result.success is True


class TestFileLockRegistryQueries:
    """Read-only lookups must work without acquiring the asyncio lock."""

    @pytest.mark.asyncio
    async def test_held_by_returns_none_for_unheld_file(self) -> None:
        """A path no one claimed has no holder."""
        registry = FileLockRegistry()
        assert registry.held_by("src/never_held.py") is None

    @pytest.mark.asyncio
    async def test_held_by_returns_holder_dataclass(self) -> None:
        """held_by gives back the FileLockHolder record for the path."""
        registry = FileLockRegistry()
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])
        holder = registry.held_by("src/foo.py")
        assert isinstance(holder, FileLockHolder)
        assert holder.task_id == "task-1"
        assert holder.agent_id == "agent-A"
        assert isinstance(holder.acquired_at, datetime)

    @pytest.mark.asyncio
    async def test_any_held_false_for_empty_list(self) -> None:
        """An empty file list cannot be 'held' by anyone — fast path."""
        registry = FileLockRegistry()
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])
        assert registry.any_held([]) is False

    @pytest.mark.asyncio
    async def test_any_held_true_when_one_file_held(self) -> None:
        """If even ONE file in the list is held, any_held is True."""
        registry = FileLockRegistry()
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])
        assert registry.any_held(["src/foo.py", "src/free.py"]) is True

    @pytest.mark.asyncio
    async def test_any_held_false_when_no_files_held(self) -> None:
        """All files free -> any_held False."""
        registry = FileLockRegistry()
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])
        assert registry.any_held(["src/free1.py", "src/free2.py"]) is False

    @pytest.mark.asyncio
    async def test_snapshot_returns_independent_copy(self) -> None:
        """Snapshot is for telemetry — mutating it must not affect state.

        Snapshot is keyed by ``(project_id, file_path)`` tuples after
        the Kaia review fix that scopes locks per project.
        """
        registry = FileLockRegistry()
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])
        snap = registry.snapshot()
        assert ("", "src/foo.py") in snap
        snap.clear()  # mutate the snapshot
        # Registry still holds the lock — the snapshot was a copy.
        assert registry.held_by("src/foo.py") is not None


class TestFileLockRegistryConcurrency:
    """Concurrent acquire/release must be atomic — no torn state."""

    @pytest.mark.asyncio
    async def test_concurrent_acquires_for_same_file_one_wins(self) -> None:
        """Two tasks racing for the same file: exactly one acquires it.

        Without the asyncio.Lock both could see the file as free,
        both insert holders, and the later writer would silently
        clobber the earlier one — exactly the bug the registry is
        meant to prevent.
        """
        registry = FileLockRegistry()
        # 20 tasks all racing for src/foo.py — exactly one wins.
        results = await asyncio.gather(
            *(
                registry.try_acquire(
                    task_id=f"task-{i}",
                    agent_id=f"agent-{i}",
                    files=["src/foo.py"],
                )
                for i in range(20)
            )
        )
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        assert len(successes) == 1
        assert len(failures) == 19
        # All failures must name the same blocker — the one winner.
        winning_task_id = registry.held_by("src/foo.py").task_id  # type: ignore[union-attr]
        for failure in failures:
            assert failure.blocker is not None
            assert failure.blocker.task_id == winning_task_id

    @pytest.mark.asyncio
    async def test_concurrent_release_and_acquire_serializes(self) -> None:
        """A release and a competing acquire on the same file don't deadlock."""
        registry = FileLockRegistry()
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])

        # Release and a new acquire race. The acquire either sees the
        # lock still held (fails) or sees it released (succeeds). Both
        # paths complete without deadlock or torn state.
        release_task = registry.release("task-1")
        acquire_task = registry.try_acquire("task-2", "agent-B", ["src/foo.py"])
        release_count, acquire_result = await asyncio.gather(release_task, acquire_task)
        assert release_count == 1
        # Whether the acquire succeeded or not, the registry's view
        # must be self-consistent: there is at most one holder.
        snap = registry.snapshot()
        holders = [h for h in snap.values() if h.task_id == "task-2"]
        if acquire_result.success:
            assert len(holders) == 1
        else:
            assert len(holders) == 0


class TestProjectScoping:
    """Locks are keyed by (project_id, file_path) — Kaia review fix.

    Without project scoping, two concurrently-running projects with
    tasks targeting the same relative path (e.g. ``src/main.py``)
    would spuriously block each other. Each project gets its own
    namespace; locks in one are invisible to the other.
    """

    @pytest.mark.asyncio
    async def test_same_path_different_projects_do_not_conflict(self) -> None:
        """Project A holding src/foo.py does NOT block project B's claim."""
        registry = FileLockRegistry()
        await registry.try_acquire(
            "task-A", "agent-1", ["src/foo.py"], project_id="proj-1"
        )
        # Project 2 wants the same path string — should succeed.
        result = await registry.try_acquire(
            "task-B", "agent-2", ["src/foo.py"], project_id="proj-2"
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_same_path_same_project_conflicts(self) -> None:
        """Within a single project, the original conflict semantics hold."""
        registry = FileLockRegistry()
        await registry.try_acquire(
            "task-A", "agent-1", ["src/foo.py"], project_id="proj-1"
        )
        result = await registry.try_acquire(
            "task-B", "agent-2", ["src/foo.py"], project_id="proj-1"
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_held_by_is_project_scoped(self) -> None:
        """``held_by`` for the wrong project returns None."""
        registry = FileLockRegistry()
        await registry.try_acquire(
            "task-A", "agent-1", ["src/foo.py"], project_id="proj-1"
        )
        assert registry.held_by("src/foo.py", project_id="proj-1") is not None
        assert registry.held_by("src/foo.py", project_id="proj-2") is None
        # Default namespace is yet another scope.
        assert registry.held_by("src/foo.py") is None

    @pytest.mark.asyncio
    async def test_any_held_is_project_scoped(self) -> None:
        """``any_held`` only sees locks in the queried project."""
        registry = FileLockRegistry()
        await registry.try_acquire(
            "task-A", "agent-1", ["src/foo.py"], project_id="proj-1"
        )
        assert registry.any_held(["src/foo.py"], project_id="proj-1") is True
        assert registry.any_held(["src/foo.py"], project_id="proj-2") is False

    @pytest.mark.asyncio
    async def test_release_frees_locks_regardless_of_project(self) -> None:
        """Release takes only ``task_id`` — task IDs are globally unique.

        Even if a task somehow held files in two projects (impossible
        under current Marcus semantics but defensible behavior), one
        ``release`` call drops them all.
        """
        registry = FileLockRegistry()
        await registry.try_acquire(
            "task-A", "agent-1", ["src/foo.py"], project_id="proj-1"
        )
        await registry.try_acquire(
            "task-A", "agent-1", ["src/bar.py"], project_id="proj-2"
        )
        count = await registry.release("task-A")
        assert count == 2


class TestAcquireResult:
    """AcquireResult is the structured return shape — exercise the contract."""

    def test_success_result_has_no_blocker(self) -> None:
        """On success, blocker fields are None."""
        result = AcquireResult(success=True)
        assert result.blocker is None
        assert result.blocker_file is None

    def test_failure_result_carries_blocker(self) -> None:
        """On failure, blocker and blocker_file are populated for messaging."""
        holder = FileLockHolder(
            task_id="task-1",
            agent_id="agent-A",
            acquired_at=datetime.now(timezone.utc),
        )
        result = AcquireResult(success=False, blocker=holder, blocker_file="src/foo.py")
        assert result.blocker is holder
        assert result.blocker_file == "src/foo.py"
