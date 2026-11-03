"""
Unit tests for parallel SQLite experiment support.

Verifies that run_comparison_experiment.py generates correct per-instance
configuration for SQLite parallel runs (db_path instead of board_id),
that the correct environment variables are passed to subprocesses, and
that concurrent create_project calls are serialized via asyncio.Lock to
prevent the 807s stall caused by concurrent kanban_client mutation.
"""

import asyncio

import pytest

pytestmark = pytest.mark.unit


class TestDefaultParallelInstancesUseSQLite:
    """Default parallel instance config must use db_path, not board_id."""

    def _make_runner(self, max_parallel: int = 3) -> object:
        """Construct an ExperimentRunner in parallel mode with no explicit instances."""
        import sys
        from pathlib import Path
        from unittest.mock import patch

        sys.path.insert(
            0,
            str(
                Path(__file__).parent.parent.parent.parent
                / "dev-tools"
                / "experiments"
                / "runners"
            ),
        )

        # Patch Path.mkdir to avoid filesystem side effects
        with patch("pathlib.Path.mkdir"):
            from run_comparison_experiment import ExperimentRunner

            runner = ExperimentRunner(
                base_dir=Path("/tmp/test_projects"),  # nosec B108
                results_dir=Path("/tmp/test_results"),  # nosec B108
                parallel=True,
                max_parallel=max_parallel,
                marcus_instances=None,
            )
        return runner

    def test_default_instances_have_db_path(self) -> None:
        """Each default instance must include db_path for SQLite isolation."""
        runner = self._make_runner(max_parallel=3)
        for instance in runner.marcus_instances:
            assert "db_path" in instance, (
                f"Instance {instance} missing db_path — parallel SQLite "
                "experiments need per-instance DB files"
            )

    def test_default_instances_do_not_have_board_id(self) -> None:
        """board_id is Planka-only and must not appear in default SQLite config."""
        runner = self._make_runner(max_parallel=3)
        for instance in runner.marcus_instances:
            assert "board_id" not in instance, (
                f"Instance {instance} still uses board_id (Planka) — "
                "default should be SQLite db_path"
            )

    def test_default_instances_have_unique_db_paths(self) -> None:
        """Each instance must get a distinct DB file to avoid write contention."""
        runner = self._make_runner(max_parallel=3)
        db_paths = [inst["db_path"] for inst in runner.marcus_instances]
        assert len(db_paths) == len(
            set(db_paths)
        ), "Parallel instances share a db_path — writes will collide"

    def test_default_instances_have_unique_ports(self) -> None:
        """Each instance must use a different port."""
        runner = self._make_runner(max_parallel=3)
        urls = [inst["url"] for inst in runner.marcus_instances]
        assert len(urls) == len(set(urls)), "Parallel instances share a URL/port"

    def test_correct_number_of_instances(self) -> None:
        """Runner creates exactly max_parallel instances."""
        runner = self._make_runner(max_parallel=5)
        assert len(runner.marcus_instances) == 5


class TestEnvVarPassingToSubprocess:
    """Subprocess env vars must carry SQLite path and Marcus URL per instance."""

    def _build_env(self, instance: dict) -> dict:
        """Simulate what run_experiment() builds for env vars."""
        import os

        env = dict(os.environ)
        env["MARCUS_URL"] = instance["url"]
        if "db_path" in instance:
            env["SQLITE_KANBAN_DB_PATH"] = instance["db_path"]
        elif "board_id" in instance:
            env["MARCUS_BOARD_ID"] = instance["board_id"]
        return env

    def test_sqlite_kanban_env_var_set_when_db_path_present(self) -> None:
        """SQLITE_KANBAN_DB_PATH must be set when instance has db_path."""
        instance = {"url": "http://localhost:4299/mcp", "db_path": "./data/exp_1.db"}
        env = self._build_env(instance)
        assert env["SQLITE_KANBAN_DB_PATH"] == "./data/exp_1.db"

    def test_marcus_url_set_from_instance(self) -> None:
        """MARCUS_URL must carry the instance-specific URL."""
        instance = {"url": "http://localhost:4299/mcp", "db_path": "./data/exp_1.db"}
        env = self._build_env(instance)
        assert env["MARCUS_URL"] == "http://localhost:4299/mcp"

    def test_planka_board_id_still_works_for_backward_compat(self) -> None:
        """Instances with board_id (Planka) must still set MARCUS_BOARD_ID."""
        instance = {
            "url": "http://localhost:4298",
            "board_id": "board_experimental_1",
        }
        env = self._build_env(instance)
        assert env["MARCUS_BOARD_ID"] == "board_experimental_1"
        assert "SQLITE_KANBAN_DB_PATH" not in env

    def test_no_board_id_in_sqlite_env(self) -> None:
        """SQLite instances must not leak MARCUS_BOARD_ID into subprocess env."""
        instance = {"url": "http://localhost:4299/mcp", "db_path": "./data/exp_1.db"}
        env = self._build_env(instance)
        assert "MARCUS_BOARD_ID" not in env


