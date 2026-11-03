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
user-invocable: true
argument-hint: "<project description> [--name \"Project Name\"] [--sessions N | --agents N] [--complexity prototype|standard|enterprise] [--decomposer contract_first|feature_based] [--tech-stack X] [--epictetus] [--model <model>] [--harness claude|codex|gemini]"
---

# Marcus Multi-Agent Experiment Launcher

You are helping the user launch a Marcus multi-agent experiment. Marcus uses an MCP
server to coordinate independent agent CLI sessions, each running in its own tmux
window. A session is one persistent worker identity that does one task per
invocation until the board drains; Marcus itself starts nothing (epic #706).

## EXECUTION RULES — READ FIRST, THESE OVERRIDE EVERYTHING BELOW

This skill runs **fully autonomously**. When `/marcus` is invoked you perform
**every step, Step 1 through Step 6, in one continuous pass** and end by launching
the experiment.

1. **NEVER stop to ask the user for confirmation.** Do not ask "Continue?",
   "Should I proceed?", "Ready to launch?", or anything similar. There is no
   approval checkpoint. There is no pause. Run all six steps and launch.
2. **The ONLY thing that stops you is a hard failure in Step 1 (Pre-flight).**
   If a pre-flight check fails, print the exact fix message for that failure and
   stop. That is the only permitted stopping point in this entire skill.
3. **Do the steps strictly in numbered order: 1, 2, 3, 4, 5, 6.** Do not skip a
   step. Do not reorder. Do not do "part of" a step and move on. Finish each step
   completely before starting the next.
4. **Do not narrate intentions and then wait.** Saying "I'll write the files now"
   followed by stopping is a failure. If you say you will do something, do it in
   the same turn.
5. **If the user gave a separate instruction in their message** (e.g. "don't do
   anything", "just answer this", "explain only"), that instruction wins — obey it
   and do NOT run the steps. The autonomy rule applies only to a plain `/marcus`
   invocation with no overriding instruction.

Definition of done: the worker sessions are started in tmux and you have reported
the tmux session name to the user (Step 6). Anything short of that is an
incomplete run.

## Overview of the Six Steps

1. **Pre-flight checks** — verify MCP server, tmux, harness CLI. (Only stopping point.)
2. **Parse arguments** — extract description, name, session count, flags.
3. **Write `project_spec.md`** into the current working directory.
4. **Write `config.yaml`** into the current working directory.
5. **Run `run_experiment.py`** — starts the session launcher (background).
6. **Report** the tmux session name and attach instructions to the user.

## Discovering Paths

Find the Marcus repo root via the installed package:

```bash
MARCUS_ROOT=$(python3 -c "from pathlib import Path; import marcus_mcp; print(Path(marcus_mcp.__file__).parent.parent.parent)")
```

From there:
- **Run script**: `${MARCUS_ROOT}/dev-tools/experiments/runners/run_experiment.py`
- **Templates**: `${MARCUS_ROOT}/dev-tools/experiments/templates/`

If the import fails, Marcus isn't installed. Tell the user this and stop:
```
Marcus is not installed. Follow the setup instructions:
  git clone https://github.com/lwgray/marcus.git
  cd marcus && pip install -e .
```

---

## Step 1: Pre-flight Checks (the ONLY stopping point)

Run all three checks before anything else. If any check fails, print its exact
fix message and STOP. If all three pass, continue immediately to Step 2 — do not
ask the user anything.

**Check 1 — Marcus MCP server is running.** Run `claude mcp list` (claude
harness), `codex mcp list` (codex harness), or `gemini mcp list` (gemini
harness) and confirm "marcus" shows "Connected".
If not running, print and stop:
```
Marcus MCP server is not running. Start it first:
  cd <MARCUS_ROOT> && ./marcus start
```

**Check 2 — tmux is installed.** Run `which tmux`. If missing, tell the user to
install tmux and stop.

**Check 3 — harness CLI is available.** Run `which claude` (default),
`which codex` (when `--harness codex`), or `which gemini` (when
`--harness gemini`). If missing, tell the user to install the
CLI and stop.

If `--harness codex` and Marcus MCP is not yet registered with codex, print and stop:
```
codex mcp add marcus --url http://localhost:4298/mcp/
```

```
codex mcp add marcus --url http://localhost:4298/mcp/
```

If `--harness gemini` and Marcus MCP is not yet registered with gemini, print and stop:
```
gemini mcp add --transport http --scope user marcus http://localhost:4298/mcp
```

**All three passed → go straight to Step 2. Do not pause. Do not ask.**

---

## Step 2: Parse Arguments

The user's input comes in as `$ARGUMENTS`. Extract:
- **Project description**: Everything that describes what to build
- **Project name**: Look for `--name "Some Name"` or `--name some_name`. If not provided, derive a short name from the description.
- **Session count**: Look for "N sessions", "--sessions N", "N agents", "--agents N", "with N workers". **Default: 2.** This is how many persistent worker sessions start; it is written as `sessions: N` in config.yaml. There is no pool sizing and no cap: exactly N sessions run for the whole project.
- **Complexity**: Look for "--complexity prototype|standard|enterprise". Default: "prototype"
- **Tech stack**: Look for `--tech-stack X`. Default: inferred (see the tech stack rule in Step 4).
- **Decomposer**: Look for "--decomposer contract_first|feature_based". Default: "contract_first" (as of v0.3.4). This controls Marcus's task decomposition strategy (GH-320):
  - `contract_first` — default. Generates interface contracts before decomposition. Board is fully populated before any agent starts (no Phase A race). Each agent owns one side of a contract. Best for tightly-coupled projects (games, dashboards, state machines).
  - `feature_based` — legacy path, splits tasks by functional requirement. Fine for loosely-coupled projects where features don't share files.
- **Epictetus mode**: Look for `--epictetus` flag. Default: not set (false). When present, the launcher does NOT tear down the tmux session after the sessions go home — it stays alive for Epictetus post-experiment interrogation.
- **Agent model**: Look for `--model <value>`. Default: not set — Marcus reads `ai.model` from `config_marcus.json` and uses that same value for the worker sessions' agent CLI (so by default Planners and workers share one model). When `--model X` is provided, X overrides for THIS run only and applies to every session. The same string is passed verbatim to whichever harness is active; invalid names surface as CLI errors in the tmux windows.
- **Agent harness**: Look for `--harness claude|codex|gemini`. Default: `claude`. `claude` runs Anthropic's claude CLI with `--dangerously-skip-permissions`; `codex` runs OpenAI's codex CLI with `exec --dangerously-bypass-approvals-and-sandbox`; `gemini` runs Google's gemini CLI with `--skip-trust --yolo`. All sessions in one run use the same harness.

After extracting all values, print one line summarizing the parse (description,
name, sessions, complexity, decomposer, tech stack, model, harness) so the user can see it —
then **continue immediately to Step 3. Printing the parse is NOT a checkpoint.
Do not wait for a reply.**

Examples:
- `/marcus Build a snake game with 3 agents` -> description="Build a snake game", name="snake_game", sessions=3, complexity="prototype", decomposer="contract_first"
- `/marcus Build a REST API for task management` -> description="Build a REST API for task management", name="task_management_api", sessions=2, complexity="prototype", decomposer="contract_first"
- `/marcus Build a chat app --name "ChatBuddy" with 4 agents` -> description="Build a chat app", name="ChatBuddy", sessions=4, complexity="prototype", decomposer="contract_first"
- `/marcus Use 2 agents to build a pomodoro timer --complexity standard --name "FocusTimer"` -> description="build a pomodoro timer", name="FocusTimer", sessions=2, complexity="standard", decomposer="contract_first"
- `/marcus Build a snake game with 2 agents --decomposer feature_based` -> description="Build a snake game", name="snake_game", sessions=2, complexity="prototype", decomposer="feature_based"
- `/marcus --decomposer feature_based Build a weather dashboard with 3 agents` -> description="Build a weather dashboard", name="weather_dashboard", sessions=3, complexity="prototype", decomposer="feature_based"
- `/marcus Build a snake game --model haiku` -> description="Build a snake game", name="snake_game", sessions=2, complexity="prototype", decomposer="contract_first", model="haiku", harness="claude"
- `/marcus Build a chat app with 3 agents --model claude-haiku-4-5-20251001` -> description="Build a chat app", name="chat_app", sessions=3, complexity="prototype", decomposer="contract_first", model="claude-haiku-4-5-20251001", harness="claude"
- `/marcus Build a TODO CLI with 2 agents --harness codex --model gpt-5-codex` -> description="Build a TODO CLI", name="todo_cli", sessions=2, complexity="prototype", decomposer="contract_first", model="gpt-5-codex", harness="codex"
- `/marcus Build a snake game --harness codex` -> description="Build a snake game", name="snake_game", sessions=2, complexity="prototype", decomposer="contract_first", harness="codex" (model left unset; codex uses its global default)
- `/marcus build a ping pong html game --harness gemini` -> description="build a ping pong html game", name="ping_pong_html_game", sessions=2, complexity="prototype", decomposer="contract_first", harness="gemini" (model left unset; gemini uses its global default)
- `/marcus Build a TODO CLI --harness gemini --model gemini-2.5-pro` -> description="Build a TODO CLI", name="todo_cli", sessions=2, complexity="prototype", decomposer="contract_first", model="gemini-2.5-pro", harness="gemini"

---

## Step 3: Write project_spec.md

Write all files into the **current working directory**. Do NOT create or mkdir a
new directory. `run_experiment.py` will create the subdirectories it needs
(`prompts/`, `logs/`, `implementation/`, `worktrees/`).

```
<cwd>/
├── config.yaml       (you create this — Step 4)
├── project_spec.md   (you create this — Step 3)
├── prompts/          (created by run_experiment.py: one prompt and one script per session)
├── logs/             (created by run_experiment.py: runner.log)
├── implementation/   (created by run_experiment.py, git repo on main where work merges)
└── worktrees/        (one git worktree per session, worktrees/<agent_id>)
```

Write the user's project description **as-is** to `project_spec.md`. Do NOT rewrite,
expand, restructure, or add sections to it. Marcus handles task decomposition — the
spec is just the raw input from the user.

Then continue immediately to Step 4.

---

## Step 4: Write config.yaml

**CRITICAL: Always write a fresh config.yaml from the template below. Do NOT read or
preserve values from any existing config.yaml in the directory. Overwrite it completely.**

Use this exact format — field names are case-sensitive:

```yaml
project_name: "<--name value if provided, otherwise derived from description>"
project_spec_file: "project_spec.md"

# How many persistent worker sessions to start (default 2).
sessions: <parsed session count>

project_options:
  complexity: "<parsed complexity>"  # "prototype" (default), "standard" for medium, "enterprise" for large
  provider: "sqlite"        # Uses local SQLite DB. Also supports: "planka", "github", "linear"
  mode: "new_project"       # Always new_project
  decomposer: "<parsed decomposer>"  # "contract_first" (default) or "feature_based" (GH-320)
  tech_stack: <parsed tech stack>  # e.g. ["python"] — pins the language; never omit (see "Tech stack rule")

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

**Decomposer rule:** Default is `contract_first` as of v0.3.4. Only
override to `feature_based` when the user passes `--decomposer feature_based`
explicitly. Do NOT infer the decomposer from phrases in the project
description — "don't use contract first" and "use contract first" both
contain the same substring, and a substring match would pick the wrong
strategy half the time. If the flag is absent, always set
`decomposer: "contract_first"`.

**Tech stack rule:** Always write a non-empty `tech_stack` list. Infer it
from the description — if the user names a language or framework ("a
Python CLI", "a React dashboard"), use it; if `--tech-stack X` is passed,
use it verbatim; if the description implies no specific stack, pick a
sensible default for the project type (CLI tool → `["python"]`; web app
→ `["typescript", "react"]`). Never leave it empty. Reason: with no
stack pinned, Marcus's decomposition picks the language independently in
each pipeline step — test53 produced Python contracts and a TypeScript
scaffold for one project (issue #604). `tech_stack` becomes a hard
constraint binding every pipeline step.

**Agent blocks:** Write one agent block per session (so `sessions: 3` gets
three blocks), using IDs `agent_unicorn_1`, `agent_unicorn_2`, and so on. The
launcher uses the first `sessions` entries as the session identities and
generates `session_<i>` identities if the list is shorter.

Then continue immediately to Step 5.

---

## Step 5: Run the Experiment

Discover the Marcus repo root:
```bash
MARCUS_ROOT=$(python3 -c "from pathlib import Path; import marcus_mcp; print(Path(marcus_mcp.__file__).parent.parent.parent)")
```

`run_experiment.py` starts the **session launcher**: it creates the project
over MCP, starts `sessions` persistent worker sessions in tmux (one git worktree
each, one task per harness invocation, fresh context each time), waits for every
session to go home when the board drains, then tears down the tmux session. It
runs for the **entire experiment** — many minutes — so it MUST be launched as a
**background process**: pass `run_in_background: true` to the Bash tool. Do NOT
run it with a foreground timeout — a timeout would kill the runner mid-experiment.

Use the current working directory as the experiment directory. If `--epictetus`,
`--model <value>`, or `--harness <value>` were passed, append them; flags combine.

```bash
# Default (claude harness):
cd "${MARCUS_ROOT}/dev-tools/experiments" && python runners/run_experiment.py <cwd>

# With Epictetus mode (tmux session kept alive for interrogation):
cd "${MARCUS_ROOT}/dev-tools/experiments" && python runners/run_experiment.py <cwd> --epictetus

# With agent model override (every session runs on this model):
cd "${MARCUS_ROOT}/dev-tools/experiments" && python runners/run_experiment.py <cwd> --model claude-haiku-4-5-20251001

# With codex harness:
cd "${MARCUS_ROOT}/dev-tools/experiments" && python runners/run_experiment.py <cwd> --harness codex --model gpt-5-codex

# Seeded board (explicit task list, no decomposition; for live checks):
cd "${MARCUS_ROOT}/dev-tools/experiments" && python runners/run_experiment.py <cwd> --seed <path to tasks.yaml>
```

This will:
- Create `prompts/`, `logs/`, `implementation/`, `worktrees/` directories
- Initialize a git repo in `implementation/` on `main`
- Call `create_project` over MCP itself (no project-creator agent) and commit the scaffold
- Create a tmux session: `marcus_<project_name_lowercase>`
- Start `sessions` worker sessions, each in its own tmux window and worktree
- Wait until every session exits on `BOARD_DRAINED`, print the board counts, tear down tmux
- Mirror live progress to `<cwd>/logs/runner.log`

**Do NOT verify the launch by tailing `logs/runner.log` right after launching.**
The launcher creates `runner.log` only once the sessions start, after
`create_project` finishes decomposing (~30–90s); `project_info.json` and
`worktrees/` appear then too. A `tail`/`cat` immediately after launch prints a
misleading `No such file or directory` (exit 1) even though the run is healthy —
never present that as an error. If you want a quick liveness check, confirm only
that the tmux session exists:
`tmux has-session -t marcus_<project_name_lowercase> && echo live`. Then go to
Step 6 and tell the user `runner.log` appears within ~90s.

Then continue immediately to Step 6.

---

## Step 6: Report to User

The experiment is now running as a background process. Tell the user:
- The tmux session name: `marcus_<project_name_lowercase_underscored>`
- Watch the launcher live: `tail -f <cwd>/logs/runner.log` (the file appears ~30–90s after launch, once project creation finishes)
- Watch the sessions: `tmux attach -t <session_name>` (one window per session, Ctrl+b n/p to switch)
- How to kill: `tmux kill-session -t <session_name>`
- The experiment directory location
- Exactly `sessions` workers run for the whole project; the launcher tears the
  tmux session down itself when the board drains and every session has gone home.

This completes the run.

## Important Notes

- The Marcus MCP server must be running at `http://localhost:4298/mcp` before launching
- Each session is a fully independent harness CLI process, re-invoked once per task: `claude --dangerously-skip-permissions --print` (default), `codex exec --dangerously-bypass-approvals-and-sandbox` when `--harness codex`, or `gemini --skip-trust --yolo` when `--harness gemini`
- Agents coordinate via MCP tools (register_agent, request_next_task, report_task_progress, etc.)
- Each session works in its own worktree (`worktrees/<agent_id>`, branch `marcus/<agent_id>`); Marcus merges accepted work into `main` in `implementation/`
- The launcher calls `mcp__marcus__create_project` itself, which uses AI to decompose the
  spec into tasks — this takes 30-90 seconds; sessions start after that
- A session keeps its identity for the whole run but gets a fresh context every task: it registers (idempotently), takes one task, does it, reports it, and exits; the session script invokes it again
- A session exits for good when `request_next_task` returns `BOARD_DRAINED`; nothing restarts a dead session, and Marcus recovers its task for another session
- There is no project-creator agent, no monitor agent, and no stall watchdog
