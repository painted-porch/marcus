# Quickstart Guide

Get Marcus running in 5 minutes.

## Prerequisites

- macOS or Linux (Windows: install [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install) and follow the Linux path)
- Python 3.11+
- `tmux` (`brew install tmux` on macOS, `sudo apt install tmux` on Ubuntu/Debian) — only needed for Runner mode
- An **MCP-compatible AI agent** (Claude Code, Codex, Gemini CLI, Kimi, AutoGen, LangGraph, or a custom runtime)
- An **LLM provider** for Marcus's task decomposition (Anthropic, OpenAI, or [Ollama](https://ollama.ai) for free local models)

> Marcus uses its own LLM provider for decomposition. Your coding agents use their own keys separately.

## Step 1 — Install Marcus

```bash
git clone https://github.com/painted-porch/marcus.git
cd marcus
pip install -e .
```

If you plan to use **Runner mode** (one-command experiments with the `/marcus` skill in Claude Code), also install the skill:

```bash
cp -r skills/marcus ~/.claude/skills/marcus
```

## Step 2 — Configure your LLM provider

```bash
cp .env.example .env
cp config_marcus.example.json config_marcus.json
```

Edit `.env` for your API key:

```bash
CLAUDE_API_KEY=sk-ant-api03-your-key-here
```

> Marcus reads `CLAUDE_API_KEY` (not `ANTHROPIC_API_KEY`) so it doesn't interfere with Claude Code's subscription auth.

