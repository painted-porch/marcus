#!/usr/bin/env python3
"""Marcus MCP Server - Modularized Version.

A lean MCP server implementation that delegates all tool logic
to specialized modules for better maintainability.
"""

import asyncio
import atexit
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from src.telemetry import get_telemetry_client, print_first_run_notice_if_needed

logger = logging.getLogger(__name__)

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


import mcp.types as types  # noqa: E402
from mcp.server import Server  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402

from src.config.marcus_config import (  # noqa: E402
    TaskLeaseSettings,
    get_config,
    setup_logging,
)
from src.config.settings import Settings  # noqa: E402

# Import for type annotations only
from src.core.assignment_lease import (  # noqa: E402
    AssignmentLeaseManager,
    LeaseMonitor,
)
from src.core.assignment_persistence import AssignmentPersistence  # noqa: E402
from src.core.code_analyzer import CodeAnalyzer  # noqa: E402
from src.core.context import Context  # noqa: E402
from src.core.event_loop_utils import EventLoopLockManager  # noqa: E402
from src.core.events import Events  # noqa: E402
from src.core.models import (  # noqa: E402
    ProjectState,
    RiskLevel,
    Task,
    TaskAssignment,
    TaskStatus,
    WorkerStatus,
)
from src.core.project_context_manager import ProjectContextManager  # noqa: E402
from src.core.project_registry import ProjectRegistry  # noqa: E402
from src.core.service_registry import (  # noqa: E402
    register_marcus_service,
    unregister_marcus_service,
)
from src.cost_tracking.ai_usage_middleware import ai_usage_middleware  # noqa: E402
from src.cost_tracking.cost_recorder import (  # noqa: E402
    CostRecorder,
    set_recorder,
)
from src.cost_tracking.cost_store import CostStore  # noqa: E402
from src.cost_tracking.token_tracker import token_tracker  # noqa: E402
from src.integrations.ai_analysis_engine import AIAnalysisEngine  # noqa: E402
from src.integrations.kanban_factory import KanbanFactory  # noqa: E402
from src.integrations.kanban_interface import KanbanInterface  # noqa: E402
from src.marcus_mcp.handlers import handle_tool_call  # noqa: E402
from src.marcus_mcp.tool_groups import get_tools_for_endpoint  # noqa: E402
from src.monitoring.assignment_monitor import AssignmentMonitor  # noqa: E402
from src.monitoring.project_monitor import ProjectMonitor  # noqa: E402


def _carry_forward_active_subtasks(
    project_tasks: Optional[List[Task]], parent_ids: Set[str]
) -> Tuple[List[Task], int]:
    """Select which in-memory subtasks survive a project-state refresh.

    Only subtasks whose parent is still on the board (``parent_ids``) are
    carried forward in Marcus's in-memory working set. Orphaned subtasks
    (parent removed, or left over from another project in a long-lived
    server process) are dropped from the working set so they stop bloating
    memory and churning the dependency-inference cache signature on the
    ``request_next_task`` hot path (issue #667).

    This trims the in-memory list ONLY. The durable record in
    ``data/marcus_state/subtasks.json`` -- which Cato reads directly as its
    subtask data source -- is never modified here. Issue #672 tracks moving
    subtask storage into the database so Cato consumes a contract rather
    than Marcus's private file.

    A transient empty ``parent_ids`` (the board fetch returned no tasks --
    the Planka provider does this on error) is treated as "no information"
    rather than "every parent is gone": all existing subtasks are preserved
    and nothing is dropped. Otherwise a single failed refresh would orphan
    every subtask, and the one-time migration guard would never re-add them
    (Codex P1 on PR #673).

    Parameters
    ----------
    project_tasks : Optional[List[Task]]
        Current in-memory task list (parents and subtasks), or None.
    parent_ids : Set[str]
        IDs of parent tasks present on the board this refresh.

    Returns
    -------
    Tuple[List[Task], int]
        ``(kept_subtasks, dropped_orphan_count)``.
    """
    subtasks = [t for t in (project_tasks or []) if getattr(t, "is_subtask", False)]
    # Empty parent set == the board fetch returned nothing this refresh,
    # almost always transient (Planka returns [] when get_all_tasks raises).
    # Preserve all subtasks rather than orphan-GC them, or one failed
    # refresh strands every subtask until restart (Codex P1 on PR #673).
    if not parent_ids:
        return subtasks, 0
    kept: List[Task] = []
    dropped = 0
    for task in subtasks:
        if getattr(task, "parent_task_id", None) in parent_ids:
            kept.append(task)
        else:
            dropped += 1
    return kept, dropped


