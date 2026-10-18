"""
Configuration invariants for the ADR-0012 liveness dials.

D3 splits liveness into two dials that used to be one lease:

* the **silence timeout** — how long an agent may make no MCP call before
  Marcus concludes it has died, and
* the **checkpoint cadence** — how often the session contract obliges an
  agent to commit and log a decision (D8).

D11 part 3 requires the first to be at least **twice** the second, as a
config invariant rather than a convention.

Why it has to be enforced rather than documented
------------------------------------------------
If the timeout is not comfortably longer than the cadence, an agent
obeying its checkpoint obligation exactly can still be declared dead in
the gap between two checkpoints. False recovery then stops being a rare
accident and becomes a designed-in property of the configuration — which
is precisely the state the ADR describes as the reason D11 was needed at
all ("D3's timeout (15-30 min) overlapped D8's checkpoint cadence
(~30 min), so false recovery was not rare; it was designed in").

Epoch fencing and board-mediated reconciliation (D11 parts 1 and 2) would
make false recovery *safe*; this invariant is what would keep it *rare*.

**Neither half ships today.** The fence was deferred pending D3 (see the
D11 amendment in the ADR), and this invariant is not enforced at startup
either — see below. That is deliberate and consistent: the two halves are
sized for each other, so shipping one alone leaves the survivor carrying
load it was never designed for.

Status: NOT YET ENFORCED AT STARTUP
-----------------------------------
Nothing in ``src/`` calls :func:`validate_silence_checkpoint_invariant`
today, and that is not an oversight to be fixed by wiring it up here.
**Neither dial exists as a configuration field yet.** The silence timeout
and the checkpoint cadence are introduced by D3's liveness retune —
replacing the current progressive-timeout curve and ``max_renewals`` —
which is Phase-3 work, not gate 2. ``MarcusConfig`` currently exposes
``silence_multiplier`` and a lease-duration curve, neither of which is
the quantity this rule constrains.

Validating a value that no operator can set would be theatre. This module
is the decided rule plus its tests, ready to be called from config load
the moment D3 lands the two fields it governs. The Phase-3 lease-retune
work item owns that wiring; until then the rule is enforced only against
the defaults recorded here.
"""

# Defaults from ADR-0012: checkpoint obligation at most every 20 minutes,
# silence timeout 45 minutes. 45 >= 2 x 20, with headroom.
DEFAULT_SILENCE_TIMEOUT_MINUTES: float = 45.0
DEFAULT_CHECKPOINT_CADENCE_MINUTES: float = 20.0

# The ratio D11 part 3 fixes. Named rather than inlined so the error
# message and the test can both cite the same source.
MIN_SILENCE_TO_CHECKPOINT_RATIO: float = 2.0


def validate_silence_checkpoint_invariant(
    silence_timeout_minutes: float,
    checkpoint_cadence_minutes: float,
) -> None:
    """
    Assert the D11 part 3 relationship between the two liveness dials.

    Parameters
    ----------
    silence_timeout_minutes : float
        How long an agent may be silent before Marcus recovers its task.
    checkpoint_cadence_minutes : float
        The maximum interval the session contract allows between an
        agent's checkpoints. Must be positive.

    Raises
    ------
    ValueError
        If the cadence is not positive, or if the timeout is less than
        ``MIN_SILENCE_TO_CHECKPOINT_RATIO`` times the cadence. The
        message names both configured values and the smallest timeout
        that would be accepted, so an operator can act on it without
        reading this module.

    Examples
    --------
    >>> validate_silence_checkpoint_invariant(45.0, 20.0)
    >>> validate_silence_checkpoint_invariant(30.0, 20.0)
    Traceback (most recent call last):
        ...
    ValueError: silence timeout (30.0 min) must be at least 2x the ...
    """
    if checkpoint_cadence_minutes <= 0:
        raise ValueError(
            "checkpoint cadence must be positive; got "
            f"{checkpoint_cadence_minutes}. A zero or negative cadence "
            "makes the D11 part 3 invariant meaningless — there is no "
            "checkpoint interval for the silence timeout to be larger "
            "than."
        )

    minimum = MIN_SILENCE_TO_CHECKPOINT_RATIO * checkpoint_cadence_minutes
    if silence_timeout_minutes < minimum:
        raise ValueError(
            f"silence timeout ({silence_timeout_minutes} min) must be at "
            f"least {MIN_SILENCE_TO_CHECKPOINT_RATIO:g}x the checkpoint "
            f"cadence ({checkpoint_cadence_minutes} min) — i.e. at least "
            f"{minimum:g} min (ADR-0012 D11 part 3). With these values an "
            "agent that checkpoints exactly on schedule can still be "
            "declared dead between two checkpoints, which makes false "
            "recovery designed-in rather than rare. Raise the timeout to "
            f"{minimum:g} or lower the checkpoint cadence to "
            f"{silence_timeout_minutes / MIN_SILENCE_TO_CHECKPOINT_RATIO:g}."
        )
