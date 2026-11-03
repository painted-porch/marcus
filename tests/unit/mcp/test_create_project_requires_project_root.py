"""
Unit tests for create_project validating ``options.project_root`` (issue #588).

``project_root`` is documented as REQUIRED and nothing enforced it. A
project created without it carried ``project_root: null`` in the
registry, its design artifacts went to a default under ``~/.marcus``,
and the first thing that noticed was the smoke gate on the last task,
which the agent cannot fix. For the sqlite provider (the default) the
option is now validated up front, before any state is touched: present,
absolute, and a directory Marcus can create. Other providers keep their
legacy behaviour with a warning.
"""

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _make_state() -> MagicMock:
    state = MagicMock()
    state.kanban_client = MagicMock()
    state.log_event = MagicMock()
    state.events = None
    return state


def _reached_pipeline(state: MagicMock) -> bool:
    """True when create_project got past validation to the pipeline start."""
    return any(
        call.args and call.args[0] == "create_project_calling"
        for call in state.log_event.call_args_list
    )


async def _call(options: Any, name: str) -> tuple[dict, MagicMock]:
    from src.marcus_mcp.tools import nlp as nlp_module

    state = _make_state()
    dedup_key = f"{name}:Build a small calculator with a CLI"
    nlp_module._recent_create_project_calls.pop(dedup_key, None)
    try:
        with patch(
            "src.integrations.kanban_factory.KanbanFactory.create",
            side_effect=RuntimeError("stop before any provider work"),
        ):
            result = await nlp_module.create_project(
                description="Build a small calculator with a CLI",
                project_name=name,
                options=options,
                state=state,
            )
    finally:
        nlp_module._recent_create_project_calls.pop(dedup_key, None)
    return result, state


class TestSqliteRequiresProjectRoot:
    """The default provider refuses to create a project it cannot verify."""

    @pytest.mark.asyncio
    async def test_missing_project_root_is_rejected_before_any_work(self) -> None:
        """No project_root: a configuration error, nothing started."""
        result, state = await _call({"provider": "sqlite"}, "NoRoot")

        assert result["success"] is False
        assert "project_root" in result["error"]
        assert result["error_code"] == "project_root_required"
        assert not _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_options_none_is_rejected_when_the_default_provider_is_sqlite(
        self,
    ) -> None:
        """options omitted entirely counts as missing when sqlite is the default."""
        cfg = MagicMock()
        cfg.kanban.provider = "sqlite"
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            result, state = await _call(None, "NoOptions")

        assert result["success"] is False
        assert result["error_code"] == "project_root_required"
        assert not _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_relative_path_is_rejected(self) -> None:
        """The gate runs commands in this directory; it must be absolute."""
        result, state = await _call(
            {"provider": "sqlite", "project_root": "relative/impl"}, "RelRoot"
        )

        assert result["success"] is False
        assert result["error_code"] == "project_root_required"
        assert "absolute" in result["error"]
        assert not _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_path_that_is_a_file_is_rejected(self, tmp_path: Path) -> None:
        """A file where a directory is needed is a configuration error."""
        not_a_dir = tmp_path / "file.txt"
        not_a_dir.write_text("x")

        result, state = await _call(
            {"provider": "sqlite", "project_root": str(not_a_dir)}, "FileRoot"
        )

        assert result["success"] is False
        assert result["error_code"] == "project_root_required"
        assert not _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_dedup_entry_is_cleared_on_rejection(self) -> None:
        """A rejected call must not poison the dedup window for the retry."""
        from src.marcus_mcp.tools import nlp as nlp_module

        await _call({"provider": "sqlite"}, "DedupClear")

        assert "DedupClear:Build a small calculator with a CLI" not in (
            nlp_module._recent_create_project_calls
        )

    @pytest.mark.asyncio
    async def test_absolute_missing_directory_is_created_and_accepted(
        self, tmp_path: Path
    ) -> None:
        """An absolute path that does not exist yet is created, as the runner expects."""
        root = tmp_path / "experiment" / "implementation"

        result, state = await _call(
            {"provider": "sqlite", "project_root": str(root)}, "NewDir"
        )

        assert root.is_dir()
        assert result.get("error_code") != "project_root_required"
        assert _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_existing_directory_is_accepted(self, tmp_path: Path) -> None:
        """The normal runner case passes straight through."""
        result, state = await _call(
            {"provider": "sqlite", "project_root": str(tmp_path)}, "ExistingDir"
        )

        assert result.get("error_code") != "project_root_required"
        assert _reached_pipeline(state)


class TestOtherProvidersKeepLegacyBehaviour:
    """Planka, GitHub and Linear projects are not blocked, only warned."""

    @pytest.mark.asyncio
    async def test_planka_without_project_root_proceeds_with_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Legacy providers reach the pipeline; the log says what is missing."""
        with caplog.at_level(logging.WARNING, logger="src.marcus_mcp.tools.nlp"):
            result, state = await _call({"provider": "planka"}, "PlankaNoRoot")

        assert result.get("error_code") != "project_root_required"
        assert _reached_pipeline(state)
        assert any("project_root" in r.getMessage() for r in caplog.records)
