"""
Unit tests for advisory vs terminal blockers (issue #719).

``report_blocker`` was designed as a help channel: Marcus analyses the
obstacle, returns concrete suggestions, and the agent keeps working (the
worker prompt's ERROR_RECOVERY says "Don't panic or abandon the task…
Continue working"). The implementation instead marked the task BLOCKED
and released the agent's assignment and lease in the same call — so the
agent received advice at the exact moment it lost the task, and (being
ephemeral) exited. One agent's dead end killed a lane permanently, with
zero retries, short-circuiting the lease-recovery → circuit-breaker
ladder that already exists for genuine failure.

The fix makes ``severity`` mean what it says: low/medium are advisory
(keep the task), high is terminal (hand it back), with a ceiling so an
agent cannot loop asking for help forever.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import Task, TaskStatus

pytestmark = pytest.mark.unit


def _make_task(task_id: str, status: TaskStatus, assigned_to: str = "") -> Task:
    """Create a minimal Task object for testing."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.status = status
    task.assigned_to = assigned_to
    task.name = f"Task {task_id}"
    task.labels = []
    task.dependencies = []
    return task


def _make_state(agent_id: str, task_id: str) -> MagicMock:
    """Create a mock Marcus state with the agent holding the task."""
    state = MagicMock()
    state.initialize_kanban = AsyncMock()
    state.provider = "sqlite"

    task = _make_task(task_id, TaskStatus.IN_PROGRESS, assigned_to=agent_id)
    state.kanban_client = MagicMock()
    state.kanban_client.get_task_by_id = AsyncMock(return_value=task)
    state.kanban_client.update_task = AsyncMock()
    state.kanban_client.add_comment = AsyncMock()

    agent = MagicMock()
    agent.name = agent_id
    agent.current_tasks = [task]
    state.agent_status = {agent_id: agent}

    state.ai_engine = MagicMock()
    state.ai_engine.analyze_blocker = AsyncMock(return_value="Try pip install -e .")

    lease = MagicMock()
    lease.agent_id = agent_id
    state.lease_manager = MagicMock()
    state.lease_manager.active_leases = {task_id: lease}

    assignment = MagicMock()
    assignment.task_id = task_id
    state.agent_tasks = {agent_id: assignment}

    state.assignment_persistence = AsyncMock()
    state.assignment_persistence.remove_assignment = AsyncMock()

    state.memory = None
    state.project_state = MagicMock()
    return state


