"""
Unit tests for unassign_task tool functionality.

Tests the manual task unassignment feature that breaks stuck task
assignments when agents crash, disconnect, or get stuck.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskAssignment, TaskStatus
from src.marcus_mcp.tools.task import unassign_task

pytestmark = pytest.mark.unit


def create_mock_task(
    task_id: str,
    name: str,
    status: TaskStatus = TaskStatus.IN_PROGRESS,
    assigned_to: Optional[str] = None,
) -> Task:
    """Helper to create a mock task."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description=f"Description for {name}",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=assigned_to,
        created_at=now,
        updated_at=now,
        due_date=now + timedelta(days=7),
        estimated_hours=8.0,
        labels=["backend"],
        dependencies=[],
    )


def create_mock_assignment(
    task_id: str, task_name: str, agent_id: str
) -> TaskAssignment:
    """Helper to create a mock task assignment."""
    return TaskAssignment(
        task_id=task_id,
        task_name=task_name,
        description=f"Description for {task_name}",
        instructions="Test instructions",
        estimated_hours=8.0,
        priority=Priority.MEDIUM,
        dependencies=[],
        assigned_to=agent_id,
        assigned_at=datetime.now(timezone.utc),
        due_date=datetime.now(timezone.utc) + timedelta(days=7),
    )


