"""Natural Language Task Processing Utilities.

Shared utilities for natural language task creation tools.
Eliminates code duplication between create_project and add_feature.
"""

import logging
from enum import Enum
from typing import Any, Dict, List

from src.core.models import Task

logger = logging.getLogger(__name__)


class TaskType(Enum):
    """Task type classification."""

    DESIGN = "design"
    DEPLOYMENT = "deployment"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"
    INTEGRATION = "integration"
    DOCUMENTATION = "documentation"
    INFRASTRUCTURE = "infrastructure"
    OTHER = "other"


class TaskClassifier:
    """Classify tasks by their type based on keywords."""

    # Keyword mappings for task classification
    TASK_KEYWORDS = {
        TaskType.DESIGN: [
            "design",
            "architect",
            "plan",
            "specification",
            "wireframe",
            "mockup",
            "diagram",
            "blueprint",
            "prototype",
            "architecture",
            "planning",
        ],
        TaskType.DEPLOYMENT: [
            "deploy",
            "release",
            "production",
            "launch",
            "rollout",
            "publish",
            "go-live",
            "deliver",
            "staging",
            "live",
        ],
        TaskType.IMPLEMENTATION: [
            "implement",
            "build",
            "create",
            "develop",
            "code",
            "construct",
            "write",
            "refactor",
            "program",
            "engineer",
            "fix",
            "bugfix",
            "bug",
            "patch",
            "hotfix",
            "repair",
            "resolve",
        ],
        TaskType.TESTING: [
            "test",
            "qa",
            "quality",
            "verify",
            "validate",
            "check",
            "assert",
            "unittest",
            "e2e",
            "coverage",
        ],
        TaskType.INTEGRATION: [
            "integration verification",
            "build verification",
            "smoke test",
            "startup verification",
            "system verification",
            "health check",
            "endpoint verification",
        ],
        TaskType.DOCUMENTATION: [
            "document",
            "docs",
            "readme",
            "guide",
            "tutorial",
            "manual",
            "wiki",
            "annotate",
            "comment",
        ],
        TaskType.INFRASTRUCTURE: [
            "setup",
            "configure",
            "install",
            "provision",
            "infrastructure",
            "database",
            "server",
            "environment",
            "docker",
            "kubernetes",
        ],
    }

    @classmethod
    def classify(cls, task: Task) -> TaskType:
        """
        Classify a task based on its name and description.

        Parameters
        ----------
        task : Task
            Task to classify

        Returns
        -------
        TaskType
            TaskType enum value
        """
        # Combine name and description for better classification
        text_to_check = f"{task.name} {task.description}".lower()

        # Check in priority order - more specific types first
        # Testing should be checked before Implementation to catch "Write tests" tasks
        priority_order = [
            TaskType.DEPLOYMENT,  # Most specific - deployment keywords are unique
            TaskType.INTEGRATION,  # Check before testing (multi-word keywords)
            TaskType.TESTING,  # Check before implementation to catch "write tests"
            TaskType.DOCUMENTATION,  # Check before implementation to catch "write docs"
            TaskType.DESIGN,  # Check before implementation to catch "design API"
            TaskType.INFRASTRUCTURE,  # Specific setup/config tasks
            TaskType.IMPLEMENTATION,  # Most general - catches remaining dev work
        ]

        for task_type in priority_order:
            keywords = cls.TASK_KEYWORDS.get(task_type, [])
            if any(keyword in text_to_check for keyword in keywords):
                return task_type

        return TaskType.OTHER

    @classmethod
    def is_type(cls, task: Task, task_type: TaskType) -> bool:
        """Check if a task is of a specific type."""
        return cls.classify(task) == task_type

    @classmethod
    def filter_by_type(cls, tasks: List[Task], task_type: TaskType) -> List[Task]:
        """Filter tasks by type."""
        return [task for task in tasks if cls.classify(task) == task_type]


