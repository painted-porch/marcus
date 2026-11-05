"""Unit tests for the condition A/C interim-cost stop check.

The whole point of this module is that a stop line is enforced from
real transcript data, read before the next spend, not estimated or
checked only after a run finishes. These tests pin the parsing against
a synthetic transcript of known shape, and the stop decision's boundary.
"""

import json

import pytest
from runners.numeric_cost_lib import OPUS_5_PRICES, session_cost, total_dollars
from runners.numeric_cost_watchdog import check_before_next_pass


def write_transcript(path, usages):
    """Write a synthetic transcript with one usage block per line.

    Parameters
    ----------
    path : pathlib.Path
        Where to write the file.
    usages : list
        One dict per line, placed at ``message.usage``.
    """
    lines = [json.dumps({"message": {"usage": u}}) for u in usages]
    path.write_text("\n".join(lines))


class TestSessionCost:
    """Parsing one transcript's accumulated usage."""

    @pytest.mark.unit
    def test_sums_usage_across_every_line(self, tmp_path):
        """Multiple usage blocks in one file accumulate."""
        path = tmp_path / "t.jsonl"
        write_transcript(
            path,
            [
                {
                    "input_tokens": 10,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 50,
                },
                {
                    "input_tokens": 5,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 500,
                    "cache_creation_input_tokens": 25,
                },
            ],
        )
        got = session_cost(path)
        assert got["input_tokens"] == 15
        assert got["output_tokens"] == 150
        assert got["cache_read_tokens"] == 1500
        assert got["cache_write_tokens"] == 75

    @pytest.mark.unit
    def test_dollar_amount_matches_opus_5_rates(self, tmp_path):
        """The price table is applied correctly, not just the tokens."""
        path = tmp_path / "t.jsonl"
        write_transcript(
            path,
            [
                {
                    "input_tokens": 1_000_000,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                }
            ],
        )
        assert session_cost(path)["dollars"] == pytest.approx(5.00)

    @pytest.mark.unit
    def test_matches_the_measured_rehearsal_figures(self, tmp_path):
        """Pins the exact real numbers this estimate was built from."""
        path = tmp_path / "case-000.jsonl"
        write_transcript(
            path,
            [
                {
                    "input_tokens": 22,
                    "output_tokens": 15770,
                    "cache_read_input_tokens": 527072,
                    "cache_creation_input_tokens": 45020,
                }
            ],
        )
        assert session_cost(path)["dollars"] == pytest.approx(0.9393, abs=0.0001)

    @pytest.mark.unit
    def test_a_trailing_partial_line_is_skipped_not_raised(self, tmp_path):
        """A transcript mid-write (live run) does not crash the check."""
        path = tmp_path / "t.jsonl"
        good = json.dumps(
            {
                "message": {
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    }
                }
            }
        )
        path.write_text(good + '\n{"message": {"usage": {"inp')
        got = session_cost(path)
        assert got["input_tokens"] == 1

    @pytest.mark.unit
    def test_missing_file_costs_nothing(self, tmp_path):
        """A case that has not started yet contributes zero, not an error."""
        got = session_cost(tmp_path / "does-not-exist.jsonl")
        assert got["dollars"] == 0.0

    @pytest.mark.unit
    def test_a_line_with_no_usage_block_is_ignored(self, tmp_path):
        """Non-usage transcript lines (tool calls, text) don't break parsing."""
        path = tmp_path / "t.jsonl"
        path.write_text(
            json.dumps({"message": {"content": "hello"}})
            + "\n"
            + json.dumps(
                {
                    "message": {
                        "usage": {
                            "input_tokens": 3,
                            "output_tokens": 4,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        }
                    }
                }
            )
        )
        assert session_cost(path)["input_tokens"] == 3


class TestTotalDollars:
    """Summing across every transcript a run has produced so far."""

    @pytest.mark.unit
    def test_sums_across_multiple_transcripts(self, tmp_path):
        """A run's cost is every case's cost added together."""
        for i, tokens in enumerate([100, 200]):
            write_transcript(
                tmp_path / f"case-{i}.jsonl",
                [
                    {
                        "input_tokens": 0,
                        "output_tokens": tokens,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    }
                ],
            )
        paths = [tmp_path / "case-0.jsonl", tmp_path / "case-1.jsonl"]
        expected = (100 + 200) * OPUS_5_PRICES[1] / 1_000_000
        assert total_dollars(paths) == pytest.approx(expected, abs=0.0001)

    @pytest.mark.unit
    def test_empty_list_costs_nothing(self):
        """No cases run yet means zero spend, not an error."""
        assert total_dollars([]) == 0.0


class TestCheckBeforeNextPass:
    """The stop decision a caller must act on before the next spend."""

    @pytest.mark.unit
    def test_spend_below_the_line_does_not_stop(self, tmp_path):
        """A run well under budget is told to continue."""
        write_transcript(
            tmp_path / "t.jsonl",
            [
                {
                    "input_tokens": 0,
                    "output_tokens": 1000,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                }
            ],
        )
        result = check_before_next_pass([tmp_path / "t.jsonl"], stop_line=75.0)
        assert result.should_stop is False

    @pytest.mark.unit
    def test_spend_exactly_at_the_line_stops(self, tmp_path):
        """The line is a ceiling that has been reached, not strictly exceeded."""
        tokens_for_exactly_one_dollar = int(1_000_000 / OPUS_5_PRICES[1])
        write_transcript(
            tmp_path / "t.jsonl",
            [
                {
                    "input_tokens": 0,
                    "output_tokens": tokens_for_exactly_one_dollar,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                }
            ],
        )
        result = check_before_next_pass([tmp_path / "t.jsonl"], stop_line=1.0)
        assert result.should_stop is True

    @pytest.mark.unit
    def test_spend_past_the_line_stops(self, tmp_path):
        """Overshoot (a single large pass) still halts the next one."""
        write_transcript(
            tmp_path / "t.jsonl",
            [
                {
                    "input_tokens": 0,
                    "output_tokens": 1_000_000,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                }
            ],
        )
        result = check_before_next_pass([tmp_path / "t.jsonl"], stop_line=1.0)
        assert result.should_stop is True
        assert result.spent > result.stop_line

    @pytest.mark.unit
    def test_result_reports_the_real_spent_amount(self, tmp_path):
        """The caller can log or display what was actually spent."""
        write_transcript(
            tmp_path / "t.jsonl",
            [
                {
                    "input_tokens": 0,
                    "output_tokens": 40000,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                }
            ],
        )
        result = check_before_next_pass([tmp_path / "t.jsonl"], stop_line=75.0)
        assert result.spent == pytest.approx(1.0, abs=0.0001)
