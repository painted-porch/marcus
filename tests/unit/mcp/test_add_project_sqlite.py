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
from unittest.mock import Mock

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
        of the one that wrote it."""
        db_path = tmp_path / "board.db"
        db_path.touch()

        server = Mock()
        server.project_registry = _file_backed_registry(tmp_path)

        result = await add_project(
            server,
            {
                "name": "sqlite-project",
                "provider": "sqlite",
                "config": {"project_id": "proj-sqlite-1", "db_path": str(db_path)},
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
