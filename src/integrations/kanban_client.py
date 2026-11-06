"""Simple MCP Kanban Client for reliable task management.

This module provides a simplified client for interacting with the kanban-mcp server,
focusing on reliability and following proven patterns that work consistently.

The client handles:
- Task retrieval from kanban boards
- Task assignment to agents
- Board status monitoring
- Automatic configuration loading

Notes
-----
This implementation avoids persistent connections and creates a new MCP session
for each operation to ensure reliability.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from src.core.models import Priority, Task, TaskStatus

logger = logging.getLogger(__name__)


class KanbanClient:
    """
    Simple MCP Kanban client that follows proven patterns for reliability.

    This client creates a new MCP session for each operation rather than
    maintaining persistent connections, which has proven more reliable in practice.

    Attributes
    ----------
    board_id : Optional[str]
        ID of the kanban board to work with
    project_id : Optional[str]
        ID of the project associated with the board

    Examples
    --------
    >>> client = KanbanClient()
    >>> tasks = await client.get_available_tasks()
    >>> for task in tasks:
    ...     print(f"Task: {task.name} - Priority: {task.priority.value}")

    Notes
    -----
    Planka credentials are loaded from environment variables or set to defaults.
    Board and project IDs are loaded from config_marcus.json if available.
    """

    def __init__(self) -> None:
        """
        Initialize the Simple MCP Kanban Client.

        Loads configuration from config_marcus.json and sets up
        Planka environment variables. Config file takes precedence.
        """
        # Initialize attributes
        self.board_id: Optional[str] = None
        self.project_id: Optional[str] = None

        # Defer kanban-mcp path detection to first use. Resolving eagerly
        # blocked startup for non-Planka providers (SQLite, GitHub, Linear)
        # that never call the subprocess. _kanban_mcp_path is resolved on
        # first access via the kanban_mcp_path property.
        self._kanban_mcp_path: Optional[str] = None

        # Load config first - this may set environment variables
        self._load_config()

        # If config didn't have IDs, try loading from environment variables
        # (set by Planka provider when switching projects)
        if self.project_id is None:
            self.project_id = os.environ.get("PLANKA_PROJECT_ID")
        if self.board_id is None:
            self.board_id = os.environ.get("PLANKA_BOARD_ID")

        # If still not set, try loading from workspace state
        workspace_state = None
        if self.project_id is None:
            workspace_state = self._load_workspace_state()
            if workspace_state:
                self.project_id = workspace_state.get("project_id")
        if self.board_id is None:
            if workspace_state is None:
                workspace_state = self._load_workspace_state()
            if workspace_state:
                self.board_id = workspace_state.get("board_id")

        if workspace_state:
            logger.info(
                "Loaded project_id and board_id from workspace state: "
                f"project={self.project_id}, board={self.board_id}"
            )

        # Set environment for Planka from .env or use defaults
        # (only if not already set by config)
        if "PLANKA_BASE_URL" not in os.environ:
            os.environ["PLANKA_BASE_URL"] = "http://localhost:3333"
        if "PLANKA_AGENT_EMAIL" not in os.environ:
            os.environ["PLANKA_AGENT_EMAIL"] = "demo@demo.demo"
        if "PLANKA_AGENT_PASSWORD" not in os.environ:
            os.environ["PLANKA_AGENT_PASSWORD"] = "demo"  # nosec B105

    def _is_running_in_docker(self) -> bool:
        """
        Detect if Marcus is running inside a Docker container.

        Returns
        -------
        bool
            True if running in Docker, False otherwise

        Notes
        -----
        Checks for common Docker environment indicators:
        - /.dockerenv file exists
        - Running on Alpine Linux (common in Docker)
        - Container-specific cgroup entries
        """
        from pathlib import Path

        # Check 1: /.dockerenv file (most reliable)
        if Path("/.dockerenv").exists():
            return True

        # Check 2: Check cgroup for docker/containerd
        try:
            if Path("/proc/1/cgroup").exists():
                with open("/proc/1/cgroup", "r") as f:
                    content = f.read()
                    if "docker" in content or "containerd" in content:
                        return True
        except Exception:
            pass  # nosec B110 - Intentional fallback for environment detection

        # Check 3: Check if hostname is a container ID (12 char hex)
        try:
            import socket

            hostname = socket.gethostname()
            # Docker container hostnames are typically 12 character hex strings
            if len(hostname) == 12 and all(c in "0123456789abcdef" for c in hostname):
                return True
        except Exception:
            pass  # nosec B110 - Intentional fallback for environment detection

        return False

    def _adjust_planka_url_for_environment(self, base_url: str) -> str:
        """
        Adjust Planka base URL based on environment (Docker vs local).

        If running in Docker, use Docker service names (planka:1337).
        If running locally, convert to localhost.

        Parameters
        ----------
        base_url : str
            Base URL from config (e.g., "http://planka:1337")

        Returns
        -------
        str
            Adjusted URL appropriate for current environment

        Examples
        --------
        >>> # In Docker
        >>> client._adjust_planka_url_for_environment("http://planka:1337")
        'http://planka:1337'

        >>> # Locally
        >>> client._adjust_planka_url_for_environment("http://planka:1337")
        'http://localhost:3333'
        """
        in_docker = self._is_running_in_docker()

        # If in Docker, keep the URL as-is (use service names)
        if in_docker:
            logger.info(f"Running in Docker - using Planka URL as-is: {base_url}")
            return base_url

        # If local, convert Docker service names to localhost
        # Handle common patterns:
        # - http://planka:1337 -> http://localhost:3333
        # - http://planka -> http://localhost:3333
        if "planka:" in base_url or base_url.endswith("planka"):
            # Extract protocol
            if base_url.startswith("https://"):
                local_url = "https://localhost:3333"
            else:
                local_url = "http://localhost:3333"
            logger.info(
                f"Running locally - converted Planka URL: {base_url} -> {local_url}"
            )
            return local_url

        # If it's already localhost or an IP, keep it
        logger.info(f"Using Planka URL from config: {base_url}")
        return base_url

    @property
    def kanban_mcp_path(self) -> str:
        """Lazily resolve and cache the kanban-mcp executable path.

        Raises
        ------
        FileNotFoundError
            If kanban-mcp cannot be found. Raised on first Planka call,
            not at construction time, so non-Planka providers are unaffected.
        """
        if self._kanban_mcp_path is None:
            self._kanban_mcp_path = self._get_kanban_mcp_path()
        return self._kanban_mcp_path

    def _get_kanban_mcp_path(self) -> str:
        """
        Automatically detect kanban-mcp path.

        Checks in priority order:
        1. KANBAN_MCP_PATH environment variable (user override)
        2. /app/kanban-mcp/dist/index.js (Docker)
        3. ../kanban-mcp/dist/index.js (sibling directory for local dev)

        Returns
        -------
        str
            Path to kanban-mcp index.js

        Raises
        ------
        FileNotFoundError
            If kanban-mcp cannot be found in any expected location
        """
        from pathlib import Path

        # 1. Check environment variable (highest priority)
        if env_path := os.getenv("KANBAN_MCP_PATH"):
            # Expand ~ and environment variables
            env_path_obj = Path(env_path).expanduser()
            if env_path_obj.exists():
                logger.info(
                    f"Using kanban-mcp from KANBAN_MCP_PATH: " f"{env_path_obj}"
                )
                return str(env_path_obj)
            else:
                logger.warning(
                    f"KANBAN_MCP_PATH set to {env_path} but file "
                    f"doesn't exist at {env_path_obj}"
                )

        # 2. Check Docker path
        docker_path = Path("/app/kanban-mcp/dist/index.js")
        if docker_path.exists():
            logger.info(f"Using kanban-mcp from Docker: {docker_path}")
            return str(docker_path)

        # 3. Check sibling directory (../kanban-mcp relative to marcus/)
        # Path(__file__) -> kanban_client.py
        # .parent -> integrations/
        # .parent -> src/
        # .parent -> marcus/
        # .parent -> parent directory containing marcus/
        marcus_root = Path(__file__).parent.parent.parent
        sibling_path = marcus_root.parent / "kanban-mcp" / "dist" / "index.js"
        if sibling_path.exists():
            logger.info(f"Using kanban-mcp from sibling directory: {sibling_path}")
            return str(sibling_path)

        # 4. Give up with helpful message
        raise FileNotFoundError(
            "Could not find kanban-mcp. Please either:\n"
            "  1. Set KANBAN_MCP_PATH environment variable to point to "
            "kanban-mcp/dist/index.js\n"
            f"  2. Clone kanban-mcp as sibling directory: "
            f"{marcus_root.parent}/kanban-mcp\n"
            "  3. Run in Docker where it's at /app/kanban-mcp\n"
            f"\nSearched in:\n"
            f"  - KANBAN_MCP_PATH env var\n"
            f"  - {docker_path}\n"
            f"  - {sibling_path}"
        )

    def _load_config(self) -> None:
        """
        Load configuration from config_marcus.json file.

        Reads project_id, board_id, and Planka credentials from the
        configuration file if it exists. Prints confirmation message
        to stderr for debugging.

        Notes
        -----
        The config file is searched in multiple locations:
        1. Current working directory
        2. Project root (relative to this file)
        """
        # Try multiple locations for config file
        from pathlib import Path

        config_paths = [
            Path("config_marcus.json"),  # Current directory
            # Project root
            Path(__file__).parent.parent.parent / "config_marcus.json",
        ]

        config_path = None
        for path in config_paths:
            if path.exists():
                config_path = path
                break

        if config_path:
            with open(config_path, "r") as f:
                config = json.load(f)
                self.project_id = config.get("project_id")
                self.board_id = config.get("board_id")

                # Load Planka credentials from config if available
                planka_config = config.get("planka", {})
                if planka_config.get("base_url"):
                    # Auto-adjust base_url based on environment
                    base_url = planka_config["base_url"]
                    base_url = self._adjust_planka_url_for_environment(base_url)
                    os.environ["PLANKA_BASE_URL"] = base_url
                if planka_config.get("email"):
                    os.environ["PLANKA_AGENT_EMAIL"] = planka_config["email"]
                if planka_config.get("password"):
                    os.environ["PLANKA_AGENT_PASSWORD"] = planka_config["password"]

                # Config loaded successfully
                # Don't print - interferes with MCP stdio
        else:
            print(
                "❌ config_marcus.json not found in any of these " "locations:",
                file=sys.stderr,
            )
            for path in config_paths:
                print(f"   - {path.absolute()}", file=sys.stderr)

    async def get_available_tasks(self) -> List[Task]:
        """
        Get all unassigned tasks from the kanban board.

        Retrieves tasks that are in "available" states (TODO, BACKLOG, READY)
        and have not been assigned to any agent.

        Returns
        -------
        List[Task]
            List of unassigned tasks sorted by priority

        Raises
        ------
        RuntimeError
            If board_id is not set in configuration

        Examples
        --------
        >>> client = KanbanClient()
        >>> tasks = await client.get_available_tasks()
        >>> print(f"Found {len(tasks)} available tasks")

        Notes
        -----
        This method creates a new MCP session for the operation.
        Tasks are filtered based on their list name (TODO, BACKLOG, etc.)
        and whether they have an assigned_to field.
        """
        if not self.board_id:
            raise RuntimeError("Board ID not set")

        # Use the exact same pattern as working scripts
        server_params = StdioServerParameters(
            command="node",
            args=[self.kanban_mcp_path],
            env=os.environ.copy(),
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # First get all lists for the board
                lists_result = await session.call_tool(
                    "mcp_kanban_list_manager",
                    {"action": "get_all", "boardId": self.board_id},
                )

                all_cards = []
                if (
                    lists_result
                    and hasattr(lists_result, "content")
                    and lists_result.content
                ):
                    first_content = cast(TextContent, lists_result.content[0])
                    lists_data = json.loads(first_content.text)
                    lists = (
                        lists_data
                        if isinstance(lists_data, list)
                        else lists_data.get("items", [])
                    )

                    # Get cards from each list
                    for lst in lists:
                        list_id = lst.get("id")
                        if list_id:
                            # Get cards for this list
                            cards_result = await session.call_tool(
                                "mcp_kanban_card_manager",
                                {"action": "get_all", "listId": list_id},
                            )

                            if (
                                cards_result
                                and hasattr(cards_result, "content")
                                and cards_result.content
                            ):
                                first_content = cast(
                                    TextContent, cards_result.content[0]
                                )
                                cards_text = first_content.text
                                if cards_text and cards_text.strip():
                                    cards_data = json.loads(cards_text)
                                    cards_list = (
                                        cards_data
                                        if isinstance(cards_data, list)
                                        else cards_data.get("items", [])
                                    )
                                    # Add list name to each card
                                    for card in cards_list:
                                        card["listName"] = lst.get("name", "")
                                        all_cards.append(card)

                # Fetch detailed card information including labels
                # NOTE: Planka's list card API doesn't include labels, use get_details
                try:
                    for card in all_cards:
                        card_id = card.get("id")
                        if not card_id:
                            continue

                        # Get full card details which includes labels
                        details_result = await session.call_tool(
                            "mcp_kanban_card_manager",
                            {"action": "get_details", "cardId": card_id},
                        )

                        if (
                            details_result
                            and hasattr(details_result, "content")
                            and details_result.content
                        ):
                            first_content = cast(TextContent, details_result.content[0])
                            card_details = json.loads(first_content.text)

                            # Update card with label data from details
                            # kanban-mcp now returns FILTERED labels
                            # (as of fix in feature/fix-card-label-filtering)
                            # No need to filter again, use labels directly
                            if "labels" in card_details:
                                card["labels"] = card_details["labels"]

                except Exception as e:
                    logger.error(
                        f"Failed to fetch card details for labels: {e}",
                        exc_info=True,
                    )
                    # Continue without labels rather than failing entirely

                # First, convert ALL cards to tasks to build complete ID mapping
                all_tasks = []
                for card in all_cards:
                    task = self._card_to_task(card)
                    all_tasks.append(task)

                # Build mapping of original IDs to new IDs from ALL tasks
                # This ensures completed/assigned tasks can still be
                # resolved as dependencies
                id_mapping = {}
                for task in all_tasks:
                    if hasattr(task, "_original_id") and task._original_id:
                        id_mapping[task._original_id] = task.id

                # Resolve dependencies using the complete mapping
                if id_mapping:
                    logger.debug(
                        f"Resolving dependencies with ID mapping: {id_mapping}"
                    )
                    for task in all_tasks:
                        if task.dependencies:
                            resolved_deps = []
                            for dep_id in task.dependencies:
                                if dep_id in id_mapping:
                                    # Dependency exists on the board - resolve it
                                    resolved_id = id_mapping[dep_id]
                                    logger.debug(
                                        f"Resolved dependency {dep_id} -> {resolved_id}"
                                    )
                                    resolved_deps.append(resolved_id)
                                else:
                                    # Dependency doesn't exist on the board
                                    # Check if it's already a board ID
                                    if dep_id in [t.id for t in all_tasks]:
                                        # It's a valid board ID, keep it
                                        resolved_deps.append(dep_id)
                                    else:
                                        # Orphaned dependency - skip it
                                        logger.warning(
                                            f"Skipping orphaned dependency "
                                            f"'{dep_id}' for task "
                                            f"'{task.name}'"
                                        )
                            task.dependencies = resolved_deps

                # Now filter for available tasks (after dependency resolution)
                tasks = []
                # Only include tasks in TODO status that aren't assigned
                for task in all_tasks:
                    if not task.assigned_to and task.status == TaskStatus.TODO:
                        tasks.append(task)

                return tasks

    async def get_all_tasks(self) -> List[Task]:
        """
        Get all tasks from the kanban board regardless of status or assignment.

        Retrieves tasks from all lists on the board, including assigned,
        unassigned, completed, and blocked tasks.

        Returns
        -------
        List[Task]
            List of all tasks on the board

        Raises
        ------
        RuntimeError
            If board_id is not set in configuration

        Examples
        --------
        >>> client = KanbanClient()
        >>> tasks = await client.get_all_tasks()
        >>> print(f"Total tasks on board: {len(tasks)}")

        Notes
        -----
        This method creates a new MCP session for the operation.
        Unlike get_available_tasks(), this includes tasks in all states
        and with any assignment status.
        """
        if not self.board_id:
            raise RuntimeError("Board ID not set")

        # Use the exact same pattern as working scripts
        server_params = StdioServerParameters(
            command="node",
            args=[self.kanban_mcp_path],
            env=os.environ.copy(),
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # First get all lists for the board
                lists_result = await session.call_tool(
                    "mcp_kanban_list_manager",
                    {"action": "get_all", "boardId": self.board_id},
                )

                all_cards = []
                if (
                    lists_result
                    and hasattr(lists_result, "content")
                    and lists_result.content
                ):
                    first_content = cast(TextContent, lists_result.content[0])
                    lists_data = json.loads(first_content.text)
                    lists = (
                        lists_data
                        if isinstance(lists_data, list)
                        else lists_data.get("items", [])
                    )

                    # Get cards from each list
                    for lst in lists:
                        list_id = lst.get("id")
                        if list_id:
                            # Get cards for this list
                            cards_result = await session.call_tool(
                                "mcp_kanban_card_manager",
                                {"action": "get_all", "listId": list_id},
                            )

                            if (
                                cards_result
                                and hasattr(cards_result, "content")
                                and cards_result.content
                            ):
                                first_content = cast(
                                    TextContent, cards_result.content[0]
                                )
                                cards_text = first_content.text
                                if cards_text and cards_text.strip():
                                    cards_data = json.loads(cards_text)
                                    cards_list = (
                                        cards_data
                                        if isinstance(cards_data, list)
                                        else cards_data.get("items", [])
                                    )
                                    # Add list name to each card
                                    for card in cards_list:
                                        card["listName"] = lst.get("name", "")
                                        all_cards.append(card)

                # Fetch detailed card information including labels
                # NOTE: Planka's list card API doesn't include labels, use get_details
                try:
                    for card in all_cards:
                        card_id = card.get("id")
                        if not card_id:
                            continue

                        # Get full card details which includes labels
                        details_result = await session.call_tool(
                            "mcp_kanban_card_manager",
                            {"action": "get_details", "cardId": card_id},
                        )

                        if (
                            details_result
                            and hasattr(details_result, "content")
                            and details_result.content
                        ):
                            first_content = cast(TextContent, details_result.content[0])
                            card_details = json.loads(first_content.text)

                            # Update card with label data from details
                            # kanban-mcp now returns FILTERED labels
                            # (as of fix in feature/fix-card-label-filtering)
                            # No need to filter again, use labels directly
                            if "labels" in card_details:
                                card["labels"] = card_details["labels"]

                except Exception as e:
                    logger.error(
                        f"Failed to fetch card details for labels: {e}",
                        exc_info=True,
                    )
                    # Continue without labels rather than failing entirely

                tasks = []

                # Convert all cards to tasks (no filtering)
                for card in all_cards:
                    task = self._card_to_task(card)
                    tasks.append(task)

                # Build mapping of original IDs to new IDs
                id_mapping = {}
                for task in tasks:
                    if hasattr(task, "_original_id") and task._original_id:
                        id_mapping[task._original_id] = task.id

                # Resolve dependencies using the mapping
                if id_mapping:
                    logger.debug(
                        f"Resolving dependencies with ID mapping: {id_mapping}"
                    )
                    for task in tasks:
                        if task.dependencies:
                            resolved_deps = []
                            for dep_id in task.dependencies:
                                if dep_id in id_mapping:
                                    # Dependency exists on the board - resolve it
                                    resolved_id = id_mapping[dep_id]
                                    logger.debug(
                                        f"Resolved dependency {dep_id} -> {resolved_id}"
                                    )
                                    resolved_deps.append(resolved_id)
                                else:
                                    # Dependency doesn't exist on the board
                                    # Check if it's already a board ID
                                    if dep_id in [t.id for t in tasks]:
                                        # It's a valid board ID, keep it
                                        resolved_deps.append(dep_id)
                                    else:
                                        # Orphaned dependency - skip it
                                        logger.warning(
                                            f"Skipping orphaned dependency "
                                            f"'{dep_id}' for task "
                                            f"'{task.name}'"
                                        )
                            task.dependencies = resolved_deps

                return tasks

        # If no lists were found or lists_result was empty, return empty list

    async def assign_task(self, task_id: str, agent_id: str) -> None:
        """
        Assign a task to an agent.

        This method:
        1. Adds a comment to the task indicating assignment
        2. Moves the task to the "In Progress" list

        Parameters
        ----------
        task_id : str
            ID of the task to assign
        agent_id : str
            ID of the agent receiving the assignment

        Examples
        --------
        >>> await client.assign_task("card-123", "agent-001")

        Notes
        -----
        The task is automatically moved to the first list containing
        "progress" in its name (case-insensitive).
        """
        server_params = StdioServerParameters(
            command="node",
            args=[self.kanban_mcp_path],
            env=os.environ.copy(),
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Add comment
                await session.call_tool(
                    "mcp_kanban_comment_manager",
                    {
                        "action": "create",
                        "cardId": task_id,
                        "text": (
                            f"📋 Task assigned to {agent_id} at "
                            f"{datetime.now(timezone.utc).isoformat()}"
                        ),
                    },
                )

                # Move to In Progress
                # First get lists
                lists_result = await session.call_tool(
                    "mcp_kanban_list_manager",
                    {"action": "get_all", "boardId": self.board_id},
                )

                if lists_result and hasattr(lists_result, "content"):
                    first_content = cast(TextContent, lists_result.content[0])
                    lists_data = json.loads(first_content.text)
                    lists = (
                        lists_data
                        if isinstance(lists_data, list)
                        else lists_data.get("items", [])
                    )

                    # Find In Progress list
                    in_progress_list = None
                    for lst in lists:
                        if "progress" in lst.get("name", "").lower():
                            in_progress_list = lst
                            break

                    if in_progress_list:
                        # Move card
                        await session.call_tool(
                            "mcp_kanban_card_manager",
                            {
                                "action": "move",
                                "id": task_id,
                                "listId": in_progress_list["id"],
                            },
                        )

    async def get_board_summary(self) -> Dict[str, Any]:
        """
        Get summary statistics for the kanban board.

        Returns
        -------
        Dict[str, Any]
            Board statistics including task counts, completion percentage,
            and other metrics provided by the kanban-mcp server

        Raises
        ------
        RuntimeError
            If board_id is not set in configuration

        Examples
        --------
        >>> summary = await client.get_board_summary()
        >>> print(f"Completion: {summary.get('completionPercentage', 0)}%")

        Notes
        -----
        The exact structure of the summary depends on the kanban-mcp
        implementation. Typically includes totalCards, completionPercentage,
        and counts by status.
        """
        if not self.board_id:
            raise RuntimeError("Board ID not set")

        server_params = StdioServerParameters(
            command="node",
            args=[self.kanban_mcp_path],
            env=os.environ.copy(),
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                result = await session.call_tool(
                    "mcp_kanban_project_board_manager",
                    {
                        "action": "get_board_summary",
                        "boardId": self.board_id,
                        "includeTaskDetails": False,
                    },
                )

                if result and hasattr(result, "content"):
                    first_content = cast(TextContent, result.content[0])
                    parsed_result = json.loads(first_content.text)
                    if isinstance(parsed_result, dict):
                        return parsed_result
                    else:
                        return {"data": parsed_result}

                return {}

    def _is_available_task(self, card: Dict[str, Any]) -> bool:
        """
        Check if a task is in an available state.

        Parameters
        ----------
        card : Dict[str, Any]
            Card data from the kanban board

        Returns
        -------
        bool
            True if the task is in an available state (TODO, BACKLOG, READY)

        Notes
        -----
        Available states are determined by the list name containing
        specific keywords (case-insensitive).
        """
        list_name = card.get("listName", "").upper()
        available_states = ["TODO", "TO DO", "BACKLOG", "READY"]
        return any(state in list_name for state in available_states)

    def _card_to_task(self, card: Dict[str, Any]) -> Task:
        """
        Convert a kanban card to a Task object.

        Parameters
        ----------
        card : Dict[str, Any]
            Card data from the kanban board containing fields like
            id, name/title, description, listName, etc.

        Returns
        -------
        Task
            Task object with data mapped from the card

        Examples
        --------
        >>> card = {"id": "123", "name": "Fix bug", "listName": "TODO"}
        >>> task = client._card_to_task(card)
        >>> print(task.status)  # TaskStatus.TODO

        Notes
        -----
        - Status is determined by the list name (DONE, PROGRESS, BLOCKED, TODO)
        - Priority defaults to MEDIUM if not specified
        - Dates default to current time if not provided
        - assigned_to is extracted from card users/assignment fields
        """
        task_name = card.get("name") or card.get("title", "")

        # Parse dates
        created_at = datetime.now(timezone.utc)
        updated_at = datetime.now(timezone.utc)

        # Determine status
        list_name = card.get("listName", "").upper()
        if "DONE" in list_name:
            status = TaskStatus.DONE
        elif "PROGRESS" in list_name:
            status = TaskStatus.IN_PROGRESS
        elif "DECLINED" in list_name:
            # ADR-0013 K5 / Codex P2 on PR #834: read-back symmetry with
            # the write side (Planka.move_task_to_column's "declined"
            # column). Only a board with such a list ever hits this --
            # one without it keeps reading TODO, same as today.
            status = TaskStatus.DECLINED
        elif "BLOCKED" in list_name or "ON HOLD" in list_name:
            status = TaskStatus.BLOCKED
        else:
            status = TaskStatus.TODO

        # Extract assignment information
        # Check for assignments in the card data structure
        assigned_to = None

        # Try different possible assignment fields
        if card.get("users"):  # Planka assigns users to cards
            users = card.get("users", [])
            if users and len(users) > 0:
                # Take the first assigned user as the assignee
                assigned_to = (
                    users[0].get("username")
                    or users[0].get("email")
                    or users[0].get("name")
                )
        elif card.get("assignedTo"):  # Alternative field name
            assigned_to = card.get("assignedTo")
        elif card.get("assigned_to"):  # Another alternative
            assigned_to = card.get("assigned_to")

        # Parse dependencies from description if they exist
        description = card.get("description", "")
        dependencies = self._parse_dependencies_from_description(description)
        original_id = self._parse_original_id_from_description(description)
        estimated_hours = self._parse_estimated_hours_from_description(description)

        # Parse labels from the card
        labels = []
        if card.get("labels"):
            for label in card.get("labels", []):
                if isinstance(label, dict) and label.get("name"):
                    labels.append(label["name"])
                elif isinstance(label, str):
                    labels.append(label)

        task = Task(
            id=card.get("id", ""),
            name=task_name,
            description=description,
            status=status,
            priority=Priority.MEDIUM,
            assigned_to=assigned_to,
            created_at=created_at,
            updated_at=updated_at,
            due_date=None,
            estimated_hours=estimated_hours,
            actual_hours=0.0,
            dependencies=dependencies,
            labels=labels,
        )

        # Store original ID as a custom attribute
        if original_id:
            setattr(task, "_original_id", original_id)

        return task

    def _parse_dependencies_from_description(self, description: str) -> List[str]:
        """
        Parse task dependencies from the description field.

        Dependencies are stored in the description as:
        🔗 Dependencies: task_id_1, task_id_2, task_id_3

        Parameters
        ----------
        description : str
            Task description that may contain dependencies

        Returns
        -------
        List[str]
            List of task IDs that this task depends on
        """
        if not description:
            return []

        import re

        # Look for the dependencies line
        pattern = r"🔗 Dependencies:\s*([^\n]+)"
        match = re.search(pattern, description)

        if match:
            deps_str = match.group(1)
            # Split by comma and clean up each dependency ID
            dependencies = [dep.strip() for dep in deps_str.split(",") if dep.strip()]
            return dependencies

        return []

    def _parse_original_id_from_description(self, description: str) -> Optional[str]:
        """
        Parse the original task ID from the description field.

        Original ID is stored in the description as:
        🏷️ Original ID: task_get_hello_design

        Parameters
        ----------
        description : str
            Task description that may contain original ID

        Returns
        -------
        Optional[str]
            Original task ID if found, None otherwise
        """
        if not description:
            return None

        import re

        # Look for the original ID line
        pattern = r"🏷️ Original ID:\s*([^\n]+)"
        match = re.search(pattern, description)

        if match:
            return match.group(1).strip()

        return None

    def _parse_estimated_hours_from_description(self, description: str) -> float:
        """
        Parse estimated hours from the description field.

        Estimated hours are stored in the description as:
        ⏱️ Estimated: 8 hours
        or
        ⏱️ Estimated: 16.5 hours

        Parameters
        ----------
        description : str
            Task description that may contain estimated hours

        Returns
        -------
        float
            Estimated hours if found, 0.0 otherwise
        """
        if not description:
            return 0.0

        import re

        # Look for the estimated hours line
        # Pattern matches: "⏱️ Estimated: 8 hours" or "⏱️ Estimated: 16.5 hours"
        pattern = r"⏱️ Estimated:\s*(\d+(?:\.\d+)?)\s*hours?"
        match = re.search(pattern, description)

        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return 0.0

        return 0.0

    async def add_comment(self, task_id: str, comment_text: str) -> None:
        """
        Add a comment to a task.

        Parameters
        ----------
        task_id : str
            ID of the task to comment on
        comment_text : str
            Text of the comment to add

        Examples
        --------
        >>> await client.add_comment("card-123", "Task completed successfully")

        Notes
        -----
        Comments are visible in the Planka UI and are timestamped automatically.
        """
        server_params = StdioServerParameters(
            command="node",
            args=[self.kanban_mcp_path],
            env=os.environ.copy(),
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                await session.call_tool(
                    "mcp_kanban_comment_manager",
                    {"action": "create", "cardId": task_id, "text": comment_text},
                )

    async def complete_task(self, task_id: str) -> None:
        """
        Mark a task as completed by moving it to the Done list.

        Parameters
        ----------
        task_id : str
            ID of the task to complete

        Examples
        --------
        >>> await client.complete_task("card-123")

        Notes
        -----
        The task is moved to the first list containing "done" or "completed"
        in its name (case-insensitive).
        """
        await self._move_task_to_list(task_id, ["done", "completed"])

    async def update_task_status(self, task_id: str, status: str) -> None:
        """
        Update a task's status by moving it to the appropriate list.

        Parameters
        ----------
        task_id : str
            ID of the task to update
        status : str
            New status (e.g., "blocked", "in_progress", "todo")

        Examples
        --------
        >>> await client.update_task_status("card-123", "blocked")

        Notes
        -----
        Status names are matched to list names containing the status keyword.
        For example, "blocked" matches any list with "blocked" in the name.
        """
        status_to_keywords = {
            "blocked": ["on hold", "blocked"],  # Try "on hold" first, then "blocked"
            "in_progress": ["progress", "in progress"],
            "todo": ["todo", "to do", "backlog"],
            "done": ["done", "completed"],
        }

        keywords = status_to_keywords.get(status.lower(), [status.lower()])
        await self._move_task_to_list(task_id, keywords)

    async def _move_task_to_list(self, task_id: str, list_keywords: List[str]) -> None:
        """
        Move a task to a list matching one of the keywords.

        Parameters
        ----------
        task_id : str
            ID of the task to move
        list_keywords : List[str]
            Keywords to match against list names (case-insensitive)

        Raises
        ------
        RuntimeError
            If no matching list is found

        Notes
        -----
        This is a helper method used by other status update methods.
        """
        server_params = StdioServerParameters(
            command="node",
            args=[self.kanban_mcp_path],
            env=os.environ.copy(),
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Get all lists
                lists_result = await session.call_tool(
                    "mcp_kanban_list_manager",
                    {"action": "get_all", "boardId": self.board_id},
                )

                if lists_result and hasattr(lists_result, "content"):
                    first_content = cast(TextContent, lists_result.content[0])
                    lists_data = json.loads(first_content.text)
                    lists = (
                        lists_data
                        if isinstance(lists_data, list)
                        else lists_data.get("items", [])
                    )

                    # Find matching list
                    target_list = None
                    for lst in lists:
                        list_name_lower = lst.get("name", "").lower()
                        for keyword in list_keywords:
                            if keyword in list_name_lower:
                                target_list = lst
                                break
                        if target_list:
                            break

                    if target_list:
                        # Move card
                        await session.call_tool(
                            "mcp_kanban_card_manager",
                            {
                                "action": "move",
                                "id": task_id,
                                "listId": target_list["id"],
                                "position": 65535,  # Default position at end of list
                            },
                        )
                    else:
                        raise RuntimeError(
                            f"No list found matching keywords: {list_keywords}"
                        )

    async def auto_setup_project(
        self,
        project_name: str,
        board_name: str = "Main Board",
        project_root: str | None = None,
    ) -> Dict[str, str]:
        """
        Automatically create a Planka project and board if they don't exist.

        This method will:
        1. Create a new project in Planka
        2. Create a new board in that project with default lists/labels
        3. Save the IDs to .marcus_workspace.json
        4. Load the IDs into memory

        Parameters
        ----------
        project_name : str
            Name of the project to create
        board_name : str
            Name of the board to create (default: "Main Board")
        project_root : str | None, optional
            Absolute path to project implementation directory

        Returns
        -------
        Dict[str, str]
            Dictionary with project_id and board_id

        Examples
        --------
        >>> client = KanbanClient()
        >>> result = await client.auto_setup_project("My Project")
        >>> print(f"Project ID: {result['project_id']}")
        >>> print(f"Board ID: {result['board_id']}")
        """
        server_params = StdioServerParameters(
            command="node",
            args=[self.kanban_mcp_path],
            env=os.environ.copy(),
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Create project
                project_result = await session.call_tool(
                    "mcp_kanban_project_board_manager",
                    {"action": "create_project", "name": project_name},
                )

                if not project_result or not hasattr(project_result, "content"):
                    raise RuntimeError("Failed to create project")

                first_content = cast(TextContent, project_result.content[0])
                project_data = json.loads(first_content.text)
                project_id = project_data.get("id")

                if not project_id:
                    raise RuntimeError("Project created but no ID returned")

                # Create board with default position
                board_result = await session.call_tool(
                    "mcp_kanban_project_board_manager",
                    {
                        "action": "create_board",
                        "projectId": project_id,
                        "name": board_name,
                        "position": 65535,
                    },
                )

                if not board_result or not hasattr(board_result, "content"):
                    raise RuntimeError("Failed to create board")

                first_content = cast(TextContent, board_result.content[0])
                board_data = json.loads(first_content.text)
                board_id = board_data.get("id")

                if not board_id:
                    raise RuntimeError("Board created but no ID returned")

                # Save to workspace file
                self._save_workspace_state(
                    project_id=project_id,
                    board_id=board_id,
                    project_name=project_name,
                    board_name=board_name,
                    project_root=project_root,
                )

                # Update instance variables
                self.project_id = project_id
                self.board_id = board_id

                return {"project_id": project_id, "board_id": board_id}

    async def get_projects(self) -> List[Dict[str, Any]]:
        """
        Get all projects from Planka.

        Returns
        -------
        List[Dict[str, Any]]
            List of projects with their details including:
            - id: Project ID
            - name: Project name
            - boards: List of boards in the project

        Examples
        --------
        >>> client = KanbanClient()
        >>> projects = await client.get_projects()
        >>> for project in projects:
        ...     print(f"Project: {project['name']} (ID: {project['id']})")

        Notes
        -----
        This method creates a new MCP session for the operation.
        Useful for discovering existing projects in Planka.
        """
        server_params = StdioServerParameters(
            command="node",
            args=[self.kanban_mcp_path],
            env=os.environ.copy(),
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Get all projects (with pagination)
                result = await session.call_tool(
                    "mcp_kanban_project_board_manager",
                    {"action": "get_projects", "page": 1, "perPage": 100},
                )

                if result and hasattr(result, "content") and result.content:
                    first_content = cast(TextContent, result.content[0])
                    if not first_content.text or not first_content.text.strip():
                        return []  # No projects found — empty board
                    projects_data = json.loads(first_content.text)

                    # Handle both list and dict responses
                    if isinstance(projects_data, list):
                        return cast(List[Dict[str, Any]], projects_data)
                    elif isinstance(projects_data, dict) and "items" in projects_data:
                        return cast(List[Dict[str, Any]], projects_data["items"])

                return []

    async def get_boards_for_project(self, project_id: str) -> List[Dict[str, Any]]:
        """
        Get all boards for a specific Planka project.

        Parameters
        ----------
        project_id : str
            The Planka project ID

        Returns
        -------
        List[Dict[str, Any]]
            List of boards for the project
        """
        server_params = StdioServerParameters(
            command="node",
            args=[self.kanban_mcp_path],
            env=os.environ.copy(),
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Get boards for project
                result = await session.call_tool(
                    "mcp_kanban_project_board_manager",
                    {"action": "get_boards", "projectId": project_id},
                )

                if result and hasattr(result, "content"):
                    first_content = cast(TextContent, result.content[0])
                    boards_data = json.loads(first_content.text)

                    # Handle both list and dict responses
                    if isinstance(boards_data, list):
                        return cast(List[Dict[str, Any]], boards_data)
                    elif isinstance(boards_data, dict) and "items" in boards_data:
                        return cast(List[Dict[str, Any]], boards_data["items"])

                return []

    def _save_workspace_state(
        self,
        project_id: str,
        board_id: str,
        project_name: str,
        board_name: str,
        project_root: str | None = None,
    ) -> None:
        """
        Save project and board IDs to workspace state file.

        Parameters
        ----------
        project_id : str
            The Planka project ID
        board_id : str
            The Planka board ID
        project_name : str
            Name of the project
        board_name : str
            Name of the board
        project_root : str | None, optional
            Absolute path to project implementation directory
        """
        from pathlib import Path

        workspace_file = Path(".marcus_workspace.json")

        workspace_data = {
            "project_id": project_id,
            "board_id": board_id,
            "project_name": project_name,
            "board_name": board_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        # Add project_root if provided
        if project_root:
            workspace_data["project_root"] = project_root

        with open(workspace_file, "w") as f:
            json.dump(workspace_data, f, indent=2)

        logger.info(f"Saved workspace state to {workspace_file.absolute()}")

    def _load_workspace_state(self) -> Optional[Dict[str, str]]:
        """
        Load project and board IDs from workspace state file.

        Returns
        -------
        Optional[Dict[str, str]]
            Dictionary with project_id, board_id, and project_root if available
        """
        from pathlib import Path

        workspace_file = Path(".marcus_workspace.json")

        if not workspace_file.exists():
            return None

        try:
            with open(workspace_file, "r") as f:
                data = json.load(f)
                result = {
                    "project_id": data.get("project_id"),
                    "board_id": data.get("board_id"),
                }
                # Include project_root if present
                if "project_root" in data:
                    result["project_root"] = data["project_root"]
                return result
        except Exception as e:
            logger.warning(f"Failed to load workspace state: {e}")
            return None
