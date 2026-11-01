# 35. Assignment Lease System

> **Primary spec:** This document focuses on the lease data model, the two liveness dials, and renewal mechanics.
> For the end-to-end liveness detection, touch-on-any-tool behavior, and the recovery handoff flow, see
> [Resilience and Task Recovery System](34-agent-recovery-system.md).

## Executive Summary

The Assignment Lease System tracks a short-lived **lease** for every
in-progress task assignment. A lease is Marcus's proof-of-life record for an
agent: while the lease is fresh, Marcus trusts that the agent is working;
once the lease expires past its grace period, the lease is evaluated for
recovery.

Since ADR-0012 D3 a lease carries two dials.
`lease_expires` answers "is the agent alive?": it sits one **silence timeout** (default 45 minutes) after the agent's last MCP call and moves forward on every call.
`budget_deadline` answers "how long may it hold the task?": `budget_multiplier` (default 3) times the estimate, fixed at claim time, and `lease_expires` never passes it.
Detecting a dead agent therefore no longer depends on how big or how chatty its task is.

## Source Files

| Component | Path |
|-----------|------|
| Lease data model + manager + monitor | `src/core/assignment_lease.py` |
| Default configuration | `src/config/marcus_config.py` — `TaskLeaseSettings` |
| Server wiring + recovery callback | `src/marcus_mcp/server.py` |
| Lease creation + progress renewal + the no-lease refusal | `src/marcus_mcp/tools/task.py` |
| Touch-on-any-tool-call | `src/marcus_mcp/handlers.py` |

## Lease Data Model

```python
@dataclass
class AssignmentLease:
    task_id: str
    agent_id: str
    assigned_at: datetime
    lease_expires: datetime
    last_renewed: datetime
    renewal_count: int = 0
    estimated_hours: float = 4.0
    progress_percentage: int = 0
    last_progress_message: str = ""
    grace_period_seconds: Optional[float] = None
    update_timestamps: list[datetime] = field(default_factory=list)
```

Key properties:

- `lease_expires` — the silence dial: last activity plus `silence_timeout_minutes`.
- `budget_deadline` — the budget dial, set once at claim time; `None` on rows persisted before D3, which are governed by silence alone.
- `update_timestamps` — every heartbeat and progress report appends a timestamp, for the audit record.
- `grace_period_seconds` — optional per-lease grace; the global `grace_period_minutes` applies when unset.
- `renewal_count` — informational; renewals are unlimited.
- `lease_epoch` — the D11 fencing token, recorded on every report (see the ADR).

## Defaults

All tuning lives in `TaskLeaseSettings` in `src/config/marcus_config.py`.

| Setting | Default | Meaning |
|---------|---------|---------|
| `silence_timeout_minutes` | `45.0` | Minutes of MCP silence after which the agent is presumed dead. |
| `budget_multiplier` | `3.0` | The budget ceiling is this many times the task's estimate. |
| `checkpoint_cadence_minutes` | `20.0` | Checkpoint interval the silence timeout must be at least twice of. |
| `default_hours` | `4.0` | Estimate used for the budget when a task carries none. |
| `grace_period_minutes` | `1.0` | Grace after expiry before recovery. |
| `min_lease_hours` | `0.0` | Lower bound on the budget. |
| `max_lease_hours` | `24.0` | Upper bound on the budget. |
| `warning_hours` | `0.0167` | Hours before expiry to emit a warning. |
| `enable_adaptive` | `true` | Scale the budget by estimate, priority and complexity. |

`AssignmentLeaseManager.from_settings` builds the manager from the dataclass or from a project's config dict, ignoring unknown and pre-D3 keys (`max_renewals`, `stuck_threshold_renewals`, `renewal_decay_factor`, `silence_multiplier`).
The manager refuses to start if `silence_timeout_minutes` is under twice `checkpoint_cadence_minutes` (`src/core/lease_invariants.py`).

## The budget ceiling

`budget_for(task)` computes the ceiling at claim time.

1. Start from the task's `estimated_hours`, or `default_hours` when the task has none.
2. When `enable_adaptive` is true, multiply by the matching `priority_multipliers` entry and by the first matching `complexity_multipliers` label.
3. Multiply by `budget_multiplier`.
4. Floor at one silence window and at `min_lease_hours`; cap at `max_lease_hours`.

A two-hour task on defaults gets a six-hour budget; a five-minute task gets forty-five minutes; a forty-hour task gets twenty-four.

## Lease Lifecycle

```
Task Assigned
     │
     ▼
create_lease() ──────────► lease_expires = now + silence timeout
     │                          budget_deadline = now + budget_for(task)
     │ ◄─── touch_lease(agent_id)    (any MCP tool call resets the silence clock)
     │
     ▼
Agent reports progress
     │
     ▼
renew_lease(task_id, progress) ──► lease_expires = min(now + silence timeout, budget_deadline)
     │
     ├──── on each renewal: update_timestamps append
     │     renewal_count += 1 (informational, unlimited)
     │
     ▼
Task completes → lease removed from active_leases
```

### `touch_lease(agent_id)`

A cheap extension triggered by the MCP dispatch loop in
`handlers.py`. **Any** tool call from an agent (including
`log_decision`, `log_artifact`, `report_blocker`, `get_task_context`, etc.)
counts as a touch — the agent proves it is alive by working. No explicit
heartbeat is ever required.

```python
# src/marcus_mcp/handlers.py
agent_id = arguments.get("agent_id") if arguments else None
if agent_id and hasattr(state, "lease_manager") and state.lease_manager:
    await state.lease_manager.touch_lease(agent_id)
```

