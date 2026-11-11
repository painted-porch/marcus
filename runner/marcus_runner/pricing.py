"""
One price table for every extension.

The Minecraft plumbing and the numerical verifier each carried their own
(``cost_lib.py``, 129 lines, and ``numeric_cost_lib.py``, 183 lines, 240
differing lines between them). This is the only copy.

Prices are dollars per million tokens as ``(input, output, cache_read,
cache_write)``; cache read is a tenth of input and cache write is one and a
quarter times input, which is Anthropic's published rule, with the exact
figures where a model lists one.
"""

from __future__ import annotations

import re
from typing import Dict, Mapping, Tuple

#: $/M tokens: (input, output, cache read, cache write).
PRICES: Dict[str, Tuple[float, float, float, float]] = {
    "claude-sonnet-4-5": (3.0, 15.0, 0.30, 3.75),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.50),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
}

#: Short names the Minecraft runs recorded, kept so their records price the same.
ALIASES: Dict[str, str] = {
    "sonnet": "claude-sonnet-4-5",
    "haiku": "claude-haiku-4-5",
    "sonnet-5": "claude-sonnet-5",
    "opus": "claude-opus-5",
    "opus-5": "claude-opus-5",
}

_DATE_SUFFIX = re.compile(r"-\d{8}$")

USAGE_KEYS: Tuple[str, str, str, str] = ("input", "output", "cacheRead", "cacheWrite")


def resolve(model: str) -> str:
    """
    Map a model name, alias or dated id to a price-table key.

    Parameters
    ----------
    model : str
        ``sonnet``, ``claude-sonnet-5``, ``claude-haiku-4-5-20251001``, and so on.

    Returns
    -------
    str
        A key of :data:`PRICES`.

    Raises
    ------
    KeyError
        When no price is known for the model; a run must not be priced by guess.
    """
    name = model.strip().lower()
    name = ALIASES.get(name, name)
    name = name.split("/", 1)[1] if name.startswith("anthropic/") else name
    name = _DATE_SUFFIX.sub("", name)
    name = ALIASES.get(name, name)
    if name not in PRICES:
        raise KeyError(
            f"no price for model {model!r}; add it to marcus_runner.pricing.PRICES"
        )
    return name


def price(usage: Mapping[str, int], model: str) -> float:
    """
    Price one usage row.

    Parameters
    ----------
    usage : Mapping[str, int]
        ``{input, output, cacheRead, cacheWrite}`` token counts; a missing key
        counts as zero.
    model : str
        The model the row was billed at.

    Returns
    -------
    float
        Dollars, rounded to six places.
    """
    rate_in, rate_out, rate_read, rate_write = PRICES[resolve(model)]
    dollars = (
        int(usage.get("input") or 0) * rate_in
        + int(usage.get("output") or 0) * rate_out
        + int(usage.get("cacheRead") or 0) * rate_read
        + int(usage.get("cacheWrite") or 0) * rate_write
    ) / 1_000_000
    return round(dollars, 6)
