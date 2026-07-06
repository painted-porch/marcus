# ADR 0012: Session-Model Migration — Spawn-per-Task → Long-Lived Pull-Loop Sessions

**Status:** Accepted (amended 2026-07-06: added D11 — false-recovery fencing, from advisory-panel review)

**Date:** 2026-07-05

**Deciders:** Lawrence Gray (with Claude analysis; decisions logged to Simon: `e836842e`, `b90e948a`, `9548fed8`, `40687aa6`)

**Tracking:** Epic [#706](https://github.com/lwgray/marcus/issues/706) (plan) · Epic [#701](https://github.com/lwgray/marcus/issues/701) (build-blockers, re-sequenced)

---

## Context

Marcus today spawns **one ephemeral agent process per task** — a `claude` CLI in a
tmux pane, in its own git worktree, killed after a single task. A layer triage of
all 22 open build-blockers showed that **4 of the 6 run-killing bugs live partly or
wholly in that spawning/babysitting layer** (#628 worktree explosion, #703 watchdog
false-kills, #667 respawn runaway, #700 merge-recovery wedge), while agent
platforms now provide process management natively.

The migration: **Marcus spawns nothing.** Long-lived agent **sessions** (started by
a thin *launcher* outside Marcus core) register once, then loop:
`request_next_task → work → report → repeat`. Marcus tracks agents at exactly one
level — the **lease** — and stays deliberately blind to the OS process.
Decomposition moves up to **session-seam granularity**. Provenance moves from
"one transcript per ephemeral process" to a **contract obligation** plus
task↔transcript links.

That migration forces ten design decisions and inherits five review-found
obligations. This ADR records all of them.

---

## Decisions

### D1 — Session isolation: one worktree per session

Each session gets a long-lived git worktree + branch scoped to the **session's
lifetime** (not per task). Worktree count = session count: small, stable, no churn.
Sessions pull `main` before starting each new card.

- **Why:** preserves isolation, attribution, and the #697 git-delta validation
  while deleting the per-task churn that produced #628 (279 worktrees for 9 tasks).
  Shortest migration distance.
- **Rejected:** shared-main checkout (no isolation; mid-refactor breakage visible to
  all; gate cannot evaluate one session's work); clone-per-session (heavyweight;
  its real payoff is *remote* sessions — documented as the future for distributed
  pools).

### D2 — Merge home: hybrid — mechanical when clean, integrator card when not

The completion gate verifies "merges cleanly onto current main" as **evidence**. A
deliberately-dumb step (~20 lines) auto-fast-forwards clean merges. **Any conflict
spawns a board-visible integrator card** claimed by an intelligent session.

- **Why:** most merges are mechanical, but conflicts need judgment — and per
  Invariant #2, implementation judgment belongs with agents on cards, not inside
  Marcus core. This encodes the #700 lesson: the moment the mechanical path would
  need intelligence, it stops and escalates. Every merge is audited.
- **Hard rule:** the mechanical path never resolves, rebases, or recovers. No
  intelligence in the dumb path, ever.
- **Rejected:** launcher-executes-all (a dumb ops layer inherits conflict
  resolution → rebuilds #700 elsewhere); integrator-card-always (every trivial
  merge waits for a session and burns tokens).

### D3 — Liveness: silence-timeout + budget ceiling (two dials, not one lease)

The old lease conflated *"is the agent alive?"* with *"how long may it hold the
card?"*, coupling failure-detection latency to card size. Split them:

- **Liveness** = MCP-activity silence timeout (~15–30 min), independent of card
  size. `touch_lease` already fires on every MCP call; the D4/D8 decision-logging
  obligation doubles as the heartbeat.
- **Budget ceiling** = a generous, estimate-scaled bound (~3× estimate) catching
  chatty zombies. **Replaces `max_renewals` / `stuck_task_threshold_renewals`**
  (review obligation O4).
- The #667 circuit breaker (PR #707) applies unchanged: N consecutive *silent*
  recoveries → BLOCKED.
- **Rejected:** fixed longer leases (dead session holds a 4-hour card for 4 hours);
  estimate-proportional leases (estimates unreliable; detection still scales with
  card size).

### D4 — Provenance enforcement: measured at the gate, enforced by liveness

`log_decision` is a contract obligation, but the completion gate **never rejects
for it** — it *measures* decision density and stamps a provenance score on the
completion record (the #677 `independently_verified=False` honesty-stamp pattern).
The **cadence** is enforced by the D3 silence timeout, which cannot be gamed
retroactively.

- **Why:** hard gates on self-reported virtue create retry pressure → compliance
  theater — the exact #636 failure that forced Invariant #2 v2. Liveness is the
  non-gameable channel; quality is measured where gating would be gamed.
- **Residual risk (named):** a stuck session could emit junk decisions to look
  alive — bounded by the budget ceiling and visible in post-hoc audit.

### D5 — Seam rule: structural boundaries decide *where*; hours only decide *whether*

Decomposition cuts **only** at declarable structural boundaries:

1. Repo / service / deploy-unit edges;
2. Interface surfaces where the **seam test** passes — *the boundary can be written
   as a contract smaller than either side* (schema, API, event, file format);
3. Repetition — the same operation × N independent targets.

Hours act only as a **floor**: no cut at all unless the chunk exceeds roughly one
session-day of work. The enterprise force-decompose rule and minute-scale
thresholds in `should_decompose` are removed.

- **Why:** hours-only thresholds fix card *count* but not cut *placement* (#649's
  disease). Contract-first already generates contracts — the seam test is a
  prompt-and-validation change, not new infrastructure. The Phase-4 detectors
  (#669 structure-compare, #638 orphan trees, #693 shadowed exports) are the
  safety net for wrong cuts.
- **Successor (named, not built):** coupling-graph min-cut — the #267
  topology-aware research direction. Structure-rule now, graph-cut when measured.

### D6 — Pool authority: operator-fixed session count

The launcher starts exactly the number of sessions the operator requests
(`--sessions N`). Board-depth autoscaling is a documented later enhancement;
`compute_desired_agent_count` is demoted to an **advisory** metric an operator or
future autoscaler may consult.

- **Why:** thinnest possible launcher; cost stays sovereign with the human — right
  for the current operating reality. Binding the scheduler signal would re-couple
  ops to core internals.

### D7 — Session ↔ project cardinality: 1:1 per session lifetime

A session is started *for* a project, registers once with it, and exits when the
board drains. Floating sessions (per-claim scoping across projects) are the
documented fleet future.

- **Why:** matches existing registration scoping, keeps the registration-persistence
  fix (obligation O1) small, minimizes migration surface.

### D8 — Partial-work recovery: resume-by-instruction + checkpoint duty

Keep the existing recovery handoff (next claimant merges the dead session's branch,
reads progress notes and decisions, continues) — it survives D1 cleanly. Add a
**checkpoint obligation** to the session contract: commit + log a decision at each
meaningful unit, at minimum ~every 30 minutes.

- **Why:** at coarse granularity, non-resumable death wastes hours. The checkpoint
  duty makes resumability a property of the work rather than luck — and it *is*
  the heartbeat (D3) *is* the provenance stream (D4): three obligations collapse
  into one behavior.

### D9 — Internal subagents: aggregate mandatory, detail optional

A runtime adapter MUST report one trustworthy number per card: **aggregate
tokens/cost for the session during the card's window, subagents included**. It MAY
pass per-subagent detail through as enrichment (`parent_agent_id` exists in the
ingester schema). Marcus core never depends on any runtime's internal transcript
format.

- **Why:** the audit ledger's credibility rides on the aggregate being right;
  mandating inner detail couples the durable core to every runtime's internals.

### D10 — "A run" leaves the core

Marcus core becomes **runless** — a continuously-operating board with no
experiment start/stop coupling in core paths. "A run" becomes a **harness-side
concept**: a labeled time-window over board activity plus a session-pool
lifecycle, owned by the launcher/experiment harness. Benchmarks (the Phase-6
Shape-B gate, coordination experiments) keep full run semantics.

- **Why:** the product vision is continuous operation; the research instrument
  needs runs. This split serves both without compromise.

### D11 — False-recovery fencing: the two-robots-one-chore rule *(added 2026-07-06)*

**The problem this closes** (found in advisory-panel review — "Kaia" seat): under
spawn-per-task, recovery *killed* the agent — death was the fencing token, so a
recovered task could never be finished twice. The session model removes that
guarantee: Marcus is deliberately process-blind, so a session that trips the D3
silence timeout while **silently concentrating** (long build, deep work, no MCP
calls) is *still alive*. Marcus reassigns its card to another session; the first
session later reports 100%. Two live completions, one card, no rule for who wins
— and D3's timeout (15–30 min) overlapped D8's checkpoint cadence (~30 min), so
false recovery was not rare; it was designed in.

**The decision — three parts:**

1. **Lease epoch (the fencing token).** Every claim of a card stamps a
   monotonically-increasing `lease_epoch` on the assignment. Every
   `report_task_progress` call carries the epoch it was issued. A report bearing
   a **stale epoch** is never applied as a completion.
2. **Preserve, don't discard — reconcile via the board.** A stale-epoch
   completion is *not* thrown away (the O3 failure): the late session's branch
   and evidence are preserved, and Marcus spawns a **board-visible
   reconciliation card** (the D2 integrator-card pattern, reused) whose contract
   is: compare the two attempts, keep the verified better one or merge them,
   record the decision. The audit trail shows exactly what happened.
3. **Pin the dials apart.** The D3 silence timeout MUST be ≥ 2× the D8
   checkpoint cadence, enforced as a config invariant (defaults: checkpoint
   obligation ≤ every 20 min; silence timeout 45 min). False recovery becomes
   rare-by-construction instead of designed-in; when it still happens, parts 1–2
   make it safe.

- **Why:** the coordinator can take the *job* away (the lease) but not the
  *life* away (the process) — so ownership must be decided by a token both
  sides carry, not by an assumption of death. This is the classic distributed
  fencing problem; the epoch is the standard answer, and routing the conflict
  to a board card keeps resolution intelligent, audited, and consistent with
  Invariant #2.
- **Consequences:** `AssignmentLease` gains `lease_epoch`; the stale-completion
  guard (`task.py:3865-3960`) changes semantics from *reject-and-discard* to
  *fence-and-reconcile*; obligation **O3 is closed by this decision**. A
  Phase-3 verification scenario is added: falsely-recover a live session, let
  both report, assert exactly one completion, zero discarded work, one
  reconciliation card in the audit trail.

---

## Inherited obligations (review-found gaps — not choices)

An adversarial review of the migration plan found five failure modes that appear
only *after* going session-based. Each is assigned a home by this ADR:

| # | Obligation | Lands in |
|---|---|---|
| O1 | `agent_status` / `agent_project_map` are never rehydrated on restart (leases are) — a restart would orphan every registered session (`server.py:200-201`, `task.py:5150-5155`) | Phase 3 registration work (with D7's 1:1 scoping) |
| O2 | Cost attribution is keyed to the spawn-registry file + per-agent cwd — both deleted. Re-home the `(session → agent/run/project)` binding at register/claim time (`worker_ingester.py:26,312-316`) | Phase 3, per D9 |
| O3 | The stale-completion guard assumes agents die on recovery; a falsely-recovered *live* session would have its coarse-task output discarded (`task.py:3865-3960`) | **Closed by D11** (epoch fencing + board-mediated reconciliation) |
| O4 | `max_renewals=10` / `stuck_task_threshold_renewals=5` flag healthy long tasks as stuck | **Deleted** — replaced by D3's budget ceiling |
| O5 | No session context management exists; context grows unboundedly across cards in one process | Session contract + harness loop (compaction/reset between cards), per D8's card boundaries |

---

## Consequences

- **Deleted, not fixed:** #628 (worktree explosion), #703 (thrash watchdog), and
  #667's spawn-backoff half — they are artifacts of per-task spawning.
- **Decomposition changes meaning:** fewer, chunkier, structurally-placed cards;
  the graph-quality bugs (#669/#615/#665/#637/#620/#619/#617/#649) must be
  **re-triaged at the new granularity** before being fixed (some will vanish).
- **The board remains the only channel** (Invariant #3): cross-session context
  flows through artifacts and decisions at card boundaries; nothing in this ADR
  introduces agent-to-agent communication.
- **Invariant #1 is strengthened:** the launcher sets only session *count*;
  work distribution remains pull-based self-selection. Marcus never assigns a
  session to a task.
- **What Marcus core is, after this:** board + leases + contract authoring +
  completion gates + audit/cost ledger. Everything process-shaped lives outside.

## References

- Epic #706 (migration plan, phases, verification procedure)
- Epic #701 (build-blocker triage: 18 keep / 2 split / 2 shed)
- PR #707 (#667 Fix 3a — the circuit breaker, board-policy half)
- Issue #267 (topology-aware decomposition — D5's research successor)
- Invariant #2 v2 (CLAUDE.md, Multi-Agency Proclamation) — verification authorship
- Simon decisions: `e836842e` (D1+D2), `b90e948a` (D3+D8), `9548fed8` (D4+D9),
  `40687aa6` (D5/D6/D7/D10), `9fd035c4` (the plan), `9479b614` (change set)
