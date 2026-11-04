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


class TestTheHistoryDatabaseFollowsTheDataDir:
    """``marcus.db`` is Marcus's own state and must move with the rest.

    The live check for this issue caught two leaks that the first pass
    missed, because three different notions of "the data directory" were
    in use at once:

    * the project-creation path built ``<checkout>/data/marcus.db`` from
      ``Path(__file__).parent.parent.parent``, so an isolated instance
      wrote task metadata, planning records and the About task's outcome
      straight into the 19 MB production history database.
    * ``config.data_dir`` defaults to the cwd-relative ``"./data"`` and
      never consulted the override at all, so a server started from a
      home directory created a stray ``~/data/marcus.db``.

    One resolver now answers the question for every caller.
    """

    def test_the_default_lands_in_the_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is written to the code checkout."""
        from src.core.paths import marcus_db_path

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))

        assert marcus_db_path() == tmp_path / "state" / "marcus.db"

    def test_the_config_default_does_not_reach_the_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``./data`` means the data directory, not the working directory.

        This is the stray ``~/data/marcus.db`` the live check found: the
        config's default was honoured literally, relative to wherever the
        server happened to start.
        """
        from src.core.paths import marcus_db_path

        monkeypatch.chdir(tmp_path / "..")
        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))

        assert marcus_db_path("./data") == tmp_path / "state" / "marcus.db"

    def test_an_absolute_configured_path_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator who gave a full path meant it."""
        from src.core.paths import marcus_db_path

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))
        configured = tmp_path / "elsewhere" / "history.db"

        assert marcus_db_path(str(configured)) == configured

    def test_the_shared_rule_is_one_implementation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The board and the history database resolve by the same rule.

        Two copies of this logic is how the notions diverged in the first
        place, so the board resolver delegates to the shared one.
        """
        from src.core.paths import resolve_data_path
        from src.integrations.providers.sqlite_kanban import _resolve_board_path

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))

        assert _resolve_board_path("data/kanban.db", "kanban.db") == resolve_data_path(
            "data/kanban.db", "kanban.db"
        )


class TestNoCallerInventsItsOwnHistoryPath:
    """A source guard, because this bug class is a new call site.

    Every leak found for this issue was a caller that built a data path
    itself instead of asking the resolver. Unit tests on the resolver
    cannot catch the next one, so the source is checked directly.
    """

    def test_nothing_builds_marcus_db_from_a_hardcoded_checkout(self) -> None:
        """No ``<something>/"data"/"marcus.db"`` outside the read path."""
        import re

        src = Path(__file__).parents[3] / "src"
        pattern = re.compile(r'/\s*"data"\s*/\s*"marcus\.db"')
        # The analysis read path takes an explicit root from its caller
        # and honours it, which is the documented behaviour; it reaches
        # the relocated store through the resolver when no root is given.
        allowed = {
            "analysis/aggregator.py",
            "analysis/storage/analysis_results.py",
            "analysis/helpers/conversation_index.py",
        }
        offenders = sorted(
            str(path.relative_to(src))
            for path in src.rglob("*.py")
            if pattern.search(path.read_text())
            and str(path.relative_to(src)) not in allowed
        )

        assert not offenders, (
            "these build marcus.db from a hardcoded root instead of "
            f"src.core.paths.marcus_db_path(): {offenders}"
        )

    def test_nothing_joins_marcus_db_onto_the_config_data_dir(self) -> None:
        """``config.data_dir`` is cwd-relative, so it must not be joined."""
        import re

        src = Path(__file__).parents[3] / "src"
        pattern = re.compile(r"data_dir\s*\)\s*\.expanduser\(\)\s*/\s*\"marcus\.db\"")
        offenders = sorted(
            str(path.relative_to(src))
            for path in src.rglob("*.py")
            if pattern.search(path.read_text())
        )

        assert not offenders, (
            "these resolve marcus.db against the cwd-relative config "
            f"data_dir instead of src.core.paths.marcus_db_path(): {offenders}"
        )


