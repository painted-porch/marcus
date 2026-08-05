"""
Unit tests for Integration Verification Task Generator.

Tests the integration verification phase that runs after implementation
completes to verify the project actually builds, starts, and works.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.integrations.nlp_task_utils import TaskType

pytestmark = pytest.mark.unit


def create_test_task(
    task_id: str,
    name: str,
    labels: list[str] | None = None,
    priority: Priority = Priority.HIGH,
    status: TaskStatus = TaskStatus.TODO,
    dependencies: list[str] | None = None,
) -> Task:
    """Create a test task with sensible defaults."""
    return Task(
        id=task_id,
        name=name,
        description=f"Description for {name}",
        status=status,
        priority=priority,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=4.0,
        labels=labels or [],
        dependencies=dependencies or [],
    )


@pytest.fixture
def sample_implementation_tasks() -> list[Task]:
    """Create sample implementation tasks for testing.

    Uses realistic AI-generated labels (not hardcoded type:feature).
    The classifier detects implementation from the task name.
    """
    return [
        create_test_task(
            "impl-001",
            "Build weather widget",
            labels=["implement", "frontend"],
        ),
        create_test_task(
            "impl-002",
            "Build clock widget",
            labels=["implement", "frontend"],
        ),
    ]


@pytest.fixture
def sample_testing_tasks() -> list[Task]:
    """Create sample testing tasks for testing."""
    return [
        create_test_task(
            "test-001",
            "Write widget tests",
            labels=["testing"],
        ),
    ]


@pytest.fixture
def sample_design_tasks() -> list[Task]:
    """Create sample design tasks for testing."""
    return [
        create_test_task(
            "design-001",
            "Design dashboard architecture",
            labels=["design"],
        ),
    ]


@pytest.fixture
def all_sample_tasks(
    sample_design_tasks: list[Task],
    sample_implementation_tasks: list[Task],
    sample_testing_tasks: list[Task],
) -> list[Task]:
    """Combine all sample tasks."""
    return sample_design_tasks + sample_implementation_tasks + sample_testing_tasks


class TestIntegrationTaskGenerator:
    """Test suite for IntegrationTaskGenerator."""

    def test_creates_task_with_correct_dependencies(
        self, all_sample_tasks: list[Task]
    ) -> None:
        """Test integration task depends on all impl and test tasks."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            all_sample_tasks, "Dashboard"
        )

        assert task is not None
        # Should depend on design, impl, and test tasks
        assert "design-001" in task.dependencies
        assert "impl-001" in task.dependencies
        assert "impl-002" in task.dependencies
        assert "test-001" in task.dependencies

    def test_no_task_when_no_implementation_tasks(self) -> None:
        """Test returns None when no implementation tasks exist."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        design_only = [
            create_test_task(
                "design-001",
                "Design system",
                labels=["type:design"],
            ),
        ]
        task = IntegrationTaskGenerator.create_integration_task(design_only, "Project")

        assert task is None

    def test_task_labels_correct(self, all_sample_tasks: list[Task]) -> None:
        """Test integration task has correct labels."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            all_sample_tasks, "Dashboard"
        )

        assert task is not None
        assert "integration" in task.labels
        assert "verification" in task.labels
        assert "type:integration" in task.labels

    def test_task_priority_urgent(self, all_sample_tasks: list[Task]) -> None:
        """Test integration task has URGENT priority."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            all_sample_tasks, "Dashboard"
        )

        assert task is not None
        assert task.priority == Priority.URGENT

    def test_task_id_format(self, all_sample_tasks: list[Task]) -> None:
        """Test integration task ID starts with correct prefix."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            all_sample_tasks, "Dashboard"
        )

        assert task is not None
        assert task.id.startswith("integration_verify_")

    def test_acceptance_criteria_set(self, all_sample_tasks: list[Task]) -> None:
        """Test integration task has meaningful acceptance criteria."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            all_sample_tasks, "Dashboard"
        )

        assert task is not None
        assert task.acceptance_criteria is not None
        assert len(task.acceptance_criteria) > 0

    def test_task_status_is_todo(self, all_sample_tasks: list[Task]) -> None:
        """Test integration task starts in TODO status."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            all_sample_tasks, "Dashboard"
        )

        assert task is not None
        assert task.status == TaskStatus.TODO

    def test_task_name_includes_project_name(
        self, all_sample_tasks: list[Task]
    ) -> None:
        """Test integration task name includes the project name."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            all_sample_tasks, "My Dashboard"
        )

        assert task is not None
        assert "My Dashboard" in task.name

    def test_works_with_generic_ai_labels(self) -> None:
        """Test integration task created with generic labels.

        AI-generated tasks often have simple labels like 'implement'
        instead of 'type:feature'. The classifier should still detect
        them as implementation tasks.
        """
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        tasks = [
            create_test_task(
                "t1",
                "Design the system",
                labels=["design"],
            ),
            create_test_task(
                "t2",
                "Build the backend API",
                labels=["implement", "backend"],
            ),
            create_test_task(
                "t3",
                "Build the frontend UI",
                labels=["frontend"],
            ),
        ]

        task = IntegrationTaskGenerator.create_integration_task(tasks, "Dashboard")

        assert task is not None, (
            "Integration task should be created even with generic "
            "AI-generated labels (not type:feature)"
        )


class TestEnhanceProjectWithIntegration:
    """Test suite for enhance_project_with_integration function."""

    def test_adds_integration_task_to_task_list(
        self, all_sample_tasks: list[Task]
    ) -> None:
        """Test that integration task is added to task list."""
        from src.integrations.integration_verification import (
            enhance_project_with_integration,
        )

        result = enhance_project_with_integration(
            all_sample_tasks, "Build a dashboard app", "Dashboard"
        )

        assert len(result) == len(all_sample_tasks) + 1
        integration_tasks = [t for t in result if "integration" in t.labels]
        assert len(integration_tasks) == 1

    def test_skips_poc_projects(self, all_sample_tasks: list[Task]) -> None:
        """Test that POC projects don't get integration tasks."""
        from src.integrations.integration_verification import (
            enhance_project_with_integration,
        )

        result = enhance_project_with_integration(
            all_sample_tasks, "Build a poc dashboard", "Dashboard"
        )

        assert len(result) == len(all_sample_tasks)

    def test_skips_demo_projects(self, all_sample_tasks: list[Task]) -> None:
        """Test that demo projects don't get integration tasks."""
        from src.integrations.integration_verification import (
            enhance_project_with_integration,
        )

        result = enhance_project_with_integration(
            all_sample_tasks, "Build a demo app", "Dashboard"
        )

        assert len(result) == len(all_sample_tasks)

    def test_documentation_depends_on_integration(self) -> None:
        """Test doc task includes integration task in dependencies."""
        from src.integrations.documentation_tasks import (
            DocumentationTaskGenerator,
        )
        from src.integrations.integration_verification import (
            enhance_project_with_integration,
        )

        # Use type:feature labels so DocumentationTaskGenerator
        # also recognizes them (it still uses hardcoded labels)
        tasks_with_typed_labels = [
            create_test_task(
                "design-001",
                "Design dashboard architecture",
                labels=["type:design"],
            ),
            create_test_task(
                "impl-001",
                "Build weather widget",
                labels=["type:feature", "component:frontend"],
            ),
            create_test_task(
                "test-001",
                "Write widget tests",
                labels=["type:testing"],
            ),
        ]

        # First add integration task
        tasks_with_integration = enhance_project_with_integration(
            tasks_with_typed_labels,
            "Build a dashboard app",
            "Dashboard",
        )

        # Find the integration task
        integration_task = next(
            t for t in tasks_with_integration if "integration" in t.labels
        )

        # Now create documentation task from the updated list
        doc_task = DocumentationTaskGenerator.create_documentation_task(
            tasks_with_integration, "Dashboard"
        )

        assert doc_task is not None
        assert integration_task.id in doc_task.dependencies

    def test_returns_unchanged_when_no_impl_tasks(self) -> None:
        """Test returns original tasks when no impl tasks exist."""
        from src.integrations.integration_verification import (
            enhance_project_with_integration,
        )

        design_only = [
            create_test_task(
                "design-001",
                "Design system",
                labels=["type:design"],
            ),
        ]
        result = enhance_project_with_integration(
            design_only, "Build a system", "System"
        )

        assert len(result) == len(design_only)


