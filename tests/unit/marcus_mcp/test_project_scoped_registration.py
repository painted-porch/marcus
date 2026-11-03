"""
Unit tests for project-scoped agent registration (fix for GH-388).

Root cause of dashboard-v88 cross-contamination: Marcus is a singleton MCP
server. When create_project is called, kanban_client.project_id is set
server-wide. Agents from earlier experiments (v86, v87) that are still alive
and polling request_next_task see the NEW project's tasks — because
kanban_client.project_id now points at the new project.

Fix: register_agent now requires a project_id parameter. The server stores
agent_id → project_id in agent_project_map. request_next_task uses
_scope_tasks_to_project to filter candidate tasks to only those belonging to
the agent's registered project, preventing cross-project task theft.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.agent import register_agent

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str,
    name: str,
    project_id: str | None = "project-alpha",
    status: TaskStatus = TaskStatus.TODO,
) -> Task:
    """Build a minimal Task for test fixtures."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description=f"Description for {name}",
        status=status,
        priority=Priority.MEDIUM,
        labels=[],
        dependencies=[],
        estimated_hours=2.0,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        project_id=project_id,
    )


def _make_state() -> Mock:
    """Build a minimal Mock server state with agent_project_map."""
    state = Mock()
    state.agent_status = {}
    state.agent_project_map = {}
    state.log_event = Mock()
    return state


# ---------------------------------------------------------------------------
# register_agent — project_id storage
# ---------------------------------------------------------------------------


class TestRegisterAgentProjectScope:
    """register_agent must store project_id in agent_project_map."""

    @pytest.mark.asyncio
    async def test_stores_project_id_in_agent_project_map(self) -> None:
        """Registering an agent stores its project_id for later task scoping."""
        state = _make_state()

        with (
            patch("src.marcus_mcp.tools.agent.conversation_logger"),
            patch("src.marcus_mcp.tools.agent.log_thinking"),
            patch("src.marcus_mcp.tools.agent.log_agent_event"),
        ):
            result = await register_agent(
                agent_id="agent_unicorn_1",
                name="Unicorn 1",
                role="Backend Developer",
                skills=["python"],
                project_id="project-alpha",
                state=state,
            )

        assert result["success"] is True
        assert state.agent_project_map["agent_unicorn_1"] == "project-alpha"

    @pytest.mark.asyncio
    async def test_different_agents_store_different_project_ids(self) -> None:
        """Multiple agents on different projects have isolated mappings."""
        state = _make_state()

        with (
            patch("src.marcus_mcp.tools.agent.conversation_logger"),
            patch("src.marcus_mcp.tools.agent.log_thinking"),
            patch("src.marcus_mcp.tools.agent.log_agent_event"),
        ):
            await register_agent(
                agent_id="agent_a",
                name="Agent A",
                role="Dev",
                skills=[],
                project_id="project-alpha",
                state=state,
            )
            await register_agent(
                agent_id="agent_b",
                name="Agent B",
                role="Dev",
                skills=[],
                project_id="project-beta",
                state=state,
            )

        assert state.agent_project_map["agent_a"] == "project-alpha"
        assert state.agent_project_map["agent_b"] == "project-beta"

    @pytest.mark.asyncio
    async def test_re_registration_updates_project_id(self) -> None:
        """Re-registering an agent with a new project_id updates the mapping."""
        state = _make_state()
        state.agent_project_map = {"agent_unicorn_1": "old-project"}

        with (
            patch("src.marcus_mcp.tools.agent.conversation_logger"),
            patch("src.marcus_mcp.tools.agent.log_thinking"),
            patch("src.marcus_mcp.tools.agent.log_agent_event"),
        ):
            await register_agent(
                agent_id="agent_unicorn_1",
                name="Unicorn 1",
                role="Dev",
                skills=[],
                project_id="new-project",
                state=state,
            )

        assert state.agent_project_map["agent_unicorn_1"] == "new-project"

    @pytest.mark.asyncio
    async def test_returns_project_id_in_response(self) -> None:
        """Registration response includes the registered project_id."""
        state = _make_state()

        with (
            patch("src.marcus_mcp.tools.agent.conversation_logger"),
            patch("src.marcus_mcp.tools.agent.log_thinking"),
            patch("src.marcus_mcp.tools.agent.log_agent_event"),
        ):
            result = await register_agent(
                agent_id="agent_unicorn_1",
                name="Unicorn 1",
                role="Dev",
                skills=[],
                project_id="project-gamma",
                state=state,
            )

        assert result["project_id"] == "project-gamma"

    @pytest.mark.asyncio
    async def test_registration_without_project_id_is_rejected(self) -> None:
        """
        Registration without project_id returns success=False.

        Agents are ephemeral — an agent that doesn't know its project has no
        business registering. No silent fallback, no snapshot.
        """
        state = _make_state()

        with (
            patch("src.marcus_mcp.tools.agent.conversation_logger"),
            patch("src.marcus_mcp.tools.agent.log_thinking"),
            patch("src.marcus_mcp.tools.agent.log_agent_event"),
        ):
            result = await register_agent(
                agent_id="agent_unicorn_1",
                name="Unicorn 1",
                role="Dev",
                skills=[],
                # project_id omitted — must be rejected
                state=state,
            )

        assert result["success"] is False
        assert "project_id" in result["error"]
        assert "agent_unicorn_1" not in state.agent_project_map


# ---------------------------------------------------------------------------
# _scope_tasks_to_project — core filtering helper
# ---------------------------------------------------------------------------


