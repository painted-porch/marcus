# ADR 0013: The Coordination Kernel and Its Extensions

**Status:** Partially accepted — **K4 and K11 accepted 2026-11-04; K2, K5, K12 and K13 accepted 2026-11-05; K1, K3, K6, K7 and K10 (narrowly) accepted 2026-11-06**; the other two remain proposals, each stating a recommendation and its alternative

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

**Accepted 2026-11-06 (Lawrence Gray: "Accept K1", following K3, K6 and K7).**

**Decision.** The kernel owns the board and nothing that presumes a domain.

In the kernel: the task graph and its gates, claiming and self-selection, leases and fencing, the event log, all mutable state under one root, board-mediated communication, and the measurement of coordination quality.

In extensions: decomposition and planning, verification policy, the worktree and merge machinery, domain duties in task instruction text, and the runner that puts workers on a machine.

**Evidence.** The citation verifier runs today with no decomposer and no planner.
Minecraft runs with no repository, so every git-shaped gate skips, and board-only completion works by a fall-through rather than by contract.

**Alternative, considered and not taken.** Keep one program and extract only verification.
Cheaper now, and it leaves the software assumptions in the core where three of four consumers must route around them.

**What this rules out.** Any kernel change justified by one consumer's needs alone.

**Why this is accepted now rather than argued from first principles: it is already drawn, nine times over, by decisions that shipped for other reasons.** K1 states the boundary as policy; it did not have to invent it, because every decision accepted before it already put something on one side or the other without citing K1, since K1 did not yet exist. One line each, kernel side first:

