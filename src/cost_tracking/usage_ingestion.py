"""Generic usage-ingestion API (ADR-0013 K8 consequence 3, issue #742).

K8's third consequence names the problem directly: per-task agent cost
must arrive through an ingestion API, not by parsing one vendor's
session transcripts. ``WorkerJSONLIngester`` (Claude-Code-specific) was
the only path cost could reach :class:`~src.cost_tracking.cost_store.CostStore`
through; this module is the vendor-neutral API any runtime posts the
same unit to instead, with ``WorkerJSONLIngester`` becoming one adapter
that posts through it rather than writing ``token_events`` directly.

The record shape is the one agreed on issue #742 with the Minecraft
harness -- the first non-Claude-Code consumer, whose agents are Node
processes with no session Claude Code's own ingester could read. Three
things that shape insisted on, each for a concrete reason:

- **Cache tokens are first-class, not an afterthought.** Minecraft's
  own run-1 numbers: cache-read tokens were 98% of total tokens and
  about 65% of dollars for one agent. A shape that buries cache counts
  inside a generic "extra" field would make the dominant cost
  invisible to any reader of this API.
- **``task_id`` is nullable, with an ``attribution`` saying why.**
  A harness that never sees usage in-process cannot know which task a
  call belongs to at the moment of the call; it attributes calls to
  tasks after the fact, from its own board-call log. Real spend also
  happens with no task at all -- between a completion and the next
  assignment, or inside the planner/controller, which must never be
  folded into a worker's task.
- **``usd`` is optional, with ``priced_by`` when present.** A runtime
  may have its own per-call price (e.g. pi's own table); Marcus's own
  canonical price is the ``v_event_cost`` view's token-count times
  ``model_prices`` calculation, which this module never overrides.
  ``reported_usd`` is provenance, kept separate so two attested prices
  for the same call are never silently mixed.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from src.cost_tracking.cost_recorder import canonical_project_id
from src.cost_tracking.cost_store import CostStore, TokenEvent

#: Where a call falls relative to the agent's claimed task, when known.
VALID_ATTRIBUTIONS = {"held", "between_tasks", "controller"}

#: Who relayed this record -- never the model itself for "harness" or
#: "controller"; "agent" is a worker's own self-report.
VALID_REPORTED_BY = {"harness", "controller", "agent"}


@dataclass(frozen=True)
class UsageRecord:
    """One model call, in the shape agreed on issue #742.

    Parameters
    ----------
    run_id : str
        Marcus run this call belongs to.
    agent_id : str
        The agent (or controller/planner) that made the call.
    seq : str
        The call's own stable identifier within this agent's run --
        a per-call index, a provider request id, or any other value
        that is the same across a replay of the same ledger. Combined
        with ``run_id`` and ``agent_id`` for idempotent dedup.
    at : str
        ISO-8601 timestamp of the call.
    provider, model : str
        Vendor and model identifier as the caller's own ledger names
        them.
    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens : int
        The four token counts every caller's ledger has carried so
        far. Default 0, never omitted silently.
    reasoning_tokens : Optional[int]
        Billed as output by most providers but tracked separately
        where the caller's own ledger already distinguishes it.
    task_id : Optional[str]
        The task this call belongs to, when known. Only meaningful
        when ``attribution`` is ``"held"`` or unset; forced to
        ``None`` for ``"between_tasks"`` and ``"controller"`` rows
        regardless of what a caller passes, since those two can never
        be a task's spend by definition.
    attribution : Optional[str]
        ``"held"`` (inside the agent's claimed task's window),
        ``"between_tasks"``, or ``"controller"``. ``None`` behaves
        like ``"held"`` for callers with no such distinction to make
        (e.g. a single-call-per-task worker).
    api : Optional[str]
        The call's own API/endpoint label, when the caller's ledger
        carries one.
    usd : Optional[float]
        The caller's own priced dollar figure, if it has one.
    basis : str
        ``"attested"`` (unverified, worker/relay-reported, the
        default), ``"vendor_usage_block"`` (relayed straight from the
        provider's own usage block, never touched by the model), or
        ``"metered"`` (Marcus observed the call itself).
    priced_by : Optional[str]
        Whose table produced ``usd``, when ``usd`` is set.
    reported_by : Optional[str]
        ``"harness"``, ``"controller"``, or ``"agent"`` -- see
        :data:`VALID_REPORTED_BY`.
    request_id_override : Optional[str]
        For a caller whose own ``seq`` is already a globally unique
        call id (Claude Code's own ``requestId``, for instance): store
        this value as ``token_events.request_id`` verbatim instead of
        deriving one from ``(run_id, agent_id, seq)``. Existing
        dedup/backfill paths that match on the provider's own id keep
        working unchanged when a caller sets this; omitted, the
        derived hash is used, which is the right default for a
        caller (like Minecraft's harness) whose ``seq`` is only
        unique within this tuple, not globally.
    operation : str
        High-level operation name for this call. Defaults to
        ``"model_call"``, the generic label for a caller with no
        finer-grained concept; a caller that already has one (the
        planner's own ``parse_prd``/``decompose_prd`` names, or
        ``WorkerJSONLIngester``'s existing ``"turn"``) passes it
        through unchanged.
    tool_intent, turn_index, session_id, parent_agent_id, subtask_id : optional
        Pass-through fields for a caller that already tracks these
        (``WorkerJSONLIngester`` does, from Claude Code's own session
        shape); ``None`` for a caller with no such concept.
    """

    run_id: str
    agent_id: str
    seq: str
    at: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: Optional[int] = None
    task_id: Optional[str] = None
    attribution: Optional[str] = None
    api: Optional[str] = None
    usd: Optional[float] = None
    basis: str = "attested"
    priced_by: Optional[str] = None
    reported_by: Optional[str] = None
    request_id_override: Optional[str] = None
    operation: str = "model_call"
    tool_intent: Optional[str] = None
    turn_index: Optional[int] = None
    session_id: Optional[str] = None
    parent_agent_id: Optional[str] = None
    subtask_id: Optional[str] = None


@dataclass(frozen=True)
class RejectedRecord:
    """One record a batch could not ingest, and why."""

    index: int
    reason: str


@dataclass(frozen=True)
class BatchAck:
    """Per-batch outcome.

    So a replay after a crash is idempotent and auditable rather than
    a bare pass/fail.
    """

    batch_id: str
    accepted: int
    already_present: int
    rejected: List[RejectedRecord] = field(default_factory=list)


def _request_id(record: UsageRecord) -> str:
    """Deterministic dedup key: (run_id, agent_id, seq).

    Codex review, round 1 on PR #867: a plain ``":"``-joined string
    is ambiguous -- ``("run", "worker:1", "2")`` and
    ``("run", "worker", "1:2")`` both join to ``"run:worker:1:2"``
    and would silently collide. A length-prefixed encoding (each
    component as ``len(component)`` followed by a null byte and the
    component's own bytes) makes the three-way split unambiguous
    regardless of what characters any component contains.
    """
    parts = (record.run_id, record.agent_id, record.seq)
    encoded = b"".join(
        f"{len(part.encode('utf-8'))}\0".encode("utf-8") + part.encode("utf-8")
        for part in parts
    )
    return hashlib.sha256(encoded).hexdigest()


def _parse_timestamp(raw: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, tolerating a trailing 'Z'.

    Returns ``None`` when ``raw`` is unparseable. Callers must reject
    the record in that case rather than let the store's own "now"
    default silently stand in for a bad or missing timestamp (Codex
    review, round 1 on PR #867) -- a replayed ledger with a corrupt
    timestamp would otherwise be recorded at the wrong time and still
    count as accepted.
    """
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _invalid_token_count(value: object, field_name: str) -> Optional[str]:
    """Validate one required token-count field.

    Must be a non-negative ``int``, never a ``bool`` (a ``bool`` is an
    ``int`` subclass in Python, so ``True``/``False`` would otherwise
    silently pass as 1/0) and never a ``float`` or ``None``.

    Returns
    -------
    Optional[str]
        A rejection reason naming the field, or ``None`` when valid.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return f"{field_name} must be a non-negative int, got {value!r}"
    return None


def _invalid_optional_token_count(value: object, field_name: str) -> Optional[str]:
    """Validate an optional token-count field.

    Same as :func:`_invalid_token_count`, but ``None`` is valid.
    """
    if value is None:
        return None
    return _invalid_token_count(value, field_name)


def _invalid_usd(value: object) -> Optional[str]:
    """Validate the optional ``usd`` field.

    Must be ``None``, or a finite, non-negative, non-bool number.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"usd must be a finite non-negative number, got {value!r}"
    if not math.isfinite(value) or value < 0:
        return f"usd must be a finite non-negative number, got {value!r}"
    return None


def ingest_usage_records(
    store: CostStore,
    project_id: str,
    records: Sequence[UsageRecord],
    batch_id: Optional[str] = None,
) -> BatchAck:
    """Ingest one batch of usage records, idempotently.

    Parameters
    ----------
    store : CostStore
        Backing store for ``token_events`` writes.
    project_id : str
        The Marcus project every record in this batch belongs to.
        Not carried per-record: a caller already knows which project
        its own run is registered under. Normalized to the canonical
        dashless form (:func:`canonical_project_id`) before storage,
        the same normalization ``PlannerContext`` and
        ``WorkerJSONLIngester`` already apply -- a caller-supplied
        dashed ``ProjectRegistry`` id would otherwise split this
        project's ledger into two halves ``CostAggregator`` never
        joins back together.
    records : Sequence[UsageRecord]
        The batch to ingest, in the issue #742 shape.
    batch_id : Optional[str]
        Caller-supplied id for this batch, echoed back on the ack so
        the caller can store it alongside its own run record. A
        content hash of the batch's own (run_id, agent_id, seq) keys
        is used when omitted.

    Returns
    -------
    BatchAck
        Per-record outcome: how many were newly accepted, how many
        were already present (an idempotent replay), and which were
        rejected and why.
    """
    canonical_pid = canonical_project_id(project_id)
    project_id = canonical_pid if canonical_pid is not None else project_id

    if batch_id is None:
        if records:
            batch_id = hashlib.sha256(
                "|".join(_request_id(r) for r in records).encode("utf-8")
            ).hexdigest()
        else:
            batch_id = hashlib.sha256(b"empty-batch").hexdigest()

    accepted = 0
    already_present = 0
    rejected: List[RejectedRecord] = []

    for index, record in enumerate(records):
        if (
            record.attribution is not None
            and record.attribution not in VALID_ATTRIBUTIONS
        ):
            rejected.append(
                RejectedRecord(index, f"invalid attribution: {record.attribution!r}")
            )
            continue
        if (
            record.reported_by is not None
            and record.reported_by not in VALID_REPORTED_BY
        ):
            rejected.append(
                RejectedRecord(index, f"invalid reported_by: {record.reported_by!r}")
            )
            continue

        timestamp = _parse_timestamp(record.at)
        if timestamp is None:
            rejected.append(RejectedRecord(index, f"invalid timestamp: {record.at!r}"))
            continue

        count_error = (
            _invalid_token_count(record.input_tokens, "input_tokens")
            or _invalid_token_count(record.output_tokens, "output_tokens")
            or _invalid_token_count(record.cache_read_tokens, "cache_read_tokens")
            or _invalid_token_count(
                record.cache_creation_tokens, "cache_creation_tokens"
            )
            or _invalid_optional_token_count(
                record.reasoning_tokens, "reasoning_tokens"
            )
        )
        if count_error is not None:
            rejected.append(RejectedRecord(index, count_error))
            continue

        usd_error = _invalid_usd(record.usd)
        if usd_error is not None:
            rejected.append(RejectedRecord(index, usd_error))
            continue

        # "between_tasks" and "controller" spend can never be a task's,
        # regardless of what a caller supplies -- Minecraft's own
        # stated rule on #742 ("the planner's calls are a controller
        # row, not an agent's, and must never be attributed to a task").
        task_id = record.task_id if record.attribution in (None, "held") else None
        agent_role = "planner" if record.attribution == "controller" else "worker"

        request_id = record.request_id_override or _request_id(record)

        event = TokenEvent(
            run_id=record.run_id,
            project_id=project_id,
            agent_id=record.agent_id,
            agent_role=agent_role,
            parent_agent_id=record.parent_agent_id,
            task_id=task_id,
            subtask_id=record.subtask_id,
            operation=record.operation,
            tool_intent=record.tool_intent,
            provider=record.provider,
            model=record.model,
            input_tokens=record.input_tokens,
            cache_creation_tokens=record.cache_creation_tokens,
            cache_read_tokens=record.cache_read_tokens,
            output_tokens=record.output_tokens,
            session_id=record.session_id,
            turn_index=record.turn_index,
            request_id=request_id,
            attribution=record.attribution,
            reported_by=record.reported_by,
            basis=record.basis,
            reported_usd=record.usd,
            priced_by=record.priced_by,
            reasoning_tokens=record.reasoning_tokens,
            api=record.api,
            timestamp=timestamp,
        )
        # Codex review, round 3 on PR #867: deriving accepted/
        # already_present from a SELECT taken before this INSERT is a
        # check-then-act race -- two callers on separate connections
        # can both see "not found" and both report "accepted" for
        # what the database ultimately stores as one row.
        # record_event_with_status's rowcount comes from the INSERT
        # OR IGNORE itself, atomic with the write.
        _event_id, was_inserted = store.record_event_with_status(event)

        if was_inserted:
            accepted += 1
        else:
            already_present += 1

    return BatchAck(
        batch_id=batch_id,
        accepted=accepted,
        already_present=already_present,
        rejected=rejected,
    )


def read_run_ledger(store: CostStore, run_id: str) -> Dict[str, Any]:
    """Read one run's ledger, grouped by agent and by task.

    Lets a consumer like ``measure.py`` or Cato read one source
    instead of re-pricing a vendor's transcripts itself.

    Parameters
    ----------
    store : CostStore
        Backing store to read from.
    run_id : str
        The run to read.

    Returns
    -------
    Dict[str, Any]
        ``{"run_id", "by_agent": {agent_id: totals}, "by_task": {task_id: totals}}``.
        ``by_task`` omits calls with no task (``attribution`` of
        ``"between_tasks"`` or ``"controller"``, or any row with no
        ``task_id``) -- those are real spend, visible in ``by_agent``,
        but not any task's.
    """
    rows = store.conn.execute(
        """
        SELECT agent_id, task_id, SUM(input_tokens), SUM(output_tokens),
               SUM(cache_read_tokens), SUM(cache_creation_tokens), COUNT(*)
        FROM token_events
        WHERE run_id = ?
        GROUP BY agent_id, task_id
        """,
        (run_id,),
    ).fetchall()

    def _empty_totals() -> Dict[str, int]:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "calls": 0,
        }

    by_agent: Dict[str, Dict[str, int]] = {}
    by_task: Dict[str, Dict[str, int]] = {}
    for agent_id, task_id, inp, out, cread, ccreate, count in rows:
        agent_bucket = by_agent.setdefault(agent_id, _empty_totals())
        agent_bucket["input_tokens"] += inp or 0
        agent_bucket["output_tokens"] += out or 0
        agent_bucket["cache_read_tokens"] += cread or 0
        agent_bucket["cache_creation_tokens"] += ccreate or 0
        agent_bucket["calls"] += count

        if task_id:
            task_bucket = by_task.setdefault(task_id, _empty_totals())
            task_bucket["input_tokens"] += inp or 0
            task_bucket["output_tokens"] += out or 0
            task_bucket["cache_read_tokens"] += cread or 0
            task_bucket["cache_creation_tokens"] += ccreate or 0
            task_bucket["calls"] += count

    return {"run_id": run_id, "by_agent": by_agent, "by_task": by_task}
