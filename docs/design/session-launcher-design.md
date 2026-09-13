# The Session Launcher — design, explained simply

**Status:** Draft, revision 4. **Decision 0 is now settled: Option A.**
Revision 2 absorbed the four Codex findings from review pass 1.
Revision 3 surfaced the single root under those findings and posed it as a choice — Option A (trustworthy recovery first) or Option B (a throwaway measurement run first).
Revision 4 records the choice: **Option A.** D3 — trustworthy recovery — is a launcher prerequisite, so it is now the *first* build step rather than a deferred one.
Nothing is built yet.

**Belongs to:** Epic #706 Phase 3 (session-model migration), ADR-0012 decisions D1, D3, D6, D7, D8, D11, and obligations O1 and O5.

---

## The one-sentence version

Today Marcus hires a worker for one chore and fires them.
We want workers who clock in once and keep taking chores until the work runs out.

## What changed in this revision, and why it matters

Revision 3 ended on an open question and a lean, not a decision.
That question — *is trustworthy recovery (D3) a prerequisite for the launcher, or can it wait behind a throwaway measurement run?* — is now answered.
The answer is **yes, it is a prerequisite**, which is Option A in section 6b.

The reasoning is the single most important thing in this document:

> **The launcher is not a small step. It is the switch that turns four sleeping bugs on at once — and three of those four are bugs about telling a live worker from a dead one.**

Every one of those four bugs is harmless *today* for the same reason: today a worker does one chore and is killed, so it cannot linger, cannot come back, cannot outlive anything.
The launcher removes the killing.
The moment workers live all day, four things that "cannot happen" start happening:

1. **Two workers finish the same chore.** Marcus reassigns a chore from a worker it *thinks* died — but under the launcher that worker is still alive and finishes anyway. Both hand in the work. (This is the exact bug the deferred D11 "fence" was written for. We deferred it because it *could not happen under the old model*. The launcher is what makes it possible.)
2. **Rejected work sneaks into `main`.** A worker keeps one workspace all day, so one branch holds many chores. The current merge grabs the *whole branch*, so a chore that was sent back can ride into `main` on the worker's *next* accepted chore. (Filed as #730.)
3. **Workers never go home.** A worker knows to stop today only because the runner kills it. Nothing else says "the board is empty." (Obligation in ADR line 69–84 / the D10 change removes the one signal that exists.)
4. **A worker's memory fills up and never empties.** One all-day worker accumulates context across every chore with no reset, until it slows down or runs out. (Obligation O5.)

Three of those four — bugs 1, 3, and the killed-worker cleanup problem in section 6b — are the *same* failure: Marcus cannot reliably tell a live worker from a dead one.
That is precisely what D3 fixes.
So the build order is no longer "ship the launcher, then tidy up," and it is no longer "run something unsafe and measure it."
It is: **make liveness trustworthy first (D3), then build the launcher on top of a signal it can actually rely on.**

---

## What actually happens today

Marcus is a workshop.
The chores live on a board.

Right now, every time a chore needs doing:

1. Marcus starts a brand-new worker.
2. The worker takes exactly one chore.
3. The worker finishes it.
4. **The worker is killed.**
5. If there is more work, Marcus starts *another* brand-new worker.

That start-and-kill machinery is where four of Marcus's six worst bugs live.
Workers pile up. A watchdog kills healthy ones by mistake. Sometimes it starts workers in an endless loop.

## What we want instead

1. You say: *"run five workers."*
2. Five workers clock in, once.
3. Each one loops: **take a chore → do it → report it → take another.**
4. When the board is empty, they clock out.

Marcus does not start them, does not watch them, and does not kill them.
Marcus tracks each worker at exactly one level: **the lease** — which chore they hold, and when it was last touched.

Several bugs disappear here rather than getting fixed, because the machinery that caused them is gone.
But — per the section above — removing the killing also removes the thing that quietly prevented four *other* bugs. That is the real work, and it starts with making the lease a trustworthy liveness signal.

---

## The pieces

### 1. The launcher — a small program *outside* Marcus

