"""
Unit tests for carrying the D11 lease epoch across the MCP protocol.

Background
----------
ADR-0012 D11 gives every claim of a task a monotonically-increasing
``lease_epoch``. The lease layer issues and persists it (see
``tests/unit/core/test_lease_epoch_fencing.py``); this module covers the
*protocol* half — Marcus handing the epoch to the agent on
``request_next_task``, and the agent handing it back on
``report_task_progress``.

Enforcement is deliberately **absent**. Marcus records the epoch and
does not act on it; see ``_observe_epoch_collision``. The fence was
removed because it contradicted Marcus's documented false-positive
recovery compensation. What follows still matters: the token has to be
issued, carried, and durable before any enforcement can be built on it.

Historical note on the removed leniency:

* A report carrying **no** epoch falls back to the ownership checks that
  exist today. ``prompts/Agent_prompt.md`` is the canonical worker spec
  that ``README.md`` tells operators to copy into their own projects;
  Marcus cannot update those copies, so strict-from-day-one would route
  every un-migrated agent's completion to a reconciliation card.
* A report carrying an epoch is fenced strictly.

Strict-everywhere becomes a config flag once the fleet has migrated. The
named residual risk is that two-robots-one-chore stays reachable for
un-updated agents until that flag flips.
"""

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.assignment_lease import AssignmentLease, AssignmentLeaseManager
from src.core.assignment_persistence import AssignmentPersistence

pytestmark = pytest.mark.unit


@pytest.fixture
def lease_manager(tmp_path) -> AssignmentLeaseManager:
    """
    Create a lease manager backed by an isolated persistence directory.

    Parameters
    ----------
    tmp_path : Path
        pytest-provided temp dir, keeping the production ``data/`` tree
        untouched (issue #724).

    Returns
    -------
    AssignmentLeaseManager
        Manager wired to a mock kanban client.
    """
    return AssignmentLeaseManager(
        Mock(), AssignmentPersistence(tmp_path / "assignments")
    )


class TestEpochIsHandedToTheAgent:
    """request_next_task must tell the agent which claim it holds."""

    def test_request_next_task_puts_the_epoch_on_the_response(self) -> None:
        """``request_next_task`` writes the claim's epoch onto its response.

        An agent cannot carry a token it was never given, so this is the
        first link in the fence. This asserts against the SOURCE of
        ``request_next_task`` rather than a dict the test builds itself —
        the previous version wrote ``response["lease_epoch"]`` and then
        asserted it was there, which held no matter what the code did.

        The behavioural coverage lives in
        ``tests/unit/marcus_mcp/test_d11_end_to_end.py``, which drives the
        real ``report_task_progress`` with an epoch obtained from a real
        lease.
        """
        import inspect

        from src.marcus_mcp.tools import task as task_module

        source = inspect.getsource(task_module.request_next_task)
        assert '"lease_epoch": assigned_lease_epoch' in source
        assert "assigned_lease_epoch = lease.lease_epoch" in source

    @pytest.mark.asyncio
    async def test_reclaim_hands_out_a_different_epoch(
        self, lease_manager: AssignmentLeaseManager
    ) -> None:
        """A replacement claimant is handed a strictly higher epoch."""
        first = await lease_manager.create_lease("task-1", "agent-1")
        await lease_manager.release_lease("task-1", reason="lease_expired")
        second = await lease_manager.create_lease("task-1", "agent-2")

        assert second.lease_epoch > first.lease_epoch


class TestLenientEnforcement:
    """A report with no epoch behaves exactly as it does today."""

    @pytest.mark.asyncio
    async def test_missing_epoch_is_not_current(
        self, lease_manager: AssignmentLeaseManager
    ) -> None:
        """is_epoch_current fails closed on a missing token.

        The leniency lives in the *caller*, not here: this predicate stays
        strict so that flipping to strict-everywhere is a call-site change
        rather than a semantics change.
        """
        await lease_manager.create_lease("task-1", "agent-1")

        assert await lease_manager.is_epoch_current("task-1", 0) is False

    @pytest.mark.asyncio
    async def test_epoch_supplied_and_current_is_accepted(
        self, lease_manager: AssignmentLeaseManager
    ) -> None:
        """The holder's own epoch is recognised as current."""
        lease = await lease_manager.create_lease("task-1", "agent-1")

        assert await lease_manager.is_epoch_current("task-1", lease.lease_epoch) is True

    @pytest.mark.asyncio
    async def test_epoch_supplied_but_superseded_is_rejected(
        self, lease_manager: AssignmentLeaseManager
    ) -> None:
        """A displaced session's token is recognised as stale."""
        first = await lease_manager.create_lease("task-1", "agent-1")
        await lease_manager.release_lease("task-1", reason="lease_expired")
        await lease_manager.create_lease("task-1", "agent-2")

        assert (
            await lease_manager.is_epoch_current("task-1", first.lease_epoch) is False
        )
