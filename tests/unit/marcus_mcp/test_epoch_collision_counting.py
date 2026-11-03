"""
Unit tests for record-only epoch-collision counting (epic #706, ADR-0012 D11).

A completion that arrives under a superseded lease epoch means two workers
held the same chore at once. Marcus deliberately allows it (the fence is
Milestone B) but must count it: the count is what sizes the fence, and
"rare" has to be a number rather than an impression.

Each task's row lists the epoch pairs already counted, so recording is one
idempotent read-modify-write of a single row under a lock: a repeat is a
no-op, concurrent reporters cannot lose an increment, and an interrupted
attempt is repaired by the next report. The count is read from those rows
by key, never by scanning the detail rows, because both persistence
backends apply their limit around the filter and a scan would silently
drop an older project's records once the collection outgrew the window
(Codex P2s on PR #789).
"""

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from src.marcus_mcp.tools.task import (
    EPOCH_COLLISION_COLLECTION,
    EPOCH_COLLISION_COUNT_COLLECTION,
    _observe_epoch_collision,
    count_epoch_collisions,
)

pytestmark = pytest.mark.unit


class _Store:
    """A tiny stand-in for the persistence facade, keyed like the real one."""

    def __init__(self) -> None:
        self.data: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.retrieve_calls: list = []

    async def store(self, collection: str, key: str, data: Dict[str, Any]) -> None:
        self.data.setdefault(collection, {})[key] = data

    async def retrieve(self, collection: str, key: str) -> Optional[Dict[str, Any]]:
        self.retrieve_calls.append((collection, key))
        return self.data.get(collection, {}).get(key)

    def details(self) -> Dict[str, Dict[str, Any]]:
        return self.data.get(EPOCH_COLLISION_COLLECTION, {})

    def counts(self) -> Dict[str, Dict[str, Any]]:
        return self.data.get(EPOCH_COLLISION_COUNT_COLLECTION, {})


def _state(highest: int = 2, *, store: Optional[_Store] = None) -> Mock:
    """A state whose lease manager reports ``highest`` as the newest epoch."""
    state = Mock()
    state.lease_manager = Mock()
    state.lease_manager.get_highest_epoch = AsyncMock(return_value=highest)
    state.active_project_id = "proj-1"
    state.project_tasks = [Mock(id="task-1", project_id="proj-1")]
    state.persistence = store if store is not None else _Store()
    del state.epoch_collision_count
    return state


class TestObservationIsRecorded:
    """Every observed collision is recorded once and counted once."""

    @pytest.mark.asyncio
    async def test_a_superseded_completion_is_recorded_and_counted(self) -> None:
        """The task's row carries the count; the detail row carries the facts."""
        store = _Store()
        state = _state(highest=3, store=store)

        assert await _observe_epoch_collision(state, "task-1", "agent-a", 1) is True

        detail = store.details()["task-1:1:3"]
        assert detail["task_id"] == "task-1"
        assert detail["agent_id"] == "agent-a"
        assert detail["lease_epoch"] == 1
        assert detail["highest_epoch"] == 3
        assert detail["project_id"] == "proj-1"
        assert detail["observed_at"]
        assert store.counts()["task-1"]["count"] == 1
        assert store.counts()["task-1"]["pairs"] == ["1:3"]
        assert state.epoch_collision_count == 1

    @pytest.mark.asyncio
    async def test_a_repeat_of_the_same_collision_is_counted_once(self) -> None:
        """A re-reported superseded completion must not inflate the count."""
        store = _Store()
        state = _state(highest=3, store=store)

        await _observe_epoch_collision(state, "task-1", "agent-a", 1)
        await _observe_epoch_collision(state, "task-1", "agent-a", 1)

        assert list(store.details()) == ["task-1:1:3"]
        assert store.counts()["task-1"]["count"] == 1
        assert store.counts()["task-1"]["pairs"] == ["1:3"]

    @pytest.mark.asyncio
    async def test_a_second_distinct_collision_adds_to_the_task(self) -> None:
        """A later, different epoch pair on the same task counts again."""
        store = _Store()
        await _observe_epoch_collision(_state(highest=3, store=store), "task-1", "a", 1)
        await _observe_epoch_collision(_state(highest=4, store=store), "task-1", "b", 2)

        assert sorted(store.details()) == ["task-1:1:3", "task-1:2:4"]
        assert store.counts()["task-1"]["count"] == 2
        assert store.counts()["task-1"]["pairs"] == ["1:3", "2:4"]

    @pytest.mark.asyncio
    async def test_the_current_holder_is_not_a_collision(self) -> None:
        """The agent carrying the newest epoch is simply the holder."""
        store = _Store()
        state = _state(highest=2, store=store)

        assert await _observe_epoch_collision(state, "task-1", "agent-a", 2) is False

        assert store.details() == {}
        assert not hasattr(state, "epoch_collision_count")

    @pytest.mark.asyncio
    async def test_a_persistence_failure_does_not_break_the_report(self) -> None:
        """Bookkeeping must never fail a completion; the observation stands."""
        state = _state(highest=3)
        state.persistence.store = AsyncMock(side_effect=RuntimeError("disk full"))

        assert await _observe_epoch_collision(state, "task-1", "agent-a", 1) is True
        assert state.epoch_collision_count == 1

    @pytest.mark.asyncio
    async def test_without_persistence_the_observation_still_counts(self) -> None:
        """A deployment without persistence keeps the in-process count."""
        state = _state(highest=3)
        state.persistence = None

        assert await _observe_epoch_collision(state, "task-1", "agent-a", 1) is True
        assert state.epoch_collision_count == 1


