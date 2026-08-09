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
