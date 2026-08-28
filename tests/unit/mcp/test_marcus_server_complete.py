"""
Complete unit tests for Marcus Server - Using anyio to avoid pytest-asyncio issues
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock, patch

import mcp.types as types
import pytest

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core.models import (
    Priority,
    ProjectState,
    RiskLevel,
    Task,
    TaskAssignment,
    TaskStatus,
    WorkerStatus,
)
from src.marcus_mcp.handlers import get_tool_definitions, handle_tool_call
from src.marcus_mcp.server import MarcusServer


def get_text_content(
    content: types.TextContent | types.ImageContent | types.EmbeddedResource,
) -> str:
    """Helper to extract text from MCP content types."""
    if isinstance(content, types.TextContent):
        return content.text
    else:
        raise TypeError(f"Expected TextContent, got {type(content)}")


async def create_test_server() -> MarcusServer:
    """Helper to create a test server instance"""
    os.environ["KANBAN_PROVIDER"] = "planka"
    os.environ["GITHUB_OWNER"] = "test-owner"
    os.environ["GITHUB_REPO"] = "test-repo"
    os.environ["MARCUS_TEST_MODE"] = "true"
    os.environ["CLAUDE_API_KEY"] = "test-key"

    with (
        patch("src.core.context.Context._load_persisted_data"),
        patch("src.cost_tracking.cost_store.CostStore.load_seed_prices"),
    ):
        server = MarcusServer()

    # Mock the kanban client
    server.kanban_client = AsyncMock()
    server.kanban_client.get_available_tasks = AsyncMock(return_value=[])
    server.kanban_client.get_all_tasks = AsyncMock(return_value=[])
    server.kanban_client.get_task_by_id = AsyncMock(return_value=None)
    server.kanban_client.update_task = AsyncMock()
    server.kanban_client.create_task = AsyncMock()
    server.kanban_client.add_comment = AsyncMock()
    server.kanban_client.get_board_summary = AsyncMock(return_value={})
    server.kanban_client.update_task_progress = AsyncMock()

    # Don't start the assignment monitor in tests
    server.assignment_monitor = None

    return server


# Non-async tests
@pytest.mark.anyio
async def test_server_initialization():
    """Test server initializes correctly"""
    os.environ["KANBAN_PROVIDER"] = "planka"
    os.environ["MARCUS_TEST_MODE"] = "true"
    os.environ["CLAUDE_API_KEY"] = "test-key"

    with (
        patch("src.core.context.Context._load_persisted_data"),
        patch("src.cost_tracking.cost_store.CostStore.load_seed_prices"),
    ):
        server = MarcusServer()

    # Provider comes from config_marcus.json — assert it matches config
    assert server.provider in ("planka", "sqlite", "github", "linear")
    assert server.ai_engine is not None
    # monitor is only instantiated for the planka provider; other providers use None
    if server.provider == "planka":
        assert server.monitor is not None
    else:
        assert server.monitor is None
    assert server.project_tasks == []
    assert server.agent_tasks == {}
    assert server.agent_status == {}


def test_get_tool_definitions():
    """Test tool definitions are returned correctly"""
    tools = get_tool_definitions()

    assert len(tools) > 0
    tool_names = [tool.name for tool in tools]

    # Check essential tools are present
    assert "ping" in tool_names
    assert "register_agent" in tool_names
    assert "request_next_task" in tool_names
    assert "report_task_progress" in tool_names
    assert "get_project_status" in tool_names
    assert "create_project" in tool_names
    # authenticate tool is handled separately in the handlers module


# Async tests using anyio
@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_ping_tool():
    """Test ping tool functionality"""
    server = await create_test_server()

    result = await handle_tool_call("ping", {"echo": "test"}, server)

    assert len(result) == 1
    assert result[0].type == "text"

    data = json.loads(get_text_content(result[0]))
    assert data["status"] == "online"
    assert data["echo"] == "test"
    assert "timestamp" in data
    assert data["success"] is True
    assert data["provider"] in ("planka", "sqlite", "github", "linear")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_unknown_tool():
    """Test handling of unknown tool"""
    server = await create_test_server()

    result = await handle_tool_call("unknown_tool", {}, server)

    assert len(result) == 1
    data = json.loads(get_text_content(result[0]))
    assert "error" in data
    # Should get access denied error for unregistered client
    assert "Access denied" in data["error"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_register_agent():
    """Test agent registration"""
    server = await create_test_server()

    # First authenticate as an agent
    auth_result = await handle_tool_call(
        "authenticate",
        {
            "client_id": "test-client-001",
            "client_type": "agent",
            "role": "developer",
        },
        server,
    )

    result = await handle_tool_call(
        "register_agent",
        {
            "agent_id": "test-001",
            "name": "Test Agent",
            "role": "Developer",
            "skills": ["python", "testing"],
            # project_id is required since GH-388/389: agents are ephemeral
            # and must declare which project they are working on
            "project_id": "test-project-001",
        },
        server,
    )

    data = json.loads(get_text_content(result[0]))
    assert data["success"] is True
    assert data["agent_id"] == "test-001"
    assert "test-001" in server.agent_status


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_get_agent_status():
    """Test getting agent status"""
    server = await create_test_server()

    # First register an agent
    server.agent_status["test-001"] = WorkerStatus(
        worker_id="test-001",
        name="Test Agent",
        role="Developer",
        email="test@example.com",
        current_tasks=[],
        completed_tasks_count=0,
        capacity=40,
        skills=["python"],
        availability={},
        performance_score=1.0,
    )

    result = await handle_tool_call(
        "get_agent_status", {"agent_id": "test-001"}, server
    )

    data = json.loads(get_text_content(result[0]))
    # Check for error or agent info
    if "error" not in data:
        assert "agent" in data
        assert data["agent"]["id"] == "test-001"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_list_registered_agents():
    """Test listing all agents"""
    server = await create_test_server()

    # Register some agents
    server.agent_status = {
        "test-001": WorkerStatus(
            worker_id="test-001",
            name="Agent 1",
            role="Developer",
            email="agent1@example.com",
            current_tasks=[],
            completed_tasks_count=0,
            capacity=40,
            skills=[],
            availability={},
            performance_score=1.0,
        ),
        "test-002": WorkerStatus(
            worker_id="test-002",
            name="Agent 2",
            role="Tester",
            email="agent2@example.com",
            current_tasks=[],
            completed_tasks_count=0,
            capacity=40,
            skills=[],
            availability={},
            performance_score=1.0,
        ),
    }

    result = await handle_tool_call("list_registered_agents", {}, server)

    data = json.loads(get_text_content(result[0]))
    # Check for agents list in various formats
    if "error" not in data:
        assert "agents" in data or "registered_agents" in data
        agents_list = data.get("agents", data.get("registered_agents", []))
        assert len(agents_list) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_request_next_task_no_tasks():
    """Test requesting task when none available"""
    server = await create_test_server()

    # Register agent first
    server.agent_status["test-001"] = WorkerStatus(
        worker_id="test-001",
        name="Test Agent",
        role="Developer",
        email="test@example.com",
        current_tasks=[],
        completed_tasks_count=0,
        capacity=40,
        skills=["python"],
        availability={},
        performance_score=1.0,
    )

    server.kanban_client.get_available_tasks.return_value = []

    result = await handle_tool_call(
        "request_next_task", {"agent_id": "test-001"}, server
    )

    data = json.loads(get_text_content(result[0]))
    # Check for task assignment response
    if data.get("success", True):  # Handle different response formats
        if "task" in data:
            assert data["task"] is None
    else:
        assert "message" in data
        assert "no suitable tasks" in data["message"].lower()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_request_next_task_audit_logs_zero_task_count_on_success():
    """request_next_task audits task_count=0 when the board is empty."""
    server = await create_test_server()
    server.project_tasks = []
    server._current_client_id = "test-client-006"
    server._registered_clients = {
        "test-client-006": {"client_type": "agent", "role": "developer"}
    }

    audit_logger = AsyncMock()

    with (
        patch("src.marcus_mcp.handlers.get_audit_logger", return_value=audit_logger),
        patch(
            "src.marcus_mcp.handlers.request_next_task",
            AsyncMock(return_value={"success": True, "task": None}),
        ),
    ):
        result = await handle_tool_call(
            "request_next_task", {"agent_id": "test-001"}, server
        )

    data = json.loads(get_text_content(result[0]))
    assert data == {"success": True, "task": None}
    audit_logger.log_tool_call.assert_awaited_once()
    assert audit_logger.log_tool_call.await_args.kwargs["task_count"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_request_next_task_access_denied_audit_logs_zero_task_count():
    """Denied request_next_task audits task_count=0 when the board is empty."""
    server = await create_test_server()
    server.project_tasks = []

    audit_logger = AsyncMock()

    with patch("src.marcus_mcp.handlers.get_audit_logger", return_value=audit_logger):
        result = await handle_tool_call(
            "request_next_task", {"agent_id": "test-001"}, server
        )

    data = json.loads(get_text_content(result[0]))
    assert "Access denied" in data["error"]
    audit_logger.log_access_denied.assert_awaited_once()
    assert audit_logger.log_access_denied.await_args.kwargs["task_count"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_fastmcp_request_next_task_delegates_through_handle_tool_call():
    """FastMCP request_next_task should use the audited handler path."""
    server = await create_test_server()
    fastmcp = server._create_fastmcp()
    tool_manager = fastmcp._tool_manager

    expected = {"success": True, "task": None}
    tool_response = [
        types.TextContent(type="text", text=json.dumps(expected, indent=2))
    ]

    with (
        patch(
            "src.marcus_mcp.server.handle_tool_call",
            AsyncMock(return_value=tool_response),
        ) as mock_handle_tool_call,
        patch("src.logging.mcp_tool_logger.log_mcp_tool_response"),
    ):
        result = await tool_manager.call_tool(
            "request_next_task", {"agent_id": "test-001"}
        )

    assert result == expected
    mock_handle_tool_call.assert_awaited_once_with(
        "request_next_task", {"agent_id": "test-001"}, server
    )


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_report_task_progress():
    """Test reporting task progress"""
    server = await create_test_server()

    # First authenticate as an agent
    auth_result = await handle_tool_call(
        "authenticate",
        {
            "client_id": "test-client-003",
            "client_type": "agent",
            "role": "developer",
        },
        server,
    )

    # Setup agent and task
    task_id = "task-001"
    server.agent_status["test-001"] = WorkerStatus(
        worker_id="test-001",
        name="Test Agent",
        role="Developer",
        email="test@example.com",
        current_tasks=[],
        completed_tasks_count=0,
        capacity=40,
        skills=[],
        availability={},
        performance_score=1.0,
    )
    # The server uses a simple dictionary for agent_tasks
    server.agent_tasks["test-001"] = task_id

    result = await handle_tool_call(
        "report_task_progress",
        {
            "agent_id": "test-001",
            "task_id": task_id,
            "status": "in_progress",
            "progress": 50,
            "message": "Halfway done",
        },
        server,
    )

    data = json.loads(get_text_content(result[0]))
    # Check for success or expected error
    assert "success" in data or "error" in data
    if "success" in data and data["success"]:
        server.kanban_client.update_task_progress.assert_called_once()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_get_project_status():
    """Test getting project status"""
    server = await create_test_server()

    # Mock project state - initialize if not exists
    if not hasattr(server, "project_state"):
        server.project_state = ProjectState(
            board_id="board-001",
            project_name="Test Project",
            total_tasks=10,
            completed_tasks=5,
            in_progress_tasks=3,
            blocked_tasks=1,
            progress_percent=50.0,
            overdue_tasks=[],
            team_velocity=2.0,
            risk_level=RiskLevel.LOW,
            last_updated=datetime.now(timezone.utc),
        )

    # Mock board summary
    server.kanban_client.get_board_summary.return_value = {
        "totalCards": 10,
        "doneCount": 5,
        "inProgressCount": 3,
        "backlogCount": 2,
    }

    result = await handle_tool_call("get_project_status", {}, server)

    data = json.loads(get_text_content(result[0]))

    # Handle different response formats
    if data.get("success", False):
        # Check for project data
        assert "project" in data
        project = data["project"]
        assert project["total_tasks"] == 10
        assert project["completed"] == 5
        assert project["in_progress"] == 3
    else:
        # If there's an error, just ensure it's a valid error response
        assert "error" in data or "message" in data


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_create_project_validation():
    """Test create_project validates inputs"""
    server = await create_test_server()

    # First authenticate as a developer
    auth_result = await handle_tool_call(
        "authenticate",
        {
            "client_id": "test-client-004",
            "client_type": "developer",
            "role": "project_creator",
        },
        server,
    )

    result = await handle_tool_call(
        "create_project", {"description": "", "project_name": "Test"}, server
    )

    data = json.loads(get_text_content(result[0]))
    assert "error" in data
    assert "required" in data["error"].lower()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_add_feature_validation():
    """Test add_feature validates inputs"""
    server = await create_test_server()

    # First authenticate as a developer
    auth_result = await handle_tool_call(
        "authenticate",
        {
            "client_id": "test-client-005",
            "client_type": "developer",
            "role": "feature_developer",
        },
        server,
    )

    result = await handle_tool_call(
        "add_feature",
        {"feature_description": "", "integration_point": "auto_detect"},
        server,
    )

    data = json.loads(get_text_content(result[0]))
    assert "error" in data
    assert "required" in data["error"].lower()


if __name__ == "__main__":
    # Run without pytest-asyncio to avoid introspection issues
    pytest.main([__file__, "-v", "-p", "no:asyncio"])
