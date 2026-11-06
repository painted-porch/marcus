"""
Unit tests for DECLINED status round-tripping through the Planka provider.

Codex P2 on PR #834: Planka's ``update_task`` mapped only the four
pre-ADR-0013-K5 statuses (TODO/IN_PROGRESS/DONE/BLOCKED), so a
``request_task_decline`` call silently never moved the card at all --
the status change the board-dual-write exists to make visible was
dropped on the floor for this one provider. This covers both
directions: writing DECLINED moves the card, and reading a card back
from a list whose name says "declined" reports TaskStatus.DECLINED.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import TaskStatus
from src.integrations.kanban_client import KanbanClient

pytestmark = pytest.mark.unit


def _planka_instance() -> "Planka":  # noqa: F821 - imported inside, see below
    from src.integrations.providers.planka import Planka

    with patch.dict(
        "os.environ",
        {
            "PLANKA_BASE_URL": "http://test.com",
            "PLANKA_AGENT_EMAIL": "test@test.com",
            "PLANKA_AGENT_PASSWORD": "password",
            "PLANKA_PROJECT_ID": "test_project",
            "PLANKA_BOARD_ID": "test_board",
        },
    ):
        return Planka({})


class TestPlankaWritesDeclinedStatus:
    """update_task must move a DECLINED task, not silently do nothing."""

    @pytest.mark.asyncio
    async def test_update_task_moves_to_the_declined_column(self) -> None:
        planka = _planka_instance()
        planka.move_task_to_column = AsyncMock(return_value=True)  # type: ignore[method-assign]
        planka.get_task_by_id = AsyncMock(return_value=Mock())  # type: ignore[method-assign]

        await planka.update_task("card-1", {"status": TaskStatus.DECLINED})

        planka.move_task_to_column.assert_awaited_once_with("card-1", "declined")

    @pytest.mark.asyncio
    async def test_move_task_to_column_declined_calls_update_task_status(
        self,
    ) -> None:
        planka = _planka_instance()
        planka.client.update_task_status = AsyncMock()

        result = await planka.move_task_to_column("card-1", "declined")

        planka.client.update_task_status.assert_awaited_once_with("card-1", "declined")
        assert result is True

    @pytest.mark.asyncio
    async def test_a_board_with_no_declined_list_fails_without_raising(self) -> None:
        """Same best-effort posture every other unmapped/missing column
        already has: log and return False, never raise into the
        caller (request_task_decline's dual-write is best-effort)."""
        planka = _planka_instance()
        planka.client.update_task_status = AsyncMock(
            side_effect=RuntimeError("no list named 'declined'")
        )

        result = await planka.move_task_to_column("card-1", "declined")

        assert result is False


class TestPlankaReadsDeclinedStatus:
    """A card parsed from a 'declined'-named list reports DECLINED."""

    def test_a_declined_list_name_parses_to_declined_status(self) -> None:
        client = KanbanClient.__new__(KanbanClient)
        card = {"id": "card-1", "name": "Verify the claim", "listName": "Declined"}

        task = client._card_to_task(card)

        assert task.status == TaskStatus.DECLINED

    def test_a_board_without_a_declined_list_is_unaffected(self) -> None:
        """No regression for boards that have never had this list --
        today's TODO/IN_PROGRESS/DONE/BLOCKED cards parse exactly as
        before."""
        client = KanbanClient.__new__(KanbanClient)

        done = client._card_to_task({"id": "c1", "name": "x", "listName": "Done"})
        blocked = client._card_to_task({"id": "c2", "name": "x", "listName": "Blocked"})
        todo = client._card_to_task({"id": "c3", "name": "x", "listName": "Backlog"})

        assert done.status == TaskStatus.DONE
        assert blocked.status == TaskStatus.BLOCKED
        assert todo.status == TaskStatus.TODO
