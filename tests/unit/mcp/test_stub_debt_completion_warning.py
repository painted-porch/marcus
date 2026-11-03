"""
Unit tests for stub-debt warning in report_task_progress.

When an agent completes a task whose output_paths files still contain
stub markers, the success response must include stub_warnings and a
stub_warning_message so the agent knows to resolve the debt.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

pytestmark = pytest.mark.unit

# Pre-stub modules that would trigger side effects on import.
for _mod in [
    "src.experiments.live_experiment_monitor",
    "src.integrations.planka.client",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _make_task(
    task_id: str,
    output_paths: Optional[List[str]] = None,
) -> Any:
    from src.core.models import Priority, Task, TaskStatus

    # IN_PROGRESS is the realistic pre-completion status: the completion
    # pipeline marks a task DONE only after validation. report_task_progress
    # now refuses a completion on an already-DONE task before the gate, so a
    # fixture that starts DONE would be short-circuited instead of exercising
    # the stub-warning path this test covers.
    return Task(
        id=task_id,
        name="Build WeatherCard",
        description="desc",
        status=TaskStatus.IN_PROGRESS,
        priority=Priority.MEDIUM,
        assigned_to="agent-1",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=1.0,
        output_paths=output_paths or [],
    )


def _make_state(task: Any, project_root: str) -> MagicMock:
    """Minimal state that satisfies report_task_progress needs."""
    state = MagicMock()
    state.provider = "planka"
    state.project_tasks = [task]

    # Async top-level
    state.initialize_kanban = AsyncMock()

    # Kanban client — all methods used on completed path
    state.kanban_client._load_workspace_state.return_value = {
        "project_root": project_root
    }
    state.kanban_client.get_all_tasks = AsyncMock(return_value=[task])
    state.kanban_client.update_task = AsyncMock(return_value={})
    state.kanban_client.update_task_progress = AsyncMock(return_value={})
    state.kanban_client.get_task_by_id = AsyncMock(return_value=task)

    # Agent status
    agent = MagicMock()
    agent.current_tasks = [task.id]
    agent.completed_tasks_count = 0
    state.agent_status = {"agent-1": agent}

    # Task assignment — needed to pass the stale-completion guard
    assignment = MagicMock()
    assignment.task_id = task.id
    assignment.assigned_at = datetime.now(timezone.utc)
    state.agent_tasks = {"agent-1": assignment}

    # Optional components — disabled to avoid needing more mocks
    state.subtask_manager = None
    state.memory = None
    state.code_analyzer = None
    state.lease_manager = None
    state.assignment_persistence = AsyncMock()
    state.assignment_persistence.remove_assignment = AsyncMock()

    return state


class TestStubWarningInCompletion:
    """report_task_progress includes stub_warnings when output_paths have stubs."""

    @pytest.mark.asyncio
    async def test_no_stub_warning_when_output_paths_empty(
        self, tmp_path: Path
    ) -> None:
        """Tasks with no output_paths get no stub_warnings key."""
        from src.marcus_mcp.tools.task import report_task_progress

        task = _make_task("task-1", output_paths=[])
        state = _make_state(task, str(tmp_path))

        with (
            patch(
                "src.marcus_mcp.tools.task._verify_agent_has_commits",
                return_value=None,
            ),
            patch(
                "src.marcus_mcp.tools.task._merge_agent_branch_to_main",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("src.marcus_mcp.tools.task.conversation_logger"),
        ):
            state.kanban_client.update_task = AsyncMock(return_value={})
            state.kanban_client.get_task_by_id = AsyncMock(return_value=task)

            result = await report_task_progress(
                agent_id="agent-1",
                task_id="task-1",
                status="completed",
                progress=100,
                message="Done",
                state=state,
            )

        assert result["success"] is True
        assert "stub_warnings" not in result

    @pytest.mark.asyncio
    async def test_stub_warning_when_output_file_has_stub_marker(
        self, tmp_path: Path
    ) -> None:
        """Completing a task whose output file still has data-stub= triggers warning."""
        from src.marcus_mcp.tools.task import report_task_progress

        # Write the stubbed file to disk
        stub_file = tmp_path / "src" / "Card.tsx"
        stub_file.parent.mkdir(parents=True, exist_ok=True)
        stub_file.write_text('<div data-stub="Card" />', encoding="utf-8")

        task = _make_task("task-2", output_paths=["src/Card.tsx"])
        state = _make_state(task, str(tmp_path))

        with (
            patch(
                "src.marcus_mcp.tools.task._verify_agent_has_commits",
                return_value=None,
            ),
            patch(
                "src.marcus_mcp.tools.task._merge_agent_branch_to_main",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("src.marcus_mcp.tools.task.conversation_logger"),
        ):
            state.kanban_client.update_task = AsyncMock(return_value={})
            state.kanban_client.get_task_by_id = AsyncMock(return_value=task)

            result = await report_task_progress(
                agent_id="agent-1",
                task_id="task-2",
                status="completed",
                progress=100,
                message="Done",
                state=state,
            )

        assert result["success"] is True
        assert "stub_warnings" in result
        assert "src/Card.tsx" in result["stub_warnings"]
        assert "stub_warning_message" in result

    @pytest.mark.asyncio
    async def test_no_stub_warning_when_output_file_is_clean(
        self, tmp_path: Path
    ) -> None:
        """Clean output file produces no stub_warnings key."""
        from src.marcus_mcp.tools.task import report_task_progress

        clean_file = tmp_path / "src" / "Button.tsx"
        clean_file.parent.mkdir(parents=True, exist_ok=True)
        clean_file.write_text(
            "export function Button() { return <button>Click</button>; }",
            encoding="utf-8",
        )

        task = _make_task("task-3", output_paths=["src/Button.tsx"])
        state = _make_state(task, str(tmp_path))

        with (
            patch(
                "src.marcus_mcp.tools.task._verify_agent_has_commits",
                return_value=None,
            ),
            patch(
                "src.marcus_mcp.tools.task._merge_agent_branch_to_main",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("src.marcus_mcp.tools.task.conversation_logger"),
        ):
            state.kanban_client.update_task = AsyncMock(return_value={})
            state.kanban_client.get_task_by_id = AsyncMock(return_value=task)

            result = await report_task_progress(
                agent_id="agent-1",
                task_id="task-3",
                status="completed",
                progress=100,
                message="Done",
                state=state,
            )

        assert result["success"] is True
        assert "stub_warnings" not in result