class TestIntegrationTaskContractFile:
    """
    GH-320 PR 2: Integration task names the contract file when
    contract-first decomposition is active.

    The integration agent must treat the contract as authoritative:
    fix implementations that diverge, never edit the contract to
    match a broken implementation. Without this instruction in the
    task description, the integration agent could silently "fix" a
    mismatch by editing the contract, breaking the invariant that
    made contract-first decomposition work.
    """

    def test_integration_task_includes_contract_preamble_when_set(
        self, sample_implementation_tasks: list[Task]
    ) -> None:
        """contract_file set → task description includes preamble."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            sample_implementation_tasks,
            project_name="Snake Game",
            contract_file="docs/api/types.ts",
        )

        assert task is not None
        assert "CONTRACT-FIRST PROJECT" in task.description
        assert "docs/api/types.ts" in task.description

    def test_integration_task_preamble_forbids_contract_modification(
        self, sample_implementation_tasks: list[Task]
    ) -> None:
        """
        Preamble must instruct the agent NOT to modify the contract.

        This is the load-bearing invariant. If an integration agent
        can silently edit the contract to match a broken
        implementation, contract-first decomposition breaks for
        all future runs.
        """
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            sample_implementation_tasks,
            project_name="Snake Game",
            contract_file="docs/api/types.ts",
        )

        assert task is not None
        description_lower = task.description.lower()
        assert "fix the implementation" in description_lower
        assert "do not modify the contract" in description_lower or (
            "do not" in description_lower and "contract" in description_lower
        )

    def test_integration_task_no_preamble_when_contract_file_none(
        self, sample_implementation_tasks: list[Task]
    ) -> None:
        """Feature-based path (contract_file=None) → no preamble."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            sample_implementation_tasks,
            project_name="Dashboard",
            contract_file=None,
        )

        assert task is not None
        assert "CONTRACT-FIRST PROJECT" not in task.description

    def test_enhance_project_with_integration_passes_contract_file(
        self, sample_implementation_tasks: list[Task]
    ) -> None:
        """
        enhance_project_with_integration forwards contract_file to the
        integration task description.
        """
        from src.integrations.integration_verification import (
            enhance_project_with_integration,
        )

        result = enhance_project_with_integration(
            sample_implementation_tasks,
            project_description="Build a snake game",
            project_name="Snake Game",
            contract_file="docs/api/snake-types.ts",
        )

        integration_tasks = [t for t in result if "type:integration" in t.labels]
        assert len(integration_tasks) == 1
        assert "docs/api/snake-types.ts" in integration_tasks[0].description
        assert "CONTRACT-FIRST PROJECT" in integration_tasks[0].description

    def test_enhance_project_with_integration_default_no_contract(
        self, sample_implementation_tasks: list[Task]
    ) -> None:
        """
        Backward-compat: callers that don't pass contract_file get the
        unchanged (non-contract-first) integration task.
        """
        from src.integrations.integration_verification import (
            enhance_project_with_integration,
        )

        result = enhance_project_with_integration(
            sample_implementation_tasks,
            project_description="Build a dashboard",
            project_name="Dashboard",
        )

        integration_tasks = [t for t in result if "type:integration" in t.labels]
        assert len(integration_tasks) == 1
        assert "CONTRACT-FIRST PROJECT" not in integration_tasks[0].description


