"""
Unit tests for the validation gate's own-fault path (issue #795).

When the LLM validation gate raises on an implementation task's
completion, the fault is Marcus's, not the agent's.  Before #795 the
gate logged "Validation system error" and accepted the completion with
nothing on the response, the event log, or the board: a crashed gate
was indistinguishable from a passed one.  The policy stays
log-and-continue (the smoke gate's docstring reserves that for
programmer errors in the verification system), but it is now visible:
the completion carries ``status="validation_unavailable"`` and
``operator_needed=True``, and an ``operator_needed`` event is logged
with ``source="validation_gate"``, mirroring ``smoke_gate_unavailable``.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, List
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskAssignment, TaskStatus
from src.marcus_mcp.tools.task import report_task_progress

pytestmark = pytest.mark.unit

AGENT_ID = "agent_unicorn_3"
TASK_ID = "b27a116b"


def _make_task() -> Task:
    """Build an implementation task that the validation gate must inspect."""
    now = datetime.now(timezone.utc)
    return Task(
        id=TASK_ID,
        name="Implement Animated Digital Clock",
        description="Build the animated flip clock component",
        status=TaskStatus.IN_PROGRESS,
        priority=Priority.HIGH,
        assigned_to=AGENT_ID,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=2.0,
        labels=["build"],
    )


def _make_assignment() -> TaskAssignment:
    """Build the assignment record for the task under test."""
    return TaskAssignment(
        task_id=TASK_ID,
        task_name="Implement Animated Digital Clock",
        description="Build the animated flip clock component",
        instructions="Implement the AnimatedClock component",
        estimated_hours=2.0,
        priority=Priority.HIGH,
        dependencies=[],
        assigned_to=AGENT_ID,
        assigned_at=datetime.now(timezone.utc),
        due_date=None,
    )


def _make_state(board_tasks: List[Task]) -> Mock:
    """Build a Mock state whose board holds ``board_tasks``."""
    state = Mock()
    state.initialize_kanban = AsyncMock()
    state.kanban_client = Mock()
    state.kanban_client.project_id = "proj-1"
    state.kanban_client.get_all_tasks = AsyncMock(return_value=board_tasks)
    state.kanban_client.update_task = AsyncMock(return_value=None)
    state.kanban_client.update_task_progress = AsyncMock()
    state.kanban_client._load_workspace_state = Mock(
        return_value={"project_root": "/tmp/test-repo"}  # nosec B108
    )
    state.agent_tasks = {AGENT_ID: _make_assignment()}
    state.project_tasks = list(board_tasks)
    state.lease_manager = None
    state.agent_status = {
        AGENT_ID: Mock(skills=[], current_tasks=[], completed_tasks_count=0)
    }
    state.assignment_persistence = Mock()
    state.assignment_persistence.remove_assignment = AsyncMock()
    state.memory = None
    state.provider = "sqlite"
    state.code_analyzer = None
    state.subtask_manager = None
    state.log_event = Mock()
    return state


@contextmanager
def _gate_installed(validator: Any) -> Iterator[None]:
    """Patch the git-shaped gates out and install ``validator`` as the gate."""
    with (
        patch(
            "src.marcus_mcp.tools.task._verify_agent_has_commits",
            return_value=None,
        ),
        patch(
            "src.marcus_mcp.tools.task._merge_agent_branch_to_main",
            new_callable=AsyncMock,
            return_value={"success": True},
        ),
        patch("src.marcus_mcp.tools.task._validate_task_completion", validator),
    ):
        yield


class TestValidationGateUnavailable:
    """Test suite for a validation gate that raises instead of judging."""

    @pytest.mark.asyncio
    async def test_crashed_gate_still_completes_but_is_stamped(self) -> None:
        """A gate crash fails open, and the response says so."""
        state = _make_state([_make_task()])
        validator = AsyncMock(side_effect=AttributeError("no task_artifacts"))

        with _gate_installed(validator):
            result = await report_task_progress(
                agent_id=AGENT_ID,
                task_id=TASK_ID,
                status="completed",
                progress=100,
                message="AnimatedClock implemented",
                state=state,
            )

        assert result["success"] is True
        assert result["status"] == "validation_unavailable"
        assert result["validation_unavailable"] is True
        assert result["operator_needed"] is True
        assert "no task_artifacts" in result["validation_error"]
        state.kanban_client.update_task.assert_called()

    @pytest.mark.asyncio
    async def test_crashed_gate_fires_an_operator_needed_event(self) -> None:
        """The crash is logged as an operator_needed event from the gate."""
        state = _make_state([_make_task()])
        validator = AsyncMock(side_effect=RuntimeError("validator exploded"))

        with _gate_installed(validator):
            await report_task_progress(
                agent_id=AGENT_ID,
                task_id=TASK_ID,
                status="completed",
                progress=100,
                message="AnimatedClock implemented",
                state=state,
            )

        operator_events = [
            call.args
            for call in state.log_event.call_args_list
            if call.args and call.args[0] == "operator_needed"
        ]
        assert len(operator_events) == 1
        payload = operator_events[0][1]
        assert payload["source"] == "validation_gate"
        assert payload["task_id"] == TASK_ID
        assert payload["agent_id"] == AGENT_ID
        assert "validator exploded" in payload["reason"]

    @pytest.mark.asyncio
    async def test_passing_gate_leaves_no_unavailable_stamp(self) -> None:
        """A gate that ran and passed produces an ordinary success."""
        state = _make_state([_make_task()])
        validator = AsyncMock(return_value=Mock(passed=True, issues=[]))

        with _gate_installed(validator):
            result = await report_task_progress(
                agent_id=AGENT_ID,
                task_id=TASK_ID,
                status="completed",
                progress=100,
                message="AnimatedClock implemented",
                state=state,
            )

        assert result["success"] is True
        assert "validation_unavailable" not in result
        assert result.get("operator_needed") is not True
        assert not [
            call
            for call in state.log_event.call_args_list
            if call.args and call.args[0] == "operator_needed"
        ]
