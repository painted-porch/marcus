"""
SQLite-backed kanban provider for zero-infrastructure Marcus experiments.

This provider implements the KanbanInterface using a local SQLite database,
eliminating the need for Docker, Planka, or any external service. Ideal for
single-user development, testing, and getting-started scenarios.

Classes
-------
SQLiteKanban
    SQLite implementation of KanbanInterface.
"""

import asyncio
import base64
import dataclasses
import enum
import json
import logging
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union

from src.core.models import Priority, Task, TaskStatus
from src.core.paths import marcus_data_dir
from src.integrations.kanban_interface import KanbanInterface, KanbanProvider

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _json_default(obj: Any) -> Any:
    """JSON encoder fallback for objects ``json.dumps`` can't natively handle.

    The kanban writer persists ``source_context``, ``completion_criteria``,
    and ``acceptance_criteria`` as JSON columns.  Upstream task generators
    (notably the outcome-coverage pipeline introduced in #449) attach
    dataclass instances such as ``UserOutcome`` to these fields.  Without
    a custom encoder, ``json.dumps`` raises ``TypeError`` and the kanban
    write silently drops the task — producing the recipe-revert-haiku
    failure where Implement/Test tasks vanish while Design/foundation
    tasks (carrying plain dicts) make it through.

    Handles, in order of preference:

    1. Pydantic v2 models → ``model_dump(mode="json")`` so nested
       ``datetime`` / ``UUID`` / ``Path`` / ``Enum`` values are emitted
       as JSON-safe primitives in one pass.  Without ``mode="json"``,
       ``model_dump`` returns native Python objects that re-trigger the
       same ``TypeError`` (Codex P2 on PR #504).
    2. Pydantic v1 / objects exposing a callable ``dict`` method.
    3. Dataclass instances → ``dataclasses.asdict``.  Nested non-JSON
       primitives (e.g. ``datetime`` fields) get a second pass through
       this same default function via ``json.dumps``'s recursive
       encoding, hitting the stdlib branches below.
    4. Common stdlib non-JSON-native types: ``datetime`` / ``date``
       (ISO 8601), ``UUID`` (string), ``Path`` (string), ``Enum``
       (``.value``), ``set`` / ``frozenset`` (list).
    5. Objects with ``__dict__`` (last resort, shallow attribute dump).

    Raises
    ------
    TypeError
        When the object exposes no recognized serialization protocol.
        Lets ``json.dumps`` surface a clear error rather than silently
        producing partial state.
    """
    # Pydantic v2 — JSON mode handles nested datetime/UUID/Path/Enum
    # in a single recursive pass.
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except TypeError:
            # Pydantic v1 / older signature without ``mode`` kwarg.
            try:
                return model_dump()
            except Exception:  # pragma: no cover - defensive
                pass

    # Pydantic v1 / arbitrary objects with a ``dict`` accessor.
    obj_dict_method = getattr(obj, "dict", None)
    if callable(obj_dict_method):
        try:
            return obj_dict_method()
        except TypeError:
            pass

    # Dataclasses.  ``asdict`` returns nested Python objects; non-JSON
    # primitives recurse back into this function on the next pass.
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)

    # Common stdlib non-JSON-native types.
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")

    if hasattr(obj, "__dict__"):
        return vars(obj)
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


# Column name → TaskStatus mapping (case-insensitive)
_COLUMN_TO_STATUS: Dict[str, TaskStatus] = {
    "backlog": TaskStatus.TODO,
    "todo": TaskStatus.TODO,
    "to do": TaskStatus.TODO,
    "ready": TaskStatus.TODO,
    "in progress": TaskStatus.IN_PROGRESS,
    "progress": TaskStatus.IN_PROGRESS,
    "in_progress": TaskStatus.IN_PROGRESS,
    "blocked": TaskStatus.BLOCKED,
    "on hold": TaskStatus.BLOCKED,
    "done": TaskStatus.DONE,
    "completed": TaskStatus.DONE,
}

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    provider TEXT DEFAULT 'sqlite',
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_projects_active
    ON projects(is_active);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo',
    priority TEXT NOT NULL DEFAULT 'medium',
    assigned_to TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    due_date TEXT,
    estimated_hours REAL DEFAULT 0.0,
    actual_hours REAL DEFAULT 0.0,
    project_id TEXT,
    project_name TEXT,
    is_subtask INTEGER DEFAULT 0,
    parent_task_id TEXT REFERENCES tasks(id),
    subtask_index INTEGER,
    source_type TEXT,
    source_context TEXT,
    completion_criteria TEXT,
    acceptance_criteria TEXT,
    validation_spec TEXT,
    provides TEXT,
    requires TEXT,
    recovery_info TEXT,
    completed_at TEXT,
    original_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status
    ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assigned
    ON tasks(assigned_to);
CREATE INDEX IF NOT EXISTS idx_tasks_project
    ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent
    ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_available
    ON tasks(status, assigned_to);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    depends_on_id TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_id)
);

CREATE TABLE IF NOT EXISTS task_labels (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    label TEXT NOT NULL,
    PRIMARY KEY (task_id, label)
);

CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    content TEXT NOT NULL,
    author TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_task
    ON comments(task_id);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    filename TEXT NOT NULL,
    filepath TEXT NOT NULL,
    content_type TEXT,
    size_bytes INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_attachments_task
    ON attachments(task_id);

