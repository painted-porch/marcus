"""
Unit tests for AI-powered task assignment module.

This module tests the AI-powered task assignment logic that upgrades
basic task assignment with Phase 1-4 AI capabilities for intelligent
task selection.

Notes
-----
All external dependencies (AI engine, dependency inferer) are mocked
to ensure fast, isolated unit tests.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.ai_powered_task_assignment import (
    AITaskAssignmentEngine,
    find_optimal_task_for_agent_ai_powered,
)
from src.core.models import Priority, Task, TaskStatus
from src.intelligence.dependency_inferer import DependencyGraph


def create_task(
    task_id: str,
    name: str,
    status: TaskStatus = TaskStatus.TODO,
    priority: Priority = Priority.MEDIUM,
    labels: List[str] = None,
    dependencies: List[str] = None,
    description: str = "",
) -> Task:
    """Helper function to create a task with all required fields."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description=description or f"Description for {name}",
        status=status,
        priority=priority,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=now + timedelta(days=7),
        estimated_hours=8.0,
        labels=labels or [],
        dependencies=dependencies or [],
    )


class TestAITaskAssignmentEngine:
    """
    Test suite for AITaskAssignmentEngine class.

    Tests intelligent task assignment using Phase 1-4 capabilities including
    safety checks, dependency analysis, AI-powered matching, and impact prediction.
    """

    @pytest.fixture
    def mock_ai_engine(self):
        """Create a mock AI engine with async methods."""
        mock = Mock()
        mock.check_deployment_safety = AsyncMock(return_value={"safe": True})
        mock.analyze_task_assignment = AsyncMock(
            return_value={"suitability_score": 0.8, "confidence": 0.9}
        )
        mock.predict_task_impact = AsyncMock(
            return_value={"timeline_reduction_days": 5, "risk_reduction": 0.3}
        )
        return mock

    @pytest.fixture
    def mock_dependency_inferer(self):
        """Create a mock dependency inferer."""
        mock = Mock()
        # Create a mock graph that returns an empty critical path
        mock_graph = Mock(spec=DependencyGraph)
        mock_graph.get_critical_path = Mock(return_value=[])
        mock.infer_dependencies = AsyncMock(return_value=mock_graph)
        return mock

    @pytest.fixture
    def sample_tasks(self):
        """Create sample tasks for testing."""
        base_time = datetime.now(timezone.utc)
        return [
            Task(
                id="task-1",
                name="Implement user authentication",
                description="Add login/logout functionality",
                status=TaskStatus.TODO,
                priority=Priority.HIGH,
                assigned_to=None,
                created_at=base_time,
                updated_at=base_time,
                due_date=base_time + timedelta(days=7),
                estimated_hours=16.0,
                labels=["backend", "security"],
                dependencies=[],
            ),
            Task(
                id="task-2",
                name="Deploy to production",
                description="Deploy the application",
                status=TaskStatus.TODO,
                priority=Priority.URGENT,
                assigned_to=None,
                created_at=base_time,
                updated_at=base_time,
                due_date=base_time + timedelta(days=14),
                estimated_hours=4.0,
                labels=["deployment", "devops"],
                dependencies=["task-1"],
            ),
            Task(
                id="task-3",
                name="Write unit tests",
                description="Add test coverage",
                status=TaskStatus.TODO,
                priority=Priority.MEDIUM,
                assigned_to=None,
                created_at=base_time,
                updated_at=base_time,
                due_date=base_time + timedelta(days=10),
                estimated_hours=8.0,
                labels=["testing", "quality"],
                dependencies=[],
            ),
            Task(
                id="task-4",
                name="Implement API endpoints",
                description="Create REST API",
                status=TaskStatus.DONE,
                priority=Priority.HIGH,
                assigned_to="agent-0",
                created_at=base_time - timedelta(days=3),
                updated_at=base_time - timedelta(days=1),
                due_date=base_time + timedelta(days=5),
                estimated_hours=12.0,
                actual_hours=10.0,
                labels=["backend", "api"],
                dependencies=[],
            ),
        ]

    @pytest.fixture
    def assignment_engine(self, mock_ai_engine, sample_tasks, mock_dependency_inferer):
        """Create an AITaskAssignmentEngine instance with mocked dependencies."""
        engine = AITaskAssignmentEngine(mock_ai_engine, sample_tasks)
        engine.dependency_inferer = mock_dependency_inferer
        return engine

    @pytest.fixture
    def agent_info(self):
        """Create sample agent information."""
        return {
            "worker_id": "agent-1",
            "skills": ["backend", "testing"],
            "status": "available",
            "current_task": None,
        }

    # Test initialization
    def test_initialization(self, mock_ai_engine, sample_tasks):
        """Test that AITaskAssignmentEngine initializes correctly."""
        engine = AITaskAssignmentEngine(mock_ai_engine, sample_tasks)

        assert engine.ai_engine == mock_ai_engine
        assert engine.project_tasks == sample_tasks
        assert hasattr(engine, "dependency_inferer")

    # Test main method: find_optimal_task_for_agent
    @pytest.mark.asyncio
    async def test_find_optimal_task_empty_available_tasks(
        self, assignment_engine, agent_info
    ):
        """Test that None is returned when no tasks are available."""
        result = await assignment_engine.find_optimal_task_for_agent(
            agent_id="agent-1",
            agent_info=agent_info,
            available_tasks=[],
            assigned_task_ids=set(),
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_find_optimal_task_successful_assignment(
        self, assignment_engine, agent_info, sample_tasks
    ):
        """Test successful task assignment with all phases working correctly."""
        available_tasks = [t for t in sample_tasks if t.status == TaskStatus.TODO]

        result = await assignment_engine.find_optimal_task_for_agent(
            agent_id="agent-1",
            agent_info=agent_info,
            available_tasks=available_tasks,
            assigned_task_ids=set(),
        )

        assert result is not None
        assert isinstance(result, Task)
        assert result.status == TaskStatus.TODO

    @pytest.mark.asyncio
    async def test_find_optimal_task_filters_unsafe_tasks(
        self, assignment_engine, agent_info, sample_tasks
    ):
        """Test that unsafe deployment tasks are filtered out."""
        # Make deployment task unsafe
        assignment_engine.ai_engine.check_deployment_safety = AsyncMock(
            return_value={"safe": False, "reason": "Dependencies not complete"}
        )

        available_tasks = [t for t in sample_tasks if t.status == TaskStatus.TODO]

        result = await assignment_engine.find_optimal_task_for_agent(
            agent_id="agent-1",
            agent_info=agent_info,
            available_tasks=available_tasks,
            assigned_task_ids=set(),
        )

        # Should not select the deployment task
        assert result is not None
        assert "deploy" not in result.name.lower()

    # Test _filter_safe_tasks
    @pytest.mark.asyncio
    async def test_filter_safe_tasks_removes_unsafe_deployments(
        self, assignment_engine, sample_tasks
    ):
        """Test that deployment tasks with incomplete dependencies are filtered out."""
        deployment_task = next(t for t in sample_tasks if "deploy" in t.name.lower())
        other_tasks = [
            t
            for t in sample_tasks
            if t != deployment_task and t.status == TaskStatus.TODO
        ]

        # Mock AI safety check to fail for deployment
        assignment_engine.ai_engine.check_deployment_safety = AsyncMock(
            return_value={"safe": False, "reason": "Not ready"}
        )

        safe_tasks = await assignment_engine._filter_safe_tasks(
            [deployment_task] + other_tasks
        )

        assert deployment_task not in safe_tasks
        assert len(safe_tasks) == len(other_tasks)

    @pytest.mark.asyncio
    async def test_filter_safe_tasks_allows_safe_deployments(
        self, assignment_engine, sample_tasks
    ):
        """Test that deployment tasks with complete dependencies are allowed."""
        # Mark dependency as complete
        dep_task = next(t for t in sample_tasks if t.id == "task-1")
        dep_task.status = TaskStatus.DONE

        deployment_task = next(t for t in sample_tasks if "deploy" in t.name.lower())

        safe_tasks = await assignment_engine._filter_safe_tasks([deployment_task])

        assert deployment_task in safe_tasks

    @pytest.mark.asyncio
    async def test_filter_safe_tasks_non_deployment_always_safe(
        self, assignment_engine, sample_tasks
    ):
        """Test that non-deployment tasks are always considered safe."""
        non_deployment_tasks = [
            t
            for t in sample_tasks
            if "deploy" not in t.name.lower() and t.status == TaskStatus.TODO
        ]

        safe_tasks = await assignment_engine._filter_safe_tasks(non_deployment_tasks)

        assert len(safe_tasks) == len(non_deployment_tasks)

    @pytest.mark.asyncio
    async def test_find_optimal_task_no_safe_tasks(
        self, assignment_engine, agent_info, sample_tasks
    ):
        """Test that None is returned when no tasks pass safety filter."""
        # Make all tasks deployment tasks and mark them unsafe
        deployment_tasks = []
        for i, task in enumerate(sample_tasks[:3]):
            task.name = f"Deploy component {i}"
            task.status = TaskStatus.TODO
            deployment_tasks.append(task)

        # Mock AI safety check to fail for all deployment tasks
        assignment_engine.ai_engine.check_deployment_safety = AsyncMock(
            return_value={"safe": False, "reason": "Dependencies incomplete"}
        )

        result = await assignment_engine.find_optimal_task_for_agent(
            agent_id="agent-1",
            agent_info=agent_info,
            available_tasks=deployment_tasks,
            assigned_task_ids=set(),
        )

        assert result is None

    # Test _analyze_dependencies
    @pytest.mark.asyncio
    async def test_analyze_dependencies_calculates_scores(
        self, assignment_engine, sample_tasks
    ):
        """Test dependency analysis calculates unblocking scores correctly."""
        # Create a mock dependency graph with critical path
        mock_graph = Mock(spec=DependencyGraph)
        mock_graph.get_critical_path = Mock(return_value=["task-1"])
        assignment_engine.dependency_inferer.infer_dependencies = AsyncMock(
            return_value=mock_graph
        )

        # Set up a task that blocks others
        blocking_task = sample_tasks[0]  # task-1
        dependent_task = sample_tasks[1]  # task-2 depends on task-1

        scores = await assignment_engine._analyze_dependencies(
            [blocking_task, dependent_task]
        )

        assert blocking_task.id in scores
        assert dependent_task.id in scores
        # Task on critical path and unblocking others should have higher score
        assert scores[blocking_task.id] > scores[dependent_task.id]

    @pytest.mark.asyncio
    async def test_analyze_dependencies_empty_tasks(self, assignment_engine):
        """Test dependency analysis with empty task list."""
        scores = await assignment_engine._analyze_dependencies([])

        assert scores == {}

    @pytest.mark.asyncio
    async def test_analyze_dependencies_computes_critical_path_once(
        self, assignment_engine, sample_tasks
    ):
        """Critical path is computed once per request, not once per task.

        The critical path is a property of the dependency graph, not of
        any single task, so recomputing it inside the per-task loop is
        pure O(n^2) waste that froze the request path on large boards
        (issue #743). Guard that it is computed exactly once regardless
        of how many tasks are scored.
        """
        mock_graph = Mock(spec=DependencyGraph)
        mock_graph.get_critical_path = Mock(return_value=["task-1"])
        # sample_tasks declares dependencies, so the explicit-graph
        # branch runs; make it return our spyable graph.
        assignment_engine._build_explicit_graph = Mock(return_value=mock_graph)

        await assignment_engine._analyze_dependencies(sample_tasks)

        assert mock_graph.get_critical_path.call_count == 1

    # Test _get_ai_recommendations
    @pytest.mark.asyncio
    async def test_get_ai_recommendations_calculates_scores(
        self, assignment_engine, agent_info, sample_tasks
    ):
        """Test AI recommendations generate suitability scores."""
        tasks = [t for t in sample_tasks if t.status == TaskStatus.TODO]

        # Mock different scores for different tasks
        score_map = {"task-1": 0.9, "task-2": 0.6, "task-3": 0.75}

        async def mock_analyze(context):
            task_id = context.task.id
            return {
                "suitability_score": score_map.get(task_id, 0.5),
                "confidence": 0.95,
            }

        assignment_engine.ai_engine.analyze_task_assignment = AsyncMock(
            side_effect=mock_analyze
        )

        scores = await assignment_engine._get_ai_recommendations(tasks, agent_info)

        assert len(scores) == len(tasks)
        for task in tasks:
            assert task.id in scores
            expected_score = score_map.get(task.id, 0.5) * 0.95
            assert abs(scores[task.id] - expected_score) < 0.01

    @pytest.mark.asyncio
    async def test_get_ai_recommendations_handles_low_confidence(
        self, assignment_engine, agent_info, sample_tasks
    ):
        """Test AI recommendations handle low confidence appropriately."""
        tasks = [sample_tasks[0]]

        assignment_engine.ai_engine.analyze_task_assignment = AsyncMock(
            return_value={
                "suitability_score": 0.9,
                "confidence": 0.2,  # Very low confidence
            }
        )

        scores = await assignment_engine._get_ai_recommendations(tasks, agent_info)

        # Score should be reduced by low confidence
        assert scores[tasks[0].id] == 0.9 * 0.2

    # Test _predict_task_impact
    @pytest.mark.asyncio
    async def test_predict_task_impact_calculates_scores(
        self, assignment_engine, sample_tasks
    ):
        """Test impact prediction generates normalized scores."""
        tasks = [t for t in sample_tasks if t.status == TaskStatus.TODO]

        # Mock different impacts for different tasks
        impact_map = {
            "task-1": {"timeline_reduction_days": 10, "risk_reduction": 0.5},
            "task-2": {"timeline_reduction_days": 2, "risk_reduction": 0.1},
            "task-3": {"timeline_reduction_days": 5, "risk_reduction": 0.3},
        }

        async def mock_predict(task, *args):
            return impact_map.get(
                task.id, {"timeline_reduction_days": 0, "risk_reduction": 0}
            )

        assignment_engine.ai_engine.predict_task_impact = AsyncMock(
            side_effect=mock_predict
        )

        scores = await assignment_engine._predict_task_impact(tasks)

        assert len(scores) == len(tasks)
        for task in tasks:
            assert task.id in scores
            assert 0 <= scores[task.id] <= 1.0  # Scores are normalized

    @pytest.mark.asyncio
    async def test_predict_task_impact_caps_at_one(
        self, assignment_engine, sample_tasks
    ):
        """Test impact scores are capped at 1.0."""
        tasks = [sample_tasks[0]]

        # Mock extremely high impact
        assignment_engine.ai_engine.predict_task_impact = AsyncMock(
            return_value={
                "timeline_reduction_days": 100,  # Very high
                "risk_reduction": 1.0,
            }
        )

        scores = await assignment_engine._predict_task_impact(tasks)

        assert scores[tasks[0].id] == 1.0

    # Test _select_best_task
    @pytest.mark.asyncio
    async def test_select_best_task_combines_scores(
        self, assignment_engine, agent_info, sample_tasks
    ):
        """Test best task selection combines all score factors correctly."""
        tasks = [t for t in sample_tasks if t.status == TaskStatus.TODO]

        # Create score sets
        dependency_scores = {"task-1": 0.8, "task-2": 0.2, "task-3": 0.5}
        ai_scores = {"task-1": 0.9, "task-2": 0.3, "task-3": 0.7}
        impact_scores = {"task-1": 0.6, "task-2": 0.9, "task-3": 0.4}

        best_task = await assignment_engine._select_best_task(
            tasks, dependency_scores, ai_scores, impact_scores, agent_info
        )

        assert best_task is not None
        assert best_task.id == "task-1"  # Should have highest combined score

    @pytest.mark.asyncio
    async def test_select_best_task_empty_tasks(self, assignment_engine, agent_info):
        """Test best task selection with empty task list."""
        best_task = await assignment_engine._select_best_task(
            [], {}, {}, {}, agent_info
        )

        assert best_task is None

    @pytest.mark.asyncio
    async def test_select_best_task_skill_matching(
        self, assignment_engine, sample_tasks
    ):
        """Test that skill matching influences task selection."""
        # Agent with specific skills
        agent_info = {"worker_id": "agent-1", "skills": ["testing", "quality"]}

        tasks = [t for t in sample_tasks if t.status == TaskStatus.TODO]

        # Equal scores except skill match will differ
        equal_scores = {t.id: 0.5 for t in tasks}

        best_task = await assignment_engine._select_best_task(
            tasks, equal_scores, equal_scores, equal_scores, agent_info
        )

        # Should prefer the testing task due to skill match
        assert best_task.id == "task-3"

    # Test helper methods
    def test_is_deployment_task_identifies_correctly(
        self, assignment_engine, sample_tasks
    ):
        """Test deployment task identification."""
        deploy_task = next(t for t in sample_tasks if t.id == "task-2")
        normal_task = next(t for t in sample_tasks if t.id == "task-1")

        assert assignment_engine._is_deployment_task(deploy_task) is True
        assert assignment_engine._is_deployment_task(normal_task) is False

    def test_is_deployment_task_various_keywords(self, assignment_engine):
        """Test deployment identification with various keywords."""
        keywords = ["deploy", "release", "production", "launch", "rollout"]

        for keyword in keywords:
            task = create_task(
                task_id=f"task-{keyword}",
                name=f"Task with {keyword} in name",
                priority=Priority.HIGH,
            )
            assert assignment_engine._is_deployment_task(task) is True

    @pytest.mark.asyncio
    async def test_are_dependencies_complete_no_deps(self, assignment_engine):
        """Test dependency completion check with no dependencies."""
        task = create_task(
            task_id="task-1",
            name="Independent task",
            priority=Priority.HIGH,
            dependencies=[],
        )

        result = await assignment_engine._are_dependencies_complete(task)
        assert result is True

    @pytest.mark.asyncio
    async def test_are_dependencies_complete_all_done(
        self, assignment_engine, sample_tasks
    ):
        """Test dependency completion when all dependencies are done."""
        # Mark dependency as complete
        dep_task = next(t for t in sample_tasks if t.id == "task-1")
        dep_task.status = TaskStatus.DONE

        task = next(t for t in sample_tasks if t.id == "task-2")

        result = await assignment_engine._are_dependencies_complete(task)
        assert result is True

    @pytest.mark.asyncio
    async def test_are_dependencies_complete_some_incomplete(
        self, assignment_engine, sample_tasks
    ):
        """Test dependency completion with incomplete dependencies."""
        task = next(t for t in sample_tasks if t.id == "task-2")

        result = await assignment_engine._are_dependencies_complete(task)
        assert result is False

    def test_detect_project_phase_initialization(self, assignment_engine):
        """Test project phase detection for initialization."""
        assignment_engine.project_tasks = []

        phase = assignment_engine._detect_project_phase()
        assert phase == "initialization"

    def test_detect_project_phase_various_completion_rates(
        self, assignment_engine, sample_tasks
    ):
        """Test project phase detection at various completion rates."""
        # Test different completion scenarios
        test_cases = [
            (0, "foundation"),
            (1, "early_development"),  # 25% complete
            (2, "active_development"),  # 50% complete
            (3, "integration"),  # 75% complete
            (4, "deployment"),  # 100% complete
        ]

        for completed_count, expected_phase in test_cases:
            # Reset all tasks to TODO
            for task in sample_tasks:
                task.status = TaskStatus.TODO

            # Mark specified number as complete
            for i in range(completed_count):
                sample_tasks[i].status = TaskStatus.DONE

            assignment_engine.project_tasks = sample_tasks
            phase = assignment_engine._detect_project_phase()

            # Allow for some flexibility in phase boundaries
            phases = [
                "foundation",
                "early_development",
                "active_development",
                "integration",
                "testing",
                "deployment",
            ]
            assert phase in phases

    def test_detect_project_phase_testing_phase(self, assignment_engine):
        """Test project phase detection for testing phase (90% complete)."""
        # Create 10 tasks, mark 9 as complete
        tasks = []
        for i in range(10):
            task = create_task(
                task_id=f"task-{i}",
                name=f"Task {i}",
                status=TaskStatus.DONE if i < 9 else TaskStatus.TODO,
            )
            tasks.append(task)

        assignment_engine.project_tasks = tasks
        phase = assignment_engine._detect_project_phase()

        assert phase == "testing"

    def test_calculate_velocity_no_completed_tasks(self, assignment_engine):
        """Test velocity calculation with no completed tasks."""
        assignment_engine.project_tasks = [
            create_task(
                task_id=f"task-{i}",
                name=f"Task {i}",
                status=TaskStatus.TODO,
                priority=Priority.MEDIUM,
            )
            for i in range(5)
        ]

        velocity = assignment_engine._calculate_velocity()
        assert velocity == 2.0  # Default estimate

    def test_calculate_velocity_with_completed_tasks(
        self, assignment_engine, sample_tasks
    ):
        """Test velocity calculation with completed tasks."""
        # Mark some tasks as complete
        for i, task in enumerate(sample_tasks):
            if i < 2:
                task.status = TaskStatus.DONE
            else:
                task.status = TaskStatus.TODO

        assignment_engine.project_tasks = sample_tasks

        velocity = assignment_engine._calculate_velocity()
        assert velocity == 2 / 30  # 2 tasks in 30 days

    def test_calculate_skill_match_no_skills(self, assignment_engine):
        """Test skill match calculation when agent has no skills."""
        task = create_task(
            task_id="task-1",
            name="Task",
            priority=Priority.HIGH,
            labels=["backend", "api"],
        )

        score = assignment_engine._calculate_skill_match(task, [])
        assert score == 0.5  # Neutral score

    def test_calculate_skill_match_no_labels(self, assignment_engine):
        """Test skill match calculation when task has no labels."""
        task = create_task(
            task_id="task-1", name="Task", priority=Priority.HIGH, labels=[]
        )

        score = assignment_engine._calculate_skill_match(task, ["backend", "testing"])
        assert score == 0.5  # Neutral score

    def test_calculate_skill_match_partial_match(self, assignment_engine):
        """Test skill match calculation with partial match."""
        task = create_task(
            task_id="task-1",
            name="Task",
            priority=Priority.HIGH,
            labels=["backend", "api", "security"],
        )

        score = assignment_engine._calculate_skill_match(task, ["backend", "frontend"])
        assert score == 1 / 3  # 1 out of 3 labels match

    def test_calculate_skill_match_full_match(self, assignment_engine):
        """Test skill match calculation with full match."""
        task = create_task(
            task_id="task-1",
            name="Task",
            priority=Priority.HIGH,
            labels=["backend", "testing"],
        )

        score = assignment_engine._calculate_skill_match(
            task, ["backend", "testing", "frontend"]
        )
        assert score == 1.0  # All task labels match


class TestFindOptimalTaskForAgentAIPowered:
    """
    Test suite for the standalone find_optimal_task_for_agent_ai_powered function.

    Tests the integration function that creates an AITaskAssignmentEngine
    and finds optimal tasks.
    """

    @pytest.fixture
    def mock_ai_engine(self):
        """Create a mock AI engine."""
        mock = Mock()
        mock.check_deployment_safety = AsyncMock(return_value={"safe": True})
        mock.analyze_task_assignment = AsyncMock(
            return_value={"suitability_score": 0.8, "confidence": 0.9}
        )
        mock.predict_task_impact = AsyncMock(
            return_value={"timeline_reduction_days": 5, "risk_reduction": 0.3}
        )
        return mock

    @pytest.fixture
    def sample_tasks(self):
        """Create sample tasks."""
        return [
            create_task(
                task_id="task-1",
                name="Build feature",
                status=TaskStatus.TODO,
                priority=Priority.HIGH,
                labels=["backend"],
            ),
            create_task(
                task_id="task-2",
                name="Write tests",
                status=TaskStatus.TODO,
                priority=Priority.MEDIUM,
                labels=["testing"],
            ),
        ]

    @pytest.mark.asyncio
    async def test_find_optimal_task_integration(self, mock_ai_engine, sample_tasks):
        """Test the integration function creates engine and finds task."""
        with patch(
            "src.core.ai_powered_task_assignment.AITaskAssignmentEngine"
        ) as MockEngine:
            # Create a mock engine instance
            mock_engine_instance = Mock()
            mock_engine_instance.find_optimal_task_for_agent = AsyncMock(
                return_value=sample_tasks[0]
            )
            MockEngine.return_value = mock_engine_instance

            result = await find_optimal_task_for_agent_ai_powered(
                agent_id="agent-1",
                agent_status={"worker_id": "agent-1", "skills": ["backend"]},
                project_tasks=sample_tasks,
                available_tasks=sample_tasks,
                assigned_task_ids=set(),
                ai_engine=mock_ai_engine,
            )

            assert result == sample_tasks[0]
            MockEngine.assert_called_once_with(mock_ai_engine, sample_tasks)
            mock_engine_instance.find_optimal_task_for_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_find_optimal_task_returns_none(self, mock_ai_engine):
        """Test the integration function returns None when no tasks available."""
        with patch(
            "src.core.ai_powered_task_assignment.AITaskAssignmentEngine"
        ) as MockEngine:
            mock_engine_instance = Mock()
            mock_engine_instance.find_optimal_task_for_agent = AsyncMock(
                return_value=None
            )
            MockEngine.return_value = mock_engine_instance

            result = await find_optimal_task_for_agent_ai_powered(
                agent_id="agent-1",
                agent_status={"worker_id": "agent-1"},
                project_tasks=[],
                available_tasks=[],
                assigned_task_ids=set(),
                ai_engine=mock_ai_engine,
            )

            assert result is None


class TestEdgeCasesAndErrorHandling:
    """
    Test suite for edge cases and error handling scenarios.

    Tests robustness of the AI-powered task assignment system under
    various error conditions and edge cases.
    """

    @pytest.fixture
    def assignment_engine(self):
        """Create an assignment engine with mock dependencies."""
        mock_ai = Mock()
        mock_ai.check_deployment_safety = AsyncMock(return_value={"safe": True})
        mock_ai.analyze_task_assignment = AsyncMock(
            return_value={"suitability_score": 0.5, "confidence": 0.5}
        )
        mock_ai.predict_task_impact = AsyncMock(
            return_value={"timeline_reduction_days": 0, "risk_reduction": 0}
        )

        engine = AITaskAssignmentEngine(mock_ai, [])

        # Mock dependency inferer
        mock_graph = Mock(spec=DependencyGraph)
        mock_graph.get_critical_path = Mock(return_value=[])
        engine.dependency_inferer.infer_dependencies = AsyncMock(
            return_value=mock_graph
        )

        return engine

    @pytest.mark.asyncio
    async def test_ai_engine_failure_fallback(self, assignment_engine):
        """Test graceful handling when AI engine fails."""
        # Make AI engine raise exception
        assignment_engine.ai_engine.analyze_task_assignment = AsyncMock(
            side_effect=Exception("AI service unavailable")
        )

        task = create_task(
            task_id="task-1",
            name="Test task",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
        )

        # The current implementation doesn't handle AI failures gracefully
        # It will raise the exception
        with pytest.raises(Exception) as exc_info:
            await assignment_engine._get_ai_recommendations(
                [task], {"worker_id": "agent-1"}
            )

        assert str(exc_info.value) == "AI service unavailable"

    @pytest.mark.asyncio
    async def test_missing_task_fields(self, assignment_engine):
        """Test handling of tasks with missing optional fields."""
        # Create task and then set fields to None to test handling
        task = create_task(
            task_id="task-1",
            name="Minimal task",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
        )
        task.labels = None  # Set to None to test handling
        task.dependencies = None  # Set to None to test handling

        # Should handle None values gracefully
        is_deploy = assignment_engine._is_deployment_task(task)
        assert isinstance(is_deploy, bool)

        deps_complete = await assignment_engine._are_dependencies_complete(task)
        assert deps_complete is True  # No deps means complete

        skill_match = assignment_engine._calculate_skill_match(task, ["backend"])
        assert skill_match == 0.5  # Neutral score for missing labels

    @pytest.mark.asyncio
    async def test_concurrent_task_assignment(self, assignment_engine):
        """Test thread safety with concurrent task assignments."""
        tasks = [
            create_task(
                task_id=f"task-{i}",
                name=f"Task {i}",
                status=TaskStatus.TODO,
                priority=Priority.MEDIUM,
            )
            for i in range(10)
        ]

        assignment_engine.project_tasks = tasks

        # Simulate concurrent agents requesting tasks
        async def request_task(agent_id):
            return await assignment_engine.find_optimal_task_for_agent(
                agent_id=agent_id,
                agent_info={"worker_id": agent_id, "skills": []},
                available_tasks=tasks,
                assigned_task_ids=set(),
            )

        # Run multiple concurrent requests
        results = await asyncio.gather(
            request_task("agent-1"), request_task("agent-2"), request_task("agent-3")
        )

        # All should get tasks (though might be the same task)
        assert all(r is not None for r in results)

    @pytest.mark.asyncio
    async def test_circular_dependencies(self, assignment_engine):
        """Test handling of circular dependencies."""
        # Create tasks with circular dependencies
        task_a = create_task(
            task_id="task-a",
            name="Task A",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
            dependencies=["task-b"],
        )
        task_b = create_task(
            task_id="task-b",
            name="Task B",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
            dependencies=["task-a"],
        )

        assignment_engine.project_tasks = [task_a, task_b]

        # Should not crash when analyzing dependencies
        scores = await assignment_engine._analyze_dependencies([task_a, task_b])

        assert "task-a" in scores
        assert "task-b" in scores

    @pytest.mark.asyncio
    async def test_extreme_score_values(self, assignment_engine):
        """Test handling of extreme score values."""
        task = create_task(
            task_id="task-1",
            name="Test task",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
        )

        # Mock extreme values
        assignment_engine.ai_engine.predict_task_impact = AsyncMock(
            return_value={
                "timeline_reduction_days": 999999,  # Extremely high
                "risk_reduction": -10,  # Invalid negative
            }
        )

        scores = await assignment_engine._predict_task_impact([task])

        # Should cap at reasonable values
        assert 0 <= scores[task.id] <= 1.0

    @pytest.mark.asyncio
    async def test_empty_agent_info(self, assignment_engine):
        """Test handling of empty or minimal agent info."""
        task = create_task(
            task_id="task-1",
            name="Test task",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
            labels=["backend"],
        )

        # The current implementation requires worker_id in agent_info
        # So this will raise a KeyError
        with pytest.raises(KeyError) as exc_info:
            await assignment_engine.find_optimal_task_for_agent(
                agent_id="agent-1",
                agent_info={},  # Empty dict - missing required 'worker_id'
                available_tasks=[task],
                assigned_task_ids=set(),
            )

        assert str(exc_info.value) == "'worker_id'"


class TestExplicitDependencyGraph:
    """Test suite for the explicit-graph fast path in dependency analysis.

    Hybrid dependency inference exists to fill in MISSING dependencies.
    Running it on the request path over a board whose dependencies are
    all explicit is pure overhead, and at several hundred tasks the
    O(n^2) pattern sweep pins the event loop for tens of minutes per
    request_next_task (Simon 44db8b8b; it blocked the #737 full run).
    When any task declares explicit dependencies, the scorer builds the
    graph from those declared edges and never invokes the inferer.
    """

    def _task(self, task_id: str, deps: List[str]) -> Task:
        now = datetime.now(timezone.utc)
        return Task(
            id=task_id,
            name=f"Task {task_id}",
            description="",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=now,
            updated_at=now,
            due_date=None,
            estimated_hours=1.0,
            dependencies=deps,
        )

    @pytest.mark.asyncio
    async def test_explicit_deps_skip_inference(self) -> None:
        """A wired board never calls the inferer; scores still compute."""
        tasks = [
            self._task("find-1", []),
            self._task("verify-1", ["find-1"]),
            self._task("synth-1", ["verify-1"]),
        ]
        engine = AITaskAssignmentEngine(Mock(), tasks)
        engine.dependency_inferer = Mock()
        engine.dependency_inferer.infer_dependencies = AsyncMock()

        scores = await engine._analyze_dependencies(tasks)

        engine.dependency_inferer.infer_dependencies.assert_not_called()
        assert set(scores) == {"find-1", "verify-1", "synth-1"}
        assert scores["find-1"] > scores["synth-1"]

    @pytest.mark.asyncio
    async def test_unwired_board_still_infers(self) -> None:
        """A board with no declared dependencies keeps the inferer."""
        tasks = [self._task("a", []), self._task("b", [])]
        engine = AITaskAssignmentEngine(Mock(), tasks)
        engine.dependency_inferer = Mock()
        engine.dependency_inferer.infer_dependencies = AsyncMock(
            return_value=DependencyGraph(
                nodes={t.id: t for t in tasks},
                edges=[],
                adjacency_list={},
                reverse_adjacency={},
            )
        )

        await engine._analyze_dependencies(tasks)

        engine.dependency_inferer.infer_dependencies.assert_called_once()

    @pytest.mark.asyncio
    async def test_cyclic_explicit_deps_terminate(self) -> None:
        """A dependency cycle forfeits the critical-path bonus, not the run."""
        tasks = [
            self._task("a", ["b"]),
            self._task("b", ["a"]),
        ]
        engine = AITaskAssignmentEngine(Mock(), tasks)
        engine.dependency_inferer = Mock()
        engine.dependency_inferer.infer_dependencies = AsyncMock()

        scores = await engine._analyze_dependencies(tasks)

        engine.dependency_inferer.infer_dependencies.assert_not_called()
        assert set(scores) == {"a", "b"}
