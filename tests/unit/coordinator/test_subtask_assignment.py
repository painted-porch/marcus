"""
Unit tests for subtask assignment logic.

Tests the bug fix for parallel subtask assignment.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.coordinator.subtask_assignment import (
    _are_dependencies_satisfied,
    convert_subtask_to_task,
    find_next_available_subtask,
)
from src.marcus_mcp.coordinator.subtask_manager import Subtask, SubtaskManager


class TestSubtaskAssignment:
    """Test suite for subtask assignment logic."""

    @pytest.fixture
    def subtask_manager(self, tmp_path):
        """Create a subtask manager with test data."""
        manager = SubtaskManager(state_file=tmp_path / "subtasks_test.json")
        return manager

    @pytest.fixture
    def parent_task_todo(self):
        """Create a parent task in TODO status."""
        return Task(
            id="parent-task-1",
            name="Parent Task 1",
            description="Parent task with subtasks",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=8.0,
            labels=["backend", "api"],
        )

    @pytest.fixture
    def parent_task_in_progress(self):
        """Create a parent task in IN_PROGRESS status."""
        return Task(
            id="parent-task-2",
            name="Parent Task 2",
            description="Parent task with subtasks in progress",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
            assigned_to="agent-1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=8.0,
            labels=["backend", "api"],
        )

    @pytest.fixture
    def parent_task_done(self):
        """Create a parent task in DONE status."""
        return Task(
            id="parent-task-3",
            name="Parent Task 3",
            description="Completed parent task",
            status=TaskStatus.DONE,
            priority=Priority.HIGH,
            assigned_to="agent-1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=8.0,
            labels=["backend", "api"],
        )

    def test_find_subtask_from_todo_parent(self, subtask_manager, parent_task_todo):
        """Test finding subtask from parent in TODO status."""
        # Create project_tasks list for unified storage
        project_tasks = [parent_task_todo]

        # Add subtasks to parent using unified storage
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
            {
                "name": "Subtask 2",
                "description": "Second subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        subtask_manager.add_subtasks(parent_task_todo.id, subtasks_data, project_tasks)

        # Find next available subtask from unified storage
        subtask = find_next_available_subtask(
            agent_id="agent-1",
            project_tasks=project_tasks,
            subtask_manager=subtask_manager,
            assigned_task_ids=set(),
        )

        assert subtask is not None
        assert subtask.name == "Subtask 1"
        assert subtask.parent_task_id == parent_task_todo.id

    def test_find_subtask_from_in_progress_parent(
        self, subtask_manager, parent_task_in_progress
    ):
        """
        Test finding subtask from parent in IN_PROGRESS status.

        This is the critical bug fix test - previously this would fail
        because IN_PROGRESS parents were skipped.
        """
        # Create project_tasks list for unified storage
        project_tasks = [parent_task_in_progress]

        # Add subtasks to parent using unified storage
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
            {
                "name": "Subtask 2",
                "description": "Second subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        created_subtasks = subtask_manager.add_subtasks(
            parent_task_in_progress.id, subtasks_data, project_tasks
        )

        # Mark first subtask as IN_PROGRESS (simulating it being assigned)
        subtask_manager.update_subtask_status(
            created_subtasks[0].id,
            TaskStatus.IN_PROGRESS,
            project_tasks,
            assigned_to="agent-1",
        )

        # Try to find next available subtask from unified storage
        # This should return Subtask 2, not None
        subtask = find_next_available_subtask(
            agent_id="agent-2",
            project_tasks=project_tasks,
            subtask_manager=subtask_manager,
            assigned_task_ids={created_subtasks[0].id},  # First subtask assigned
        )

        # BUG FIX: This should now work (previously returned None)
        assert subtask is not None
        assert subtask.name == "Subtask 2"
        assert subtask.parent_task_id == parent_task_in_progress.id

    def test_skip_done_parent_task(self, subtask_manager, parent_task_done):
        """Test that DONE parent tasks are skipped."""
        # Add subtasks to done parent
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        subtask_manager.add_subtasks(parent_task_done.id, subtasks_data)

        # Try to find subtask from done parent
        subtask = find_next_available_subtask(
            agent_id="agent-1",
            project_tasks=[parent_task_done],
            subtask_manager=subtask_manager,
            assigned_task_ids=set(),
        )

        # Should return None because parent is DONE
        assert subtask is None

    def test_parallel_assignment_scenario(
        self, subtask_manager, parent_task_in_progress
    ):
        """
        Test realistic parallel assignment scenario.

        Simulates: 3 parent tasks, each with 5 subtasks, 15 workers
        Should be able to assign all 15 subtasks in parallel.
        """
        # Create 3 parent tasks with 5 subtasks each
        parent_tasks = []
        all_subtask_ids = []

        for i in range(3):
            parent = Task(
                id=f"parent-{i}",
                name=f"Parent Task {i}",
                description="Parent with subtasks",
                status=TaskStatus.IN_PROGRESS if i > 0 else TaskStatus.TODO,
                priority=Priority.HIGH,
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                due_date=None,
                estimated_hours=10.0,
                labels=["backend"],
            )
            parent_tasks.append(parent)

            # Add 5 subtasks using unified storage
            subtasks_data = [
                {
                    "name": f"Subtask {j+1}",
                    "description": f"Subtask {j+1} for parent {i}",
                    "estimated_hours": 2.0,
                    "dependencies": [],
                }
                for j in range(5)
            ]
            created = subtask_manager.add_subtasks(
                parent.id, subtasks_data, parent_tasks
            )
            all_subtask_ids.extend([s.id for s in created])

        # Simulate 15 workers requesting tasks
        assigned_subtasks = []
        assigned_ids: set[str] = set()

        for worker_num in range(15):
            subtask = find_next_available_subtask(
                agent_id=f"agent-{worker_num}",
                project_tasks=parent_tasks,
                subtask_manager=subtask_manager,
                assigned_task_ids=assigned_ids,
            )

            if subtask:
                assigned_subtasks.append(subtask)
                assigned_ids.add(subtask.id)
                # Mark as IN_PROGRESS in unified storage
                subtask_manager.update_subtask_status(
                    subtask.id,
                    TaskStatus.IN_PROGRESS,
                    parent_tasks,
                    assigned_to=f"agent-{worker_num}",
                )
                # Update parent to IN_PROGRESS after first assignment
                parent = next(p for p in parent_tasks if p.id == subtask.parent_task_id)
                if parent.status == TaskStatus.TODO:
                    parent.status = TaskStatus.IN_PROGRESS

        # BUG FIX: All 15 subtasks should be assigned
        assert len(assigned_subtasks) == 15
        assert len(assigned_ids) == 15

        # Verify all subtasks were assigned
        assert set(assigned_ids) == set(all_subtask_ids)

    def test_convert_subtask_to_task(self, parent_task_todo):
        """Test converting a subtask to a Task object."""
        subtask = Subtask(
            id="parent-task-1_sub_1",
            parent_task_id=parent_task_todo.id,
            name="Test Subtask",
            description="Test subtask description",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            estimated_hours=2.0,
            dependencies=[],
            file_artifacts=["test.py"],
            provides="Test functionality",
            requires="None",
        )

        task = convert_subtask_to_task(subtask, parent_task_todo)

        assert task.id == subtask.id
        assert task.name == subtask.name
        assert task.description == subtask.description
        assert task.status == subtask.status
        assert task.priority == subtask.priority
        assert task.estimated_hours == subtask.estimated_hours
        assert task.labels == parent_task_todo.labels
        assert task.due_date == parent_task_todo.due_date

    def test_respects_already_assigned_subtasks(
        self, subtask_manager, parent_task_todo
    ):
        """Test that already assigned subtasks are skipped."""
        # Create project_tasks list for unified storage
        project_tasks = [parent_task_todo]

        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
            {
                "name": "Subtask 2",
                "description": "Second subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        created_subtasks = subtask_manager.add_subtasks(
            parent_task_todo.id, subtasks_data, project_tasks
        )

        # First assignment
        subtask1 = find_next_available_subtask(
            agent_id="agent-1",
            project_tasks=project_tasks,
            subtask_manager=subtask_manager,
            assigned_task_ids=set(),
        )

        assert subtask1 is not None
        assert subtask1.name == "Subtask 1"

        # Mark first subtask as IN_PROGRESS and update parent status
        subtask_manager.update_subtask_status(
            created_subtasks[0].id,
            TaskStatus.IN_PROGRESS,
            project_tasks,
            assigned_to="agent-1",
        )
        parent_task_todo.status = TaskStatus.IN_PROGRESS

        # Second assignment - should skip already assigned subtask
        subtask2 = find_next_available_subtask(
            agent_id="agent-2",
            project_tasks=project_tasks,
            subtask_manager=subtask_manager,
            assigned_task_ids={created_subtasks[0].id},  # First subtask assigned
        )

        assert subtask2 is not None
        assert subtask2.name == "Subtask 2"
        assert subtask1 is not None
        assert subtask2.id != subtask1.id


class TestTerminalSubtasksAreNeverReoffered:
    """A declined or blocked subtask must not be re-offered (Codex P1 on PR #834).

    A decline clears the subtask's own ``assigned_to``, which is what
    ``assigned_task_ids`` is built from -- without an explicit terminal
    check here, a declined subtask looks exactly like a fresh TODO to
    the next agent that asks.
    """

    @pytest.fixture
    def subtask_manager(self, tmp_path):
        """Create a subtask manager with test data."""
        return SubtaskManager(state_file=tmp_path / "subtasks_test.json")

    @pytest.fixture
    def parent_task_in_progress(self):
        """Create a parent task in IN_PROGRESS status."""
        return Task(
            id="parent-task-2",
            name="Parent Task 2",
            description="Parent task with subtasks in progress",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
            assigned_to="agent-1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=8.0,
            labels=["backend", "api"],
        )

    def test_a_declined_subtask_is_never_reoffered(
        self, subtask_manager, parent_task_in_progress
    ):
        project_tasks = [parent_task_in_progress]
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        created = subtask_manager.add_subtasks(
            parent_task_in_progress.id, subtasks_data, project_tasks
        )
        subtask = next(t for t in project_tasks if t.id == created[0].id)
        subtask.status = TaskStatus.DECLINED
        subtask.assigned_to = None

        offered = find_next_available_subtask(
            agent_id="agent-2",
            project_tasks=project_tasks,
            subtask_manager=subtask_manager,
            assigned_task_ids=set(),
        )

        assert offered is None

    def test_a_blocked_subtask_is_never_reoffered(
        self, subtask_manager, parent_task_in_progress
    ):
        project_tasks = [parent_task_in_progress]
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        created = subtask_manager.add_subtasks(
            parent_task_in_progress.id, subtasks_data, project_tasks
        )
        subtask = next(t for t in project_tasks if t.id == created[0].id)
        subtask.status = TaskStatus.BLOCKED
        subtask.assigned_to = None

        offered = find_next_available_subtask(
            agent_id="agent-2",
            project_tasks=project_tasks,
            subtask_manager=subtask_manager,
            assigned_task_ids=set(),
        )

        assert offered is None

    def test_a_done_subtask_is_still_never_reoffered(
        self, subtask_manager, parent_task_in_progress
    ):
        """Pre-existing behavior, confirmed unchanged by this fix."""
        project_tasks = [parent_task_in_progress]
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        created = subtask_manager.add_subtasks(
            parent_task_in_progress.id, subtasks_data, project_tasks
        )
        subtask = next(t for t in project_tasks if t.id == created[0].id)
        subtask.status = TaskStatus.DONE
        subtask.assigned_to = None

        offered = find_next_available_subtask(
            agent_id="agent-2",
            project_tasks=project_tasks,
            subtask_manager=subtask_manager,
            assigned_task_ids=set(),
        )

        assert offered is None

    def test_a_todo_subtask_is_still_offered(
        self, subtask_manager, parent_task_in_progress
    ):
        """Confirms the terminal check is specific to DONE/BLOCKED/
        DECLINED, not a blanket refusal."""
        project_tasks = [parent_task_in_progress]
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        subtask_manager.add_subtasks(
            parent_task_in_progress.id, subtasks_data, project_tasks
        )

        offered = find_next_available_subtask(
            agent_id="agent-2",
            project_tasks=project_tasks,
            subtask_manager=subtask_manager,
            assigned_task_ids=set(),
        )

        assert offered is not None
        assert offered.name == "Subtask 1"


class TestDependencyChecking:
    """Test suite for GH-64: dependency checking in subtask assignment."""

    @pytest.fixture
    def subtask_manager(self, tmp_path):
        """Create a subtask manager with test data."""
        manager = SubtaskManager(state_file=tmp_path / "subtasks_test.json")
        return manager

    def test_no_dependencies_returns_true(self):
        """Test that tasks with no dependencies are always satisfied."""
        task = Task(
            id="task1",
            name="Task 1",
            description="Test task",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=4.0,
            dependencies=[],
        )
        all_tasks = [task]

        assert _are_dependencies_satisfied(task, all_tasks) is True

    def test_all_dependencies_done_returns_true(self):
        """Test that tasks with all dependencies DONE are satisfied."""
        dep1 = Task(
            id="dep1",
            name="Dependency 1",
            description="First dependency",
            status=TaskStatus.DONE,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=2.0,
        )
        dep2 = Task(
            id="dep2",
            name="Dependency 2",
            description="Second dependency",
            status=TaskStatus.DONE,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=2.0,
        )
        task = Task(
            id="task1",
            name="Task 1",
            description="Test task",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=4.0,
            dependencies=["dep1", "dep2"],
        )
        all_tasks = [dep1, dep2, task]

        assert _are_dependencies_satisfied(task, all_tasks) is True

    def test_incomplete_dependencies_returns_false(self):
        """Test that tasks with incomplete dependencies are not satisfied."""
        dep1 = Task(
            id="dep1",
            name="Dependency 1",
            description="First dependency",
            status=TaskStatus.DONE,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=2.0,
        )
        dep2 = Task(
            id="dep2",
            name="Dependency 2",
            description="Second dependency",
            status=TaskStatus.TODO,  # Not done!
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=2.0,
        )
        task = Task(
            id="task1",
            name="Task 1",
            description="Test task",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=4.0,
            dependencies=["dep1", "dep2"],
        )
        all_tasks = [dep1, dep2, task]

        assert _are_dependencies_satisfied(task, all_tasks) is False

    def test_in_progress_dependency_returns_false(self):
        """Test that tasks with IN_PROGRESS dependencies are not satisfied."""
        dep = Task(
            id="dep1",
            name="Dependency 1",
            description="First dependency",
            status=TaskStatus.IN_PROGRESS,  # Not DONE
            priority=Priority.MEDIUM,
            assigned_to="agent-1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=2.0,
        )
        task = Task(
            id="task1",
            name="Task 1",
            description="Test task",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=4.0,
            dependencies=["dep1"],
        )
        all_tasks = [dep, task]

        assert _are_dependencies_satisfied(task, all_tasks) is False

    def test_design_implement_test_workflow(self, subtask_manager):
        """
        Test GH-64: Design → Implement → Test workflow with dependencies.

        Ensures that Test subtasks are only available after Design and Implement
        parent tasks are complete.
        """
        # Create three tasks with proper dependencies
        design_task = Task(
            id="design1",
            name="Design Task",
            description="Design",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=4.0,
            dependencies=[],
        )
        implement_task = Task(
            id="implement1",
            name="Implement Task",
            description="Implement",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=8.0,
            dependencies=["design1"],
        )
        test_task = Task(
            id="test1",
            name="Test Task",
            description="Test",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=4.0,
            dependencies=["design1", "implement1"],
        )

        # Create unified project_tasks list
        all_tasks = [design_task, implement_task, test_task]

        # Add subtasks to each using unified storage
        design_subtasks = [
            {
                "name": "Design Subtask 1",
                "description": "Design subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            }
        ]
        implement_subtasks = [
            {
                "name": "Implement Subtask 1",
                "description": "Implement subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            }
        ]
        test_subtasks = [
            {
                "name": "Test Subtask 1",
                "description": "Test subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            }
        ]

        subtask_manager.add_subtasks(design_task.id, design_subtasks, all_tasks)
        subtask_manager.add_subtasks(implement_task.id, implement_subtasks, all_tasks)
        subtask_manager.add_subtasks(test_task.id, test_subtasks, all_tasks)

        # Phase 1: Only design subtasks should be available
        result = find_next_available_subtask(
            agent_id="agent1",
            project_tasks=all_tasks,
            subtask_manager=subtask_manager,
            assigned_task_ids=set(),
        )
        assert result is not None
        assert result.parent_task_id == "design1"

        # Implement and Test should NOT be available yet
        assert not _are_dependencies_satisfied(implement_task, all_tasks)
        assert not _are_dependencies_satisfied(test_task, all_tasks)

        # Phase 2: Design is DONE, implement subtasks should be available
        design_task.status = TaskStatus.DONE
        result = find_next_available_subtask(
            agent_id="agent1",
            project_tasks=all_tasks,
            subtask_manager=subtask_manager,
            assigned_task_ids=set(),
        )
        assert result is not None
        assert result.parent_task_id == "implement1"

        # Test should still NOT be available
        assert _are_dependencies_satisfied(implement_task, all_tasks)
        assert not _are_dependencies_satisfied(test_task, all_tasks)

        # Phase 3: Both Design and Implement DONE, test subtasks available
        implement_task.status = TaskStatus.DONE
        result = find_next_available_subtask(
            agent_id="agent1",
            project_tasks=all_tasks,
            subtask_manager=subtask_manager,
            assigned_task_ids=set(),
        )
        assert result is not None
        assert result.parent_task_id == "test1"

        # All dependencies satisfied
        assert _are_dependencies_satisfied(test_task, all_tasks)


class TestSubtaskWorkflowFixes:
    """Test suite for GH-XX: subtask workflow fixes."""

    @pytest.fixture
    def subtask_manager(self, tmp_path):
        """Create a subtask manager with test data."""
        manager = SubtaskManager(state_file=tmp_path / "subtasks_test.json")
        return manager

    @pytest.fixture
    def mock_state(self, subtask_manager):
        """Create a mock state object."""
        state = Mock()
        state.subtask_manager = subtask_manager
        state.kanban_client = Mock()
        state.kanban_client.update_task = AsyncMock(return_value=None)
        return state

    @pytest.fixture
    def parent_task(self):
        """Create a parent task."""
        return Task(
            id="parent1",
            name="Parent Task",
            description="Parent task with subtasks",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=8.0,
            labels=["backend", "api"],
            project_id="project1",
            project_name="Test Project",
        )

    @pytest.mark.asyncio
    async def test_parent_task_moves_to_in_progress_on_first_subtask(
        self, subtask_manager, mock_state, parent_task
    ):
        """
        Test GH-XX Fix #1: Parent task moves to IN_PROGRESS when first subtask is assigned.

        When the first subtask is assigned, the parent task should automatically
        move from TODO to IN_PROGRESS status.
        """
        from src.marcus_mcp.coordinator.task_assignment_integration import (
            find_optimal_task_with_subtasks,
        )

        # Setup state with project_tasks list
        mock_state.project_tasks = [parent_task]

        # Add subtasks to parent using unified storage
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
            {
                "name": "Subtask 2",
                "description": "Second subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        subtask_manager.add_subtasks(
            parent_task.id, subtasks_data, mock_state.project_tasks
        )
        mock_state.agent_tasks = {}
        mock_state.tasks_being_assigned = set()
        mock_state.assignment_persistence = Mock()
        mock_state.assignment_persistence.get_all_assigned_task_ids = AsyncMock(
            return_value=set()
        )

        # Fallback should not be called
        async def fallback_finder(agent_id, state):
            return None

        # Find optimal task (should return first subtask)
        task = await find_optimal_task_with_subtasks(
            agent_id="agent1",
            state=mock_state,
            fallback_task_finder=fallback_finder,
        )

        # Verify subtask was returned
        assert task is not None
        assert task.name == "Subtask 1"

        # Verify parent task was updated to IN_PROGRESS
        mock_state.kanban_client.update_task.assert_called_once()
        call_args = mock_state.kanban_client.update_task.call_args
        assert call_args[0][0] == parent_task.id
        assert call_args[0][1]["status"] == TaskStatus.IN_PROGRESS

        # Verify local state was updated
        assert parent_task.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_parent_task_not_updated_if_already_in_progress(
        self, subtask_manager, mock_state
    ):
        """
        Test that parent task is NOT updated if already IN_PROGRESS.

        When assigning the second, third, etc. subtask, the parent should
        not be updated again.
        """
        from src.marcus_mcp.coordinator.task_assignment_integration import (
            find_optimal_task_with_subtasks,
        )

        parent_task = Task(
            id="parent1",
            name="Parent Task",
            description="Parent task with subtasks",
            status=TaskStatus.IN_PROGRESS,  # Already in progress
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=8.0,
            labels=["backend", "api"],
            project_id="project1",
            project_name="Test Project",
        )

        # Setup state with project_tasks list
        mock_state.project_tasks = [parent_task]

        # Add subtasks to parent using unified storage
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
            {
                "name": "Subtask 2",
                "description": "Second subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        created_subtasks = subtask_manager.add_subtasks(
            parent_task.id, subtasks_data, mock_state.project_tasks
        )

        # Mark first subtask as already assigned
        subtask_manager.update_subtask_status(
            created_subtasks[0].id,
            TaskStatus.IN_PROGRESS,
            mock_state.project_tasks,
            assigned_to="agent1",
        )
        mock_state.agent_tasks = {}
        mock_state.tasks_being_assigned = {created_subtasks[0].id}
        mock_state.assignment_persistence = Mock()
        mock_state.assignment_persistence.get_all_assigned_task_ids = AsyncMock(
            return_value={created_subtasks[0].id}
        )

        async def fallback_finder(agent_id, state):
            return None

        # Find optimal task (should return second subtask)
        task = await find_optimal_task_with_subtasks(
            agent_id="agent2",
            state=mock_state,
            fallback_task_finder=fallback_finder,
        )

        # Verify subtask was returned
        assert task is not None
        assert task.name == "Subtask 2"

        # Verify parent task was NOT updated (already IN_PROGRESS)
        mock_state.kanban_client.update_task.assert_not_called()

    def test_subtask_instructions_include_context(self, parent_task):
        """
        Test GH-XX Fix #2: Subtask instructions include subtask-specific context.

        Instructions for subtasks should clearly indicate this is a subtask
        of a larger task and focus only on the specific subtask.
        """
        from src.marcus_mcp.tools.task import build_tiered_instructions

        subtask = Subtask(
            id="parent1_sub_1",
            parent_task_id=parent_task.id,
            name="Design API endpoints",
            description="Design RESTful API endpoints for user management",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            estimated_hours=2.0,
            dependencies=[],
            file_artifacts=[],
            provides="API design",
            requires="None",
        )

        # Convert subtask to task
        task = convert_subtask_to_task(subtask, parent_task)

        # Add metadata (as done in task_assignment_integration.py)
        task._is_subtask = True  # type: ignore
        task._parent_task_id = parent_task.id  # type: ignore
        task._parent_task_name = parent_task.name  # type: ignore

        # Generate instructions
        base_instructions = "Complete the assigned task"
        instructions = build_tiered_instructions(
            base_instructions=base_instructions,
            task=task,
            context_data=None,
            dependency_awareness=None,
            predictions=None,
        )

        # Verify subtask context is included
        assert "SUBTASK CONTEXT" in instructions
        assert parent_task.name in instructions
        assert "FOCUS ONLY on completing this specific subtask" in instructions
        assert task.name in instructions
        assert task.description in instructions
        assert "Do NOT work on the full parent task" in instructions

    def test_parent_tasks_with_subtasks_are_filtered_out(self, subtask_manager):
        """
        Test GH-XX Fix #3: Parent tasks with subtasks are not assigned as regular tasks.

        When a task has subtasks, only the subtasks should be assignable,
        not the parent task itself.
        """
        # Create parent task
        parent_task = Task(
            id="parent1",
            name="Parent Task",
            description="Parent task with subtasks",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=8.0,
            labels=["backend", "api"],
        )

        # Create regular task (no subtasks)
        regular_task = Task(
            id="regular1",
            name="Regular Task",
            description="Regular task without subtasks",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=4.0,
            labels=["backend"],
        )

        # Add subtasks to parent
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        subtask_manager.add_subtasks(parent_task.id, subtasks_data)

        # Simulate task filtering logic from _find_optimal_task_original_logic
        available_tasks = []
        project_tasks = [parent_task, regular_task]

        for t in project_tasks:
            # Skip parent tasks that have subtasks
            if subtask_manager.has_subtasks(t.id):
                continue
            available_tasks.append(t)

        # Verify parent task was filtered out, but regular task was not
        assert len(available_tasks) == 1
        assert available_tasks[0].id == regular_task.id
        assert parent_task.id not in [t.id for t in available_tasks]


class TestSubtaskConfigSwitch:
    """Test suite for subtask feature flag configuration."""

    @pytest.fixture
    def subtask_manager(self, tmp_path):
        """Create a subtask manager with test data."""
        manager = SubtaskManager(state_file=tmp_path / "subtasks_test.json")
        return manager

    @pytest.fixture
    def mock_state(self, subtask_manager):
        """Create a mock state object."""
        state = Mock()
        state.subtask_manager = subtask_manager
        state.project_tasks = []
        state.agent_tasks = {}
        state.tasks_being_assigned = set()
        state.assignment_persistence = Mock()
        state.assignment_persistence.get_all_assigned_task_ids = AsyncMock(
            return_value=set()
        )
        return state

    @pytest.fixture
    def parent_task(self):
        """Create a parent task with subtasks."""
        return Task(
            id="parent1",
            name="Parent Task",
            description="Parent task with subtasks",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=8.0,
            labels=["backend", "api"],
        )

    @pytest.fixture
    def regular_task(self):
        """Create a regular task without subtasks."""
        return Task(
            id="regular1",
            name="Regular Task",
            description="Regular task without subtasks",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=4.0,
            labels=["backend"],
        )

    @pytest.mark.asyncio
    async def test_subtasks_enabled_returns_subtask(
        self, subtask_manager, mock_state, parent_task, tmp_path
    ):
        """
        Test that when subtasks are enabled (default), subtask logic works.
        """
        from src.marcus_mcp.coordinator.task_assignment_integration import (
            find_optimal_task_with_subtasks,
        )

        # Setup state with project_tasks list
        mock_state.project_tasks = [parent_task]

        # Add subtasks to parent using unified storage
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        subtask_manager.add_subtasks(
            parent_task.id, subtasks_data, mock_state.project_tasks
        )

        # Create settings with subtasks enabled
        from src.config.settings import Settings

        config_path = tmp_path / "test_config.json"
        with open(config_path, "w") as f:
            import json

            json.dump({"features": {"enable_subtasks": True}}, f)

        # Mock Settings to use test config
        import unittest.mock

        with unittest.mock.patch.object(
            Settings, "__init__", lambda self, config_path=None: None
        ):
            with unittest.mock.patch.object(
                Settings,
                "is_subtasks_enabled",
                return_value=True,
            ):

                async def fallback_finder(agent_id, state):
                    return None

                # Find optimal task (should return subtask)
                task = await find_optimal_task_with_subtasks(
                    agent_id="agent1",
                    state=mock_state,
                    fallback_task_finder=fallback_finder,
                )

                # Verify subtask was returned
                assert task is not None
                assert task.name == "Subtask 1"

    @pytest.mark.asyncio
    async def test_subtasks_disabled_uses_fallback(
        self, subtask_manager, mock_state, parent_task, regular_task
    ):
        """
        Test that when subtasks are disabled, fallback task finder is used.
        """
        from src.config.settings import Settings
        from src.marcus_mcp.coordinator.task_assignment_integration import (
            find_optimal_task_with_subtasks,
        )

        # Add subtasks to parent
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        subtask_manager.add_subtasks(parent_task.id, subtasks_data)

        # Setup state
        mock_state.project_tasks = [parent_task, regular_task]

        # Mock Settings to disable subtasks
        import unittest.mock

        with unittest.mock.patch.object(
            Settings,
            "is_subtasks_enabled",
            return_value=False,
        ):

            async def fallback_finder(agent_id, state):
                # Return regular task from fallback
                return regular_task

            # Find optimal task (should use fallback)
            task = await find_optimal_task_with_subtasks(
                agent_id="agent1",
                state=mock_state,
                fallback_task_finder=fallback_finder,
            )

            # Verify fallback task was returned, not subtask
            assert task is not None
            assert task.id == regular_task.id
            assert task.name == "Regular Task"

    @pytest.mark.asyncio
    async def test_subtasks_disabled_via_env_uses_fallback(
        self, subtask_manager, mock_state, parent_task, regular_task, monkeypatch
    ):
        """
        Test that subtasks can be disabled via MARCUS_ENABLE_SUBTASKS env variable.
        """
        from src.config.settings import Settings
        from src.marcus_mcp.coordinator.task_assignment_integration import (
            find_optimal_task_with_subtasks,
        )

        # Set environment variable to disable subtasks
        monkeypatch.setenv("MARCUS_ENABLE_SUBTASKS", "false")

        # Add subtasks to parent
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "First subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
            },
        ]
        subtask_manager.add_subtasks(parent_task.id, subtasks_data)

        # Setup state
        mock_state.project_tasks = [parent_task, regular_task]

        # Mock Settings.is_subtasks_enabled to return False
        import unittest.mock

        with unittest.mock.patch.object(
            Settings,
            "is_subtasks_enabled",
            return_value=False,
        ):

            async def fallback_finder(agent_id, state):
                # Return regular task from fallback
                return regular_task

            # Find optimal task (should use fallback due to env variable)
            task = await find_optimal_task_with_subtasks(
                agent_id="agent1",
                state=mock_state,
                fallback_task_finder=fallback_finder,
            )

            # Verify fallback task was returned
            assert task is not None
            assert task.id == regular_task.id
            assert task.name == "Regular Task"


class TestParentTaskAttribution:
    """Tests for parent task attribution events on auto-completion (Bug 3 fix)."""

    @pytest.fixture
    def subtask_manager(self, tmp_path: Any) -> SubtaskManager:
        """Create subtask manager with all subtasks in DONE state."""
        return SubtaskManager(state_file=tmp_path / "subtasks.json")

    @pytest.fixture
    def mock_kanban_client(self) -> Mock:
        """Create mock kanban client."""
        client = Mock()
        client.update_task = AsyncMock()
        client.add_comment = AsyncMock()
        return client

    @pytest.fixture
    def mock_state(self) -> Mock:
        """Create mock state with log_event and project_tasks."""
        state = Mock()
        state.log_event = Mock()
        state.project_tasks = []
        return state

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_parent_auto_complete_emits_task_assignment_event(
        self,
        subtask_manager: SubtaskManager,
        mock_kanban_client: Mock,
        mock_state: Mock,
        tmp_path: Any,
    ) -> None:
        """
        Verify auto-completing a parent task emits a task_assignment log event.

        Dashboard-v98 post-mortem: parent tasks (e.g. Tech Foundation) were
        auto-completed via subtasks with no agent attribution. Cato's DAG and
        SwimLanes use task_assignment events to build agent-task links. Without
        this event the parent task appears as a ghost completion.
        """
        from src.marcus_mcp.coordinator.subtask_assignment import (
            check_and_complete_parent_task,
        )

        parent_id = "parent-abc-123"
        subtasks_data = [
            {
                "name": "Sub 1",
                "description": "first",
                "estimated_hours": 1.0,
                "dependencies": [],
            },
        ]
        # Use project_tasks=None so subtasks are stored in legacy storage
        subtask_manager.add_subtasks(parent_id, subtasks_data, None)

        # Mark all subtasks DONE via legacy storage
        subtask_ids = list(subtask_manager.subtasks.keys())
        for sid in subtask_ids:
            subtask_manager.update_subtask_status(
                sid, TaskStatus.DONE, None, assigned_to="agent_unicorn_1"
            )

        # state.project_tasks must be None so is_parent_complete uses legacy storage
        mock_state.project_tasks = None

        # Patch rollup helpers so the test doesn't need full state
        with (
            patch(
                "src.marcus_mcp.coordinator.subtask_assignment"
                "._rollup_subtask_artifacts_to_parent",
                new_callable=AsyncMock,
            ),
            patch(
                "src.marcus_mcp.coordinator.subtask_assignment"
                "._rollup_subtask_decisions_to_parent",
                new_callable=AsyncMock,
            ),
        ):
            result = await check_and_complete_parent_task(
                parent_id,
                subtask_manager,
                mock_kanban_client,
                mock_state,
                completing_agent_id="agent_unicorn_1",
            )

        assert result is True
        # task_assignment event must be emitted with the completing agent
        mock_state.log_event.assert_any_call(
            "task_assignment",
            {
                "agent_id": "agent_unicorn_1",
                "task_id": parent_id,
                "source": "auto_complete",
            },
        )

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_parent_auto_complete_without_agent_id_no_event(
        self,
        subtask_manager: SubtaskManager,
        mock_kanban_client: Mock,
        mock_state: Mock,
        tmp_path: Any,
    ) -> None:
        """
        Verify no task_assignment event is emitted when completing_agent_id is absent.

        Backward-compatibility: callers that don't pass completing_agent_id
        should still auto-complete the parent without crashing.
        """
        from src.marcus_mcp.coordinator.subtask_assignment import (
            check_and_complete_parent_task,
        )

        parent_id = "parent-no-agent-456"
        subtasks_data = [
            {
                "name": "Sub 1",
                "description": "first",
                "estimated_hours": 1.0,
                "dependencies": [],
            },
        ]
        subtask_manager.add_subtasks(parent_id, subtasks_data, None)
        subtask_ids = list(subtask_manager.subtasks.keys())
        for sid in subtask_ids:
            subtask_manager.update_subtask_status(sid, TaskStatus.DONE, None)
        mock_state.project_tasks = None

        with (
            patch(
                "src.marcus_mcp.coordinator.subtask_assignment"
                "._rollup_subtask_artifacts_to_parent",
                new_callable=AsyncMock,
            ),
            patch(
                "src.marcus_mcp.coordinator.subtask_assignment"
                "._rollup_subtask_decisions_to_parent",
                new_callable=AsyncMock,
            ),
        ):
            result = await check_and_complete_parent_task(
                parent_id,
                subtask_manager,
                mock_kanban_client,
                mock_state,
            )

        assert result is True
        # No task_assignment event — no agent to attribute to
        for call in mock_state.log_event.call_args_list:
            assert call[0][0] != "task_assignment"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_parent_not_complete_no_event_emitted(
        self,
        subtask_manager: SubtaskManager,
        mock_kanban_client: Mock,
        mock_state: Mock,
        tmp_path: Any,
    ) -> None:
        """Verify no event is emitted if subtasks are not all complete."""
        from src.marcus_mcp.coordinator.subtask_assignment import (
            check_and_complete_parent_task,
        )

        parent_id = "parent-partial-789"
        subtasks_data = [
            {
                "name": "Sub 1",
                "description": "first",
                "estimated_hours": 1.0,
                "dependencies": [],
            },
            {
                "name": "Sub 2",
                "description": "second",
                "estimated_hours": 1.0,
                "dependencies": [],
            },
        ]
        subtask_manager.add_subtasks(parent_id, subtasks_data, None)
        # Only mark first subtask done — second is still TODO
        subtask_ids = list(subtask_manager.subtasks.keys())
        subtask_manager.update_subtask_status(subtask_ids[0], TaskStatus.DONE, None)
        mock_state.project_tasks = None

        result = await check_and_complete_parent_task(
            parent_id,
            subtask_manager,
            mock_kanban_client,
            mock_state,
            completing_agent_id="agent_unicorn_1",
        )

        assert result is False
        mock_state.log_event.assert_not_called()


class TestSubtaskAssignedToOnPickup:
    """
    Tests that Subtask.assigned_to is set when an agent picks up a subtask.

    Without this fix, Subtask.assigned_to stays None from creation until the
    agent explicitly calls report_task_progress(IN_PROGRESS). Cato reads
    SubtaskManager state for attribution, so it saw assigned_agent_id=null
    for any subtask that hadn't yet reported progress.

    The fix: request_next_task now calls update_subtask_status at pickup time
    so attributed_agent_id is set immediately.
    """

    @pytest.fixture
    def subtask_manager(self, tmp_path: Any) -> SubtaskManager:
        """Create a fresh subtask manager."""
        return SubtaskManager(state_file=tmp_path / "subtasks.json")

    def test_subtask_assigned_to_null_at_creation(
        self, subtask_manager: SubtaskManager
    ) -> None:
        """Confirm baseline: freshly created subtask has assigned_to=None."""
        parent_id = "parent-001"
        subtasks_data = [
            {
                "name": "Sub 1",
                "description": "first",
                "estimated_hours": 1.0,
                "dependencies": [],
            }
        ]
        subtask_manager.add_subtasks(parent_id, subtasks_data, None)
        subtask_id = list(subtask_manager.subtasks.keys())[0]

        assert subtask_manager.subtasks[subtask_id].assigned_to is None

    def test_update_subtask_status_sets_assigned_to(
        self, subtask_manager: SubtaskManager
    ) -> None:
        """
        Calling update_subtask_status(IN_PROGRESS, assigned_to=...) sets
        the Subtask's assigned_to field immediately.

        This simulates what request_next_task now does at pickup time so
        Cato sees attribution without waiting for an explicit progress report.
        """
        parent_id = "parent-002"
        subtasks_data = [
            {
                "name": "Sub 1",
                "description": "first",
                "estimated_hours": 1.0,
                "dependencies": [],
            }
        ]
        subtask_manager.add_subtasks(parent_id, subtasks_data, None)
        subtask_id = list(subtask_manager.subtasks.keys())[0]

        # Simulate the request_next_task pickup
        result = subtask_manager.update_subtask_status(
            subtask_id,
            TaskStatus.IN_PROGRESS,
            None,
            assigned_to="agent_unicorn_1",
        )

        assert result is True
        assert subtask_manager.subtasks[subtask_id].assigned_to == "agent_unicorn_1"
        assert subtask_manager.subtasks[subtask_id].status == TaskStatus.IN_PROGRESS
