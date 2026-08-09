"""
Unit tests for contract scope annotation (GH-356).

Tests that ``_collect_task_artifacts`` annotates dependency artifacts with
``scope_annotation`` at retrieval time:

- ``in_scope``  — the requesting task owns this domain, implement it completely.
- ``reference_only`` — coordination boundary only, do not implement.

Annotation is contextual: the same artifact is ``in_scope`` for one agent and
``reference_only`` for another, so it cannot be set at generation time.

Annotation rules
----------------
- Contract-first requesting task (has ``domain:X`` label): dep artifacts from
  the same domain → ``in_scope``; dep artifacts from other domains → ``reference_only``.
- Feature-based requesting task (no ``domain:`` labels): all dep artifacts →
  ``reference_only`` (no domain ownership to compare).
- Own-task artifacts (logged for the requesting task itself) receive no annotation.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.context import _collect_task_artifacts

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_task(
    task_id: str,
    name: str,
    labels: List[str] | None = None,
    dependencies: List[str] | None = None,
    responsibility: str | None = None,
    source_type: str | None = None,
) -> Task:
    """Build a minimal Task for annotation testing."""
    return Task(
        id=task_id,
        name=name,
        description=f"Task {name}",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=_now(),
        updated_at=_now(),
        due_date=None,
        estimated_hours=1.0,
        labels=labels or [],
        dependencies=dependencies or [],
        responsibility=responsibility,
        source_type=source_type,
    )


def _make_artifact(
    filename: str, artifact_type: str = "specification"
) -> Dict[str, Any]:
    """Build a minimal artifact dict."""
    return {
        "filename": filename,
        "location": f"docs/{filename}",
        "artifact_type": artifact_type,
        "description": f"Artifact {filename}",
    }


class MockKanbanClient:
    """Kanban client that returns no attachments."""

    async def get_attachments(self, task_id: str) -> Dict[str, Any]:
        """Return empty attachment list."""
        return {"success": True, "data": []}


class MockState:
    """Minimal state for _collect_task_artifacts tests."""

    def __init__(self) -> None:
        self.task_artifacts: Dict[str, List[Dict[str, Any]]] = {}
        self.project_tasks: List[Task] = []
        self.kanban_client: MockKanbanClient = MockKanbanClient()
        self.context = object()  # non-None sentinel


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Contract-first scope annotation
# ---------------------------------------------------------------------------


class TestContractFirstScopeAnnotation:
    """Scope annotation for contract-first requesting tasks (has ``domain:X`` label)."""

    @pytest.mark.asyncio
    async def test_same_domain_dep_is_in_scope(self) -> None:
        """
        Contract-first task depends on its own domain's ghost → ``in_scope``.

        Both the requesting impl task and the design ghost carry the same
        ``domain:weather-widget`` label, so the dep artifact is in-scope.
        """
        # Arrange
        ghost = _make_task(
            "ghost-weather",
            "Design WeatherWidget",
            labels=["design", "auto_completed", "domain:weather-widget"],
            source_type="contract_first_design",
        )
        impl_task = _make_task(
            "impl-weather",
            "Implement WeatherWidget",
            labels=["contract_first", "implementation", "domain:weather-widget"],
            dependencies=["ghost-weather"],
            responsibility="WeatherWidget interface from types.ts",
        )

        state = MockState()
        state.project_tasks = [impl_task, ghost]
        state.task_artifacts["ghost-weather"] = [
            _make_artifact("weather-interface-contracts.ts")
        ]

        # Act
        artifacts = await _collect_task_artifacts("impl-weather", impl_task, state)

        # Assert
        dep_artifacts = [
            a for a in artifacts if a.get("dependency_task_id") == "ghost-weather"
        ]
        assert len(dep_artifacts) == 1
        assert dep_artifacts[0]["scope_annotation"] == "in_scope"

    @pytest.mark.asyncio
    async def test_different_domain_dep_is_reference_only(self) -> None:
        """
        Contract-first task depending on another domain's ghost → ``reference_only``.

        Requesting task owns weather-widget; dep ghost owns time-widget →
        the artifact is a coordination reference, not this agent's implementation
        target.
        """
        # Arrange
        ghost_time = _make_task(
            "ghost-time",
            "Design TimeWidget",
            labels=["design", "auto_completed", "domain:time-widget"],
            source_type="contract_first_design",
        )
        impl_weather = _make_task(
            "impl-weather",
            "Implement WeatherWidget",
            labels=["contract_first", "implementation", "domain:weather-widget"],
            dependencies=["ghost-time"],
            responsibility="WeatherWidget interface from types.ts",
        )

        state = MockState()
        state.project_tasks = [impl_weather, ghost_time]
        state.task_artifacts["ghost-time"] = [
            _make_artifact("time-interface-contracts.ts")
        ]

        # Act
        artifacts = await _collect_task_artifacts("impl-weather", impl_weather, state)

        # Assert
        dep_artifacts = [
            a for a in artifacts if a.get("dependency_task_id") == "ghost-time"
        ]
        assert len(dep_artifacts) == 1
        assert dep_artifacts[0]["scope_annotation"] == "reference_only"

    @pytest.mark.asyncio
    async def test_mixed_deps_annotated_correctly(self) -> None:
        """
        Contract-first task with deps on own domain (in_scope) and foreign
        domain (reference_only) receives correct annotation on each artifact.
        """
        # Arrange
        ghost_weather = _make_task(
            "ghost-weather",
            "Design WeatherWidget",
            labels=["design", "auto_completed", "domain:weather-widget"],
        )
        ghost_time = _make_task(
            "ghost-time",
            "Design TimeWidget",
            labels=["design", "auto_completed", "domain:time-widget"],
        )
        impl_weather = _make_task(
            "impl-weather",
            "Implement WeatherWidget",
            labels=["contract_first", "implementation", "domain:weather-widget"],
            dependencies=["ghost-weather", "ghost-time"],
            responsibility="WeatherWidget interface from types.ts",
        )

        state = MockState()
        state.project_tasks = [impl_weather, ghost_weather, ghost_time]
        state.task_artifacts["ghost-weather"] = [_make_artifact("weather-contracts.ts")]
        state.task_artifacts["ghost-time"] = [_make_artifact("time-contracts.ts")]

        # Act
        artifacts = await _collect_task_artifacts("impl-weather", impl_weather, state)

        # Assert
        by_dep = {
            a["dependency_task_id"]: a for a in artifacts if "dependency_task_id" in a
        }
        assert by_dep["ghost-weather"]["scope_annotation"] == "in_scope"
        assert by_dep["ghost-time"]["scope_annotation"] == "reference_only"

    @pytest.mark.asyncio
    async def test_dep_without_domain_label_is_reference_only(self) -> None:
        """
        Contract-first requesting task; dep task has no ``domain:`` label →
        cannot confirm same domain → ``reference_only``.
        """
        # Arrange
        unlabelled_dep = _make_task(
            "some-dep",
            "Some prerequisite",
            labels=[],
        )
        impl_weather = _make_task(
            "impl-weather",
            "Implement WeatherWidget",
            labels=["contract_first", "implementation", "domain:weather-widget"],
            dependencies=["some-dep"],
            responsibility="WeatherWidget interface from types.ts",
        )

        state = MockState()
        state.project_tasks = [impl_weather, unlabelled_dep]
        state.task_artifacts["some-dep"] = [_make_artifact("something.md")]

        # Act
        artifacts = await _collect_task_artifacts("impl-weather", impl_weather, state)

        # Assert
        dep_artifacts = [
            a for a in artifacts if a.get("dependency_task_id") == "some-dep"
        ]
        assert len(dep_artifacts) == 1
        assert dep_artifacts[0]["scope_annotation"] == "reference_only"


# ---------------------------------------------------------------------------
# Feature-based scope annotation
# ---------------------------------------------------------------------------


class TestFeatureBasedScopeAnnotation:
    """Scope annotation for feature-based requesting tasks (no ``domain:`` labels)."""

    @pytest.mark.asyncio
    async def test_feature_based_design_dep_is_reference_only(self) -> None:
        """
        Feature-based task with a design dep → dep artifacts are ``reference_only``.

        In feature-based decomposition the requesting task has no domain label,
        so no dep artifact belongs to its domain.
        """
        # Arrange
        design_task = _make_task(
            "design-1",
            "Design API",
            labels=["design"],
        )
        feature_task = _make_task(
            "feature-1",
            "Implement weather feature",
            labels=[],  # no domain: label → feature_based
            dependencies=["design-1"],
        )

        state = MockState()
        state.project_tasks = [feature_task, design_task]
        state.task_artifacts["design-1"] = [_make_artifact("api-design.md")]

        # Act
        artifacts = await _collect_task_artifacts("feature-1", feature_task, state)

        # Assert
        dep_artifacts = [
            a for a in artifacts if a.get("dependency_task_id") == "design-1"
        ]
        assert len(dep_artifacts) == 1
        assert dep_artifacts[0]["scope_annotation"] == "reference_only"

    @pytest.mark.asyncio
    async def test_feature_based_non_design_dep_is_reference_only(self) -> None:
        """
        Feature-based task with a non-design dep → also ``reference_only``.

        All dep artifacts are coordination context for feature-based tasks —
        none are owned by the requesting agent.
        """
        # Arrange
        impl_dep = _make_task(
            "impl-dep",
            "Implement shared utils",
            labels=["implementation"],
        )
        feature_task = _make_task(
            "feature-2",
            "Implement dashboard",
            labels=[],
            dependencies=["impl-dep"],
        )

        state = MockState()
        state.project_tasks = [feature_task, impl_dep]
        state.task_artifacts["impl-dep"] = [_make_artifact("utils.ts")]

        # Act
        artifacts = await _collect_task_artifacts("feature-2", feature_task, state)

        # Assert
        dep_artifacts = [
            a for a in artifacts if a.get("dependency_task_id") == "impl-dep"
        ]
        assert len(dep_artifacts) == 1
        assert dep_artifacts[0]["scope_annotation"] == "reference_only"

    @pytest.mark.asyncio
    async def test_foundation_dep_excluded_from_collected_artifacts(self) -> None:
        """
        Pre-fork synthesis foundation deps are excluded from
        ``_collect_task_artifacts``.

        Issue #595 Fix 2: foundation (``source_type='pre_fork_synthesis'``)
        output is delivered project-globally via the ``project_contract``
        field, not through the 1-hop dependency artifact path. Skipping it
        here keeps ``project_contract`` the single channel and avoids
        double-shipping the contract into agent context.
        """
        # Arrange
        foundation_task = _make_task(
            "foundation-1",
            "Shared Design System",
            labels=["design"],
            source_type="pre_fork_synthesis",
        )
        feature_task = _make_task(
            "feature-3",
            "Implement widget",
            labels=[],
            dependencies=["foundation-1"],
        )

        state = MockState()
        state.project_tasks = [feature_task, foundation_task]
        state.task_artifacts["foundation-1"] = [_make_artifact("design-tokens.ts")]

        # Act
        artifacts = await _collect_task_artifacts("feature-3", feature_task, state)

        # Assert — foundation dep artifacts are NOT in the 1-hop result;
        # they are delivered via project_contract instead.
        dep_artifacts = [
            a for a in artifacts if a.get("dependency_task_id") == "foundation-1"
        ]
        assert dep_artifacts == []


# ---------------------------------------------------------------------------
# Own-task artifacts have no scope annotation
# ---------------------------------------------------------------------------


class TestOwnTaskArtifactsNoAnnotation:
    """Own-task (non-dep) artifacts must not receive scope_annotation."""

    @pytest.mark.asyncio
    async def test_own_logged_artifact_has_no_scope_annotation(self) -> None:
        """
        Artifacts logged for the requesting task itself have no ``scope_annotation``.

        Scope annotation is only meaningful for dep artifacts — the requesting
        task's own logged artifacts are its implementation outputs.
        """
        # Arrange
        task = _make_task(
            "my-task",
            "My task",
            labels=["contract_first", "domain:my-domain"],
        )

        state = MockState()
        state.project_tasks = [task]
        state.task_artifacts["my-task"] = [_make_artifact("output.ts")]

        # Act
        artifacts = await _collect_task_artifacts("my-task", task, state)

        # Assert
        own_artifacts = [a for a in artifacts if "dependency_task_id" not in a]
        assert len(own_artifacts) >= 1
        for artifact in own_artifacts:
            assert "scope_annotation" not in artifact

    @pytest.mark.asyncio
    async def test_no_deps_no_scope_annotation(self) -> None:
        """Task with no dependencies produces no annotated artifacts."""
        # Arrange
        task = _make_task(
            "solo-task",
            "Solo task",
            labels=["contract_first", "domain:solo"],
        )

        state = MockState()
        state.project_tasks = [task]

        # Act
        artifacts = await _collect_task_artifacts("solo-task", task, state)

        # Assert — no dep artifacts means no scope_annotation anywhere
        assert all("scope_annotation" not in a for a in artifacts)


# ---------------------------------------------------------------------------
# P1 fix: scope_annotation must not mutate shared state
# ---------------------------------------------------------------------------


class TestScopeAnnotationIsolation:
    """scope_annotation must not bleed back into state.task_artifacts (P1 Codex fix)."""

    @pytest.mark.asyncio
    async def test_annotation_does_not_mutate_stored_artifact(self) -> None:
        """
        scope_annotation set during retrieval must not persist into
        state.task_artifacts.

        The shallow list .copy() was a bug — dict objects were shared.
        After task A annotates B's artifact as ``reference_only``, a
        subsequent retrieval by task B (same domain) must still see
        ``in_scope``, not A's stale annotation.
        """
        # Arrange — two impl tasks sharing a ghost dep
        ghost = _make_task(
            "ghost-w",
            "Design WeatherWidget",
            labels=["design", "auto_completed", "domain:weather-widget"],
        )
        impl_weather = _make_task(
            "impl-weather",
            "Implement WeatherWidget",
            labels=["contract_first", "domain:weather-widget"],
            dependencies=["ghost-w"],
        )
        impl_time = _make_task(
            "impl-time",
            "Implement TimeWidget",
            labels=["contract_first", "domain:time-widget"],
            dependencies=["ghost-w"],
        )

        state = MockState()
        state.project_tasks = [impl_weather, impl_time, ghost]
        state.task_artifacts["ghost-w"] = [_make_artifact("weather-contracts.ts")]

        # Act — time agent retrieves first (weather is reference_only for it)
        await _collect_task_artifacts("impl-time", impl_time, state)

        # Act — weather agent retrieves second
        artifacts_weather = await _collect_task_artifacts(
            "impl-weather", impl_weather, state
        )

        # Assert — weather agent must see in_scope, not time agent's reference_only
        dep_artifacts = [
            a for a in artifacts_weather if a.get("dependency_task_id") == "ghost-w"
        ]
        assert len(dep_artifacts) == 1
        assert dep_artifacts[0]["scope_annotation"] == "in_scope"

    @pytest.mark.asyncio
    async def test_stored_artifact_has_no_scope_annotation_after_retrieval(
        self,
    ) -> None:
        """
        state.task_artifacts must not contain scope_annotation after retrieval.

        Annotation is purely a retrieval-time concern — storing it would
        make it a global (generation-time) value, breaking the contextual
        semantics.
        """
        # Arrange
        ghost = _make_task(
            "ghost-x",
            "Design X",
            labels=["design", "auto_completed", "domain:widget-x"],
        )
        impl = _make_task(
            "impl-x",
            "Implement X",
            labels=["contract_first", "domain:widget-x"],
            dependencies=["ghost-x"],
        )

        state = MockState()
        state.project_tasks = [impl, ghost]
        state.task_artifacts["ghost-x"] = [_make_artifact("x-contracts.ts")]

        # Act
        await _collect_task_artifacts("impl-x", impl, state)

        # Assert — stored artifact untouched
        stored = state.task_artifacts["ghost-x"][0]
        assert "scope_annotation" not in stored


# ---------------------------------------------------------------------------
# P2 fix: Kanban dep attachments must carry scope_annotation
# ---------------------------------------------------------------------------


class MockKanbanClientWithAttachments:
    """Kanban client that returns a configurable attachment list."""

    def __init__(self, dep_attachments: list[dict]) -> None:
        self._dep_attachments = dep_attachments

    async def get_attachments(self, task_id: str) -> dict:
        """Return attachments for any card."""
        return {"success": True, "data": self._dep_attachments}


class TestKanbanDepAttachmentAnnotation:
    """Kanban dep attachments must receive scope_annotation (P2 Codex fix)."""

    @pytest.mark.asyncio
    async def test_kanban_dep_attachment_gets_scope_annotation(self) -> None:
        """
        Dependency artifacts arriving via Kanban attachments must also carry
        ``scope_annotation`` so the prompt contract matches the data layer.

        Previously only logged artifacts (state.task_artifacts) were annotated;
        Kanban attachments were silently missing the field.
        """
        # Arrange
        dep_task = _make_task(
            "dep-kanban",
            "Design Widget",
            labels=["design", "auto_completed", "domain:weather-widget"],
        )
        impl_task = _make_task(
            "impl-weather",
            "Implement WeatherWidget",
            labels=["contract_first", "domain:weather-widget"],
            dependencies=["dep-kanban"],
        )

        kanban_attachment = {
            "id": "attach-1",
            "filename": "weather-contracts.ts",
            "created_by": "marcus",
            "created_at": "2026-04-17T00:00:00Z",
        }

        state = MockState()
        state.project_tasks = [impl_task, dep_task]
        state.kanban_client = MockKanbanClientWithAttachments([kanban_attachment])
        # No logged artifacts — only Kanban attachments

        # Act
        artifacts = await _collect_task_artifacts("impl-weather", impl_task, state)

        # Assert
        dep_artifacts = [
            a for a in artifacts if a.get("dependency_task_id") == "dep-kanban"
        ]
        assert len(dep_artifacts) == 1
        assert dep_artifacts[0]["scope_annotation"] == "in_scope"

    @pytest.mark.asyncio
    async def test_kanban_dep_attachment_different_domain_is_reference_only(
        self,
    ) -> None:
        """Kanban dep attachment from a different domain → reference_only."""
        # Arrange
        dep_task = _make_task(
            "dep-time-kanban",
            "Design TimeWidget",
            labels=["design", "auto_completed", "domain:time-widget"],
        )
        impl_task = _make_task(
            "impl-weather",
            "Implement WeatherWidget",
            labels=["contract_first", "domain:weather-widget"],
            dependencies=["dep-time-kanban"],
        )

        kanban_attachment = {
            "id": "attach-2",
            "filename": "time-contracts.ts",
            "created_by": "marcus",
            "created_at": "2026-04-17T00:00:00Z",
        }

        state = MockState()
        state.project_tasks = [impl_task, dep_task]
        state.kanban_client = MockKanbanClientWithAttachments([kanban_attachment])

        # Act
        artifacts = await _collect_task_artifacts("impl-weather", impl_task, state)

        # Assert
        dep_artifacts = [
            a for a in artifacts if a.get("dependency_task_id") == "dep-time-kanban"
        ]
        assert len(dep_artifacts) == 1
        assert dep_artifacts[0]["scope_annotation"] == "reference_only"

    @pytest.mark.asyncio
    async def test_foundation_dep_kanban_attachment_also_excluded(self) -> None:
        """
        Foundation deps are skipped entirely — Kanban attachments included.

        Issue #595 Fix 2: ``project_contract`` is the sole foundation
        channel. Gating only logged artifacts would still leak a
        foundation task's Kanban attachments to direct dependents via the
        1-hop path, reintroducing the split-channel inconsistency the fix
        removes (Codex P2 on PR #598).
        """
        # Arrange
        foundation_task = _make_task(
            "foundation-k",
            "Shared Setup",
            labels=["design"],
            source_type="pre_fork_synthesis",
        )
        feature_task = _make_task(
            "feature-k",
            "Implement widget",
            labels=[],
            dependencies=["foundation-k"],
        )

        state = MockState()
        state.project_tasks = [feature_task, foundation_task]
        state.kanban_client = MockKanbanClientWithAttachments(
            [
                {
                    "id": "attach-f",
                    "filename": "setup.ts",
                    "created_by": "marcus",
                    "created_at": "2026-05-21T00:00:00Z",
                }
            ]
        )

        # Act
        artifacts = await _collect_task_artifacts("feature-k", feature_task, state)

        # Assert — nothing from the foundation dep, neither logged nor Kanban
        dep_artifacts = [
            a for a in artifacts if a.get("dependency_task_id") == "foundation-k"
        ]
        assert dep_artifacts == []
