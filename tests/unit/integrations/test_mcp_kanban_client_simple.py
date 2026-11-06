"""
Unit tests for KanbanClient.

This module provides comprehensive unit tests for the KanbanClient class,
covering initialization, task operations, board management, and error handling.

Notes
-----
All external MCP server calls and file system operations are mocked to ensure fast,
isolated unit tests that don't require external services.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, Mock, call, mock_open, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.integrations.kanban_client import KanbanClient


class TestKanbanClient:
    """
    Test suite for KanbanClient.

    Tests cover initialization, configuration loading, task operations,
    board management, and comprehensive error handling scenarios.
    """

    @pytest.fixture
    def mock_client_session(self):
        """Create a mock MCP client session."""
        session = AsyncMock()

        # Mock tool call responses
        session.call_tool = AsyncMock()
        session.initialize = AsyncMock()

        return session

    @pytest.fixture
    def mock_stdio_client(self, mock_client_session):
        """Create a mock stdio client context manager."""

        @asynccontextmanager
        async def _mock_stdio_client(server_params):
            # Mock read/write streams
            read_stream = Mock()
            write_stream = Mock()

            yield (read_stream, write_stream)

        return _mock_stdio_client

    @pytest.fixture
    def mock_client_session_context(self, mock_client_session):
        """Create a mock ClientSession context manager."""

        @asynccontextmanager
        async def _mock_client_session(read, write):
            yield mock_client_session

        return _mock_client_session

    @pytest.fixture
    def sample_config(self):
        """Create sample configuration data."""
        return {
            "project_id": "test-project-123",
            "board_id": "test-board-456",
            "planka": {
                "base_url": "http://test-planka:3333",
                "email": "test@example.com",
                "password": "test-password",
            },
        }

    @pytest.fixture
    def sample_lists(self):
        """Create sample kanban lists."""
        return [
            {"id": "list-1", "name": "TODO"},
            {"id": "list-2", "name": "In Progress"},
            {"id": "list-3", "name": "Done"},
            {"id": "list-4", "name": "Blocked"},
            {"id": "list-5", "name": "Backlog"},
        ]

    @pytest.fixture
    def sample_cards(self):
        """Create sample kanban cards."""
        return [
            {
                "id": "card-1",
                "name": "Implement feature X",
                "description": "Add new feature to the system",
                "listName": "TODO",
                "users": [],
            },
            {
                "id": "card-2",
                "name": "Fix bug Y",
                "description": "Resolve critical bug",
                "listName": "In Progress",
                "users": [{"username": "agent-001"}],
            },
            {
                "id": "card-3",
                "name": "Update documentation",
                "description": "Update API docs",
                "listName": "Done",
                "users": [],
            },
            {
                "id": "card-4",
                "name": "Research new tech",
                "description": "Investigate options",
                "listName": "Backlog",
                "assignedTo": "agent-002",
            },
        ]

    @pytest.mark.skip(
        reason="Config environment variable mocking needs fixing - skipping temporarily"
    )
    def test_initialization_with_config_file(self, sample_config):
        """Test client initialization when config file exists."""
        config_json = json.dumps(sample_config)

        # Selective mock: config_marcus.json exists, others may not
        original_exists = Path.exists

        def selective_path_exists(self):
            if "config_marcus.json" in str(self):
                return True
            return original_exists(self)

        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=True),
            patch("pathlib.Path.exists", selective_path_exists),
            patch("builtins.open", mock_open(read_data=config_json)),
            patch("src.integrations.kanban_client.os.environ", {}) as mock_env,
            patch("sys.stderr"),
        ):
            client = KanbanClient()

            # Verify config was loaded
            assert client.project_id == "test-project-123"
            assert client.board_id == "test-board-456"

            # Verify environment variables were set from config
            assert mock_env["PLANKA_BASE_URL"] == "http://test-planka:3333"
            assert mock_env["PLANKA_AGENT_EMAIL"] == "test@example.com"
            assert mock_env["PLANKA_AGENT_PASSWORD"] == "test-password"

    def test_initialization_without_config_file(self):
        """Test client initialization when config file doesn't exist."""

        # Create a mock that handles the Path operations
        def mock_exists(path):
            return False  # Always return False for any path

        # Selective mock: only block config_marcus.json, allow kanban-mcp path
        original_exists = Path.exists

        def selective_path_exists(self):
            if "config_marcus.json" in str(self):
                return False
            return original_exists(self)

        with (
            patch("os.path.exists", side_effect=mock_exists),
            patch("pathlib.Path.exists", selective_path_exists),
            patch("src.integrations.kanban_client.os.environ", {}) as mock_env,
            patch("sys.stderr"),
            patch.object(KanbanClient, "_load_workspace_state", return_value=None),
        ):
            client = KanbanClient()

            # Verify defaults are set
            assert client.project_id is None
            assert client.board_id is None

            # Verify default environment variables
            assert mock_env["PLANKA_BASE_URL"] == "http://localhost:3333"
            assert mock_env["PLANKA_AGENT_EMAIL"] == "demo@demo.demo"
            assert mock_env["PLANKA_AGENT_PASSWORD"] == "demo"

    def test_initialization_with_existing_env_vars(self):
        """Test that existing environment variables are not overwritten."""
        existing_env = {
            "PLANKA_BASE_URL": "http://existing:3333",
            "PLANKA_AGENT_EMAIL": "existing@example.com",
            "PLANKA_AGENT_PASSWORD": "existing-password",
        }

        # Selective mock: only block config_marcus.json, allow kanban-mcp path
        original_exists = Path.exists

        def selective_path_exists(self):
            if "config_marcus.json" in str(self):
                return False
            return original_exists(self)

        with (
            patch("os.path.exists", return_value=False),
            patch("pathlib.Path.exists", selective_path_exists),
            patch(
                "src.integrations.kanban_client.os.environ", existing_env.copy()
            ) as mock_env,
            patch("sys.stderr"),
            patch.object(KanbanClient, "_load_workspace_state", return_value=None),
        ):
            client = KanbanClient()

            # Verify existing env vars were preserved
            assert mock_env["PLANKA_BASE_URL"] == "http://existing:3333"
            assert mock_env["PLANKA_AGENT_EMAIL"] == "existing@example.com"
            assert mock_env["PLANKA_AGENT_PASSWORD"] == "existing-password"

    def test_load_config_partial_planka_config(self):
        """Test loading config with partial Planka configuration."""
        partial_config = {
            "project_id": "test-project",
            "board_id": "test-board",
            "planka": {
                "base_url": "http://partial:3333"
                # Missing email and password
            },
        }

        config_json = json.dumps(partial_config)

        # Selective mock: config_marcus.json exists, others may not
        original_exists = Path.exists

        def selective_path_exists(self):
            if "config_marcus.json" in str(self):
                return True
            return original_exists(self)

        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=True),
            patch("pathlib.Path.exists", selective_path_exists),
            patch("builtins.open", mock_open(read_data=config_json)),
            patch("src.integrations.kanban_client.os.environ", {}) as mock_env,
            patch("sys.stderr"),
        ):
            client = KanbanClient()

            # Verify partial config was loaded
            assert mock_env["PLANKA_BASE_URL"] == "http://partial:3333"
            # Defaults should be set for missing values
            assert mock_env["PLANKA_AGENT_EMAIL"] == "demo@demo.demo"
            assert mock_env["PLANKA_AGENT_PASSWORD"] == "demo"

    @pytest.mark.unit
    def test_load_config_honors_marcus_config_env_var(self):
        """MARCUS_CONFIG must win over both the cwd and repo-root
        candidates (issue #891).

        _load_config's own candidate list never checked MARCUS_CONFIG
        at all -- every other config-loading path in this codebase
        (MarcusConfig.get_config(), the legacy ConfigLoader) checks it
        first. An isolated experiment or live check that sets
        MARCUS_CONFIG expecting Marcus to honor it could not actually
        rely on that here: this client would silently fall back to
        whatever config_marcus.json happened to be in the cwd or the
        repo root instead, exactly the class of bug the #883/#886
        live check's own addendum found.

        Both the cwd path and the MARCUS_CONFIG path "exist" here,
        with different content, so a correct priority order is the
        only way this test passes -- it isn't enough for the override
        to merely work when nothing else is present.
        """
        override_config = json.dumps({"project_id": "from-marcus-config-override"})
        cwd_config = json.dumps({"project_id": "from-cwd-should-lose"})

        def selective_path_exists(self):
            path_str = str(self)
            if path_str == "/fake/override/config_marcus.json":
                return True
            if path_str == "config_marcus.json":
                return True
            return False

        def selective_open(path, *args, **kwargs):
            if str(path) == "/fake/override/config_marcus.json":
                return mock_open(read_data=override_config)()
            return mock_open(read_data=cwd_config)()

        with (
            patch(
                "src.integrations.kanban_client.os.environ",
                {"MARCUS_CONFIG": "/fake/override/config_marcus.json"},
            ),
            patch("pathlib.Path.exists", selective_path_exists),
            patch("builtins.open", selective_open),
            patch("sys.stderr"),
            patch.object(KanbanClient, "_load_workspace_state", return_value=None),
        ):
            client = KanbanClient()

        assert client.project_id == "from-marcus-config-override"

    @pytest.mark.asyncio
    async def test_get_available_tasks_success(
        self,
        mock_stdio_client,
        mock_client_session_context,
        mock_client_session,
        sample_lists,
        sample_cards,
    ):
        """Test successful retrieval of available tasks."""
        # Setup mock responses
        lists_response = Mock()
        lists_response.content = [Mock(text=json.dumps(sample_lists))]

        cards_responses = []
        for i, lst in enumerate(sample_lists):
            # Only TODO, Backlog have cards in our sample
            if lst["name"] in ["TODO", "Backlog"]:
                cards = [c for c in sample_cards if c["listName"] == lst["name"]]
                response = Mock()
                response.content = [Mock(text=json.dumps(cards))]
                cards_responses.append(response)
            else:
                response = Mock()
                response.content = [Mock(text="[]")]
                cards_responses.append(response)

        # Configure mock to return different responses
        mock_client_session.call_tool.side_effect = [lists_response] + cards_responses

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board-456"

            tasks = await client.get_available_tasks()

            # Verify we got the unassigned tasks from TODO and Backlog
            assert len(tasks) == 1  # Only card-1 is unassigned and in TODO
            assert tasks[0].id == "card-1"
            assert tasks[0].name == "Implement feature X"
            assert tasks[0].status == TaskStatus.TODO
            assert tasks[0].assigned_to is None

    @pytest.mark.asyncio
    async def test_get_available_tasks_no_board_id(self):
        """Test get_available_tasks raises error when board_id is not set."""
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = None

            with pytest.raises(RuntimeError, match="Board ID not set"):
                await client.get_available_tasks()

    @pytest.mark.asyncio
    async def test_get_available_tasks_empty_board(
        self, mock_stdio_client, mock_client_session_context, mock_client_session
    ):
        """Test get_available_tasks with empty board."""
        # Setup mock responses for empty board
        lists_response = Mock()
        lists_response.content = [Mock(text="[]")]

        mock_client_session.call_tool.return_value = lists_response

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board-456"

            tasks = await client.get_available_tasks()

            assert len(tasks) == 0

    @pytest.mark.asyncio
    async def test_get_all_tasks_success(
        self,
        mock_stdio_client,
        mock_client_session_context,
        mock_client_session,
        sample_lists,
        sample_cards,
    ):
        """Test successful retrieval of all tasks."""
        # Setup mock responses
        lists_response = Mock()
        lists_response.content = [Mock(text=json.dumps(sample_lists))]

        # Create responses for each list
        cards_responses = []
        for lst in sample_lists:
            cards = [c for c in sample_cards if c.get("listName") == lst["name"]]
            response = Mock()
            response.content = [Mock(text=json.dumps(cards) if cards else "[]")]
            cards_responses.append(response)

        mock_client_session.call_tool.side_effect = [lists_response] + cards_responses

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board-456"

            tasks = await client.get_all_tasks()

            # Should get all 4 sample cards as tasks
            assert len(tasks) == 4
            task_ids = [t.id for t in tasks]
            assert "card-1" in task_ids
            assert "card-2" in task_ids
            assert "card-3" in task_ids
            assert "card-4" in task_ids

    @pytest.mark.asyncio
    async def test_assign_task_success(
        self,
        mock_stdio_client,
        mock_client_session_context,
        mock_client_session,
        sample_lists,
    ):
        """Test successful task assignment."""
        # Setup mock responses
        comment_response = Mock()
        comment_response.content = [Mock(text='{"id": "comment-1"}')]

        lists_response = Mock()
        lists_response.content = [Mock(text=json.dumps(sample_lists))]

        move_response = Mock()
        move_response.content = [Mock(text='{"success": true}')]

        mock_client_session.call_tool.side_effect = [
            comment_response,
            lists_response,
            move_response,
        ]

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board-456"

            await client.assign_task("card-1", "agent-001")

            # Verify correct tool calls were made
            assert mock_client_session.call_tool.call_count == 3

            # Check comment creation
            comment_call = mock_client_session.call_tool.call_args_list[0]
            assert comment_call[0][0] == "mcp_kanban_comment_manager"
            assert comment_call[0][1]["action"] == "create"
            assert comment_call[0][1]["cardId"] == "card-1"
            assert "agent-001" in comment_call[0][1]["text"]

            # Check card move
            move_call = mock_client_session.call_tool.call_args_list[2]
            assert move_call[0][0] == "mcp_kanban_card_manager"
            assert move_call[0][1]["action"] == "move"
            assert move_call[0][1]["id"] == "card-1"
            assert move_call[0][1]["listId"] == "list-2"  # In Progress list

    @pytest.mark.asyncio
    async def test_assign_task_no_progress_list(
        self, mock_stdio_client, mock_client_session_context, mock_client_session
    ):
        """Test task assignment when no In Progress list exists."""
        # Setup mock responses
        comment_response = Mock()
        comment_response.content = [Mock(text='{"id": "comment-1"}')]

        # Lists without "progress" in name
        lists = [{"id": "list-1", "name": "TODO"}, {"id": "list-2", "name": "Done"}]
        lists_response = Mock()
        lists_response.content = [Mock(text=json.dumps(lists))]

        mock_client_session.call_tool.side_effect = [comment_response, lists_response]

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board-456"

            # Should complete without error (just adds comment, no move)
            await client.assign_task("card-1", "agent-001")

            # Only comment call should be made
            assert mock_client_session.call_tool.call_count == 2

    @pytest.mark.asyncio
    async def test_get_board_summary_success(
        self, mock_stdio_client, mock_client_session_context, mock_client_session
    ):
        """Test successful board summary retrieval."""
        summary_data = {
            "totalCards": 10,
            "completionPercentage": 30,
            "byStatus": {"todo": 5, "in_progress": 2, "done": 3},
        }

        summary_response = Mock()
        summary_response.content = [Mock(text=json.dumps(summary_data))]

        mock_client_session.call_tool.return_value = summary_response

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board-456"

            summary = await client.get_board_summary()

            assert summary["totalCards"] == 10
            assert summary["completionPercentage"] == 30
            assert summary["byStatus"]["todo"] == 5

    @pytest.mark.asyncio
    async def test_get_board_summary_no_board_id(self):
        """Test get_board_summary raises error when board_id is not set."""
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = None

            with pytest.raises(RuntimeError, match="Board ID not set"):
                await client.get_board_summary()

    @pytest.mark.asyncio
    async def test_add_comment_success(
        self, mock_stdio_client, mock_client_session_context, mock_client_session
    ):
        """Test successful comment addition."""
        comment_response = Mock()
        comment_response.content = [
            Mock(text='{"id": "comment-1", "text": "Test comment"}')
        ]

        mock_client_session.call_tool.return_value = comment_response

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()

            await client.add_comment("card-1", "Test comment")

            # Verify correct tool call
            mock_client_session.call_tool.assert_called_once_with(
                "mcp_kanban_comment_manager",
                {"action": "create", "cardId": "card-1", "text": "Test comment"},
            )

    @pytest.mark.asyncio
    async def test_complete_task_success(
        self,
        mock_stdio_client,
        mock_client_session_context,
        mock_client_session,
        sample_lists,
    ):
        """Test successful task completion."""
        lists_response = Mock()
        lists_response.content = [Mock(text=json.dumps(sample_lists))]

        move_response = Mock()
        move_response.content = [Mock(text='{"success": true}')]

        mock_client_session.call_tool.side_effect = [lists_response, move_response]

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board-456"

            await client.complete_task("card-1")

            # Verify card was moved to Done list
            move_call = mock_client_session.call_tool.call_args_list[1]
            assert move_call[0][1]["listId"] == "list-3"  # Done list

    @pytest.mark.asyncio
    async def test_update_task_status_blocked(
        self,
        mock_stdio_client,
        mock_client_session_context,
        mock_client_session,
        sample_lists,
    ):
        """Test updating task status to blocked."""
        lists_response = Mock()
        lists_response.content = [Mock(text=json.dumps(sample_lists))]

        move_response = Mock()
        move_response.content = [Mock(text='{"success": true}')]

        mock_client_session.call_tool.side_effect = [lists_response, move_response]

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board-456"

            await client.update_task_status("card-1", "blocked")

            # Verify card was moved to Blocked list
            move_call = mock_client_session.call_tool.call_args_list[1]
            assert move_call[0][1]["listId"] == "list-4"  # Blocked list

    @pytest.mark.asyncio
    async def test_move_task_to_list_not_found(
        self, mock_stdio_client, mock_client_session_context, mock_client_session
    ):
        """Test error when target list is not found."""
        # Lists without matching keywords
        lists = [{"id": "list-1", "name": "TODO"}, {"id": "list-2", "name": "Working"}]
        lists_response = Mock()
        lists_response.content = [Mock(text=json.dumps(lists))]

        mock_client_session.call_tool.return_value = lists_response

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board-456"

            with pytest.raises(RuntimeError, match="No list found matching keywords"):
                await client._move_task_to_list("card-1", ["completed", "done"])

    def test_is_available_task(self):
        """Test task availability checking."""
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()

            # Available tasks
            assert client._is_available_task({"listName": "TODO"})
            assert client._is_available_task({"listName": "TO DO"})
            assert client._is_available_task({"listName": "Backlog"})
            assert client._is_available_task({"listName": "READY"})
            assert client._is_available_task({"listName": "todo items"})

            # Not available tasks
            assert not client._is_available_task({"listName": "In Progress"})
            assert not client._is_available_task({"listName": "Done"})
            assert not client._is_available_task({"listName": "Blocked"})
            assert not client._is_available_task({"listName": "Review"})

    def test_card_to_task_basic(self):
        """Test basic card to task conversion."""
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()

            card = {
                "id": "card-123",
                "name": "Test Task",
                "description": "Test description",
                "listName": "TODO",
            }

            task = client._card_to_task(card)

            assert task.id == "card-123"
            assert task.name == "Test Task"
            assert task.description == "Test description"
            assert task.status == TaskStatus.TODO
            assert task.priority == Priority.MEDIUM
            assert task.assigned_to is None

    def test_card_to_task_with_status_mapping(self):
        """Test card to task conversion with different statuses."""
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()

            # Test DONE status
            card_done = {"id": "1", "name": "Done task", "listName": "Done"}
            task_done = client._card_to_task(card_done)
            assert task_done.status == TaskStatus.DONE

            # Test IN_PROGRESS status
            card_progress = {"id": "2", "name": "WIP task", "listName": "In Progress"}
            task_progress = client._card_to_task(card_progress)
            assert task_progress.status == TaskStatus.IN_PROGRESS

            # Test BLOCKED status
            card_blocked = {"id": "3", "name": "Blocked task", "listName": "Blocked"}
            task_blocked = client._card_to_task(card_blocked)
            assert task_blocked.status == TaskStatus.BLOCKED

    def test_card_to_task_with_assignment(self):
        """Test card to task conversion with different assignment formats."""
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()

            # Test with users field (Planka format)
            card_users = {
                "id": "1",
                "name": "Task 1",
                "listName": "TODO",
                "users": [
                    {"username": "john_doe", "email": "john@example.com"},
                    {"username": "jane_doe"},
                ],
            }
            task_users = client._card_to_task(card_users)
            assert task_users.assigned_to == "john_doe"

            # Test with assignedTo field
            card_assigned = {
                "id": "2",
                "name": "Task 2",
                "listName": "TODO",
                "assignedTo": "agent-001",
            }
            task_assigned = client._card_to_task(card_assigned)
            assert task_assigned.assigned_to == "agent-001"

            # Test with assigned_to field
            card_assigned_to = {
                "id": "3",
                "name": "Task 3",
                "listName": "TODO",
                "assigned_to": "agent-002",
            }
            task_assigned_to = client._card_to_task(card_assigned_to)
            assert task_assigned_to.assigned_to == "agent-002"

    def test_card_to_task_fallback_values(self):
        """Test card to task conversion with missing fields."""
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()

            # Minimal card
            card = {"id": "minimal-1"}
            task = client._card_to_task(card)

            assert task.id == "minimal-1"
            assert task.name == ""
            assert task.description == ""
            assert task.status == TaskStatus.TODO
            assert task.priority == Priority.MEDIUM
            assert task.assigned_to is None

            # Card with title instead of name
            card_title = {"id": "2", "title": "Task with title"}
            task_title = client._card_to_task(card_title)
            assert task_title.name == "Task with title"

    def test_card_to_task_stamps_the_configured_project_id(self):
        """Codex P1, round 1 on PR #842: a card carries no project id
        of its own -- the client is scoped to one project per
        instance, so _card_to_task must stamp it from self.project_id.
        Previously left None, silently breaking any project_id-scoped
        filter over tasks from this provider (e.g.
        get_all_board_tasks)."""
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.project_id = "planka-project-123"

            task = client._card_to_task({"id": "card-1", "name": "Task"})

            assert task.project_id == "planka-project-123"

    def test_card_to_task_prefers_the_registry_project_id(self):
        """Issue #854: the real fix for #849/#853's Planka/Linear

        regression. When the client is constructed from a registered
        ProjectConfig, ``registry_project_id`` carries that config's
        own ``id`` (set by ``project_context_manager``), which must
        win over the provider's own ``project_id`` -- the two are
        different strings for Planka/Linear (unlike SQLite, GH-306),
        and a task's ``project_id`` must match a registry key
        directly for every K2-keyed lookup to find it."""
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.project_id = "planka-project-123"
            client.registry_project_id = "uuid-registry-9"

            task = client._card_to_task({"id": "card-1", "name": "Task"})

            assert task.project_id == "uuid-registry-9"

    def test_card_to_task_falls_back_without_a_registry_project_id(self):
        """A client constructed outside project_context_manager (a

        standalone script, a test) has no registry id to carry;
        falling back to the provider's own project id keeps every
        existing, non-registry-aware construction path working."""
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.project_id = "planka-project-123"

            task = client._card_to_task({"id": "card-1", "name": "Task"})

            assert task.project_id == "planka-project-123"

    @pytest.mark.asyncio
    async def test_mcp_connection_error(self):
        """Test handling of MCP connection errors."""

        # Create a mock that raises ConnectionError when entering context
        def failing_stdio_client(server_params):
            @asynccontextmanager
            async def _failing():
                raise ConnectionError("Failed to connect to MCP server")
                yield  # This will never be reached

            return _failing()

        with (
            patch("src.integrations.kanban_client.stdio_client", failing_stdio_client),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board"

            with pytest.raises(ConnectionError):
                await client.get_available_tasks()

    @pytest.mark.asyncio
    async def test_json_parsing_error(
        self, mock_stdio_client, mock_client_session_context, mock_client_session
    ):
        """Test handling of JSON parsing errors."""
        # Return invalid JSON
        invalid_response = Mock()
        invalid_response.content = [Mock(text="invalid json {")]

        mock_client_session.call_tool.return_value = invalid_response

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board"

            with pytest.raises(json.JSONDecodeError):
                await client.get_available_tasks()

    @pytest.mark.asyncio
    async def test_empty_response_handling(
        self, mock_stdio_client, mock_client_session_context, mock_client_session
    ):
        """Test handling of empty responses."""
        # Test with None response
        mock_client_session.call_tool.return_value = None

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board"

            tasks = await client.get_available_tasks()
            assert len(tasks) == 0

    @pytest.mark.asyncio
    async def test_get_projects_empty_string_returns_empty_list(
        self, mock_stdio_client, mock_client_session_context, mock_client_session
    ):
        """Test get_projects returns [] when the MCP response body is an empty string."""
        empty_string_response = Mock()
        empty_string_response.content = [Mock(text="")]

        mock_client_session.call_tool.return_value = empty_string_response

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()

            projects = await client.get_projects()

            assert projects == []

    @pytest.mark.asyncio
    async def test_get_projects_valid_json_returns_project_list(
        self, mock_stdio_client, mock_client_session_context, mock_client_session
    ):
        """Test get_projects returns the parsed project list for a valid JSON array."""
        expected_projects = [
            {"id": "proj-1", "name": "Project One"},
            {"id": "proj-2", "name": "Project Two"},
        ]
        valid_response = Mock()
        valid_response.content = [Mock(text=json.dumps(expected_projects))]

        mock_client_session.call_tool.return_value = valid_response

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()

            projects = await client.get_projects()

            assert projects == expected_projects

    @pytest.mark.asyncio
    async def test_malformed_response_structure(
        self, mock_stdio_client, mock_client_session_context, mock_client_session
    ):
        """Test handling of responses with unexpected structure."""
        # Response without content attribute
        malformed_response = Mock(spec=[])  # No content attribute

        mock_client_session.call_tool.return_value = malformed_response

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board"

            tasks = await client.get_available_tasks()
            assert len(tasks) == 0

    def test_config_file_read_error(self):
        """Test handling of config file read errors."""
        # Selective mock: config_marcus.json exists, others may not
        original_exists = Path.exists

        def selective_path_exists(self):
            if "config_marcus.json" in str(self):
                return True
            return original_exists(self)

        # The module doesn't handle file read errors, so we expect it to raise
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=True),
            patch("pathlib.Path.exists", selective_path_exists),
            patch("builtins.open", side_effect=IOError("Permission denied")),
            patch("src.integrations.kanban_client.os.environ", {}) as mock_env,
            patch("sys.stderr"),
        ):
            # The module will raise the IOError
            with pytest.raises(IOError, match="Permission denied"):
                client = KanbanClient()

    def test_config_file_invalid_json(self):
        """Test handling of invalid JSON in config file."""
        # Selective mock: config_marcus.json exists, others may not
        original_exists = Path.exists

        def selective_path_exists(self):
            if "config_marcus.json" in str(self):
                return True
            return original_exists(self)

        # The module doesn't handle JSON errors, so we expect it to raise
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=True),
            patch("pathlib.Path.exists", selective_path_exists),
            patch("builtins.open", mock_open(read_data="invalid json {")),
            patch("src.integrations.kanban_client.os.environ", {}) as mock_env,
            patch("sys.stderr"),
        ):
            # The module will raise JSONDecodeError
            with pytest.raises(json.JSONDecodeError):
                client = KanbanClient()

    @pytest.mark.asyncio
    async def test_concurrent_operations(
        self,
        mock_stdio_client,
        mock_client_session_context,
        mock_client_session,
        sample_lists,
    ):
        """Test that concurrent operations work correctly."""
        # Setup mock responses
        lists_response = Mock()
        lists_response.content = [Mock(text=json.dumps(sample_lists))]

        summary_response = Mock()
        summary_response.content = [Mock(text='{"totalCards": 5}')]

        # Mock responses for multiple operations
        # First operation: get_available_tasks needs lists + 5 card responses
        # Second operation: get_board_summary
        empty_cards = Mock()
        empty_cards.content = [Mock(text="[]")]

        mock_client_session.call_tool.side_effect = [
            lists_response,  # For get_available_tasks
            empty_cards,
            empty_cards,
            empty_cards,
            empty_cards,
            empty_cards,  # 5 empty card responses
            summary_response,  # For get_board_summary
        ]

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board"

            # Run multiple operations concurrently
            tasks_coro = client.get_available_tasks()
            summary_coro = client.get_board_summary()

            tasks, summary = await asyncio.gather(tasks_coro, summary_coro)

            # Both should succeed
            assert isinstance(tasks, list)
            assert isinstance(summary, dict)
            assert summary["totalCards"] == 5

    @pytest.mark.asyncio
    async def test_session_cleanup_on_error(
        self, mock_stdio_client, mock_client_session_context, mock_client_session
    ):
        """Test that sessions are properly cleaned up even on errors."""
        # Make call_tool raise an exception
        mock_client_session.call_tool.side_effect = RuntimeError("Tool error")

        # Track whether context managers were properly used
        stdio_entered = False
        stdio_exited = False

        @asynccontextmanager
        async def tracking_stdio_client(server_params):
            nonlocal stdio_entered, stdio_exited
            stdio_entered = True
            try:
                yield (Mock(), Mock())
            finally:
                stdio_exited = True

        with (
            patch("src.integrations.kanban_client.stdio_client", tracking_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board"

            with pytest.raises(RuntimeError):
                await client.get_available_tasks()

            # Context managers should be entered and exited even with error
            assert stdio_entered
            assert stdio_exited

    @pytest.mark.asyncio
    async def test_get_board_summary_empty_response(
        self, mock_stdio_client, mock_client_session_context, mock_client_session
    ):
        """Test get_board_summary with empty response."""
        # Response with no content - the module will raise TypeError
        empty_response = Mock()
        empty_response.content = None

        mock_client_session.call_tool.return_value = empty_response

        with (
            patch("src.integrations.kanban_client.stdio_client", mock_stdio_client),
            patch(
                "src.integrations.kanban_client.ClientSession",
                mock_client_session_context,
            ),
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()
            client.board_id = "test-board-456"

            # The module will raise TypeError when trying to access None[0]
            with pytest.raises(TypeError):
                await client.get_board_summary()

    def test_card_to_task_with_empty_users_list(self):
        """Test card to task conversion when users list is empty."""
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()

            card = {
                "id": "card-1",
                "name": "Test Task",
                "listName": "TODO",
                "users": [],  # Empty users list
            }

            task = client._card_to_task(card)
            assert task.assigned_to is None

    def test_card_to_task_with_user_missing_fields(self):
        """Test card to task conversion when user objects have missing fields."""
        with (
            patch("src.integrations.kanban_client.os.path.exists", return_value=False),
            patch("src.integrations.kanban_client.os.environ", {}),
            patch("sys.stderr"),
        ):
            client = KanbanClient()

            # User with only email
            card = {
                "id": "card-1",
                "name": "Test Task",
                "listName": "TODO",
                "users": [{"email": "test@example.com"}],
            }

            task = client._card_to_task(card)
            assert task.assigned_to == "test@example.com"

            # User with only name
            card2 = {
                "id": "card-2",
                "name": "Test Task 2",
                "listName": "TODO",
                "users": [{"name": "John Doe"}],
            }

            task2 = client._card_to_task(card2)
            assert task2.assigned_to == "John Doe"
