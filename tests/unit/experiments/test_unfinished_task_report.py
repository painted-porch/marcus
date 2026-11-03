"""
Unit tests for the end-of-run unfinished-work report.

A run could finish with work silently missing: the last line printed was
``run finished — spawned N ephemeral agents``, and nothing ever said
"task X never got done, so its deliverable is absent". A user had to
watch the live progress line or go inspect the board to discover it, so
a partially-failed run looked exactly like a successful one.

``get_project_status`` names the unfinished tasks (id, name, status and
the blocker text; see ``tests/unit/mcp/test_project_status_unfinished_tasks.py``)
and the session launcher renders them. What remains here is the provider
read path that report needs: the SQLite blocker store.
"""

from datetime import datetime, timezone
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import Priority, Task, TaskStatus

pytestmark = pytest.mark.unit


def _task(
    task_id: str,
    name: str,
    status: TaskStatus,
    source_context: dict | None = None,
) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description=name,
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        labels=[],
        dependencies=[],
        source_context=source_context,
    )


class TestSqliteProviderExposesBlockers:
    """The SQLite provider must offer the read path the report needs.

    It already WRITES blocker descriptions (``update_task`` with a
    ``blocker`` key INSERTs into the ``blockers`` table) but offered no
    way to read them back, so the recorded explanation was unreachable.
    """

    @pytest.mark.asyncio
    async def test_get_task_blockers_returns_written_description(
        self, tmp_path: Any
    ) -> None:
        from src.integrations.providers.sqlite_kanban import SQLiteKanban

        provider = SQLiteKanban({"db_path": str(tmp_path / "k.db")})
        await provider.connect()
        task = await provider.create_task({"name": "Implement CLI", "description": "d"})

        await provider.update_task(task.id, {"blocker": "needs a paid API key"})
        blockers = await provider.get_task_blockers(task.id)

        assert [b["description"] for b in blockers] == ["needs a paid API key"]

    @pytest.mark.asyncio
    async def test_blockers_returned_oldest_first(self, tmp_path: Any) -> None:
        """Order matters: the report shows the most recent (last) one."""
        from src.integrations.providers.sqlite_kanban import SQLiteKanban

        provider = SQLiteKanban({"db_path": str(tmp_path / "k2.db")})
        await provider.connect()
        task = await provider.create_task({"name": "T", "description": "d"})

        await provider.update_task(task.id, {"blocker": "first"})
        await provider.update_task(task.id, {"blocker": "second"})
        blockers = await provider.get_task_blockers(task.id)

        assert [b["description"] for b in blockers] == ["first", "second"]

    @pytest.mark.asyncio
    async def test_no_blockers_returns_empty(self, tmp_path: Any) -> None:
        from src.integrations.providers.sqlite_kanban import SQLiteKanban

        provider = SQLiteKanban({"db_path": str(tmp_path / "k3.db")})
        await provider.connect()
        task = await provider.create_task({"name": "T", "description": "d"})

        assert await provider.get_task_blockers(task.id) == []
