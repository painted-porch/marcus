"""
Unit tests for MARCUS_DATA_DIR actually isolating Marcus's state (#792).

``MARCUS_DATA_DIR`` exists so two Marcus instances can run on one machine
without sharing state. Four stores claim to be Marcus's state, and only
the board honoured the variable:

* the project registry's board path was stored **relative**
  (``./data/kanban.db``), so it resolved against whatever directory the
  server happened to start in rather than the data directory. A project
  created at runtime became unreadable immediately afterwards, because
  Marcus read a different file from the one it had just written.
* the assignment store, which holds the worker roster
  (``registrations.json``), resolved against the code checkout. Two
  instances with different data directories therefore shared one roster,
  and ADR-0012 O1's rehydration could hand one instance the other's
  registrations.
* the project history (``marcus.db`` and ``project_history/``) resolved
  against the code checkout too, so a live check accumulated state inside
  a git worktree.
* the cost store was hardcoded at ``~/.marcus/costs.db``.

Every failure was silent, which is why this is tested rather than
documented.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


class TestAssignmentStoreHonoursTheDataDir:
    """The worker roster must not be shared between instances."""

    def test_default_lands_in_the_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two instances with different data directories get their own."""
        from src.core.assignment_persistence import AssignmentPersistence

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "a"))
        first = AssignmentPersistence()
        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "b"))
        second = AssignmentPersistence()

        assert first.storage_dir == tmp_path / "a" / "assignments"
        assert second.storage_dir == tmp_path / "b" / "assignments"
        assert first.assignments_file != second.assignments_file

    def test_the_roster_is_not_shared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The file ADR-0012 O1 rehydrates from is per instance.

        This is the one that was observed: a server started with its own
        data directory logged "Rehydrated 3 worker registration(s)" from
        the code checkout's copy.
        """
        from src.core.assignment_persistence import AssignmentPersistence
        from src.core.registration_persistence import RegistrationPersistence

        # The server builds the roster store from the assignment store's
        # directory (``server.py:234``), so fixing one fixes both. This
        # pins that wiring, because the roster is what ADR-0012 O1
        # rehydrates and sharing it across instances is the sharp edge.
        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "a"))
        first = RegistrationPersistence(AssignmentPersistence().storage_dir)
        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "b"))
        second = RegistrationPersistence(AssignmentPersistence().storage_dir)

        assert first.registrations_file == (
            tmp_path / "a" / "assignments" / "registrations.json"
        )
        assert second.registrations_file == (
            tmp_path / "b" / "assignments" / "registrations.json"
        )

    def test_an_explicit_path_still_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Callers that pass a directory are unaffected."""
        from src.core.assignment_persistence import AssignmentPersistence

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "ignored"))
        store = AssignmentPersistence(storage_dir=tmp_path / "explicit")

        assert store.storage_dir == tmp_path / "explicit"


class TestProjectHistoryHonoursTheDataDir:
    """History and marcus.db belong beside the rest of the state."""

    def test_history_and_db_land_in_the_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither goes near the code checkout."""
        from src.core.project_history import ProjectHistoryPersistence

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))
        store = ProjectHistoryPersistence()

        assert store.history_dir == tmp_path / "state" / "project_history"
        assert store.db_path == tmp_path / "state" / "marcus.db"

    def test_an_explicit_root_still_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The analysis tools pass a root explicitly and must keep working."""
        from src.core.project_history import ProjectHistoryPersistence

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "ignored"))
        store = ProjectHistoryPersistence(marcus_root=tmp_path / "repo")

        assert store.history_dir == tmp_path / "repo" / "data" / "project_history"
        assert store.db_path == tmp_path / "repo" / "data" / "marcus.db"