| Provider  | Cost | Setup |
|-----------|------|-------|
| Anthropic | Paid | Set `CLAUDE_API_KEY` in `.env` — works out of the box |
| OpenAI    | Paid | Set `OPENAI_API_KEY` in `.env`, set `ai.provider` to `"openai"` in `config_marcus.json` |
| Ollama    | Free | Install [Ollama](https://ollama.ai), pull a model, set `ai.provider` to `"local"` ([setup guide](setup-local-llm.md)) |

## Step 3 — Start Marcus

```bash
./marcus start
```

Marcus is now running at `http://localhost:4298/mcp` with **SQLite as the default kanban board** — no Docker, no Postgres, no external services. Marcus creates `data/kanban.db` automatically on first project creation.

Inspect the board from the command line at any time:

```bash
./marcus board                          # rich terminal view
sqlite3 data/kanban.db "SELECT name, status, assigned_to FROM tasks"
```

Other useful commands:

```bash
./marcus status                          # is Marcus running?
./marcus logs --tail 50                  # recent logs
./marcus stop                            # shut down
./marcus restart                         # restart with previous config
```

## Step 4 — Choose a visual dashboard (optional)

### Option A: Cato — real-time visualization

Cato is the active Marcus dashboard. Built-in kanban board, live agent activity, board health, run history.

```bash
# In a sibling directory to marcus/
git clone https://github.com/painted-porch/cato.git
cd cato && pip install -e . && ./cato start
# Open http://localhost:5173
```

### Option B: Planka — drag-and-drop kanban

If you want a hosted kanban UI you can drag tasks around in. Requires Docker; Docker is **infrastructure only** (Planka + Postgres). Marcus itself still runs locally via `./marcus start`.

```bash
docker compose up -d
# Open http://localhost:3333  (login: demo@demo.demo / demo)
```

Then point Marcus at Planka by editing `config_marcus.json`:

```json
{
  "kanban": {
    "provider": "planka",
    "planka_base_url": "http://localhost:3333",
    "planka_email": "demo@demo.demo",
    "planka_password": "demo"  // pragma: allowlist secret
  }
}
```

Restart Marcus: `./marcus restart`.

> Create at least one list (e.g., Backlog / In Progress / Done) on the Planka board before creating projects, or task creation will fail.

## Step 5 — Your first project

Marcus supports two operating modes. Pick whichever matches your agent.

### Runner mode — Claude Code + the `/marcus` skill (one command)

```bash
mkdir ~/projects/my-todo-app
cd ~/projects/my-todo-app
claude --dangerously-skip-permissions
```

Inside Claude Code:

```
/marcus Build a todo app with authentication using 3 agents
```

The `/marcus` skill registers the MCP server, injects the agent system prompt, decomposes the project, and spawns N agents in tmux panes. Walk away; come back to working software.

### Attach mode — any MCP-compatible agent

Use Attach mode for Codex, Gemini CLI, Kimi, AutoGen, LangGraph, or a custom runtime. You wire the agents yourself.

**1. Connect your agent to Marcus.** Point your agent's MCP config at `http://localhost:4298/mcp` (HTTP transport).

For Claude Code without tmux:

```bash
cd ~/projects/my-todo-app
claude mcp add --transport http marcus http://localhost:4298/mcp
```

For other runtimes, consult that agent's MCP-server-registration docs.

**2. Give your agent the system prompt.** `prompts/Agent_prompt.md` is the complete behavioral spec for a Marcus worker — how to call tools, manage the work loop, handle context, report blockers, when to exit. **Without it your agent won't know the protocol.**

```bash
cp <marcus-dir>/prompts/Agent_prompt.md ~/projects/my-todo-app/CLAUDE.md
```

For non-Claude agents, paste the contents into the agent's system prompt.

**3. Bootstrap the board.** One agent calls `create_project` with `description` and `project_name`. Marcus returns `project_id`, `recommended_agents`, and the task graph.

**4. Start workers.** Each worker calls `register_agent` once, then loops: `request_next_task` → `get_task_context` → do the work → `log_decision` → `log_artifact` → `report_task_progress` (25/50/75/100) → immediately request the next task. See [Agent Workflow Guide](../guides/agent-workflows/agent-workflow.md) and [PROTOCOL.md](https://github.com/painted-porch/marcus/blob/main/PROTOCOL.md).

## Add More Agents

Marcus throughput scales with the number of agents pulling from the board. Pick the option that matches your setup:

### `/marcus` skill (Claude Code Runner mode — recommended for one-off projects)

Already covered above. Pass `--agents N` to spawn N independent Claude agents in tmux panes:

```
/marcus Build X with 4 agents
```

### Attach mode — multiple agents from any runtime

Once Marcus is running, any number of MCP-compatible agents can attach to the same `http://localhost:4298/mcp`. Open multiple terminal panes, multiple Claude windows, or wire up any combination of Codex, Gemini, custom runners. Each agent calls `register_agent` once and joins the work loop. They pull different tasks from the same board automatically.

### Posidonius — multi-run experiments and dashboards

[Posidonius](https://github.com/lwgray/posidonius) is the experiment platform for launching and monitoring **multiple Marcus runs** — useful for benchmarking, parameter sweeps, or batches of independent projects. It spawns experiments, tracks them in a web UI, and feeds results back to Cato/Epictetus.

```bash
git clone https://github.com/lwgray/posidonius.git
cd posidonius && pip install -e .
```

By default Posidonius writes projects to `~/experiments/`. See the [Posidonius README](https://github.com/lwgray/posidonius) for setup.

### Git worktrees (orthogonal — code isolation, not agent count)

Worktrees aren't a way to *add* agents — they're a way to keep agents from stepping on each other's working tree:

```bash
git worktree add ../project-agent2 -b agent2-branch
```

Layer this on top of `/marcus`, Attach mode, or Posidonius when multiple agents will edit the same files.

## What you'll see

When agents start working with Marcus:

- ✅ Agent registers (`Agent claude-1 registered`)
- ✅ Project decomposed into tasks on the board
- ✅ Tasks moving through TODO → IN PROGRESS → DONE
- ✅ Progress updates: 25%, 50%, 75%, 100%
- ✅ Context flowing between tasks (API specs → implementation → tests)
- ✅ If you started Cato, all of the above visible at `http://localhost:5173`
- ✅ If you used Planka, the same flow visible at `http://localhost:3333`

## Verify everything works

From your agent (or directly via MCP):

- `ping` — test Marcus connectivity
- `get_agent_status` — agent capabilities, assignments, health
- `get_project_status` — project health, tasks, predictions

## Common Issues

### `Connection refused`

Check Marcus is running: `./marcus status`. If not: `./marcus start`. The default port is `4298`; if it's in use, start with `./marcus start --port 5000`.

### `No tasks available`

The board is empty. An agent must call `create_project` first to decompose a description into tasks.

### `Agent not registered`

The agent must call `register_agent` once at startup before requesting tasks. The system prompt at `prompts/Agent_prompt.md` handles this automatically — make sure your agent has it loaded.

### Planka: `Failed to create any tasks` / `find_target_list failed`

The Planka board has no lists/columns. Open `http://localhost:3333` and create at least: Backlog, In Progress, Done.

### LLM provider errors

- Verify `CLAUDE_API_KEY` (or `OPENAI_API_KEY`) is set in `.env` and has credits.
- For free local LLMs: switch to Ollama via the [setup guide](setup-local-llm.md).

## Next Steps

1. **[Core Concepts](core-concepts.md)** — agents, tasks, projects, the board pattern
2. **[Agent Workflow Guide](../guides/agent-workflows/agent-workflow.md)** — how agents work the board
3. **[Configuration Reference](../developer/configuration.md)** — every option in `config_marcus.json`
4. **[PROTOCOL.md](https://github.com/painted-porch/marcus/blob/main/PROTOCOL.md)** — build a runner for a new agent runtime
5. **Build a custom programmatic agent** — see `examples/inspector_demo.py` in the repo

## Getting Help

- [Discord](https://discord.com/channels/1409498120739487859/1409498121456848907)
- [GitHub Issues](https://github.com/painted-porch/marcus/issues)
- [GitHub Discussions](https://github.com/painted-porch/marcus/discussions)

---

*Ready to learn more? Continue to [Core Concepts](core-concepts.md).*
