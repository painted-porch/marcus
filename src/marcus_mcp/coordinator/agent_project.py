"""Shared lookup for the project an agent's write should be stamped to.

ADR-0013 K6 (Accepted 2026-11-06): the agent's registration is the
only source of the project for any write; the server's active
project -- whichever project a human last switched to -- is never a
fallback. The active project is ``None`` before any project is
selected and need not be the agent's on a multi-project server, so
sourcing a write from it can file the write under the wrong project,
or drop it silently (#805).

``give_opinion`` (PR #800) and ``log_decision`` (#805, PR #813) each
built this lookup independently against ``state.agent_project_map``;
this is that lookup, factored out so every subsequent write site uses
the identical rule rather than drifting.
"""

from typing import Any, Optional


def project_for_agent(agent_id: str, state: Any) -> Optional[str]:
    """Return the project ``agent_id`` is registered to, if any.

    Parameters
    ----------
    agent_id : str
        The agent whose write is being stamped.
    state : Any
        Marcus server state. ``state.agent_project_map`` is the only
        source consulted -- ``register_agent`` refuses a registration
        without a ``project_id``, and the map is rehydrated from
        persisted registrations after a restart, so a registered
        agent is always found here.

    Returns
    -------
    Optional[str]
        The agent's registered project id, or ``None`` when the agent
        is not registered. The server's active project is
        deliberately never consulted as a fallback (ADR-0013 K6).
    """
    mapping = getattr(state, "agent_project_map", None)
    if isinstance(mapping, dict):
        mapped = mapping.get(agent_id)
        if mapped:
            return str(mapped)
    return None
