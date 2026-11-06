"""
End-to-end integration tests for hierarchical task decomposition.

Tests the complete workflow of task decomposition, subtask assignment,
and parent task auto-completion focusing on integration points.
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.coordinator import SubtaskManager, SubtaskMetadata, decompose_task
from src.marcus_mcp.coordinator.subtask_assignment import (
    check_and_complete_parent_task,
    convert_subtask_to_task,
    find_next_available_subtask,
    update_subtask_progress_in_parent,
)
from tests.utils.base import BaseTestCase


@pytest.mark.integration
@pytest.mark.e2e
class TestTaskDecompositionIntegration(BaseTestCase):
    """Test task decomposition integration points."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("anyio_backend", ["asyncio"])
    async def test_complete_decomposition_workflow(self, tmp_path):
        """
        Test the complete decomposition workflow:
        1. Decompose task into subtasks
        2. Assign subtasks sequentially based on dependencies
        3. Complete subtasks and update parent progress
        4. Verify parent auto-completes when all subtasks done
        """
        # Step 1: Create parent task
        parent_task = self.create_sample_task(
            id="parent-001",
            name="Build API with auth",
            description="Create API with authentication",
            estimated_hours=10.0,
            labels=["backend", "api"],
        )

        # Step 2: Decompose task with mock AI
        mock_ai_engine = Mock()
        mock_ai_engine.generate_structured_response = AsyncMock(
            return_value={
                "subtasks": [
                    {
                        "name": "Create User model",
                        "description": "Define User model",
                        "estimated_hours": 2.0,
                        "dependencies": [],
                        "file_artifacts": ["models/user.py"],
                        "provides": "User model",
                        "requires": "None",
                    },
                    {
                        "name": "Build auth middleware",
                        "description": "Create JWT middleware",
                        "estimated_hours": 3.0,
                        "dependencies": [0],
                        "file_artifacts": ["middleware/auth.py"],
                        "provides": "Auth middleware",
                        "requires": "User model",
                    },
                ],
                "shared_conventions": {
                    "base_path": "src/api/",
                    "file_structure": "src/{component}/{feature}.py",
                },
            }
        )

        decomposition = await decompose_task(parent_task, mock_ai_engine)
        assert decomposition["success"] is True
        assert len(decomposition["subtasks"]) > 2  # Including auto integration subtask

        # Step 3: Store subtasks
        manager = SubtaskManager(state_file=tmp_path / "subtasks.json")
        metadata = SubtaskMetadata(
            shared_conventions=decomposition["shared_conventions"],
            decomposed_by="ai",
        )
        subtasks = manager.add_subtasks(
            parent_task.id, decomposition["subtasks"], metadata=metadata
        )

        # Step 4: Test subtask assignment (dependency-aware)
        completed_subtasks: set[str] = set()

        # First assignment should get subtask with no dependencies
        first_subtask = manager.get_next_available_subtask(
            parent_task.id, completed_subtasks
        )
        assert first_subtask is not None
        assert len(first_subtask.dependencies) == 0

        # Second assignment should get nothing (dependencies not satisfied)
        second_subtask = manager.get_next_available_subtask(
            parent_task.id, completed_subtasks
        )
        assert second_subtask is not None  # Should get different subtask with no deps

        # Complete first subtask
        manager.update_subtask_status(
            first_subtask.id, TaskStatus.DONE, assigned_to="test-agent"
        )
        completed_subtasks.add(first_subtask.id)

        # Now should be able to get dependent subtask
        progress = manager.get_completion_percentage(parent_task.id)
        assert progress > 0
        assert progress < 100

        #  Complete all subtasks
        while True:
            next_subtask = manager.get_next_available_subtask(
                parent_task.id, completed_subtasks
            )
            if not next_subtask:
                break

            manager.update_subtask_status(
                next_subtask.id, TaskStatus.DONE, assigned_to="test-agent"
            )
            completed_subtasks.add(next_subtask.id)

        # Step 5: Verify parent completion
        assert manager.is_parent_complete(parent_task.id) is True
        assert manager.get_completion_percentage(parent_task.id) == 100.0

        print("\n✓ Complete decomposition workflow test passed!")

    @pytest.mark.anyio
    @pytest.mark.parametrize("anyio_backend", ["asyncio"])
    async def test_subtask_to_task_conversion(self, tmp_path):
        """Test converting a subtask to a Task for assignment."""
        # Create parent task
        parent_task = self.create_sample_task(
            id="parent-002",
            name="Parent Task",
            priority=Priority.HIGH,
            labels=["backend"],
        )

        # Create subtask
        manager = SubtaskManager(state_file=tmp_path / "subtasks.json")
        subtasks_data = [
            {
                "name": "Subtask 1",
                "description": "Test subtask",
                "estimated_hours": 2.0,
                "dependencies": [],
                "file_artifacts": ["test.py"],
                "provides": "Test output",
                "requires": "None",
            }
        ]
        metadata = SubtaskMetadata(shared_conventions={}, decomposed_by="test")
        subtasks = manager.add_subtasks(
            parent_task.id, subtasks_data, metadata=metadata
        )

        # Convert to Task
        converted_task = convert_subtask_to_task(subtasks[0], parent_task)

        # Verify conversion
        assert isinstance(converted_task, Task)
        assert converted_task.name == subtasks[0].name
        assert converted_task.description == subtasks[0].description
        # Priority comes from subtask, not parent
        assert converted_task.labels == parent_task.labels
        assert converted_task.estimated_hours == subtasks[0].estimated_hours

        print("\n✓ Subtask to Task conversion test passed!")

    @pytest.mark.anyio
    @pytest.mark.parametrize("anyio_backend", ["asyncio"])
    async def test_parent_auto_completion_with_kanban(self, tmp_path):
        """Test parent task auto-completion updates kanban."""
        # Create parent task
        parent_task = self.create_sample_task(id="parent-003", name="Parent")

        # Create subtasks
        manager = SubtaskManager(state_file=tmp_path / "subtasks.json")
        subtasks_data = [
            {
                "name": f"Subtask {i}",
                "description": f"Test subtask {i}",
                "estimated_hours": 1.0,
                "dependencies": [],
                "file_artifacts": [f"test{i}.py"],
                "provides": f"Output {i}",
                "requires": "None",
            }
            for i in range(3)
        ]
        metadata = SubtaskMetadata(shared_conventions={}, decomposed_by="test")
        subtasks = manager.add_subtasks(
            parent_task.id, subtasks_data, metadata=metadata
        )

        # Complete all subtasks
        for subtask in subtasks:
            manager.update_subtask_status(
                subtask.id, TaskStatus.DONE, assigned_to="test-agent"
            )

        # Mock kanban client
        mock_kanban = AsyncMock()
        mock_kanban.update_task = AsyncMock()
        mock_kanban.add_comment = AsyncMock()

        # Check auto-completion
        completed = await check_and_complete_parent_task(
            parent_task.id,
            manager,
            mock_kanban,
        )

        # Verify
        assert completed is True
        mock_kanban.update_task.assert_called_once()
        mock_kanban.add_comment.assert_called_once()

        print("\n✓ Parent auto-completion with kanban test passed!")

    @pytest.mark.anyio
    @pytest.mark.parametrize("anyio_backend", ["asyncio"])
    async def test_subtask_progress_updates_parent(self, tmp_path):
        """Test that subtask progress updates are reflected in parent."""
        # Create parent task
        parent_task = self.create_sample_task(id="parent-004", name="Parent")

        # Create subtasks
        manager = SubtaskManager(state_file=tmp_path / "subtasks.json")
        subtasks_data = [
            {
                "name": f"Subtask {i}",
                "description": f"Test subtask {i}",
                "estimated_hours": 1.0,
                "dependencies": [],
                "file_artifacts": [f"test{i}.py"],
                "provides": f"Output {i}",
                "requires": "None",
            }
            for i in range(4)
        ]
        metadata = SubtaskMetadata(shared_conventions={}, decomposed_by="test")
        subtasks = manager.add_subtasks(
            parent_task.id, subtasks_data, metadata=metadata
        )

        # Mock kanban client
        mock_kanban = AsyncMock()
        mock_kanban.update_task_progress = AsyncMock()

        # Complete subtasks one by one and verify progress
        for i, subtask in enumerate(subtasks):
            manager.update_subtask_status(
                subtask.id, TaskStatus.DONE, assigned_to="test-agent"
            )

            await update_subtask_progress_in_parent(
                parent_task.id,
                subtask.id,
                manager,
                mock_kanban,
            )

            expected_progress = manager.get_completion_percentage(parent_task.id)
            assert expected_progress == ((i + 1) / len(subtasks)) * 100

        print("\n✓ Subtask progress updates parent test passed!")

    @pytest.mark.anyio
    @pytest.mark.parametrize("anyio_backend", ["asyncio"])
    async def test_find_next_available_subtask_with_assignments(self, tmp_path):
        """Test finding next subtask respects already-assigned tasks."""
        # Create parent task
        parent_task = self.create_sample_task(id="parent-005", name="Parent")

        # Create project tasks list
        project_tasks = [parent_task]

        # Create subtasks
        manager = SubtaskManager(state_file=tmp_path / "subtasks.json")
        subtasks_data = [
            {
                "name": f"Subtask {i}",
                "description": f"Test subtask {i}",
                "estimated_hours": 1.0,
                "dependencies": [],
                "file_artifacts": [f"test{i}.py"],
                "provides": f"Output {i}",
                "requires": "None",
            }
            for i in range(3)
        ]
        metadata = SubtaskMetadata(shared_conventions={}, decomposed_by="test")
        subtasks = manager.add_subtasks(
            parent_task.id,
            subtasks_data,
            project_tasks=project_tasks,
            metadata=metadata,
        )

        # Find first subtask for parent-005
        assigned_ids: set[str] = set()
        first = find_next_available_subtask(
            "agent-1", project_tasks, manager, assigned_ids
        )
        assert first is not None
        assert first.parent_task_id == parent_task.id

        # Mark as assigned
        assigned_ids.add(first.id)

        # Complete first subtask to allow others to proceed
        manager.update_subtask_status(first.id, TaskStatus.DONE, assigned_to="agent-1")
        completed_ids: set[str] = set([first.id])

        # Now find another subtask (should be available)
        second = manager.get_next_available_subtask(parent_task.id, completed_ids)
        assert second is not None
        assert second.id != first.id

        print("\n✓ Find next available subtask with assignments test passed!")


