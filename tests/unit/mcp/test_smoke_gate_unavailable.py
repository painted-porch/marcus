"""
Unit tests for _run_product_smoke_gate when the gate itself cannot run.

Three paths leave the gate unable to start: the workspace state cannot be
loaded, it carries no project_root, or project_root is not a directory.
None of them is the agent's fault, and none may pass (Codex P2-2: ``None``
is the "verification passed" sentinel). Since the 2026-11-02 calculator
run they also may not be blamed on the agent: the old response told the
agent to "fix the workspace state", which it cannot, and the per-task
instructions told it to file a blocker, so two high blockers spent two of
the lane's three repair requeues on a server misconfiguration. The gate
now PARKS the task: the completion is refused with status
``smoke_gate_unavailable``, the task stays IN_PROGRESS with the lease
intact, the agent is told to wait and re-report, and an operator is
alerted. Under Invariant #2 (v2) verification is Marcus's job, so a gate
Marcus cannot run is Marcus's fault.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Import the private function under test.
# We reach into the module to grab the private coroutine directly.
# ---------------------------------------------------------------------------
from src.marcus_mcp.tools.task import _run_product_smoke_gate  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------


def _make_task(task_id: str = "task-42") -> Any:
    """Return a minimal Task-like stub."""
    task = Mock()
    task.id = task_id
    return task


def _make_state(ws_state: Any = None, ws_raises: Any = None) -> Any:
    """Return a minimal state stub with a mock kanban_client.

    Parameters
    ----------
    ws_state : Any
        Dict returned by `_load_workspace_state()`, or None.
    ws_raises : Any
        If set, `_load_workspace_state()` raises this exception instead.
    """
    state = Mock()
    kanban = Mock()
    if ws_raises is not None:
        kanban._load_workspace_state = Mock(side_effect=ws_raises)
    else:
        kanban._load_workspace_state = Mock(return_value=ws_state)
    state.kanban_client = kanban
    return state


def _make_verify_result(success: bool) -> Any:
    """Return a minimal VerifyResult-like stub."""
    result = Mock()
    result.success = success
    result.steps = []
    result.failure_summary = "command exited non-zero"
    result.blocker_message = "Fix the command."
    result.to_dict = Mock(return_value={})
    return result


# ---------------------------------------------------------------------------
# Tests: _gate_unavailable paths
# ---------------------------------------------------------------------------


class TestSmokeGateUnavailable:
    """Tests for the P2-2 hard-rejection paths in _run_product_smoke_gate."""

    @pytest.mark.asyncio
    async def test_rejects_when_workspace_state_load_raises(self) -> None:
        """
        Verify a rejection dict is returned when workspace state load fails.

        Previously this path returned None (pass). After the P2-2 fix it
        must return a rejection with error='smoke_gate_unavailable'.
        """
        task = _make_task("task-ws-err")
        state = _make_state(ws_raises=RuntimeError("db unavailable"))

        result = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-1",
            state=state,
            start_command="npm start",
            readiness_probe=None,
        )

        assert result is not None, "Must not return None (pass) on ws load failure"
        assert result["success"] is False
        assert result["error"] == "smoke_gate_unavailable"
        assert result["status"] == "smoke_gate_unavailable"
        assert "workspace state load failed" in result["failure_summary"]
        assert result["task_id"] == "task-ws-err"

    @pytest.mark.asyncio
    async def test_rejects_when_project_root_missing(self) -> None:
        """
        Verify a rejection dict is returned when project_root is absent.

        workspace state loads successfully but contains no project_root key.
        Previously returned None; now must reject.
        """
        task = _make_task("task-no-root")
        state = _make_state(ws_state={"other_key": "value"})

        result = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-2",
            state=state,
            start_command="python -m app",
            readiness_probe=None,
        )

        assert result is not None, "Must not return None when project_root absent"
        assert result["success"] is False
        assert result["error"] == "smoke_gate_unavailable"
        assert "project_root not found" in result["failure_summary"]

    @pytest.mark.asyncio
    async def test_rejects_when_project_root_not_a_directory(
        self, tmp_path: Path
    ) -> None:
        """
        Verify a rejection dict is returned when project_root is not a directory.

        project_root in workspace state points to a non-existent path.
        Previously returned None; now must reject.
        """
        task = _make_task("task-bad-dir")
        nonexistent = str(tmp_path / "does_not_exist")
        state = _make_state(ws_state={"project_root": nonexistent})

        result = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-3",
            state=state,
            start_command="cargo run",
            readiness_probe=None,
        )

        assert result is not None, "Must not return None when project_root not a dir"
        assert result["success"] is False
        assert result["error"] == "smoke_gate_unavailable"
        assert "not a directory" in result["failure_summary"]

    @pytest.mark.asyncio
    async def test_returns_none_when_verification_passes(self, tmp_path: Path) -> None:
        """
        Verify the function returns None (pass sentinel) on successful verification.

        This is the golden-path test: valid project_root + passing
        verify_deliverable → None.
        """
        task = _make_task("task-ok")
        state = _make_state(ws_state={"project_root": str(tmp_path)})
        passing_result = _make_verify_result(success=True)

        with patch(
            "src.integrations.product_smoke.verify_deliverable",
            new_callable=AsyncMock,
            return_value=passing_result,
        ):
            result = await _run_product_smoke_gate(
                task=task,
                agent_id="agent-4",
                state=state,
                start_command="echo ok",
                readiness_probe=None,
            )

        assert result is None, "Passing verification must return None"

    @pytest.mark.asyncio
    async def test_rejection_includes_task_and_agent_ids(self) -> None:
        """
        Verify task_id and agent_id appear in the rejection payload.

        Agents need these fields to correlate the rejection with their work.
        """
        task = _make_task("task-id-check")
        state = _make_state(ws_state={})  # no project_root

        result = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-id-check",
            state=state,
            start_command="yarn dev",
            readiness_probe=None,
        )

        assert result is not None
        assert result["task_id"] == "task-id-check"
        assert result["agent_id"] == "agent-id-check"

    @pytest.mark.asyncio
    async def test_unavailable_gate_parks_the_task_and_alerts_an_operator(
        self,
    ) -> None:
        """The agent keeps the task, waits, re-reports; it never files a blocker.

        The response names the operator action (the registry key), says the
        fault is Marcus's, and carries a retry interval; an operator_needed
        event is logged on the server.
        """
        task = _make_task("task-parked")
        state = _make_state(ws_state=None)  # _load_workspace_state returns None
        state.log_event = Mock()

        result = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-5",
            state=state,
            start_command="make serve",
            readiness_probe=None,
        )

        assert result is not None
        assert result["status"] == "smoke_gate_unavailable"
        assert result["operator_needed"] is True
        assert result["retry_after_seconds"] > 0
        assert "should_exit" not in result
        text = result["message"].lower()
        assert "not a problem with your work" in text
        assert "do not file a blocker" in text
        assert "keep the task" in text
        assert "project_root" in result["operator_action"]
        state.log_event.assert_called_once()
        assert state.log_event.call_args.args[0] == "operator_needed"
        assert state.log_event.call_args.args[1]["task_id"] == "task-parked"

    @pytest.mark.asyncio
    async def test_parking_tolerates_a_state_without_log_event(self) -> None:
        """A bare state object still gets the parked response."""
        task = _make_task("task-bare")
        state = _make_state(ws_state={})
        del state.log_event

        result = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-6",
            state=state,
            start_command="make serve",
            readiness_probe=None,
        )

        assert result is not None
        assert result["status"] == "smoke_gate_unavailable"
