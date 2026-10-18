"""
Unit tests for layered agent-spawning primitives (issue #595 Fix 3).

`compute_dag_layers` partitions the task graph into dependency layers
(topological generations) using pure graph topology — no time estimates.
`compute_desired_agent_count` derives, from live task state, how many
agents should be alive right now: the width of the earliest layer that
still has incomplete work, clamped to a floor and a ceiling.
"""

from datetime import datetime, timezone
from typing import List, Optional

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.coordinator.scheduler import (
    compute_active_layer_signal,
    compute_dag_layers,
    compute_desired_agent_count,
    count_unclaimed_tasks_in_active_layer,
)

pytestmark = pytest.mark.unit


def _task(
    task_id: str,
    *,
    dependencies: Optional[List[str]] = None,
    status: TaskStatus = TaskStatus.TODO,
    is_subtask: bool = False,
    parent_task_id: Optional[str] = None,
) -> Task:
    """Build a Task with the fields the scheduler primitives read."""
    task = Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=1.0,
        dependencies=dependencies or [],
    )
    task.is_subtask = is_subtask
    task.parent_task_id = parent_task_id
    return task


class TestComputeDagLayers:
    """compute_dag_layers partitions tasks into topological generations."""

    def test_no_dependencies_all_in_layer_zero(self) -> None:
        """Tasks with no dependencies all land in layer 0."""
        layers = compute_dag_layers([_task("a"), _task("b"), _task("c")])

        assert len(layers) == 1
        assert {t.id for t in layers[0]} == {"a", "b", "c"}

    def test_linear_chain_is_one_task_per_layer(self) -> None:
        """A → B → C produces three single-task layers."""
        tasks = [
            _task("a"),
            _task("b", dependencies=["a"]),
            _task("c", dependencies=["b"]),
        ]

        layers = compute_dag_layers(tasks)

        assert [[t.id for t in layer] for layer in layers] == [["a"], ["b"], ["c"]]

    def test_diamond_graph_layers(self) -> None:
        """A; B,C depend on A; D depends on B,C → [A] [B,C] [D]."""
        tasks = [
            _task("a"),
            _task("b", dependencies=["a"]),
            _task("c", dependencies=["a"]),
            _task("d", dependencies=["b", "c"]),
        ]

        layers = compute_dag_layers(tasks)

        assert [t.id for t in layers[0]] == ["a"]
        assert {t.id for t in layers[1]} == {"b", "c"}
        assert [t.id for t in layers[2]] == ["d"]

    def test_task_sits_one_layer_after_its_deepest_dependency(self) -> None:
        """A task with deps in different layers lands after the deepest."""
        # a(0) → b(1); c(0); d depends on b and c → max(1,0)+1 = 2
        tasks = [
            _task("a"),
            _task("b", dependencies=["a"]),
            _task("c"),
            _task("d", dependencies=["b", "c"]),
        ]

        layers = compute_dag_layers(tasks)

        assert len(layers) == 3
        assert [t.id for t in layers[2]] == ["d"]

    def test_width_profile_matches_graph_shape(self) -> None:
        """A 1,1,3,2,1-wide graph yields layers of those widths."""
        tasks = [
            _task("l0"),
            _task("l1", dependencies=["l0"]),
            _task("l2a", dependencies=["l1"]),
            _task("l2b", dependencies=["l1"]),
            _task("l2c", dependencies=["l1"]),
            _task("l3a", dependencies=["l2a"]),
            _task("l3b", dependencies=["l2b"]),
            _task("l4", dependencies=["l3a", "l3b"]),
        ]

        layers = compute_dag_layers(tasks)

        assert [len(layer) for layer in layers] == [1, 1, 3, 2, 1]

    def test_container_parents_are_excluded(self) -> None:
        """A parent that has subtasks is a container — not a workable task."""
        tasks = [
            _task("parent"),
            _task("sub1", is_subtask=True, parent_task_id="parent"),
            _task("sub2", is_subtask=True, parent_task_id="parent"),
            _task("atomic"),
        ]

        layers = compute_dag_layers(tasks)

        all_ids = {t.id for layer in layers for t in layer}
        assert all_ids == {"sub1", "sub2", "atomic"}
        assert "parent" not in all_ids

    def test_done_tasks_are_retained_in_structure(self) -> None:
        """Completed tasks stay in the layering so structure is stable."""
        tasks = [
            _task("a", status=TaskStatus.DONE),
            _task("b", dependencies=["a"]),
        ]

        layers = compute_dag_layers(tasks)

        assert [t.id for t in layers[0]] == ["a"]
        assert [t.id for t in layers[1]] == ["b"]

    def test_cycle_raises_value_error(self) -> None:
        """A dependency cycle is rejected."""
        tasks = [
            _task("a", dependencies=["b"]),
            _task("b", dependencies=["a"]),
        ]

        with pytest.raises(ValueError, match="cycle"):
            compute_dag_layers(tasks)

    def test_empty_task_list_returns_empty(self) -> None:
        """No tasks → no layers."""
        assert compute_dag_layers([]) == []


