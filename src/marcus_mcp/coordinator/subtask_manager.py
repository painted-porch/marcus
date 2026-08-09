"""
Subtask Manager for Marcus.

This module handles hierarchical task decomposition, tracking subtasks
and their relationship to parent tasks.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.models import Priority, Task, TaskStatus
from src.core.paths import marcus_data_dir

logger = logging.getLogger(__name__)


@dataclass
class Subtask:
    """
    Represents a subtask decomposed from a parent task.

    Parameters
    ----------
    id : str
        Unique identifier for the subtask
    parent_task_id : str
        ID of the parent task this subtask belongs to
    name : str
        Short descriptive name of the subtask
    description : str
        Detailed description of what needs to be done
    status : TaskStatus
        Current state of the subtask
    priority : Priority
        Urgency level inherited from parent or adjusted
    assigned_to : Optional[str]
        ID of the worker assigned to this subtask
    created_at : datetime
        Timestamp when subtask was created
    estimated_hours : float
        Estimated time to complete in hours
    dependencies : List[str], optional
        List of other subtask IDs that must be completed first
    dependency_types : List[str], optional
        Type of each dependency: "hard" (blocks start) or "soft" (can use mock/contract)
        Must match length of dependencies list
    file_artifacts : List[str], optional
        Expected file outputs from this subtask
    provides : Optional[str]
        What this subtask provides for dependent subtasks
    requires : Optional[str]
        What this subtask requires from dependencies
    """

    id: str
    parent_task_id: str
    name: str
    description: str
    status: TaskStatus
    priority: Priority
    assigned_to: Optional[str]
    created_at: datetime
    estimated_hours: float
    dependencies: List[str] = field(default_factory=list)
    dependency_types: List[str] = field(default_factory=list)
    file_artifacts: List[str] = field(default_factory=list)
    output_paths: List[str] = field(default_factory=list)
    provides: Optional[str] = None
    requires: Optional[str] = None
    # Per-subtask acceptance criteria (#557) — kept here so criteria
    # survive persistence/reload and are not lost when subtask Task
    # objects are reconstructed from legacy storage.
    acceptance_criteria: List[str] = field(default_factory=list)
    order: int = 0  # Execution order within parent


@dataclass
class SubtaskMetadata:
    """
    Metadata about decomposed tasks.

    Parameters
    ----------
    shared_conventions : Dict[str, Any]
        Shared file structure and naming conventions
    decomposed_at : datetime
        When the parent task was decomposed
    decomposed_by : str
        What triggered decomposition (ai, manual, etc.)
    """

    shared_conventions: Dict[str, Any] = field(default_factory=dict)
    decomposed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decomposed_by: str = "ai"


class SubtaskManager:
    """
    Manages hierarchical task decomposition and subtask tracking.

    This class handles:
    - Creating subtasks from parent tasks
    - Tracking subtask-to-parent relationships
    - Managing subtask completion and parent auto-completion
    - Persisting subtask state for recovery
    """

    def __init__(self, state_file: Optional[Path] = None):
        """
        Initialize the subtask manager.

        Parameters
        ----------
        state_file : Optional[Path]
            Path to JSON file for persisting subtask state.
            Defaults to data/marcus_state/subtasks.json
        """
        if state_file is None:
            state_file = marcus_data_dir() / "marcus_state" / "subtasks.json"

        self.state_file = state_file
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        # In-memory tracking
        self.subtasks: Dict[str, Subtask] = {}  # subtask_id -> Subtask
        self.parent_to_subtasks: Dict[str, List[str]] = {}  # parent_id -> [subtask_ids]
        self.metadata: Dict[str, SubtaskMetadata] = {}  # parent_id -> metadata

        # Load existing state
        self._load_state()

    def add_subtasks(
        self,
        parent_task_id: str,
        subtasks: List[Dict[str, Any]],
        project_tasks: Optional[List[Task]] = None,
        metadata: Optional[SubtaskMetadata] = None,
    ) -> List[Task]:
        """
        Add subtasks for a parent task to unified project_tasks storage.

        Creates Task objects with is_subtask=True and appends them to
        project_tasks list for unified dependency graph.

        Parameters
        ----------
        parent_task_id : str
            ID of the parent task
        subtasks : List[Dict[str, Any]]
            List of subtask dictionaries with fields:
            - name, description, estimated_hours, dependencies, etc.
        project_tasks : Optional[List[Task]]
            Unified task storage (server.project_tasks). If None, uses legacy mode.
        metadata : Optional[SubtaskMetadata]
            Metadata about the decomposition

        Returns
        -------
        List[Task]
            Created Task objects (with is_subtask=True)
        """
        created_tasks = []

        for idx, subtask_data in enumerate(subtasks):
            # Generate unique subtask ID
            subtask_id = f"{parent_task_id}_sub_{idx + 1}"

            # Get dependencies and dependency_types with migration fallback
            dependencies = subtask_data.get("dependencies", [])
            dependency_types = subtask_data.get("dependency_types", [])

            # Migration: if dependency_types not provided, default all to "hard"
            if not dependency_types and dependencies:
                dependency_types = ["hard"] * len(dependencies)
                logger.debug(
                    f"Migration: defaulting all dependencies to 'hard' for {subtask_id}"
                )

            # Store in legacy format (always needed for backwards compatibility)
            legacy_subtask = Subtask(
                id=subtask_id,
                parent_task_id=parent_task_id,
                name=subtask_data["name"],
                description=subtask_data["description"],
                status=TaskStatus.TODO,
                priority=subtask_data.get("priority", Priority.MEDIUM),
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                estimated_hours=subtask_data.get("estimated_hours", 1.0),
                dependencies=dependencies,
                dependency_types=dependency_types,
                file_artifacts=subtask_data.get("file_artifacts", []),
                output_paths=subtask_data.get("output_paths", []),
                provides=subtask_data.get("provides"),
                requires=subtask_data.get("requires"),
                acceptance_criteria=subtask_data.get("acceptance_criteria", []),
                order=idx,
            )

            # Keep legacy storage
            self.subtasks[subtask_id] = legacy_subtask

            # Create Task object for return and unified storage
            task = Task(
                id=subtask_id,
                name=subtask_data["name"],
                description=subtask_data["description"],
                status=TaskStatus.TODO,
                priority=subtask_data.get("priority", Priority.MEDIUM),
                estimated_hours=subtask_data.get("estimated_hours", 1.0),
                dependencies=dependencies,
                labels=subtask_data.get("labels", []),
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                due_date=None,
                # Subtask-specific fields
                is_subtask=True,
                parent_task_id=parent_task_id,
                subtask_index=idx,
                # Cross-parent dependency wiring fields
                provides=subtask_data.get("provides"),
                requires=subtask_data.get("requires"),
                # Exact files this subtask is expected to produce
                output_paths=subtask_data.get("output_paths", []),
                # Per-subtask acceptance criteria (#557): without these,
                # WorkAnalyzer auto-passes the subtask and the validation
                # gate becomes vacuous — stub/placeholder work slips through.
                acceptance_criteria=subtask_data.get("acceptance_criteria", []),
            )

            # Add to unified storage if provided
            if project_tasks is not None:
                project_tasks.append(task)

            created_tasks.append(task)

        # Track parent relationship
        self.parent_to_subtasks[parent_task_id] = [t.id for t in created_tasks]

        # Store metadata
        if metadata is None:
            metadata = SubtaskMetadata()
        self.metadata[parent_task_id] = metadata

        # Persist state
        self._save_state()

        logger.info(
            f"Created {len(created_tasks)} subtasks for parent task {parent_task_id}"
        )

        return created_tasks

    def get_subtasks(
        self, parent_task_id: str, project_tasks: Optional[List[Task]] = None
    ) -> List[Task]:
        """
        Get all subtasks for a parent task from unified storage.

        Parameters
        ----------
        parent_task_id : str
            ID of the parent task
        project_tasks : Optional[List[Task]]
            Unified task storage. If None, falls back to legacy storage.

        Returns
        -------
        List[Task]
            List of Task objects (with is_subtask=True) ordered by subtask_index
        """
        if project_tasks is not None:
            # Query from unified storage
            subtasks = [
                t
                for t in project_tasks
                if t.is_subtask and t.parent_task_id == parent_task_id
            ]
            return sorted(subtasks, key=lambda t: t.subtask_index or 0)
        else:
            # Fall back to legacy storage for backwards compatibility
            subtask_ids = self.parent_to_subtasks.get(parent_task_id, [])
            # Get Subtask objects from legacy storage
            legacy_subtask_list = [
                self.subtasks[sid] for sid in subtask_ids if sid in self.subtasks
            ]
            # Convert Subtask to Task for consistent return type
            tasks: List[Task] = []
            for subtask in sorted(legacy_subtask_list, key=lambda s: s.order):
                task = Task(
                    id=subtask.id,
                    name=subtask.name,
                    description=subtask.description,
                    status=subtask.status,
                    priority=subtask.priority,
                    estimated_hours=subtask.estimated_hours,
                    dependencies=subtask.dependencies,
                    labels=[],
                    assigned_to=subtask.assigned_to,
                    created_at=subtask.created_at,
                    updated_at=subtask.created_at,
                    due_date=None,
                    is_subtask=True,
                    parent_task_id=subtask.parent_task_id,
                    subtask_index=subtask.order,
                    provides=subtask.provides,
                    requires=subtask.requires,
                    acceptance_criteria=subtask.acceptance_criteria,
                )
                tasks.append(task)
            return tasks

    def get_next_available_subtask(
        self,
        parent_task_id: str,
        completed_subtask_ids: set[str],
        project_tasks: Optional[List[Task]] = None,
    ) -> Optional[Task]:
        """
        Get the next available subtask that has all dependencies completed.

        Parameters
        ----------
        parent_task_id : str
            ID of the parent task
        completed_subtask_ids : set
            Set of completed subtask IDs
        project_tasks : Optional[List[Task]]
            Unified task storage. If None, falls back to legacy storage.

        Returns
        -------
        Optional[Task]
            Next available subtask or None if all complete or blocked
        """
        subtasks = self.get_subtasks(parent_task_id, project_tasks)

        for subtask in subtasks:
            # Skip if already completed or in progress
            if subtask.status in [TaskStatus.DONE, TaskStatus.IN_PROGRESS]:
                continue

            # Check if all dependencies are completed
            deps_complete = all(
                dep_id in completed_subtask_ids for dep_id in subtask.dependencies
            )

            if deps_complete:
                return subtask

        return None

    def update_subtask_status(
        self,
        subtask_id: str,
        status: TaskStatus,
        project_tasks: Optional[List[Task]] = None,
        assigned_to: Optional[str] = None,
    ) -> bool:
        """
        Update the status of a subtask in unified storage.

        Parameters
        ----------
        subtask_id : str
            ID of the subtask
        status : TaskStatus
            New status
        project_tasks : Optional[List[Task]]
            Unified task storage. If None, falls back to legacy storage.
        assigned_to : Optional[str]
            Agent assigned to the subtask

        Returns
        -------
        bool
            True if update successful
        """
        if project_tasks is not None:
            # Update in unified storage
            task = next((t for t in project_tasks if t.id == subtask_id), None)
            if task is None:
                logger.warning(f"Subtask {subtask_id} not found in project_tasks")
                return False

            task.status = status
            task.updated_at = datetime.now(timezone.utc)
            if assigned_to:
                task.assigned_to = assigned_to

            # Also update legacy storage for backwards compatibility
            if subtask_id in self.subtasks:
                self.subtasks[subtask_id].status = status
                if assigned_to:
                    self.subtasks[subtask_id].assigned_to = assigned_to

            self._save_state()
            return True
        else:
            # Fall back to legacy storage
            if subtask_id not in self.subtasks:
                logger.warning(f"Subtask {subtask_id} not found")
                return False

            subtask = self.subtasks[subtask_id]
            subtask.status = status
            if assigned_to:
                subtask.assigned_to = assigned_to

            self._save_state()
            return True

    def is_parent_complete(
        self, parent_task_id: str, project_tasks: Optional[List[Task]] = None
    ) -> bool:
        """
        Check if all subtasks of a parent are complete.

        Parameters
        ----------
        parent_task_id : str
            ID of the parent task
        project_tasks : Optional[List[Task]]
            Unified task storage. If None, falls back to legacy storage.

        Returns
        -------
        bool
            True if all subtasks are complete
        """
        subtasks = self.get_subtasks(parent_task_id, project_tasks)
        if not subtasks:
            return False

        return all(s.status == TaskStatus.DONE for s in subtasks)

    def get_completion_percentage(
        self, parent_task_id: str, project_tasks: Optional[List[Task]] = None
    ) -> float:
        """
        Get completion percentage for a parent task based on subtasks.

        Parameters
        ----------
        parent_task_id : str
            ID of the parent task
        project_tasks : Optional[List[Task]]
            Unified task storage. If None, falls back to legacy storage.

        Returns
        -------
        float
            Completion percentage (0-100)
        """
        subtasks = self.get_subtasks(parent_task_id, project_tasks)
        if not subtasks:
            return 0.0

        completed = sum(1 for s in subtasks if s.status == TaskStatus.DONE)
        return (completed / len(subtasks)) * 100

    def get_subtask_context(self, subtask_id: str) -> Dict[str, Any]:
        """
        Get full context for a subtask including parent and dependencies.

        Parameters
        ----------
        subtask_id : str
            ID of the subtask

        Returns
        -------
        Dict[str, Any]
            Context dictionary with parent info, shared conventions, and dependencies
        """
        if subtask_id not in self.subtasks:
            return {}

        subtask = self.subtasks[subtask_id]
        parent_id = subtask.parent_task_id

        # Get metadata
        metadata = self.metadata.get(parent_id, SubtaskMetadata())

        # Get dependency artifacts
        dependency_artifacts = {}
        for dep_id in subtask.dependencies:
            if dep_id in self.subtasks:
                dep = self.subtasks[dep_id]
                dependency_artifacts[dep_id] = {
                    "name": dep.name,
                    "provides": dep.provides,
                    "file_artifacts": dep.file_artifacts,
                    "status": dep.status.value,
                }

        return {
            "subtask": asdict(subtask),
            "parent_task_id": parent_id,
            "shared_conventions": metadata.shared_conventions,
            "dependency_artifacts": dependency_artifacts,
            "sibling_subtasks": [
                {"id": s.id, "name": s.name, "status": s.status.value}
                for s in self.get_subtasks(parent_id)
                if s.id != subtask_id
            ],
        }

    def has_subtasks(
        self, task_id: str, project_tasks: Optional[List[Task]] = None
    ) -> bool:
        """
        Check if a task has been decomposed into subtasks.

        Parameters
        ----------
        task_id : str
            ID of the task to check
        project_tasks : Optional[List[Task]]
            Unified task storage. If None, falls back to legacy storage.

        Returns
        -------
        bool
            True if task has subtasks
        """
        if project_tasks is not None:
            # Check unified storage
            return any(
                t.is_subtask and t.parent_task_id == task_id for t in project_tasks
            )
        else:
            # Fall back to legacy storage
            return task_id in self.parent_to_subtasks

    def remove_subtasks(
        self, parent_task_id: str, project_tasks: Optional[List[Task]] = None
    ) -> bool:
        """
        Remove all subtasks for a parent task from unified storage.

        Parameters
        ----------
        parent_task_id : str
            ID of the parent task
        project_tasks : Optional[List[Task]]
            Unified task storage. If None, falls back to legacy storage.

        Returns
        -------
        bool
            True if removal successful
        """
        if project_tasks is not None:
            # Remove from unified storage
            initial_len = len(project_tasks)
            # Filter out subtasks with matching parent_task_id
            project_tasks[:] = [
                t
                for t in project_tasks
                if not (t.is_subtask and t.parent_task_id == parent_task_id)
            ]
            removed_any = len(project_tasks) < initial_len

            # Also remove from legacy storage
            if parent_task_id in self.parent_to_subtasks:
                subtask_ids = self.parent_to_subtasks[parent_task_id]
                for sid in subtask_ids:
                    if sid in self.subtasks:
                        del self.subtasks[sid]
                del self.parent_to_subtasks[parent_task_id]

            if parent_task_id in self.metadata:
                del self.metadata[parent_task_id]

            self._save_state()
            return removed_any
        else:
            # Fall back to legacy storage
            if parent_task_id not in self.parent_to_subtasks:
                return False

            # Remove all subtasks
            subtask_ids = self.parent_to_subtasks[parent_task_id]
            for sid in subtask_ids:
                if sid in self.subtasks:
                    del self.subtasks[sid]

            del self.parent_to_subtasks[parent_task_id]

            if parent_task_id in self.metadata:
                del self.metadata[parent_task_id]

            self._save_state()
            return True

    def _save_state(self) -> None:
        """Persist subtask state to JSON file."""
        try:
            state = {
                "subtasks": {
                    sid: {
                        **asdict(subtask),
                        "status": subtask.status.value,
                        "priority": subtask.priority.value,
                        "created_at": subtask.created_at.isoformat(),
                    }
                    for sid, subtask in self.subtasks.items()
                },
                "parent_to_subtasks": self.parent_to_subtasks,
                "metadata": {
                    pid: {
                        "shared_conventions": meta.shared_conventions,
                        "decomposed_at": meta.decomposed_at.isoformat(),
                        "decomposed_by": meta.decomposed_by,
                    }
                    for pid, meta in self.metadata.items()
                },
            }

            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2)

        except Exception as e:
            logger.error(f"Error saving subtask state: {e}")

    def _load_state(self) -> None:
        """Load subtask state from JSON file."""
        if not self.state_file.exists():
            logger.info("No existing subtask state file found")
            return

        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)

            # Load subtasks
            for sid, data in state.get("subtasks", {}).items():
                # Migration: add dependency_types if not present
                if "dependency_types" not in data:
                    dependencies = data.get("dependencies", [])
                    if dependencies:
                        data["dependency_types"] = ["hard"] * len(dependencies)
                        logger.debug(
                            f"Migration: adding dependency_types for "
                            f"loaded subtask {sid}"
                        )
                    else:
                        data["dependency_types"] = []

                self.subtasks[sid] = Subtask(
                    **{
                        **data,
                        "status": TaskStatus(data["status"]),
                        "priority": Priority(data["priority"]),
                        "created_at": datetime.fromisoformat(data["created_at"]),
                    }
                )

            # Load parent relationships
            self.parent_to_subtasks = state.get("parent_to_subtasks", {})

            # Load metadata
            for pid, meta_data in state.get("metadata", {}).items():
                self.metadata[pid] = SubtaskMetadata(
                    shared_conventions=meta_data["shared_conventions"],
                    decomposed_at=datetime.fromisoformat(meta_data["decomposed_at"]),
                    decomposed_by=meta_data["decomposed_by"],
                )

            logger.info(f"Loaded {len(self.subtasks)} subtasks from state file")

        except Exception as e:
            logger.error(f"Error loading subtask state: {e}")

    def migrate_to_unified_storage(self, project_tasks: List[Task]) -> None:
        """
        Migrate old Subtask objects to unified Task storage.

        Converts legacy Subtask objects stored in self.subtasks to Task
        objects with is_subtask=True and appends them to project_tasks.

        IMPORTANT: Only migrates subtasks whose parent tasks exist in
        project_tasks. This ensures subtasks from other projects don't
        leak into the current project.

        This method is called during initialization to handle backwards
        compatibility with old state files.

        Parameters
        ----------
        project_tasks : List[Task]
            Unified task storage to migrate subtasks into
        """
        if not self.subtasks:
            logger.debug("No subtasks to migrate")
            return

        # Get set of parent task IDs that exist in current project_tasks
        parent_task_ids = {t.id for t in project_tasks if not t.is_subtask}

        logger.info(
            f"Migrating subtasks for {len(parent_task_ids)} parent tasks "
            f"from {len(self.subtasks)} total subtasks in legacy storage"
        )

        migrated_count = 0
        for subtask_id, subtask in self.subtasks.items():
            # Only migrate if parent task exists in current project
            if subtask.parent_task_id not in parent_task_ids:
                continue

            # Convert Subtask to Task with subtask fields
            task = Task(
                id=subtask.id,
                name=subtask.name,
                description=subtask.description,
                status=subtask.status,
                priority=subtask.priority,
                estimated_hours=subtask.estimated_hours,
                dependencies=subtask.dependencies,
                labels=[],  # Legacy subtasks didn't have labels
                assigned_to=subtask.assigned_to,
                created_at=subtask.created_at,
                updated_at=subtask.created_at,
                due_date=None,
                # Subtask-specific fields
                is_subtask=True,
                parent_task_id=subtask.parent_task_id,
                subtask_index=subtask.order,
                # Cross-parent dependency wiring fields
                provides=subtask.provides,
                requires=subtask.requires,
                acceptance_criteria=subtask.acceptance_criteria,
            )

            project_tasks.append(task)
            migrated_count += 1

        logger.info(
            f"Successfully migrated {migrated_count} subtasks to unified storage "
            f"(filtered from {len(self.subtasks)} total legacy subtasks)"
        )
