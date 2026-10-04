"""
Inspector - Unified MCP Testing Client for Marcus Workflows.

This module provides a testing and development client that supports both stdio
and HTTP connections to Marcus MCP server. Inspector is designed for testing
Marcus functionality, debugging workflows, and validating MCP protocol integration.

Connection Types
----------------
- stdio: Spawns isolated Marcus instance for testing (recommended for development)
- http: Connects to running Marcus HTTP server (recommended for integration testing)

Classes
-------
Inspector
    Testing client supporting both stdio and HTTP connections

Examples
--------
Test Marcus workflow with stdio (isolated testing):

>>> import asyncio
>>> from src.worker.inspector import Inspector
>>>
>>> async def test_task_assignment():
...     client = Inspector(connection_type='stdio')
...     async with client.connect() as session:
...         # Simulate agent registration
...         result = await client.register_agent(
...             "test-agent",
...             "Test Agent",
...             "Developer",
...             ["python", "testing"]
...         )
...
...         # Verify task assignment works
...         task = await client.request_next_task("test-agent")
...         assert task is not None
>>>
>>> asyncio.run(test_task_assignment())

Test Marcus workflow with HTTP (integration testing):

>>> async def test_http_workflow():
...     client = Inspector(connection_type='http')
...     async with client.connect(url="http://localhost:4298/mcp") as session:
...         # Test against running Marcus instance
...         result = await client.register_agent(
...             "integration-test", "Test", "Developer", []
...         )
...         # Verify workflow
...         assert result.get('success')
>>>
>>> asyncio.run(test_http_workflow())

Notes
-----
- Inspector is for TESTING Marcus, not for production AI agents
- Use stdio for isolated unit/integration tests
- Use http for testing against running Marcus instances
- Inherits all MCP communication methods for workflow testing
"""

import asyncio
import json
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    TypeVar,
    cast,
)

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextContent

# Type variable for retry decorator
T = TypeVar("T")


def _extract_text_from_result(result: CallToolResult) -> str:
    """
    Extract text content from MCP tool call result.

    Parameters
    ----------
    result : CallToolResult
        The CallToolResult from MCP tool call

    Returns
    -------
    str
        The text content if available, empty string otherwise
    """
    if not result.content:
        return ""

    for content_item in result.content:
        # Check if it's a TextContent object or has a text attribute (for testing)
        if isinstance(content_item, TextContent) or hasattr(content_item, "text"):
            return str(content_item.text)

    return ""


