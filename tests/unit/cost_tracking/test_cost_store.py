"""
Unit tests for src.cost_tracking.cost_store.

Validates schema creation, seed loading, event/experiment writes, and the
v_event_cost view's price-versioning behavior.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from src.cost_tracking.cost_store import (
    CostStore,
    ModelPrice,
    Run,
    TokenEvent,
    _b,
)


@pytest.fixture
def store(tmp_path: Path) -> CostStore:
    """Provide a fresh CostStore backed by a tmp SQLite file."""
    db = tmp_path / "costs.db"
    return CostStore(db_path=db)


@pytest.fixture
def seeded_store(store: CostStore) -> CostStore:
    """CostStore with a single default model_prices row for tests."""
    store.record_price(
        ModelPrice(
            model="claude-sonnet-4-6",
            provider="anthropic",
            effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
            input_per_million=3.0,
            cache_creation_per_million=3.75,
            cache_read_per_million=0.30,
            output_per_million=15.0,
            source="default",
        )
    )
    return store


@pytest.fixture
def base_event() -> TokenEvent:
    """Reusable TokenEvent for happy-path tests."""
    return TokenEvent(
        run_id="exp_1",
        project_id="proj_1",
        agent_id="planner",
        agent_role="planner",
        operation="parse_prd",
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=1000,
        cache_creation_tokens=2000,
        cache_read_tokens=4000,
        output_tokens=500,
    )


class TestSchemaInitialization:
    """Schema and view creation."""

    def test_init_creates_all_tables(self, store: CostStore) -> None:
        """All four schema objects exist after construction."""
        names = {
            row[0]
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        assert {"runs", "token_events", "model_prices", "v_event_cost"} <= names

    def test_init_enables_wal_mode(self, store: CostStore) -> None:
        """WAL mode enabled for safe concurrent reads during writes."""
        mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_token_events_total_tokens_is_generated(
        self, seeded_store: CostStore, base_event: TokenEvent
    ) -> None:
        """total_tokens column auto-sums the four input fields."""
        seeded_store.record_event(base_event)
        row = seeded_store.conn.execute(
            "SELECT total_tokens FROM token_events"
        ).fetchone()
        assert row[0] == 1000 + 2000 + 4000 + 500


class TestRunsRenameMigration:
    """Regression tests for the ``experiments`` → ``runs`` rename.

    Locks in :meth:`CostStore._runs_rename_migration`. Opening a
    pre-rename DB must transparently:

    - Rename the ``experiments`` table to ``runs``.
    - Rename the ``experiment_id`` column to ``run_id`` on both
      ``runs`` and ``token_events``.
    - Add the new ``path`` column on ``runs`` with default
      ``'unknown'`` for legacy rows.
    - Preserve all existing data.

    Without these tests, a future refactor could silently break
    upgrades for any deployed Marcus that already has cost data.
    """

    def _build_legacy_db(self, db_path: Path) -> None:
        """Hand-write a pre-rename schema with one experiment + one event."""
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
        CREATE TABLE experiments (
          experiment_id    TEXT PRIMARY KEY,
          project_id       TEXT NOT NULL,
          board_id         TEXT,
          project_name     TEXT,
          decomposer       TEXT,
          complexity       TEXT,
          provider         TEXT,
          model            TEXT,
          num_agents       INTEGER,
          started_at       TIMESTAMP NOT NULL,
          ended_at         TIMESTAMP,
          total_tasks      INTEGER,
          completed_tasks  INTEGER,
          blocked_tasks    INTEGER,
          budget_usd       REAL,
          notes            TEXT
        );
        CREATE INDEX idx_exp_project ON experiments(project_id);

        CREATE TABLE token_events (
          event_id              INTEGER PRIMARY KEY AUTOINCREMENT,
          experiment_id         TEXT NOT NULL,
          project_id            TEXT NOT NULL,
          agent_id              TEXT NOT NULL,
          agent_role            TEXT NOT NULL,
          parent_agent_id       TEXT,
          task_id               TEXT,
          subtask_id            TEXT,
          operation             TEXT NOT NULL,
          provider              TEXT NOT NULL,
          model                 TEXT NOT NULL,
          input_tokens          INTEGER NOT NULL DEFAULT 0,
          cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
          cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
          output_tokens         INTEGER NOT NULL DEFAULT 0,
          total_tokens          INTEGER GENERATED ALWAYS AS
                                  (input_tokens + cache_creation_tokens
                                   + cache_read_tokens + output_tokens)
                                STORED,
          latency_ms            INTEGER,
          session_id            TEXT,
          turn_index            INTEGER,
          request_id            TEXT,
          status                TEXT NOT NULL DEFAULT 'ok',
          error_type            TEXT,
          timestamp             TIMESTAMP NOT NULL
                                DEFAULT '2026-05-12T00:00:00.000Z'
        );
        CREATE INDEX idx_te_exp     ON token_events(experiment_id);
        CREATE INDEX idx_te_project ON token_events(project_id);
        CREATE INDEX idx_te_agent   ON token_events(experiment_id, agent_id);
        CREATE INDEX idx_te_task    ON token_events(task_id);
        CREATE UNIQUE INDEX ux_te_request_id
            ON token_events(request_id) WHERE request_id IS NOT NULL;

        INSERT INTO experiments
            (experiment_id, project_id, project_name, started_at)
            VALUES ('e_old', 'p1', 'Legacy Project', '2026-05-11T00:00:00Z');
        INSERT INTO token_events
            (experiment_id, project_id, agent_id, agent_role,
             operation, provider, model, input_tokens)
            VALUES ('e_old', 'p1', 'planner', 'planner',
                    'parse_prd', 'anthropic',
                    'claude-sonnet-4-6', 100);
        """)
        conn.commit()
        conn.close()

    def test_migration_renames_table_and_columns(self, tmp_path: Path) -> None:
        """After CostStore opens a legacy DB, the new schema is in place."""
        db = tmp_path / "legacy.db"
        self._build_legacy_db(db)

        store = CostStore(db_path=db)

        # ``runs`` table exists; ``experiments`` is gone.
        names = {
            row[0]
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "runs" in names
        assert "experiments" not in names

        # ``runs.run_id`` exists; old ``experiment_id`` is gone.
        run_cols = {
            c[1] for c in store.conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert "run_id" in run_cols
        assert "experiment_id" not in run_cols

        # ``token_events.run_id`` exists; old ``experiment_id`` is gone.
        te_cols = {
            c[1]
            for c in store.conn.execute("PRAGMA table_info(token_events)").fetchall()
        }
        assert "run_id" in te_cols
        assert "experiment_id" not in te_cols

    def test_migration_adds_path_column_with_unknown_default(
        self, tmp_path: Path
    ) -> None:
        """``runs.path`` is added with 'unknown' for legacy rows."""
        db = tmp_path / "legacy.db"
        self._build_legacy_db(db)

        store = CostStore(db_path=db)

        path = store.conn.execute("SELECT path FROM runs").fetchone()[0]
        assert path == "unknown"

    def test_migration_preserves_data(self, tmp_path: Path) -> None:
        """Existing experiments + token_events rows survive the rename."""
        db = tmp_path / "legacy.db"
        self._build_legacy_db(db)

        store = CostStore(db_path=db)

        run_row = store.conn.execute(
            "SELECT run_id, project_id, project_name FROM runs"
        ).fetchone()
        assert run_row == ("e_old", "p1", "Legacy Project")

        te_row = store.conn.execute(
            "SELECT run_id, project_id, operation, input_tokens FROM token_events"
        ).fetchone()
        assert te_row == ("e_old", "p1", "parse_prd", 100)

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        """Re-opening an already-migrated DB is a no-op (no errors)."""
        db = tmp_path / "legacy.db"
        self._build_legacy_db(db)

        # First open triggers the migration
        store_a = CostStore(db_path=db)
        path_after_first = store_a.conn.execute("SELECT path FROM runs").fetchone()[0]
        store_a.conn.close()

        # Second open must succeed without raising and not double-rename
        store_b = CostStore(db_path=db)
        path_after_second = store_b.conn.execute("SELECT path FROM runs").fetchone()[0]
        assert path_after_second == path_after_first

    def test_migration_skipped_on_fresh_install(self, tmp_path: Path) -> None:
        """A fresh DB (no legacy tables) skips the rename path cleanly."""
        # Just create with no prep — SCHEMA_SQL builds the new layout
        # directly and the migration short-circuits.
        store = CostStore(db_path=tmp_path / "fresh.db")
        cols = {c[1] for c in store.conn.execute("PRAGMA table_info(runs)").fetchall()}
        assert "run_id" in cols
        assert "path" in cols


class TestPhase0PersistSignalsMigration:
    """Regression tests for Phase 0 column adds (Marcus #546).

    Phase 0 (ML forecasting umbrella #544) adds 12 columns to ``runs``
    and 2 columns to ``token_events`` so already-captured signals can
    be queried directly instead of reconstructed from event logs.

    The migration is ``CostStore._phase0_persist_signals_migration``.
    It must:

    - Add all 14 columns idempotently (PRAGMA check first).
    - Be lock-friendly: SQLite ``ALTER TABLE ADD COLUMN`` is
      metadata-only, so it does not rewrite the table even on a
      multi-million-row ``token_events``.
    - Leave legacy rows with NULL in every new column (no backfill).
    - Survive re-opening an already-migrated DB without errors.

    Falsification recipe: comment out the ``_phase0_persist_signals_migration``
    call in ``_initialize_database`` and confirm
    ``test_migration_adds_all_runs_columns`` fails with the missing
    column name in the assertion message.
    """

    #: Phase 0 column names — imported from the production tuples
    #: so the test list cannot drift from the migrator's list.  If a
    #: 13th column is added to the migrator without updating tests,
    #: every test in this class still validates the full set (no
    #: silent under-coverage).  Kaia review fix.
    EXPECTED_RUNS_COLUMNS = {name for name, _ in CostStore._PHASE0_RUNS_COLUMNS}
    EXPECTED_TOKEN_EVENTS_COLUMNS = {
        name for name, _ in CostStore._PHASE0_TOKEN_EVENTS_COLUMNS
    }

    def _build_post3_db(self, db_path: Path) -> None:
        """Hand-write a v0.3.6.post3-era schema with one run + one event.

        This is the state of cost.db immediately *after* the
        ``runs`` rename + tool_intent migrations from v0.3.6 land but
        *before* Phase 0 columns exist. Phase 0 must migrate cleanly
        from this state.
        """
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
        CREATE TABLE runs (
          run_id           TEXT PRIMARY KEY,
          project_id       TEXT NOT NULL,
          board_id         TEXT,
          project_name     TEXT,
          decomposer       TEXT,
          complexity       TEXT,
          provider         TEXT,
          model            TEXT,
          num_agents       INTEGER,
          started_at       TIMESTAMP NOT NULL,
          ended_at         TIMESTAMP,
          total_tasks      INTEGER,
          completed_tasks  INTEGER,
          blocked_tasks    INTEGER,
          budget_usd       REAL,
          notes            TEXT,
          path             TEXT NOT NULL DEFAULT 'unknown'
        );

        CREATE TABLE token_events (
          event_id              INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id                TEXT NOT NULL,
          project_id            TEXT NOT NULL,
          agent_id              TEXT NOT NULL,
          agent_role            TEXT NOT NULL,
          parent_agent_id       TEXT,
          task_id               TEXT,
          subtask_id            TEXT,
          operation             TEXT NOT NULL,
          tool_intent           TEXT,
          provider              TEXT NOT NULL,
          model                 TEXT NOT NULL,
          input_tokens          INTEGER NOT NULL DEFAULT 0,
          cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
          cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
          output_tokens         INTEGER NOT NULL DEFAULT 0,
          total_tokens          INTEGER GENERATED ALWAYS AS
                                  (input_tokens + cache_creation_tokens
                                   + cache_read_tokens + output_tokens)
                                STORED,
          latency_ms            INTEGER,
          session_id            TEXT,
          turn_index            INTEGER,
          request_id            TEXT,
          status                TEXT NOT NULL DEFAULT 'ok',
          error_type            TEXT,
          timestamp             TIMESTAMP NOT NULL
                                DEFAULT '2026-05-12T00:00:00.000Z'
        );

        INSERT INTO runs (run_id, project_id, project_name, started_at)
            VALUES ('r_old', 'p1', 'Pre-Phase0 Project',
                    '2026-05-13T00:00:00Z');
        INSERT INTO token_events
            (run_id, project_id, agent_id, agent_role,
             operation, provider, model, input_tokens)
            VALUES ('r_old', 'p1', 'planner', 'planner',
                    'parse_prd', 'anthropic',
                    'claude-sonnet-4-6', 100);
        """)
        conn.commit()
        conn.close()

    def test_migration_adds_all_runs_columns(self, tmp_path: Path) -> None:
        """After Phase 0 migration, every expected runs column exists."""
        db = tmp_path / "post3.db"
        self._build_post3_db(db)

        store = CostStore(db_path=db)

        cols = {c[1] for c in store.conn.execute("PRAGMA table_info(runs)").fetchall()}
        missing = self.EXPECTED_RUNS_COLUMNS - cols
        assert not missing, f"Phase 0 migration left columns missing on runs: {missing}"

    def test_migration_adds_all_token_events_columns(self, tmp_path: Path) -> None:
        """After Phase 0 migration, every expected token_events column exists."""
        db = tmp_path / "post3.db"
        self._build_post3_db(db)

        store = CostStore(db_path=db)

        cols = {
            c[1]
            for c in store.conn.execute("PRAGMA table_info(token_events)").fetchall()
        }
        missing = self.EXPECTED_TOKEN_EVENTS_COLUMNS - cols
        assert (
            not missing
        ), f"Phase 0 migration left columns missing on token_events: {missing}"

    def test_migration_preserves_existing_data(self, tmp_path: Path) -> None:
        """Pre-Phase-0 rows survive the migration unchanged."""
        db = tmp_path / "post3.db"
        self._build_post3_db(db)

        store = CostStore(db_path=db)

        run = store.conn.execute(
            "SELECT run_id, project_id, project_name FROM runs"
        ).fetchone()
        assert run == ("r_old", "p1", "Pre-Phase0 Project")

        te = store.conn.execute(
            "SELECT run_id, project_id, operation, input_tokens FROM token_events"
        ).fetchone()
        assert te == ("r_old", "p1", "parse_prd", 100)

    def test_legacy_rows_have_null_in_new_columns(self, tmp_path: Path) -> None:
        """Phase 0 does not backfill; legacy rows have NULL in new columns.

        Backfill is intentionally out-of-scope for the migration — it
        is a separate concern that the subscribers (Task #6) handle
        going forward, and historical NULLs are honest signal: "we did
        not capture this back then." A silent backfill would
        manufacture fake history.
        """
        db = tmp_path / "post3.db"
        self._build_post3_db(db)

        store = CostStore(db_path=db)

        new_col_list = ", ".join(sorted(self.EXPECTED_RUNS_COLUMNS))
        row = store.conn.execute(
            f"SELECT {new_col_list} FROM runs WHERE run_id = 'r_old'"
        ).fetchone()
        assert all(v is None for v in row), (
            f"Expected all new columns NULL for legacy row, got: "
            f"{dict(zip(sorted(self.EXPECTED_RUNS_COLUMNS), row))}"
        )

        te_new_cols = ", ".join(sorted(self.EXPECTED_TOKEN_EVENTS_COLUMNS))
        te_row = store.conn.execute(
            f"SELECT {te_new_cols} FROM token_events"
        ).fetchone()
        assert all(v is None for v in te_row), (
            f"Expected all new token_events columns NULL for legacy row, got: "
            f"{dict(zip(sorted(self.EXPECTED_TOKEN_EVENTS_COLUMNS), te_row))}"
        )

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        """Re-opening an already-migrated DB does not raise or re-add columns."""
        db = tmp_path / "post3.db"
        self._build_post3_db(db)

        store_a = CostStore(db_path=db)
        cols_after_first = {
            c[1] for c in store_a.conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        store_a.conn.close()

        # Second open must succeed without raising. Re-running an
        # already-applied ALTER TABLE would crash with "duplicate
        # column name" — the migration must PRAGMA-check first.
        store_b = CostStore(db_path=db)
        cols_after_second = {
            c[1] for c in store_b.conn.execute("PRAGMA table_info(runs)").fetchall()
        }

        assert cols_after_first == cols_after_second

    def test_migration_skipped_on_fresh_install(self, tmp_path: Path) -> None:
        """A brand-new DB gets all Phase 0 columns via SCHEMA_SQL, not the migrator."""
        store = CostStore(db_path=tmp_path / "fresh.db")

        runs_cols = {
            c[1] for c in store.conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        te_cols = {
            c[1]
            for c in store.conn.execute("PRAGMA table_info(token_events)").fetchall()
        }

        assert self.EXPECTED_RUNS_COLUMNS <= runs_cols
        assert self.EXPECTED_TOKEN_EVENTS_COLUMNS <= te_cols

    def test_migration_resumes_after_partial_failure(self, tmp_path: Path) -> None:
        """Per-column PRAGMA check resumes cleanly from a partial migration.

        Simulates the failure mode that motivates the per-column
        granularity in the migrator: Marcus crashed (or was killed)
        partway through Phase 0 with only some columns added.  The
        next open must add the missing columns and leave the
        already-present ones untouched.

        A naive whole-table ``ALTER TABLE`` migrator that ran without
        per-column checks would crash on the second open with a
        ``duplicate column name`` error.  This test exists to lock in
        the chosen per-column design.

        Falsification recipe: replace the ``if col_name not in
        runs_cols`` guard in ``_phase0_persist_signals_migration``
        with an unconditional ``ALTER TABLE`` loop and confirm this
        test fails with ``sqlite3.OperationalError: duplicate
        column name``.
        """
        db = tmp_path / "partial.db"
        self._build_post3_db(db)

        # Manually add just one Phase 0 column before the migrator
        # runs.  This simulates a previous attempt that crashed after
        # writing the first ALTER but before the second.
        conn = sqlite3.connect(str(db))
        conn.execute("ALTER TABLE runs ADD COLUMN intent_fidelity_score REAL")
        conn.commit()
        conn.close()

        # Opening the store must complete without error and end with
        # ALL Phase 0 columns present.
        store = CostStore(db_path=db)

        cols = {c[1] for c in store.conn.execute("PRAGMA table_info(runs)").fetchall()}
        missing = self.EXPECTED_RUNS_COLUMNS - cols
        assert (
            not missing
        ), f"Migrator did not resume from partial state; missing: {missing}"


class TestBoolToIntHelper:
    """Tests for :func:`_b`, the bool→int coercion helper.

    The helper exists so Phase 0 subscribers (Task #6) writing to
    ``runs.did_complete``, ``runs.is_local_llm`` and
    ``token_events.was_retry`` cannot silently pass non-bool values
    that get coerced to 1 via Python truthiness.  ``_b("true")``
    must raise instead of returning 1.
    """

    def test_true_maps_to_one(self) -> None:
        """``True`` becomes ``1``."""
        assert _b(True) == 1

    def test_false_maps_to_zero(self) -> None:
        """``False`` becomes ``0``."""
        assert _b(False) == 0

    def test_none_passes_through(self) -> None:
        """``None`` stays ``None`` (column is nullable)."""
        assert _b(None) is None

    def test_string_raises(self) -> None:
        """Strings raise TypeError — no truthiness coercion.

        This is the load-bearing safety property: a config read that
        returns the string ``"true"`` must NOT silently write 1 to
        the DB.  It must crash so the bug is visible.
        """
        with pytest.raises(TypeError, match="requires bool or None"):
            _b("true")  # type: ignore[arg-type]

    def test_int_raises(self) -> None:
        """Non-bool ints raise TypeError — even 0 and 1.

        SQLite would accept these directly, but accepting them in
        ``_b`` defeats the type guard.  Force call sites to pass
        proper bools or None.
        """
        with pytest.raises(TypeError, match="requires bool or None"):
            _b(1)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="requires bool or None"):
            _b(0)  # type: ignore[arg-type]


class TestRecordEvent:
    """token_events insert behavior."""

    def test_records_event_returns_event_id(
        self, seeded_store: CostStore, base_event: TokenEvent
    ) -> None:
        """record_event returns the auto-incremented PK."""
        event_id = seeded_store.record_event(base_event)
        assert isinstance(event_id, int) and event_id > 0

    def test_records_event_persists_all_fields(
        self, seeded_store: CostStore, base_event: TokenEvent
    ) -> None:
        """Every TokenEvent field round-trips through the DB."""
        seeded_store.record_event(base_event)
        row = seeded_store.conn.execute(
            "SELECT run_id, agent_id, agent_role, operation, "
            "input_tokens, cache_creation_tokens, cache_read_tokens, "
            "output_tokens FROM token_events"
        ).fetchone()
        assert row == (
            "exp_1",
            "planner",
            "planner",
            "parse_prd",
            1000,
            2000,
            4000,
            500,
        )

    def test_default_status_is_ok(
        self, seeded_store: CostStore, base_event: TokenEvent
    ) -> None:
        """status defaults to 'ok' when not provided."""
        seeded_store.record_event(base_event)
        status = seeded_store.conn.execute(
            "SELECT status FROM token_events"
        ).fetchone()[0]
        assert status == "ok"

    def test_duplicate_request_id_is_ignored(
        self, seeded_store: CostStore, base_event: TokenEvent
    ) -> None:
        """Re-inserting an event with the same request_id is idempotent.

        Cato's dashboard polls run_ingest every 30s with a fresh ingester
        whose in-memory dedup set is empty. Without DB-level dedup, every
        poll re-inserts every event and counts double silently. The
        partial UNIQUE index on request_id plus INSERT OR IGNORE
        guarantees idempotency regardless of process boundaries.
        """
        base_event.request_id = "req_dup_1"
        first_id = seeded_store.record_event(base_event)
        second_id = seeded_store.record_event(base_event)

        assert first_id == second_id
        count = seeded_store.conn.execute(
            "SELECT COUNT(*) FROM token_events WHERE request_id = 'req_dup_1'"
        ).fetchone()[0]
        assert count == 1

    def test_init_dedups_preexisting_duplicates(self, tmp_path: Path) -> None:
        """Opening a dirty DB compacts duplicates before adding the index.

        Simulates a pre-PR-#511 install: build a DB without the partial
        UNIQUE index, hand-insert duplicate request_id rows (which the
        old INSERT path allowed), then re-open via CostStore. The
        constructor's migration must dedup so CREATE UNIQUE INDEX
        succeeds and Marcus starts.
        """
        db = tmp_path / "dirty.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE token_events (
              event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              project_id    TEXT NOT NULL,
              agent_id      TEXT NOT NULL,
              agent_role    TEXT NOT NULL,
              parent_agent_id TEXT,
              task_id       TEXT,
              subtask_id    TEXT,
              operation     TEXT NOT NULL,
              provider      TEXT NOT NULL,
              model         TEXT NOT NULL,
              input_tokens  INTEGER NOT NULL DEFAULT 0,
              cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
              cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
              output_tokens INTEGER NOT NULL DEFAULT 0,
              total_tokens  INTEGER GENERATED ALWAYS AS
                              (input_tokens + cache_creation_tokens
                               + cache_read_tokens + output_tokens) STORED,
              latency_ms    INTEGER,
              session_id    TEXT,
              turn_index    INTEGER,
              request_id    TEXT,
              status        TEXT NOT NULL DEFAULT 'ok',
              error_type    TEXT,
              timestamp     TIMESTAMP NOT NULL DEFAULT '2026-05-11T00:00:00.000Z'
            );
            """)
        for _ in range(3):
            conn.execute(
                "INSERT INTO token_events "
                "(run_id, project_id, agent_id, agent_role, operation, "
                " provider, model, request_id) VALUES "
                "('e','p','a','planner','op','anthropic','m','req_dup')"
            )
        for _ in range(2):
            conn.execute(
                "INSERT INTO token_events "
                "(run_id, project_id, agent_id, agent_role, operation, "
                " provider, model, request_id) VALUES "
                "('e','p','a','planner','op','anthropic','m', NULL)"
            )
        conn.commit()
        conn.close()

        store = CostStore(db_path=db)

        dup_count = store.conn.execute(
            "SELECT COUNT(*) FROM token_events WHERE request_id = 'req_dup'"
        ).fetchone()[0]
        assert dup_count == 1
        null_count = store.conn.execute(
            "SELECT COUNT(*) FROM token_events WHERE request_id IS NULL"
        ).fetchone()[0]
        assert null_count == 2
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(
                "INSERT INTO token_events "
                "(run_id, project_id, agent_id, agent_role, operation, "
                " provider, model, request_id) VALUES "
                "('e','p','a','planner','op','anthropic','m','req_dup')"
            )

    def test_dedup_migration_skipped_when_index_already_exists(
        self, tmp_path: Path
    ) -> None:
        """Subsequent CostStore opens must not run the DELETE.

        Once ``ux_te_request_id`` exists the DB has no duplicates by
        construction. The migration must short-circuit so it doesn't
        take a write lock on every startup — that's what caused
        ``OperationalError: database is locked`` against a concurrently-
        polled Cato. Uses ``set_trace_callback`` to capture every SQL
        statement issued during init and asserts none of them is the
        dedup DELETE.
        """
        db = tmp_path / "x.db"
        # First open creates the index.
        CostStore(db_path=db).conn.close()

        # Second open: spy on all SQL via trace callback before init runs.
        seen: list[str] = []
        original_connect = sqlite3.connect

        def tracing_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            conn = original_connect(*args, **kwargs)  # type: ignore[arg-type]
            conn.set_trace_callback(seen.append)
            return conn

        import unittest.mock as mock

        with mock.patch(
            "src.cost_tracking.cost_store.sqlite3.connect", tracing_connect
        ):
            CostStore(db_path=db)

        assert not any(
            "DELETE FROM token_events" in s for s in seen
        ), f"dedup DELETE issued despite existing index; statements: {seen}"

    def test_null_request_id_allows_multiple_rows(
        self, seeded_store: CostStore, base_event: TokenEvent
    ) -> None:
        """NULL request_id is exempt from the unique constraint.

        Older rows and non-Claude providers may lack a request_id; the
        partial WHERE clause keeps them unconstrained so multiple NULLs
        can coexist.
        """
        assert base_event.request_id is None
        seeded_store.record_event(base_event)
        seeded_store.record_event(base_event)
        count = seeded_store.conn.execute(
            "SELECT COUNT(*) FROM token_events WHERE request_id IS NULL"
        ).fetchone()[0]
        assert count == 2


class TestProjectNames:
    """Persistent project-name snapshot for cost-data attribution.

    The Marcus project registry can delete a project at any time, but
    its cost data lives forever. ``project_names`` snapshots the name
    so the dashboard can render the right label after deletion.
    """

    def test_upsert_then_get_roundtrips(self, store: CostStore) -> None:
        """Round-trip a (project_id, name) through the names table."""
        store.upsert_project_name("proj_abc", "hangman")
        assert store.get_project_name("proj_abc") == "hangman"

    def test_upsert_is_idempotent(self, store: CostStore) -> None:
        """Same (id, name) twice doesn't duplicate; bumps last_seen."""
        store.upsert_project_name("proj_abc", "hangman")
        store.upsert_project_name("proj_abc", "hangman")
        count = store.conn.execute(
            "SELECT COUNT(*) FROM project_names WHERE project_id='proj_abc'"
        ).fetchone()[0]
        assert count == 1

    def test_upsert_updates_changed_name(self, store: CostStore) -> None:
        """Renaming the same project_id updates in place."""
        store.upsert_project_name("proj_abc", "old-name")
        store.upsert_project_name("proj_abc", "new-name")
        assert store.get_project_name("proj_abc") == "new-name"

    def test_get_returns_none_when_unknown(self, store: CostStore) -> None:
        """Unknown project_id returns None, not an empty string."""
        assert store.get_project_name("never_seen") is None

    def test_upsert_ignores_empty_inputs(self, store: CostStore) -> None:
        """Empty id or name is a silent no-op."""
        store.upsert_project_name("", "x")
        store.upsert_project_name("x", "")
        count = store.conn.execute("SELECT COUNT(*) FROM project_names").fetchone()[0]
        assert count == 0


class TestRebindProjectId:
    """UPDATE token_events from a placeholder project_id to the real one.

    Drives the create_project two-phase attribution flow: tool entry
    pushes a ``pending:<hex>`` placeholder; tool exit rebinds every row
    to the real project_id that the registry just assigned.
    """

    def test_rebinds_all_matching_rows(
        self, seeded_store: CostStore, base_event: TokenEvent
    ) -> None:
        """Every row tagged with from_id flips to to_id."""
        base_event.project_id = "pending:abc123"
        base_event.request_id = "req_rebind_1"
        seeded_store.record_event(base_event)
        base_event.request_id = "req_rebind_2"
        seeded_store.record_event(base_event)

        n = seeded_store.rebind_project_id(from_id="pending:abc123", to_id="real_xyz")
        assert n == 2

        remaining = seeded_store.conn.execute(
            "SELECT COUNT(*) FROM token_events WHERE project_id='pending:abc123'"
        ).fetchone()[0]
        assert remaining == 0
        moved = seeded_store.conn.execute(
            "SELECT COUNT(*) FROM token_events WHERE project_id='real_xyz'"
        ).fetchone()[0]
        assert moved == 2

    def test_returns_zero_when_no_match(self, seeded_store: CostStore) -> None:
        """Rebinding a placeholder that doesn't exist is a no-op."""
        n = seeded_store.rebind_project_id(from_id="pending:nope", to_id="x")
        assert n == 0

    def test_noop_when_from_equals_to(self, seeded_store: CostStore) -> None:
        """Rebinding to the same id short-circuits."""
        n = seeded_store.rebind_project_id(from_id="same", to_id="same")
        assert n == 0

    def test_rebind_deletes_orphan_name_row(self, seeded_store: CostStore) -> None:
        """Rebinding drops the placeholder's project_names entry.

        Without this, every create_project leaves a dead row indexed by
        'pending:<hex>' that nothing will ever look up (Kaia review on
        PR #515). The real id's row is preserved (or upserted later by
        the caller).
        """
        seeded_store.upsert_project_name("pending:xyz", "myproj (creating)")
        seeded_store.rebind_project_id(from_id="pending:xyz", to_id="real_id")
        assert seeded_store.get_project_name("pending:xyz") is None


class TestProjectBudget:
    """Project-level budget caps stored in the cost DB."""

    def test_set_then_get_returns_value(self, store: CostStore) -> None:
        """Round-trip a budget through the dedicated table."""
        store.set_project_budget("proj_1", 50.0, note="poc cap")
        row = store.get_project_budget("proj_1")
        assert row is not None
        assert row["budget_usd"] == 50.0
        assert row["note"] == "poc cap"
        assert row["set_at"]  # timestamp present

    def test_get_returns_none_when_no_cap(self, store: CostStore) -> None:
        """Projects without a budget row return None."""
        assert store.get_project_budget("never_set") is None

    def test_set_upserts_in_place(self, store: CostStore) -> None:
        """Re-setting the same project updates rather than duplicating."""
        store.set_project_budget("proj_1", 50.0)
        store.set_project_budget("proj_1", 100.0, note="bumped")
        row = store.get_project_budget("proj_1")
        assert row is not None
        assert row["budget_usd"] == 100.0
        assert row["note"] == "bumped"
        count = store.conn.execute(
            "SELECT COUNT(*) FROM project_budgets WHERE project_id='proj_1'"
        ).fetchone()[0]
        assert count == 1

    def test_set_zero_clears_the_cap(self, store: CostStore) -> None:
        """Setting budget to 0 or negative removes the row (no cap)."""
        store.set_project_budget("proj_1", 50.0)
        store.set_project_budget("proj_1", 0)
        assert store.get_project_budget("proj_1") is None


class TestRecordRun:
    """experiments table upsert behavior."""

    def test_records_new_experiment(self, store: CostStore) -> None:
        """A fresh run_id inserts a row."""
        store.record_run(
            Run(
                run_id="exp_1",
                project_id="proj_1",
                started_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
                project_name="hangman",
            )
        )
        row = store.conn.execute("SELECT project_id, project_name FROM runs").fetchone()
        assert row == ("proj_1", "hangman")

    def test_upserts_existing_experiment(self, store: CostStore) -> None:
        """Re-recording the same run_id updates fields, doesn't duplicate."""
        started = datetime(2026, 5, 10, tzinfo=timezone.utc)
        store.record_run(Run(run_id="exp_1", project_id="p", started_at=started))
        store.record_run(
            Run(
                run_id="exp_1",
                project_id="p",
                started_at=started,
                completed_tasks=5,
            )
        )
        rows = store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()
        assert rows[0] == 1
        completed = store.conn.execute(
            "SELECT completed_tasks FROM runs WHERE run_id='exp_1'"
        ).fetchone()[0]
        assert completed == 5


class TestPhase0Wiring:
    """Phase 0 (#546) signal persistence onto ``runs`` / ``token_events``.

    Covers the three write paths added in #6:

    - ``persist_phase0_run_signals`` — open-time + fidelity signals.
    - ``close_run`` deriving did_complete / completion_pct_final /
      total_wall_time_seconds.
    - ``record_event`` persisting was_retry / retry_reason.
    """

    def _open_run(self, store: CostStore, run_id: str = "r1") -> None:
        store.record_run(
            Run(
                run_id=run_id,
                project_id="p1",
                started_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc),
            )
        )

    def test_persist_run_signals_writes_all_columns(self, store: CostStore) -> None:
        """Every open-time / fidelity column round-trips through the DB."""
        self._open_run(store)
        ok = store.persist_phase0_run_signals(
            "r1",
            intent_fidelity_score=0.87,
            coverage_before_fill=0.6,
            coverage_after_fill=0.95,
            prd_length_chars=1234,
            detected_tech_stack="python,react",
            started_at_tz_offset_min=-480,
            is_local_llm=True,
            domain="fintech",
            structural_category="web app",
        )
        assert ok is True
        row = store.conn.execute(
            "SELECT intent_fidelity_score, coverage_before_fill, "
            "coverage_after_fill, prd_length_chars, detected_tech_stack, "
            "started_at_tz_offset_min, is_local_llm, domain, "
            "structural_category FROM runs WHERE run_id='r1'"
        ).fetchone()
        assert row == (
            0.87,
            0.6,
            0.95,
            1234,
            "python,react",
            -480,
            1,  # is_local_llm True -> 1
            "fintech",
            "web app",
        )

    def test_persist_run_signals_coalesce_does_not_clobber(
        self, store: CostStore
    ) -> None:
        """A second partial call leaves earlier columns untouched.

        The open-time write and the later fidelity write both target
        the same row; COALESCE guards mean neither erases the other.
        """
        self._open_run(store)
        store.persist_phase0_run_signals("r1", domain="gaming", prd_length_chars=500)
        store.persist_phase0_run_signals("r1", intent_fidelity_score=0.7)
        row = store.conn.execute(
            "SELECT domain, prd_length_chars, intent_fidelity_score "
            "FROM runs WHERE run_id='r1'"
        ).fetchone()
        assert row == ("gaming", 500, 0.7)

    def test_persist_run_signals_returns_false_for_unknown_run(
        self, store: CostStore
    ) -> None:
        """No matching run_id → False, no row created."""
        assert store.persist_phase0_run_signals("nope", domain="fintech") is False

    def test_close_run_derives_completion_signals(self, store: CostStore) -> None:
        """close_run computes did_complete / pct / wall-time from the row."""
        self._open_run(store)
        store.close_run(
            "r1",
            ended_at=datetime(2026, 5, 10, 13, 0, 0, tzinfo=timezone.utc),
            total_tasks=10,
            completed_tasks=10,
        )
        row = store.conn.execute(
            "SELECT did_complete, completion_pct_final, "
            "total_wall_time_seconds FROM runs WHERE run_id='r1'"
        ).fetchone()
        # 10/10 done; one hour wall time.
        assert row[0] == 1
        assert row[1] == 100.0
        assert row[2] == pytest.approx(3600.0)

    def test_close_run_partial_completion(self, store: CostStore) -> None:
        """A run that finished only some tasks gets did_complete=0."""
        self._open_run(store)
        store.close_run(
            "r1",
            ended_at=datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc),
            total_tasks=8,
            completed_tasks=2,
        )
        row = store.conn.execute(
            "SELECT did_complete, completion_pct_final FROM runs " "WHERE run_id='r1'"
        ).fetchone()
        assert row[0] == 0
        assert row[1] == 25.0

    def test_close_run_zero_tasks_leaves_pct_null(self, store: CostStore) -> None:
        """completion_pct is NULL (not 0) when there were no tasks.

        Percentage of nothing is undefined — NULL is the honest value
        a forecasting model should see, not a misleading 0%.
        """
        self._open_run(store)
        store.close_run(
            "r1",
            ended_at=datetime(2026, 5, 10, 12, 5, 0, tzinfo=timezone.utc),
            total_tasks=0,
            completed_tasks=0,
        )
        row = store.conn.execute(
            "SELECT did_complete, completion_pct_final FROM runs " "WHERE run_id='r1'"
        ).fetchone()
        assert row[0] == 0
        assert row[1] is None

    def test_close_run_unknown_task_count_leaves_did_complete_null(
        self, store: CostStore
    ) -> None:
        """did_complete is NULL (not 0) when total_tasks was never recorded.

        A run closed without task counts carries no completion signal.
        NULL says "we do not know"; 0 would falsely tell a forecasting
        model the run failed.  Distinct from the zero-task case
        (total_tasks = 0 → did_complete 0).

        Falsification recipe: drop the ``WHEN total_tasks IS NULL THEN
        NULL`` arm of the CASE in ``_derive_phase0_close_signals``.
        Confirm this test fails because did_complete reads 0.
        """
        self._open_run(store)
        # Close with only a timestamp — no total_tasks / completed_tasks.
        store.close_run(
            "r1",
            ended_at=datetime(2026, 5, 10, 12, 20, 0, tzinfo=timezone.utc),
        )
        row = store.conn.execute(
            "SELECT did_complete, completion_pct_final FROM runs " "WHERE run_id='r1'"
        ).fetchone()
        assert row[0] is None
        assert row[1] is None

    def test_record_event_persists_retry_provenance(
        self, seeded_store: CostStore
    ) -> None:
        """was_retry / retry_reason round-trip through token_events."""
        seeded_store.record_event(
            TokenEvent(
                run_id="r1",
                project_id="p1",
                agent_id="planner",
                agent_role="planner",
                operation="parse_prd",
                provider="anthropic",
                model="claude-sonnet-4-6",
                request_id="req-retry",
                was_retry=True,
                retry_reason="truncation",
            )
        )
        row = seeded_store.conn.execute(
            "SELECT was_retry, retry_reason FROM token_events "
            "WHERE request_id='req-retry'"
        ).fetchone()
        assert row == (1, "truncation")

    def test_record_event_first_attempt_has_null_retry(
        self, seeded_store: CostStore
    ) -> None:
        """A first-attempt call leaves was_retry NULL, not 0.

        NULL distinguishes 'first try' from 'a retry we recorded' with
        no backfill needed.
        """
        seeded_store.record_event(
            TokenEvent(
                run_id="r1",
                project_id="p1",
                agent_id="planner",
                agent_role="planner",
                operation="parse_prd",
                provider="anthropic",
                model="claude-sonnet-4-6",
                request_id="req-first",
            )
        )
        row = seeded_store.conn.execute(
            "SELECT was_retry, retry_reason FROM token_events "
            "WHERE request_id='req-first'"
        ).fetchone()
        assert row == (None, None)


class TestRecordPrice:
    """model_prices versioning."""

    def test_records_price(self, store: CostStore) -> None:
        """A single price row inserts cleanly."""
        store.record_price(
            ModelPrice(
                model="m",
                provider="p",
                effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                input_per_million=1.0,
                output_per_million=2.0,
                source="default",
            )
        )
        row = store.conn.execute("SELECT COUNT(*) FROM model_prices").fetchone()
        assert row[0] == 1

    def test_versioned_prices_coexist(self, store: CostStore) -> None:
        """Two prices for the same (model, provider) at different effective_from coexist."""
        for ef in (
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        ):
            store.record_price(
                ModelPrice(
                    model="m",
                    provider="p",
                    effective_from=ef,
                    input_per_million=1.0,
                    output_per_million=2.0,
                    source="default",
                )
            )
        count = store.conn.execute("SELECT COUNT(*) FROM model_prices").fetchone()[0]
        assert count == 2

    def test_duplicate_effective_from_rejected(self, store: CostStore) -> None:
        """Same (model, provider, effective_from) twice raises IntegrityError."""
        ef = datetime(2025, 1, 1, tzinfo=timezone.utc)
        price = ModelPrice(
            model="m",
            provider="p",
            effective_from=ef,
            input_per_million=1.0,
            output_per_million=2.0,
            source="default",
        )
        store.record_price(price)
        with pytest.raises(sqlite3.IntegrityError):
            store.record_price(price)


class TestEventCostView:
    """v_event_cost computes cost using the price active at event timestamp."""

    def test_cost_uses_active_price(
        self, seeded_store: CostStore, base_event: TokenEvent
    ) -> None:
        """1000 in + 2000 cache_create + 4000 cache_read + 500 out at default price."""
        seeded_store.record_event(base_event)
        cost = seeded_store.conn.execute(
            "SELECT cost_usd FROM v_event_cost"
        ).fetchone()[0]
        # 1000*3/1e6 + 2000*3.75/1e6 + 4000*0.30/1e6 + 500*15/1e6
        # = 0.003 + 0.0075 + 0.0012 + 0.0075 = 0.0192
        assert cost == pytest.approx(0.0192, rel=1e-6)

    def test_cost_uses_historical_price_when_event_predates_new_one(
        self, store: CostStore
    ) -> None:
        """Event at t=2025-06 uses 2025-01 price even if 2026-01 price exists."""
        old = datetime(2025, 1, 1, tzinfo=timezone.utc)
        new = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store.record_price(
            ModelPrice(
                model="m",
                provider="p",
                effective_from=old,
                input_per_million=1.0,
                output_per_million=1.0,
                source="default",
            )
        )
        store.record_price(
            ModelPrice(
                model="m",
                provider="p",
                effective_from=new,
                input_per_million=10.0,
                output_per_million=10.0,
                source="default",
            )
        )
        # Event in mid-2025 should use old price
        store.record_event(
            TokenEvent(
                run_id="e",
                project_id="proj",
                agent_id="a",
                agent_role="planner",
                operation="op",
                provider="p",
                model="m",
                input_tokens=1_000_000,
                output_tokens=0,
                timestamp=datetime(2025, 6, 1, tzinfo=timezone.utc),
            )
        )
        cost = store.conn.execute("SELECT cost_usd FROM v_event_cost").fetchone()[0]
        assert cost == pytest.approx(1.0, rel=1e-6)


class TestCodexP1LegacyModelSeeds:
    """Regression: out-of-box Anthropic models must seed (Codex P1 on PR #497).

    Marcus's default config uses ``claude-3-haiku-20240307`` and the historic
    settings default ``claude-3-sonnet-20241022``. Without seed rows, the
    inner join in ``v_event_cost`` drops every event for those models from
    aggregations.
    """

    def test_default_anthropic_model_has_seed_price(self, store: CostStore) -> None:
        """The model anthropic_provider.py defaults to is in DEFAULT_SEED."""
        store.load_seed_prices()
        models = {
            row[0]
            for row in store.conn.execute(
                "SELECT model FROM model_prices WHERE provider='anthropic'"
            )
        }
        assert "claude-3-haiku-20240307" in models
        # Sonnet 3.7 is the actual deprecated Sonnet 3 release on
        # Anthropic's pricing page; claude-3-sonnet-20241022 was a name
        # I made up in the original seed and dropped during the
        # 2026-05-11 pricing refresh.
        assert "claude-3-7-sonnet-20250219" in models

    def test_event_for_legacy_model_appears_in_cost_view(
        self, store: CostStore
    ) -> None:
        """Event for claude-3-haiku-20240307 produces a v_event_cost row."""
        store.load_seed_prices()
        store.record_event(
            TokenEvent(
                run_id="e",
                project_id="p",
                agent_id="planner",
                agent_role="planner",
                operation="parse_prd",
                provider="anthropic",
                model="claude-3-haiku-20240307",
                input_tokens=1_000_000,
                output_tokens=0,
            )
        )
        cost = store.conn.execute("SELECT cost_usd FROM v_event_cost").fetchone()
        assert cost is not None  # would be None if INNER JOIN dropped the event
        assert cost[0] == pytest.approx(0.25, rel=1e-6)


class TestCodexP2TimestampFormat:
    """Regression: same-day price must apply to events lacking explicit ts.

    Codex P2 on PR #497: SQLite's bare CURRENT_TIMESTAMP defaults to
    ``YYYY-MM-DD HH:MM:SS`` (space separator) while ``record_price``
    stores ISO format with a ``T`` separator. Lexical comparison in
    ``v_event_cost`` would silently drop events whose default timestamp
    sorts before any price row for the same model.
    """

    def test_event_with_default_timestamp_joins_iso_priced_row(
        self, store: CostStore
    ) -> None:
        """Insert price first, then event without a timestamp; v_event_cost row exists."""
        store.record_price(
            ModelPrice(
                model="m",
                provider="p",
                effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                input_per_million=1.0,
                output_per_million=1.0,
                source="default",
            )
        )
        store.record_event(
            TokenEvent(
                run_id="e",
                project_id="p",
                agent_id="planner",
                agent_role="planner",
                operation="op",
                provider="p",
                model="m",
                input_tokens=1_000_000,
                output_tokens=0,
                # timestamp deliberately omitted — uses SQLite default
            )
        )
        row = store.conn.execute("SELECT cost_usd FROM v_event_cost").fetchone()
        assert row is not None, "default timestamp format must allow join"
        assert row[0] == pytest.approx(1.0, rel=1e-6)

    def test_default_timestamp_uses_iso_t_separator(self, store: CostStore) -> None:
        """Stored default timestamp has the 'T' separator, not a space."""
        store.load_seed_prices()
        store.record_event(
            TokenEvent(
                run_id="e",
                project_id="p",
                agent_id="planner",
                agent_role="planner",
                operation="op",
                provider="anthropic",
                model="claude-3-haiku-20240307",
                input_tokens=1,
                output_tokens=1,
            )
        )
        ts = store.conn.execute("SELECT timestamp FROM token_events").fetchone()[0]
        assert (
            "T" in ts and " " not in ts
        ), f"expected ISO format with T separator, got {ts!r}"


class TestSeedLoader:
    """load_seed_prices idempotency."""

    def test_load_seed_inserts_default_prices(self, store: CostStore) -> None:
        """Calling load_seed_prices populates the default Anthropic / OpenAI rows."""
        store.load_seed_prices()
        models = {
            row[0]
            for row in store.conn.execute(
                "SELECT model FROM model_prices WHERE source='default'"
            )
        }
        # At least the headline Anthropic + OpenAI defaults should be there
        assert "claude-sonnet-4-6" in models
        assert "claude-haiku-4-5" in models

    def test_load_seed_idempotent(self, store: CostStore) -> None:
        """Running load_seed_prices twice doesn't duplicate rows."""
        store.load_seed_prices()
        first_count = store.conn.execute(
            "SELECT COUNT(*) FROM model_prices"
        ).fetchone()[0]
        store.load_seed_prices()
        second_count = store.conn.execute(
            "SELECT COUNT(*) FROM model_prices"
        ).fetchone()[0]
        assert first_count == second_count


class TestTaskNamesSnapshot:
    """``task_names`` table mirrors the project_names snapshot pattern (#530)."""

    def test_record_and_retrieve_task_name(self, tmp_path: Path) -> None:
        """Round-trip a single task name."""
        s = CostStore(db_path=tmp_path / "costs.db")
        s.record_task_name("abc123", "Implement scoring logic")
        assert s.get_task_name("abc123") == "Implement scoring logic"

    def test_upsert_on_conflict(self, tmp_path: Path) -> None:
        """A name change updates in place."""
        s = CostStore(db_path=tmp_path / "costs.db")
        s.record_task_name("abc123", "Old name")
        s.record_task_name("abc123", "New name")
        assert s.get_task_name("abc123") == "New name"

    def test_empty_inputs_are_no_op(self, tmp_path: Path) -> None:
        """Empty task_id or name silently does nothing — doesn't crash."""
        s = CostStore(db_path=tmp_path / "costs.db")
        s.record_task_name("", "name")
        s.record_task_name("abc", "")
        assert s.get_task_name("abc") is None

    def test_missing_task_returns_none(self, tmp_path: Path) -> None:
        """get_task_name on an unrecorded id returns None, not an error."""
        s = CostStore(db_path=tmp_path / "costs.db")
        assert s.get_task_name("never_recorded") is None


class TestCloseRun:
    """``close_run`` stamps lifecycle fields on a previously-open row (#537)."""

    def test_explicit_ended_at_writes_to_row(self, store: CostStore) -> None:
        """Pass ended_at directly; UPDATE writes it to the row."""
        store.record_run(
            Run(
                run_id="r1",
                project_id="p1",
                project_name="x",
                started_at=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
            )
        )
        end = datetime(2026, 5, 13, 10, 30, tzinfo=timezone.utc)
        ok = store.close_run(
            "r1",
            ended_at=end,
            total_tasks=10,
            completed_tasks=8,
            blocked_tasks=1,
            num_agents=3,
        )
        assert ok is True
        row = store.conn.execute(
            "SELECT ended_at, total_tasks, completed_tasks, blocked_tasks, "
            "num_agents FROM runs WHERE run_id = 'r1'"
        ).fetchone()
        assert row[0] == end.isoformat()
        assert row[1:] == (10, 8, 1, 3)

    def test_ended_at_falls_back_to_last_event_timestamp(
        self, store: CostStore
    ) -> None:
        """No ended_at supplied → use MAX(token_events.timestamp)."""
        store.record_run(
            Run(
                run_id="r1",
                project_id="p1",
                project_name="x",
                started_at=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
            )
        )
        store.record_price(
            ModelPrice(
                model="claude-sonnet-4-6",
                provider="anthropic",
                effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                input_per_million=3.0,
                output_per_million=15.0,
                source="test",
            )
        )
        last_ts = datetime(2026, 5, 13, 10, 25, tzinfo=timezone.utc)
        store.record_event(
            TokenEvent(
                run_id="r1",
                project_id="p1",
                agent_id="a1",
                agent_role="worker",
                operation="turn",
                provider="anthropic",
                model="claude-sonnet-4-6",
                request_id="req_1",
                timestamp=last_ts,
                input_tokens=100,
            )
        )
        ok = store.close_run("r1")
        assert ok is True
        row = store.conn.execute(
            "SELECT ended_at FROM runs WHERE run_id = 'r1'"
        ).fetchone()
        assert row[0] == last_ts.isoformat()

    def test_returns_false_when_no_events_and_no_ended_at(
        self, store: CostStore
    ) -> None:
        """Run with no events + no explicit ended_at: leave open, return False."""
        store.record_run(
            Run(
                run_id="empty",
                project_id="p1",
                project_name="x",
                started_at=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
            )
        )
        ok = store.close_run("empty")
        assert ok is False
        row = store.conn.execute(
            "SELECT ended_at FROM runs WHERE run_id = 'empty'"
        ).fetchone()
        assert row[0] is None

    def test_returns_false_when_run_id_unknown(self, store: CostStore) -> None:
        """Unknown run_id returns False, no error."""
        ok = store.close_run(
            "nope",
            ended_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
        )
        assert ok is False

    def test_none_fields_preserve_existing_values(self, store: CostStore) -> None:
        """COALESCE-guarded UPDATE: None args don't overwrite existing data."""
        store.record_run(
            Run(
                run_id="r1",
                project_id="p1",
                project_name="x",
                started_at=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
                total_tasks=15,  # pre-set
            )
        )
        store.close_run(
            "r1",
            ended_at=datetime(2026, 5, 13, 11, tzinfo=timezone.utc),
            # total_tasks deliberately not passed
        )
        row = store.conn.execute(
            "SELECT total_tasks FROM runs WHERE run_id = 'r1'"
        ).fetchone()
        assert row[0] == 15  # original value preserved


class TestCloseLatestOpenRunForProject:
    """``close_latest_open_run_for_project`` is the live-close helper (#537)."""

    def test_closes_only_latest_open_run(self, store: CostStore) -> None:
        """Older open run is preserved; only MAX(started_at) is stamped.

        Regression for Codex P2 on PR #538: the schema allows multiple
        open runs per project (retries, repeated traversals). A bulk
        close would stamp historical rows with the just-finished
        experiment's ended_at + counters, corrupting them. The live
        hook must scope to one row.
        """
        store.record_run(
            Run(
                run_id="r_old",
                project_id="proj_x",
                project_name="x",
                started_at=datetime(2026, 5, 13, 10, tzinfo=timezone.utc),
            )
        )
        store.record_run(
            Run(
                run_id="r_new",
                project_id="proj_x",
                project_name="x",
                started_at=datetime(2026, 5, 13, 11, tzinfo=timezone.utc),
            )
        )
        # Different project — must NOT be touched.
        store.record_run(
            Run(
                run_id="r_other",
                project_id="other",
                project_name="o",
                started_at=datetime(2026, 5, 13, 10, tzinfo=timezone.utc),
            )
        )

        end = datetime(2026, 5, 13, 12, tzinfo=timezone.utc)
        closed_id = store.close_latest_open_run_for_project(
            "proj_x", ended_at=end, total_tasks=8
        )
        assert closed_id == "r_new"

        rows = {
            run_id: (ended_at, total_tasks)
            for run_id, ended_at, total_tasks in store.conn.execute(
                "SELECT run_id, ended_at, total_tasks FROM runs ORDER BY run_id"
            )
        }
        # Latest stamped with the experiment's counters.
        assert rows["r_new"] == (end.isoformat(), 8)
        # Older row left untouched for the periodic backfill.
        assert rows["r_old"] == (None, None)
        # Different project untouched.
        assert rows["r_other"] == (None, None)

    def test_returns_none_when_no_open_runs(self, store: CostStore) -> None:
        """Empty / fully-closed project returns None — caller treats as no-op."""
        end = datetime(2026, 5, 13, 12, tzinfo=timezone.utc)
        # Project never recorded.
        assert store.close_latest_open_run_for_project("ghost", ended_at=end) is None

        # Project with a row that's already closed.
        store.record_run(
            Run(
                run_id="r_done",
                project_id="proj_y",
                project_name="y",
                started_at=datetime(2026, 5, 13, 10, tzinfo=timezone.utc),
                ended_at=datetime(2026, 5, 13, 11, tzinfo=timezone.utc),
            )
        )
        assert store.close_latest_open_run_for_project("proj_y", ended_at=end) is None

    def test_dashed_project_id_canonicalizes_to_dashless(
        self, store: CostStore
    ) -> None:
        """Dashed UUID from caller matches dashless row in ``runs``.

        Regression for the silent no-op bug Kaia caught on PR #538:
        ``record_run`` stores dashless via ``canonical_project_id``,
        but ``end_experiment`` passes ``monitor.project_id`` straight
        through. Without canonicalization here, the dashed form never
        matches the dashless row and the live close silently closes
        zero runs.
        """
        dashless = "4cca814b47024489a914a9868da9e4fc"
        dashed = "4cca814b-4702-4489-a914-a9868da9e4fc"

        store.record_run(
            Run(
                run_id="r_canon",
                project_id=dashless,
                project_name="canon",
                started_at=datetime(2026, 5, 13, 10, tzinfo=timezone.utc),
            )
        )

        end = datetime(2026, 5, 13, 12, tzinfo=timezone.utc)
        # Caller hands in the dashed form — must still close the row.
        closed_id = store.close_latest_open_run_for_project(dashed, ended_at=end)
        assert closed_id == "r_canon"

        row = store.conn.execute(
            "SELECT ended_at FROM runs WHERE run_id = ?", ("r_canon",)
        ).fetchone()
        assert row[0] == end.isoformat()


class TestGetLatestOpenRunForProject:
    """``get_latest_open_run_for_project`` resolves the setup run (#768).

    Run-time planner LLM calls (instruction generation, blocker
    analysis, work validation) must attribute their ``token_events`` to
    the same ``run`` that ``create_project`` opened at setup time. That
    run is the most recently started still-open row in ``runs`` for the
    project. This read-only lookup is how the run-time call sites find
    it.
    """

    def test_returns_latest_open_run(self, store: CostStore) -> None:
        """Among several open runs, the MAX(started_at) run_id is returned."""
        store.record_run(
            Run(
                run_id="r_old",
                project_id="proj_x",
                project_name="x",
                started_at=datetime(2026, 5, 13, 10, tzinfo=timezone.utc),
            )
        )
        store.record_run(
            Run(
                run_id="r_new",
                project_id="proj_x",
                project_name="x",
                started_at=datetime(2026, 5, 13, 11, tzinfo=timezone.utc),
            )
        )
        # Different project — must not be returned.
        store.record_run(
            Run(
                run_id="r_other",
                project_id="other",
                project_name="o",
                started_at=datetime(2026, 5, 13, 12, tzinfo=timezone.utc),
            )
        )

        assert store.get_latest_open_run_for_project("proj_x") == "r_new"

    def test_returns_none_when_no_open_runs(self, store: CostStore) -> None:
        """Never-recorded or fully-closed project returns None."""
        # Never recorded.
        assert store.get_latest_open_run_for_project("ghost") is None

        # Recorded but already closed — not an *open* run.
        store.record_run(
            Run(
                run_id="r_done",
                project_id="proj_y",
                project_name="y",
                started_at=datetime(2026, 5, 13, 10, tzinfo=timezone.utc),
                ended_at=datetime(2026, 5, 13, 11, tzinfo=timezone.utc),
            )
        )
        assert store.get_latest_open_run_for_project("proj_y") is None

    def test_dashed_project_id_canonicalizes_to_dashless(
        self, store: CostStore
    ) -> None:
        """A dashed UUID from the caller still matches the dashless row.

        ``record_run`` stores the dashless form via
        ``canonical_project_id``. ProjectRegistry hands out the dashed
        form, so without canonicalization here the lookup would silently
        miss and the run-time calls would fall back to 'unassigned'.
        """
        dashless = "4cca814b47024489a914a9868da9e4fc"
        dashed = "4cca814b-4702-4489-a914-a9868da9e4fc"
        store.record_run(
            Run(
                run_id="r_canon",
                project_id=dashless,
                project_name="canon",
                started_at=datetime(2026, 5, 13, 10, tzinfo=timezone.utc),
            )
        )

        assert store.get_latest_open_run_for_project(dashed) == "r_canon"


class TestCloseCurrentRunForProject:
    """``close_current_run_for_project`` closes the newest run and nothing older.

    The drain signal may fire once per worker. The first call must close
    the project's current run; later calls must be no-ops even when older
    runs for the project were never closed, instead of walking backward
    and stamping history with today's counts (Codex P2 on PR #782).
    """

    def _seed(self, store: CostStore) -> None:
        store.record_run(
            Run(
                run_id="r_old",
                project_id="proj_x",
                project_name="x",
                started_at=datetime(2026, 5, 13, 10, tzinfo=timezone.utc),
            )
        )
        store.record_run(
            Run(
                run_id="r_new",
                project_id="proj_x",
                project_name="x",
                started_at=datetime(2026, 5, 13, 11, tzinfo=timezone.utc),
            )
        )

    def _rows(self, store: CostStore) -> dict:
        return {
            run_id: (ended_at, total_tasks)
            for run_id, ended_at, total_tasks in store.conn.execute(
                "SELECT run_id, ended_at, total_tasks FROM runs ORDER BY run_id"
            )
        }

    def test_first_call_closes_the_newest_run(self, store: CostStore) -> None:
        """The current run is stamped; the older open run is left alone."""
        self._seed(store)
        end = datetime(2026, 5, 13, 12, tzinfo=timezone.utc)

        assert (
            store.close_current_run_for_project("proj_x", ended_at=end, total_tasks=8)
            == "r_new"
        )
        rows = self._rows(store)
        assert rows["r_new"] == (end.isoformat(), 8)
        assert rows["r_old"] == (None, None)

    def test_second_call_is_a_no_op_even_with_older_open_runs(
        self, store: CostStore
    ) -> None:
        """Once the current run is closed, repeated calls touch nothing."""
        self._seed(store)
        end = datetime(2026, 5, 13, 12, tzinfo=timezone.utc)
        store.close_current_run_for_project("proj_x", ended_at=end, total_tasks=8)

        later = datetime(2026, 5, 13, 13, tzinfo=timezone.utc)
        assert (
            store.close_current_run_for_project(
                "proj_x", ended_at=later, total_tasks=99
            )
            is None
        )
        rows = self._rows(store)
        assert rows["r_new"] == (end.isoformat(), 8)
        assert rows["r_old"] == (None, None)

    def test_unknown_project_returns_none(self, store: CostStore) -> None:
        """A project with no runs is a no-op."""
        end = datetime(2026, 5, 13, 12, tzinfo=timezone.utc)
        assert store.close_current_run_for_project("ghost", ended_at=end) is None

    def test_normalizes_dashed_project_ids(self, store: CostStore) -> None:
        """Dashed and dashless forms of the same project id resolve alike."""
        dashless = "0123456789abcdef0123456789abcdef"
        dashed = "01234567-89ab-cdef-0123-456789abcdef"
        store.record_run(
            Run(
                run_id="r_canon",
                project_id=dashless,
                project_name="c",
                started_at=datetime(2026, 5, 13, 10, tzinfo=timezone.utc),
            )
        )
        end = datetime(2026, 5, 13, 12, tzinfo=timezone.utc)
        assert store.close_current_run_for_project(dashed, ended_at=end) == "r_canon"
