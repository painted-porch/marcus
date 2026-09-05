# The Session Launcher — design, explained simply

**Status:** Draft for review.
Nothing is built yet.

**Belongs to:** Epic #706 Phase 3 (session-model migration), ADR-0012 decisions D1, D3, D6, D7, D8.

---

## The one-sentence version

Today Marcus hires a worker for one chore and fires them.
We want workers who clock in once and keep taking chores until the work runs out.

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
Marcus only knows one thing about each worker: **which chore they are holding right now.**
That record is called a **lease**.

Several bugs disappear here rather than getting fixed, because the machinery that caused them is gone.

---

## The pieces

There are only three.

### 1. The launcher — a small program *outside* Marcus

It starts N workers and then gets out of the way.
It is deliberately dumb: it does not decide who does what, does not restart anything, does not watch for trouble.

**Good news:** we are not building this from scratch.
`dev-tools/experiments/runners/run_experiment.py` is already the thing that starts workers today.
The launcher is that file with the start-and-kill loop removed.

### 2. The pull loop — the instructions each worker follows

> Register once.
> Ask for a chore.
> If you get one, do it and report it, then ask again.
> If there is no chore, wait a bit and ask again.
> When the board is empty, stop.

**More good news:** `prompts/Agent_prompt.md` — the instruction sheet real users copy — **already says this.**
It has taught the continuous loop since before any of this started.
The sheet that says "do one chore then exit" is the *experiment* copy, and that one is being deleted.

So this is mostly deleting a wrong instruction sheet, not writing a new one.

### 3. The lease — Marcus's only handle on a worker

Marcus already has this.
It says: *this worker holds this chore, and I last heard from them at this time.*

No change needed to what a lease **is**.
What changes is that it becomes the *only* thing Marcus tracks.

---

## What could go wrong, and what we do about it

### A worker outlives a Marcus restart

**This one is a hard blocker and it is not built yet.**

Today Marcus forgets every worker's registration when it restarts.
That was fine when workers died after one chore — there was nothing to forget.

With workers that stay all day, restarting Marcus means five live workers it no longer recognises.
They keep asking for chores and keep getting an error, forever.
The error does not even look like an error: it reads as an ordinary "no work right now."

This is obligation **O1** in the ADR.
**It must be fixed before or alongside the launcher, not after.**

There is a wrinkle: the fix cannot go where you would expect.
Marcus does not load this state at startup — it loads it lazily, the first time a worker asks for a chore.
Putting the fix in startup means it never runs at all in the mode Marcus actually uses.

### All five workers wake up at the same instant

When there is no work, a worker is told "wait 30 seconds."
Every worker gets the same number.
Start five together and they wake in lockstep, five times a minute, forever, all in the same second.

The fix is jitter — each worker waits 30 seconds *give or take a bit*.
This is deliberately **not** done yet, because it should be built and tested against a real pull loop rather than guessed at in advance.
It belongs here.

### Workers never stop

Right now a worker knows to exit because the runner kills it.
Nobody is going to kill it any more.

So the board needs to be able to say **"there is nothing left, you can go home"** — clearly, not by the worker guessing from a status page.

Related and awkward: today a *run* ends because an AI worker decides to call a tool that ends it.
That is a strange amount of trust.
It needs replacing with something the launcher decides.

### Non-Claude workers get no instructions at all

The machinery being deleted is also the only thing that writes the instruction sheet into `CLAUDE.md`, `AGENTS.md`, and `GEMINI.md`.

Delete it carelessly and a codex or gemini worker starts with **no instructions**, never registers, and silently does nothing.
The alternative installer only writes the Claude file.

So the launcher has to keep doing this one job the old machinery did.

### The workers' folders must keep their current shape

Each worker gets its own folder to work in.
Cost tracking — which lives in a *different repository*, Cato — figures out which worker spent what by **reading that folder's path**.

Move or rename the folder and cost tracking silently stops.
No error, in either repo.

The path shape is `.../worktrees/<worker-id>`.
**Keep it**, or change Cato at the same moment.

---

## Decisions I need from you

**1. When the board is empty, do workers wait or go home?**

*Go home* is simpler and matches today's behaviour.
*Wait* is what you eventually want for a Marcus that runs continuously.
I lean **go home** for now — it is smaller, and "wait forever" is hard to tell apart from "stuck."

**2. If a worker dies, does the launcher start a replacement?**

*No* keeps the launcher genuinely dumb, and dumb is the whole point — the old machinery's restarting logic is what caused the bugs we are deleting.
*Yes* is more robust but re-grows the thing we are removing.
I lean **no**, and let you notice and restart it.

**3. Does the O1 restart fix ship before the launcher, or with it?**

I lean **before**, as its own change.
It is a real bug today, it is independently testable, and shipping the launcher on top of a known orphaning bug means debugging two new things at once.

---

## What I would build, in order

1. **The restart fix (O1)** — its own change, its own PR.
2. **The launcher** — take `run_experiment.py`, remove the start-and-kill loop, add `--sessions N`.
3. **The instruction sheets** — delete the "one chore then exit" copy, keep the good one, add the stop signal and the checkpoint duty.
4. **Jitter** — now that there is a real loop to test it against.
5. **Run it** — three workers, one small project, watch what actually happens.

Step 5 is the point.
Everything before it is preparation.

---

## How we will know it worked

Start Marcus and three workers on a small project.

- All three take chores and finish them, without Marcus starting anything.
- Kill one worker mid-chore. Its lease expires and another worker picks the chore up.
- **Restart Marcus while all three are working.** They keep going. *(This is the one that fails today.)*
- The board empties. All three stop on their own.
- Cost tracking still attributes spend to the right worker.

The third one is the real test.
It is the failure that only appears with workers that stay.

---

## One thing I want to say plainly

The last large piece of this migration took three review rounds to discover that its design contradicted itself.
The cause was not bad code — it was that nobody had written the contract down, so each round fixed a different guess at what it meant.

This document exists so that does not happen twice.
If something here is vague, that vagueness is the risk.
Say so now and I will make it specific before writing any code.

---

## Related

- Epic [#706](https://github.com/lwgray/marcus/issues/706) — Phase 3 plan and checklist
- [ADR-0012](../architecture/adr/0012-session-model-migration.md) — D1 (worktrees), D3 (liveness), D6 (fixed worker count), D7 (one project per worker), D8 (checkpoints), and obligations O1–O5
- Issue [#730](https://github.com/lwgray/marcus/issues/730) — merge by commit range; blocks D1
- PR #731 — lease-ownership hardening, and the D11 amendment explaining why the fencing work was deferred
- PR #732 — `retry_after`, where the jitter question is recorded
