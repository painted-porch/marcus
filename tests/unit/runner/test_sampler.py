"""Unit tests for marcus_runner.sampler."""

import sqlite3

import pytest
from marcus_runner import sampler

pytestmark = pytest.mark.unit


@pytest.fixture
def board(tmp_path):
    """A board with one task in every state the rule distinguishes."""
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.execute(
        "create table tasks"
        " (id text, name text, status text, assigned_to text, project_id text)"
    )
    con.execute("create table task_dependencies (task_id text, depends_on_id text)")
    rows = [
        ("t0", "About: war", "done", None, "p"),
        ("t1", "logs", "done", None, "p"),
        ("t2", "walls", "blocked", None, "p"),
        ("t3", "food", "declined", None, "p"),
        ("t4", "survey", "in_progress", "Ash", "p"),
        ("t5", "shelter", "todo", "Bly", "p"),
        ("t6", "north wall", "todo", None, "p"),
        ("t7", "cobble", "todo", None, "p"),
        ("t8", "other project", "todo", None, "q"),
    ]
    con.executemany("insert into tasks values (?,?,?,?,?)", rows)
    con.execute("insert into task_dependencies values ('t6', 't2')")
    con.commit()
    con.close()
    return db


def test_sample_applies_the_drain_verdicts_rule(board):
    """Done; blocked or declined; in flight; unclaimable; claimable; no About card."""
    s = sampler.sample(board, "p")
    assert s["tasks_total"] == 7
    assert s["tasks_done"] == 1
    assert s["tasks_blocked"] == 2
    assert s["tasks_in_flight"] == 2
    assert s["tasks_unclaimable"] == 1
    assert s["tasks_claimable"] == 1
    assert s["claimable"] == ["cobble"]
    assert s["holders"] == {"Ash": "survey", "Bly": "shelter"}


def test_sample_without_a_project_counts_the_whole_board(board):
    """A data directory persists across runs; the filter keeps a run's count honest."""
    assert sampler.sample(board)["tasks_total"] == 8


def test_find_board_takes_the_file_or_searches_the_directory(tmp_path, board):
    """A backups copy never wins; the shallowest kanban.db does."""
    assert sampler.find_board(board) == board
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "kanban.db").write_text("")
    assert sampler.find_board(tmp_path) == board
    assert sampler.find_board(tmp_path / "nowhere") is None
