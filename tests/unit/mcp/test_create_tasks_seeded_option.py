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
    project.id = "proj-registry-1"
    project.provider_config = {
        "project_id": "kp-1",
        "board_id": "kb-1",
        "board_name": "Main",
    }
    state.project_registry.get_active_project = AsyncMock(return_value=project)
    kanban_client = MagicMock()
    kanban_client.get_all_tasks = AsyncMock(return_value=[])
    state.project_manager.get_kanban_client = AsyncMock(return_value=kanban_client)
    state.subtask_manager = None
    state.ai_engine = MagicMock()
    state.refresh_project_state = AsyncMock()
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
    async def test_a_valid_seed_goes_straight_onto_the_current_board(self) -> None:
        """No decomposition, no new project scope, no About card: the seed is
        built into Task objects and created on the active project's board,
        stamped with that project's id (the live check on 2026-11-05 showed
        create_project_from_description minting a fresh sqlite project scope
        plus an About card, so agents on the real project never saw the
        curated task)."""
        from src.marcus_mcp.tools import nlp as nlp_module

        state = _state()
        created_task = MagicMock()
        created_task.id = "k-77"
        created_task.name = "Address mobs.north"
        creator = MagicMock()
        creator.create_tasks_on_board = AsyncMock(return_value=[created_task])
        creator.create_project_from_description = AsyncMock(
            side_effect=AssertionError("a seeded list must not be decomposed")
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
        assert result["tasks"] == [{"id": "k-77", "name": "Address mobs.north"}]
        tasks = creator.create_tasks_on_board.call_args.args[0]
        assert [t.name for t in tasks] == ["Address mobs.north"]
        assert all(t.source_type == "seeded" for t in tasks)
        # task_metadata is stamped with the project the agents are registered to.
        assert creator.active_project_id == "proj-registry-1"
        state.refresh_project_state.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_depends_on_resolves_against_an_existing_board_task(self) -> None:
        """Issue #841: a runtime task may depend on a task already on

        the board, scoped to the caller's own project -- confirmed
        with the Minecraft planner session, the first real consumer.
        """
        from src.marcus_mcp.tools import nlp as nlp_module

        state = _state()
        existing = MagicMock()
        existing.id = "board-task-1"
        existing.name = "Raise the north wall"
        existing.project_id = "proj-registry-1"
        other_project_task = MagicMock()
        other_project_task.id = "board-task-2"
        other_project_task.name = "Raise the north wall"
        other_project_task.project_id = "some-other-project"
        kanban_client = await state.project_manager.get_kanban_client()
        kanban_client.get_all_tasks = AsyncMock(
            return_value=[existing, other_project_task]
        )

        created_task = MagicMock()
        created_task.id = "k-78"
        created_task.name = "Torches on the north wall"
        creator = MagicMock()
        creator.create_tasks_on_board = AsyncMock(return_value=[created_task])
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator",
            return_value=creator,
        ):
            result = await nlp_module.create_tasks(
                task_description="curated",
                options={
                    "tasks": [
                        {
                            "name": "Torches on the north wall",
                            "depends_on": ["Raise the north wall"],
                        }
                    ]
                },
                state=state,
            )

        assert result["success"] is True
        tasks = creator.create_tasks_on_board.call_args.args[0]
        (torches,) = tasks
        # Resolved to the SAME project's task, never the other
        # project's same-named one.
        assert torches.dependencies == ["board-task-1"]

    @pytest.mark.asyncio
    async def test_depends_on_resolves_for_a_planka_shaped_project(self) -> None:
        """Issue #855, Codex P2: the board-task filter must match a

        Planka-shaped task (``project_id`` is the PROVIDER's own id,
        not the registry id) via ``task_belongs_to_project``, the
        same identity gap PR #853 bridged elsewhere."""
        from src.marcus_mcp.tools import nlp as nlp_module

        state = _state()
        project = MagicMock()
        project.name = "Camp"
        project.id = "uuid-registry-1"
        project.provider_config = {
            "project_id": "planka-proj-77",
            "board_id": "kb-1",
            "board_name": "Main",
        }
        state.project_registry.get_active_project = AsyncMock(return_value=project)

        existing = MagicMock()
        existing.id = "board-task-1"
        existing.name = "Raise the north wall"
        existing.project_id = "planka-proj-77"  # the PROVIDER's own id
        kanban_client = await state.project_manager.get_kanban_client()
        kanban_client.get_all_tasks = AsyncMock(return_value=[existing])

        created_task = MagicMock()
        created_task.id = "k-78"
        created_task.name = "Torches on the north wall"
        creator = MagicMock()
        creator.create_tasks_on_board = AsyncMock(return_value=[created_task])
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator",
            return_value=creator,
        ):
            result = await nlp_module.create_tasks(
                task_description="curated",
                options={
                    "tasks": [
                        {
                            "name": "Torches on the north wall",
                            "depends_on": ["Raise the north wall"],
                        }
                    ]
                },
                state=state,
            )

        assert result["success"] is True
        tasks = creator.create_tasks_on_board.call_args.args[0]
        (torches,) = tasks
        assert torches.dependencies == ["board-task-1"]

    @pytest.mark.asyncio
    async def test_a_dropped_dependency_is_named_in_the_reply(self) -> None:
        """Codex P2, round 3 on PR #855: when create_tasks_on_board

        drops an edge (an in-batch dependency whose target failed
        creation), the reply must name it -- a caller reading only
        ``tasks_created``/``tasks`` has no way to see that its graph
        is not what it asked for."""
        from src.marcus_mcp.tools import nlp as nlp_module

        state = _state()
        created_task = MagicMock()
        created_task.id = "k-77"
        created_task.name = "Torches"
        creator = MagicMock()
        creator.create_tasks_on_board = AsyncMock(return_value=[created_task])
        creator.dependencies_dropped = [
            {"task": "Torches", "missing_dependency": "Wall"}
        ]
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator",
            return_value=creator,
        ):
            result = await nlp_module.create_tasks(
                task_description="curated",
                options={
                    "tasks": [
                        {"name": "Wall"},
                        {"name": "Torches", "depends_on": ["Wall"]},
                    ]
                },
                state=state,
            )

        assert result["success"] is True
        assert result["dependencies_dropped"] == [
            {"task": "Torches", "missing_dependency": "Wall"}
        ]

    @pytest.mark.asyncio
    async def test_no_dropped_dependencies_key_when_nothing_was_dropped(self) -> None:
        """The field must not appear at all on the ordinary success

        path -- a caller checking ``"dependencies_dropped" in result``
        must see False when nothing was dropped, not an empty list
        with its own meaning to parse."""
        from src.marcus_mcp.tools import nlp as nlp_module

        state = _state()
        created_task = MagicMock()
        created_task.id = "k-77"
        created_task.name = "Address mobs.north"
        creator = MagicMock()
        creator.create_tasks_on_board = AsyncMock(return_value=[created_task])
        creator.dependencies_dropped = []
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator",
            return_value=creator,
        ):
            result = await nlp_module.create_tasks(
                task_description="curated", options={"tasks": SEED}, state=state
            )

        assert result["success"] is True
        assert "dependencies_dropped" not in result