class TestPhaseEnumUpdates:
    """Test that INTEGRATION is properly added to phase enums."""

    def test_integration_phase_exists(self) -> None:
        """Test TaskPhase.INTEGRATION exists."""
        from src.core.phase_dependency_enforcer import TaskPhase

        assert hasattr(TaskPhase, "INTEGRATION")

    def test_integration_phase_between_testing_and_documentation(
        self,
    ) -> None:
        """Test INTEGRATION phase is ordered between TESTING and DOCS."""
        from src.core.phase_dependency_enforcer import TaskPhase

        assert TaskPhase.TESTING.value < TaskPhase.INTEGRATION.value
        assert TaskPhase.INTEGRATION.value < TaskPhase.DOCUMENTATION.value

    def test_integration_type_exists(self) -> None:
        """Test TaskType.INTEGRATION exists."""
        assert hasattr(TaskType, "INTEGRATION")
        assert TaskType.INTEGRATION.value == "integration"

    def test_type_to_phase_mapping(self) -> None:
        """Test TaskType.INTEGRATION maps to TaskPhase.INTEGRATION."""
        from src.core.phase_dependency_enforcer import (
            PhaseDependencyEnforcer,
            TaskPhase,
        )

        mapping = PhaseDependencyEnforcer.TYPE_TO_PHASE_MAP
        assert TaskType.INTEGRATION in mapping
        assert mapping[TaskType.INTEGRATION] == TaskPhase.INTEGRATION

    def test_integration_in_phase_order(self) -> None:
        """Test INTEGRATION is in PHASE_ORDER list."""
        from src.core.phase_dependency_enforcer import (
            PhaseDependencyEnforcer,
            TaskPhase,
        )

        assert TaskPhase.INTEGRATION in PhaseDependencyEnforcer.PHASE_ORDER
        # Should be after TESTING and before DOCUMENTATION
        order = PhaseDependencyEnforcer.PHASE_ORDER
        testing_idx = order.index(TaskPhase.TESTING)
        integration_idx = order.index(TaskPhase.INTEGRATION)
        doc_idx = order.index(TaskPhase.DOCUMENTATION)
        assert testing_idx < integration_idx < doc_idx


