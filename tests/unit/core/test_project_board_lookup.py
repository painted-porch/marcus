"""
Unit tests for ``ProjectContextManager.get_project_board``.

A write that belongs to a project other than the active one must reach
that project's board.  The lookup reuses or creates the project's
context exactly as ``switch_project`` does, but never changes which
project is active.
"""

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.event_loop_utils import EventLoopLockManager
from src.core.project_context_manager import ProjectContext, ProjectContextManager

pytestmark = pytest.mark.unit


def _project(project_id: str, name: str) -> Mock:
    """Build a registry entry; ``name`` is set after construction because
    ``Mock(name=...)`` names the mock itself rather than an attribute."""
    project = Mock(id=project_id, provider="sqlite")
    project.name = name
    return project


def _manager(project: Any) -> ProjectContextManager:
    """Build a manager without touching config, with one known project."""
    manager: Any = ProjectContextManager.__new__(ProjectContextManager)
    manager.registry = Mock()
    manager.registry.get_project = AsyncMock(return_value=project)
    manager.contexts = {}
    manager.active_project_id = "p-active"
    manager.active_project_name = "Active"
    manager._lock_manager = EventLoopLockManager()
    return manager


class TestGetProjectBoard:
    """Test suite for resolving another project's board."""

    @pytest.mark.asyncio
    async def test_returns_the_projects_client_and_name_without_switching(
        self,
    ) -> None:
        """The client and name come back; the active project is untouched."""
        project = _project("p-b", "Project B")
        manager = _manager(project)
        context = ProjectContext("p-b")
        context.kanban_client = Mock()
        manager._get_or_create_context = AsyncMock(return_value=context)

        board = await manager.get_project_board("p-b")

        assert board == (context.kanban_client, "Project B")
        manager._get_or_create_context.assert_awaited_once_with(project)
        assert manager.active_project_id == "p-active"

    @pytest.mark.asyncio
    async def test_unknown_project_yields_no_board(self) -> None:
        """A project the registry does not know has no board."""
        manager = _manager(None)
        manager._get_or_create_context = AsyncMock()

        assert await manager.get_project_board("nope") is None
        manager._get_or_create_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_without_a_client_yields_no_board(self) -> None:
        """A context whose client failed to connect is reported as no board."""
        project = _project("p-b", "Project B")
        manager = _manager(project)
        manager._get_or_create_context = AsyncMock(return_value=ProjectContext("p-b"))

        assert await manager.get_project_board("p-b") is None
