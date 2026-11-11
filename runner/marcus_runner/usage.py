"""
Read what a run's agents spent, from the rows they wrote, while the run is going.

Two layouts are read, because two kinds of agent exist today:

* ``<state>/<agent>/<agent>/pi-sessions/**/*.jsonl``: the pi coding agent's
  own transcripts, as the Minecraft harness writes them; every usage mapping
  nested anywhere in an entry counts.
* ``<state>/<agent>/usage.jsonl``: one row per model call, ``{"at": ..,
  "model": .., "usage": {input, output, cacheRead, cacheWrite}}``, which any
  runtime can write (ADR-0013 K8: a non-Claude-Code runtime has no transcript
  to parse).

A file may be mid-write when it is read during a live run: a trailing partial
line is skipped, so this is safe to call repeatedly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple

from . import pricing

_ANTHROPIC_NAMES = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_input_tokens": "cacheRead",
    "cache_creation_input_tokens": "cacheWrite",
}


def normalize_usage(raw: Any) -> Optional[Dict[str, int]]:
    """
    Read a usage mapping in either pi's or Anthropic's field names.

    Parameters
    ----------
    raw : Any
        A mapping with ``input``/``output`` or ``input_tokens``/``output_tokens``.

    Returns
    -------
    Optional[Dict[str, int]]
        ``{input, output, cacheRead, cacheWrite}`` with every key present, or
        ``None`` if ``raw`` is not a usage mapping.
    """
    if not isinstance(raw, dict):
        return None
    mapped = {pricing.USAGE_KEYS[i]: 0 for i in range(4)}
    seen = False
    for key, value in raw.items():
        name = _ANTHROPIC_NAMES.get(key, key)
        if name in mapped and isinstance(value, int):
            mapped[name] = value
            if name in ("input", "output"):
                seen = True
    return mapped if seen else None


def _usages(entry: Any) -> Iterable[Dict[str, Any]]:
    """Yield every usage mapping nested anywhere in a transcript entry."""
    if isinstance(entry, dict):
        if isinstance(entry.get("input"), int) and isinstance(entry.get("output"), int):
            yield entry
        for value in entry.values():
            yield from _usages(value)
    elif isinstance(entry, list):
        for value in entry:
            yield from _usages(value)


def _lines(file: Path) -> Iterator[Any]:
    for line in file.read_text(errors="replace").splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def rows(state_dir: Path) -> Iterator[Tuple[str, Dict[str, int], Optional[str]]]:
    """
    Yield ``(agent, usage, model)`` for every usage row under ``state_dir``.

    Parameters
    ----------
    state_dir : Path
        The run's state directory, one subdirectory per agent.

    Yields
    ------
    Tuple[str, Dict[str, int], Optional[str]]
        The agent's name, its normalised usage, and the model the row names
        (``None`` for pi transcripts, which do not say).
    """
    for session_dir in sorted(state_dir.glob("*/*/pi-sessions")):
        agent = session_dir.parent.name
        for file in sorted(session_dir.rglob("*.jsonl")):
            for entry in _lines(file):
                for usage in _usages(entry):
                    normalized = normalize_usage(usage)
                    if normalized is not None:
                        yield agent, normalized, None
    for file in sorted(state_dir.glob("*/usage.jsonl")):
        agent = file.parent.name
        for entry in _lines(file):
            if not isinstance(entry, dict):
                continue
            normalized = normalize_usage(entry.get("usage"))
            if normalized is not None:
                model = entry.get("model")
                yield agent, normalized, str(model) if model else None


def costs(state_dir: Path, model_hint: str = "sonnet") -> Dict[str, Dict[str, Any]]:
    """
    Price each agent's usage.

    Parameters
    ----------
    state_dir : Path
        The run's state directory.
    model_hint : str
        The price list for rows that do not name their model.

    Returns
    -------
    Dict[str, Dict[str, Any]]
        Per agent: ``model_calls``, ``billed_calls`` (rows with any tokens;
        pi records a zero row for an aborted continuation), token totals,
        ``dollars`` and the models seen.
    """
    per_agent: Dict[str, Dict[str, Any]] = {}
    for agent, usage, model in rows(state_dir):
        row = per_agent.setdefault(
            agent,
            {
                "model_calls": 0,
                "billed_calls": 0,
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "dollars": 0.0,
                "models": [],
            },
        )
        row["model_calls"] += 1
        tokens = sum(usage.values())
        if tokens > 0:
            row["billed_calls"] += 1
        row["input"] += usage["input"]
        row["output"] += usage["output"]
        row["cache_read"] += usage["cacheRead"]
        row["cache_write"] += usage["cacheWrite"]
        name = model or model_hint
        row["dollars"] += pricing.price(usage, name)
        key = pricing.resolve(name)
        if key not in row["models"]:
            row["models"].append(key)
    for row in per_agent.values():
        row["dollars"] = round(row["dollars"], 4)
    return per_agent


def total_dollars(state_dir: Path, model_hint: str = "sonnet") -> float:
    """
    Sum every agent's spend: the one number a stop-line check needs.

    Parameters
    ----------
    state_dir : Path
        The run's state directory.
    model_hint : str
        The price list for rows that do not name their model.

    Returns
    -------
    float
        Dollars so far, rounded to four places.
    """
    return float(
        round(
            sum(
                float(agent["dollars"])
                for agent in costs(state_dir, model_hint).values()
            ),
            4,
        )
    )


def write_usage_row(
    path: Path,
    usage: Dict[str, Any],
    model: str,
    at: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Append one usage row in the simple layout, for runtimes that write their own.

    Parameters
    ----------
    path : Path
        ``<state>/<agent>/usage.jsonl``; parents are created.
    usage : Dict[str, Any]
        Token counts in either naming.
    model : str
        The model billed.
    at : str
        An ISO timestamp.
    extra : Optional[Dict[str, Any]]
        Any other fields to keep beside the row (a turn number, a task id).
    """
    normalized = normalize_usage(usage) or {k: 0 for k in pricing.USAGE_KEYS}
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": at, "model": model, "usage": normalized, **(extra or {})}
    with path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
