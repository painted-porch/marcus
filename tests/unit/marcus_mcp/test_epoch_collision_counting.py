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
    async def test_counts_only_this_projects_records(self) -> None:
        """Another project's collisions do not inflate this project's figure."""
        state = _state()
        state.persistence.query = AsyncMock(
            return_value=[
                {"project_id": "proj-1", "task_id": "a"},
                {"project_id": "proj-1", "task_id": "b"},
                {"project_id": "other", "task_id": "c"},
            ]
        )

        assert await count_epoch_collisions(state, "proj-1") == 2

    @pytest.mark.asyncio
    async def test_none_counts_every_record(self) -> None:
        """A single-project deployment can ask for the total."""
        state = _state()
        state.persistence.query = AsyncMock(return_value=[{}, {}, {}])

        assert await count_epoch_collisions(state, None) == 3

    @pytest.mark.asyncio
    async def test_a_clean_run_counts_zero(self) -> None:
        """No records means no collisions, not an error."""
        assert await count_epoch_collisions(_state(), "proj-1") == 0

    @pytest.mark.asyncio
    async def test_a_read_failure_counts_zero(self) -> None:
        """A broken store must not fail the status read it is reported in."""
        state = _state()
        state.persistence.query = AsyncMock(side_effect=RuntimeError("locked"))

        assert await count_epoch_collisions(state, "proj-1") == 0

    @pytest.mark.asyncio
    async def test_without_persistence_counts_zero(self) -> None:
        """No persistence, no durable count."""
        assert await count_epoch_collisions(_state(persistence=False), "proj-1") == 0