class TestComputeDesiredAgentCount:
    """compute_desired_agent_count derives live agent demand from layers."""

    def test_no_cap_returns_full_layer_width(self) -> None:
        """Default (no max_agents) → the active layer's full width."""
        tasks = [_task(f"t{i}") for i in range(6)]

        assert compute_desired_agent_count(tasks) == 6

    def test_returns_active_layer_width(self) -> None:
        """Width of the earliest layer with incomplete work, under the cap."""
        tasks = [
            _task("l0", status=TaskStatus.DONE),
            _task("l1a", dependencies=["l0"]),
            _task("l1b", dependencies=["l0"]),
            _task("l1c", dependencies=["l0"]),
        ]

        assert compute_desired_agent_count(tasks, max_agents=10) == 3

    def test_capped_by_max_agents(self) -> None:
        """The active layer width is clamped to max_agents."""
        tasks = [_task(f"t{i}") for i in range(8)]

        assert compute_desired_agent_count(tasks, max_agents=3) == 3

    def test_single_task_layer_wants_one_agent(self) -> None:
        """A one-task active layer wants exactly one agent."""
        tasks = [_task("only")]

        assert compute_desired_agent_count(tasks, max_agents=5) == 1

    def test_all_done_returns_zero(self) -> None:
        """When every task is DONE the whole pool should retire."""
        tasks = [
            _task("a", status=TaskStatus.DONE),
            _task("b", dependencies=["a"], status=TaskStatus.DONE),
        ]

        assert compute_desired_agent_count(tasks, max_agents=5) == 0

    def test_active_layer_is_earliest_incomplete_layer(self) -> None:
        """An incomplete task in an early layer is what sets demand."""
        # layer 0 done, layer 1 has one incomplete task, layer 2 wide
        tasks = [
            _task("l0", status=TaskStatus.DONE),
            _task("l1", dependencies=["l0"], status=TaskStatus.IN_PROGRESS),
            _task("l2a", dependencies=["l1"]),
            _task("l2b", dependencies=["l1"]),
            _task("l2c", dependencies=["l1"]),
        ]

        # active layer is layer 1 (width 1), not the wider layer 2
        assert compute_desired_agent_count(tasks, max_agents=10) == 1

    def test_rewind_reopens_an_earlier_layer(self) -> None:
        """A completed early task reset to TODO makes its layer active again."""
        tasks = [
            _task("l0", status=TaskStatus.TODO),  # was DONE, rewound
            _task("l1a", dependencies=["l0"], status=TaskStatus.DONE),
            _task("l1b", dependencies=["l0"], status=TaskStatus.DONE),
        ]

        # layer 0 is incomplete again → demand is layer 0's width, not layer 1's
        assert compute_desired_agent_count(tasks, max_agents=10) == 1

    def test_max_agents_zero_returns_zero(self) -> None:
        """A zero ceiling yields zero regardless of work."""
        assert compute_desired_agent_count([_task("a")], max_agents=0) == 0

    def test_empty_task_list_returns_zero(self) -> None:
        """No tasks → no agents wanted."""
        assert compute_desired_agent_count([], max_agents=5) == 0


