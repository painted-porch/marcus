"""
Sample the board while a run is going, so frontier width has a time series.

Marcus reports its own ready-frontier width (``tasks_claimable``) only on a
``request_next_task`` reply that is not an assignment (#804), so on a healthy
wide board the agents' logs say little about the frontier. This sampler reads
the run's SQLite board read-only every few seconds and applies the rule the
drain verdict applies:

* DONE is done;
* BLOCKED or DECLINED is blocked, and nobody is coming back to it;
* IN_PROGRESS, or TODO with a name on it, is in flight;
* a TODO whose dependencies are not all done is unclaimable;
* everything else is claimable, and that count is the frontier width.

Lifted from the Minecraft plumbing's ``sample_board.py``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Statuses nobody comes back to; the drain verdict counts both as blocked
#: (ADR-0013 K5).
TERMINAL = frozenset({"blocked", "declined"})


def find_board(where: Path) -> Optional[Path]:
    """
    Resolve the board file from a path that may be the file or its directory.

    Parameters
    ----------
    where : Path
        The ``kanban.db`` file, or a data directory to search.

    Returns
    -------
    Optional[Path]
        The board file, or ``None`` when there is not one yet.
    """
    if where.is_file():
        return where
    if not where.is_dir():
        return None
    found = [p for p in where.rglob("kanban.db") if "backups" not in p.parts]
    return min(found, key=lambda p: len(p.parts)) if found else None


def sample(db: Path, project_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Take one sample of the board.

    Parameters
    ----------
    db : Path
        The run's SQLite board file.
    project_id : Optional[str]
        Count only this project's tasks; a data directory persists across
        runs and holds every past run's tasks too.

    Returns
    -------
    Dict[str, Any]
        Counts, the claimable task names, the blocked names and who holds what.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        query = (
            "select id, name, status, assigned_to from tasks"
            " where name not like 'About:%'"
        )
        params: Tuple[str, ...] = ()
        if project_id:
            query += " and project_id = ?"
            params = (project_id,)
        rows = con.execute(query, params).fetchall()
        deps = con.execute(
            "select task_id, depends_on_id from task_dependencies"
        ).fetchall()
    finally:
        con.close()

    status = {row[0]: row[2] for row in rows}
    dependencies: Dict[str, List[str]] = {}
    for task_id, depends_on in deps:
        dependencies.setdefault(task_id, []).append(depends_on)

    done: List[str] = []
    in_flight: List[str] = []
    blocked: List[str] = []
    unclaimable: List[str] = []
    claimable: List[str] = []
    holders: Dict[str, str] = {}
    for task_id, name, state, assignee in rows:
        if state == "done":
            done.append(name)
        elif state in TERMINAL:
            blocked.append(name)
        elif state == "in_progress" or (state == "todo" and assignee):
            in_flight.append(name)
            if assignee:
                holders[assignee] = name
        elif any(status.get(dep) != "done" for dep in dependencies.get(task_id, [])):
            unclaimable.append(name)
        else:
            claimable.append(name)
    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tasks_total": len(rows),
        "tasks_done": len(done),
        "tasks_in_flight": len(in_flight),
        "tasks_blocked": len(blocked),
        "tasks_unclaimable": len(unclaimable),
        "tasks_claimable": len(claimable),
        "claimable": sorted(claimable),
        "blocked": sorted(blocked),
        "holders": holders,
    }


def main(argv: Optional[List[str]] = None) -> int:
    """Sample until the deadline. Returns an exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", required=True, help="the board file or the data directory holding it"
    )
    parser.add_argument("--project", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--every", type=float, default=15.0)
    parser.add_argument("--minutes", type=float, default=30.0)
    args = parser.parse_args(argv)

    where = Path(args.db)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.minutes * 60
    db = find_board(where)
    with out.open("a") as handle:
        while time.time() < deadline:
            if db is None:
                db = find_board(where)
                if db is None:
                    time.sleep(args.every)
                    continue
            try:
                handle.write(json.dumps(sample(db, args.project)) + "\n")
            except sqlite3.Error as error:
                handle.write(
                    json.dumps(
                        {
                            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            "error": str(error),
                        }
                    )
                    + "\n"
                )
            handle.flush()
            time.sleep(args.every)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
