"""
Unit tests for the Marcus data-directory isolation guard (issue #724).

On 2026-08-09, pytest runs destroyed six live project boards: tests
construct real Marcus components whose data paths default to the
cwd-relative ``./data`` — the production data directory when running
from the repo root. This guard makes that impossible:

* ``marcus_data_dir()`` is the single resolver for default data paths.
  With ``MARCUS_DATA_DIR`` unset and pytest not running, it returns
  ``Path("data")`` — byte-identical to the historical behavior, so
  production is unchanged.
* Under pytest, the conftest isolation fixture points
  ``MARCUS_DATA_DIR`` at a session temp dir, so every default-path
  component writes there.
* Belt-and-braces: if anything resolves a default data path under
  pytest WITHOUT the override (fixture bypassed, env deleted), the
  resolver refuses loudly instead of touching production.
"""

import os
from pathlib import Path
from typing import Any

import pytest

from src.core.paths import DATA_DIR_ENV, marcus_data_dir

pytestmark = pytest.mark.unit


class TestResolver:
    """marcus_data_dir() is the one place defaults come from."""

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DATA_DIR_ENV, "/srv/marcus-data-elsewhere")
        assert marcus_data_dir() == Path("/srv/marcus-data-elsewhere")

    def test_production_parity_when_not_under_pytest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env unset + not pytest -> exactly the historical Path('data')."""
        monkeypatch.delenv(DATA_DIR_ENV, raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        assert marcus_data_dir() == Path("data")

    def test_refuses_production_default_under_pytest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under pytest with no override, refuse rather than resolve."""
        monkeypatch.delenv(DATA_DIR_ENV, raising=False)
        # PYTEST_CURRENT_TEST is set by pytest itself right now.
        with pytest.raises(RuntimeError, match="724"):
            marcus_data_dir()


class TestFixtureIsolation:
    """The autouse conftest fixture must make defaults land in a temp dir."""

    def test_suite_env_points_at_temp_dir(self) -> None:
        """The isolation fixture set MARCUS_DATA_DIR for this session."""
        value = os.environ.get(DATA_DIR_ENV)
        assert value, "isolation fixture did not set MARCUS_DATA_DIR"
        assert "marcus" in Path(value).parts[-1] or "pytest" in value

    def test_resolver_returns_the_temp_dir(self) -> None:
        resolved = marcus_data_dir()
        assert resolved.is_absolute()
        assert str(resolved) == os.environ[DATA_DIR_ENV]


class TestComponentsUseTheResolver:
    """Default-constructed components must land inside the isolated dir."""

    @pytest.mark.asyncio
    async def test_sqlite_default_db_is_isolated(self) -> None:
        """SQLiteKanban({}) must resolve under MARCUS_DATA_DIR, not ./data."""
        from src.integrations.providers.sqlite_kanban import SQLiteKanban

        provider = SQLiteKanban({})
        isolated = Path(os.environ[DATA_DIR_ENV])
        assert provider.db_path.is_relative_to(isolated)

    def test_audit_logger_default_is_isolated(self) -> None:
        from src.marcus_mcp.audit import AuditLogger

        logger_ = AuditLogger()
        isolated = Path(os.environ[DATA_DIR_ENV])
        assert Path(logger_.log_dir).is_relative_to(isolated)

    def test_token_tracker_default_is_isolated(self) -> None:
        from src.cost_tracking.token_tracker import TokenTracker

        tracker = TokenTracker()
        isolated = Path(os.environ[DATA_DIR_ENV])
        assert Path(tracker.data_file).is_relative_to(isolated)


class TestFactoryFallbackIsIsolated:
    """The kanban factory's fallback bypassed the provider default (#724).

    Test-built real servers go through KanbanFactory, which passed an
    EXPLICIT "./data/kanban.db" into the provider — explicit paths are
    honored, so the guard never fired and the full suite wrote 8 rows
    into the production board even after the first fix. The factory
    fallback must route through the shared resolver.
    """

    def test_factory_sqlite_fallback_lands_in_isolated_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SQLITE_KANBAN_DB_PATH", raising=False)
        from src.integrations.kanban_factory import KanbanFactory

        provider = KanbanFactory.create("sqlite")
        isolated = Path(os.environ[DATA_DIR_ENV])
        assert Path(provider.db_path).is_relative_to(isolated)


