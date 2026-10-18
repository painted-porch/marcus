<p align="center">
  <img src="docs/assets/logo.png" alt="Marcus" width="140">
</p>

<h1 align="center">Marcus</h1>

<p align="center">
  <strong>Work you can act on without re-checking it yourself — whether an AI or a person did it.</strong>
</p>

<p align="center">
  <a href="#get-started">Quickstart</a> •
  <a href="https://docs.marcus-ai.dev">Docs</a> •
  <a href="https://discord.gg/DZWTbXr4">Discord</a> •
  <a href="ROADMAP.md">Roadmap</a> •
  <a href="PROTOCOL.md">Protocol</a>
</p>

<p align="center">
  <a href="https://github.com/painted-porch/marcus"><img src="https://img.shields.io/github/stars/painted-porch/marcus?style=social" alt="GitHub Stars"></a>
  <a href="https://discord.gg/DZWTbXr4"><img
src="https://img.shields.io/discord/1409498120739487859?color=7289da&label=Discord&logo=discord&logoColor=white" alt="Discord"></a>
  <img src="https://img.shields.io/badge/python-3.11+-blue?logo=python&logoColor=white" alt="Python 3.11+">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-Compatible-green" alt="MCP Compatible"></a>
  <a href="docs/source/getting-started/setup-local-llm.md"><img src="https://img.shields.io/badge/Contribute-Free%20with%20Ollama-ff6b35?logo=ollama&logoColor=white" alt="Contribute for free with Ollama"></a>
</p>

  ---

## What is Marcus?

Marcus is open-source software for getting real work out of AI agents, and for actually being able to trust what comes back.

Here is the problem it solves.
When an AI hands you a piece of work, or for that matter a person you don't know, you usually can't tell whether it's right without checking it yourself.
That piece of work might be some code, an answer with a source, or a number in a report.
And if you have to re-check everything by hand, you've thrown away most of the reason you asked an AI in the first place.