def retry_with_backoff(
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """
    Decorate operations with exponential backoff retry logic.

    Parameters
    ----------
    max_attempts : int, optional
        Maximum number of retry attempts, by default 3
    initial_delay : float, optional
        Initial delay in seconds, by default 1.0
    max_delay : float, optional
        Maximum delay between retries, by default 60.0
    exponential_base : float, optional
        Base for exponential backoff, by default 2.0
    jitter : bool, optional
        Whether to add random jitter to delays, by default True

    Returns
    -------
    Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]
        Decorated function that retries on failure
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except (ConnectionError, RuntimeError, asyncio.TimeoutError) as e:
                    last_exception = e

                    if attempt == max_attempts - 1:
                        # Last attempt failed
                        print(
                            f"❌ {func.__name__} failed after "
                            f"{max_attempts} attempts: {e}"
                        )
                        raise

                    # Calculate delay with exponential backoff
                    delay = min(initial_delay * (exponential_base**attempt), max_delay)

                    # Add jitter if enabled
                    if jitter:
                        # Use cryptographically secure random for jitter
                        secure_random = secrets.SystemRandom()
                        delay = delay * (0.5 + secure_random.random())

                    print(
                        f"⚠️  {func.__name__} failed "
                        f"(attempt {attempt + 1}/{max_attempts}), "
                        f"retrying in {delay:.1f}s: {e}"
                    )

                    await asyncio.sleep(delay)

            # Should never reach here, but just in case
            raise last_exception or RuntimeError(f"{func.__name__} failed unexpectedly")

        return wrapper

    return decorator


class Inspector:
    """
    Unified Testing Client for Marcus MCP workflows.

    This client provides a testing interface for validating Marcus functionality
    via either stdio (isolated instance) or HTTP (shared server). It is designed
    for development, testing, and debugging Marcus workflows, NOT for production
    AI agent usage.

    Inspector supports the full MCP protocol for simulating agent behavior and
    validating that Marcus task assignment, progress tracking, and coordination
    systems work correctly.

    Attributes
    ----------
    session : Optional[ClientSession]
        The active MCP client session, None if not connected
    connection_type : Literal['stdio', 'http']
        The type of connection to use

    Methods
    -------
    connect(url: Optional[str] = None) -> AsyncIterator[ClientSession]
        Connect to Marcus using the configured connection type
    register_agent(agent_id, name, role, skills) -> Dict[str, Any]
        Simulate agent registration for testing
    request_next_task(agent_id) -> Dict[str, Any]
        Test task assignment workflow
    report_task_progress(agent_id, task_id, status, progress, message) -> Dict[str, Any]
        Test progress reporting workflow
    report_blocker(agent_id, task_id, description, severity) -> Dict[str, Any]
        Test blocker reporting workflow
    get_project_status() -> Dict[str, Any]
        Test project status retrieval
    get_agent_status(agent_id) -> Dict[str, Any]
        Test agent status retrieval

    Examples
    --------
    Test task assignment with stdio:

    >>> client = Inspector(connection_type='stdio')
    >>> async with client.connect() as session:
    ...     # Simulate agent workflow for testing
    ...     result = await client.register_agent("test-1", "Test", "Developer", [])
    ...     task = await client.request_next_task("test-1")
    ...     assert task is not None

    Test with HTTP connection:

    >>> client = Inspector(connection_type='http')
    >>> async with client.connect(url="http://localhost:4298/mcp") as session:
    ...     # Test against running Marcus instance
    ...     result = await client.register_agent("test-1", "Test", "Developer", [])
    ...     assert result.get('success')

    Notes
    -----
    - Inspector is for TESTING, not production agent usage
    - stdio: Best for isolated unit/integration tests
    - http: Best for testing against running Marcus instances
    - All methods simulate real agent behavior for validation
    """

    def __init__(self, connection_type: Literal["stdio", "http"] = "stdio") -> None:
        """
        Initialize the Inspector testing client.

        Parameters
        ----------
        connection_type : Literal['stdio', 'http'], default='stdio'
            The type of connection to use:
            - 'stdio': Spawn isolated Marcus instance (for testing)
            - 'http': Connect to running Marcus HTTP server (for integration)
        """
        self.session: Optional[ClientSession] = None
        self.connection_type = connection_type
        self._connection_attempts = 0
        self._last_connection_time = 0.0

    @asynccontextmanager
    async def connect(
        self,
        url: Optional[str] = None,
        timeout: float = 30,
        sse_read_timeout: float = 300,
    ) -> AsyncIterator[ClientSession]:
        """
        Connect to Marcus MCP server using the configured connection type.

        This method automatically uses the appropriate transport (stdio or HTTP)
        based on the connection_type specified during initialization.

        Parameters
        ----------
        url : str, optional
            The HTTP URL of the Marcus MCP server endpoint (required for http mode)
            Default: "http://localhost:4298/mcp"
            Ignored for stdio mode
        timeout : float, optional
            HTTP timeout for regular operations in seconds (http mode only)
            Default: 30
        sse_read_timeout : float, optional
            Timeout for SSE read operations in seconds (http mode only)
            Default: 300

        Yields
        ------
        ClientSession
            An active MCP client session for communicating with Marcus

        Raises
        ------
        RuntimeError
            If connection fails
        ValueError
            If http mode is selected but no URL is provided

        Examples
        --------
        Stdio connection:

        >>> client = Inspector(connection_type='stdio')
        >>> async with client.connect() as session:
        ...     await client.register_agent("test-1", "Test", "Developer", [])

        HTTP connection:

        >>> client = Inspector(connection_type='http')
        >>> async with client.connect(url="http://localhost:4298/mcp") as session:
        ...     await client.register_agent("test-1", "Test", "Developer", [])

        Notes
        -----
        - stdio: Spawns isolated Marcus instance for testing
        - http: Connects to running Marcus HTTP server using streamablehttp_client
        - Session cleanup is guaranteed even if exceptions occur
        """
        if self.connection_type == "stdio":
            # Stdio connection - spawn isolated Marcus instance
            project_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..")
            )
            server_cmd = [
                sys.executable,
                "-m",
                "src.marcus_mcp.server",
                "--stdio",  # Force stdio mode to avoid port conflicts
            ]

            # Inherit current environment and add PYTHONPATH
            env = os.environ.copy()
            env["PYTHONPATH"] = project_root

            server_params = StdioServerParameters(
                command=server_cmd[0], args=server_cmd[1:], env=env
            )

            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    self.session = session
                    await session.initialize()

                    # List available tools to verify connection
                    tools_response = await session.list_tools()
                    if hasattr(tools_response, "tools"):
                        tools = tools_response.tools
                    else:
                        tools = cast(List[Any], tools_response)
                    tool_names = [t.name for t in tools]
                    print(
                        f"Connected to Marcus (stdio). "
                        f"Available tools: {tool_names}"
                    )

                    yield session

        elif self.connection_type == "http":
            # HTTP connection - connect to running Marcus server
            if url is None:
                url = "http://localhost:4298/mcp"

            async with streamablehttp_client(
                url,
                timeout=timeout,
                sse_read_timeout=sse_read_timeout,
            ) as (read_stream, write_stream, get_session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    self.session = session
                    await session.initialize()

                    # Get session ID from the transport
                    session_id = get_session_id()
                    if session_id:
                        print(f"Connected to Marcus HTTP server at {url}")
                        print(f"Session ID: {session_id}")

                    # List available tools to verify connection
                    tools_response = await session.list_tools()
                    if hasattr(tools_response, "tools"):
                        tools = tools_response.tools
                    else:
                        tools = cast(List[Any], tools_response)
                    tool_names = [t.name for t in tools]
                    print(f"Available tools: {', '.join(tool_names)}")

                    yield session
        else:
            raise ValueError(
                f"Invalid connection_type: {self.connection_type}. "
                "Must be 'stdio' or 'http'"
            )

    def _should_attempt_reconnect(self) -> bool:
        """Check if we should attempt to reconnect based on recent attempts."""
        current_time = time.time()

        # Reset attempt counter if it's been more than 5 minutes
        if current_time - self._last_connection_time > 300:
            self._connection_attempts = 0

        # Allow up to 5 rapid reconnection attempts
        return self._connection_attempts < 5

    async def ensure_connected(self) -> bool:
        """Ensure we have an active connection, attempt reconnect if needed."""
        if self.session and not getattr(self.session, "_closed", True):
            return True

        if not self._should_attempt_reconnect():
            print("⚠️  Too many reconnection attempts, please wait before retrying")
            return False

        print("🔄 Attempting to reconnect to Marcus...")
        self._connection_attempts += 1
        self._last_connection_time = time.time()

        try:
            # Try to establish new connection
            async with self.connect() as session:
                self.session = session
                print("✅ Reconnected to Marcus successfully")
                self._connection_attempts = 0  # Reset on success
                return True
        except Exception as e:
            print(f"❌ Reconnection failed: {e}")
            return False

    async def register_agent(
        self,
        agent_id: str,
        name: str,
        role: str,
        skills: List[str],
        project_id: str = "",
        vendor: str = "",
        principal: str = "agent",
    ) -> Dict[str, Any]:
        """
        Simulate agent registration for testing Marcus workflows.

        This method tests the agent registration workflow by simulating an agent
        registering with Marcus. Use this to validate that Marcus correctly handles
        agent registration, stores agent information, and returns proper responses.

        Parameters
        ----------
        agent_id : str
            Unique identifier for the test agent (e.g., "test-agent-001")
        name : str
            Human-readable display name for the test agent
        role : str
            Agent's role for testing (e.g., "Developer", "QA Engineer")
        skills : List[str]
            List of skills to test skill-based task assignment
        project_id : str, default=""
            Project the worker registers into (required by the server
            since GH-388; empty keeps legacy callers compiling but the
            server will reject the registration).
        vendor : str, default=""
            Which vendor's model the worker runs on (issue #737);
            eligibility never offers a verify task to the claim
            author's vendor.
        principal : str, default="agent"
            "agent" or "human" (issue #737); an approve task is
            offered to and completable by a human principal only.

        Returns
        -------
        Dict[str, Any]
            Registration response containing:
            - success: bool indicating registration success
            - agent_id: str confirming the registered agent ID
            - message: str with registration status details

        Raises
        ------
        RuntimeError
            If no active connection exists to Marcus server

        Examples
        --------
        Test agent registration:

        >>> client = Inspector(connection_type='stdio')
        >>> async with client.connect() as session:
        ...     result = await client.register_agent(
        ...         "test-agent-1",
        ...         "Test Agent",
        ...         "Developer",
        ...         ["python", "testing"]
        ...     )
        ...     assert result.get('success') is True

        Notes
        -----
        - This simulates agent behavior for testing Marcus
        - Use to validate Marcus registration workflow
        - Not for production AI agent usage
        """
        if not self.session:
            raise RuntimeError("Not connected to Marcus")

        result = await self.session.call_tool(
            "register_agent",
            arguments={
                "agent_id": agent_id,
                "name": name,
                "role": role,
                "skills": skills,
                "project_id": project_id,
                "vendor": vendor,
                "principal": principal,
            },
        )

        text_content = _extract_text_from_result(result)
        return json.loads(text_content) if text_content else {}

    @retry_with_backoff(max_attempts=3, initial_delay=2.0)
    async def request_next_task(self, agent_id: str) -> Dict[str, Any]:
        """
        Test task assignment workflow by requesting next task.

        This method tests Marcus's task assignment logic by simulating an agent
        requesting work. Use this to validate that Marcus correctly assigns tasks,
        respects dependencies, and provides proper task information.

        Parameters
        ----------
        agent_id : str
            Unique identifier of the test agent requesting a task

        Returns
        -------
        Dict[str, Any]
            Task assignment response containing:
            - task: Dict[str, Any] or None with task details if assigned
            - message: str with status about task assignment

        Raises
        ------
        RuntimeError
            If no active connection exists to Marcus server

        Examples
        --------
        Test task assignment:

        >>> client = Inspector(connection_type='stdio')
        >>> async with client.connect() as session:
        ...     await client.register_agent("test-1", "Test", "Dev", [])
        ...     task = await client.request_next_task("test-1")
        ...     if task.get('task'):
        ...         print(f"Task assigned: {task['task']['id']}")

        Notes
        -----
        - Tests Marcus task assignment logic
        - Validates dependency resolution
        - Not for production AI agent usage
        """
        if not self.session:
            raise RuntimeError("Not connected to Marcus")

        result = await self.session.call_tool(
            "request_next_task", arguments={"agent_id": agent_id}
        )

        text_content = _extract_text_from_result(result)
        return json.loads(text_content) if text_content else {}

    @retry_with_backoff(max_attempts=3, initial_delay=1.0, max_delay=30.0)
    async def report_task_progress(
        self,
        agent_id: str,
        task_id: str,
        status: str,
        progress: int = 0,
        message: str = "",
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Test progress reporting workflow.

        This method tests Marcus's task progress tracking by simulating progress
        reports. Use this to validate that Marcus correctly updates task status,
        tracks completion, and handles status transitions.

        Parameters
        ----------
        agent_id : str
            Unique identifier of the test agent reporting progress
        task_id : str
            Unique identifier of the task being updated
        status : str
            Task status: "in_progress", "completed", "blocked", "paused"
        progress : int, optional
            Completion percentage from 0 to 100, by default 0
        message : str, optional
            Descriptive message about progress, by default ""
        evidence : Optional[Dict[str, Any]], optional
            Typed evidence payload (issue #737). Required by the
            server when completing a find, verify, or synthesize
            task; also carries an approval decision. Omitted from the
            call when None.

        Returns
        -------
        Dict[str, Any]
            Progress report response containing:
            - success: bool indicating if report was processed
            - task_id: str confirming the updated task
            - status: str confirming the new status

        Raises
        ------
        RuntimeError
            If no active connection exists to Marcus server

        Examples
        --------
        Test progress reporting:

        >>> async with client.connect() as session:
        ...     await client.report_task_progress(
        ...         "test-1", "task-123", "in_progress", 50,
        ...         "Halfway complete"
        ...     )
        ...     await client.report_task_progress(
        ...         "test-1", "task-123", "completed", 100,
        ...         "Task finished"
        ...     )

        Notes
        -----
        - Tests Marcus progress tracking
        - Validates status transitions
        - Not for production AI agent usage
        """
        if not self.session:
            raise RuntimeError("Not connected to Marcus")

        arguments: Dict[str, Any] = {
            "agent_id": agent_id,
            "task_id": task_id,
            "status": status,
            "progress": progress,
            "message": message,
        }
        if evidence is not None:
            arguments["evidence"] = evidence
        result = await self.session.call_tool(
            "report_task_progress",
            arguments=arguments,
        )

        text_content = _extract_text_from_result(result)
        return json.loads(text_content) if text_content else {}

    @retry_with_backoff(max_attempts=3, initial_delay=1.0, max_delay=30.0)
    async def report_blocker(
        self,
        agent_id: str,
        task_id: str,
        blocker_description: str,
        severity: str = "medium",
    ) -> Dict[str, Any]:
        """
        Test blocker reporting workflow.

        This method tests Marcus's blocker handling by simulating blocker reports.
        Use this to validate that Marcus correctly records blockers, generates
        AI suggestions, and handles escalation.

        Parameters
        ----------
        agent_id : str
            Unique identifier of the test agent reporting the blocker
        task_id : str
            Unique identifier of the blocked task
        blocker_description : str
            Detailed description of the blocking issue
        severity : str, optional
            Severity level: "low", "medium", "high", by default "medium"

        Returns
        -------
        Dict[str, Any]
            Blocker report response containing:
            - success: bool indicating if blocker was recorded
            - blocker_id: str unique identifier for this blocker
            - suggestions: List[str] AI-generated resolution suggestions

        Raises
        ------
        RuntimeError
            If no active connection exists to Marcus server

        Examples
        --------
        Test blocker reporting:

        >>> async with client.connect() as session:
        ...     result = await client.report_blocker(
        ...         "test-1", "task-123",
        ...         "Database connection failed",
        ...         "high"
        ...     )
        ...     assert 'suggestions' in result

        Notes
        -----
        - Tests Marcus blocker handling
        - Validates AI suggestion generation
        - Not for production AI agent usage
        """
        if not self.session:
            raise RuntimeError("Not connected to Marcus")

        result = await self.session.call_tool(
            "report_blocker",
            arguments={
                "agent_id": agent_id,
                "task_id": task_id,
                "blocker_description": blocker_description,
                "severity": severity,
            },
        )

        text_content = _extract_text_from_result(result)
        return json.loads(text_content) if text_content else {}

    async def get_project_status(self) -> Dict[str, Any]:
        """
        Test project status retrieval.

        This method tests Marcus's status reporting by fetching project metrics.
        Use this to validate that Marcus correctly tracks project state, calculates
        metrics, and provides comprehensive status information.

        Returns
        -------
        Dict[str, Any]
            Project status response containing:
            - project_info: Dict with project details
            - task_metrics: Dict with task statistics
            - agent_metrics: Dict with agent statistics
            - performance_metrics: Dict with performance data

        Raises
        ------
        RuntimeError
            If no active connection exists to Marcus server

        Examples
        --------
        Test status retrieval:

        >>> async with client.connect() as session:
        ...     status = await client.get_project_status()
        ...     assert 'task_metrics' in status
        ...     assert 'agent_metrics' in status

        Notes
        -----
        - Tests Marcus status reporting
        - Validates metric calculations
        - Not for production AI agent usage
        """
        if not self.session:
            raise RuntimeError("Not connected to Marcus")

        result = await self.session.call_tool("get_project_status", arguments={})

        text_content = _extract_text_from_result(result)
        return json.loads(text_content) if text_content else {}

    async def get_agent_status(self, agent_id: str) -> Dict[str, Any]:
        """
        Test agent status retrieval.

        This method tests Marcus's agent tracking by fetching agent-specific status.
        Use this to validate that Marcus correctly tracks agent state, assignments,
        and performance.

        Parameters
        ----------
        agent_id : str
            Unique identifier of the agent to query

        Returns
        -------
        Dict[str, Any]
            Agent status response containing agent details and metrics

        Raises
        ------
        RuntimeError
            If no active connection exists to Marcus server

        Examples
        --------
        Test agent status:

        >>> async with client.connect() as session:
        ...     await client.register_agent("test-1", "Test", "Dev", [])
        ...     status = await client.get_agent_status("test-1")
        ...     assert status.get('agent_id') == "test-1"

        Notes
        -----
        - Tests Marcus agent tracking
        - Validates agent state management
        - Not for production AI agent usage
        """
        if not self.session:
            raise RuntimeError("Not connected to Marcus")

        result = await self.session.call_tool(
            "get_agent_status", arguments={"agent_id": agent_id}
        )

        text_content = _extract_text_from_result(result)
        return json.loads(text_content) if text_content else {}


# Convenience function to maintain API compatibility
async def create_inspector(
    connection_type: Literal["stdio", "http"] = "stdio",
) -> Inspector:
    """
    Create and return a new Inspector testing client.

    Parameters
    ----------
    connection_type : Literal['stdio', 'http'], default='stdio'
        The type of connection to use:
        - 'stdio': Spawn isolated Marcus instance (recommended for testing)
        - 'http': Connect to running Marcus HTTP server (recommended for integration)

    Returns
    -------
    Inspector
        A new testing client instance ready for connection

    Examples
    --------
    >>> client = await create_inspector('stdio')
    >>> async with client.connect() as session:
    ...     # Test Marcus workflows
    ...     pass

    >>> client = await create_inspector('http')
    >>> async with client.connect(url="http://localhost:4298/mcp") as session:
    ...     # Test against running instance
    ...     pass
    """
    return Inspector(connection_type)