class TestScopeTasksToProject:
    """
    _scope_tasks_to_project filters a task list to a given project_id.
    This is the core primitive that prevents cross-project task theft.
    """

    def test_filters_out_other_project_tasks(self) -> None:
        """Tasks from other projects are excluded."""
        from src.marcus_mcp.tools.task import _scope_tasks_to_project

        own = _make_task("t1", "Own Task", project_id="project-alpha")
        stolen = _make_task("t2", "Stolen Task", project_id="project-beta")

        result = _scope_tasks_to_project([own, stolen], "project-alpha")

        ids = {t.id for t in result}
        assert "t1" in ids
        assert "t2" not in ids

    def test_keeps_tasks_matching_project_id(self) -> None:
        """Tasks matching the project_id are all retained."""
        from src.marcus_mcp.tools.task import _scope_tasks_to_project

        t1 = _make_task("t1", "Task A", project_id="project-alpha")
        t2 = _make_task("t2", "Task B", project_id="project-alpha")
        other = _make_task("t3", "Task C", project_id="project-beta")

        result = _scope_tasks_to_project([t1, t2, other], "project-alpha")

        ids = {t.id for t in result}
        assert ids == {"t1", "t2"}

    def test_none_project_id_raises(self) -> None:
        """
        No project_id → ValueError.

        Agents are ephemeral (one project, then terminated — GH-389).
        A missing project_id is a misconfiguration that should fail loudly
        rather than silently returning no tasks.
        """
        from src.marcus_mcp.tools.task import _scope_tasks_to_project

        t1 = _make_task("t1", "Task A", project_id="project-alpha")

        with pytest.raises(ValueError, match="no project_id"):
            _scope_tasks_to_project([t1], "")

    def test_empty_string_project_id_raises(self) -> None:
        """Empty string project_id is also a misconfiguration — raises."""
        from src.marcus_mcp.tools.task import _scope_tasks_to_project

        with pytest.raises(ValueError):
            _scope_tasks_to_project([], "")

    def test_tasks_without_project_id_included(self) -> None:
        """Legacy tasks with project_id=None are always included."""
        from src.marcus_mcp.tools.task import _scope_tasks_to_project

        legacy = _make_task("t1", "Legacy Task", project_id=None)
        own = _make_task("t2", "Own Task", project_id="project-alpha")
        other = _make_task("t3", "Other Project", project_id="project-beta")

        result = _scope_tasks_to_project([legacy, own, other], "project-alpha")

        ids = {t.id for t in result}
        assert "t1" in ids, "Legacy tasks (project_id=None) must be included"
        assert "t2" in ids
        assert "t3" not in ids

    def test_empty_list_returns_empty(self) -> None:
        """Empty task list → empty result."""
        from src.marcus_mcp.tools.task import _scope_tasks_to_project

        assert _scope_tasks_to_project([], "project-alpha") == []

    def test_returns_new_list_not_mutating_input(self) -> None:
        """Result is a new list, not a mutation of the input."""
        from src.marcus_mcp.tools.task import _scope_tasks_to_project

        tasks = [
            _make_task("t1", "Own", project_id="project-alpha"),
            _make_task("t2", "Other", project_id="project-beta"),
        ]
        original_len = len(tasks)

        result = _scope_tasks_to_project(tasks, "project-alpha")

        assert len(tasks) == original_len  # input unchanged
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Integration: _find_optimal_task_original_logic uses scoping
# ---------------------------------------------------------------------------


class TestTaskSelectionUsesProjectScoping:
    """
    _find_optimal_task_original_logic must call _scope_tasks_to_project
    with the agent's registered project_id before selecting a task.
    """

    @pytest.mark.asyncio
    async def test_scope_called_with_agent_project_id(self) -> None:
        """_scope_tasks_to_project is called with the agent's project_id."""
        from src.marcus_mcp.tools import task as task_module

        state = Mock()
        state.agent_project_map = {"agent_unicorn_1": "project-alpha"}
        state.project_tasks = []
        state.agent_tasks = {}
        state.tasks_being_assigned = set()
        state.agent_status = {}  # no agent → early exit
        state.project_state = None
        state.ai_engine = None

        import asyncio

        state.assignment_lock = asyncio.Lock()

        with patch.object(
            task_module,
            "_scope_tasks_to_project",
            wraps=task_module._scope_tasks_to_project,
        ) as mock_scope:
            # No agent registered → returns None early
            result = await task_module._find_optimal_task_original_logic(
                agent_id="agent_unicorn_1", state=state
            )

        # Even though result is None (no agent), scope was called with correct id
        mock_scope.assert_called_once_with([], "project-alpha")

    @pytest.mark.asyncio
    async def test_unregistered_agent_raises_value_error(self) -> None:
        """
        Agent not in agent_project_map raises ValueError.

        Agents are ephemeral — calling request_next_task without a registered
        project_id is a misconfiguration that must fail loudly, not silently
        return an empty task list.
        """
        from src.marcus_mcp.tools import task as task_module

        t1 = _make_task("t1", "Task A", project_id="project-alpha")

        state = Mock()
        state.agent_project_map = {}  # agent has no registered project
        state.project_tasks = [t1]
        state.agent_tasks = {}
        state.tasks_being_assigned = set()
        state.agent_status = {}
        state.project_state = None
        state.ai_engine = None

        import asyncio

        state.assignment_lock = asyncio.Lock()

        with pytest.raises(ValueError, match="no project_id"):
            await task_module._find_optimal_task_original_logic(
                agent_id="agent_unicorn_1", state=state
            )