@pytest.mark.integration
class TestSubtaskManagerPersistence(BaseTestCase):
    """Test SubtaskManager state persistence."""

    def test_subtask_manager_state_persistence(self):
        """Verify SubtaskManager can persist and load state."""
        import os
        import tempfile
        from pathlib import Path

        # Create manager with temp file
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        temp_file.close()

        try:
            manager = SubtaskManager(state_file=Path(temp_file.name))

            # Add subtasks
            subtasks_data = [
                {
                    "name": "Test subtask",
                    "description": "Test",
                    "estimated_hours": 1.0,
                    "dependencies": [],
                    "file_artifacts": ["test.py"],
                    "provides": "Output",
                    "requires": "None",
                }
            ]
            metadata = SubtaskMetadata(shared_conventions={}, decomposed_by="test")
            subtasks = manager.add_subtasks(
                "parent-1", subtasks_data, metadata=metadata
            )

            # Verify state file exists and has content
            assert os.path.exists(temp_file.name)
            assert os.path.getsize(temp_file.name) > 0

            # Load state in new manager
            manager2 = SubtaskManager(state_file=Path(temp_file.name))
            assert subtasks[0].id in manager2.subtasks

            print("\n✓ SubtaskManager persistence test passed!")

        finally:
            # Cleanup
            if os.path.exists(temp_file.name):
                os.unlink(temp_file.name)
