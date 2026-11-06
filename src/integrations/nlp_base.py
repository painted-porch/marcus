"""Base class for Natural Language task creation.

Provides shared functionality for create_project and add_feature tools.
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.core.models import Task
from src.core.paths import marcus_db_path
from src.core.task_graph_validator import TaskGraphValidator
from src.integrations.enhanced_task_classifier import EnhancedTaskClassifier
from src.integrations.nlp_task_utils import (
    SafetyChecker,
    TaskBuilder,
    TaskType,
)

logger = logging.getLogger(__name__)


class NaturalLanguageTaskCreator(ABC):
    """
    Base class for natural language task creation tools.

    Provides common functionality for:
    - Task creation on kanban boards
    - Safety checks and validation
    - Task classification
    - Error handling
    """

    def __init__(
        self,
        kanban_client: Any,
        ai_engine: Any = None,
        subtask_manager: Any = None,
        complexity: str = "standard",
    ) -> None:
        """
        Initialize the base task creator.

        Parameters
        ----------
        kanban_client : Any
            Kanban board client with create_task method
        ai_engine : Any, optional
            Optional AI engine for enhanced processing
        subtask_manager : Any, optional
            Optional SubtaskManager for registering decomposed subtasks
        complexity : str, default="standard"
            Project complexity level: "prototype", "standard", "enterprise"
        """
        self.kanban_client = kanban_client
        self.ai_engine = ai_engine
        self.subtask_manager = subtask_manager
        self.complexity = complexity
        self.task_classifier = EnhancedTaskClassifier()
        self.task_builder = TaskBuilder()
        self.safety_checker = SafetyChecker()
        self.active_project_id: Optional[str] = None

    async def create_tasks_on_board(
        self,
        tasks: List[Task],
        skip_validation: bool = False,
        update_dependencies: bool = True,
    ) -> List[Task]:
        """
        Create tasks on the kanban board.

        This is the main shared functionality between create_project and add_feature.

        Parameters
        ----------
        tasks : List[Task]
            List of tasks to create
        skip_validation : bool
            Skip dependency validation if True
        update_dependencies : bool
            Update task dependencies with new IDs after creation

        Returns
        -------
        List[Task]
            List of created tasks

        Raises
        ------
        RuntimeError
            If kanban client doesn't support task creation
        """
        # A seeded board is exactly the graph the caller gave; the auto-fixer
        # would rewrite it (for example make a lone README task depend on
        # itself as the "final" task). Its dependencies were validated when
        # the seed was built (names, self-reference, cycles).
        if tasks and all(getattr(t, "source_type", None) == "seeded" for t in tasks):
            if not skip_validation:
                logger.info("Seeded board: task graph taken as given, no auto-fix")
            skip_validation = True
        # CRITICAL: Auto-fix task graph issues BEFORE committing to Kanban
        # This fixes problems automatically rather than raising exceptions
        if not skip_validation:
            # Auto-fix task graph issues
            fixed_tasks, user_warnings = TaskGraphValidator.validate_and_fix(tasks)
            tasks = fixed_tasks  # Use the fixed version

            # Log user-friendly warnings
            if user_warnings:
                logger.warning(
                    f"Task graph auto-fixed: {len(user_warnings)} issues corrected"
                )
                for warning in user_warnings:
                    logger.info(f"  • {warning}")

            # Also run legacy safety checker (diagnostic warnings)
            errors = self.safety_checker.validate_dependencies(tasks)
            if errors:
                logger.warning(f"Dependency validation errors: {errors}")

        # Check if kanban client supports task creation
        if not hasattr(self.kanban_client, "create_task"):
            from src.core.error_framework import ErrorContext, KanbanIntegrationError

            raise KanbanIntegrationError(
                board_name=getattr(self.kanban_client, "board_id", "unknown"),
                operation="task_creation_validation",
                context=ErrorContext(
                    operation="create_tasks_on_board",
                    integration_name="natural_language_tools",
                    custom_context={
                        "client_type": type(self.kanban_client).__name__,
                        "details": (
                            f"Kanban client "
                            f"{type(self.kanban_client).__name__} does not "
                            f"support task creation. Expected "
                            f"KanbanClientWithCreate or compatible "
                            f"implementation. Current client type: "
                            f"{type(self.kanban_client).__module__}."
                            f"{type(self.kanban_client).__name__}"
                        ),
                    },
                ),
            )

        created_tasks = []
        failed_tasks = []

        for task in tasks:
            try:
                # Build task data using utility
                task_data = self.task_builder.build_task_data(task)

                # Create task on board
                logger.info(f"Creating task: {task.name}")
                kanban_task = await self.kanban_client.create_task(task_data)
                created_tasks.append(kanban_task)

                # Store task metadata for Phase 1 analysis
                try:
                    from src.core.persistence import SQLitePersistence

                    # Issue #792: this used to be the code checkout, so
                    # an instance running with its own MARCUS_DATA_DIR
                    # wrote task metadata into the production history.
                    db_path = marcus_db_path()
                    persistence = SQLitePersistence(db_path=db_path)

                    # kanban_task is a Task object, not a dict
                    task_id = kanban_task.id
                    if task_id:
                        await persistence.store(
                            "task_metadata",
                            str(task_id),
                            {
                                "task_id": str(task_id),
                                "name": task.name,
                                "description": task.description,
                                "priority": task_data.get("priority"),
                                "estimated_hours": task.estimated_hours,
                                "labels": task.labels,
                                "dependencies": task.dependencies,
                                "project_id": self.active_project_id,
                                "source_type": getattr(task, "source_type", None),
                                "created_at": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                except Exception as log_error:
                    logger.warning(
                        f"Failed to log task metadata for '{task.name}': {log_error}"
                    )

                # Snapshot the kanban task's human name into costs.db so the
                # Cato dashboard can render real names in "Tokens by task"
                # instead of opaque hex IDs (Marcus #530). Lives in the
                # shared task-creation method so BOTH create_project and
                # add_feature flows are covered automatically — Codex P2 on
                # PR #536 caught that the project-only snapshot left the
                # feature-adder path silently dropping names.
                #
                # Failure is logged + swallowed: cost tracking is optional
                # and must never block kanban task creation.
                try:
                    from src.cost_tracking.cost_recorder import (
                        get_recorder as _cost_get_recorder,
                    )

                    _cost_store = _cost_get_recorder().store
                    if kanban_task.id and task.name:
                        _cost_store.record_task_name(str(kanban_task.id), task.name)
                except Exception as _name_err:  # pragma: no cover
                    logger.debug(
                        "Failed to snapshot task name for %s: %s",
                        kanban_task.id,
                        _name_err,
                    )

            except Exception as e:
                from src.core.error_framework import (
                    ErrorContext,
                    KanbanIntegrationError,
                )
                from src.core.error_monitoring import record_error_for_monitoring

                # Log original exception BEFORE wrapping so the root-cause
                # traceback is visible in production logs (issue #480).
                # The wrapped KanbanIntegrationError message alone does not
                # contain the original str(e) in any log line.
                logger.error(
                    f"Task creation failed for '{task.name}': {e!r}",
                    exc_info=True,
                )

                # Create proper error with context
                kanban_error = KanbanIntegrationError(
                    board_name=getattr(self.kanban_client, "board_id", "unknown"),
                    operation="individual_task_creation",
                    context=ErrorContext(
                        operation="create_tasks_on_board",
                        integration_name="natural_language_tools",
                        custom_context={
                            "task_name": task.name,
                            "task_type": getattr(task, "task_type", "unknown"),
                            "details": f"Failed to create task '{task.name}': {str(e)}",
                        },
                    ),
                )

                # Record for monitoring but continue processing
                record_error_for_monitoring(kanban_error)
                logger.error(f"Failed to create task '{task.name}': {kanban_error}")
                failed_tasks.append((task, str(kanban_error)))
                # Continue with other tasks even if one fails

        # Log summary
        logger.info(
            f"Task creation complete: {len(created_tasks)} succeeded, "
            f"{len(failed_tasks)} failed"
        )

        if failed_tasks:
            logger.error(f"Failed tasks: {[(t.name, e) for t, e in failed_tasks]}")

        # Check if no tasks were created at all
        if not created_tasks and tasks:
            from src.core.error_framework import ErrorContext, KanbanIntegrationError

            raise KanbanIntegrationError(
                board_name=getattr(self.kanban_client, "board_id", "unknown"),
                operation="batch_task_creation",
                context=ErrorContext(
                    operation="create_tasks_on_board",
                    integration_name="natural_language_tools",
                    custom_context={
                        "total_tasks": len(tasks),
                        "failed_tasks": len(failed_tasks),
                        "details": (
                            f"Failed to create any of {len(tasks)} tasks. "
                            f"All task creation attempts failed. This "
                            f"indicates a fundamental issue with the kanban "
                            f"integration or board configuration."
                        ),
                    },
                ),
            )

        # Remap dependencies from slug IDs to real UUIDs
        if update_dependencies and created_tasks:
            # Codex P2, round 3 on PR #855: an edge this call drops
            # (an in-batch dependency whose target failed creation)
            # is surfaced here, not swallowed -- a caller reading only
            # this method's own List[Task] return has no way to see
            # it, the same convention create_tasks (nlp.py) already
            # uses for active_project_id.
            self.dependencies_dropped = await self._remap_dependencies(
                tasks, created_tasks
            )
            # After remap, update the task_metadata rows in marcus.db
            # so Cato sees the real UUIDs (not the synthetic slug IDs
            # that were written during the per-task creation loop
            # above). Without this, Cato's dependency graph contains
            # unresolvable slug references and no edges render.
            await self._update_task_metadata_dependencies(created_tasks)

        # Decompose tasks that meet criteria and add as checklist items
        await self._decompose_and_add_subtasks(created_tasks, tasks)

        # Wire cross-parent dependencies after decomposition completes
        await self._wire_cross_parent_dependencies()

        return created_tasks

    async def _update_task_metadata_dependencies(
        self,
        created_tasks: List[Task],
    ) -> None:
        """Update task_metadata rows after dependency remap.

        The per-task creation loop at ``create_tasks_on_board``
        persists ``task_metadata`` with the pre-remap slug-based
        dependency IDs. After ``_remap_dependencies`` converts those
        to real kanban UUIDs, the in-memory ``created_tasks`` have
        correct dependencies but the ``task_metadata`` rows in
        marcus.db still contain the slugs. Cato reads
        ``task_metadata.dependencies`` for its graph so the slugs
        produce unresolvable edges.

        This helper re-reads each task_metadata row, patches the
        ``dependencies`` field with the remapped values, and writes
        it back. Best-effort — persistence errors are logged but
        don't fail task creation.

        Parameters
        ----------
        created_tasks : List[Task]
            Tasks after dependency remap (post-``_remap_dependencies``).
        """
        # Setup persistence once. A failure here (can't import, can't
        # open db) skips the whole pass — no per-task recovery is
        # possible without the persistence client. Any successful
        # setup proceeds to the per-task loop where errors are
        # isolated so one bad row doesn't block the rest.
        try:
            from src.core.persistence import SQLitePersistence

            # Issue #792: the checkout, regardless of MARCUS_DATA_DIR.
            db_path = marcus_db_path()
            persistence = SQLitePersistence(db_path=db_path)
        except Exception as e:
            logger.warning(
                f"Failed to initialize persistence for task_metadata "
                f"dependency update (Cato edges may not render): {e}"
            )
            return

        def _get(obj: Any, attr: str, default: Any = None) -> Any:
            if hasattr(obj, attr):
                return getattr(obj, attr)
            if isinstance(obj, dict):
                return obj.get(attr, default)
            return default

        updated = 0
        failed = 0
        for task in created_tasks:
            # Isolate per-task errors so one malformed row or
            # transient SQLite error doesn't abort updates for the
            # rest of the batch (Codex P2 on PR #340).
            try:
                task_id = _get(task, "id", "")
                if not task_id:
                    continue
                new_deps = list(_get(task, "dependencies", []) or [])

                # Read existing metadata, patch deps, write back.
                existing = await persistence.retrieve("task_metadata", str(task_id))
                if existing is None:
                    # No metadata row for this task — nothing to update.
                    continue
                existing["dependencies"] = new_deps
                await persistence.store("task_metadata", str(task_id), existing)
                updated += 1
            except Exception as e:
                failed += 1
                logger.warning(
                    f"Failed to update task_metadata dependencies "
                    f"for task {_get(task, 'id', '?')} "
                    f"({_get(task, 'name', '?')}): {e}"
                )

        if updated or failed:
            logger.info(
                f"task_metadata dependency update: "
                f"{updated} succeeded, {failed} failed"
            )

    async def _remap_dependencies(
        self,
        original_tasks: List[Task],
        created_tasks: List[Task],
    ) -> List[Dict[str, str]]:
        """Remap dependencies from slug IDs to real kanban UUIDs.

        After task creation, dependencies still reference the original
        slug-style IDs (e.g. ``design_project_domain``). This method
        builds a slug-to-UUID mapping and updates both the in-memory
        Task objects and the kanban board storage.

        Parameters
        ----------
        original_tasks : List[Task]
            Tasks as generated by NLP (with original slug IDs).
        created_tasks : List[Task]
            Tasks as created on the kanban board (with real UUIDs).

        Returns
        -------
        List[Dict[str, str]]
            One entry per dependency edge dropped because its target
            was an in-batch task that itself failed creation (Codex
            P2, round 3 on PR #855) -- ``{"task": <name whose edge
            was dropped>, "missing_dependency": <the failed task's
            name or id>}``. Empty when nothing was dropped.
        """

        # Build mapping: original_id / slug → real UUID
        # Use name-based matching instead of positional zip, since
        # failed task creations can shift positions.
        # Helper to access fields on Task objects or dicts
        def _get(obj: Any, attr: str, default: Any = None) -> Any:
            if hasattr(obj, attr):
                return getattr(obj, attr)
            if isinstance(obj, dict):
                return obj.get(attr, default)
            return default

        def _set(obj: Any, attr: str, value: Any) -> None:
            if hasattr(obj, attr):
                setattr(obj, attr, value)
            elif isinstance(obj, dict):
                obj[attr] = value

        slug_to_uuid: Dict[str, str] = {}
        orig_by_name: Dict[str, Any] = {_get(t, "name", ""): t for t in original_tasks}
        for created in created_tasks:
            c_name = _get(created, "name", "")
            c_id = _get(created, "id", "")
            orig = orig_by_name.get(c_name)
            orig_id_val = _get(orig, "id", "") if orig else ""
            if orig and orig_id_val and c_id:
                slug_to_uuid[str(orig_id_val)] = str(c_id)
            # Also map by original_id if stored
            orig_id = _get(created, "original_id")
            if orig_id and c_id:
                slug_to_uuid[str(orig_id)] = str(c_id)

        if not slug_to_uuid:
            return []

        logger.info(
            f"Remapping dependencies: {len(slug_to_uuid)} " f"slug-to-UUID entries"
        )

        # Remap dependencies on in-memory tasks
        all_ids = {_get(t, "id", "") for t in created_tasks}
        # Codex P2, round 3 on PR #855: a batch member's own id, used
        # to tell "this dep_id was never a batch member at all, so it
        # must be a validated existing-board id" apart from "this
        # dep_id WAS a batch member, but that member failed creation
        # and is in neither slug_to_uuid nor all_ids" -- the two read
        # identically to every check above them, and conflating them
        # let a failed in-batch task's id survive remapping disguised
        # as a validated board reference, persisting a dependency on
        # a task that was never created.
        original_ids = {_get(t, "id", "") for t in original_tasks}
        dropped: List[Dict[str, str]] = []

        for task in created_tasks:
            deps = _get(task, "dependencies", [])
            if not deps:
                continue
            # Codex P2, round 2 on PR #855: never trust what the
            # provider echoes back for this. KanbanClientWithCreate's
            # create_task() on Planka returns a Task built by
            # _card_to_task, which never sets source_type at all --
            # so _get(task, "source_type") read here was always None
            # for a real Planka board, silently defeating the #841
            # fix below for the one provider it most needed to work
            # on. Decide provenance from the ORIGINAL task Marcus
            # itself built and handed to create_task, matched by
            # name, the same lookup already used to build
            # slug_to_uuid above.
            original = orig_by_name.get(_get(task, "name", ""))
            is_seeded = _get(original, "source_type") == "seeded"
            remapped: List[str] = []
            for dep_id in deps:
                if dep_id in slug_to_uuid:
                    remapped.append(slug_to_uuid[dep_id])
                elif dep_id in all_ids:
                    remapped.append(dep_id)
                elif dep_id in original_ids:
                    # An in-batch reference whose target was never
                    # created (it failed and so is in neither map
                    # above) -- not a validated existing-board id,
                    # regardless of whether THIS task is seeded. Drop
                    # the edge explicitly and surface it, rather than
                    # silently keeping a reference to a task that
                    # does not exist (which would leave this task
                    # permanently unclaimable even if the missing one
                    # is recreated later under a different id).
                    missing = next(
                        (t for t in original_tasks if _get(t, "id", "") == dep_id),
                        None,
                    )
                    missing_name = _get(missing, "name", dep_id) if missing else dep_id
                    task_name = _get(task, "name", "?")
                    logger.warning(
                        f"Dependency {missing_name!r} on task "
                        f"{task_name!r} was never created -- "
                        f"dropping the edge"
                    )
                    dropped.append(
                        {"task": task_name, "missing_dependency": missing_name}
                    )
                elif is_seeded:
                    # Issue #841: a seeded task's depends_on may
                    # already be a real, pre-existing board task's
                    # id -- validated and resolved by
                    # build_seeded_tasks before this ever runs, not a
                    # slug to remap and not part of this batch (it is
                    # not in original_ids, ruled out above), so
                    # neither map above recognizes it. Trust it
                    # rather than orphaning it; only a seeded task
                    # can carry one, so a genuinely bad id from the
                    # decomposed (non-seeded) path is still caught
                    # below.
                    remapped.append(dep_id)
                else:
                    logger.warning(
                        f"Orphaned dependency '{dep_id}' "
                        f"on task '{_get(task, 'name', '?')}'"
                        f" — skipping"
                    )
            _set(task, "dependencies", remapped)

        # Persist remapped dependencies to kanban board
        # SQLiteKanban stores deps in task_dependencies table
        if hasattr(self.kanban_client, "_with_connection"):
            # SQLite provider — update junction table directly
            import sqlite3

            def _update_deps(conn: sqlite3.Connection) -> None:
                for task in created_tasks:
                    tid = _get(task, "id", "")
                    deps = _get(task, "dependencies", [])
                    conn.execute(
                        "DELETE FROM task_dependencies " "WHERE task_id = ?",
                        (tid,),
                    )
                    for dep_id in deps:
                        conn.execute(
                            "INSERT OR IGNORE INTO "
                            "task_dependencies "
                            "(task_id, depends_on_id) "
                            "VALUES (?, ?)",
                            (tid, dep_id),
                        )
                conn.commit()

            try:
                await self.kanban_client._run_in_executor(
                    lambda: self.kanban_client._with_connection(_update_deps)
                )
                logger.info("Persisted remapped dependencies to SQLite")
            except Exception as e:
                logger.warning(f"Failed to persist remapped deps: {e}")

        return dropped

    async def _decompose_and_add_subtasks(
        self, created_tasks: List[Task], original_tasks: List[Task]
    ) -> None:
        """
        Decompose tasks that meet criteria and add subtasks as Planka checklist items.

        Uses parallel AI calls for performance - all task decompositions are
        executed concurrently instead of sequentially.

        Parameters
        ----------
        created_tasks : List[Task]
            Tasks that were created on the Kanban board
        original_tasks : List[Task]
            Original task objects with estimated_hours
        """
        # Skip decomposition if no AI engine is available
        if not self.ai_engine:
            logger.warning(
                "⚠️  No AI engine available for task decomposition - skipping. "
                "Subtasks will not be created!"
            )
            return

        # Log decomposition context
        logger.info(
            f"🔍 Task decomposition started: {len(created_tasks)} tasks, "
            f"SubtaskManager available: {self.subtask_manager is not None}, "
            f"Complexity: {self.complexity}"
        )

        # Note: AI engine now uses LLMAbstraction which automatically
        # checks provider availability and supports local models (Ollama),
        # Anthropic, and OpenAI. If no provider is configured,
        # decompose_task will raise a clear error message.

        import asyncio

        from src.marcus_mcp.coordinator import decompose_task, should_decompose

        # Create mapping of task names to original tasks (for estimated_hours)
        task_map = {task.name: task for task in original_tasks}

        # Collect all tasks that need decomposition for parallel execution
        decomposition_jobs = []
        task_metadata = []  # Track (created_task, original_task) pairs

        for created_task in created_tasks:
            # Get original task to check estimated hours
            original_task = task_map.get(created_task.name)
            if not original_task:
                continue

            # A seeded task is exactly what the caller asked for: never split it.
            if getattr(original_task, "source_type", None) == "seeded":
                logger.info(f"Seeded task '{original_task.name}' is not decomposed")
                continue
            # Check if task should be decomposed
            # Pass complexity for prototype mode check
            if not should_decompose(original_task, project_complexity=self.complexity):
                continue

            logger.info(
                f"Queueing task '{created_task.name}' for decomposition "
                f"({original_task.estimated_hours}h)"
            )

            # Add decomposition job to parallel execution list
            # CRITICAL: Pass created_task which has the real Planka ID,
            # not original_task. We need to create a task object that has
            # both the real ID and the original details
            task_with_real_id = Task(
                id=created_task.id,  # Real Planka ID
                name=original_task.name,
                description=original_task.description,
                status=original_task.status,
                priority=original_task.priority,
                assigned_to=original_task.assigned_to,
                created_at=original_task.created_at,
                updated_at=original_task.updated_at,
                due_date=original_task.due_date,
                estimated_hours=original_task.estimated_hours,
                actual_hours=original_task.actual_hours,
                dependencies=original_task.dependencies,
                labels=original_task.labels,
                project_id=getattr(original_task, "project_id", None),
                project_name=getattr(original_task, "project_name", None),
                # #557 Fix 1: carry the parent's acceptance criteria into
                # decomposition. For contract_first these were generated by
                # decompose_by_contract with the domain contract in hand, so
                # subtask criteria can be derived FROM them instead of being
                # re-invented blind (which risks drift from the contract).
                acceptance_criteria=getattr(original_task, "acceptance_criteria", []),
                completion_criteria=getattr(original_task, "completion_criteria", None),
            )
            # Pass complexity through project_context for time budgets and validation
            project_context = {"complexity": self.complexity}
            decomposition_jobs.append(
                decompose_task(
                    task_with_real_id, self.ai_engine, project_context=project_context
                )
            )
            task_metadata.append((created_task, original_task))

        # Execute all decompositions in parallel
        if not decomposition_jobs:
            logger.debug("No tasks require decomposition")
            return

        logger.info(f"Decomposing {len(decomposition_jobs)} tasks in parallel...")

        decomposition_results = await asyncio.gather(
            *decomposition_jobs, return_exceptions=True
        )

        # Process results and add checklist items
        successful_count = 0
        failed_count = 0

        for idx, result in enumerate(decomposition_results):
            created_task, original_task = task_metadata[idx]

            try:
                # Handle exceptions from gather (both Exception and BaseException)
                if isinstance(result, BaseException):
                    failed_count += 1
                    logger.error(
                        (
                            f"Decomposition failed for task "
                            f"'{created_task.name}': {result}"
                        ),
                        exc_info=result if isinstance(result, Exception) else None,
                    )
                    continue

                # Handle failed decomposition responses
                if not result.get("success"):
                    failed_count += 1
                    logger.warning(
                        f"Failed to decompose task '{created_task.name}': "
                        f"{result.get('error')}"
                    )
                    continue

                # Successfully decomposed - add subtasks
                subtasks = result.get("subtasks", [])
                shared_conventions = result.get("shared_conventions", {})
                num_subtasks = len(subtasks)
                logger.info(
                    f"Task '{created_task.name}' decomposed into "
                    f"{num_subtasks} subtasks"
                )

                # Register subtasks with SubtaskManager (GH-62 fix)
                if self.subtask_manager:
                    from src.marcus_mcp.coordinator.subtask_manager import (
                        SubtaskMetadata,
                    )

                    metadata = SubtaskMetadata(
                        shared_conventions=shared_conventions,
                        decomposed_by="ai",
                    )
                    self.subtask_manager.add_subtasks(
                        parent_task_id=created_task.id,
                        subtasks=subtasks,
                        metadata=metadata,
                    )
                    logger.info(
                        f"Registered {num_subtasks} subtasks with SubtaskManager "
                        f"for task '{created_task.name}'"
                    )
                else:
                    logger.warning(
                        "SubtaskManager not available - subtasks will only exist as "
                        "checklist items (GH-62)"
                    )

                # Add subtasks as checklist items in Planka. Skip for any
                # other provider — SQLite, GitHub, and Linear all manage
                # subtasks via SubtaskManager (already done above), and
                # spawning the kanban-mcp Node service for non-Planka
                # providers either errors out (Planka env vars missing)
                # or fails silently for first-time users without
                # kanban-mcp installed at all.
                if self._is_planka_provider():
                    await self._add_subtasks_as_checklist(created_task.id, subtasks)
                successful_count += 1

            except Exception as e:
                failed_count += 1
                logger.error(
                    (
                        f"Error processing decomposition for task "
                        f"'{created_task.name}': {e}"
                    ),
                    exc_info=True,
                )
                # Continue with other tasks even if processing fails

        # Log summary
        logger.info(
            f"Task decomposition complete: {successful_count} succeeded, "
            f"{failed_count} failed"
        )

    async def _wire_cross_parent_dependencies(self) -> None:
        """
        Wire cross-parent dependencies after decomposition completes.

        This is called after all tasks have been decomposed to create
        fine-grained dependencies between subtasks of different parent tasks.
        """
        # Skip if no subtask manager or AI engine
        if not self.subtask_manager or not self.ai_engine:
            logger.debug(
                "Skipping cross-parent dependency wiring - "
                "required components not available"
            )
            return

        # Note: Cross-parent dependency wiring is implemented in
        # server.refresh_project_state() where we have access to unified
        # project_tasks storage
        logger.info(
            "Cross-parent dependency wiring will be performed "
            "during project state refresh"
        )

    def _is_planka_provider(self) -> bool:
        """Return True if the wired kanban client is the Planka provider.

        Used to gate Planka-specific code paths (notably
        ``_add_subtasks_as_checklist``, which spawns the ``kanban-mcp``
        Node service to create checklist items on a Planka card).

        The check is robust to mocks and partial imports: it inspects
        the client's ``provider`` attribute (set by every concrete
        ``KanbanInterface`` subclass) and falls back to a string match
        on the class name when ``provider`` is absent.

        Returns
        -------
        bool
            True iff the kanban client is a Planka provider.
        """
        provider = getattr(self.kanban_client, "provider", None)
        if provider is not None:
            # KanbanProvider enum values are strings under the hood;
            # accept either the enum or its string equivalent.
            provider_str = getattr(provider, "value", str(provider))
            if str(provider_str).lower() == "planka":
                return True
        # Fallback when provider attribute isn't set (e.g. partial mocks)
        return "Planka" in type(self.kanban_client).__name__

    async def _add_subtasks_as_checklist(
        self, parent_card_id: str, subtasks: List[Dict[str, Any]]
    ) -> None:
        """
        Add subtasks as checklist items (tasks) in Planka.

        Parameters
        ----------
        parent_card_id : str
            ID of the parent card in Planka
        subtasks : List[Dict[str, Any]]
            List of subtask definitions from decomposition
        """
        try:
            import os

            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            # Use same server params as PlankaKanban
            # Use local path for kanban-mcp
            kanban_mcp_path = os.path.expanduser("~/dev/kanban-mcp/dist/index.js")
            server_params = StdioServerParameters(
                command="node",
                args=[kanban_mcp_path],
                env=os.environ.copy(),
            )

            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    # Create checklist item for each subtask
                    for idx, subtask in enumerate(subtasks):
                        subtask_name = subtask.get("name", f"Subtask {idx + 1}")

                        # Create task (checklist item) in Planka
                        result = await session.call_tool(
                            "mcp_kanban_task_manager",
                            {
                                "action": "create",
                                "cardId": parent_card_id,
                                "name": subtask_name,
                                "position": (idx + 1) * 65535,
                            },
                        )

                        if result:
                            logger.info(
                                f"Created checklist item '{subtask_name}' "
                                f"on card {parent_card_id}"
                            )

            num_added = len(subtasks)
            logger.info(
                f"Added {num_added} subtasks as checklist items "
                f"to card {parent_card_id}"
            )

        except Exception as e:
            logger.error(
                f"Error adding subtasks as checklist items to card "
                f"{parent_card_id}: {e}",
                exc_info=True,
            )

    async def apply_safety_checks(self, tasks: List[Task]) -> List[Task]:
        """
        Apply safety checks to ensure logical task ordering.

        This method can be overridden by subclasses for custom safety logic.

        Parameters
        ----------
        tasks : List[Task]
            List of tasks to check

        Returns
        -------
        List[Task]
            List of tasks with updated dependencies
        """
        # Import phase dependency enforcer
        from src.core.phase_dependency_enforcer import PhaseDependencyEnforcer

        # First apply phase-based dependencies for proper ordering
        phase_enforcer = PhaseDependencyEnforcer()
        tasks = phase_enforcer.enforce_phase_dependencies(tasks)

        # Then apply legacy safety checks for additional constraints
        # These may add extra dependencies but won't conflict with phase ordering

        # Apply implementation dependencies (implementation depends on design)
        tasks = self.safety_checker.apply_implementation_dependencies(tasks)

        # Apply testing dependencies (testing depends on implementation)
        tasks = self.safety_checker.apply_testing_dependencies(tasks)

        # Apply deployment dependencies (deployment depends on everything)
        tasks = self.safety_checker.apply_deployment_dependencies(tasks)

        return tasks

    def classify_tasks(self, tasks: List[Task]) -> Dict[TaskType, List[Task]]:
        """
        Classify tasks by their type.

        Parameters
        ----------
        tasks : List[Task]
            List of tasks to classify

        Returns
        -------
        Dict[TaskType, List[Task]]
            Dictionary mapping task types to lists of tasks
        """
        classified: Dict[TaskType, List[Task]] = {
            task_type: [] for task_type in list(TaskType)
        }

        for task in tasks:
            task_type = self.task_classifier.classify(task)
            classified[task_type].append(task)

        return classified

    def classify_tasks_with_details(
        self, tasks: List[Task]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Classify tasks and return detailed classification info per task.

        Parameters
        ----------
        tasks : List[Task]
            List of tasks to classify

        Returns
        -------
        Dict[str, Dict[str, Any]]
            Dictionary mapping task IDs to classification details
        """
        from src.integrations.enhanced_task_classifier import EnhancedTaskClassifier

        classifier = EnhancedTaskClassifier()
        results = {}

        for task in tasks:
            result = classifier.classify_with_confidence(task)
            results[task.id] = {
                "type": result.task_type.value,
                "confidence": result.confidence,
                "reasoning": result.reasoning,
            }

        return results

    def get_tasks_by_type(self, tasks: List[Task], task_type: TaskType) -> List[Any]:
        """Get all tasks of a specific type."""
        return self.task_classifier.filter_by_type(tasks, task_type)

    def is_deployment_task(self, task: Task) -> Any:
        """Check if task is deployment-related."""
        return self.task_classifier.is_type(task, TaskType.DEPLOYMENT)

    def is_implementation_task(self, task: Task) -> Any:
        """Check if task is implementation-related."""
        return self.task_classifier.is_type(task, TaskType.IMPLEMENTATION)

    def is_testing_task(self, task: Task) -> Any:
        """Check if task is testing-related."""
        return self.task_classifier.is_type(task, TaskType.TESTING)

    @abstractmethod
    async def process_natural_language(
        self, description: str, **kwargs: Any
    ) -> List[Task]:
        """
        Process natural language description into tasks.

        This method must be implemented by subclasses.

        Parameters
        ----------
        description : str
            Natural language description
        **kwargs : Any
            Additional parameters specific to the implementation

        Returns
        -------
        List[Task]
            List of generated tasks
        """
        pass

    async def create_from_description(
        self, description: str, apply_safety: bool = True, **kwargs: Any
    ) -> Dict[str, Any]:
        """
        Create tasks from natural language description.

        Parameters
        ----------
        description : str
            Natural language description
        apply_safety : bool
            Whether to apply safety checks
        **kwargs : Any
            Additional parameters for processing

        Returns
        -------
        Dict[str, Any]
            Dictionary with creation results
        """
        try:
            # Process natural language into tasks
            tasks = await self.process_natural_language(description, **kwargs)

            # Apply safety checks if requested
            if apply_safety:
                tasks = await self.apply_safety_checks(tasks)

            # Create tasks on board
            created_tasks = await self.create_tasks_on_board(tasks)

            # Build result
            result = {
                "success": True,
                "tasks_created": len(created_tasks),
                "tasks": [
                    {
                        "id": task.id,
                        "name": task.name,
                        "type": self.task_classifier.classify(task).value,
                    }
                    for task in created_tasks
                ],
                "task_types": {
                    task_type.value: len(
                        self.get_tasks_by_type(created_tasks, task_type)
                    )
                    for task_type in list(TaskType)
                },
            }

            return result

        except Exception as e:
            logger.error(f"Error in create_from_description: {str(e)}")
            return {"success": False, "error": str(e), "tasks_created": 0}