class TaskBuilder:
    """Build task data structures for kanban board creation."""

    @staticmethod
    def build_task_data(task: Task) -> Dict[str, Any]:
        """
        Build a dictionary of task data for kanban board creation.

        Parameters
        ----------
        task : Task
            Task object to convert

        Returns
        -------
        Dict[str, Any]
            Dictionary with task data ready for kanban API
        """
        # Convert status to string value
        status_value = (
            task.status.value if hasattr(task.status, "value") else task.status
        )

        # DEBUG: Log status conversion for About tasks
        if "About" in task.name:
            logger.info(
                f"[DEBUG] build_task_data for '{task.name}': "
                f"task.status={task.status} (type: {type(task.status).__name__}), "
                f"status_value='{status_value}' (type: {type(status_value).__name__})"
            )

        result = {
            "name": task.name,
            "description": task.description,
            "priority": (
                task.priority.value
                if hasattr(task.priority, "value")
                else task.priority
            ),
            "labels": task.labels,
            "estimated_hours": task.estimated_hours,
            "dependencies": task.dependencies,
            # Store the original task ID for dependency mapping
            "original_id": task.id,
            # Include acceptance criteria if available
            "acceptance_criteria": getattr(task, "acceptance_criteria", []),
            # Include completion criteria — list of behavior strings
            # populated by #607 step 3 (test-coverage criteria) and
            # step 4 (gap-fill rollup). Without this line both steps'
            # output was silently dropped before kanban persistence,
            # making the field empty for every task in the DB and
            # both PRs functionally inert in production despite
            # passing unit tests. Persisted by sqlite_kanban as a
            # JSON blob; the persistence path gates on truthiness,
            # so passing ``None`` here is the right "no criteria"
            # signal for non-feature tasks (design / NFR / infra).
            "completion_criteria": getattr(task, "completion_criteria", None),
            # Include subtasks if available
            "subtasks": getattr(task, "subtasks", []),
            # Additional fields that might be needed
            "status": status_value,
            "assigned_to": task.assigned_to,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "metadata": {"ai_generated": True, "source": "natural_language"},
        }

        # Pass through source_type and source_context if present (GH-297)
        source_type = getattr(task, "source_type", None)
        if source_type:
            result["source_type"] = source_type
        source_context = getattr(task, "source_context", None)
        if source_context:
            result["source_context"] = source_context

        # Issue #911 (ADR-0013 K2): the typed-chain fields. ``task_type``
        # and ``inputs`` always travel (their defaults, "implement" and
        # {}, are exactly what the SQLite provider minted when they were
        # absent, so an untyped task's card is unchanged). The caller's
        # own ``id`` travels only for a seeded task whose spec supplied
        # one -- a decomposer's "task_1" is a placeholder the dependency
        # remap replaces, and must keep being replaced.
        result["task_type"] = getattr(task, "task_type", "implement") or "implement"
        result["inputs"] = dict(getattr(task, "inputs", None) or {})
        if source_type == "seeded" and (source_context or {}).get("caller_id"):
            result["id"] = task.id

        # DEBUG: Verify status is in result for About tasks
        if "About" in task.name:
            logger.info(
                f"[DEBUG] build_task_data result for '{task.name}': "
                f"'status' in result={('status' in result)}, "
                f"result['status']='{result.get('status')}'"
            )

        return result

    @staticmethod
    def build_minimal_task_data(task: Task) -> Dict[str, Any]:
        """Build minimal task data (for APIs with fewer fields)."""
        return {
            "name": task.name,
            "description": task.description,
            "priority": (
                task.priority.value
                if hasattr(task.priority, "value")
                else task.priority
            ),
            "labels": task.labels,
        }


