# Marcus Session Protocol

**v0.4-draft · July 2026 · the partnership interface**

Marcus is a system of record for work done by AI agents, built as a
multi-agent coordination system where a shared board is the only channel.
This document is the contract any session (a vendor CLI, a custom runtime, or
a human) follows to participate in a Marcus-coordinated project. It extends
[`PROTOCOL.md`](https://github.com/painted-porch/marcus/blob/main/PROTOCOL.md)
(shipped, v0.3.8) with the session-model semantics now landing, and delineates
what is convergent with Stanford's DeLM (Mao & Mirhoseini, 2026) versus what
only Marcus provides.

Status labels: **[SHIPPED]** = on `main` today. **[LANDING]** = session-model
migration (epic #706).

---

## 1. Invariants (non-negotiable)

1. **Sessions self-select work.** Work is pulled, never pushed or pre-assigned.
2. **Sessions own the *how*.** Marcus owns the *what* and the *why*. Two sessions
   given the same card may produce legitimately different implementations.
3. **The board is the only channel.** No session-to-session messages. No session
   knows another exists.

## 2. Roles

| Role | Description |
|---|---|
| **Board** | The Marcus MCP server (`/mcp` endpoint). Owns all state: cards, leases, artifacts, decisions, ledger. |
| **Session** | Any MCP-capable participant: Claude Code, Codex, Gemini CLI, AutoGen/AG2, LangGraph, a human. Stateless by design; all state lives on the board. |
| **Launcher** | External to Marcus **[LANDING]**. Starts long-lived sessions; Marcus tracks exactly one thing per session: the lease. |
| **Gates** | Completion checks that demand evidence, not claims **[LANDING: behavioral verification]**. |
| **Ledger** | The audit story, in two layers. Shipped: immutable per-call cost events with agent, task, and session attribution, price-versioned so history is never rewritten (`cost_tracking`), plus conversation and event logs stamped with task IDs (`src/logging`). Landing: a unified per-task view of who, why, cost, and transcript, assembled for humans by Cato. |

## 3. Lifecycle

**Bootstrap (creator role, once):** `create_project(description, project_name)`
→ Marcus decomposes into a dependency-ordered task graph on the board.

**Session loop (workers):**

```
register_agent(agent_id, name, role, skills)        # once at startup
loop:
  request_next_task()                               # → card + lease, or retry_after
  get_task_context(task_id)                         # dependency artifacts, DAG-routed
  ... do the work (session owns the how) ...
  log_decision(...)                                 # architectural choices → board
  log_artifact(...)                                 # specs, schemas, files → board
  report_task_progress(25/50/75/100)                # or report_blocker → AI assist
  # completion passes the gate or returns with feedback
```

"No task" is transient: sleep and retry; never exit. Exit is the launcher's call.

## 4. What the board guarantees **[SHIPPED]**

- **Single leaseholder**: one session per card at a time.
- **Dependency ordering**: `request_next_task` only returns unblocked cards.
- **Lease recovery**: a dead session's expired lease returns the card to the pool
  with its progress intact; another session resumes from board state.
- **Artifact routing**: context arrives from dependency tasks automatically; the
  board walks the DAG so sessions never have to.
- **Scope annotation**: dependency artifacts arrive marked `in_scope` or
  `reference_only`.

## 5. Lease and liveness semantics

- **[SHIPPED]** Time-limited leases with expiry states (`ACTIVE`,
  `EXPIRING_SOON` under 1h, `EXPIRED`); adaptive renewal duration calculated
  from the session's own median update cadence (`src/core/assignment_lease.py`).
- **[LANDING]** **Two-dial liveness** (ADR-0012 / D3): the single lease splits
  into a *silence timeout* on MCP activity (default 45 min; every tool call
  touches the lease, so decision-logging doubles as the heartbeat) and a
  *budget ceiling* (about 3x estimate) that catches chatty zombies. Renewal
  counting is retired.
- **[LANDING]** **Lease-epoch fencing** (ADR-0012 / D11, the two-robots-one-chore
  rule): every claim of a card stamps a monotonic `lease_epoch`; every progress
  report carries the epoch it was issued. A stale-epoch report is **never
  applied as a completion and never discarded either**: its branch and evidence
  are preserved and routed to a board-visible *reconciliation card* that
  compares the attempts, keeps or merges the verified better one, and records
  the decision. A config invariant pins the dials apart (silence timeout at
  least 2x the checkpoint cadence), making false recovery rare by construction
  and safe when it happens.

## 6. Evidence and provenance

- **[SHIPPED]** Decisions and artifacts are first-class board objects. Every
  LLM call writes an immutable `token_events` row with agent, task, session,
  and model attribution; pricing is versioned by effective date and cost is
  computed at query time, so price changes never rewrite history. Task
  assignments, progress, and blockers are logged as conversation events
  stamped with their task IDs. The aggregator even counts its own attribution
  gaps (orphan events) rather than hiding them.
- **[LANDING]** The unified ledger walk: one view per task joining who, why,
  cost, and the full session transcript. The session model's registry makes
  transcripts first-class; Cato assembles the human-readable trail.
- **[LANDING]** **Evidence-gated completion:** the acceptance contract is
  authored at planning time, when the card is cut; the gate checks that the
  work *behaves*, with tests run and evidence attached, not merely that it was
  claimed. Failure-sharing and admission-verified context land in the same
  phase.
- **[LANDING]** **Measured, not gated** (ADR-0012 / D4): the gate never rejects
  on *self-reported* virtue. Decision density is measured and stamped as a
  provenance score on the completion record; cadence is enforced through the
  liveness channel, which cannot be gamed retroactively. Hard-gating
  self-reports produces compliance theater; Marcus refuses it by design.

## 7. Decomposition rule **[LANDING]**

Cut work apart only where the boundary between two pieces can be written down
as a **contract smaller than either piece**: repository edges, declarable
interfaces, repeated operations across many targets. Structural boundaries
decide *where* to cut; effort estimates only decide *whether* (D5).
Coordination happens only at the seams no single session can see across.

---

## 8. Marcus and DeLM: the honest delineation

Two systems arrived at the same substrate independently. The convergence is
the validation; the differences are the regime.

### Convergent (both systems, independently)

- Shared state is the coordination substrate: no orchestrator routing, no
  agent-to-agent messaging.
- Agents/sessions **self-select** work from a queue/board, asynchronously.
- Compact, **verified** updates over raw traces; unsupported claims are
  rejected before they contaminate shared state.
- **Failures are first-class shared state**: a dead end recorded once is a
  detour no one else takes.

### Uniquely Marcus (the durability and accountability regime)

- **Persistence across time.** The board outlives any session, any day, any
  project phase. DeLM's shared context lives for one task run.
- **Registration and identity.** Named, long-lived, heterogeneous sessions:
  any vendor's CLI or a human on the same protocol. DeLM's workers are
  ephemeral and homogeneous within a single run.
- **Lease and liveness semantics.** Exclusive ownership with expiry recovery
  (shipped), evolving to silence-timeout liveness plus budget ceilings
  (landing). DeLM claims tasks but has no liveness model; its workers are
  presumed alive.
- **Fencing and reconciliation (D11).** Split-brain completion is detected,
  fenced, and reconciled openly. DeLM has no failure-recovery story because
  its regime doesn't need one.
- **Provenance ledger as product.** Who, why, cost, and transcript per task,
  auditable weeks later. DeLM logs traces for research; the ledger is not the
  point.
- **Contract-at-planning-time.** Acceptance criteria are authored when the
  card is cut, and gates judge behavior against that contract. DeLM verifies
  that a gist faithfully summarizes its evidence: fidelity, not acceptance.
- **Seam decomposition rule.** An explicit, stated criterion: structural
  boundaries decide *where* to cut; effort estimates only decide *whether*
  (D5). DeLM's decomposition is LLM-generated; its own Limitations section
  names adaptive decomposition as open work.
- **Gating philosophy.** Hard-gate only non-gameable channels (behavioral
  evidence, liveness); measure and stamp what is self-reported (D4). DeLM
  hard-gates admission because gist fidelity is externally checkable. It is
  the same principle, which Marcus extends across a fleet's whole lifecycle to
  avoid compliance theater.

### Uniquely DeLM (and worth adopting)

- **Hierarchical gist, summary, and raw layers with selective unfolding**:
  cost-bounded context access; agents pay for detail only when a subtask
  needs it.
- **Admission-time verification of every shared-context entry**, implemented
  and ablated (accuracy drops from 60.1% to 55.2% without it). Marcus's
  admission-verified context is on the roadmap; DeLM's is measured. Adopt the
  mechanism, cite the ablation.
- **Stable shared-state prefix for KV-cache reuse**: a systems optimization
  that falls out of board-first design; free efficiency Marcus should claim
  too.

---

## 9. Conformance

A runner or session integration is conforming when a **vanilla outside agent**
(stock Claude Code, an AG2 instance, anything MCP-capable with nothing
Marcus-specific beyond this protocol in its prompt) can `register_agent`, pull
a card, complete it through the gate, and appear correctly in the ledger.

Reference materials: [`PROTOCOL.md`](https://github.com/painted-porch/marcus/blob/main/PROTOCOL.md) ·
[`prompts/Agent_prompt.md`](https://github.com/painted-porch/marcus/blob/main/prompts/Agent_prompt.md) ·
synthetic-agent harness (issue #383).