It starts N workers and then gets out of the way.
Deliberately dumb: it does not decide who does what, does not restart the dead, does not watch for trouble.

**Good news:** not built from scratch.
`dev-tools/experiments/runners/run_experiment.py` already starts workers today; the launcher is that file with the start-and-kill loop removed.

### 2. The pull loop — the instructions each worker follows

> Register once. Ask for a chore. Do it, report it, ask again.
> No chore? Wait a bit, ask again. Board drained? Stop.

**Good news:** `prompts/Agent_prompt.md` — the sheet real users copy — already teaches the continuous loop.
The sheet that says "do one chore then exit" is the experiment copy, and it is being deleted.
So the prompt work is mostly *deleting a wrong sheet*.

**But — correcting the first draft:** the "board drained? stop" line in the prompt is a *lie the prompt cannot make true on its own.* See piece 4.

**What a "worker" actually is — the load-bearing clarification.**
A worker is **not** one endless AI conversation that runs all day.
A worker is a **persistent identity and workspace-owner** running a small loop.
The loop is the harness: register once, then for each chore start a *fresh context*, do the chore, report, and reset for the next one.
The identity persists across chores; the conversation does not.
This is what lets the launcher stay dumb (it never reaches inside a chore) *and* keeps a worker's memory from growing without bound (each chore is a clean slate).
It is also why "register once" (piece 3 / O1) and "reset per chore" (O5) do not fight: identity is per-worker, context is per-chore.

### 3. The lease — Marcus's handle on a worker

It says: *this worker holds this chore, last heard from at this time.*

**This is where the first draft was wrong.**
The first draft said "no change needed to what a lease is."
That is only true under the old kill-after-one-chore model.
Under the launcher, the lease has to answer a question it never had to answer before: *when I reassign a chore because a worker went quiet, and the "quiet" worker turns out to be alive and finishes it, who wins?*
Today the answer was "the reassignment always wins, because the quiet worker was killed."
Under the launcher, nobody was killed, so **both can win**, which means neither does — the same chore ships twice, and its code merges twice.

Fixing that is the D3 → D11 work (piece 5), and D3 is now the first thing we build.
The lease layer is not "no change." It is the foundation the launcher stands on.

### 4. The drain signal — a *server* change, not a prompt line

When the board is empty, workers must be told to go home.
The first draft put this in the instruction sheet. That cannot work, and here is the exact reason, verified in code:

- The only "you may exit" signal today is `should_exit` in `request_next_task` (`src/marcus_mcp/tools/task.py` ~2896–2942).
- It fires **only** when a live *experiment monitor* was started and has since stopped.
- Without that monitor, the same function explicitly tells workers to **retry forever** (~3003–3032).
- **This migration deletes the experiment monitor** (that is the D10 "a run leaves the core" change).

So after the migration, unless we build a replacement, a drained board leaves every worker polling until you kill them by hand.
The replacement is a **board-computed condition** — *no claimable tasks and no live leases* — returned by `request_next_task`, decided by the server, not by a model remembering to call a tool.
This is its own build item with its own test. It is not a prompt edit.
Note this signal leans directly on "no live leases," which is only meaningful once D3 makes liveness trustworthy — which is why D3 comes first.

### 5. Trustworthy recovery, then the fence (D3 → D11)

This is the lease change piece 3 pointed at, and under Option A it is the **first** build step, not the last.

- **D3 — split "is it alive?" from "how long may it hold the chore?"** Today those are one dial, so detecting a dead worker is tied to how big its chore is. D3 makes "alive" a simple silence timeout and "how long" a separate budget. This is what makes recovery *trustworthy* — reliable enough that we can stop compensating for false alarms.
- **Delete the re-grant compensation.** Today, when a quiet worker's lease is recovered, its *next report recreates the lease* — Marcus's built-in apology for false recovery. That apology and any fence are contradictory: one says "recovery might be wrong, let them back in," the other says "recovery happened, they're out." Both cannot be true. Once D3 makes recovery reliable, we delete the apology. (This contradiction is exactly why the fence failed three review rounds and was deferred; D3-first is what resolves it.)
- **Then D11 — the fence — enforced where it matters.** Not at the coordinator (the mistake that cost three review rounds in August) but at the *resource*: the DONE write and the merge into `main`. Note #730 below is the same fence seen from the merge side. D11 lands in Milestone B, sized to whatever small residual collision rate remains after D3.

