"""
Unit tests for ADR-0013 K4's injected-check lookup (issue #847).

``injected_checks_for_task_type`` is the shared read every kernel
call site uses to learn whether a task type has a declared, required
check the worker did not author. The data shape is restricted at
registration (``ProjectRegistry.add_project``, see
``tests/unit/core/test_project_registry.py``); these tests cover the
read side -- what comes back for a registered type, an unregistered
one, and a declared-but-not-enabled one.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from src.core.persistence import MemoryPersistence, Persistence
from src.core.project_registry import ProjectConfig, ProjectRegistry
from src.marcus_mcp.coordinator.task_type_registration import (
    injected_checks_for_task_type,
)

pytestmark = pytest.mark.unit


async def _registry_with(
    task_type_registrations: Dict[str, Dict[str, Any]],
) -> ProjectRegistry:
    """A real, initialized registry holding one project with these registrations."""
    registry = ProjectRegistry(Persistence(MemoryPersistence()))
    await registry.initialize()
    now = datetime.now(timezone.utc)
    await registry.add_project(
        ProjectConfig(
            id="proj-1",
            name="Payments Rework",
            provider="sqlite",
            provider_config={},
            created_at=now,
            last_used=now,
            task_type_registrations=task_type_registrations,
        )
    )
    return registry


class TestInjectedChecksForTaskType:
    @pytest.mark.asyncio
    async def test_a_declared_and_enabled_check_is_returned(self) -> None:
        registry = await _registry_with(
            {
                "verify": {
                    "checks_injectable": True,
                    "injected_checks": [
                        {
                            "name": "cross_check_totals",
                            "description": "Totals must agree",
                        }
                    ],
                }
            }
        )
        state = SimpleNamespace(project_registry=registry)

        result = injected_checks_for_task_type("verify", state, "proj-1")

        assert result == [
            {"name": "cross_check_totals", "description": "Totals must agree"}
        ]

    @pytest.mark.asyncio
    async def test_no_registration_for_the_type_returns_nothing(self) -> None:
        registry = await _registry_with({})
        state = SimpleNamespace(project_registry=registry)

        assert injected_checks_for_task_type("verify", state, "proj-1") == []

    def test_no_project_registry_returns_nothing(self) -> None:
        state = SimpleNamespace()

        assert injected_checks_for_task_type("verify", state, "proj-1") == []

    def test_no_resolvable_project_returns_nothing(self) -> None:
        """A bare Mock-shaped registry with nothing cached: not a
        ``ProjectRegistry`` at all, so the lookup must not raise."""
        state = SimpleNamespace(project_registry=object())

        assert injected_checks_for_task_type("verify", state, "proj-1") == []