class SafetyChecker:
    """Apply safety checks to ensure logical task ordering."""

    def __init__(self) -> None:
        """Initialize SafetyChecker with enhanced task classifier."""
        from src.integrations.enhanced_task_classifier import EnhancedTaskClassifier

        self.task_classifier = EnhancedTaskClassifier()

    def apply_deployment_dependencies(self, tasks: List[Task]) -> List[Task]:
        """
        Ensure deployment tasks depend on implementation and testing tasks.

        This prevents premature deployment by establishing proper dependencies.

        Parameters
        ----------
        tasks : List[Task]
            List of tasks to check

        Returns
        -------
        List[Task]
            List of tasks with updated dependencies
        """
        deployment_tasks = self.task_classifier.filter_by_type(
            tasks, TaskType.DEPLOYMENT
        )
        implementation_tasks = self.task_classifier.filter_by_type(
            tasks, TaskType.IMPLEMENTATION
        )
        testing_tasks = self.task_classifier.filter_by_type(tasks, TaskType.TESTING)

        for deploy_task in deployment_tasks:
            # Ensure deployment depends on ALL implementation tasks
            for impl_task in implementation_tasks:
                if impl_task.id not in deploy_task.dependencies:
                    deploy_task.dependencies.append(impl_task.id)
                    logger.debug(
                        f"Added dependency: {deploy_task.name} depends on "
                        f"{impl_task.name}"
                    )

            # Ensure deployment depends on ALL testing tasks
            for test_task in testing_tasks:
                if test_task.id not in deploy_task.dependencies:
                    deploy_task.dependencies.append(test_task.id)
                    logger.debug(
                        f"Added dependency: {deploy_task.name} depends on "
                        f"{test_task.name}"
                    )

        return tasks

    def apply_testing_dependencies(self, tasks: List[Task]) -> List[Task]:
        """
        Ensure testing tasks depend on implementation tasks.

        Parameters
        ----------
        tasks : List[Task]
            List of tasks to check

        Returns
        -------
        List[Task]
            List of tasks with updated dependencies
        """
        testing_tasks = self.task_classifier.filter_by_type(tasks, TaskType.TESTING)
        implementation_tasks = self.task_classifier.filter_by_type(
            tasks, TaskType.IMPLEMENTATION
        )

        for test_task in testing_tasks:
            # Find related implementation tasks (by matching labels or keywords)
            related_impl_tasks = SafetyChecker._find_related_tasks(
                test_task, implementation_tasks
            )

            if not related_impl_tasks:
                logger.warning(
                    f"No related implementation tasks found for test task "
                    f"'{test_task.name}' with labels: {test_task.labels}"
                )
            else:
                logger.info(
                    f"Found {len(related_impl_tasks)} related implementation "
                    f"tasks for '{test_task.name}'"
                )

            for impl_task in related_impl_tasks:
                if impl_task.id not in test_task.dependencies:
                    test_task.dependencies.append(impl_task.id)
                    logger.info(
                        f"Added dependency: {test_task.name} depends on "
                        f"{impl_task.name}"
                    )

        return tasks

    def apply_implementation_dependencies(self, tasks: List[Task]) -> List[Task]:
        """
        Ensure implementation tasks depend on design tasks.

        Supports both bundled domain designs (GH-108) and per-feature designs
        for backward compatibility with existing workflows.

        Bundled designs have IDs like: design_user_authentication
        Per-feature designs have IDs like: task_user-login_design

        Parameters
        ----------
        tasks : List[Task]
            List of tasks to check

        Returns
        -------
        List[Task]
            List of tasks with updated dependencies
        """
        design_tasks = self.task_classifier.filter_by_type(tasks, TaskType.DESIGN)
        implementation_tasks = self.task_classifier.filter_by_type(
            tasks, TaskType.IMPLEMENTATION
        )

        # Separate bundled and per-feature designs (GH-108)
        bundled_design_tasks = [
            dt for dt in design_tasks if dt.id.startswith("design_")
        ]
        per_feature_design_tasks = [
            dt for dt in design_tasks if not dt.id.startswith("design_")
        ]

        logger.info(
            f"Found {len(bundled_design_tasks)} bundled domain designs, "
            f"{len(per_feature_design_tasks)} per-feature designs"
        )

        for impl_task in implementation_tasks:
            # Try bundled designs first (domain-level, GH-108)
            related_design_tasks = SafetyChecker._find_related_tasks(
                impl_task, bundled_design_tasks
            )

            # Fallback to per-feature designs for backward compatibility
            if not related_design_tasks:
                related_design_tasks = SafetyChecker._find_related_tasks(
                    impl_task, per_feature_design_tasks
                )
                if related_design_tasks:
                    logger.debug(
                        f"Using per-feature design dependencies for "
                        f"'{impl_task.name}' (bundled designs not found)"
                    )

            if not related_design_tasks:
                logger.debug(f"No design dependencies found for '{impl_task.name}'")
            else:
                logger.info(
                    f"Found {len(related_design_tasks)} design tasks "
                    f"for '{impl_task.name}'"
                )

            for design_task in related_design_tasks:
                if design_task.id not in impl_task.dependencies:
                    impl_task.dependencies.append(design_task.id)
                    logger.info(
                        f"Added design dependency: {impl_task.name} "
                        f"depends on {design_task.name}"
                    )

        return tasks

    @staticmethod
    def _find_related_tasks(task: Task, candidate_tasks: List[Task]) -> List[Task]:
        """Find tasks that are related based on labels and keywords."""
        related = []

        # Extract feature labels from task
        task_feature_labels = {
            label for label in task.labels if label.startswith("feature:")
        }

        for candidate in candidate_tasks:
            # First priority: Check feature label overlap (tasks in same feature)
            candidate_feature_labels = {
                label for label in candidate.labels if label.startswith("feature:")
            }
            if task_feature_labels & candidate_feature_labels:
                related.append(candidate)
                continue

            # Second priority: Check component label overlap
            task_component_labels = {
                label for label in task.labels if label.startswith("component:")
            }
            candidate_component_labels = {
                label for label in candidate.labels if label.startswith("component:")
            }
            if task_component_labels & candidate_component_labels:
                related.append(candidate)
                continue

            # Third priority: Check any label overlap (excluding type labels)
            task_other_labels = set(task.labels) - {
                label for label in task.labels if label.startswith("type:")
            }
            candidate_other_labels = set(candidate.labels) - {
                label for label in candidate.labels if label.startswith("type:")
            }
            if task_other_labels & candidate_other_labels:
                related.append(candidate)
                continue

            # Fourth priority: Check keyword similarity in names
            task_words = set(task.name.lower().split())
            candidate_words = set(candidate.name.lower().split())
            # Remove common words
            common_words = {
                "the",
                "a",
                "an",
                "and",
                "or",
                "for",
                "to",
                "in",
                "of",
                "design",
                "implement",
                "test",
                "create",
                "build",
                "develop",
            }
            task_words -= common_words
            candidate_words -= common_words

            # Need at least 2 matching words for keyword-based relation
            if len(task_words & candidate_words) >= 2:
                related.append(candidate)

        return related

    @staticmethod
    def validate_dependencies(tasks: List[Task]) -> List[str]:
        """
        Validate that all dependencies reference existing tasks.

        Parameters
        ----------
        tasks : List[Task]
            List of tasks to validate

        Returns
        -------
        List[str]
            List of validation errors (empty if valid)
        """
        errors = []
        task_ids = {task.id for task in tasks}

        for task in tasks:
            for dep_id in task.dependencies:
                if dep_id not in task_ids:
                    errors.append(
                        f"Task '{task.name}' has invalid dependency '{dep_id}'"
                    )

        return errors
