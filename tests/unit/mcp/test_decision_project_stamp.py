"""
Unit tests for a decision being filed under the agent's own project.

Marcus can host several projects on one server, and each agent registers
against exactly one of them. Decisions an agent logs are persisted to
that project's history, which is what a human reads afterwards to
understand why a project turned out the way it did.

``_persist_decision_to_history`` defaulted to the server's **active**
project, the one a human last switched to, rather than the agent's own.
On a multi-project server an agent working on project X could have its
decision filed under project Y, and on a server where no project had
been made active yet the decision was dropped with only a debug line
(#805).

``give_opinion`` was fixed for this in PR #800 and its helper,
``_project_for_agent``, already documents why the active project is the
wrong source: it is ``None`` before any project is selected and need not
be the agent's on a multi-project server. ``log_decision`` simply was
not changed with it.
"""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

pytestmark = pytest.mark.unit


class _History:
    """Records what was persisted, and under which project."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    async def record_decision(self, *args: Any, **kwargs: Any) -> None:
        """Capture a decision write."""
        self.rows.append({"args": args, "kwargs": kwargs})


def _state(
    active: Optional[str],
    agent_projects: Optional[Dict[str, str]] = None,
) -> Any:
    """Build server state with an active project and a registration map.

    Parameters
    ----------
    active : Optional[str]
        The server's active project, as ``switch_project`` would set it.
    agent_projects : Optional[Dict[str, str]]
        The agent-to-project registrations.

    Returns
    -------
    Any
        A state object with what the persistence helper reads.
    """
    return SimpleNamespace(
        current_project_id=active,
        current_project_name=active,
        agent_project_map=dict(agent_projects or {}),
        project_history_persistence=None,
        kanban_client=None,
        events=None,
        project_tasks=[],
    )


class TestTheAgentsProjectIsTheSource:
    """A decision belongs to the project its author is registered to."""

    def test_the_helper_reads_the_registration(self) -> None:
        """``_project_for_agent`` is the one source, as for opinions."""
        from src.marcus_mcp.tools.context import _project_for_agent

        state = _state(active="project-Y", agent_projects={"alpha": "project-X"})

        assert _project_for_agent("alpha", state) == "project-X"

    def test_the_active_project_is_not_a_fallback(self) -> None:
        """An unregistered caller resolves to nothing, not to the server's."""
        from src.marcus_mcp.tools.context import _project_for_agent

        state = _state(active="project-Y", agent_projects={})

        assert _project_for_agent("stranger", state) is None


class TestPersistingRequiresAnExplicitProject:
    """The helper must not quietly inherit the active project."""

    @pytest.mark.asyncio
    async def test_a_caller_must_name_the_project(self) -> None:
        """Passing no project is a programming error, not a default.

        The silent fallback is what let `log_decision` file under the
        wrong project for as long as it did, so it is gone rather than
        merely unused.
        """
        import inspect

        from src.marcus_mcp.tools.context import _persist_decision_to_history

        signature = inspect.signature(_persist_decision_to_history)
        project_param = signature.parameters["project_id"]

        assert project_param.default is inspect.Parameter.empty

    @pytest.mark.asyncio
    async def test_both_callers_pass_one(self) -> None:
        """Neither `log_decision` nor `give_opinion` relies on a default."""
        import inspect

        from src.marcus_mcp.tools import context

        source = inspect.getsource(context)
        calls = source.count("_persist_decision_to_history(")
        passes = source.count("project_id=project_id")

        # One definition plus two call sites.
        assert calls == 3
        assert passes >= 2


class TestLogDecisionFilesUnderTheAgentsProject:
    """The user-visible behaviour the issue describes."""

    @pytest.mark.asyncio
    async def test_a_decision_lands_in_the_agents_project(self) -> None:
        """Agent on X, server active on Y: the decision belongs to X."""
        from src.marcus_mcp.tools import context

        captured: Dict[str, Any] = {}

        async def _spy(logged: Any, state: Any, project_id: str) -> None:
            captured["project_id"] = project_id

        state = _state(active="project-Y", agent_projects={"alpha": "project-X"})
        state.context = SimpleNamespace(
            log_decision=_make_log_decision(),
        )

        original = context._persist_decision_to_history
        context._persist_decision_to_history = _spy  # type: ignore[assignment]
        try:
            await context.log_decision(
                agent_id="alpha",
                task_id="task-1",
                decision="I chose X because Y so Z",
                state=state,
            )
        finally:
            context._persist_decision_to_history = original  # type: ignore[assignment]

        assert captured.get("project_id") == "project-X"

    @pytest.mark.asyncio
    async def test_a_decision_is_not_dropped_when_nothing_is_active(self) -> None:
        """A fresh server has no active project; the decision still lands.

        This is the worse of the two outcomes in the issue: the decision
        was discarded with a debug-level log line and nothing reported a
        problem.
        """
        from src.marcus_mcp.tools import context

        captured: Dict[str, Any] = {}

        async def _spy(logged: Any, state: Any, project_id: str) -> None:
            captured["project_id"] = project_id

        state = _state(active=None, agent_projects={"alpha": "project-X"})
        state.context = SimpleNamespace(log_decision=_make_log_decision())

        original = context._persist_decision_to_history
        context._persist_decision_to_history = _spy  # type: ignore[assignment]
        try:
            await context.log_decision(
                agent_id="alpha",
                task_id="task-1",
                decision="I chose X because Y so Z",
                state=state,
            )
        finally:
            context._persist_decision_to_history = original  # type: ignore[assignment]

        assert captured.get("project_id") == "project-X"


def _make_log_decision() -> Any:
    """Build a stub context whose ``log_decision`` returns a decision."""

    async def _log_decision(
        agent_id: str, task_id: str, what: str, why: str, impact: str
    ) -> Any:
        return SimpleNamespace(
            decision_id="dec-1",
            agent_id=agent_id,
            task_id=task_id,
            what=what,
            why=why,
            impact=impact,
            to_dict=lambda: {"decision_id": "dec-1"},
        )

    return _log_decision
