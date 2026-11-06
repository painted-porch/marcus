"""
Workspace isolation and security management for autonomous agents.

This module ensures that client agents only operate within their designated
project directories and cannot access the Marcus installation or system files.

The WorkspaceManager provides:
- Automatic detection of Marcus installation directory
- Configuration-based workspace assignment
- Path validation and security enforcement
- Audit logging of security violations

Examples
--------
>>> manager = WorkspaceManager()
>>> workspace = manager.assign_agent_workspace("agent-001", "/home/user/project")
>>> manager.validate_path("/home/user/project/src/file.py")  # OK
>>> manager.validate_path("/usr/lib/python3/site-packages")  # Raises SecurityError
"""

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from src.config.config_path import resolve_config_candidates


class WorkspaceSecurityError(Exception):
    """
    Raised when a security violation is detected.

    This exception indicates an agent attempted to access a forbidden path.

    Examples
    --------
    >>> raise WorkspaceSecurityError("Access denied to system directory")
    """

    pass


@dataclass
class WorkspaceConfig:
    """
    Configuration for a single agent workspace.

    Parameters
    ----------
    workspace_id : str
        Unique identifier for the workspace
    path : str
        Filesystem path to the workspace directory
    agent_id : Optional[str], default=None
        ID of the agent assigned to this workspace
    is_readonly : bool, default=False
        Whether the workspace is read-only

    Notes
    -----
    Paths are automatically expanded and converted to absolute paths
    during initialization.
    """

    workspace_id: str
    path: str
    agent_id: Optional[str] = None
    is_readonly: bool = False

    def __post_init__(self) -> None:
        """
        Expand and validate the workspace path.

        Converts relative paths to absolute and expands user home directory.
        """
        self.path = os.path.abspath(os.path.expanduser(self.path))


