"""
The measures every board-mediated run reports, read from the record.

* pulls: how many ``request_next_task`` calls were assignments, retries or
  the drain verdict, and the idle-poll ratio (test18's 88% idle polls is the
  number this exists to show);
* the ready frontier as the kernel reported it on non-assignment replies, and
  as the sampler read it from outside;
* the board's own counts from a copy of ``kanban.db`` when the server is gone
  (a watchdog halt kills the server before measurement runs).

Lifted from the Minecraft plumbing's ``measure.py``; the candidate funnel and
the domain blocks stay with the extension that owns them.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Every parseable row of a JSONL file; a missing file is an empty list."""
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def read_calls(state_dir: Path) -> List[Dict[str, Any]]:
    """
    Every board call every agent recorded, oldest first.

    Parameters
    ----------
    state_dir : Path
        The run's state directory; ``board-calls.jsonl`` is read from
        ``<agent>/`` and from the doubled ``<agent>/<agent>/`` layout.

    Returns
    -------
    List[Dict[str, Any]]
        Rows with ``at``, ``agent``, ``tool``, ``args``, ``outcome``, ``ms``, ``reply``.
    """
    files = sorted(
        set(state_dir.glob("*/board-calls.jsonl"))
        | set(state_dir.glob("*/*/board-calls.jsonl"))
    )
    calls: List[Dict[str, Any]] = []
    for file in files:
        calls.extend(read_jsonl(file))
    calls.sort(key=lambda c: str(c.get("at", "")))
    return calls


def pulls(calls: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Count the pulls and compute the idle-poll ratio.

    Parameters
    ----------
    calls : Iterable[Dict[str, Any]]
        Board calls.

    Returns
    -------
    Dict[str, Any]
        ``total``, ``assigned``, ``retry``, ``drained`` and ``idle_poll_ratio``
        (retries over total, ``None`` with no pulls).
    """
    outcomes = Counter(
        str(c.get("outcome")) for c in calls if c.get("tool") == "request_next_task"
    )
    total = sum(outcomes.values())
    return {
        "total": total,
        "assigned": outcomes.get("assigned", 0),
        "retry": outcomes.get("retry", 0),
        "drained": outcomes.get("drained", 0),
        "idle_poll_ratio": (
            round(outcomes.get("retry", 0) / total, 3) if total else None
        ),
    }


def frontier_series(calls: Iterable[Dict[str, Any]]) -> List[Tuple[str, int]]:
    """Extract the frontier width the kernel reported on each non-assignment pull.

    Returns ``(agent, width)`` pairs.
    """
    series: List[Tuple[str, int]] = []
    for call in calls:
        if call.get("tool") != "request_next_task":
            continue
        reply = call.get("reply") or {}
        drain = reply.get("drain") or reply.get("frontier") or {}
        width = drain.get("tasks_claimable") if isinstance(drain, dict) else None
        if isinstance(width, int):
            series.append((str(call.get("agent")), width))
    return series


def summarize(values: List[int]) -> Dict[str, Any]:
    """Summarise a series: samples, min, max and mean; ``None`` values when empty."""
    if not values:
        return {"samples": 0, "min": None, "max": None, "mean": None}
    return {
        "samples": len(values),
        "min": min(values),
        "max": max(values),
        "mean": round(sum(values) / len(values), 3),
    }


def board_calls(calls: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """How many calls of each tool the agents made."""
    return dict(Counter(str(c.get("tool")) for c in calls))


def board_from_db(kanban_db: Path, project_id: str) -> Dict[str, Any]:
    """
    Read the board's counts from a copy of ``kanban.db``.

    Parameters
    ----------
    kanban_db : Path
        The board file.
    project_id : str
        The project to count.

    Returns
    -------
    Dict[str, Any]
        ``total_tasks``, ``completed``, ``in_progress``, ``blocked``, ``todo``,
        ``declined``.
    """
    con = sqlite3.connect(f"file:{kanban_db}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "select status, count(*) from tasks where project_id = ? group by status",
            (project_id,),
        ).fetchall()
    finally:
        con.close()
    by_status = {str(status): int(count) for status, count in rows}
    return {
        "total_tasks": sum(by_status.values()),
        "completed": by_status.get("done", 0) + by_status.get("completed", 0),
        "in_progress": by_status.get("in_progress", 0),
        "blocked": by_status.get("blocked", 0),
        "declined": by_status.get("declined", 0),
        "todo": by_status.get("todo", 0),
    }


def sampled_frontier(samples: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarise the sampler's frontier series, keeping the series."""
    widths = [
        int(s["tasks_claimable"])
        for s in samples
        if isinstance(s.get("tasks_claimable"), int)
    ]
    out = summarize(widths)
    out["series"] = widths
    return out


def utilization(
    samples: Iterable[Dict[str, Any]], agents: Optional[int] = None
) -> Dict[str, Any]:
    """How many agents held a task at each sample, and the mean fraction busy."""
    holding = [
        len(s.get("holders") or {})
        for s in samples
        if isinstance(s.get("holders"), dict)
    ]
    out = {
        "samples": len(holding),
        "mean_agents_holding_a_task": (
            round(sum(holding) / len(holding), 3) if holding else None
        ),
        "series": holding,
    }
    if agents and holding:
        out["mean_fraction_busy"] = round(sum(holding) / len(holding) / agents, 3)
    return out
