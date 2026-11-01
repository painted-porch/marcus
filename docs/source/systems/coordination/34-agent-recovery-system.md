# Resilience and Task Recovery System

## Overview

Marcus runs many autonomous agents in parallel, each working on its own git
worktree. Agents can die in many ways: the tmux pane is killed, a network
connection drops, the process crashes, or the host machine reboots. When that
happens, the tasks those agents held must be detected, released, and handed
off to other agents so work continues — without losing the committed progress
the dead agent already made.

The **Resilience and Task Recovery System** is how Marcus does that.
It uses **lease-based liveness detection on two dials** (ADR-0012 D3: a silence timeout and a budget ceiling), a **worktree-aware handoff protocol**, and an **in-memory state cleanup callback** to safely return recovered tasks to the assignment pool.

This document describes the final implementation landed on the
`feature/resilience-wiring-cleanup` branch.

## Design Goals

1. **Detect dead agents quickly** — seconds to minutes, not hours.
2. **Minimize false positives** — don't recover tasks from agents that are
   simply slow. Slow is not dead.
3. **Preserve committed work** — if the dead agent made real progress and
   committed it to their branch, the next agent should build on it, not
   restart from scratch.
4. **Stay loosely coupled** — agents don't need to know about leases or send
   explicit heartbeats; any MCP tool call proves they're alive.
5. **Match Marcus's board-mediated pattern** — no WebSockets, no bespoke
   heartbeat protocol. Polling plus a board as the source of truth.

## Architecture at a Glance

```
┌─────────────────────────────────────────────────────────────────────┐
│                          MarcusServer                              │
│                                                                     │
│  ┌─────────────────────┐      ┌──────────────────────────────┐    │
│  │ AssignmentLease-    │◄─────┤ LeaseMonitor (asyncio task)  │    │
│  │ Manager             │      │ polls every 60s              │    │
│  │                     │      └──────────────────────────────┘    │
│  │  active_leases      │                                           │
│  │  on_recovery_       │──────┐                                    │
│  │    callback         │      │  cleans agent_tasks,               │
│  └──────────┬──────────┘      │  tasks_being_assigned              │
│             │                 ▼                                    │
│             │         ┌──────────────────┐                         │
│             │         │ state (server)   │                         │
│             │         │ agent_tasks{}    │                         │
│             │         │ tasks_being_     │                         │
│             │         │   assigned{}     │                         │
│             │         └──────────────────┘                         │
│             │                                                      │
│             │  touch_lease() on every MCP tool call                │
│             │                                                      │
│  ┌──────────▼──────────┐                                           │
│  │ handlers.py         │                                           │
│  │ (MCP tool dispatch) │◄──── agents call tools                    │
│  └─────────────────────┘                                           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
                        ┌───────────────┐
                        │ Kanban Board  │
                        │ (source of    │
                        │  truth for    │
                        │  task state)  │
                        └───────────────┘
```

## Key Components

### 1. AssignmentLeaseManager

**File**: `src/core/assignment_lease.py`

The lease manager tracks a lease for every in-progress task assignment.
A lease is a lightweight record with:

- `agent_id` — which agent holds the task
- `task_id` — the task being leased
- `assigned_at` — when it was first handed out
- `lease_expires` — when the lease would expire without renewal
- `renewal_count` — how many times it has been renewed
- `progress_percentage` — the last known progress
- Update history — timestamps used to compute the agent's median update
  interval

Three important methods drive lease lifecycle:

- `touch_lease(agent_id)` — a cheap extension. Called on any MCP tool
  activity from the agent. Does not require progress data.
- `renew_lease(task_id, progress)` — a full renewal with progress data. Called
  when the agent explicitly reports progress.
- `recover_expired_lease(lease)` — resets the task to `TODO`, clears
  `assigned_to`, builds a `RecoveryInfo` object, dual-writes to the board,
  and invokes `on_recovery_callback`.

#### The two liveness dials (ADR-0012 D3)

The lease used to be one dial that meant both "is the agent alive?" and "how long may it hold the task?", so detecting a dead agent was tied to how big and how chatty its task was.
D3 splits it in two.

