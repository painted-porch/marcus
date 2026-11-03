"""
Unit tests for the ADR-0012 D11 fence at the resource.

Marcus can take the *job* away from a session (the lease) but not its
*life* (the process). A session that goes quiet during a long build is
still alive; Marcus hands its task to someone else, and the first session
later reports 100%. Two live completions, one task.

The fence decides who wins from the **lease epoch**, a counter stamped at
each claim and kept durably per task. It runs before the two resources a
completion touches — the board's DONE write and the merge into ``main`` —
rather than relying on in-memory ownership, which has disagreed with
itself before (issue #485).

A refused completion is never discarded. The late session's commits stay
on its branch and a board-visible reconciliation card asks an agent to
compare the two attempts and keep the better work (D11 part 2), which is
what closes obligation O3.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.task import (
    _fence_stale_epoch,
    _find_reconciliation_card,
    _open_reconciliation_card,
    maybe_open_reconciliation_card,
)

pytestmark = pytest.mark.unit


def _task(
    task_id: str = "contested",
    name: str = "Implement the scoring rules",
    status: TaskStatus = TaskStatus.DONE,
    assigned_to: Optional[str] = "session_2",
    description: str = "",
) -> Task:
    """Build a task for the fence to name on a card.

    Parameters
    ----------
    task_id : str
        Task id.
    name : str
        Task name.
    status : TaskStatus
        Task status.
    assigned_to : Optional[str]
        The agent the board thinks holds it.
    description : str
        Task description.

    Returns
    -------
    Task
        The task.
    """
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description=description,
        status=status,
        priority=Priority.HIGH,
        assigned_to=assigned_to,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=2.0,
        labels=["implement"],
        dependencies=[],
    )


def _state(highest_epoch: int = 2, project_tasks: Optional[List[Task]] = None) -> Mock:
    """Build a state double with a lease manager and a writable board.

    Parameters
    ----------
    highest_epoch : int
        What ``get_highest_epoch`` should answer.
    project_tasks : Optional[List[Task]]
        The in-memory task store the fence appends a new card to.

    Returns
    -------
    Mock
        The state double. Created cards are on
        ``state.kanban_client.create_task``.
    """
    state = Mock()
    state.project_tasks = project_tasks if project_tasks is not None else []
    state.lease_manager = Mock()
    state.lease_manager.get_highest_epoch = AsyncMock(return_value=highest_epoch)
    created: Dict[str, Any] = {}

    async def _create_task(task_data: Dict[str, Any]) -> Task:
        created.update(task_data)
        card = _task(
            task_id="recon_card_1",
            name=task_data["name"],
            status=TaskStatus.TODO,
            assigned_to=None,
            description=task_data["description"],
        )
        return card

    state.kanban_client = Mock()
    state.kanban_client.create_task = AsyncMock(side_effect=_create_task)
    state.kanban_client.get_all_tasks = AsyncMock(return_value=[])
    state.kanban_client.update_task = AsyncMock()
    state.created_card_data = created
    return state


class TestFenceDecision:
    """Only a provably superseded claim is refused."""

    @pytest.mark.asyncio
    async def test_a_superseded_claim_is_refused(self) -> None:
        """A report under epoch 1 loses to a task now claimed at epoch 2."""
        state = _state(highest_epoch=2)

        result = await _fence_stale_epoch(state, _task(), "contested", "session_1", 1)

        assert result is not None
        assert result["success"] is False
        assert result["status"] == "fenced_stale_epoch"
        assert result["lease_epoch"] == 1
        assert result["current_lease_epoch"] == 2

    @pytest.mark.asyncio
    async def test_the_current_claimant_is_not_refused(self) -> None:
        """The holder of the latest claim completes normally."""
        state = _state(highest_epoch=2)

        assert await _fence_stale_epoch(state, _task(), "contested", "s", 2) is None

    @pytest.mark.asyncio
    async def test_a_report_with_no_epoch_falls_through(self) -> None:
        """Without a token the fence cannot decide, so it does not.

        That report is still subject to the ownership guard, which is the
        pre-D11 behaviour.
        """
        state = _state(highest_epoch=5)

        assert await _fence_stale_epoch(state, _task(), "contested", "s", 0) is None

    @pytest.mark.asyncio
    async def test_a_task_never_claimed_is_not_fenced(self) -> None:
        """A zero high-water mark fences nothing."""
        state = _state(highest_epoch=0)

        assert await _fence_stale_epoch(state, _task(), "contested", "s", 1) is None

    @pytest.mark.asyncio
    async def test_no_lease_manager_means_no_fence(self) -> None:
        """A deployment without leases cannot arbitrate, and says so by
        letting the report through rather than inventing a verdict."""
        state = _state()
        state.lease_manager = None

        assert await _fence_stale_epoch(state, _task(), "contested", "s", 1) is None

    @pytest.mark.asyncio
    async def test_an_unreadable_high_water_mark_falls_through(self) -> None:
        """A persistence failure must not turn into a failed completion."""
        state = _state()
        state.lease_manager.get_highest_epoch = AsyncMock(
            side_effect=RuntimeError("store unreadable")
        )

        assert await _fence_stale_epoch(state, _task(), "contested", "s", 1) is None


class TestReconciliationCard:
    """A refused completion goes to the board, not to the bin."""

    @pytest.mark.asyncio
    async def test_the_fence_opens_a_card_naming_both_attempts(self) -> None:
        """The card carries what an agent needs to decide."""
        state = _state(highest_epoch=2)

        result = await _fence_stale_epoch(state, _task(), "contested", "session_1", 1)

        assert result is not None
        assert result["reconciliation_task_id"] == "recon_card_1"
        card = state.created_card_data
        assert "Reconcile two completions of" in card["name"]
        assert "Implement the scoring rules" in card["name"]
        assert card["priority"] == "urgent"
        assert card["dependencies"] == []
        assert "reconciliation" in card["labels"]
        body = card["description"]
        assert "marcus/session_1" in body
        assert "session_2" in body, "the accepted agent is named"
        assert "log_decision" in body
        assert "Nothing has been deleted" in body

    @pytest.mark.asyncio
    async def test_the_card_does_not_dictate_the_outcome(self) -> None:
        """Invariant #2: Marcus says what to decide, not what to decide for.

        The card must leave all three outcomes open — keep, replace, or
        combine — rather than telling the agent which work wins.
        """
        state = _state(highest_epoch=2)

        await _fence_stale_epoch(state, _task(), "contested", "session_1", 1)

        body = state.created_card_data["description"].lower()
        assert "keep what is on `main`" in body
        assert "take a refused branch's work instead" in body
        assert "combine them" in body

    @pytest.mark.asyncio
    async def test_the_new_card_is_claimable_at_once(self) -> None:
        """It is appended to the in-memory store, not left for a refresh."""
        tasks: List[Task] = []
        state = _state(highest_epoch=2, project_tasks=tasks)

        await _fence_stale_epoch(state, _task(), "contested", "session_1", 1)

        assert len(tasks) == 1
        assert tasks[0].status == TaskStatus.TODO

    @pytest.mark.asyncio
    async def test_repeated_reports_do_not_pile_up_cards(self) -> None:
        """A displaced session retries; the board must not grow a card each
        time."""
        tasks: List[Task] = []
        state = _state(highest_epoch=2, project_tasks=tasks)

        task = _task()
        first = await _fence_stale_epoch(state, task, "contested", "session_1", 1)
        second = await _fence_stale_epoch(state, task, "contested", "session_1", 1)

        assert first is not None and second is not None
        assert first["reconciliation_task_id"] == second["reconciliation_task_id"]
        assert len(tasks) == 1
        assert state.kanban_client.create_task.await_count == 1

    @pytest.mark.asyncio
    async def test_a_resolved_card_does_not_block_a_new_one(self) -> None:
        """Once reconciled, a fresh collision deserves a fresh card."""
        done_card = _task(
            task_id="old_card",
            name="Reconcile two completions of: x",
            status=TaskStatus.DONE,
            assigned_to=None,
            description="<!-- marcus:reconciles task contested -->",
        )
        tasks = [done_card]
        state = _state(highest_epoch=3, project_tasks=tasks)

        result = await _fence_stale_epoch(state, _task(), "contested", "session_1", 1)

        assert result is not None
        assert result["reconciliation_task_id"] == "recon_card_1"
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_a_failed_board_write_still_refuses_the_completion(self) -> None:
        """The fence holds even when the card cannot be written.

        Letting the completion through because the board write failed
        would merge work Marcus had decided to refuse.
        """
        state = _state(highest_epoch=2)
        state.kanban_client.create_task = AsyncMock(
            side_effect=RuntimeError("board down")
        )

        result = await _fence_stale_epoch(state, _task(), "contested", "session_1", 1)

        assert result is not None
        assert result["success"] is False
        assert result["reconciliation_task_id"] is None
        assert "recorded on the task" in result["message"]

    @pytest.mark.asyncio
    async def test_no_card_while_the_replacement_is_still_working(self) -> None:
        """A card opened too early asks for a comparison that cannot be made.

        The replacement has claimed but not finished, so ``main`` holds no
        implementation for this task yet. An agent could claim the card at
        once (it has no dependencies) and reconcile against nothing, and
        the replacement could then land and overwrite that decision
        (Codex P1 on PR #794).
        """
        # Arrange — the task is still in flight.
        task = _task(status=TaskStatus.IN_PROGRESS, assigned_to="session_2")
        state = _state(highest_epoch=2, project_tasks=[])

        # Act
        result = await _fence_stale_epoch(state, task, "contested", "session_1", 1)

        # Assert — refused, recorded, but no card yet.
        assert result is not None
        assert result["success"] is False
        assert result["status"] == "fenced_stale_epoch"
        assert result["reconciliation_task_id"] is None
        assert state.kanban_client.create_task.await_count == 0
        recorded = (task.source_context or {})["refused_attempts"]
        assert [a["agent_id"] for a in recorded] == ["session_1"]
        assert recorded[0]["branch"] == "marcus/session_1"
        assert "recorded on the task" in result["message"]

    @pytest.mark.asyncio
    async def test_the_card_opens_when_the_replacement_completes(self) -> None:
        """Both implementations exist at acceptance, so that is when it opens."""
        # Arrange — a refusal recorded while the task was in flight.
        task = _task(status=TaskStatus.IN_PROGRESS, assigned_to="session_2")
        state = _state(highest_epoch=2, project_tasks=[])
        await _fence_stale_epoch(state, task, "contested", "session_1", 1)
        assert state.kanban_client.create_task.await_count == 0

        # Act — the replacement's completion is accepted.
        task.status = TaskStatus.DONE
        card_id = await maybe_open_reconciliation_card(state, task, "contested")

        # Assert
        assert card_id == "recon_card_1"
        body = state.created_card_data["description"]
        assert "marcus/session_1" in body
        assert "session_2" in body

    @pytest.mark.asyncio
    async def test_an_ordinary_completion_opens_nothing(self) -> None:
        """No refused attempt, no card. This runs on every completion."""
        task = _task(status=TaskStatus.DONE)
        state = _state()

        assert await maybe_open_reconciliation_card(state, task, "contested") is None
        assert state.kanban_client.create_task.await_count == 0

    @pytest.mark.asyncio
    async def test_a_second_displaced_session_is_added_to_the_card(self) -> None:
        """A task recovered twice displaces two sessions; name both.

        Reusing the card without updating it meant the reconciling agent
        never heard about the second branch and could close the card
        without looking at it (Codex P2 on PR #794).
        """
        # Arrange — one refusal, card open, then a second recovery.
        task = _task(status=TaskStatus.DONE, assigned_to="session_3")
        state = _state(highest_epoch=3, project_tasks=[])
        first = await _fence_stale_epoch(state, task, "contested", "session_1", 1)
        assert first is not None

        # Act — a different displaced session reports.
        second = await _fence_stale_epoch(state, task, "contested", "session_2", 2)

        # Assert — same card, both branches named on it.
        assert second is not None
        assert second["reconciliation_task_id"] == first["reconciliation_task_id"]
        assert state.kanban_client.create_task.await_count == 1
        updated = state.kanban_client.update_task.await_args_list[-1]
        body = (updated.args[1] if len(updated.args) > 1 else updated.kwargs)[
            "description"
        ]
        assert "marcus/session_1" in body
        assert "marcus/session_2" in body
        recorded = (task.source_context or {})["refused_attempts"]
        assert [a["agent_id"] for a in recorded] == ["session_1", "session_2"]

    @pytest.mark.asyncio
    async def test_the_refusal_reaches_the_cached_task_too(self) -> None:
        """The board's copy and the cached copy are different objects.

        A provider like sqlite hydrates fresh Task objects in
        ``get_all_tasks()``, so stamping the board's copy never reached
        ``state.project_tasks`` — and that is the copy the acceptance
        hook reads, so the card silently never opened (Codex P2 on PR
        #794).
        """
        # Arrange — two objects for one task, as the sqlite path produces.
        board_copy = _task(status=TaskStatus.IN_PROGRESS, assigned_to="session_2")
        cached_copy = _task(status=TaskStatus.IN_PROGRESS, assigned_to="session_2")
        assert board_copy is not cached_copy
        state = _state(highest_epoch=2, project_tasks=[cached_copy])

        # Act — the fence stamps the board's copy.
        await _fence_stale_epoch(state, board_copy, "contested", "session_1", 1)

        # Assert — the cached copy carries it, so the hook can see it.
        recorded = (cached_copy.source_context or {}).get("refused_attempts") or []
        assert [a["agent_id"] for a in recorded] == ["session_1"]

        cached_copy.status = TaskStatus.DONE
        assert (
            await maybe_open_reconciliation_card(state, cached_copy, "contested")
            == "recon_card_1"
        )

    @pytest.mark.asyncio
    async def test_the_refusal_survives_a_board_that_drops_it(
        self, tmp_path: Path
    ) -> None:
        """Only sqlite round-trips source_context, so the record lives here.

        Planka, GitHub and Linear ignore ``source_context`` on
        ``update_task``, and a board refresh replaces the cached task
        objects, so a board-only record is gone by the time the
        replacement completes (Codex P2 on PR #794).
        """
        from src.core.assignment_persistence import AssignmentPersistence

        # Arrange — a provider that accepts the write and keeps nothing.
        board_copy = _task(status=TaskStatus.IN_PROGRESS, assigned_to="session_2")
        state = _state(highest_epoch=2, project_tasks=[board_copy])
        state.assignment_persistence = AssignmentPersistence(
            storage_dir=tmp_path / "assignments"
        )

        # Act — the refusal, then a refresh that wipes every stamp.
        await _fence_stale_epoch(state, board_copy, "contested", "session_1", 1)
        refreshed = _task(status=TaskStatus.DONE, assigned_to="session_2")
        state.project_tasks = [refreshed]
        state.kanban_client.get_all_tasks = AsyncMock(return_value=[refreshed])

        card_id = await maybe_open_reconciliation_card(state, refreshed, "contested")

        # Assert
        assert card_id == "recon_card_1"
        assert "marcus/session_1" in state.created_card_data["description"]

    @pytest.mark.asyncio
    async def test_two_refusals_are_both_kept_in_the_store(
        self, tmp_path: Path
    ) -> None:
        """A task recovered twice keeps both displaced sessions on record."""
        from src.core.assignment_persistence import AssignmentPersistence

        persistence = AssignmentPersistence(storage_dir=tmp_path / "assignments")
        first = await persistence.record_refused_attempt(
            "contested", {"agent_id": "s1", "lease_epoch": 1}
        )
        again = await persistence.record_refused_attempt(
            "contested", {"agent_id": "s1", "lease_epoch": 1}
        )
        second = await persistence.record_refused_attempt(
            "contested", {"agent_id": "s2", "lease_epoch": 2}
        )

        assert [a["agent_id"] for a in first] == ["s1"]
        assert [a["agent_id"] for a in again] == ["s1"], "a repeat adds nothing"
        assert [a["agent_id"] for a in second] == ["s1", "s2"]

        # And it survives a fresh process.
        reopened = AssignmentPersistence(storage_dir=tmp_path / "assignments")
        stored = await reopened.get_refused_attempts("contested")
        assert [a["agent_id"] for a in stored] == ["s1", "s2"]
        assert await reopened.get_refused_attempts("other") == []

    @pytest.mark.asyncio
    async def test_the_hook_falls_back_to_the_board(self) -> None:
        """Even with a stale cache, the persisted record is found.

        The refusal is written to the board, so a cached object that
        never received the stamp must not be taken as proof that no
        attempt was refused.
        """
        # Arrange — only the board knows about the refusal.
        board_copy = _task(status=TaskStatus.DONE, assigned_to="session_2")
        board_copy.source_context = {
            "refused_attempts": [
                {
                    "agent_id": "session_1",
                    "branch": "marcus/session_1",
                    "lease_epoch": 1,
                    "refused_at": "2026-11-03T00:00:00+00:00",
                }
            ]
        }
        stale_cached = _task(status=TaskStatus.DONE, assigned_to="session_2")
        state = _state(project_tasks=[stale_cached])
        state.kanban_client.get_all_tasks = AsyncMock(return_value=[board_copy])

        # Act
        card_id = await maybe_open_reconciliation_card(state, stale_cached, "contested")

        # Assert
        assert card_id == "recon_card_1"
        assert "marcus/session_1" in state.created_card_data["description"]

    @pytest.mark.asyncio
    async def test_a_replacement_that_finishes_mid_refusal_still_gets_a_card(
        self, tmp_path: Path
    ) -> None:
        """The status in hand can be stale by the time the refusal lands.

        The replacement can finish while the epoch lookup is awaited, and
        its own acceptance hook can run before the refusal is recorded,
        finding nothing. Both reports would then finish with the refusal
        stored and no card scheduled (Codex P2 on PR #794).
        """
        from src.core.assignment_persistence import AssignmentPersistence

        # Arrange — the fence is handed an in-progress snapshot, while the
        # board already says DONE.
        stale_snapshot = _task(status=TaskStatus.IN_PROGRESS, assigned_to="session_2")
        finished = _task(status=TaskStatus.DONE, assigned_to="session_2")
        state = _state(highest_epoch=2, project_tasks=[finished])
        state.assignment_persistence = AssignmentPersistence(
            storage_dir=tmp_path / "assignments"
        )
        state.kanban_client.get_all_tasks = AsyncMock(return_value=[finished])

        # Act
        result = await _fence_stale_epoch(
            state, stale_snapshot, "contested", "session_1", 1
        )

        # Assert — the card opened despite the stale snapshot.
        assert result is not None
        assert result["status"] == "fenced_stale_epoch"
        assert result["reconciliation_task_id"] == "recon_card_1"

    @pytest.mark.asyncio
    async def test_the_card_is_not_labelled_implementation(self) -> None:
        """Its deliverable is a decision, so the commit gate must not apply.

        One permitted outcome is keeping what is already on main, which
        changes no files. Labelling the card as implementation put it in
        front of a commit check it could never satisfy, so a valid
        reconciliation could not be closed without manufacturing an
        unrelated commit (Codex P2 on PR #794).
        """
        state = _state(highest_epoch=2)

        await _fence_stale_epoch(state, _task(), "contested", "session_1", 1)

        labels = state.created_card_data["labels"]
        assert labels == ["reconciliation"]
        for implementation_label in ("implement", "implementation", "feature"):
            assert implementation_label not in labels

    @pytest.mark.asyncio
    async def test_overlapping_refusals_produce_one_card(self, tmp_path: Path) -> None:
        """Two refusals arriving together must not each open a card.

        The card is only appended to the cache after ``create_task``
        returns, so two concurrent calls could both find nothing and both
        create one, leaving two claimable cards deciding the same thing
        (Codex P2 on PR #794).
        """
        import asyncio

        from src.core.assignment_persistence import AssignmentPersistence

        # Arrange — a board whose create is slow enough to interleave.
        task = _task(status=TaskStatus.DONE, assigned_to="session_3")
        state = _state(highest_epoch=3, project_tasks=[])
        state.assignment_persistence = AssignmentPersistence(
            storage_dir=tmp_path / "assignments"
        )
        created: list = []

        async def _slow_create(task_data):
            await asyncio.sleep(0.05)
            created.append(task_data)
            card = _task(
                task_id=f"recon_{len(created)}",
                name=task_data["name"],
                status=TaskStatus.TODO,
                assigned_to=None,
                description=task_data["description"],
            )
            return card

        state.kanban_client.create_task = AsyncMock(side_effect=_slow_create)

        # Act — two displaced sessions report at the same moment.
        first, second = await asyncio.gather(
            _fence_stale_epoch(state, task, "contested", "session_1", 1),
            _fence_stale_epoch(state, task, "contested", "session_2", 2),
        )

        # Assert
        assert first is not None and second is not None
        assert len(created) == 1, "two cards for one contested task"
        assert first["reconciliation_task_id"] == second["reconciliation_task_id"]

    @pytest.mark.asyncio
    async def test_a_settled_refusal_does_not_reopen(self, tmp_path: Path) -> None:
        """A repeat report after the card was completed must not open another.

        ``_find_reconciliation_card`` deliberately ignores a DONE card so
        a fresh collision gets a fresh card, which meant an old refusal
        repeated later re-litigated a settled decision (Codex P2 on PR
        #794).
        """
        from src.core.assignment_persistence import AssignmentPersistence

        # Arrange — one refusal, carded, and the card then completed.
        task = _task(status=TaskStatus.DONE, assigned_to="session_2")
        state = _state(highest_epoch=2, project_tasks=[])
        state.assignment_persistence = AssignmentPersistence(
            storage_dir=tmp_path / "assignments"
        )
        first = await _fence_stale_epoch(state, task, "contested", "session_1", 1)
        assert first is not None
        card_id = first["reconciliation_task_id"]
        for candidate in state.project_tasks:
            if str(candidate.id) == card_id:
                candidate.status = TaskStatus.DONE

        # Act — the displaced session reports the same thing again.
        repeat = await _fence_stale_epoch(state, task, "contested", "session_1", 1)

        # Assert — pointed at the settled card, and no second one.
        assert repeat is not None
        assert repeat["reconciliation_task_id"] == card_id
        assert state.kanban_client.create_task.await_count == 1

    @pytest.mark.asyncio
    async def test_a_new_refusal_after_a_settled_one_still_gets_a_card(
        self, tmp_path: Path
    ) -> None:
        """A genuinely new collision is not silenced by an old settled card."""
        from src.core.assignment_persistence import AssignmentPersistence

        task = _task(status=TaskStatus.DONE, assigned_to="session_3")
        state = _state(highest_epoch=3, project_tasks=[])
        state.assignment_persistence = AssignmentPersistence(
            storage_dir=tmp_path / "assignments"
        )
        first = await _fence_stale_epoch(state, task, "contested", "session_1", 1)
        assert first is not None
        for candidate in state.project_tasks:
            candidate.status = TaskStatus.DONE

        second = await _fence_stale_epoch(state, task, "contested", "session_2", 2)

        assert second is not None
        assert state.kanban_client.create_task.await_count == 2
        assert "marcus/session_2" in state.created_card_data["description"]

    @pytest.mark.asyncio
    async def test_the_marker_finds_only_this_tasks_card(self) -> None:
        """Another task's card is not mistaken for this one's."""
        other = _task(
            task_id="other_card",
            status=TaskStatus.TODO,
            assigned_to=None,
            description="<!-- marcus:reconciles task something_else -->",
        )
        state = _state(project_tasks=[other])

        assert await _find_reconciliation_card(state, "contested") is None

    @pytest.mark.asyncio
    async def test_the_card_can_be_opened_without_a_resolved_task(self) -> None:
        """A board read that missed the task still produces a usable card."""
        state = _state()

        card_id = await _open_reconciliation_card(
            state,
            None,
            "contested",
            [{"agent_id": "session_1", "branch": "marcus/session_1", "lease_epoch": 1}],
        )

        assert card_id == "recon_card_1"
        assert "contested" in state.created_card_data["name"]