class TestDurabilityOnConnect:
    """WAL durability measures added after the #724 whodunit.

    The boards were lost because three months of commits lived only in
    the kanban.db-wal sidecar (a pinned reader stalled checkpoints at
    2026-05-14) and the sidecar was destroyed. Three measures on
    connect(): force a TRUNCATE checkpoint so the main file is always
    current, log the resolved absolute db path + WAL size so a growing
    sidecar is visible, and snapshot via the SQLite backup API (which is
    WAL-safe — a bare file copy without sidecars reproduces the loss).
    """

    async def _provider(self, tmp_path: Any, name: str = "k.db") -> Any:
        from src.integrations.providers.sqlite_kanban import SQLiteKanban

        provider = SQLiteKanban({"db_path": str(tmp_path / name)})
        assert await provider.connect()
        return provider

    @pytest.mark.asyncio
    async def test_connect_truncates_the_wal(self, tmp_path: Any) -> None:
        """After connect, a bare copy of the main file holds all data."""
        import shutil
        import sqlite3

        provider = await self._provider(tmp_path)
        task = await provider.create_task({"name": "T", "description": "d"})
        await provider.disconnect()

        provider2 = await self._provider(tmp_path)  # reconnect checkpoints
        bare = tmp_path / "bare-copy.db"
        shutil.copyfile(tmp_path / "k.db", bare)  # deliberately no sidecars
        con = sqlite3.connect(f"file:{bare}?mode=ro", uri=True)
        count = list(con.execute("SELECT count(*) FROM tasks"))[0][0]
        assert count == 1, "main file must be self-sufficient after connect"

    @pytest.mark.asyncio
    async def test_connect_logs_resolved_path(
        self, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The absolute db path must appear in the connect log line."""
        import logging

        with caplog.at_level(logging.INFO):
            await self._provider(tmp_path)
        assert str(tmp_path) in caplog.text
        assert "wal" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_connect_snapshots_via_backup_api(self, tmp_path: Any) -> None:
        """An existing db gets a readable snapshot under backups/."""
        import sqlite3

        from src.integrations.providers import sqlite_kanban as mod

        provider = await self._provider(tmp_path)
        await provider.create_task({"name": "T", "description": "d"})
        await provider.disconnect()

        mod._SNAPSHOT_TAKEN.clear()  # allow a snapshot in this process
        await self._provider(tmp_path)

        backups = sorted((tmp_path / "backups").glob("kanban-*.db"))
        assert backups, "no snapshot created"
        con = sqlite3.connect(f"file:{backups[-1]}?mode=ro", uri=True)
        assert list(con.execute("SELECT count(*) FROM tasks"))[0][0] == 1

    @pytest.mark.asyncio
    async def test_snapshot_retention_keeps_seven(self, tmp_path: Any) -> None:
        """Old snapshots beyond the newest 7 are pruned."""
        from src.integrations.providers import sqlite_kanban as mod

        provider = await self._provider(tmp_path)
        await provider.create_task({"name": "T", "description": "d"})
        await provider.disconnect()

        backups_dir = tmp_path / "backups"
        backups_dir.mkdir(exist_ok=True)
        for i in range(9):
            (backups_dir / f"kanban-2026010{i}-000000.db").write_bytes(b"x")

        mod._SNAPSHOT_TAKEN.clear()
        await self._provider(tmp_path)

        remaining = sorted(backups_dir.glob("kanban-*.db"))
        assert len(remaining) == 7

    @pytest.mark.asyncio
    async def test_fresh_db_is_not_snapshotted(self, tmp_path: Any) -> None:
        """A brand-new (empty) database produces no snapshot noise."""
        await self._provider(tmp_path, name="fresh.db")
        assert not (tmp_path / "backups").exists()