class MarcusServer:
    """Marcus MCP Server with modularized architecture."""

    def __init__(self) -> None:
        """Initialize Marcus server instance."""
        # Config is already loaded by marcus.py, but ensure it's available
        self.config = get_config()
        self.settings = Settings()

        # Initialize project management components
        self.project_registry = ProjectRegistry()
        self.project_manager = ProjectContextManager(self.project_registry)

        # Get default provider from config
        self.provider = self.config.kanban.provider or "sqlite"

        # Check if in multi-project mode
        self.is_multi_project_mode = self.config.single_project_mode is False

        # Create realtime log with line buffering
        # Use absolute path based on Marcus root directory
        marcus_root = Path(__file__).parent.parent.parent
        log_dir = marcus_root / "logs" / "conversations"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.realtime_log = open(
            log_dir / f"realtime_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.jsonl",
            "a",
            buffering=1,  # Line buffering
        )
        atexit.register(self.realtime_log.close)
        atexit.register(unregister_marcus_service)

        # Core components
        self.kanban_client: Optional[KanbanInterface] = (
            None  # Will be set by project manager
        )
        self.ai_engine = AIAnalysisEngine()
        # ProjectMonitor uses KanbanClient (Planka-only). Skip for other providers
        # so non-Planka startups are not blocked by kanban-mcp path detection.
        self.monitor = ProjectMonitor() if self.provider == "planka" else None

        # Cached embedding model for cross-parent dependency wiring.
        # Loaded once on first refresh_project_state call — avoids reloading
        # the SentenceTransformer (all-MiniLM-L6-v2) on every create_project,
        # which was adding ~40s to the first call and tripping MCP timeouts.
        self._embedding_model: Optional[Any] = None

        # Token tracking for cost monitoring
        self.token_tracker = token_tracker

        # Cost recorder: writes one row per provider LLM call to SQLite,
        # capturing input / cache_creation / cache_read / output tokens
        # for the cost dashboard (#409). Stored at ~/.marcus/costs.db.
        # Best-effort: any store failure is swallowed by the recorder so
        # the provider call path can never be broken by cost tracking.
        self.cost_store = CostStore(db_path=Path.home() / ".marcus" / "costs.db")
        self.cost_store.load_seed_prices()
        self.cost_recorder = CostRecorder(store=self.cost_store, enabled=True)
        set_recorder(self.cost_recorder)

        # Code analyzer for GitHub
        self.code_analyzer = None
        if self.provider == "github":
            self.code_analyzer = CodeAnalyzer()

        # State tracking
        self.agent_tasks: Dict[str, TaskAssignment] = {}
        self.agent_status: Dict[str, WorkerStatus] = {}
        self.agent_project_map: Dict[str, str] = {}  # agent_id → project_id (GH-388)
        self.project_state: Optional[ProjectState] = None
        self.project_tasks: List[Any] = []

        # Assignment persistence and locking
        self.assignment_persistence = AssignmentPersistence()
        self._lock_manager = EventLoopLockManager()
        self.tasks_being_assigned: set[str] = set()

        # File-level write-lock registry (#206 MVP, v0.3.9).
        # In-process registry tracking which file paths each
        # in-progress task is authorized to write to.
        # ``request_next_task`` filters candidates by it;
        # ``report_task_progress`` releases on DONE / BLOCKED.
        # See ``src/core/file_lock_registry.py`` and the design doc
        # at ``docs/design/206-file-lock-registry-mvp.md``.
        from src.core.file_lock_registry import FileLockRegistry

        self.file_lock_registry = FileLockRegistry()

        # Subtask management for hierarchical task decomposition
        from src.marcus_mcp.coordinator import SubtaskManager

        self.subtask_manager = SubtaskManager()
        self._subtasks_migrated = False  # Track if migration has been done

        # Assignment monitoring
        self.assignment_monitor: Optional[AssignmentMonitor] = None

        # Lease management
        self.lease_manager: Optional[AssignmentLeaseManager] = None
        self.lease_monitor: Optional[LeaseMonitor] = None

        # Gridlock detection
        from src.core.gridlock_detector import GridlockDetector

        self.gridlock_detector = GridlockDetector(
            request_threshold=3,
            time_window_minutes=5,
            alert_cooldown_minutes=10,
        )

        # Track active connections and cleanup state
        self._cleanup_done = False
        self._active_operations: Set[Any] = set()
        self._shutdown_event = asyncio.Event()

        # New enhancement systems (optional based on config)
        # Declare optional attributes
        self.events: Optional[Events] = None
        self.context: Optional[Context] = None
        self.memory: Any = None

        # Get feature configurations
        config_loader = get_config()

        # Persistence layer (if any enhanced features are enabled)
        self.persistence = None
        if any(
            [
                config_loader.features.events,
                config_loader.features.context,
                config_loader.features.memory,
            ]
        ):
            from src.core.persistence import Persistence, SQLitePersistence

            # Use SQLite for better performance
            persistence_path = Path(config_loader.data_dir).expanduser() / "marcus.db"
            self.persistence = Persistence(backend=SQLitePersistence(persistence_path))

        # Events system for loose coupling
        if config_loader.features.events:
            self.events = Events(
                store_history=True,
                persistence=self.persistence,
            )
        else:
            self.events = None

        # Context system for rich task assignments
        if config_loader.features.context:
            # Use hybrid inference settings from config
            use_hybrid = True

            self.context = Context(
                events=self.events,
                persistence=self.persistence,
                use_hybrid_inference=use_hybrid
                and config_loader.hybrid_inference.enable_ai_inference,
                ai_engine=self.ai_engine if use_hybrid else None,
            )
            # Link global context to project manager for project_id syncing
            self.project_manager.set_global_context(self.context)
        else:
            self.context = None

        # Memory system for learning and prediction
        if config_loader.features.memory:
            # Check if we should use enhanced memory
            if config_loader.memory.use_v2_predictions:
                from src.core.memory_enhanced import MemoryEnhanced

                self.memory = MemoryEnhanced(
                    events=self.events, persistence=self.persistence
                )
            else:
                from src.core.memory import Memory

                self.memory = Memory(events=self.events, persistence=self.persistence)

            # Apply memory-specific settings
            if config_loader.memory.learning_rate:
                self.memory.learning_rate = config_loader.memory.learning_rate
            if config_loader.memory.min_samples:
                self.memory.confidence_threshold = config_loader.memory.min_samples
        else:
            self.memory = None

        # Visualization removed
        self.event_visualizer = None

        # Log startup
        self.log_event(
            "server_startup",
            {
                "provider": self.provider,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Create MCP server instance
        server: Server[Any] = Server("marcus")
        self.server = server

        # Register handlers
        self._register_handlers()

        # Setup signal handlers for graceful shutdown
        self._setup_signal_handlers()

        # FastMCP instance for HTTP transport (created on demand)
        self._fastmcp: Optional[FastMCP] = None
        self._endpoint_apps: Dict[str, FastMCP] = {}

    @property
    def assignment_lock(self) -> asyncio.Lock:
        """Get assignment lock for the current event loop."""
        return self._lock_manager.get_lock()

    @property
    def current_project_id(self) -> Optional[str]:
        """
        Get the current active project ID.

        Returns
        -------
        Optional[str]
            Active project ID or None if no project is active.
        """
        return self.project_manager.active_project_id

    @property
    def current_project_name(self) -> Optional[str]:
        """
        Get the current active project name.

        Returns
        -------
        Optional[str]
            Active project name or None if no project is active.
        """
        return self.project_manager.active_project_name

    def _register_handlers(self) -> None:
        """Register MCP tool handlers."""

        @self.server.list_tools()  # type: ignore[untyped-decorator,no-untyped-call,misc]  # noqa: E501
        async def handle_list_tools() -> List[types.Tool]:
            """Return list of available tools based on client role."""
            # Import here to avoid circular dependency
            from src.marcus_mcp.tools.auth import get_tool_definitions_for_client

            # Get current client ID if available
            client_id = None
            if hasattr(self, "_current_client_id"):
                client_id = self._current_client_id

            # Get tools based on client access
            return get_tool_definitions_for_client(client_id, self)

        @self.server.call_tool()  # type: ignore[untyped-decorator,misc]
        async def handle_call_tool(
            name: str, arguments: Optional[Dict[str, Any]]
        ) -> List[types.TextContent | types.ImageContent | types.EmbeddedResource]:
            """Handle tool calls with graceful handling of cancelled operations."""
            try:
                return await handle_tool_call(name, arguments, self)
            except Exception as e:
                # Gracefully handle connection errors from aborted operations
                # These occur when clients cancel requests mid-flight (e.g., Ctrl+C)
                error_type = type(e).__name__
                if (
                    "BrokenResourceError" in error_type
                    or "ClosedResourceError" in error_type
                ):
                    logger.warning(
                        f"Client connection closed during tool call '{name}' "
                        f"(likely aborted by user). This is expected behavior and "
                        f"does not indicate a problem."
                    )
                    # Don't try to return a response - the connection is closed
                    # Return empty list to satisfy the type signature
                    return []
                else:
                    # For other exceptions, re-raise so they're handled normally
                    raise

        @self.server.list_prompts()  # type: ignore[untyped-decorator,no-untyped-call,misc]  # noqa: E501
        async def handle_list_prompts() -> List[types.Prompt]:
            """Return list of available prompts - not used by Marcus."""
            return []

        @self.server.list_resources()  # type: ignore[untyped-decorator,no-untyped-call,misc]  # noqa: E501
        async def handle_list_resources() -> List[types.Resource]:
            """Return list of available resources - not used by Marcus."""
            return []

        @self.server.read_resource()  # type: ignore[untyped-decorator,no-untyped-call,misc]  # noqa: E501
        async def handle_read_resource(uri: str) -> str:
            """Read a resource - Marcus doesn't use resources currently."""
            raise ValueError(f"Resource not found: {uri}")

        @self.server.get_prompt()  # type: ignore[untyped-decorator,no-untyped-call,misc]  # noqa: E501
        async def handle_get_prompt(
            name: str, arguments: Optional[Dict[str, str]] = None
        ) -> types.GetPromptResult:
            """Get a prompt - Marcus doesn't use prompts currently."""
            raise ValueError(f"Prompt not found: {name}")

    def _setup_signal_handlers(self) -> None:
        """Set up signal handlers for graceful shutdown."""

        def signal_handler(signum: int, frame: Any) -> None:
            """Handle interrupt signals gracefully."""
            print(f"\n⚠️  Received signal {signum}, initiating graceful shutdown...")
            # Set shutdown event
            self._shutdown_event.set()
            # Create task for async cleanup
            if asyncio.get_event_loop().is_running():
                asyncio.create_task(self._cleanup_on_shutdown())
            else:
                # If no event loop, do sync cleanup
                self._sync_cleanup()

        # Register handlers for common interruption signals
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    async def _call_tool_via_handler_as_dict(
        self, name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Route a tool through the audited handler and decode the JSON result."""
        response = await handle_tool_call(name, arguments, self)
        if not response:
            return {"error": "Empty response from tool handler"}

        first_item = response[0]
        if not isinstance(first_item, types.TextContent):
            return {"error": f"Unsupported response type: {type(first_item).__name__}"}

        try:
            parsed = json.loads(first_item.text)
        except json.JSONDecodeError:
            return {"error": "Invalid JSON response format"}

        return parsed if isinstance(parsed, dict) else {"result": parsed}

    def _sync_cleanup(self) -> None:
        """Clean up resources synchronously when event loop is not available."""
        if self._cleanup_done:
            return

        self._cleanup_done = True

        try:
            print("🧹 Performing synchronous cleanup...")

            # Clean up tasks being assigned
            if self.tasks_being_assigned:
                print(
                    f"  Clearing {len(self.tasks_being_assigned)} pending task "
                    "assignments"
                )
                self.tasks_being_assigned.clear()

            # Close realtime log
            if hasattr(self, "realtime_log") and self.realtime_log:
                self.realtime_log.close()

            print("✅ Cleanup completed")

        except Exception as e:
            print(f"❌ Error during cleanup: {e}")

        # Force exit after cleanup
        os._exit(0)

    async def _cleanup_on_shutdown(self) -> None:
        """Clean up resources on shutdown."""
        if self._cleanup_done:
            return

        self._cleanup_done = True

        try:
            print("🧹 Cleaning up active operations...")

            # Clean up tasks being assigned
            if self.tasks_being_assigned:
                print(
                    f"  Clearing {len(self.tasks_being_assigned)} pending task "
                    "assignments"
                )
                self.tasks_being_assigned.clear()

            # Cancel active operations and clean up task assignments
            if self._active_operations:
                print(f"  Cancelling {len(self._active_operations)} active operations")
                for op in self._active_operations:
                    if isinstance(op, str) and op.startswith("task_assignment_"):
                        # Extract task ID and remove from being assigned
                        task_id = op.replace("task_assignment_", "")
                        if task_id in self.tasks_being_assigned:
                            self.tasks_being_assigned.discard(task_id)
                            print(f"    Cleaned up task assignment: {task_id}")
                    elif asyncio.iscoroutine(op) or asyncio.isfuture(op):
                        try:
                            op.cancel()  # type: ignore[union-attr]
                        except AttributeError:
                            pass

            # Stop assignment monitor if running
            if self.assignment_monitor and hasattr(self.assignment_monitor, "_running"):
                self.assignment_monitor._running = False

            # Stop lease monitor if running
            if self.lease_monitor:
                await self.lease_monitor.stop()

            # Close realtime log
            if hasattr(self, "realtime_log") and self.realtime_log:
                self.realtime_log.close()

            # Persist any pending state
            if self.assignment_persistence:
                await self.assignment_persistence.cleanup()

            print("✅ Cleanup completed")

        except Exception as e:
            print(f"❌ Error during cleanup: {e}")

        # Force exit after cleanup
        os._exit(0)

    async def initialize(self) -> None:
        """Initialize all Marcus server components."""
        # Initialize AI engine
        await self.ai_engine.initialize()

        # Initialize project management (skip auto-switch, we do it after sync)
        await self.project_manager.initialize(auto_switch=False)

        # Auto-sync projects from provider
        if self.provider == "planka":
            logger.info("Auto-syncing projects from Planka...")
            try:
                from src.marcus_mcp.tools.project_management import (
                    discover_planka_projects,
                )

                result = await discover_planka_projects(self, {"auto_sync": True})
                if result.get("success"):
                    sync_result = result.get("sync_result", {})
                    summary = sync_result.get("summary", {})
                    logger.info(
                        f"Auto-synced Planka projects: "
                        f"{summary.get('added', 0)} added, "
                        f"{summary.get('updated', 0)} updated, "
                        f"{summary.get('skipped', 0)} skipped"
                    )
                else:
                    logger.warning(f"Planka auto-sync failed: {result.get('error')}")
            except Exception as e:
                logger.warning(f"Failed to auto-sync Planka projects: {e}")
        else:
            logger.info(
                f"Using {self.provider} provider — " f"skipping Planka auto-sync"
            )

        # Switch to active project
        try:
            active_project = await self.project_manager.registry.get_active_project()
            if active_project:
                logger.info(f"Switching to active project: {active_project.name}")
                await self.project_manager.switch_project(active_project.id)
        except Exception as e:
            logger.warning(f"Failed to switch to active project: {e}")

        # Auto-select default project if configured
        default_project_name = get_config().default_project_name
        if default_project_name:
            logger.info(f"Auto-selecting default project: {default_project_name}")
            try:
                from src.marcus_mcp.tools.project_management import select_project

                result = await select_project(self, {"name": default_project_name})
                if result.get("success"):
                    logger.info(
                        f"Successfully selected default project: {default_project_name}"
                    )
                else:
                    logger.info(
                        f"Default project '{default_project_name}' "
                        f"not found — will be created on first use"
                    )
            except Exception as e:
                logger.warning(
                    f"Failed to auto-select default project "
                    f"'{default_project_name}': {e}"
                )

        # CRITICAL: Force creation of all locks in the current event loop
        # This prevents "lock is bound to a different event loop" errors
        _ = self.assignment_lock  # Force lock creation
        if self.assignment_persistence:
            _ = self.assignment_persistence.lock  # Force lock creation
        if self.project_manager:
            _ = self.project_manager.lock  # Force lock creation

        # Check if we're in multi-project mode or legacy mode
        if not get_config().single_project_mode:
            # Get kanban client from project manager
            self.kanban_client = await self.project_manager.get_kanban_client()
            if self.kanban_client:
                await self.project_registry.get_active_project()
                # Initialize monitors and lease system even in multi-project mode
                await self._initialize_monitoring_systems()
        else:
            # Legacy mode - create kanban client directly
            await self.initialize_kanban()

            # Migrate to multi-project if needed
            await self._migrate_to_multi_project()

        # If still no kanban client in multi-project mode, check project registry
        if not self.kanban_client and not get_config().single_project_mode:
            # Get active project from registry
            active_project = await self.project_registry.get_active_project()
            if active_project:
                # Switch to the active project
                await self.project_manager.switch_project(active_project.id)
                self.kanban_client = await self.project_manager.get_kanban_client()

                # Initialize monitoring systems for the synced project
                if self.kanban_client:
                    await self._initialize_monitoring_systems()

                # Project synced - don't print as it interferes with MCP

        # Initialize event visualizer if available
        if self.event_visualizer:
            # EventIntegratedVisualizer doesn't have initialize method
            pass

        # Wrap AI engine for token tracking
        self.ai_engine = ai_usage_middleware.wrap_ai_provider(self.ai_engine)

        # Pattern learning components removed (API infrastructure cleanup)
        # Don't print during initialization - it interferes with MCP stdio

    async def _migrate_to_multi_project(self) -> None:
        """
        Migrate legacy configuration to multi-project format.

        Note: With auto-sync always enabled, this migration is rarely needed.
        Auto-sync discovers real projects from the Kanban provider on startup,
        so Default Project creation is skipped in most cases.
        """
        if self.kanban_client and get_config().single_project_mode:
            # Check if we've already migrated (have projects in registry)
            existing_projects = await self.project_registry.list_projects()
            if existing_projects:
                logger.info(
                    f"Skipping migration - {len(existing_projects)} "
                    "projects already in registry"
                )
                return

            # Auto-sync always runs and discovers real projects
            # Skip creating a placeholder Default Project
            logger.info(
                "Skipping Default Project creation - "
                "auto-sync will discover real projects from Kanban provider"
            )
            return

    async def _initialize_monitoring_systems(self) -> None:
        """Initialize assignment monitor and lease manager for current kanban client."""
        if not self.kanban_client:
            return

        # Initialize assignment monitor
        if self.assignment_monitor is None:
            self.assignment_monitor = AssignmentMonitor(
                self.assignment_persistence, self.kanban_client
            )
            await self.assignment_monitor.start()

        # Initialize lease management
        if self.lease_manager is None:
            from src.core.assignment_lease import (
                AssignmentLeaseManager,
                LeaseMonitor,
            )

            # Get lease configuration - project-specific or global
            lease_config: Union[TaskLeaseSettings, Dict[str, Any]] = {}

            # Check for project-specific lease config
            if hasattr(self, "project_registry") and self.project_registry:
                active_project = await self.project_registry.get_active_project()
                if active_project and hasattr(active_project, "task_lease"):
                    lease_config = active_project.task_lease

            # Fall back to global config
            if not lease_config:
                global_config = get_config()
                lease_config = global_config.task_lease

            # Extract values from dataclass or dict
            if isinstance(lease_config, TaskLeaseSettings):
                # It's a TaskLeaseSettings dataclass
                self.lease_manager = AssignmentLeaseManager(
                    self.kanban_client,
                    self.assignment_persistence,
                    default_lease_hours=lease_config.default_hours,
                    max_renewals=lease_config.max_renewals,
                    warning_threshold_hours=lease_config.warning_hours,
                    priority_multipliers=lease_config.priority_multipliers,
                    complexity_multipliers=lease_config.complexity_multipliers,
                    grace_period_minutes=lease_config.grace_period_minutes,
                    renewal_decay_factor=lease_config.renewal_decay_factor,
                    min_lease_hours=lease_config.min_lease_hours,
                    max_lease_hours=lease_config.max_lease_hours,
                    stuck_task_threshold_renewals=lease_config.stuck_threshold_renewals,
                    enable_adaptive_leases=lease_config.enable_adaptive,
                    task_list=self.project_tasks,
                    silence_multiplier=lease_config.silence_multiplier,
                )
            else:
                # It's a dict from project config
                # Fallbacks match TaskLeaseSettings aggressive defaults
                defaults = TaskLeaseSettings()
                self.lease_manager = AssignmentLeaseManager(
                    self.kanban_client,
                    self.assignment_persistence,
                    default_lease_hours=lease_config.get(
                        "default_hours", defaults.default_hours
                    ),
                    max_renewals=lease_config.get(
                        "max_renewals", defaults.max_renewals
                    ),
                    warning_threshold_hours=lease_config.get(
                        "warning_hours", defaults.warning_hours
                    ),
                    priority_multipliers=lease_config.get("priority_multipliers"),
                    complexity_multipliers=lease_config.get("complexity_multipliers"),
                    grace_period_minutes=lease_config.get(
                        "grace_period_minutes", defaults.grace_period_minutes
                    ),
                    renewal_decay_factor=lease_config.get(
                        "renewal_decay_factor", defaults.renewal_decay_factor
                    ),
                    min_lease_hours=lease_config.get(
                        "min_lease_hours", defaults.min_lease_hours
                    ),
                    max_lease_hours=lease_config.get(
                        "max_lease_hours", defaults.max_lease_hours
                    ),
                    stuck_task_threshold_renewals=lease_config.get(
                        "stuck_threshold_renewals",
                        defaults.stuck_threshold_renewals,
                    ),
                    enable_adaptive_leases=lease_config.get(
                        "enable_adaptive", defaults.enable_adaptive
                    ),
                    task_list=self.project_tasks,
                    silence_multiplier=lease_config.get(
                        "silence_multiplier", defaults.silence_multiplier
                    ),
                )
            self.lease_monitor = LeaseMonitor(self.lease_manager)

            # Wire up recovery callback so in-memory tracking is cleaned
            # when a lease is recovered. Without this, agent_tasks keeps
            # the recovered task and the assignment filter blocks other
            # agents from grabbing it.
            self.lease_manager.on_recovery_callback = self._handle_lease_recovery

            # NOTE: Don't start the monitor here. In HTTP mode,
            # this runs on a temporary event loop that's abandoned
            # when uvicorn starts. The monitor must start on
            # uvicorn's loop via ensure_lease_monitor_running().
            logger.info("Assignment lease system initialized (monitor pending)")

    def _handle_lease_recovery(self, agent_id: str, task_id: str) -> None:
        """Clean up all in-memory state when a lease is recovered.

        Invoked by ``AssignmentLeaseManager`` when it reclaims a stale
        lease.  Three state buckets must stay in sync with the lease
        manager — if any are left stale, ``request_next_task`` and
        ``report_task_progress`` disagree about ownership and the
        original agent's completed work gets stranded (issue #485).

        Parameters
        ----------
        agent_id : str
            Agent whose lease was just recovered.
        task_id : str
            Task that was reclaimed from ``agent_id``.
        """
        if agent_id in self.agent_tasks:
            del self.agent_tasks[agent_id]
            logger.info(f"Recovery cleanup: removed agent_tasks[{agent_id}]")

        # Issue #485: also clear current_tasks on the agent_status
        # entry.  request_next_task reads agent.current_tasks to decide
        # whether the agent already has work; without this clear, the
        # endpoint blocks the recovered agent from picking up new
        # tasks even though the lease manager and agent_tasks both
        # say they no longer own anything.
        agent = self.agent_status.get(agent_id)
        if agent is not None and agent.current_tasks:
            agent.current_tasks = [t for t in agent.current_tasks if t.id != task_id]
            logger.info(
                f"Recovery cleanup: removed task {task_id} from "
                f"agent_status[{agent_id}].current_tasks"
            )

        self.tasks_being_assigned.discard(task_id)
        # Reset validation retry counter so the new agent is not
        # penalised for the previous agent's failures (issue #400).
        from src.marcus_mcp.tools.task import clear_validation_retry

        clear_validation_retry(task_id)

        # #206 MVP (Codex P1 review fix): release file locks held by
        # the recovered task. Lease recovery bypasses
        # ``report_task_progress`` entirely — the lease manager resets
        # the task to TODO on the board and invokes this callback so
        # in-memory state can catch up. Without this release the
        # recovered task's declared files stay claimed indefinitely
        # and ``_find_optimal_task_original_logic`` keeps skipping
        # every contending task (including the recovered task itself
        # the next time it would be assigned), creating a stuck queue
        # after agent silence/timeouts.
        #
        # ``release`` is async but this callback is sync — schedule
        # it on the current event loop as a fire-and-forget. We are
        # invoked from inside the lease manager's async recovery
        # task so an event loop is guaranteed running. The release
        # itself is idempotent and logs internally, so a never-awaited
        # task warning is fine.
        if hasattr(self, "file_lock_registry"):
            try:
                import asyncio as _asyncio

                _asyncio.create_task(self.file_lock_registry.release(task_id))
            except RuntimeError:
                # No running event loop — should not happen given the
                # call site, but fail closed: log and move on so
                # recovery cleanup is not blocked by the registry.
                logger.warning(
                    "[#206] Could not schedule file-lock release for "
                    "recovered task %s — no running event loop",
                    task_id,
                )

    async def ensure_lease_monitor_running(self) -> None:
        """Start the lease monitor if it exists but isn't running.

        In HTTP mode, the monitor must start on uvicorn's event loop,
        not on the temporary setup loop. This method is called from
        MCP tool handlers which run on the correct loop.
        """
        if self.lease_monitor and not self.lease_monitor._running:
            logger.info("Starting lease monitor on active event loop")
            await self.lease_monitor.start()

    async def initialize_kanban(self) -> None:
        """Initialize kanban client if not already done."""
        from src.core.error_framework import ErrorContext, KanbanIntegrationError

        if not self.kanban_client:
            try:
                # Ensure configuration is loaded before creating kanban client
                self._ensure_environment_config()

                # Create kanban client
                self.kanban_client = KanbanFactory.create(self.provider)

                # Validate the client supports task creation
                if not hasattr(self.kanban_client, "create_task"):
                    raise KanbanIntegrationError(
                        board_name=self.provider,
                        operation="client_initialization",
                        context=ErrorContext(
                            operation="kanban_initialization",
                            integration_name="mcp_server",
                            custom_context={
                                "provider": self.provider,
                                "details": (
                                    f"Kanban client "
                                    f"{type(self.kanban_client).__name__} "
                                    f"does not support task creation. Expected "
                                    f"KanbanClientWithCreate or compatible."
                                ),
                            },
                        ),
                    )

                # Connect to the kanban board
                if hasattr(self.kanban_client, "connect"):
                    await self.kanban_client.connect()

                # Initialize monitoring systems
                await self._initialize_monitoring_systems()

                # Kanban client initialized successfully

            except Exception as e:
                if isinstance(e, KanbanIntegrationError):
                    raise
                raise KanbanIntegrationError(
                    board_name=self.provider,
                    operation="client_initialization",
                    context=ErrorContext(
                        operation="kanban_initialization",
                        integration_name="mcp_server",
                        custom_context={
                            "provider": self.provider,
                            "details": f"Failed to initialize kanban client: {str(e)}",
                        },
                    ),
                ) from e

    def _ensure_environment_config(self) -> None:
        """Ensure environment variables are set from config_marcus.json."""
        from src.core.error_framework import ConfigurationError, ErrorContext

        try:
            # Load from centralized config
            config = get_config()

            # Set Planka environment variables if not already set
            if config.kanban.planka_base_url and "PLANKA_BASE_URL" not in os.environ:
                os.environ["PLANKA_BASE_URL"] = config.kanban.planka_base_url
            if config.kanban.planka_email and "PLANKA_AGENT_EMAIL" not in os.environ:
                os.environ["PLANKA_AGENT_EMAIL"] = config.kanban.planka_email
            if (
                config.kanban.planka_password
                and "PLANKA_AGENT_PASSWORD" not in os.environ
            ):
                os.environ["PLANKA_AGENT_PASSWORD"] = config.kanban.planka_password
            if config.kanban.board_name and "PLANKA_PROJECT_NAME" not in os.environ:
                os.environ["PLANKA_PROJECT_NAME"] = config.kanban.board_name

            # Support GitHub
            if config.kanban.github_token and "GITHUB_TOKEN" not in os.environ:
                os.environ["GITHUB_TOKEN"] = config.kanban.github_token
            if config.kanban.github_owner and "GITHUB_OWNER" not in os.environ:
                os.environ["GITHUB_OWNER"] = config.kanban.github_owner
            if config.kanban.github_repo and "GITHUB_REPO" not in os.environ:
                os.environ["GITHUB_REPO"] = config.kanban.github_repo

            # Support Linear
            if config.kanban.linear_api_key and "LINEAR_API_KEY" not in os.environ:
                os.environ["LINEAR_API_KEY"] = config.kanban.linear_api_key
            if config.kanban.linear_team_id and "LINEAR_TEAM_ID" not in os.environ:
                os.environ["LINEAR_TEAM_ID"] = config.kanban.linear_team_id

            # Configuration loaded successfully from config_marcus.json

        except FileNotFoundError as e:
            raise ConfigurationError(
                service_name="MCP Server",
                config_type="config_marcus.json",
                missing_field="configuration file",
                context=ErrorContext(
                    operation="environment_config_loading",
                    integration_name="mcp_server",
                ),
            ) from e
        except Exception as e:
            raise ConfigurationError(
                "Failed to load environment configuration for kanban integration",
                context=ErrorContext(
                    operation="environment_config_loading",
                    integration_name="mcp_server",
                    custom_context={
                        "service_name": "MCP Server",
                        "config_type": "environment variables",
                        "missing_field": "kanban configuration",
                        "error": str(e),
                    },
                ),
            ) from e

    def log_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Log events immediately to realtime log and optionally to Events system."""
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            **data,
        }
        self.realtime_log.write(json.dumps(event) + "\n")

        # Also publish to Events system if available.
        # Guard: skip the fire-and-forget task when events is None
        # (unit test fixture sets server.events = None) or when
        # MARCUS_TEST_MODE is active (test_env_vars fixture in
        # tests/unit/conftest.py). Without this guard, the
        # create_task call outlives the test's event loop and
        # produces "Task was destroyed but it is pending" warnings
        # at teardown. See PR #323 for the original event-loop
        # contamination fix; this closes the remaining teardown gap.
        if self.events and not os.environ.get("MARCUS_TEST_MODE"):
            # Run async publish in a fire-and-forget manner
            asyncio.create_task(self._publish_event_async(event_type, data))

    async def _publish_event_async(self, event_type: str, data: Dict[str, Any]) -> None:
        """Publish events asynchronously to the Events system."""
        try:
            source = data.get("source", "marcus")
            if self.events:
                await self.events.publish(event_type, source, data)
        except Exception as e:
            # Don't let event publishing errors affect main flow
            print(f"Error publishing event: {e}", file=sys.stderr)

    async def refresh_project_state(self) -> None:
        """Refresh project state from kanban board."""
        if not self.kanban_client:
            await self.initialize_kanban()

        try:
            # Get all tasks from the board
            # CRITICAL: After subtasks are migrated, we need to update parent tasks
            # while preserving the migrated subtasks in memory
            if self.kanban_client is not None:
                parent_tasks = await self.kanban_client.get_all_tasks()

                subtask_count = (
                    len(self.subtask_manager.subtasks) if self.subtask_manager else 0
                )
                logger.info(
                    f"[DEBUG] refresh_project_state: got {len(parent_tasks)} "
                    f"parent tasks, _subtasks_migrated={self._subtasks_migrated}, "
                    f"subtask_manager exists={self.subtask_manager is not None}, "
                    f"subtask_manager.subtasks count={subtask_count}"
                )

                # Capture recovery_info from existing tasks before refresh
                # (recovery_info is in-memory only, not stored in Kanban)
                recovery_info_map: dict[str, Any] = {}
                if self.project_tasks:
                    for task in self.project_tasks:
                        if getattr(task, "recovery_info", None):
                            recovery_info_map[task.id] = task.recovery_info

                if not self._subtasks_migrated:
                    # First time: copy parent tasks to avoid mutating source
                    # Migration will append subtasks to this list
                    self.project_tasks = parent_tasks.copy()
                    logger.info(
                        f"[DEBUG] First refresh: set project_tasks to "
                        f"{len(parent_tasks)} parent tasks"
                    )
                else:
                    # After migration: preserve subtasks, update parents.
                    #
                    # Issue #667 (Option 2, non-destructive): only carry
                    # forward subtasks whose parent is still on the board.
                    # Orphaned subtasks (parent removed, or left over from a
                    # different project in a long-lived server process)
                    # otherwise accumulate in this in-memory working set,
                    # growing memory and -- because this list is fed to
                    # ``Context.analyze_dependencies`` on the
                    # ``request_next_task`` hot path -- churning the
                    # dependency-cache signature, which triggers surprise
                    # LLM inference calls.
                    #
                    # We trim the in-memory list ONLY. The durable record in
                    # ``data/marcus_state/subtasks.json`` is left untouched:
                    # Cato reads that file directly as its subtask data
                    # source, so deleting from it would blank Cato's views.
                    # Issue #672 tracks the proper fix (move subtask storage
                    # into the DB so Cato reads a contract, not this file).
                    parent_ids = {t.id for t in parent_tasks}
                    existing_subtasks, dropped_orphans = _carry_forward_active_subtasks(
                        self.project_tasks, parent_ids
                    )

                    # Combine refreshed parents with preserved subtasks
                    self.project_tasks = parent_tasks + existing_subtasks

                    msg = (
                        f"Refreshed {len(parent_tasks)} parent tasks, "
                        f"preserved {len(existing_subtasks)} subtasks"
                    )
                    if dropped_orphans:
                        msg += (
                            f", dropped {dropped_orphans} orphaned subtasks "
                            f"from the in-memory working set "
                            f"(subtasks.json unchanged)"
                        )
                    logger.info(msg)

                # v0.3.8.post1: invalidate the foundation-contract cache
                # whenever project_tasks is reassigned from kanban —
                # the foundation task set might have changed and the
                # cache would otherwise serve stale content for up to
                # the TTL. Best-effort: never block project refresh on
                # cache miss.
                try:
                    from src.marcus_mcp.tools.context import (
                        invalidate_foundation_contract_cache,
                    )

                    _pid = getattr(self, "current_project_id", None)
                    invalidate_foundation_contract_cache(str(_pid) if _pid else None)
                except Exception:  # noqa: BLE001 - never break refresh
                    pass

                # Issue #626: the dependency-inference cache is NOT
                # invalidated here. ``refresh_project_state`` runs
                # immediately before ``analyze_dependencies`` on every
                # ``request_next_task`` poll (see
                # ``src/marcus_mcp/tools/task.py`` lines 1538 and 1621);
                # explicit invalidation here would defeat the cache on
                # every call. Instead, the signature-based cache key
                # in ``src/core/context.py::_deps_cache_key`` invalidates
                # naturally whenever any task's id, name, description,
                # labels, or dependencies change between refreshes —
                # which is exactly when the inference result could be
                # stale. (Codex P1 on PR #631.)

                # Re-apply recovery_info to refreshed tasks
                if recovery_info_map:
                    restored = 0
                    for task in self.project_tasks:
                        if task.id in recovery_info_map:
                            task.recovery_info = recovery_info_map[task.id]
                            restored += 1
                    if restored:
                        logger.info(
                            f"Preserved recovery_info for "
                            f"{restored} task(s) across refresh"
                        )

            # Migrate subtasks from SubtaskManager to unified project_tasks storage
            # ONLY run migration once to avoid duplicate subtasks
            if (
                self.subtask_manager
                and self.project_tasks is not None
                and not self._subtasks_migrated
            ):
                logger.info(
                    f"[DEBUG] Before migration: project_tasks has "
                    f"{len(self.project_tasks)} tasks"
                )

                # CRITICAL FIX: Only set migration flag if we have parent tasks
                # If project_tasks is empty, migration will skip all subtasks,
                # but we need to allow retry on next refresh when parents exist
                has_parent_tasks = any(
                    not getattr(t, "is_subtask", False) for t in self.project_tasks
                )

                if has_parent_tasks:
                    self.subtask_manager.migrate_to_unified_storage(self.project_tasks)
                    self._subtasks_migrated = True
                    migrated_subtasks = sum(
                        1 for t in self.project_tasks if getattr(t, "is_subtask", False)
                    )
                    logger.info(
                        f"[DEBUG] After migration: project_tasks has "
                        f"{len(self.project_tasks)} tasks, "
                        f"subtasks: {migrated_subtasks}"
                    )
                else:
                    logger.warning(
                        "[DEBUG] Skipping migration: no parent tasks in project_tasks. "
                        "Will retry on next refresh when parent tasks are available."
                    )

                # Wire cross-parent dependencies after migration completes
                # Creates fine-grained dependencies between different parents
                try:
                    from src.marcus_mcp.coordinator import (
                        wire_cross_parent_dependencies,
                    )

                    logger.info("Wiring cross-parent dependencies...")

                    # Use cached embedding model for candidate filtering.
                    # Loaded lazily on first call; reused on subsequent calls to
                    # avoid the ~40s SentenceTransformer load on every create_project.
                    if self._embedding_model is None:
                        try:
                            from sentence_transformers import SentenceTransformer

                            self._embedding_model = SentenceTransformer(
                                "all-MiniLM-L6-v2"
                            )
                            logger.info(
                                "Loaded embedding model for dependency matching "
                                "(cached for subsequent calls)"
                            )
                        except ImportError:
                            logger.debug(
                                "sentence-transformers not available, "
                                "using LLM-only matching"
                            )
                        except Exception as e:
                            logger.warning(f"Failed to load embedding model: {e}")
                    embedding_model = self._embedding_model

                    # Wire dependencies
                    stats = await wire_cross_parent_dependencies(
                        self.project_tasks, self.ai_engine, embedding_model
                    )

                    logger.info(
                        f"Cross-parent dependency wiring complete: "
                        f"{stats.get('dependencies_created', 0)} dependencies created, "
                        f"{stats.get('subtasks_analyzed', 0)} subtasks analyzed"
                    )

                except Exception as e:
                    logger.error(
                        f"Failed to wire cross-parent dependencies: {e}", exc_info=True
                    )
                    # Don't fail the refresh if dependency wiring fails

            # Update memory system with project tasks for cascade analysis
            if self.memory and self.project_tasks:
                self.memory.update_project_tasks(self.project_tasks)

            # Update project state
            if self.project_tasks:
                total_tasks = len(self.project_tasks)
                completed_tasks = len(
                    [t for t in self.project_tasks if t.status == TaskStatus.DONE]
                )
                in_progress_tasks = len(
                    [
                        t
                        for t in self.project_tasks
                        if t.status == TaskStatus.IN_PROGRESS
                    ]
                )

                board_id = getattr(self.kanban_client, "board_id", "unknown")
                self.project_state = ProjectState(
                    board_id=board_id,
                    project_name="Current Project",  # Would need to get from board
                    total_tasks=total_tasks,
                    completed_tasks=completed_tasks,
                    in_progress_tasks=in_progress_tasks,
                    blocked_tasks=0,  # Would need to track this separately
                    progress_percent=(
                        (completed_tasks / total_tasks * 100)
                        if total_tasks > 0
                        else 0.0
                    ),
                    overdue_tasks=[],  # Would need to check due dates
                    team_velocity=0.0,  # Would need to calculate
                    risk_level=RiskLevel.LOW,  # Simplified
                    last_updated=datetime.now(timezone.utc),
                )

            # Create a JSON-serializable version of project_state
            project_state_data = None
            if self.project_state:
                project_state_data = {
                    "board_id": self.project_state.board_id,
                    "project_name": self.project_state.project_name,
                    "total_tasks": self.project_state.total_tasks,
                    "completed_tasks": self.project_state.completed_tasks,
                    "in_progress_tasks": self.project_state.in_progress_tasks,
                    "blocked_tasks": self.project_state.blocked_tasks,
                    "progress_percent": self.project_state.progress_percent,
                    "team_velocity": self.project_state.team_velocity,
                    "risk_level": self.project_state.risk_level.value,  # Enum to str
                    "last_updated": self.project_state.last_updated.isoformat(),
                }

            # Update lease manager's task list reference after refresh
            if hasattr(self, "lease_manager") and self.lease_manager:
                self.lease_manager.update_task_list(self.project_tasks)

            self.log_event(
                "project_state_refreshed",
                {
                    "task_count": len(self.project_tasks),
                    "project_state": project_state_data,
                },
            )

        except Exception as e:
            self.log_event("project_state_refresh_error", {"error": str(e)})
            raise

    def _create_fastmcp(self) -> FastMCP:
        """Create and configure FastMCP instance with agent tools by default."""
        if self._fastmcp is None:
            # Check if we should use all tools or agent tools
            use_all_tools = "--all-tools" in sys.argv

            if use_all_tools:
                self._fastmcp = FastMCP(
                    "marcus",
                    instructions=(
                        "Marcus - Multi-Agent Resource Coordination and "
                        "Understanding System (All Tools)"
                    ),
                )
                # Register all tools with FastMCP
                self._register_fastmcp_tools()
            else:
                # Default to agent tools for new users
                self._fastmcp = FastMCP(
                    "marcus-agent",
                    instructions=(
                        "Marcus - Multi-Agent Resource Coordination and "
                        "Understanding System (Agent Mode)"
                    ),
                )
                # Register only agent tools
                self._register_endpoint_tools(self._fastmcp, "agent")

        return self._fastmcp

    def _create_endpoint_app(self, endpoint_type: str) -> FastMCP:
        """Create FastMCP instance for a specific endpoint type."""
        if endpoint_type not in self._endpoint_apps:
            # Create unique app name for each endpoint
            app_name = f"marcus-{endpoint_type}"
            instructions = {
                "human": "Marcus MCP Server - Human Developer Tools",
                "agent": "Marcus MCP Server - Autonomous Agent Tools",
                "analytics": "Marcus MCP Server - Analytics & Monitoring Tools",
            }.get(endpoint_type, "Marcus MCP Server")

            # Create FastMCP instance
            app = FastMCP(app_name, instructions=instructions)

            # Register only tools allowed for this endpoint
            self._register_endpoint_tools(app, endpoint_type)

            self._endpoint_apps[endpoint_type] = app

        return self._endpoint_apps[endpoint_type]

    def _register_fastmcp_tools(self) -> None:
        """Register all tools with FastMCP instance."""
        # Import only what we need for avoiding duplicates
        # Actual imports happen inside each function to avoid conflicts

        if self._fastmcp is None:
            raise RuntimeError("FastMCP instance not initialized")

        # Store reference to self for closures
        server = self

        # Register core tools with proper signatures
        @self._fastmcp.tool()  # type: ignore[misc]
        async def ping(echo: str = "") -> Dict[str, Any]:
            """Check Marcus status and connectivity."""
            from .tools.system import ping as ping_impl

            return await ping_impl(echo=echo, state=server)

        @self._fastmcp.tool()  # type: ignore[misc]
        async def register_agent(
            agent_id: str,
            name: str,
            role: str,
            skills: List[str] = [],
            project_id: str = "",
        ) -> Dict[str, Any]:
            """Register a new agent with the Marcus system."""
            from src.logging.mcp_tool_logger import log_mcp_tool_response

            from .tools.agent import register_agent as impl

            result = await impl(
                agent_id=agent_id,
                name=name,
                role=role,
                skills=skills,
                project_id=project_id,
                state=server,
            )

            # Log MCP tool response
            log_mcp_tool_response(
                tool_name="register_agent",
                arguments={
                    "agent_id": agent_id,
                    "name": name,
                    "role": role,
                    "skills": skills,
                    "project_id": project_id,
                },
                response=result,
            )

            return result

        @self._fastmcp.tool()  # type: ignore[misc]
        async def get_agent_status(agent_id: str) -> Dict[str, Any]:
            """Get status and current assignment for an agent."""
            from .tools.agent import get_agent_status as impl

            return await impl(agent_id=agent_id, state=server)

        @self._fastmcp.tool()  # type: ignore[misc]
        async def request_next_task(agent_id: str) -> Dict[str, Any]:
            """Request the next optimal task assignment for an agent."""
            return await server._call_tool_via_handler_as_dict(
                "request_next_task", {"agent_id": agent_id}
            )

        @self._fastmcp.tool()  # type: ignore[misc]
        async def report_task_progress(
            agent_id: str,
            task_id: str,
            status: str,
            progress: int = 0,
            message: str = "",
            start_command: Optional[str] = None,
            readiness_probe: Optional[str] = None,
            verifications: Optional[List[Dict[str, Any]]] = None,
            evidence: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]:
            """Report progress on a task.

            For integration verification tasks (type:integration
            label), the agent MUST declare either ``verifications``
            (preferred, #523 Slice B) OR ``start_command`` (legacy)
            when marking the task complete.  Marcus runs each
            declared command as a subprocess and rejects the
            completion if any exits non-zero.  ``verifications``
            takes precedence over ``start_command`` when both are
            provided.

            ``evidence`` (issue #677): for app types Marcus classified
            with a behavior-evidence contract (web, data pipeline, CLI,
            library, API service, ML), the agent must additionally
            submit behavior evidence captured by actually RUNNING the
            assembled product (e.g. web → ``dom`` + ``console_errors``;
            pipeline → ``output``; CLI → ``exit_code`` + ``stdout``).
            Marcus judges the submitted evidence against the per-type
            bar — a build that exits 0 and a server that returns 200 do
            not pass.  See the report_task_progress docstring in
            tools/task.py for the full contract and examples.
            """
            from .tools.task import report_task_progress as impl

            return await impl(
                agent_id=agent_id,
                task_id=task_id,
                status=status,
                progress=progress,
                message=message,
                state=server,
                start_command=start_command,
                readiness_probe=readiness_probe,
                verifications=verifications,
                evidence=evidence,
            )

        @self._fastmcp.tool()  # type: ignore[misc]
        async def report_blocker(
            agent_id: str,
            task_id: str,
            blocker_description: str,
            severity: str = "medium",
        ) -> Dict[str, Any]:
            """Report a blocker on a task."""
            from .tools.task import report_blocker as impl

            return await impl(
                agent_id=agent_id,
                task_id=task_id,
                blocker_description=blocker_description,
                severity=severity,
                state=server,
            )

        @self._fastmcp.tool()  # type: ignore[misc]
        async def request_task_redo(
            agent_id: str,
            task_id: str,
            reason: str,
        ) -> Dict[str, Any]:
            """Send a completed task back to the board for a fresh agent to redo."""
            from .tools.task import request_task_redo as impl

            return await impl(
                agent_id=agent_id,
                task_id=task_id,
                reason=reason,
                state=server,
            )

        @self._fastmcp.tool()  # type: ignore[misc]
        async def get_all_board_tasks(board_id: str, project_id: str) -> Dict[str, Any]:
            """Get all tasks from a specific Planka board for validation/inspection."""
            from .tools.task import get_all_board_tasks as impl

            return await impl(
                board_id=board_id,
                project_id=project_id,
                state=server,
            )

        @self._fastmcp.tool()  # type: ignore[misc]
        async def get_project_status() -> Dict[str, Any]:
            """Get current project status and metrics."""
            from .tools.project import get_project_status as impl

            result = await impl(state=server)
            return (
                dict(result)
                if isinstance(result, dict)
                else {"error": "Invalid response format"}
            )

        @self._fastmcp.tool()  # type: ignore[misc]
        async def create_project(
            description: str,
            project_name: str,
            options: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]:
            """Create a complete project from natural language description."""
            from .tools.nlp import create_project as impl

            return await impl(
                description=description,
                project_name=project_name,
                options=options,
                state=server,
            )

    def _register_endpoint_tools(self, app: FastMCP, endpoint_type: str) -> None:
        """Register tools for a specific endpoint based on allowed tools."""
        # Get allowed tools for this endpoint
        allowed_tools = get_tools_for_endpoint(endpoint_type)

        # Store reference to self for closures
        server = self

        # Register each allowed tool
        if "ping" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def ping(echo: str = "") -> Dict[str, Any]:
                """Check Marcus status and connectivity."""
                from .tools.system import ping as ping_impl

                return await ping_impl(echo=echo, state=server)

        if "authenticate" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def authenticate(
                client_id: str,
                client_type: str,
                role: str,
                metadata: Optional[Dict[str, Any]] = None,
            ) -> Dict[str, Any]:
                """Authenticate a client with role-based access."""
                from .tools.auth import authenticate as auth_impl

                return await auth_impl(
                    client_id=client_id,
                    client_type=client_type,
                    role=role,
                    metadata=metadata,
                    state=server,
                )

        if "register_agent" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def register_agent(
                agent_id: str,
                name: str,
                role: str,
                skills: List[str] = [],
                project_id: str = "",
            ) -> Dict[str, Any]:
                """Register a new agent with the Marcus system."""
                from src.logging.mcp_tool_logger import log_mcp_tool_response

                from .tools.agent import register_agent as impl

                result = await impl(
                    agent_id=agent_id,
                    name=name,
                    role=role,
                    skills=skills,
                    project_id=project_id,
                    state=server,
                )

                # Log MCP tool response
                log_mcp_tool_response(
                    tool_name="register_agent",
                    arguments={
                        "agent_id": agent_id,
                        "name": name,
                        "role": role,
                        "skills": skills,
                        "project_id": project_id,
                    },
                    response=result,
                )

                return result

        if "get_agent_status" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_agent_status(agent_id: str) -> Dict[str, Any]:
                """Get status and current assignment for an agent."""
                from .tools.agent import get_agent_status as impl

                return await impl(agent_id=agent_id, state=server)

        if "request_next_task" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def request_next_task(agent_id: str) -> Dict[str, Any]:
                """Request the next optimal task assignment for an agent."""
                from src.logging.mcp_tool_logger import log_mcp_tool_response

                final_result = await server._call_tool_via_handler_as_dict(
                    "request_next_task", {"agent_id": agent_id}
                )

                # Log MCP tool response
                log_mcp_tool_response(
                    tool_name="request_next_task",
                    arguments={"agent_id": agent_id},
                    response=final_result,
                )

                return final_result

        if "report_task_progress" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def report_task_progress(
                agent_id: str,
                task_id: str,
                status: str,
                progress: int = 0,
                message: str = "",
                start_command: Optional[str] = None,
                readiness_probe: Optional[str] = None,
                verifications: Optional[List[Dict[str, Any]]] = None,
                evidence: Optional[Dict[str, Any]] = None,
            ) -> Dict[str, Any]:
                """Report progress on a task.

                For integration verification tasks (type:integration
                label), the agent MUST declare either ``verifications``
                (preferred, #523 Slice B) OR ``start_command`` (legacy)
                when marking the task complete.  Marcus runs each
                declared command as a subprocess and rejects the
                completion if any exits non-zero.  ``verifications``
                takes precedence over ``start_command`` when both are
                provided.

                ``evidence`` (issue #677): for app types with a
                behavior-evidence contract, submit evidence captured by
                running the assembled product (web → ``dom`` +
                ``console_errors``; pipeline → ``output``; CLI →
                ``exit_code`` + ``stdout``).  Marcus judges the evidence
                against the per-type bar.  See the report_task_progress
                docstring in tools/task.py for the full contract.
                """
                from src.logging.mcp_tool_logger import log_mcp_tool_response

                from .tools.task import report_task_progress as impl

                result = await impl(
                    agent_id=agent_id,
                    task_id=task_id,
                    status=status,
                    progress=progress,
                    message=message,
                    state=server,
                    start_command=start_command,
                    readiness_probe=readiness_probe,
                    verifications=verifications,
                    evidence=evidence,
                )

                # Log MCP tool response
                log_mcp_tool_response(
                    tool_name="report_task_progress",
                    arguments={
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "status": status,
                        "progress": progress,
                        "message": message,
                        "start_command": start_command,
                        "readiness_probe": readiness_probe,
                        "verifications": verifications,
                        "evidence": evidence,
                    },
                    response=result,
                )

                return result

        if "report_blocker" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def report_blocker(
                agent_id: str,
                task_id: str,
                blocker_description: str,
                severity: str = "medium",
            ) -> Dict[str, Any]:
                """Report a blocker on a task."""
                from src.logging.mcp_tool_logger import log_mcp_tool_response

                from .tools.task import report_blocker as impl

                result = await impl(
                    agent_id=agent_id,
                    task_id=task_id,
                    blocker_description=blocker_description,
                    severity=severity,
                    state=server,
                )

                # Log MCP tool response
                log_mcp_tool_response(
                    tool_name="report_blocker",
                    arguments={
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "blocker_description": blocker_description,
                        "severity": severity,
                    },
                    response=result,
                )

                return result

        if "request_task_redo" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def request_task_redo(
                agent_id: str,
                task_id: str,
                reason: str,
            ) -> Dict[str, Any]:
                """Send a completed task back to the board for a fresh agent to redo."""
                from src.logging.mcp_tool_logger import log_mcp_tool_response

                from .tools.task import request_task_redo as impl

                result = await impl(
                    agent_id=agent_id,
                    task_id=task_id,
                    reason=reason,
                    state=server,
                )

                # Log MCP tool response
                log_mcp_tool_response(
                    tool_name="request_task_redo",
                    arguments={
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "reason": reason,
                    },
                    response=result,
                )

                return result

        if "unassign_task" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def unassign_task(
                task_id: str, agent_id: Optional[str] = None
            ) -> Dict[str, Any]:
                """Manually unassign a task from an agent."""
                from .tools.task import unassign_task as impl

                return await impl(
                    task_id=task_id,
                    agent_id=agent_id,
                    state=server,
                )

        if "get_all_board_tasks" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_all_board_tasks(
                board_id: str, project_id: str
            ) -> Dict[str, Any]:
                """Get all tasks from a specific Planka board."""
                from .tools.task import get_all_board_tasks as impl

                return await impl(
                    board_id=board_id,
                    project_id=project_id,
                    state=server,
                )

        if "get_project_status" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_project_status() -> Dict[str, Any]:
                """Get current project status and metrics."""
                from .tools.project import get_project_status as impl

                return await impl(state=server)  # type: ignore[no-any-return]

        if "create_project" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def create_project(
                description: str,
                project_name: str,
                options: Optional[Dict[str, Any]] = None,
            ) -> Dict[str, Any]:
                """
                Create a complete project from natural language description.

                Automatically discovers existing projects by name or creates new ones.
                Breaks down description into tasks with priorities and dependencies.

                Parameters
                ----------
                description : str
                    Natural language project description
                project_name : str
                    Name for the project
                options : Optional[Dict[str, Any]]
                    Optional settings:
                    - mode: "new_project" (default) | "auto" | "select_project"
                      - new_project: Force create new (skip discovery) [DEFAULT]
                      - auto: Find existing or create new
                      - select_project: Find and select existing (no task creation)
                    - project_id: Use specific project ID (skips discovery)
                    - complexity: "prototype" | "standard" | "enterprise"
                    - provider: "planka" | "github" | "linear"

                Returns
                -------
                Dict[str, Any]
                    Dict with success, project_id, tasks_created, and board info.
                """
                from src.logging.mcp_tool_logger import log_mcp_tool_response

                from .tools.nlp import create_project as impl

                result = await impl(
                    description=description,
                    project_name=project_name,
                    options=options,
                    state=server,
                )

                # Log MCP tool response
                log_mcp_tool_response(
                    tool_name="create_project",
                    arguments={
                        "description": description,
                        "project_name": project_name,
                        "options": options,
                    },
                    response=result,
                )

                return result

        if "create_tasks" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def create_tasks(
                task_description: str,
                board_name: Optional[str] = None,
                project_name: Optional[str] = None,
                options: Optional[Dict[str, Any]] = None,
            ) -> Dict[str, Any]:
                """
                Create tasks on an existing project and board using natural language.

                This tool is similar to create_project but works on existing
                projects. It can either use an existing board or create a new
                board within the project.

                Parameters
                ----------
                task_description : str
                    Natural language description of the tasks to create
                board_name : Optional[str]
                    Board name to use. If not provided, uses the active project's board.
                    If the board doesn't exist, it will be created.
                project_name : Optional[str]
                    Project name to use. If not provided, uses the active project.
                options : Optional[Dict[str, Any]]
                    Optional configuration:
                    - complexity: "prototype", "standard" (default), "enterprise"
                    - team_size: 1-20 for estimation (default: 1)
                    - create_board: bool - Force create new board (default: False)

                Returns
                -------
                Dict[str, Any]
                    On success: {success, tasks_created, board: {project_id,
                    board_id, board_name}}. On error: {success: False, error: str}
                """
                from .tools.nlp import create_tasks as impl

                return await impl(
                    task_description=task_description,
                    board_name=board_name,
                    project_name=project_name,
                    options=options,
                    state=server,
                )

        if "diagnose_project" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def diagnose_project() -> Dict[str, Any]:
                """
                Run comprehensive diagnostics on the current project.

                Analyzes why tasks aren't being assigned or completed, including:
                - Circular dependencies
                - Bottleneck tasks blocking others
                - Missing dependencies
                - Long dependency chains

                Returns
                -------
                Dict[str, Any]
                    Diagnostic report with issues and actionable recommendations
                """
                from .tools.diagnostics import diagnose_project as impl

                return await impl(state=server)

        if "capture_stall_snapshot" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def capture_stall_snapshot(
                include_conversation_hours: int = 24,
            ) -> Dict[str, Any]:
                """
                Capture comprehensive snapshot when project development stalls.

                This tool captures:
                - Complete diagnostic report with all task statuses
                - Conversation history leading up to the stall
                - Task completion timeline to detect anomalies
                - Dependency lock visualization (ASCII diagram)
                - Early/anomalous task completions (e.g., "Project Success" too early)
                - Pattern detection (repeated failures, "no task" loops)
                - Actionable recommendations

                Use this when:
                - Development has stalled (no tasks being assigned)
                - "No tasks available" but tasks exist
                - Agents repeatedly report blockers
                - Tasks completing in wrong order

                Parameters
                ----------
                include_conversation_hours : int
                    Hours of conversation history to include (default: 24)

                Returns
                -------
                Dict[str, Any]
                    Complete stall snapshot saved to file with analysis
                """
                from .tools.diagnostics import capture_stall_snapshot as impl

                return await impl(
                    state=server,
                    include_conversation_hours=include_conversation_hours,
                )

        if "replay_snapshot_conversations" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def replay_snapshot_conversations(
                snapshot_file: str,
            ) -> Dict[str, Any]:
                """
                Replay and analyze conversations from a stall snapshot.

                Analyzes conversation patterns to identify what led to the stall,
                including error patterns, repeated failures, and activity gaps.

                Parameters
                ----------
                snapshot_file : str
                    Path to the stall snapshot JSON file
                    (usually in logs/stall_snapshots/)

                Returns
                -------
                Dict[str, Any]
                    Conversation analysis with timeline and key events
                """
                from .tools.diagnostics import replay_snapshot_conversations as impl

                return await impl(snapshot_file=snapshot_file)

        if "list_projects" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def list_projects() -> List[Dict[str, Any]]:
                """List all available projects."""
                from .tools.project_management import list_projects as impl

                return await impl(server, {})

        if "select_project" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def select_project(
                name: Optional[str] = None, project_id: Optional[str] = None
            ) -> Dict[str, Any]:
                """Select an existing project to work on."""
                from .tools.project_management import select_project as impl

                return await impl(server, {"name": name, "project_id": project_id})

        if "discover_planka_projects" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def discover_planka_projects(
                auto_sync: bool = False,
            ) -> Dict[str, Any]:
                """
                Discover all projects from Planka automatically.

                Optionally auto-sync them into Marcus's registry.
                """
                from .tools.project_management import discover_planka_projects as impl

                return await impl(server, {"auto_sync": auto_sync})

        if "sync_projects" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def sync_projects(projects: List[Dict[str, Any]]) -> Dict[str, Any]:
                """Sync projects from Planka/provider into Marcus registry."""
                from .tools.project_management import sync_projects as impl

                return await impl(server, {"projects": projects})

        if "switch_project" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def switch_project(project_name: str) -> Dict[str, Any]:
                """Switch to a different project."""
                from .tools.project_management import switch_project as impl

                return await impl(server, {"project_name": project_name})

        if "get_current_project" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_current_project() -> Dict[str, Any]:
                """Get the currently active project."""
                from .tools.project_management import get_current_project as impl

                return await impl(server, {})

        if "add_feature" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def add_feature(
                description: str,
                context: Optional[Dict[str, Any]] = None,
            ) -> Dict[str, Any]:
                """Add a feature to existing project using natural language."""
                from .tools.nlp import add_feature as impl

                return await impl(
                    feature_description=description,
                    integration_point="current",
                    state=server,
                )

        if "get_usage_report" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_usage_report(
                start_date: Optional[str] = None,
                end_date: Optional[str] = None,
                group_by: str = "day",
            ) -> Dict[str, Any]:
                """Generate usage analytics and audit reports."""
                from .tools.audit_tools import get_usage_report as impl

                return await impl(
                    days=7,
                    state=server,
                )

        # Experiment tracking tools
        if "start_experiment" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def start_experiment(
                experiment_name: str,
                board_id: Optional[str] = None,
                project_id: Optional[str] = None,
                run_name: Optional[str] = None,
                tracking_interval: int = 30,
                params: Optional[Dict[str, Any]] = None,
                tags: Optional[Dict[str, str]] = None,
            ) -> Dict[str, Any]:
                """Start a live experiment with MLflow tracking."""
                from .tools.experiments import start_experiment as impl

                return await impl(
                    experiment_name=experiment_name,
                    board_id=board_id,
                    project_id=project_id,
                    run_name=run_name,
                    tracking_interval=tracking_interval,
                    params=params,
                    tags=tags,
                    state=self,
                )

        if "end_experiment" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def end_experiment() -> Dict[str, Any]:
                """End the current experiment and finalize results."""
                from .tools.experiments import end_experiment as impl

                return await impl()

        if "get_experiment_status" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_experiment_status() -> Dict[str, Any]:
                """Get current experiment status and project task counts.

                Returns a dict with lifecycle signals, run metadata,
                and kanban-sourced task counts.

                Lifecycle signals:
                - experiment_started (bool): True once start_experiment
                  has been called and a monitor is active. False during
                  the startup window between create_project and
                  start_experiment. Agents must NOT exit while this is
                  False — the experiment hasn't begun yet.
                - is_running (bool): True while the experiment is
                  active, False once it has finished. Combine with
                  experiment_started to distinguish three states:
                    * not_started=False, is_running=False → startup
                      window. Wait and re-poll.
                    * started=True,  is_running=True  → active.
                    * started=True,  is_running=False → finished. Exit.

                Kanban truth task counts (present when started=True):
                - total_tasks, completed_tasks, in_progress_tasks,
                  backlog_tasks, blocked_tasks (all int).

                Computing project progress:
                    percent = round(100 * completed_tasks / total_tasks, 1)
                    project_done = (
                        in_progress_tasks == 0
                        and (completed_tasks + blocked_tasks) == total_tasks
                    )

                Note: blocked_tasks count toward "done" because Marcus
                treats a blocked task as terminal — the project should
                not stall waiting for it. Marcus uses the same formula
                in LiveExperimentMonitor._check_completion to flip
                is_running to false, so reading is_running is equivalent
                to evaluating the formula yourself.

                Run metadata (present when started=True):
                - run_name, experiment_name, registered_agents.

                Observability counters (event tallies for MLflow):
                - task_assignments, task_completions, blockers_reported,
                  artifacts_created, decisions_logged, context_requests.
                  These are not project totals; use the kanban-truth
                  fields above for completion or progress math.
                """
                from .tools.experiments import get_experiment_status as impl

                return await impl()

        if "query_project_history" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def query_project_history(
                project_id: str,
                query_type: str,
                status: Optional[str] = None,
                agent_id: Optional[str] = None,
                task_id: Optional[str] = None,
                artifact_type: Optional[str] = None,
                affecting_task_id: Optional[str] = None,
                event_type: Optional[str] = None,
                keyword: Optional[str] = None,
                start_time: Optional[str] = None,
                end_time: Optional[str] = None,
            ) -> Dict[str, Any]:
                """
                Query project history with flexible filtering.

                Unified tool for querying project execution history across
                tasks, decisions, artifacts, agents, timeline, and conversations.

                Query Types:
                - summary: Get project summary statistics
                - tasks: Find tasks (filter by status, agent_id, or timerange)
                - blocked_tasks: Find tasks with blockers
                - task_dependencies: Get dependency chain (requires task_id)
                - decisions: Find decisions (filter by task_id, agent_id, or
                  affecting_task_id)
                - artifacts: Find artifacts (filter by task_id, artifact_type,
                  or agent_id)
                - agent_history: Get agent history (requires agent_id)
                - agent_metrics: Get agent performance metrics (requires agent_id)
                - timeline: Search timeline events (filter by event_type, agent_id,
                  task_id, or timerange)
                - conversations: Search conversations (filter by keyword, agent_id,
                  or task_id)

                Parameters
                ----------
                project_id : str
                    Project to query
                query_type : str
                    Type of query (see above for options)
                status : Optional[str]
                    Filter by task status (for tasks query)
                agent_id : Optional[str]
                    Filter by agent ID (for tasks/decisions/artifacts/timeline/
                    conversations queries, or required for agent_history/agent_metrics)
                task_id : Optional[str]
                    Filter by task ID (for decisions/artifacts/timeline/conversations,
                    or required for task_dependencies)
                artifact_type : Optional[str]
                    Filter by artifact type (for artifacts query)
                affecting_task_id : Optional[str]
                    Find decisions affecting this task (for decisions query)
                event_type : Optional[str]
                    Filter by event type (for timeline query)
                keyword : Optional[str]
                    Search keyword (for conversations query)
                start_time : Optional[str]
                    Start of time range in ISO format (for tasks/timeline queries)
                end_time : Optional[str]
                    End of time range in ISO format (for tasks/timeline queries)

                Returns
                -------
                Dict[str, Any]
                    Query results with success status and data

                Examples
                --------
                Get project summary:
                >>> query_project_history("proj123", "summary")

                Find completed tasks:
                >>> query_project_history("proj123", "tasks", status="completed")

                Find decisions by agent:
                >>> query_project_history("proj123", "decisions", agent_id="agent1")

                Search conversations for keyword:
                >>> query_project_history("proj123", "conversations", keyword="API")
                """
                from .tools.history import query_project_history as impl

                # Build filters dict from optional parameters
                filters = {}
                if status is not None:
                    filters["status"] = status
                if agent_id is not None:
                    filters["agent_id"] = agent_id
                if task_id is not None:
                    filters["task_id"] = task_id
                if artifact_type is not None:
                    filters["artifact_type"] = artifact_type
                if affecting_task_id is not None:
                    filters["affecting_task_id"] = affecting_task_id
                if event_type is not None:
                    filters["event_type"] = event_type
                if keyword is not None:
                    filters["keyword"] = keyword
                if start_time is not None:
                    filters["start_time"] = start_time
                if end_time is not None:
                    filters["end_time"] = end_time

                return await impl(
                    project_id=project_id,
                    query_type=query_type,
                    state=server,
                    **filters,
                )

        if "list_project_history_files" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def list_project_history_files() -> Dict[str, Any]:
                """
                List all projects with available history data.

                Scans the project history storage directory to find all projects
                that have recorded history data (decisions, artifacts, snapshots).

                Returns
                -------
                Dict[str, Any]
                    Dict with success status and list of projects with history:
                    - projects: List of project dicts with:
                      - project_id: Project identifier
                      - project_name: Human-readable project name
                      - has_decisions: Whether decision history exists
                      - has_artifacts: Whether artifact history exists
                      - has_snapshot: Whether snapshot exists
                      - last_updated: ISO timestamp of last update
                    - count: Number of projects found

                Examples
                --------
                >>> list_project_history_files()
                {
                    "success": True,
                    "projects": [
                        {
                            "project_id": "proj123",
                            "project_name": "My App",
                            "has_decisions": True,
                            "has_artifacts": True,
                            "has_snapshot": False,
                            "last_updated": "2025-11-07T12:00:00Z"
                        }
                    ],
                    "count": 1
                }
                """
                from .tools.history import list_project_history_files as impl

                return await impl(state=server)

        if "get_task_context" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_task_context(task_id: str) -> Dict[str, Any]:
                """Get the full context for a specific task."""
                from src.logging.mcp_tool_logger import log_mcp_tool_response

                from .tools.context import get_task_context as impl

                result = await impl(task_id=task_id, state=server)

                # Log MCP tool response
                log_mcp_tool_response(
                    tool_name="get_task_context",
                    arguments={"task_id": task_id},
                    response=result,
                )

                return result

        if "log_decision" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def log_decision(
                agent_id: str,
                task_id: str,
                decision: str,
            ) -> Dict[str, Any]:
                """Log an architectural decision."""
                from src.logging.mcp_tool_logger import log_mcp_tool_response

                from .tools.context import log_decision as impl

                result = await impl(
                    agent_id=agent_id,
                    task_id=task_id,
                    decision=decision,
                    state=server,
                )

                # Log MCP tool response
                log_mcp_tool_response(
                    tool_name="log_decision",
                    arguments={
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "decision": decision,
                    },
                    response=result,
                )

                return result

        if "log_artifact" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def log_artifact(
                task_id: str,
                filename: str,
                content: Union[str, Dict[str, Any], List[Any]],
                artifact_type: str,
                project_root: str,
                description: str = "",
                location: Optional[str] = None,
            ) -> Dict[str, Any]:
                """
                Store an artifact with smart location management.

                Marcus accepts ANY artifact type, making it domain-agnostic.
                Use domain-specific types like "podcast-script", "research",
                "video-storyboard", "marketing-copy", etc.

                Standard types (with predefined locations):
                - specification → docs/specifications/
                - api → docs/api/
                - design → docs/design/
                - architecture → docs/architecture/
                - documentation → docs/
                - reference → docs/references/
                - temporary → tmp/artifacts/

                Custom types:
                - Any other type → docs/artifacts/ (default fallback)

                Parameters
                ----------
                task_id : str
                    The current task ID
                filename : str
                    Name for the artifact file
                content : str or dict or list
                    The artifact content to store. A dict or list is
                    serialized to a JSON string; a str is stored as-is.
                artifact_type : str
                    Type of artifact (any string accepted - use descriptive names
                    like "podcast-script", "research", "video-storyboard", etc.)
                project_root : str
                    REQUIRED: Absolute path to the project root directory where
                    artifacts will be created. All agents working on the same
                    project MUST use the same project_root path.
                    Example: /Users/username/projects/my-app
                description : str, optional
                    Description of the artifact
                location : str, optional
                    Override the default location with a custom relative path

                Returns
                -------
                Dict[str, Any]
                    Result with artifact location and storage details
                """
                from src.logging.mcp_tool_logger import log_mcp_tool_response

                from .tools.attachment import log_artifact as impl

                result = await impl(
                    task_id=task_id,
                    filename=filename,
                    content=content,
                    artifact_type=artifact_type,
                    project_root=project_root,
                    description=description,
                    location=location,
                    state=server,
                )

                # Log MCP tool response
                log_mcp_tool_response(
                    tool_name="log_artifact",
                    arguments={
                        "task_id": task_id,
                        "filename": filename,
                        "content": content,
                        "artifact_type": artifact_type,
                        "project_root": project_root,
                        "description": description,
                        "location": location,
                    },
                    response=result,
                )

                return result

        if "check_task_dependencies" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def check_task_dependencies(task_id: str) -> Dict[str, Any]:
                """Check dependencies for a specific task."""
                from .tools.board_health import check_task_dependencies as impl

                return await impl(task_id=task_id, state=server)

        # Prediction and AI intelligence tools
        if "predict_completion_time" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def predict_completion_time(
                project_id: Optional[str] = None,
                include_confidence: bool = True,
            ) -> Dict[str, Any]:
                """Predict project completion time with confidence intervals."""
                from .tools.predictions import predict_completion_time as impl

                return await impl(
                    project_id=project_id,
                    include_confidence=include_confidence,
                    state=server,
                )

        if "predict_task_outcome" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def predict_task_outcome(
                task_id: str,
                agent_id: Optional[str] = None,
            ) -> Dict[str, Any]:
                """Predict the outcome of a task assignment."""
                from .tools.predictions import predict_task_outcome as impl

                return await impl(
                    task_id=task_id,
                    agent_id=agent_id,
                    state=server,
                )

        if "predict_blockage_probability" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def predict_blockage_probability(
                task_id: str,
                include_mitigation: bool = True,
            ) -> Dict[str, Any]:
                """Predict probability of task becoming blocked."""
                from .tools.predictions import predict_blockage_probability as impl

                return await impl(
                    task_id=task_id,
                    include_mitigation=include_mitigation,
                    state=server,
                )

        if "predict_cascade_effects" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def predict_cascade_effects(
                task_id: str,
                delay_days: int = 1,
            ) -> Dict[str, Any]:
                """Predict cascade effects of task delays."""
                from .tools.predictions import predict_cascade_effects as impl

                return await impl(
                    task_id=task_id,
                    delay_days=delay_days,
                    state=server,
                )

        if "get_task_assignment_score" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_task_assignment_score(
                task_id: str,
                agent_id: str,
            ) -> Dict[str, Any]:
                """Get assignment fitness score for agent-task pairing."""
                from .tools.predictions import get_task_assignment_score as impl

                return await impl(
                    task_id=task_id,
                    agent_id=agent_id,
                    state=server,
                )

        # Analytics and metrics tools
        if "get_system_metrics" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_system_metrics(
                time_window: str = "1h",
            ) -> Dict[str, Any]:
                """Get current system-wide performance metrics."""
                from .tools.analytics import get_system_metrics as impl

                return await impl(
                    time_window=time_window,
                    state=server,
                )

        if "get_agent_metrics" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_agent_metrics(
                agent_id: str,
                time_window: str = "7d",
            ) -> Dict[str, Any]:
                """Get performance metrics for a specific agent."""
                from .tools.analytics import get_agent_metrics as impl

                return await impl(
                    agent_id=agent_id,
                    time_window=time_window,
                    state=server,
                )

        if "get_project_metrics" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_project_metrics(
                project_id: Optional[str] = None,
                time_window: str = "7d",
            ) -> Dict[str, Any]:
                """Get metrics for a project including velocity and health."""
                from .tools.analytics import get_project_metrics as impl

                return await impl(
                    project_id=project_id,
                    time_window=time_window,
                    state=server,
                )

        if "get_task_metrics" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_task_metrics(
                time_window: str = "30d",
                group_by: str = "status",
            ) -> Dict[str, Any]:
                """Get aggregated task metrics grouped by various fields."""
                from .tools.analytics import get_task_metrics as impl

                return await impl(
                    time_window=time_window,
                    group_by=group_by,
                    state=server,
                )

        # Code production metrics tools
        if "get_code_metrics" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_code_metrics(
                agent_id: str,
                start_date: Optional[str] = None,
                end_date: Optional[str] = None,
            ) -> Dict[str, Any]:
                """Get code production metrics for a specific agent."""
                from .tools.code_metrics import get_code_metrics as impl

                return await impl(
                    agent_id=agent_id,
                    start_date=start_date,
                    end_date=end_date,
                    state=server,
                )

        if "get_repository_metrics" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_repository_metrics(
                repository: str,
                time_window: str = "7d",
            ) -> Dict[str, Any]:
                """Get code metrics for an entire repository."""
                from .tools.code_metrics import get_repository_metrics as impl

                return await impl(
                    repository=repository,
                    time_window=time_window,
                    state=server,
                )

        if "get_code_review_metrics" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_code_review_metrics(
                agent_id: Optional[str] = None,
                time_window: str = "7d",
            ) -> Dict[str, Any]:
                """Get code review activity and participation metrics."""
                from .tools.code_metrics import get_code_review_metrics as impl

                return await impl(
                    agent_id=agent_id,
                    time_window=time_window,
                    state=server,
                )

        if "get_code_quality_metrics" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_code_quality_metrics(
                repository: str,
                branch: str = "main",
            ) -> Dict[str, Any]:
                """Get code quality metrics from static analysis."""
                from .tools.code_metrics import get_code_quality_metrics as impl

                return await impl(
                    repository=repository,
                    branch=branch,
                    state=server,
                )

        # Pipeline tools removed - all pipeline functionality moved to Cato

        # Project management tools
        if "add_project" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def add_project(
                name: str,
                provider: str,
                config: Dict[str, Any],
                tags: List[str] = [],
                make_active: bool = True,
            ) -> Dict[str, Any]:
                """Add a new project configuration."""
                from .tools.project_management import add_project as impl

                return await impl(
                    server,
                    {
                        "name": name,
                        "provider": provider,
                        "config": config,
                        "tags": tags,
                        "make_active": make_active,
                    },
                )

        if "remove_project" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def remove_project(
                project_id: str, confirm: bool = False
            ) -> Dict[str, Any]:
                """Remove a project from the registry."""
                from .tools.project_management import remove_project as impl

                return await impl(
                    server,
                    {"project_id": project_id, "confirm": confirm},
                )

        if "update_project" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def update_project(
                project_id: str,
                name: Optional[str] = None,
                tags: Optional[List[str]] = None,
                config: Optional[Dict[str, Any]] = None,
            ) -> Dict[str, Any]:
                """Update project configuration."""
                from .tools.project_management import update_project as impl

                return await impl(
                    server,
                    {
                        "project_id": project_id,
                        "name": name,
                        "tags": tags,
                        "config": config,
                    },
                )

        # Agent management tools
        if "list_registered_agents" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def list_registered_agents() -> Dict[str, Any]:
                """List all registered agents."""
                from .tools.agent import list_registered_agents as impl

                return await impl(state=server)

        if "check_assignment_health" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def check_assignment_health() -> Dict[str, Any]:
                """Check the health of the assignment tracking system."""
                from .tools.system import check_assignment_health as impl

                return await impl(state=server)

        if "check_board_health" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def check_board_health() -> Dict[str, Any]:
                """Analyze overall board health and detect systemic issues."""
                from .tools.board_health import check_board_health as impl

                return await impl(state=server)

        # Scheduling tools
        if "get_optimal_agent_count" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_optimal_agent_count(
                include_details: bool = False,
            ) -> Dict[str, Any]:
                """Calculate optimal number of agents using CPM analysis."""
                from .tools.scheduling import get_optimal_agent_count as impl

                return await impl(
                    include_details=include_details,
                    state=server,
                )

        if "get_desired_agent_count" in allowed_tools:

            @app.tool()  # type: ignore[misc]
            async def get_desired_agent_count(
                max_agents: Optional[int] = None,
            ) -> Dict[str, Any]:
                """Return the layered-spawning signal for the runner (#595 Fix 3).

                Returns desired_agent_count (width of the earliest DAG
                layer with incomplete work) and unclaimed_tasks (TODO
                tasks in that layer). Both 0 when all work is DONE.
                max_agents is an optional cap; omit it (None) to size the
                pool to each layer's full width — the default.
                """
                from .tools.scheduling import get_desired_agent_count as impl

                return await impl(
                    max_agents=max_agents,
                    state=server,
                )

    async def run(self) -> None:
        """Run the MCP server."""
        try:
            # Force unbuffered output for immediate response delivery
            import os
            import sys

            # Set Python to use unbuffered output
            os.environ["PYTHONUNBUFFERED"] = "1"

            # Ensure stdout/stderr are unbuffered
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(line_buffering=True)
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(line_buffering=True)

            async with stdio_server() as (read_stream, write_stream):
                # Enable debug logging if requested
                if os.environ.get("MCP_DEBUG") == "1":
                    from .debug_stdio_simple import wrap_stdio_for_debug

                    read_stream, write_stream = wrap_stdio_for_debug(
                        read_stream, write_stream
                    )

                try:
                    await self.server.run(
                        read_stream,
                        write_stream,
                        self.server.create_initialization_options(),
                    )
                finally:
                    # Cancel all pending tasks to ensure clean shutdown
                    pending = asyncio.all_tasks() - {asyncio.current_task()}
                    for task in pending:
                        task.cancel()

                    # Wait briefly for cancellations
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
        except Exception as e:
            print(f"❌ MCP server error: {e}", file=sys.stderr)
            import traceback

            traceback.print_exc(file=sys.stderr)
            raise


async def run_multi_endpoint_server(server: MarcusServer) -> None:
    """Run Marcus with multiple endpoints on different ports."""
    import argparse
    import asyncio

    import uvicorn

    # Parse command line arguments for port overrides
    parser = argparse.ArgumentParser()
    parser.add_argument("--multi", action="store_true")
    parser.add_argument("--human-port", type=int)
    parser.add_argument("--agent-port", type=int)
    parser.add_argument("--analytics-port", type=int)
    args, _ = parser.parse_known_args()

    # Get multi-endpoint config
    config = get_config()

    # Override ports with command line arguments if provided
    multi_endpoint_config = {}
    for endpoint_type in ["human", "agent", "analytics"]:
        endpoint_cfg = getattr(config.multi_endpoint, endpoint_type)
        multi_endpoint_config[endpoint_type] = {
            "port": endpoint_cfg.port,
            "enabled": endpoint_cfg.enabled,
            "host": endpoint_cfg.host,
            "path": endpoint_cfg.path,
        }

        # Check for command line override
        port_arg = getattr(args, f"{endpoint_type}_port", None)
        if port_arg:
            multi_endpoint_config[endpoint_type]["port"] = port_arg

    # Pretty output
    print("\n" + "=" * 70)
    print("    Marcus MCP Server (Multi-Endpoint Mode)")
    print("=" * 70)

    tasks = []

    async def run_endpoint(endpoint_type: str, endpoint_config: Dict[str, Any]) -> None:
        """Run a single endpoint."""
        port = int(endpoint_config.get("port", 4298))
        host = endpoint_config.get("host", "127.0.0.1")
        path = endpoint_config.get("path", "/mcp")

        # Create app for this endpoint
        app = server._create_endpoint_app(endpoint_type)

        print(f"\n[I] {endpoint_type.capitalize()} endpoint:")
        print(f"    URL: http://{host}:{port}{path}")
        print(f"    Tools: {len(get_tools_for_endpoint(endpoint_type))} available")

        # Create Starlette app
        starlette_app = app.streamable_http_app()

        # Configure uvicorn
        config = uvicorn.Config(
            app=starlette_app,
            host=host,
            port=port,
            log_level="error",  # Reduce noise
            access_log=False,
            loop="asyncio",
        )

        # Create and run server
        server_instance = uvicorn.Server(config)
        await server_instance.serve()

    # Start all endpoints
    for endpoint_type, endpoint_config in multi_endpoint_config.items():
        if endpoint_config.get("enabled", True):
            task = asyncio.create_task(run_endpoint(endpoint_type, endpoint_config))
            tasks.append(task)

    print("\n[I] All endpoints started successfully!")
    print("\n[I] Connection examples:")
    print(
        "    Human (Claude Code): "
        "claude mcp add -t http marcus-human http://localhost:4298/mcp"
    )
    print("    Agent workers:       Configure to connect to http://localhost:4299/mcp")
    print("    Seneca analytics:    Configure to connect to http://localhost:4300/mcp")
    print("\n[I] Press Ctrl+C to stop all endpoints")

    try:
        # Wait for all servers
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\n⚠️  Shutting down all endpoints...")
        # Cancel all tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        print("✅ All endpoints stopped")


#: Providers that run a local LLM rather than a hosted API.  Used for
#: the ``is_local_llm`` field on the ``session_started`` telemetry event.
_LOCAL_LLM_PROVIDERS: frozenset[str] = frozenset({"ollama", "local"})


#: Allowed ``runner`` values for the ``session_started`` event.  The
#: discriminator tells the dashboard which entry point produced the
#: session (interactive MCP, /marcus meta-runner, or Posidonius batch).
#: Unknown values pass through verbatim — PostHog can filter them out
#: later if a new runner appears without taxonomy update.
_RUNNER_DEFAULT: str = "mcp_direct"


def _build_session_started_properties(server: Any) -> Dict[str, Any]:
    """Build the property dict for the ``session_started`` event.

    Reads runtime environment + config without ever touching the
    secrets in those configs.  See ``docs/telemetry.md`` § Session
    events for the contract.

    Parameters
    ----------
    server : MarcusServer
        The initialized MarcusServer instance.  Only ``server.config``
        is read; nothing else is touched.

    Returns
    -------
    dict
        Properties suitable for ``TelemetryClient.capture("session_started", ...)``.

    Notes
    -----
    The function is defensive: any AttributeError on the config
    object (e.g., a custom config double in tests) falls through
    to ``"unknown"`` rather than raising.
    """
    import platform
    import sys
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        marcus_version = _pkg_version("marcus-ai")
    except PackageNotFoundError:
        marcus_version = "unknown"

    config = getattr(server, "config", None)
    ai_cfg = getattr(config, "ai", None) if config is not None else None
    kanban_cfg = getattr(config, "kanban", None) if config is not None else None

    ai_provider = getattr(ai_cfg, "provider", "unknown") if ai_cfg else "unknown"
    ai_model = getattr(ai_cfg, "model", "unknown") if ai_cfg else "unknown"
    kanban_provider = (
        getattr(kanban_cfg, "provider", "unknown") if kanban_cfg else "unknown"
    )

    return {
        "marcus_version": marcus_version,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "os": platform.system().lower(),
        "kanban_provider": kanban_provider,
        "ai_provider": ai_provider,
        # Single ``model`` config field is used for both planner and
        # agent today — see ``src/config/marcus_config.py:AISettings``.
        # The split surfaces in a later release (per #540) — for now
        # both keys carry the same value, which is the honest answer.
        "planner_model": ai_model,
        "agent_model": ai_model,
        "is_local_llm": ai_provider in _LOCAL_LLM_PROVIDERS,
        "runner": os.environ.get("MARCUS_RUNNER", _RUNNER_DEFAULT),
    }


def _fire_session_started_event(server: Any) -> None:
    """Emit the ``session_started`` telemetry event.

    Best-effort.  Any error inside property gathering OR the
    telemetry client itself is swallowed so server startup is
    never blocked by telemetry.

    Parameters
    ----------
    server : MarcusServer
        The initialized MarcusServer instance.
    """
    try:
        properties = _build_session_started_properties(server)
        get_telemetry_client().capture("session_started", properties)
    except Exception:  # noqa: BLE001 - telemetry must never crash startup
        # Telemetry is best-effort.  A broken property gather or a
        # broken client must not block the server's first request.
        pass


def _handle_early_exit_paths(argv: List[str]) -> Optional[int]:
    """Route subcommands that should pre-empt MCP server startup.

    Called as the very first action in :func:`main` so that user-
    facing subcommands (notably ``marcus telemetry disable``) work
    even when the rest of Marcus is in a degraded state.  Opting
    out of telemetry must never depend on a working MCP server,
    kanban connection, or LLM provider.

    Parameters
    ----------
    argv : list of str
        ``sys.argv`` (full, including the program name at ``[0]``).

    Returns
    -------
    int or None
        Process exit code if a subcommand was handled — caller
        should ``sys.exit(rc)`` immediately.  ``None`` if no
        subcommand matched and ``main`` should proceed to start
        the MCP server.

    Notes
    -----
    Transport flags (``--stdio``, ``--http``, ``--multi``,
    ``--port``) are NOT subcommands — they are arguments to the
    server itself and pass through to :func:`main`.  Only
    ``telemetry`` is handled here for v0.3.7.
    """
    if len(argv) >= 2 and argv[1] == "telemetry":
        from src.telemetry.cli import handle_telemetry_cli

        return handle_telemetry_cli(argv[2:])
    return None


async def main() -> None:
    """Run the Marcus MCP server."""
    # Route early-exit subcommands (e.g. `marcus telemetry disable`)
    # BEFORE any MCP / config / kanban / LLM setup, so that opting
    # out of telemetry works even when the rest of Marcus is broken.
    early_exit = _handle_early_exit_paths(sys.argv)
    if early_exit is not None:
        sys.exit(early_exit)

    # First-run telemetry notice — stderr-only, idempotent via a
    # marker file, honors MARCUS_TELEMETRY=off.  Printed before
    # any other startup output so users see the disclosure at the
    # very top of their first session.
    print_first_run_notice_if_needed()

    # Get transport from config
    config = get_config()
    transport = config.transport.type or "stdio"

    # Command line overrides
    if "--http" in sys.argv:
        transport = "http"
    elif "--stdio" in sys.argv:
        transport = "stdio"
    elif "--multi" in sys.argv:
        transport = "multi"

    # Parse port argument for single endpoint mode
    if "--port" in sys.argv:
        # Port override is handled by CLI argument parsing
        pass

    try:
        server = MarcusServer()

        # Initialize server silently
        await server.initialize()

        # Fire the session_started telemetry event now that the
        # server is initialized and the config is loaded.  Best-
        # effort; swallows all errors so a telemetry hiccup cannot
        # block startup.
        _fire_session_started_event(server)

        # Get current project for service registration
        current_project = None
        if hasattr(server.project_manager, "get_current_project"):
            try:
                current_project = server.project_manager.get_current_project()
                current_project = current_project.name if current_project else None
            except Exception:
                pass  # nosec B110

        # Register service for discovery (silently)
        register_marcus_service(
            mcp_command=sys.executable + " " + " ".join(sys.argv),
            log_dir=str(Path(server.realtime_log.name).parent),
            project_name=current_project,
            provider=server.provider,
        )

        # Run server with selected transport
        if transport == "multi":
            # Run multi-endpoint mode
            await run_multi_endpoint_server(server)
        elif transport == "http":
            # Use FastMCP for HTTP transport
            fastmcp = server._create_fastmcp()

            # Get HTTP configuration from config file
            host = config.transport.http_host
            port = config.transport.http_port
            path = config.transport.http_path
            log_level = config.transport.log_level

            # Pretty output similar to Jupyter
            print("\n" + "=" * 70)
            print("    Marcus MCP Server (HTTP Mode)")
            print("=" * 70)
            print(f"\n[I] Server URL:    http://{host}:{port}{path}")
            print("[I] Transport:     Streamable HTTP")
            print(f"[I] Log level:     {log_level}")
            print("\n[I] The Marcus server is running at:")
            print(f"    http://{host}:{port}{path}")
            print("\n[I] To connect Claude, use this configuration:")
            print(f'    {{"url": "http://{host}:{port}{path}"}}')

            # For HTTP transport, we need to run it differently
            # FastMCP uses "streamable-http" as the transport name
            import uvicorn

            # Create the Starlette app from FastMCP
            app = fastmcp.streamable_http_app()

            # uvicorn.run() creates its own event loop internally, which raises
            # RuntimeError here because asyncio.run(main()) already owns one.
            # Use the async Server.serve() path instead.
            uvicorn_config = uvicorn.Config(
                app, host=host, port=port, log_level=log_level
            )
            await uvicorn.Server(uvicorn_config).serve()
        else:
            # Use existing stdio transport
            await server.run()
    except Exception as e:
        # Log errors to stderr only in case of failure
        print(f"Failed to start Marcus MCP server: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        raise


def cli_main() -> None:
    """Run the ``marcus`` command — synchronous console-script entry point.

    ``pyproject.toml`` maps the ``marcus`` script to this function.  A
    generated console script calls its target *synchronously*, so an
    ``async def main`` would merely return an un-awaited coroutine and
    do nothing — in particular ``marcus telemetry disable`` / ``purge``
    would never write the config.

    This wrapper handles the synchronous early-exit subcommands first
    (so opting out works even if the rest of Marcus is broken), then
    runs the async server via :func:`asyncio.run`.
    """
    early_exit = _handle_early_exit_paths(sys.argv)
    if early_exit is not None:
        sys.exit(early_exit)
    asyncio.run(main())


if __name__ == "__main__":
    # Load config first to get transport settings
    config = get_config()

    # Setup logging IMMEDIATELY after config load
    setup_logging()

    # Get transport type from config, with command line override
    transport = config.transport.type or "stdio"

    # Command line arguments override config
    if "--http" in sys.argv:
        transport = "http"
    elif "--stdio" in sys.argv:
        transport = "stdio"
    elif "--multi" in sys.argv:
        transport = "multi"

    if transport == "multi":
        # For multi-endpoint mode, use asyncio.run
        asyncio.run(main())
    elif transport == "http":
        # For HTTP mode, run the async initialization first
        async def setup_http_server() -> MarcusServer:
            """Set up HTTP server with async initialization."""
            server = MarcusServer()
            await server.initialize()

            # Register service for discovery
            current_project = None
            if hasattr(server.project_manager, "get_current_project"):
                try:
                    current_project = server.project_manager.get_current_project()
                    current_project = current_project.name if current_project else None
                except Exception:
                    pass  # nosec B110

            register_marcus_service(
                mcp_command=sys.executable + " " + " ".join(sys.argv),
                log_dir=str(Path(server.realtime_log.name).parent),
                project_name=current_project,
                provider=server.provider,
            )

            return server

        # Run the async setup
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server = loop.run_until_complete(setup_http_server())

        # Create FastMCP instance
        fastmcp = server._create_fastmcp()

        # Get HTTP configuration from config file
        host = config.transport.http_host
        port = config.transport.http_port
        path = config.transport.http_path
        log_level = config.transport.log_level

        # Pretty output similar to Jupyter
        print("\n" + "=" * 70)
        print("    Marcus MCP Server (HTTP Mode)")
        print("=" * 70)
        print(f"\n[I] Server URL:    http://{host}:{port}{path}")
        print("[I] Transport:     Streamable HTTP")
        print(f"[I] Log level:     {log_level}")
        print("\n[I] The Marcus server is running at:")
        print(f"    http://{host}:{port}{path}")
        print("\n[I] To connect Claude, use this configuration:")
        print(f'    {{"url": "http://{host}:{port}{path}"}}')

        # Setup shutdown handler for HTTP mode
        def http_signal_handler(signum: int, frame: Any) -> None:
            """Handle shutdown for HTTP server."""
            print(f"\n⚠️  Received signal {signum}, initiating graceful shutdown...")

            # Run cleanup in the event loop
            if hasattr(server, "_shutdown_event"):
                server._shutdown_event.set()

            # Perform sync cleanup
            if hasattr(server, "_sync_cleanup"):
                server._sync_cleanup()

        # Register signal handlers
        signal.signal(signal.SIGINT, http_signal_handler)
        signal.signal(signal.SIGTERM, http_signal_handler)

        # Run with uvicorn directly
        import uvicorn

        app = fastmcp.streamable_http_app()

        # Configure uvicorn with graceful shutdown
        uvicorn_config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_level=log_level,
            loop="asyncio",
            access_log=False,  # Reduce noise
        )

        server_instance = uvicorn.Server(uvicorn_config)

        # Run the server (this will handle shutdown gracefully)
        try:
            server_instance.run()
        except KeyboardInterrupt:
            print("\n✅ HTTP server shutdown complete")
    else:
        # For stdio mode, use the standard async run
        asyncio.run(main())
