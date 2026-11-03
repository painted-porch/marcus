"""
Unit tests for dependency inference staying off the event loop and bounded.

In the 2026-11-02 calculator run the first request_next_task after a
restart ran hybrid dependency inference over 834 tasks. The pattern and
ambiguity phases are pure CPU (pairwise regex matching) but ran on the
asyncio loop, so the server sat at 99% CPU for seven minutes, logged
nothing, and accepted no HTTP connections; every agent timed out. These
tests pin three guards: the CPU phases run in a worker thread, a task
count above the cap skips implicit inference, and a wall-clock budget
falls back to explicit dependencies only. Simon thought 25f0bcc3.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import List
from unittest.mock import Mock

import pytest

from src.config.hybrid_inference_config import HybridInferenceConfig
from src.core.models import Priority, Task, TaskStatus
from src.intelligence.dependency_inferer_hybrid import HybridDependencyInferer

pytestmark = pytest.mark.unit


def _task(task_id: str, name: str, deps: List[str] | None = None) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description="",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        labels=["auth"],
        dependencies=deps or [],
    )


def _tasks(n: int) -> List[Task]:
    return [_task(str(i), f"Implement feature {i}") for i in range(n)]


def _pattern_only(**overrides: object) -> HybridDependencyInferer:
    config = HybridInferenceConfig(enable_ai_inference=False, **overrides)  # type: ignore[arg-type]
    return HybridDependencyInferer(None, config)


class TestOffLoop:
    """The CPU phases must not block the event loop."""

    @pytest.mark.asyncio
    async def test_pattern_phase_runs_in_a_worker_thread(self) -> None:
        """A slow synchronous pattern phase does not stall a concurrent coroutine."""
        inferer = _pattern_only()

        def _slow_patterns(tasks: List[Task]) -> dict:
            time.sleep(0.3)
            return {}

        inferer._pattern_dependencies_sync = _slow_patterns  # type: ignore[method-assign]
        ticks = 0
        stop = False

        async def _ticker() -> None:
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.01)

        ticker = asyncio.create_task(_ticker())
        await inferer.infer_dependencies(_tasks(3))
        stop = True
        await ticker

        assert ticks >= 10, f"loop starved: only {ticks} ticks during a 300 ms phase"

    @pytest.mark.asyncio
    async def test_ambiguity_phase_runs_in_a_worker_thread(self) -> None:
        """Same guarantee for the second CPU phase."""
        inferer = _pattern_only()

        def _slow_pairs(tasks: List[Task], pattern_deps: dict) -> list:
            time.sleep(0.3)
            return []

        inferer._ambiguous_pairs_sync = _slow_pairs  # type: ignore[method-assign]
        ticks = 0
        stop = False

        async def _ticker() -> None:
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.01)

        ticker = asyncio.create_task(_ticker())
        await inferer.infer_dependencies(_tasks(3))
        stop = True
        await ticker

        assert ticks >= 10

    @pytest.mark.asyncio
    async def test_async_wrappers_still_work(self) -> None:
        """Callers of the old async phase methods get the same results."""
        inferer = _pattern_only()
        tasks = [_task("1", "Design auth"), _task("2", "Implement auth")]

        pattern = await inferer._get_pattern_dependencies(tasks)
        pairs = await inferer._identify_ambiguous_pairs(tasks, pattern)

        assert isinstance(pattern, dict)
        assert isinstance(pairs, list)


class TestBounds:
    """Inference over an oversized or slow graph degrades to explicit dependencies."""

    @pytest.mark.asyncio
    async def test_task_count_above_the_cap_skips_implicit_inference(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Above the cap nothing pairwise runs; the graph carries no inferred edges."""
        inferer = _pattern_only(max_tasks_for_inference=3)
        inferer._pattern_dependencies_sync = Mock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not run")
        )

        with caplog.at_level(logging.WARNING):
            graph = await inferer.infer_dependencies(_tasks(4))

        assert graph.edges == []
        assert set(graph.nodes) == {"0", "1", "2", "3"}
        assert any("cap" in r.getMessage().lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_at_the_cap_inference_still_runs(self) -> None:
        """The cap is inclusive."""
        inferer = _pattern_only(max_tasks_for_inference=3)
        called: list = []
        inferer._pattern_dependencies_sync = lambda tasks: called.append(len(tasks)) or {}  # type: ignore[method-assign]

        await inferer.infer_dependencies(_tasks(3))

        assert called == [3]

    @pytest.mark.asyncio
    async def test_time_budget_exceeded_falls_back_to_explicit_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A phase that outlives the budget is abandoned; the caller gets explicit deps."""
        inferer = _pattern_only(inference_time_budget_seconds=0.05)

        def _slow_patterns(tasks: List[Task]) -> dict:
            time.sleep(0.3)
            return {}

        inferer._pattern_dependencies_sync = _slow_patterns  # type: ignore[method-assign]

        started = time.monotonic()
        with caplog.at_level(logging.WARNING):
            graph = await inferer.infer_dependencies(_tasks(3))
        elapsed = time.monotonic() - started

        assert graph.edges == []
        assert elapsed < 0.25, f"did not give up at the budget: {elapsed:.2f}s"
        assert any("budget" in r.getMessage().lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_small_graph_still_infers_patterns(self) -> None:
        """The guards do not change results for an ordinary board."""
        inferer = _pattern_only()
        tasks = [
            _task("1", "Design User Authentication"),
            _task("2", "Implement User Login"),
            _task("3", "Test Authentication Flow"),
        ]

        graph = await inferer.infer_dependencies(tasks)

        assert len(graph.edges) >= 1


class TestConfig:
    """The two new knobs validate and round-trip like the others."""

    def test_defaults(self) -> None:
        """Shipped defaults: 200 tasks, 20 seconds."""
        config = HybridInferenceConfig()
        assert config.max_tasks_for_inference == 200
        assert config.inference_time_budget_seconds == 20.0

    def test_validate_rejects_bad_values(self) -> None:
        """A zero cap or a non-positive budget is a configuration error."""
        with pytest.raises(ValueError):
            HybridInferenceConfig(max_tasks_for_inference=0).validate()
        with pytest.raises(ValueError):
            HybridInferenceConfig(inference_time_budget_seconds=0).validate()

    def test_round_trip(self) -> None:
        """to_dict / from_dict carry the new keys."""
        config = HybridInferenceConfig(
            max_tasks_for_inference=50, inference_time_budget_seconds=5.0
        )
        again = HybridInferenceConfig.from_dict(config.to_dict())
        assert again.max_tasks_for_inference == 50
        assert again.inference_time_budget_seconds == 5.0


class TestScopedInputAtTheRequestSite:
    """request_next_task analyses the agent's project, never the whole file."""

    def test_only_the_agents_project_and_legacy_tasks_are_analysed(self) -> None:
        """Other projects' tasks are left out; project_id None is shared."""
        from src.marcus_mcp.tools.task import _tasks_for_dependency_analysis

        mine = _task("a", "Mine")
        mine.project_id = "proj-1"
        other = _task("b", "Other")
        other.project_id = "proj-2"
        legacy = _task("c", "Legacy")
        legacy.project_id = None
        state = Mock()
        state.project_tasks = [mine, other, legacy]
        state.agent_project_map = {"agent-1": "proj-1"}

        scoped = _tasks_for_dependency_analysis(state, "agent-1")

        assert [t.id for t in scoped] == ["a", "c"]

    def test_unregistered_agent_gets_the_whole_list(self) -> None:
        """With no project to scope by, behaviour is unchanged."""
        from src.marcus_mcp.tools.task import _tasks_for_dependency_analysis

        tasks = [_task("a", "A"), _task("b", "B")]
        state = Mock()
        state.project_tasks = tasks
        state.agent_project_map = {}

        assert _tasks_for_dependency_analysis(state, "ghost") == tasks
