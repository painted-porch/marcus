"""
Unit tests: ``create_tasks`` accepts a seeded ``options.tasks`` list.

The runner-side curator (PAPER_DESIGN, "Closing the loop without kernel
code") turns a corroborated opinion into exactly one task with a fixed
acceptance criterion. That is only a transparent rule if the task reaches
the board as written: ``create_tasks`` must honour ``options.tasks`` the way
``create_project`` does (no LLM decomposition of the description) and must
refuse a malformed list before touching the project, with the same
``tasks_invalid`` shape.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

SEED = [
    {
        "name": "Address mobs.north",
        "description": "Two agents reported mobs at the north edge.",
        "labels": ["minecraft", "curated"],
        "completion_criteria": ["No hostile entity within the north zone"],
        "priority": "high",
    }
]


def _state() -> MagicMock:
    state = MagicMock()
    project = MagicMock()
    project.name = "Camp"
    project.provider_config = {
        "project_id": "kp-1",
        "board_id": "kb-1",
        "board_name": "Main",
    }
    state.project_registry.get_active_project = AsyncMock(return_value=project)
    state.project_manager.get_kanban_client = AsyncMock(return_value=MagicMock())
    state.subtask_manager = None
    state.ai_engine = MagicMock()
    return state


class TestCreateTasksSeeded:
    @pytest.mark.asyncio
    async def test_malformed_seed_is_rejected_before_the_creator_runs(self) -> None:
        from src.marcus_mcp.tools import nlp as nlp_module

        state = _state()
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as creator_cls:
            result = await nlp_module.create_tasks(
                task_description="curated",
                options={"tasks": [{"name": "A"}, {"name": "A"}]},
                state=state,
            )
        assert result["success"] is False
        assert result["error_code"] == "tasks_invalid"
        creator_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_valid_seed_is_forwarded_to_the_creator_unchanged(self) -> None:
        from src.marcus_mcp.tools import nlp as nlp_module

        state = _state()
        creator = MagicMock()
        creator.create_project_from_description = AsyncMock(
            return_value={
                "success": True,
                "tasks_created": 1,
                "tasks": [{"name": "Address mobs.north"}],
            }
        )
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator",
            return_value=creator,
        ):
            result = await nlp_module.create_tasks(
                task_description="curated", options={"tasks": SEED}, state=state
            )
        assert result["success"] is True
        assert result["tasks_created"] == 1
        forwarded: Any = creator.create_project_from_description.call_args.kwargs[
            "options"
        ]
        assert forwarded["tasks"] == SEED
