"""
Unit tests: a typed seed spec lands on a real SQLite board as given.

Issue #911 end to end, below the MCP layer: ``build_seeded_tasks`` turns a
spec carrying ``task_type``, ``inputs`` and ``id`` into Tasks, and
``create_tasks_on_board`` (``src/integrations/nlp_base.py``) puts them on a
real ``SQLiteKanban`` with those three intact, then writes the
``task_metadata`` row Cato renders with the card's type on it. No MCP
server is involved; the board is a temporary file.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, List

import pytest

from src.core.models import Task
from src.integrations.providers.sqlite_kanban import SQLiteKanban
from src.integrations.seeded_tasks import build_seeded_tasks

pytestmark = pytest.mark.unit

SPECS = [
    {
        "name": "Find the source of tow-0001",
        "id": "tow-0001-find",
        "task_type": "find",
        "inputs": {"case_id": "tow-0001", "query": "who said it"},
        "labels": ["tow", "case:tow-0001"],
        "estimated_hours": 0.1,
        "priority": "high",
    },
    {
        "name": "Verify the claim for tow-0001",
        "id": "tow-0001-verify",
        "task_type": "verify",
        "inputs": {"case_id": "tow-0001"},
        "depends_on": ["Find the source of tow-0001"],
        "labels": ["tow", "case:tow-0001"],
    },
]


def _creator(kanban: SQLiteKanban) -> Any:
    from src.integrations.nlp_base import NaturalLanguageTaskCreator

    class _Creator(NaturalLanguageTaskCreator):
        async def process_natural_language(
            self, description: str, **kwargs: Any
        ) -> List[Task]:
            return []

    creator = _Creator(kanban_client=kanban)
    creator.active_project_id = "reg-proj-1"
    return creator


class TestTypedSeedOnARealBoard:
    """The board row carries the spec's id, type and inputs; the metadata row its type."""

    @pytest.mark.asyncio
    async def test_cards_carry_id_type_inputs_and_dependency(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both cards land under the caller's ids, typed, with inputs and the edge."""
        monkeypatch.setattr(
            "src.integrations.nlp_base.marcus_db_path",
            lambda *a, **k: tmp_path / "marcus.db",
        )
        kanban = SQLiteKanban(
            {"db_path": str(tmp_path / "kanban.db"), "project_name": "p"}
        )
        await kanban.connect()
        try:
            created = await _creator(kanban).create_tasks_on_board(
                build_seeded_tasks(SPECS, "p")
            )
            board = {t.id: t for t in await kanban.get_all_tasks()}
        finally:
            await kanban.disconnect()

        assert sorted(t.id for t in created) == ["tow-0001-find", "tow-0001-verify"]
        find, verify = board["tow-0001-find"], board["tow-0001-verify"]
        assert find.task_type == "find"
        assert find.inputs == {"case_id": "tow-0001", "query": "who said it"}
        assert verify.task_type == "verify"
        assert verify.dependencies == ["tow-0001-find"]

    @pytest.mark.asyncio
    async def test_metadata_row_names_the_cards_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cato's row for a seeded card carries ``task_type`` under the caller's id."""
        meta_db = tmp_path / "marcus.db"
        monkeypatch.setattr(
            "src.integrations.nlp_base.marcus_db_path", lambda *a, **k: meta_db
        )
        kanban = SQLiteKanban(
            {"db_path": str(tmp_path / "kanban.db"), "project_name": "p"}
        )
        await kanban.connect()
        try:
            await _creator(kanban).create_tasks_on_board(build_seeded_tasks(SPECS, "p"))
        finally:
            await kanban.disconnect()

        rows = {
            json.loads(r[0])["task_id"]: json.loads(r[0])
            for r in sqlite3.connect(meta_db).execute(
                "SELECT data FROM persistence WHERE collection='task_metadata'"
            )
        }
        assert set(rows) == {"tow-0001-find", "tow-0001-verify"}
        assert rows["tow-0001-find"]["task_type"] == "find"
        assert rows["tow-0001-verify"]["task_type"] == "verify"
        assert rows["tow-0001-verify"]["project_id"] == "reg-proj-1"
        assert rows["tow-0001-verify"]["dependencies"] == ["tow-0001-find"]
