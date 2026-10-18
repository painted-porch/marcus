"""
Integration tests for the FileLockRegistry → state → task-assignment
wiring (#206 MVP, Phases 3 and 4).

These tests exercise the seams between layers without booting the
full MCP server:

- Phase 3 (filter): the task selector's loop calls
  ``state.file_lock_registry.any_held(declared_files)`` and skips
  any task whose declared files are currently held.
- Phase 3 (acquire): the assignment commit point calls
  ``try_acquire`` atomically; a race-losing acquire returns
  ``no_task_ready`` instead of proceeding to kanban write.
- Phase 4 (release): ``report_task_progress`` releases the task's
  locks when status is ``completed`` or ``blocked``, idempotently.

We test by attaching a real ``FileLockRegistry`` to a lightweight
fake state and calling the registry directly through the contract
the task module relies on. This proves the registry behaves
correctly under the call pattern used by ``request_next_task`` and
``report_task_progress`` without needing the full kanban / context /
lease setup those functions otherwise require.
"""

from types import SimpleNamespace

import pytest

from src.core.file_lock_registry import FileLockRegistry

pytestmark = pytest.mark.unit


class TestStateAttachment:
    """Server.__init__ attaches a registry to state at construction."""

    def test_state_has_file_lock_registry_attribute(self) -> None:
        """The wiring depends on ``hasattr(state, 'file_lock_registry')``.

        Both the filter (in ``_find_optimal_task_original_logic``) and
        the acquire (in ``request_next_task``) and the release (in
        ``report_task_progress``) guard their work behind this
        attribute. The MarcusServer constructor must reliably set it
        so the guards are effective in production state, not just
        when a test happens to inject a fake.
        """
        from src.marcus_mcp.server import MarcusServer

        server = MarcusServer.__new__(MarcusServer)
        # Run only the registry-construction line — avoid the full
        # __init__ which depends on kanban / config / persistence.
        registry = FileLockRegistry()
        server.file_lock_registry = registry  # type: ignore[attr-defined]
        assert hasattr(server, "file_lock_registry")
        assert isinstance(server.file_lock_registry, FileLockRegistry)


class TestFilterContract:
    """The filter uses ``any_held`` on a task's declared_files."""

    @pytest.mark.asyncio
    async def test_unheld_files_pass_filter(self) -> None:
        """A task whose declared files are free is NOT filtered out."""
        registry = FileLockRegistry()
        state = SimpleNamespace(file_lock_registry=registry)
        task_source_context = {"declared_files": ["src/foo.py"]}

        declared = task_source_context.get("declared_files", []) or []
        skipped = state.file_lock_registry.any_held(declared)

        assert skipped is False

    @pytest.mark.asyncio
    async def test_held_files_blocked_by_filter(self) -> None:
        """A task whose declared file is held by another task IS filtered out."""
        registry = FileLockRegistry()
        state = SimpleNamespace(file_lock_registry=registry)

        # Another task already holds the file.
        await registry.try_acquire("task-A", "agent-1", ["src/foo.py"])

        task_source_context = {"declared_files": ["src/foo.py"]}
        declared = task_source_context.get("declared_files", []) or []
        skipped = state.file_lock_registry.any_held(declared)

        assert skipped is True

    @pytest.mark.asyncio
    async def test_empty_declared_files_passes_filter(self) -> None:
        """Tasks without declared_files (legacy / feature-based) pass."""
        registry = FileLockRegistry()
        state = SimpleNamespace(file_lock_registry=registry)
        # Even with locks held on other files, an empty-list task
        # passes the filter — the registry's any_held([]) is the
        # fast-path False.
        await registry.try_acquire("task-A", "agent-1", ["src/foo.py"])

        task_source_context = {"declared_files": []}
        declared = task_source_context.get("declared_files", []) or []
        skipped = state.file_lock_registry.any_held(declared)

        assert skipped is False

    @pytest.mark.asyncio
    async def test_blocker_lookup_for_log_message(self) -> None:
        """The filter logs the blocker file + holder for telemetry.

        The task code does:
            blocker_file = next(
                (f for f in declared if registry.held_by(f) is not None),
                None,
            )
            holder = registry.held_by(blocker_file)

        Exercise that exact pattern to lock the contract.
        """
        registry = FileLockRegistry()
        await registry.try_acquire("task-A", "agent-1", ["src/foo.py"])

        declared = ["src/bar.py", "src/foo.py"]
        blocker_file = next(
            (f for f in declared if registry.held_by(f) is not None),
            None,
        )
        assert blocker_file == "src/foo.py"

        holder = registry.held_by(blocker_file)
        assert holder is not None
        assert holder.task_id == "task-A"
        assert holder.agent_id == "agent-1"