class TestKanbanInitLock:
    """Per-event-loop lock must serialize concurrent kanban_client init.

    Without serialization, three simultaneous create_project calls all
    evaluate need_new_client=True, all call KanbanFactory.create
    concurrently, and the last one to finish overwrites state.kanban_client
    — leaving the first two holding references to orphaned, disconnected
    clients. The dedup guard then detects the stalled call as "still
    running" and loops for ~720s.

    The lock is scoped per event loop via EventLoopLockManager because
    HTTP transport may run requests on different event loops; a module-
    level asyncio.Lock would bind to the first loop and raise "is bound
    to a different event loop" on the second.
    """

    def test_kanban_init_lock_manager_exists_at_module_level(self) -> None:
        """_kanban_init_lock_manager must be an EventLoopLockManager."""
        from src.core.event_loop_utils import EventLoopLockManager
        from src.marcus_mcp.tools import nlp

        assert hasattr(nlp, "_kanban_init_lock_manager"), (
            "_kanban_init_lock_manager not found in nlp module — concurrent "
            "create_project calls will corrupt state.kanban_client"
        )
        assert isinstance(nlp._kanban_init_lock_manager, EventLoopLockManager), (
            "_kanban_init_lock_manager must be an EventLoopLockManager so "
            "each event loop gets its own lock"
        )

    @pytest.mark.asyncio
    async def test_lock_serializes_concurrent_access(self) -> None:
        """Lock must prevent concurrent entry — no interleaving of start/end."""
        from src.marcus_mcp.tools.nlp import _kanban_init_lock_manager

        lock = _kanban_init_lock_manager.get_lock()
        results: list[str] = []

        async def acquire_and_work() -> None:
            async with lock:
                results.append("start")
                await asyncio.sleep(0.01)
                results.append("end")

        await asyncio.gather(*[acquire_and_work() for _ in range(3)])

        # Correct serialization: start,end,start,end,start,end
        # Concurrent (broken): start,start,end,end,...
        for i in range(0, len(results), 2):
            assert results[i] == "start", f"Expected 'start' at index {i}: {results}"
            assert (
                results[i + 1] == "end"
            ), f"Lock not serializing — interleaved access detected: {results}"

    def test_distinct_locks_per_event_loop(self) -> None:
        """Each event loop must get its own lock object (no cross-loop binding)."""
        from src.marcus_mcp.tools.nlp import _kanban_init_lock_manager

        async def fetch_lock() -> int:
            # Return id() of the lock returned in this loop
            return id(_kanban_init_lock_manager.get_lock())

        # Run twice in two separate event loops via asyncio.run, which
        # creates and tears down a fresh loop each call.
        lock_id_loop_a = asyncio.run(fetch_lock())
        lock_id_loop_b = asyncio.run(fetch_lock())

        # Different loops → different lock instances. If the manager
        # returned the same lock, asyncio would raise "bound to a
        # different event loop" when the second loop tried to acquire.
        assert lock_id_loop_a != lock_id_loop_b, (
            "EventLoopLockManager returned the same lock object for two "
            "different event loops — this would cause cross-loop binding "
            "errors under HTTP transport"
        )
