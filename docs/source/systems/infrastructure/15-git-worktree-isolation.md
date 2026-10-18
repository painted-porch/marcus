# Git Worktree Isolation

## Status

| Field | Value |
|-------|-------|
| Status | Implemented |
| Version | 1.0 |
| Date | 2026-03-31 |

## Problem

Marcus runs multiple worker agents in parallel during experiments. All agents produce code that ultimately lives in a single `implementation/` directory on the `main` branch. Without isolation, parallel agents overwrite each other's files — a failure mode we call **Type A failure**.

A naive fix would be to let agents work in isolation and merge everything at the end. But Marcus decomposes projects into a DAG of tasks with fan-in dependencies. For example, "Test Time Widget" depends on "Implement Time Widget" and needs to see its code before it can run. Integration-only-at-the-end does not work because dependent tasks need to see completed work mid-experiment.

The system needs a mechanism that provides:

1. **Isolation** — agents never interfere with each other's in-progress work
2. **Visibility** — when a task completes, its code becomes visible to dependent tasks immediately
3. **Attribution** — every line of code is traceable to the agent that wrote it

## Solution

Three mechanisms work together to solve this:

### 1. Per-agent git worktrees

Each worker agent gets its own git worktree checked out on a dedicated branch named `marcus/{agent_id}`. The worktree is a separate directory with its own working tree but shares the same `.git` object store as the main repository.

### 2. Per-worktree git identity

Each worktree has `git config user.name` set to the agent's ID. This ensures `git blame` and `git log` attribute commits to the correct agent without relying on global git configuration.

### 3. Merge-on-completion

When a task passes validation and is marked DONE, Marcus merges the agent's branch back to `main`. Dependent tasks then start from an updated `main` that contains all completed work. If the merge produces conflicts, Marcus aborts the merge and sends the agent back to resolve them.

## Responsibility Matrix

| Actor | Responsibility |
|-------|---------------|
| `run_experiment.py` | `git init`, create `implementation/` on `main` |
| `spawn_agents.py` | Creates worktrees per worker via `_create_agent_worktree()`. Sets git identity. Sets working directory in agent prompt. |
| Marcus MCP (`task.py`) | `_merge_agent_branch_to_main()` in `report_task_progress()` after validation passes. Sends agent back on conflicts. |
| Worker agent | Works in its worktree, commits to its branch, resolves conflicts if sent back |
| Integration agent | Works on `main` (everything already merged), verifies and fixes |
| Epictetus | Uses `git blame` to attribute code per agent for contribution analysis |

## Directory Structure

```
experiment_dir/
├── config.yaml                              <- NOT in git
├── project_info.json                        <- NOT in git
├── prompts/                                 <- NOT in git
├── logs/                                    <- NOT in git
├── implementation/                          <- .git HERE, main branch
│   ├── .git/
│   ├── CLAUDE.md
│   ├── docs/architecture/...               <- design artifacts
│   ├── package.json                        <- scaffold
│   └── src/                                <- scaffold + merged code
└── worktrees/                               <- dedicated worktree directory
    ├── agent_unicorn_1/                     <- worktree, branch marcus/agent_unicorn_1
    │   ├── .git -> ../../implementation/.git
    │   └── (agent 1's isolated work)
    └── agent_unicorn_2/                     <- worktree, branch marcus/agent_unicorn_2
        ├── .git -> ../../implementation/.git
        └── (agent 2's isolated work)
```

The `worktrees/` directory is outside `implementation/` to avoid nested
tracking issues, and separate from experiment infrastructure so agents
only see code files. Each worktree's `.git` is a symlink to the shared
object store — merges are local operations with no network overhead.

**Stale .git protection:** `run_experiment.py` removes any `.git` at the
experiment root on startup. Only `implementation/.git` should exist. A
stale root `.git` (from `rm -rf *` not removing hidden files) would cause
worktrees to branch from the wrong repo.

## Branch Structure

```
main ─────●──────●──────────────────●────●────●────────->
          |      |                  |    |    |
          |  design artifacts   merge1 merge2 |
          |  (Marcus)                         integration
          |
          ├── marcus/agent_unicorn_1
          |   ●──●──●──●
          |
          └── marcus/agent_unicorn_2
              ●──●──●──●
```

