#!/usr/bin/env python3
"""
One-shot backfill of ``runs.ended_at`` from ``token_events.timestamp``.

The ``runs`` table has lifecycle columns (``ended_at``, ``total_tasks``,
``completed_tasks``, etc.) that the schema clearly anticipates, but
until Marcus #537 no Python code ever wrote them. Every row in ``runs``
has been "open" since insertion. This blocks any dashboard query
filtering on ``ended_at IS NOT NULL`` and prevents wall-clock-time
analysis (cost-per-minute, coordination tax rate, run duration).

Going forward, the board closes runs live: the first ``request_next_task``
that returns ``BOARD_DRAINED`` for a project stamps its open run. This
backfill closes the rest: historical runs that pre-date the fix, and runs
whose board never drained (abandoned projects, ``path=direct`` sessions
that stopped asking for work).

Heuristic
---------
For each ``runs`` row where ``ended_at IS NULL``, set ``ended_at`` to
``MAX(timestamp) FROM token_events WHERE run_id = ?`` — the timestamp
of the last LLM call attributed to that run. That's the best on-disk
approximation we have for unattended close. Runs with zero token
events are left open (genuinely empty runs are ambiguous and likely
indicate the run died before any LLM call).

Idempotent: re-runnable. Rows with ``ended_at`` already set are
skipped (the CostStore's UPDATE only fills NULL fields).

Usage
-----
.. code-block:: console

    $ python scripts/backfill_run_close_state.py
    Closed 23 runs (10 had zero events and were skipped).

Pass ``--costs-db PATH`` to override the cost-store path (defaults
to ``~/.marcus/costs.db``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.cost_tracking.cost_store import CostStore  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--costs-db",
        type=Path,
        default=Path.home() / ".marcus" / "costs.db",
        help="Path to costs.db (default: ~/.marcus/costs.db).",
    )
    return parser.parse_args()


def backfill(costs_db: Path) -> Tuple[int, int]:
    """Close every open run whose token_events have a max timestamp.

    Returns
    -------
    (closed, skipped) : tuple of int
        Counts for runs successfully closed and runs left open
        because they had zero token_events.
    """
    store = CostStore(db_path=costs_db)

    open_rows = store.conn.execute(
        "SELECT run_id FROM runs WHERE ended_at IS NULL"
    ).fetchall()

    closed = 0
    skipped = 0
    for (run_id,) in open_rows:
        # ``close_run`` returns False when there are no events to
        # derive ended_at from — those runs stay open.
        if store.close_run(run_id):
            closed += 1
        else:
            skipped += 1
    return closed, skipped


def main() -> int:
    """Parse CLI args, run the backfill, and print a summary."""
    args = _parse_args()
    closed, skipped = backfill(args.costs_db)
    print(
        f"Closed {closed} runs"
        + (f" ({skipped} had zero events and were skipped)." if skipped > 0 else ".")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
