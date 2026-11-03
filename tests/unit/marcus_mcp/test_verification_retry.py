"""Unit tests for bounded verification retry and the declined outcome.

A verify task that finds a wrong figure has to do something about it.
Marcus re-opens the task that authored the claim, hands it the named
failures, and bounds how many times that may happen. At the bound the
case is declined rather than quietly shipped.

These tests pin that behaviour, which is what lets Marcus stand in
for a workflow's own retry loop.
"""

from typing import Any, Dict, List

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.verification_retry import (
    MAX_VERIFICATION_ROUNDS,
    apply_verification_outcome,
    verification_round,
)


def make_task(
    task_id: str,
    task_type: str,
    status: TaskStatus = TaskStatus.TODO,
    dependencies: List[str] | None = None,
    inputs: Dict[str, Any] | None = None,
) -> Task:
    """Build a task for the retry tests.

    Parameters
    ----------
    task_id : str
        Identifier.
    task_type : str
        One of the system-of-record task types.
    status : TaskStatus
        Starting status.
    dependencies : List[str], optional
        Task ids this one depends on.
    inputs : Dict[str, Any], optional
        Typed payload the board wrote.

    Returns
    -------
    Task
        The constructed task.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=f"{task_type} {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        dependencies=dependencies or [],
        labels=[],
        task_type=task_type,
        inputs=inputs or {},
    )


class FakeKanban:
    """Minimal kanban double recording the updates Marcus requests."""

    def __init__(self) -> None:
        """Initialize the recorder."""
        self.updates: List[tuple] = []

    async def update_task(self, task_id: str, data: Dict[str, Any]) -> None:
        """Record an update.

        Parameters
        ----------
        task_id : str
            Task being updated.
        data : Dict[str, Any]
            Fields to write.
        """
        self.updates.append((task_id, data))


class FakeState:
    """Server-state double holding tasks and a kanban recorder."""

    def __init__(self, tasks: List[Task]) -> None:
        """Initialize with the board's tasks.

        Parameters
        ----------
        tasks : List[Task]
            Tasks on the board.
        """
        self.project_tasks = tasks
        self.kanban_client = FakeKanban()


@pytest.fixture
def case():
    """Build a four-task case: find, verify, approve, synthesize.

    Returns
    -------
    Tuple[FakeState, Task, Task, Task, Task]
        The state and the four tasks.
    """
    find = make_task("t-find", "find", status=TaskStatus.DONE)
    find.source_context = {
        "evidence": {"claims_path": "case/claims.json", "report": "r", "raw": "x"}
    }
    verify = make_task(
        "t-verify",
        "verify",
        status=TaskStatus.DONE,
        dependencies=["t-find"],
        inputs={
            "staged_dir": "case/eval_1",
            "claims_path": "case/claims.json",
            "report": "r",
        },
    )
    approve = make_task("t-approve", "approve", dependencies=["t-verify"])
    synth = make_task("t-synth", "synthesize", dependencies=["t-approve"])
    state = FakeState([find, verify, approve, synth])
    return state, find, verify, approve, synth


PASS = {"verdict": "verified", "failed_claims": []}
FAIL = {
    "verdict": "failed",
    "failed_claims": [{"claim_id": "c3", "detail": "stated 23, recomputed 22.08"}],
}


class TestPassingVerification:
    """A clean verdict leaves the chain alone."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_verified_outcome_takes_no_action(self, case):
        """Nothing is re-opened when every claim passed."""
        state, find, verify, _, _ = case
        assert await apply_verification_outcome(verify, PASS, state) is None
        assert find.status is TaskStatus.DONE

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_non_verify_task_is_ignored(self, case):
        """Only a verify task drives this path."""
        state, find, _, _, _ = case
        assert await apply_verification_outcome(find, FAIL, state) is None


class TestRetry:
    """A failed verdict re-opens the author with the named failures."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_author_is_reopened(self, case):
        """The find task goes back to TODO and loses its assignee."""
        state, find, verify, _, _ = case
        find.assigned_to = "writer-1"
        result = await apply_verification_outcome(verify, FAIL, state)
        assert result["action"] == "retry"
        assert find.status is TaskStatus.TODO
        assert find.assigned_to is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_failures_are_handed_to_the_author(self, case):
        """The writer is told exactly which claims failed."""
        state, find, verify, _, _ = case
        await apply_verification_outcome(verify, FAIL, state)
        assert find.inputs["failed_claims"][0]["claim_id"] == "c3"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_verifier_is_reopened_too(self, case):
        """The check must run again on the rewritten report."""
        state, _, verify, _, _ = case
        await apply_verification_outcome(verify, FAIL, state)
        assert verify.status is TaskStatus.TODO

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_stale_claim_is_cleared_from_the_verifier(self, case):
        """A retry must not re-check the previous round's claim.

        The handoff lets existing keys win, so a verify task that kept
        its old inputs would check the superseded claim and pass.
        """
        state, _, verify, _, _ = case
        await apply_verification_outcome(verify, FAIL, state)
        assert "claims_path" not in verify.inputs
        assert verify.inputs["staged_dir"] == "case/eval_1"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_round_counter_increments(self, case):
        """Each retry is counted on the record."""
        state, find, verify, _, _ = case
        await apply_verification_outcome(verify, FAIL, state)
        assert verification_round(find) == 1
        find.status = TaskStatus.DONE
        verify.status = TaskStatus.DONE
        await apply_verification_outcome(verify, FAIL, state)
        assert verification_round(find) == 2

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_retry_is_written_to_the_board(self, case):
        """The re-open survives a restart."""
        state, _, verify, _, _ = case
        await apply_verification_outcome(verify, FAIL, state)
        written = {tid for tid, _ in state.kanban_client.updates}
        assert {"t-find", "t-verify"} <= written


class TestDecline:
    """At the bound the case is declined, not shipped."""

    async def _exhaust(self, state, find, verify):
        """Run the loop until the bound is reached.

        Returns
        -------
        Dict[str, Any]
            The final outcome.
        """
        result = None
        for _ in range(MAX_VERIFICATION_ROUNDS + 1):
            find.status = TaskStatus.DONE
            verify.status = TaskStatus.DONE
            result = await apply_verification_outcome(verify, FAIL, state)
        return result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_case_declines_at_the_bound(self, case):
        """The outcome is a decline once the rounds are used up."""
        state, find, verify, _, _ = case
        result = await self._exhaust(state, find, verify)
        assert result["action"] == "declined"
        assert result["rounds"] == MAX_VERIFICATION_ROUNDS

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_declined_case_does_not_reopen_the_author(self, case):
        """A declined case stops retrying."""
        state, find, verify, _, _ = case
        await self._exhaust(state, find, verify)
        assert find.status is not TaskStatus.TODO

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_downstream_tasks_are_blocked(self, case):
        """Nothing downstream of a declined verification runs."""
        state, find, verify, approve, synth = case
        await self._exhaust(state, find, verify)
        assert approve.status is TaskStatus.BLOCKED
        assert synth.status is TaskStatus.BLOCKED

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_declined_reason_names_the_failing_claims(self, case):
        """The record says why the case was declined."""
        state, find, verify, _, _ = case
        result = await self._exhaust(state, find, verify)
        assert "c3" in str(result["failed_claims"])

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_decline_is_written_to_the_board(self, case):
        """The declined outcome is recorded, not just returned."""
        state, find, verify, _, _ = case
        await self._exhaust(state, find, verify)
        payloads = [d for _, d in state.kanban_client.updates]
        assert any("declined" in str(p) for p in payloads)
