# Marcus: A Stoic Approach to Multi-Agent Software Development

## Philosophy

Marcus embodies a radical departure from prescriptive multi-agent frameworks. Named after Marcus Aurelius, the platform embraces Stoic principles: **control what you can, accept what you cannot, and learn from what emerges.**

The technical core of that philosophy: **board-mediated coordination** — a modern, agent-native take on the classical [Blackboard pattern](https://en.wikipedia.org/wiki/Blackboard_(design_pattern)) (Hayes-Roth, 1985), applied to autonomous LLM agents coordinating over MCP. Every other framework today treats coordination as a *conversation* problem. Marcus treats it as a *state management* problem. Agents read and write to a shared board; they never message each other directly.

## The Three Agent Invariants

Marcus is a board-mediated, blackboard-architecture Multi-Agent System. Three invariants are non-negotiable:

1. **Agents self-select work.** Agents pull tasks via `request_next_task`. Marcus never pushes work, never assigns without request, never forces a specific agent onto a specific task.
2. **Agents make all implementation decisions.** Marcus says **WHAT** to build and **WHY** it matters. Marcus never says HOW — no library choices, no patterns, no internal code structure. Two agents given the same task must be able to produce legitimately different implementations.
3. **Agents communicate exclusively through the board.** No agent-to-agent direct communication. No Marcus-to-agent push outside task assignment. The board is the only channel.

## Core Principles

### 1. Bring Your Own Agent (BYOA)
Marcus is agent-agnostic. We don't prescribe how your agents should think or operate. Like a good director of engineering, we provide context, clear objectives, and then trust professionals to deliver. Your agents — whether Claude Code, Codex, Gemini CLI, Kimi, AutoGen, LangGraph, or a custom MCP runtime — bring their own capabilities and approaches.

### 2. Context Over Control
We believe agents with the right context and visibility into dependencies can successfully complete tasks. Marcus provides:
- Clear task definitions
- Dependency awareness
- Implementation context from previous work (artifacts, decisions)
- Impact visibility for downstream tasks

We don't micromanage HOW agents accomplish tasks.

### 3. Board-Based Communication Only
Agents communicate exclusively through the project board by logging decisions and artifacts that affect others. No direct agent-to-agent communication. This serves multiple purposes:
- **Preserves context windows** — eliminates conversation overload
- **Maintains transparency** — all coordination is visible
- **Reduces complexity** — no conversation state management
- **Enables research** — complete audit trail for analysis
- **Survives failure** — board state outlives any single agent

### 4. Embrace Stochastic Reality
Real-world software development is random and messy. Agents work in parallel, sometimes duplicate effort, sometimes discover better solutions by accident. Marcus doesn't fight this — it embraces it. The non-linearity often leads to innovative solutions perfectly coordinated systems would miss.

### 5. Safety Through Guardrails, Not Restrictions
Like a computer vision model that works in daylight but fails at night, we don't restrict the model — we install bright lights. Our hybrid intelligence approach uses:
- **Rules for safety** — prevent illogical dependencies, ensure task ordering
- **AI for intelligence** — semantic understanding, optimal matching, contextual instructions
- **Fallbacks for reliability** — system continues functioning when AI fails

### 6. Research-First Design
Marcus is built as both a tool **and** a research platform. Every conversation is logged, every decision tracked, every outcome measured. This enables:
- Academic study of multi-agent coordination
- Community learning from collective patterns
- Evolution of best practices through empirical data
- Development of specialized agent templates

### 7. Democratized Access
Marcus scales from individual developers to enterprises:
- **Cost tracking** — know exactly what coordination costs
- **Model flexibility** — swap expensive models for cheaper ones, including free local LLMs via Ollama
- **MIT-licensed** — use it in courses, papers, and experiments
- **Multiple deployment modes** — research, development, production

## The Marcus Ecosystem

### Marcus — the orchestration server
The MCP-speaking coordination layer. Runs at `http://localhost:4298/mcp`. Decomposes natural-language project descriptions into structured task graphs, manages agent registration, routes context and artifacts, enforces the work loop, recovers from agent failures.

### Cato — the visual dashboard
[Cato](https://github.com/painted-porch/cato) is the active Marcus dashboard. Real-time visualization of agents and tasks, built-in kanban view, board health monitoring, run history. Sibling product — install separately, point at the same data store, get a UI for free.

### Posidonius — the experiment platform
[Posidonius](https://github.com/lwgray/posidonius) launches and monitors **multi-run** Marcus experiments — benchmarking, parameter sweeps, parallel batches across any CLI agent. Web UI, run history, integration with Epictetus for grading agent output.

### Epictetus — the grader
A code auditor that grades software projects (and the agents that built them). Wired into the Posidonius pipeline as a post-run audit step.

### `/marcus` skill — Runner mode launcher
A Claude Code skill that wraps experiment setup into one command. Spawns N independent Claude CLI agents in tmux panes, each registering with the Marcus MCP server. The fastest path from idea to coordinated multi-agent run.

## What Makes Marcus Different

### From other multi-agent frameworks (AutoGen, CrewAI, LangGraph)
- **Coordination model** — board-mediated state vs. group-chat conversation
- **Scale** — group chat collapses past 2–3 agents; the board pattern adds throughput as agents are added
- **Failure recovery** — a crashed agent loses chat context; the board carries on
- **Observability** — chat logs vs. a queryable, structured audit trail
- **Agent identity** — ephemeral with platform-side learning, not persistent personas

### From traditional project management tools
- **Active intelligence** — understands tasks, not just tracks them
- **Autonomous execution** — agents work independently once assigned
- **Continuous learning** — every project makes the platform smarter
- **Stochastic advantage** — randomness as a feature, not a bug

### From simple AI assistants
- **Project-level thinking** — understands entire systems
- **Multi-agent coordination** — manages teams, not individuals
- **Safety guarantees** — hybrid intelligence prevents catastrophic errors
- **Observable behavior** — full transparency through the board and Cato

## The Vision

Marcus is evolving toward a future where:
- **Community templates** — continuously updated agent templates for different domains, skill sets, and project sizes
- **Optimal coordinators** — specialized models trained on successful patterns
- **Research platform** — standard environment for multi-agent studies
- **Accessible AI development** — individual developers can build like enterprises

## The Stoic Way

Like its namesake, Marcus follows Stoic principles:
- **Focus on what you control** — task structure, context, dependencies
- **Accept what you cannot** — how agents choose to solve problems
- **Learn from what emerges** — pattern recognition and continuous improvement
- **Practice transparency** — all actions visible, all decisions logged

Marcus doesn't try to control everything. It creates the conditions for success and lets intelligence emerge. This is the Stoic way of multi-agent development: pragmatic, observable, and continuously learning.

## Getting Started

Marcus is open source and welcomes contributors who share this philosophy. Whether you're an individual developer, a research team, or an enterprise, Marcus adapts to your needs while maintaining its core principles.

Remember: we don't tell agents how to think. We give them what they need to succeed, then get out of their way. The magic happens in the space between structure and freedom.

---

*"You have power over your mind — not outside events. Realize this, and you will find strength." — Marcus Aurelius*
