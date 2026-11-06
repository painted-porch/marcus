"""
Project Registry for Marcus Multi-Project Support.

Manages multiple project configurations across different providers
(Planka, Linear, GitHub). Provides CRUD operations and persistence
for project definitions.
"""

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.error_framework import (
    ErrorContext,
    InvalidConfigurationError,
    StateConflictError,
)
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

        ``requires_source_tree`` (ADR-0013 K3, Accepted 2026-11-06,
        issue #798): an optional ``bool`` on a registered task type's
        entry. ``False`` declares that this task type's completion is
        not code-producing and has no source tree by design (a
        Minecraft build, an ops runbook) -- the validation gate skips
        its source-file check for that type rather than treating "no
        source files" as "the agent wrote nothing." Absent (the
        default for every existing registration and every
        unregistered project) behaves exactly as before K3: the gate
        still requires source files for a task that reaches it.
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


#: The only keys an ADR-0013 K4 injected-check declaration may carry.
#: Deliberately just a name and a human-readable description -- never
#: a command, script, or endpoint. K2's own rule is that dispatch is
#: pull, not call; the kernel must not be handed anything it could run.
_INJECTED_CHECK_ALLOWED_KEYS = {"name", "description"}


def _validate_task_type_registrations(
    task_type_registrations: Dict[str, Dict[str, Any]],
) -> None:
    """Reject a malformed ADR-0013 K4 ``injected_checks`` declaration.

    Checked at registration (``ProjectRegistry.add_project``), not
    read silently and ignored or silently acted on: a project that
    gets this wrong should see the error immediately, not discover at
    assignment time that nothing it declared took effect.

    Parameters
    ----------
    task_type_registrations : Dict[str, Dict[str, Any]]
        The registrations a :class:`ProjectConfig` is about to carry.

    Raises
    ------
    InvalidConfigurationError
        ``injected_checks`` is non-empty while ``checks_injectable``
        is not explicitly ``True``; a check is missing ``name``; or a
        check carries any key besides ``name``/``description``.
    """
    for task_type, registration in task_type_registrations.items():
        injected_checks = registration.get("injected_checks") or []
        if not injected_checks:
            continue
        key = f"task_type_registrations.{task_type}.injected_checks"
        if registration.get("checks_injectable") is not True:
            raise InvalidConfigurationError(
                config_key=key,
                expected_format=(
                    "checks_injectable must be explicitly true when "
                    "injected_checks is non-empty"
                ),
                context=ErrorContext(
                    operation="validate_task_type_registrations",
                    custom_context={"task_type": task_type},
                ),
            )
        for check in injected_checks:
            if not check.get("name"):
                raise InvalidConfigurationError(
                    config_key=key,
                    expected_format="each injected check needs a non-empty 'name'",
                    context=ErrorContext(
                        operation="validate_task_type_registrations",
                        custom_context={"task_type": task_type, "check": check},
                    ),
                )
            extra = set(check) - _INJECTED_CHECK_ALLOWED_KEYS
            if extra:
                raise InvalidConfigurationError(
                    config_key=key,
                    expected_format=(
                        "an injected check may only carry "
                        f"{sorted(_INJECTED_CHECK_ALLOWED_KEYS)}; got extra "
                        f"key(s) {sorted(extra)} (K2: dispatch is pull, not "
                        "call -- a check cannot carry anything the kernel "
                        "would run)"
                    ),
                    context=ErrorContext(
                        operation="validate_task_type_registrations",
                        custom_context={"task_type": task_type, "check": check},
                    ),
                )


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

        Raises
        ------
        InvalidConfigurationError
            ``config.task_type_registrations`` carries a malformed
            ADR-0013 K4 ``injected_checks`` declaration (issue #847).
        """
        _validate_task_type_registrations(config.task_type_registrations)

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

        Raises
        ------
        InvalidConfigurationError
            ``updates["task_type_registrations"]`` carries a malformed
            ADR-0013 K4 ``injected_checks`` declaration (issue #847).
            Checked here too, not just in ``add_project``: a K2
            registration is routinely written through this call
            (e.g. seeding a board after it is already registered).
        """
        if "task_type_registrations" in updates:
            _validate_task_type_registrations(updates["task_type_registrations"])

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


async def register_sqlite_board(
    project_id: str, db_path: str, registry: Optional["ProjectRegistry"] = None
) -> str:
    """Register a directly-seeded SQLite board in the project registry.

    The sanctioned registration path for anything that writes tasks
    straight into a SQLite kanban board, bypassing Marcus's own
    project-creation pipeline (issue #741): an experiment seeder
    today, any future intake adapter tomorrow. Call this BEFORE
    seeding so tasks can be created with their final, registry-prefixed
    ids straight away -- no seeder-side id rewrite ever needs to exist.

    Registration is idempotent on ``project_id`` (also the registry
    id, GH-306): re-registering the same project_id against the same
    db path never duplicates an entry, and ``add_project`` only claims
    the active slot when no project was active before, so registering
    never disturbs a running server. Re-registering the same
    project_id against a *different* db path is refused (see Raises)
    rather than silently overwritten. A board registered by a
    pre-GH-306 caller (carrying a generated UUID as its registry id
    instead of project_id) is migrated in place on first
    re-registration: its ``task_type_registrations`` and
    active-project status are carried to the canonical project_id
    record, and the UUID record is retired, rather than creating a
    second, parallel project for the same board.

    Parameters
    ----------
    project_id : str
        The board's project id, also used as the kanban project name.
    db_path : str
        SQLite board file; stored absolute so the entry stays valid
        from any working directory.
    registry : ProjectRegistry | None
        Injected in tests; defaults to Marcus's file-backed registry
        under the package root.

    Returns
    -------
    str
        The registry project id (the existing one when the board was
        already registered).

    Raises
    ------
    StateConflictError
        ``project_id`` is already registered against a *different*
        database path. ``ProjectConfig.id`` is the kanban project id
        (GH-306), so ``add_project`` would otherwise overwrite that
        record under the same id -- silently discarding the original
        board's path and any K2 registration it carries, and
        redirecting anything already holding this project id to the
        new board.
    """
    absolute = str(Path(db_path).resolve())
    registry = registry or ProjectRegistry()
    await registry.initialize()

    existing = await registry.get_project(project_id)
    if existing is not None:
        existing_db_path = existing.provider_config.get("db_path")
        if existing_db_path != absolute:
            raise StateConflictError(
                conflict_description=(
                    f"project id {project_id!r} is already registered "
                    f"against a different board ({existing_db_path!r}); "
                    f"refusing to overwrite it with {absolute!r}. Use a "
                    "different project_id for the new board."
                ),
                context=ErrorContext(
                    operation="register_sqlite_board",
                    custom_context={
                        "project_id": project_id,
                        "existing_db_path": existing_db_path,
                        "requested_db_path": absolute,
                    },
                ),
            )
        return existing.id

    # Migration path: a board registered by a pre-GH-306 caller carries
    # a generated UUID as its registry id instead of project_id, found
    # here by the name+path match that was the only lookup available
    # before GH-306. Left alone, this id-first branch would create a
    # second, parallel record with empty task_type_registrations while
    # the original -- carrying any real K2 registration -- stayed live
    # under the old UUID: duplicate projects for every registry
    # consumer, and the task-project-id lookup still unable to find
    # this board's rules. Carrying the legacy record's registrations
    # and active-project status over to the canonical record, then
    # retiring the UUID one, keeps this a single project throughout.
    legacy = None
    for candidate in await registry.list_projects(provider="sqlite"):
        cfg = candidate.provider_config
        if cfg.get("project_name") == project_id and cfg.get("db_path") == absolute:
            legacy = candidate
            break

    task_type_registrations: Dict[str, Dict[str, Any]] = {}
    legacy_was_active = False
    if legacy is not None:
        task_type_registrations = dict(legacy.task_type_registrations)
        active = await registry.get_active_project()
        legacy_was_active = active is not None and active.id == legacy.id

    config = ProjectConfig(
        # GH-306: the kanban project id is the single source of truth,
        # not an auto-generated registry UUID -- otherwise task.project_id
        # (written from this same string at task creation) never matches
        # a registry lookup by id, breaking any per-project K2
        # registration a seeded board declares (issue #830, second half).
        id=project_id,
        name=f"{project_id} - Main Board",
        provider="sqlite",
        provider_config={
            "db_path": absolute,
            "project_name": project_id,
            "attachments_dir": str(Path(absolute).parent / "attachments"),
            "project_id": project_id,
        },
        tags=["auto-created", "sqlite", "tow-experiment"],
        task_type_registrations=task_type_registrations,
    )
    new_id = await registry.add_project(config)

    if legacy is not None:
        await registry.delete_project(legacy.id)
        if legacy_was_active:
            await registry.set_active_project(new_id)

    return new_id
