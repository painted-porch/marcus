"""Cost carried on a progress report (issue #742; kernel contract #939).

A worker that calls a model with its own key is the only party that knows
what its task cost. ADR-0013 K8 decided that per-task cost reaches the kernel
through an ingestion API rather than by parsing one vendor's transcripts, and
PR #867 built that API,
:func:`~src.cost_tracking.usage_ingestion.ingest_usage_records`.
Nothing called it from a live path, so the cost dashboard read $0.00 for an
entire run and the money appeared only after a batch import at the end.

This module is the live caller. A worker puts the calls it made on the report
it already sends, ``report_task_progress(cost=[...])``, and the kernel records
them in ``token_events`` at once.

The division of labour is fixed by the contract
(``docs/architecture/kernel-contract.md``):

* **The kernel stamps** the agent (the caller), the task (the report's), the
  project (the agent's registration, ADR-0013 K6), the run (the project's open
  run, the same one the planner's calls use) and ``attribution="held"``.
* **The worker supplies** what only it knows about each call: a ``seq`` that is
  stable across a resend, the time, provider, model, token counts and,
  optionally, its own price.
* **Nothing is recorded for a refused report**, the same rule the opinion
  marker follows. Records are deduplicated on ``(run, agent, seq)``, so a
  worker resends refused records with its next report at no risk.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.cost_tracking.usage_ingestion import UsageRecord, ingest_usage_records
from src.marcus_mcp.coordinator.agent_project import project_for_agent

#: Keys a cost record may carry. Everything the kernel stamps is absent.
COST_RECORD_FIELDS = frozenset(
    {
        "seq",
        "at",
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "reasoning_tokens",
        "usd",
        "priced_by",
        "api",
        "basis",
        "reported_by",
    }
)

#: ``metered`` is absent: only Marcus observing a call itself is metered.
COST_RECORD_BASES = frozenset({"attested", "vendor_usage_block"})

#: ``controller`` is absent: a controller's calls are never a task's spend.
COST_RECORD_REPORTERS = frozenset({"agent", "harness"})

_UNASSIGNED_RUN = "unassigned"


def _shape_error(record: Any) -> Optional[str]:
    """Return why a record cannot become a usage record, or ``None``.

    Parameters
    ----------
    record : Any
        One entry of the report's ``cost`` list.

    Returns
    -------
    Optional[str]
        A reason naming the offending field, or ``None`` when the shape is
        acceptable. Counts, prices and timestamps are checked afterwards by
        :func:`ingest_usage_records`, which owns those rules.
    """
    if not isinstance(record, dict):
        return f"a cost record must be an object, got {type(record).__name__}"
    unknown = sorted(set(record) - COST_RECORD_FIELDS)
    if unknown:
        return (
            f"unknown field {unknown[0]!r}; allowed: {sorted(COST_RECORD_FIELDS)}"
            " (the kernel stamps agent, task, project, run and attribution)"
        )
    seq = record.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, (str, int)) or seq == "":
        return f"seq must be a non-empty string or an integer, got {seq!r}"
    for name in ("at", "provider", "model"):
        value = record.get(name)
        if not isinstance(value, str) or not value.strip():
            return f"{name} must be a non-empty string, got {value!r}"
    basis = record.get("basis", "attested")
    if basis not in COST_RECORD_BASES:
        return f"basis must be one of {sorted(COST_RECORD_BASES)}, got {basis!r}"
    reported_by = record.get("reported_by", "agent")
    if reported_by not in COST_RECORD_REPORTERS:
        return (
            f"reported_by must be one of {sorted(COST_RECORD_REPORTERS)},"
            f" got {reported_by!r}"
        )
    return None


def _usage_record(
    record: Dict[str, Any], run_id: str, agent_id: str, task_id: str
) -> UsageRecord:
    """Build the ingestion record, with the kernel's stamps.

    Parameters
    ----------
    record : Dict[str, Any]
        A cost record that passed :func:`_shape_error`.
    run_id, agent_id, task_id : str
        The kernel's stamps.

    Returns
    -------
    UsageRecord
        The record :func:`ingest_usage_records` takes.
    """
    return UsageRecord(
        run_id=run_id,
        agent_id=agent_id,
        seq=str(record["seq"]),
        at=record["at"],
        provider=record["provider"],
        model=record["model"],
        input_tokens=record.get("input_tokens", 0),
        output_tokens=record.get("output_tokens", 0),
        cache_read_tokens=record.get("cache_read_tokens", 0),
        cache_creation_tokens=record.get("cache_creation_tokens", 0),
        reasoning_tokens=record.get("reasoning_tokens"),
        task_id=task_id,
        attribution="held",
        api=record.get("api"),
        usd=record.get("usd"),
        basis=record.get("basis", "attested"),
        priced_by=record.get("priced_by"),
        reported_by=record.get("reported_by", "agent"),
    )


def _not_recorded(reason: str) -> Dict[str, Any]:
    """Return the reply for a cost list the kernel did not record."""
    return {"recorded": False, "reason": reason}


def record_report_cost(
    state: Any,
    agent_id: str,
    task_id: str,
    cost: Any,
    accepted: bool,
) -> Dict[str, Any]:
    """Record the model calls a worker reported with its progress.

    Parameters
    ----------
    state : Any
        Marcus server state; ``cost_store`` and ``agent_project_map`` are read.
    agent_id : str
        The reporting agent, which is the agent the calls are stamped with.
    task_id : str
        The reported task, which the calls are attributed to.
    cost : Any
        The report's ``cost`` argument, as it arrived: it should be a list
        with one object per model call, and anything else is refused.
    accepted : bool
        Whether the board accepted the report. A refused report records
        nothing.

    Returns
    -------
    Dict[str, Any]
        ``{"recorded": False, "reason": ...}`` when nothing was recorded, or
        ``{"recorded": True, "run_id", "batch_id", "accepted",
        "already_present", "rejected": [{"index", "reason"}]}``, where
        ``index`` is the record's position in ``cost``. Never raises: the
        report's own outcome does not depend on its cost.
    """
    if not isinstance(cost, list):
        return _not_recorded(
            f"cost must be a list of call records, got {type(cost).__name__}"
        )
    if not accepted:
        return _not_recorded(
            "the report was refused, so its cost was not recorded; send these"
            " records again with your next report (records are deduplicated"
            " by seq, so a resend is never counted twice)"
        )
    project_id = project_for_agent(agent_id, state)
    if not project_id:
        return _not_recorded(
            f"agent {agent_id!r} is not registered to a project, so there is"
            " no project to attribute the spend to"
        )
    store = getattr(state, "cost_store", None)
    if store is None:
        return _not_recorded("this server has no cost store")

    try:
        run_id = store.get_latest_open_run_for_project(project_id) or _UNASSIGNED_RUN
        rejected: List[Dict[str, Any]] = []
        positions: List[int] = []
        records: List[UsageRecord] = []
        for index, record in enumerate(cost):
            error = _shape_error(record)
            if error is not None:
                rejected.append({"index": index, "reason": error})
                continue
            positions.append(index)
            records.append(_usage_record(record, run_id, agent_id, task_id))
        ack = ingest_usage_records(store, project_id, records)
    except Exception as error:  # noqa: BLE001 - cost never fails the report
        return _not_recorded(f"the cost store refused the batch: {error}")

    rejected.extend(
        {"index": positions[item.index], "reason": item.reason} for item in ack.rejected
    )
    rejected.sort(key=lambda item: item["index"])
    return {
        "recorded": True,
        "run_id": run_id,
        "batch_id": ack.batch_id,
        "accepted": ack.accepted,
        "already_present": ack.already_present,
        "rejected": rejected,
    }