class TestTheBoardPathIsAbsolute:
    """The registry must not store a path that depends on the cwd."""

    def test_the_default_board_path_is_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative default is what made a project vanish after creation.

        The registry stored ``./data/kanban.db``, and the client rebuilt
        on the next project switch resolved it against the process's
        working directory instead of the data directory.
        """
        from src.marcus_mcp.tools.nlp import default_sqlite_provider_paths

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))
        paths = default_sqlite_provider_paths()

        assert Path(paths["db_path"]).is_absolute()
        assert Path(paths["attachments_dir"]).is_absolute()
        assert paths["db_path"] == str(tmp_path / "state" / "kanban.db")
        assert paths["attachments_dir"] == str(tmp_path / "state" / "attachments")

    def test_a_relative_configured_path_resolves_against_the_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hand-written relative path is legitimate and must be stable.

        Resolving it against the working directory means the same config
        points at different files depending on where Marcus was started.
        """
        from src.integrations.providers.sqlite_kanban import SQLiteKanban

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))
        board = SQLiteKanban({"db_path": "boards/mine.db"})

        assert board.db_path == tmp_path / "state" / "boards" / "mine.db"

    def test_an_absolute_configured_path_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator who gave a full path meant it."""
        from src.integrations.providers.sqlite_kanban import SQLiteKanban

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))
        board = SQLiteKanban({"db_path": str(tmp_path / "elsewhere" / "board.db")})

        assert board.db_path == tmp_path / "elsewhere" / "board.db"


class TestTheCostStoreHonoursTheDataDir:
    """An isolated instance must not write into the real cost history."""

    def test_the_override_redirects_the_cost_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asking for isolation should isolate spend too."""
        from src.core.paths import cost_store_path

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))

        assert cost_store_path() == tmp_path / "state" / "costs.db"

    def test_without_the_override_the_home_location_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Production is unchanged, so the dashboard keeps reading one file."""
        from src.core.paths import cost_store_path

        monkeypatch.delenv("MARCUS_DATA_DIR", raising=False)

        assert cost_store_path() == Path.home() / ".marcus" / "costs.db"


class TestTheDataDirPrefixIsAppliedOnce:
    """A relative ``MARCUS_DATA_DIR`` must not be prefixed twice.

    Two call sites used to build the default board path themselves by
    joining ``marcus_data_dir()``, and the provider's resolver then joined
    it again because the result was still relative. With
    ``MARCUS_DATA_DIR=scratch/state`` the startup client opened
    ``scratch/state/scratch/state/kanban.db`` while the registry recorded
    ``scratch/state/kanban.db``, which is the same split-brain the
    resolver exists to prevent (Codex P2 on PR #801).
    """

    def test_a_relative_override_resolves_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The data directory is joined a single time."""
        from src.integrations.providers.sqlite_kanban import SQLiteKanban

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MARCUS_DATA_DIR", "scratch/state")
        board = SQLiteKanban({"db_path": None})

        assert board.db_path == tmp_path / "scratch" / "state" / "kanban.db"

    def test_the_startup_client_and_the_registry_agree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What a created project records is what the client already uses.

        This is the user-visible half: a project created against the
        startup client has to stay readable when the client is rebuilt
        from the registry.
        """
        from src.integrations.providers.sqlite_kanban import SQLiteKanban
        from src.marcus_mcp.tools.nlp import default_sqlite_provider_paths

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MARCUS_DATA_DIR", "scratch/state")

        registered = default_sqlite_provider_paths()
        startup = SQLiteKanban({"db_path": None})

        assert Path(registered["db_path"]) == startup.db_path

    def test_an_absolute_override_is_returned_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Making the prefix absolute must not rewrite symlinks.

        An earlier pass at this used ``resolve()``, which turned a macOS
        ``/var/...`` data directory into ``/private/var/...``. Nothing
        broke functionally, because both name one file, but a caller
        comparing a resolved path against the configured value saw two
        different strings, and the suite's isolation guard failed.
        """
        from src.integrations.providers.sqlite_kanban import _resolve_board_path

        link = tmp_path / "link"
        link.symlink_to(tmp_path / "real", target_is_directory=True)
        monkeypatch.setenv("MARCUS_DATA_DIR", str(link))

        assert _resolve_board_path(None, "kanban.db") == link / "kanban.db"

    def test_resolving_an_already_resolved_path_is_stable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The resolver is idempotent, so a second pass cannot nest.

        Any future caller that pre-resolves is harmless, which is what
        keeps this class of bug from coming back.
        """
        from src.integrations.providers.sqlite_kanban import _resolve_board_path

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MARCUS_DATA_DIR", "scratch/state")

        once = _resolve_board_path(None, "kanban.db")
        twice = _resolve_board_path(str(once), "kanban.db")

        assert once == twice == tmp_path / "scratch" / "state" / "kanban.db"


class TestHistoryIsReadFromWhereItIsWritten:
    """The analysis read path must follow the relocated history store.

    ``query_project_history`` builds a ``ProjectHistoryAggregator`` with no
    root. The aggregator auto-detects the code checkout for conversation
    logs, which genuinely live beside the code, and used to pass that
    checkout straight through to ``ProjectHistoryPersistence``. Because an
    explicit root wins over the data directory, reads went to
    ``<checkout>/data`` while the writers used ``MARCUS_DATA_DIR``, so
    decisions and artifacts an agent had just logged came back empty
    (Codex P1 on PR #801).
    """

    def test_the_aggregator_reads_the_relocated_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reader and writer open the same file."""
        from src.analysis.aggregator import ProjectHistoryAggregator
        from src.core.project_history import ProjectHistoryPersistence

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))
        writer = ProjectHistoryPersistence()
        reader = ProjectHistoryAggregator()

        assert reader.history_persistence.db_path == writer.db_path
        assert reader.history_persistence.history_dir == writer.history_dir

    def test_marcus_state_follows_the_data_dir_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``marcus_state`` is data, so it moves with the rest."""
        from src.analysis.aggregator import ProjectHistoryAggregator

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))
        reader = ProjectHistoryAggregator()

        assert reader.state_dir == tmp_path / "state" / "marcus_state"

    def test_conversation_logs_stay_beside_the_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Logs are not data; moving them would break decision loading."""
        from src.analysis.aggregator import ProjectHistoryAggregator

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))
        reader = ProjectHistoryAggregator()

        assert reader.logs_dir == Path(__file__).parents[3] / "logs"

    def test_an_explicit_root_still_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller that passes a root means that root, for everything."""
        from src.analysis.aggregator import ProjectHistoryAggregator

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "ignored"))
        reader = ProjectHistoryAggregator(marcus_root=tmp_path / "repo")

        assert reader.state_dir == tmp_path / "repo" / "data" / "marcus_state"
        assert (
            reader.history_persistence.db_path
            == tmp_path / "repo" / "data" / "marcus.db"
        )
