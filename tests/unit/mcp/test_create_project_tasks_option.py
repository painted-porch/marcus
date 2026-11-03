"""
Unit tests: ``create_project`` refuses a malformed ``options.tasks`` up front.

A seeded board hands ``create_project`` the exact task list. A bad list must
be rejected before any state is touched, with every problem named, the same
way a missing ``project_root`` is (issue #588).
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _state() -> MagicMock:
    state = MagicMock()
    state.kanban_client = MagicMock()
    state.log_event = MagicMock()
    state.events = None
    return state


def _reached_pipeline(state: MagicMock) -> bool:
    return any(
        call.args and call.args[0] == "create_project_calling"
        for call in state.log_event.call_args_list
    )


async def _call(options: Any, name: str, tmp_path: Path) -> tuple[dict, MagicMock]:
    from src.marcus_mcp.tools import nlp as nlp_module

    state = _state()
    options = {"provider": "sqlite", "project_root": str(tmp_path / "impl"), **options}
    dedup_key = f"{name}:Build a tiny thing"
    nlp_module._recent_create_project_calls.pop(dedup_key, None)
    fake_config = MagicMock()
    fake_config.kanban.provider = "sqlite"
    try:
        with (
            patch("src.config.marcus_config.get_config", return_value=fake_config),
            patch(
                "src.integrations.kanban_factory.KanbanFactory.create",
                side_effect=RuntimeError("stop before any provider work"),
            ),
        ):
            result = await nlp_module.create_project(
                description="Build a tiny thing",
                project_name=name,
                options=options,
                state=state,
            )
    finally:
        nlp_module._recent_create_project_calls.pop(dedup_key, None)
    return result, state


class TestTasksOptionValidation:
    """Malformed seeds stop at the door; valid ones reach the pipeline."""

    @pytest.mark.asyncio
    async def test_malformed_tasks_are_rejected_before_any_work(
        self, tmp_path: Path
    ) -> None:
        """Every problem is named and the pipeline never starts."""
        result, state = await _call(
            {"tasks": [{"name": "A"}, {"name": "A", "depends_on": ["Q"]}]},
            "seed-bad",
            tmp_path,
        )
        assert result["success"] is False
        assert result["error_code"] == "tasks_invalid"
        assert "repeats the name 'A'" in result["error"]
        assert "unknown task 'Q'" in result["error"]
        assert not _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_valid_tasks_reach_the_pipeline(self, tmp_path: Path) -> None:
        """A well-formed seed passes validation (the pipeline is stubbed to stop)."""
        result, state = await _call(
            {
                "tasks": [
                    {"name": "Implement add_one_plus_one", "labels": ["implement"]}
                ]
            },
            "seed-ok",
            tmp_path,
        )
        assert result.get("error_code") != "tasks_invalid"
        assert _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_without_tasks_option_nothing_changes(self, tmp_path: Path) -> None:
        """No tasks option: the validator is a no-op."""
        result, state = await _call({}, "no-seed", tmp_path)
        assert result.get("error_code") != "tasks_invalid"
        assert _reached_pipeline(state)
