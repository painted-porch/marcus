"""
Unit tests: a seeded card's id that is already on the board is refused up front.

Codex P2 on PR #919: the seed-spec validator checks a caller-supplied ``id``
against the caller's project's tasks, but ``create_project`` mints a new
project (no tasks to pass) and the SQLite board file is shared by every
project on the server, so an id taken by another project's card slipped
through to the insert. SQLite then rejected that one card, the batch went
on without it, and the reply said success for an incomplete board.
Both seeding tools now ask the board for each supplied id before creating
anything (``src/marcus_mcp/tools/nlp.py``).
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus

pytestmark = pytest.mark.unit


def _board_task(task_id: str) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name="Older card",
        description="",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
    )


def _fake_client(taken: set[str]) -> MagicMock:
    from src.integrations.kanban_interface import KanbanProvider

    client = MagicMock()
    client.provider = KanbanProvider.SQLITE
    client.connect = AsyncMock()
    client.get_task_by_id = AsyncMock(
        side_effect=lambda task_id: _board_task(task_id) if task_id in taken else None
    )
    client.get_all_tasks = AsyncMock(return_value=[])
    return client


class TestCreateProjectRefusesATakenId:
    """create_project asks the shared board before the creator runs."""

    async def _call(self, tmp_path: Path, taken: set[str]) -> tuple[dict, MagicMock]:
        from src.marcus_mcp.tools import nlp as nlp_module

        state = MagicMock()
        state.log_event = MagicMock()
        state.events = None
        state.kanban_client = None
        fake_config = MagicMock()
        fake_config.kanban.provider = "sqlite"
        name = f"seed-collision-{len(taken)}"
        nlp_module._recent_create_project_calls.clear()
        try:
            with (
                patch("src.config.marcus_config.get_config", return_value=fake_config),
                patch(
                    "src.integrations.kanban_factory.KanbanFactory.create",
                    return_value=_fake_client(taken),
                ),
                patch(
                    "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
                ) as creator_cls,
            ):
                creator_cls.return_value.create_project_from_description = AsyncMock(
                    return_value={"success": False, "error": "stubbed pipeline"}
                )
                result = await nlp_module.create_project(
                    description="Seed two cards",
                    project_name=name,
                    options={
                        "provider": "sqlite",
                        "project_root": str(tmp_path / "root"),
                        "tasks": [
                            {"name": "Find", "id": "c1-find", "task_type": "find"},
                            {
                                "name": "Verify",
                                "id": "c1-verify",
                                "task_type": "verify",
                                "depends_on": ["Find"],
                            },
                        ],
                    },
                    state=state,
                )
        finally:
            nlp_module._recent_create_project_calls.clear()
        return result, creator_cls

    @pytest.mark.asyncio
    async def test_an_id_held_by_another_project_is_refused_before_any_card(
        self, tmp_path: Path
    ) -> None:
        """The reply names the taken id and the creator never runs."""
        result, creator_cls = await self._call(tmp_path, taken={"c1-find"})
        assert result["success"] is False
        assert result["error_code"] == "tasks_invalid"
        assert (
            "tasks[0].id 'c1-find' already identifies a board task" in result["error"]
        )
        assert not creator_cls.called

    @pytest.mark.asyncio
    async def test_free_ids_reach_the_creator(self, tmp_path: Path) -> None:
        """No collision: the pipeline runs (stubbed here)."""
        result, creator_cls = await self._call(tmp_path, taken=set())
        assert result.get("error_code") != "tasks_invalid"
        assert creator_cls.called


class TestCreateTasksRefusesATakenId:
    """create_tasks asks the board too, beyond the caller's own project."""

    @pytest.mark.asyncio
    async def test_an_id_held_by_another_project_is_refused(self) -> None:
        from src.marcus_mcp.tools import nlp as nlp_module

        state = MagicMock()
        project = MagicMock()
        project.name = "Camp"
        project.id = "proj-registry-1"
        project.provider_config = {"project_id": "kp-1", "board_id": "kb-1"}
        state.project_registry.get_active_project = AsyncMock(return_value=project)
        state.project_manager.get_kanban_client = AsyncMock(
            return_value=_fake_client({"taken-id"})
        )
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as creator_cls:
            result = await nlp_module.create_tasks(
                task_description="seeded",
                options={"tasks": [{"name": "A", "id": "taken-id"}]},
                state=state,
            )
        assert result["success"] is False
        assert result["error_code"] == "tasks_invalid"
        assert "'taken-id' already identifies a board task" in result["error"]
        assert not creator_cls.called