class TestCountUnclaimedTasksInActiveLayer:
    """count_unclaimed_tasks_in_active_layer counts claimable work."""

    def test_counts_todo_tasks_in_active_layer(self) -> None:
        """TODO tasks in the active layer are counted."""
        tasks = [
            _task("l0", status=TaskStatus.DONE),
            _task("a", dependencies=["l0"], status=TaskStatus.TODO),
            _task("b", dependencies=["l0"], status=TaskStatus.TODO),
            _task("c", dependencies=["l0"], status=TaskStatus.IN_PROGRESS),
        ]

        assert count_unclaimed_tasks_in_active_layer(tasks) == 2

    def test_in_progress_and_done_are_not_unclaimed(self) -> None:
        """Only TODO counts — IN_PROGRESS is claimed, DONE is finished."""
        tasks = [
            _task("a", status=TaskStatus.IN_PROGRESS),
            _task("b", status=TaskStatus.DONE),
        ]

        # active layer 0 has incomplete work ('a') but no TODO task
        assert count_unclaimed_tasks_in_active_layer(tasks) == 0

    def test_blocked_tasks_are_not_unclaimed(self) -> None:
        """A BLOCKED task is not claimable, so it is not counted."""
        tasks = [
            _task("a", status=TaskStatus.BLOCKED),
            _task("b", status=TaskStatus.TODO),
        ]

        assert count_unclaimed_tasks_in_active_layer(tasks) == 1

    def test_blocked_only_layer_does_not_pin_the_count(self) -> None:
        """A blocked-only layer is settled — the count advances past it.

        Regression (#600 review): _active_layer must treat BLOCKED as
        settled, like DONE. Otherwise a layer holding only a blocked
        task pins the cursor and the count returns 0 even though
        downstream TODO work exists.
        """
        tasks = [
            _task("l0", status=TaskStatus.DONE),
            _task("b", dependencies=["l0"], status=TaskStatus.BLOCKED),
            _task("d", dependencies=["b"], status=TaskStatus.TODO),
        ]

        # layer [b] is settled (DONE/BLOCKED only) — cursor advances to [d]
        assert count_unclaimed_tasks_in_active_layer(tasks) == 1

    def test_zero_when_all_done(self) -> None:
        """No active layer (all DONE) → zero unclaimed."""
        tasks = [_task("a", status=TaskStatus.DONE)]

        assert count_unclaimed_tasks_in_active_layer(tasks) == 0

    def test_counts_only_the_active_layer(self) -> None:
        """Unclaimed is read from the earliest incomplete layer, not later ones."""
        tasks = [
            _task("l0", status=TaskStatus.IN_PROGRESS),
            _task("a", dependencies=["l0"], status=TaskStatus.TODO),
            _task("b", dependencies=["l0"], status=TaskStatus.TODO),
        ]

        # active layer is layer 0 (l0 incomplete) — 0 TODO there, not layer 1's 2
        assert count_unclaimed_tasks_in_active_layer(tasks) == 0

    def test_empty_task_list_returns_zero(self) -> None:
        """No tasks → zero unclaimed."""
        assert count_unclaimed_tasks_in_active_layer([]) == 0


