"""
Unit tests for record-only epoch-collision counting (epic #706, ADR-0012 D11).

A completion that arrives under a superseded lease epoch means two workers
held the same chore at once. Marcus deliberately allows it (the fence is
Milestone B) but must count it: the count is what sizes the fence, and
"rare" has to be a number rather than an impression. Each observation is
persisted once per task and epoch pair, counted per project, reported by
``get_project_status`` and printed by the launcher when it is not zero.
"""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from src.marcus_mcp.tools.task import (
    EPOCH_COLLISION_COLLECTION,
    _observe_epoch_collision,
    count_epoch_collisions,
)

pytestmark = pytest.mark.unit


def _state(highest: int = 2, *, persistence: bool = True) -> Mock:
    """A state whose lease manager reports ``highest`` as the newest epoch."""
    state = Mock()
    state.lease_manager = Mock()
    state.lease_manager.get_highest_epoch = AsyncMock(return_value=highest)
    state.active_project_id = "proj-1"
    state.project_tasks = [Mock(id="task-1", project_id="proj-1")]
    if persistence:
        state.persistence = Mock()
        state.persistence.store = AsyncMock()
        state.persistence.query = AsyncMock(return_value=[])
    else:
        state.persistence = None
    del state.epoch_collision_count
    return state


class TestObservationIsRecorded:
    """Every observed collision is persisted and counted."""

    @pytest.mark.asyncio
    async def test_a_superseded_completion_is_recorded_once(self) -> None:
        """The record carries the task, agent, both epochs and the project."""
        state = _state(highest=3)

        assert await _observe_epoch_collision(state, "task-1", "agent-a", 1) is True

        state.persistence.store.assert_awaited_once()
        collection, key, data = state.persistence.store.await_args.args
        assert collection == EPOCH_COLLISION_COLLECTION
        assert key == "task-1:1:3"
        assert data["task_id"] == "task-1"
        assert data["agent_id"] == "agent-a"
        assert data["lease_epoch"] == 1
        assert data["highest_epoch"] == 3
        assert data["project_id"] == "proj-1"
        assert data["observed_at"]
        assert state.epoch_collision_count == 1

    @pytest.mark.asyncio
    async def test_a_repeat_of_the_same_collision_reuses_the_key(self) -> None:
        """A re-reported superseded completion must not inflate the count."""
        state = _state(highest=3)

        await _observe_epoch_collision(state, "task-1", "agent-a", 1)
        await _observe_epoch_collision(state, "task-1", "agent-a", 1)

        keys = {call.args[1] for call in state.persistence.store.await_args_list}
        assert keys == {"task-1:1:3"}

    @pytest.mark.asyncio
    async def test_the_current_holder_is_not_a_collision(self) -> None:
        """The agent carrying the newest epoch is simply the holder."""
        state = _state(highest=2)

        assert await _observe_epoch_collision(state, "task-1", "agent-a", 2) is False

        state.persistence.store.assert_not_awaited()
        assert not hasattr(state, "epoch_collision_count")

    @pytest.mark.asyncio
    async def test_a_persistence_failure_does_not_break_the_report(self) -> None:
        """Bookkeeping must never fail a completion; the observation still stands."""
        state = _state(highest=3)
        state.persistence.store = AsyncMock(side_effect=RuntimeError("disk full"))

        assert await _observe_epoch_collision(state, "task-1", "agent-a", 1) is True
        assert state.epoch_collision_count == 1

    @pytest.mark.asyncio
    async def test_without_persistence_the_observation_still_counts(self) -> None:
        """A deployment without persistence keeps the in-process count."""
        state = _state(highest=3, persistence=False)

        assert await _observe_epoch_collision(state, "task-1", "agent-a", 1) is True
        assert state.epoch_collision_count == 1


class TestCountEpochCollisions:
    """The count is per project and never raises."""

    @pytest.mark.asyncio
    async def test_counts_only_this_boards_tasks(self) -> None:
        """A record for a task on another board does not inflate the figure.

        Counting by task membership is what makes the figure right even
        when the stamped project id is missing, as it was in the live
        check for PR #789.
        """
        state = _state()
        state.persistence.query = AsyncMock(
            return_value=[
                {"project_id": None, "task_id": "a"},
                {"project_id": "proj-1", "task_id": "b"},
                {"project_id": "other", "task_id": "c"},
            ]
        )

        assert await count_epoch_collisions(state, {"a", "b"}) == 2

    @pytest.mark.asyncio
    async def test_none_counts_every_record(self) -> None:
        """A single-project deployment can ask for the total."""
        state = _state()
        state.persistence.query = AsyncMock(return_value=[{}, {}, {}])

        assert await count_epoch_collisions(state, None) == 3

    @pytest.mark.asyncio
    async def test_a_clean_run_counts_zero(self) -> None:
        """No records means no collisions, not an error."""
        assert await count_epoch_collisions(_state(), {"task-1"}) == 0

    @pytest.mark.asyncio
    async def test_a_read_failure_counts_zero(self) -> None:
        """A broken store must not fail the status read it is reported in."""
        state = _state()
        state.persistence.query = AsyncMock(side_effect=RuntimeError("locked"))

        assert await count_epoch_collisions(state, {"task-1"}) == 0

    @pytest.mark.asyncio
    async def test_without_persistence_counts_zero(self) -> None:
        """No persistence, no durable count."""
        assert await count_epoch_collisions(_state(persistence=False), {"t"}) == 0


class TestProjectIdProvenance:
    """The record's project id comes from the task row when there is one.

    ``state.active_project_id`` was None when a real completion arrived in
    the live check for PR #789, which is why the count is taken by task
    membership and the stamp is provenance only.
    """

    @pytest.mark.asyncio
    async def test_the_task_row_supplies_the_project_id(self) -> None:
        """The board is the authority, not the server's active-project field."""
        state = _state(highest=3)
        state.active_project_id = "stale-or-missing"
        state.project_tasks = [Mock(id="task-1", project_id="from-the-task")]

        await _observe_epoch_collision(state, "task-1", "agent-a", 1)

        assert state.persistence.store.await_args.args[2]["project_id"] == (
            "from-the-task"
        )

    @pytest.mark.asyncio
    async def test_an_unknown_task_falls_back_to_the_active_project(self) -> None:
        """A completion for a task not in memory still records what it knows."""
        state = _state(highest=3)
        state.project_tasks = []

        await _observe_epoch_collision(state, "task-9", "agent-a", 1)

        assert state.persistence.store.await_args.args[2]["project_id"] == "proj-1"

    @pytest.mark.asyncio
    async def test_no_project_id_anywhere_is_recorded_as_none(self) -> None:
        """A missing id must not drop the observation; the count is by task."""
        state = _state(highest=3)
        state.project_tasks = []
        state.active_project_id = None

        assert await _observe_epoch_collision(state, "task-9", "agent-a", 1) is True
        assert state.persistence.store.await_args.args[2]["project_id"] is None