**Why D3 has to come first — the honest limit that settles Decision 0.**
An earlier version of this doc imagined the first run could make false recovery "rare by construction" with a generous timeout, and skip D3.
That is false, and it is worth being blunt about why, because it is exactly D3's reason to exist.
Before D3 the lease is **one dial** that means both "is the worker alive?" and "how long may it hold the chore?" at once.
Make that dial long and a genuinely-dead worker's chore sits unclaimed for the whole window — which directly breaks the acceptance-run test that a killed worker's chore is picked up promptly.
Make it short and a worker doing focused work for a few quiet minutes trips recovery *while alive* — the collision.
There is **no single value that avoids both.** That is precisely the knot D3 unties by splitting the one dial into two.
Because there is no safe single-dial setting, there is no safe way to run the launcher before D3 — which is the whole of Decision 0.

### 6. Safe merges before session-scoped workspaces (#730 → D1)

**D1** gives each worker one long-lived workspace for its whole shift, instead of a fresh one per chore.
That is where "small, stable, no churn" comes from — but it is also what lets one branch hold many chores, which is what lets rejected work ride into `main` (bug #2 at the top).

So **#730 — merge only the commit range of *this* chore, not the whole branch — must land before D1**, not after.
The first draft listed #730 as "related work." It is a prerequisite for session-scoped workspaces.
Correcting the first draft's other claim: the launcher does **not** require D1. The first run keeps per-chore workspaces (which dodges #730 entirely); D1 and #730 move together into Milestone B.

### 6b. The one crack under everything — resolved by building D3 first

Three separate problems kept surfacing every time this design was reviewed. They are the same problem wearing three coats, and the root is one sentence:

> **Before D3, the lease is not a trustworthy "is this worker alive?" signal — yet the launcher leans on lease-liveness for three different jobs.**

The three coats, and how **Option A (D3 first)** settles each:

1. **The drain signal leans on it.** Piece 4 defines "done, go home" as *no claimable tasks and no live leases.* Before D3, if a head-down worker on the final chore lets its short lease lapse while still working, the chore flips back to claimable and an idle worker grabs it — a collision — instead of the clean wait-then-drain we described. **With D3:** a live worker checkpoints within the silence timeout, so its lease does not lapse while it works. "No live leases" becomes trustworthy, and the clean drain holds. **Fixed outright.**
2. **Teardown of a *killed* worker's workspace has no owner.** Teardown-on-completion never fires for a worker that died mid-chore. The launcher is deliberately dumb, Marcus tracks only leases, and the dead worker cannot clean up after itself — so its worktree is orphaned, reviving #628 on exactly the kill path the acceptance run exercises. **With D3:** a genuinely-dead worker is *detected* (silence timeout), so the recovery path is a real event that *something* can hang cleanup on. **Fixed** — the recovery path owns the teardown.
3. **Recovery salvage fights teardown.** The next worker takes the chore in its *own* fresh workspace (it must, to keep the #730 dodge), so it cannot see the dead worker's partial work or its checkpoint commits. **With D3:** recovery is reliable, so saving a dead worker's partial work becomes a sensible thing to build — but the *salvage* itself still needs a stable workspace the next worker can inherit, which is D1. So D3 makes coat 3 **tractable**, and the checkpoint duty (D8) lands with D1 in Milestone B, not in the first run. The first run discards a dead worker's partial work; because recovery is now rare (D3), this is a small, bounded loss, not a routine one.

**The decision, recorded:**

- **Option A — chosen.** D3 is a launcher prerequisite. This is what Codex's review pass 1 said in its first finding, and three rounds of trying to design around it did not hold. A real silence-timeout (D3's two dials) makes liveness trustworthy, which fixes coats 1 and 2 outright and makes coat 3 tractable. Cost: the first build step is D3, not the launcher — the launcher is no longer the near-term deliverable.
- **Option B — rejected.** Option B kept D3 in Milestone B and ran a deliberately-unsafe measurement run first, accepting three stated breakages (premature reclaim of the final chore, orphaned worktrees on the kill path, and discarded partial work). The recurring failure of every "defer D3" draft *was itself the finding*: the launcher's core jobs *are* liveness jobs, so deferring the liveness fix produced a fresh contradiction each round. The one real argument for B — that we cannot size D11's fence without a measured collision rate — only defends the *collision* coat, not the orphaned-worktree or discarded-salvage coats, which no amount of measurement informs. That asymmetry is why B loses. Under A, we still get the collision rate: the first run records it (piece 9 of the build order), just on top of a trustworthy liveness signal rather than a broken one.

### 7. The two carry-over pieces from the first draft (still true)

- **Non-Claude workers need their instruction files.** The machinery being deleted is the only code that writes `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`. Delete it carelessly and Codex/Gemini workers start blank and silently do nothing. The launcher keeps this one job.
- **The workspace path shape is a cross-repo contract.** Cost tracking (in the separate Cato repo) identifies workers by reading the `worktrees/<worker-id>` path. Rename it and cost tracking stops silently, in both repos. Keep the shape, or change Cato in lockstep.
- **Everyone wakes at once.** Idle workers are all told "wait 30 seconds" — the same 30. Five started together poll in lockstep forever. Add jitter — but only once a real loop exists to test it against.

---

## The build order — two milestones

Decision 0 (Option A) fixes the order: trustworthy recovery first, then the launcher on top of it, then full production hardening.

### Milestone A — the first session run, on a trustworthy liveness signal

The goal of Milestone A is **one real run of the session model**, with recovery that Marcus can actually rely on.
D3 leads, because every later piece leans on the liveness signal it provides.

- [ ] **D3 — trustworthy recovery.** Split the single lease dial into a silence timeout ("is it alive?", default 45 min, ≥2× the checkpoint cadence) and a budget ceiling ("how long may it hold the chore?", ~3× estimate). Then **delete the re-grant compensation**, which only existed to paper over false recovery. *First — everything below depends on liveness being trustworthy.*
- [ ] **O1 — survive a restart.** Persist worker registration and rehydrate it on the *lazy* path (the first `request_next_task` after restart), because the startup path never runs in the mode Marcus deploys in. *Its own PR.*
- [ ] **The drain signal (piece 4).** Board-computed "no claimable tasks and no live leases," returned by `request_next_task`. Server change, own test. Reliable now that D3 makes "no live leases" trustworthy. *Required even for a first run — without it the run never ends.*
- [ ] **The launcher.** `run_experiment.py` minus the start-and-kill loop, plus `--sessions N`. Keeps writing the instruction files. Inverts the current "exit after one turn" behaviour so the loop stays alive.
- [ ] **Per-chore context reset (O5, minimum viable).** The reset happens in the harness loop (piece 2) at the chore boundary it already has: each chore runs in a fresh context, the worker identity persists. Minimum viable = a clean context per chore; smarter compaction *within* a long chore is Milestone B. The boundary is per-chore, not per-turn, so it does not reintroduce the supervision we deleted.
- [ ] **Per-chore workspace: create AND tear down.** Explicitly create a fresh workspace + branch for each chore (reusing `run_experiment.py`'s existing worktree-creation) **and add teardown when the chore completes** — and, now that D3 detects a dead worker (6b coat 2), wire the recovery path to tear down the *killed* worker's workspace too. Without teardown, long-lived workers reintroduce the #628 worktree explosion. Per-chore workspaces (not per-session) are also what let the first run dodge #730/D1; that dodge silently fails if a worker reuses one workspace across chores, so this item is load-bearing.
- [ ] **One instruction sheet.** Delete the one-chore copy, keep the loop copy, extend the drift-guard test to pin the loop contract. *(The checkpoint duty from D8 is deliberately NOT added here — see 6b coat 3: salvage needs the stable workspace D1 provides, so D8 lands with D1 in Milestone B.)*
- [ ] **Jitter** on the retry interval (now there is a loop to test it against).
- [ ] **Record-only collision counting.** Rely on the already-shipped record-only observation (`_observe_epoch_collision`, PR #731) to *count* any residual two-workers collision — it logs a stale completion but does not block it. With D3 in place a collision is now rare (liveness is trustworthy), but not yet impossible; the count that remains is what sizes D11's fence in Milestone B.

### The acceptance run (the end of Milestone A)

Start Marcus and three workers on a small project.

- All three take chores and finish them, without Marcus starting anything.
- Kill one worker mid-chore. Its silence timeout elapses and another worker picks the chore up promptly. Its workspace is torn down by the recovery path.
- **Restart Marcus while all three are working.** They keep going. *(Fails today — this is O1.)*
- The board empties. All three stop on their own, cleanly: they wait for the last live lease to finish, then exit. *(Requires the drain signal, piece 4 — not the prompt. Holds because D3 makes liveness trustworthy.)*
- Cost tracking still attributes spend to the right worker. *(Requires the workspace path shape, piece 7.)*
- A completed worker's workspace is torn down; a *killed* worker's workspace is torn down by the recovery path. No orphaned worktrees.
- Any residual two-workers collision is **logged** (not yet prevented — the fence is Milestone B). With D3 in, this should be rare; whatever count remains sizes D11.

Under Option A the acceptance run is fully specifiable — no "conditional on the open decision" caveats remain, because the decision is made.

### Milestone B — production-safe sessions

Only after Milestone A has run and we have the residual numbers:

- [ ] **#730 — commit-range merge**, then **D1 — session-scoped workspaces**, in that order.
- [ ] **D8 — checkpoint salvage**, now that D1 gives a recovering worker a stable workspace to inherit the dead worker's checkpoint commits from.
- [ ] **D11 — the fence**, enforced at the DONE write and the merge, sized to the residual collision rate Milestone A measured. Closes the rare-but-nonzero collision to zero.
- [ ] **O5 — full context compaction** if Milestone A showed it is needed.

---

## Decisions still open

Decision 0 is settled (Option A). Two small choices remain; neither blocks starting D3.

**1. When the board is empty, do workers wait or go home?**
Lean **go home** — smaller, and "wait forever" is hard to tell from "stuck."
(Either way, the *signal* is a server change per piece 4; this is only about what the worker does on receiving it.)

**2. If a worker dies, does the launcher start a replacement?**
Lean **no** — a dumb launcher is the whole point; restart logic is what caused the deleted bugs. You notice and restart.

---

## One thing I want to say plainly

The last large piece of this migration took three review rounds to discover its design contradicted itself, because nobody wrote the contract down first.
This document is that contract — and it did its job twice: review pass 1 caught that the "clean first step" framing hid the fact that the launcher arms the very bug we deferred, and the self-review rounds that followed kept cracking on the *same* root until it was named, which is what forced Decision 0.
That decision — D3 first — is this revision.
If anything here is still vague, that vagueness is the risk. Say so, and I make it specific before any code.

---

## Related

- Epic [#706](https://github.com/painted-porch/marcus/issues/706) — Phase 3 plan and checklist
- [ADR-0012](../architecture/adr/0012-session-model-migration.md) — D1 (workspaces), D3 (liveness), D6 (fixed worker count), D7 (one project per worker), D8 (checkpoints), D11 (the fence, deferred to Milestone B), obligations O1 (restart) and O5 (context)
- Issue [#730](https://github.com/painted-porch/marcus/issues/730) — commit-range merge; **prerequisite for D1**, and the merge-side view of the D11 fence
- PR #731 — lease-ownership hardening; ships the record-only collision observation the first run relies on; the D11 amendment explains why the fence was deferred
- PR #732 — `retry_after`, where the jitter question is recorded