@dataclass
class ProjectWorkspaces:
    """
    Configuration for all project workspaces.

    Parameters
    ----------
    main_workspace : str
        Primary project workspace path
    agent_workspaces : Dict[str, str], optional
        Mapping of agent IDs to their dedicated workspace paths

    Examples
    --------
    >>> config = ProjectWorkspaces(
    ...     main_workspace="/home/user/project",
    ...     agent_workspaces={"agent-001": "/home/user/project/agent1"}
    ... )
    """

    main_workspace: str
    agent_workspaces: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Expand all workspace paths to absolute paths."""
        self.main_workspace = os.path.abspath(os.path.expanduser(self.main_workspace))
        self.agent_workspaces = {
            agent_id: os.path.abspath(os.path.expanduser(path))
            for agent_id, path in self.agent_workspaces.items()
        }


class WorkspaceManager:
    """
    Manages workspace isolation and security boundaries for Marcus.

    This class enforces security by maintaining a list of forbidden paths
    (including the Marcus installation) and validating all agent workspace
    assignments against these restrictions.

    Attributes
    ----------
    marcus_root : str
        Automatically detected Marcus installation directory
    forbidden_paths : Set[str]
        Set of paths that agents are not allowed to access
    agent_workspaces : Dict[str, WorkspaceConfig]
        Mapping of agent IDs to their workspace configurations
    project_config : Optional[ProjectWorkspaces]
        Project-level workspace configuration

    Examples
    --------
    >>> manager = WorkspaceManager()
    >>> workspace = manager.assign_agent_workspace("agent-001")
    >>> print(workspace.path)  # /home/user/project
    >>> manager.validate_path("/etc/passwd")  # Raises WorkspaceSecurityError

    Notes
    -----
    The Marcus installation directory is automatically detected and added
    to the forbidden paths list to prevent agents from modifying Marcus itself.
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        """
        Initialize WorkspaceManager with automatic Marcus detection.

        Parameters
        ----------
        config_path : Optional[str], default=None
            Path to configuration file. If not provided, searches default locations.
        """
        # Auto-detect Marcus installation directory
        self.marcus_root: str = self._detect_marcus_root()

        # Initialize forbidden paths - Marcus directory is always forbidden
        self.forbidden_paths: Set[str] = {
            self.marcus_root,
        }

        # Add common Python installation paths to forbidden list
        self._add_system_paths_to_forbidden()

        # Workspace assignments: agent_id -> WorkspaceConfig
        self.agent_workspaces: Dict[str, WorkspaceConfig] = {}

        # Project workspace configuration
        self.project_config: Optional[ProjectWorkspaces] = None

        # Load configuration
        if config_path:
            self.load_config(config_path)
        else:
            # Try to load from default locations
            self._load_default_config()

    def _detect_marcus_root(self) -> str:
        """
        Automatically detect Marcus installation directory.

        Returns
        -------
        str
            Absolute path to Marcus root directory

        Notes
        -----
        This works by finding the root of the Marcus package structure
        relative to this file's location.
        """
        # Get the directory of this file
        current_file = os.path.abspath(__file__)

        # Navigate up to find the Marcus root
        # We're in src/core/workspace_manager.py, so go up 2 levels
        marcus_root = os.path.dirname(os.path.dirname(os.path.dirname(current_file)))

        # Validate that we found the correct directory
        expected_markers = ["src", "scripts", "config_marcus.json"]
        for marker in expected_markers:
            if not os.path.exists(os.path.join(marcus_root, marker)):
                # If marker is missing, we might be in a different structure
                # Try alternative detection methods
                break

        return marcus_root

    def _add_system_paths_to_forbidden(self) -> None:
        """
        Add system Python paths to forbidden list.

        This prevents agents from modifying Python installations or
        system libraries.
        """
        forbidden_prefixes = [
            "/usr/lib/python",
            "/usr/local/lib/python",
            "/opt/homebrew/lib/python",
            "/System/Library",
            os.path.dirname(os.__file__),  # Python installation
        ]

        for prefix in forbidden_prefixes:
            if os.path.exists(prefix):
                self.forbidden_paths.add(os.path.abspath(prefix))

    def _load_default_config(self) -> None:
        """
        Try to load configuration from default locations.

        Searches in order:
        1. ``MARCUS_CONFIG`` environment variable, if set (highest priority)
        2. XDG config directory (~/.config/marcus/config.json)
        3. Local config in Marcus root

        Notes
        -----
        Issue #891: this used to check ``MARCUS_CONFIG`` LAST, after
        the XDG and Marcus-root locations -- the opposite priority of
        every other config-loading path in this codebase
        (``MarcusConfig.get_config()``, the legacy ``ConfigLoader``,
        ``KanbanClient._load_config``). An explicit override could
        lose to an ambient repo-root file here even when both were
        set.
        """
        # resolve_config_candidates puts MARCUS_CONFIG first when set
        # (issue #891); the rest of this list is this loader's own
        # fallback order.
        config_locations = [
            str(p)
            for p in resolve_config_candidates(
                os.path.expanduser("~/.config/marcus/config.json"),
                os.path.join(self.marcus_root, "config_marcus.json"),
            )
        ]

        for location in config_locations:
            if os.path.exists(location):
                try:
                    self.load_config(location)
                    print(f"Loaded workspace config from: {location}", file=sys.stderr)
                    return
                except Exception as e:
                    print(
                        f"Failed to load config from {location}: {e}", file=sys.stderr
                    )
                    continue

    def load_config(self, config_path: str) -> None:
        """
        Load workspace configuration from file.

        Parameters
        ----------
        config_path : str
            Path to JSON configuration file

        Raises
        ------
        FileNotFoundError
            If configuration file doesn't exist
        WorkspaceSecurityError
            If configured workspaces overlap with forbidden paths

        Notes
        -----
        Configuration format::

            {
                "project": {
                    "workspaces": {
                        "main": "/path/to/project",
                        "agents": {
                            "agent-001": "/path/to/agent1/workspace"
                        }
                    }
                },
                "security": {
                    "additional_forbidden_paths": ["/sensitive/data"]
                }
            }
        """
        config_path = os.path.expanduser(config_path)

        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Configuration file not found: {config_path}")

        with open(config_path, "r") as f:
            config = json.load(f)

        # Load project workspace configuration
        if "project" in config and "workspaces" in config["project"]:
            workspaces = config["project"]["workspaces"]
            self.project_config = ProjectWorkspaces(
                main_workspace=workspaces.get("main", ""),
                agent_workspaces=workspaces.get("agents", {}),
            )

            # Validate that project workspaces don't overlap with forbidden paths
            self._validate_project_workspaces()

        # Load additional forbidden paths if specified
        if "security" in config and "additional_forbidden_paths" in config["security"]:
            for path in config["security"]["additional_forbidden_paths"]:
                self.add_forbidden_path(path)

    def _validate_project_workspaces(self) -> None:
        """
        Ensure project workspaces don't overlap with forbidden paths.

        Raises
        ------
        WorkspaceSecurityError
            If any workspace overlaps with a forbidden path
        """
        if not self.project_config:
            return

        all_workspaces = [self.project_config.main_workspace]
        all_workspaces.extend(self.project_config.agent_workspaces.values())

        for workspace in all_workspaces:
            for forbidden in self.forbidden_paths:
                if workspace.startswith(forbidden) or forbidden.startswith(workspace):
                    raise WorkspaceSecurityError(
                        f"Workspace {workspace} overlaps with "
                        f"forbidden path {forbidden}"
                    )

    def assign_agent_workspace(
        self, agent_id: str, workspace_path: Optional[str] = None
    ) -> WorkspaceConfig:
        """
        Assign a workspace to an agent.

        Parameters
        ----------
        agent_id : str
            Unique identifier for the agent
        workspace_path : Optional[str], default=None
            Explicit workspace path. If not provided, uses project configuration.

        Returns
        -------
        WorkspaceConfig
            The assigned workspace configuration

        Raises
        ------
        ValueError
            If no workspace is available for the agent
        WorkspaceSecurityError
            If the workspace path is forbidden

        Examples
        --------
        >>> workspace = manager.assign_agent_workspace(
        ...     "agent-001", "/home/user/project"
        ... )
        >>> print(workspace.workspace_id)  # "agent-001_workspace"

        Notes
        -----
        If workspace_path is not provided, the method tries:
        1. Pre-configured workspace for the specific agent
        2. The main project workspace as fallback
        """
        if workspace_path:
            # Explicit workspace assignment
            workspace = WorkspaceConfig(
                workspace_id=f"{agent_id}_workspace",
                path=workspace_path,
                agent_id=agent_id,
            )
        elif self.project_config and agent_id in self.project_config.agent_workspaces:
            # Use pre-configured workspace
            workspace = WorkspaceConfig(
                workspace_id=f"{agent_id}_workspace",
                path=self.project_config.agent_workspaces[agent_id],
                agent_id=agent_id,
            )
        elif self.project_config:
            # Fallback to main workspace
            workspace = WorkspaceConfig(
                workspace_id=f"{agent_id}_workspace",
                path=self.project_config.main_workspace,
                agent_id=agent_id,
            )
        else:
            raise ValueError(f"No workspace available for agent {agent_id}")

        # Validate the workspace
        self.validate_path(workspace.path)

        # Store the assignment
        self.agent_workspaces[agent_id] = workspace

        return workspace

    def get_agent_workspace(self, agent_id: str) -> Optional[WorkspaceConfig]:
        """
        Get the assigned workspace for an agent.

        Parameters
        ----------
        agent_id : str
            The agent's unique identifier

        Returns
        -------
        Optional[WorkspaceConfig]
            The workspace configuration if assigned, None otherwise
        """
        return self.agent_workspaces.get(agent_id)

    def validate_path(self, path: str) -> str:
        """
        Validate that a path is allowed (not in forbidden paths).

        Parameters
        ----------
        path : str
            Path to validate (can be relative or contain ~)

        Returns
        -------
        str
            The absolute path if valid

        Raises
        ------
        WorkspaceSecurityError
            If the path is within a forbidden directory

        Examples
        --------
        >>> manager.validate_path("/home/user/project/file.py")  # OK
        >>> manager.validate_path("~/project/file.py")  # Expands and validates
        >>> manager.validate_path("/usr/lib/python3/os.py")  # Raises error
        """
        absolute_path = os.path.abspath(os.path.expanduser(path))

        # Check against all forbidden paths
        for forbidden in self.forbidden_paths:
            if absolute_path.startswith(forbidden):
                raise WorkspaceSecurityError(
                    f"Access denied: {path} is within forbidden path {forbidden}"
                )

        return absolute_path

    def is_path_allowed(self, path: str) -> bool:
        """
        Check if a path is allowed without raising an exception.

        Parameters
        ----------
        path : str
            Path to check

        Returns
        -------
        bool
            True if path is allowed, False if forbidden

        Examples
        --------
        >>> if manager.is_path_allowed("/home/user/file.py"):
        ...     print("Path is safe to access")
        """
        try:
            self.validate_path(path)
            return True
        except WorkspaceSecurityError:
            return False

    def add_forbidden_path(self, path: str) -> None:
        """
        Add a path to the forbidden list.

        Parameters
        ----------
        path : str
            Path to mark as forbidden

        Examples
        --------
        >>> manager.add_forbidden_path("/sensitive/data")
        >>> manager.is_path_allowed("/sensitive/data/file.txt")  # False
        """
        absolute_path = os.path.abspath(os.path.expanduser(path))
        self.forbidden_paths.add(absolute_path)

    def get_forbidden_paths(self) -> List[str]:
        """
        Get list of all forbidden paths.

        Returns
        -------
        List[str]
            Sorted list of forbidden paths
        """
        return sorted(list(self.forbidden_paths))

    def get_task_assignment_data(self, agent_id: str) -> Dict[str, Any]:
        """
        Get workspace data to include in task assignments.

        This data should be sent to agents so they know where to work
        and what paths to avoid.

        Parameters
        ----------
        agent_id : str
            The agent's identifier

        Returns
        -------
        Dict[str, Any]
            Dictionary containing:
            - workspace_path: Where the agent should work
            - workspace_id: Unique workspace identifier
            - forbidden_paths: List of paths to avoid
            - is_readonly: Whether the workspace is read-only

        Examples
        --------
        >>> data = manager.get_task_assignment_data("agent-001")
        >>> print(data['workspace_path'])  # /home/user/project
        >>> print(len(data['forbidden_paths']))  # Number of forbidden paths
        """
        workspace = self.get_agent_workspace(agent_id)
        if not workspace:
            workspace = self.assign_agent_workspace(agent_id)

        return {
            "workspace_path": workspace.path,
            "workspace_id": workspace.workspace_id,
            "forbidden_paths": self.get_forbidden_paths(),
            "is_readonly": workspace.is_readonly,
        }

    def log_security_violation(
        self, agent_id: str, attempted_path: str, operation: str
    ) -> None:
        """
        Log a security violation attempt.

        Parameters
        ----------
        agent_id : str
            ID of the agent that attempted the violation
        attempted_path : str
            Path the agent tried to access
        operation : str
            Operation attempted (e.g., "read", "write", "execute")

        Examples
        --------
        >>> manager.log_security_violation(
        ...     "agent-001",
        ...     "/etc/passwd",
        ...     "read"
        ... )

        Notes
        -----
        Currently logs to stderr. In production, this would write to
        a security audit log file.
        """
        violation = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "attempted_path": attempted_path,
            "operation": operation,
            "marcus_root": self.marcus_root,
        }

        # In production, this would log to a security audit file
        # For now, print to stderr
        print(f"SECURITY VIOLATION: {json.dumps(violation)}", file=sys.stderr)