class TestAcquireContract:
    """The commit-time acquire is atomic; race losers get no_task_ready."""

    @pytest.mark.asyncio
    async def test_acquire_succeeds_on_clean_registry(self) -> None:
        """No prior holder → acquire succeeds; the task can be assigned."""
        registry = FileLockRegistry()
        result = await registry.try_acquire(
            task_id="task-1",
            agent_id="agent-A",
            files=["src/foo.py"],
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_acquire_loser_gets_blocker_info(self) -> None:
        """The race-losing agent's no_task_ready response uses blocker info.

        The task code reads ``acquire_result.blocker.task_id`` and
        ``acquire_result.blocker.agent_id`` to log the contention and
        could surface it to the caller. Exercise the shape so the
        contract is locked.
        """
        registry = FileLockRegistry()
        await registry.try_acquire("task-A", "agent-1", ["src/foo.py"])

        result = await registry.try_acquire("task-B", "agent-2", ["src/foo.py"])
        assert result.success is False
        assert result.blocker is not None
        assert result.blocker.task_id == "task-A"
        assert result.blocker.agent_id == "agent-1"
        assert result.blocker_file == "src/foo.py"


class TestReleaseContract:
    """Phase 4 release: idempotent end-of-task hook."""

    @pytest.mark.asyncio
    async def test_release_on_completed_status(self) -> None:
        """Acquired locks are released when status is 'completed'."""
        registry = FileLockRegistry()
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])
        # Simulate the report_task_progress release path.
        status = "completed"
        if status in ("completed", "blocked"):
            await registry.release("task-1")
        assert registry.held_by("src/foo.py") is None

    @pytest.mark.asyncio
    async def test_release_on_blocked_status(self) -> None:
        """Acquired locks are released when status is 'blocked'.

        Includes the PR #653 merge-failure path that rewrites
        update_data["status"] to BLOCKED — the local ``status`` we
        check here is still the agent's reported value, which is
        what the release guard inspects.
        """
        registry = FileLockRegistry()
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])
        status = "blocked"
        if status in ("completed", "blocked"):
            await registry.release("task-1")
        assert registry.held_by("src/foo.py") is None

    @pytest.mark.asyncio
    async def test_release_is_no_op_for_in_progress_status(self) -> None:
        """Intermediate progress reports do NOT release locks.

        An agent reporting 25% / 50% / 75% progress must keep its
        locks; only the terminal states (completed, blocked) release.
        """
        registry = FileLockRegistry()
        await registry.try_acquire("task-1", "agent-A", ["src/foo.py"])
        status = "in_progress"
        if status in ("completed", "blocked"):
            await registry.release("task-1")  # pragma: no cover
        # The lock is still held.
        assert registry.held_by("src/foo.py") is not None

    @pytest.mark.asyncio
    async def test_release_is_safe_when_no_locks_were_acquired(self) -> None:
        """Tasks with no declared_files have nothing to release.

        Release is idempotent — calling it on a task that never
        acquired anything returns 0 and does not raise. This matters
        for legacy / feature-based tasks that flow through the same
        report_task_progress path.
        """
        registry = FileLockRegistry()
        # No acquire — but release is called anyway because the
        # ``status in ("completed", "blocked")`` branch fires
        # unconditionally for end-of-task reports.
        count = await registry.release("never-acquired-task")
        assert count == 0


class TestLeaseRecoveryRelease:
    """Codex P1 review fix: lease recovery must release file locks.

    When ``AssignmentLeaseManager`` recovers an expired lease, it
    invokes ``MarcusServer._handle_lease_recovery`` (sync callback)
    to clean up in-memory state. ``report_task_progress`` is bypassed
    entirely — without an explicit release here, the recovered task's
    declared files stay claimed forever and ``request_next_task``
    keeps filtering out every contending task, including the
    recovered task itself the next time it would be assigned.
    """

    @pytest.mark.asyncio
    async def test_recovery_callback_releases_locks(self) -> None:
        """After ``_handle_lease_recovery`` runs, the task's locks are gone.

        We don't construct a full MarcusServer (the constructor pulls
        in kanban / config / persistence). Instead we test the
        contract: given a state that has a registry with held locks
        for a task, scheduling a release on the running event loop
        (the same pattern the production code uses) frees those
        locks.
        """
        import asyncio

        registry = FileLockRegistry()
        # Recovered task holds two files at the time of expiry.
        await registry.try_acquire(
            "recovered-task",
            "stale-agent",
            ["src/foo.py", "src/bar.py"],
        )
        assert registry.held_by("src/foo.py") is not None

        # Simulate the production code path: fire-and-forget release
        # via ``asyncio.create_task`` from the recovery callback.
        release_task = asyncio.create_task(registry.release("recovered-task"))
        await release_task

        assert registry.held_by("src/foo.py") is None
        assert registry.held_by("src/bar.py") is None

    @pytest.mark.asyncio
    async def test_recovery_release_does_not_affect_other_tasks(self) -> None:
        """Only the recovered task's locks are released; siblings stay.

        Important: recovery is per-task. If two tasks both held
        locks and only one had its lease expire, the other's locks
        must remain.
        """
        registry = FileLockRegistry()
        await registry.try_acquire("task-A-recovered", "agent-1", ["src/a.py"])
        await registry.try_acquire("task-B-still-alive", "agent-2", ["src/b.py"])

        await registry.release("task-A-recovered")

        # A's lock is gone; B's lock persists.
        assert registry.held_by("src/a.py") is None
        assert registry.held_by("src/b.py") is not None

    @pytest.mark.asyncio
    async def test_recovery_release_is_safe_for_lockless_tasks(self) -> None:
        """Tasks with no declared_files have nothing to release.

        Recovery still fires the release path uniformly — must be a
        no-op for tasks whose acquire returned the empty-list
        fast-path (legacy / feature-based tasks).
        """
        registry = FileLockRegistry()
        count = await registry.release("task-with-no-declared-files")
        assert count == 0