class TestComputeActiveLayerSignal:
    """compute_active_layer_signal returns desired + unclaimed in one pass."""

    def test_no_cap_sizes_desired_to_full_layer_width(self) -> None:
        """max_agents=None (default) → desired is the full active-layer width."""
        tasks = [_task(f"t{i}") for i in range(7)]

        signal = compute_active_layer_signal(tasks)

        assert signal.desired_agent_count == 7

    def test_blocked_task_does_not_pin_the_active_layer(self) -> None:
        """A BLOCKED task settles its layer like DONE.

        Regression (#595): a blocked task used to pin the active-layer
        cursor — ``unclaimed`` was 0 so the runner spawned nothing and
        the run stalled. A BLOCKED task is terminal, so the cursor must
        advance past it to the next layer with real work.
        """
        tasks = [
            _task("l0", status=TaskStatus.DONE),
            _task("b", dependencies=["l0"], status=TaskStatus.BLOCKED),
            _task("d", dependencies=["b"], status=TaskStatus.TODO),
        ]

        signal = compute_active_layer_signal(tasks)

        # cursor advances past the blocked layer to [d]
        assert signal.desired_agent_count == 1
        assert signal.unclaimed_tasks == 1

    def test_all_done_or_blocked_is_settled(self) -> None:
        """Every task DONE or BLOCKED → the run is settled, 0 agents."""
        tasks = [
            _task("a", status=TaskStatus.DONE),
            _task("b", dependencies=["a"], status=TaskStatus.BLOCKED),
        ]

        signal = compute_active_layer_signal(tasks)

        assert signal.desired_agent_count == 0
        assert signal.unclaimed_tasks == 0

    def test_reports_max_layer_width(self) -> None:
        """max_layer_width is the widest layer across the whole DAG."""
        tasks = [
            _task("l0", status=TaskStatus.DONE),
            _task("a", dependencies=["l0"]),
            _task("b", dependencies=["l0"]),
            _task("c", dependencies=["l0"]),
            _task("d", dependencies=["l0"]),
            _task("z", dependencies=["a"]),
        ]

        signal = compute_active_layer_signal(tasks)

        # layers: [l0] (1) -> [a,b,c,d] (4) -> [z] (1) -> widest is 4
        assert signal.max_layer_width == 4

    def test_returns_desired_and_unclaimed(self) -> None:
        """Both fields are derived from the active layer."""
        tasks = [
            _task("l0", status=TaskStatus.DONE),
            _task("a", dependencies=["l0"], status=TaskStatus.TODO),
            _task("b", dependencies=["l0"], status=TaskStatus.TODO),
            _task("c", dependencies=["l0"], status=TaskStatus.IN_PROGRESS),
        ]

        signal = compute_active_layer_signal(tasks, max_agents=10)

        assert signal.desired_agent_count == 3  # active layer width
        assert signal.unclaimed_tasks == 2  # TODO only

    def test_desired_capped_by_max_agents_unclaimed_is_not(self) -> None:
        """desired_agent_count is capped at max_agents; unclaimed is the raw count."""
        tasks = [_task(f"t{i}") for i in range(8)]

        signal = compute_active_layer_signal(tasks, max_agents=3)

        assert signal.desired_agent_count == 3
        assert signal.unclaimed_tasks == 8

    def test_all_done_returns_zeros(self) -> None:
        """All DONE → both fields 0."""
        signal = compute_active_layer_signal(
            [_task("a", status=TaskStatus.DONE)], max_agents=5
        )

        assert signal.desired_agent_count == 0
        assert signal.unclaimed_tasks == 0

    def test_max_agents_zero_returns_zeros(self) -> None:
        """A zero ceiling yields zeros."""
        signal = compute_active_layer_signal([_task("a")], max_agents=0)

        assert signal.desired_agent_count == 0
        assert signal.unclaimed_tasks == 0

    def test_reports_in_flight_tasks_in_active_layer(self) -> None:
        """in_flight_tasks counts IN_PROGRESS tasks in the active layer (#632).

        The active layer here is ``[a, b, c]``: a (DONE) is in a prior
        layer; b (TODO) and c (IN_PROGRESS) are at the next level. Of
        the two unsettled tasks, one is IN_PROGRESS — that is the
        ``in_flight_tasks`` value the runner needs to compute the
        spawn gap.
        """
        tasks = [
            _task("a", status=TaskStatus.DONE),
            _task("b", dependencies=["a"], status=TaskStatus.TODO),
            _task("c", dependencies=["a"], status=TaskStatus.IN_PROGRESS),
        ]

        signal = compute_active_layer_signal(tasks)

        assert signal.in_flight_tasks == 1
        assert signal.unclaimed_tasks == 1
        assert signal.desired_agent_count == 2

    def test_in_flight_zero_when_no_in_progress_in_active_layer(self) -> None:
        """in_flight_tasks is 0 when the active layer has no IN_PROGRESS work."""
        tasks = [_task("a", status=TaskStatus.TODO)]

        signal = compute_active_layer_signal(tasks)

        assert signal.in_flight_tasks == 0

    def test_in_flight_zero_when_all_done(self) -> None:
        """in_flight_tasks is 0 when every task is settled."""
        signal = compute_active_layer_signal(
            [_task("a", status=TaskStatus.DONE)], max_agents=5
        )

        assert signal.in_flight_tasks == 0

    def test_in_flight_counts_only_active_layer_not_prior_in_progress(self) -> None:
        """Symmetry with desired/unclaimed: in_flight is scoped to the active layer.

        Construct a graph where the active layer contains IN_PROGRESS
        work but a prior layer also has IN_PROGRESS (a stuck task from
        an earlier layer that — under the active-layer cursor — should
        still pin layer 0 as the active layer). The in_flight count
        should reflect IN_PROGRESS tasks in the layer the runner is
        actually spawning agents for.
        """
        tasks = [
            _task("l0_a", status=TaskStatus.DONE),
            _task("l0_b", status=TaskStatus.IN_PROGRESS),
            _task("l1_a", dependencies=["l0_a", "l0_b"], status=TaskStatus.TODO),
        ]

        signal = compute_active_layer_signal(tasks)

        # Active layer is [l0_a, l0_b] (l0_b not settled). l1_a is in
        # the next layer; not counted here. in_flight == 1 (l0_b).
        assert signal.desired_agent_count == 2
        assert signal.in_flight_tasks == 1
        assert signal.unclaimed_tasks == 0
