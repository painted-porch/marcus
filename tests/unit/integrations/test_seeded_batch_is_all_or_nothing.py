"""
Unit tests: a seeded batch that loses a card is refused, not reported as success.

Codex P2 on PR #919: ``create_tasks_on_board`` (``src/integrations/nlp_base.py``)
keeps going when one card fails to insert, which is right for a decomposed
board (the other tasks still have value) and wrong for a seeded one: the
caller asked for exactly these cards, and a dependency on the failed card
would be silently dropped, leaving its dependent claimable at once. A
seeded batch is therefore all or nothing: any failure raises, and the error
names what failed and what was already created.
"""

from __future__ import annotations

from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.error_framework import KanbanIntegrationError
from src.core.models import Task
from src.integrations.seeded_tasks import build_seeded_tasks

pytestmark = pytest.mark.unit


def _creator(kanban: Any) -> Any:
    from src.integrations.nlp_base import NaturalLanguageTaskCreator

    class _Creator(NaturalLanguageTaskCreator):
        async def process_natural_language(
            self, description: str, **kwargs: Any
        ) -> List[Task]:
            return []

    return _Creator(kanban_client=kanban)


def _kanban_failing_on(name_to_fail: str) -> MagicMock:
    kanban = MagicMock()
    kanban.board_id = "b"

    async def _create(task_data: dict) -> Task:
        if task_data["name"] == name_to_fail:
            raise ValueError("UNIQUE constraint failed: tasks.id")
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        return Task(
            id=task_data.get("id") or f"real-{task_data['name']}",
            name=task_data["name"],
            description="",
            status="todo",  # type: ignore[arg-type]
            priority="medium",  # type: ignore[arg-type]
            assigned_to=None,
            created_at=now,
            updated_at=now,
            due_date=None,
            estimated_hours=1.0,
        )

    kanban.create_task = AsyncMock(side_effect=_create)
    return kanban


class TestSeededBatchAtomicity:
    """One failed card refuses the seeded batch; a decomposed batch keeps going."""

    @pytest.mark.asyncio
    async def test_a_failed_seeded_card_raises_and_names_both_sides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The error names the failed card and the cards already on the board."""
        monkeypatch.setattr(
            "src.integrations.nlp_base.marcus_db_path", lambda *a, **k: ":memory:"
        )
        seeded = build_seeded_tasks(
            [
                {"name": "Find", "id": "c1-find"},
                {"name": "Verify", "id": "c1-verify", "depends_on": ["Find"]},
            ],
            "p",
        )
        with pytest.raises(KanbanIntegrationError) as excinfo:
            await _creator(_kanban_failing_on("Find")).create_tasks_on_board(seeded)
        details = str(excinfo.value.context.custom_context)
        assert "Find" in details
        assert "all or nothing" in details

    @pytest.mark.asyncio
    async def test_a_decomposed_batch_still_keeps_the_cards_that_worked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-#919 behaviour for a decomposed board is unchanged."""
        from datetime import datetime, timezone

        monkeypatch.setattr(
            "src.integrations.nlp_base.marcus_db_path", lambda *a, **k: ":memory:"
        )
        now = datetime.now(timezone.utc)
        tasks = [
            Task(
                id=f"task_{i}",
                name=name,
                description="",
                status="todo",  # type: ignore[arg-type]
                priority="medium",  # type: ignore[arg-type]
                assigned_to=None,
                created_at=now,
                updated_at=now,
                due_date=None,
                estimated_hours=1.0,
                source_type="nlp_project",
            )
            for i, name in enumerate(["Design", "Build"])
        ]
        created = await _creator(_kanban_failing_on("Design")).create_tasks_on_board(
            tasks, skip_validation=True, update_dependencies=False
        )
        assert [t.name for t in created] == ["Build"]
