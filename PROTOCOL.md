# Marcus Agent Protocol

This document describes the protocol any agent must follow to participate in a
Marcus-coordinated project. It is the developer-facing complement to
[`prompts/Agent_prompt.md`](prompts/Agent_prompt.md), which is the agent-facing
version of the same specification.

**If you are building a runner** (a system that spawns agents and connects them
to Marcus), this document tells you what your agents must do.

**If you are an agent author** (writing the prompt your agent will follow),
use [`prompts/Agent_prompt.md`](prompts/Agent_prompt.md) directly — it is written
to be copied verbatim into your agent's system prompt.

---

## Overview

Marcus is an MCP server. Any agent that can call MCP tools can participate in a
Marcus project. The agent does not need to be Claude — it needs to speak
[Model Context Protocol](https://modelcontextprotocol.io/).

Marcus exposes its interface at `http://localhost:4298/mcp` by default.

---

## Required MCP Tools

An agent must be capable of calling these tools:

| Tool | Purpose |
|---|---|
| `create_project` | Bootstrap the project: decompose a description into tasks on the board |
| `register_agent` | Announce agent identity to Marcus at startup |
| `request_next_task` | Pull the next available task from the board |
| `get_task_context` | Fetch dependency artifacts before starting work |
| `report_task_progress` | Report progress at milestones (25/50/75/100%) |
| `report_blocker` | Report a blocking problem and receive AI suggestions |
| `log_decision` | Record an architectural decision to the board |
| `log_artifact` | Store a file reference (spec, design doc, schema) |

---

## Two Agent Roles

Every run has two distinct roles. One agent plays **project creator**; the rest
are **workers**. A single agent can play both roles (create, then immediately
enter the work loop).

### Project Creator

The creator calls `create_project` once to bootstrap the board:

```
create_project(
    description:  str,   # natural language description of what to build
    project_name: str,   # name for the project board
    options:      dict   # optional — e.g. {"num_agents": 3}
)
```

Returns `recommended_agents`, `project_id`, and the full task graph. When
`create_project` returns, tasks are on the board and immediately available.

### Workers

Workers call `register_agent` and enter the work loop. They never call
`create_project`.

---

## Agent Lifecycle

Every agent follows this sequence:

### 1. Register (once at startup)

```
register_agent(
    agent_id:  str,   # unique within this experiment
    name:      str,   # display name
    role:      str,   # e.g. "full-stack", "backend", "synthetic"
    skills:    list[str]
)
```

Call this exactly once. Do not re-register during a run.

### 2. Work Loop

```
a. request_next_task()
   → returns: task | {retry_after_seconds, retry_reason}

b. If no task: sleep retry_after_seconds, then goto (a)
   Do not exit. "No task" is transient — leases may be recovering,
   dependencies resolving, or the board completing. Stay alive.

c. If task received:
   i.   If task has dependencies: call get_task_context(task_id)
   ii.  Do the work
   iii. Call log_decision for any significant architectural choice made
   iv.  Call log_artifact for any file produced (spec, schema, design doc, etc.)
   v.   Call report_task_progress at 25%, 50%, 75%, 100%
        (if blocked: call report_blocker instead)
   vi.  Immediately call request_next_task (do not wait)
```

### 3. Exit

Exit when your runner signals completion. The board itself does not push
an exit signal to agents — that is the runner's responsibility. A common
pattern is to poll `get_experiment_status` in a separate monitor process
and shut agents down when the board is fully complete, but this is
runner-specific logic, not part of the agent protocol.

---

## The Three Agent Invariants

These are non-negotiable. Violating them breaks the coordination model:

1. **Agents self-select work.** Agents pull tasks via `request_next_task`.
   Marcus never pushes. No runner should pre-assign tasks to specific agents.

2. **Agents make all implementation decisions.** Marcus communicates WHAT to
   build and WHY. It never specifies HOW — no library choices, no file structure,
   no internal architecture. Two agents given the same task must be free to
   produce legitimately different implementations.

3. **Agents communicate exclusively through the board.** No agent-to-agent
   messages. No direct coordination. The board is the only channel.

---

## What Marcus Guarantees

- **Task isolation.** Only one agent holds a lease on any task at a time.
- **Dependency ordering.** `request_next_task` only returns tasks whose
  dependencies are complete. Agents never need to check this themselves.
- **Lease recovery.** If an agent dies mid-task, Marcus detects the expired
  lease and returns the task to the pool.
- **Artifact routing.** `get_task_context` returns artifacts from dependency
  tasks. Agents do not need to know which task produced an artifact — Marcus
  walks the DAG.
- **Scope annotation.** Dependency artifacts carry a `scope_annotation` field:
  `in_scope` (implement fully) or `reference_only` (do not implement, read for
  context only).

---

## Minimal Conforming Runner

A runner is any system that:

1. Creates the project — it calls `create_project` via Marcus (with
   `project_root` set) and makes sure `project_info.json` exists for the
   workers.
2. Starts N **worker sessions** — each registers once, then enters the work
   loop above, one task at a time.
3. Lets the board end the run — a session exits when `request_next_task`
   returns `status: "BOARD_DRAINED"`; the runner waits for every session to
   exit and tears down. No monitor agent is needed, and the runner must not
   restart a session Marcus has already recovered from.

The reference runner is
[`dev-tools/experiments/runners/session_launcher.py`](dev-tools/experiments/runners/session_launcher.py)
(run through `run_experiment.py`), which implements this pattern with agent
CLI sessions in tmux windows, one chore per harness invocation.

---

## Implementing a New Runner

To add support for a different agent runtime (LangGraph, AutoGen, Codex, etc.):

1. Connect your agent to the Marcus MCP server at `http://localhost:4298/mcp`.
2. Copy [`prompts/Agent_prompt.md`](prompts/Agent_prompt.md) into your agent's
   system prompt (or translate it to your runtime's prompt format).
3. Ensure your runner implements the create-project / worker-sessions /
   board-ends-the-run pattern described above.
4. Validate against the synthetic agent test harness (see issue
   [#383](https://github.com/lwgray/marcus/issues/383)) — if a synthetic agent
   can complete an experiment end-to-end with your runner, your integration is
   conforming.

---

## Quick Reference

```
Creator:   create_project(description, project_name) → board populated, tasks ready
Startup:   register_agent
Work loop: request_next_task → [get_task_context] → work → log_decision/log_artifact → report_task_progress(100%) → repeat
No task:   sleep retry_after_seconds → retry (do not exit)
Exit:      request_next_task returns BOARD_DRAINED (should_exit: true)
```

See [`prompts/Agent_prompt.md`](prompts/Agent_prompt.md) for the complete
agent-facing specification including artifact management, blocker workflow,
git conventions, and progress reporting format.