CREATE TABLE IF NOT EXISTS blockers (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    description TEXT NOT NULL,
    severity TEXT DEFAULT 'medium',
    created_at TEXT NOT NULL,
    resolved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_blockers_task
    ON blockers(task_id);
"""


class SQLiteKanban(KanbanInterface):
    """SQLite-backed kanban board — zero external dependencies.

    Parameters
    ----------
    config : Dict[str, Any]
        Provider configuration:
        - db_path : str
            Path to SQLite database file.
            Default: ``./data/kanban.db``
        - project_name : str
            Default project name. Default: ``"Marcus Project"``
        - attachments_dir : str
            Directory for attachment files.
            Default: ``./data/attachments``
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.provider = KanbanProvider.SQLITE

        db_path_cfg = config.get("db_path")
        self.db_path = (
            Path(db_path_cfg) if db_path_cfg else marcus_data_dir() / "kanban.db"
        )
        self.project_name: str = config.get("project_name", "Marcus Project")
        attachments_cfg = config.get("attachments_dir")
        self.attachments_dir = (
            Path(attachments_cfg)
            if attachments_cfg
            else marcus_data_dir() / "attachments"
        )
        self.connected = False

        # Project/board scoping — each experiment gets its own IDs
        # so multiple experiments can share one DB file
        self.project_id: Optional[str] = config.get("project_id")
        self.board_id: Optional[str] = config.get("board_id")

        # Workspace state — used by validation to find project_root
        self._project_root: Optional[str] = config.get("project_root")

        logger.info(
            f"[SQLiteKanban] Initialized with db_path={self.db_path}, "
            f"project_id={self.project_id}"
        )

    # ----------------------------------------------------------
    # Connection
    # ----------------------------------------------------------

    async def connect(self) -> bool:
        """Create database and initialize schema.

        Returns
        -------
        bool
            True if connection succeeded.
        """
        try:
            await self._run_in_executor(self._init_db)
            self.connected = True
            logger.info("[SQLiteKanban] Connected successfully")
            return True
        except Exception as e:
            logger.error(f"[SQLiteKanban] Connect failed: {e}")
            return False

    async def disconnect(self) -> None:
        """Mark provider as disconnected."""
        self.connected = False
        logger.info("[SQLiteKanban] Disconnected")

    def _load_workspace_state(self) -> Optional[Dict[str, str]]:
        """Load workspace state for validation system.

        Returns project_id, board_id, and project_root so the
        validation system can find implementation files.

        Returns
        -------
        Optional[Dict[str, str]]
            Workspace state dict, or None if not configured.
        """
        state: Dict[str, str] = {}
        if self.project_id:
            state["project_id"] = self.project_id
        if self.board_id:
            state["board_id"] = self.board_id
        if self._project_root:
            state["project_root"] = self._project_root
        return state if state else None

    # ----------------------------------------------------------
    # Project/Board Setup
    # ----------------------------------------------------------

    async def auto_setup_project(
        self,
        project_name: str,
        board_name: str = "Main Board",
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new project scope within this database.

        Generates unique project_id and board_id so multiple
        experiments can share one SQLite file without task
        collisions.

        Parameters
        ----------
        project_name : str
            Name for the project.
        board_name : str
            Name for the board (stored for reference).
        project_root : Optional[str]
            Directory where agents write code.

        Returns
        -------
        Dict[str, Any]
            Dict with project_id and board_id.
        """
        if not self.connected:
            await self.connect()

        self.project_id = uuid.uuid4().hex
        self.board_id = uuid.uuid4().hex
        self.project_name = project_name
        if project_root:
            self._project_root = project_root

        logger.info(
            f"[SQLiteKanban] Created project scope: "
            f"project_id={self.project_id}, "
            f"board_id={self.board_id}, "
            f"name={project_name}"
        )

        return {
            "project_id": self.project_id,
            "board_id": self.board_id,
        }

    # ----------------------------------------------------------
    # Task Creation
    # ----------------------------------------------------------

    async def create_task(self, task_data: Dict[str, Any]) -> Task:
        """Create a new task on the board.

        Parameters
        ----------
        task_data : Dict[str, Any]
            Task fields including name, description, priority,
            labels, dependencies, estimated_hours, etc.

        Returns
        -------
        Task
            The created task with generated ID and timestamps.
        """
        if not self.connected:
            await self.connect()

        task_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # Parse status
        raw_status = task_data.get("status", "todo")
        if isinstance(raw_status, TaskStatus):
            status = raw_status
        else:
            status = self._resolve_status(str(raw_status))

        # Parse priority
        raw_priority = task_data.get("priority", "medium")
        if isinstance(raw_priority, Priority):
            priority = raw_priority
        else:
            try:
                priority = Priority(str(raw_priority).lower())
            except ValueError:
                priority = Priority.MEDIUM

        labels: List[str] = task_data.get("labels", [])
        dependencies: List[str] = task_data.get("dependencies", [])

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, name, description, status, priority,
                        assigned_to, created_at, updated_at,
                        due_date, estimated_hours, actual_hours,
                        project_id, project_name, is_subtask,
                        parent_task_id, subtask_index, source_type,
                        source_context, completion_criteria,
                        acceptance_criteria,
                        validation_spec, provides, requires,
                        original_id
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?,
                        ?,
                        ?, ?, ?,
                        ?, ?,
                        ?, ?, ?,
                        ?
                    )
                    """,
                    (
                        task_id,
                        task_data.get("name", ""),
                        task_data.get("description", ""),
                        status.value,
                        priority.value,
                        task_data.get("assigned_to"),
                        now_iso,
                        now_iso,
                        task_data.get("due_date"),
                        task_data.get("estimated_hours", 0.0),
                        task_data.get("actual_hours", 0.0),
                        task_data.get("project_id", self.project_id),
                        task_data.get("project_name", self.project_name),
                        1 if task_data.get("is_subtask") else 0,
                        task_data.get("parent_task_id"),
                        task_data.get("subtask_index"),
                        task_data.get("source_type"),
                        (
                            json.dumps(
                                task_data["source_context"],
                                default=_json_default,
                            )
                            if task_data.get("source_context")
                            else None
                        ),
                        (
                            json.dumps(
                                task_data["completion_criteria"],
                                default=_json_default,
                            )
                            if task_data.get("completion_criteria")
                            else None
                        ),
                        (
                            json.dumps(
                                task_data["acceptance_criteria"],
                                default=_json_default,
                            )
                            if task_data.get("acceptance_criteria")
                            else None
                        ),
                        task_data.get("validation_spec"),
                        task_data.get("provides"),
                        task_data.get("requires"),
                        task_data.get("original_id"),
                    ),
                )

                for label in labels:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_labels "
                        "(task_id, label) VALUES (?, ?)",
                        (task_id, label),
                    )

                for dep_id in dependencies:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_dependencies "
                        "(task_id, depends_on_id) VALUES (?, ?)",
                        (task_id, dep_id),
                    )

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await self._run_in_executor(lambda: self._with_connection(_insert))

        task = await self.get_task_by_id(task_id)
        if task is None:
            raise RuntimeError(f"Failed to retrieve created task {task_id}")
        return task

    # ----------------------------------------------------------
    # Task Retrieval
    # ----------------------------------------------------------

    async def get_available_tasks(self) -> List[Task]:
        """Get unassigned TODO tasks.

        Returns
        -------
        List[Task]
            Tasks with status=TODO and no assignment.
        """
        if not self.connected:
            await self.connect()

        def _query(conn: sqlite3.Connection) -> List[sqlite3.Row]:
            sql = "SELECT * FROM tasks " "WHERE status = ? AND assigned_to IS NULL"
            params: list[Any] = [TaskStatus.TODO.value]
            if self.project_id:
                sql += " AND project_id = ?"
                params.append(self.project_id)
            return conn.execute(sql, params).fetchall()

        rows = await self._run_in_executor(lambda: self._with_connection(_query))
        return [await self._hydrate_task(row) for row in rows]

    async def get_task_blockers(self, task_id: str) -> List[Dict[str, Any]]:
        """Return the blocker records recorded against a task.

        ``update_task`` with a ``blocker`` key and ``report_blocker`` both
        INSERT into the ``blockers`` table, but nothing read them back, so
        the recorded explanation for a blocked task was unreachable —
        end-of-run reporting rendered every blocked lane as "no blocker
        text recorded" (Codex P1 on PR #723).

        Parameters
        ----------
        task_id : str
            Task whose blockers to fetch.

        Returns
        -------
        List[Dict[str, Any]]
            ``{"id", "description", "severity", "created_at"}`` per
            blocker, oldest first — consumers wanting the operative
            reason take the last entry. Empty when none were recorded.
        """
        if not self.connected:
            await self.connect()

        def _query(conn: sqlite3.Connection) -> List[sqlite3.Row]:
            return conn.execute(
                "SELECT id, description, severity, created_at "
                "FROM blockers WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()

        rows = await self._run_in_executor(lambda: self._with_connection(_query))
        return [dict(row) for row in rows]

    async def get_all_tasks(self) -> List[Task]:
        """Get all tasks regardless of status or assignment.

        Returns
        -------
        List[Task]
            All tasks on the board.
        """
        if not self.connected:
            await self.connect()

        def _query(conn: sqlite3.Connection) -> List[sqlite3.Row]:
            if self.project_id:
                return conn.execute(
                    "SELECT * FROM tasks WHERE project_id = ?",
                    (self.project_id,),
                ).fetchall()
            return conn.execute("SELECT * FROM tasks").fetchall()

        rows = await self._run_in_executor(lambda: self._with_connection(_query))
        return [await self._hydrate_task(row) for row in rows]

    async def get_task_by_id(self, task_id: str) -> Optional[Task]:
        """Get a specific task by ID.

        Parameters
        ----------
        task_id : str
            The task's unique identifier.

        Returns
        -------
        Optional[Task]
            The task if found, None otherwise.
        """
        if not self.connected:
            await self.connect()

        def _query(
            conn: sqlite3.Connection,
        ) -> Optional[sqlite3.Row]:
            result: Optional[sqlite3.Row] = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            return result

        row = await self._run_in_executor(lambda: self._with_connection(_query))
        if row is None:
            return None
        return await self._hydrate_task(row)

    # ----------------------------------------------------------
    # Task Updates
    # ----------------------------------------------------------

    async def update_task(
        self, task_id: str, updates: Dict[str, Any]
    ) -> Optional[Task]:
        """Update task fields atomically.

        Handles compound updates (e.g. status + assigned_to together).

        Parameters
        ----------
        task_id : str
            Task to update.
        updates : Dict[str, Any]
            Fields to update. Supports: status, assigned_to, name,
            description, priority, completed_at, blocker, and
            any other Task column.

        Returns
        -------
        Optional[Task]
            Updated task, or None if task not found.
        """
        if not self.connected:
            await self.connect()

        # Check task exists
        existing = await self.get_task_by_id(task_id)
        if existing is None:
            return None

        now_iso = datetime.now(timezone.utc).isoformat()

        def _update(conn: sqlite3.Connection) -> None:
            set_clauses: List[str] = ["updated_at = ?"]
            params: List[Any] = [now_iso]

            for key, value in updates.items():
                if key == "status":
                    if isinstance(value, TaskStatus):
                        set_clauses.append("status = ?")
                        params.append(value.value)
                    else:
                        resolved = self._resolve_status(str(value))
                        set_clauses.append("status = ?")
                        params.append(resolved.value)
                elif key == "priority":
                    if isinstance(value, Priority):
                        set_clauses.append("priority = ?")
                        params.append(value.value)
                    else:
                        try:
                            p = Priority(str(value).lower())
                        except ValueError:
                            p = Priority.MEDIUM
                        set_clauses.append("priority = ?")
                        params.append(p.value)
                elif key == "assigned_to":
                    set_clauses.append("assigned_to = ?")
                    params.append(value)
                elif key == "name":
                    set_clauses.append("name = ?")
                    params.append(value)
                elif key == "description":
                    set_clauses.append("description = ?")
                    params.append(value)
                elif key == "completed_at":
                    set_clauses.append("completed_at = ?")
                    params.append(value)
                elif key == "actual_hours":
                    set_clauses.append("actual_hours = ?")
                    params.append(value)
                elif key == "due_date":
                    set_clauses.append("due_date = ?")
                    params.append(value)
                elif key == "blocker":
                    # Store blocker in blockers table
                    blocker_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO blockers "
                        "(id, task_id, description, severity, "
                        "created_at) VALUES (?, ?, ?, ?, ?)",
                        (
                            blocker_id,
                            task_id,
                            str(value),
                            "medium",
                            now_iso,
                        ),
                    )
                elif key == "source_context":
                    # Merge into existing source_context JSON, not
                    # replace. Codex P1 on PR #660: prior to this
                    # branch, update_task silently ignored
                    # source_context entirely so the #659 scaffold
                    # anchor never landed on the row. Read-modify-write
                    # under the same connection keeps the merge atomic.
                    # Callers passing a dict (e.g. ``{"scaffold_path":
                    # "..."}``) merge their keys in; ``None`` clears
                    # the field; an empty dict ``{}`` is a no-op.
                    if value is None:
                        set_clauses.append("source_context = ?")
                        params.append(None)
                    elif isinstance(value, dict):
                        if not value:
                            # Empty dict — no-op so we don't churn
                            # updated_at when there's nothing to merge.
                            continue
                        row = conn.execute(
                            "SELECT source_context FROM tasks " "WHERE id = ?",
                            (task_id,),
                        ).fetchone()
                        existing_json = row["source_context"] if row else None
                        existing_ctx: Dict[str, Any] = {}
                        if existing_json:
                            try:
                                parsed = json.loads(existing_json)
                                if isinstance(parsed, dict):
                                    existing_ctx = parsed
                            except (json.JSONDecodeError, TypeError):
                                # Corrupt row — overwrite rather than
                                # silently preserve unparseable state.
                                existing_ctx = {}
                        merged = {**existing_ctx, **value}
                        set_clauses.append("source_context = ?")
                        params.append(
                            json.dumps(
                                merged,
                                default=_json_default,
                            )
                        )

            params.append(task_id)
            # Safe: set_clauses only contains hardcoded column names
            # from the elif chain above — never user input.
            sql = f"UPDATE tasks SET {', '.join(set_clauses)} " f"WHERE id = ?"
            conn.execute(sql, params)
            conn.commit()

        await self._run_in_executor(lambda: self._with_connection(_update))
        return await self.get_task_by_id(task_id)

    async def assign_task(self, task_id: str, assignee_id: str) -> bool:
        """Assign task to an agent.

        Sets assigned_to, moves to IN_PROGRESS, and adds a
        timestamped assignment comment.

        Parameters
        ----------
        task_id : str
            Task to assign.
        assignee_id : str
            Agent ID to assign to.

        Returns
        -------
        bool
            True if assignment succeeded.
        """
        if not self.connected:
            await self.connect()

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        comment_id = uuid.uuid4().hex

        def _assign(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE tasks SET assigned_to = ?, "
                "status = ?, updated_at = ? WHERE id = ?",
                (
                    assignee_id,
                    TaskStatus.IN_PROGRESS.value,
                    now_iso,
                    task_id,
                ),
            )
            conn.execute(
                "INSERT INTO comments "
                "(id, task_id, content, author, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    comment_id,
                    task_id,
                    (f"\U0001f4cb Task assigned to " f"{assignee_id} at {now_iso}"),
                    "system",
                    now_iso,
                ),
            )
            conn.commit()

        try:
            await self._run_in_executor(lambda: self._with_connection(_assign))
            return True
        except Exception as e:
            logger.error(f"[SQLiteKanban] assign_task failed: {e}")
            return False

    async def move_task_to_column(self, task_id: str, column_name: str) -> bool:
        """Move task to a column by name.

        Maps column name aliases to TaskStatus values
        (case-insensitive).

        Parameters
        ----------
        task_id : str
            Task to move.
        column_name : str
            Target column name (e.g. "In Progress", "done",
            "backlog", "on hold").

        Returns
        -------
        bool
            True if move succeeded.
        """
        if not self.connected:
            await self.connect()

        status = self._resolve_status(column_name)
        now_iso = datetime.now(timezone.utc).isoformat()

        def _move(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? " "WHERE id = ?",
                (status.value, now_iso, task_id),
            )
            conn.commit()

        try:
            await self._run_in_executor(lambda: self._with_connection(_move))
            return True
        except Exception as e:
            logger.error(f"[SQLiteKanban] move_task_to_column failed: {e}")
            return False

    # ----------------------------------------------------------
    # Comments
    # ----------------------------------------------------------

    async def add_comment(self, task_id: str, comment: str) -> bool:
        """Add a comment to a task.

        Subtask IDs (formatted ``{parent_id}_sub_{N}``) are automatically
        resolved to their parent task, since subtasks live in marcus.db's
        ``subtasks`` collection rather than as rows in this DB's ``tasks``
        table. This matches how subtasks are presented to users (as
        checklist items on the parent card) and avoids ``FOREIGN KEY
        constraint failed`` errors that would otherwise drop the comment.

        Parameters
        ----------
        task_id : str
            Task to comment on. Subtask IDs are accepted and routed to
            the parent.
        comment : str
            Comment content (supports markdown).

        Returns
        -------
        bool
            True if the comment was stored. False if the resolved task
            does not exist or the insert failed for any other reason.
        """
        if not self.connected:
            await self.connect()

        # Resolve subtask IDs to parent. Subtask IDs always have the
        # form "{parent_uuid_hex}_sub_{N}". Anything before "_sub_" is
        # the parent task id.
        resolved_id = task_id
        if "_sub_" in task_id:
            resolved_id = task_id.split("_sub_", 1)[0]

        comment_id = uuid.uuid4().hex
        now_iso = datetime.now(timezone.utc).isoformat()

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO comments "
                "(id, task_id, content, author, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (comment_id, resolved_id, comment, None, now_iso),
            )
            conn.commit()

        try:
            await self._run_in_executor(lambda: self._with_connection(_insert))
            return True
        except Exception as e:
            logger.error(f"[SQLiteKanban] add_comment failed: {e}")
            return False

    # ----------------------------------------------------------
    # Progress + Blockers + Metrics
    # ----------------------------------------------------------

    async def report_blocker(
        self,
        task_id: str,
        blocker_description: str,
        severity: str = "medium",
    ) -> bool:
        """Report a blocker on a task.

        Moves task to BLOCKED, stores blocker record, and adds
        a structured comment. Does NOT unassign the agent.

        Parameters
        ----------
        task_id : str
            Blocked task.
        blocker_description : str
            Description of the impediment.
        severity : str
            Blocker severity: "low", "medium", or "high".

        Returns
        -------
        bool
            True if blocker was recorded.
        """
        if not self.connected:
            await self.connect()

        now_iso = datetime.now(timezone.utc).isoformat()
        blocker_id = uuid.uuid4().hex
        comment_id = uuid.uuid4().hex

        def _block(conn: sqlite3.Connection) -> None:
            # Move to blocked (keep assigned_to!)
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? " "WHERE id = ?",
                (TaskStatus.BLOCKED.value, now_iso, task_id),
            )
            # Store blocker record
            conn.execute(
                "INSERT INTO blockers "
                "(id, task_id, description, severity, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    blocker_id,
                    task_id,
                    blocker_description,
                    severity,
                    now_iso,
                ),
            )
            # Add structured comment
            comment_text = (
                f"\U0001f6ab BLOCKER ({severity.upper()}): " f"{blocker_description}"
            )
            conn.execute(
                "INSERT INTO comments "
                "(id, task_id, content, author, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    comment_id,
                    task_id,
                    comment_text,
                    None,
                    now_iso,
                ),
            )
            conn.commit()

        try:
            await self._run_in_executor(lambda: self._with_connection(_block))
            return True
        except Exception as e:
            logger.error(f"[SQLiteKanban] report_blocker failed: {e}")
            return False

    async def update_task_progress(
        self, task_id: str, progress_data: Dict[str, Any]
    ) -> bool:
        """Update task progress with comment and conditional moves.

        Parameters
        ----------
        task_id : str
            Task to update.
        progress_data : Dict[str, Any]
            Progress info with keys:
            - progress (int): 0-100 percentage
            - status (str): "in_progress", "blocked", "completed"
            - message (str): Progress description

        Returns
        -------
        bool
            True if progress was recorded.
        """
        if not self.connected:
            await self.connect()

        progress = progress_data.get("progress", 0)
        status = progress_data.get("status", "in_progress")
        message = progress_data.get("message", "")

        # Add progress comment
        comment_text = f"\U0001f4ca Progress: {progress}% - {message}"
        await self.add_comment(task_id, comment_text)

        # Conditional status changes
        if status == "blocked":
            await self.move_task_to_column(task_id, "blocked")
        elif progress >= 100 or status in (
            "completed",
            "done",
        ):
            await self.move_task_to_column(task_id, "done")

        return True

    async def get_project_metrics(self) -> Dict[str, Any]:
        """Get task counts grouped by status.

        Returns
        -------
        Dict[str, Any]
            Metrics dict with total_tasks, backlog_tasks,
            in_progress_tasks, completed_tasks, blocked_tasks.
        """
        if not self.connected:
            await self.connect()

        def _query(
            conn: sqlite3.Connection,
        ) -> Dict[str, int]:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM tasks "
                + ("WHERE project_id = ? " if self.project_id else "")
                + "GROUP BY status",
                (self.project_id,) if self.project_id else (),
            ).fetchall()
            counts: Dict[str, int] = {}
            for row in rows:
                counts[row[0]] = row[1]
            return counts

        counts = await self._run_in_executor(lambda: self._with_connection(_query))

        total = sum(counts.values())
        return {
            "total_tasks": total,
            "backlog_tasks": counts.get(TaskStatus.TODO.value, 0),
            "in_progress_tasks": counts.get(TaskStatus.IN_PROGRESS.value, 0),
            "completed_tasks": counts.get(TaskStatus.DONE.value, 0),
            "blocked_tasks": counts.get(TaskStatus.BLOCKED.value, 0),
        }

    # ----------------------------------------------------------
    # Project Management
    # ----------------------------------------------------------

    async def create_project(self, name: str, description: str = "") -> Dict[str, Any]:
        """Create a new project.

        Parameters
        ----------
        name : str
            Project name.
        description : str
            Optional project description.

        Returns
        -------
        Dict[str, Any]
            Created project record with id, name, timestamps.
        """
        if not self.connected:
            await self.connect()

        project_id = uuid.uuid4().hex
        now_iso = datetime.now(timezone.utc).isoformat()

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO projects "
                "(id, name, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, name, description, now_iso, now_iso),
            )
            conn.commit()

        await self._run_in_executor(lambda: self._with_connection(_insert))
        return {
            "id": project_id,
            "name": name,
            "description": description,
            "created_at": now_iso,
            "is_active": True,
        }

    async def list_projects(self) -> List[Dict[str, Any]]:
        """List all active projects.

        Returns
        -------
        List[Dict[str, Any]]
            List of project records.
        """
        if not self.connected:
            await self.connect()

        def _query(
            conn: sqlite3.Connection,
        ) -> List[Dict[str, Any]]:
            rows = conn.execute(
                "SELECT p.id, p.name, p.description, "
                "p.created_at, p.updated_at, p.is_active, "
                "(SELECT COUNT(*) FROM tasks t "
                " WHERE t.project_id = p.id) AS task_count "
                "FROM projects p "
                "WHERE p.is_active = 1 "
                "ORDER BY p.updated_at DESC"
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "description": r["description"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "is_active": bool(r["is_active"]),
                    "task_count": r["task_count"],
                }
                for r in rows
            ]

        return await self._run_in_executor(lambda: self._with_connection(_query))

    async def delete_project(self, project_id: str, hard_delete: bool = False) -> bool:
        """Delete a project (soft delete by default).

        Soft delete marks the project inactive. Hard delete
        removes the project and all its tasks, comments,
        blockers, labels, dependencies, and attachments.

        Parameters
        ----------
        project_id : str
            Project to delete.
        hard_delete : bool
            If True, permanently remove all data.
            If False, mark as inactive (soft delete).

        Returns
        -------
        bool
            True if project was found and deleted.
        """
        if not self.connected:
            await self.connect()

        if hard_delete:

            def _hard_delete(conn: sqlite3.Connection) -> bool:
                # Check project exists
                row = conn.execute(
                    "SELECT id FROM projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
                if row is None:
                    return False

                # Get task IDs for cascade
                task_ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM tasks " "WHERE project_id = ?",
                        (project_id,),
                    ).fetchall()
                ]

                # Cascade delete related records
                for tid in task_ids:
                    conn.execute(
                        "DELETE FROM comments WHERE task_id = ?",
                        (tid,),
                    )
                    conn.execute(
                        "DELETE FROM blockers WHERE task_id = ?",
                        (tid,),
                    )
                    conn.execute(
                        "DELETE FROM task_labels " "WHERE task_id = ?",
                        (tid,),
                    )
                    conn.execute(
                        "DELETE FROM task_dependencies " "WHERE task_id = ?",
                        (tid,),
                    )
                    conn.execute(
                        "DELETE FROM attachments " "WHERE task_id = ?",
                        (tid,),
                    )

                conn.execute(
                    "DELETE FROM tasks WHERE project_id = ?",
                    (project_id,),
                )
                conn.execute(
                    "DELETE FROM projects WHERE id = ?",
                    (project_id,),
                )
                conn.commit()
                return True

            return await self._run_in_executor(
                lambda: self._with_connection(_hard_delete)
            )
        else:
            now_iso = datetime.now(timezone.utc).isoformat()

            def _soft_delete(conn: sqlite3.Connection) -> bool:
                cursor = conn.execute(
                    "UPDATE projects SET is_active = 0, " "updated_at = ? WHERE id = ?",
                    (now_iso, project_id),
                )
                conn.commit()
                return cursor.rowcount > 0

            return await self._run_in_executor(
                lambda: self._with_connection(_soft_delete)
            )

    async def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        """Get a single project by ID.

        Parameters
        ----------
        project_id : str
            Project ID.

        Returns
        -------
        Optional[Dict[str, Any]]
            Project record, or None if not found.
        """
        if not self.connected:
            await self.connect()

        def _query(
            conn: sqlite3.Connection,
        ) -> Optional[Dict[str, Any]]:
            row = conn.execute(
                "SELECT id, name, description, created_at, "
                "updated_at, is_active FROM projects "
                "WHERE id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "is_active": bool(row["is_active"]),
            }

        return await self._run_in_executor(lambda: self._with_connection(_query))

    # ----------------------------------------------------------
    # Attachments
    # ----------------------------------------------------------

    async def upload_attachment(
        self,
        task_id: str,
        filename: str,
        content: Union[str, bytes],
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload an attachment to a task.

        Stores file on disk, metadata in SQLite.

        Parameters
        ----------
        task_id : str
            Task to attach to.
        filename : str
            Name for the file.
        content : Union[str, bytes]
            Base64-encoded string or raw bytes.
        content_type : Optional[str]
            MIME type of the content.

        Returns
        -------
        Dict[str, Any]
            Result with success flag and attachment data.
        """
        if not self.connected:
            await self.connect()

        att_id = uuid.uuid4().hex
        now_iso = datetime.now(timezone.utc).isoformat()

        # Decode content
        if isinstance(content, str):
            raw_bytes = base64.b64decode(content)
        else:
            raw_bytes = content

        # Write file to disk
        task_dir = self.attachments_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        filepath = task_dir / f"{att_id}_{filename}"
        filepath.write_bytes(raw_bytes)

        size = len(raw_bytes)

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO attachments "
                "(id, task_id, filename, filepath, "
                "content_type, size_bytes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    att_id,
                    task_id,
                    filename,
                    str(filepath),
                    content_type,
                    size,
                    now_iso,
                ),
            )
            conn.commit()

        try:
            await self._run_in_executor(lambda: self._with_connection(_insert))
            return {
                "success": True,
                "data": {
                    "id": att_id,
                    "filename": filename,
                    "url": str(filepath),
                    "size": size,
                },
            }
        except Exception as e:
            logger.error(f"[SQLiteKanban] upload_attachment failed: {e}")
            return {"success": False, "error": str(e)}

    async def get_attachments(self, task_id: str) -> Dict[str, Any]:
        """Get all attachments for a task.

        Parameters
        ----------
        task_id : str
            Task to get attachments for.

        Returns
        -------
        Dict[str, Any]
            Result with success flag and attachment list.
        """
        if not self.connected:
            await self.connect()

        def _query(
            conn: sqlite3.Connection,
        ) -> List[Dict[str, Any]]:
            rows = conn.execute(
                "SELECT id, filename, filepath, content_type, "
                "size_bytes, created_at, created_by "
                "FROM attachments WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            return [
                {
                    "id": r[0],
                    "filename": r[1],
                    "url": r[2],
                    "content_type": r[3],
                    "size": r[4],
                    "created_at": r[5],
                    "created_by": r[6],
                }
                for r in rows
            ]

        data = await self._run_in_executor(lambda: self._with_connection(_query))
        return {"success": True, "data": data}

    async def download_attachment(
        self,
        attachment_id: str,
        filename: str,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Download an attachment's content.

        Parameters
        ----------
        attachment_id : str
            Attachment ID.
        filename : str
            Expected filename.
        task_id : Optional[str]
            Optional task ID for context.

        Returns
        -------
        Dict[str, Any]
            Result with base64-encoded content.
        """
        if not self.connected:
            await self.connect()

        def _query(
            conn: sqlite3.Connection,
        ) -> Optional[Dict[str, str]]:
            row = conn.execute(
                "SELECT filepath, content_type FROM attachments " "WHERE id = ?",
                (attachment_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "filepath": row[0],
                "content_type": row[1] or "",
            }

        meta = await self._run_in_executor(lambda: self._with_connection(_query))
        if meta is None:
            return {
                "success": False,
                "error": f"Attachment {attachment_id} not found",
            }

        filepath = Path(meta["filepath"])
        if not filepath.exists():
            return {
                "success": False,
                "error": f"File not found: {filepath}",
            }

        raw = filepath.read_bytes()
        encoded = base64.b64encode(raw).decode()
        return {
            "success": True,
            "data": {
                "content": encoded,
                "filename": filename,
                "content_type": meta["content_type"],
            },
        }

    async def delete_attachment(
        self,
        attachment_id: str,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Delete an attachment (file + DB record).

        Parameters
        ----------
        attachment_id : str
            Attachment to delete.
        task_id : Optional[str]
            Optional task ID for context.

        Returns
        -------
        Dict[str, Any]
            Result with success flag.
        """
        if not self.connected:
            await self.connect()

        def _get_and_delete(
            conn: sqlite3.Connection,
        ) -> Optional[str]:
            row = conn.execute(
                "SELECT filepath FROM attachments WHERE id = ?",
                (attachment_id,),
            ).fetchone()
            if row is None:
                return None
            filepath = row[0]
            conn.execute(
                "DELETE FROM attachments WHERE id = ?",
                (attachment_id,),
            )
            conn.commit()
            return str(filepath)

        filepath_str = await self._run_in_executor(
            lambda: self._with_connection(_get_and_delete)
        )
        if filepath_str is None:
            return {
                "success": False,
                "error": (f"Attachment {attachment_id} not found"),
            }

        # Remove file from disk
        filepath = Path(filepath_str)
        if filepath.exists():
            filepath.unlink()

        return {"success": True}

    # ----------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------

    def _init_db(self) -> None:
        """Create schema, run migrations, and enable WAL mode."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_raw_connection()
        try:
            conn.executescript(_SCHEMA_SQL)
            # Migrate existing DBs: add columns that may not exist
            self._migrate_schema(conn)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Add columns to existing tables if missing.

        Safe to call on new or old databases — ALTER TABLE ADD COLUMN
        is a no-op if the column already exists (caught by except).
        """
        migrations = [
            "ALTER TABLE tasks ADD COLUMN acceptance_criteria TEXT",
        ]
        for sql in migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # Column already exists

    def _get_raw_connection(self) -> sqlite3.Connection:
        """Create a short-lived SQLite connection.

        Returns
        -------
        sqlite3.Connection
            Connection with WAL mode, FK enforcement, and
            Row factory enabled.
        """
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _with_connection(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run a function with a short-lived connection.

        Parameters
        ----------
        fn : Callable
            Function that takes a sqlite3.Connection.

        Returns
        -------
        T
            Return value of fn.
        """
        conn = self._get_raw_connection()
        try:
            return fn(conn)
        finally:
            conn.close()

    async def _run_in_executor(self, fn: Callable[[], T]) -> T:
        """Run a sync function in a thread pool executor.

        Parameters
        ----------
        fn : Callable
            Zero-argument function to execute.

        Returns
        -------
        T
            Return value of fn.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, fn)

    def _resolve_status(self, column_name: str) -> TaskStatus:
        """Map a column name or status string to TaskStatus.

        Parameters
        ----------
        column_name : str
            Column name or status string (case-insensitive).

        Returns
        -------
        TaskStatus
            Resolved status enum value.
        """
        lower = column_name.strip().lower()

        # Direct enum value match
        for ts in TaskStatus:
            if ts.value == lower:
                return ts

        # Column name alias match
        if lower in _COLUMN_TO_STATUS:
            return _COLUMN_TO_STATUS[lower]

        # Substring match (for things like "In Progress Board")
        for key, status in _COLUMN_TO_STATUS.items():
            if key in lower:
                return status

        logger.warning(
            f"[SQLiteKanban] Unknown status/column "
            f"'{column_name}', defaulting to TODO"
        )
        return TaskStatus.TODO

    async def _hydrate_task(self, row: sqlite3.Row) -> Task:
        """Convert a DB row to a fully hydrated Task.

        Fetches labels and dependencies from junction tables.

        Parameters
        ----------
        row : sqlite3.Row
            Raw database row from the tasks table.

        Returns
        -------
        Task
            Fully populated Task dataclass.
        """

        def _get_relations(
            conn: sqlite3.Connection,
        ) -> tuple[List[str], List[str]]:
            labels = [
                r[0]
                for r in conn.execute(
                    "SELECT label FROM task_labels " "WHERE task_id = ?",
                    (row["id"],),
                ).fetchall()
            ]
            deps = [
                r[0]
                for r in conn.execute(
                    "SELECT depends_on_id FROM " "task_dependencies WHERE task_id = ?",
                    (row["id"],),
                ).fetchall()
            ]
            return labels, deps

        labels, deps = await self._run_in_executor(
            lambda: self._with_connection(_get_relations)
        )

        # Parse source_context and completion_criteria JSON
        source_context = None
        if row["source_context"]:
            try:
                source_context = json.loads(row["source_context"])
            except (json.JSONDecodeError, TypeError):
                pass

        completion_criteria = None
        if row["completion_criteria"]:
            try:
                completion_criteria = json.loads(row["completion_criteria"])
            except (json.JSONDecodeError, TypeError):
                pass

        acceptance_criteria: list[str] = []
        try:
            ac_raw = row["acceptance_criteria"]
            if ac_raw:
                acceptance_criteria = json.loads(ac_raw)
        except (json.JSONDecodeError, TypeError, IndexError, KeyError):
            pass

        return Task(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            status=TaskStatus(row["status"]),
            priority=Priority(row["priority"]),
            assigned_to=row["assigned_to"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            due_date=(
                datetime.fromisoformat(row["due_date"]) if row["due_date"] else None
            ),
            estimated_hours=row["estimated_hours"] or 0.0,
            actual_hours=row["actual_hours"] or 0.0,
            dependencies=deps,
            labels=labels,
            project_id=row["project_id"],
            project_name=row["project_name"],
            source_type=row["source_type"],
            source_context=source_context,
            completion_criteria=completion_criteria,
            acceptance_criteria=acceptance_criteria,
            validation_spec=row["validation_spec"],
            is_subtask=bool(row["is_subtask"]),
            parent_task_id=row["parent_task_id"],
            subtask_index=row["subtask_index"],
            provides=row["provides"],
            requires=row["requires"],
        )
