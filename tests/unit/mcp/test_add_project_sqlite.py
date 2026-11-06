"""Unit tests for the add_project MCP tool's sqlite provider support.

Issue #886: add_project hard-refused provider="sqlite", so there was
no tool-level way to register a SQLite project -- only
create_project's own internal auto-create flow did it. This exercises
the real add_project tool against a real, file-backed ProjectRegistry
(not a mock) so a passing test actually proves the entry lands in
projects.json and is visible to an independent reader, which is the
user-facing point of the fix -- PR #885 made concurrent writes to
that file safe, but add_project still couldn't write one at all for
sqlite.
"""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.persistence import FilePersistence, Persistence
from src.core.project_registry import ProjectRegistry
from src.marcus_mcp.tools.project_management import ADD_PROJECT_PROVIDERS, add_project

pytestmark = pytest.mark.unit


def test_advertised_schema_accepts_every_provider_the_implementation_does() -> None:
    """The legacy stdio transport validates a tool call's arguments
    against its advertised JSON schema before handle_tool_call ever
    runs (confirmed by Codex review on this PR: "sqlite" reached
    add_project's own validation fine when called directly, but the
    real stdio tool call never got past the server's own schema
    check, because handlers.py's advertised enum had its own,
    separate ["planka", "linear", "github"] literal that this PR's
    first round forgot to update alongside add_project's).

    Same class of drift as #871's project_root and #880's kind
    argument reaching an older server: an accepted-value list and an
    advertised schema, copy-pasted into two places, silently drift.
    Closed here by making handlers.py import ADD_PROJECT_PROVIDERS
    rather than hold its own literal -- this test asserts that import
    actually happened, so a future hand-edit back to a hardcoded list
    fails here instead of shipping an unreachable branch again.
    """
    from src.marcus_mcp.handlers import get_tool_definitions

    add_project_tool = next(
        tool for tool in get_tool_definitions("human") if tool.name == "add_project"
    )
    advertised = add_project_tool.inputSchema["properties"]["provider"]["enum"]

    assert set(advertised) == set(ADD_PROJECT_PROVIDERS)


def _file_backed_registry(storage_dir: Path) -> ProjectRegistry:
    return ProjectRegistry(
        persistence=Persistence(backend=FilePersistence(storage_dir=storage_dir))
    )