| Dial | Question | Default | How it moves |
|------|----------|---------|--------------|
| Silence timeout | Is the agent alive? | 45 minutes | Every MCP call (`touch_lease`) and every progress report (`renew_lease`) resets `lease_expires` to now plus the timeout. |
| Budget ceiling | How long may it hold the task? | 3x the estimate | Fixed at claim time as `budget_deadline`; heartbeats never extend `lease_expires` past it. |

The budget is `budget_multiplier` times the task's estimate, scaled by `priority_multipliers` and `complexity_multipliers`, never less than one silence window, and never more than `max_lease_hours`.
A quiet agent doing focused work for forty minutes is not recovered.
A dead agent's task is reclaimed when the silence timeout elapses.
A chatty agent that never finishes is reclaimed when its budget runs out.

The silence timeout must be at least twice the checkpoint cadence the session contract obliges (`checkpoint_cadence_minutes`, default 20); `src/core/lease_invariants.py` enforces that when the manager is built.

### 2. LeaseMonitor

**File**: `src/core/assignment_lease.py`

A background `asyncio` task that wakes up every 60 seconds and walks
`active_leases`, calling `check_expired_leases`. For each expired lease it
calls `should_recover_expired_lease` (the cadence-aware check) and, if the
check says recover, calls `recover_expired_lease`.

**Critical detail — event loop affinity**: the `LeaseMonitor` must run on
`uvicorn`'s event loop, not on whatever loop happened to exist at server
setup time. The HTTP transport starts its own loop for each request context,
and a monitor created during setup will be bound to the wrong loop and never
fire. To solve this, the server exposes `ensure_lease_monitor_running()` and
the first call to `request_next_task` (handled on the correct loop)
lazily starts the monitor.

```python
# src/marcus_mcp/tools/task.py
if hasattr(state, "ensure_lease_monitor_running"):
    await state.ensure_lease_monitor_running()
```

### 3. What expiry means

**File**: `src/core/assignment_lease.py` — `should_recover_expired_lease`

Under D3, expiry *is* the liveness decision.
A lease expires only when the agent has been silent for the whole silence window or has spent its budget, so there is no further cadence heuristic to consult.
The one remaining check is defense in depth: a task the board already shows as `DONE` or `BLOCKED` is never recovered, because its lease is stale bookkeeping and recovering it would hand finished work to a fresh agent.
Each such skip increments `recoveries_skipped_terminal_status`, since it points at a lease that should have been released upstream.

### 4. Recovery Callback Pattern

**File**: `src/marcus_mcp/server.py`

When `recover_expired_lease` fires, the task is reset on the board — but the
server also holds **in-memory** tracking of who owns what:

- `state.agent_tasks[agent_id]` — what the agent is currently assigned
- `state.tasks_being_assigned` — tasks mid-assignment

If those aren't cleaned up, the assignment filter will keep refusing to
offer the recovered task to anyone, because it still looks taken.

The lease manager solves this with a callback, set by the server:

```python
self.lease_manager.on_recovery_callback = _on_recovery
```

Inside `_on_recovery`, the server removes the entry from `agent_tasks` and
`tasks_being_assigned`. This keeps `AssignmentLeaseManager` free of direct
dependencies on server state while still wiring the two together.

### 5. Touch-on-Any-Tool-Call

**File**: `src/marcus_mcp/handlers.py`

Marcus never asks agents to send heartbeats. Instead, **every MCP tool call
from an agent acts as a heartbeat**. The dispatch loop inspects the tool
arguments and, if an `agent_id` is present, calls:

```python
await state.lease_manager.touch_lease(agent_id)
```

This means `log_decision`, `log_artifact`, `report_blocker`,
`get_task_context`, and every other tool the agent might call all keep the
lease alive. Agents prove they are working by working.

### 6. No lease, no progress (the re-grant is gone)

Earlier versions recreated an agent's lease when it reported progress after a recovery, as an apology for recovery having been wrong.
That apology and any ownership fence contradict each other, which is why the D11 fence could not be enforced while it existed.
D3 makes recovery trustworthy, so the apology is deleted.
An `in_progress` report from an agent with no live lease is refused before anything reaches the board, with `status: "lease_recovered"` and a message telling the agent to request its next task.
A holder whose lease lapsed but is still inside the grace period may still renew; grace is exactly the window in which Marcus has not yet given up on it.

