"""
Task Scheduling and Critical Path Method (CPM) for Marcus.

This module implements scheduling algorithms to calculate optimal agent counts
and project timelines using the Critical Path Method.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from src.core.models import Task, TaskStatus

logger = logging.getLogger(__name__)


@dataclass
class ProjectSchedule:
    """
    Scheduling analysis for optimal agent allocation.

    Parameters
    ----------
    optimal_agents : int
        Recommended number of agents for maximum efficiency
    critical_path_hours : float
        Duration of the longest dependency chain
    max_parallelism : int
        Maximum number of tasks that can run simultaneously
    estimated_completion_hours : float
        Expected project completion time with optimal agents
    single_agent_hours : float
        Total time if using only one agent
    efficiency_gain : float
        Percentage improvement from using optimal agents (0.0-1.0)
    parallel_opportunities : List[Dict[str, Any]]
        Time points where multiple tasks can run in parallel
    """

    optimal_agents: int
    critical_path_hours: float
    max_parallelism: int
    estimated_completion_hours: float
    single_agent_hours: float
    efficiency_gain: float
    parallel_opportunities: List[Dict[str, Any]] = field(default_factory=list)


def topological_sort(tasks: List[Task]) -> List[Task]:
    """
    Sort tasks in topological order (dependencies before dependents).

    Uses Kahn's algorithm for topological sorting.

    Parameters
    ----------
    tasks : List[Task]
        List of tasks to sort

    Returns
    -------
    List[Task]
        Tasks sorted in dependency order

    Raises
    ------
    ValueError
        If the dependency graph contains a cycle
    """
    # Build adjacency list and in-degree count
    task_map = {task.id: task for task in tasks}
    in_degree = {task.id: 0 for task in tasks}
    adjacency = defaultdict(list)

    for task in tasks:
        for dep_id in task.dependencies:
            if dep_id in task_map:
                adjacency[dep_id].append(task.id)
                in_degree[task.id] += 1

    # Start with tasks that have no dependencies
    queue = [task_id for task_id, degree in in_degree.items() if degree == 0]
    sorted_tasks = []

    while queue:
        # Remove a task with no dependencies
        current_id = queue.pop(0)
        sorted_tasks.append(task_map[current_id])

        # Reduce in-degree for all dependents
        for dependent_id in adjacency[current_id]:
            in_degree[dependent_id] -= 1
            if in_degree[dependent_id] == 0:
                queue.append(dependent_id)

    # If we couldn't sort all tasks, there's a cycle
    if len(sorted_tasks) != len(tasks):
        raise ValueError(
            f"Dependency cycle detected: sorted {len(sorted_tasks)} of "
            f"{len(tasks)} tasks"
        )

    return sorted_tasks


def detect_cycles(tasks: List[Task]) -> bool:
    """
    Detect if there are any cycles in the task dependency graph.

    Uses depth-first search with colored nodes (white/gray/black).

    Parameters
    ----------
    tasks : List[Task]
        List of tasks to check

    Returns
    -------
    bool
        True if a cycle exists, False otherwise
    """
    task_map = {task.id: task for task in tasks}
    adjacency = defaultdict(list)

    # Build adjacency list
    for task in tasks:
        for dep_id in task.dependencies:
            if dep_id in task_map:
                adjacency[task.id].append(dep_id)

    # Color states: white (unvisited), gray (visiting), black (visited)
    WHITE, GRAY, BLACK = 0, 1, 2
    colors = {task.id: WHITE for task in tasks}

    def has_cycle_dfs(task_id: str) -> bool:
        """DFS helper to detect cycles."""
        if colors[task_id] == GRAY:
            # Back edge found - cycle exists
            return True
        if colors[task_id] == BLACK:
            # Already fully explored
            return False

        # Mark as currently visiting
        colors[task_id] = GRAY

        # Check all dependencies
        for dep_id in adjacency[task_id]:
            if has_cycle_dfs(dep_id):
                return True

        # Mark as fully explored
        colors[task_id] = BLACK
        return False

    # Check all tasks
    for task_id in colors:
        if colors[task_id] == WHITE:
            if has_cycle_dfs(task_id):
                return True

    return False


def _calculate_max_parallelism(task_times: Dict[str, Dict[str, Any]]) -> int:
    """
    Calculate true maximum parallelism using sweep-line algorithm.

    This correctly counts overlapping task intervals, not just tasks
    that start at the same time.

    Parameters
    ----------
    task_times : Dict[str, Dict[str, Any]]
        Mapping of task_id to {start, finish, task} times

    Returns
    -------
    int
        Maximum number of tasks running simultaneously at any point

    Examples
    --------
    Task A: [0────10]  (starts at 0, ends at 10)
    Task B:    [5────15]  (starts at 5, ends at 15)

    Returns 2 (both tasks overlap from t=5 to t=10)
    """
    if not task_times:
        return 0

    # Create events for task start (+1) and end (-1) times
    events = []
    for times in task_times.values():
        start = times["start"]
        finish = times["finish"]
        events.append((start, 1))  # Task starts (increment counter)
        events.append((finish, -1))  # Task ends (decrement counter)

    if not events:
        return 0

    # Sort events by time, with ends (-1) before starts (1) at same time
    # This ensures we don't double-count when a task ends exactly when another starts
    events.sort(key=lambda x: (x[0], x[1]))

    max_parallelism = 0
    current_parallelism = 0

    for time, delta in events:
        current_parallelism += delta
        max_parallelism = max(max_parallelism, current_parallelism)

    return max_parallelism


def calculate_task_times(tasks: List[Task]) -> Dict[str, Dict[str, Any]]:
    """
    Calculate earliest start and finish times for each task using CPM.

    Parameters
    ----------
    tasks : List[Task]
        List of tasks (must be in topological order)

    Returns
    -------
    Dict[str, Dict[str, Any]]
        Mapping of task_id to {start, finish, task} times
    """
    # Sort tasks topologically first
    try:
        sorted_tasks = topological_sort(tasks)
    except ValueError as e:
        logger.error(f"Cannot calculate task times: {e}")
        raise

    task_times: Dict[str, Dict[str, Any]] = {}

    for task in sorted_tasks:
        # Earliest start = latest finish of all dependencies
        earliest_start = 0.0
        if task.dependencies:
            dep_finish_times: List[float] = [
                task_times[dep_id]["finish"]
                for dep_id in task.dependencies
                if dep_id in task_times
            ]
            if dep_finish_times:
                earliest_start = max(dep_finish_times)

        task_times[task.id] = {
            "start": earliest_start,
            "finish": earliest_start + task.estimated_hours,
            "task": task,
        }

    return task_times


def calculate_optimal_agents(tasks: List[Task]) -> ProjectSchedule:
    """
    Calculate optimal number of agents using unified dependency graph.

    Uses critical path method (CPM) to find:
    1. Longest dependency chain (critical path)
    2. Maximum parallelism at any time point
    3. Optimal agent count for maximum efficiency

    Workable Task Selection
    -----------------------
    Schedules tasks that represent actual work, filtering based on structure:

    - **Subtasks**: Always included (decomposed work items)
    - **Atomic parents**: Parents with NO subtasks (atomic work or prototype mode)
    - **Container parents**: Excluded (parents that HAVE subtasks are just containers)
    - **Completed tasks**: Always excluded

    This handles three project modes:
    1. **Standard mode**: Mix of decomposed (parents+subtasks) and atomic tasks
    2. **Prototype mode**: All tasks are atomic parents (no decomposition)
    3. **Hybrid mode**: Some tasks decomposed, others atomic

    Example:
        tasks = [
            Task(id="parent_A", is_subtask=False),  # Has subtasks → excluded
            Task(id="parent_A_sub1", is_subtask=True, parent="parent_A"),  # → included
            Task(id="parent_A_sub2", is_subtask=True, parent="parent_A"),  # → included
            Task(id="parent_B", is_subtask=False),  # No subtasks → included (atomic)
        ]
        # Schedules: parent_A_sub1, parent_A_sub2, parent_B (3 tasks)

    Parameters
    ----------
    tasks : List[Task]
        All tasks in the project (parents + subtasks)

    Returns
    -------
    ProjectSchedule
        Complete scheduling analysis with optimal agent count

    Raises
    ------
    ValueError
        If dependency graph contains cycles
    """
    if not tasks:
        return ProjectSchedule(
            optimal_agents=0,
            critical_path_hours=0.0,
            max_parallelism=0,
            estimated_completion_hours=0.0,
            single_agent_hours=0.0,
            efficiency_gain=0.0,
            parallel_opportunities=[],
        )

    # Filter to only workable tasks based on project structure:
    # - Subtasks (decomposed work)
    # - Parent tasks that have NO subtasks (atomic work or prototype mode)
    # - Exclude parent tasks that HAVE subtasks (they're just containers)
    # - Always exclude completed tasks

    # Build map of parent tasks to their subtasks
    parent_to_subtasks: Dict[str, List[Task]] = {}
    for task in tasks:
        if getattr(task, "is_subtask", False):
            parent_id = getattr(task, "parent_task_id", None)
            if parent_id:
                if parent_id not in parent_to_subtasks:
                    parent_to_subtasks[parent_id] = []
                parent_to_subtasks[parent_id].append(task)

    # Select workable tasks:
    # 1. All subtasks (decomposed work)
    # 2. Parent tasks with NO subtasks (atomic work)
    workable_tasks = []
    for task in tasks:
        if task.status == TaskStatus.DONE:
            continue  # Skip completed tasks

        is_subtask = getattr(task, "is_subtask", False)
        has_children = task.id in parent_to_subtasks

        if is_subtask:
            # Include all subtasks (they're actual work)
            workable_tasks.append(task)
        elif not has_children:
            # Include parent tasks with no subtasks (atomic work)
            workable_tasks.append(task)
        # else: parent with subtasks - skip (it's just a container)

    logger.debug(
        f"Scheduling {len(workable_tasks)} workable tasks "
        f"from {len(tasks)} total tasks "
        f"(subtasks + atomic parents, excluding containers)"
    )

    if not workable_tasks:
        logger.info("No workable tasks found (all tasks completed)")
        return ProjectSchedule(
            optimal_agents=0,
            critical_path_hours=0.0,
            max_parallelism=0,
            estimated_completion_hours=0.0,
            single_agent_hours=0.0,
            efficiency_gain=0.0,
            parallel_opportunities=[],
        )

    # Check for cycles in workable tasks
    if detect_cycles(workable_tasks):
        raise ValueError("Dependency graph contains cycles - cannot schedule")

    # Calculate task times using workable tasks only
    task_times = calculate_task_times(workable_tasks)

    # Find critical path (longest path)
    critical_path = max(times["finish"] for times in task_times.values())

    # Find maximum parallelism using sweep-line algorithm
    # This correctly handles overlapping task intervals
    max_parallelism = _calculate_max_parallelism(task_times)

    # Build time slices for parallel opportunities reporting
    # Note: This still groups by start time for visualization purposes
    time_slices = defaultdict(list)
    for task_id, times in task_times.items():
        time_slices[times["start"]].append(task_id)

    # Calculate total work from workable tasks only
    total_work = sum(task.estimated_hours for task in workable_tasks)

    # CRITICAL: Since agents cannot be dynamically scaled after start,
    # we must provision for PEAK parallelism, not average.
    # Idle agents have low cost (just polling), but insufficient agents
    # create a bottleneck that cannot be resolved without user intervention.
    #
    # Strategy: Use max_parallelism directly to handle peak demand.
    # Agents will idle during low-demand periods, which is acceptable.
    optimal = max_parallelism

    # Calculate efficiency gain
    single_agent_time = total_work
    multi_agent_time = critical_path
    efficiency_gain = (
        (single_agent_time - multi_agent_time) / single_agent_time
        if single_agent_time > 0
        else 0.0
    )

    # Find parallel opportunities (for reporting)
    # Include ALL time slices to show utilization over time
    parallel_opportunities = []
    for time, task_ids in sorted(time_slices.items()):
        parallel_opportunities.append(
            {
                "time": time,
                "task_count": len(task_ids),
                "utilization_percent": (
                    round((len(task_ids) / optimal * 100), 1) if optimal > 0 else 0
                ),
                "idle_agents": optimal - len(task_ids),
                "tasks": [task_times[tid]["task"].name for tid in task_ids],
            }
        )

    logger.info(
        f"Calculated optimal agents: {optimal} agents for "
        f"{len(workable_tasks)} workable tasks "
        f"({len(tasks) - len(workable_tasks)} parents/done filtered out)"
    )
    logger.info(f"Critical path: {critical_path}h, Max parallelism: {max_parallelism}")
    logger.info(
        f"Efficiency gain: {efficiency_gain:.1%} "
        f"({single_agent_time}h → {multi_agent_time}h)"
    )

    return ProjectSchedule(
        optimal_agents=optimal,
        critical_path_hours=critical_path,
        max_parallelism=max_parallelism,
        estimated_completion_hours=critical_path,
        single_agent_hours=single_agent_time,
        efficiency_gain=efficiency_gain,
        parallel_opportunities=parallel_opportunities,
    )


# ---------------------------------------------------------------------------
# Layered agent spawning (issue #595 Fix 3)
# ---------------------------------------------------------------------------


def _workable_tasks(tasks: List[Task], *, include_done: bool = True) -> List[Task]:
    """
    Select tasks that represent real work, mirroring ``calculate_optimal_agents``.

    Workable = subtasks plus *atomic* parent tasks (parents with no
    subtasks). Container parents — parents that have subtasks — are
    organizational, not work, and are excluded.

    Parameters
    ----------
    tasks : List[Task]
        All project tasks.
    include_done : bool, default True
        Keep completed tasks when True. Layer-structure computation needs
        them so the structure is stable as work completes; callers that
        only want outstanding work pass False.

    Returns
    -------
    List[Task]
        The workable subset, original order preserved.
    """
    parent_ids_with_subtasks: Set[str] = set()
    for task in tasks:
        if getattr(task, "is_subtask", False):
            parent_id = getattr(task, "parent_task_id", None)
            if parent_id:
                parent_ids_with_subtasks.add(parent_id)

    workable: List[Task] = []
    for task in tasks:
        if not include_done and task.status == TaskStatus.DONE:
            continue
        is_subtask = getattr(task, "is_subtask", False)
        is_container = task.id in parent_ids_with_subtasks
        if is_subtask or not is_container:
            workable.append(task)
    return workable


def compute_dag_layers(tasks: List[Task]) -> List[List[Task]]:
    """
    Partition workable tasks into dependency layers (topological generations).

    Layer 0 holds workable tasks with no workable-task dependency. Layer
    N holds workable tasks whose every workable dependency sits in a
    layer earlier than N — i.e. a task is placed one layer after its
    deepest dependency. This is pure graph topology: it uses no time
    estimates, so the structure is stable regardless of estimate quality
    (issue #595 Fix 3).

    Dependencies pointing outside the workable set — to a container
    parent, or to a task absent from ``tasks`` — are ignored, so a task
    is layered only against the workable dependencies it actually has.

    Parameters
    ----------
    tasks : List[Task]
        All project tasks. Containers are filtered out; completed tasks
        are retained so the layer structure stays complete and stable.

    Returns
    -------
    List[List[Task]]
        Layers in dependency order. Empty list when there is no workable
        task.

    Raises
    ------
    ValueError
        If the workable dependency graph contains a cycle.
    """
    workable = _workable_tasks(tasks, include_done=True)
    if not workable:
        return []

    by_id: Dict[str, Task] = {t.id: t for t in workable}
    workable_ids = set(by_id)
    layer_index: Dict[str, int] = {}
    visiting: Set[str] = set()

    def depth(task_id: str) -> int:
        cached = layer_index.get(task_id)
        if cached is not None:
            return cached
        if task_id in visiting:
            raise ValueError(f"Dependency cycle detected involving task {task_id}")
        visiting.add(task_id)
        deps = [dep for dep in by_id[task_id].dependencies if dep in workable_ids]
        result = 0 if not deps else 1 + max(depth(dep) for dep in deps)
        visiting.discard(task_id)
        layer_index[task_id] = result
        return result

    for task in workable:
        depth(task.id)

    layers: List[List[Task]] = [[] for _ in range(max(layer_index.values()) + 1)]
    for task in workable:
        layers[layer_index[task.id]].append(task)
    return layers


@dataclass
class ActiveLayerSignal:
    """
    Live layered-spawning signal for the runner controller (issue #595 Fix 3).

    All four fields describe the *active layer* — the earliest DAG layer
    with incomplete work — except ``max_layer_width`` which is whole-graph.
    The runner's spawn formula uses three of them::

        spawn = max(0, min(desired_agent_count - in_flight_tasks,
                           unclaimed_tasks))

    The ``max(0, ...)`` clamp matters: ``desired_agent_count`` drops at a
    layer boundary while a just-finished agent is still being reaped, so
    ``desired_agent_count - in_flight_tasks`` can briefly go negative.

    Attributes
    ----------
    desired_agent_count : int
        Active-layer width — i.e. how many agents the active layer can
        use in parallel — capped at ``max_agents`` when one is set.
        0 when all work is DONE.
    unclaimed_tasks : int
        TODO tasks in the active layer — claimable work the runner may
        still spawn agents for. 0 when all work is DONE.
    in_flight_tasks : int
        IN_PROGRESS tasks in the active layer — tasks Marcus has already
        assigned to an agent and is waiting on. The runner uses this as
        the "coverage" signal in place of its old tmux-pane count, which
        was wrong under the ephemeral lifecycle (issue #632): an alive
        pane is not the same as an agent still about to claim work, and
        a hung post-completion agent would block spawning indefinitely.
    max_layer_width : int
        Width of the *widest* DAG layer across the whole project — the
        most parallelism the graph can ever offer. A small value means a
        near-sequential graph where multi-agent coordination buys little.
    """

    desired_agent_count: int
    unclaimed_tasks: int
    in_flight_tasks: int
    max_layer_width: int


# A layer no longer needs an agent once every task in it is "settled":
# DONE or BLOCKED. BLOCKED is terminal — report_blocker releases the
# lease and Marcus never auto-resolves it — and Marcus's own completion
# formula treats ``completed + blocked == total`` as done
# (live_experiment_monitor._check_completion). The active-layer cursor
# must agree, or a single blocked task pins the cursor and starves every
# downstream layer indefinitely (issue #595).
_SETTLED_STATUSES = (TaskStatus.DONE, TaskStatus.BLOCKED)


def compute_active_layer_signal(
    tasks: List[Task], max_agents: Optional[int] = None
) -> ActiveLayerSignal:
    """
    Compute the runner's layered-spawning signal in a single pass (#595 Fix 3).

    Computes the DAG layers once and derives ``desired_agent_count`` and
    ``unclaimed_tasks`` (from the active layer) plus ``max_layer_width``
    (across all layers). Recomputed from current task state on every call
    with no cursor, so it stays correct when tasks are reset (e.g. a
    rewind, issue #593, re-opening a layer).

    Parameters
    ----------
    tasks : List[Task]
        All project tasks.
    max_agents : Optional[int]
        Hard ceiling on concurrent agents. ``None`` (the default) means
        *no cap* — ``desired_agent_count`` is the active layer's full
        width, so the pool naturally sizes to each layer and peaks at
        the widest layer. Pass an int only to cap below that.

    Returns
    -------
    ActiveLayerSignal
        ``desired_agent_count`` and ``unclaimed_tasks`` are 0 when
        ``max_agents <= 0`` or every workable task is DONE;
        ``max_layer_width`` always reflects the whole graph.
    """
    layers = compute_dag_layers(tasks)
    max_layer_width = max((len(layer) for layer in layers), default=0)

    def _signal(desired: int, unclaimed: int, in_flight: int) -> ActiveLayerSignal:
        return ActiveLayerSignal(
            desired_agent_count=desired,
            unclaimed_tasks=unclaimed,
            in_flight_tasks=in_flight,
            max_layer_width=max_layer_width,
        )

    if max_agents is not None and max_agents <= 0:
        return _signal(0, 0, 0)

    active: Optional[List[Task]] = None
    for layer in layers:
        if any(task.status not in _SETTLED_STATUSES for task in layer):
            active = layer
            break
    if active is None:
        return _signal(0, 0, 0)

    width = len(active)
    desired = width if max_agents is None else min(max_agents, width)
    unclaimed = sum(1 for task in active if task.status == TaskStatus.TODO)
    # Count by status alone, not status + assigned_to. An IN_PROGRESS
    # task without an ``assigned_to`` is an orphan from a different bug
    # (lease expiration should reset it to TODO). Filtering on both
    # fields would couple this signal to "every IN_PROGRESS write sets
    # assigned_to" — a wider invariant we don't want to audit here.
    in_flight = sum(1 for task in active if task.status == TaskStatus.IN_PROGRESS)
    return _signal(desired, unclaimed, in_flight)


def compute_desired_agent_count(
    tasks: List[Task], max_agents: Optional[int] = None
) -> int:
    """
    Compute how many agents should be alive right now (issue #595 Fix 3).

    The *active layer* is the earliest DAG layer that still contains a
    task that is not DONE or BLOCKED. The desired count is that layer's
    width; 0 when every task is DONE or BLOCKED.

    A thin wrapper over :func:`compute_active_layer_signal` for callers
    that need only the count.

    Parameters
    ----------
    tasks : List[Task]
        All project tasks.
    max_agents : Optional[int]
        Hard ceiling on concurrent agents. ``None`` (default) means no
        cap — the count is the active layer's full width.

    Returns
    -------
    int
        Desired live agent count.
    """
    return compute_active_layer_signal(tasks, max_agents).desired_agent_count


def _active_layer(tasks: List[Task]) -> Optional[List[Task]]:
    """
    Return the *active layer* — the earliest DAG layer with workable work.

    The active layer is the earliest layer (from :func:`compute_dag_layers`)
    that still contains a task not in ``DONE`` or ``BLOCKED``. Returns
    ``None`` when every task is settled — DONE or BLOCKED (a blocked task
    is terminal, so it settles its layer like a done one; see
    ``_SETTLED_STATUSES``).

    Parameters
    ----------
    tasks : List[Task]
        All project tasks.

    Returns
    -------
    Optional[List[Task]]
        The active layer's tasks, or ``None`` if all work is complete.
    """
    for layer in compute_dag_layers(tasks):
        if any(task.status not in _SETTLED_STATUSES for task in layer):
            return layer
    return None


def count_unclaimed_tasks_in_active_layer(tasks: List[Task]) -> int:
    """
    Count claimable (TODO) tasks in the active layer (issue #595 Fix 3).

    The runner controller's spawn formula is
    ``min(desired_agent_count - live_agents, unclaimed)``. The
    ``unclaimed`` term is this count: it is what stops the controller
    spawning an agent for which no claimable task exists — such an agent
    would receive "no task" and idle, the cost Fix 3 removes.

    Only ``TODO`` tasks count. ``IN_PROGRESS`` tasks are already claimed,
    ``DONE`` tasks are finished, and ``BLOCKED`` tasks are not claimable.

    Parameters
    ----------
    tasks : List[Task]
        All project tasks.

    Returns
    -------
    int
        Number of TODO tasks in the active layer; 0 when all work is DONE.
    """
    layer = _active_layer(tasks)
    if layer is None:
        return 0
    return sum(1 for task in layer if task.status == TaskStatus.TODO)
