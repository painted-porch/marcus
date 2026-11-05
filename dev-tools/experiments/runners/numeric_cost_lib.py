"""Shared cost-parsing logic for the numeric-verifier condition A/C runs.

Reads real accumulated spend from Claude Code's own session transcripts,
the same files condition A's writer and self-check agents already write
while running. A stop-line check that estimates cost from case counts
instead of reading what was actually billed is exactly the gap that let
a Minecraft run reach $11.33 against an approved $6 (#815): nobody read
live cost during the run. This module reads the real number instead.

Separated from the stop policy (``numeric_cost_watchdog.py``) the same
way Minecraft's ``cost_lib.py`` and ``cost_watchdog.py`` are: one parses,
one decides. Not a reuse of either Minecraft file -- the schema, the
price table, and the runner shape all differ, named below.

Schema difference from Minecraft's ``pi`` harness: a Claude Code
transcript line nests usage at ``message.usage`` with keys
``input_tokens``, ``output_tokens``, ``cache_read_input_tokens``,
``cache_creation_input_tokens`` -- not the flat ``input``/``output``/
``cacheRead``/``cacheWrite`` keys ``pi`` writes.

Price-table difference: this project runs Opus 5 (``$5``/``$25`` per
MTok input/output, cache read ``$0.50``, cache write ``$6.25``), which
Minecraft's sonnet/haiku-only table does not carry.

Runner-shape difference: Minecraft's agents run in parallel Docker
containers, so its watchdog is a separate process polling and killing
containers from outside. Condition A is a single sequential loop
issuing one ``claude -p`` call at a time, so the check belongs inline,
before each next case or round -- see ``numeric_cost_watchdog.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

#: (input, output, cache_read, cache_write) dollars per MTok.
PriceTable = Tuple[float, float, float, float]

#: $/MTok for Opus 5: input, output, cache read, cache write.
#: Source: the claude-api skill's pricing table, read 2026-11-05.
OPUS_5_PRICES: PriceTable = (5.00, 25.00, 0.50, 6.25)

#: $/MTok for Haiku 4.5. Same source and date as OPUS_5_PRICES.
#: Roughly a fifth of Opus's input/output rate; pricing a Haiku
#: transcript under the Opus table overstates its cost by that
#: factor, which is the bug this table exists to prevent.
HAIKU_4_5_PRICES: PriceTable = (1.00, 5.00, 0.10, 1.25)

#: Model id (as passed to ``claude --model``) to its price table.
#: A caller names the model explicitly; there is no silent default
#: that happens to match whichever model actually produced the
#: transcript being priced.
MODEL_PRICES = {
    "claude-opus-5": OPUS_5_PRICES,
    "claude-haiku-4-5": HAIKU_4_5_PRICES,
}


def prices_for_model(model: str) -> PriceTable:
    """Look up the price table for a model name.

    Parameters
    ----------
    model : str
        A model id as passed to ``claude --model``.

    Returns
    -------
    PriceTable
        That model's ``(input, output, cache_read, cache_write)``
        dollars-per-MTok rates.

    Raises
    ------
    KeyError
        When the model has no price table on record. This is a hard
        failure by design: guessing a price table for an unlisted
        model risks exactly the under- or over-pricing bug this
        function exists to rule out.
    """
    if model not in MODEL_PRICES:
        raise KeyError(
            f"no price table for {model!r}; add one to MODEL_PRICES "
            f"in numeric_cost_lib.py before pricing its transcripts"
        )
    return MODEL_PRICES[model]


def _usage_blocks(path: Path) -> Iterable[Dict[str, Any]]:
    """Yield every usage block in one transcript file.

    Parameters
    ----------
    path : Path
        A Claude Code session transcript (``*.jsonl``).

    Yields
    ------
    Dict[str, Any]
        Each line's ``message.usage`` mapping, when present. A trailing
        partial line from a transcript still being written is skipped,
        the same tolerance a post-run report needs, so this is safe to
        call while a session is in progress.
    """
    if not path.exists():
        return
    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = (
            entry.get("message", {}).get("usage") if isinstance(entry, dict) else None
        )
        if isinstance(usage, dict):
            yield usage


def session_cost(
    transcript_path: Path, prices: PriceTable = OPUS_5_PRICES
) -> Dict[str, Any]:
    """Price one session transcript's accumulated token usage.

    Parameters
    ----------
    transcript_path : Path
        The ``*.jsonl`` transcript to read.
    prices : tuple
        ``(input, output, cache_read, cache_write)`` dollars per MTok.

    Returns
    -------
    Dict[str, Any]
        ``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
        ``cache_write_tokens``, and ``dollars`` (rounded to 4 places).
    """
    rate_in, rate_out, rate_read, rate_write = prices
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for usage in _usage_blocks(transcript_path):
        totals["input"] += int(usage.get("input_tokens") or 0)
        totals["output"] += int(usage.get("output_tokens") or 0)
        totals["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
        totals["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
    dollars = (
        totals["input"] * rate_in
        + totals["output"] * rate_out
        + totals["cache_read"] * rate_read
        + totals["cache_write"] * rate_write
    ) / 1_000_000
    return {
        "input_tokens": totals["input"],
        "output_tokens": totals["output"],
        "cache_read_tokens": totals["cache_read"],
        "cache_write_tokens": totals["cache_write"],
        "dollars": round(dollars, 4),
    }


def total_dollars(
    transcript_paths: Iterable[Path], prices: PriceTable = OPUS_5_PRICES
) -> float:
    """Sum accumulated cost across every transcript given so far.

    This is the one number a stop-line check needs: the approved
    budget covers the whole run, not one case or one agent.

    Parameters
    ----------
    transcript_paths : Iterable[Path]
        Every transcript written so far in this run.
    prices : tuple
        ``(input, output, cache_read, cache_write)`` dollars per MTok.

    Returns
    -------
    float
        Total dollars spent so far, across every transcript given.
    """
    return float(
        round(sum(session_cost(p, prices)["dollars"] for p in transcript_paths), 4)
    )
