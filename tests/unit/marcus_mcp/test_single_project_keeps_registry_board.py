"""
Unit tests for issue #766: single-project mode must serve the registry's board.

``MarcusServer.initialize()`` switches the project manager to the registry's
active project (which builds a correctly scoped kanban client inside the
manager) and then, in the shipped default ``single_project_mode``, built a
fresh unscoped client with ``initialize_kanban()`` and served that instead.
Agents got every project's tasks, or none. These tests pin that the scoped
client wins, that the fallback survives for an empty registry, and that a
mismatch can never again be silent.
"""

import logging
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.marcus_mcp.server import MarcusServer

pytestmark = pytest.mark.unit


def _fake_server(*, scoped_client: object, active_project: object) -> Mock:
    """A server stand-in with just what ``initialize()`` touches."""
    fake = Mock()
    fake.ai_engine = Mock()
    fake.ai_engine.initialize = AsyncMock()
    fake.project_manager = Mock()
    fake.project_manager.initialize = AsyncMock()
    fake.project_manager.registry.get_active_project = AsyncMock(
        return_value=active_project
    )
    fake.project_manager.switch_project = AsyncMock(return_value=True)
    fake.project_manager.get_kanban_client = AsyncMock(return_value=scoped_client)
    fake.project_registry.get_active_project = AsyncMock(return_value=active_project)
    fake.provider = "sqlite"
    fake.kanban_client = None

    bare_client = Mock(name="bare-client")
    bare_client.project_id = None
    bare_client.db_path = "data/kanban.db"

    async def _initialize_kanban() -> None:
        if fake.kanban_client is None:  # the real method's guard
            fake.kanban_client = bare_client

    fake.initialize_kanban = AsyncMock(side_effect=_initialize_kanban)
    fake._bare_client = bare_client
    fake._migrate_to_multi_project = AsyncMock()
    fake._initialize_monitoring_systems = AsyncMock()
    fake.event_visualizer = None
    # The real mismatch check, so the warning tests exercise it.
    fake._warn_if_board_mismatch = MarcusServer._warn_if_board_mismatch.__get__(
        fake, MarcusServer
    )
    return fake


def _config(single_project_mode: bool = True) -> Mock:
    cfg = Mock()
    cfg.single_project_mode = single_project_mode
    cfg.default_project_name = None
    return cfg


def _project(
    project_id: str = "proj-1",
    name: str = "simple-calculator",
    provider_project_id: Optional[str] = None,
) -> Mock:
    """A registry row; ``provider_project_id`` is what the provider scopes by."""
    project = Mock()
    project.id = project_id
    project.name = name
    project.provider_config = (
        {"project_id": provider_project_id} if provider_project_id else {}
    )
    return project


def _scoped(project_id: str = "proj-1") -> Mock:
    client = Mock(name="scoped-client")
    client.project_id = project_id
    client.db_path = "/srv/spike/kanban.db"
    return client


async def _run_initialize(fake: Mock, single: bool = True) -> None:
    with (
        patch("src.marcus_mcp.server.get_config", return_value=_config(single)),
        patch(
            "src.marcus_mcp.server.ai_usage_middleware.wrap_ai_provider",
            side_effect=lambda x: x,
        ),
    ):
        await MarcusServer.initialize(fake)  # type: ignore[arg-type]


