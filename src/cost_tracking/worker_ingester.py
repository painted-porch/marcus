"""
Batch ingester for Claude Code worker session JSONL logs.

Worker sessions (see ``dev-tools/experiments/runners/session_launcher.py``)
each run a Claude Code CLI subprocess that writes a session log to
``~/.claude/projects/<dir>/<session-id>.jsonl``. Every assistant turn lands
as a JSON record carrying a ``message.usage`` block with input / cache /
output token counts. This module ingests those logs into the Marcus cost
store so the Cato dashboard can display per-agent / per-task / per-turn
spend alongside the planner-side data captured in
:mod:`src.cost_tracking.cost_recorder`.

Design choices
--------------
- **Batch, not tail.** The ingester reads a file end-to-end on each call.
  A separate live-tail mode can be layered on later via filesystem
  watchers; this batch implementation is simpler to test and is enough
  for post-experiment analysis.
- **UUID-based dedup.** Each JSONL record has a unique ``uuid`` field.
  The ingester tracks already-seen UUIDs (per session) in an in-memory
  set scoped to the ``CostStore`` instance, so re-running ``ingest_file``
  is idempotent within a single process. Cross-process dedup would
  require a side table — added later if needed.
- **Caller supplies binding.** The ingester does not know how to map a
  session/cwd to an ``agent_id``/``run_id``/``project_id``. Callers
  provide a ``resolve_binding`` callable (typically wired from the
  ``project_info.json`` the launcher gives every session).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

from src.cost_tracking.cost_recorder import canonical_project_id
from src.cost_tracking.cost_store import CostStore
from src.cost_tracking.usage_ingestion import UsageRecord, ingest_usage_records
from src.cost_tracking.worker_intent import (
    classify_tool_intent,
    extract_task_id_from_user_message,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentBinding:
    """Per-event attribution returned by a caller's ``resolve_binding``.

    Parameters
    ----------
    agent_id : str
        Stable agent name as registered with Marcus (e.g.
        ``'agent_unicorn_1'``).
    run_id : str
        Marcus run ID this session belongs to (joins to ``runs`` table).
    project_id : str
        Marcus project ID — usually the same value the planner uses.
    parent_agent_id : str, optional
        For subagents launched by another worker, the parent's
        ``agent_id``. Plain workers leave this ``None``.
    task_id : str, optional
        Active task at the time of the turn, when known. The ingester
        cannot infer this from the JSONL alone; callers may extract it
        from MCP tool-call records and pass it in.
    """

    agent_id: str
    run_id: str
    project_id: str
    parent_agent_id: Optional[str] = None
    task_id: Optional[str] = None


# ``resolve_binding`` receives the raw JSONL record and returns either a
# binding (event will be ingested) or ``None`` (event will be dropped).
ResolveBinding = Callable[[Dict[str, Any]], Optional[AgentBinding]]


# ---------------------------------------------------------------------------
# Ingester
# ---------------------------------------------------------------------------


class WorkerJSONLIngester:
    """Reads Claude Code session JSONL files and writes ``token_events`` rows.

    Parameters
    ----------
    store : CostStore
        Backing store for ``token_events`` writes.
    resolve_binding : callable
        ``(record: dict) -> AgentBinding | None``. Returning ``None``
        skips the event. Use this to filter out sessions you don't care
        about (e.g. project-creator agents that share the same JSONL
        directory but should not count against worker cost).

    Notes
    -----
    Per-session turn indices and seen-UUID sets are kept on the
    instance, so reusing one ingester across multiple ``ingest_file``
    calls preserves both counters and dedup state.
    """

    def __init__(
        self,
        *,
        store: CostStore,
        resolve_binding: ResolveBinding,
    ) -> None:
        self.store = store
        self.resolve_binding = resolve_binding
        self._turn_counter: Dict[str, int] = defaultdict(int)
        self._seen_uuids: Set[str] = set()
        # Per-session running task tracker (Marcus #527 Phase 1.5).
        # Updated when a user-record carries a Marcus MCP tool_result
        # echoing a task_id; consumed when the next assistant record on
        # the same session emits a token_events row. Lives per-instance
        # so a single ingest_file call walks the file in order with the
        # tracker following the agent's task lifecycle.
        self._session_task: Dict[str, str] = {}

    # -- file-level API ---------------------------------------------------

    def ingest_file(self, path: Path) -> int:
        """Ingest every assistant record in one JSONL file.

        Parameters
        ----------
        path : Path
            Path to a Claude Code session JSONL.

        Returns
        -------
        int
            Number of ``token_events`` rows inserted by this call.
        """
        inserted = 0
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("skipping malformed JSONL line in %s", path)
                    continue
                if self._ingest_record(record):
                    inserted += 1
        return inserted

    def ingest_directory(self, dir_path: Path) -> int:
        """Recursively ingest every ``*.jsonl`` file under a directory."""
        total = 0
        for jsonl in sorted(dir_path.rglob("*.jsonl")):
            total += self.ingest_file(jsonl)
        return total

    def backfill_file(self, path: Path) -> int:
        """Walk a JSONL file and backfill ``task_id`` / ``tool_intent``.

        Used to populate the new columns on rows that were ingested
        before the parser landed (Marcus #527 Phase 1.5 + 2). Walks
        the file in order, maintains the same per-session task
        tracker as :meth:`ingest_file`, and calls
        :meth:`CostStore.update_attribution` for each assistant
        record carrying a ``request_id``. Skips rows that don't exist
        in the DB (the UPDATE rowcount comes back zero) and rows
        whose columns are already populated (the store's
        ``COALESCE`` guards no-op them).

        Idempotent: safe to re-run on the same file. The store's
        ``update_attribution`` only fills NULL columns, so a second
        pass after manual edits will not overwrite curated values.

        Returns
        -------
        int
            Number of rows whose ``task_id`` and/or ``tool_intent``
            were filled in by this call.
        """
        # Use a fresh per-call tracker so backfill state doesn't bleed
        # into a subsequent ingest_file call (or vice versa).
        tracker: Dict[str, str] = {}
        updated = 0
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                session_id = record.get("sessionId") or "unknown"
                record_type = record.get("type")

                if record_type == "user":
                    message = record.get("message") or {}
                    content = (
                        message.get("content") if isinstance(message, dict) else None
                    )
                    tid = extract_task_id_from_user_message(content)
                    if tid is not None:
                        tracker[session_id] = tid
                    continue

                if record_type != "assistant":
                    continue

                request_id = record.get("requestId")
                if not request_id:
                    continue

                message = record.get("message") or {}
                content = message.get("content") if isinstance(message, dict) else None
                tool_intent = classify_tool_intent(content)
                task_id = tracker.get(session_id)

                # classify_tool_intent always returns a non-None string, so
                # there's always at least one column to potentially fill.
                # update_attribution's COALESCE guards turn this into a
                # no-op when the row already has those values populated.
                rowcount = self.store.update_attribution(
                    request_id=request_id,
                    task_id=task_id,
                    tool_intent=tool_intent,
                )
                if rowcount > 0:
                    updated += 1
        return updated

    def backfill_directory(self, dir_path: Path) -> int:
        """Recursively backfill every ``*.jsonl`` file under a directory."""
        total = 0
        for jsonl in sorted(dir_path.rglob("*.jsonl")):
            total += self.backfill_file(jsonl)
        return total

    # -- record-level helpers --------------------------------------------

    def _ingest_record(self, record: Dict[str, Any]) -> bool:
        """Insert one token_events row if the record is an assistant turn.

        Also walks ``user`` records (tool_result carriers) to update
        the per-session task tracker (Marcus #527 Phase 1.5) — those
        records produce no row themselves but update the state used by
        the next assistant record on the same session.

        Returns
        -------
        bool
            True if a row was inserted, False if the record was skipped
            (wrong type, missing usage, deduped, or binding rejected).
        """
        session_id = record.get("sessionId") or "unknown"
        record_type = record.get("type")

        # User records carry tool_results — scan them for Marcus MCP
        # task_id echoes and update the tracker. No row written. The
        # tracker is per-session so concurrent agents don't bleed.
        if record_type == "user":
            message = record.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            tid = extract_task_id_from_user_message(content)
            if tid is not None:
                self._session_task[session_id] = tid
            return False

        if record_type != "assistant":
            return False
        message = record.get("message") or {}
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            return False

        uuid = record.get("uuid")
        if uuid and uuid in self._seen_uuids:
            return False

        binding = self.resolve_binding(record)
        if binding is None:
            return False

        self._turn_counter[session_id] += 1
        turn_index = self._turn_counter[session_id]

        # task_id: prefer caller-supplied binding, fall back to the
        # tracker. The fallback covers the common case where the
        # resolver only knows agent_id/project_id from the cwd and
        # never sets task_id; the tracker fills it in from MCP
        # tool-result history.
        resolved_task_id = binding.task_id or self._session_task.get(session_id)

        # tool_intent: classify the assistant message's content (#527
        # Phase 2). Pure function in worker_intent.py.
        content = message.get("content") if isinstance(message, dict) else None
        tool_intent = classify_tool_intent(content)

        # Normalize project_id to the cost-data canonical form (dashless
        # hex). Bindings come from the launcher via project_info.json,
        # which may carry either dashed or dashless UUIDs depending on
        # which Marcus code path created the project. Normalizing here
        # keeps every token_events row consistent regardless of source.
        canonical_pid = canonical_project_id(binding.project_id)
        project_id = canonical_pid if canonical_pid is not None else binding.project_id

        # Issue #742 / ADR-0013 K8 consequence 3: this ingester is now
        # one caller of the generic usage-ingestion API, not the only
        # path into token_events. request_id_override keeps the raw
        # Claude Code requestId as the stored request_id verbatim
        # (globally unique already), so existing exact-match dedup and
        # the backfill path (update_attribution, keyed on this same
        # column) are unaffected by this refactor.
        usage_record = UsageRecord(
            run_id=binding.run_id,
            agent_id=binding.agent_id,
            seq=str(record.get("requestId") or f"{session_id}:{turn_index}"),
            at=record.get("timestamp") or "",
            provider="anthropic",
            model=str(message.get("model", "unknown")),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            task_id=resolved_task_id,
            basis="attested",
            reported_by="agent",
            request_id_override=record.get("requestId"),
            operation="turn",
            tool_intent=tool_intent,
            turn_index=turn_index,
            session_id=session_id,
            parent_agent_id=binding.parent_agent_id,
        )
        try:
            ack = ingest_usage_records(self.store, project_id, [usage_record])
        except Exception as exc:  # pragma: no cover
            logger.warning("ingester swallowed store error: %s", exc)
            # Do not advance dedup counters on insert failure; let a
            # retry pick this record up.
            self._turn_counter[session_id] -= 1
            return False
        if ack.rejected:  # pragma: no cover - defensive, our own shape
            logger.warning("ingester record rejected: %s", ack.rejected)
            self._turn_counter[session_id] -= 1
            return False

        if uuid:
            self._seen_uuids.add(uuid)
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(raw: Optional[str]) -> Optional[datetime]:
    """Parse Claude Code's ISO-Z timestamps into aware datetimes.

    Returns ``None`` when ``raw`` is empty or unparseable, letting the
    store fall back to its default timestamp expression.
    """
    if not raw:
        return None
    try:
        # Python 3.11+ accepts the trailing 'Z' via fromisoformat directly.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