class TestProjectIdProvenance:
    """The record's project id comes from the task row when there is one.

    ``state.active_project_id`` was None when a real completion arrived in
    the live check for PR #789, which is why the count is taken by task and
    the stamp is provenance only.
    """

    @pytest.mark.asyncio
    async def test_the_task_row_supplies_the_project_id(self) -> None:
        """The board is the authority, not the server's active-project field."""
        store = _Store()
        state = _state(highest=3, store=store)
        state.active_project_id = "stale-or-missing"
        state.project_tasks = [Mock(id="task-1", project_id="from-the-task")]

        await _observe_epoch_collision(state, "task-1", "agent-a", 1)

        assert store.details()["task-1:1:3"]["project_id"] == "from-the-task"

    @pytest.mark.asyncio
    async def test_an_unknown_task_falls_back_to_the_active_project(self) -> None:
        """A completion for a task not in memory still records what it knows."""
        store = _Store()
        state = _state(highest=3, store=store)
        state.project_tasks = []

        await _observe_epoch_collision(state, "task-9", "agent-a", 1)

        assert store.details()["task-9:1:3"]["project_id"] == "proj-1"

    @pytest.mark.asyncio
    async def test_no_project_id_anywhere_is_recorded_as_none(self) -> None:
        """A missing id must not drop the observation; the count is by task."""
        store = _Store()
        state = _state(highest=3, store=store)
        state.project_tasks = []
        state.active_project_id = None

        assert await _observe_epoch_collision(state, "task-9", "agent-a", 1) is True
        assert store.details()["task-9:1:3"]["project_id"] is None
        assert store.counts()["task-9"]["count"] == 1


class TestCountEpochCollisions:
    """The count is read by key, per task, and never raises."""

    @pytest.mark.asyncio
    async def test_sums_the_given_tasks_and_ignores_the_others(self) -> None:
        """A collision on another board's task does not inflate this figure."""
        store = _Store()
        await store.store(EPOCH_COLLISION_COUNT_COLLECTION, "a", {"count": 2})
        await store.store(EPOCH_COLLISION_COUNT_COLLECTION, "b", {"count": 1})
        await store.store(EPOCH_COLLISION_COUNT_COLLECTION, "elsewhere", {"count": 7})
        state = _state(store=store)

        assert await count_epoch_collisions(state, {"a", "b"}) == 3

    @pytest.mark.asyncio
    async def test_reads_by_key_so_no_scan_can_truncate_it(self) -> None:
        """Only the asked-for keys are read, however large the collection is."""
        store = _Store()
        for index in range(2500):
            await store.store(
                EPOCH_COLLISION_COUNT_COLLECTION, f"old-{index}", {"count": 1}
            )
        await store.store(EPOCH_COLLISION_COUNT_COLLECTION, "mine", {"count": 4})
        state = _state(store=store)

        assert await count_epoch_collisions(state, {"mine"}) == 4
        assert store.retrieve_calls == [(EPOCH_COLLISION_COUNT_COLLECTION, "mine")]

    @pytest.mark.asyncio
    async def test_a_clean_board_counts_zero(self) -> None:
        """No records means no collisions, not an error."""
        assert await count_epoch_collisions(_state(), {"task-1"}) == 0

    @pytest.mark.asyncio
    async def test_an_empty_board_counts_zero(self) -> None:
        """Nothing to count, and no reads attempted."""
        store = _Store()
        assert await count_epoch_collisions(_state(store=store), set()) == 0
        assert store.retrieve_calls == []

    @pytest.mark.asyncio
    async def test_a_read_failure_does_not_lose_the_other_tasks(self) -> None:
        """One broken key must not zero the whole figure."""
        store = _Store()
        await store.store(EPOCH_COLLISION_COUNT_COLLECTION, "good", {"count": 3})
        state = _state(store=store)
        real_retrieve = store.retrieve

        async def flaky(collection: str, key: str) -> Any:
            if key == "bad":
                raise RuntimeError("locked")
            return await real_retrieve(collection, key)

        state.persistence.retrieve = flaky

        assert await count_epoch_collisions(state, {"good", "bad"}) == 3

    @pytest.mark.asyncio
    async def test_a_corrupt_count_is_ignored(self) -> None:
        """A non-integer count is treated as absent rather than raising."""
        store = _Store()
        await store.store(EPOCH_COLLISION_COUNT_COLLECTION, "t", {"count": "lots"})

        assert await count_epoch_collisions(_state(store=store), {"t"}) == 0

    @pytest.mark.asyncio
    async def test_without_persistence_counts_zero(self) -> None:
        """No persistence, no durable count."""
        state = _state()
        state.persistence = None

        assert await count_epoch_collisions(state, {"t"}) == 0


