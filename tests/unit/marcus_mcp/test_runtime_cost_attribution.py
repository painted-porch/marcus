"""
Unit tests for run-time planner cost attribution (#768).

Run-time planner LLM calls — generating a task's instructions, analyzing
a blocker, validating completed work — must record their token usage to
the cost store attributed to the active project and the run that
``create_project`` opened at setup time. The attribution is resolved by
``src.marcus_mcp.tools.task._planner_cost_context``, a shared helper that
builds the ``PlannerContext`` the three run-time call sites push around
their AI calls.

These tests mock every external dependency (project registry, cost
store) and assert the resolved context, never touching the network or a
real database other than a throwaway temp SQLite file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest

pytestmark = pytest.mark.unit

from src.cost_tracking.cost_recorder import get_recorder, set_recorder
from src.cost_tracking.cost_store import CostStore, Run
from src.marcus_mcp.tools.task import _planner_cost_context


@pytest.fixture
def cost_store(tmp_path: Any) -> CostStore:
    """A throwaway on-disk CostStore for run lookups."""
    store = CostStore(db_path=tmp_path / "costs.db")
    store.load_seed_prices()
    return store


@pytest.fixture(autouse=True)
def _reset_recorder() -> Any:
    """Reset the module recorder singleton after each test."""
    yield
    set_recorder(None)


def _make_task(project_id: Optional[str], task_id: str = "T1") -> Any:
    """Build a minimal task-like object carrying a project_id."""
    return SimpleNamespace(id=task_id, project_id=project_id)


def _make_state(
    *,
    cost_store: Optional[CostStore] = None,
    agent_project_map: Optional[dict] = None,
    active_project_id: Optional[str] = None,
) -> Any:
    """Build a fake Marcus server state with the attribution inputs.

    ``active_project_id`` is exposed as ``selected_project_id``, the field
    the shared cost resolver reads for the active-project fallback.
    """
    return SimpleNamespace(
        cost_store=cost_store,
        agent_project_map=agent_project_map or {},
        selected_project_id=active_project_id,
        current_project_id=None,
    )


class TestPlannerCostContext:
    """``_planner_cost_context`` resolves (project_id, run_id) for attribution."""

    @pytest.mark.asyncio
    async def test_attributes_to_task_project_and_open_setup_run(
        self, cost_store: CostStore
    ) -> None:
        """Project comes from the task; run_id is the project's open run."""
        cost_store.record_run(
            Run(
                run_id="setup_run",
                project_id="projhex",
                project_name="Proj",
                started_at=datetime(2026, 11, 1, 10, tzinfo=timezone.utc),
            )
        )
        state = _make_state(cost_store=cost_store)
        task = _make_task("projhex", task_id="T42")

        cm = await _planner_cost_context(state, task=task, agent_id="agent-1")
        with cm:
            ctx = get_recorder().current()
            assert ctx is not None
            assert ctx.project_id == "projhex"
            assert ctx.run_id == "setup_run"
            assert ctx.task_id == "T42"
            assert ctx.agent_id == "agent-1"

        # Context is popped on exit.
        assert get_recorder().current() is None

    @pytest.mark.asyncio
    async def test_canonicalizes_dashed_project_id(self, cost_store: CostStore) -> None:
        """A dashed project UUID still matches the dashless run row."""
        dashless = "4cca814b47024489a914a9868da9e4fc"
        dashed = "4cca814b-4702-4489-a914-a9868da9e4fc"
        cost_store.record_run(
            Run(
                run_id="run_canon",
                project_id=dashless,
                project_name="Proj",
                started_at=datetime(2026, 11, 1, 10, tzinfo=timezone.utc),
            )
        )
        state = _make_state(cost_store=cost_store)
        task = _make_task(dashed)

        cm = await _planner_cost_context(state, task=task, agent_id="a")
        with cm:
            ctx = get_recorder().current()
            assert ctx is not None
            # PlannerContext normalizes project_id to the dashless form.
            assert ctx.project_id == dashless
            assert ctx.run_id == "run_canon"

    @pytest.mark.asyncio
    async def test_falls_back_to_agent_project_map(self, cost_store: CostStore) -> None:
        """When the task has no project_id, the agent's project is used."""
        cost_store.record_run(
            Run(
                run_id="r1",
                project_id="agentproj",
                project_name="Proj",
                started_at=datetime(2026, 11, 1, 10, tzinfo=timezone.utc),
            )
        )
        state = _make_state(
            cost_store=cost_store,
            agent_project_map={"agent-1": "agentproj"},
        )
        task = _make_task(None)

        cm = await _planner_cost_context(state, task=task, agent_id="agent-1")
        with cm:
            ctx = get_recorder().current()
            assert ctx is not None
            assert ctx.project_id == "agentproj"
            assert ctx.run_id == "r1"

    @pytest.mark.asyncio
    async def test_falls_back_to_active_project(self, cost_store: CostStore) -> None:
        """With neither task nor agent mapping, the active project is used."""
        cost_store.record_run(
            Run(
                run_id="r_active",
                project_id="activeproj",
                project_name="Proj",
                started_at=datetime(2026, 11, 1, 10, tzinfo=timezone.utc),
            )
        )
        state = _make_state(cost_store=cost_store, active_project_id="activeproj")

        cm = await _planner_cost_context(state, task=None, agent_id="ghost")
        with cm:
            ctx = get_recorder().current()
            assert ctx is not None
            assert ctx.project_id == "activeproj"
            assert ctx.run_id == "r_active"

    @pytest.mark.asyncio
    async def test_no_project_yields_no_context(self, cost_store: CostStore) -> None:
        """Unresolvable project → a no-op context, call lands in 'unassigned'."""
        state = _make_state(cost_store=cost_store)
        task = _make_task(None)

        cm = await _planner_cost_context(state, task=task, agent_id="ghost")
        with cm:
            # Nothing pushed — the provider path will fall through to the
            # 'unassigned' bucket, which is the honest default.
            assert get_recorder().current() is None

    @pytest.mark.asyncio
    async def test_project_without_open_run_still_attributes_project(
        self, cost_store: CostStore
    ) -> None:
        """A known project with no open run attributes to project + 'unassigned'.

        The project total stays correct even when the run row cannot be
        resolved (edge case: run already closed).
        """
        state = _make_state(cost_store=cost_store)
        task = _make_task("projnorow")

        cm = await _planner_cost_context(state, task=task, agent_id="a")
        with cm:
            ctx = get_recorder().current()
            assert ctx is not None
            assert ctx.project_id == "projnorow"
            assert ctx.run_id == "unassigned"