class TestConfigValidationCreatesNoStrayDirectories:
    """Validation must create the directory Marcus will actually use.

    ``MarcusConfig.validate`` creates ``data_dir`` and ``cache_dir`` to
    check they are writable, and it used the configured value literally.
    Both default to cwd-relative values, so a server started from a home
    directory left an empty ``~/data`` and ``~/cache`` behind even after
    every writer had been redirected. The live check for #792 caught
    this after the writers were fixed.
    """

    def test_the_configured_directories_resolve_against_the_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``./data`` is the data directory; ``./cache`` sits inside it."""
        from src.core.paths import resolve_data_subdir

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))

        assert resolve_data_subdir("./data") == tmp_path / "state"
        assert resolve_data_subdir("./cache") == tmp_path / "state" / "cache"

    def test_an_absolute_configured_directory_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator who gave a full path meant it."""
        from src.core.paths import resolve_data_subdir

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))

        assert resolve_data_subdir(str(tmp_path / "elsewhere")) == (
            tmp_path / "elsewhere"
        )

    def test_validation_leaves_nothing_in_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: no stray directory where the server started."""
        from src.config.marcus_config import MarcusConfig

        cwd = tmp_path / "somewhere-else"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))

        config = MarcusConfig()
        config.data_dir = "./data"
        config.cache_dir = "./cache"
        config.ai.anthropic_api_key = "test-key"
        config.validate()

        assert not (cwd / "data").exists()
        assert not (cwd / "cache").exists()
        assert (tmp_path / "state").is_dir()
        assert (tmp_path / "state" / "cache").is_dir()


class TestStoresThatLivedInTheCheckoutStayThere:
    """Without an override, a checkout-relative default must not move.

    Codex found this on the third pass, and it was a regression this
    branch introduced. Two kinds of historical default were conflated:

    * the board and ``config.data_dir`` defaulted to the **cwd-relative**
      ``./data``, so ``marcus_data_dir()`` is exact parity for them.
    * the worker roster, the project history and the creation-path
      writers built ``<checkout>/data`` from ``Path(__file__)``, on
      purpose, with the comment "use absolute path to ensure it works
      regardless of working directory".

    Routing the second group through ``marcus_data_dir()`` moved them to
    ``<cwd>/data`` whenever Marcus was started outside the checkout, so a
    production deployment restarted from elsewhere would silently stop
    rehydrating its registered workers (ADR-0012 O1) and abandon its
    history. ``marcus_state_dir()`` keeps the checkout for them while
    still honouring the override.
    """

    def test_the_checkout_is_kept_when_no_override_is_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The working directory does not decide where state lives."""
        from src.core.paths import marcus_state_dir

        monkeypatch.delenv("MARCUS_DATA_DIR", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.chdir(tmp_path)

        assert marcus_state_dir() == Path(__file__).parents[3] / "data"

    def test_the_override_still_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Isolation is still isolation; that is the whole issue."""
        from src.core.paths import marcus_state_dir

        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))

        assert marcus_state_dir() == tmp_path / "state"

    def test_the_pytest_guard_still_applies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolving the production directory under pytest is refused.

        Issue #724: tests once destroyed the live board this way, so the
        checkout-relative resolver has to refuse just as loudly as the
        cwd-relative one.
        """
        from src.core.paths import marcus_state_dir

        monkeypatch.delenv("MARCUS_DATA_DIR", raising=False)

        with pytest.raises(RuntimeError, match="724"):
            marcus_state_dir()

    def test_the_roster_keeps_its_checkout_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact case Codex named: a restart from another directory."""
        from src.core.assignment_persistence import AssignmentPersistence

        monkeypatch.delenv("MARCUS_DATA_DIR", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.chdir(tmp_path)
        store = AssignmentPersistence()

        assert store.storage_dir == (Path(__file__).parents[3] / "data" / "assignments")

    def test_the_history_keeps_its_checkout_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An existing deployment must not abandon its history."""
        from src.core.project_history import ProjectHistoryPersistence

        monkeypatch.delenv("MARCUS_DATA_DIR", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.chdir(tmp_path)
        store = ProjectHistoryPersistence()

        root = Path(__file__).parents[3]
        assert store.db_path == root / "data" / "marcus.db"
        assert store.history_dir == root / "data" / "project_history"

    def test_the_history_database_keeps_its_checkout_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The creation-path writers resolved against the checkout too."""
        from src.core.paths import marcus_db_path

        monkeypatch.delenv("MARCUS_DATA_DIR", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.chdir(tmp_path)

        assert marcus_db_path() == Path(__file__).parents[3] / "data" / "marcus.db"

    def test_a_configured_relative_path_is_still_cwd_relative(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``config.data_dir`` keeps its own historical meaning.

        Its default really was cwd-relative, so parity for it is the
        opposite of parity for the stores above. Conflating the two is
        what caused the regression.
        """
        from src.core.paths import marcus_db_path

        monkeypatch.delenv("MARCUS_DATA_DIR", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.chdir(tmp_path)

        assert marcus_db_path("./data") == tmp_path / "data" / "marcus.db"
