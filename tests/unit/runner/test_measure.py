"""Unit tests for marcus_runner.measure."""

import json
import sqlite3

import pytest
from marcus_runner import measure

pytestmark = pytest.mark.unit


def _call(agent, at, outcome, reply=None, tool="request_next_task"):
    return {
        "at": at,
        "agent": agent,
        "tool": tool,
        "args": {},
        "outcome": outcome,
        "ms": 1,
        "reply": reply or {},
    }


def test_pulls_and_idle_poll_ratio():
    """Retries over total pulls; other tools never count."""
    calls = [
        _call("a", "1", "assigned"),
        _call("a", "2", "retry"),
        _call("a", "3", "retry"),
        _call("a", "4", "drained"),
        _call("a", "5", "ok", tool="report_task_progress"),
    ]
    assert measure.pulls(calls) == {
        "total": 4,
        "assigned": 1,
        "retry": 2,
        "drained": 1,
        "idle_poll_ratio": 0.5,
    }
    assert measure.pulls([])["idle_poll_ratio"] is None


def test_frontier_series_reads_drain_or_frontier_on_non_assignments():
    """The kernel reports tasks_claimable only on replies that are not assignments."""
    calls = [
        _call("a", "1", "retry", {"drain": {"tasks_claimable": 3}}),
        _call("b", "2", "retry", {"frontier": {"tasks_claimable": 0}}),
        _call("a", "3", "assigned", {"task": {"id": "t"}}),
    ]
    assert measure.frontier_series(calls) == [("a", 3), ("b", 0)]
    assert measure.summarize([3, 0]) == {"samples": 2, "min": 0, "max": 3, "mean": 1.5}
    assert measure.summarize([])["mean"] is None


def test_read_calls_merges_both_layouts_oldest_first(tmp_path):
    """Minecraft writes <agent>/<agent>/board-calls.jsonl; a thin runtime <agent>/."""
    (tmp_path / "Ash" / "Ash").mkdir(parents=True)
    (tmp_path / "blue-1").mkdir()
    (tmp_path / "Ash" / "Ash" / "board-calls.jsonl").write_text(
        json.dumps(_call("Ash", "2026-11-11T00:00:02Z", "retry")) + "\n"
    )
    (tmp_path / "blue-1" / "board-calls.jsonl").write_text(
        json.dumps(_call("blue-1", "2026-11-11T00:00:01Z", "assigned")) + "\nbroken\n"
    )
    calls = measure.read_calls(tmp_path)
    assert [c["agent"] for c in calls] == ["blue-1", "Ash"]
    assert measure.board_calls(calls) == {"request_next_task": 2}


def test_board_from_db_counts_a_projects_statuses(tmp_path):
    """When the watchdog has killed the server, the copy of the board is the source."""
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.execute(
        "create table tasks"
        " (id text, name text, status text, assigned_to text, project_id text)"
    )
    con.executemany(
        "insert into tasks values (?,?,?,?,?)",
        [
            ("1", "a", "done", None, "p"),
            ("2", "b", "todo", None, "p"),
            ("3", "c", "blocked", None, "p"),
            ("4", "d", "done", None, "q"),
        ],
    )
    con.commit()
    con.close()
    assert measure.board_from_db(db, "p") == {
        "total_tasks": 3,
        "completed": 1,
        "in_progress": 0,
        "blocked": 1,
        "declined": 0,
        "todo": 1,
    }


def test_sampled_frontier_and_utilization():
    """The sampler's series summarised; holders counted per sample."""
    samples = [
        {"tasks_claimable": 2, "holders": {"a": "x"}},
        {"tasks_claimable": 0, "holders": {"a": "x", "b": "y"}},
        {"error": "locked"},
    ]
    assert measure.sampled_frontier(samples) == {
        "samples": 2,
        "min": 0,
        "max": 2,
        "mean": 1.0,
        "series": [2, 0],
    }
    util = measure.utilization(samples, agents=2)
    assert util["series"] == [1, 2]
    assert util["mean_fraction_busy"] == 0.75
