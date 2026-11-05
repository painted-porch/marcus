# ADR 0013: The Coordination Kernel and Its Extensions

**Status:** Partially accepted — **K4 and K11 accepted 2026-11-04; K12 and K13 accepted 2026-11-05**; the other nine remain proposals, each stating a recommendation and its alternative

**Date:** 2026-11-04

**Deciders:** Lawrence Gray (drafted with Claude; consumer requirements gathered from the four live consumers, see Context)

**Tracking:** Phase 4 modularization (label `phase-4-modularization`) · supersedes the old epic [#706](https://github.com/painted-porch/marcus/issues/706) Phase 4 "verification moat" and Phase 5 "seam decomposition"

---

## Context

Marcus is sold and used as "AI builds your software", and it is built that way: one program in which coordination and software-building are interleaved.
That does not match how it is actually used.
Three of its four live consumers are not building software.

The decision of 2026-10-30 was to re-architect Marcus as a thin **coordination kernel**, the shared board plus observability, with functions as snap-on **extensions**.
This ADR draws that boundary.

### The four consumers, and why the number matters

A kernel designed around one consumer is a monolith with a new name.
Every decision below is therefore justified by at least two independent consumers, or by a filed defect, and the witnesses are named.

| Consumer | State | Shape |
|---|---|---|
| **Software-building** | shipping | `session_launcher.py` plus `worker_host.py`: git worktree per session, instruction files, chore loop, decomposer, planner model |
| **AI citation verifier** (On the Record / Tow) | shipping | `tow_runner.py` seeds a `find` → `verify` → `approve` → `synthesize` chain directly, with no decomposer and no planner model |
| **Numerical-report verifier** | POC | modelled on the Tow family; writer is a headless session, verifier calls no model at all |
| **Minecraft coordination** | experiment | Node processes in Docker, no repository, their own pull loop against a protocol document |

Two facts from that table carry most of this ADR.

First, **the citation verifier already runs Marcus with no decomposer and no planner model**, and says so in its own docstring.
The software-building extension is therefore not hypothetical.
Something already works without it.

Second, **two worker shells already exist**: `worker_host.py` for software-building and `tow_worker_common.py` for the citation chain.
The split this ADR formalizes has already happened in practice, unmanaged.

### The evidence that shaped the decisions

Four findings arrived from the consumers during this drafting and are referenced throughout.

- **Check authorship predicts the bug class.** The citation verifier's checks are fixed in harness code; the numerical verifier derives mandatory cross-checks from staged data and injects them. Neither has the bug class that #735, #677 and #647 describe. Software-building, the one consumer that lets the agent author its own verification, has all three.
- **A redone task leaves a stale claim** (#803). The handoff's own stated invariant, "legitimate chain keys never collide by construction", holds only while a task closes once. Reproduced twice, on two branches.
- **Frontier width is only observable when the board is failing** (#804). The measure of whether a plan is parallel is discarded on every assignment.
- **Agents wrote zero opinions in 30 agent-minutes.** Two three-agent runs. They read the aggregate on every pull and never wrote one. The deliberation write path is unexercised, which bears on D11 below.

---

## Decisions

### K1 — What the kernel is

**Recommendation.** The kernel owns the board and nothing that presumes a domain.

In the kernel: the task graph and its gates, claiming and self-selection, leases and fencing, the event log, all mutable state under one root, board-mediated communication, and the measurement of coordination quality.

In extensions: decomposition and planning, verification policy, the worktree and merge machinery, domain duties in task instruction text, and the runner that puts workers on a machine.

**Evidence.** The citation verifier runs today with no decomposer and no planner.
Minecraft runs with no repository, so every git-shaped gate skips, and board-only completion works by a fall-through rather than by contract.

**Alternative.** Keep one program and extract only verification.
Cheaper now, and it leaves the software assumptions in the core where three of four consumers must route around them.

**What this rules out.** Any kernel change justified by one consumer's needs alone.

### K2 — How an extension registers

**Recommendation.** An extension declares, per project, a named set of task types it serves and, for each, the evidence it requires, the verifier the kernel should call, and whether checks may be injected.
Registration is board state, not configuration, so the record of what verified a project is in the project.

**Open.** Whether registration is a first-class MCP tool or part of the project record written at creation.

### K3 — Evidence policy is declared, never inferred from labels

**Recommendation.** A project declares its evidence policy. "No repository" becomes a stated case rather than a fall-through.

**Evidence.** Today an implementation label turns on a source-tree validator that rejects a project with no source files (#798).
A domain whose actual verb is "build a wall" must avoid the word "build" to avoid a gate it can never pass, and omitting the label silently turns validation off entirely.
Vocabulary is doing policy.

**What this rules out.** `build`, `create`, `implement`, `develop` and `feature` as a control channel.

### K4 — Check authorship: mechanism in the kernel, policy per task type

**Accepted 2026-11-04 (Lawrence Gray).**

**Decision.** The kernel guarantees that a verification extension can inject required checks the worker did not author.
Whether injection is required is a per-task-type policy the extension chooses.

For tasks the kernel itself creates, Marcus authors the acceptance criteria and the verification, which is the Multi-Agency Proclamation's Invariant #2 v2.

**Evidence, and its limit.** The one consumer that delegates check authorship to the agent carries #735, #677 and #647; the two whose checks are owned by the harness carry none of that shape.
That is a structural argument with one negative case, not an observed asymmetry across three consumers: the other two cannot have the bug, because their checks are fixed in code and no agent can decline one.
It is weaker than an experiment and stronger than a preference, and it is accepted on that footing.
A numerical writer passed clean, with full declared coverage and no dishonesty, against a case whose bucket table was eight records short of its own summary count, because no check was obliged to look.

**Why policy and not a blanket rule.** The deliberation layer's emergence baseline deliberately records ground truth beside each opinion and never enforces it, in order to observe false consensus.
A blanket injection rule makes that experiment impossible to run.
An opinion is a dated observation, not a claim about a deliverable.

### K5 — A terminal declined outcome

**Recommendation.** The kernel gains a terminal state for "this could not be verified", distinct from done, from failed, and from worker-reported blocked.

**Evidence.** Two consumers need it and neither can express it.
The citation verifier has `unverifiable` with a `decline_reason`, which "counts as declined, never as correct", inside its own evidence vocabulary only.
The numerical verifier sets dependents to BLOCKED, which reads as a worker reporting an obstruction when no worker did.

### K6 — Every board write is stamped from the agent's registration

**Recommendation.** The agent's registration is the only source of the project for any write. Multi-project is a first-class case, not an edge.

**Evidence.** #805: `log_decision` files a decision under the server's active project, so on a multi-project server a decision lands under the wrong project, and on a server with no active project it is dropped with a debug line.
`give_opinion` was fixed in PR #800 and the reasoning is already written in `_project_for_agent`'s docstring.

### K7 — A re-open clears what the previous close handed off

**Recommendation.** The kernel's re-open paths clear the handed-off inputs the re-opened task contributed, or the handoff becomes idempotent per upstream task.

**Evidence.** #803. The existing security rule, that a claim author must never overwrite a dependent's board-seeded inputs, is correct and must stay.
Its stated assumption is what breaks.

**Why the kernel and not the extension.** An extension can work around this only by reaching into the handoff contract to undo it, which is what the numerical verifier currently does.
Both this and K4 are cases where an extension needs a kernel guarantee *modified*, and that is the sharpest test this ADR has for what belongs in the kernel.

### K8 — Runtime neutrality

**Recommendation.** The agent contract assumes nothing about the agent's runtime.

Three consequences: artifacts are delivered as content, or through a content-addressed store, rather than as paths on the server's filesystem; task instruction text carries no domain duties the kernel assembled; and per-task agent cost arrives through an ingestion API rather than being read from one vendor's transcripts.

**Evidence.** `log_artifact` requires `project_root`, an absolute server-side path meaning "where the code lives", so a non-software project invents a directory that is deliberately not a repository.
Every Minecraft task's instructions carry the D8 checkpoint duty telling the agent a checkpoint is a commit on its branch; the agent has no branch, and the prompt must teach it to ignore text the kernel assembled.
Minecraft agents' tokens live in their own transcripts, so cost had to be computed by parsing them, which makes cost-per-task incomparable across consumers.

### K9 — Measurement belongs to the kernel

**Recommendation.** Coordination quality is measured by the kernel and reported on every reply, not reconstructed by each consumer.

Two specifics: frontier width accompanies every `request_next_task` reply, including the ones that hand out work (#804); and a report carrying no lease epoch against a task that has one is reported as unfenced, in the reply and in the event stream.

**Evidence.** Frontier width is the quantity behind the coordination claim, and it is discarded on every assignment, so it is visible only as it collapses.
One consumer had to sample the board file from outside Marcus every 15 seconds and reimplement the kernel's claimability rules to get a time series.
Separately, a restarted agent keeps reporting with no epoch and the fence silently does not apply, so the fence looks effective and is not.
The kernel is right to accept an epoch-less report, because an agent that never had one is legitimate, and wrong to accept it silently.

**Also measured, and already adequate.** Idle-poll ratio and epoch collisions.
The one call that invokes a model is assignment, at 3 to 20 seconds against 10 to 600 milliseconds for every other board call.
That is the coordination tax, and it is the kernel's.

### K10 — Rules for mutating a live graph

**Recommendation.** Any feature that rewires a running board obeys two rules: never rewire a leased task, and add and mark unclaimable rather than delete.

Two capabilities follow: a scorer-component registry with visible weights, and an explicit-task creation path at runtime, since `create_tasks` takes prose and `create_project(options.tasks)` is setup-only.

### K11 — Verification is the first cut

**Accepted 2026-11-04 (Lawrence Gray).**

**Decision.** Pull verification out first, then carve software-building off the remainder.

**Evidence.** On the Record already proved verification detaches.
The verification extension point now has two consumers with convergent requirements, K4 and K5 each having two independent witnesses.
#798 gives the cut a failing acceptance test on day one.

**Alternative.** Carve software-building off first, leaving a minimal board kernel.
The larger and riskier cut, and it would define the extension interface against a hypothesis rather than a consumer that exists.

**The witnesses are different claims.** The numerical-report verifier proves the verification extension point is real.
Minecraft proves the kernel is domain-agnostic.
Each needs its own witness and neither substitutes for the other.

### K12 — Offer-time eligibility: the kernel never offers a verification task to the agent whose own work it would verify

**Accepted 2026-11-05 (Lawrence Gray).**

**Decision.** `request_next_task` excludes, for any task whose type is "verify this artifact," every agent registered as that artifact's producer or as that agent's vendor.
The exclusion happens where the kernel decides what to offer, and the refusal is silent, so the rule cannot be probed by a worker testing where its edge is.

**Evidence.** The citation verifier already builds this (`src/marcus_mcp/coordinator/eligibility.py` → `filter_eligible_tasks`, #737) and names it as one of four board-enforced guarantees: "no one checks their own work."
It is not named anywhere in this ADR, and nothing stops a second verification extension from shipping without it.

**Why this is not an accuracy argument.** The full-200 readout measured Condition B, the full institution with eligibility included, against Condition C, a plain filter with no eligibility rule and no human signature, in a benign lab, and C's raw error rate was not worse: 9.5 percent against B's 12.1, with the readout's own conclusion that "the checker is what catches the errors, and the checker is about 300 lines of ordinary code."
Eligibility is not what lowers that number.
What it buys is everything the readout lists as beyond C's reach: a check that cannot be skipped, run by someone who cannot see the claim under review, under adversarial pressure rather than a benign lab with no knowledge a filter was coming.
This is the same point arXiv 2609.12039 makes generally, cited here as supporting vocabulary rather than evidence: narrowing the gaps needs authority from outside the inner loop, and the agent whose own work is on trial is not outside it.

**Why the kernel and not the extension.** Every verification extension needs this same exclusion, and one that omits it fails silently, the same shape K4 and K7 both guard against by putting the mechanism in the kernel rather than each extension's discretion.

### K13 — `update_task` leaves the caller's in-memory view consistent with what it just wrote

**Accepted 2026-11-05 (Lawrence Gray).**

**Decision.** `update_task` mutates the matching `Task` object in `state.project_tasks`, when one exists, with exactly the fields the caller passed, as part of the same call that writes the board. A caller that reads `state.project_tasks` immediately afterward sees its own write; nothing waits for `refresh_project_state`, which runs on a schedule this call does not control.

**Evidence.** The same defect, found three times in one day, at three unrelated call sites, each independently: #807 (a dependent's handoff merge basis), #816 (`request_task_redo`'s own completion check), #814 (the phase gate a same-reply frontier count depends on). In every case, something wrote the board, and something else in the *same request* read `state.project_tasks` expecting the write to already be visible there. It was not, because nothing besides the next scheduled `refresh_project_state` ever made it visible.

**Why a kernel guarantee and not a sweep.** A sweep finds every call site that exists today. `update_task` call sites are not finished growing — #814 was new code, not old debt, and had the bug on arrival. A sweep is a one-time patch against a problem that regenerates on the next PR that calls `update_task` and forgets to also touch `state.project_tasks`. The guarantee removes the thing to forget.

**Why the mechanism sits at the `state.kanban_client` boundary, not inside the provider classes.** `KanbanInterface` and its four implementations (`sqlite_kanban.py`, `planka_kanban.py`, `github_kanban.py`, `linear_kanban.py`) have no reference to a `MarcusServer` or to `state.project_tasks` today, and several of them are constructed directly by standalone tooling (`dev-tools/experiments/runners/tow_runner.py`'s seed scripts, the live-check scripts this ADR's own evidence was gathered with) with no `MarcusServer` in the process at all. Teaching the provider classes about `state.project_tasks` would couple a board adapter to a specific server's in-memory cache, breaking that reuse. The guarantee is implemented as a thin wrapper applied where `state.kanban_client` is assigned: every provider stays exactly as decoupled as it is today, and every *caller* of `state.kanban_client.update_task(...)`, present or future, gets the guarantee without doing anything to earn it.

**What this does not do.** It does not remove the need for #807's durable `committed_inputs` snapshot. That snapshot exists because Planka, GitHub and Linear never round-trip `inputs` through the board at all, which is a durability gap across requests and providers, not an in-process staleness window; K13 closes the latter, not the former.

---

## Inherited obligations

These are not choices. They are consequences this ADR must not lose.

- **O1 — #803 and #805 precede the numerical POC.** That POC's bounded retry re-closes the same task by design, turning a zero-adoption path into a routine one. Both defects corrupt exactly the record the verification product exists to produce.
- **O2 — the deliberation write path is unexercised.** Counted priority would weight a signal no agent has produced in 30 agent-minutes of real runs. This is not an argument against building it. The ADR must not treat the write path as exercised, and the hold on it is well founded.
- **O3 — one claimability rule, not two.** Any frontier-width reporting must use the kernel's own rule, or the kernel and the consumers will drift.
- **O4 — the seam test still governs.** Cut only where the interface between two chunks can be written as a contract smaller than either chunk.
- **O5 — the graph-quality bugs are re-triaged after the boundary, not before.** #669, #615, #665, #637, #620, #619 and #617 may shrink or vanish at a new granularity, and the boundary decides which side each lands on.

---

## Consequences

**Good.** Each extension becomes independently sellable, and a verification buyer is not a build-my-software buyer.
Three of four consumers stop routing around software assumptions.
The two kernel guarantees that two consumers each needed get decided once rather than worked around twice.

**Costly.** The boundary runs straight through `src/marcus_mcp/tools/task.py`, which is 9,965 lines holding board protocol, lease fencing, footprint landing, reconciliation and worker prompts together.
`src/ai/` is 15,135 lines and almost entirely software-building.
Of 129,065 lines in `src/`, four subsystems are 72 percent of the total.

**Accepted risk.** A kernel with two consumers is better evidenced than one with a single consumer, and still not proof of generality.
K1's test, that no kernel change is justified by one consumer alone, is the guard against that, and it only works if it is applied.

---

## References

- ADR-0012, the session model, whose D1, D3, D8, D10 and D11 are kernel decisions this ADR inherits
- The Multi-Agency Proclamation in `CLAUDE.md`, whose Invariant #2 v2 K4 implements
- #798, the parked validation gate, designated the first concrete seam
- #803, #804, #805, the three defects found while gathering these requirements
- #807, #816, #814, the three independent recurrences of the same stale-in-memory shape K13's evidence rests on
- #735, #677, #647, the software-building verification bugs that K4's evidence rests on
- #299, domain-agnostic coordination, which K1 and K3 retire
- #792, merged as `dc996a6f`, which placed all kernel state under one root
- #737 and `docs/design/on-the-record-as-built.md`, where K12's existing mechanism (`filter_eligible_tasks`) and its "no one checks their own work" guarantee are already shipped
- The full-200 readout (`docs/design/research/tow-full-200-readout-2026-10-16.md`), whose Condition B/C comparison K12's evidence rests on, and corrects a prior reading of
- [arXiv 2609.12039](https://arxiv.org/abs/2609.12039), "Reality Is the Final Verifier," cited by K4 and K12 as supporting vocabulary, not as evidence
