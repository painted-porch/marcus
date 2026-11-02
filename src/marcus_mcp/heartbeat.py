"""
The heartbeat on any tool call, for every transport (issue #765).

Marcus never asks agents for explicit heartbeats. Any MCP tool call that
carries an ``agent_id`` proves the agent alive and extends its lease
(``touch_lease``), which under ADR-0012 D3 is the whole liveness signal.
The hook used to live only in the stdio dispatcher; HTTP mode serves tools
through FastMCP registrations that never reached it, so over HTTP only a
progress report extended a lease and a live, chatty agent could be
recovered as dead. :class:`HeartbeatFastMCP` puts the same hook on the
HTTP path, once, covering every registered tool.
"""

import inspect
import logging
from typing import Any, Dict, Optional, Sequence, Union

from mcp.server.fastmcp import FastMCP
from mcp.types import ContentBlock

logger = logging.getLogger(__name__)


async def touch_on_tool_call(arguments: Optional[Dict[str, Any]], state: Any) -> None:
    """Extend the calling agent's lease after a tool call, best-effort.

    Parameters
    ----------
    arguments : Optional[Dict[str, Any]]
        The tool call's arguments; only ``agent_id`` is read.
    state : Any
        Marcus server state. Nothing happens without a lease manager.

    Notes
    -----
    Failures are logged and swallowed: the agent must still receive its
    tool result, and the lease monitor remains the authority on liveness.
    """
    agent_id = arguments.get("agent_id") if arguments else None
    if not agent_id:
        return
    lease_manager = getattr(state, "lease_manager", None)
    if lease_manager:
        try:
            await lease_manager.touch_lease(agent_id)
        except (
            Exception
        ) as error:  # noqa: BLE001 - a heartbeat must never fail the call
            logger.warning(f"[heartbeat] touch_lease failed for {agent_id}: {error}")
    # ADR-0012 O1: the same call moves the registration's last_seen, the
    # eviction clock, so a live worker is never swept from the roster.
    registrations = getattr(state, "registration_persistence", None)
    if registrations is not None:
        try:
            touched = registrations.touch(agent_id)
            if inspect.isawaitable(touched):
                await touched
        except Exception as error:  # noqa: BLE001 - never fail the call
            logger.warning(
                f"[heartbeat] registration touch failed for {agent_id}: {error}"
            )


async def rehydrate_on_tool_call(state: Any) -> None:
    """Load the worker roster before a tool runs, best-effort (ADR-0012 O1).

    ``agent_status`` and ``agent_project_map`` are rebuilt from disk on
    the first tool call after a restart (the lazy path; the startup path
    never runs in HTTP mode). The server method is idempotent, so calling
    it on every tool call costs one attribute check after the first.

    Parameters
    ----------
    state : Any
        Marcus server state. Nothing happens without
        ``ensure_registrations_loaded``.
    """
    ensure = getattr(state, "ensure_registrations_loaded", None)
    if not callable(ensure):
        return
    try:
        pending = ensure()
        if inspect.isawaitable(pending):
            await pending
    except Exception as error:  # noqa: BLE001 - never fail the call
        logger.warning(f"[heartbeat] registration rehydration failed: {error}")


# FastMCP is typed in the project's environment and untyped in the pre-commit
# hook's; the ignore covers the second and is marked unused for the first.
class HeartbeatFastMCP(FastMCP):  # type: ignore[misc,unused-ignore]
    """A FastMCP whose every successful tool call is a heartbeat.

    Parameters
    ----------
    *args : Any
        Passed to :class:`FastMCP`.
    state : Any
        Marcus server state, consulted for the lease manager on each call.
    **kwargs : Any
        Passed to :class:`FastMCP`.
    """

    def __init__(self, *args: Any, state: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.marcus_state = state

    async def call_tool(
        self, name: str, arguments: Dict[str, Any]
    ) -> Union[Sequence[ContentBlock], Dict[str, Any]]:
        """Load the roster, run the tool, then record the heartbeat.

        Parameters
        ----------
        name : str
            Tool name.
        arguments : Dict[str, Any]
            Tool arguments.

        Returns
        -------
        Union[Sequence[ContentBlock], Dict[str, Any]]
            The tool's own result, unchanged. A failing tool propagates
            its error and records no heartbeat.
        """
        await rehydrate_on_tool_call(self.marcus_state)
        result: Union[Sequence[ContentBlock], Dict[str, Any]] = await super().call_tool(
            name, arguments
        )
        await touch_on_tool_call(arguments, self.marcus_state)
        return result
