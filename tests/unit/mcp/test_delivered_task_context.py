"""
Unit tests for guaranteed task-context delivery (issue #605).

Marcus is a multi-agent coordination platform. An agent pulls a task via
the ``request_next_task`` MCP tool, then builds it. To build something
that fits the rest of the project the agent needs *context*: the
project's shared foundation contract and the architectural artifacts and
decisions of its dependencies.

Before #605 that context only arrived if the agent made a *second*,
optional ``get_task_context`` call — and a context-build was skipped
entirely whenever more than five TODO tasks existed. Issue #605 folds
context into the ``request_next_task`` response itself, in three
scope-labelled tiers:

1. ``project_contract`` — the project-global foundation contract.
2. ``dependency_artifacts`` — direct-dependency artifacts (``in_scope``).
3. ``transitive_context`` — transitive ancestor artifacts + all upstream
   decisions (``reference_only``).

These tests cover the new assembly helpers
(:func:`_collect_transitive_context`, :func:`assemble_task_context`) and
the architectural-artifact filter that keeps source code out of context.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.context import (
    ARCHITECTURAL_ARTIFACT_TYPES,
    _collect_transitive_context,
    _is_architectural_artifact,
    assemble_task_context,
)

pytestmark = pytest.mark.unit


class _MockDecision:
    """Minimal architectural-decision stand-in."""

    def __init__(self, task_id: str, what: str) -> None:
        self.task_id = task_id
        self._what = what

    def to_dict(self) -> Dict[str, Any]:
        return {"task_id": self.task_id, "what": self._what}


class _MockContext:
    """Mock Context subsystem carrying a flat decision list."""

    def __init__(self) -> None:
        self.decisions: List[_MockDecision] = []


class _MockState:
    """Mock Marcus server state."""

    def __init__(self) -> None:
        self.task_artifacts: Dict[str, List[Dict[str, Any]]] = {}
        self.project_tasks: List[Task] = []
        self.context = _MockContext()
        self.kanban_client = None
        self.events = None
        self.subtask_manager: Any = None


class _MockKanbanClient:
    """Mock Kanban client returning canned attachment data per card_id."""

    def __init__(
        self,
        attachments_by_card: Dict[str, List[Dict[str, Any]]] | None = None,
    ) -> None:
        self._attachments_by_card = attachments_by_card or {}

    async def get_attachments(self, task_id: str) -> Dict[str, Any]:
        return {
            "success": True,
            "data": self._attachments_by_card.get(task_id, []),
        }


def _task(
    task_id: str,
    *,
    source_type: str = "",
    dependencies: List[str] | None = None,
    labels: List[str] | None = None,
) -> Task:
    """Build a Task with the fields the context helpers touch."""
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=1.0,
        dependencies=dependencies or [],
        labels=labels or [],
        source_type=source_type,
    )


@pytest.fixture()
def state() -> _MockState:
    """Provide a fresh mock state per test."""
    return _MockState()


class TestIsArchitecturalArtifact:
    """_is_architectural_artifact keeps source code out of context."""

    @pytest.mark.parametrize("artifact_type", sorted(ARCHITECTURAL_ARTIFACT_TYPES))
    def test_architectural_types_pass(self, artifact_type: str) -> None:
        """All six doc-backed artifact types are deliverable."""
        assert _is_architectural_artifact({"artifact_type": artifact_type})

    def test_temporary_type_rejected(self) -> None:
        """The ``temporary`` type is not delivered as context."""
        assert not _is_architectural_artifact({"artifact_type": "temporary"})

    def test_custom_type_rejected(self) -> None:
        """A non-standard type (e.g. real code) is not delivered."""
        assert not _is_architectural_artifact({"artifact_type": "source_code"})

    def test_missing_type_treated_as_architectural(self) -> None:
        """An artifact with no artifact_type is treated as deliverable."""
        assert _is_architectural_artifact({"filename": "notes.md"})


class TestCollectTransitiveContext:
    """_collect_transitive_context gathers ambient ancestor material."""

    @pytest.mark.asyncio
    async def test_collects_artifacts_beyond_first_hop(self, state: _MockState) -> None:
        """
        Artifacts two hops upstream are returned as reference_only.

        Graph: leaf -> mid -> root. ``mid`` is a direct dependency of
        ``leaf`` (handled elsewhere); ``root`` is transitive and should
        appear here.
        """
        leaf = _task("leaf", dependencies=["mid"])
        mid = _task("mid", dependencies=["root"])
        root = _task("root")
        state.project_tasks = [leaf, mid, root]
        state.task_artifacts["mid"] = [
            {"filename": "mid.md", "artifact_type": "design"}
        ]
        state.task_artifacts["root"] = [
            {"filename": "root.md", "artifact_type": "architecture"}
        ]

        result = await _collect_transitive_context("leaf", leaf, state)

        # ``mid`` is a direct dep — excluded here. ``root`` is transitive.
        assert [a["filename"] for a in result["artifacts"]] == ["root.md"]
        assert result["artifacts"][0]["scope_annotation"] == "reference_only"

    @pytest.mark.asyncio
    async def test_excludes_direct_dependencies(self, state: _MockState) -> None:
        """Direct (one-hop) dependency artifacts are not transitive."""
        leaf = _task("leaf", dependencies=["mid"])
        mid = _task("mid")
        state.project_tasks = [leaf, mid]
        state.task_artifacts["mid"] = [
            {"filename": "mid.md", "artifact_type": "design"}
        ]

        result = await _collect_transitive_context("leaf", leaf, state)

        assert result["artifacts"] == []

    @pytest.mark.asyncio
    async def test_excludes_foundation_tasks(self, state: _MockState) -> None:
        """Foundation output rides project_contract, not transitive."""
        leaf = _task("leaf", dependencies=["mid"])
        mid = _task("mid", dependencies=["f1"])
        foundation = _task("f1", source_type="pre_fork_synthesis")
        state.project_tasks = [leaf, mid, foundation]
        state.task_artifacts["f1"] = [
            {"filename": "tsconfig.json", "artifact_type": "specification"}
        ]

        result = await _collect_transitive_context("leaf", leaf, state)

        assert result["artifacts"] == []

    @pytest.mark.asyncio
    async def test_excludes_source_code_artifacts(self, state: _MockState) -> None:
        """Non-architectural artifacts are filtered out transitively."""
        leaf = _task("leaf", dependencies=["mid"])
        mid = _task("mid", dependencies=["root"])
        root = _task("root")
        state.project_tasks = [leaf, mid, root]
        state.task_artifacts["root"] = [
            {"filename": "feature.py", "artifact_type": "temporary"},
            {"filename": "spec.md", "artifact_type": "specification"},
        ]

        result = await _collect_transitive_context("leaf", leaf, state)

        assert [a["filename"] for a in result["artifacts"]] == ["spec.md"]

    @pytest.mark.asyncio
    async def test_collects_transitive_decisions(self, state: _MockState) -> None:
        """Decisions on transitive ancestors propagate as reference_only."""
        leaf = _task("leaf", dependencies=["mid"])
        mid = _task("mid", dependencies=["root"])
        root = _task("root")
        state.project_tasks = [leaf, mid, root]
        state.context.decisions = [
            _MockDecision("root", "Use UTC everywhere"),
            _MockDecision("unrelated", "off-graph decision"),
        ]

        result = await _collect_transitive_context("leaf", leaf, state)

        assert [d["what"] for d in result["decisions"]] == ["Use UTC everywhere"]
        assert result["decisions"][0]["scope_annotation"] == "reference_only"

    @pytest.mark.asyncio
    async def test_handles_dependency_cycle(self, state: _MockState) -> None:
        """A cyclic dependency graph does not cause infinite recursion."""
        leaf = _task("leaf", dependencies=["a"])
        a = _task("a", dependencies=["b"])
        b = _task("b", dependencies=["a"])  # cycle a <-> b
        state.project_tasks = [leaf, a, b]
        state.task_artifacts["b"] = [{"filename": "b.md", "artifact_type": "design"}]

        result = await _collect_transitive_context("leaf", leaf, state)

        assert [a["filename"] for a in result["artifacts"]] == ["b.md"]

    @pytest.mark.asyncio
    async def test_empty_without_transitive_ancestry(self, state: _MockState) -> None:
        """A task with only direct deps has empty transitive context."""
        leaf = _task("leaf", dependencies=["mid"])
        mid = _task("mid")
        state.project_tasks = [leaf, mid]

        result = await _collect_transitive_context("leaf", leaf, state)

        assert result == {"artifacts": [], "decisions": []}

    @pytest.mark.asyncio
    async def test_kanban_attachments_from_transitive_ancestors(
        self, state: _MockState
    ) -> None:
        """
        Regression (PR #606 review P2): the transitive walk also fetches
        Kanban attachments for ancestors — not just artifacts logged via
        log_artifact — otherwise architectural docs attached to a multi-
        hop ancestor silently disappear.
        """
        leaf = _task("leaf", dependencies=["mid"])
        mid = _task("mid", dependencies=["root"])
        root = _task("root")
        state.project_tasks = [leaf, mid, root]
        state.kanban_client = _MockKanbanClient(
            {
                "root": [
                    {"id": "att-1", "filename": "design.md", "created_by": "u1"},
                ]
            }
        )

        result = await _collect_transitive_context("leaf", leaf, state)

        names = [a["filename"] for a in result["artifacts"]]
        assert names == ["design.md"]
        assert result["artifacts"][0]["scope_annotation"] == "reference_only"
        assert result["artifacts"][0]["dependency_task_id"] == "root"


class TestAssembleTaskContext:
    """assemble_task_context builds the full three-tier delivery bundle."""

    @pytest.mark.asyncio
    async def test_bundle_has_all_three_tiers(self, state: _MockState) -> None:
        """The assembled bundle exposes the three documented fields."""
        leaf = _task("leaf")
        state.project_tasks = [leaf]

        bundle = await assemble_task_context("leaf", leaf, state)

        assert set(bundle) == {
            "project_contract",
            "dependency_artifacts",
            "transitive_context",
        }

    @pytest.mark.asyncio
    async def test_includes_foundation_contract(self, state: _MockState) -> None:
        """Tier 1: the project-global foundation contract is present."""
        foundation = _task("f1", source_type="pre_fork_synthesis")
        leaf = _task("leaf")
        state.project_tasks = [foundation, leaf]
        state.task_artifacts["f1"] = [
            {"filename": "package.json", "artifact_type": "specification"}
        ]

        bundle = await assemble_task_context("leaf", leaf, state)

        contract = bundle["project_contract"]
        assert [a["filename"] for a in contract["artifacts"]] == ["package.json"]

    @pytest.mark.asyncio
    async def test_includes_direct_dependency_artifacts(
        self, state: _MockState
    ) -> None:
        """Tier 2: architectural artifacts from direct dependencies."""
        leaf = _task("leaf", dependencies=["mid"])
        mid = _task("mid")
        state.project_tasks = [leaf, mid]
        state.task_artifacts["mid"] = [
            {"filename": "mid.md", "artifact_type": "design"}
        ]

        bundle = await assemble_task_context("leaf", leaf, state)

        assert [a["filename"] for a in bundle["dependency_artifacts"]] == ["mid.md"]

    @pytest.mark.asyncio
    async def test_filters_source_code_from_direct_dependencies(
        self, state: _MockState
    ) -> None:
        """Source-code artifacts from direct deps are not delivered."""
        leaf = _task("leaf", dependencies=["mid"])
        mid = _task("mid")
        state.project_tasks = [leaf, mid]
        state.task_artifacts["mid"] = [
            {"filename": "mid.py", "artifact_type": "temporary"},
            {"filename": "mid.md", "artifact_type": "design"},
        ]

        bundle = await assemble_task_context("leaf", leaf, state)

        assert [a["filename"] for a in bundle["dependency_artifacts"]] == ["mid.md"]

    @pytest.mark.asyncio
    async def test_includes_transitive_context(self, state: _MockState) -> None:
        """Tier 3: transitive ancestor artifacts ride transitive_context."""
        leaf = _task("leaf", dependencies=["mid"])
        mid = _task("mid", dependencies=["root"])
        root = _task("root")
        state.project_tasks = [leaf, mid, root]
        state.task_artifacts["root"] = [
            {"filename": "root.md", "artifact_type": "architecture"}
        ]

        bundle = await assemble_task_context("leaf", leaf, state)

        transitive = bundle["transitive_context"]
        assert [a["filename"] for a in transitive["artifacts"]] == ["root.md"]
        assert transitive["artifacts"][0]["scope_annotation"] == "reference_only"

    @pytest.mark.asyncio
    async def test_dependency_artifacts_excludes_own_artifacts(
        self, state: _MockState
    ) -> None:
        """
        Regression (PR #606 review P2): dependency_artifacts is filtered
        to dependency-sourced entries only — the requesting task's *own*
        logged artifacts must not appear there. _collect_task_artifacts
        returns both the task's own artifacts and its dependencies', so
        assemble_task_context filters by dependency_task_id.
        """
        leaf = _task("leaf", dependencies=["mid"])
        mid = _task("mid")
        state.project_tasks = [leaf, mid]
        # Own artifact on leaf — no dependency_task_id will be set by
        # _collect_task_artifacts.
        state.task_artifacts["leaf"] = [
            {"filename": "leaf-own.md", "artifact_type": "design"}
        ]
        # Dependency artifact on mid — _collect_task_artifacts will
        # tag it with dependency_task_id=mid.
        state.task_artifacts["mid"] = [
            {"filename": "mid.md", "artifact_type": "design"}
        ]

        bundle = await assemble_task_context("leaf", leaf, state)

        names = [a["filename"] for a in bundle["dependency_artifacts"]]
        assert names == ["mid.md"]

    @pytest.mark.asyncio
    async def test_degrades_to_empty_without_subsystems(
        self, state: _MockState
    ) -> None:
        """Bundle fields degrade to empty values, never raising."""
        leaf = _task("leaf")
        state.project_tasks = [leaf]
        state.context = None  # type: ignore[assignment]

        bundle = await assemble_task_context("leaf", leaf, state)

        assert bundle["project_contract"] == {"artifacts": [], "decisions": []}
        assert bundle["dependency_artifacts"] == []
        assert bundle["transitive_context"] == {"artifacts": [], "decisions": []}