class TestUnassignTask:
    """Test suite for unassign_task functionality."""

    @pytest.fixture
    def mock_state(self):
        """Create a comprehensive mock state object."""
        state = Mock()

        # Mock agent status with current tasks
        mock_agent = Mock()
        mock_agent.current_tasks = [
            create_mock_task("task-1", "Test Task", assigned_to="agent-1")
        ]
        mock_agent.completed_tasks_count = 0

        state.agent_status = {"agent-1": mock_agent}

        # Mock agent tasks
        state.agent_tasks = {
            "agent-1": create_mock_assignment("task-1", "Test Task", "agent-1")
        }

        # Mock tasks being assigned
        state.tasks_being_assigned = {"task-1"}

        # Mock active operations
        state._active_operations = {"task_assignment_task-1"}

        # Mock lease manager. PR #731 ("lease-ownership hardening") changed
        # unassign_task from deleting state.lease_manager.active_leases[task_id]
        # directly to awaiting the real release_lease() method -- release_lease
        # must be an AsyncMock, not the plain Mock() every attribute of a bare
        # Mock() auto-creates, or awaiting it raises
        # "object Mock can't be used in 'await' expression".
        mock_lease_manager = Mock()
        mock_lease_manager.active_leases = {"task-1": Mock()}
        mock_lease_manager.release_lease = AsyncMock()
        state.lease_manager = mock_lease_manager

        # Mock kanban client
        state.kanban_client = Mock()
        state.kanban_client.update_task = AsyncMock()

        # Mock assignment persistence
        state.assignment_persistence = Mock()
        state.assignment_persistence.remove_assignment = AsyncMock()

        # Mock refresh
        state.refresh_project_state = AsyncMock()

        # Mock initialize_kanban
        state.initialize_kanban = AsyncMock()

        # Mock log_event
        state.log_event = Mock()

        return state

    @pytest.mark.asyncio
    async def test_unassign_task_with_agent_id_provided(self, mock_state):
        """Test successful unassignment when agent_id is explicitly provided."""
        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        # Verify success
        assert result["success"] is True
        assert result["task_id"] == "task-1"
        assert result["agent_id"] == "agent-1"
        assert "successfully unassigned" in result["message"]

        # Verify all cleanup locations were cleared
        # 1. agent_tasks removed
        assert "agent-1" not in mock_state.agent_tasks

        # 2. agent current_tasks cleared
        assert mock_state.agent_status["agent-1"].current_tasks == []

        # 3. assignment_persistence removed
        mock_state.assignment_persistence.remove_assignment.assert_called_once_with(
            "agent-1"
        )

        # 4. tasks_being_assigned cleared
        assert "task-1" not in mock_state.tasks_being_assigned

        # 5. _active_operations cleared
        assert "task_assignment_task-1" not in mock_state._active_operations

        # 6. lease released via the real release_lease() method (not a
        # direct dict delete -- see the mock_state fixture's own comment)
        mock_state.lease_manager.release_lease.assert_called_once_with(
            "task-1", reason="unassigned"
        )

        # 7. kanban updated to TODO
        mock_state.kanban_client.update_task.assert_called_once_with(
            "task-1", {"status": TaskStatus.TODO, "assigned_to": None, "progress": 0}
        )

        # Verify refresh was called
        mock_state.refresh_project_state.assert_called_once()

        # Verify event logged
        mock_state.log_event.assert_called_once()
        event_call = mock_state.log_event.call_args
        assert event_call[0][0] == "task_unassigned"

    @pytest.mark.asyncio
    async def test_unassign_task_auto_detect_from_agent_tasks(self, mock_state):
        """Test auto-detection of agent_id from state.agent_tasks."""
        result = await unassign_task(
            task_id="task-1", agent_id=None, state=mock_state  # No agent_id provided
        )

        # Should successfully detect agent-1 and unassign
        assert result["success"] is True
        assert result["agent_id"] == "agent-1"
        assert "task-1" not in mock_state.tasks_being_assigned

    @pytest.mark.asyncio
    async def test_unassign_task_auto_detect_from_current_tasks(self, mock_state):
        """Test auto-detection of agent_id from agent.current_tasks."""
        # Remove from agent_tasks so it must use current_tasks
        mock_state.agent_tasks = {}

        result = await unassign_task(task_id="task-1", agent_id=None, state=mock_state)

        # Should successfully detect agent-1 from current_tasks
        assert result["success"] is True
        assert result["agent_id"] == "agent-1"

    @pytest.mark.asyncio
    async def test_unassign_task_releases_a_stranded_card_named_only_on_the_board(
        self, mock_state
    ):
        """A stranded card (#900) has no in-memory holder; the board's assignee is the one to release.

        The BOARD_DRAINED message sends an operator here for exactly such
        cards (Codex P2 on PR #907), so refusing with "not currently
        assigned" would advertise a remedy that does not work.
        """
        mock_state.agent_tasks = {}
        mock_state.agent_status = {}
        mock_state.lease_manager.active_leases = {}
        mock_state.project_tasks = [
            create_mock_task("task-9", "Stranded", assigned_to="agent-vanished")
        ]

        result = await unassign_task(task_id="task-9", agent_id=None, state=mock_state)

        assert result["success"] is True
        assert result["agent_id"] == "agent-vanished"
        assert result["assignee_source"] == "board"
        mock_state.kanban_client.update_task.assert_called_once_with(
            "task-9", {"status": TaskStatus.TODO, "assigned_to": None, "progress": 0}
        )

    @pytest.mark.asyncio
    async def test_unassign_task_board_fallback_preserves_the_agents_unrelated_live_work(
        self, mock_state
    ):
        """Releasing stranded card A named to agent-1 must not touch agent-1's live task B.

        Codex P2 on PR #907: the board fallback resolved the stale name as
        the holder and then ran agent-wide cleanup, deleting agent-1's
        agent_tasks entry for B, emptying its current_tasks and removing
        B's persisted row while B's lease stayed live. Only records that
        reference the requested task may be removed.
        """
        live_b = create_mock_task("task-B", "Live work", assigned_to="agent-1")
        mock_state.agent_tasks = {
            "agent-1": create_mock_assignment("task-B", "Live work", "agent-1")
        }
        agent = Mock()
        agent.current_tasks = [live_b]
        mock_state.agent_status = {"agent-1": agent}
        mock_state.lease_manager.active_leases = {"task-B": Mock()}
        mock_state.tasks_being_assigned = set()
        mock_state.project_tasks = [
            create_mock_task("task-A", "Stranded", assigned_to="agent-1"),
            live_b,
        ]

        result = await unassign_task(task_id="task-A", agent_id=None, state=mock_state)

        assert result["success"] is True
        assert result["agent_id"] == "agent-1"
        assert "agent-1" in mock_state.agent_tasks
        assert mock_state.agent_tasks["agent-1"].task_id == "task-B"
        assert agent.current_tasks == [live_b]
        mock_state.assignment_persistence.remove_assignment.assert_not_called()
        mock_state.lease_manager.release_lease.assert_not_called()
        mock_state.kanban_client.update_task.assert_called_once_with(
            "task-A", {"status": TaskStatus.TODO, "assigned_to": None, "progress": 0}
        )

    @pytest.mark.asyncio
    async def test_unassign_task_still_clears_records_that_reference_the_task(
        self, mock_state
    ):
        """The ordinary path: the agent's records for THIS task are removed, as before."""
        result = await unassign_task(task_id="task-1", agent_id=None, state=mock_state)

        assert result["success"] is True
        assert "agent-1" not in mock_state.agent_tasks
        assert mock_state.agent_status["agent-1"].current_tasks == []
        mock_state.assignment_persistence.remove_assignment.assert_called_once_with(
            "agent-1"
        )

    @pytest.mark.asyncio
    async def test_unassign_task_releases_a_coordinator_owned_ghost(self, mock_state):
        """A TODO design ghost still named to Marcus is released the same way."""
        mock_state.agent_tasks = {}
        mock_state.agent_status = {}
        mock_state.lease_manager.active_leases = {}
        mock_state.project_tasks = [
            create_mock_task("ghost-1", "Design the lexer", assigned_to="Marcus")
        ]

        result = await unassign_task(task_id="ghost-1", agent_id=None, state=mock_state)

        assert result["success"] is True
        assert result["agent_id"] == "Marcus"
        mock_state.kanban_client.update_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_unassign_task_not_currently_assigned(self, mock_state):
        """Test error when task is not currently assigned."""
        # Clear all assignments
        mock_state.agent_tasks = {}
        mock_state.agent_status = {}

        result = await unassign_task(
            task_id="task-999", agent_id=None, state=mock_state
        )

        # Should return error
        assert result["success"] is False
        assert "not currently assigned" in result["error"]
        assert result["task_id"] == "task-999"

        # Should NOT call kanban update
        mock_state.kanban_client.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_unassign_task_without_lease_manager(self, mock_state):
        """Test unassignment when lease_manager is not available."""
        # Remove lease manager
        mock_state.lease_manager = None

        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        # Should still succeed (lease manager is optional)
        assert result["success"] is True
        assert "agent-1" not in mock_state.agent_tasks

    @pytest.mark.asyncio
    async def test_unassign_task_without_active_operations(self, mock_state):
        """Test unassignment when _active_operations is not available."""
        # Remove _active_operations
        del mock_state._active_operations

        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        # Should still succeed (_active_operations is optional)
        assert result["success"] is True
        assert "agent-1" not in mock_state.agent_tasks

    @pytest.mark.asyncio
    async def test_unassign_task_kanban_update_failure(self, mock_state):
        """Test error handling when kanban update fails."""
        # Make kanban update fail
        mock_state.kanban_client.update_task.side_effect = Exception(
            "Kanban unavailable"
        )

        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        # Should return error
        assert result["success"] is False
        assert "Kanban unavailable" in result["error"]
        assert result["task_id"] == "task-1"

    @pytest.mark.asyncio
    async def test_unassign_task_clears_only_specified_agent(self, mock_state):
        """Test that unassignment only affects the specified agent."""
        # Add a second agent with a different task
        mock_agent_2 = Mock()
        mock_agent_2.current_tasks = [
            create_mock_task("task-2", "Task 2", assigned_to="agent-2")
        ]
        mock_state.agent_status["agent-2"] = mock_agent_2
        mock_state.agent_tasks["agent-2"] = create_mock_assignment(
            "task-2", "Task 2", "agent-2"
        )

        # Unassign task-1 from agent-1
        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        # Verify agent-1's task was cleared
        assert result["success"] is True
        assert "agent-1" not in mock_state.agent_tasks

        # Verify agent-2's task is still there
        assert "agent-2" in mock_state.agent_tasks
        assert mock_state.agent_tasks["agent-2"].task_id == "task-2"

    @pytest.mark.asyncio
    async def test_unassign_task_with_multiple_tasks_in_current_tasks(self, mock_state):
        """Releasing one task leaves the agent's other current task in place (#900).

        This used to assert the whole list was emptied, pinning the
        agent-wide wipe Codex flagged on PR #907: releasing one card must
        not erase the agent's record of unrelated work.
        """
        mock_task_1 = create_mock_task("task-1", "Task 1", assigned_to="agent-1")
        mock_task_2 = create_mock_task("task-2", "Task 2", assigned_to="agent-1")
        mock_state.agent_status["agent-1"].current_tasks = [mock_task_1, mock_task_2]

        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        assert result["success"] is True
        assert mock_state.agent_status["agent-1"].current_tasks == [mock_task_2]

    @pytest.mark.asyncio
    @patch("src.marcus_mcp.tools.task.conversation_logger")
    async def test_unassign_task_logging_calls(
        self, mock_conversation_logger, mock_state
    ):
        """Test that all logging calls are made correctly."""
        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        assert result["success"] is True

        # Verify PM decision was logged
        mock_conversation_logger.log_pm_decision.assert_called_once()
        decision_call = mock_conversation_logger.log_pm_decision.call_args
        assert "Manually unassign" in decision_call[1]["decision"]

        # Verify worker message was logged
        assert mock_conversation_logger.log_worker_message.called

    @pytest.mark.asyncio
    async def test_unassign_task_initialize_kanban_called(self, mock_state):
        """Test that initialize_kanban is called."""
        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        assert result["success"] is True
        mock_state.initialize_kanban.assert_called_once()

    @pytest.mark.asyncio
    async def test_unassign_task_persistence_failure_handling(self, mock_state):
        """Test error handling when persistence removal fails."""
        # Make persistence removal fail
        mock_state.assignment_persistence.remove_assignment.side_effect = Exception(
            "Database error"
        )

        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        # Should return error
        assert result["success"] is False
        assert "Database error" in result["error"]

    @pytest.mark.asyncio
    async def test_unassign_task_agent_not_in_agent_status(self, mock_state):
        """Test unassignment when agent exists in agent_tasks but not agent_status."""
        # Remove agent from agent_status
        mock_state.agent_status = {}

        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        # Should still succeed (agent_status update is conditional)
        assert result["success"] is True
        assert "agent-1" not in mock_state.agent_tasks

        # Kanban and persistence should still be updated
        mock_state.kanban_client.update_task.assert_called_once()
        mock_state.assignment_persistence.remove_assignment.assert_called_once()

    @pytest.mark.asyncio
    async def test_unassign_task_with_no_lease_for_task(self, mock_state):
        """Test unassignment when task has no lease."""
        # Remove lease for this specific task
        mock_state.lease_manager.active_leases = {}

        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        # Should still succeed
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_unassign_task_return_structure(self, mock_state):
        """Test that success response has correct structure."""
        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        # Verify response structure
        assert "success" in result
        assert "message" in result
        assert "agent_id" in result
        assert "task_id" in result
        assert result["success"] is True
        assert isinstance(result["message"], str)
        assert result["agent_id"] == "agent-1"
        assert result["task_id"] == "task-1"

    @pytest.mark.asyncio
    async def test_unassign_task_error_return_structure(self, mock_state):
        """Test that error response has correct structure."""
        # Make it fail by removing all assignments
        mock_state.agent_tasks = {}
        mock_state.agent_status = {}

        result = await unassign_task(
            task_id="task-999", agent_id=None, state=mock_state
        )

        # Verify error response structure
        assert "success" in result
        assert "error" in result
        assert "task_id" in result
        assert result["success"] is False
        assert isinstance(result["error"], str)
        assert result["task_id"] == "task-999"

    @pytest.mark.asyncio
    async def test_unassign_task_clears_tasks_being_assigned(self, mock_state):
        """Test that task is removed from tasks_being_assigned set."""
        # Ensure task is in tasks_being_assigned
        mock_state.tasks_being_assigned = {"task-1", "task-2"}

        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        assert result["success"] is True
        # task-1 should be removed, task-2 should remain
        assert "task-1" not in mock_state.tasks_being_assigned
        assert "task-2" in mock_state.tasks_being_assigned

    @pytest.mark.asyncio
    async def test_unassign_task_exception_handling(self, mock_state):
        """Test general exception handling."""
        # Make initialize_kanban raise an unexpected error
        mock_state.initialize_kanban.side_effect = RuntimeError("Unexpected error")

        result = await unassign_task(
            task_id="task-1", agent_id="agent-1", state=mock_state
        )

        # Should catch and return error
        assert result["success"] is False
        assert "error" in result
        assert result["task_id"] == "task-1"
