"""
The ADR-0012 D11 verification scenario (PRE-3 gate 2's acceptance test).

Quoted from the ADR:

    A Phase-3 verification scenario is added: falsely-recover a live
    session, let both report, assert exactly one completion, zero
    discarded work, one reconciliation card in the audit trail.

This module implements that scenario against the real lease manager and
the real fencing decision. It is the single test that says whether gate 2
achieved what it set out to achieve, so it asserts the three outcomes the
ADR names — not the implementation that produces them.

The scenario
------------
1. Agent A claims a task and is issued epoch N.
2. Agent A goes quiet — deep work, no MCP calls — and trips the silence
   timeout. Marcus concludes it died and releases the lease.
3. Agent B claims the same task and is issued epoch N+1.
4. Agent A, which was alive the whole time, finishes and reports 100%
   carrying epoch N.
5. Agent B also finishes and reports 100% carrying epoch N+1.

Exactly one of those completions may be applied. Neither agent's work may
be thrown away. The collision must be visible on the board.
"""

from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.assignment_lease import AssignmentLeaseManager
from src.core.assignment_persistence import AssignmentPersistence
from src.marcus_mcp.tools.task import (
    open_reconciliation_card,
    should_fence_stale_epoch,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def board() -> Mock:
    """
    Create a kanban double that records every card it is asked to create.

    Returns
    -------
    Mock
        Client exposing ``created`` — the task_data dicts passed to
        ``create_task``.
    """
    client = Mock()
    client.created: List[Dict[str, Any]] = []

    async def _create_task(task_data: Dict[str, Any]):
        client.created.append(task_data)
        made = Mock()
        made.id = f"recon-{len(client.created)}"
        return made

    client.create_task = AsyncMock(side_effect=_create_task)
    client.update_task = AsyncMock()
    return client


@pytest.fixture
def manager(tmp_path, board: Mock) -> AssignmentLeaseManager:
    """
    Create a lease manager over isolated persistence.

    Returns
    -------
    AssignmentLeaseManager
        Manager wired to the recording board double.
    """
    return AssignmentLeaseManager(
        board, AssignmentPersistence(tmp_path / "assignments")
    )


class TestD11VerificationScenario:
    """Falsely recover a live session; both report; assert the three outcomes."""

    @pytest.mark.asyncio
    async def test_exactly_one_completion_is_applied(
        self, manager: AssignmentLeaseManager, board: Mock
    ) -> None:
        """Two live agents finish one card; only one completion lands."""
        # 1. Agent A claims.
        lease_a = await manager.create_lease("task-1", "agent-a")

        # 2. Agent A goes quiet; Marcus falsely concludes it died.
        await manager.release_lease("task-1", reason="lease_expired")

        # 3. Agent B claims the same card.
        lease_b = await manager.create_lease("task-1", "agent-b")

        # 4 & 5. Both report completion, each carrying its own epoch.
        a_fenced = await should_fence_stale_epoch(
            manager, "task-1", lease_a.lease_epoch
        )
        b_fenced = await should_fence_stale_epoch(
            manager, "task-1", lease_b.lease_epoch
        )

        applied = [
            agent
            for agent, fenced in (("agent-a", a_fenced), ("agent-b", b_fenced))
            if not fenced
        ]
        assert applied == [
            "agent-b"
        ], f"expected exactly one completion (the live holder), got {applied}"

    @pytest.mark.asyncio
    async def test_zero_discarded_work(
        self, manager: AssignmentLeaseManager, board: Mock
    ) -> None:
        """The fenced agent's work is preserved and pointed at, not dropped."""
        lease_a = await manager.create_lease("task-1", "agent-a")
        await manager.release_lease("task-1", reason="lease_expired")
        lease_b = await manager.create_lease("task-1", "agent-b")

        assert await should_fence_stale_epoch(manager, "task-1", lease_a.lease_epoch)

        state = Mock()
        state.kanban_client = board
        await open_reconciliation_card(
            state=state,
            task_id="task-1",
            task_name="Build the API",
            late_agent_id="agent-a",
            late_epoch=lease_a.lease_epoch,
            current_epoch=lease_b.lease_epoch,
            message="Endpoint implemented and tested",
        )

        body = board.created[0]["description"]
        # The fenced agent's branch is named, so the work is reachable.
        assert "marcus/agent-a" in body
        # And the card says so in as many words.
        assert "preserved" in body.lower()
        assert "discarded" in body.lower()

    @pytest.mark.asyncio
    async def test_exactly_one_reconciliation_card(
        self, manager: AssignmentLeaseManager, board: Mock
    ) -> None:
        """One collision produces one card — not zero, not two."""
        lease_a = await manager.create_lease("task-1", "agent-a")
        await manager.release_lease("task-1", reason="lease_expired")
        lease_b = await manager.create_lease("task-1", "agent-b")

        state = Mock()
        state.kanban_client = board

        for agent, epoch in (
            ("agent-a", lease_a.lease_epoch),
            ("agent-b", lease_b.lease_epoch),
        ):
            if await should_fence_stale_epoch(manager, "task-1", epoch):
                await open_reconciliation_card(
                    state=state,
                    task_id="task-1",
                    task_name="Build the API",
                    late_agent_id=agent,
                    late_epoch=epoch,
                    current_epoch=lease_b.lease_epoch,
                    message="done",
                )

        assert len(board.created) == 1

    @pytest.mark.asyncio
    async def test_the_card_is_in_the_audit_trail(
        self, manager: AssignmentLeaseManager, board: Mock
    ) -> None:
        """The release that caused this is recorded with its epoch.

        "One reconciliation card in the audit trail" means an operator can
        reconstruct what happened: which epoch was released, why, and
        which claim superseded it.
        """
        lease_a = await manager.create_lease("task-1", "agent-a")
        await manager.release_lease("task-1", reason="lease_expired")
        await manager.create_lease("task-1", "agent-b")

        releases = [
            e for e in manager.lease_history if e.get("event") == "lease_released"
        ]
        assert len(releases) == 1
        assert releases[0]["lease_epoch"] == lease_a.lease_epoch
        assert releases[0]["agent_id"] == "agent-a"
        assert releases[0]["reason"] == "lease_expired"

    @pytest.mark.asyncio
    async def test_the_scenario_survives_a_marcus_restart(
        self, tmp_path, board: Mock
    ) -> None:
        """The fence still holds when Marcus restarts mid-scenario.

        This is the variant that would silently pass without persistence:
        if the epoch sequence reset on restart, agent B would be issued
        the same epoch agent A holds, and the displaced agent's report
        would compare equal to the live one.
        """
        storage = tmp_path / "assignments"

        first = AssignmentLeaseManager(board, AssignmentPersistence(storage))
        lease_a = await first.create_lease("task-1", "agent-a")

        # Marcus restarts while agent A still holds the card.
        second = AssignmentLeaseManager(board, AssignmentPersistence(storage))
        await second.load_active_leases()

        # The silence timeout trips; the card is reassigned.
        await second.release_lease("task-1", reason="lease_expired")
        lease_b = await second.create_lease("task-1", "agent-b")

        assert lease_b.lease_epoch > lease_a.lease_epoch
        assert await should_fence_stale_epoch(second, "task-1", lease_a.lease_epoch)
        assert not await should_fence_stale_epoch(second, "task-1", lease_b.lease_epoch)
