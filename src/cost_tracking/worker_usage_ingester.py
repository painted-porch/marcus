"""Ingest non-git worker usage into the cost store (issue #737, step 8).

The existing worker ingester (``worker_ingester.py``) attributes
Claude-session costs through the ``worktrees/<agent_id>`` path shape.
Pull-loop workers (the Tow finder) have no worktree and no Claude
session; they write one JSONL row per engine call with the fields the
record needs: ``agent_id``, ``task_id``, ``case_id``, token counts,
and the engine name. This module ingests those rows into
``token_events`` keyed directly by ``agent_id`` and ``task_id``,
which is the "attributed without a worktree path" patch the issue
names.

Idempotent by construction: each row's ``request_id`` is a
deterministic hash of its content, and :meth:`CostStore.record_event`
uses ``INSERT OR IGNORE`` on that id, so re-running the ingester
after a crash or a re-export adds nothing.
"""

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Union

from src.cost_tracking.cost_store import CostStore, TokenEvent

logger = logging.getLogger(__name__)


def _request_id(row: Dict[str, Any]) -> str:
    """Build the deterministic dedup id for one usage row.

    Parameters
    ----------
    row : Dict[str, Any]
        The parsed usage row.

    Returns
    -------
    str
        A stable id derived from the row's identifying fields.
    """
    basis = json.dumps(
        {
            "ts": row.get("ts"),
            "agent_id": row.get("agent_id"),
            "task_id": row.get("task_id"),
            "engine": row.get("engine"),
            "prompt_tokens": row.get("prompt_tokens"),
            "completion_tokens": row.get("completion_tokens"),
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
    return f"towusage-{digest}"


def _parse_ts(raw: Any) -> Any:
    """Parse the row's ISO timestamp, or None to let SQLite stamp it."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def ingest_usage_log(
    store: CostStore,
    path: Union[str, Path],
    run_id: str,
    project_id: str,
) -> int:
    """Ingest one worker usage JSONL file into the cost store.

    Parameters
    ----------
    store : CostStore
        The cost store (``~/.marcus/costs.db`` in production, a
        temporary one in tests).
    path : Union[str, Path]
        The usage JSONL file a worker wrote (one JSON object per
        line).
    run_id : str
        Run identifier the rows are recorded under.
    project_id : str
        Marcus project id the rows belong to.

    Returns
    -------
    int
        The number of NEW rows recorded; duplicates count zero.
    """
    new_rows = 0
    with open(path, encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as parse_err:
                logger.warning(
                    "[worker-usage] skipping malformed line %d of %s: %s",
                    line_number,
                    path,
                    parse_err,
                )
                continue

            before = _count_for_request(store, _request_id(row))
            store.record_event(
                TokenEvent(
                    run_id=run_id,
                    project_id=project_id,
                    agent_id=str(row.get("agent_id") or "unknown"),
                    agent_role="worker",
                    operation="engine_call",
                    provider=str(row.get("engine") or "unknown"),
                    model=str(row.get("model") or "unknown"),
                    input_tokens=int(row.get("prompt_tokens") or 0),
                    output_tokens=int(row.get("completion_tokens") or 0),
                    task_id=str(row.get("task_id") or "") or None,
                    request_id=_request_id(row),
                    timestamp=_parse_ts(row.get("ts")),
                )
            )
            after = _count_for_request(store, _request_id(row))
            if after > before:
                new_rows += 1
    return new_rows


def _count_for_request(store: CostStore, request_id: str) -> int:
    """Count token_events rows for one request id."""
    row = store.conn.execute(
        "SELECT COUNT(*) FROM token_events WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    return int(row[0])


def events_for_tasks(store: CostStore, task_ids: Sequence[str]) -> List[Dict[str, Any]]:
    """Read the cost rows for a set of tasks, for the audit bundle.

    Parameters
    ----------
    store : CostStore
        The cost store to query.
    task_ids : Sequence[str]
        The case's task ids.

    Returns
    -------
    List[Dict[str, Any]]
        One dict per token_events row: agent_id, task_id, provider,
        model, token counts, and timestamp.
    """
    ids = [t for t in task_ids if t]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = store.conn.execute(
        f"""
        SELECT agent_id, task_id, provider, model,
               input_tokens, output_tokens, total_tokens, timestamp
        FROM token_events
        WHERE task_id IN ({placeholders})
        ORDER BY timestamp
        """,  # nosec B608 - placeholders only; ids are bound parameters
        ids,
    ).fetchall()
    return [
        {
            "agent_id": r[0],
            "task_id": r[1],
            "provider": r[2],
            "model": r[3],
            "input_tokens": r[4],
            "output_tokens": r[5],
            "total_tokens": r[6],
            "timestamp": r[7],
        }
        for r in rows
    ]