class TestIntegrationTaskClassification:
    """Test that integration tasks are classified correctly."""

    def test_classify_integration_verification_task(self) -> None:
        """Test task with 'integration verification' is classified."""
        from src.integrations.enhanced_task_classifier import (
            EnhancedTaskClassifier,
        )

        classifier = EnhancedTaskClassifier()
        task = create_test_task(
            "int-001",
            "Integration verification for Dashboard",
            labels=["integration", "verification"],
        )

        result = classifier.classify(task)
        assert result == TaskType.INTEGRATION

    def test_classify_build_verification_task(self) -> None:
        """Test task with 'build verification' is classified."""
        from src.integrations.enhanced_task_classifier import (
            EnhancedTaskClassifier,
        )

        classifier = EnhancedTaskClassifier()
        task = create_test_task(
            "int-002",
            "Build verification and smoke test",
            labels=["integration"],
        )

        result = classifier.classify(task)
        assert result == TaskType.INTEGRATION

    def test_integration_not_confused_with_testing(self) -> None:
        """Test 'Write integration tests' classifies as TESTING."""
        from src.integrations.enhanced_task_classifier import (
            EnhancedTaskClassifier,
        )

        classifier = EnhancedTaskClassifier()
        task = create_test_task(
            "test-002",
            "Write integration tests for API",
            labels=["type:testing"],
        )

        result = classifier.classify(task)
        # Should be TESTING, not INTEGRATION
        assert result == TaskType.TESTING