### 7. RecoveryInfo

**File**: `src/core/models.py`

A structured record attached to the task model when recovery happens.

```python
@dataclass
class RecoveryInfo:
    recovered_at: datetime
    recovered_from_agent: str
    previous_progress: int
    time_spent_minutes: float
    recovery_reason: str
    previous_agent_branch: Optional[str]
    instructions: str
    recovery_expires_at: datetime  # 24h window
```

`RecoveryInfo` is **dual-written**:

1. Set on `task.recovery_info` (in-memory, source of truth for handoff)
2. Appended as a Kanban comment (durable audit trail)

Because `recovery_info` is in-memory only, `server.refresh_project_state`
explicitly **captures and re-applies** it across refreshes so that a refresh
can't silently drop the handoff context.

### 8. Worktree-Aware Recovery Instructions

Every Marcus agent works on its own git branch: `marcus/<agent_id>`. When an
agent dies, commits they made still live on that branch. The recovery
instructions tell the **next** agent exactly how to pick them up:

```
git merge marcus/<dead-agent> --no-edit
git log marcus/<dead-agent>
```

The next agent merges committed work, reviews what was done, then continues
from where the previous agent left off. This is the difference between
"recovered" and "redone."

### 9. Recovery Handoff in Task Instructions

**File**: `src/marcus_mcp/tools/task.py` — `build_tiered_instructions`

Task instructions are built in layers. A new **Layer 1.1: Recovery Handoff**
sits just above the normal task body. When `task.recovery_info` is set and
not expired (24h window), the layer is populated with the full handoff
message: previous agent ID, previous progress, time spent, recovery reason,
and the git merge instructions.

The next agent sees the handoff as soon as they receive the task — no
separate notification, no risk of missing it.

### 10. Assignment Filter Respects `assigned_to`

**File**: `src/marcus_mcp/tools/task.py` — `_find_optimal_task_original_logic`

The assignment filter honors **both** in-memory tracking (`agent_tasks`,
persistence) **and** the board-level `assigned_to` field. This has two
important effects:

1. Design tasks are assigned to the literal string `"Marcus"` and are
   handled internally by `_run_design_phase`. The filter skips them so no
   agent tries to grab them.
2. Recovered tasks have `assigned_to` cleared by `recover_expired_lease`.
   Because the filter checks `assigned_to is None`, the task immediately
   re-enters the pool.

### 11. Gridlock Detector

**File**: `src/core/gridlock_detector.py`