class TestSingleProjectModeServesTheRegistryBoard:
    """The registry's active project is what agents see, in every mode."""

    @pytest.mark.asyncio
    async def test_scoped_client_is_kept(self) -> None:
        """With an active registry project, the server serves that client."""
        scoped = _scoped()
        fake = _fake_server(scoped_client=scoped, active_project=_project())

        await _run_initialize(fake)

        assert fake.kanban_client is scoped
        fake._initialize_monitoring_systems.assert_awaited()

    @pytest.mark.asyncio
    async def test_bare_client_is_never_built_over_a_scoped_one(self) -> None:
        """initialize_kanban may run, but it must not replace the scoped client."""
        scoped = _scoped()
        fake = _fake_server(scoped_client=scoped, active_project=_project())

        await _run_initialize(fake)

        assert fake.kanban_client is not fake._bare_client

    @pytest.mark.asyncio
    async def test_empty_registry_still_builds_the_default_client(self) -> None:
        """No active project: the legacy fallback keeps working."""
        fake = _fake_server(scoped_client=None, active_project=None)

        await _run_initialize(fake)

        assert fake.kanban_client is fake._bare_client
        fake.initialize_kanban.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mismatch_is_logged_loudly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If the served client's scope differs from the registry, say so at WARNING."""
        fake = _fake_server(scoped_client=None, active_project=_project("proj-1"))
        # The manager switched but produced no client (connect failed, say),
        # so the fallback builds the bare one: scope None vs registry proj-1.

        with caplog.at_level(logging.WARNING, logger="src.marcus_mcp.server"):
            await _run_initialize(fake)

        assert fake.kanban_client is fake._bare_client
        messages = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any("proj-1" in m and "None" in m for m in messages), messages

    @pytest.mark.asyncio
    async def test_matching_scope_logs_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A correctly scoped server is quiet."""
        fake = _fake_server(
            scoped_client=_scoped("proj-1"), active_project=_project("proj-1")
        )

        with caplog.at_level(logging.WARNING, logger="src.marcus_mcp.server"):
            await _run_initialize(fake)

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    async def test_scope_is_compared_with_the_provider_project_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A registry row added through add_project has its own UUID.

        The provider's project id lives in provider_config; a client scoped
        to that id is correct and must not be warned about (Codex P2 on
        PR #774).
        """
        registry_row = _project(
            "4c9cf448-6209-43c2-8626-de5b0217527c",  # Marcus-generated
            provider_project_id="b45355cf",  # what the board is filtered by
        )
        fake = _fake_server(
            scoped_client=_scoped("b45355cf"), active_project=registry_row
        )

        with caplog.at_level(logging.WARNING, logger="src.marcus_mcp.server"):
            await _run_initialize(fake)

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    async def test_client_without_a_project_id_is_not_checked(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Providers that scope by board or repo expose no project_id; stay quiet."""
        client = Mock(spec=["connect", "create_task"])  # no project_id attribute
        fake = _fake_server(scoped_client=client, active_project=_project("proj-1"))

        with caplog.at_level(logging.WARNING, logger="src.marcus_mcp.server"):
            await _run_initialize(fake)

        assert fake.kanban_client is client
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    async def test_multi_project_mode_unchanged(self) -> None:
        """The multi-project path already did the right thing; it still does."""
        scoped = _scoped()
        fake = _fake_server(scoped_client=scoped, active_project=_project())

        await _run_initialize(fake, single=False)

        assert fake.kanban_client is scoped
        fake.initialize_kanban.assert_not_awaited()


class TestRegistryHonorsTheDataDir:
    """Issue #766 (and the #724 family): MARCUS_DATA_DIR must isolate the registry too."""

    def test_default_registry_dir_follows_marcus_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the override set, the registry lives under it."""
        from src.core.persistence import FilePersistence

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "isolated"))

        persistence = FilePersistence()

        assert persistence.storage_dir == tmp_path / "isolated" / "marcus_state"
        assert persistence.storage_dir.is_dir()

    def test_without_override_the_repo_default_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No override: the historical repo-root path, not a cwd-relative one."""
        from src.core import persistence as persistence_module
        from src.core.persistence import FilePersistence

        monkeypatch.delenv("MARCUS_DATA_DIR", raising=False)
        repo_root = Path(persistence_module.__file__).resolve().parent.parent.parent
        with patch.object(Path, "mkdir"):
            persistence = FilePersistence()

        assert persistence.storage_dir == repo_root / "data" / "marcus_state"
