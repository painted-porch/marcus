"""
Unit tests for ``query(where=..., offset=...)`` on both persistence backends.

``SQLitePersistence.query`` used to apply ``LIMIT limit * 2`` in SQL and
only then run ``filter_func`` in Python, so a match sitting behind more
than ``2 * limit`` newer non-matching rows was never returned and the
caller saw an empty page while rows still existed (Codex P2 on PR #800).
The query now prefilters JSON fields in SQL (``where``), streams rows
through ``filter_func`` without a premature limit, and honours a real
``offset``.  ``FilePersistence`` gets the same contract.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.persistence import FilePersistence, Persistence, SQLitePersistence

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def _insert_sqlite(db_path: Path, rows: list[tuple[str, dict[str, Any], int]]) -> None:
    """Insert rows with explicit stored_at minutes so ordering is deterministic."""
    with sqlite3.connect(db_path) as conn:
        for key, data, minute in rows:
            conn.execute(
                "INSERT INTO persistence (collection, key, data, stored_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    "c",
                    key,
                    json.dumps(data),
                    (T0 + timedelta(minutes=minute)).isoformat(),
                ),
            )
        conn.commit()


@pytest.fixture
def sqlite_backend(tmp_path: Path) -> SQLitePersistence:
    """SQLite backend on a temporary database."""
    return SQLitePersistence(db_path=tmp_path / "t.db")


@pytest.fixture
def file_backend(tmp_path: Path) -> FilePersistence:
    """File backend in a temporary directory."""
    return FilePersistence(storage_dir=tmp_path / "files")


class TestSQLiteQueryPrefilter:
    """Test suite for the SQLite backend."""

    @pytest.mark.asyncio
    async def test_match_behind_many_newer_rows_is_found(
        self, sqlite_backend: SQLitePersistence
    ) -> None:
        """One old match behind 25 newer non-matches, limit 5: still found."""
        rows = [("target", {"project_id": "p1"}, 0)]
        rows += [(f"foreign{i}", {"project_id": "other"}, i + 1) for i in range(25)]
        _insert_sqlite(sqlite_backend.db_path, rows)

        items = await sqlite_backend.query(
            "c", filter_func=lambda item: item.get("project_id") == "p1", limit=5
        )

        assert [item["_key"] for item in items] == ["target"]

    @pytest.mark.asyncio
    async def test_where_equality_prefilters_rows(
        self, sqlite_backend: SQLitePersistence
    ) -> None:
        """``where`` keeps only rows whose JSON field equals the value."""
        _insert_sqlite(
            sqlite_backend.db_path,
            [("a1", {"project_id": "a"}, 0), ("b1", {"project_id": "b"}, 1)],
        )

        items = await sqlite_backend.query("c", where={"project_id": "a"})

        assert [item["_key"] for item in items] == ["a1"]

    @pytest.mark.asyncio
    async def test_where_list_with_none_matches_null_and_missing(
        self, sqlite_backend: SQLitePersistence
    ) -> None:
        """A list of values is IN, and None matches a null or absent field."""
        _insert_sqlite(
            sqlite_backend.db_path,
            [
                ("a1", {"project_id": "a"}, 0),
                ("b1", {"project_id": "b"}, 1),
                ("n1", {"project_id": None}, 2),
                ("m1", {}, 3),
            ],
        )

        items = await sqlite_backend.query("c", where={"project_id": ["a", None]})

        assert sorted(item["_key"] for item in items) == ["a1", "m1", "n1"]

    @pytest.mark.asyncio
    async def test_offset_skips_matching_rows_newest_first(
        self, sqlite_backend: SQLitePersistence
    ) -> None:
        """offset counts matches, newest first, both with and without a filter."""
        _insert_sqlite(
            sqlite_backend.db_path,
            [(f"k{i}", {"n": i, "keep": i % 2 == 0}, i) for i in range(8)],
        )

        plain = await sqlite_backend.query("c", limit=2, offset=2)
        filtered = await sqlite_backend.query(
            "c", filter_func=lambda item: item["keep"], limit=2, offset=1
        )

        assert [item["n"] for item in plain] == [5, 4]
        assert [item["n"] for item in filtered] == [4, 2]

    @pytest.mark.asyncio
    async def test_items_carry_stored_at(
        self, sqlite_backend: SQLitePersistence
    ) -> None:
        """Rows report when they were stored, like the file backend does."""
        _insert_sqlite(sqlite_backend.db_path, [("k", {"n": 1}, 0)])

        [item] = await sqlite_backend.query("c")

        assert item["_stored_at"] == T0.isoformat()

    @pytest.mark.asyncio
    async def test_where_field_name_is_validated(
        self, sqlite_backend: SQLitePersistence
    ) -> None:
        """A field name is an identifier, never raw SQL."""
        with pytest.raises(ValueError):
            await sqlite_backend.query("c", where={"x') OR 1=1 --": "a"})


class TestFileQueryPrefilter:
    """Test suite for the file backend's matching contract."""

    @pytest.mark.asyncio
    async def test_where_list_with_none_and_offset(
        self, file_backend: FilePersistence
    ) -> None:
        """Same where semantics and offset as SQLite."""
        await file_backend.store("c", "a1", {"project_id": "a", "n": 1})
        await file_backend.store("c", "b1", {"project_id": "b", "n": 2})
        await file_backend.store("c", "n1", {"project_id": None, "n": 3})
        await file_backend.store("c", "m1", {"n": 4})

        kept = await file_backend.query("c", where={"project_id": ["a", None]})
        paged = await file_backend.query("c", limit=2, offset=1)

        assert sorted(item["_key"] for item in kept) == ["a1", "m1", "n1"]
        assert len(paged) == 2


class TestPersistenceWrapperPassesThrough:
    """Test suite for the ``Persistence`` facade."""

    @pytest.mark.asyncio
    async def test_query_forwards_where_and_offset(self) -> None:
        """The facade forwards the new arguments to the backend."""
        backend = Mock()
        backend.query = AsyncMock(return_value=[])
        persistence = Persistence(backend=backend)

        await persistence.query("c", None, 7, where={"k": "v"}, offset=3)

        backend.query.assert_awaited_once_with("c", None, 7, where={"k": "v"}, offset=3)