class TestAdvisoryBlockerKeepsTheTask:
    """low/medium severity = "I need help", not "take this away"."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("severity", ["low", "medium"])
    async def test_advisory_blocker_does_not_block_the_task(
        self, severity: str
    ) -> None:
        """The board must not be flipped to BLOCKED for an advisory blocker."""
        from src.marcus_mcp.tools.task import clear_blocker_attempts, report_blocker

        clear_blocker_attempts("task-1")
        state = _make_state("agent-1", "task-1")

        result = await report_blocker(
            agent_id="agent-1",
            task_id="task-1",
            blocker_description="pytest import error, unsure of package layout",
            severity=severity,
            state=state,
        )

        assert result["success"] is True
        assert result["advisory"] is True
        for call in state.kanban_client.update_task.await_args_list:
            assert call.args[1].get("status") != TaskStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_advisory_blocker_keeps_assignment_and_lease(self) -> None:
        """The agent must still hold the task after asking for help."""
        from src.marcus_mcp.tools.task import clear_blocker_attempts, report_blocker

        clear_blocker_attempts("task-1")
        state = _make_state("agent-1", "task-1")

        await report_blocker(
            agent_id="agent-1",
            task_id="task-1",
            blocker_description="transient port conflict",
            severity="medium",
            state=state,
        )

        assert "agent-1" in state.agent_tasks
        assert "task-1" in state.lease_manager.active_leases
        state.assignment_persistence.remove_assignment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_advisory_response_tells_agent_it_still_owns_the_task(self) -> None:
        """The response must be unambiguous, or the agent will exit anyway."""
        from src.marcus_mcp.tools.task import clear_blocker_attempts, report_blocker

        clear_blocker_attempts("task-1")
        state = _make_state("agent-1", "task-1")

        result = await report_blocker(
            agent_id="agent-1",
            task_id="task-1",
            blocker_description="transient port conflict",
            severity="medium",
            state=state,
        )

        message = result["message"].lower()
        assert "still" in message and "task-1" in result["message"]
        assert result["suggestions"] == "Try pip install -e ."

    @pytest.mark.asyncio
    async def test_advisory_blocker_is_recorded_on_the_card(self) -> None:
        """Audit trail: every blocker is commented, advisory or not."""
        from src.marcus_mcp.tools.task import clear_blocker_attempts, report_blocker

        clear_blocker_attempts("task-1")
        state = _make_state("agent-1", "task-1")

        await report_blocker(
            agent_id="agent-1",
            task_id="task-1",
            blocker_description="transient port conflict",
            severity="medium",
            state=state,
        )

        state.kanban_client.add_comment.assert_awaited_once()
        comment = state.kanban_client.add_comment.await_args.args[1]
        assert "transient port conflict" in comment
        assert "ADVISORY" in comment


class TestTerminalBlockerUnchanged:
    """high severity keeps the pre-#719 behavior: hand the task back."""

    @pytest.mark.asyncio
    async def test_high_severity_blocks_and_releases(self) -> None:
        """A terminal blocker still marks BLOCKED and frees the agent."""
        from src.marcus_mcp.tools.task import clear_blocker_attempts, report_blocker

        clear_blocker_attempts("task-1")
        state = _make_state("agent-1", "task-1")

        result = await report_blocker(
            agent_id="agent-1",
            task_id="task-1",
            blocker_description="needs a paid API key we do not have",
            severity="high",
            state=state,
            skip_ai_analysis=True,
        )

        assert result["success"] is True
        assert result.get("advisory") is False
        blocked_writes = [
            c
            for c in state.kanban_client.update_task.await_args_list
            if c.args[1].get("status") == TaskStatus.BLOCKED
        ]
        assert len(blocked_writes) == 1
        assert "agent-1" not in state.agent_tasks
        assert "task-1" not in state.lease_manager.active_leases


class TestAdvisoryCeiling:
    """An agent cannot loop asking for help forever."""

    @pytest.mark.asyncio
    async def test_ceiling_escalates_to_terminal(self) -> None:
        """After MAX_ADVISORY_BLOCKER_ATTEMPTS, the next one is terminal."""
        from src.marcus_mcp.tools.task import (
            MAX_ADVISORY_BLOCKER_ATTEMPTS,
            clear_blocker_attempts,
            report_blocker,
        )

        clear_blocker_attempts("task-1")
        state = _make_state("agent-1", "task-1")

        for _ in range(MAX_ADVISORY_BLOCKER_ATTEMPTS):
            result = await report_blocker(
                agent_id="agent-1",
                task_id="task-1",
                blocker_description="still stuck",
                severity="medium",
                state=state,
            )
            assert result["advisory"] is True

        final = await report_blocker(
            agent_id="agent-1",
            task_id="task-1",
            blocker_description="still stuck",
            severity="medium",
            state=state,
        )

        assert final["advisory"] is False
        assert "agent-1" not in state.agent_tasks

    @pytest.mark.asyncio
    async def test_counter_is_per_task(self) -> None:
        """Advisory attempts on one task must not penalise another."""
        from src.marcus_mcp.tools.task import (
            _record_blocker_attempt,
            clear_blocker_attempts,
        )

        clear_blocker_attempts("task-a")
        clear_blocker_attempts("task-b")
        assert _record_blocker_attempt("task-a") == 1
        assert _record_blocker_attempt("task-a") == 2
        assert _record_blocker_attempt("task-b") == 1

    @pytest.mark.asyncio
    async def test_clear_resets_the_counter(self) -> None:
        """Recovery/reassignment gives the next agent a fresh budget."""
        from src.marcus_mcp.tools.task import (
            _record_blocker_attempt,
            clear_blocker_attempts,
        )

        clear_blocker_attempts("task-c")
        _record_blocker_attempt("task-c")
        _record_blocker_attempt("task-c")
        clear_blocker_attempts("task-c")
        assert _record_blocker_attempt("task-c") == 1
