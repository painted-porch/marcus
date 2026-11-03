---
name: marcus
description: >
  Launch Marcus multi-agent experiments with independent agent CLI sessions in tmux.
  Use when the user says "build X with Marcus", "run a Marcus experiment",
  "start N sessions with Marcus", "spawn N agents with Marcus", "/marcus", or
  references the Marcus multi-agent framework. This skill handles experiment
  setup, config generation, and starting persistent worker sessions that
  coordinate via the Marcus MCP server. Each session runs in its own tmux
  window with full autonomy. NOT for ClawTeam — this is
  specifically for the Marcus MCP-based multi-agent system.
  NOTE: "Marcus" is also the name of the MCP server that agents use for coordination.
  The /marcus skill launches experiments that USE the Marcus MCP server — they are
  complementary, not competing. The MCP server must be running before invoking this skill.
  The Marcus MCP server is agent-agnostic; this skill supports Anthropic's claude CLI,
  OpenAI's codex CLI, and Google's gemini CLI as agent harnesses (selected via --harness).
user-invocable: true
argument-hint: "<project description> [--name \"Project Name\"] [--sessions N | --agents N] [--complexity prototype|standard|enterprise] [--decomposer contract_first|feature_based] [--epictetus] [--model <model>] [--harness claude|codex|gemini]"
---

# Marcus Multi-Agent Experiment Launcher (Gemini)

