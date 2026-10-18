"""
Unit tests for context management tools.

Tests the get_task_context and log_decision functions in the context module.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.context import get_task_context, log_decision


class MockContextResult:
    """Mock context result object."""

    def __init__(self, implementations=None, decisions=None):
        self.implementations = implementations or []
        self.decisions = decisions or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "implementations": self.implementations,
            "decisions": self.decisions,
        }


class MockContext:
    """Mock context system for testing."""

    def __init__(self):
        self.get_context_calls = []
        self.log_decision_calls = []
        self.mock_context_result = MockContextResult()

    async def get_context(
        self, task_id: str, dependencies: List[str]
    ) -> MockContextResult:
        """Mock get_context method."""
        self.get_context_calls.append(
            {"task_id": task_id, "dependencies": dependencies}
        )
        return self.mock_context_result

    async def log_decision(
        self, agent_id: str, task_id: str, what: str, why: str, impact: str
    ) -> Any:
        """Mock log_decision method."""
        decision = Mock()
        decision.decision_id = "test-decision-id"
        decision.to_dict = Mock(
            return_value={
                "decision_id": "test-decision-id",
                "agent_id": agent_id,
                "task_id": task_id,
                "what": what,
                "why": why,
                "impact": impact,
            }
        )
        self.log_decision_calls.append(
            {
                "agent_id": agent_id,
                "task_id": task_id,
                "what": what,
                "why": why,
                "impact": impact,
            }
        )
        return decision


class MockKanbanClient:
    """Mock Kanban client for testing."""

    def __init__(self):
        self.add_comment_calls = []
        self.get_attachments_calls = []
        self.mock_attachments = []

    async def add_comment(self, task_id: str, comment: str) -> Dict[str, Any]:
        """Mock add_comment method."""
        self.add_comment_calls.append({"task_id": task_id, "comment": comment})
        return {"success": True}

    async def get_attachments(self, task_id: str) -> Dict[str, Any]:
        """Mock get_attachments method."""
        self.get_attachments_calls.append({"task_id": task_id})
        return {"success": True, "data": self.mock_attachments}


class MockState:
    """Mock state object for testing."""

    def __init__(self):
        self.task_artifacts: Dict[str, List[Dict[str, Any]]] = {}
        self.task_decisions: Dict[str, Any] = {}
        self.task_blockers: Dict[str, Any] = {}
        self.project_tasks: List[Task] = []
        self.context = MockContext()
        self.kanban_client = MockKanbanClient()
        self.events = None
        self.subtask_manager = None


@pytest.fixture
def mock_state():
    """Create a mock state object."""
    return MockState()


@pytest.fixture
def sample_task():
    """Create a sample task for testing."""
    return Task(
        id="test-task-1",
        name="Test Task",
        description="Test task description",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=1.0,
        dependencies=[],
    )


class TestGetTaskContext:
    """Test suite for get_task_context function."""

    @pytest.mark.asyncio
    async def test_get_task_context_returns_logged_artifacts(
        self, mock_state, sample_task
    ):
        """Test that get_task_context returns artifacts logged via log_artifact."""
        # Arrange
        mock_state.project_tasks = [sample_task]
        mock_state.task_artifacts["test-task-1"] = [
            {
                "filename": "api_spec.md",
                "location": "docs/api/api_spec.md",
                "artifact_type": "api",
                "description": "API specification",
                "is_default_location": True,
            }
        ]

        # Act
        result = await get_task_context("test-task-1", mock_state)

        # Assert
        assert result["success"] is True
        context = result["context"]
        assert "artifacts" in context
        assert len(context["artifacts"]) == 1
        assert context["artifacts"][0]["filename"] == "api_spec.md"
        assert context["artifacts"][0]["artifact_type"] == "api"

    @pytest.mark.asyncio
    async def test_get_task_context_returns_empty_when_no_artifacts(
        self, mock_state, sample_task
    ):
        """Test that get_task_context returns empty list when no artifacts logged."""
        # Arrange
        mock_state.project_tasks = [sample_task]
        # Don't add any artifacts

        # Act
        result = await get_task_context("test-task-1", mock_state)

        # Assert
        assert result["success"] is True
        context = result["context"]
        assert "artifacts" in context
        assert context["artifacts"] == []

    @pytest.mark.asyncio
    async def test_get_task_context_includes_kanban_attachments(
        self, mock_state, sample_task
    ):
        """Test that get_task_context includes Kanban attachments."""
        # Arrange
        mock_state.project_tasks = [sample_task]
        mock_state.kanban_client.mock_attachments = [
            {
                "id": "attach-1",
                "filename": "design.png",
                "created_by": "user-1",
                "created_at": "2024-01-01T00:00:00Z",
            }
        ]

        # Act
        result = await get_task_context("test-task-1", mock_state)

        # Assert
        assert result["success"] is True
        context = result["context"]
        assert len(context["artifacts"]) == 1
        assert context["artifacts"][0]["filename"] == "design.png"
        assert context["artifacts"][0]["storage_type"] == "attachment"

    @pytest.mark.asyncio
    async def test_get_task_context_includes_dependency_artifacts(
        self, mock_state, sample_task
    ):
        """Test that get_task_context includes artifacts from dependency tasks."""
        # Arrange
        dependency_task = Task(
            id="dep-task-1",
            name="Dependency Task",
            description="Dependency task",
            status=TaskStatus.DONE,
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=2.0,
            dependencies=[],
        )
        sample_task.dependencies = ["dep-task-1"]

        mock_state.project_tasks = [sample_task, dependency_task]

        # Add artifact to dependency task
        mock_state.task_artifacts["dep-task-1"] = [
            {
                "filename": "dep_spec.md",
                "location": "docs/specifications/dep_spec.md",
                "artifact_type": "specification",
                "description": "Dependency specification",
                "is_default_location": True,
            }
        ]

        # Act
        result = await get_task_context("test-task-1", mock_state)

        # Assert
        assert result["success"] is True
        context = result["context"]
        assert len(context["artifacts"]) == 1
        assert context["artifacts"][0]["filename"] == "dep_spec.md"
        assert context["artifacts"][0]["dependency_task_id"] == "dep-task-1"
        assert context["artifacts"][0]["dependency_task_name"] == "Dependency Task"

    @pytest.mark.asyncio
    async def test_get_task_context_combines_all_artifact_sources(
        self, mock_state, sample_task
    ):
        """Test that get_task_context combines artifacts from all sources."""
        # Arrange
        dependency_task = Task(
            id="dep-task-1",
            name="Dependency Task",
            description="Dependency",
            status=TaskStatus.DONE,
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=2.0,
            dependencies=[],
        )
        sample_task.dependencies = ["dep-task-1"]
        mock_state.project_tasks = [sample_task, dependency_task]

        # Add logged artifact for current task
        mock_state.task_artifacts["test-task-1"] = [
            {
                "filename": "my_spec.md",
                "location": "docs/specifications/my_spec.md",
                "artifact_type": "specification",
                "description": "My specification",
                "is_default_location": True,
            }
        ]

        # Add logged artifact for dependency
        mock_state.task_artifacts["dep-task-1"] = [
            {
                "filename": "dep_spec.md",
                "location": "docs/specifications/dep_spec.md",
                "artifact_type": "specification",
                "description": "Dependency specification",
                "is_default_location": True,
            }
        ]

        # Add Kanban attachment
        mock_state.kanban_client.mock_attachments = [
            {
                "id": "attach-1",
                "filename": "design.png",
                "created_by": "user-1",
                "created_at": "2024-01-01T00:00:00Z",
            }
        ]

        # Act
        result = await get_task_context("test-task-1", mock_state)

        # Assert
        assert result["success"] is True
        context = result["context"]
        # Should have 4 artifacts:
        # 1. my_spec.md (logged for task)
        # 2. design.png (Kanban attachment for task)
        # 3. dep_spec.md (logged for dependency)
        # 4. design.png (Kanban attachment for dependency - same mock returns for all cards)
        assert len(context["artifacts"]) == 4

        # Check we have artifacts from each source
        filenames = [a["filename"] for a in context["artifacts"]]
        assert "my_spec.md" in filenames
        assert "dep_spec.md" in filenames
        assert (
            filenames.count("design.png") == 2
        )  # Once from task, once from dependency

    @pytest.mark.asyncio
    async def test_get_task_context_fails_when_task_not_found(self, mock_state):
        """Test that get_task_context returns error when task doesn't exist."""
        # Arrange - don't add any tasks

        # Act
        result = await get_task_context("nonexistent-task", mock_state)

        # Assert
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_get_task_context_fails_when_context_system_disabled(
        self, mock_state, sample_task
    ):
        """Test that get_task_context fails gracefully when context system disabled."""
        # Arrange
        mock_state.project_tasks = [sample_task]
        mock_state.context = None  # Disable context system

        # Act
        result = await get_task_context("test-task-1", mock_state)

        # Assert
        assert result["success"] is False
        assert "Context system not enabled" in result["error"]

    @pytest.mark.asyncio
    async def test_get_task_context_handles_kanban_client_errors_gracefully(
        self, mock_state, sample_task
    ):
        """Test that get_task_context handles Kanban client errors gracefully."""
        # Arrange
        mock_state.project_tasks = [sample_task]

        # Make kanban client raise an error
        async def failing_get_attachments(card_id):
            raise Exception("Kanban connection error")

        mock_state.kanban_client.get_attachments = failing_get_attachments

        # Act
        result = await get_task_context("test-task-1", mock_state)

        # Assert - should succeed despite kanban error
        assert result["success"] is True
        context = result["context"]
        assert "artifacts" in context

    @pytest.mark.asyncio
    async def test_get_task_context_with_subtask(self, mock_state):
        """Test that get_task_context returns subtask-specific context."""
        # Arrange
        mock_subtask_manager = Mock()
        mock_subtask_manager.subtasks = {
            "subtask-1": {
                "id": "subtask-1",
                "name": "Subtask 1",
                "parent_task_id": "parent-task-1",
            }
        }
        mock_subtask_manager.get_subtask_context = Mock(
            return_value={
                "parent_task_id": "parent-task-1",
                "subtask": {
                    "id": "subtask-1",
                    "name": "Subtask 1",
                    "description": "Subtask description",
                },
                "shared_conventions": ["Convention 1"],
                "dependency_artifacts": [],
                "sibling_subtasks": [],
            }
        )
        # Mock get_subtasks to return empty list (no sibling subtasks)
        mock_subtask_manager.get_subtasks = Mock(return_value=[])

        parent_task = Task(
            id="parent-task-1",
            name="Parent Task",
            description="Parent task",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=4.0,
            dependencies=[],
        )

        mock_state.subtask_manager = mock_subtask_manager
        mock_state.project_tasks = [parent_task]

        # Act
        result = await get_task_context("subtask-1", mock_state)

        # Assert
        assert result["success"] is True
        context = result["context"]
        assert context["is_subtask"] is True
        assert "parent_task" in context
        assert context["parent_task"]["id"] == "parent-task-1"
        assert "shared_conventions" in context
        assert "sibling_subtasks" in context

    @pytest.mark.asyncio
    async def test_get_task_context_logs_agent_event(self, mock_state, sample_task):
        """Test that get_task_context writes a context_requested agent event."""
        # Arrange
        mock_state.project_tasks = [sample_task]
        mock_state._current_client_id = "agent-2"

        with patch("src.marcus_mcp.tools.context.log_agent_event") as mock_log:
            # Act
            result = await get_task_context("test-task-1", mock_state)

        # Assert
        assert result["success"] is True
        mock_log.assert_called_once_with(
            "context_requested",
            {"agent_id": "agent-2", "task_id": "test-task-1", "project": "unknown"},
        )


