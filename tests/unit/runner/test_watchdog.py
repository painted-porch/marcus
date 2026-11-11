"""Unit tests for marcus_runner.watchdog."""

from pathlib import Path

import pytest
from marcus_runner import watchdog

pytestmark = pytest.mark.unit


def test_watch_once_kills_at_the_line_not_only_past_it(monkeypatch, tmp_path):
    """A stop line reached is reached: the comparison is >=."""
    monkeypatch.setattr(watchdog, "total_dollars", lambda state, hint: 6.0)
    kills = []
    result = watchdog.watch_once(tmp_path, 6.0, lambda: kills.append(1))
    assert result.killed is True
    assert kills == [1]
    monkeypatch.setattr(watchdog, "total_dollars", lambda state, hint: 5.99)
    assert watchdog.watch_once(tmp_path, 6.0, lambda: kills.append(1)).killed is False
    assert kills == [1]


def test_a_stop_line_file_is_read_fresh_every_poll(monkeypatch, tmp_path):
    """The numeric verifier read its cap once at import; a raise must apply at once."""
    line = tmp_path / "stop-line"
    line.write_text("1.0\n")
    monkeypatch.setattr(watchdog, "total_dollars", lambda state, hint: 2.0)
    stop = watchdog.StopLineFile(line)
    assert watchdog.watch_once(tmp_path, stop, lambda: None).killed is True
    line.write_text("5.0\n")
    result = watchdog.watch_once(tmp_path, stop, lambda: None)
    assert result.stop_line == 5.0
    assert result.killed is False
    line.unlink()
    assert stop() == 5.0


def test_watch_returns_on_the_breaching_poll_without_sleeping(monkeypatch, tmp_path):
    """After a kill the run is gone; the watchdog never sleeps or polls again."""
    monkeypatch.setattr(watchdog, "total_dollars", lambda state, hint: 10.0)
    sleeps = []
    polls = []
    last = watchdog.watch(
        tmp_path,
        1.0,
        lambda: None,
        poll_seconds=30,
        on_poll=polls.append,
        sleep=sleeps.append,
    )
    assert last is not None and last.killed is True
    assert polls and sleeps == []


def test_watch_stops_at_max_polls(monkeypatch, tmp_path):
    """A watchdog that outlives its run stops on its own."""
    monkeypatch.setattr(watchdog, "total_dollars", lambda state, hint: 0.0)
    sleeps = []
    polls = []
    watchdog.watch(
        tmp_path,
        100.0,
        lambda: None,
        poll_seconds=1,
        max_polls=3,
        on_poll=polls.append,
        sleep=sleeps.append,
    )
    assert len(polls) == 3
    assert len(sleeps) == 2


def test_kill_action_kills_each_pid_file_and_tolerates_a_missing_one(
    monkeypatch, tmp_path
):
    """One failing half must not block the other."""
    ran = []
    monkeypatch.setattr(
        watchdog.subprocess,
        "run",
        lambda argv, **kw: ran.append(argv) or type("R", (), {"stdout": ""})(),
    )
    good = tmp_path / "a.pid"
    good.write_text("4242\n")
    watchdog.kill_action([tmp_path / "missing.pid", good])()
    assert ran == [["kill", "4242"]]
    assert Path(good).read_text() == "4242\n"
