"""
Unit tests for ADR-0012 D11 part 2: preserve, don't discard.

A stale-epoch completion is never applied and never thrown away. The late
agent's evidence is preserved and Marcus opens a **board-visible
reconciliation card** whose contract is: compare the two attempts, keep
the verified better one or merge them, record the decision.

Why a card rather than a rule inside Marcus
-------------------------------------------
Deciding which of two implementations is better is implementation
judgement, and under the Multi-Agency Proclamation's Invariant #2 that
belongs to an agent working a card — not to Marcus core. Marcus's job is
to notice the collision, preserve both sides, and make the conflict
visible. This reuses the D2 integrator-card pattern.

Interaction with the #667 Fix 2 heuristic
-----------------------------------------
The pre-existing guard accepts a late completion when the task looks
*uncontested* — no replacement lease, no other agent's assignment, no
in-flight assignment. That heuristic reads a **deleted** lease as
"uncontested", which is exactly what a recovered task looks like. Left
alone it would wave through the very reports the epoch fence exists to
catch. The epoch verdict therefore takes precedence when the agent
supplied a token; the heuristic still governs epoch-less reports, which
is what keeps the rollout lenient.
"""

from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.assignment_lease import AssignmentLeaseManager
from src.core.assignment_persistence import AssignmentPersistence

pytestmark = pytest.mark.unit


@pytest.fixture
def kanban_client() -> Mock:
    """
    Create a kanban client that records created cards.

    Returns
    -------
    Mock
        Client exposing ``created`` — the list of task_data dicts passed
        to ``create_task``.
    """
    client = Mock()
    client.created: List[Dict[str, Any]] = []

    async def _create_task(task_data: Dict[str, Any]):
        client.created.append(task_data)
        created = Mock()
        created.id = f"recon-{len(client.created)}"
        created.name = task_data.get("name", "")
        return created

    client.create_task = AsyncMock(side_effect=_create_task)
    client.update_task = AsyncMock()
    return client


@pytest.fixture
def lease_manager(tmp_path, kanban_client: Mock) -> AssignmentLeaseManager:
    """
    Create a lease manager over isolated persistence.

    Returns
    -------
    AssignmentLeaseManager
        Manager wired to the recording kanban client.
    """
    return AssignmentLeaseManager(
        kanban_client, AssignmentPersistence(tmp_path / "assignments")
    )