class TestRecordingIsIdempotentAndSerialized:
    """The two hazards Codex found in the first roll-up (P2s on PR #789)."""

    @pytest.mark.asyncio
    async def test_concurrent_reporters_cannot_lose_an_increment(self) -> None:
        """Four holders reporting four distinct pairs at once count four.

        Without the lock each would read the same row and the last write
        would win, leaving the count at one.
        """
        import asyncio

        store = _Store()
        pairs = [(1, 5), (2, 5), (3, 5), (4, 5)]
        await asyncio.gather(
            *[
                _observe_epoch_collision(
                    _state(highest=high, store=store), "task-1", f"agent-{low}", low
                )
                for low, high in pairs
            ]
        )

        row = store.counts()["task-1"]
        assert row["count"] == 4
        assert sorted(row["pairs"]) == ["1:5", "2:5", "3:5", "4:5"]

    @pytest.mark.asyncio
    async def test_concurrent_reports_of_the_same_pair_count_once(self) -> None:
        """The same superseded completion arriving twice at once is one."""
        import asyncio

        store = _Store()
        results = await asyncio.gather(
            *[
                _observe_epoch_collision(
                    _state(highest=3, store=store), "task-1", "agent-a", 1
                )
                for _ in range(4)
            ]
        )

        # All four observed a collision (that is what the observer reports);
        # only one of them counted it.
        assert results == [True, True, True, True]
        assert store.counts()["task-1"]["count"] == 1
        assert store.counts()["task-1"]["pairs"] == ["1:3"]

    @pytest.mark.asyncio
    async def test_a_failed_write_is_repaired_by_the_next_report(self) -> None:
        """An interrupted attempt must not be excluded from the count forever.

        The first attempt's row write fails; the retry has to count it,
        which it can because the row, not a detail flag, is the record.
        (``_observe_epoch_collision`` returns True either way: it reports
        that a collision was observed, not that it was recorded.)
        """
        store = _Store()
        state = _state(highest=3, store=store)
        real_store = store.store
        calls = {"n": 0}

        async def flaky(collection: str, key: str, data: Any) -> None:
            if collection == EPOCH_COLLISION_COUNT_COLLECTION:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("interrupted")
            await real_store(collection, key, data)

        state.persistence.store = flaky

        await _observe_epoch_collision(state, "task-1", "agent-a", 1)
        assert store.counts() == {}, "the interrupted attempt left nothing behind"

        await _observe_epoch_collision(state, "task-1", "agent-a", 1)
        assert store.counts()["task-1"]["count"] == 1, "the retry repaired it"

    @pytest.mark.asyncio
    async def test_a_lost_detail_row_does_not_lose_the_count(self) -> None:
        """Provenance is written after the count and cannot undo it."""
        store = _Store()
        state = _state(highest=3, store=store)
        real_store = store.store

        async def no_details(collection: str, key: str, data: Any) -> None:
            if collection == EPOCH_COLLISION_COLLECTION:
                raise RuntimeError("provenance disk full")
            await real_store(collection, key, data)

        state.persistence.store = no_details

        assert await _observe_epoch_collision(state, "task-1", "agent-a", 1) is True
        assert store.counts()["task-1"]["count"] == 1
        assert store.details() == {}

    @pytest.mark.asyncio
    async def test_a_corrupt_pairs_list_is_rebuilt_rather_than_trusted(self) -> None:
        """A row whose pairs are not strings must not break the next report."""
        store = _Store()
        await store.store(
            EPOCH_COLLISION_COUNT_COLLECTION, "task-1", {"pairs": "not-a-list"}
        )

        assert (
            await _observe_epoch_collision(
                _state(highest=3, store=store), "task-1", "agent-a", 1
            )
            is True
        )
        assert store.counts()["task-1"]["pairs"] == ["1:3"]
        assert store.counts()["task-1"]["count"] == 1