- **K2** (how an extension registers). Kernel: the registration record itself (`ProjectConfig.task_type_registrations`), board state with project lifetime, and the pull-based offer that makes it reachable. Extension: the policy declared in that record, and the worker process that pulls work and computes its own verdict — the kernel never calls into it.
- **K3** (evidence policy is declared, never inferred from labels). Narrower than it first reads: `should_validate_task`'s label vocabulary still decides whether a task enters the validation gate at all, unchanged by this decision (K3's own "What this does not change" paragraph above). Once inside, `validate_implementation_task` gathers evidence and checks `has_source_files()` first; the declaration is consulted only afterward, to skip the resulting no-source-files failure for a registered task type. Kernel: that one skip, keyed on the same K2 registration field. Extension: the declaration itself (`requires_source_tree`) — not a second control channel that opts a task out of validation, only a stated exception to one specific failure inside the gate it was already routed into.
- **K4** (check authorship). Decided for the kernel: a mechanism that lets a verification extension inject a check the worker did not author, with the required-or-not choice left to the extension as per-type policy. Checking the code while writing this section found that the mechanism itself is not yet built — `ProjectConfig.task_type_registrations` can carry a `checks_injectable` entry today (round-tripped by its own tests), but nothing in the kernel reads it or injects anything. K4's boundary call stands; its kernel-side implementation does not exist yet. Listed as a follow-up below, not fixed here.
- **K5** (a terminal declined outcome). Kernel: `DECLINED` as a status with its own claimability semantics, extending the same settled/unsettled gate `DONE` and `BLOCKED` already use. Extension: what evidence content makes a claim actually unverifiable — a vocabulary question the kernel's gate never reads.
- **K6** (write attribution). Kernel: the single rule, and now the single shared lookup (`project_for_agent`), that every write is stamped from the writing agent's registration. Extension: nothing — this is kernel-internal plumbing with no extension-visible surface at all, which is itself evidence the rule belongs there.
- **K7** (handoff idempotency on redo). Kernel: the re-open path's guarantee that a dependent only ever sees the re-opened task's latest contribution, backed by a durable provenance record. Extension: the evidence shape that gets handed off, not the mechanism that makes a second handoff safe.
- **K11** (verification is the first cut). Not a boundary line by itself — a sequencing decision about which extension to carve off first — but its own justification is K1's claim stated operationally: the citation verifier already proved an extension detaches cleanly, before this ADR said in words what "detaches cleanly" has to mean.
- **K12** (offer-time eligibility). Kernel: the exclusion mechanism itself, enforced where `request_next_task` decides what to offer, silent so it cannot be probed. Extension: nothing new — the mechanism runs over data (producer, vendor) the extension already supplied under K2; a second verification extension gets the guarantee without writing it.
- **K13** (write-consistency). Kernel: the guarantee lives at the `state.kanban_client` boundary, one wrapper applied where the client is assigned, so every caller gets it without earning it. Extension/provider: untouched — `KanbanInterface` and its four implementations stay exactly as decoupled from `MarcusServer` as they were, which is what let standalone tooling (`tow_runner.py`'s seed scripts) keep constructing a provider directly with no server in the process.

Read together, every one of the nine put the *mechanism* in the kernel and the *policy it governs* in the extension — never the reverse, and never a case where an extension needed to reach into kernel internals to get a guarantee it could not otherwise have (K4 and K7 are the two sharpest instances of that specific test, already named where each was decided).
K1 is therefore not a new argument.
It is the line those nine already drew, written down once so K8, K9 and K10 — the three still-open proposals, each itself a kernel-scope question — are decided against a stated boundary instead of three separate implicit ones.

**No code change.** This decision is a description of a boundary the shipped code already observes, not a new guarantee to build.
If writing this section surfaces something that crosses the boundary it describes, that is a found gap for a follow-up issue, not a defect in this decision, and is listed under Related below rather than fixed in this PR.

**SQLite-only support policy (Lawrence Gray, 2026-11-06).** `KanbanInterface` names four providers, but only SQLite is supported going forward.
Planka, Linear and GitHub are best-effort.
A Codex review finding that touches only adapter-specific code for one of the three best-effort providers is filed as an issue, tagged with the `adapter` label, and its review thread resolved with a pointer to that issue rather than blocking the PR.
A kernel bug that merely surfaces through a best-effort adapter's example still gets fixed normally — this policy narrows which *provider-specific* findings block a merge, not which kernel findings do.

### K2 — How an extension registers

**Accepted 2026-11-05 (Lawrence Gray).**

**Decision.** An extension declares, per project, a named set of task types it serves and, for each, the evidence it requires and whether checks may be injected.
Dispatch is pull, not call: the kernel never invokes an extension's code directly; it gates a task of the declared type and offers it on the board, and the extension's own agent, scoped to its project by the registration, claims it through `request_next_task` like any other agent and reports its verdict as evidence.
Registration is board state, not configuration, so the record of what verified a project is in the project.

**The open question, resolved: the project record, not a new tool.** Both extensions that exist today already write the project record directly — `tow_runner.py` and the numerical-report verifier's `numeric_runner.py` each construct a `ProjectConfig` and call `registry.add_project()` from Python, in-process. Neither goes through `create_project`, the existing MCP tool, because that tool's provider whitelist (`src/marcus_mcp/tools/project_management.py`) does not even accept `sqlite`, the provider both of them use. A new MCP tool for registration would be a *third* surface for something two extensions already do twice, the same way, without one.

K2's own rationale argues the same way from a different direction: "the record of what verified a project is in the project" is a durability claim, and the project record is the only thing in the kernel with project lifetime. A tool call is an event; a registration that must outlive every process that reads it needs a record to land in regardless of what writes it. `register_agent` looked like a precedent for a first-class tool, but its lifetime is the opposite of what K2 needs: per-process, ephemeral, re-asserted on every worker start, where K2's registration must survive the process that wrote it.

**What this is not, yet.** The project record cannot take this as-is. `ProjectConfig.from_dict` filters every incoming dict to a fixed `expected_fields` set (`src/core/project_registry.py`) and silently drops anything outside it — so a registration block in `options` would vanish on the next read unless that set names it explicitly. `tags` is already overloaded for provenance (`["auto-created", "sqlite", "tow-experiment"]`) and `provider_config` is provider-specific by name; stuffing policy into either repeats K3's own mistake, vocabulary doing policy, one level up. The fix is a new, explicit field on `ProjectConfig`, not a reuse of an existing one.

**The sharpest evidence for doing this at all, direct from the numerical-report verifier owner.** Their POC (`~/dev/marcus-worktrees/numeric-verifier`, unpushed, not on `origin/develop`) has `task.py` import `apply_verification_outcome` by name and call it unconditionally in the evidence-close path, for every task matching the kernel's own hardcoded `EVIDENCE_REQUIRED_TASK_TYPES`. Their own words: "our verifier owns no task type; it piggybacks on the system-of-record's four." No `project_id` scoping — two such extensions on one server would share one import slot, and dispatch becomes "whichever function got imported runs globally for both projects," silently. Their verdict: without a registration mechanism, a hardcoded kernel-side import was the *only* way to hook in at all — not a shortcut they chose over a cleaner path, but the only path that existed. This is the identical shape O1 already names for #803 and #805: a defect that corrupts the record the verification product exists to produce, except here it is not a bug landed in the kernel, it is what building against the kernel without K2 required.

**The sub-question the owner raised, and why it resolves without a callable field.** Asked what the hook point actually needs to be, they framed it as "the kernel calls MY function instead of its own, for the task types I handle, scoped to my project" — and flagged, correctly, that no data-only declaration captures *which function runs*; either the registration carries a callable or endpoint reference, or K2 scopes itself to schema and ownership and leaves dispatch to a separate decision.

It resolves without a callable field, because "the kernel calls into extension code" is not what K4 or the shipped citation-verifier chain actually do, and building it that way would reopen K8 (runtime neutrality) and Invariant #1 (agents self-select; the kernel never pushes work into a process). The citation verifier's `find → verify → approve → synthesize` chain already answers "which function runs" today, for the one extension that ships: by registering ownership of a task type, not a function. The kernel gates a task of that type and offers it on the board; the extension's own agent, scoped to its project by the registration, pulls it through the ordinary `request_next_task` loop, computes its own verdict, and reports it as evidence — which the handoff (#807, now kernel-guaranteed) carries to whatever depends on it. Nothing calls into the extension's process; the extension calls into the kernel, the same as every other agent. K2's registration therefore needs exactly what its own Recommendation already says — task types, evidence schema, whether checks are injectable — and no callable or endpoint reference. The numeric verifier's `apply_verification_outcome` import stops being necessary the moment it can register `verify` as a task type it owns, scoped to its own project, instead of piggybacking on the citation chain's four.

**One-time at creation is sufficient.** The owner confirmed directly: claim schema, tolerance rules and the retry bound are fixed before their writer runs; per-case variation is task data, not registration, and nothing in their workstream needs a mid-project policy change.

**The ownership-plus-pull model, confirmed live for two of its three steps.** The owner live-checked reopen and bound against a real server: a verifier agent holding its claimed `verify` task calls the kernel's own `request_task_redo` on the `find` task, and a fourth attempt is correctly refused `max_redo_count_exceeded` — zero extension-to-kernel calls, exactly the pull model this section argues for. The third step, terminal decline, is not covered by anything that exists today; that gap is K5's evidence, not K2's, and K5 is sharpened accordingly below.

**A third confirmation, from the same live check.** An agent registered with role `"approver"` was offered a `verify`-type task anyway — role strings are not enforced anywhere today, and nothing scopes which task types an agent, or an extension, actually owns. That is precisely the gap K2's registration field closes.

**Remaining implementation question, not blocking acceptance.** Whether extensions keep constructing `ProjectConfig` in-process as both do today, or get an MCP write path onto the same field, is a detail of how the record gets written, not of where the record lives. Both are compatible with this decision; neither needs deciding before K2 itself is accepted.

**Answered with evidence 2026-11-07, by packaging the citation verifier (K11's first cut).** Today an extension must construct in-process, because the MCP seed path carries no `task_type`, no `inputs` and no caller-supplied id, and nothing writes `task_metadata` rows; registration alone is covered by `add_project`. The written contract, including the two options for the software-building cut, is `docs/architecture/extension-contract.md`.

### K3 — Evidence policy is declared, never inferred from labels

**Accepted 2026-11-06 (Lawrence Gray, in the chief-of-staff channel: "Go with the recommended order of the next kernel decisions" — K3 first, since it closes #798).**

**Decision.** A project declares its evidence policy. "No repository" becomes a stated case rather than a fall-through.

**Evidence.** Today an implementation label turns on a source-tree validator that rejects a project with no source files (#798).
A domain whose actual verb is "build a wall" must avoid the word "build" to avoid a gate it can never pass, and omitting the label silently turns validation off entirely.
Vocabulary is doing policy.

**What this rules out.** `build`, `create`, `implement`, `develop` and `feature` as a control channel.

**The declaration, concretely.** K2's own registration field already carries a project's per-task-type policy (`ProjectConfig.task_type_registrations`), so K3 extends it rather than inventing a second mechanism: `task_type_registrations[task_type]["requires_source_tree"] = False` declares that a task type's completion is not code-producing and has no source tree by design.
`WorkAnalyzer.validate_implementation_task` (`src/ai/validation/work_analyzer.py`) checks this before its existing "no source files found" immediate-fail branch; when declared, it passes without attempting the runtime-test/LLM-source-review pipeline below, which assumes files to review.
Absent — the default for every existing registration and every unregistered project — the gate behaves exactly as it did before K3.

**What this does not change.** `should_validate_task`'s label vocabulary (`src/ai/validation/task_filter.py`) still decides *whether* a task is routed into the validation gate at all; K3 only changes what the gate does once a task reaches it.
A board-only project's implementation-shaped task still needs a label `should_validate_task` recognizes to be validated at all — K3 is the fix for what counts as evidence once that happens, not a second control channel replacing the first.
Narrowing that label vocabulary too is a separate, smaller decision this ADR does not need to make to close #798.

**Confirmed live.** `WorkAnalyzer.gather_evidence` run for real (not mocked) against a temp directory with no git repository and no source files — #798's own reproduction shape — with a real `ProjectRegistry` carrying the declaration: the gate passed, and the LLM validator (patched to raise if reached) was never called.
Full record: `~/marcus-experiments/798-k3-live-check/live_check.py`.

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

**Accepted 2026-11-05 (Lawrence Gray).**

**Decision.** The kernel gains a terminal state for "this could not be verified", distinct from done, from failed, and from worker-reported blocked.

**Evidence.** Two consumers need it and neither can express it.
The citation verifier has `unverifiable` with a `decline_reason`, which "counts as declined, never as correct", inside its own evidence vocabulary only.
The numerical verifier sets dependents to BLOCKED, which reads as a worker reporting an obstruction when no worker did.

**Confirmed live, and decisive.** The numerical-report verifier owner live-checked the reopen-and-decline path against a real server. Reopen and bound are already covered by existing kernel tools with zero extension-to-kernel calls: a verifier agent holding its claimed `verify` task calls `request_task_redo` on the `find` task, and a fourth attempt is correctly refused `max_redo_count_exceeded`. Decline is not. The only thing an agent can call to say "I cannot verify this" is `report_blocker`, and `report_blocker` does not mark the task terminally anything — it returns `requeued_for_repair: true` and sends the task back for up to `MAX_BLOCKER_REPAIR_ATTEMPTS` (2) more agents on a budget (`MAX_BLOCKER_REPAIR_REQUEUES`, 3) that is entirely separate from `request_task_redo`'s own `MAX_TASK_REDO_COUNT` (3). An agent that correctly concludes "this cannot be verified" has no way to say so; the kernel silently treats its decline as "try again," on a second counter the first one's own bound never sees, defeating it.

**The implementation choice, with a recommendation.** Two ways to make a decline stick, and `task_claimability.py`'s own code decides between them:

- **(a) A new terminal status** (e.g. `DECLINED`), reachable by the task's current holder, that `deps_allow_claim` treats as settled-but-not-done: an ordinary (strict-gate) dependent still requires every upstream `DONE` and so correctly stays unclaimable forever, exactly as "distinct from done" demands, while an integration task's best-effort gate (`SETTLED_FOR_BEST_EFFORT`, which already treats `BLOCKED` this way) treats it as settled, the same way a `BLOCKED` upstream already degrades an integration claim today.
- **(b) Gate on evidence content** instead of status: a completed `verify` task whose evidence carries a declined verdict gates its dependents the same way a status would. Verified directly against the live code: `deps_allow_claim` reads only `task.status` today, never evidence content, so this is new kernel capability, not a missing case in an existing one.

**Recommend (a).** Reading evidence content to decide claimability puts the kernel's gate in the business of understanding an extension's evidence vocabulary — the identical mistake K3 names for labels, one layer down: vocabulary, this time an evidence shape instead of a string, doing policy. A status is already the kernel's own vocabulary, already has a settled/unsettled gate built for it, and already treats one terminal non-done status (`BLOCKED`) as settled for best-effort claims. `DECLINED` extends a mechanism that exists; evidence-content gating invents one.

**The same live check also confirms K2's own evidence a third way.** An agent registered with role `"approver"` was offered the requeued `verify` task — role strings are not enforced anywhere, and there is no task-type ownership scoping at all today. That is exactly the gap K2's registration field closes.

### K6 — Every board write is stamped from the agent's registration

**Accepted 2026-11-06 (Lawrence Gray: "Accept K6 and K7 together", following K3).**

**Decision.** The agent's registration is the only source of the project for any write. Multi-project is a first-class case, not an edge. The server's active project — whichever project a human last switched to — is never a fallback: it is `None` before any project is selected and need not be the agent's on a multi-project server, so sourcing a write from it can file the write under the wrong project, or drop it silently.

**Evidence.** #805: `log_decision` filed a decision under the server's active project, so on a multi-project server a decision landed under the wrong project, and on a server with no active project it was dropped with a debug line.
`give_opinion` was fixed for the same defect in PR #800, and `log_decision` in PR #813.
Both built the identical lookup (`state.agent_project_map`) independently.

**What accepting K6 found, and fixed.** `get_project_board_context` (`src/marcus_mcp/tools/task.py`), which tags every conversation-log line an agent's request produces — `request_next_task`, `report_task_progress`, `report_blocker`, `unassign_task` — carried the same defect: it read the server's active project, not the calling agent's own registration, at all four call sites.
Fixed in the same PR that accepts this decision: the lookup PR #800 and PR #813 each built privately is factored out once, as `project_for_agent` (`src/marcus_mcp/coordinator/agent_project.py`), and all four call sites now pass their own `agent_id` through it.
`log_decision` and `give_opinion` now import the shared helper instead of each keeping (or, in `log_decision`'s case, having kept) a private copy.

### K7 — A re-open clears what the previous close handed off

**Accepted 2026-11-06 (Lawrence Gray: "Accept K6 and K7 together", following K3).**

**Decision.** The kernel's re-open paths clear the handed-off inputs the re-opened task contributed, or the handoff becomes idempotent per upstream task.

**Evidence.** #803. The existing security rule, that a claim author must never overwrite a dependent's board-seeded inputs, is correct and must stay.
Its stated assumption is what breaks.

**Why the kernel and not the extension.** An extension can work around this only by reaching into the handoff contract to undo it, which is what the numerical verifier currently does.
Both this and K4 are cases where an extension needs a kernel guarantee *modified*, and that is the sharpest test this ADR has for what belongs in the kernel.

**Already shipped, no code change needed.** `_apply_handoff_to_dependent` (`src/marcus_mcp/tools/task.py`), redesigned in PR #807 and its Codex review rounds, already implements the idempotent-per-upstream-task shape the recommendation above describes: a closing task clears only the keys it itself contributed on a prior close, board-seeded keys and another upstream task's contribution are untouched, and the replacement is backed by a durable provenance record (`record_input_provenance`/`get_input_provenance`) rather than only the in-memory task.
K13 (accepted 2026-11-05) supports the same correctness property from the other side: a caller reading `state.project_tasks` immediately after a write sees that write, which is what keeps the handoff's critical section (one lock per dependent, `_handoff_lock`) from racing against a stale in-memory read.
Confirmed by reading the current implementation and its three Codex review rounds (narrow provenance only after the board write lands; roll back the in-memory copy when the board write fails; serialize handoffs into one dependent) against #803's own verification steps; no new test was needed because `tests/unit/marcus_mcp/test_handoff_set_replacement.py` already exercises this exact path.

### K8 — Runtime neutrality

**Accepted 2026-11-06 (Lawrence Gray, in the chief-of-staff channel: "Accept K8 runtime neutrality," in the recommended order below).**

**Decision.** The agent contract assumes nothing about the agent's runtime.

Three consequences: artifacts are delivered as content, or through a content-addressed store, rather than as paths on the server's filesystem; task instruction text carries no domain duties the kernel assembled; and per-task agent cost arrives through an ingestion API rather than being read from one vendor's transcripts.

**Evidence.** `log_artifact` requires `project_root`, an absolute server-side path meaning "where the code lives", so a non-software project invents a directory that is deliberately not a repository.
Every Minecraft task's instructions carry the D8 checkpoint duty telling the agent a checkpoint is a commit on its branch; the agent has no branch, and the prompt must teach it to ignore text the kernel assembled.
Minecraft agents' tokens live in their own transcripts, so cost had to be computed by parsing them, which makes cost-per-task incomparable across consumers.

**Evidence and sizing, by consequence.** For each consequence: the exact code it touches today, the external extension it hurts, and an engineer-day estimate.

- **(3) Per-task cost arrives through an ingestion API, not vendor transcript parsing — 3-4 engineer-days.**
  Touches `src/cost_tracking/worker_ingester.py` (370 lines): `WorkerJSONLIngester` reads `~/.claude/projects/<dir>/<session-id>.jsonl` directly, Claude Code's own session-log format, keyed on `message.usage` token-count blocks unique to that CLI, and hardcodes `provider="anthropic"`.
  Hurts Minecraft severely: its agents are Node processes with no Claude Code session at all, so there is no JSONL file for this ingester to read, and cost has to be computed by a separate, ad hoc parse of whatever Minecraft's own process happens to log.
  The citation and numerical verifiers are largely unaffected today, since both currently run as Claude Code sessions, but that is incidental to their implementation, not a guarantee.
  Largest of the three because it is new API surface, not a parameter relaxation: a generic cost-ingestion endpoint/schema any runtime can call (token/cost counts keyed by `agent_id`/`task_id`/`run_id`, vendor-agnostic), with `WorkerJSONLIngester` kept as one caller of that API (the Claude-Code-specific adapter) rather than the only path in.
  Directly unblocks #742 (cost reaching the record as a field of task completion rather than a batch sidecar read).
- **(2) Task instruction text carries no kernel-assembled domain duties — 1 engineer-day.**
  Touches `_build_checkpoint_duty_prompt` (`src/marcus_mcp/tools/task.py:2746-2889`), unconditionally appended to every task's instructions as "Layer 0.2: the ADR-0012 D8 checkpoint duty" ("always included" per its own layer comment) — text that assumes a git branch exists.
  Hurts Minecraft: it has no branch at all, so its own prompt has to separately instruct the agent to ignore text the kernel itself assembled into its instructions, duplicated and contradictory.
  The citation and numerical verifiers are unaffected; both run from real git checkouts where the duty is accurate.
  Smallest of the three: one function, one call site, the same declaration shape K3 already established to skip the injection for a project that declares it has no commit-based recovery story.
- **(1) Artifacts as content, not server-filesystem paths — 2-3 engineer-days.**
  Touches `log_artifact` (`src/marcus_mcp/tools/attachment.py:32-134`): `project_root` is a required parameter that hard-fails if absent, not absolute, or does not exist on the server's own filesystem.
  Hurts Minecraft: it has no repository, so every artifact-logging task invents a fake `project_root` directory purely to satisfy this check, with nothing meaningful written there.
  The AI-citation and numerical-report verifiers are unaffected, both running from a real checkout with a real `project_root`.
  Largest agent-facing contract change of the three, so it benefits from the other two already landing: add an alternate content-addressed path (store the artifact's bytes/hash in the existing `ProjectHistoryPersistence` store instead of requiring a filesystem location), make `project_root` optional when content is supplied this way, and update the handful of callers/readers that currently assume a resolved filesystem path.

| Consequence | Code touched | Extension hurt | Engineer-days |
| --- | --- | --- | --- |
| (3) Cost ingestion API | `cost_tracking/worker_ingester.py` (Claude-Code-JSONL-only) | Minecraft (no Claude Code session to parse at all) | 3-4 |
| (2) No kernel-assembled domain duties in instructions | `task.py:2746-2889` (`_build_checkpoint_duty_prompt`) | Minecraft (no branch to commit to) | 1 |
| (1) Artifacts as content | `attachment.py:32-134` (`log_artifact`) | Minecraft (fake `project_root`) | 2-3 |
| **Total** | | | **6-8** |

**Recommended order, and why.** (3) first: it unblocks #742 immediately and is the one with zero safety net today (no fallback cost path for a non-Claude-Code runtime at all).
Then (2): cheapest, and it reuses K3's own declaration pattern one level up rather than inventing a new mechanism.
Then (1) last: the largest agent-facing contract change, and it benefits from the other two already having landed a working pattern for "a project declares what it does not have."

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

**Accepted narrowly 2026-11-06 (Lawrence Gray, in the chief-of-staff channel: "Go with your recommendation about K10").**

**Decision.** Runtime task creation may declare dependencies on existing board tasks, add-only, project-scoped, never touching a leased task.

The full recommendation below is wider than this. Only the sentence above is accepted; the scorer-component registry half stays an open proposal.

**Recommendation (full, for the open half).** Any feature that rewires a running board obeys two rules: never rewire a leased task, and add and mark unclaimable rather than delete.

Two capabilities follow: a scorer-component registry with visible weights, and an explicit-task creation path at runtime, since `create_tasks` takes prose and `create_project(options.tasks)` is setup-only.

**Evidence for the narrow accept.** Issue #841: `create_tasks(options.tasks)` (PR #820) resolved `depends_on` only against the same call's own batch, never against a task already on the board, so a runtime-created task could only ever depend on something created in the same breath. The Minecraft planner (`~/marcus-experiments/minecraft-plumbing/planner.py`) hit this immediately -- its first natural output depended on a task already on the board and had to drop the dependency, with its prompt instructed to emit only tasks whose prerequisites are already done.

**Why narrow, not the whole proposal.** `depends_on` resolving against the board is exactly the add-only, never-rewire-a-leased-task shape K10 already states: a new task declaring an edge *into* an existing one touches nothing about that task, leased or not. The scorer-component registry is a separate, substantially larger capability with no concrete consumer yet, and accepting it now would be deciding it by accident rather than against its own evidence.

**Confirmed with the first real consumer before implementation.** The Minecraft planner session confirmed the resolution order (in-batch names first, then the board, scoped strictly to the caller's project) and two specific behaviors before any code was written: an ambiguous name (two or more board tasks in the project sharing the exact name) must be refused naming every candidate id, never guessed, since nothing on the board forbids two tasks with the same name; and the dependency target's own status never matters -- a dependency on a DONE task is already satisfied, and on a BLOCKED or DECLINED one the new task is simply unclaimable until that changes, which is this same add-and-mark-unclaimable rule working as intended, not a reason to refuse the dependency.

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

**Why the mechanism sits at the `state.kanban_client` boundary, not inside the provider classes.** `KanbanInterface` and its four implementations (`sqlite_kanban.py`, `planka_kanban.py`, `github_kanban.py`, `linear_kanban.py`) have no reference to a `MarcusServer` or to `state.project_tasks` today, and several of them are constructed directly by standalone tooling (`extensions/citation_verifier/citation_verifier/runner.py`'s seed scripts, the live-check scripts this ADR's own evidence was gathered with) with no `MarcusServer` in the process at all. Teaching the provider classes about `state.project_tasks` would couple a board adapter to a specific server's in-memory cache, breaking that reuse. The guarantee is implemented as a thin wrapper applied where `state.kanban_client` is assigned: every provider stays exactly as decoupled as it is today, and every *caller* of `state.kanban_client.update_task(...)`, present or future, gets the guarantee without doing anything to earn it.

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
- #818, which K2 gates and which gathers a worked example from K2's own resolution once it ships
- The numerical-report verifier owner's direct account of `apply_verification_outcome`, `~/dev/marcus-worktrees/numeric-verifier` (unpushed), K2's evidence
- The same owner's live check of the reopen/bound/decline model against a real server (`~/marcus-experiments/numeric-verifier/k2-livecheck/RESULT.md`), K2 and K5's evidence
- #735, #677, #647, the software-building verification bugs that K4's evidence rests on
- #299, domain-agnostic coordination, which K1 and K3 retire
- #792, merged as `dc996a6f`, which placed all kernel state under one root
- #737 and `docs/design/on-the-record-as-built.md`, where K12's existing mechanism (`filter_eligible_tasks`) and its "no one checks their own work" guarantee are already shipped
- The full-200 readout (`docs/design/research/tow-full-200-readout-2026-10-16.md`), whose Condition B/C comparison K12's evidence rests on, and corrects a prior reading of
- [arXiv 2609.12039](https://arxiv.org/abs/2609.12039), "Reality Is the Final Verifier," cited by K4 and K12 as supporting vocabulary, not as evidence