Marcus fixes this by changing the rules for how work gets turned in, not by hoping for a smarter AI.
It runs a shared task board (think of a team's to-do list that everyone pulls work from) and enforces three rules automatically:

1. **Nothing counts as "done" without proof.** A worker can't just declare something finished; it has to attach the evidence, which means the code *and* its passing tests, the link behind a claim, or the document a number came from.
2. **Someone other than the worker checks that proof, and the worker is never told how.** The check is run by a different party, often a small deterministic script, and whoever did the task never sees the test their work will have to pass. Why that matters: if they knew the exact test, they could do the bare minimum to pass it instead of actually doing the job right. Kept in the dark, the only way through is to genuinely be correct.
3. **A person has the final say.** On the work that matters, a human signs off at the end; software can never wave its own work through, so a real person is always accountable for what ships.

The result: you don't have to trust any single worker, AI or human.
You trust the process that every piece of work had to survive before it reached you.
Untrusted work goes in; work you can actually act on comes out.

That one idea powers two very different things you can run today.

**1. Coordinate AI coding agents.**
You describe what you want built, and Marcus breaks it into tasks on the shared board. The board is a [kanban](https://en.wikipedia.org/wiki/Kanban_board) board, which is a set of to-do cards that move across columns to "done."
Several AI agents (Claude Code, Codex, Gemini CLI, or any [MCP](https://modelcontextprotocol.io/)-compatible tool) each grab tasks, write the code, and coordinate only through the board, never by chatting at each other.
You walk away; you come back to working software.

**2. Keep AI answers honest (a worked example).**
We re-ran a 2025 Columbia Journalism Review study of AI search engines: four engines, 200 real news questions where we already knew the correct source.
On their own, the engines got **7 in 10 answers wrong**, and **51% of the links they cited were fabricated, dead, or pointed to the wrong article.**
Then we ran the same engines through Marcus, which fetched every cited page, checked it, and refused the ones that didn't hold up.
The result: **0 fabricated links reached the reader**, because the fakes get caught, not fixed.
See it case by case: **[On the Record →](https://marcus-ai.dev/on-the-record/)**
Run the whole study yourself, step by step: **[REPRODUCING.md](REPRODUCING.md)**

Checking news links was just an easy thing to measure.
The same board handles any claim a script can check: code against its tests, a number against its source document, or a legal citation against the court record.
Coordinating AI agents is how Marcus started; making their work trustworthy enough to act on is what it is.

---

## Why Marcus

Multi-agent AI is broken at scale. Every framework today coordinates agents through
**conversation** — group chats, message passing, chain-of-thought relays. This works
with 2–3 agents. At scale, it collapses:

- **Context degrades.** Each agent gets a growing wall of chat history. Signal drowns in noise.
- **Work duplicates.** Without shared state, agents don't know what others have done.
- **Failures cascade.** One agent crashes and the conversation context is gone. No recovery.
- **Adding agents adds chaos.** More agents = more messages = slower, less reliable coordination.

The fundamental mistake: treating coordination as a conversation problem. It's a
**state management** problem.

|                        | Group Chat Coordination     | Board-Mediated Coordination   |
|------------------------|-----------------------------|-------------------------------|
| **Used by**            | AutoGen, CrewAI, LangGraph  | **Marcus**                    |
| **Coordination**       | Conversation between agents | Shared board state            |
| **Context at scale**   | Degrades                    | Preserved per-task            |
| **Agent failure**      | Lost context, no recovery   | Resume from board state       |
| **Visibility**         | Chat logs                   | Full audit trail + dashboard  |
| **Add more agents**    | More chaos                  | More throughput               |
| **Enterprise ready**   | Limited governance          | Audit trails, accountability  |

Marcus doesn't compete on raw speed. It competes on **coordination quality,
observability, and enterprise readiness.**

Still — coordination quality shows up in the timing. The board-mediated
thesis is validated head-to-head in
[`marcus-mini`](https://github.com/lwgray/marcus-mini), our lean
reference implementation: against AutoGen `SelectorGroupChat` (chat-based)
and LangGraph supervisor-worker (orchestrator-based) on identical
workloads, the gap grows with project size — ~3× at 9 tasks, ~7× at 27 tasks.
Full numbers and reproduction steps:
[`experiments/topologies/`](https://github.com/lwgray/marcus-mini/tree/main/experiments/topologies).

> *The moment it clicks: I didn't have to manage any of this.*

[![Demo](docs/assets/frontpage.png)](https://youtu.be/Js8cdsFGbHk/)
---

## How It Works

Marcus uses a simple idea: **give agents a shared task board instead of making them
talk to each other.** We call this **board-mediated coordination** — a modern,
agent-native take on the classical
[blackboard pattern](https://en.wikipedia.org/wiki/Blackboard_(design_pattern))
(Hayes-Roth, 1985), applied to autonomous LLM agents coordinating over MCP.

<p align="center">
  <img src="docs/assets/tasks.png" width="720" alt="The anatomy of a pristine task — requirements, dependencies, artifacts">
</p>

Each task carries its own context — requirements, dependencies, artifacts from
prior tasks. When an agent picks up a task, it gets exactly the context it needs.
No chat history. No lost threads. No duplicate work.

When an agent fails, the task stays on the board with its progress. Another agent
picks it up and continues. **The board is the system of record.**

### Architecture

<p align="center">
  <img src="docs/assets/stack.png" width="720" alt="How Marcus fits into your stack — dependency ordering, artifact routing, lease isolation">
</p>

- **Agents are stateless.** All state lives on the board.
- **Tasks are the unit of coordination.** Each has context, dependencies, artifacts.
- **MCP is the interface.** Any MCP-compatible agent works with Marcus.
- **Observability is built in.** Every action is traceable through the board and Cato.

See [Architecture Docs](https://marcus-ai.dev) for deep dives.

---

## What Marcus does

The board-mediated model gives you four properties that conversation-based
frameworks can't offer:

- **Atomic work claiming.** A lease-based SQL transaction with a dependency check
  guarantees two agents can't grab the same task — no lock manager, no consensus
  protocol. A task in progress is held under a time-limited lease; if the agent
  dies, the lease expires and another agent picks it up automatically.
- **Async handoffs.** Agent A finishes a task and logs an artifact — a spec,
  schema, or design decision. Agent B, running independently, picks up a dependent
  task hours later and reads that artifact as context. Neither needs to know the
  other exists.
- **Agent blindness as a feature.** Because agents don't know about each other,
  you can add, remove, swap providers, or lose an agent mid-run freely. This is
  why Marcus can run Claude Code, Codex, and a custom runtime on the same project
  simultaneously.
- **Full observability.** Every task, artifact, decision, and agent action is
  persisted to the board. Replay what happened, audit who did what, measure
  coordination overhead, and debug why an agent went off-rails — without
  instrumenting anything. Cato gives you a live visualization; the board gives
  you the ground truth.

<p align="center">
  <img src="docs/assets/modes.png" width="720" alt="Universal agent support via MCP — Runner mode and Attach mode">
</p>

---

## Get Started

**Prerequisites:**
- macOS or Linux (Windows users: install [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install), then follow the Linux instructions)
- Python 3.11+
- `tmux` (`brew install tmux` on macOS, `sudo apt install tmux` on Ubuntu/Debian)
- An LLM provider (Anthropic, OpenAI, or [Ollama](https://ollama.ai))
- An MCP-compatible coding agent
  - **Runner mode** (one-command): [Claude Code](https://claude.ai/code) + `tmux`
  - **Attach mode** (any agent): Codex, Gemini CLI, Kimi, AutoGen, LangGraph, custom

### Step 1: Install

```bash
git clone https://github.com/painted-porch/marcus.git
cd marcus
pip install -e .
cp -r skills/marcus ~/.claude/skills/marcus  # Claude Code + Runner mode only
```

### Step 2: Configure your LLM provider

```bash
cp .env.example .env
cp config_marcus.example.json config_marcus.json
```

Edit `.env` for your API key:

```bash
CLAUDE_API_KEY=sk-ant-api03-your-key-here
```

> Marcus reads `CLAUDE_API_KEY` (not `ANTHROPIC_API_KEY`) so it doesn't
> interfere with Claude Code's subscription auth.

Do not have a provider key yet?
Get an Anthropic key at [console.anthropic.com](https://console.anthropic.com), or an OpenAI key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys).

| Provider  | Cost | Setup |
|-----------|------|-------|
| Anthropic | Paid | Set `CLAUDE_API_KEY` in `.env` — works out of the box |
| OpenAI    | Paid | Set `OPENAI_API_KEY` in `.env`, set `ai.provider` to `"openai"` in `config_marcus.json` |
| Ollama    | Free | Install [Ollama](https://ollama.ai), pull a model, set `ai.provider` to `"local"` |

> This is the LLM Marcus uses for task decomposition. Your coding agents use their own keys separately.

### Step 3: Start Marcus

```bash
./marcus start
```

When the server is up, Marcus prints the local address it is listening on, which defaults to `http://localhost:4298`.
Seeing that line means the start worked; if that port is busy, pass `--port` with another number.

See the board in your terminal at any time:

```bash
./marcus board               # snapshot — print once and exit
./marcus board --watch       # live view — refreshes every 2 s (Ctrl+C to stop)
./marcus board -w --interval 5   # live view with a 5-second refresh rate
./marcus board --project my-project   # filter to a specific project
./marcus board --list        # list all projects in the database
```

The live board (`--watch`) polls the SQLite database and re-renders in-place — you can watch tasks move from **Backlog → In Progress → Done** as agents work in real time.

### Step 4: Choose a visual dashboard (optional)

**Cato** — real-time visualization with a built-in kanban board. Watch every agent decision, every coordination event, every artifact lineage as it happens.

📹 **[Watch the Cato demo →](assets/demo/cato_demo.mp4)** *(One prompt, eight agents, zero chat — fully observable.)*

```bash
# In a sibling directory
git clone https://github.com/painted-porch/cato.git
cd cato && pip install -e . && ./cato start
# Open http://localhost:5173
```

**Planka** — drag-and-drop kanban UI (requires Docker):

```bash
docker compose up -d
# Edit config_marcus.json: set kanban.provider to "planka"
./marcus start
# Open http://localhost:3333  (login: demo@demo.demo / demo)
```

### Step 5: Your first project — Runner mode

```bash
mkdir ~/projects/my-todo-app
cd ~/projects/my-todo-app
claude --dangerously-skip-permissions
```

Then inside Claude Code, prompt:

```
/marcus Build a todo app with authentication using 3 agents
```

The `/marcus` skill registers the MCP server, injects the agent prompt, decomposes
the project, and spawns agents in tmux panes. You walk away, you come back to
working software.

<details>
<summary><strong>Using a different agent? Use Attach mode — same board, same coordination, you wire the agents yourself.</strong></summary>

Marcus is an MCP server at `http://localhost:4298/mcp`. Any agent that speaks MCP
can participate. Runner mode automates the wiring below; Attach mode gives you
manual control.

---

**Step 1 — Connect your agent to Marcus**

Point your agent's MCP configuration at the running Marcus server:

```
http://localhost:4298/mcp   (HTTP transport)
```

For Claude Code users without tmux, register from inside your project directory:

```bash
cd ~/projects/my-todo-app
claude mcp add --transport http marcus http://localhost:4298/mcp
```

For other runtimes, consult your agent's MCP docs for how to register an HTTP MCP server.

---

**Step 2 — Give your agent the system prompt**

`prompts/Agent_prompt.md` is the complete behavioral spec for a Marcus worker. It
tells your agent exactly how to call Marcus tools, manage the work loop, handle
context and artifacts, report blockers, and when to exit. **Without it your agent
won't know the protocol and will stall.**

Copy it into every project directory an agent will run in:

```bash
cp <marcus-dir>/prompts/Agent_prompt.md ~/projects/my-todo-app/CLAUDE.md
```

For non-Claude runtimes, paste the contents into your agent's system prompt instead.

---

**Step 3 — Bootstrap the board (one agent, once)**

One agent must call `create_project` to decompose the work and populate the board.
Do this before workers start:

Ask your agent to call the `create_project` MCP tool:

- `description` — what you want to build, in plain language
- `project_name` — a name for the board

Marcus returns a `project_id`, `recommended_agents` count, and the full task graph. When it returns, tasks are on the board and immediately available.

That same agent can then join the work loop as a worker.

---

**Step 4 — Start workers**

Each worker calls `register_agent` once at startup (name, role, skills), then enters
the work loop driven by `Agent_prompt.md`:

1. Call `request_next_task` — Marcus returns a task or a retry signal
2. If no task: wait `retry_after_seconds` and try again — **do not exit**; work may still arrive
3. If task received: call `get_task_context` to fetch artifacts from dependency tasks
4. Do the work
5. Call `log_decision` for any significant architectural choice
6. Call `log_artifact` for any file other agents will need (specs, schemas, design docs)
7. Call `report_task_progress` at 25%, 50%, 75%, 100%
8. Immediately call `request_next_task` again — do not wait

Marcus handles dependency ordering, lease isolation, and artifact routing.

> **Building a runner for another runtime (AutoGen, LangGraph, custom)?**
> See [PROTOCOL.md](PROTOCOL.md) for the machine-readable agent protocol spec.

</details>

<details>
<summary><strong>Running experiments at scale? Use Posidonius for multi-run management.</strong></summary>

[Posidonius](https://github.com/lwgray/posidonius) is the experiment dashboard
for launching and managing multi-agent runs across any CLI agent. It handles
spawning, experiment tracking, and provides a web UI for monitoring.

```bash
git clone https://github.com/lwgray/posidonius.git
cd posidonius && pip install -e .
```

See the [Posidonius README](https://github.com/lwgray/posidonius) for setup.
By default Posidonius writes projects to `~/experiments/`.

</details>

---

## Documentation

- [Configuration Reference](docs/source/developer/configuration.md) — all options
- [Agent Workflow Guide](docs/source/guides/agent-workflows/agent-workflow.md) — how agents interact
- [Development Workflow](docs/source/developer/development-workflow.md) — daily dev workflows
- [Local Development Setup](docs/source/developer/local-development.md) — first-time setup
- [Architecture Deep Dive](docs/source/architecture/) — the board pattern in detail
- [PROTOCOL.md](PROTOCOL.md) — agent protocol spec (build your own runner)
- [ROADMAP.md](ROADMAP.md) — where we're headed

---

## Community

- [**Discord**](https://discord.gg/DZWTbXr4) — real-time help, questions, and feedback. Report errors in `#bugs`
- [**GitHub Issues**](https://github.com/painted-porch/marcus/issues) — detailed bug reports (use [bug report template](/.github/ISSUE_TEMPLATE/bug_report.md)) and feature requests
- [**GitHub Discussions**](https://github.com/painted-porch/marcus/discussions) — ideas and long-form questions

**For researchers and educators:** Board-mediated coordination extends the blackboard pattern to autonomous LLM agents - a named, citable variant for multi-agent-over-MCP systems. Marcus is MIT-licensed — use it in courses,
papers, and experiments.  The pattern is documented in the [Architecture Docs](docs/source/architecture/).

Named after Marcus Aurelius. The Stoic philosophy runs deep: discipline,
transparency, and letting the system — not any single agent — hold the truth.

---

## 🍽️ PyCon 2026 Sprint Menu

**[View the full sprint menu →](docs/sprints/pycon-2026.md)**

26 curated issues across three tiers (appetizers 15–45 min · main courses 1–3 hr · desserts 3+ hr) plus 7 night-cap bonus explorations. All tagged [`pycon_2026`](https://github.com/painted-porch/marcus/issues?q=is%3Aopen+label%3Apycon_2026). Comment on an issue to claim it before you start.

---

## Contributing

Marcus is open source and community-driven. Good first contributions:

1. **Kanban provider integrations** — Jira, Trello, Linear support
2. **Runners** — automated workflows for new CLI agents (Codex, Gemini CLI, Kimi, AutoGen); see [PROTOCOL.md](PROTOCOL.md)
3. **Documentation** — tutorials, use cases, examples
4. **Use-case definitions** — show what Marcus can build beyond software

```bash
# Fork, then:
git clone https://github.com/YOUR_USERNAME/marcus.git
cd marcus
pip install -r requirements-dev.txt
pytest tests/
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [Local Development Setup](docs/source/developer/local-development.md).

---

<details>
<summary><strong>Changelog &amp; milestones</strong></summary>

### Recent updates

| Date           | Update |
|----------------|--------|
| **2026-04-26** | v0.3.6 — parallel experiment isolation, agent auto-termination, DONE-task board integrity guards |
| **2026-04-17** | v0.3.4 — `contract_first` default decomposer, `recommended_agents` in API response, `PROTOCOL.md` |
| **2026-04-16** | Presented Marcus and Cato at Machine Learning Ambassador Conference, John Deere Financial (Des Moines, IA) |
| **2026-04-03** | v0.3.0 — SQLite default provider, Epictetus evaluation, `/marcus` skill |
| **2026-03-21** | v0.2.1 — lease recovery, progressive timeouts, structured agent handoffs |
| **2026-03-16** | v0.2.0 — AI-powered validation, centralized config, 115 commits since v0.1.3.1 |
| **2025-10-20** | v0.1.3.1 — sweep-line parallelism algorithm, tmux multi-agent support |
| **2025-10-19** | Presented Marcus to "AI Assistants" Biweekly Group, Blue River Technology (Santa Clara, CA) |
| **2025-10-13** | v0.1.1 — initial release as "PM Agent", MCP protocol, Planka integration |
| **2025-06-15** | Project started as "PM Agent" — rebranded to Marcus in October 2025 |

### Version milestones

| Version    | Date       | Commits | Highlights |
|------------|------------|---------|------------|
| **v0.3.6** | 2026-04-26 | 28      | Parallel experiment isolation, agent auto-termination, DONE-task board integrity guards, Phase 4 lease tuning, spec-coverage ordering fix |
| **v0.3.4** | 2026-04-17 | —       | `contract_first` default decomposer, `recommended_agents` in `create_project` response, `PROTOCOL.md`, pre-fork synthesis, scope annotation |
| **v0.3.0** | 2026-04-03 | 59      | SQLite default provider, Epictetus evaluation, `/marcus` one-command experiments, resilience overhaul |
| **v0.2.1** | 2026-03-21 | 1       | Lease recovery, structured handoffs, configurable LLM temperature |
| **v0.2.0** | 2026-03-16 | 115     | AI-powered validation, centralized config, constraint propagation, MLflow tracking |
| **v0.1.3** | 2025-10-18 | 2       | Optimized project creation, subtask assignment fix |
| **v0.1.2** | 2025-10-16 | ~50     | CPM scheduling, unified dependency graphs, cross-parent wiring |
| **v0.1.1** | 2025-10-13 | ~80     | Initial release. Rebranded PM Agent → Marcus. MCP, Planka, NLP tools |

Full history in [CHANGELOG.md](CHANGELOG.md). What's next in [ROADMAP.md](ROADMAP.md).

</details>

---

## License

MIT — see [LICENSE](LICENSE).

<p align="center"><em>The board is the system.</em></p>
