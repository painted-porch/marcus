"""
Unit tests for registrations surviving a Marcus restart (ADR-0012 O1).

Leases were always rehydrated from disk; ``agent_status`` and
``agent_project_map`` never were, so a restart under live workers turned
each of them into an unknown agent whose every poll failed. These tests
pin the lazy rehydration (the pre-dispatch hook on both transports and
the explicit call in ``request_next_task``), the TTL eviction that keeps
the roster from inflating forever, and idempotent re-registration.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus, WorkerStatus
from src.core.registration_persistence import (
    RegistrationPersistence,
    record_from_worker,
)
from src.marcus_mcp.heartbeat import (
    HeartbeatFastMCP,
    rehydrate_on_tool_call,
    touch_on_tool_call,
)
from src.marcus_mcp.server import MarcusServer

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _task(task_id: str) -> Task:
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=TaskStatus.IN_PROGRESS,
        priority=Priority.MEDIUM,
        labels=[],
        dependencies=[],
        estimated_hours=1.0,
        assigned_to=None,
        created_at=NOW,
        updated_at=NOW,
        due_date=None,
        project_id="proj-1",
    )


def _worker(agent_id: str, completed: int = 0) -> WorkerStatus:
    return WorkerStatus(
        worker_id=agent_id,
        name=agent_id.title(),
        role="Backend Developer",
        email=None,
        current_tasks=[],
        completed_tasks_count=completed,
        capacity=40,
        skills=["python"],
        availability={"monday": True},
        performance_score=1.0,
        vendor="anthropic",
        principal="agent",
    )


async def _seed(
    store: RegistrationPersistence,
    agent_id: str,
    project_id: str = "proj-1",
    last_seen: datetime = NOW,
    completed: int = 0,
) -> None:
    await store.save(
        record_from_worker(
            _worker(agent_id, completed),
            project_id=project_id,
            registered_at=last_seen,
            last_seen=last_seen,
        )
    )


def _fake_server(
    tmp_path: Path,
    *,
    leases: Optional[Dict[str, str]] = None,
    tasks: Optional[List[Task]] = None,
    ttl_hours: float = 24.0,
) -> Mock:
    """A server stand-in carrying exactly what rehydration touches.

    Parameters
    ----------
    leases : Optional[Dict[str, str]]
        ``task_id -> agent_id`` of live leases.
    """
    fake = Mock()
    fake.agent_status = {}
    fake.agent_project_map = {}
    fake.agent_tasks = {}
    fake.project_tasks = list(tasks or [])
    fake.registration_persistence = RegistrationPersistence(tmp_path)
    fake.registration_ttl_hours = ttl_hours
    fake.registration_sweep_interval_seconds = 3600.0
    fake._registrations_loaded = False
    fake._registrations_swept_at = None
    fake.ensure_lease_monitor_running = AsyncMock()
    fake.lease_manager = Mock()
    fake.lease_manager.active_leases = {}
    for task_id, agent_id in (leases or {}).items():
        lease = Mock()
        lease.agent_id = agent_id
        lease.task_id = task_id
        fake.lease_manager.active_leases[task_id] = lease
    # Bind the real methods under test.
    fake.ensure_registrations_loaded = MarcusServer.ensure_registrations_loaded.__get__(
        fake, MarcusServer
    )
    fake.evict_stale_registrations = MarcusServer.evict_stale_registrations.__get__(
        fake, MarcusServer
    )
    fake._attach_live_leases_to_registrations = (
        MarcusServer._attach_live_leases_to_registrations.__get__(fake, MarcusServer)
    )
    return fake


class TestRehydration:
    """The roster comes back from disk on the first tool call after a restart."""

    @pytest.mark.asyncio
    async def test_registrations_and_project_map_come_back(
        self, tmp_path: Path
    ) -> None:
        """Both in-memory maps are rebuilt from registrations.json."""
        store = RegistrationPersistence(tmp_path)
        await _seed(store, "agent-a", project_id="proj-1", completed=2)
        await _seed(store, "agent-b", project_id="proj-2")
        server = _fake_server(tmp_path)  # a fresh process: empty maps

        await server.ensure_registrations_loaded(now=NOW)

        assert set(server.agent_status) == {"agent-a", "agent-b"}
        assert server.agent_status["agent-a"].completed_tasks_count == 2
        assert server.agent_status["agent-a"].vendor == "anthropic"
        assert server.agent_project_map == {"agent-a": "proj-1", "agent-b": "proj-2"}

    @pytest.mark.asyncio
    async def test_second_call_is_a_no_op(self, tmp_path: Path) -> None:
        """Rehydration happens once; later calls do not re-read the file."""
        store = RegistrationPersistence(tmp_path)
        await _seed(store, "agent-a")
        server = _fake_server(tmp_path)
        await server.ensure_registrations_loaded(now=NOW)

        await _seed(RegistrationPersistence(tmp_path), "agent-late")
        await server.ensure_registrations_loaded(now=NOW + timedelta(seconds=5))

        assert "agent-late" not in server.agent_status

    @pytest.mark.asyncio
    async def test_memory_wins_over_disk(self, tmp_path: Path) -> None:
        """A worker that re-registered after the restart is not overwritten."""
        store = RegistrationPersistence(tmp_path)
        await _seed(store, "agent-a", completed=7)
        server = _fake_server(tmp_path)
        live = _worker("agent-a", completed=9)
        live.role = "re-registered"
        server.agent_status["agent-a"] = live
        server.agent_project_map["agent-a"] = "proj-new"

        await server.ensure_registrations_loaded(now=NOW)

        assert server.agent_status["agent-a"] is live
        assert server.agent_project_map["agent-a"] == "proj-new"

    @pytest.mark.asyncio
    async def test_current_task_is_rebuilt_from_the_live_lease(
        self, tmp_path: Path
    ) -> None:
        """Ownership comes from the lease store, so one-task-per-agent still holds."""
        store = RegistrationPersistence(tmp_path)
        await _seed(store, "agent-a")
        task = _task("task-1")
        server = _fake_server(tmp_path, leases={"task-1": "agent-a"}, tasks=[task])

        await server.ensure_registrations_loaded(now=NOW)

        assert server.agent_status["agent-a"].current_tasks == [task]

    @pytest.mark.asyncio
    async def test_leases_are_loaded_before_ownership_is_attached(
        self, tmp_path: Path
    ) -> None:
        """Over HTTP the persisted leases are read only when the monitor starts.

        The hook must start it first, or it attaches against an empty lease
        table and sweeps with no holders known (Codex P1/P2 on PR #772).
        """
        store = RegistrationPersistence(tmp_path)
        await _seed(store, "agent-a")
        task = _task("task-1")
        server = _fake_server(tmp_path, tasks=[task])

        async def _start_monitor() -> None:
            lease = Mock()
            lease.agent_id = "agent-a"
            server.lease_manager.active_leases["task-1"] = lease

        server.ensure_lease_monitor_running = AsyncMock(side_effect=_start_monitor)

        await server.ensure_registrations_loaded(now=NOW)

        server.ensure_lease_monitor_running.assert_awaited_once()
        assert server.agent_status["agent-a"].current_tasks == [task]

    @pytest.mark.asyncio
    async def test_ownership_is_reconciled_again_once_tasks_are_loaded(
        self, tmp_path: Path
    ) -> None:
        """project_tasks arrives after the first hook; the next call attaches."""
        store = RegistrationPersistence(tmp_path)
        await _seed(store, "agent-a")
        server = _fake_server(tmp_path, leases={"task-1": "agent-a"})  # no tasks yet
        await server.ensure_registrations_loaded(now=NOW)
        assert server.agent_status["agent-a"].current_tasks == []

        task = _task("task-1")
        server.project_tasks = [task]  # refresh_project_state ran
        await server.ensure_registrations_loaded(now=NOW + timedelta(seconds=1))

        assert server.agent_status["agent-a"].current_tasks == [task]

    @pytest.mark.asyncio
    async def test_lease_on_an_unknown_task_leaves_current_tasks_empty(
        self, tmp_path: Path
    ) -> None:
        """A lease whose task is not in project_tasks is tolerated."""
        store = RegistrationPersistence(tmp_path)
        await _seed(store, "agent-a")
        server = _fake_server(tmp_path, leases={"task-gone": "agent-a"})

        await server.ensure_registrations_loaded(now=NOW)

        assert server.agent_status["agent-a"].current_tasks == []

    @pytest.mark.asyncio
    async def test_no_store_is_a_no_op(self, tmp_path: Path) -> None:
        """A deployment without the store (or a failed init) just skips."""
        server = _fake_server(tmp_path)
        server.registration_persistence = None

        await server.ensure_registrations_loaded(now=NOW)

        assert server.agent_status == {}


class TestEviction:
    """A roster that only grows is as wrong as one that empties on restart."""

    @pytest.mark.asyncio
    async def test_stale_registrations_are_evicted_at_load(
        self, tmp_path: Path
    ) -> None:
        """Silent for longer than the TTL: gone from memory and from disk."""
        store = RegistrationPersistence(tmp_path)
        await _seed(store, "agent-fresh", last_seen=NOW - timedelta(hours=1))
        await _seed(store, "agent-stale", last_seen=NOW - timedelta(hours=25))
        server = _fake_server(tmp_path, ttl_hours=24.0)

        await server.ensure_registrations_loaded(now=NOW)

        assert set(server.agent_status) == {"agent-fresh"}
        assert "agent-stale" not in server.agent_project_map
        assert set(await RegistrationPersistence(tmp_path).load()) == {"agent-fresh"}

    @pytest.mark.asyncio
    async def test_a_live_lease_holder_is_never_evicted(self, tmp_path: Path) -> None:
        """Holding a task means alive, whatever the clock says."""
        store = RegistrationPersistence(tmp_path)
        await _seed(store, "agent-holder", last_seen=NOW - timedelta(days=3))
        server = _fake_server(tmp_path, leases={"task-1": "agent-holder"})

        await server.ensure_registrations_loaded(now=NOW)

        assert "agent-holder" in server.agent_status

    @pytest.mark.asyncio
    async def test_sweep_repeats_on_the_hook_after_the_interval(
        self, tmp_path: Path
    ) -> None:
        """Workers that go silent after the restart are evicted later, too."""
        store = RegistrationPersistence(tmp_path)
        await _seed(store, "agent-a", last_seen=NOW)
        server = _fake_server(tmp_path, ttl_hours=0.5)  # 30-minute TTL
        await server.ensure_registrations_loaded(now=NOW)  # sweeps at NOW

        # Stale already, but inside the hourly sweep interval: untouched.
        await server.ensure_registrations_loaded(now=NOW + timedelta(minutes=40))
        still_there = "agent-a" in server.agent_status
        # Past the interval: the sweep runs and evicts.
        await server.ensure_registrations_loaded(now=NOW + timedelta(minutes=61))

        assert still_there is True
        assert "agent-a" not in server.agent_status

    @pytest.mark.asyncio
    async def test_sweep_is_deferred_until_the_lease_store_exists(
        self, tmp_path: Path
    ) -> None:
        """No lease manager yet means no way to spot a holder: do not evict."""
        store = RegistrationPersistence(tmp_path)
        await _seed(store, "agent-stale", last_seen=NOW - timedelta(days=2))
        server = _fake_server(tmp_path)
        server.lease_manager = None

        await server.ensure_registrations_loaded(now=NOW)

        assert "agent-stale" in server.agent_status
        assert server._registrations_swept_at is None
        # Once the lease store is there, the next call sweeps.
        server.lease_manager = Mock()
        server.lease_manager.active_leases = {}
        await server.ensure_registrations_loaded(now=NOW + timedelta(seconds=1))
        assert "agent-stale" not in server.agent_status

    @pytest.mark.asyncio
    async def test_evict_returns_the_evicted_ids(self, tmp_path: Path) -> None:
        """The sweep reports what it removed, for the log line."""
        store = RegistrationPersistence(tmp_path)
        await _seed(store, "agent-stale", last_seen=NOW - timedelta(days=2))
        server = _fake_server(tmp_path)
        await server.ensure_registrations_loaded(now=NOW - timedelta(days=2))

        evicted = await server.evict_stale_registrations(now=NOW)

        assert evicted == ["agent-stale"]


class TestHooks:
    """One hook, both transports, plus the explicit call in request_next_task."""

    @pytest.mark.asyncio
    async def test_rehydrate_on_tool_call_awaits_the_server_method(self) -> None:
        """The hook defers to ensure_registrations_loaded."""
        state = Mock()
        state.ensure_registrations_loaded = AsyncMock()

        await rehydrate_on_tool_call(state)

        state.ensure_registrations_loaded.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rehydrate_tolerates_a_state_without_the_method(self) -> None:
        """Older state objects and plain Mocks are not an error."""
        await rehydrate_on_tool_call(object())
        await rehydrate_on_tool_call(Mock())  # non-awaitable attribute

    @pytest.mark.asyncio
    async def test_rehydrate_swallows_errors(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A broken roster must not fail the agent's tool call."""
        state = Mock()
        state.ensure_registrations_loaded = AsyncMock(side_effect=OSError("disk"))

        await rehydrate_on_tool_call(state)

        assert any("registration" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_http_call_tool_rehydrates_before_running_the_tool(self) -> None:
        """Over HTTP the roster is loaded before the tool sees the state."""
        order: List[str] = []
        state = Mock()

        async def _ensure() -> None:
            order.append("rehydrate")

        state.ensure_registrations_loaded = _ensure
        state.lease_manager = None
        state.registration_persistence = None

        async def _fake_super_call(self: Any, name: str, arguments: Any) -> Any:
            order.append("tool")
            return {"ok": True}

        with patch("src.marcus_mcp.heartbeat.FastMCP.call_tool", new=_fake_super_call):
            app = HeartbeatFastMCP("marcus-test", state=state)
            await app.call_tool("ping", {"echo": "x"})

        assert order == ["rehydrate", "tool"]

    @pytest.mark.asyncio
    async def test_stdio_dispatch_rehydrates_before_the_tool(self) -> None:
        """The stdio dispatcher runs the same hook before dispatch."""
        from src.marcus_mcp import handlers

        order: List[str] = []

        async def _rehydrate(state: Any) -> None:
            order.append("rehydrate")

        async def _ping(echo: str, state: Any) -> Dict[str, Any]:
            order.append("tool")
            return {"status": "ok", "echo": echo}

        state = Mock(spec=[])  # attribute-poor: nothing to mistake for config
        with (
            patch.object(handlers, "rehydrate_on_tool_call", new=_rehydrate),
            patch.object(handlers, "ping", new=_ping),
            patch.object(handlers, "get_client_tools", return_value=["ping"]),
            patch.object(handlers, "touch_on_tool_call", new=AsyncMock()),
        ):
            await handlers.handle_tool_call("ping", {"echo": "x"}, state)

        assert order == ["rehydrate", "tool"]

    @pytest.mark.asyncio
    async def test_request_next_task_rehydrates_first(self) -> None:
        """The ADR's lazy path: the first poll after a restart finds the roster."""
        from src.marcus_mcp.tools import task as task_tools

        state = Mock()
        state.ensure_lease_monitor_running = AsyncMock()
        state.ensure_registrations_loaded = AsyncMock()
        state.agent_status = {}
        state.agent_project_map = {}
        state.agent_tasks = {}
        state.initialize_kanban = AsyncMock()
        state.refresh_project_state = AsyncMock()
        state.log_event = Mock()
        state.lease_manager = None

        order: List[str] = []
        state.refresh_project_state = AsyncMock(
            side_effect=lambda: order.append("refresh")
        )
        state.ensure_registrations_loaded = AsyncMock(
            side_effect=lambda: order.append("rehydrate")
        )

        with patch.object(
            task_tools, "get_project_board_context", new=AsyncMock(return_value={})
        ):
            await task_tools.request_next_task("agent-a", state)

        state.ensure_registrations_loaded.assert_awaited_once()
        # After the board is loaded, so the reconciliation has tasks to attach.
        assert order == ["refresh", "rehydrate"]

    @pytest.mark.asyncio
    async def test_touch_on_tool_call_also_touches_the_registration(self) -> None:
        """Every tool call moves last_seen, so a live worker is never evicted."""
        state = Mock()
        state.lease_manager = Mock()
        state.lease_manager.touch_lease = AsyncMock(return_value=True)
        state.registration_persistence = Mock()
        state.registration_persistence.touch = AsyncMock(return_value=False)

        await touch_on_tool_call({"agent_id": "agent-a"}, state)

        state.registration_persistence.touch.assert_awaited_once_with("agent-a")


class TestRegisterAgentPersists:
    """register_agent writes the roster and re-registration is idempotent."""

    def _state(self, tmp_path: Path) -> Mock:
        state = Mock()
        state.agent_status = {}
        state.agent_project_map = {}
        state.agent_tasks = {}
        state.log_event = Mock()
        state.registration_persistence = RegistrationPersistence(tmp_path)
        return state

    @pytest.mark.asyncio
    async def test_registration_is_written_to_disk(self, tmp_path: Path) -> None:
        """What register_agent was told is on disk when it returns."""
        from src.marcus_mcp.tools.agent import register_agent

        state = self._state(tmp_path)
        with patch(
            "src.experiments.live_experiment_monitor.get_active_monitor",
            return_value=None,
        ):
            result = await register_agent(
                agent_id="agent-a",
                name="Alice",
                role="Backend Developer",
                skills=["python"],
                state=state,
                project_id="proj-1",
                vendor="anthropic",
            )

        assert result["success"] is True
        on_disk = await RegistrationPersistence(tmp_path).load()
        assert on_disk["agent-a"]["project_id"] == "proj-1"
        assert on_disk["agent-a"]["vendor"] == "anthropic"
        assert on_disk["agent-a"]["skills"] == ["python"]
        assert on_disk["agent-a"]["registered_at"]
        assert on_disk["agent-a"]["last_seen"]

    @pytest.mark.asyncio
    async def test_re_registration_keeps_current_task_and_counter(
        self, tmp_path: Path
    ) -> None:
        """A harness that re-registers mid-chore does not lose its task."""
        from src.marcus_mcp.tools.agent import register_agent

        state = self._state(tmp_path)
        existing = _worker("agent-a", completed=5)
        task = _task("task-1")
        existing.current_tasks = [task]
        state.agent_status["agent-a"] = existing
        state.agent_project_map["agent-a"] = "proj-1"

        with patch(
            "src.experiments.live_experiment_monitor.get_active_monitor",
            return_value=None,
        ):
            await register_agent(
                agent_id="agent-a",
                name="Alice",
                role="Backend Developer",
                skills=["python", "sql"],
                state=state,
                project_id="proj-1",
            )

        refreshed = state.agent_status["agent-a"]
        assert refreshed.current_tasks == [task]
        assert refreshed.completed_tasks_count == 5
        assert refreshed.skills == ["python", "sql"]
        on_disk = await RegistrationPersistence(tmp_path).load()
        assert on_disk["agent-a"]["completed_tasks_count"] == 5

    @pytest.mark.asyncio
    async def test_persistence_failure_does_not_fail_registration(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A disk hiccup is logged; the worker still starts."""
        from src.marcus_mcp.tools.agent import register_agent

        state = self._state(tmp_path)
        state.registration_persistence = Mock()
        state.registration_persistence.save = AsyncMock(side_effect=OSError("disk"))

        with patch(
            "src.experiments.live_experiment_monitor.get_active_monitor",
            return_value=None,
        ):
            result = await register_agent(
                agent_id="agent-a",
                name="Alice",
                role="dev",
                skills=[],
                state=state,
                project_id="proj-1",
            )

        assert result["success"] is True
        assert "agent-a" in state.agent_status
        assert any("registration" in r.getMessage().lower() for r in caplog.records)


class TestCompletionCounterPersists:
    """completed_tasks_count survives a restart like the rest of the record."""

    @pytest.mark.asyncio
    async def test_record_task_completion_increments_and_persists(
        self, tmp_path: Path
    ) -> None:
        """The counter bump reaches the roster file."""
        from src.marcus_mcp.tools.task import _record_task_completion

        state = Mock()
        state.agent_status = {"agent-a": _worker("agent-a", completed=1)}
        state.registration_persistence = RegistrationPersistence(tmp_path)
        await _seed(state.registration_persistence, "agent-a", completed=1)

        await _record_task_completion(state, "agent-a")

        assert state.agent_status["agent-a"].completed_tasks_count == 2
        on_disk = await RegistrationPersistence(tmp_path).load()
        assert on_disk["agent-a"]["completed_tasks_count"] == 2

    @pytest.mark.asyncio
    async def test_record_task_completion_tolerates_unknown_agent_and_no_store(
        self,
    ) -> None:
        """Neither an unregistered agent nor a missing store is an error."""
        from src.marcus_mcp.tools.task import _record_task_completion

        state = Mock()
        state.agent_status = {}
        state.registration_persistence = None

        await _record_task_completion(state, "ghost")