class TestAddProjectSqlite:
    @pytest.mark.asyncio
    async def test_registering_a_sqlite_project_lands_in_projects_json(
        self, tmp_path: Path
    ) -> None:
        """Registering through the tool must be readable by a fresh,
        independent registry instance -- not just the in-memory cache
        of the one that wrote it.

        Also covers project_root: register_sqlite_board itself has no
        concept of it (its own caller, the Tow experiment runner, never
        has one to give it), so a caller registering an EXISTING board
        through this tool and supplying project_root must see it carried
        through -- dropping it would register a project that comes back
        less capable than one created through create_project's own
        auto-register path (project_root is what log_artifact's
        filesystem mode and the software smoke gate both need). An
        independent reader is exactly what would catch a silently
        dropped field.
        """
        db_path = tmp_path / "board.db"
        db_path.touch()
        project_root = tmp_path / "existing-board-workspace"
        project_root.mkdir()

        server = Mock()
        server.project_registry = _file_backed_registry(tmp_path)
        server.project_manager = AsyncMock()

        result = await add_project(
            server,
            {
                "name": "sqlite-project",
                "provider": "sqlite",
                "config": {
                    "project_id": "proj-sqlite-1",
                    "db_path": str(db_path),
                    "project_root": str(project_root),
                },
                "make_active": False,
            },
        )

        assert result["success"] is True
        assert result["project"]["id"] == "proj-sqlite-1"
        assert result["project"]["provider"] == "sqlite"

        # A completely separate registry instance, reading the same
        # directory fresh, must see it -- proves this hit the file,
        # not just this call's own in-memory cache.
        reader = _file_backed_registry(tmp_path)
        stored = await reader.get_project("proj-sqlite-1")
        assert stored is not None
        assert stored.provider == "sqlite"
        assert stored.provider_config["db_path"] == str(db_path.resolve())
        assert stored.provider_config["project_root"] == str(project_root)

    @pytest.mark.asyncio
    async def test_missing_project_id_or_db_path_is_rejected_with_a_clear_error(
        self, tmp_path: Path
    ) -> None:
        server = Mock()
        server.project_registry = _file_backed_registry(tmp_path)

        result = await add_project(
            server,
            {
                "name": "sqlite-project",
                "provider": "sqlite",
                "config": {"project_id": "proj-sqlite-2"},  # no db_path
                "make_active": False,
            },
        )

        assert result["success"] is False
        assert "db_path" in result["error"]

    @pytest.mark.asyncio
    async def test_reregistering_the_same_project_id_against_a_different_db_is_refused(
        self, tmp_path: Path
    ) -> None:
        """register_sqlite_board's own conflict guard must surface as
        a clean tool error, not an unhandled exception."""
        db_path_a = tmp_path / "board-a.db"
        db_path_a.touch()
        db_path_b = tmp_path / "board-b.db"
        db_path_b.touch()

        server = Mock()
        server.project_registry = _file_backed_registry(tmp_path)

        first = await add_project(
            server,
            {
                "name": "sqlite-project",
                "provider": "sqlite",
                "config": {"project_id": "proj-sqlite-3", "db_path": str(db_path_a)},
                "make_active": False,
            },
        )
        assert first["success"] is True

        second = await add_project(
            server,
            {
                "name": "sqlite-project",
                "provider": "sqlite",
                "config": {"project_id": "proj-sqlite-3", "db_path": str(db_path_b)},
                "make_active": False,
            },
        )
        assert second["success"] is False

    @pytest.mark.asyncio
    async def test_re_registering_with_a_new_project_root_reaches_an_already_loaded_client(
        self, tmp_path: Path
    ) -> None:
        """Re-registering with a new project_root must reach a client
        that already has a cached context (issue #886, Codex review
        round 3).

        ProjectContextManager._get_or_create_context returns an
        already-cached context unchanged, so a registry update to
        provider_config was persisted but never reached an
        already-loaded client -- switch_project on its own does not
        rebuild one. A test that only re-reads the registry row would
        pass while this bug stands (that is exactly what the
        round-2 test did); this one reads the value back through
        SQLiteKanban's own workspace state, the path that actually
        serves it to the smoke gate, after a real ProjectContextManager
        has already built and cached a client for this project once.
        """
        from unittest.mock import patch

        from src.core.project_context_manager import ProjectContextManager

        db_path = tmp_path / "board.db"

        with patch("src.core.project_context_manager.get_config", return_value=Mock()):
            manager = ProjectContextManager(registry=_file_backed_registry(tmp_path))

        server = Mock()
        server.project_registry = manager.registry
        server.project_manager = manager

        # First registration: no project_root yet, make_active=True so
        # a real context gets built and cached.
        first = await add_project(
            server,
            {
                "name": "sqlite-project",
                "provider": "sqlite",
                "config": {"project_id": "proj-sqlite-4", "db_path": str(db_path)},
                "make_active": True,
            },
        )
        assert first["success"] is True
        server.kanban_client = await manager.get_kanban_client()
        assert server.kanban_client._project_root is None

        # Re-registration: same board, now with a project_root. The
        # context for proj-sqlite-4 is already cached at this point.
        new_root = tmp_path / "existing-board-workspace"
        new_root.mkdir()
        second = await add_project(
            server,
            {
                "name": "sqlite-project",
                "provider": "sqlite",
                "config": {
                    "project_id": "proj-sqlite-4",
                    "db_path": str(db_path),
                    "project_root": str(new_root),
                },
                "make_active": True,
            },
        )
        assert second["success"] is True

        # Read back through the path that actually serves it to the
        # smoke gate, not the registry row.
        client = await manager.get_kanban_client()
        assert client is not None
        assert client._project_root == str(new_root)
