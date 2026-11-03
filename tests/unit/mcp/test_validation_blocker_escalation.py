"""Unit tests for validation same-issue escalation (replaces blocker creation).

Prior behaviour: _handle_validation_failure created a BLOCKED task when the
validator returned identical issues on a second attempt.  BLOCKED tasks are
invisible to request_next_task (TODO-only) and skipped by lease recovery, so
they were permanently stuck (64 deadlocked tasks, health report 2026-04-26).

New behaviour: same-issue repeat → escalate (auto-pass through completion
pipeline) instead of blocking.  The validation_escalated flag causes the
caller to fall through to the completion path exactly like MAX_VALIDATION_RETRIES.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus

pytestmark = pytest.mark.unit


def _make_task(task_id: str = "task-1") -> Task:
    """Build a minimal task fixture."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name="Test task",
        description="...",
        status=TaskStatus.IN_PROGRESS,
        priority=Priority.MEDIUM,
        assigned_to="agent-1",
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=2.0,
        dependencies=[],
        labels=[],
    )


def _make_validation_result(issue_text: str = "Missing feature X") -> Any:
    """Build a minimal ValidationResult-like mock."""
    from src.ai.validation.validation_models import ValidationIssue, ValidationSeverity

    issue = ValidationIssue(
        severity=ValidationSeverity.MAJOR,
        issue=issue_text,
        evidence="file.py:10",
        remediation="Add feature",
        criterion="Feature X must be implemented",
    )
    result = MagicMock()
    result.passed = False
    result.issues = [issue]
    return result


def _make_state() -> Any:
    """Minimal Marcus state for _handle_validation_failure."""
    state = MagicMock()
    state.kanban_client = AsyncMock()
    state.kanban_client.update_task = AsyncMock()
    state.kanban_client.add_comment = AsyncMock()
    state.kanban_client.get_task_by_id = AsyncMock(return_value=MagicMock())
    state.agent_status = {}
    state.agent_tasks = {}
    state.assignment_persistence = AsyncMock()
    state.assignment_persistence.remove_assignment = AsyncMock()
    state.lease_manager = MagicMock()
    state.lease_manager.active_leases = {}
    state.ai_engine = AsyncMock()
    state.ai_engine.analyze_blocker = AsyncMock(return_value="Try X")
    state.project_tasks = []
    state.project_registry = None
    state.provider = "sqlite"
    state.initialize_kanban = AsyncMock()
    return state


