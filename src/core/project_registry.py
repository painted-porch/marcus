"""
Project Registry for Marcus Multi-Project Support.

Manages multiple project configurations across different providers
(Planka, Linear, GitHub). Provides CRUD operations and persistence
for project definitions.
"""

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.core.persistence import Persistence


def _utc_now() -> datetime:
    """Return current UTC datetime with timezone info."""
    return datetime.now(timezone.utc)


@dataclass
class ProjectConfig:
    """Configuration for a single project.

    Attributes
    ----------
    task_type_registrations : Dict[str, Dict[str, Any]]
        ADR-0013 K2 (Accepted 2026-11-05): an extension's declared
        ownership of this project's task types, keyed by task type.
        Each entry is a dict describing that task type's evidence
        requirement and whether the kernel may inject required
        checks on top of what the worker submits -- not a callable
        or endpoint reference. Dispatch is pull, not call (K2's own
        decision text): the kernel never invokes an extension's code
        directly, so nothing here ever names a function to run.
        Empty for a project that registers nothing, which behaves
        exactly as it did before K2 existed.
    """

    id: str
    name: str
    provider: str  # 'planka', 'linear', 'github'
    provider_config: Dict[str, Any]  # Provider-specific configuration
    created_at: datetime = field(default_factory=_utc_now)
    last_used: datetime = field(default_factory=_utc_now)
    tags: List[str] = field(default_factory=list)
    task_type_registrations: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for persistence."""
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["last_used"] = self.last_used.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectConfig":
        """Create from dictionary."""
        # Only keep fields that ProjectConfig expects
        expected_fields = {
            "id",
            "name",
            "provider",
            "provider_config",
            "created_at",
            "last_used",
            "tags",
            "task_type_registrations",
        }
        filtered_data = {k: v for k, v in data.items() if k in expected_fields}

        # Convert datetime strings, ensuring timezone-aware
        created_at = datetime.fromisoformat(filtered_data["created_at"])
        if created_at.tzinfo is None:
            # Convert naive datetime to UTC-aware
            created_at = created_at.replace(tzinfo=timezone.utc)
        filtered_data["created_at"] = created_at

        last_used = datetime.fromisoformat(filtered_data["last_used"])
        if last_used.tzinfo is None:
            # Convert naive datetime to UTC-aware
            last_used = last_used.replace(tzinfo=timezone.utc)
        filtered_data["last_used"] = last_used

        return cls(**filtered_data)


class ProjectRegistry:
    """
    Registry for managing multiple projects.

    Provides CRUD operations and persistence for project configurations.
    Uses the existing Persistence layer for storage.
    """

    COLLECTION = "projects"
    ACTIVE_PROJECT_KEY = "active_project"

    def __init__(self, persistence: Optional[Persistence] = None):
        """
        Initialize the project registry.

        Parameters
        ----------
            persistence
                Optional persistence instance. If not provided,.
                        creates a new one with file backend.
        """
        self.persistence = persistence or Persistence()
        self._cache: Dict[str, ProjectConfig] = {}
        self._active_project_id: Optional[str] = None

    async def initialize(self) -> None:
        """Initialize the registry and load active project."""
        # Load active project ID
        active_data = await self.persistence.retrieve(
            self.COLLECTION, self.ACTIVE_PROJECT_KEY
        )
        if active_data:
            self._active_project_id = active_data.get("project_id")

        # Pre-load all projects into cache
        all_projects = await self.persistence.query(self.COLLECTION)
        for proj_data in all_projects:
            if "id" in proj_data:  # Skip metadata entries
                project = ProjectConfig.from_dict(proj_data)
                self._cache[project.id] = project

    async def add_project(self, config: ProjectConfig) -> str:
        """
        Add a new project to the registry.

        Parameters
        ----------
            config
                Project configuration.

        Returns
        -------
            Project ID
        """
        # Generate ID if not provided
        if not config.id:
            config.id = str(uuid.uuid4())

        # Store in persistence
        await self.persistence.store(self.COLLECTION, config.id, config.to_dict())

        # Update cache
        self._cache[config.id] = config

        # If this is the first project, make it active
        if not self._active_project_id:
            await self.set_active_project(config.id)

        return config.id

    async def get_project(self, project_id: str) -> Optional[ProjectConfig]:
        """
        Get a project by ID.

        Parameters
        ----------
            project_id
                Project ID.

        Returns
        -------
            Project configuration or None if not found
        """
        # Check cache first
        if project_id in self._cache:
            return self._cache[project_id]

        # Load from persistence
        data = await self.persistence.retrieve(self.COLLECTION, project_id)
        if data:
            project = ProjectConfig.from_dict(data)
            self._cache[project_id] = project
            return project

        return None

    async def list_projects(
        self, filter_tags: Optional[List[str]] = None, provider: Optional[str] = None
    ) -> List[ProjectConfig]:
        """
        List all projects with optional filtering.

        Parameters
        ----------
            filter_tags
                Only return projects with these tags.
            provider
                Only return projects using this provider.

        Returns
        -------
            List of project configurations
        """
        projects = list(self._cache.values())

        # Apply filters
        if filter_tags:
            projects = [
                p for p in projects if any(tag in p.tags for tag in filter_tags)
            ]

        if provider:
            projects = [p for p in projects if p.provider == provider]

        # Sort by last used, ensuring all timestamps are timezone-aware
        def sort_key(p: ProjectConfig) -> datetime:
            """Get timezone-aware last_used for sorting."""
            if p.last_used.tzinfo is None:
                # Convert naive to UTC-aware for sorting
                return p.last_used.replace(tzinfo=timezone.utc)
            return p.last_used

        projects.sort(key=sort_key, reverse=True)

        return projects

    async def update_project(self, project_id: str, updates: Dict[str, Any]) -> bool:
        """
        Update a project configuration.

        Parameters
        ----------
            project_id
                Project ID.
            updates
                Fields to update.

        Returns
        -------
            True if successful
        """
        project = await self.get_project(project_id)
        if not project:
            return False

        # Apply updates
        for key, value in updates.items():
            if hasattr(project, key):
                setattr(project, key, value)

        # Update last_used timestamp
        project.last_used = datetime.now(timezone.utc)

        # Save to persistence
        await self.persistence.store(self.COLLECTION, project_id, project.to_dict())

        # Update cache
        self._cache[project_id] = project

        return True

    async def delete_project(self, project_id: str) -> bool:
        """
        Delete a project from the registry.

        Parameters
        ----------
            project_id
                Project ID.

        Returns
        -------
            True if successful
        """
        if project_id not in self._cache:
            return False

        # Remove from persistence
        await self.persistence.delete(self.COLLECTION, project_id)

        # Remove from cache
        del self._cache[project_id]

        # If this was the active project, clear it
        if self._active_project_id == project_id:
            self._active_project_id = None
            # Try to set another project as active
            remaining = await self.list_projects()
            if remaining:
                await self.set_active_project(remaining[0].id)

        return True

    async def set_active_project(self, project_id: str) -> bool:
        """
        Set the active project.

        Parameters
        ----------
            project_id
                Project ID to make active.

        Returns
        -------
            True if successful
        """
        project = await self.get_project(project_id)
        if not project:
            return False

        self._active_project_id = project_id

        # Update last_used timestamp
        await self.update_project(project_id, {"last_used": datetime.now(timezone.utc)})

        # Store active project ID
        await self.persistence.store(
            self.COLLECTION, self.ACTIVE_PROJECT_KEY, {"project_id": project_id}
        )

        return True

    async def get_active_project(self) -> Optional[ProjectConfig]:
        """
        Get the currently active project.

        Returns
        -------
            Active project configuration or None
        """
        if self._active_project_id:
            return await self.get_project(self._active_project_id)
        return None

    def get_project_sync(self, project_id: str) -> Optional[ProjectConfig]:
        """
        Get a project by id from cache, without any I/O.

        For callers that cannot await -- issue #830's eligibility check
        runs synchronously inside the assignment path. Reads only the
        in-memory cache ``initialize``/``add_project``/``switch_project``
        already populate; a project that has never been loaded into this
        process reads as not found rather than being fetched.

        Parameters
        ----------
            project_id
                Project id to look up.

        Returns
        -------
            That project's configuration, or None if it is not cached.
        """
        return self._cache.get(project_id)

    def get_active_project_sync(self) -> Optional[ProjectConfig]:
        """
        Get the currently active project from cache, without any I/O.

        Returns
        -------
            Active project configuration, or None if there is no active
            project or it is not yet cached.
        """
        if self._active_project_id is None:
            return None
        return self.get_project_sync(self._active_project_id)

    async def create_from_legacy_config(self, legacy_config: Dict[str, Any]) -> str:
        """
        Create a project from legacy single-project configuration.

        Parameters
        ----------
            legacy_config
                Legacy configuration dictionary.

        Returns
        -------
            Created project ID
        """
        # Determine provider from config
        provider = legacy_config.get("kanban", {}).get("provider", "planka")

        # Extract provider-specific config
        provider_config = {}
        if provider == "planka":
            provider_config = {
                "project_id": legacy_config.get("project_id"),
                "board_id": legacy_config.get("board_id"),
            }
        elif provider == "github":
            github_cfg = legacy_config.get("github", {})
            provider_config = {
                "owner": github_cfg.get("owner"),
                "repo": github_cfg.get("repo"),
                "project_number": github_cfg.get("project_number", 1),
            }
        elif provider == "linear":
            linear_cfg = legacy_config.get("linear", {})
            provider_config = {
                "team_id": linear_cfg.get("team_id"),
                "project_id": linear_cfg.get("project_id"),
            }

        # Create project config
        project = ProjectConfig(
            id=str(uuid.uuid4()),
            name=legacy_config.get("project_name", "Default Project"),
            provider=provider,
            provider_config=provider_config,
            tags=["migrated", "default"],
        )

        return await self.add_project(project)
