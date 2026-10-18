"""
Unit tests for SQLiteKanban provider.

Tests the SQLite-backed kanban board implementation that provides
zero-infrastructure task management for Marcus experiments.
"""

import asyncio
import base64
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.integrations.providers.sqlite_kanban import SQLiteKanban

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Provide a temporary database path."""
    return str(tmp_path / "test_kanban.db")


@pytest.fixture
def attachments_dir(tmp_path: Path) -> str:
    """Provide a temporary attachments directory."""
    return str(tmp_path / "attachments")


@pytest.fixture
def config(db_path: str, attachments_dir: str) -> Dict[str, Any]:
    """Provide a standard test config."""
    return {
        "db_path": db_path,
        "project_name": "Test Project",
        "attachments_dir": attachments_dir,
    }


@pytest.fixture
def kanban(config: Dict[str, Any]) -> SQLiteKanban:
    """Create an unconnected SQLiteKanban instance."""
    return SQLiteKanban(config)


@pytest.fixture
async def connected_kanban(kanban: SQLiteKanban) -> SQLiteKanban:
    """Create a connected SQLiteKanban instance."""
    await kanban.connect()
    yield kanban  # type: ignore[misc]
    await kanban.disconnect()


def _sample_task_data(**overrides: Any) -> Dict[str, Any]:
    """Create sample task data with optional overrides."""
    data: Dict[str, Any] = {
        "name": "Implement feature X",
        "description": "Build the core logic for feature X",
        "priority": "high",
        "estimated_hours": 4.0,
        "labels": ["backend", "feature"],
        "dependencies": [],
    }
    data.update(overrides)
    return data


# ============================================================
# Phase 1: Schema + Connection
# ============================================================


class TestSQLiteKanbanConnection:
    """Test connection and schema initialization."""

    @pytest.mark.asyncio
    async def test_connect_creates_database_and_all_tables(
        self, kanban: SQLiteKanban, db_path: str
    ) -> None:
        """Test that connect() creates the DB file with all 6 tables."""
        assert not os.path.exists(db_path)
        result = await kanban.connect()
        assert result is True
        assert os.path.exists(db_path)

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' " "ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()

        assert "tasks" in tables
        assert "task_dependencies" in tables
        assert "task_labels" in tables
        assert "comments" in tables
        assert "attachments" in tables
        assert "blockers" in tables

    @pytest.mark.asyncio
    async def test_connect_enables_wal_mode(
        self, kanban: SQLiteKanban, db_path: str
    ) -> None:
        """Test that WAL journal mode is enabled for concurrency."""
        await kanban.connect()
        conn = sqlite3.connect(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    @pytest.mark.asyncio
    async def test_connect_is_idempotent(self, kanban: SQLiteKanban) -> None:
        """Test that calling connect() twice doesn't fail."""
        assert await kanban.connect() is True
        assert await kanban.connect() is True
        assert kanban.connected is True

    @pytest.mark.asyncio
    async def test_disconnect_sets_connected_false(self, kanban: SQLiteKanban) -> None:
        """Test that disconnect() marks provider as disconnected."""
        await kanban.connect()
        assert kanban.connected is True
        await kanban.disconnect()
        assert kanban.connected is False

    @pytest.mark.asyncio
    async def test_schema_version_is_set(
        self, kanban: SQLiteKanban, db_path: str
    ) -> None:
        """Test that PRAGMA user_version is set after schema init."""
        await kanban.connect()
        conn = sqlite3.connect(db_path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == 1

    @pytest.mark.asyncio
    async def test_schema_has_correct_indexes(
        self, kanban: SQLiteKanban, db_path: str
    ) -> None:
        """Test that performance indexes are created."""
        await kanban.connect()
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        conn.close()

        expected = {
            "idx_tasks_status",
            "idx_tasks_assigned",
            "idx_tasks_project",
            "idx_tasks_parent",
            "idx_tasks_available",
            "idx_comments_task",
            "idx_attachments_task",
            "idx_blockers_task",
        }
        assert expected.issubset(indexes)


# ============================================================
# Phase 2: Task Creation + Retrieval
# ============================================================


class TestSQLiteKanbanCreateTask:
    """Test task creation."""

    @pytest.mark.asyncio
    async def test_create_task_returns_task_with_uuid_id(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that created task has a UUID string ID."""
        task = await connected_kanban.create_task(_sample_task_data())
        assert isinstance(task, Task)
        assert isinstance(task.id, str)
        assert len(task.id) == 32  # uuid4 hex

    @pytest.mark.asyncio
    async def test_create_task_sets_fields_correctly(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that all input fields are stored on the Task."""
        task = await connected_kanban.create_task(
            _sample_task_data(
                name="My Task",
                description="Do the thing",
                priority="urgent",
                estimated_hours=8.0,
            )
        )
        assert task.name == "My Task"
        assert task.description == "Do the thing"
        assert task.priority == Priority.URGENT
        assert task.estimated_hours == 8.0
        assert task.status == TaskStatus.TODO

    @pytest.mark.asyncio
    async def test_create_task_stores_labels(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that labels are persisted in the junction table."""
        task = await connected_kanban.create_task(
            _sample_task_data(labels=["backend", "urgent", "api"])
        )
        assert sorted(task.labels) == ["api", "backend", "urgent"]

    @pytest.mark.asyncio
    async def test_create_task_stores_dependencies(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that dependencies are stored in junction table."""
        task = await connected_kanban.create_task(
            _sample_task_data(dependencies=["dep-1", "dep-2"])
        )
        assert sorted(task.dependencies) == ["dep-1", "dep-2"]

    @pytest.mark.asyncio
    async def test_create_task_sets_timestamps(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that created_at and updated_at are set."""
        before = datetime.now(timezone.utc)
        task = await connected_kanban.create_task(_sample_task_data())
        after = datetime.now(timezone.utc)
        assert before <= task.created_at <= after
        assert before <= task.updated_at <= after

    @pytest.mark.asyncio
    async def test_create_task_maps_status_string(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test status string mapping to TaskStatus enum."""
        task = await connected_kanban.create_task(
            _sample_task_data(status="in_progress")
        )
        assert task.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_create_task_default_status_is_todo(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that tasks default to TODO status."""
        task = await connected_kanban.create_task(_sample_task_data())
        assert task.status == TaskStatus.TODO

    @pytest.mark.asyncio
    async def test_create_task_with_subtask_fields(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test subtask relationship fields are stored."""
        parent = await connected_kanban.create_task(
            _sample_task_data(name="Parent Task")
        )
        child = await connected_kanban.create_task(
            _sample_task_data(
                name="Child Task",
                is_subtask=True,
                parent_task_id=parent.id,
                subtask_index=0,
            )
        )
        assert child.is_subtask is True
        assert child.parent_task_id == parent.id
        assert child.subtask_index == 0

    @pytest.mark.asyncio
    async def test_create_task_with_project_fields(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test project context fields are stored."""
        task = await connected_kanban.create_task(
            _sample_task_data(
                project_id="proj-123",
                project_name="My Project",
            )
        )
        assert task.project_id == "proj-123"
        assert task.project_name == "My Project"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_create_task_serializes_dataclass_in_source_context(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Dataclass objects nested in source_context must serialize.

        Regression test for the recipe-revert-haiku failure: per-feature
        Implement and Test tasks carry UserOutcome dataclass instances
        in their source_context to support outcome-coverage tracking.
        The SQLite kanban writer used plain ``json.dumps`` which raised
        ``TypeError: Object of type UserOutcome is not JSON serializable``,
        causing every Implement/Test task to be silently dropped while
        Design/foundation tasks (which carry plain dicts) made it to
        the board.

        The fix wires a default encoder that handles dataclasses,
        Pydantic models, and other objects exposing ``model_dump`` /
        ``dict`` / ``__dict__``.
        """
        from dataclasses import dataclass

        @dataclass
        class FakeOutcome:
            id: str
            action: str

        outcome = FakeOutcome(id="outcome_login", action="user can log in")

        task = await connected_kanban.create_task(
            _sample_task_data(
                source_type="feature_based",
                source_context={"covers_outcomes": [outcome], "domain": "auth"},
            )
        )

        assert task.id is not None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_create_task_serializes_dataclass_in_completion_criteria(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Dataclasses in completion_criteria also must serialize."""
        from dataclasses import dataclass

        @dataclass
        class Criterion:
            description: str
            verifier: str

        task = await connected_kanban.create_task(
            _sample_task_data(
                completion_criteria=[
                    Criterion(description="login works", verifier="curl")
                ],
            )
        )

        assert task.id is not None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_create_task_serializes_nested_non_json_primitives(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Nested datetime/UUID/Path/Enum values must serialize.

        Codex P2 on PR #504: ``model_dump()`` and ``dataclasses.asdict()``
        return Python objects unchanged for non-JSON-native types
        (``datetime``, ``UUID``, ``Path``, ``Enum``).  Without explicit
        handling for those types in ``_json_default``, the encoder
        recursively invokes itself on a ``datetime`` value, has no
        protocol match, and raises — same silent task-drop symptom as
        the original bug, just one level deeper.

        Pins coverage for the common stdlib types embedded inside
        dataclass / Pydantic-model fields.
        """
        from dataclasses import dataclass
        from datetime import datetime, timezone
        from enum import Enum
        from pathlib import Path
        from uuid import uuid4

        class Severity(Enum):
            HIGH = "high"

        @dataclass
        class Diagnostic:
            id_: object
            created_at: datetime
            workspace: Path
            severity: Severity

        task = await connected_kanban.create_task(
            _sample_task_data(
                source_context={
                    "diagnostic": Diagnostic(
                        id_=uuid4(),
                        created_at=datetime.now(timezone.utc),
                        workspace=Path(__file__).parent,
                        severity=Severity.HIGH,
                    )
                },
            )
        )

        assert task.id is not None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_create_task_handles_broken_dict_method(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Objects whose ``.dict()`` raises must fall through to other paths.

        Pydantic-shaped objects (and arbitrary user code) may expose a
        callable ``dict`` attribute that raises when invoked without
        args — e.g. a stub method or a class whose ``dict`` shadows the
        builtin.  The encoder must catch and fall through to dataclass /
        stdlib / ``__dict__`` branches rather than propagating.
        """

        class Wrapper:
            def __init__(self, value: str) -> None:
                self.value = value

            def dict(self) -> Any:  # noqa: D401, A003
                raise TypeError("intentional break")

        task = await connected_kanban.create_task(
            _sample_task_data(source_context={"wrapped": Wrapper("ok")}),
        )

        assert task.id is not None


class TestSQLiteKanbanGetTasks:
    """Test task retrieval methods."""

    @pytest.mark.asyncio
    async def test_get_all_tasks_returns_all(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test get_all_tasks returns tasks regardless of status."""
        await connected_kanban.create_task(_sample_task_data(name="Task 1"))
        await connected_kanban.create_task(
            _sample_task_data(name="Task 2", status="in_progress")
        )
        await connected_kanban.create_task(
            _sample_task_data(name="Task 3", status="done")
        )
        tasks = await connected_kanban.get_all_tasks()
        assert len(tasks) == 3

    @pytest.mark.asyncio
    async def test_get_all_tasks_hydrates_labels_and_deps(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that retrieved tasks include labels and deps."""
        await connected_kanban.create_task(
            _sample_task_data(
                labels=["api", "core"],
                dependencies=["dep-a"],
            )
        )
        tasks = await connected_kanban.get_all_tasks()
        assert sorted(tasks[0].labels) == ["api", "core"]
        assert tasks[0].dependencies == ["dep-a"]

    @pytest.mark.asyncio
    async def test_get_available_tasks_unassigned_todo_only(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that only unassigned TODO tasks are returned."""
        # Available: TODO + unassigned
        await connected_kanban.create_task(_sample_task_data(name="Available"))
        # Not available: assigned
        t2 = await connected_kanban.create_task(_sample_task_data(name="Assigned"))
        await connected_kanban.assign_task(t2.id, "agent-1")
        # Not available: done
        await connected_kanban.create_task(
            _sample_task_data(name="Done", status="done")
        )

        available = await connected_kanban.get_available_tasks()
        assert len(available) == 1
        assert available[0].name == "Available"

    @pytest.mark.asyncio
    async def test_get_task_by_id_found(self, connected_kanban: SQLiteKanban) -> None:
        """Test retrieving an existing task by ID."""
        created = await connected_kanban.create_task(_sample_task_data(name="Find Me"))
        found = await connected_kanban.get_task_by_id(created.id)
        assert found is not None
        assert found.name == "Find Me"
        assert found.id == created.id

    @pytest.mark.asyncio
    async def test_get_task_by_id_not_found(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that missing task returns None."""
        result = await connected_kanban.get_task_by_id("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_all_tasks_empty_board(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test get_all_tasks on empty board returns empty list."""
        tasks = await connected_kanban.get_all_tasks()
        assert tasks == []


# ============================================================
# Phase 3: Assignment + Status Transitions
# ============================================================


class TestSQLiteKanbanAssignment:
    """Test task assignment and status transitions."""

    @pytest.mark.asyncio
    async def test_assign_task_sets_assigned_to(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test assign_task sets the assigned_to field."""
        task = await connected_kanban.create_task(_sample_task_data())
        result = await connected_kanban.assign_task(task.id, "agent-1")
        assert result is True
        updated = await connected_kanban.get_task_by_id(task.id)
        assert updated is not None
        assert updated.assigned_to == "agent-1"

    @pytest.mark.asyncio
    async def test_assign_task_moves_to_in_progress(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test assign_task auto-moves to IN_PROGRESS."""
        task = await connected_kanban.create_task(_sample_task_data())
        await connected_kanban.assign_task(task.id, "agent-1")
        updated = await connected_kanban.get_task_by_id(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_assign_task_adds_timestamped_comment(
        self, connected_kanban: SQLiteKanban, db_path: str
    ) -> None:
        """Test assign_task adds a coordination comment."""
        task = await connected_kanban.create_task(_sample_task_data())
        await connected_kanban.assign_task(task.id, "agent-1")

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT content FROM comments WHERE task_id = ?",
            (task.id,),
        ).fetchall()
        conn.close()

        assert len(rows) == 1
        assert "agent-1" in rows[0][0]
        assert "assigned" in rows[0][0].lower()


class TestSQLiteKanbanMoveTask:
    """Test column/status movement."""

    @pytest.mark.asyncio
    async def test_move_to_column_updates_status(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test move_task_to_column updates the status."""
        task = await connected_kanban.create_task(_sample_task_data())
        result = await connected_kanban.move_task_to_column(task.id, "in progress")
        assert result is True
        updated = await connected_kanban.get_task_by_id(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_move_handles_case_insensitive(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test column names are case-insensitive."""
        task = await connected_kanban.create_task(_sample_task_data())
        await connected_kanban.move_task_to_column(task.id, "In Progress")
        updated = await connected_kanban.get_task_by_id(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_move_maps_column_aliases(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test column name aliases map correctly."""
        task = await connected_kanban.create_task(_sample_task_data())

        # "completed" → done
        await connected_kanban.move_task_to_column(task.id, "completed")
        updated = await connected_kanban.get_task_by_id(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.DONE

        # "on hold" → blocked
        await connected_kanban.move_task_to_column(task.id, "on hold")
        updated = await connected_kanban.get_task_by_id(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.BLOCKED

        # "backlog" → todo
        await connected_kanban.move_task_to_column(task.id, "backlog")
        updated = await connected_kanban.get_task_by_id(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.TODO


class TestSQLiteKanbanUpdateTask:
    """Test compound update_task behavior."""

    @pytest.mark.asyncio
    async def test_update_task_compound_status_and_assignment(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test update_task handles status + assigned_to together."""
        task = await connected_kanban.create_task(_sample_task_data())
        updated = await connected_kanban.update_task(
            task.id,
            {
                "status": TaskStatus.IN_PROGRESS,
                "assigned_to": "agent-1",
            },
        )
        assert updated is not None
        assert updated.status == TaskStatus.IN_PROGRESS
        assert updated.assigned_to == "agent-1"

    @pytest.mark.asyncio
    async def test_update_task_updates_timestamp(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that update_task refreshes updated_at."""
        task = await connected_kanban.create_task(_sample_task_data())
        original_updated = task.updated_at
        # Small delay to ensure timestamp differs
        await asyncio.sleep(0.01)
        updated = await connected_kanban.update_task(task.id, {"name": "New Name"})
        assert updated is not None
        assert updated.updated_at > original_updated

    @pytest.mark.asyncio
    async def test_update_task_nonexistent_returns_none(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test updating non-existent task returns None."""
        result = await connected_kanban.update_task("nonexistent", {"name": "Nope"})
        assert result is None

    @pytest.mark.asyncio
    async def test_update_task_status_to_done(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test updating status to DONE."""
        task = await connected_kanban.create_task(_sample_task_data())
        updated = await connected_kanban.update_task(
            task.id, {"status": TaskStatus.DONE}
        )
        assert updated is not None
        assert updated.status == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_update_task_with_completed_at(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test storing completed_at timestamp."""
        task = await connected_kanban.create_task(_sample_task_data())
        now = datetime.now(timezone.utc).isoformat()
        updated = await connected_kanban.update_task(
            task.id,
            {"status": TaskStatus.DONE, "completed_at": now},
        )
        assert updated is not None
        assert updated.status == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_update_task_recovery_back_to_todo(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test recovery flow: move assigned task back to TODO."""
        task = await connected_kanban.create_task(_sample_task_data())
        await connected_kanban.assign_task(task.id, "agent-1")
        # Recovery: return to backlog
        updated = await connected_kanban.update_task(
            task.id,
            {"status": TaskStatus.TODO, "assigned_to": None},
        )
        assert updated is not None
        assert updated.status == TaskStatus.TODO
        assert updated.assigned_to is None

    @pytest.mark.asyncio
    async def test_update_task_source_context_merges_into_existing(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Codex P1 on PR #660: ``source_context`` updates MUST merge.

        Without merging, an anchoring update like ``{"scaffold_path":
        "src/core/gameEngine.js"}`` would clobber the ``contract_file``
        and ``responsibility`` fields the decomposer wrote at task
        creation. Verify both old and new keys survive after the
        merge.
        """
        data = _sample_task_data()
        data["source_context"] = {
            "contract_file": "docs/arch/engine.md",
            "responsibility": "owns engine contract",
        }
        task = await connected_kanban.create_task(data)

        # Anchor the scaffold path — must NOT lose contract_file /
        # responsibility.
        updated = await connected_kanban.update_task(
            task.id,
            {"source_context": {"scaffold_path": "src/core/gameEngine.js"}},
        )
        assert updated is not None
        assert updated.source_context == {
            "contract_file": "docs/arch/engine.md",
            "responsibility": "owns engine contract",
            "scaffold_path": "src/core/gameEngine.js",
        }

    @pytest.mark.asyncio
    async def test_update_task_source_context_new_value_overrides_existing(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """New value for an existing key wins (standard dict merge semantics)."""
        data = _sample_task_data()
        data["source_context"] = {"scaffold_path": "src/old/path.js"}
        task = await connected_kanban.create_task(data)

        updated = await connected_kanban.update_task(
            task.id,
            {"source_context": {"scaffold_path": "src/new/path.js"}},
        )
        assert updated is not None
        assert updated.source_context == {"scaffold_path": "src/new/path.js"}

    @pytest.mark.asyncio
    async def test_update_task_source_context_empty_dict_is_no_op(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """An empty ``source_context`` dict must not erase the existing value.

        Codex catch: ``{"source_context": {}}`` previously fell through
        the entire elif chain silently. Now it explicitly no-ops on the
        column so updated_at doesn't churn either.
        """
        data = _sample_task_data()
        data["source_context"] = {"contract_file": "docs/arch.md"}
        task = await connected_kanban.create_task(data)

        updated = await connected_kanban.update_task(
            task.id,
            {"source_context": {}},
        )
        assert updated is not None
        assert updated.source_context == {"contract_file": "docs/arch.md"}

    @pytest.mark.asyncio
    async def test_update_task_source_context_none_clears_field(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """``source_context = None`` is the explicit "drop everything" signal."""
        data = _sample_task_data()
        data["source_context"] = {"contract_file": "docs/arch.md"}
        task = await connected_kanban.create_task(data)

        updated = await connected_kanban.update_task(
            task.id,
            {"source_context": None},
        )
        assert updated is not None
        # The model normalizes a cleared source_context to either
        # None or an empty dict depending on the deserializer. Either
        # form is acceptable; what matters is that the prior keys
        # are gone.
        assert not updated.source_context

    @pytest.mark.asyncio
    async def test_update_task_source_context_merges_when_no_prior(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """A task created without source_context can still be anchored later."""
        data = _sample_task_data()
        # No source_context at create time.
        task = await connected_kanban.create_task(data)

        updated = await connected_kanban.update_task(
            task.id,
            {"source_context": {"scaffold_path": "src/core/engine.js"}},
        )
        assert updated is not None
        assert updated.source_context == {"scaffold_path": "src/core/engine.js"}


# ============================================================
# Phase 4: Comments + Progress + Blockers + Metrics
# ============================================================


class TestSQLiteKanbanComments:
    """Test comment storage."""

    @pytest.mark.asyncio
    async def test_add_comment_stores_with_timestamp(
        self, connected_kanban: SQLiteKanban, db_path: str
    ) -> None:
        """Test comments are stored with created_at."""
        task = await connected_kanban.create_task(_sample_task_data())
        result = await connected_kanban.add_comment(task.id, "Hello from agent")
        assert result is True

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT content, created_at FROM comments " "WHERE task_id = ?",
            (task.id,),
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "Hello from agent"
        assert rows[0][1] is not None

    @pytest.mark.asyncio
    async def test_add_multiple_comments(
        self, connected_kanban: SQLiteKanban, db_path: str
    ) -> None:
        """Test multiple comments on one task."""
        task = await connected_kanban.create_task(_sample_task_data())
        await connected_kanban.add_comment(task.id, "Comment 1")
        await connected_kanban.add_comment(task.id, "Comment 2")
        await connected_kanban.add_comment(task.id, "Comment 3")

        conn = sqlite3.connect(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE task_id = ?",
            (task.id,),
        ).fetchone()[0]
        conn.close()
        assert count == 3


class TestSQLiteKanbanBlocker:
    """Test blocker reporting."""

    @pytest.mark.asyncio
    async def test_report_blocker_moves_to_blocked(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test report_blocker moves task to BLOCKED status."""
        task = await connected_kanban.create_task(_sample_task_data())
        await connected_kanban.assign_task(task.id, "agent-1")
        result = await connected_kanban.report_blocker(task.id, "API is down", "high")
        assert result is True
        updated = await connected_kanban.get_task_by_id(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_report_blocker_keeps_agent_assigned(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that blocker does NOT unassign the agent."""
        task = await connected_kanban.create_task(_sample_task_data())
        await connected_kanban.assign_task(task.id, "agent-1")
        await connected_kanban.report_blocker(task.id, "Dependency failed", "medium")
        updated = await connected_kanban.get_task_by_id(task.id)
        assert updated is not None
        assert updated.assigned_to == "agent-1"

    @pytest.mark.asyncio
    async def test_report_blocker_adds_comment(
        self, connected_kanban: SQLiteKanban, db_path: str
    ) -> None:
        """Test that blocker adds a structured comment."""
        task = await connected_kanban.create_task(_sample_task_data())
        await connected_kanban.report_blocker(task.id, "Can't connect to DB", "high")
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT content FROM comments WHERE task_id = ?",
            (task.id,),
        ).fetchall()
        conn.close()
        # Should have a blocker comment
        contents = [r[0] for r in rows]
        assert any("BLOCKER" in c and "HIGH" in c for c in contents)

    @pytest.mark.asyncio
    async def test_report_blocker_stores_in_blockers_table(
        self, connected_kanban: SQLiteKanban, db_path: str
    ) -> None:
        """Test blocker is persisted in the blockers table."""
        task = await connected_kanban.create_task(_sample_task_data())
        await connected_kanban.report_blocker(task.id, "Network timeout", "medium")
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT description, severity FROM blockers " "WHERE task_id = ?",
            (task.id,),
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "Network timeout"
        assert rows[0][1] == "medium"


class TestSQLiteKanbanProgress:
    """Test progress reporting."""

    @pytest.mark.asyncio
    async def test_update_progress_adds_comment(
        self, connected_kanban: SQLiteKanban, db_path: str
    ) -> None:
        """Test progress update adds formatted comment."""
        task = await connected_kanban.create_task(_sample_task_data())
        result = await connected_kanban.update_task_progress(
            task.id,
            {
                "progress": 50,
                "status": "in_progress",
                "message": "Halfway done",
            },
        )
        assert result is True

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT content FROM comments WHERE task_id = ?",
            (task.id,),
        ).fetchall()
        conn.close()
        contents = [r[0] for r in rows]
        assert any("50%" in c and "Halfway done" in c for c in contents)

    @pytest.mark.asyncio
    async def test_update_progress_at_100_moves_to_done(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test 100% progress moves task to DONE."""
        task = await connected_kanban.create_task(_sample_task_data())
        await connected_kanban.update_task_progress(
            task.id,
            {
                "progress": 100,
                "status": "completed",
                "message": "All done",
            },
        )
        updated = await connected_kanban.get_task_by_id(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_update_progress_blocked_moves_to_blocked(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test blocked progress moves task to BLOCKED."""
        task = await connected_kanban.create_task(_sample_task_data())
        await connected_kanban.update_task_progress(
            task.id,
            {
                "progress": 30,
                "status": "blocked",
                "message": "Waiting on API",
            },
        )
        updated = await connected_kanban.get_task_by_id(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.BLOCKED


class TestSQLiteKanbanMetrics:
    """Test project metrics."""

    @pytest.mark.asyncio
    async def test_get_project_metrics_counts_by_status(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test metrics accurately count tasks by status."""
        await connected_kanban.create_task(_sample_task_data(name="T1"))
        await connected_kanban.create_task(_sample_task_data(name="T2"))
        t3 = await connected_kanban.create_task(_sample_task_data(name="T3"))
        await connected_kanban.assign_task(t3.id, "agent-1")
        await connected_kanban.create_task(_sample_task_data(name="T4", status="done"))

        metrics = await connected_kanban.get_project_metrics()
        assert metrics["total_tasks"] == 4
        assert metrics["backlog_tasks"] == 2
        assert metrics["in_progress_tasks"] == 1
        assert metrics["completed_tasks"] == 1

    @pytest.mark.asyncio
    async def test_get_project_metrics_empty_board(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test metrics on empty board returns zeros."""
        metrics = await connected_kanban.get_project_metrics()
        assert metrics["total_tasks"] == 0
        assert metrics["backlog_tasks"] == 0


# ============================================================
# Phase 5: Attachments
# ============================================================


class TestSQLiteKanbanAttachments:
    """Test attachment handling."""

    @pytest.mark.asyncio
    async def test_upload_attachment_stores_file(
        self, connected_kanban: SQLiteKanban, attachments_dir: str
    ) -> None:
        """Test upload creates file on disk and metadata in DB."""
        task = await connected_kanban.create_task(_sample_task_data())
        content = base64.b64encode(b"Hello World").decode()
        result = await connected_kanban.upload_attachment(
            task.id, "test.txt", content, "text/plain"
        )
        assert result["success"] is True
        assert result["data"]["filename"] == "test.txt"
        assert result["data"]["size"] > 0

    @pytest.mark.asyncio
    async def test_get_attachments_returns_list(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test get_attachments returns stored attachments."""
        task = await connected_kanban.create_task(_sample_task_data())
        content = base64.b64encode(b"data").decode()
        await connected_kanban.upload_attachment(task.id, "file1.txt", content)
        await connected_kanban.upload_attachment(task.id, "file2.txt", content)
        result = await connected_kanban.get_attachments(task.id)
        assert result["success"] is True
        assert len(result["data"]) == 2

    @pytest.mark.asyncio
    async def test_download_attachment_returns_base64(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test download returns base64-encoded content."""
        task = await connected_kanban.create_task(_sample_task_data())
        original = b"Test content for download"
        encoded = base64.b64encode(original).decode()
        upload_result = await connected_kanban.upload_attachment(
            task.id, "download_me.txt", encoded, "text/plain"
        )
        att_id = upload_result["data"]["id"]

        result = await connected_kanban.download_attachment(
            att_id, "download_me.txt", task.id
        )
        assert result["success"] is True
        decoded = base64.b64decode(result["data"]["content"])
        assert decoded == original

    @pytest.mark.asyncio
    async def test_delete_attachment_removes_file_and_row(
        self, connected_kanban: SQLiteKanban, db_path: str
    ) -> None:
        """Test delete removes both file and DB record."""
        task = await connected_kanban.create_task(_sample_task_data())
        content = base64.b64encode(b"delete me").decode()
        upload = await connected_kanban.upload_attachment(
            task.id, "doomed.txt", content
        )
        att_id = upload["data"]["id"]

        result = await connected_kanban.delete_attachment(att_id, task.id)
        assert result["success"] is True

        conn = sqlite3.connect(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE id = ?",
            (att_id,),
        ).fetchone()[0]
        conn.close()
        assert count == 0


# ============================================================
# Phase 5b: Project Management
# ============================================================


class TestSQLiteKanbanProjects:
    """Test project CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_project(self, connected_kanban: SQLiteKanban) -> None:
        """Test creating a project returns record with ID."""
        project = await connected_kanban.create_project("My Project", "A test project")
        assert project["id"]
        assert project["name"] == "My Project"
        assert project["description"] == "A test project"
        assert project["is_active"] is True

    @pytest.mark.asyncio
    async def test_list_projects(self, connected_kanban: SQLiteKanban) -> None:
        """Test listing active projects."""
        await connected_kanban.create_project("Project A")
        await connected_kanban.create_project("Project B")
        projects = await connected_kanban.list_projects()
        assert len(projects) == 2
        names = {p["name"] for p in projects}
        assert names == {"Project A", "Project B"}

    @pytest.mark.asyncio
    async def test_list_projects_includes_task_count(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that list includes task count per project."""
        proj = await connected_kanban.create_project("Counted")
        await connected_kanban.create_task(
            _sample_task_data(name="T1", project_id=proj["id"])
        )
        await connected_kanban.create_task(
            _sample_task_data(name="T2", project_id=proj["id"])
        )
        projects = await connected_kanban.list_projects()
        assert projects[0]["task_count"] == 2

    @pytest.mark.asyncio
    async def test_soft_delete_project(self, connected_kanban: SQLiteKanban) -> None:
        """Test soft delete marks project inactive."""
        proj = await connected_kanban.create_project("Doomed")
        result = await connected_kanban.delete_project(proj["id"])
        assert result is True
        # Should not appear in list_projects
        projects = await connected_kanban.list_projects()
        assert len(projects) == 0

    @pytest.mark.asyncio
    async def test_hard_delete_project_cascades(
        self, connected_kanban: SQLiteKanban, db_path: str
    ) -> None:
        """Test hard delete removes project and all its tasks."""
        proj = await connected_kanban.create_project("Nuke Me")
        task = await connected_kanban.create_task(
            _sample_task_data(name="Task in project", project_id=proj["id"])
        )
        await connected_kanban.add_comment(task.id, "A comment")
        result = await connected_kanban.delete_project(proj["id"], hard_delete=True)
        assert result is True
        # Tasks should be gone
        tasks = await connected_kanban.get_all_tasks()
        assert len(tasks) == 0
        # Project should be gone
        got = await connected_kanban.get_project(proj["id"])
        assert got is None

    @pytest.mark.asyncio
    async def test_get_project(self, connected_kanban: SQLiteKanban) -> None:
        """Test getting a single project by ID."""
        proj = await connected_kanban.create_project("Find Me")
        got = await connected_kanban.get_project(proj["id"])
        assert got is not None
        assert got["name"] == "Find Me"

    @pytest.mark.asyncio
    async def test_get_project_not_found(self, connected_kanban: SQLiteKanban) -> None:
        """Test getting non-existent project returns None."""
        got = await connected_kanban.get_project("nonexistent")
        assert got is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_project(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test deleting non-existent project returns False."""
        result = await connected_kanban.delete_project("nonexistent")
        assert result is False


# ============================================================
# Phase 6: Config + Factory Integration
# ============================================================


class TestSQLiteKanbanFactory:
    """Test factory registration and config integration."""

    @pytest.mark.asyncio
    async def test_factory_creates_sqlite_provider(self, tmp_path: Path) -> None:
        """Test KanbanFactory.create('sqlite') works."""
        from unittest.mock import MagicMock

        from src.integrations.kanban_factory import KanbanFactory

        mock_config = MagicMock()
        mock_config.kanban.sqlite_db_path = str(tmp_path / "factory_test.db")
        mock_config.kanban.board_name = "Test"
        mock_config.kanban.sqlite_attachments_dir = str(tmp_path / "att")

        with patch(
            "src.integrations.kanban_factory.get_config",
            return_value=mock_config,
        ):
            kanban = KanbanFactory.create(
                "sqlite",
                {
                    "db_path": str(tmp_path / "factory.db"),
                    "project_name": "Test",
                },
            )
        assert isinstance(kanban, SQLiteKanban)

    @pytest.mark.asyncio
    async def test_sqlite_in_kanban_provider_enum(self) -> None:
        """Test SQLITE is in the KanbanProvider enum."""
        from src.integrations.kanban_interface import KanbanProvider

        assert hasattr(KanbanProvider, "SQLITE")
        assert KanbanProvider.SQLITE.value == "sqlite"

    @pytest.mark.asyncio
    async def test_config_provider_sqlite_accepted(self, tmp_path: Path) -> None:
        """Test that provider='sqlite' is a valid config value."""
        from src.config.marcus_config import KanbanSettings

        settings = KanbanSettings(
            provider="sqlite",
            sqlite_db_path=str(tmp_path / "config_test.db"),
        )
        assert settings.provider == "sqlite"


# ============================================================
# Project Scoping Tests
# ============================================================


class TestSQLiteKanbanProjectScoping:
    """Test that multiple experiments are isolated by project_id."""

    @pytest.mark.asyncio
    async def test_auto_setup_project_generates_ids(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test auto_setup_project sets project_id and board_id."""
        result = await connected_kanban.auto_setup_project(project_name="Experiment 1")
        assert "project_id" in result
        assert "board_id" in result
        assert len(result["project_id"]) == 32
        assert len(result["board_id"]) == 32
        assert connected_kanban.project_id == result["project_id"]
        assert connected_kanban.board_id == result["board_id"]

    @pytest.mark.asyncio
    async def test_tasks_scoped_to_project(self, config: Dict[str, Any]) -> None:
        """Test tasks from different projects don't mix."""
        kanban = SQLiteKanban(config)
        await kanban.connect()

        # Project A
        await kanban.auto_setup_project(project_name="Project A")
        await kanban.create_task(_sample_task_data(name="Task A1"))
        await kanban.create_task(_sample_task_data(name="Task A2"))

        # Project B (same DB file)
        await kanban.auto_setup_project(project_name="Project B")
        await kanban.create_task(_sample_task_data(name="Task B1"))

        # Should only see Project B's task
        tasks = await kanban.get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].name == "Task B1"

        available = await kanban.get_available_tasks()
        assert len(available) == 1
        assert available[0].name == "Task B1"

    @pytest.mark.asyncio
    async def test_metrics_scoped_to_project(self, config: Dict[str, Any]) -> None:
        """Test metrics only count tasks in current project."""
        kanban = SQLiteKanban(config)
        await kanban.connect()

        # Project A: 3 tasks
        await kanban.auto_setup_project(project_name="Project A")
        await kanban.create_task(_sample_task_data(name="A1"))
        await kanban.create_task(_sample_task_data(name="A2"))
        await kanban.create_task(_sample_task_data(name="A3"))

        # Project B: 1 task
        await kanban.auto_setup_project(project_name="Project B")
        await kanban.create_task(_sample_task_data(name="B1"))

        metrics = await kanban.get_project_metrics()
        assert metrics["total_tasks"] == 1

    @pytest.mark.asyncio
    async def test_project_id_stored_on_tasks(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that tasks get the current project_id."""
        result = await connected_kanban.auto_setup_project(
            project_name="Scoped Project"
        )
        task = await connected_kanban.create_task(_sample_task_data(name="Scoped Task"))
        assert task.project_id == result["project_id"]

    @pytest.mark.asyncio
    async def test_get_task_by_id_works_across_projects(
        self, config: Dict[str, Any]
    ) -> None:
        """Test get_task_by_id finds task regardless of project scope."""
        kanban = SQLiteKanban(config)
        await kanban.connect()

        await kanban.auto_setup_project(project_name="Project A")
        task_a = await kanban.create_task(_sample_task_data(name="Task A"))

        # Switch to project B
        await kanban.auto_setup_project(project_name="Project B")

        # Should still find task A by ID (direct lookup)
        found = await kanban.get_task_by_id(task_a.id)
        assert found is not None
        assert found.name == "Task A"


# ============================================================
# Additional coverage tests
# ============================================================


class TestSQLiteKanbanEdgeCases:
    """Edge cases and error paths for coverage."""

    @pytest.mark.asyncio
    async def test_auto_connect_on_create_task(self, kanban: SQLiteKanban) -> None:
        """Test that create_task auto-connects if not connected."""
        assert kanban.connected is False
        task = await kanban.create_task(_sample_task_data())
        assert kanban.connected is True
        assert task.name == "Implement feature X"

    @pytest.mark.asyncio
    async def test_auto_connect_on_get_all_tasks(self, kanban: SQLiteKanban) -> None:
        """Test that get_all_tasks auto-connects."""
        tasks = await kanban.get_all_tasks()
        assert kanban.connected is True
        assert tasks == []

    @pytest.mark.asyncio
    async def test_auto_connect_on_get_available_tasks(
        self, kanban: SQLiteKanban
    ) -> None:
        """Test that get_available_tasks auto-connects."""
        tasks = await kanban.get_available_tasks()
        assert kanban.connected is True
        assert tasks == []

    @pytest.mark.asyncio
    async def test_auto_connect_on_get_task_by_id(self, kanban: SQLiteKanban) -> None:
        """Test that get_task_by_id auto-connects."""
        result = await kanban.get_task_by_id("nonexistent")
        assert kanban.connected is True
        assert result is None

    @pytest.mark.asyncio
    async def test_auto_connect_on_update_task(self, kanban: SQLiteKanban) -> None:
        """Test that update_task auto-connects."""
        result = await kanban.update_task("nonexistent", {"name": "x"})
        assert kanban.connected is True
        assert result is None

    @pytest.mark.asyncio
    async def test_auto_connect_on_assign_task(self, kanban: SQLiteKanban) -> None:
        """Test that assign_task auto-connects."""
        # Will fail because task doesn't exist, but should connect
        await kanban.assign_task("nonexistent", "agent-1")
        assert kanban.connected is True

    @pytest.mark.asyncio
    async def test_auto_connect_on_move_task(self, kanban: SQLiteKanban) -> None:
        """Test that move_task_to_column auto-connects."""
        await kanban.move_task_to_column("nonexistent", "done")
        assert kanban.connected is True

    @pytest.mark.asyncio
    async def test_auto_connect_on_add_comment(self, kanban: SQLiteKanban) -> None:
        """Test that add_comment auto-connects."""
        await kanban.add_comment("nonexistent", "test")
        assert kanban.connected is True

    @pytest.mark.asyncio
    async def test_auto_connect_on_report_blocker(self, kanban: SQLiteKanban) -> None:
        """Test that report_blocker auto-connects."""
        await kanban.report_blocker("nonexistent", "blocked")
        assert kanban.connected is True

    @pytest.mark.asyncio
    async def test_auto_connect_on_progress(self, kanban: SQLiteKanban) -> None:
        """Test that update_task_progress auto-connects."""
        await kanban.update_task_progress("nonexistent", {"progress": 50})
        assert kanban.connected is True

    @pytest.mark.asyncio
    async def test_auto_connect_on_metrics(self, kanban: SQLiteKanban) -> None:
        """Test that get_project_metrics auto-connects."""
        metrics = await kanban.get_project_metrics()
        assert kanban.connected is True
        assert metrics["total_tasks"] == 0

    @pytest.mark.asyncio
    async def test_auto_connect_on_upload(self, kanban: SQLiteKanban) -> None:
        """Test that upload_attachment auto-connects."""
        task = await kanban.create_task(_sample_task_data())
        content = base64.b64encode(b"data").decode()
        result = await kanban.upload_attachment(task.id, "f.txt", content)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_auto_connect_on_get_attachments(self, kanban: SQLiteKanban) -> None:
        """Test that get_attachments auto-connects."""
        result = await kanban.get_attachments("nonexistent")
        assert result["success"] is True
        assert result["data"] == []

    @pytest.mark.asyncio
    async def test_auto_connect_on_download(self, kanban: SQLiteKanban) -> None:
        """Test that download_attachment auto-connects."""
        result = await kanban.download_attachment("x", "f.txt")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_resolve_status_unknown_defaults_to_todo(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that unknown status string defaults to TODO."""
        task = await connected_kanban.create_task(
            _sample_task_data(status="mystery_status")
        )
        assert task.status == TaskStatus.TODO

    @pytest.mark.asyncio
    async def test_create_task_with_source_context(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that source_context JSON is stored and retrieved."""
        ctx = {"origin": "nlp", "confidence": 0.95}
        task = await connected_kanban.create_task(_sample_task_data(source_context=ctx))
        assert task.source_context == ctx

    @pytest.mark.asyncio
    async def test_create_task_with_completion_criteria(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test that completion_criteria JSON is stored."""
        criteria = {"tests_pass": True, "coverage": 80}
        task = await connected_kanban.create_task(
            _sample_task_data(completion_criteria=criteria)
        )
        assert task.completion_criteria == criteria

    @pytest.mark.asyncio
    async def test_update_task_priority(self, connected_kanban: SQLiteKanban) -> None:
        """Test updating priority field."""
        task = await connected_kanban.create_task(_sample_task_data(priority="low"))
        updated = await connected_kanban.update_task(task.id, {"priority": "urgent"})
        assert updated is not None
        assert updated.priority == Priority.URGENT

    @pytest.mark.asyncio
    async def test_update_task_priority_enum(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test updating priority with Priority enum."""
        task = await connected_kanban.create_task(_sample_task_data())
        updated = await connected_kanban.update_task(
            task.id, {"priority": Priority.LOW}
        )
        assert updated is not None
        assert updated.priority == Priority.LOW

    @pytest.mark.asyncio
    async def test_update_task_description(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test updating description."""
        task = await connected_kanban.create_task(_sample_task_data())
        updated = await connected_kanban.update_task(
            task.id, {"description": "New description"}
        )
        assert updated is not None
        assert updated.description == "New description"

    @pytest.mark.asyncio
    async def test_update_task_actual_hours(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test updating actual_hours."""
        task = await connected_kanban.create_task(_sample_task_data())
        updated = await connected_kanban.update_task(task.id, {"actual_hours": 3.5})
        assert updated is not None
        assert updated.actual_hours == 3.5

    @pytest.mark.asyncio
    async def test_update_task_due_date(self, connected_kanban: SQLiteKanban) -> None:
        """Test updating due_date."""
        task = await connected_kanban.create_task(_sample_task_data())
        updated = await connected_kanban.update_task(
            task.id, {"due_date": "2026-04-01T00:00:00+00:00"}
        )
        assert updated is not None
        assert updated.due_date is not None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_attachment(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test deleting attachment that doesn't exist."""
        result = await connected_kanban.delete_attachment("fake-id")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_download_nonexistent_attachment(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test downloading attachment that doesn't exist."""
        result = await connected_kanban.download_attachment("fake-id", "fake.txt")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_create_task_with_priority_enum(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test create_task with Priority enum value."""
        task = await connected_kanban.create_task(
            _sample_task_data(priority=Priority.URGENT)
        )
        assert task.priority == Priority.URGENT

    @pytest.mark.asyncio
    async def test_create_task_with_status_enum(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test create_task with TaskStatus enum value."""
        task = await connected_kanban.create_task(
            _sample_task_data(status=TaskStatus.BLOCKED)
        )
        assert task.status == TaskStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_resolve_status_substring_match(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test substring matching for column names."""
        task = await connected_kanban.create_task(_sample_task_data())
        # "My Progress Board" should match "progress"
        await connected_kanban.move_task_to_column(task.id, "My Progress Board")
        updated = await connected_kanban.get_task_by_id(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_update_task_status_string(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test update_task with status as string."""
        task = await connected_kanban.create_task(_sample_task_data())
        updated = await connected_kanban.update_task(task.id, {"status": "done"})
        assert updated is not None
        assert updated.status == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_update_task_invalid_priority_defaults(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Test update_task with invalid priority defaults to MEDIUM."""
        task = await connected_kanban.create_task(_sample_task_data())
        updated = await connected_kanban.update_task(
            task.id, {"priority": "nonexistent_priority"}
        )
        assert updated is not None
        assert updated.priority == Priority.MEDIUM


# ============================================================
# add_comment subtask resolution
# ============================================================


@pytest.mark.unit
class TestAddCommentSubtaskResolution:
    """Verify add_comment handles subtask IDs by resolving to parent.

    Subtasks live in marcus.db's ``subtasks`` collection, not in
    kanban.db's ``tasks`` table. The ``comments`` table has a
    ``FOREIGN KEY (task_id) REFERENCES tasks(id)`` constraint, so
    passing a subtask id like ``parent_id_sub_N`` directly fails.

    The fix: resolve any subtask id to its parent before inserting.
    Comments on subtasks land on the parent card, which matches how
    subtasks are visually presented (as checklist items on the parent).
    """

    @pytest.mark.asyncio
    async def test_add_comment_with_parent_task_id_succeeds(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """Baseline: comments on real task IDs work."""
        task = await connected_kanban.create_task(_sample_task_data())
        result = await connected_kanban.add_comment(task.id, "test comment")
        assert result is True

    @pytest.mark.asyncio
    async def test_add_comment_with_subtask_id_resolves_to_parent(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """A ``{parent_id}_sub_N`` id must resolve to parent and succeed."""
        parent = await connected_kanban.create_task(_sample_task_data())
        subtask_id = f"{parent.id}_sub_1"

        result = await connected_kanban.add_comment(subtask_id, "subtask comment")

        assert result is True, (
            "add_comment must resolve subtask IDs to parent rather than "
            "fail with FOREIGN KEY constraint"
        )

    @pytest.mark.asyncio
    async def test_add_comment_with_unknown_id_returns_false(
        self, connected_kanban: SQLiteKanban
    ) -> None:
        """An ID that's neither a task nor a subtask format must fail soft."""
        result = await connected_kanban.add_comment("not-a-real-id", "x")
        assert result is False
