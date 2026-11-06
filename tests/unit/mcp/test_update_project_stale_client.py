"""Unit tests for update_project's stale-cached-client gap.

Issue #896: #886/#888 (Codex review round 3) fixed this exact bug for
add_project's sqlite re-registration branch -- a registry write to
provider_config was persisted but never reached an already-loaded,
cached client, because ProjectContextManager._get_or_create_context
returns a cached context unchanged and switch_project on its own does
not rebuild one. update_project (src/marcus_mcp/tools/
project_management.py) writes provider_config through the exact same
registry.update_project() call, with no equivalent eviction, so it
has the identical gap for its own, more general caller.

These tests build a real ProjectContextManager and a real,
file-backed ProjectRegistry (not mocks), so a passing test proves the
already-loaded client actually sees the new config -- a test that
only re-reads the registry row would pass while the bug stands.
"""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.core.persistence import FilePersistence, Persistence
from src.core.project_context_manager import ProjectContextManager
from src.core.project_registry import ProjectRegistry, register_sqlite_board
from src.marcus_mcp.tools.project_management import add_project, update_project

pytestmark = pytest.mark.unit


def _file_backed_registry(storage_dir: Path) -> ProjectRegistry:
    return ProjectRegistry(
        persistence=Persistence(backend=FilePersistence(storage_dir=storage_dir))
    )


class TestUpdateProjectStaleClient:
    @pytest.mark.asyncio
    async def test_update_project_reaches_an_already_loaded_client(
        self, tmp_path: Path
    ) -> None:
        """Updating a registered project's config must reach a client
        that already has a cached context, the same way #888 made
        add_project's sqlite re-registration branch do.

        Reads the value back through SQLiteKanban's own workspace
        state (client._project_root), the path the rest of the server
        actually uses, not through the registry row.
        """
        db_path = tmp_path / "board.db"

        with patch("src.core.project_context_manager.get_config", return_value=Mock()):
            manager = ProjectContextManager(registry=_file_backed_registry(tmp_path))

        server = Mock()
        server.project_registry = manager.registry
        server.project_manager = manager

        # Register with no project_root, make_active=True so a real
        # client gets built and cached.
        registered = await add_project(
            server,
            {
                "name": "sqlite-project",
                "provider": "sqlite",
                "config": {"project_id": "proj-update-1", "db_path": str(db_path)},
                "make_active": True,
            },
        )
        assert registered["success"] is True
        server.kanban_client = await manager.get_kanban_client()
        assert server.kanban_client._project_root is None

        # The context for proj-update-1 is already cached at this
        # point. update_project now changes provider_config.
        new_root = tmp_path / "existing-board-workspace"
        new_root.mkdir()
        current = await manager.registry.get_project("proj-update-1")
        assert current is not None
        result = await update_project(
            server,
            {
                "project_id": "proj-update-1",
                "config": {**current.provider_config, "project_root": str(new_root)},
            },
        )
        assert result["success"] is True

        # Read back through the path that actually serves it, not
        # the registry row.
        client = await manager.get_kanban_client()
        assert client is not None
        assert client._project_root == str(new_root)

    @pytest.mark.asyncio
    async def test_update_project_on_an_inactive_project_does_not_need_a_rebuild(
        self, tmp_path: Path
    ) -> None:
        """Updating a project that isn't currently active and has no
        cached context at all must still succeed -- invalidate_context
        on an uncached project_id is a no-op, not an error."""
        db_path = tmp_path / "board.db"
        registry = _file_backed_registry(tmp_path)

        project_id = await register_sqlite_board(
            project_id="proj-update-2",
            db_path=str(db_path),
            registry=registry,
        )

        with patch("src.core.project_context_manager.get_config", return_value=Mock()):
            manager = ProjectContextManager(registry=registry)

        server = Mock()
        server.project_registry = registry
        server.project_manager = manager

        result = await update_project(
            server,
            {"project_id": project_id, "name": "renamed-project"},
        )
        assert result["success"] is True
        assert result["project"]["name"] == "renamed-project"
