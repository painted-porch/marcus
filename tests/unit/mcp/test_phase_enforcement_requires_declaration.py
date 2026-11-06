"""
Unit tests: phase enforcement runs only when a project's K2
registration declares it (issue #852, ADR-0013 K1/K3).

On a Minecraft board with no software-phase declaration, a keyword
classifier built for software task text (``EnhancedTaskClassifier``)
read "and document the target quantity" -- a real sentence, a real
whole-word match on "document" -- as a documentation task, and then
held it behind an unrelated in-progress "food" task because they
shared a label. Nothing in that board's own rules (status, ownership,
dependencies) held either task; Marcus inferred a software phase
order the project never declared.

The classifier's word match was not a substring bug: "document" is a
real standalone word in the task's own description, and
``EnhancedTaskClassifier`` already matches on ``\\b{keyword}s?\\b``
boundaries (verified directly against the real classifier with the
run's own task text). The fix is narrower and more fundamental: phase
enforcement must never run at all on a project that never declared it
wants phase order enforced.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.core.project_registry import ProjectConfig
from src.marcus_mcp.coordinator.task_type_registration import (
    project_declares_phase_order,
)
from src.marcus_mcp.tools.task import apply_board_wide_gates

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 6, 5, 0, tzinfo=timezone.utc)
PROJECT = "minecraft-camp"


def _task(
    task_id: str,
    name: str,
    description: str = "",
    status: TaskStatus = TaskStatus.TODO,
    labels: Optional[List[str]] = None,
) -> Task:
    return Task(
        id=task_id,
        name=name,
        description=description,
        status=status,
        priority=Priority.MEDIUM,
        labels=labels or [],
        dependencies=[],
        estimated_hours=1.0,
        assigned_to=None,
        created_at=NOW,
        updated_at=NOW,
        due_date=None,
        project_id=PROJECT,
    )


def _state(registry_project: Optional[ProjectConfig]) -> Any:
    state = MagicMock()
    if registry_project is not None:
        from src.core.project_registry import ProjectRegistry

        registry = MagicMock(spec=ProjectRegistry)
        registry.get_project_sync = MagicMock(return_value=registry_project)
        registry.get_active_project_sync = MagicMock(return_value=registry_project)
        state.project_registry = registry
    else:
        state.project_registry = None
    return state


def _minecraft_tasks() -> tuple[Task, Task, Task]:
    """The run's own trace shape (issue #852's exact repro)."""
    food_in_progress = _task(
        "food-stock",
        "Stock the camp chest with food",
        labels=["food", "minecraft"],
        status=TaskStatus.IN_PROGRESS,
    )
    food_alt = _task(
        "food-alt",
        "food alternative strategy",
        description=(
            "Provenance: planned from cobblestone.requirement (Cyn_2139). "
            "Decide an alternate food source and document the target "
            "quantity."
        ),
        labels=["food", "minecraft", "planned", "strategy"],
    )
    cobblestone = _task(
        "cobble-confirm",
        "cobblestone quantity confirmation",
        description=(
            "Provenance: planned from cobblestone.requirement (Cyn_2139). "
            "Confirm and document the required cobblestone quantity."
        ),
        labels=["cobblestone", "minecraft", "planned", "requirement"],
    )
    return food_in_progress, food_alt, cobblestone


class TestPhaseEnforcementIsOffByDefault:
    """A project with no K2 phase declaration gets no phase gate."""

    def test_minecraft_board_with_no_declaration_offers_both_tasks(self) -> None:
        """The exact run-1 repro: neither task is held."""
        food_in_progress, food_alt, cobblestone = _minecraft_tasks()
        scoped = [food_in_progress, food_alt, cobblestone]
        tasks_by_id = {t.id: t for t in scoped}
        available = [food_alt, cobblestone]

        registry_project = ProjectConfig(
            id=PROJECT,
            name="Minecraft Camp",
            provider="sqlite",
            provider_config={},
        )  # enforce_phase_order defaults to False
        state = _state(registry_project)

        result = apply_board_wide_gates(
            available, scoped, tasks_by_id, state, project_id=PROJECT
        )

        assert food_alt in result
        assert cobblestone in result

    def test_no_registry_at_all_also_offers_both_tasks(self) -> None:
        """Legacy/no-registry deployments keep today's non-phase behavior."""
        food_in_progress, food_alt, cobblestone = _minecraft_tasks()
        scoped = [food_in_progress, food_alt, cobblestone]
        tasks_by_id = {t.id: t for t in scoped}
        available = [food_alt, cobblestone]

        state = _state(None)

        result = apply_board_wide_gates(
            available, scoped, tasks_by_id, state, project_id=PROJECT
        )

        assert food_alt in result
        assert cobblestone in result

    def test_no_phase_hold_is_recorded_when_enforcement_never_ran(self) -> None:
        """calculate_retry_after_seconds must not surface a phase reason
        that never actually fired."""
        food_in_progress, food_alt, cobblestone = _minecraft_tasks()
        scoped = [food_in_progress, food_alt, cobblestone]
        tasks_by_id = {t.id: t for t in scoped}
        available = [food_alt, cobblestone]

        registry_project = ProjectConfig(
            id=PROJECT, name="Minecraft Camp", provider="sqlite", provider_config={}
        )
        state = _state(registry_project)

        apply_board_wide_gates(
            available, scoped, tasks_by_id, state, project_id=PROJECT
        )

        assert getattr(state, "_last_phase_hold", None) is None