class TestShouldAddIntegrationTask:
    """Test the should_add_integration_task decision logic."""

    def test_normal_project_gets_integration(self) -> None:
        """Test normal projects get integration verification."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert IntegrationTaskGenerator.should_add_integration_task(
            "Build an e-commerce platform"
        )

    def test_poc_skips_integration(self) -> None:
        """Test POC projects skip integration verification."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert not IntegrationTaskGenerator.should_add_integration_task(
            "Build a poc to test the idea"
        )

    def test_proof_of_concept_skips_integration(self) -> None:
        """Test proof of concept projects skip integration."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert not IntegrationTaskGenerator.should_add_integration_task(
            "Proof of concept for new API"
        )

    def test_demo_skips_integration(self) -> None:
        """Test demo projects skip integration verification."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert not IntegrationTaskGenerator.should_add_integration_task(
            "Create a demo for the team"
        )

    def test_experiment_gets_integration(self) -> None:
        """Test experiment projects DO get integration verification.

        Regression test for GH-320 PR 1. Previously the keyword
        "experiment" was in the skip list, which meant Marcus's own
        multi-agent experiments never exercised integration verification
        and we could not measure whether integration verification was
        catching the bugs contract-first decomposition introduces.
        """
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert IntegrationTaskGenerator.should_add_integration_task(
            "Experiment with new rendering"
        )

    def test_test_project_still_skips_integration(self) -> None:
        """Test projects with 'test' in the description still skip.

        Complements ``test_experiment_gets_integration``: we only
        removed "experiment" from the skip list, not "test". Projects
        literally named "test this thing" are still POC-like and
        should not get the integration phase.
        """
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert not IntegrationTaskGenerator.should_add_integration_task(
            "Quick test of the new encoder"
        )

    def test_description_mentioning_test_suite_still_gets_integration(
        self,
    ) -> None:
        """
        Descriptions that mention a 'test suite' as a requirement
        must NOT be classified as POC projects.

        Regression test for the substring-match bug: the original
        implementation used ``"test" in description_lower`` which
        fired on any compound containing "test" — including the
        word "test suite" — and silently suppressed the integration
        verification task for real projects whose descriptions
        happened to mention testing infrastructure.
        """
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert IntegrationTaskGenerator.should_add_integration_task(
            "Build a dashboard web app with a comprehensive test suite"
        )

    def test_description_with_unit_tests_still_gets_integration(self) -> None:
        """'Unit tests' is not a POC indicator."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert IntegrationTaskGenerator.should_add_integration_task(
            "Build an auth service with unit tests and integration tests"
        )

    def test_description_with_test_driven_still_gets_integration(self) -> None:
        """'Test-driven development' is not a POC indicator."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert IntegrationTaskGenerator.should_add_integration_task(
            "Use test-driven development to build a REST API"
        )

    def test_description_with_latest_still_gets_integration(self) -> None:
        """Word boundary: 'latest' must not match 'test'."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert IntegrationTaskGenerator.should_add_integration_task(
            "Build an app using the latest React version"
        )

    def test_description_with_contest_still_gets_integration(self) -> None:
        """Word boundary: 'contest' must not match 'test'."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert IntegrationTaskGenerator.should_add_integration_task(
            "Build a contest voting platform for community events"
        )

    def test_description_with_testing_framework_still_gets_integration(
        self,
    ) -> None:
        """'Testing framework' is infrastructure, not POC intent."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert IntegrationTaskGenerator.should_add_integration_task(
            "Build a testing framework for browser automation"
        )

    def test_plural_pocs_skip_integration(self) -> None:
        """
        Regression test for Codex P1 on PR #333: the word-boundary
        regex must match plural ``pocs`` as well as singular ``poc``.
        """
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert not IntegrationTaskGenerator.should_add_integration_task(
            "Build POCs to evaluate different approaches"
        )

    def test_plural_demos_skip_integration(self) -> None:
        """
        Regression test for Codex P1 on PR #333: the word-boundary
        regex must match plural ``demos`` as well as singular
        ``demo``.
        """
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert not IntegrationTaskGenerator.should_add_integration_task(
            "Create demos for the stakeholder presentation"
        )

    def test_plural_proof_of_concepts_skip_integration(self) -> None:
        """
        Regression test for Codex P1 on PR #333: the word-boundary
        regex must match ``proof of concepts`` as well as the
        singular ``proof of concept``.
        """
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        assert not IntegrationTaskGenerator.should_add_integration_task(
            "Proof of concepts for three candidate architectures"
        )


class TestIntegrationTaskFunctionalRequirements:
    """
    Test that functional requirements are attached to the integration
    task as acceptance criteria (GH-320 task #64).

    The integration agent already verifies the merged product — giving
    it the full requirements list means it checks whether user intent
    was realized, even if no individual impl task carries a specific
    verb. This was the gap in Experiment 4 v2 where both agents built
    API plumbing but no UI because no task carried "display" forward
    and the integration agent had no reference back to the user's ask.
    """

    @pytest.fixture
    def sample_tasks(self) -> list[Task]:
        """Two implementation tasks for a dashboard project."""
        return [
            create_test_task(
                "t1",
                "Implement WeatherWidget",
                labels=["contract_first", "implementation"],
            ),
            create_test_task(
                "t2",
                "Implement TimeWidget",
                labels=["contract_first", "implementation"],
            ),
        ]

    def test_requirements_appended_to_acceptance_criteria(
        self, sample_tasks: list[Task]
    ) -> None:
        """
        When functional_requirements are provided, each requirement's
        name must appear in the integration task's acceptance_criteria.
        """
        from src.integrations.integration_verification import (
            enhance_project_with_integration,
        )

        requirements = [
            {"id": "f1", "name": "Display current weather temperature"},
            {"id": "f2", "name": "Display current time with timezone"},
        ]

        result = enhance_project_with_integration(
            sample_tasks,
            "Build a dashboard",
            "Dashboard",
            functional_requirements=requirements,
        )

        integration_task = next((t for t in result if "integration" in t.labels), None)
        assert integration_task is not None, "Integration task not created"

        for req in requirements:
            assert any(
                req["name"] in criterion
                for criterion in integration_task.acceptance_criteria
            ), (
                f"Requirement {req['name']!r} not found in "
                f"acceptance_criteria: {integration_task.acceptance_criteria}"
            )

    def test_no_requirements_preserves_default_criteria(
        self, sample_tasks: list[Task]
    ) -> None:
        """
        When no functional_requirements are provided (feature-based
        path), the integration task keeps its default acceptance
        criteria unchanged.
        """
        from src.integrations.integration_verification import (
            enhance_project_with_integration,
        )

        result = enhance_project_with_integration(
            sample_tasks,
            "Build a dashboard",
            "Dashboard",
        )

        integration_task = next((t for t in result if "integration" in t.labels), None)
        assert integration_task is not None
        # Default criteria include "Tests run with full terminal output"
        assert any("Tests run" in c for c in integration_task.acceptance_criteria)
        # No requirement-specific criteria
        assert not any("Display" in c for c in integration_task.acceptance_criteria)


class TestStartCommandBuildPipelineRequirement:
    """
    Integration verification prompt must require the agent's declared
    start_command to exercise the build pipeline, not merely the test
    suite.

    Regression for dashboard-v73: agent_unicorn_1 declared
    ``start_command='cd worktrees/agent_unicorn_1 && npm test'`` for
    the integration verification task. The smoke gate ran vitest in
    the worktree and it passed in <1s — but vitest never invokes the
    build pipeline. Had the React app been missing public/index.html
    (the v71 failure shape), npm test would still pass while
    npm run build would fail. The prompt now teaches the agent that
    a passing test suite does not prove the deliverable can be built
    and started, and requires start_command to exercise the build
    pipeline (chain build + test if both apply).

    Tool-agnostic by design: the prompt names the *contract* (must
    exercise the build pipeline) without naming any specific tool.
    Marcus stays platform-agnostic; the agent picks the right
    command for their stack.
    """


def _user_outcome(
    out_id: str,
    action: str = "user can do X",
    signal: str = "X is observable",
    scope: str = "in_scope",
):
    """Build a UserOutcome for outcome-wiring tests."""
    from src.ai.advanced.prd.outcome_extractor import UserOutcome

    return UserOutcome(id=out_id, action=action, success_signal=signal, scope=scope)


class TestIntegrationTaskOutcomesWiring:
    """``create_integration_task`` + ``enhance_project_with_integration``
    accept user outcomes and surface them on the integration task.

    Issue #523 Slice B: in-scope outcomes are stored on
    ``task.source_context["in_scope_outcome_ids"]`` so the smoke gate
    can require a matching ``VerificationSpec`` per outcome at
    completion time, and the description gains a "Verifications
    required" section so the agent knows what to declare.
    """

    @pytest.fixture
    def sample_tasks(self) -> list[Task]:
        return [
            create_test_task(
                "t1",
                "Implement engine",
                labels=["contract_first", "implementation"],
            ),
            create_test_task(
                "t2",
                "Implement renderer",
                labels=["contract_first", "implementation"],
            ),
        ]

    def test_in_scope_outcome_ids_stored_on_source_context(
        self, sample_tasks: list[Task]
    ) -> None:
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        outcomes = [
            _user_outcome("outcome_play", "user can play", "snake moves"),
            _user_outcome("outcome_score", "user can see score", "score updates"),
        ]
        task = IntegrationTaskGenerator.create_integration_task(
            sample_tasks, project_name="Snake", outcomes=outcomes
        )

        assert task is not None
        assert task.source_context is not None
        ids = task.source_context.get("in_scope_outcome_ids")
        assert ids == ["outcome_play", "outcome_score"]

    def test_out_of_scope_outcomes_filtered(self, sample_tasks: list[Task]) -> None:
        """Out-of-scope outcomes don't gate completion or grow description."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        outcomes = [
            _user_outcome("outcome_play", "user can play", "snake moves"),
            _user_outcome(
                "outcome_admin",
                "admin can do X",
                "admin tools visible",
                scope="out_of_scope",
            ),
        ]
        task = IntegrationTaskGenerator.create_integration_task(
            sample_tasks, project_name="Snake", outcomes=outcomes
        )

        assert task is not None
        ids = (task.source_context or {}).get("in_scope_outcome_ids")
        assert ids == ["outcome_play"]
        # Description names the in-scope outcome's user-facing action but not
        # the out-of-scope one (the self-verify prompt lists outcomes by
        # action — success_signal, the spine of "done means").
        assert "user can play" in task.description
        assert "admin can do X" not in task.description

    def test_no_outcomes_leaves_description_unchanged_default(
        self, sample_tasks: list[Task]
    ) -> None:
        """``outcomes=None`` preserves the legacy description shape.

        Legacy callers (feature-based path until follow-up wiring) get
        no Verifications section and no ``in_scope_outcome_ids``.  Issue
        #677 added a ``structural_category`` key to ``source_context``
        (defaulting to ``"unknown"``) so the smoke gate can judge
        behavior evidence — but with no behavior contract for the
        default category, the description is otherwise unchanged and the
        outcome-coverage key stays absent.
        """
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            sample_tasks, project_name="Snake"
        )
        assert task is not None
        # in_scope_outcome_ids must remain absent — the coverage rule
        # only applies when outcomes were wired (gate uses .get()).
        assert "in_scope_outcome_ids" not in (task.source_context or {})
        assert (task.source_context or {}).get("structural_category") == "unknown"
        assert "Verifications required" not in task.description
        assert "Behavior evidence required" not in task.description

    def test_empty_outcomes_list_stores_empty_list_not_none(
        self, sample_tasks: list[Task]
    ) -> None:
        """``outcomes=[]`` (extractor ran, no outcomes) stores empty list.

        Distinguishes "wiring present, nothing in scope" from "wiring
        absent" — the smoke gate's coverage check at completion needs
        this distinction to know whether to apply the rule.
        """
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            sample_tasks, project_name="Snake", outcomes=[]
        )
        assert task is not None
        assert task.source_context is not None
        assert task.source_context.get("in_scope_outcome_ids") == []

    def test_all_outcomes_out_of_scope_stores_empty_list(
        self, sample_tasks: list[Task]
    ) -> None:
        """All-out-of-scope input → empty in_scope_outcome_ids list."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        outcomes = [
            _user_outcome("o_oos", "user does X", "X visible", scope="out_of_scope"),
        ]
        task = IntegrationTaskGenerator.create_integration_task(
            sample_tasks, project_name="Snake", outcomes=outcomes
        )
        assert task is not None
        assert (task.source_context or {}).get("in_scope_outcome_ids") == []

    def test_enhance_project_with_integration_forwards_outcomes(
        self, sample_tasks: list[Task]
    ) -> None:
        """The top-level helper threads outcomes to the generator."""
        from src.integrations.integration_verification import (
            enhance_project_with_integration,
        )

        outcomes = [_user_outcome("outcome_play", "user can play", "snake moves")]
        result = enhance_project_with_integration(
            sample_tasks,
            "Build a snake game",
            "Snake",
            outcomes=outcomes,
        )

        integration_task = next((t for t in result if "integration" in t.labels), None)
        assert integration_task is not None
        assert (integration_task.source_context or {}).get("in_scope_outcome_ids") == [
            "outcome_play"
        ]


class TestBehaviorEvidenceThreading:
    """Issue #677: the integration task carries the per-type behavior contract.

    The description must require the agent to RUN the assembled product and
    capture *behavior evidence* against the per-type bar, and the task must
    stash ``structural_category`` on ``source_context`` so the product smoke
    gate can judge that evidence.  Non-web types must NOT be told to curl an
    HTTP endpoint — Marcus builds any software, not just web apps.
    """

    def test_stashes_structural_category_on_source_context(
        self, all_sample_tasks: list[Task]
    ) -> None:
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            all_sample_tasks, "Dashboard", structural_category="web app"
        )
        assert task is not None
        assert task.source_context is not None
        assert task.source_context["structural_category"] == "web app"

    def test_fuzzy_type_has_no_behavior_section(
        self, all_sample_tasks: list[Task]
    ) -> None:
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            all_sample_tasks, "Misc", structural_category="other"
        )
        assert task is not None
        assert "Behavior evidence required" not in task.description
        # ...but the category is still stashed for the gate's fallback logic.
        assert (task.source_context or {}).get("structural_category") == "other"

    def test_default_category_is_backward_compatible(
        self, all_sample_tasks: list[Task]
    ) -> None:
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            all_sample_tasks, "Dashboard"
        )
        assert task is not None
        assert "Behavior evidence required" not in task.description


class TestSelfVerifyPrompt:
    """Issue #677 rework: the integration prompt is short, generic, and
    criteria-based — it empowers the agent to RUN and self-verify the product
    with whatever tools, and states Marcus only runs the build."""

    def _outcomes(self):
        return [
            _user_outcome("o_play", "play the snake game", "a snake moves on screen"),
            _user_outcome("o_score", "see their score", "a score counter is visible"),
        ]

    def _desc(self, category="game", contract=None):
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            [create_test_task("impl-1", "Build engine", labels=["implement"])],
            project_name="Snake",
            outcomes=self._outcomes(),
            structural_category=category,
            contract_file=contract,
        )
        assert task is not None
        return task.description

    def test_lists_outcomes_as_the_spine(self) -> None:
        desc = self._desc()
        assert "play the snake game" in desc
        assert "see their score" in desc
        assert '"Done" means' in desc

    def test_tells_agent_to_actually_run_it_with_any_tool(self) -> None:
        desc = self._desc().lower()
        assert "run" in desc
        assert "whatever tools" in desc or "install a tool" in desc

    def test_frames_agent_as_skeptic_not_closer(self) -> None:
        desc = self._desc().lower()
        # A MAS built it and always leaves mistakes; find and fix them.
        assert "multi-agent system built this" in desc
        assert "find" in desc and "fix" in desc
        assert "not here to confirm it works" in desc

    def test_bans_the_unit_test_shortcut(self) -> None:
        # The exact dodge that shipped an idle game (test98): a green test
        # suite reported as "it works."
        desc = self._desc().lower()
        assert "unit-test suite is not proof" in desc
        assert "isolation" in desc

    def test_states_marcus_runs_no_check_no_safety_net(self) -> None:
        # Codex P1 on #679: the build floor was removed, so the prompt must
        # NOT promise Marcus runs the build (an agent could skip it expecting
        # Marcus to catch compile errors — no one would). It must make the
        # agent own build+run with no safety net.
        desc = self._desc().lower()
        assert "marcus does not run" in desc
        assert "no safety net" in desc
        assert "marcus runs the project's build" not in desc

    def test_is_generic_no_tech_specifics(self) -> None:
        # The body must not hardcode a stack/tool. (The contract preamble may
        # name a file path, so test the no-contract body.)
        desc = self._desc(contract=None).lower()
        for token in ["react", "npm ", "curl", "uvicorn", "pytest", "json", "vite"]:
            assert token not in desc, f"prompt leaked tech-specific token: {token!r}"

    def test_is_short(self) -> None:
        # The old wall was ~4,000 words. The self-verify prompt is a fraction.
        # Raised 350 -> 400 for the #627 redo-lane rule (glue in place,
        # logic failures via request_task_redo) — still <10% of the wall.
        assert len(self._desc(contract=None).split()) < 400

    def test_contract_preamble_preserved_when_contract_first(self) -> None:
        desc = self._desc(contract="docs/contract.md")
        assert "contract" in desc.lower()
        assert "docs/contract.md" in desc
        # Stale "Phase 1 / step 9" references from the old wall are gone.
        assert "Phase 1" not in desc
        assert "step 9" not in desc


class TestGotchaPropagationToSkeptic:
    """#680: gotchas stamped on impl tasks reach the integration (skeptic) task."""

    def test_gotcha_criteria_propagate_to_integration_task(self) -> None:
        """A GOTCHA-prefixed criterion on an impl task is copied onto the
        integration verification task's acceptance_criteria so the skeptic
        actively tests for that failure mode."""
        from src.integrations.integration_verification import (
            enhance_project_with_integration,
        )
        from src.marcus_mcp.coordinator.outcome_coverage import (
            GOTCHA_CRITERION_PREFIX,
        )

        gotcha = f"{GOTCHA_CRITERION_PREFIX}reversal is ignored, not instant death"
        impl = create_test_task(
            "impl-001", "Build snake movement", labels=["implement"]
        )
        impl.acceptance_criteria = [gotcha]

        result = enhance_project_with_integration([impl], "Build a snake game", "Snake")

        integration_task = next(t for t in result if "integration" in t.labels)
        assert gotcha in integration_task.acceptance_criteria

    def test_no_gotchas_leaves_integration_checklist_unchanged(self) -> None:
        """Impl tasks with no gotcha criteria add nothing extra to the skeptic."""
        from src.integrations.integration_verification import (
            enhance_project_with_integration,
        )
        from src.marcus_mcp.coordinator.outcome_coverage import (
            GOTCHA_CRITERION_PREFIX,
        )

        impl = create_test_task(
            "impl-001", "Build snake movement", labels=["implement"]
        )
        result = enhance_project_with_integration([impl], "Build a snake game", "Snake")

        integration_task = next(t for t in result if "integration" in t.labels)
        assert not any(
            c.startswith(GOTCHA_CRITERION_PREFIX)
            for c in integration_task.acceptance_criteria
        )


class TestIntegrationTaskRedoGuidance:
    """
    Issue #627: the integration description must teach the redo decision.

    When integration finds a sibling agent's completed work is
    substantively wrong, the agent should send it back via
    request_task_redo (clean attribution, Invariant #2) rather than
    rewrite the sibling's lane — but mechanical glue fixes at contract
    boundaries stay in-lane and should be fixed in place.
    """

    def test_description_names_request_task_redo(
        self, sample_implementation_tasks: list[Task]
    ) -> None:
        """The integration task description must mention the redo tool."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            sample_implementation_tasks,
            project_name="Snake Game",
        )

        assert task is not None
        assert "request_task_redo" in task.description

    def test_description_draws_the_glue_vs_logic_line(
        self, sample_implementation_tasks: list[Task]
    ) -> None:
        """The description distinguishes in-place glue fixes from redo-worthy logic failures."""
        from src.integrations.integration_verification import (
            IntegrationTaskGenerator,
        )

        task = IntegrationTaskGenerator.create_integration_task(
            sample_implementation_tasks,
            project_name="Snake Game",
        )

        assert task is not None
        description_lower = task.description.lower()
        assert "glue" in description_lower
        assert "redo" in description_lower
