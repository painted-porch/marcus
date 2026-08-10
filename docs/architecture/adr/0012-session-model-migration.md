# ADR 0012: Session-Model Migration — Spawn-per-Task → Long-Lived Pull-Loop Sessions

**Status:** Accepted (amended 2026-07-06: added D11 — false-recovery fencing, from
advisory-panel review; code pointers corrected 2026-08-09 from the PRE-3 gate-1
call-site inventory — see "Pointer corrections" below)

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

- **Liveness** = MCP-activity silence timeout, independent of card size
  (**default 45 min** — must be ≥ 2× the D8 checkpoint cadence; see the D11
  config invariant). `touch_lease` already fires on every MCP call; the D4/D8
  decision-logging obligation doubles as the heartbeat.
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
meaningful unit, at minimum **every 20 minutes** (default; paired with D3's
45-min silence timeout to satisfy the D11 ≥ 2× invariant).

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
  guard (`task.py:4136-4308`) changes semantics from *reject-and-discard* to
  *fence-and-reconcile*; obligation **O3 is closed by this decision**. A
  Phase-3 verification scenario is added: falsely-recover a live session, let
  both report, assert exactly one completion, zero discarded work, one
  reconciliation card in the audit trail.

#### D11 amendment (2026-08-09): fencing covers four paths, not one

The PRE-3 gate-1 inventory found that D11 as originally written fences only the
*completion* path, while **three further paths hand out or overwrite ownership
with no holder check at all**. All three are made more likely by the session
model, not less. Gate 2 covers all four (Simon `05f56b57`):

1. **Completion** — the original D11 scope (`task.py:4136-4308`).
2. **`renew_lease` takes no `agent_id`** (`assignment_lease.py:437`, lookup at
   `:460`). Any agent's progress report overwrites the real holder's
   `progress_percentage` / `last_progress_message` and resets its expiry. The two
   ownership guards in `report_task_progress` are gated on `status == "completed"`
   and `status == "blocked"` — **plain in-progress reports are unguarded.**
3. **`report_task_progress` silently re-grants the lease**
   (`task.py:4999-5007`): `create_lease(task_id, agent_id, task_obj)` fires for
   whoever reported, gated only on the task not already being DONE. This is a
   second, undocumented claim site outside `request_next_task`.
4. **`touch_lease` is ownership-blind** (`assignment_lease.py:676-698`): it scans
   `active_leases.values()` for the *first* lease matching `agent_id` and breaks,
   never checking which task the call concerned. It is the highest-frequency lease
   mutation in the codebase (fired from `handlers.py` after every MCP call
   carrying an `agent_id`) and a literal `active_leases` grep never finds it.

**Also required:** `lease_epoch` must be written by `_persist_lease`
(`assignment_lease.py:1408-1426`) or a Marcus restart resets the epoch and defeats
the fencing. And the four external `del active_leases[...]` sites (`task.py:500,
4721, 5673, 6582`) bypass `lease_lock`, which every manager-internal mutation
takes — the fencing token is only as reliable as the least careful of them.

**No fencing token exists today.** A whole-repo grep for `epoch` / `lease_epoch` /
`fencing` returns nothing relevant. D11 is greenfield on both sides of the
protocol: the dataclass has no field, `request_next_task` returns none, and
`report_task_progress` accepts none. It is a **protocol change** requiring
coordinated edits to Marcus, the MCP tool signatures, and the agent prompts — not
a modification of an existing check.

---

## Pointer corrections (2026-08-09)

Line references in the original draft had drifted. Corrected from the PRE-3
gate-1 inventory, each re-verified against source:

