# Introduction to Marcus

## A Stoic Approach to Multi-Agent Software Development

Marcus is an open-source orchestration server for AI coding agents. You describe what to build. Marcus breaks the work into tasks on a shared kanban board. Multiple agents pull tasks independently, write the code, and coordinate **through the board — never through chat**. You walk away; you come back to working software.

Named after Marcus Aurelius. The Stoic philosophy runs deep: **discipline, transparency, and letting the system — not any single agent — hold the truth.**

## The Core Idea: Board-Mediated Coordination

Every other multi-agent framework today coordinates agents through **conversation** — group chats, message passing, chain-of-thought relays. This works with 2–3 agents. At scale, it collapses: context degrades, work duplicates, failures cascade, and adding agents adds chaos.

The fundamental mistake: treating coordination as a *conversation* problem. It's a **state management** problem.

Marcus uses a different approach: **give agents a shared task board instead of making them talk to each other.** We call this **board-mediated coordination** — a modern, agent-native take on the classical [Blackboard pattern](https://en.wikipedia.org/wiki/Blackboard_(design_pattern)) (Hayes-Roth, 1985), applied to autonomous LLM agents coordinating over MCP.

|                        | Group Chat Coordination     | Board-Mediated Coordination |
|------------------------|-----------------------------|-----------------------------|
| **Used by**            | AutoGen, CrewAI, LangGraph  | **Marcus**                  |
| **Coordination**       | Conversation between agents | Shared board state          |
| **Context at scale**   | Degrades                    | Preserved per-task          |
| **Agent failure**      | Lost context, no recovery   | Resume from board state     |
| **Visibility**         | Chat logs                   | Full audit trail + dashboard |
| **Add more agents**    | More chaos                  | More throughput             |

Marcus doesn't compete on raw speed. It competes on **coordination quality, observability, and enterprise readiness.**

## The Three Agent Invariants

Marcus is a board-mediated, blackboard-architecture Multi-Agent System. Three invariants are non-negotiable:

1. **Agents self-select work.** Agents pull tasks via `request_next_task`. Marcus never pushes work, never assigns without request, never forces a specific agent onto a specific task.
2. **Agents make all implementation decisions.** Marcus says **WHAT** to build and **WHY** it matters. Marcus never says HOW — no library choices, no patterns, no internal code structure. Two agents given the same task must be able to produce legitimately different implementations.
3. **Agents communicate exclusively through the board.** No agent-to-agent direct communication. No Marcus-to-agent push outside task assignment. The board is the only channel.

## Core Principles

### 1. Bring Your Own Agent (BYOA)

Marcus is agent-agnostic. Any [MCP](https://modelcontextprotocol.io/)-compatible agent works: Claude Code, Codex, Gemini CLI, Kimi, AutoGen, LangGraph, or a custom runtime.

Two operating modes:

- **Runner mode** — one-command experiments via the `/marcus` skill in Claude Code. Skill registers the MCP server, injects the agent prompt, decomposes the project, spawns N agents in tmux panes.
- **Attach mode** — any MCP agent connects to `http://localhost:4298/mcp`. You wire the agents yourself; same board, same coordination. See [PROTOCOL.md](https://github.com/painted-porch/marcus/blob/main/PROTOCOL.md) to build a runner for a new runtime.

### 2. Context Over Control

Each task carries its own context — requirements, dependencies, artifacts from prior tasks. When an agent picks up a task, it gets exactly the context it needs. No chat history. No lost threads. No duplicate work.

Marcus provides:

- **Clear task definitions** with rich descriptions
- **Dependency awareness** of what must be done first
- **Implementation context** from previous work (artifacts, decisions)
- **Impact visibility** for downstream tasks

Marcus does not micromanage HOW agents accomplish tasks.

### 3. Resilience by Default

When an agent fails, the task stays on the board with its progress. Another agent picks it up and continues. **The board is the system of record.** Lease recovery, structured handoffs, and progressive timeouts are built in.

### 4. Embrace Stochastic Reality

Real-world software development is messy. Agents work in parallel, sometimes duplicate effort, sometimes discover better solutions by accident. Marcus embraces this — the non-linearity often leads to solutions perfectly coordinated systems would miss.

### 5. Safety Through Guardrails, Not Restrictions

Marcus uses a **hybrid intelligence** approach:

- **Rules for safety** — prevent illogical dependencies, ensure task ordering
- **AI for intelligence** — semantic understanding, optimal matching, contextual instructions
- **Fallbacks for reliability** — system continues functioning when AI fails

### 6. Research-First Design

Every conversation logged, every decision tracked, every outcome measured. This enables academic study of multi-agent coordination, community learning from collective patterns, and evidence-based evolution of best practices.

### 7. Democratized Access

- **Cost tracking** — know exactly what coordination costs
- **Model flexibility** — swap expensive models for cheaper ones, including free local LLMs via Ollama
- **Multiple deployment modes** — research, development, production
- **MIT-licensed** — use it in courses, papers, and experiments

## The Marcus Ecosystem

### Marcus — the orchestration server

The coordination server. Runs at `http://localhost:4298/mcp`. Decomposes natural-language project descriptions, manages the task graph, routes artifacts and context, enforces the work loop, recovers from failures.

### Cato — the visual dashboard

[Cato](https://github.com/painted-porch/cato) is the active Marcus dashboard. Real-time visualization of agents, tasks, and board health, with a built-in kanban view. Sibling product — install separately and point at the same data store.

### Posidonius — the experiment platform

[Posidonius](https://github.com/lwgray/posidonius) is the experiment dashboard for launching and monitoring **multi-run** Marcus experiments — benchmarking, parameter sweeps, and parallel batches across any CLI agent. Web UI, run history, integration with Epictetus for grading agent output.

### Epictetus — the grader

A code auditor that grades software projects (and the agents that built them). Integrated into the Posidonius experiment pipeline as a post-run audit step.

### `/marcus` skill — Runner mode launcher

A Claude Code skill (`skills/marcus/SKILL.md`) that wraps experiment setup into one command. Spawns independent Claude CLI agents in tmux panes, each registering with the Marcus MCP server. The fastest way to go from idea → coordinated multi-agent run.

## What Makes Marcus Different

### From traditional project management tools

- **Active intelligence** — understands tasks, not just tracks them
- **Autonomous execution** — agents work independently once assigned
- **Continuous learning** — every project makes the platform smarter
- **Stochastic advantage** — randomness as a feature, not a bug

### From simple AI assistants

- **Project-level thinking** — understands entire systems, not just individual tasks
- **Multi-agent coordination** — manages teams, not individuals
- **Safety guarantees** — hybrid intelligence prevents catastrophic errors
- **Observable behavior** — full transparency through Cato

### From other multi-agent frameworks

- **Coordination model** — board-mediated state vs. group-chat conversation
- **Agent identity** — ephemeral with platform-side learning, not persistent personas
- **Communication** — board-only, not agent-to-agent messaging
- **Approach** — research and discovery, not predetermined workflows
- **Focus** — practical outcomes, not cognitive empathy

## Who Should Use Marcus

- **Solo developers** managing multiple projects who need intelligent task organization
- **Small teams (2–5)** coordinating without formal processes
- **Open-source maintainers** who need visibility into project health
- **Indie hackers** building products with AI assistance who want predictable delivery
- **Research teams** studying multi-agent coordination — Marcus is MIT-licensed and instrumented end-to-end

## The Stoic Way

> **Focus on what you control** — task structure, context, dependencies
>
> **Accept what you cannot** — how agents choose to solve problems
>
> **Learn from what emerges** — pattern recognition and continuous improvement
>
> **Practice transparency** — all actions visible, all decisions logged

---

## Next Steps

- **[Quickstart Guide](quickstart.md)** — install and run Marcus in 5 minutes
- **[Core Concepts](core-concepts.md)** — agents, tasks, projects, dependencies
- **[Concepts](../concepts/)** — design principles in depth
- **[Agent Workflows](../guides/agent-workflows/)** — how agents work the board
- **[PROTOCOL.md](https://github.com/painted-porch/marcus/blob/main/PROTOCOL.md)** — agent protocol spec for building new runners

---

*"You have power over your mind — not outside events. Realize this, and you will find strength." — Marcus Aurelius*
