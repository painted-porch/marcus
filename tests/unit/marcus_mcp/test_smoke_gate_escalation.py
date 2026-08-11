"""
Unit tests for the smoke-gate missing-verifications retry ceiling (issue #676).

When an integration task carries in-scope outcomes but the agent reports
completion without declaring ``verifications``, the product smoke gate
rejects with ``error="verifications_required_but_missing"``. Before this
fix that rejection had NO ceiling: the agent could re-send the same
incomplete report forever, the gate rejected each time, and the run
eventually froze into gridlock (snake-pr667-5: 6 identical rejections).

The fix converts the rejection into a TERMINAL escalation after a small
number of identical attempts, so the agent stops retrying and the failure
is surfaced (with the same remediation payload) instead of looping.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import TaskStatus
from src.marcus_mcp.tools.task import (
    MAX_SMOKE_MISSING_VERIFICATION_ATTEMPTS,
    _clear_smoke_attempts,
    _escalate_missing_verifications_response,
    _record_missing_verification_attempt,
    _terminalize_escalated_smoke_task,
)


def _wire_release_lease(lease_manager) -> None:
    """Make a mocked lease manager honour release_lease().

    Production code now drops leases through
    ``AssignmentLeaseManager.release_lease`` rather than deleting from
    ``active_leases`` directly, so the fencing epoch stays behind the
    same lock every other mutation takes (ADR-0012 D11). A bare
    ``MagicMock`` would accept the call and leave the dict untouched,
    which makes these tests assert against a fiction.
    """

    async def _release(task_id, reason="released"):
        return lease_manager.active_leases.pop(task_id, None) is not None

    lease_manager.release_lease = AsyncMock(side_effect=_release)


pytestmark = pytest.mark.unit


def _missing_verifications_rejection() -> dict:
    """A representative missing-verifications rejection from the smoke gate."""
    return {
        "success": False,
        "status": "smoke_verification_failed",
        "error": "verifications_required_but_missing",
        "agent_id": "agent-1",
        "task_id": "task-int",
        "blocker": "## Verifications required ...",
        "missing_outcome_ids": ["outcome_play", "outcome_restart"],
        "required_outcome_ids": ["outcome_play", "outcome_restart"],
    }


class TestMissingVerificationAttemptCounter:
    """The per-task attempt counter increments and clears."""

    def test_record_increments_per_task(self) -> None:
        _clear_smoke_attempts("t-count")
        assert _record_missing_verification_attempt("t-count") == 1
        assert _record_missing_verification_attempt("t-count") == 2
        assert _record_missing_verification_attempt("t-count") == 3

    def test_counters_are_independent_per_task(self) -> None:
        _clear_smoke_attempts("t-a")
        _clear_smoke_attempts("t-b")
        _record_missing_verification_attempt("t-a")
        _record_missing_verification_attempt("t-a")
        assert _record_missing_verification_attempt("t-b") == 1

    def test_clear_resets_count(self) -> None:
        _record_missing_verification_attempt("t-clear")
        _clear_smoke_attempts("t-clear")
        assert _record_missing_verification_attempt("t-clear") == 1


class TestEscalationResponse:
    """The terminal escalation preserves remediation but stops retries."""

    def test_marks_terminal_and_escalated(self) -> None:
        out = _escalate_missing_verifications_response(
            _missing_verifications_rejection()
        )
        assert out["terminal"] is True
        assert out["escalated"] is True
        assert out["success"] is False

    def test_uses_a_distinct_error_so_it_is_not_a_retry_signal(self) -> None:
        out = _escalate_missing_verifications_response(
            _missing_verifications_rejection()
        )
        # Must NOT keep the retryable error code, or the agent treats it as
        # "fix and retry" rather than "stop, escalated".
        assert out["error"] != "verifications_required_but_missing"
        assert out["error"] == "verifications_required_escalated"

    def test_preserves_remediation_payload(self) -> None:
        out = _escalate_missing_verifications_response(
            _missing_verifications_rejection()
        )
        assert out["missing_outcome_ids"] == ["outcome_play", "outcome_restart"]
        assert "blocker" in out
        # original is not mutated
        src = _missing_verifications_rejection()
        _escalate_missing_verifications_response(src)
        assert src["error"] == "verifications_required_but_missing"


class TestCeilingDecision:
    """Attempts at/under the ceiling stay retryable; beyond it escalates."""

    def test_first_attempts_under_ceiling(self) -> None:
        _clear_smoke_attempts("t-ceil")
        for i in range(1, MAX_SMOKE_MISSING_VERIFICATION_ATTEMPTS + 1):
            assert (
                _record_missing_verification_attempt("t-ceil")
                <= MAX_SMOKE_MISSING_VERIFICATION_ATTEMPTS
            )

    def test_attempt_beyond_ceiling_triggers_escalation(self) -> None:
        _clear_smoke_attempts("t-ceil2")
        last = 0
        for _ in range(MAX_SMOKE_MISSING_VERIFICATION_ATTEMPTS + 1):
            last = _record_missing_verification_attempt("t-ceil2")
        assert last > MAX_SMOKE_MISSING_VERIFICATION_ATTEMPTS


def _state_with_lease(task_id: str, agent_id: str) -> Mock:
    """Build a Mock state holding a lease + assignment for the task."""
    state = Mock()
    state.kanban_client = Mock()
    state.kanban_client.update_task = AsyncMock()
    state.agent_status = {agent_id: Mock(current_tasks=[task_id])}
    state.agent_tasks = {agent_id: Mock(task_id=task_id)}
    state.assignment_persistence = Mock()
    state.assignment_persistence.remove_assignment = AsyncMock()
    state.lease_manager = Mock()
    state.lease_manager.active_leases = {task_id: object()}
    _wire_release_lease(state.lease_manager)
    return state


class TestTerminalizeEscalatedTask:
    """Codex P1 (#678): escalation must terminalize the BOARD, not just the
    MCP response — else the task idles IN_PROGRESS until lease timeout."""

    @pytest.mark.asyncio
    async def test_marks_task_blocked_on_board(self) -> None:
        state = _state_with_lease("t-term", "agent-1")
        await _terminalize_escalated_smoke_task(
            state, "t-term", "agent-1", "required verifications never declared"
        )
        state.kanban_client.update_task.assert_awaited_once()
        args = state.kanban_client.update_task.await_args
        assert args.args[0] == "t-term"
        assert args.args[1]["status"] == TaskStatus.BLOCKED
        assert "blocker" in args.args[1]

    @pytest.mark.asyncio
    async def test_releases_lease_and_assignment(self) -> None:
        state = _state_with_lease("t-term2", "agent-1")
        await _terminalize_escalated_smoke_task(state, "t-term2", "agent-1", "blocker")
        # lease released, assignment removed, agent slot freed
        assert "t-term2" not in state.lease_manager.active_leases
        assert "agent-1" not in state.agent_tasks
        state.assignment_persistence.remove_assignment.assert_awaited_once_with(
            "agent-1"
        )

    @pytest.mark.asyncio
    async def test_board_failure_does_not_raise(self) -> None:
        # Best-effort: a kanban failure must not lose the escalation.
        state = _state_with_lease("t-term3", "agent-1")
        state.kanban_client.update_task = AsyncMock(side_effect=RuntimeError("boom"))
        await _terminalize_escalated_smoke_task(state, "t-term3", "agent-1", "blocker")
        # still released the lease despite the board error
        assert "t-term3" not in state.lease_manager.active_leases
