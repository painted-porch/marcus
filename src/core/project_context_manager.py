"""Project Context Manager for Marcus Multi-Project Support.

Manages project switching, state isolation, and kanban client lifecycle
for multiple concurrent projects.
"""

import asyncio
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from src.config.marcus_config import get_config
from src.core.assignment_persistence import AssignmentPersistence
from src.core.context import Context
from src.core.event_loop_utils import EventLoopLockManager
from src.core.events import Events
from src.core.models import ProjectState
from src.core.paths import marcus_data_dir
from src.core.persistence import Persistence
from src.core.project_registry import ProjectConfig, ProjectRegistry
from src.integrations.kanban_factory import KanbanFactory
from src.integrations.kanban_interface import KanbanInterface
from src.logging.conversation_logger import conversation_logger

logger = logging.getLogger(__name__)


class ProjectContext:
    """Container for project-specific state and services."""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.kanban_client: Optional[KanbanInterface] = None
        self.context: Optional[Context] = None
        self.events: Optional[Events] = None
        self.project_state: Optional[ProjectState] = None
        self.assignment_persistence: Optional[AssignmentPersistence] = None
        self.last_accessed = datetime.now(timezone.utc)
        self.is_connected = False


class ProjectContextManager:
    """Manages multiple project contexts with state isolation.

    Handles:
    - Project switching
    - Kanban client lifecycle
    - State isolation per project
    - Resource cleanup for inactive projects
    """

    MAX_CACHED_PROJECTS = 10
    IDLE_TIMEOUT_MINUTES = 30

    def __init__(
        self,
        registry: Optional[ProjectRegistry] = None,
        global_context: Optional[Context] = None,
    ):
        """Initialize the project context manager.

        Parameters
        ----------
        registry : Optional[ProjectRegistry]
            Optional project registry instance.
        global_context : Optional[Context]
            Optional global context instance to update with project_id changes.
        """
        self.registry = registry or ProjectRegistry()
        self.persistence = Persistence()
        self.config = get_config()

        # Use OrderedDict for LRU behavior
        self.contexts: OrderedDict[str, ProjectContext] = OrderedDict()
        self.active_project_id: Optional[str] = None
        self.active_project_name: Optional[str] = None

        # Global context to sync project_id with
        self._global_context = global_context

        # Lock manager for event loop safe operations
        self._lock_manager = EventLoopLockManager()

        # Background task for cleanup
        self._cleanup_task: Optional[asyncio.Task[None]] = None

    @property
    def lock(self) -> asyncio.Lock:
        """Get context lock for the current event loop."""
        return self._lock_manager.get_lock()

    def set_global_context(self, context: Context) -> None:
        """Set the global context to sync project_id with.

        Parameters
        ----------
        context : Context
            The global context instance to update when projects switch.
        """
        self._global_context = context
        logger.debug("Global context linked to project context manager")

    async def initialize(self, auto_switch: bool = True) -> None:
        """
        Initialize the context manager.

        Parameters
        ----------
        auto_switch : bool
            If True, automatically switch to the active project.
            Set to False if you want to sync projects before switching.
        """
        await self.registry.initialize()

        # Load the active project (only if auto_switch is enabled)
        if auto_switch:
            active_project = await self.registry.get_active_project()
            if active_project:
                await self.switch_project(active_project.id)

        # Start background cleanup task
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def shutdown(self) -> None:
        """Shutdown the context manager and cleanup resources."""
        # Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Disconnect all clients
        for context in self.contexts.values():
            if context.kanban_client and context.is_connected:
                try:
                    await context.kanban_client.disconnect()
                except Exception as e:
                    logger.error(f"Error disconnecting client: {e}")

    async def switch_project(self, project_id: str) -> bool:
        """Switch to a different project.

        Parameters
        ----------
        project_id : str
            Target project ID.

        Returns
        -------
        bool
            True if successful.
        """
        async with self.lock:
            # Get project config
            project = await self.registry.get_project(project_id)
            if not project:
                logger.error(f"Project {project_id} not found")
                conversation_logger.log_pm_decision(
                    decision=f"Failed to switch to project {project_id}",
                    rationale="Project not found in registry",
                    confidence_score=1.0,
                    decision_factors={"project_id": project_id, "error": "not_found"},
                )
                return False

            # Save current project state if exists
            previous_project_id = self.active_project_id
            previous_project = None
            if self.active_project_id and self.active_project_id in self.contexts:
                previous_project = await self.registry.get_project(
                    self.active_project_id
                )
                await self._save_project_state(self.active_project_id)

            # Log the project switch decision
            conversation_logger.log_pm_decision(
                decision=(
                    f"Switching project from "
                    f"'{previous_project.name if previous_project else 'None'}' "
                    f"to '{project.name}'"
                ),
                rationale="User requested project switch",
                confidence_score=1.0,
                decision_factors={
                    "previous_project_id": previous_project_id,
                    "previous_project_name": (
                        previous_project.name if previous_project else None
                    ),
                    "new_project_id": project_id,
                    "new_project_name": project.name,
                    "provider": project.provider,
                    "tags": project.tags,
                },
            )

            # Load or create context
            await self._get_or_create_context(project)

            # Update active project
            self.active_project_id = project_id
            self.active_project_name = project.name
            await self.registry.set_active_project(project_id)

            # Update global context's project_id if it exists
            if self._global_context is not None:
                self._global_context.project_id = project_id
                logger.debug(
                    f"Updated global context project_id to {project_id} "
                    f"for project '{project.name}'"
                )

            # Move to end for LRU
            self.contexts.move_to_end(project_id)

            # Cleanup old contexts if needed
            await self._cleanup_old_contexts()

            # Log successful switch
            conversation_logger.log_kanban_interaction(
                action="project_switched",
                direction="internal",
                data={
                    "project_id": project_id,
                    "project_name": project.name,
                    "provider": project.provider,
                    "cached_contexts": len(self.contexts),
                    "active_project": project_id,
                    "total_projects": len(await self.registry.list_projects()),
                },
            )

            logger.info(f"Switched to project: {project.name} ({project_id})")
            return True

    async def get_kanban_client(self) -> Optional[KanbanInterface]:
        """Get the kanban client for the active project.

        Returns
        -------
        Optional[KanbanInterface]
            Kanban client or None if no active project.
        """
        if not self.active_project_id:
            return None

        context = self.contexts.get(self.active_project_id)
        if not context:
            return None

        # Update last accessed
        context.last_accessed = datetime.now(timezone.utc)

        return context.kanban_client

    async def get_active_context(self) -> Optional[Context]:
        """Get the context for the active project."""
        if not self.active_project_id:
            return None

        context = self.contexts.get(self.active_project_id)
        return context.context if context else None

    async def get_active_events(self) -> Optional[Events]:
        """Get the events for the active project."""
        if not self.active_project_id:
            return None

        context = self.contexts.get(self.active_project_id)
        return context.events if context else None

    async def get_active_project_state(self) -> Optional[ProjectState]:
        """Get the project state for the active project."""
        if not self.active_project_id:
            return None

        context = self.contexts.get(self.active_project_id)
        return context.project_state if context else None

    async def get_active_assignment_persistence(
        self,
    ) -> Optional[AssignmentPersistence]:
        """Get the assignment persistence for the active project."""
        if not self.active_project_id:
            return None

        context = self.contexts.get(self.active_project_id)
        return context.assignment_persistence if context else None

    async def _get_or_create_context(self, project: ProjectConfig) -> ProjectContext:
        """Get existing context or create new one."""
        if project.id in self.contexts:
            conversation_logger.log_kanban_interaction(
                action="context_reused",
                direction="internal",
                data={
                    "project_id": project.id,
                    "project_name": project.name,
                    "provider": project.provider,
                },
            )
            return self.contexts[project.id]

        # Log context creation
        conversation_logger.log_pm_thinking(
            thought=(
                f"Creating new context for project '{project.name}' - "
                f"No existing context found for project {project.id}"
            ),
            context={"project_id": project.id, "provider": project.provider},
        )

        # Create new context
        context = ProjectContext(project.id)

        # Create kanban client
        provider_config = self._build_provider_config(project)
        context.kanban_client = KanbanFactory.create(project.provider, provider_config)

        # Connect client
        try:
            await context.kanban_client.connect()
            context.is_connected = True

            # Log successful connection
            conversation_logger.log_kanban_interaction(
                action="provider_connected",
                direction="to_kanban",
                data={
                    "project_id": project.id,
                    "project_name": project.name,
                    "provider": project.provider,
                    "connection_status": "success",
                },
            )
        except Exception as e:
            logger.error(f"Failed to connect kanban client: {e}")

            # Log connection failure
            conversation_logger.log_kanban_interaction(
                action="provider_connection_failed",
                direction="to_kanban",
                data={
                    "project_id": project.id,
                    "project_name": project.name,
                    "provider": project.provider,
                    "error": str(e),
                },
            )
            raise

        # Create project-specific services
        context.context = Context(
            events=None, persistence=self.persistence, project_id=project.id
        )

        context.events = Events(store_history=True, persistence=self.persistence)

        # Create project-specific assignment persistence directory

        assignments_dir = marcus_data_dir() / "assignments" / f"project_{project.id}"
        context.assignment_persistence = AssignmentPersistence(
            storage_dir=assignments_dir
        )

        # Load project state
        context.project_state = await self._load_project_state(project.id)

        # Store context
        self.contexts[project.id] = context

        # Log context creation complete
        conversation_logger.log_kanban_interaction(
            action="context_created",
            direction="internal",
            data={
                "project_id": project.id,
                "project_name": project.name,
                "provider": project.provider,
                "services_initialized": ["context", "events", "assignment_persistence"],
            },
        )

        return context

    def _build_provider_config(self, project: ProjectConfig) -> Dict[str, Any]:
        """Build provider configuration merging global and project configs."""
        config = {}

        # Get global provider credentials
        if project.provider == "planka":
            config.update(
                {
                    "base_url": self.config.kanban.planka_base_url,
                    "email": self.config.kanban.planka_email,
                    "password": self.config.kanban.planka_password,
                }
            )
        elif project.provider == "github":
            config.update({"token": self.config.kanban.github_token})
        elif project.provider == "linear":
            config.update({"api_key": self.config.kanban.linear_api_key})
        elif project.provider == "sqlite":
            config.update(
                {
                    # Issue #724 (Codex P1 on PR #725): explicit
                    # "./data/..." literals here bypassed the factory and
                    # provider fallbacks — a test switching to a registered
                    # sqlite project connected straight to the production
                    # board. Route the fallback through the shared resolver;
                    # config-specified paths are still honored.
                    "db_path": (
                        self.config.kanban.sqlite_db_path
                        or str(marcus_data_dir() / "kanban.db")
                    ),
                    "project_name": project.name,
                    "attachments_dir": (
                        self.config.kanban.sqlite_attachments_dir
                        or str(marcus_data_dir() / "attachments")
                    ),
                }
            )

        # Merge with project-specific config
        config.update(project.provider_config)

        return config

    async def _save_project_state(self, project_id: str) -> None:
        """Save project state to persistence."""
        context = self.contexts.get(project_id)
        if not context or not context.project_state:
            return

        # Save to persistence
        await self.persistence.store(
            "project_states",
            project_id,
            {
                "state": context.project_state.__dict__,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def _load_project_state(self, project_id: str) -> Optional[ProjectState]:
        """Load project state from persistence."""
        data = await self.persistence.retrieve("project_states", project_id)
        if data and "state" in data:
            # Convert back to ProjectState
            # This is simplified - actual implementation would reconstruct the object
            return None  # TODO: Implement state reconstruction
        return None

    async def _cleanup_old_contexts(self) -> None:
        """Cleanup old contexts if exceeding cache limit."""
        if len(self.contexts) <= self.MAX_CACHED_PROJECTS:
            return

        # Remove oldest contexts (excluding active)
        to_remove = []
        for project_id in self.contexts:
            if project_id != self.active_project_id:
                to_remove.append(project_id)
                if len(self.contexts) - len(to_remove) <= self.MAX_CACHED_PROJECTS:
                    break

        for project_id in to_remove:
            await self._remove_context(project_id)

    async def _remove_context(self, project_id: str) -> None:
        """Remove and cleanup a project context."""
        context = self.contexts.get(project_id)
        if not context:
            return

        # Save state
        await self._save_project_state(project_id)

        # Disconnect client
        if context.kanban_client and context.is_connected:
            try:
                await context.kanban_client.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting client: {e}")

        # Remove from cache
        del self.contexts[project_id]
        logger.info(f"Removed context for project {project_id}")

    async def _cleanup_loop(self) -> None:
        """Background task to cleanup idle projects."""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes

                async with self.lock:
                    now = datetime.now(timezone.utc)
                    idle_threshold = now - timedelta(minutes=self.IDLE_TIMEOUT_MINUTES)

                    to_remove = []
                    for project_id, context in self.contexts.items():
                        if (
                            project_id != self.active_project_id
                            and context.last_accessed < idle_threshold
                        ):
                            to_remove.append(project_id)

                    for project_id in to_remove:
                        await self._remove_context(project_id)
                        logger.info(f"Cleaned up idle project: {project_id}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")
