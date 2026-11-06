"""
Unit tests for ADR-0013 K6 applied to conversation-log context.

``get_project_board_context`` (``src/marcus_mcp/tools/task.py``) tags
every conversation-log line an agent's request produces (task request,
progress update, blocker report, manual unassignment) with a
project_id/project_name/board_id/board_name context block. It sourced
that block from the server's **active** project -- whichever project a
human last switched to -- rather than the calling agent's own
registration, the same defect #805 found in ``log_decision`` and K6
names as its evidence. On a multi-project server, agent A working on
project X could have its own log lines tagged with project Y.
"""

from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from src.marcus_mcp.tools.task import get_project_board_context

pytestmark = pytest.mark.unit


def _project(project_id: str, name: str, provider: str = "planka") -> Any:
    return SimpleNamespace(
        id=project_id,
        name=name,
        provider=provider,
        provider_config={"board_id": f"board-{project_id}"},
    )


def _state(
    active_id: Optional[str],
    projects: Dict[str, Any],
    agent_projects: Optional[Dict[str, str]] = None,
) -> Any:
    async def _get_active_project() -> Any:
        return projects.get(active_id) if active_id else None

    async def _get_project(project_id: str) -> Any:
        return projects.get(project_id)

    return SimpleNamespace(
        project_registry=SimpleNamespace(
            get_active_project=_get_active_project,
            get_project=_get_project,
        ),
        agent_project_map=dict(agent_projects or {}),
    )


class TestContextIsStampedFromTheAgentsOwnProject:
    @pytest.mark.asyncio
    async def test_the_agents_project_wins_over_the_active_one(self) -> None:
        """Agent on X, server active on Y: context must carry X."""
        state = _state(
            active_id="project-Y",
            projects={
                "project-X": _project("project-X", "Payments Rework"),
                "project-Y": _project("project-Y", "Dashboard Rebuild"),
            },
            agent_projects={"alpha": "project-X"},
        )

        context = await get_project_board_context(state, "alpha")

        assert context["project_id"] == "project-X"
        assert context["project_name"] == "Payments Rework"

    @pytest.mark.asyncio
    async def test_an_unregistered_agent_gets_no_project_not_the_active_one(
        self,
    ) -> None:
        """The active project must never be a silent fallback (K6)."""
        state = _state(
            active_id="project-Y",
            projects={"project-Y": _project("project-Y", "Dashboard Rebuild")},
            agent_projects={},
        )

        context = await get_project_board_context(state, "stranger")

        assert context["project_id"] is None
        assert context["project_name"] is None

    @pytest.mark.asyncio
    async def test_no_agent_id_keeps_the_pre_k6_active_project_behavior(
        self,
    ) -> None:
        """Callers with no agent in scope are unaffected by this fix."""
        state = _state(
            active_id="project-Y",
            projects={"project-Y": _project("project-Y", "Dashboard Rebuild")},
        )

        context = await get_project_board_context(state)

        assert context["project_id"] == "project-Y"


class TestEveryWriteCallSitePassesTheAgent:
    """Codex-shaped audit: every call must pass its own agent_id,

    or the fix is unreachable from any real caller."""

    def test_every_call_site_passes_a_second_argument(self) -> None:
        import inspect

        from src.marcus_mcp.tools import task

        source = inspect.getsource(task)
        bare_calls = source.count("get_project_board_context(state)")

        assert bare_calls == 0