The `main` branch accumulates completed work. Each agent branch diverges from `main` at the point the agent started its current task and is merged back when the task passes validation.

## Task Lifecycle with Worktrees

```
┌─────────────────────────────────────────────────────────────────┐
│                        TASK LIFECYCLE                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. Agent gets task                                             │
│     └──> Works in worktree, commits to marcus/{agent_id}        │
│                                                                 │
│  2. Agent reports 100%                                          │
│     └──> Validation gate runs                                   │
│                                                                 │
│  3. Validation fails?                                           │
│     └──> Agent fixes, stays on branch, NO merge                 │
│                                                                 │
│  4. Validation passes?                                          │
│     └──> Marcus merges branch to main                           │
│                                                                 │
│  5. Merge conflicts?                                            │
│     └──> Marcus aborts merge                                    │
│     └──> Sends agent back: "git merge main, fix conflicts,      │
│          commit, report again"                                  │
│                                                                 │
│  6. Merge succeeds?                                             │
│     └──> Task truly DONE, code on main                          │
│     └──> Dependent tasks can now start                          │
│                                                                 │
│  7. Agent gets next task                                        │
│     └──> Runs `git merge main --no-edit` in worktree            │
│     └──> Sees all previously completed work                     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Per-Task Visibility (GH-302)

Worktrees are created once at spawn time, not per-task. When an agent
picks up a new task, it runs `git merge main --no-edit` to incorporate
all previously merged code. This is enforced in two places:

1. **Shell script** (belt): before Claude starts, the worker script
   merges main into the worktree
2. **Worker prompt** (suspenders): step 5 of the task workflow says
   "FIRST: run `git merge main --no-edit` to get latest completed work"

This ensures that when Agent 2 picks up a task that depends on Agent 1's
completed work, it sees that code — because Agent 1's branch was merged
to main on completion, and Agent 2 merges main before starting.

## Interface Comparison

Marcus has three interfaces. Each handles worktree creation differently:

| Interface | Who creates worktrees? | Where? |
|-----------|----------------------|--------|
| `/marcus` skill | `spawn_agents.py` (automatic) | In experiment runner |
| Posidonius | `spawn_agents.py` via `run_experiment.py` (automatic) | Same code path |
| MCP direct | User (manual `git worktree add`) | User's responsibility |

For the `/marcus` skill and Posidonius interfaces, worktree creation is fully automated. When using the MCP server directly (without the experiment runner), the user is responsible for setting up git worktrees manually.

## What's NOT Handled (Deferred)

- **Feature branches** — There is no Feature entity. Tasks branch directly from `main`. A feature branch abstraction may be added later if experiments grow to need it.
- **Worktree cleanup** — Worktrees are not cleaned up after experiments. The entire experiment directory is disposable, so cleanup is unnecessary.
- **Cross-agent worktree reuse** — Each task gets a new worktree from the current `main`. Worktrees are not reused across tasks.

## Implementation Files

- `dev-tools/experiments/runners/spawn_agents.py` — `_create_agent_worktree()`, `spawn_worker()`, `create_worker_prompt()`
- `src/marcus_mcp/tools/task.py` — `_merge_agent_branch_to_main()` in `report_task_progress()`

## Prerequisite: Project Scaffolding

Worktree isolation requires project scaffolding (Phase A.5) to be effective.
Without scaffolding on main before worktrees branch, each agent independently
scaffolds the same project — duplicating work and creating merge conflicts on
every shared file. See [16-project-scaffolding.md](16-project-scaffolding.md).

## See Also

- [Project Scaffolding](16-project-scaffolding.md) — shared infrastructure prerequisite for worktrees
- [Design Autocomplete](../coordination/52-design-autocomplete.md) — Phase A, produces architecture doc that scaffolding reads
- [Workspace Isolation and Feature Context](../../design/workspace-isolation-and-feature-context.md) — earlier design notes on isolation
- GitHub Issues: [#250](https://github.com/painted-porch/marcus/issues/250), [#249](https://github.com/painted-porch/marcus/issues/249), [#300](https://github.com/painted-porch/marcus/issues/300)
