"""Unit tests for marcus_runner.usage."""

import json

import pytest
from marcus_runner import usage

pytestmark = pytest.mark.unit


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "{not json\n")


def test_pi_layout_counts_every_nested_usage_and_bills_only_nonzero(tmp_path):
    """pi records a zero row for an aborted continuation: a call, not billed."""
    _write(
        tmp_path / "Ash" / "Ash" / "pi-sessions" / "s.jsonl",
        [
            {
                "message": {
                    "usage": {
                        "input": 1000,
                        "output": 100,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    }
                }
            },
            {"usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}},
        ],
    )
    per_agent = usage.costs(tmp_path, "sonnet")
    assert per_agent["Ash"]["model_calls"] == 2
    assert per_agent["Ash"]["billed_calls"] == 1
    assert per_agent["Ash"]["input"] == 1000
    assert per_agent["Ash"]["dollars"] == pytest.approx(0.0045)


def test_simple_layout_prices_each_row_by_its_own_model(tmp_path):
    """A runtime that writes its own rows names the model; the hint is a fallback."""
    _write(
        tmp_path / "blue-1" / "usage.jsonl",
        [
            {
                "at": "t",
                "model": "claude-haiku-4-5",
                "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
            },
            {"at": "t", "usage": {"input": 0, "output": 100_000}},
        ],
    )
    per_agent = usage.costs(tmp_path, "claude-sonnet-5")
    assert per_agent["blue-1"]["dollars"] == pytest.approx(1.0 + 1.0)
    assert per_agent["blue-1"]["models"] == ["claude-haiku-4-5", "claude-sonnet-5"]
    assert usage.total_dollars(tmp_path, "claude-sonnet-5") == pytest.approx(2.0)


def test_normalize_reads_anthropic_and_pi_names():
    """Both namings land on the four keys, every key present."""
    assert usage.normalize_usage(
        {"input_tokens": 5, "output_tokens": 6, "cache_read_input_tokens": 7}
    ) == {
        "input": 5,
        "output": 6,
        "cacheRead": 7,
        "cacheWrite": 0,
    }
    assert usage.normalize_usage({"foo": 1}) is None
    assert usage.normalize_usage("x") is None


def test_write_usage_row_appends_a_normalised_row(tmp_path):
    """The simple layout is written one row per call, parents created."""
    path = tmp_path / "red-2" / "usage.jsonl"
    usage.write_usage_row(
        path,
        {"input_tokens": 3, "output_tokens": 4},
        "claude-sonnet-5",
        "2026-11-11T00:00:00Z",
        {"turn": 2},
    )
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows == [
        {
            "at": "2026-11-11T00:00:00Z",
            "model": "claude-sonnet-5",
            "usage": {"input": 3, "output": 4, "cacheRead": 0, "cacheWrite": 0},
            "turn": 2,
        }
    ]


def test_an_empty_state_dir_costs_nothing(tmp_path):
    """No rows, no agents, zero dollars."""
    assert usage.costs(tmp_path) == {}
    assert usage.total_dollars(tmp_path) == 0.0