You are helping the user launch a Marcus multi-agent experiment. Marcus uses an MCP
server to coordinate independent agent CLI sessions, each running in its own tmux
window. A session is one persistent worker identity that does one task per
invocation until the board drains; Marcus itself starts nothing (epic #706).

This skill is invoked from within the Gemini CLI. When the user runs `/marcus` from
inside Gemini, the typical (but not required) intent is to run **gemini** sessions —
pass `--harness gemini` to make that explicit. The skill also accepts `--harness
claude` or `--harness codex` to run the other supported agents from a Gemini driver
session if that's what the user wants.

## What You Do

1. Parse the user's request to extract: project description, session count, harness, model
2. Create an experiment directory with config.yaml and project_spec.md
3. Run `run_experiment.py`, which starts the worker sessions in tmux
4. Report the tmux session name so the user can attach

## Discovering Paths

Find the Marcus repo root via the installed package:

```bash
MARCUS_ROOT=$(python3 -c "from pathlib import Path; import marcus_mcp; print(Path(marcus_mcp.__file__).parent.parent.parent)")
```

From there:
- **Run script**: `${MARCUS_ROOT}/dev-tools/experiments/runners/run_experiment.py`
- **Templates**: `${MARCUS_ROOT}/dev-tools/experiments/templates/`

If the import fails, Marcus isn't installed. Tell the user:
```
Marcus is not installed. Follow the setup instructions:
  git clone https://github.com/lwgray/marcus.git
  cd marcus && pip install -e .
```

## How to Parse Arguments

The user's input comes in as `$ARGUMENTS`. Extract:
- **Project description**: Everything that describes what to build
- **Project name**: Look for `--name "Some Name"` or `--name some_name`. If not provided, derive a short name from the description.
- **Session count**: Look for "N sessions", "--sessions N", "N agents", "--agents N", "with N workers". Default: 2. Written as `sessions: N` in config.yaml; exactly N persistent sessions run for the whole project.
- **Complexity**: Look for "--complexity prototype|standard|enterprise". Default: "prototype"
- **Decomposer**: Look for "--decomposer contract_first|feature_based". Default: "contract_first" (as of v0.3.4). This controls Marcus's task decomposition strategy (GH-320):
  - `contract_first` — default. Generates interface contracts before decomposition. Board is fully populated before any agent starts (no Phase A race). Each agent owns one side of a contract. Best for tightly-coupled projects (games, dashboards, state machines).
  - `feature_based` — legacy path, splits tasks by functional requirement. Fine for loosely-coupled projects where features don't share files.
- **Epictetus mode**: Look for `--epictetus` flag. Default: not set (false). When present, the launcher does NOT tear down the tmux session after the sessions go home — it stays alive for Epictetus post-experiment interrogation.
- **Agent model**: Look for `--model <value>`. Default: not set — Marcus reads `ai.model` from `config_marcus.json` and uses that same value for the worker sessions' agent CLI (so by default Planners and Agents share one model). When `--model X` is provided, X overrides for THIS run only and applies to every session. The same string is passed verbatim to whichever harness is active — accepts `claude --model` values (e.g. `sonnet`, `opus`, `haiku`), `codex --model` values (e.g. `gpt-5-codex`, `o3`), or `gemini --model` values (e.g. `gemini-2.5-pro`, `gemini-2.5-flash`). No client-side validation against per-harness namespaces — invalid model names surface as CLI errors inside the session windows. Affects ONLY the worker sessions — Marcus's Planner model continues to read from `config_marcus.json`.
- **Agent harness**: Look for `--harness claude|codex|gemini`. Default: `claude` (matches `run_experiment.py` default). When invoked from inside Gemini CLI, you typically want `--harness gemini` — but the runner does not auto-detect the caller. `gemini` runs `gemini --skip-trust --yolo` (bypass the trusted-directory dialog and auto-approve all tool calls — required for unattended tmux windows). All sessions in a single run use the same harness; mixed-harness teams are out of scope for v1. The runner pre-flights `which <cli>` and fails fast if the binary is missing.

Examples:
- `/marcus Build a snake game with 3 agents` -> description="Build a snake game", name="snake_game", sessions=3, complexity="prototype", decomposer="contract_first", harness="claude"
- `/marcus Build a TODO CLI with 2 agents --harness gemini` -> description="Build a TODO CLI", name="todo_cli", sessions=2, complexity="prototype", decomposer="contract_first", harness="gemini"
- `/marcus Build a TODO CLI --harness gemini --model gemini-2.5-pro` -> description="Build a TODO CLI", name="todo_cli", sessions=2, complexity="prototype", decomposer="contract_first", model="gemini-2.5-pro", harness="gemini"
- `/marcus Build a snake game with 4 agents --harness gemini --model gemini-2.5-flash` -> description="Build a snake game", name="snake_game", sessions=4, complexity="prototype", decomposer="contract_first", model="gemini-2.5-flash", harness="gemini"

## Step-by-Step Execution

### Step 1: Use the Current Working Directory

Write all files into the current working directory. Do NOT create or mkdir a new directory.
`run_experiment.py` will create the subdirectories it needs (`prompts/`, `logs/`, `implementation/`, `worktrees/`).

```
<cwd>/
├── config.yaml       (you create this)
├── project_spec.md   (you create this)
├── prompts/          (created by run_experiment.py: one prompt and one script per session)
├── logs/             (created by run_experiment.py: runner.log)
├── implementation/   (created by run_experiment.py, git repo on main where work merges)
└── worktrees/        (one git worktree per session, worktrees/<agent_id>)
```

### Step 2: Generate project_spec.md

Write the user's project description **as-is** to `project_spec.md`. Do NOT rewrite,
expand, restructure, or add sections to it. Marcus handles task decomposition — the
spec is just the raw input from the user.

### Step 3: Generate config.yaml

**CRITICAL: Always write a fresh config.yaml from the template below. Do NOT read or
preserve values from any existing config.yaml in the directory. Overwrite it completely.**

Use this exact format — field names are case-sensitive:

```yaml
project_name: "<--name value if provided, otherwise derived from description>"
project_spec_file: "project_spec.md"

# How many persistent worker sessions to start (default 2).
sessions: <parsed session count>

# Harness selection: omit the field (defaults to 'claude') or set explicitly.
# CLI flag --harness on run_experiment.py overrides this value.
harness: "<parsed harness>"  # "claude" (default), "codex", or "gemini"

project_options:
  complexity: "<parsed complexity>"
  provider: "sqlite"
  mode: "new_project"
  decomposer: "<parsed decomposer>"

agents:
  - id: "agent_unicorn_1"
    name: "Unicorn Developer 1"
    role: "full-stack"
    skills:
      - "python"
      - "javascript"
      - "typescript"
      - "react"
      - "fastapi"
      - "sqlalchemy"
      - "postgresql"
      - "database-design"
      - "rest-api"
      - "jwt"
      - "security"
      - "pytest"
      - "integration-testing"
  # Repeat for each session identity...

timeouts:
  project_creation: 900
```

**Complexity heuristic:**
- "prototype" — Simple single-page apps, scripts, small tools
- "standard" — Most projects (APIs, full-stack apps, moderate features)
- "enterprise" — Large systems with many components, microservices, complex auth

**Decomposer rule:** Default is `contract_first` as of v0.3.4. Only override to
`feature_based` when the user passes `--decomposer feature_based` explicitly.

**Agent blocks:** Generate one agent block per session (so `sessions: 3` gets three
blocks) with incrementing IDs `agent_unicorn_1`, `agent_unicorn_2`, and so on. The
launcher uses the first `sessions` entries as the session identities and generates
`session_<i>` identities if the list is shorter.

### Step 4: Initialize and Run the Experiment

First, discover the Marcus repo root:
```bash
MARCUS_ROOT=$(python3 -c "from pathlib import Path; import marcus_mcp; print(Path(marcus_mcp.__file__).parent.parent.parent)")
```

Then run it directly (using the current working directory as the experiment directory).
**IMPORTANT:** `run_experiment.py` runs for the whole experiment (it waits for every
session to go home), so launch it as a background process and do not block on it.
Project creation alone takes 30-90 seconds before the sessions start.

Append `--epictetus`, `--model <value>`, and/or `--harness <value>` to the command
based on what the user passed. All flags combine.

```bash
# Gemini harness, gemini's default model:
cd "${MARCUS_ROOT}/dev-tools/experiments" && python runners/run_experiment.py <cwd> --harness gemini

# Gemini harness with explicit model:
cd "${MARCUS_ROOT}/dev-tools/experiments" && python runners/run_experiment.py <cwd> --harness gemini --model gemini-2.5-pro

# Claude or codex harness from inside Gemini (rare but supported):
cd "${MARCUS_ROOT}/dev-tools/experiments" && python runners/run_experiment.py <cwd> --harness claude
cd "${MARCUS_ROOT}/dev-tools/experiments" && python runners/run_experiment.py <cwd> --harness codex
```

This will:
- Create `prompts/`, `logs/`, `implementation/`, `worktrees/` directories
- Initialize a git repo in `implementation/` on `main`
- Write the session contract to CLAUDE.md, AGENTS.md, and GEMINI.md in `implementation/`
- Pre-flight `which gemini` (or `which claude` / `which codex` depending on `--harness`)
- Call `create_project` over MCP itself (no project-creator agent) and commit the scaffold
- Create a tmux session: `marcus_<project_name_lowercase>`
- Start `sessions` worker sessions, each in its own tmux window and worktree, and wait for them to go home on `BOARD_DRAINED`
- Mirror live progress to `<cwd>/logs/runner.log`
- For gemini harness, each session runs `gemini --skip-trust --yolo` with the Marcus MCP server registered via `gemini mcp add --transport http --scope user marcus http://localhost:4298/mcp`. Workers are wrapped in a bounded bash relaunch loop (`MAX_RELAUNCHES=50`) because `gemini -p` is fire-and-exit; the loop breaks on a clean `rc=0` exit so a worker that finishes its goal does not keep churning.

### Step 5: Report to User

After launching, tell the user:
- The tmux session name: `marcus_<project_name_lowercase_underscored>`
- The harness being used (claude, codex, or gemini)
- How to attach: `tmux attach -t <session_name>`
- How to navigate: one window per session, Ctrl+b n/p to switch
- How to follow progress: `tail -f <cwd>/logs/runner.log` (appears once the sessions start, ~30-90s after launch)
- How to kill: `tmux kill-session -t <session_name>`
- The experiment directory location
- Session count: exactly `sessions` workers for the whole project; the launcher tears tmux down when the board drains

## Important Notes

- The Marcus MCP server must be running at `http://localhost:4298/mcp` before launching
- Each session is a fully independent harness CLI process, re-invoked once per task. For gemini: `gemini --skip-trust --yolo` (bypass trusted-directory + auto-approve all tool calls). For codex: `codex exec --dangerously-bypass-approvals-and-sandbox`. For claude: `claude --dangerously-skip-permissions`.
- Agents coordinate via MCP tools (register_agent, request_next_task, report_task_progress, etc.)
- Each session works in its own worktree (`worktrees/<agent_id>`); Marcus merges accepted work into `main` in `implementation/`
- The launcher calls `mcp__marcus__create_project` itself, which uses AI to decompose the spec into tasks — this takes 30-90 seconds; sessions start after that
- A session keeps its identity for the whole run but gets a fresh context every task: it registers (idempotently), takes one task, does it, reports it, and exits; the session script invokes it again
- A session exits for good when `request_next_task` returns `BOARD_DRAINED`; nothing restarts a dead session, and Marcus recovers its task for another session
- There is no project-creator agent, no monitor agent, and no stall watchdog

## Pre-flight Checks

Before running, verify:
1. Marcus MCP is registered for the active harness:
   - claude: `claude mcp list` shows "marcus"
   - codex: `codex mcp list` shows "marcus"
   - gemini: `gemini mcp list` shows "marcus"
2. tmux is installed: `which tmux`
3. Harness CLI is on PATH: `which claude` or `which codex` or `which gemini`

If Marcus MCP is not running, tell the user:
```
Marcus MCP server is not running. Start it first:
  cd <MARCUS_ROOT> && ./marcus start
```

If `--harness gemini` and Marcus MCP is not yet registered with gemini, tell the user:
```
gemini mcp add --transport http --scope user marcus http://localhost:4298/mcp
```

If `--harness codex` and Marcus MCP is not yet registered with codex, tell the user:
```
codex mcp add marcus --url http://localhost:4298/mcp/
```

If `--harness claude` and Marcus MCP is not yet registered with claude, tell the user:
```
claude mcp add marcus -t http http://localhost:4298/mcp
```
