"""
Unit tests for Inspector (unified MCP client).

Tests both stdio and HTTP connection types.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.worker.inspector import Inspector, create_inspector

pytestmark = pytest.mark.unit


class MockTool:
    """Mock tool with name attribute."""

    def __init__(self, tool_name):
        """Initialize mock tool with name."""
        self.name = tool_name


class MockClientSession:
    """Mock ClientSession for testing."""

    def __init__(self):
        """Initialize mock session."""
        self.initialize = AsyncMock()
        self.list_tools = AsyncMock(
            return_value=Mock(
                tools=[
                    MockTool("register_agent"),
                    MockTool("request_next_task"),
                    MockTool("report_task_progress"),
                    MockTool("report_blocker"),
                    MockTool("ping"),
                ]
            )
        )
        self.call_tool = AsyncMock()


class TestInspectorInitialization:
    """Test Inspector initialization and configuration."""

    def test_default_initialization(self):
        """Test Inspector initializes with default stdio connection type."""
        client = Inspector()
        assert client.connection_type == "stdio"
        assert client.session is None

    def test_stdio_initialization(self):
        """Test Inspector initializes with stdio connection type."""
        client = Inspector(connection_type="stdio")
        assert client.connection_type == "stdio"
        assert client.session is None

    def test_http_initialization(self):
        """Test Inspector initializes with HTTP connection type."""
        client = Inspector(connection_type="http")
        assert client.connection_type == "http"
        assert client.session is None


class TestInspectorAgentMethods:
    """Test Inspector agent-related methods (register, task request, etc.)."""

    @pytest.fixture
    def stdio_client(self):
        """Create an Inspector instance with stdio."""
        return Inspector(connection_type="stdio")

    @pytest.fixture
    def http_client(self):
        """Create an Inspector instance with HTTP."""
        return Inspector(connection_type="http")

    @pytest.fixture
    def mock_session(self):
        """Create a mock session."""
        return MockClientSession()

    @pytest.mark.asyncio
    async def test_register_agent(self, stdio_client, mock_session):
        """Test agent registration."""
        # Setup mock response
        mock_result = Mock()
        mock_result.content = [
            Mock(
                text=json.dumps(
                    {
                        "success": True,
                        "message": "Agent registered successfully",
                        "agent_id": "test-agent-001",
                    }
                )
            )
        ]
        mock_session.call_tool.return_value = mock_result

        # Set session
        stdio_client.session = mock_session

        # Call register_agent
        result = await stdio_client.register_agent(
            "test-agent-001", "Test Agent", "Developer", ["python", "testing"]
        )

        # Verify
        assert result["success"] is True
        assert result["agent_id"] == "test-agent-001"
        mock_session.call_tool.assert_called_once_with(
            "register_agent",
            arguments={
                "agent_id": "test-agent-001",
                "name": "Test Agent",
                "role": "Developer",
                "skills": ["python", "testing"],
                # project_id (GH-388) and vendor/principal (issue #737,
                # PR #739) were added to the real call after this test
                # was written; register_agent's own defaults ("", "",
                # "agent") are what a caller who doesn't pass them (as
                # this test's own call below doesn't) actually sends.
                "project_id": "",
                "vendor": "",
                "principal": "agent",
            },
        )

    @pytest.mark.asyncio
    async def test_request_next_task(self, http_client, mock_session):
        """Test task request."""
        # Setup mock response
        mock_result = Mock()
        mock_result.content = [
            Mock(
                text=json.dumps(
                    {
                        "success": True,
                        "task": {
                            "task_id": "task-123",
                            "title": "Test Task",
                            "priority": "high",
                        },
                    }
                )
            )
        ]
        mock_session.call_tool.return_value = mock_result

        # Set session
        http_client.session = mock_session

        # Call request_next_task
        result = await http_client.request_next_task("test-agent-001")

        # Verify
        assert result["success"] is True
        assert result["task"]["task_id"] == "task-123"
        mock_session.call_tool.assert_called_once_with(
            "request_next_task", arguments={"agent_id": "test-agent-001"}
        )

    @pytest.mark.asyncio
    async def test_report_task_progress(self, stdio_client, mock_session):
        """Test progress reporting."""
        # Setup mock response
        mock_result = Mock()
        mock_result.content = [
            Mock(
                text=json.dumps(
                    {"success": True, "message": "Progress updated successfully"}
                )
            )
        ]
        mock_session.call_tool.return_value = mock_result

        # Set session
        stdio_client.session = mock_session

        # Call report_task_progress
        result = await stdio_client.report_task_progress(
            "test-agent-001", "task-123", "in_progress", 50, "Halfway done"
        )

        # Verify
        assert result["success"] is True
        mock_session.call_tool.assert_called_once_with(
            "report_task_progress",
            arguments={
                "agent_id": "test-agent-001",
                "task_id": "task-123",
                "status": "in_progress",
                "progress": 50,
                "message": "Halfway done",
            },
        )

    @pytest.mark.asyncio
    async def test_report_blocker(self, http_client, mock_session):
        """Test blocker reporting."""
        # Setup mock response
        mock_result = Mock()
        mock_result.content = [
            Mock(
                text=json.dumps(
                    {
                        "success": True,
                        "suggestions": ["Try solution A", "Try solution B"],
                    }
                )
            )
        ]
        mock_session.call_tool.return_value = mock_result

        # Set session
        http_client.session = mock_session

        # Call report_blocker
        result = await http_client.report_blocker(
            "test-agent-001", "task-123", "Missing API credentials", "medium"
        )

        # Verify
        assert result["success"] is True
        assert len(result["suggestions"]) == 2

    @pytest.mark.asyncio
    async def test_no_session_error(self, stdio_client):
        """Test error when no session is established."""
        with pytest.raises(RuntimeError, match="Not connected to Marcus"):
            await stdio_client.register_agent("test", "Test", "Developer", [])

    @pytest.mark.asyncio
    async def test_empty_response_handling(self, stdio_client, mock_session):
        """Test handling of empty responses."""
        # Setup mock response with no content
        mock_result = Mock()
        mock_result.content = []
        mock_session.call_tool.return_value = mock_result

        # Set session
        stdio_client.session = mock_session

        # Call method
        result = await stdio_client.request_next_task("test-agent-001")

        # Verify empty dict returned
        assert result == {}


class TestInspectorStdioConnection:
    """Test Inspector stdio connection functionality."""

    @pytest.mark.asyncio
    @patch("src.worker.inspector.stdio_client")
    @patch("src.worker.inspector.ClientSession")
    async def test_stdio_connect(self, mock_client_session, mock_stdio_client):
        """Test stdio connection establishes correctly."""
        # Setup mocks
        mock_read_stream = AsyncMock()
        mock_write_stream = AsyncMock()
        mock_session_instance = MockClientSession()

        @asynccontextmanager
        async def mock_stdio_context(params):
            yield (mock_read_stream, mock_write_stream)

        @asynccontextmanager
        async def mock_session_context(read, write):
            yield mock_session_instance

        mock_stdio_client.return_value = mock_stdio_context(None)
        mock_client_session.return_value = mock_session_context(None, None)

        # Test connection
        client = Inspector(connection_type="stdio")
        async with client.connect() as session:
            assert session == mock_session_instance
            assert client.session == mock_session_instance
            mock_session_instance.initialize.assert_called_once()
            mock_session_instance.list_tools.assert_called_once()

    @pytest.mark.asyncio
    async def test_stdio_invalid_connection_type(self):
        """Test that invalid connection type raises error."""
        client = Inspector(connection_type="stdio")
        # Manually set invalid type to test error handling
        client.connection_type = "invalid"

        with pytest.raises(ValueError, match="Invalid connection_type"):
            async with client.connect():
                pass


class TestInspectorHttpConnection:
    """Test Inspector HTTP connection functionality."""

    @pytest.mark.asyncio
    @patch("src.worker.inspector.streamablehttp_client")
    @patch("src.worker.inspector.ClientSession")
    async def test_http_connect_with_url(
        self, mock_client_session, mock_streamable_http
    ):
        """Test HTTP connection with custom URL."""
        # Setup mocks
        mock_read_stream = AsyncMock()
        mock_write_stream = AsyncMock()
        mock_get_session_id = Mock(return_value="test-session-123")
        mock_session_instance = MockClientSession()

        @asynccontextmanager
        async def mock_http_context(url, timeout, sse_read_timeout):
            yield (mock_read_stream, mock_write_stream, mock_get_session_id)

        @asynccontextmanager
        async def mock_session_context(read, write):
            yield mock_session_instance

        mock_streamable_http.side_effect = mock_http_context
        mock_client_session.side_effect = mock_session_context

        # Test connection
        client = Inspector(connection_type="http")
        test_url = "http://localhost:4298/mcp"

        async with client.connect(url=test_url) as session:
            assert session == mock_session_instance
            assert client.session == mock_session_instance
            mock_session_instance.initialize.assert_called_once()
            mock_session_instance.list_tools.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.worker.inspector.streamablehttp_client")
    @patch("src.worker.inspector.ClientSession")
    async def test_http_connect_default_url(
        self, mock_client_session, mock_streamable_http
    ):
        """Test HTTP connection with default URL."""
        # Setup mocks
        mock_read_stream = AsyncMock()
        mock_write_stream = AsyncMock()
        mock_get_session_id = Mock(return_value="test-session-456")
        mock_session_instance = MockClientSession()

        # Track URL that was passed
        captured_url = None

        @asynccontextmanager
        async def mock_http_context(url, timeout, sse_read_timeout):
            nonlocal captured_url
            captured_url = url
            yield (mock_read_stream, mock_write_stream, mock_get_session_id)

        @asynccontextmanager
        async def mock_session_context(read, write):
            yield mock_session_instance

        mock_streamable_http.side_effect = mock_http_context
        mock_client_session.side_effect = mock_session_context

        # Test connection without URL (should use default)
        client = Inspector(connection_type="http")
        async with client.connect() as session:
            assert session == mock_session_instance
            # Verify default URL was used
            assert captured_url == "http://localhost:4298/mcp"


class TestCreateInspectorFunction:
    """Test the create_inspector convenience function."""

    @pytest.mark.asyncio
    async def test_create_inspector_default(self):
        """Test create_inspector with default arguments."""
        client = await create_inspector()
        assert isinstance(client, Inspector)
        assert client.connection_type == "stdio"

    @pytest.mark.asyncio
    async def test_create_inspector_stdio(self):
        """Test create_inspector with stdio type."""
        client = await create_inspector(connection_type="stdio")
        assert isinstance(client, Inspector)
        assert client.connection_type == "stdio"

    @pytest.mark.asyncio
    async def test_create_inspector_http(self):
        """Test create_inspector with HTTP type."""
        client = await create_inspector(connection_type="http")
        assert isinstance(client, Inspector)
        assert client.connection_type == "http"