class TestReconciliationCardContent:
    """The card must carry enough for an agent to settle the conflict."""

    @pytest.mark.asyncio
    async def test_card_is_created_on_the_board(
        self, kanban_client: Mock, lease_manager: AssignmentLeaseManager
    ) -> None:
        """A fenced report produces exactly one board card."""
        from src.marcus_mcp.tools.task import open_reconciliation_card

        state = Mock()
        state.kanban_client = kanban_client

        await open_reconciliation_card(
            state=state,
            task_id="task-1",
            task_name="Build the API",
            late_agent_id="agent-1",
            late_epoch=1,
            current_epoch=2,
            message="Finished the endpoint",
        )

        assert len(kanban_client.created) == 1

    @pytest.mark.asyncio
    async def test_card_names_both_attempts(
        self, kanban_client: Mock, lease_manager: AssignmentLeaseManager
    ) -> None:
        """Both agents' branches are identified so neither is lost."""
        from src.marcus_mcp.tools.task import open_reconciliation_card

        state = Mock()
        state.kanban_client = kanban_client

        await open_reconciliation_card(
            state=state,
            task_id="task-1",
            task_name="Build the API",
            late_agent_id="agent-1",
            late_epoch=1,
            current_epoch=2,
            message="Finished the endpoint",
        )

        card = kanban_client.created[0]
        body = card["description"]
        assert "agent-1" in body
        assert "marcus/agent-1" in body
        assert "task-1" in body

    @pytest.mark.asyncio
    async def test_card_records_both_epochs(
        self, kanban_client: Mock, lease_manager: AssignmentLeaseManager
    ) -> None:
        """The audit trail shows exactly which claim was superseded."""
        from src.marcus_mcp.tools.task import open_reconciliation_card

        state = Mock()
        state.kanban_client = kanban_client

        await open_reconciliation_card(
            state=state,
            task_id="task-1",
            task_name="Build the API",
            late_agent_id="agent-1",
            late_epoch=3,
            current_epoch=7,
            message="Finished the endpoint",
        )

        body = kanban_client.created[0]["description"]
        assert "3" in body
        assert "7" in body

    @pytest.mark.asyncio
    async def test_card_states_the_work_is_preserved(
        self, kanban_client: Mock, lease_manager: AssignmentLeaseManager
    ) -> None:
        """The card must not read as a failure notice.

        The whole point of D11 part 2 is that nothing is discarded; a card
        that implies otherwise invites an agent to redo work that already
        exists.
        """
        from src.marcus_mcp.tools.task import open_reconciliation_card

        state = Mock()
        state.kanban_client = kanban_client

        await open_reconciliation_card(
            state=state,
            task_id="task-1",
            task_name="Build the API",
            late_agent_id="agent-1",
            late_epoch=1,
            current_epoch=2,
            message="Finished the endpoint",
        )

        body = kanban_client.created[0]["description"].lower()
        assert "preserved" in body
        assert "log_decision" in body

    @pytest.mark.asyncio
    async def test_card_is_labelled_for_discovery(
        self, kanban_client: Mock, lease_manager: AssignmentLeaseManager
    ) -> None:
        """The card carries a label so it can be found and counted."""
        from src.marcus_mcp.tools.task import open_reconciliation_card

        state = Mock()
        state.kanban_client = kanban_client

        await open_reconciliation_card(
            state=state,
            task_id="task-1",
            task_name="Build the API",
            late_agent_id="agent-1",
            late_epoch=1,
            current_epoch=2,
            message="Finished the endpoint",
        )

        assert "reconciliation" in kanban_client.created[0]["labels"]

    @pytest.mark.asyncio
    async def test_card_creation_failure_does_not_raise(
        self, lease_manager: AssignmentLeaseManager
    ) -> None:
        """A board outage must not turn into a crashed report path.

        The completion is already fenced; failing to open the card is bad
        but must be logged rather than propagated, or a board hiccup takes
        down the whole progress-reporting path.
        """
        from src.marcus_mcp.tools.task import open_reconciliation_card

        state = Mock()
        state.kanban_client = Mock()
        state.kanban_client.create_task = AsyncMock(
            side_effect=RuntimeError("board unreachable")
        )

        result = await open_reconciliation_card(
            state=state,
            task_id="task-1",
            task_name="Build the API",
            late_agent_id="agent-1",
            late_epoch=1,
            current_epoch=2,
            message="Finished the endpoint",
        )

        assert result is None


class TestEpochVerdictBeatsTheUncontestedHeuristic:
    """The #667 heuristic must not wave through a fenced report."""

    @pytest.mark.asyncio
    async def test_recovered_task_looks_uncontested_but_is_still_fenced(
        self, lease_manager: AssignmentLeaseManager
    ) -> None:
        """A released lease reads as 'uncontested' — the trap this closes.

        After recovery releases the lease and before a replacement claims
        it, all three #667 signals are clear. Without the epoch taking
        precedence, the displaced agent's completion would be accepted.
        """
        from src.marcus_mcp.tools.task import should_fence_stale_epoch

        first = await lease_manager.create_lease("task-1", "agent-1")
        await lease_manager.release_lease("task-1", reason="lease_expired")
        await lease_manager.create_lease("task-1", "agent-2")

        # The displaced agent still carries its original token.
        assert (
            await should_fence_stale_epoch(lease_manager, "task-1", first.lease_epoch)
            is True
        )

    @pytest.mark.asyncio
    async def test_epochless_report_is_left_to_the_heuristic(
        self, lease_manager: AssignmentLeaseManager
    ) -> None:
        """Lenient rollout: no token means the old behaviour applies."""
        from src.marcus_mcp.tools.task import should_fence_stale_epoch

        await lease_manager.create_lease("task-1", "agent-1")
        await lease_manager.release_lease("task-1", reason="lease_expired")
        await lease_manager.create_lease("task-1", "agent-2")

        assert await should_fence_stale_epoch(lease_manager, "task-1", None) is False
