"""
Unit tests for principals and vendors at registration (issue #737, step 2).

An agent registers with a ``vendor`` (e.g. perplexity, google, openai)
and a ``principal`` ("agent" or "human"), stored on ``WorkerStatus``.
Eligibility (step 3) reads both: a verify task is never offered to the
claim author's vendor, and an approve task is offered only to a human
principal.  These tests pin the defaults, the storage, the rejection of
unknown principal values, and the stdio tool schema declaration.
"""

from typing import Any
from unittest.mock import Mock, patch

import pytest

from src.core.models import WorkerStatus
from src.marcus_mcp.handlers import get_tool_definitions
from src.marcus_mcp.tools.agent import register_agent

pytestmark = pytest.mark.unit


def _make_state() -> Mock:
    """Build a minimal Mock server state for registration."""
    state = Mock()
    state.agent_status = {}
    state.agent_project_map = {}
    state.log_event = Mock()
    return state


async def _register(state: Mock, **overrides: Any) -> Any:
    """Call register_agent with sane defaults and logging patched out."""
    kwargs: dict[str, Any] = {
        "agent_id": "finder-pplx-1",
        "name": "Perplexity finder",
        "role": "finder",
        "skills": ["find"],
        "project_id": "tow-pilot",
        "state": state,
    }
    kwargs.update(overrides)
    with (
        patch("src.marcus_mcp.tools.agent.conversation_logger"),
        patch("src.marcus_mcp.tools.agent.log_thinking"),
        patch("src.marcus_mcp.tools.agent.log_agent_event"),
        patch(
            "src.experiments.live_experiment_monitor.get_active_monitor",
            return_value=None,
        ),
    ):
        return await register_agent(**kwargs)


class TestWorkerStatusPrincipalFields:
    """Test suite for the new WorkerStatus model fields."""

    def test_worker_status_defaults(self) -> None:
        """WorkerStatus built without the new fields gets safe defaults.

        Every existing caller constructs WorkerStatus without vendor or
        principal; the defaults must keep them working.
        """
        status = WorkerStatus(
            worker_id="dev-001",
            name="Alice",
            role="Backend Developer",
            email=None,
            current_tasks=[],
            completed_tasks_count=0,
            capacity=40,
            skills=["python"],
            availability={},
        )

        assert status.vendor == ""
        assert status.principal == "agent"


class TestRegisterAgentPrincipals:
    """Test suite for vendor and principal at registration."""

    @pytest.mark.asyncio
    async def test_agent_principal_and_vendor_are_stored(self) -> None:
        """An agent principal registers with its vendor recorded."""
        state = _make_state()

        result = await _register(state, vendor="perplexity", principal="agent")

        assert result["success"] is True
        stored = state.agent_status["finder-pplx-1"]
        assert stored.vendor == "perplexity"
        assert stored.principal == "agent"

    @pytest.mark.asyncio
    async def test_human_principal_is_stored(self) -> None:
        """A human principal registers the same way an agent does."""
        state = _make_state()

        result = await _register(
            state,
            agent_id="larry",
            name="Larry",
            role="reviewer",
            skills=[],
            vendor="",
            principal="human",
        )

        assert result["success"] is True
        assert state.agent_status["larry"].principal == "human"

    @pytest.mark.asyncio
    async def test_defaults_are_agent_with_no_vendor(self) -> None:
        """Registering without the new arguments stays backward compatible."""
        state = _make_state()

        result = await _register(state)

        assert result["success"] is True
        stored = state.agent_status["finder-pplx-1"]
        assert stored.vendor == ""
        assert stored.principal == "agent"

    @pytest.mark.asyncio
    async def test_unknown_principal_is_rejected(self) -> None:
        """An unknown principal value is refused with the valid values named."""
        state = _make_state()

        result = await _register(state, principal="robot")

        assert result["success"] is False
        assert "principal" in result["error"]
        assert "agent" in result["error"]
        assert "human" in result["error"]

    @pytest.mark.asyncio
    async def test_unknown_principal_leaves_no_ghost_registration(self) -> None:
        """A rejected registration mutates no state (atomic, like GH-388)."""
        state = _make_state()

        await _register(state, principal="robot")

        assert "finder-pplx-1" not in state.agent_status
        assert "finder-pplx-1" not in state.agent_project_map


class TestRegisterAgentToolSchema:
    """Test suite for the stdio tool schema declaration."""

    def test_schema_declares_vendor_and_principal(self) -> None:
        """The register_agent input schema declares the new fields.

        The stdio dispatcher builds tools from get_tool_definitions;
        without the declaration, MCP clients cannot pass the fields.
        """
        tools = {t.name: t for t in get_tool_definitions("agent")}
        properties = tools["register_agent"].inputSchema["properties"]

        assert "vendor" in properties
        assert "principal" in properties
        assert properties["principal"]["enum"] == ["agent", "human"]
