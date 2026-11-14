"""
The run's spend ledger: one price table, one stop line, checked before every call.

Every model call in this extension goes through :class:`Ledger`. It writes
the call's usage row in the toolkit's layout (``<state>/<agent>/usage.jsonl``)
so ``marcus_toolkit.watchdog`` can read the same rows, and it refuses the
next call once accumulated spend reaches the approved line. The price table
is the toolkit's plus the DeepInfra row the toolkit does not carry yet.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from marcus_toolkit import pricing, usage

#: Dollars per million tokens: (input, output, cache read, cache write).
EXTRA_PRICES: dict[str, tuple[float, float, float, float]] = {
    # DeepInfra's standard tier for the original V4-Flash weights, 2026-11-14.
    "deepseek-ai/deepseek-v4-flash": (0.09, 0.18, 0.09, 0.09),
    "deepseek-ai/deepseek-v4-flash-0731": (0.09, 0.18, 0.09, 0.09),
}

#: The rehearsal's scripted models, priced at zero BY NAME. An unknown model
#: still raises, so a real model can never be priced at zero by accident.
FREE_MODELS: frozenset[str] = frozenset({"scripted-checker", "scripted", "fake-model"})


class StopLineReached(RuntimeError):
    """The approved spend line has been reached; no further model call may run."""


def resolve_price(model: str) -> tuple[float, float, float, float]:
    """
    Return the price tuple for a model, from this table or the toolkit's.

    Parameters
    ----------
    model : str
        A model id as the client reports it.

    Returns
    -------
    Tuple[float, float, float, float]
        Dollars per million tokens.

    Raises
    ------
    KeyError
        When neither table prices the model; a run is never priced by guess.
    """
    key = model.strip().lower()
    if key in EXTRA_PRICES:
        return EXTRA_PRICES[key]
    if key in FREE_MODELS:
        return (0.0, 0.0, 0.0, 0.0)
    priced: tuple[float, float, float, float] = pricing.PRICES[pricing.resolve(model)]
    return priced


def price_usage(tokens: Mapping[str, int], model: str) -> float:
    """
    Price one usage row.

    Parameters
    ----------
    tokens : Mapping[str, int]
        ``input``, ``output``, ``cacheRead``, ``cacheWrite`` counts.
    model : str
        The model billed.

    Returns
    -------
    float
        Dollars.
    """
    inp, out, cread, cwrite = resolve_price(model)
    return (
        int(tokens.get("input", 0)) * inp
        + int(tokens.get("output", 0)) * out
        + int(tokens.get("cacheRead", 0)) * cread
        + int(tokens.get("cacheWrite", 0)) * cwrite
    ) / 1_000_000


class Ledger:
    """
    The run's spend record and stop line.

    Parameters
    ----------
    state_dir : Path
        The run's ``state/`` directory; rows go to ``<state>/<agent>/usage.jsonl``.
    stop_line : float
        Dollars. A call is refused when accumulated spend is at or above it.
    """

    def __init__(self, state_dir: Path, stop_line: float) -> None:
        self.state_dir = Path(state_dir)
        self.stop_line = float(stop_line)

    def total(self) -> float:
        """
        Sum every usage row under the state directory at today's prices.

        Returns
        -------
        float
            Dollars spent so far.
        """
        if not self.state_dir.is_dir():
            return 0.0
        dollars = 0.0
        for _agent, tokens, model in usage.rows(self.state_dir):
            dollars += price_usage(tokens, model or "")
        return dollars

    def check(self) -> float:
        """
        Refuse the next call if the line is reached.

        Returns
        -------
        float
            The spend so far, when below the line.

        Raises
        ------
        StopLineReached
            When spend is at or above the line.
        """
        spent = self.total()
        if spent >= self.stop_line:
            raise StopLineReached(
                f"spend ${spent:.4f} has reached the stop line ${self.stop_line:.2f}"
            )
        return spent

    def record(
        self,
        agent: str,
        tokens: Mapping[str, Any],
        model: str,
        extra: Mapping[str, Any] | None = None,
    ) -> float:
        """
        Append one usage row and return its price.

        Parameters
        ----------
        agent : str
            The process that made the call (``adviser``, ``checker-C`` ...).
        tokens : Mapping[str, Any]
            Token counts in either naming the toolkit accepts.
        model : str
            The model billed.
        extra : Optional[Mapping[str, Any]]
            Fields kept beside the row (a chain id, an arm).

        Returns
        -------
        float
            Dollars for this row.
        """
        normalized = usage.normalize_usage(dict(tokens)) or {
            k: 0 for k in pricing.USAGE_KEYS
        }
        usage.write_usage_row(
            self.state_dir / agent / "usage.jsonl",
            normalized,
            model,
            datetime.now(timezone.utc).isoformat(),
            dict(extra or {}),
        )
        return price_usage(normalized, model)
