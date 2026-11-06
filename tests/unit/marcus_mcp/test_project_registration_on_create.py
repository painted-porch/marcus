"""Test that create_project properly registers projects with ProjectRegistry."""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure CLAUDE_API_KEY is set so config validation passes."""
    monkeypatch.setenv("CLAUDE_API_KEY", "test-key-for-unit-tests")


@pytest.fixture(autouse=True)
def _patch_kanban_factory():
    """Issue #856: create_project always builds a fresh client via
    KanbanFactory.create now, even when the requested provider matches
    the one already on state.kanban_client (it must never reuse/mutate
    an existing instance -- that was the bug this fixed). Without this
    patch the REAL factory runs and tries to construct/connect a real
    Planka provider, which only happens to work here because this is a
    dev machine with a local Planka reachable -- it fails in CI, and
    even locally it clobbers the Mock's project_id/board_id the test
    depends on. The fake factory returns a fresh Mock carrying the same
    ids the test already expects.
    """
    from unittest.mock import AsyncMock, Mock, patch

    from src.integrations.kanban_interface import KanbanProvider

    def _fake_create(provider, config=None):
        client = Mock()
        client.provider = KanbanProvider.PLANKA
        client.project_id = "1670692878487127607"
        client.board_id = "1670692878621345337"
        client.connect = AsyncMock()
        return client

    with patch(
        "src.integrations.kanban_factory.KanbanFactory.create",
        side_effect=_fake_create,
    ):
        yield


@pytest.fixture(autouse=True)
def _clear_create_project_dedup_cache():
    """Clear the module-level dedup cache between tests.

    ``src.marcus_mcp.tools.nlp.create_project`` rejects duplicate
    calls within a 10-minute window via the
    ``_recent_create_project_calls`` dict. In a test suite that calls
    ``create_project`` multiple times with similar inputs, the second
    call is silently rejected as a duplicate and the test sees
    ``result["success"] is False``, which looks like a product bug
    but is actually state leaking between tests. Clearing the cache
    before and after each test makes each test hermetic.
    """
    from src.marcus_mcp.tools import nlp as nlp_module

    nlp_module._recent_create_project_calls.clear()
    yield
    nlp_module._recent_create_project_calls.clear()


@pytest.mark.asyncio
async def test_create_project_registers_with_project_registry() -> None:
    """Test that create_project registers the new project in ProjectRegistry."""
    from src.integrations.kanban_interface import KanbanProvider
    from src.marcus_mcp.tools.nlp import create_project

    # Mock state with all required attributes
    state = Mock()
    state.log_event = Mock()
    state.kanban_client = Mock()
    state.kanban_client.provider = KanbanProvider.PLANKA
    state.kanban_client.project_id = "1670692878487127607"
    state.kanban_client.board_id = "1670692878621345337"
    state.ai_engine = Mock()
    state.subtask_manager = Mock()
    state.project_registry = Mock()
    state.project_manager = Mock()

    # Mock the add_project to return a Marcus project ID
    marcus_project_id = "abc-123-def-456"
    state.project_registry.add_project = AsyncMock(return_value=marcus_project_id)
    state.project_registry.get_active_project = AsyncMock(return_value=None)
    state.project_manager.switch_project = AsyncMock()
    state.project_manager.get_kanban_client = AsyncMock(
        return_value=state.kanban_client
    )
    state._subtasks_migrated = False

    # Mock the NaturalLanguageProjectCreator
    mock_creator_result: Dict[str, Any] = {
        "success": True,
        "project_name": "Flight Simulator",
        "tasks_created": 29,
        "task_ids": ["task-1", "task-2"],
        "board": {
            "project_name": "Flight Simulator",
            "board_name": "Main Board",
        },
    }

    with patch(
        "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
    ) as MockCreator:
        mock_instance = MockCreator.return_value
        mock_instance.create_project_from_description = AsyncMock(
            return_value=mock_creator_result
        )

        # Call create_project — the fake KanbanFactory.create (patched
        # above) returns a client with the same project_id/board_id.
        result = await create_project(
            description="Build a flight simulator game",
            project_name="Flight Simulator",
            options={"provider": "planka"},
            state=state,
        )

        # Verify project was registered
        assert state.project_registry.add_project.called
        call_args = state.project_registry.add_project.call_args[0][0]

        # Verify ProjectConfig was created correctly
        assert call_args.name == "Flight Simulator - Main Board"
        assert call_args.provider == "planka"
        assert call_args.provider_config["project_id"] == "1670692878487127607"
        assert call_args.provider_config["board_id"] == "1670692878621345337"
        assert call_args.provider_config["project_name"] == "Flight Simulator"
        assert call_args.provider_config["board_name"] == "Main Board"
        assert "auto-created" in call_args.tags
        assert "planka" in call_args.tags

        # Verify project was switched to (may be called more than once)
        state.project_manager.switch_project.assert_called_with(marcus_project_id)

        # Verify Marcus project_id was added to result
        assert result["project_id"] == marcus_project_id
        assert result["success"] is True


@pytest.mark.asyncio
async def test_create_project_handles_missing_registry_gracefully(
    tmp_path: Path,
) -> None:
    """Test that create_project works even if project_registry is not available."""
    from src.marcus_mcp.tools.nlp import create_project

    # Mock state without project_registry
    state = Mock(spec=["log_event", "kanban_client", "ai_engine", "subtask_manager"])
    state.log_event = Mock()
    state.kanban_client = Mock()
    state.kanban_client.project_id = "1670692878487127607"
    state.kanban_client.board_id = "1670692878621345337"
    state.ai_engine = Mock()
    state.subtask_manager = Mock()
    # No project_registry attribute (spec prevents it from existing)

    mock_creator_result: Dict[str, Any] = {
        "success": True,
        "project_name": "Flight Simulator",
        "tasks_created": 29,
    }

    with patch(
        "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
    ) as MockCreator:
        mock_instance = MockCreator.return_value
        mock_instance.create_project_from_description = AsyncMock(
            return_value=mock_creator_result
        )

        # Call create_project - should not crash
        result = await create_project(
            description="Build a flight simulator game",
            project_name="Flight Simulator",
            # #588: the sqlite default now requires a project_root up front;
            # these tests are about registry failures, not that rule.
            options={"project_root": str(tmp_path)},
            state=state,
        )

        # Should still return success
        assert result["success"] is True
        # But no project_id added since registry unavailable
        assert "project_id" not in result


@pytest.mark.asyncio
async def test_create_project_handles_registration_errors(tmp_path: Path) -> None:
    """Test that create_project continues even if registration fails."""
    from src.marcus_mcp.tools.nlp import create_project

    # Mock state with failing project_registry
    state = Mock()
    state.log_event = Mock()
    state.kanban_client = Mock()
    state.kanban_client.project_id = "1670692878487127607"
    state.kanban_client.board_id = "1670692878621345337"
    state.ai_engine = Mock()
    state.subtask_manager = Mock()
    state.project_registry = Mock()
    state.project_manager = Mock()

    # Make add_project raise an exception
    state.project_registry.add_project = AsyncMock(
        side_effect=Exception("Database error")
    )
    state.project_registry.get_active_project = AsyncMock(return_value=None)

    mock_creator_result: Dict[str, Any] = {
        "success": True,
        "project_name": "Flight Simulator",
        "tasks_created": 29,
    }

    with patch(
        "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
    ) as MockCreator:
        mock_instance = MockCreator.return_value
        mock_instance.create_project_from_description = AsyncMock(
            return_value=mock_creator_result
        )

        # Call create_project - should not crash despite registration error
        result = await create_project(
            description="Build a flight simulator game",
            project_name="Flight Simulator",
            # #588: the sqlite default now requires a project_root up front;
            # these tests are about registry failures, not that rule.
            options={"project_root": str(tmp_path)},
            state=state,
        )

        # Should still return success (registration failure doesn't fail the operation)
        assert result["success"] is True