A separate safety net. Rather than counting raw request volume (which
produces false positives under Marcus's 30-second polling pattern), the
detector looks at **task state**: if every `TODO` is blocked by unfinished
dependencies and there are **zero** in-progress tasks, the system is
gridlocked. It also tracks distinct requesting agents for metrics.

## Configuration

All resilience tuning lives in `src/config/marcus_config.py` under `TaskLeaseSettings`.

| Setting | Default | Meaning |
|---------|---------|---------|
| `silence_timeout_minutes` | `45.0` | Minutes of MCP silence after which the agent is presumed dead. |
| `budget_multiplier` | `3.0` | The budget ceiling is this many times the task's estimate. |
| `checkpoint_cadence_minutes` | `20.0` | The session contract's checkpoint interval; the silence timeout must be at least twice it. |
| `default_hours` | `4.0` | Estimate used for the budget when a task carries none. |
| `grace_period_minutes` | `1.0` | Grace after expiry before recovery. |
| `min_lease_hours` | `0.0` | Lower bound on the budget (the silence window is always a floor too). |
| `max_lease_hours` | `24.0` | Upper bound on the budget; no task is held longer. |
| `warning_hours` | `0.0167` | Hours before expiry to emit a warning. |
| `enable_adaptive` | `true` | Scale the budget by estimate, priority and complexity. |

`priority_multipliers` and `complexity_multipliers` scale the budget, not the heartbeat.
The pre-D3 keys `max_renewals`, `stuck_threshold_renewals`, `renewal_decay_factor` and `silence_multiplier` are still accepted so older config files load, and are ignored.

## Full Recovery Flow (Agent Dies)

The following trace shows everything that happens from assignment to
handoff.

```
T+0s    Agent-A requests a task.
        ├─ Task assigned: status=IN_PROGRESS, assigned_to=Agent-A
        ├─ state.agent_tasks[Agent-A] = task
        └─ AssignmentLeaseManager creates lease (expires in 45 min of silence; budget 3x estimate)

T+15s   Agent-A calls log_decision(...)
        └─ handlers.py touches lease → extended

T+40s   Agent-A calls report_task_progress(progress=15)
        ├─ lease renewed with progress
        └─ silence clock reset: expires 45 min from now (never past the budget)

T+55s   ☠️  Agent-A's tmux pane is killed. No more tool calls.

T+46m   Lease expires past grace (45 min of silence + 1 min grace). LeaseMonitor wakes up (60s interval).
        ├─ should_recover_expired_lease(lease):
        │   ├─ board status is IN_PROGRESS (not terminal) → recover
        │
        └─ recover_expired_lease(lease):
            ├─ Build RecoveryInfo(
            │     recovered_from_agent="Agent-A",
            │     previous_progress=15,
            │     time_spent_minutes=2.0,
            │     recovery_reason="lease_expired",
            │     previous_agent_branch="marcus/Agent-A",
            │     instructions="git merge marcus/Agent-A ...",
            │     recovery_expires_at=now+24h
            │   )
            ├─ task.recovery_info = <info>
            ├─ task.assigned_to = None
            ├─ Kanban: status=TODO, assigned_to=None
            ├─ Kanban comment with handoff text
            ├─ active_leases.pop(task_id)
            ├─ persistence.remove_assignment(Agent-A)
            └─ on_recovery_callback(Agent-A, task_id)
                └─ server cleans:
                    ├─ state.agent_tasks.pop(Agent-A)
                    └─ state.tasks_being_assigned.discard(task_id)

T+180s  Agent-B calls request_next_task.
        ├─ ensure_lease_monitor_running() (already running)
        ├─ Assignment filter walks TODO tasks:
        │   ├─ task.status == TODO ✓
        │   ├─ task.id not in all_assigned_ids ✓
        │   ├─ task.assigned_to is None ✓
        │   └─ task selected
        │
        ├─ build_tiered_instructions(task, agent=Agent-B):
        │   └─ Layer 1.1: Recovery Handoff
        │       "⚠️ RECOVERY ADDENDUM — recovered from Agent-A
        │        git merge marcus/Agent-A --no-edit
        │        git log marcus/Agent-A
        │        Previous agent reached 15% ..."
        │
        └─ Lease created for Agent-B (fresh 45-minute silence window, fresh budget)

T+181s  Agent-B runs git merge marcus/Agent-A, sees Agent-A's commits,
        continues the task from 15%.
```

## Design Task Handling

Design tasks are a special case. They are created with
`assigned_to="Marcus"` and handled internally by `_run_design_phase` as a
background task on the server. The assignment filter treats any task whose
`assigned_to` is `"Marcus"` as off-limits to agents. When the design task
completes, it is marked `done` on the board, which unblocks its dependents
through the normal dependency system.

This is why the assignment filter must check `assigned_to` and not just the
server's in-memory `agent_tasks`: the Marcus-owned design tasks don't live
in `agent_tasks` at all.

## Key Architectural Decisions

### Polling over WebSocket heartbeats

Marcus is board-mediated. Every durable piece of state lives on the board.
Adding a parallel heartbeat channel would introduce a second source of
truth with its own failure modes. Polling the leases every 60 seconds fits
the existing pattern and is cheap: it's an in-memory walk of a dict.

### Two dials over one lease (ADR-0012 D3)

One lease that meant both "alive?" and "how long?" forced a choice: a short lease recovered quiet-but-working agents, a long lease let a dead agent hold a large task for hours.
There is no single value that avoids both, which is why the silence timeout and the budget ceiling are separate settings.
An earlier cadence heuristic (median update interval times a multiplier) tried to adapt one dial per agent; it was removed with D3 because the explicit timeout is the liveness decision and needs no inference.

### Touch-on-any-tool as the liveness signal

Explicit heartbeats would require every agent to opt in and stay in sync
with the protocol. Touching the lease on any MCP tool call means the
heartbeat is implicit in real work. Agents that are doing things stay
alive. Agents that are stuck or dead stop touching. That is exactly the
signal we want.

### No re-grant on progress report (ADR-0012 D3)

Earlier versions recreated a lease when an agent reported progress after a recovery, treating recovery as possibly wrong.
That compensation contradicted any ownership fence, so D3 deleted it: a report with no live lease is refused and the agent is told to request its next task.

### Callback for state cleanup

`AssignmentLeaseManager` does not import server state. The server injects
a callback at construction time, which the manager fires on recovery. This
keeps the lease module independently testable and prevents a circular
dependency.

### Lazy monitor start on the correct loop

The HTTP transport spins up its event loop per request context. A monitor
created during `__init__` is bound to a loop that no longer exists by the
time a request arrives. Deferring monitor start to the first
`request_next_task` call — which runs on the live request loop — pins the
monitor to the right loop and keeps it alive for the server lifetime.

## Testing

Coverage for this system is split across unit, integration, and handoff
tests.

| Test | Path | Covers |
|------|------|--------|
| Assignment lease unit | `tests/unit/core/test_assignment_lease.py` | Lease lifecycle, touch/renew, persistence, circuit breaker. |
| D3 liveness | `tests/unit/core/test_d3_liveness.py` | The silence timeout, the budget ceiling, their persistence, the invariant. |
| Gridlock detector | `tests/unit/core/test_gridlock_detector.py` | Task-state gridlock detection. |
| Recovery handoff | `tests/unit/mcp/test_recovery_handoff.py` | Layer 1.1 instructions and 24h expiry. |
| Resilience end-to-end | `tests/integration/test_resilience_e2e.py` | Full kill → recover → reassign flow. |

## Troubleshooting

### Recovered tasks are not reassigned to any agent

Check the assignment filter in `_find_optimal_task_original_logic`. A task
will be skipped if any of the following are still true:

- `task.assigned_to` is not `None`
- `task.id` is still in `state.agent_tasks[some_agent]`
- `task.id` is still in `state.tasks_being_assigned`

All three must be cleared during recovery. If one is not, the
`on_recovery_callback` wiring in `server.py` is broken or the lease
manager was constructed without a callback set.

### Leases never expire even though agents are dead

The `LeaseMonitor` is probably bound to the wrong event loop. Confirm that
`ensure_lease_monitor_running()` is being called from `request_next_task`
and that the first call actually runs. You can add a debug log in the
monitor's poll loop to verify it is ticking.

### Recovery fires on agents that are actually alive

Either the silence timeout is shorter than your agents' quiet stretches, or agents are not touching the lease at all.
Options:

1. Raise `silence_timeout_minutes` (default `45`); keep it at least twice `checkpoint_cadence_minutes`.
2. If recovery fires at the budget ceiling, raise `budget_multiplier` or check the task's estimate.
3. Confirm that the tool the agent is calling passes `agent_id` in its arguments; if it does not, `touch_lease` is never called.

### `recovery_info` disappears after a refresh

`refresh_project_state` in `server.py` must capture `recovery_info` before
the refresh and re-apply it afterward. If this block is removed or
reordered, handoff information is silently lost. The recovery_info field
is in-memory only — it is not stored by the Kanban provider. The Kanban
comment remains as an audit trail, but the next agent will not see the
Layer 1.1 handoff in their task instructions.

### Design tasks are being offered to agents

Confirm the assignment filter is checking `task.assigned_to != "Marcus"`.
Design tasks rely on this exact string match.

### A false-positive recovery left an orphaned agent

Normally the agent's next `report_task_progress` recreates the lease.
If that is not happening, verify that the progress-report handler calls
into `AssignmentLeaseManager` when the lease is missing rather than
failing. Check the logs for "lease not found, recreating" or similar.

## Related Documentation

- [Assignment Lease System](35-assignment-lease-system.md) — deeper dive
  into the lease data model and adaptive duration math.
- [Orphan Task Recovery](33-orphan-task-recovery.md) — complementary
  recovery path for tasks left behind by non-lease mechanisms.
- [Agent Coordination](21-agent-coordination.md) — how task assignment,
  progress reporting, and the assignment filter fit together.
- [Smart Retry Strategy](46-smart-retry-strategy.md) — retry and backoff
  policies used alongside recovery.
