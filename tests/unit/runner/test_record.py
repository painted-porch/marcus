"""Unit tests for marcus_runner.record."""

import json
import re

import pytest
from marcus_runner import record

pytestmark = pytest.mark.unit


def test_stamp_has_the_minecraft_shape():
    """YYYYMMDDTHHMMSS."""
    assert re.fullmatch(r"\d{8}T\d{6}", record.stamp_now())


def test_config_lines_round_trip(tmp_path):
    """Every default that applied, one line each, readable back."""
    run = record.RunRecord(tmp_path / "runs", "20261111T000000")
    run.config_line("minutes", 17)
    run.config_line("stop_line_dollars", 20.0)
    assert run.config() == {"minutes": "17", "stop_line_dollars": "20.0"}
    assert run.path("run-config.txt").read_text().startswith("minutes:            17\n")


def test_jsonl_append_and_copy_in(tmp_path):
    """Events are appended and flushed; a missing source copies to None."""
    run = record.RunRecord(tmp_path / "runs")
    run.append_jsonl("events.jsonl", {"event": "posted"})
    run.append_jsonl("events.jsonl", {"event": "promoted"})
    rows = [
        json.loads(line) for line in run.path("events.jsonl").read_text().splitlines()
    ]
    assert [r["event"] for r in rows] == ["posted", "promoted"]
    src = tmp_path / "kanban.db"
    src.write_text("db")
    assert run.copy_in(src).read_text() == "db"
    assert run.copy_in(tmp_path / "missing") is None