### `renew_lease(task_id, progress, message, *, agent_id)`

The full renewal path used by `report_task_progress`.
It records the progress, appends an `update_timestamps` entry, and moves `lease_expires` to now plus the silence timeout, capped at `budget_deadline`.
Only the holder may renew; a lapsed lease still inside grace renews normally.

### No lease, no progress

If `report_task_progress` finds no live lease for the task, the report is refused before anything reaches the board (`status: "lease_recovered"`), and the agent is told to request its next task.
Earlier versions recreated the lease here; D3 deleted that, because the apology for a wrong recovery contradicted any ownership fence, and D3 makes recovery trustworthy enough not to need it.

### `recover_expired_lease(lease)`

Resets the task to `TODO`, clears `assigned_to`, builds a `RecoveryInfo`,
dual-writes to the board (field + comment), removes the lease from
`active_leases`, and finally invokes `on_recovery_callback(agent_id,
task_id)`.

See [Resilience and Task Recovery System](34-agent-recovery-system.md) for
the full recovery flow, including `RecoveryInfo`, worktree-aware handoff
instructions, and the terminal-status guard that gates recovery.

## LeaseMonitor

```python
class LeaseMonitor:
    """Background asyncio task that polls every 60 seconds."""

    check_interval = 60  # seconds
```

On each poll it walks `active_leases`, identifies expired leases (past
grace), calls `should_recover_expired_lease` (the terminal-status guard),
and invokes `recover_expired_lease` when the check says to recover.

### Lazy start on the correct event loop

The monitor **must not** be started during server setup. The HTTP
transport spins up its own event loop per request context, and a monitor
task created during setup is bound to a loop that no longer exists by the
time a request arrives. Instead the server exposes
`ensure_lease_monitor_running()` and the first `request_next_task` call
lazily starts the monitor on the live request loop:

```python
# src/marcus_mcp/tools/task.py
if hasattr(state, "ensure_lease_monitor_running"):
    await state.ensure_lease_monitor_running()
```

## Recovery Callback

`AssignmentLeaseManager` does not import server state. The server sets a
callback at initialization so in-memory assignment tracking
(`agent_tasks`, `tasks_being_assigned`) is cleaned whenever a lease is
recovered:

```python
# src/marcus_mcp/server.py
def _on_recovery(agent_id: str, task_id: str) -> None:
    if agent_id in self.agent_tasks:
        del self.agent_tasks[agent_id]
    self.tasks_being_assigned.discard(task_id)

self.lease_manager.on_recovery_callback = _on_recovery
```

Without this, the assignment filter would keep the recovered task marked
as taken and no agent could pick it up.

## Multipliers

`priority_multipliers` and `complexity_multipliers` scale the budget ceiling, never the silence timeout.

### Priority Multipliers

```python
"priority_multipliers": {
    "critical": 0.5,
    "high": 0.75,
    "medium": 1.0,
    "low": 1.5,
}
```

### Complexity Multipliers

```python
"complexity_multipliers": {
    "simple": 0.5,
    "complex": 1.5,
    "research": 2.0,
    "epic": 3.0,
}
```

> **Note on default inconsistency**: `AssignmentLeaseManager.__init__` has
> its own inline default of `max_lease_hours = 0.0333` (120 seconds), which
> differs from the `TaskLeaseSettings` dataclass default of `0.0833`
> (5 minutes). When the server initializes the manager using
> `TaskLeaseSettings`, the `TaskLeaseSettings` values take effect. The
> manager's inline default only applies if the manager is constructed
> without configuration arguments.

### Renewal Decay

```python
# With decay factor 0.9:
# Renewal 0: 1.00 * base
# Renewal 1: 0.90 * base
# Renewal 2: 0.81 * base
# ...
# bounded by min_lease_hours
```


## Configuration

```json
{
  "task_lease": {
    "silence_timeout_minutes": 45.0,
    "budget_multiplier": 3.0,
    "checkpoint_cadence_minutes": 20.0,
    "default_hours": 4.0,
    "warning_hours": 0.0167,
    "grace_period_minutes": 1.0,
    "min_lease_hours": 0.0,
    "max_lease_hours": 24.0,
    "enable_adaptive": true,
    "priority_multipliers": {
      "critical": 0.5,
      "high": 0.75,
      "medium": 1.0,
      "low": 1.5
    },
    "complexity_multipliers": {
      "simple": 0.5,
      "complex": 1.5,
      "research": 2.0,
      "epic": 3.0
    }
  }
}
```

Project-specific overrides live under each project in the project
registry; the global fallback lives in `src/config/marcus_config.py`.

## Statistics and Health

`AssignmentLeaseManager.get_statistics()` returns counts for active,
expiring-soon, expired, and stuck leases plus average and max renewal
counts. These are exposed through the `ping` health tool for operational
visibility.

## Related Documentation

- [Resilience and Task Recovery System](34-agent-recovery-system.md) — the
  primary spec describing the two liveness dials, touch-on-any-tool-call,
  the no-lease refusal, the recovery callback, the lazy monitor start,
  worktree-aware handoff, and the full kill → recover → reassign flow.
- [Orphan Task Recovery](33-orphan-task-recovery.md) — the startup
  reconciler and assignment monitor that act as a safety net for
  assignments left behind by non-lease code paths.
- [Agent Coordination](21-agent-coordination.md) — how task assignment,
  progress reporting, and the assignment filter fit together.
- [Smart Retry Strategy](46-smart-retry-strategy.md) — retry and backoff
  policies used alongside recovery.
