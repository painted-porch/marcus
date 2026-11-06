"""Unit tests for the decisions-kind query plan (issue #890).

The ``decisions`` collection shares one SQLite table with every opinion
on the board. Finding the newest N rows of one ``kind`` (e.g.
``"decision"``) filters on two ``json_extract`` expressions
(``project_id``, ``kind``) that had no index at all -- only
``stored_at`` was indexed, so a kind-filtered read on a board whose
newest rows are mostly a different kind walks almost the whole
project's history on every pull (#880 made this a per-cycle read).

These tests assert the query planner actually uses an index for the
query shape ``_load_project_rows``/``load_decisions`` issue (collection
+ project_id + kind, ordered by stored_at), and that correctness is
unchanged for the one case that makes a plain equality index
insufficient: rows written before ``kind`` existed, which have no
``kind`` field at all and must still match ``kind="decision"``.
"""

import sqlite3
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from src.core.persistence import SQLitePersistence

pytestmark = pytest.mark.unit


@pytest.fixture
def sqlite_backend(tmp_path: Path) -> SQLitePersistence:
    return SQLitePersistence(db_path=tmp_path / "t.db")


def test_schema_creates_an_index_covering_project_id_and_kind(
    sqlite_backend: SQLitePersistence,
) -> None:
    """A fresh (and, by IF NOT EXISTS, an existing) database must gain
    the new index on open -- no hand-run migration step."""
    with sqlite3.connect(sqlite_backend.db_path) as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    assert "idx_persistence_project_kind" in indexes


def _explain(
    sqlite_backend: SQLitePersistence, where: Dict[str, Any], params: Tuple[Any, ...]
) -> str:
    """Build the REAL WHERE clause through ``_where_sql`` (so this test
    can never silently drift from the production query) and return the
    flattened ``EXPLAIN QUERY PLAN`` text for it.
    """
    where_sql, where_params = sqlite_backend._where_sql(where)
    sql = (
        "SELECT key, data, stored_at FROM persistence WHERE collection = ?"
        f"{where_sql} ORDER BY stored_at DESC, key DESC LIMIT ? OFFSET ?"
    )
    with sqlite3.connect(sqlite_backend.db_path) as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN " + sql,
            (*params, *where_params, 10, 0),
        ).fetchall()
    return " ".join(str(cell) for row in plan for cell in row)


def test_query_plan_uses_the_new_index_for_project_and_kind_filter(
    sqlite_backend: SQLitePersistence,
) -> None:
    """EXPLAIN QUERY PLAN for the REAL query ``_load_project_rows``
    issues (collection = ?, project_id filter, kind filter, ORDER BY
    stored_at DESC) must show the seek constraining all three columns
    and no temp b-tree sort.

    Issue #890 round 2 (Codex, confirmed live): a first version of
    this index was used for collection and project_id only, with kind
    left unconstrained and a temp b-tree still needed for the ORDER
    BY -- exactly the scan this index exists to remove -- while a
    weaker version of this same test passed because it only checked
    the index's NAME appeared in the plan, not what it actually
    constrained. This asserts the constrained-column count and the
    absence of a sort, not the index's name, so a future change that
    reintroduces either regression fails here again.
    """
    plan_text = _explain(
        sqlite_backend,
        {"project_id": "proj-1", "kind": ["decision", None]},
        ("decisions",),
    )
    assert plan_text.count("=?") == 3, plan_text
    assert "B-TREE" not in plan_text, plan_text

    # The non-legacy kind path (e.g. "assessment") must get the same
    # equality seek, not just the "decision" + None shape.
    plan_text_assessment = _explain(
        sqlite_backend,
        {"project_id": "proj-1", "kind": ["assessment"]},
        ("decisions",),
    )
    assert plan_text_assessment.count("=?") == 3, plan_text_assessment
    assert "B-TREE" not in plan_text_assessment, plan_text_assessment


@pytest.mark.asyncio
async def test_kind_filter_still_matches_rows_with_no_kind_field(
    sqlite_backend: SQLitePersistence,
) -> None:
    """Decision.from_dict loads a row with no ``kind`` field at all as
    a decision (rows written before #880 added the field). The index
    must serve the ``IN (...) OR ... IS NULL`` clause this relies on,
    not just an equality lookup."""
    await sqlite_backend.store(
        "decisions", "legacy-row", {"what": "pre-kind row", "project_id": "proj-1"}
    )
    await sqlite_backend.store(
        "decisions",
        "assessment-row",
        {"what": "an opinion", "project_id": "proj-1", "kind": "assessment"},
    )
    await sqlite_backend.store(
        "decisions",
        "decision-row",
        {"what": "a real decision", "project_id": "proj-1", "kind": "decision"},
    )

    results = await sqlite_backend.query(
        "decisions",
        where={"project_id": "proj-1", "kind": ["decision", None]},
        limit=10,
    )

    keys = {r["_key"] for r in results}
    assert keys == {"legacy-row", "decision-row"}