class TestLogDecision:
    """Test suite for log_decision function."""

    @pytest.mark.asyncio
    async def test_log_decision_successful(self, mock_state):
        """Test that log_decision successfully logs a decision."""
        # Arrange
        agent_id = "agent-1"
        task_id = "task-1"
        decision = (
            "I chose REST API because it's widely supported. This affects the frontend."
        )

        # Act
        result = await log_decision(agent_id, task_id, decision, mock_state)

        # Assert
        assert result["success"] is True
        assert "decision_id" in result
        assert result["decision_id"] == "test-decision-id"

        # Verify decision was logged
        assert len(mock_state.context.log_decision_calls) == 1
        logged = mock_state.context.log_decision_calls[0]
        assert logged["agent_id"] == agent_id
        assert logged["task_id"] == task_id
        assert "REST API" in logged["what"]

    @pytest.mark.asyncio
    async def test_log_decision_adds_kanban_comment(self, mock_state, sample_task):
        """Test that log_decision adds a comment to the Kanban card."""
        # Arrange
        mock_state.project_tasks = [sample_task]
        agent_id = "agent-1"
        task_id = sample_task.id
        decision = (
            "I chose PostgreSQL because it supports ACID. This affects data layer."
        )

        # Act
        result = await log_decision(agent_id, task_id, decision, mock_state)

        # Assert
        assert result["success"] is True

        # Verify comment was added
        assert len(mock_state.kanban_client.add_comment_calls) == 1
        comment_call = mock_state.kanban_client.add_comment_calls[0]
        assert "ARCHITECTURAL DECISION" in comment_call["comment"]
        assert "PostgreSQL" in comment_call["comment"]

    @pytest.mark.asyncio
    async def test_log_decision_parses_structured_format(self, mock_state):
        """Test that log_decision parses structured decision format."""
        # Arrange
        agent_id = "agent-1"
        task_id = "task-1"
        decision = (
            "I chose microservices architecture because it enables scaling. "
            "This affects deployment and infrastructure requirements."
        )

        # Act
        result = await log_decision(agent_id, task_id, decision, mock_state)

        # Assert
        assert result["success"] is True

        # Verify parsed decision components
        logged = mock_state.context.log_decision_calls[0]
        assert "microservices" in logged["what"]
        assert "scaling" in logged["why"]
        assert "deployment" in logged["impact"] or "infrastructure" in logged["impact"]

    @pytest.mark.asyncio
    async def test_log_decision_handles_unstructured_format(self, mock_state):
        """Test that log_decision handles unstructured decision text."""
        # Arrange
        agent_id = "agent-1"
        task_id = "task-1"
        decision = "Using Redis for caching"

        # Act
        result = await log_decision(agent_id, task_id, decision, mock_state)

        # Assert
        assert result["success"] is True

        # Verify decision was logged with defaults
        logged = mock_state.context.log_decision_calls[0]
        assert logged["what"] == decision
        assert logged["why"] == "Not specified"
        assert logged["impact"] == "May affect dependent tasks"

    @pytest.mark.asyncio
    async def test_log_decision_fails_when_context_system_disabled(self, mock_state):
        """Test that log_decision fails when context system is disabled."""
        # Arrange
        mock_state.context = None
        agent_id = "agent-1"
        task_id = "task-1"
        decision = "Some decision"

        # Act
        result = await log_decision(agent_id, task_id, decision, mock_state)

        # Assert
        assert result["success"] is False
        assert "Context system not enabled" in result["error"]

    @pytest.mark.asyncio
    async def test_log_decision_handles_kanban_comment_failure_gracefully(
        self, mock_state, sample_task
    ):
        """Test that log_decision continues even if Kanban comment fails."""
        # Arrange
        mock_state.project_tasks = [sample_task]

        async def failing_add_comment(task_id, comment):
            raise Exception("Kanban connection error")

        mock_state.kanban_client.add_comment = failing_add_comment

        agent_id = "agent-1"
        task_id = sample_task.id
        decision = "Some decision"

        # Act
        result = await log_decision(agent_id, task_id, decision, mock_state)

        # Assert - should still succeed
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Option B + C: design artifact usage_guidance injection
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str,
    name: str,
    labels: Optional[List[str]] = None,
    dependencies: Optional[List[str]] = None,
) -> Task:
    """Create a minimal Task for testing."""
    return Task(
        id=task_id,
        name=name,
        description="",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=1.0,
        labels=labels or [],
        dependencies=dependencies or [],
    )


