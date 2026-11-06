"""
Unit tests for ``_response_project_for_task`` (issue #849).

``request_next_task``'s response stamps project_id/project_name on the
task it hands out. It used to read the server's active project
unconditionally (the same defect #805 found in ``log_decision`` and K6
names); fixed in #849 to resolve from the task's own ``project_id``.

A plain ``get_project(task_project_id)`` lookup is sufficient for
every provider, including Planka and Linear: issue #854 made every
kanban client carry its owning ``ProjectConfig``'s registry id down
at construction, so a task's ``project_id`` is always a registry key
directly (the same guarantee GH-306 already gave SQLite). PR #853's
interim bridge for the two providers that did not yet have this
guarantee (``get_project_by_provider_id``) is retired; Planka- and
Linear-shaped construction tests now live where the id is actually
stamped (``tests/unit/integrations/test_mcp_kanban_client_simple.py``,
``tests/unit/integrations/test_linear_kanban_project_id.py``,
``tests/unit/integrations/test_planka_registry_project_id.py``,
``tests/unit/core/test_project_context_manager_provider_config.py``).
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.core.persistence import MemoryPersistence, Persistence
from src.core.project_registry import ProjectConfig, ProjectRegistry
from src.marcus_mcp.tools.task import _response_project_for_task

pytestmark = pytest.mark.unit


def _task(project_id: Any = None) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id="task-1",
        name="Implement the login form",
        description="",
        status=TaskStatus.IN_PROGRESS,
        priority=Priority.MEDIUM,
        assigned_to="agent-1",
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        project_id=project_id,
    )


async def _registry() -> ProjectRegistry:
    registry = ProjectRegistry(Persistence(MemoryPersistence()))
    await registry.initialize()
    return registry


class TestSingleProjectServer:
    """The ordinary case: one registered project, GH-306 shaped (a

    sqlite board whose registry id equals the task's own
    project_id). This is the case Codex flagged as regressed if the
    fallback chain is built wrong.
    """

    @pytest.mark.asyncio
    async def test_a_single_sqlite_project_resolves_directly(self) -> None:
        registry = await _registry()
        now = datetime.now(timezone.utc)
        await registry.add_project(
            ProjectConfig(
                id="camp-1",
                name="Camp",
                provider="sqlite",
                provider_config={"db_path": "camp.db"},
                created_at=now,
                last_used=now,
            )
        )
        state = SimpleNamespace(project_registry=registry)

        resolved = await _response_project_for_task(_task("camp-1"), state)

        assert resolved is not None
        assert resolved.id == "camp-1"


class TestLegacyAndMultiProject:
    @pytest.mark.asyncio
    async def test_a_legacy_task_with_no_project_id_falls_back_to_active(
        self,
    ) -> None:
        registry = await _registry()
        now = datetime.now(timezone.utc)
        await registry.add_project(
            ProjectConfig(
                id="camp-1",
                name="Camp",
                provider="sqlite",
                provider_config={},
                created_at=now,
                last_used=now,
            )
        )
        state = SimpleNamespace(project_registry=registry)

        resolved = await _response_project_for_task(_task(None), state)

        assert resolved is not None
        assert resolved.id == "camp-1"

    @pytest.mark.asyncio
    async def test_the_agents_own_project_wins_over_the_active_one(self) -> None:
        """The #849 multi-project regression test: Y active, task on X."""
        registry = await _registry()
        now = datetime.now(timezone.utc)
        await registry.add_project(
            ProjectConfig(
                id="project-y",
                name="Dashboard Rebuild",
                provider="sqlite",
                provider_config={},
                created_at=now,
                last_used=now,
            )
        )
        await registry.add_project(
            ProjectConfig(
                id="project-x",
                name="Payments Rework",
                provider="sqlite",
                provider_config={},
                created_at=now,
                last_used=now,
            )
        )
        state = SimpleNamespace(project_registry=registry)

        resolved = await _response_project_for_task(_task("project-x"), state)

        assert resolved is not None
        assert resolved.id == "project-x"

    @pytest.mark.asyncio
    async def test_no_project_registry_resolves_to_none(self) -> None:
        state = SimpleNamespace()

        assert await _response_project_for_task(_task("x"), state) is None