| Was | Is | Notes |
|---|---|---|
| `task.py:3865-3960` (stale-completion guard, cited twice) | **`task.py:4136-4308`** | ~300 lines further down, and now *three* blocks: cold-cache fallback (`4180-4196`), #667 Fix 2 uncontested-accept (`4209-4283`), final rejection (`4285-4308`) |
| `task.py:5150-5155` (`_scope_tasks_to_project` raise) | **`task.py:5817`** | 5150-5155 is now an unrelated except/finally block. Note the raise never reaches the caller — `request_next_task` swallows it at `task.py:3015`, so the O1 failure presents as an ordinary failed poll, which a long-lived session will retry forever |
| `task.py:3874-3876` (lease reload) | **`assignment_lease.py:1430`** | 3874-3876 is a docstring about subtask lookup. Rehydration is lazily triggered from inside `request_next_task`, *not* at startup — so O1's fix cannot live in `MarcusServer.__init__` or it will never run in HTTP mode (`server.py:793-797`) |
| `worker_ingester.py:26,312-316` | **`:24-27`** (binding docstring), **`:299-304`** (the cwd comment) | See the corrected O2 row above |
| `server.py:200-201` | unchanged — **correct** | verified |

## Inherited obligations (review-found gaps — not choices)

An adversarial review of the migration plan found five failure modes that appear
only *after* going session-based. Each is assigned a home by this ADR:

| # | Obligation | Lands in |
|---|---|---|
| O1 | `agent_status` / `agent_project_map` are never rehydrated on restart (leases are) — a restart would orphan every registered session (`server.py:200-201`; the raise is `task.py:5817` in `_scope_tasks_to_project`; lease rehydration for contrast is `assignment_lease.py:1430`, called only from `LeaseMonitor.start` at `:1771`). Neither dict is ever deleted, popped, or cleared anywhere in `src/` — the state is *both* non-durable across restarts and append-only within a process | Phase 3 registration work (with D7's 1:1 scoping) |
| O2 | Cost attribution is keyed to **the per-agent cwd** — the spawn-registry half of this obligation does not exist (no registry file is written anywhere; `resolve_binding` has no in-repo implementation). The live consumer is **Cato**, not Marcus: `cato/backend/cost_ingest.py:100` requires the path shape `<experiment_dir>/worktrees/<agent_id>` and returns `None` otherwise, silently dropping the event. `project_id` comes from `project_info.json` (written by core, `nlp.py:1267-1297`); `task_id` is derived in-stream by the `_session_task` tracker (`worker_ingester.py:299-304`) and is therefore **already session-safe**; `run_id` is already `"unassigned"` on worker rows. Re-home = keep the worktree path shape stable or change Cato in lockstep | Phase 3, per D9 |
| O3 | The stale-completion guard assumes agents die on recovery; a falsely-recovered *live* session would have its coarse-task output discarded (`task.py:4136-4308`). **Partly pre-solved:** the #667 Fix 2 block (`task.py:4209-4283`) already accepts a late completion when the card is *uncontested*, so only the contested branch (`:4285-4308`) discards. That existing three-signal heuristic will *fight* the epoch check unless removed — a deleted lease reads as "uncontested" | **Closed by D11** (epoch fencing + board-mediated reconciliation) |
| O4 | ~~`max_renewals=10` / `stuck_task_threshold_renewals=5` flag healthy long tasks as stuck~~ — **this premise is false.** Verified 2026-08-09: `stuck_task_threshold_renewals` is **write-only dead config** (ctor param `:219`, docstring `:252`, stored `:291`, **read by nothing**) — it flags no task and has no test. `max_renewals` is **warn-only**: its only readers are `:534` (logs a warning, then renews normally) and `:1511` (a statistic). Neither denies a renewal, escalates, or reassigns. Deleting both is nearly free **and changes nothing about premature recovery** — the mechanism that actually reclaims healthy long-running cards is the progressive-timeout curve (`:1573-1594`, 180/240/300/360s windows + 60-90s grace) plus a *second* renewal-duration curve (`:129-176`) with its own hardcoded `renewal_count > 5` ceiling at `:167`. D3 must retune those, not just delete the two named dials | **Deleted** — but D3's budget ceiling must replace the *progressive-timeout curve*, not the dead dials |
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