class TestDesignArtifactUsageGuidance:
    """Test usage_guidance injection for design dependency artifacts (Options B & C).

    Option B: label-based detection — feature_based design tasks have "design"
    label but NOT "auto_completed".  Artifacts from those deps receive a
    COORDINATION REFERENCE note so agents know to build the full feature, not
    just implement the interface.

    Option C: artifact_role field — takes precedence over label detection when
    present.  Enables role-aware guidance without relying on task labels.
    """

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_feature_based_design_dep_gets_coordination_guidance(
        self, mock_state: MockState
    ) -> None:
        """Feature_based design dep artifacts receive COORDINATION REFERENCE guidance.

        Design tasks in feature_based runs have a "design" label but no
        "auto_completed" label.  Agents depending on them should be told
        that those artifacts define a coordination boundary, not their
        implementation spec.
        """
        # Arrange
        design_task = _make_task("design-1", "Design Weather Domain", labels=["design"])
        impl_task = _make_task(
            "impl-1", "Implement Weather Widget", dependencies=["design-1"]
        )
        mock_state.project_tasks = [design_task, impl_task]
        mock_state.task_artifacts["design-1"] = [
            {
                "filename": "weather-interface-contracts.md",
                "location": "docs/specifications/weather-interface-contracts.md",
                "artifact_type": "specification",
                "description": "Interface contracts for Weather domain",
                "is_default_location": True,
            }
        ]

        # Act
        result = await get_task_context("impl-1", mock_state)

        # Assert
        assert result["success"] is True
        artifacts = result["context"]["artifacts"]
        assert len(artifacts) == 1
        assert "usage_guidance" in artifacts[0]
        assert "COORDINATION REFERENCE" in artifacts[0]["usage_guidance"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_contract_first_ghost_dep_does_not_get_coordination_guidance(
        self, mock_state: MockState
    ) -> None:
        """Contract_first ghost design deps do NOT get coordination guidance.

        Contract_first ghost tasks carry both "design" AND "auto_completed"
        labels.  Those agents already get framing from the contract_notice
        layer in build_tiered_instructions — adding guidance here would
        duplicate and potentially conflict with that framing.
        """
        # Arrange: contract_first ghost has "design" + "auto_completed"
        ghost = _make_task(
            "ghost-1",
            "Design GameState Domain",
            labels=["design", "auto_completed", "domain:gamestate"],
        )
        impl_task = _make_task(
            "impl-1", "Implement GameState", dependencies=["ghost-1"]
        )
        mock_state.project_tasks = [ghost, impl_task]
        mock_state.task_artifacts["ghost-1"] = [
            {
                "filename": "gamestate-interface-contracts.md",
                "location": "docs/specifications/gamestate-interface-contracts.md",
                "artifact_type": "specification",
                "description": "Interface contracts for GameState",
                "is_default_location": True,
            }
        ]

        # Act
        result = await get_task_context("impl-1", mock_state)

        # Assert
        assert result["success"] is True
        artifacts = result["context"]["artifacts"]
        assert len(artifacts) == 1
        assert "usage_guidance" not in artifacts[0]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_non_design_dep_artifact_has_no_usage_guidance(
        self, mock_state: MockState
    ) -> None:
        """Artifacts from non-design dependency tasks do not get usage_guidance.

        Only design-phase artifacts need role clarification.  Output from
        regular implementation deps should flow through unchanged.
        """
        # Arrange: plain implementation dep, no "design" label
        impl_dep = _make_task("impl-dep-1", "Implement Core Logic")
        impl_task = _make_task("impl-1", "Implement UI", dependencies=["impl-dep-1"])
        mock_state.project_tasks = [impl_dep, impl_task]
        mock_state.task_artifacts["impl-dep-1"] = [
            {
                "filename": "core-output.ts",
                "location": "src/core-output.ts",
                "artifact_type": "documentation",
                "description": "Core logic output",
                "is_default_location": True,
            }
        ]

        # Act
        result = await get_task_context("impl-1", mock_state)

        # Assert
        assert result["success"] is True
        artifacts = result["context"]["artifacts"]
        assert len(artifacts) == 1
        assert "usage_guidance" not in artifacts[0]

    # ------------------------------------------------------------------
    # Option C: artifact_role field tests
    # ------------------------------------------------------------------

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_artifact_role_interface_contract_gets_coordination_guidance(
        self, mock_state: MockState
    ) -> None:
        """artifact_role='interface_contract' yields COORDINATION REFERENCE guidance.

        Option C: artifact_role field takes precedence over label-based detection.
        """
        # Arrange: dep task has no "design" label, but artifact has artifact_role
        dep_task = _make_task("dep-1", "Some Task")
        impl_task = _make_task("impl-1", "Implement Feature", dependencies=["dep-1"])
        mock_state.project_tasks = [dep_task, impl_task]
        mock_state.task_artifacts["dep-1"] = [
            {
                "filename": "domain-interface-contracts.md",
                "location": "docs/specifications/domain-interface-contracts.md",
                "artifact_type": "specification",
                "artifact_role": "interface_contract",
                "description": "Interface contracts",
                "is_default_location": True,
            }
        ]

        # Act
        result = await get_task_context("impl-1", mock_state)

        # Assert
        assert result["success"] is True
        artifacts = result["context"]["artifacts"]
        assert len(artifacts) == 1
        assert "usage_guidance" in artifacts[0]
        assert "COORDINATION REFERENCE" in artifacts[0]["usage_guidance"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_artifact_role_implementation_spec_gets_implementation_guidance(
        self, mock_state: MockState
    ) -> None:
        """artifact_role='implementation_spec' yields IMPLEMENTATION GUIDE guidance."""
        # Arrange
        dep_task = _make_task("dep-1", "Design Domain", labels=["design"])
        impl_task = _make_task("impl-1", "Implement Feature", dependencies=["dep-1"])
        mock_state.project_tasks = [dep_task, impl_task]
        mock_state.task_artifacts["dep-1"] = [
            {
                "filename": "domain-data-models.md",
                "location": "docs/specifications/domain-data-models.md",
                "artifact_type": "specification",
                "artifact_role": "implementation_spec",
                "description": "Data models",
                "is_default_location": True,
            }
        ]

        # Act
        result = await get_task_context("impl-1", mock_state)

        # Assert
        assert result["success"] is True
        artifacts = result["context"]["artifacts"]
        assert len(artifacts) == 1
        assert "usage_guidance" in artifacts[0]
        assert "IMPLEMENTATION GUIDE" in artifacts[0]["usage_guidance"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_artifact_role_takes_precedence_over_label_detection(
        self, mock_state: MockState
    ) -> None:
        """artifact_role field overrides label-based detection when both are present.

        A design dep task (would trigger label-based guidance) whose artifact
        has artifact_role='implementation_spec' should get IMPLEMENTATION GUIDE
        guidance, not the COORDINATION REFERENCE from the label path.
        """
        # Arrange: design dep task (label path would give COORDINATION REFERENCE)
        # but artifact has artifact_role='implementation_spec'
        design_task = _make_task("design-1", "Design Domain", labels=["design"])
        impl_task = _make_task("impl-1", "Implement Feature", dependencies=["design-1"])
        mock_state.project_tasks = [design_task, impl_task]
        mock_state.task_artifacts["design-1"] = [
            {
                "filename": "domain-data-models.md",
                "artifact_type": "specification",
                "artifact_role": "implementation_spec",
                "description": "Data models",
                "is_default_location": True,
            }
        ]

        # Act
        result = await get_task_context("impl-1", mock_state)

        # Assert: artifact_role wins — IMPLEMENTATION GUIDE, not COORDINATION REFERENCE
        assert result["success"] is True
        artifacts = result["context"]["artifacts"]
        assert len(artifacts) == 1
        assert "IMPLEMENTATION GUIDE" in artifacts[0]["usage_guidance"]
        assert "COORDINATION REFERENCE" not in artifacts[0]["usage_guidance"]
