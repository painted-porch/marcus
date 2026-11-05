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

    async def append_decision(self, *args: Any, **kwargs: Any) -> None:
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


class TestPersistedHistoryUsesTheRightProjectsName:
    """Codex P2 on PR #813.

    ``project_id`` was already resolved from the agent's own project
    (#805), but ``project_name`` was still read from the SERVER's
    active project. On a multi-project server the two can differ; if
    the agent's project has no existing history file yet,
    ``append_decision`` initializes it with whatever name is passed,
    so the active project's name (or ``None``) would be stamped onto
    the agent's own project's history permanently.
    """

    @pytest.mark.asyncio
    async def test_the_name_comes_from_the_same_project_as_the_id(self) -> None:
        """Agent on X, server active on Y: history must get X's name."""
        from src.marcus_mcp.tools.context import _persist_decision_to_history

        history = _History()
        registry = SimpleNamespace(
            get_project=_make_project_lookup(
                {"project-X": "Payments Rework", "project-Y": "Dashboard Rebuild"}
            )
        )
        state = SimpleNamespace(
            current_project_id="project-Y",
            current_project_name="Dashboard Rebuild",
            project_registry=registry,
            project_history_persistence=history,
            project_tasks=[],
        )
        logged = SimpleNamespace(
            decision_id="dec-1",
            task_id="task-1",
            agent_id="alpha",
            timestamp=None,
            what="chose X",
            why="because Y",
            impact="Z",
        )

        await _persist_decision_to_history(logged, state, project_id="project-X")

        assert history.rows[0]["args"][:2] == ("project-X", "Payments Rework")

    @pytest.mark.asyncio
    async def test_no_registry_degrades_to_the_id_not_a_guess(self) -> None:
        """Without a registry, fall back to the id -- never the active
        project's name, which could belong to someone else's history."""
        from src.marcus_mcp.tools.context import _persist_decision_to_history

        history = _History()
        state = SimpleNamespace(
            current_project_id="project-Y",
            current_project_name="Dashboard Rebuild",
            project_history_persistence=history,
            project_tasks=[],
        )
        logged = SimpleNamespace(
            decision_id="dec-1",
            task_id="task-1",
            agent_id="alpha",
            timestamp=None,
            what="chose X",
            why="because Y",
            impact="Z",
        )

        await _persist_decision_to_history(logged, state, project_id="project-X")

        assert history.rows[0]["args"][:2] == ("project-X", "project-X")

    @pytest.mark.asyncio
    async def test_a_registry_lookup_failure_degrades_to_the_id(self) -> None:
        """A registry error must not stamp the active project's name either."""
        from src.marcus_mcp.tools.context import _persist_decision_to_history

        history = _History()

        async def _boom(_project_id: str) -> None:
            raise RuntimeError("registry unavailable")

        state = SimpleNamespace(
            current_project_id="project-Y",
            current_project_name="Dashboard Rebuild",
            project_registry=SimpleNamespace(get_project=_boom),
            project_history_persistence=history,
            project_tasks=[],
        )
        logged = SimpleNamespace(
            decision_id="dec-1",
            task_id="task-1",
            agent_id="alpha",
            timestamp=None,
            what="chose X",
            why="because Y",
            impact="Z",
        )

        await _persist_decision_to_history(logged, state, project_id="project-X")

        assert history.rows[0]["args"][:2] == ("project-X", "project-X")


def _make_project_lookup(names: Dict[str, str]) -> Any:
    """Build a ``get_project`` stub returning a config with just a name."""

    async def _get_project(project_id: str) -> Any:
        if project_id not in names:
            return None
        return SimpleNamespace(id=project_id, name=names[project_id])

    return _get_project


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
