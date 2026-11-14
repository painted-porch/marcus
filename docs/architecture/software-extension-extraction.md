# Scoping the software-building extension's extraction

**Status: scoping only.**
No code is moved by this document.
Nothing here changes behavior.

First written 2026-11-06 against `develop` (issue #862).
Revised 2026-11-14 against `develop` at `b33dad5f`, after Lawrence Gray decided on 2026-11-13 that software-building verification becomes a `verify` card owned by a verifier agent rather than an in-process gate behind a shim (Program board item `PVTI_lADODj3MGM4BqP6DzhIeyPo`).
That decision removes this document's one unbuilt prerequisite and changes the sizing and the order; §2 carries the new basis and §3 follows it.
Every line range in §5 was regenerated from the file's own syntax tree on this pass, and every reference cited in the revised sections was re-read on `b33dad5f`.

## Summary

**Total estimate: 35-53 engineer-days** across six concerns.

The decision of 2026-11-13 is what changed this number.
The earlier total of 31-43 days assumed the two verification gates stayed inside the kernel behind an in-process shim, with a further 8-12 days to build a "deferred-completion" mechanism if anyone wanted them genuinely out.
Verification as a card needs no such mechanism, because nothing is held pending a verdict: the `implement` card closes on its own evidence and a dependent `verify` card carries the verdict.
So the 8-12 day branch is gone, the shim is gone, and in its place is one shared concern (declaring the task types, emitting the verify cards, moving the merge gate, wiring the redo loop) at 8-12 days that both gates then build on.
Set against the earlier numbers: this finishes the job for about 2-4 days less than building the deferred-completion mechanism would have cost, and about 4-10 days more than stopping at a shim.
What the extra days buy is the thing the shim never did, which is the gate actually leaving the kernel.

| # | Concern | Engineer-days | Blocked on |
| --- | --- | --- | --- |
| 1 | Cost tracking (§4) | 3-5 remaining | Nothing. The generic transport shipped in PR #867; what is left is moving the two vendor-coupled modules and deciding the legacy duplicate's fate |
| 2 | Decomposition & planning, clean half (§1, `nlp_task_utils.py`) | 1 | Nothing. Can start immediately |
| 3 | **Verification as a card: the shared basis for both gates (described in §2)** | 8-12 | Nothing. The kernel precondition (declaring task types over MCP) landed on 2026-11-14 in PR #919 |
| 4 | Validation gate (§2) | 10-15 | Concern 3 |
| 5 | Product smoke gate (§3) | 6-9 | Concern 3. Shares `report_task_progress`'s completion path with #4, so do not run the two concurrently on parallel branches |
| 6 | Decomposition & planning, hard half (§1, `nlp.py` and `nlp_base.py`) | 7-11 | Nothing. The seeded `create_tasks` graph-authoring path already exists (K10 narrow accept); the remaining work is schema extension, not new kernel capability |

**Recommended order, and why:**

1. **Cost tracking first (§4).**
   The generic transport is now built (PR #867), and what is left (moving the two vendor-coupled modules, deciding the legacy duplicate's fate) is the cheapest and most independent remaining work.
   The vendor-coupled half has zero `src/` callers today, so nothing waits on it and it waits on nothing.
2. **The decomposer's clean half in parallel (§1).**
   `nlp_task_utils.py` touches no kernel state at all and can move almost mechanically.
3. **Verification as a card, before either gate moves (§2).**
   Both gates' destination is a `verify` card that has to exist, be declared, and be reachable before anything can be moved onto it.
   Doing this first also means each gate's own extraction is then a body of logic finding a new home, not a restructuring of the completion path at the same time.
4. **The validation gate (§2).**
   This reverses the earlier version's order, which put the smoke gate first because K5 was already built.
   K5 no longer differentiates them, and the validation gate is now the better first move for a different reason: a verifier agent that claims a `verify` card, runs its checks and reports a verdict is exactly the shape the AI-citation verifier has been running in production since October, so it is the lower-novelty half of the pair.
   Prove the pattern there.
5. **The smoke gate after it, not concurrently (§3).**
   Both gates are concentrated in the same region of `report_task_progress`, and parallel branches will produce conflicting rewrites of it.
   The smoke gate is also the one that adds genuinely new pieces on top of the pattern: the product runner as a subprocess the verifier owns, the operator-park path, and the consequence for how a run reports success (§3).
6. **The decomposer's hard half last (§1).**
   The graph-authoring mechanism it needs already exists, so this is ordinary extension work rather than a first cut against an undesigned mechanism.
   It is still the largest remaining single piece and it benefits from the other concerns' patterns being proven first.

**Assumptions behind every number below:** one engineer already familiar with this codebase; Python only, no Cato (the dashboard) front-end work; existing tests updated, no new test infrastructure built; "done" means the concern lives behind a registration mechanism with the existing test suites green and no behavior change for the software-building consumer; K2's `ProjectConfig.task_type_registrations` field and its MCP write path are already landed and are not re-counted.

## Glossary

| Term | Meaning |
| --- | --- |
| Kernel | The part of Marcus every project needs: the board, task lifecycle, leases, eligibility, registration. |
| Extension | A package that registers task types and policies with the kernel (ADR-0013 K2) and runs its own agents against the board — never code the kernel calls into directly. |
| ADR-0013 | `docs/architecture/adr/0013-kernel-and-extensions.md`, the decision record this scoping document is built against. |
| K-item | One of ADR-0013's thirteen numbered decisions (K1 through K13); each is self-contained with its own evidence. |
| Smoke gate | `_run_product_smoke_gate`, a product-specific check run when a software `integration` task is reported complete. |
| Validation gate | `WorkAnalyzer.validate_implementation_task` (`src/ai/validation/work_analyzer.py`), the code-review-plus-runtime-test check run on an `implement` task's completion. |
| Decomposer | The code that turns a prose project/feature description into a graph of board tasks: `src/marcus_mcp/tools/nlp.py`, `src/integrations/nlp_base.py`, `src/integrations/nlp_task_utils.py`. |
| Pull-mode dispatch | ADR-0013 K2's model: the kernel never calls into an extension's code; it gates and offers a task, and the extension's own agent pulls it through `request_next_task`. |
| `TaskType` | The eight-value software-lifecycle vocabulary (design, implementation, testing, deployment, …) used throughout the decomposer and the validation gate. |
| Verification as a card | The decision of 2026-11-13: a verification verdict reaches the kernel because an agent pulled a `verify` card and reported the verdict as evidence, never because the kernel called a checker. |
| Typed-evidence gate | The kernel's check, at close, that a card of a declared type reported the evidence its type requires (`_apply_typed_evidence_gate`). |
| Offer-time eligibility | The kernel's refusal to offer a verify-shaped card to the agent who closed the card it checks, or to that agent's vendor (ADR-0013 K12). |
| Handoff | The kernel copying a closing card's evidence into its dependents' `inputs` (ADR-0013 K7, `_handoff_evidence_to_dependents`). |

---

## 1. Decomposition & planning (`nlp.py`, `nlp_base.py`, `nlp_task_utils.py`)

**Files:** `src/marcus_mcp/tools/nlp.py` (2181 lines), `src/integrations/nlp_base.py` (1129 lines), `src/integrations/nlp_task_utils.py` (552 lines).
**MCP-reachable entry points (3):** `create_project` (`nlp.py:388`), `add_feature` (`nlp.py:1836`), `create_tasks` (`nlp.py:1870`).
**Sizing: 8-12 engineer-days total** (revised down from an earlier 12-17 draft — Codex review on PR #866 found the seeded `create_tasks` path already provides the graph-authoring mechanism this document previously described as missing; see below), split as:

- **1 day** — `nlp_task_utils.py` (`TaskClassifier`, `TaskBuilder`, `SafetyChecker`, `TaskType`): touches no server state at all, and can move almost mechanically.
- **4-6 days** — `nlp_base.py`'s `NaturalLanguageTaskCreator`.
  The graph-authoring mechanism itself (the seeded `create_tasks` path, `nlp.py:2106-2113`) already exists and needs no new kernel capability, and PR #919 has since widened its schema to carry `task_type`, `inputs` and a caller-supplied `id`.
  What is left is extending the schema for the fields still missing (`acceptance_criteria`, `subtasks`, `source_type`/`source_context`) and routing an extracted decomposer's writes through that tool instead of calling `create_tasks_on_board` in-process the way today's in-kernel decomposer does.
  `_remap_dependencies`'s raw-SQL write (`nlp_base.py:399-580`) stays kernel-internal either way and is not part of this estimate — it is a `KanbanInterface`-conformance fix on its own schedule, not an extraction blocker.
- **3-5 days** — `nlp.py`'s three MCP entry points, separating the decomposition-only logic from the kernel registration writes each one also performs in the same function body (see "Tangled kernel writes," below).

### Inventory table: decomposition & planning

| Item | File:line | Concern | Kernel / Extension | Callers in kernel | Callees in kernel | Target mechanism |
| --- | --- | --- | --- | --- | --- | --- |
| `create_project` | `nlp.py:388-602` | MCP entry point | Mixed | `server.py:2137`, `handlers.py:1801` | `cost_store.record_run`, `project_registry.add_project`, `project_manager.switch_project` | K2 (registration write) for the kernel half; the seeded `create_tasks` path for the decomposition half (see "The graph-authoring write path already exists," below) |
| `_create_project_inner` | `nlp.py:967-1833` | Decomposition + registration, most mixed function in the file | Mixed | `create_project` only | writes `state.kanban_client` twice (1383, 1402), `project_registry.add_project` (1575), `project_manager.switch_project` (1585), `state.log_event` | Split required before any mechanism applies |
| `add_feature` | `nlp.py:1836-1867` | MCP entry point | Extension | `server.py:2366` | none (pass-through) | K2 task-type ownership |
| `create_tasks` | `nlp.py:1870-2181` | MCP entry point; seeded path is K10's accepted narrow case | Mixed | `server.py:2196` (HTTP only — absent from the stdio handler and `tools/__init__.py`) | `project_manager.get_kanban_client`, `state.refresh_project_state` | K10 (seeded/add-only path, accepted); prose path has no mechanism |
| `NaturalLanguageTaskCreator.create_tasks_on_board` | `nlp_base.py:65-352` | The board-write loop | Mixed | called by `create_from_description`, the seeded path, `nlp_tools.py` | `kanban_client.create_task`, `TaskGraphValidator.validate_and_fix` | K10 for the add-only shape; reachable externally today through the seeded `create_tasks` path (see "The graph-authoring write path already exists," below) |
| `NaturalLanguageTaskCreator._remap_dependencies` | `nlp_base.py:438-619` | Dependency-edge persistence, **the sharpest kernel/extension entanglement in this concern** | Kernel-shaped, extension-executed | `create_tasks_on_board` | raw SQL on `kanban_client._with_connection`/`_run_in_executor` — no `KanbanInterface` method exists | None needed for the extraction: it is `create_tasks_on_board`'s own internal detail, not something an external caller touches (see below) |
| `NaturalLanguageTaskCreator._decompose_and_add_subtasks` | `nlp_base.py:621-825` | AI decomposition + Planka checklist side effect | Extension | `create_tasks_on_board` | `from src.marcus_mcp.coordinator import decompose_task, should_decompose` — **imports the kernel's own namespace to find itself** | None required once moved out of `coordinator/` |
| `SafetyChecker.apply_*_dependencies` (3 methods) | `nlp_task_utils.py:292-454` | Design-before-implement, test-after-implement, deploy-last lifecycle rules | Extension | `apply_safety_checks` | none | K2 (declared per-type policy) |
| `TaskBuilder.build_task_data` | `nlp_task_utils.py:176-266` | Task object to board-dict conversion | Mixed | `create_tasks_on_board` | none | Kernel fields (name/status/priority/dependencies) stay as-is; `acceptance_criteria`/`completion_criteria` content is K4 territory once K4's kernel-side injection exists |
| `TaskType` (enum) | `nlp_task_utils.py:16-26` | Software-lifecycle vocabulary | Extension, **currently load-bearing in the kernel** | consumed by `task.py:10409-10410` (`apply_board_wide_gates`) via `EnhancedTaskClassifier`/`PhaseDependencyEnforcer` | — | No kernel-owned replacement exists; this is the reverse coupling (kernel depends on extension) named below |

### Tangled kernel writes inside `_create_project_inner`

Direct, no-tool-boundary writes to kernel state, found while reading `nlp.py` for this inventory:

- `state.kanban_client` reassigned twice (`nlp.py:1383`, `1402`) — a decomposition tool replaces the server's board handle.
- `state.project_registry.add_project` (`nlp.py:1575`) — K2's own registration record, written in-process.
- `state.project_manager.switch_project` (`nlp.py:1585`) — mutates the server's active project as a side effect of creating one.
- `state.log_event` — nine direct writes into the kernel's event log (`nlp.py:1351, 1450, 1462, 1472, 1675, 1695, 1713, 1726, 1744`).
- `src.marcus_mcp.tools.context._about_card_id` / `_persist_decision_to_history` — two *private* functions imported from a sibling kernel tool module (`nlp.py:863`, `915`).

None of this is a defect; it is simply why `_create_project_inner` cannot be lifted as one unit — the kernel writes have to be separated from the decomposition logic before either can move independently.

### The reverse coupling: the kernel borrows the extension's vocabulary

`src/marcus_mcp/tools/task.py:10600-10601`, inside `apply_board_wide_gates` (a kernel function on the task-offer path, `task.py:10519-10744`), imports `PhaseDependencyEnforcer` and `EnhancedTaskClassifier`, both of which import `TaskType` from `nlp_task_utils.py`.
**The kernel's decision about what work to offer an agent currently depends on the software-building extension's own vocabulary.**
This has to be broken — a kernel-owned replacement for phase/type classification, or a K2-based declaration the kernel reads instead — before the decomposer can be extracted without leaving a residual import.

### The graph-authoring write path already exists; the gap is schema richness

(Corrected from an earlier draft of this document, Codex review on PR #866: the mechanism below was previously described as missing.
It is not.)

K2's registration record (`ProjectConfig.task_type_registrations`) covers an extension that *claims* a task type the kernel already created, which is not what a decomposer needs — a decomposer *mints* the graph in the first place.
What a decomposer needs instead already ships: `create_tasks(options.tasks)` (`nlp.py:2106-2113`, accepted narrowly as K10's "runtime task creation may depend on an existing board task," issue #841/PR #855) builds seeded tasks, resolves their dependencies (against the same call's own batch first, then the project's existing board tasks), and persists the whole graph through `create_tasks_on_board` — reachable as an ordinary HTTP MCP tool call, with the caller never touching `kanban_client` itself.
An external decomposer can author its generated graph through this tool today.

Two concrete, smaller gaps remain, neither of which is "no mechanism exists":

- **Schema richness, now mostly closed.** PR #919 (merged 2026-11-14 as `b33dad5f`, issue #911) widened the seed spec to carry `task_type`, `inputs` and a caller-supplied `id`, let `depends_on` name a card by that id, and added `options.task_type_registrations` to the same `create_project` call.
  That matters here beyond convenience: an external decomposer can now mint *typed* cards, which is the precondition for it emitting the `verify` cards §2's decision depends on.
  What a full external decomposer still cannot send, measured against what `TaskBuilder.build_task_data` attaches: `acceptance_criteria`, `subtasks`, `source_type`/`source_context`, and a non-TODO `status`.
  Extending the schema for those is ordinary additive work of the same shape #911 already proved, not new kernel capability.
- **The raw-SQL dependency write stays kernel-internal.** `_remap_dependencies`'s private-method SQL write (`nlp_base.py:438-619`) is `create_tasks_on_board`'s own internal implementation detail — it is not something an external caller touches, and K10's narrow accept did not require replacing it.
  It remains worth fixing on its own merits (K1's "every provider should go through `KanbanInterface`" standard), but it is not a blocker to extracting the decomposer.

This changes concern #5's sizing materially — see its own section below.

---

## 2. The validation gate (`src/ai/validation/`)

**Files:** `__init__.py` (27 lines), `task_filter.py` (90), `validation_models.py` (262), `retry_tracker.py` (216), `task_completeness_validator.py` (515, effectively dead code — no `src/` caller), `work_analyzer.py` (2246).
**Sizing: 10-15 engineer-days**, on top of the shared 8-12 days of concern 3 below.
Revised down from 12-18 by the decision of 2026-11-13: the earlier number carried the shim seam and its escalation machinery (`_handle_validation_failure`, `_alert_validation_unavailable`, `_stamp_validation_unavailable`, `_build_escalation_error_response`), which are now deleted rather than wrapped, and the gate's kernel reads become inputs the card carries rather than reads an out-of-process checker still somehow has to make.
The earlier "+8-12 days if full pull-mode dispatch is attempted" branch is withdrawn; see below for why no such mechanism is needed.

### The K3 boundary, already drawn in code

K3 (Accepted 2026-11-06) already splits this gate cleanly, and the split is small:

- **Kernel-owned (the one skip):** `work_analyzer.py:243-263` — `if not evidence.has_source_files()` and a K2 registration lookup (`requires_source_tree is False`), returning an early pass.
- **Everything else — extension-owned:** the no-source-files FAIL verdict (264-288), the tests-are-ground-truth merge semantics (290-396), the entire code-review LLM prompt and parser (1289-1877), and the citation-verification anti-hallucination pass (1879-2173).

### Inventory table: validation gate (selected — the full gate is ~45 functions; these are the kernel-coupled ones)

| Item | File:line | Concern | Kernel / Extension | Kernel state touched | Target mechanism |
| --- | --- | --- | --- | --- | --- |
| `should_validate_task` | `task_filter.py:11-90` | Label-vocabulary gate for whether validation runs at all | Extension (K3's own "what this does not change") | none | No mechanism yet — narrowing this vocabulary is its own, smaller, undecided question |
| `_should_validate_completion` | `task.py:6484-6519` | Subtask-aware wrapper | Mixed | reads `board_tasks` | Deleted with the seam: under verification as a card the kernel never decides whether to validate, it offers a `verify` card the decomposer emitted |
| `WorkAnalyzer._get_project_root` | `work_analyzer.py:426-508` | Resolves a server-side filesystem path | Extension, reads kernel state | `state.kanban_client._load_workspace_state()`, `state.workspace_manager.project_config`, `state.task_artifacts` | **Resolved by the decision, not by K8 consequence 1**: a verifier agent runs where the code is and resolves its own root, so the kernel stops handing a server-side absolute path to a checker inside it |
| `WorkAnalyzer._get_git_delta_files` | `work_analyzer.py:510-581` | Scopes evidence to the agent's own commits | Mixed, reads kernel assignment state directly | **`state.agent_tasks[agent_id].baseline_commit`** — the sharpest entanglement in this gate | The `verify` card's `inputs`, through the handoff. This is the one piece of new wiring the decision needs; see "What actually changes" below |
| `WorkAnalyzer._get_decisions` | `work_analyzer.py:804-828` | Pulls architectural decisions for the prompt | Kernel call, in-process | `src.marcus_mcp.tools.context.get_task_context(task.id, state)` | `get_task_context` over MCP, which every agent already calls; already correct in shape, just called in-process today |
| `_validate_task_completion` | `task.py:4881-4928`, called from `_record_progress` at `task.py:8106` | **The call seam** | Mixed | `_planner_cost_context`, module globals `_work_analyzer`/`_retry_tracker` | **Deleted, not shimmed.** The verdict arrives as a `verify` card's typed evidence instead; see below |
| `_handle_validation_failure` | `task.py:4931-5015` | Retry vs. escalate | Mixed | `_retry_tracker` | Deleted. `request_task_redo` and its `MAX_TASK_REDO_COUNT` bound already are the kernel-shaped half; the "same issues" content becomes the verifier's own evidence |
| validation gate call site | inside `_record_progress`, `task.py:8106` | Selection + ceiling + escalation | Mixed | `state.lease_manager.touch_lease` | Deleted with the seam. Selection becomes the decomposer emitting a `verify` card; the ceiling becomes the redo bound |

### The verdict path is not missing: verification is a card (decided 2026-11-13)

An earlier version of this section said there was no mechanism for "a completion is held pending another agent's verdict," and sized building one at 8-12 days.
That was the wrong question.
Nothing needs to be held.

Lawrence Gray decided on 2026-11-13, on Program board item `PVTI_lADODj3MGM4BqP6DzhIeyPo`:

> DECIDED by Larry 2026-11-13: verification as a card (option b).
> The kernel checks the shape of the evidence that comes back and never calls into extension code.
> The #862 scoping and the extraction plan proceed on this basis; no shim is built.

The mechanism this needs already runs in production.
The AI-citation verifier's chain is a `find` card that closes on the finder's own evidence, a dependent `verify` card a different agent claims, and a verdict reported as that card's typed evidence.
Its free rehearsal closes 13 of 13 cards through the kernel's gates with zero kernel callbacks into extension code, and `docs/architecture/extension-contract.md` records that evidence.
Every kernel piece the software-building version needs is the same piece, already built and already exercised:

| What the gate needs | The kernel mechanism, today |
| --- | --- |
| A card whose close must carry a verdict | The typed-evidence gate, `_apply_typed_evidence_gate` (`task.py:1047`), keyed off the project's declared types |
| The declaration of those types | `ProjectConfig.task_type_registrations` (ADR-0013 K2, #831), writable over MCP as `create_project(options.task_type_registrations=...)` since PR #919 (2026-11-14) |
| The verify card never going to the author of the work it checks | Offer-time eligibility (ADR-0013 K12), which reads `verifies_task_type` off that same declaration |
| The implement card's evidence reaching the verifier | The handoff, `_handoff_evidence_to_dependents` (`task.py:1245`), which copies a closing card's `inputs` plus its evidence, minus `raw`, into each dependent's `inputs` |
| Sending failed work back | `request_task_redo` (`task.py:9271`), whose own docstring names integration verification as its caller and whose only caller guard is that the caller holds a claimed task, which a verifier holding the verify card satisfies |
| The bound on that loop | `MAX_TASK_REDO_COUNT = 3` (`task.py:126`), recorded on the card, replacing `MAX_VALIDATION_RETRIES` |
| A terminal outcome when the work cannot be verified | `request_task_decline` and the `DECLINED` status (ADR-0013 K5, `task.py:9574`, PR #834) |

### What actually changes, and what it costs

Four things change, and none of them is a new kernel mechanism.

1. **`DONE` on an `implement` card stops meaning "verified."**
   It means the builder reported the evidence its type requires.
   The verdict lives on the dependent `verify` card.
   This is the real content of the decision and everything below follows from it.
2. **The merge to `main` moves.**
   Today `_merge_agent_branch_to_main` is reached from the completion path (`task.py:8506`) and from merge recovery (`task.py:5476`), so an unverified implementation lands on `main` and the gate's refusal is what keeps bad work out.
   Under verification as a card the merge belongs to the `verify` card's close, not the `implement` card's.
   This is the single riskiest piece of the whole extraction, because it sits in `_record_progress`, the worst-tangled function in the file (§5).
3. **The in-process refusal becomes a redo.**
   Today a failed verdict refuses the completion and the same agent, still holding the task, tries again.
   Under a card the verifier reports its verdict and calls `request_task_redo` on the card it checked.
4. **A run's success signal has to read the verify cards.**
   The experiment runners and `RESULTS.md` records count completed cards today.
   With verification on its own cards, a board of closed `implement` cards and failed `verify` cards would read as a successful run.

One piece of new wiring is genuinely needed, and it is small.
The verifier needs the baseline commit of the work it is checking, which the kernel captures at claim time (`_capture_baseline_commit`) and holds in `state.agent_tasks[agent_id].baseline_commit`, the sharpest kernel read in the whole gate.
The handoff copies the closing card's evidence, so either the builder reports its own branch and commit range as evidence (agent-reported, and the verifier can check it against the board), or the kernel stamps the baseline it already holds into the handoff payload.
The second is one key in `_handoff_evidence_to_dependents` and is the more honest of the two, since the baseline is the kernel's own record of when the claim started rather than something the builder should be trusted to report about itself.
Either way it is a key in a payload, not a mechanism.

### The declaration concern 3 would write

This is the concrete deliverable of concern 3, in the shape the kernel already reads.
The software-building extension declares it on the `create_project` call that mints a project, as `options.task_type_registrations` (PR #919), the same way the citation verifier declares its four types.

| Task type | Who may close it | Evidence its close must carry | What its `inputs` carry |
| --- | --- | --- | --- |
| `implement` | the builder that claimed it | the files it changed, the tests it ran and their result, the branch and commit range | the acceptance criteria the decomposer wrote |
| `verify` | a verifier agent, never the author of the `implement` card it depends on, nor that author's vendor | the verdict, the checks that produced it, and the reasons behind a rejection | the implement card's id, its evidence through the handoff, and the baseline commit |
| `integration` | the builder that claimed it | the same as `implement`, plus the composition it assembled | the cards it integrates |
| `verify-product` | a verifier agent, under the same exclusion | the smoke verdict and the probe results | the integration card's id and the verification specs |

Two fields do the load-bearing work, and both already exist.
`verifies_task_type` on a verify-shaped entry is what makes the kernel refuse to offer that card to the author of the work it checks.
`evidence_schema` is what makes the kernel refuse a close that does not carry a verdict, which is the whole guarantee the in-process gate provides today.
`checks_injectable` stays false throughout unless the kernel is meant to add required checks of its own on top of the verifier's, which ADR-0013 K4 allows and this extension does not need.

A fifth type is deliberately absent.
There is no `review` type separate from `verify`: the code review and the runtime test are two checks one verifier runs and reports together, exactly as the citation chain's verifier fetches a page and compares a date in one card.
Splitting them would double the cards for no gain in what the board can enforce.

### The risk this decision carries, stated plainly

The repair loop loses its warm start.
Today a refused completion leaves the same agent holding the same task with its context, its worktree and its reasoning intact, and it fixes the work in place.
Under a card, the `implement` card closes, the agent moves on or drains, and the redo arrives later, possibly to a different agent in a new session that has to rebuild the context from the board.
That makes each repair more expensive in both tokens and wall-clock, and it leans hard on the handoff carrying enough for a cold agent to act on.
Two open issues sit on exactly this seam: #809 (the repair loop only has a path for "the implementation was wrong," not for "the requirement was wrong") and #823 (`report_blocker`'s wording assumes the agent got stuck, which is the wrong frame for a verifier declining a claim after the redo bound).
Neither blocks the decision.
Both should be read before concern 3 is built, because the quality of the handoff is what decides whether this costs a little more or a lot more.

### What this does not change

The kernel still refuses a close whose evidence does not match the declared shape.
It still refuses to offer a verify card to the agent whose work it checks, or to that agent's vendor.
It still never calls into extension code.
The three Agent Invariants are unaffected: the verifier self-selects its card, decides for itself how to check the work, and communicates the verdict only through the board.

## 3. The product smoke gate

**Code:** `_run_product_smoke_gate` (`task.py:2289-2598`, 310 lines) and `_terminalize_escalated_smoke_task` (`task.py:1495-1560`), plus `src/integrations/product_smoke.py` (~1180 lines, the subprocess/server/probe runner).
**Sizing: 6-9 engineer-days**, on top of the shared 8-12 days of concern 3, and including roughly a day to migrate `_terminalize_escalated_smoke_task` onto the existing `DECLINED` status.
Revised from a flat 8 days, which assumed an in-process shim stayed at the completion call site.
There is no shim now (§2), so the call seam is deleted rather than wrapped; against that, this gate carries two pieces the validation gate does not, named below.

### Inventory table: product smoke gate

| Item | File:line | Concern | Kernel / Extension | Kernel state touched | Target mechanism |
| --- | --- | --- | --- | --- | --- |
| `_run_product_smoke_gate` | `task.py:2289-2598`, called from `_record_progress` at `task.py:8211` | Gate body: resolve root, judge behavior evidence, run verification subprocesses | Mixed, heavily extension | reads `state.kanban_client._load_workspace_state()`; writes lease via `park_for_operator` | The body becomes a verifier agent's own program, claiming a `verify` card and reporting the smoke verdict as its typed evidence (§2). K4 names which task type owns the check; the call seam at `8211` is deleted, not shimmed |
| ↳ `_gate_unavailable` (closure) | `task.py:2398` | Park the task when the gate cannot run | **Kernel** | `lease_manager.park_for_operator`, `state.log_event("operator_needed")` | **The one mechanism this gate still needs that does not exist**: an operator-park a verifier can ask for from outside, rather than a closure inside the gate. Small, and in this concern's estimate; see below |
| ↳ project-root resolution | `task.py:2467-2495` | Resolve a server-side absolute path | Mixed | `state.kanban_client._load_workspace_state()` | The verifier resolves its own root, as in §2. What remains for K8 consequence 1 is `log_artifact`, not this |
| ↳ behavior-evidence judgment | `task.py:2514-2547` | Per-product-type evidence bar | Extension | `task.source_context["structural_category"]` only | K4, and the `verify` card's declared evidence schema |
| **`_terminalize_escalated_smoke_task`** | `task.py:1495-1560` | **The one place this gate mutates kernel task state** | **Kernel** | `kanban_client.update_task` to `BLOCKED`, clears `agent_status`/`agent_tasks`, releases `assignment_persistence` and the lease | Migrate onto `request_task_decline`'s already-built `DECLINED` status and release sequence (`task.py:9574-9800`, PR #834). Not a missing mechanism, a one-day migration this inventory is flagging |
| smoke-gate instruction block | `task.py:2877` | Injects "SMOKE GATE REQUIRED" duty text into the prompt | Extension | prompt assembly only | **K8 consequence 2**. Under verification as a card the duty belongs on the `verify` card's own description, which the decomposer writes, so the kernel stops injecting it into every task's prompt |
| `verify_verification_specs`, `VerificationSpec`/`Step`/`Result` | `product_smoke.py` | Subprocess/server/probe runner | Extension | none | No mechanism needed; pure extension logic, and the body of the verifier agent |

### Why the migration, not a prerequisite decision

K5 (a terminal `DECLINED` status) is Accepted and built: `request_task_decline` (`task.py:9574-9800`) landed in PR #834, merged to `develop` as `a162fc90` after six Codex review rounds.
`_terminalize_escalated_smoke_task` was written before that landed and still reaches directly into `state.agent_tasks` (`del state.agent_tasks[agent_id]`) and `state.assignment_persistence` to fake a terminal outcome by writing `BLOCKED` by hand, rather than using the status and release sequence that already exist for exactly this case.
This is not a missing kernel capability.
It is one function that has not yet been updated to use a capability that already shipped, and migrating it is folded into this concern's estimate rather than sequenced as a separate prerequisite.

### What this gate carries that the validation gate does not

The verdict path is settled for both gates by the decision of 2026-11-13 (§2), so this section no longer names a shared missing mechanism.
Two pieces are specific to this gate, and they are why its estimate did not fall as far as the validation gate's.

1. **The operator-park path.**
   `_gate_unavailable` (`task.py:2398`) is a closure inside the gate body that parks the task and logs `operator_needed` when the gate cannot run at all, which is the correct behavior and is kernel-shaped.
   A verifier agent outside the kernel has no way to ask for it today.
   `report_blocker` is the nearest existing tool and is the wrong frame, which is the same mismatch issue #823 already filed from the other direction.
   This is a small addition, costed inside this concern's 6-9 days, and it is the only mechanism either gate still needs.
2. **The run's success signal.**
   This gate is what makes "the integration card closed" mean "the product runs."
   Once the smoke verdict lives on a `verify` card, a board of closed `implement` and `integration` cards says nothing about whether the product works, and the experiment runners, `/marcus` completion runs and the `RESULTS.md` records all read completion counts today.
   Changing what those read is not kernel work and is not in this estimate, but it has to happen in the same pass or the first run after the extraction will report a success it did not have.

---
## 4. Cost tracking (`src/cost_tracking/`)

**Files:** `cost_store.py` (1743), `cost_recorder.py` (454), `cost_aggregator.py` (780), `operations.py` (403), `worker_ingester.py` (370), `worker_intent.py` (254), `worker_usage_ingester.py` (186), `ai_usage_middleware.py` (185), `token_tracker.py` (347).
**Sizing: 3-5 engineer-days remaining** (revised down from an earlier 5-8 — Codex review on PR #866: combining this concern's full estimate with ADR-0013 K8's own consequence-3 sizing double-counted the same deliverable; see the correction below) — the least tangled of the four concerns, because the cut line is already drawn in the code.

**Status update: the generic transport is now built.** PR #867 (`feat/742-k8-cost-ingestion-api`, shape agreed with the Minecraft harness on issue #742) shipped `src/cost_tracking/usage_ingestion.py` — `UsageRecord`, `ingest_usage_records` (with per-batch accept/already-present/rejected acknowledgement for idempotent replay), and `read_run_ledger` — and converted `WorkerJSONLIngester` into one caller of it rather than the only path into `token_events`, with all 21 of its existing tests passing unmodified.
That was the single biggest piece of this concern's original estimate (building the transport, 2-3 days of the original 5-8).
What remains, below, is the rest.

### The K8-consequence-3 verdict

**Kernel, already neutral — nothing to move:**

| Item | File:line | Why kernel |
| --- | --- | --- |
| `CostStore` + `TokenEvent` + `record_event` | `cost_store.py:335-400, 639-1743, 919-987` | Vendor-neutral append-only event store; the schema names no provider |
| `CostRecorder` + `PlannerContext` | `cost_recorder.py:67-418` | Provider-neutral attribution hook, already called identically by all three LLM providers |
| `CostAggregator`, `events_for_tasks` | `cost_aggregator.py` (whole file), `worker_usage_ingester.py:144-186` | Pure reads over the neutral schema |
| `worker_usage_ingester.ingest_usage_log` + `_request_id` | `worker_usage_ingester.py:31-66, 69-132` | **The de facto prototype of K8's ingestion API**: a worker reports its own usage, provider is data not code, dedup is content-hashed — built for exactly the "no Claude session, no worktree" pull-loop case |

**Extension, hard vendor coupling — must move or be replaced:**

| Item | File:line | Why extension | Callers in `src/` |
| --- | --- | --- | --- |
| `WorkerJSONLIngester` (whole class) | `worker_ingester.py:94-350` | Reads `~/.claude/projects/<dir>/<session-id>.jsonl`, Claude Code's own log format; hardcodes `provider="anthropic"` at line 328 | **None** — only tests and experiment-side tooling |
| `classify_tool_intent`, `extract_task_id_from_*` | `worker_intent.py:68-254` | Matches literal Claude Code tool names (`mcp__marcus__*`, `Edit`/`Write`/`Bash`…) and record shapes | `worker_ingester.py` only |
| `TokenTracker`, `AIUsageMiddleware` | `token_tracker.py`, `ai_usage_middleware.py` (532 lines combined) | A second, legacy, duplicate cost system with a hardcoded `$0.03/1k "for Claude"` rate and a placeholder 5x projection multiplier; writes a JSON file, not `token_events` | `server.py:75,202,803`; `ai_analysis_engine.py:282,1635,1671` |
| `DEFAULT_SEED` pricing rows | `cost_store.py:~450-640` | Anthropic-specific price table with cache-write/cache-read multipliers | `load_seed_prices` |

**Target mechanism:** K8 consequence 3 named this directly — a generic ingestion API wrapping `record_event`, with `WorkerJSONLIngester` kept as one caller of it (the Claude-Code-specific adapter) rather than the only path in.
**Built, in PR #867** (as a direct function call, `ingest_usage_records`, rather than a separate MCP tool or HTTP endpoint — the payload schema, dedup primitive, and semantics `worker_usage_ingester.ingest_usage_log` already proved are now generalized into it).

What is left, within this concern's revised 3-5 days: (1) move `worker_ingester.py` and `worker_intent.py` (the two Claude-Code-vendor-coupled modules, now adapters rather than the only path in) out of the kernel as the actual extraction act — **1-2 days**; (2) decide the legacy `token_tracker.py`/`ai_usage_middleware.py` pair's fate (delete or extract), which requires first confirming nothing reads `token_usage.json` — **2-3 days**.

**On double-counting with ADR-0013's own K8 sizing:** ADR-0013's K8 section estimates consequence 3 (the ingestion API) at 3-4 engineer-days on its own, scoped narrowly to the API itself.
This document's cost-tracking concern scoped the *same deliverable* plus two additional pieces (moving the vendor-coupled modules, and the legacy-duplicate decision) at a wider 5-8 days.
Adding K8's full 6-8-day total (covering all three consequences) to this document's estimate therefore counted the shipped transport twice.
Fixed here by stating this concern's remaining 3-5 days as what is left *after* PR #867, and by not re-adding K8 consequence 3's own days separately in the summary below — see the corrected total.

**Not part of this concern, found while investigating issue #883:** `_store_config_snapshot` (`src/marcus_mcp/tools/nlp.py:172`) also writes to `marcus.db`, but into its own unrelated `project_config` collection (project-creation-time AI/model/provider metadata, not `token_events`), lives outside `src/cost_tracking/` entirely, and is provider-agnostic (captures `cfg.ai.provider`/`model`/`temperature` as data, with no hardcoded vendor parsing) — so it changes none of this section's estimates or its vendor-coupling table, it just completes the "what lives where" picture for whoever starts this extraction.

---

## 5. Complete kernel/extension marking of `task.py`

`src/marcus_mcp/tools/task.py` is 11,508 lines on `develop`: **105 top-level functions, 0 classes.**
Every line range in the table below was regenerated from the file's own syntax tree on 2026-11-14, so the coordinates are current rather than carried over from the first pass; the four functions PR #907 added since (`_warn_stranded_once`, `reclaim_stamp`, `assignment_write_payload`, `_board_assignee`, all kernel) are classified here for the first time.
This is the complete marking issue #862 asked for, so the eventual module-split's shape is visible from this document.
**TANGLED** marks a function whose body interleaves kernel and extension logic so tightly that it needs its own internal split before any kernel/extension cut is expressible at all — these are the functions to budget the most review time against, independent of the per-concern sizing above.

| Function | Lines | Kernel / Extension / Mixed | One-line why |
| --- | --- | --- | --- |
| `_record_repair_attempt` | 190-215 | Kernel | Claim-lifecycle counter, no domain content |
| `clear_repair_attempts` | 218-231 | Kernel | Claim budget reset |
| `_record_repair_requeue` | 234-249 | Kernel | Agent-blind re-offer bound |
| `_release_task_file_locks` | 252-287 | Kernel | File-lock registry release, path-agnostic |
| `_not_current_assignment_reply` | 290-315 | Kernel | Claim-ownership refusal shape |
| `_agent_still_holds_task` | 318-329 | Kernel | Claim ownership check |
| `_agent_holds_live_lease` | 332-360 | Kernel | Lease/fencing lookup (K1) |
| `_repair_instructions` | 363-401 | Mixed | Handoff record is kernel; the coaching prose is instruction-text policy (K8 consequence 2) |
| `_record_blocker_attempt` | 404-419 | Kernel | Advisory-blocker ceiling |
| `clear_blocker_attempts` | 422-433 | Kernel | Counter reset |
| `_handoff_lock` | 462-480 | Kernel | K7 handoff idempotency mechanism |
| `_reconciliation_lock` | 483-501 | Kernel | D11 fence idempotency |
| `_worktree_path_for_branch` | 504-546 | Extension | git worktree plumbing |
| `_commit_exists` | 549-578 | Extension | git plumbing |
| `_card_commit_range` | 581-631 | Extension | git rev-list over branches |
| `_capture_baseline_commit` | 634-690 | Extension | git HEAD capture at claim time |
| `_baseline_commit_for_task` | 693-756 | Mixed | Durable per-claim lookup is kernel-shaped; the stored value (a git sha) exists only for the extension |
| `_record_missing_verification_attempt` | 759-775 | Extension | Smoke-gate counter |
| `_record_behavior_evidence_attempt` | 778-795 | Mixed | Generic retry-ceiling mechanism, extension-named |
| `_clear_smoke_attempts` | 798-801 | Extension | Smoke-gate counters |
| `_escalate_missing_verifications_response` | 804-844 | Extension | Verification-policy content |
| `_escalate_behavior_evidence_response` | 847-888 | Extension | Verification-policy content |
| `_task_type_requires_typed_evidence` | 903-933 | Kernel | K2 registration lookup + documented fallback |
| `_typed_evidence_rejection` | 942-1002 | Kernel | Schema-keyed refusal, no domain vocabulary |
| `_escalate_typed_evidence_response` | 1005-1044 | Kernel | Terminal-status escalation of a kernel gate |
| `_apply_typed_evidence_gate` | 1047-1138 | Kernel | K2/K4 mechanism itself |
| `_task_type_is_evidence_recorded` | 1152-1179 | Kernel | K2 registration lookup |
| `_apply_approval_gate` | 1182-1242 | Kernel | Principal-based gate on the task graph |
| `_handoff_evidence_to_dependents` | 1245-1318 | Kernel | Board-mediated communication, K7 handoff |
| `_apply_handoff_to_dependent` (>100 lines) | 1321-1492 | Kernel | K7's named implementation |
| `_terminalize_escalated_smoke_task` | 1495-1560 | Mixed | BLOCKED write + release is kernel; exists only to serve the smoke gate's escalation, and should migrate onto `request_task_decline`'s `DECLINED` status (see §3) |
| `clear_validation_retry` | 1563-1586 | Mixed | Budget reset is kernel; smoke/validation counters it clears are extension |
| `get_project_board_context` | 1589-1655 | Kernel | K6 write-attribution lookup |
| `_response_project_for_task` | 1658-1701 | Kernel | K6 attribution from the task's own project |
| `_planner_cost_context` (>100) | 1704-1808 | Kernel | Cost attribution, provider-agnostic |
| `_get_task_type` | 1811-1830 | Extension | Implementation/design/testing vocabulary |
| `_build_mandatory_workflow_prompt` | 1833-1882 | Extension | Domain duties in instruction text — K8 consequence 2 verbatim |
| `_best_effort_addendum` | 1885-1918 | Mixed | Disclosing degraded upstreams is kernel graph info; wording is extension prose |
| `_is_composition_task` | 1921-1960 | Extension | Composition-synthesis artifact detection |
| `_missing_verifications_for_required_outcomes_response` | 1963-2046 | Extension | Verification-policy content |
| `_missing_coverage_response` | 2049-2147 | Extension | Verification-policy content |
| `_text_contains_render_markup` | 2166-2191 | Extension | HTML keyword heuristic |
| `_behavior_evidence_rejection` | 2194-2286 | Extension | Per-app-type behavior contract |
| `_run_product_smoke_gate` (>100, **TANGLED**) | 2289-2598 | Extension, with a kernel island | See §3 — `_gate_unavailable` inside it is kernel |
| `_parse_contract_metadata` (>100) | 2601-2704 | Extension | Contract-first decomposition artifact |
| `_resolve_scaffold_path` | 2707-2744 | Extension | Source-file scaffolding |
| `_build_checkpoint_duty_prompt` | 2747-2805 | Mixed | Cadence numbers are kernel; duty text is K8 consequence 2 |
| `build_tiered_instructions` (>100, **TANGLED**) | 2808-3373 | Extension, mostly | Kernel islands: recovery handoff, acceptance criteria, dependency/decision-log layers, interleaved |
| `jittered_retry_seconds` | 3392-3418 | Kernel | Poll-cadence decorrelation |
| `calculate_retry_after_seconds` (>100) | 3421-3602 | Kernel | Pull-loop cadence and ETA from the graph |
| `_close_run_on_drain` | 3605-3658 | Kernel | Cost-store run lifecycle on drain |
| `_fire_cost_summary_for_project` | 3661-3696 | Kernel | K9 measurement |
| `_warn_stranded_once` | 3699-3730 | Kernel | Once-per-card heads-up that a stranded assignment was reclaimed (#900); no domain content |
| `reclaim_stamp` | 3733-3773 | Kernel | Keeps who held a stranded card across the assignment write (#900) |
| `assignment_write_payload` | 3776-3809 | Kernel | The single board write that lands a claim, stamp included |
| `_frontier_for_agent` | 3812-3868 | Kernel | K9/#804 frontier reporting |
| `request_next_task` (980 lines, **TANGLED**) | 3871-4878 | Mixed, badly | Kernel: selection/claim/lease/persistence/drain. Extension interleaved: GitHub analyzer, merge-conflict sweep, baseline capture, instruction building |
| `_validate_task_completion` | 4881-4928 | Extension | Verification policy, lazy-imports `src/ai/validation` |
| `_handle_validation_failure` | 4931-5015 | Extension | Retry/escalation over extension evidence |
| `_format_blocker_description` | 5018-5054 | Extension | Verification-policy prose |
| `_verify_agent_has_commits` (>100) | 5057-5160 | Extension | git commit gate |
| `_apply_merge_failure_to_update_data` (>100, **TANGLED**) | 5163-5277 | Mixed | BLOCKED-flip is kernel; git conflict content is extension, same body |
| `_attempt_merge_recovery` (>100) | 5280-5528 | Extension | Worktree/git landing machinery |
| `_sweep_blocked_merge_conflicts` | 5531-5575 | Extension | Exists only for git merge conflicts |
| `_is_ancestor` | 5578-5611 | Extension | git plumbing |
| `_card_footprint` | 5614-5701 | Extension | git log/diff plumbing |
| `_take_tip_paths` | 5704-5749 | Extension | git checkout/rm |
| `_blob_at` | 5752-5775 | Extension | git plumbing |
| `_remove_paths_the_task_deleted` | 5778-5838 | Extension | git plumbing |
| `_apply_footprint_three_way` | 5841-5887 | Extension | git apply --3way |
| `_merge_card_range` (>100) | 5890-6191 | Extension | Entire body is branches/commits/merges |
| `_merge_agent_branch_to_main` (>100) | 6194-6447 | Extension | ADR names the merge machinery as extension |
| `_resolve_completed_task` | 6450-6481 | Kernel | Task-graph lookup across stores |
| `_should_validate_completion` | 6484-6519 | Extension | Delegates to label-vocabulary extension policy |
| `_record_epoch_collision` | 6536-6607 | Kernel | K9/D11 fencing measurement |
| `_store_epoch_collision_detail` | 6610-6656 | Kernel | Event-log provenance |
| `count_epoch_collisions` | 6659-6693 | Kernel | Measurement readout |
| `_board_tasks_for_fence` | 6696-6720 | Kernel | Board read for the fence |
| `_record_refused_attempt` | 6723-6813 | Mixed | Durable refusal record is kernel (D11); hardcodes a git branch name |
| `_task_is_terminal` | 6816-6836 | Kernel | Status predicate |
| `_find_reconciliation_card` | 6839-6862 | Kernel | Fence idempotency marker |
| `_open_reconciliation_card` (>100, **TANGLED**) | 6865-7048 | Mixed | Card mechanics are kernel (D11); card body text is git-specific extension prose |
| `maybe_open_reconciliation_card` | 7051-7134 | Kernel | D11 fence timing |
| `_fence_stale_epoch` (>100, **TANGLED**) | 7137-7271 | Mixed | Fence itself is kernel; refusal message hardcodes a git branch name |
| `_observe_epoch_collision` | 7274-7347 | Kernel | K9 fencing measurement |
| `_live_lease_held_by_other` | 7350-7379 | Kernel | Lease ownership predicate |
| `report_task_progress` | 7382-7470 | Kernel | Board-mediated communication, delegates all gating |
| `_record_progress` (1651 lines, **TANGLED — worst in file**) | 7473-9123 | Mixed, severely | Kernel: fence/guards/lease/persistence/handoff/reconciliation. Extension interleaved: validation gate, smoke gate, commit-verification, merge, GitHub analyzer, stub scanner |
| `_alert_validation_unavailable` | 9126-9189 | Mixed | `operator_needed` channel is kernel; exists only because the extension's gate crashed |
| `_stamp_validation_unavailable` | 9192-9227 | Mixed | Reply-stamping is kernel; the thing stamped is extension-gate health |
| `_build_escalation_error_response` | 9230-9268 | Extension | Validation-escalation payload |
| `request_task_redo` (>100, **TANGLED**) | 9271-9571 | Mixed | Kernel guards/reset/dual-write; extension branch naming and git-range text |
| `request_task_decline` (>100) | 9574-9800 | Kernel | ADR K5: `DECLINED` status, no software content |
| `report_blocker` (>100, **TANGLED**) | 9803-10333 | Mixed | Kernel guards/dedupe/rollback/release; extension AI-suggestion prose, branch naming |
| `_tasks_for_dependency_analysis` | 10339-10364 | Kernel | Project-scoped graph read |
| `_record_task_completion` | 10367-10399 | Kernel | K2's registration roster field |
| `_scope_tasks_to_project` | 10402-10437 | Kernel | Project scoping of the graph |
| `_classify_task_cached` | 10440-10485 | Mixed | Memoization is kernel-neutral; the cached value is extension classification |
| `find_optimal_task_for_agent` | 10488-10516 | Kernel | Self-selection dispatch |
| `apply_board_wide_gates` (>100, **TANGLED**) | 10519-10812 | Extension, with a kernel contract | ADR names the phase logic as extension; the shared-rule contract (O3) is kernel |
| `_find_optimal_task_original_logic` (>100, **TANGLED**) | 10815-11154 | Mixed | Kernel: lock/scoping/filters/reservation; extension: deployment-keyword penalty, AI ranking |
| `find_optimal_task_basic` | 11157-11226 | Mixed | Skill/priority scoring is kernel; deployment-keyword penalty is extension |
| `get_all_board_tasks` | 11229-11279 | Kernel | Pure board read |
| `_board_assignee` | 11282-11319 | Kernel | Reads the assignee the board itself records, for a stranded card (#900) |
| `unassign_task` (>100) | 11322-11508 | Kernel | Claim release across every coordination store |

**Module-level software-specific vocabulary**, worth flagging separately because these constants travel with whichever function owns them.
`EVIDENCE_REQUIRED_TASK_TYPES = frozenset({"find", "verify", "synthesize"})` (line 900) is the same hardcoded set ADR-0013's K2 section already names as a root cause.
`_RENDER_MARKUP_MARKERS` (2153-2191) is an HTML tag list, the most explicitly web-app-specific constant in the file.
`deployment_keywords` is defined twice, at lines 11092 and 11185.
The `"marcus/{agent_id}"` branch-name literal appears on 18 lines, 11 of them constructing it in code rather than describing it in a docstring, and it leaks into otherwise-kernel functions (`_record_refused_attempt`, `_fence_stale_epoch`).

---

## 6. Cross-cutting K8 dependency callouts

Items that cannot fully move until a specific K8 consequence lands, gathered from the concerns above.
The decision of 2026-11-13 shrank this list, because two of the items on it were only on it as long as a checker ran inside the kernel and still needed a server-side path.

- **K8 consequence 1 (artifacts as content):** `log_artifact` itself (`attachment.py:32-134`, named directly in K8's own acceptance) is what remains.
  The two gates' project-root resolutions (`WorkAnalyzer._get_project_root`, §2; the smoke gate's own, §3) are no longer waiting on it: a verifier agent runs where the code is and resolves its own root, so there is no kernel handing an absolute path to a checker inside itself.
  That is a consequence of the decision, not of K8 landing.
- **K8 consequence 2 (no kernel-assembled domain duties in instructions):** `_build_mandatory_workflow_prompt` and `_build_checkpoint_duty_prompt` (§5), and the smoke-gate instruction block (`task.py:2877`, §3).
  All three inject software-specific duty text into every task's prompt, unconditionally.
  The smoke-gate block has a cheaper exit than the other two: once the smoke verdict lives on a `verify` card, the duty belongs on that card's own description, which the decomposer writes.
- **K8 consequence 3 (cost ingestion API):** the entire §4 vendor-coupled half (`worker_ingester.py`, `worker_intent.py`).
  The transport itself shipped in PR #867.

Neither remaining consequence blocks the other concerns from moving.
**On additivity (Codex review on PR #866, still correct):** K8's three-consequence total of 6-8 engineer-days is not fully additive to this document's total.
Consequence 3 (3-4 of those days) is the same deliverable §4 scopes and is now built, and §4's own 3-5-day remainder already reflects that.
Consequence 2 remains genuinely additive at roughly 2-3 days; consequence 1's remaining weight is `log_artifact` alone, which this document does not scope.
Net: this document's 35-53 days, plus K8 consequence 2, plus whatever `log_artifact` costs on its own schedule.

## 7. Confirmation

No code was moved, renamed, or behavior-changed by this document.

On the first pass (2026-11-06) every file:line reference was read directly from `develop` rather than reconstructed from memory or from the issue's own worked example.
On this pass (2026-11-14, `develop` at `b33dad5f`) §5's 105 line ranges were regenerated from `task.py`'s own syntax tree, and every reference cited in §1, §2, §3 and §6 was re-read at that commit.
The claims the revision turns on were each checked against the code rather than inferred from the decision: that `request_task_redo`'s only caller guard is holding a claimed task (`task.py:9271`), that the handoff copies a closing card's `inputs` plus evidence minus `raw` into its dependents (`task.py:1245`), that the merge to `main` is reached from the completion path (`task.py:8506`) and from merge recovery (`task.py:5476`), and that the redo bound is `MAX_TASK_REDO_COUNT = 3` on the card (`task.py:126`).

## Related

- ADR-0013 (`docs/architecture/adr/0013-kernel-and-extensions.md`), especially K2, K3, K4, K5, K8, K10, K12.
- `docs/architecture/extension-contract.md`, which records the citation chain's evidence that verification as a card works: 13 of 13 cards closed through the kernel's gates with zero kernel callbacks into extension code.
- Program board item `PVTI_lADODj3MGM4BqP6DzhIeyPo`, where the decision of 2026-11-13 this revision is written against is recorded.
- PR #919 (`b33dad5f`), where the MCP write path for typed cards and the K2 declaration landed; PR #834 (`a162fc90`), where K5's `DECLINED` status and `request_task_decline` landed; PR #867, where the cost-ingestion transport landed.
- `docs/architecture/building-an-extension.md`, the practical guide this extraction will eventually let a fifth extension follow without reading the kernel's source.
- Issue #862 (this document's own source), #841/#855 (K10's narrow accept), #798 (K3), #830 (K2's evidence/handoff gap), #742 (K8 consequence 3).
- Open issues on the repair seam this decision leans on: #809 (the repair loop has no path for "the requirement was wrong") and #823 (`report_blocker`'s wording assumes the agent got stuck, not a verifier declining a claim).