class TestValidationEscalationOnSameIssues:
    """Repeated identical validation issues must escalate, not block."""

    @pytest.mark.asyncio
    async def test_same_issues_returns_escalated_not_blocker_created(
        self,
    ) -> None:
        """_handle_validation_failure with same issues → escalated=True, no blocker."""
        from src.marcus_mcp.tools.task import _handle_validation_failure

        task = _make_task()
        state = _make_state()
        validation_result = _make_validation_result("Missing feature X")

        # Simulate a prior attempt with the same issue fingerprint
        mock_retry_tracker = MagicMock()
        mock_retry_tracker.get_attempt_count.return_value = 1
        mock_retry_tracker.is_retry_with_same_issues.return_value = True

        with (patch("src.marcus_mcp.tools.task._retry_tracker", mock_retry_tracker),):
            response = await _handle_validation_failure(
                task, "agent-1", validation_result, state
            )

        assert response.get("escalated") is True, (
            "Same-issue repeat must return escalated=True, not blocker_created. "
            "BLOCKED tasks are permanently stuck (invisible to request_next_task)."
        )
        assert response.get("status") == "validation_escalated"
        assert (
            response.get("blocker_created", False) is False
        ), "Must NOT create a blocker — blocked tasks are never retried."

    @pytest.mark.asyncio
    async def test_report_blocker_not_called_on_same_issues(
        self,
    ) -> None:
        """report_blocker must NOT be called when validator repeats same issues."""
        from src.marcus_mcp.tools.task import _handle_validation_failure

        task = _make_task()
        state = _make_state()
        validation_result = _make_validation_result("Missing feature X")

        mock_retry_tracker = MagicMock()
        mock_retry_tracker.get_attempt_count.return_value = 1
        mock_retry_tracker.is_retry_with_same_issues.return_value = True

        with (
            patch("src.marcus_mcp.tools.task._retry_tracker", mock_retry_tracker),
            patch(
                "src.marcus_mcp.tools.task.report_blocker",
                new=AsyncMock(),
            ) as mock_blocker,
        ):
            await _handle_validation_failure(task, "agent-1", validation_result, state)

        mock_blocker.assert_not_called(), (
            "report_blocker must not be called when escalating — "
            "it would set task to BLOCKED and strand it forever."
        )

    @pytest.mark.asyncio
    async def test_first_failure_still_returns_failure_not_escalated(
        self,
    ) -> None:
        """First validation failure → plain failure response (agent can retry)."""
        from src.marcus_mcp.tools.task import _handle_validation_failure

        task = _make_task()
        state = _make_state()
        validation_result = _make_validation_result("Missing feature X")

        mock_retry_tracker = MagicMock()
        mock_retry_tracker.get_attempt_count.return_value = 0
        mock_retry_tracker.is_retry_with_same_issues.return_value = False

        with (patch("src.marcus_mcp.tools.task._retry_tracker", mock_retry_tracker),):
            response = await _handle_validation_failure(
                task, "agent-1", validation_result, state
            )

        assert response.get("status") == "validation_failed"
        assert response.get("escalated", False) is False

    @pytest.mark.asyncio
    async def test_attempt_recorded_on_escalation(self) -> None:
        """Retry tracker records the attempt even on escalation."""
        from src.marcus_mcp.tools.task import _handle_validation_failure

        task = _make_task()
        state = _make_state()
        validation_result = _make_validation_result()

        mock_retry_tracker = MagicMock()
        mock_retry_tracker.get_attempt_count.return_value = 1
        mock_retry_tracker.is_retry_with_same_issues.return_value = True

        with (patch("src.marcus_mcp.tools.task._retry_tracker", mock_retry_tracker),):
            await _handle_validation_failure(task, "agent-1", validation_result, state)

        mock_retry_tracker.record_attempt.assert_called_once_with(
            task.id, validation_result
        )


class TestEscalationPayloadStrip:
    """Escalation payload must not carry success=False into the completion response.

    report_task_progress builds its final return as:
        {"success": True, "message": "...", **escalation_payload}

    If escalation_payload contains "success": False (the raw failure_response),
    the dict merge overwrites the explicit True and callers see a failed completion
    even though the task was finalized on the board (Codex P1, PR #421).
    """

    @pytest.mark.asyncio
    async def test_escalation_payload_does_not_contain_success_key(
        self,
    ) -> None:
        """failure_response from same-issue escalation must have success stripped."""
        from src.marcus_mcp.tools.task import _handle_validation_failure

        task = _make_task()
        state = _make_state()
        validation_result = _make_validation_result()

        mock_retry_tracker = MagicMock()
        mock_retry_tracker.get_attempt_count.return_value = 1
        mock_retry_tracker.is_retry_with_same_issues.return_value = True

        with (patch("src.marcus_mcp.tools.task._retry_tracker", mock_retry_tracker),):
            response = await _handle_validation_failure(
                task, "agent-1", validation_result, state
            )

        # Simulate how report_task_progress merges the payload
        final = {
            "success": True,
            "message": "Progress updated successfully",
            **response,
        }

        assert final["success"] is True, (
            "escalation_payload must not carry success=False — it would override "
            "the explicit 'success': True in the completion response and make a "
            "finalized DONE task appear failed to the caller."
        )

    @pytest.mark.asyncio
    async def test_escalation_response_still_carries_diagnostic_fields(
        self,
    ) -> None:
        """Issues and attempt_count survive the strip — only success/message removed."""
        from src.marcus_mcp.tools.task import _handle_validation_failure

        task = _make_task()
        state = _make_state()
        validation_result = _make_validation_result("Missing feature X")

        mock_retry_tracker = MagicMock()
        mock_retry_tracker.get_attempt_count.return_value = 1
        mock_retry_tracker.is_retry_with_same_issues.return_value = True

        with (patch("src.marcus_mcp.tools.task._retry_tracker", mock_retry_tracker),):
            response = await _handle_validation_failure(
                task, "agent-1", validation_result, state
            )

        assert response.get("escalated") is True
        assert response.get("status") == "validation_escalated"
        assert "issues" in response
        assert "attempt_count" in response
