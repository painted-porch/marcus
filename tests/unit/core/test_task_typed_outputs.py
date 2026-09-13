"""
Unit tests for task types and typed outputs (issue #737, step 1).

A task gains a ``task_type`` (find, verify, approve, synthesize, or
implement), an ``inputs`` payload (what the board hands the task, e.g.
the claim a verify task checks), and an ``output_schema`` (the contract
evidence must match before the task may close).  These tests pin three
contracts: the model defaults keep every existing caller working, the
SQLite provider round-trips the new fields, and a board created before
this change loads unchanged with ``task_type`` defaulting to implement.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.integrations.providers.sqlite_kanban import SQLiteKanban

pytestmark = pytest.mark.unit


def _make_task(**overrides: Any) -> Task:
    """Create a Task with required fields and optional overrides."""
    now = datetime.now(timezone.utc)
    data: Dict[str, Any] = {
        "id": "TASK-001",
        "name": "Identify the source of the excerpt",
        "description": "Find headline, publisher, date, and URL",
        "status": TaskStatus.TODO,
        "priority": Priority.HIGH,
        "assigned_to": None,
        "created_at": now,
        "updated_at": now,
        "due_date": now + timedelta(days=1),
        "estimated_hours": 1.0,
    }
    data.update(overrides)
    return Task(**data)


class TestTaskTypedOutputModelFields:
    """Test suite for the new Task model fields."""

    def test_task_defaults_to_implement_type(self) -> None:
        """A Task built without the new fields gets safe defaults.

        Every existing caller constructs Task without task_type,
        inputs, or output_schema; the defaults must keep them working.
        """
        task = _make_task()

        assert task.task_type == "implement"
        assert task.inputs == {}
        assert task.output_schema is None

    def test_task_accepts_typed_output_fields(self) -> None:
        """A Task carries task_type, inputs, and output_schema."""
        schema = {
            "type": "object",
            "required": ["headline", "publisher", "date", "url"],
        }
        task = _make_task(
            task_type="find",
            inputs={"excerpt": "Collectively, they provided..."},
            output_schema=schema,
        )

        assert task.task_type == "find"
        assert task.inputs == {"excerpt": "Collectively, they provided..."}
        assert task.output_schema == schema

    def test_inputs_default_is_not_shared_between_tasks(self) -> None:
        """Each Task gets its own inputs dict, not a shared default."""
        first = _make_task()
        second = _make_task(id="TASK-002")

        first.inputs["claim"] = {"url": "https://example.org"}

        assert second.inputs == {}


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Provide a temporary database path."""
    return str(tmp_path / "test_kanban.db")


@pytest.fixture
def kanban(db_path: str, tmp_path: Path) -> SQLiteKanban:
    """Create an unconnected SQLiteKanban instance."""
    return SQLiteKanban(
        {
            "db_path": db_path,
            "project_name": "Typed Outputs Project",
            "attachments_dir": str(tmp_path / "attachments"),
        }
    )


def _sample_task_data(**overrides: Any) -> Dict[str, Any]:
    """Create sample task data for the provider with overrides."""
    data: Dict[str, Any] = {
        "name": "Identify the source of the excerpt",
        "description": "Find headline, publisher, date, and URL",
        "priority": "high",
        "estimated_hours": 1.0,
        "labels": ["tow"],
        "dependencies": [],
    }
    data.update(overrides)
    return data


class TestTaskTypedOutputSqliteRoundTrip:
    """Test suite for persisting the new fields in SQLiteKanban."""

    @pytest.mark.asyncio
    async def test_task_round_trips_with_type_and_schema(
        self, kanban: SQLiteKanban
    ) -> None:
        """A task keeps task_type, inputs, and output_schema on reload."""
        await kanban.connect()
        schema = {
            "type": "object",
            "required": ["headline", "publisher", "date", "url"],
        }
        inputs = {"excerpt": "Collectively, they provided..."}

        created = await kanban.create_task(
            _sample_task_data(
                task_type="find",
                inputs=inputs,
                output_schema=schema,
            )
        )
        loaded = await kanban.get_task_by_id(created.id)

        assert loaded is not None
        assert loaded.task_type == "find"
        assert loaded.inputs == inputs
        assert loaded.output_schema == schema

    @pytest.mark.asyncio
    async def test_task_without_type_defaults_to_implement(
        self, kanban: SQLiteKanban
    ) -> None:
        """A task created without the new fields loads with defaults."""
        await kanban.connect()

        created = await kanban.create_task(_sample_task_data())
        loaded = await kanban.get_task_by_id(created.id)

        assert loaded is not None
        assert loaded.task_type == "implement"
        assert loaded.inputs == {}
        assert loaded.output_schema is None

    @pytest.mark.asyncio
    async def test_all_five_task_types_round_trip(self, kanban: SQLiteKanban) -> None:
        """Each of the five task types survives a round trip."""
        await kanban.connect()

        for task_type in ("find", "verify", "approve", "synthesize", "implement"):
            created = await kanban.create_task(_sample_task_data(task_type=task_type))
            loaded = await kanban.get_task_by_id(created.id)
            assert loaded is not None
            assert loaded.task_type == task_type


# The tasks table exactly as it existed before this change (develop at
# a4236573), so the migration test exercises a genuinely old board.
_LEGACY_TASKS_DDL = """
CREATE TABLE tasks (
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
"""


class TestLegacyBoardMigration:
    """Test suite for loading boards created before task_type existed."""

    @pytest.mark.asyncio
    async def test_legacy_board_loads_with_implement_default(
        self, db_path: str, kanban: SQLiteKanban
    ) -> None:
        """A pre-change board loads and its tasks default to implement.

        Builds a database with the old schema and one row, then
        connects the provider so _migrate_schema adds the new columns.
        The existing task must hydrate with task_type "implement",
        empty inputs, and no output_schema.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(_LEGACY_TASKS_DDL)
            conn.execute(
                "INSERT INTO tasks (id, name, status, priority, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("legacy-1", "Old task", "todo", "medium", now_iso, now_iso),
            )
            conn.commit()
        finally:
            conn.close()

        connected = await kanban.connect()
        assert connected is True

        loaded = await kanban.get_task_by_id("legacy-1")
        assert loaded is not None
        assert loaded.task_type == "implement"
        assert loaded.inputs == {}
        assert loaded.output_schema is None
