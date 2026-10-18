"""
Lightweight phase-by-phase wall-clock instrumentation helpers.

Background
----------
``src/marcus_mcp/tools/task.py::request_next_task`` already has an
inline ``_mark`` timing pattern that emits a structured log line per
call. This module factors that pattern into a small reusable helper so
other Marcus entry points (``report_task_progress``,
``create_project_from_description``, etc.) can adopt the same shape
without copying the boilerplate.

The goal is observability of where Marcus spends time per task
lifecycle, so a single "task takes 4 minutes" observation can be
decomposed into:

- ``request_next_task`` (assignment cost)
- ``report_task_progress`` (per-progress + completion processing)
- ``create_project_from_description`` (one-time setup)

Plus the agent's actual execution time, which Marcus does not control.

Usage
-----
>>> import logging
>>> logger = logging.getLogger(__name__)
>>> async def some_entry_point(agent_id, task_id):
...     async with async_phase_timer(
...         "some_entry_point", logger,
...         agent_id=agent_id, task_id=task_id,
...     ) as timer:
...         # ... work ...
...         timer.mark("phase_a")
...         # ... more work ...
...         timer.mark("phase_b")
...         return {"ok": True}
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict


class PhaseTimer:
    """Phase-by-phase wall-clock timer.

    A new instance starts the wall clock on construction. Call
    :meth:`mark` at each phase boundary; the marks are cumulative
    milliseconds since construction. :meth:`to_phase_durations`
    converts them to per-phase deltas at log time.

    Attributes
    ----------
    None public; use :meth:`mark`, :meth:`to_phase_durations`,
    :meth:`total_ms`.
    """

    def __init__(self) -> None:
        """Start the wall clock immediately."""
        self._start = time.perf_counter()
        self._marks: Dict[str, float] = {}

    def mark(self, name: str) -> None:
        """Record a cumulative timestamp under ``name``.

        Parameters
        ----------
        name
            Phase identifier. Must be unique within this timer; later
            marks with the same name overwrite earlier ones.
        """
        self._marks[name] = (time.perf_counter() - self._start) * 1000.0

    def to_phase_durations(self) -> Dict[str, float]:
        """Convert cumulative marks to per-phase deltas in ms.

        Returns
        -------
        Dict[str, float]
            Ordered mapping of phase name to elapsed ms since the
            previous mark. The first mark is measured against the
            timer's construction time.
        """
        items = list(self._marks.items())
        out: Dict[str, float] = {}
        for i, (name, cumulative) in enumerate(items):
            prev = items[i - 1][1] if i > 0 else 0.0
            out[name] = round(cumulative - prev, 2)
        return out

    def total_ms(self) -> float:
        """Return the cumulative ms recorded under the ``"total"`` mark.

        Returns
        -------
        float
            Cumulative ms, or ``0.0`` if ``"total"`` was never marked.
        """
        return round(self._marks.get("total", 0.0), 2)


@asynccontextmanager
async def async_phase_timer(
    operation: str,
    log: logging.Logger,
    **context: Any,
) -> AsyncIterator[PhaseTimer]:
    """Async context manager that logs phase timings on exit.

    Wraps a :class:`PhaseTimer` so the timing log fires even when the
    wrapped block raises. Each exit also marks ``"total"`` so callers
    don't have to.

    Parameters
    ----------
    operation
        Identifier used as the log line prefix (e.g.,
        ``"report_task_progress"``).
    log
        Logger to emit on exit.
    **context
        Free-form context fields to include in the log line (e.g.,
        ``agent_id=...``, ``task_id=...``). Values are formatted with
        ``repr`` so strings keep their quotes.

    Yields
    ------
    PhaseTimer
        The timer; call ``.mark(name)`` at each phase boundary.

    Notes
    -----
    The log format matches the pre-existing inline pattern in
    :func:`src.marcus_mcp.tools.task.request_next_task` so all Marcus
    timing log lines can be grepped together::

        operation timing: key1=val1 key2=val2 total_ms=N phases={...}
    """
    timer = PhaseTimer()
    try:
        yield timer
    finally:
        timer.mark("total")
        ctx_str = " ".join(f"{k}={v!r}" for k, v in context.items())
        log.info(
            "%s timing: %s total_ms=%s phases=%s",
            operation,
            ctx_str,
            timer.total_ms(),
            timer.to_phase_durations(),
        )
