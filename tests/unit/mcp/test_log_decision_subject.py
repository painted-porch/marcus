"""
Unit tests for ``log_decision`` carrying an optional subject (#880).

A decision is a record of why an agent did something: "I planted saplings
because the camp ran out of logs." Larry decided on 2026-11-06 (Minecraft
deliberation layer, design attribute 11) that a decision may name the
subject it answers, so the record can show an observation and the response
to it side by side, and that a decision never counts toward anything: not
as an opinion, not as support for a candidate.

The ``Decision`` row already has ``subject`` and ``kind`` fields, and the
kernel's ``Context.log_decision`` already accepts them. What was missing is
the last metre: the MCP tool agents call took one free-text string and
could not carry a subject at all. These tests pin the three things the
change must hold to:

1. the tool accepts ``subject`` and forwards it to the implementation;
2. the implementation normalises it exactly as ``give_opinion`` does and
   passes it to the kernel, with ``kind`` left as ``"decision"``;
3. a decision with a subject is still not an opinion, so the opinion
   counts the crew agrees by are untouched.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.unit


def _state(agent_projects: Optional[Dict[str, str]] = None) -> Any:
    """Build the server state ``log_decision`` reads.

    Parameters
    ----------
    agent_projects : Optional[Dict[str, str]]
        The agent-to-project registrations.

    Returns
    -------
    Any
        A state object with a kernel spy at ``state.context``.
    """
    captured: Dict[str, Any] = {}

    async def _kernel_log_decision(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            decision_id="dec_1",
            to_dict=lambda: {"decision_id": "dec_1", **kwargs},
        )

    state = SimpleNamespace(
        current_project_id="project-X",
        current_project_name="project-X",
        agent_project_map=dict(agent_projects or {"alpha": "project-X"}),
        project_history_persistence=None,
        kanban_client=None,
        events=None,
        project_tasks=[],
        context=SimpleNamespace(log_decision=_kernel_log_decision),
    )
    state.captured = captured
    return state


async def _run_impl(state: Any, **extra: Any) -> Dict[str, Any]:
    """Call the implementation with persistence stubbed out."""
    from src.marcus_mcp.tools import context

    with patch.object(context, "_persist_decision_to_history", AsyncMock()):
        return await context.log_decision(
            agent_id="alpha",
            task_id="task-1",
            decision="I planted saplings because the camp ran out of logs. "
            "This affects the wood supply.",
            state=state,
            **extra,
        )


class TestTheImplementationCarriesTheSubject:
    """The subject reaches the kernel, normalised, as a decision."""

    @pytest.mark.asyncio
    async def test_a_subject_is_normalised_like_an_opinions(self) -> None:
        """``"Wood Shortage!"`` is stored as ``wood.shortage``.

        One normaliser for the whole board: an opinion on ``wood.shortage``
        and a decision answering it must land on the same slug or the record
        cannot put them side by side.
        """
        state = _state()

        result = await _run_impl(state, subject="Wood Shortage!")

        assert result["success"] is True, result
        assert state.captured["subject"] == "wood.shortage"

    @pytest.mark.asyncio
    async def test_the_kind_stays_decision(self) -> None:
        """A subject does not turn the row into an opinion."""
        state = _state()

        await _run_impl(state, subject="wood.shortage")

        assert state.captured.get("kind", "decision") == "decision"

    @pytest.mark.asyncio
    async def test_no_subject_stores_none(self) -> None:
        """Today's call, with no subject, is unchanged: the row has none."""
        state = _state()

        result = await _run_impl(state)

        assert result["success"] is True, result
        assert state.captured.get("subject") is None

    @pytest.mark.asyncio
    async def test_a_blank_subject_is_no_subject(self) -> None:
        """Whitespace is not a subject; it must not become an empty slug."""
        state = _state()

        await _run_impl(state, subject="   ")

        assert state.captured.get("subject") is None

    @pytest.mark.asyncio
    async def test_the_parsed_what_and_why_are_unchanged(self) -> None:
        """The subject rides beside the existing parse, not instead of it."""
        state = _state()

        await _run_impl(state, subject="wood.shortage")

        assert state.captured["what"] == "I planted saplings"
        assert state.captured["why"] == "the camp ran out of logs"


class TestTheToolForwardsTheSubject:
    """The MCP wrapper names ``subject`` so agents can send it."""

    async def _call_wrapper(self, arguments: Dict[str, Any]) -> AsyncMock:
        """Register the agent tools on a fresh app and call ``log_decision``."""
        from mcp.server.fastmcp import FastMCP

        from src.marcus_mcp.server import MarcusServer

        server: Any = MarcusServer.__new__(MarcusServer)
        app = FastMCP("test-marcus-agent")
        server._register_endpoint_tools(app, "agent")

        impl = AsyncMock(return_value={"success": True, "decision_id": "dec_1"})
        with patch("src.marcus_mcp.tools.context.log_decision", impl):
            await app.call_tool("log_decision", arguments)
        return impl

    @pytest.mark.asyncio
    async def test_subject_reaches_the_implementation(self) -> None:
        """What the agent sends is what the implementation receives."""
        impl = await self._call_wrapper(
            {
                "agent_id": "alpha",
                "task_id": "task-1",
                "decision": "I planted saplings because the camp ran out of logs.",
                "subject": "Wood Shortage!",
            }
        )

        assert impl.await_args.kwargs["subject"] == "Wood Shortage!"

    @pytest.mark.asyncio
    async def test_the_subject_is_optional(self) -> None:
        """A call without a subject still works and sends none."""
        impl = await self._call_wrapper(
            {
                "agent_id": "alpha",
                "task_id": "task-1",
                "decision": "I planted saplings because the camp ran out of logs.",
            }
        )

        assert impl.await_args.kwargs.get("subject") is None


class TestADecisionIsNeverAnOpinion:
    """The line Larry drew: a decision is a record of why, never a vote."""

    def test_a_decision_with_a_subject_is_not_counted_as_an_opinion(self) -> None:
        """The opinion grouping asks for ``kind="assessment"`` and skips the rest.

        Two agents' opinions on ``wood.shortage`` count 2; Eir's decision on
        the same subject leaves the count at 2.
        """
        from src.analysis.query_api import group_opinions_by_subject
        from src.core.context import Decision

        when = datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc)

        def _row(agent: str, kind: str) -> Decision:
            return Decision(
                decision_id=f"dec-{agent}",
                task_id=None,
                agent_id=agent,
                timestamp=when,
                what="w",
                why="y",
                impact="i",
                project_id="project-X",
                subject="wood.shortage",
                kind=kind,
            )

        rows = [
            _row("Bly", "assessment"),
            _row("Dov", "assessment"),
            _row("Eir", "decision"),
        ]

        entries = group_opinions_by_subject(rows, kind="assessment")

        assert len(entries) == 1
        assert entries[0]["subject"] == "wood.shortage"
        assert entries[0]["count"] == 2
        assert "Eir" not in entries[0]["agents"]