class TestPhaseEnforcementStillWorksWhenDeclared:
    """A project that opts in keeps the exact pre-#852 phase behavior."""

    def test_an_opted_in_software_project_still_holds_the_doc_task(self) -> None:
        """Same shape, but the project declares enforce_phase_order=True:
        the documentation-classified task is held behind the in-progress
        food/implementation task, exactly as before this fix."""
        food_in_progress, food_alt, cobblestone = _minecraft_tasks()
        scoped = [food_in_progress, food_alt, cobblestone]
        tasks_by_id = {t.id: t for t in scoped}
        available = [food_alt, cobblestone]

        registry_project = ProjectConfig(
            id=PROJECT,
            name="Minecraft Camp (opted into phases)",
            provider="sqlite",
            provider_config={},
            enforce_phase_order=True,
        )
        state = _state(registry_project)

        result = apply_board_wide_gates(
            available, scoped, tasks_by_id, state, project_id=PROJECT
        )

        assert food_alt not in result, (
            "with phases declared, the classifier's real whole-word match "
            "on 'document' still applies -- this is the accepted software "
            "convention the project opted into, not a bug."
        )

    def test_the_hold_is_recorded_for_the_retry_reason(self) -> None:
        """Issue #852 point 3: when a hold actually happens, the reason is
        recorded for calculate_retry_after_seconds to surface, instead of
        an unrelated in-progress task's ETA."""
        food_in_progress, food_alt, cobblestone = _minecraft_tasks()
        scoped = [food_in_progress, food_alt, cobblestone]
        tasks_by_id = {t.id: t for t in scoped}
        available = [food_alt, cobblestone]

        registry_project = ProjectConfig(
            id=PROJECT,
            name="Minecraft Camp (opted into phases)",
            provider="sqlite",
            provider_config={},
            enforce_phase_order=True,
        )
        state = _state(registry_project)

        apply_board_wide_gates(
            available, scoped, tasks_by_id, state, project_id=PROJECT
        )

        hold = getattr(state, "_last_phase_hold", None)
        assert hold is not None
        # Both food_alt and cobblestone are held in this fixture; the
        # recorded reason is the latest one evaluated, not a specific
        # task -- it only matters that SOME real hold reason is there,
        # naming an actual task and phase, not a placeholder.
        assert "blocked by in-progress task" in hold["reason"]
        assert hold["task_phase"] is not None
        assert hold["task_name"] in {
            "food alternative strategy",
            "cobblestone quantity confirmation",
        }


class TestProjectDeclaresPhaseOrder:
    """Unit coverage for the K2 lookup helper itself."""

    def test_defaults_to_false_when_never_set(self) -> None:
        project = ProjectConfig(
            id="p1", name="P1", provider="sqlite", provider_config={}
        )
        state = _state(project)

        assert project_declares_phase_order(state, "p1") is False

    def test_true_when_explicitly_declared(self) -> None:
        project = ProjectConfig(
            id="p1",
            name="P1",
            provider="sqlite",
            provider_config={},
            enforce_phase_order=True,
        )
        state = _state(project)

        assert project_declares_phase_order(state, "p1") is True

    def test_false_with_no_project_registry(self) -> None:
        state = _state(None)

        assert project_declares_phase_order(state, "p1") is False

    def test_false_when_the_project_cannot_be_resolved(self) -> None:
        from src.core.project_registry import ProjectRegistry

        registry = MagicMock(spec=ProjectRegistry)
        registry.get_project_sync = MagicMock(return_value=None)
        state = MagicMock()
        state.project_registry = registry

        assert project_declares_phase_order(state, "missing") is False

    def test_falls_back_to_active_project_when_no_project_id(self) -> None:
        project = ProjectConfig(
            id="p1",
            name="P1",
            provider="sqlite",
            provider_config={},
            enforce_phase_order=True,
        )
        state = _state(project)

        assert project_declares_phase_order(state, None) is True


class TestProjectConfigEnforcePhaseOrderField:
    """The new K2 field itself round-trips correctly."""

    def test_defaults_to_false(self) -> None:
        project = ProjectConfig(
            id="p1", name="P1", provider="sqlite", provider_config={}
        )

        assert project.enforce_phase_order is False

    def test_round_trips_through_to_dict_and_from_dict(self) -> None:
        project = ProjectConfig(
            id="p1",
            name="P1",
            provider="sqlite",
            provider_config={},
            enforce_phase_order=True,
        )

        restored = ProjectConfig.from_dict(project.to_dict())

        assert restored.enforce_phase_order is True

    def test_a_legacy_dict_with_no_field_restores_to_false(self) -> None:
        """A project registered before this field existed must not break."""
        project = ProjectConfig(
            id="p1", name="P1", provider="sqlite", provider_config={}
        )
        data: Dict[str, Any] = project.to_dict()
        del data["enforce_phase_order"]

        restored = ProjectConfig.from_dict(data)

        assert restored.enforce_phase_order is False
