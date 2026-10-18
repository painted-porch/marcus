"""
Unit tests for ``src.core.perf_instrumentation``.

The helper exists to factor the inline ``_mark`` pattern from
``request_next_task`` so other Marcus entry points can adopt the same
shape. These tests verify the contract: marks are cumulative, deltas
are computed correctly, and the async context manager logs even when
the wrapped block raises.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from src.core.perf_instrumentation import PhaseTimer, async_phase_timer


class TestPhaseTimer:
    """Direct unit tests for ``PhaseTimer``."""

    def test_marks_accumulate_in_insertion_order(self):
        """Marks recorded in the order they were called."""
        timer = PhaseTimer()
        timer.mark("a")
        timer.mark("b")
        timer.mark("c")
        keys = list(timer.to_phase_durations().keys())
        assert keys == ["a", "b", "c"]

    def test_to_phase_durations_returns_deltas_not_cumulative(self):
        """Each phase value is the elapsed ms since the previous mark.

        Construct a timer, monkey-patch its internal start time, and
        write three known cumulative marks; verify the deltas come out
        as the differences between consecutive marks.
        """
        timer = PhaseTimer()
        # Bypass real time.monotonic by writing cumulative ms directly.
        timer._marks = {
            "phase_a": 100.0,
            "phase_b": 250.0,
            "phase_c": 400.0,
        }
        deltas = timer.to_phase_durations()
        assert deltas == {
            "phase_a": 100.0,
            "phase_b": 150.0,
            "phase_c": 150.0,
        }

    def test_total_ms_reads_total_mark_when_present(self):
        """``total_ms`` returns the cumulative value at the ``"total"`` mark."""
        timer = PhaseTimer()
        timer._marks = {"phase_a": 50.0, "total": 200.0}
        assert timer.total_ms() == 200.0

    def test_total_ms_returns_zero_when_total_never_marked(self):
        """Without a ``"total"`` mark, ``total_ms`` defaults to ``0.0``."""
        timer = PhaseTimer()
        timer._marks = {"phase_a": 50.0}
        assert timer.total_ms() == 0.0

    def test_mark_overwrites_previous_value_for_same_name(self):
        """Re-marking the same name overwrites the earlier timestamp."""
        timer = PhaseTimer()
        timer._marks = {"phase_a": 50.0}
        timer.mark("phase_a")
        # Real mark recorded; previous synthetic value is gone.
        assert timer._marks["phase_a"] != 50.0


class TestAsyncPhaseTimer:
    """Tests for the ``async_phase_timer`` context manager."""

    @pytest.mark.asyncio
    async def test_logs_on_normal_exit(self):
        """On clean exit, the timer logs operation + total_ms + phases."""
        log = MagicMock(spec=logging.Logger)
        async with async_phase_timer("op_name", log, agent="x") as timer:
            timer.mark("phase_a")
        assert log.info.called
        args = log.info.call_args.args
        # First arg is the format string; subsequent args are the
        # positional values fed into it.
        assert args[0] == "%s timing: %s total_ms=%s phases=%s"
        assert args[1] == "op_name"
        assert "agent='x'" in args[2]

    @pytest.mark.asyncio
    async def test_logs_even_when_wrapped_block_raises(self):
        """A raising block still triggers the timing log via finally."""
        log = MagicMock(spec=logging.Logger)
        with pytest.raises(RuntimeError):
            async with async_phase_timer("op_name", log) as timer:
                timer.mark("phase_a")
                raise RuntimeError("boom")
        assert log.info.called

    @pytest.mark.asyncio
    async def test_marks_total_automatically_on_exit(self):
        """``async_phase_timer`` records a ``"total"`` mark on exit.

        Callers do not need to call ``timer.mark("total")`` explicitly.
        """
        log = MagicMock(spec=logging.Logger)
        captured: list[PhaseTimer] = []
        async with async_phase_timer("op_name", log) as timer:
            captured.append(timer)
        assert "total" in captured[0]._marks

    @pytest.mark.asyncio
    async def test_context_kwargs_appear_in_log_line(self):
        """All ``**context`` kwargs are formatted into the log line."""
        log = MagicMock(spec=logging.Logger)
        async with async_phase_timer(
            "op_name", log, agent_id="a1", task_id="t1", status="completed"
        ):
            pass
        ctx_str = log.info.call_args.args[2]
        assert "agent_id='a1'" in ctx_str
        assert "task_id='t1'" in ctx_str
        assert "status='completed'" in ctx_str
