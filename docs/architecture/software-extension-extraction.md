# Scoping the software-building extension's extraction

**Status: scoping only.**
No code is moved by this document.
Every claim below was checked against the code on `develop` as of 2026-11-06 (issue #862).
Nothing here changes behavior.

## Summary

**Total estimate: 37-57 engineer-days** across four concerns, not counting two prerequisites that are each their own separate decision with their own sizing:

- K5 (a terminal `DECLINED` status, Accepted but unbuilt) — 3-5 days, blocks the smoke gate's extraction.
- Full pull-mode dispatch for the validation gate (a "deferred-completion" mechanism that does not exist anywhere in Marcus today) — +8-12 days on top of the validation gate's own 12-18, if attempted in this pass rather than deferred behind a shim.

| # | Concern | Engineer-days | Blocked on |
| --- | --- | --- | --- |
| 1 | Cost tracking (`src/cost_tracking/`) | 5-8 | Nothing — can start immediately |
| 2 | Decomposition & planning, clean half (`src/integrations/nlp_task_utils.py`) | 1 | Nothing — can start immediately |
| 3 | Product smoke gate (`_run_product_smoke_gate` and its callers) | 8-14 | K5 (terminal `DECLINED`), not yet built |
| 4 | Validation gate (`src/ai/validation/`) | 12-18 | Shares `report_task_progress`'s completion path with #3; do not run concurrently with #3 on parallel branches |
| 5 | Decomposition & planning, hard half (`nlp.py`, `nlp_base.py`) | 11-16 | K10's "explicit-task creation path at runtime," named as open in ADR-0013 but not yet built |

**Recommended order, and why:**

1. **Cost tracking first.** It is the cheapest, the most independent (the vendor-coupled half has zero `src/` callers today), and it builds the generic ingestion-API pattern that K8 consequence 3 needs anyway — doing it first buys a worked example of "registration plus ingestion" at the lowest risk in the whole set.
2. **The decomposer's clean half in parallel.** `nlp_task_utils.py` touches no kernel state at all and can move almost mechanically; there is no reason to wait for anything else.
3. **The smoke gate, but only after K5 lands.**
   K5 (Accepted 2026-11-05) is the terminal `DECLINED` status the smoke gate's own escalation path currently fakes by hand-mutating `agent_tasks`/`assignment_persistence` directly.
   Extracting the gate before K5 exists means the extension keeps a hand inside kernel assignment state, which fails the same "does an extension need a kernel guarantee modified" test K7 and K4 were decided against.
4. **The validation gate, after the smoke gate, not concurrently with it.**
   Both gates are concentrated in the same roughly 240-line region of `report_task_progress` (`src/marcus_mcp/tools/task.py:7867-8110`).
   Attempting both on parallel branches will produce conflicting rewrites of that region.
5. **The decomposer's hard half last, and only as a first cut.**
   `nlp_base.py`'s `_remap_dependencies` writes the task-dependency graph by raw SQL through a private SQLite-only method (`self.kanban_client._with_connection`), bypassing `KanbanInterface` entirely — this is K10's own "explicit-task creation path at runtime," named in ADR-0013 as still open.
   Sizing this concern today is a first cut against a mechanism that has to be designed as part of doing it, not an estimate against known work.

**Assumptions behind every number below:** one engineer already familiar with this codebase; Python only, no Cato (the dashboard) front-end work; existing tests updated, no new test infrastructure built; "done" means the concern lives behind a registration mechanism with the existing test suites green and no behavior change for the software-building consumer; K2's `ProjectConfig.task_type_registrations` field is already landed and is not re-counted.

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

---

## 1. Decomposition & planning (`nlp.py`, `nlp_base.py`, `nlp_task_utils.py`)

**Files:** `src/marcus_mcp/tools/nlp.py` (2032 lines), `src/integrations/nlp_base.py` (1090 lines), `src/integrations/nlp_task_utils.py` (540 lines).
**MCP-reachable entry points (3):** `create_project` (`nlp.py:387`), `add_feature` (`nlp.py:1694`), `create_tasks` (`nlp.py:1728`).
**Sizing: 12-17 engineer-days total**, split as:

- **1 day** — `nlp_task_utils.py` (`TaskClassifier`, `TaskBuilder`, `SafetyChecker`, `TaskType`): touches no server state at all, and can move almost mechanically.
- **7-11 days** — `nlp_base.py`'s `NaturalLanguageTaskCreator`, chiefly `_remap_dependencies` (`nlp_base.py:399-580`), which writes the task-dependency graph through raw SQL against a private SQLite-only method rather than any `KanbanInterface` method.
  This is the one piece of this scope that requires building new kernel capability (K10's still-open graph-authoring mechanism) rather than just moving existing code.
- **3-5 days** — `nlp.py`'s three MCP entry points, separating the decomposition-only logic from the kernel registration writes each one also performs in the same function body (see "Tangled kernel writes," below).

### Inventory table: decomposition & planning

| Item | File:line | Concern | Kernel / Extension | Callers in kernel | Callees in kernel | Target mechanism |
| --- | --- | --- | --- | --- | --- | --- |
| `create_project` | `nlp.py:387-601` | MCP entry point | Mixed | `server.py:2110`, `handlers.py:1605` | `cost_store.record_run`, `project_registry.add_project`, `project_manager.switch_project` | K2 (registration write) for the kernel half; no mechanism for the decomposition half — see "missing mechanism" below |
| `_create_project_inner` | `nlp.py:850-1691` | Decomposition + registration, most mixed function in the file | Mixed | `create_project` only | writes `state.kanban_client` twice (1277, 1446), `project_registry.add_project`, `project_manager.switch_project`, `state.log_event` | Split required before any mechanism applies |
| `add_feature` | `nlp.py:1694-1725` | MCP entry point | Extension | `server.py:2339` | none (pass-through) | K2 task-type ownership |
| `create_tasks` | `nlp.py:1728-2032` | MCP entry point; seeded path is K10's accepted narrow case | Mixed | `server.py:2169` (HTTP only — absent from the stdio handler and `tools/__init__.py`) | `project_manager.get_kanban_client`, `state.refresh_project_state` | K10 (seeded/add-only path, accepted); prose path has no mechanism |
| `NaturalLanguageTaskCreator.create_tasks_on_board` | `nlp_base.py:65-313` | The board-write loop | Mixed | called by `create_from_description`, the seeded path, `nlp_tools.py` | `kanban_client.create_task`, `TaskGraphValidator.validate_and_fix` | K10 for the add-only shape; no mechanism for decomposition-time graph authorship |
| `NaturalLanguageTaskCreator._remap_dependencies` | `nlp_base.py:399-580` | Dependency-edge persistence, **the sharpest kernel/extension entanglement in this concern** | Kernel-shaped, extension-executed | `create_tasks_on_board` | raw SQL on `kanban_client._with_connection`/`_run_in_executor` — no `KanbanInterface` method exists | **Missing mechanism** — see below |
| `NaturalLanguageTaskCreator._decompose_and_add_subtasks` | `nlp_base.py:582-786` | AI decomposition + Planka checklist side effect | Extension | `create_tasks_on_board` | `from src.marcus_mcp.coordinator import decompose_task, should_decompose` — **imports the kernel's own namespace to find itself** | None required once moved out of `coordinator/` |
| `SafetyChecker.apply_*_dependencies` (3 methods) | `nlp_task_utils.py:280-442` | Design-before-implement, test-after-implement, deploy-last lifecycle rules | Extension | `apply_safety_checks` | none | K2 (declared per-type policy) |
| `TaskBuilder.build_task_data` | `nlp_task_utils.py:175-254` | Task object to board-dict conversion | Mixed | `create_tasks_on_board` | none | Kernel fields (name/status/priority/dependencies) stay as-is; `acceptance_criteria`/`completion_criteria` content is K4 territory once K4's kernel-side injection exists |
| `TaskType` (enum) | `nlp_task_utils.py:16-26` | Software-lifecycle vocabulary | Extension, **currently load-bearing in the kernel** | consumed by `task.py:10409-10410` (`apply_board_wide_gates`) via `EnhancedTaskClassifier`/`PhaseDependencyEnforcer` | — | No kernel-owned replacement exists; this is the reverse coupling (kernel depends on extension) named below |

### Tangled kernel writes inside `_create_project_inner`

Direct, no-tool-boundary writes to kernel state, found while reading `nlp.py` for this inventory:

- `state.kanban_client` reassigned twice (`nlp.py:1277`, `1446`) — a decomposition tool replaces the server's board handle.
- `state.project_registry.add_project` (`nlp.py:1433`) — K2's own registration record, written in-process.
- `state.project_manager.switch_project` (`nlp.py:1443`) — mutates the server's active project as a side effect of creating one.
- `state.log_event` — nine direct writes into the kernel's event log (`nlp.py:1226, 1315, 1327, 1337, 1533, 1553, 1571, 1584, 1602`).
- `src.marcus_mcp.tools.context._about_card_id` / `_persist_decision_to_history` — two *private* functions imported from a sibling kernel tool module (`nlp.py:745-748`).

None of this is a defect; it is simply why `_create_project_inner` cannot be lifted as one unit — the kernel writes have to be separated from the decomposition logic before either can move independently.

### The reverse coupling: the kernel borrows the extension's vocabulary

`src/marcus_mcp/tools/task.py:10409-10410`, inside `apply_board_wide_gates` (a kernel function on the task-offer path), imports `PhaseDependencyEnforcer` and `EnhancedTaskClassifier`, both of which import `TaskType` from `nlp_task_utils.py`.
**The kernel's decision about what work to offer an agent currently depends on the software-building extension's own vocabulary.**
This has to be broken — a kernel-owned replacement for phase/type classification, or a K2-based declaration the kernel reads instead — before the decomposer can be extracted without leaving a residual import.

### Missing mechanism: a graph-authoring write path

K2's registration record (`ProjectConfig.task_type_registrations`) covers an extension that *claims* a task type the kernel already created.
The decomposer does something K2 was never scoped for: it *mints* the entire graph and its edges, including the raw-SQL write into `task_dependencies` named above.
ADR-0013's K10 names "an explicit-task creation path at runtime" as a capability it recommends but does not accept (only the narrower "runtime task creation may depend on an existing board task" is Accepted, per K10 and issue #841/PR #855).
Until that lands, the only supported path for an external decomposer is the seeded path (`create_tasks`'s `options.tasks` branch) — which is HTTP-only today and still hands the raw `kanban_client` to `create_tasks_on_board`.

---

## 2. The validation gate (`src/ai/validation/`)

**Files:** `__init__.py` (27 lines), `task_filter.py` (90), `validation_models.py` (262), `retry_tracker.py` (216), `task_completeness_validator.py` (515, effectively dead code — no `src/` caller), `work_analyzer.py` (2246).
**Sizing: 12-18 engineer-days** (add 8-12 more if full pull-mode dispatch is attempted now rather than deferred behind an in-process shim).

### The K3 boundary, already drawn in code

K3 (Accepted 2026-11-06) already splits this gate cleanly, and the split is small:

- **Kernel-owned (the one skip):** `work_analyzer.py:243-263` — `if not evidence.has_source_files()` and a K2 registration lookup (`requires_source_tree is False`), returning an early pass.
- **Everything else — extension-owned:** the no-source-files FAIL verdict (264-288), the tests-are-ground-truth merge semantics (290-396), the entire code-review LLM prompt and parser (1289-1877), and the citation-verification anti-hallucination pass (1879-2173).

### Inventory table: validation gate (selected — the full gate is ~45 functions; these are the kernel-coupled ones)

| Item | File:line | Concern | Kernel / Extension | Kernel state touched | Target mechanism |
| --- | --- | --- | --- | --- | --- |
| `should_validate_task` | `task_filter.py:11-90` | Label-vocabulary gate for whether validation runs at all | Extension (K3's own "what this does not change") | none | No mechanism yet — narrowing this vocabulary is its own, smaller, undecided question |
| `_should_validate_completion` | `task.py:6293-6328` | Subtask-aware wrapper | Mixed | reads `board_tasks` | — |
| `WorkAnalyzer._get_project_root` | `work_analyzer.py:426-508` | Resolves a server-side filesystem path | Extension, reads kernel state | `state.kanban_client._load_workspace_state()`, `state.workspace_manager.project_config`, `state.task_artifacts` | **K8 consequence 1** (artifacts/paths as content) |
| `WorkAnalyzer._get_git_delta_files` | `work_analyzer.py:510-581` | Scopes evidence to the agent's own commits | Mixed, reads kernel assignment state directly | **`state.agent_tasks[agent_id].baseline_commit`** — the sharpest entanglement in this gate | Needs the baseline commit delivered to the extension some other way (not a kernel read it should own) |
| `WorkAnalyzer._get_decisions` | `work_analyzer.py:804-828` | Pulls architectural decisions for the prompt | Kernel call, in-process | `src.marcus_mcp.tools.context.get_task_context(task.id, state)` | Board-mediated communication (K1) — already correct in shape, just called synchronously |
| `_validate_task_completion` | `task.py:4690-4737` | **The call seam** | Mixed | `_planner_cost_context`, module globals `_work_analyzer`/`_retry_tracker` | **Missing mechanism** — see below |
| `_handle_validation_failure` | `task.py:4740-4824` | Retry vs. escalate | Mixed | `_retry_tracker` | Retry/escalation ceiling is kernel-shaped (same family as `MAX_TASK_REDO_COUNT`); "same issues" content is extension evidence |
| validation gate call site | `task.py:7889-7996` | Selection + ceiling + escalation | Mixed | `state.lease_manager.touch_lease` | K2 for selection once label vocabulary is replaced; kernel escalation stays |

### Missing mechanism: a pull-mode verdict return path

Today the kernel *calls* the gate synchronously inside `report_task_progress` (`_validate_task_completion`), and consumes a `ValidationResult` object in-process — exactly what K2 forbids ("the kernel never invokes an extension's code directly").
Under pull-mode, the validator would be a separate agent claiming a `verify`-type task and reporting its own evidence, which means the *blocking* semantics — an agent's completion report cannot close until the gate says yes — have to be re-expressed as a dependency edge plus the existing typed-evidence gate (K2/K4), and the retry ceiling has to move with it.
**There is no mechanism today for "a completion is held pending another agent's verdict."**
This single unbuilt piece is why the validation gate's estimate carries an explicit +8-12 day branch rather than being folded into the 12-18 day baseline: the baseline assumes an in-process adapter shim stays at the call seam for this pass, deferring full pull-mode dispatch to a follow-up.

---

## 3. The product smoke gate

**Code:** `_run_product_smoke_gate` (`task.py:2288-2597`, 310 lines) and `_terminalize_escalated_smoke_task` (`task.py:1494-1558`), plus `src/integrations/product_smoke.py` (~1180 lines, the subprocess/server/probe runner).
**Sizing: 8 engineer-days if K5 lands first, 14 if the extraction has to work around K5's absence.**

### Inventory table: product smoke gate

| Item | File:line | Concern | Kernel / Extension | Kernel state touched | Target mechanism |
| --- | --- | --- | --- | --- | --- |
| `_run_product_smoke_gate` | `task.py:2288-2597` | Gate body: resolve root, judge behavior evidence, run verification subprocesses | Mixed, heavily extension | reads `state.kanban_client._load_workspace_state()`; writes lease via `park_for_operator` | K4 (injected check on the `integration` type) for the gate itself |
| ↳ `_gate_unavailable` (closure) | `task.py:2397-2465` | Park the task when the gate cannot run | **Kernel** | `lease_manager.park_for_operator`, `state.log_event("operator_needed")` | **Missing mechanism**: an operator-park response contract any extension could invoke, not just this one |
| ↳ project-root resolution | `task.py:2467-2495` | Resolve a server-side absolute path | Mixed | `state.kanban_client._load_workspace_state()` | **K8 consequence 1** |
| ↳ behavior-evidence judgment | `task.py:2514-2547` | Per-product-type evidence bar | Extension | `task.source_context["structural_category"]` only | K4 |
| **`_terminalize_escalated_smoke_task`** | `task.py:1494-1558` | **The one place this gate mutates kernel task state** | **Kernel** | `kanban_client.update_task` to `BLOCKED`, clears `agent_status`/`agent_tasks`, releases `assignment_persistence` and the lease | **This is K5's `DECLINED` done by hand as `BLOCKED`** — the exact gap K5 was accepted to close |
| smoke-gate instruction block | `task.py:2852-2890` | Injects "SMOKE GATE REQUIRED" duty text into the prompt | Extension | prompt assembly only | **K8 consequence 2** |
| `verify_verification_specs`, `VerificationSpec`/`Step`/`Result` | `product_smoke.py` | Subprocess/server/probe runner | Extension | none | No mechanism needed; pure extension logic |

### Why K5 is a hard prerequisite, not a nice-to-have

`_terminalize_escalated_smoke_task` reaches directly into `state.agent_tasks` (`del state.agent_tasks[agent_id]`) and `state.assignment_persistence` to fake a terminal outcome using `BLOCKED`, because no terminal `DECLINED` status exists yet.
Extracting the smoke gate without K5 landed first means the extension keeps that same hand inside kernel assignment state — precisely the "extension needs a kernel guarantee modified" test K7 and K4 were each decided against, now reproduced a third time.
Sequence K5 (its own 3-5 days, not counted in this concern's 8-14) before attempting this extraction.

---

## 4. Cost tracking (`src/cost_tracking/`)

**Files:** `cost_store.py` (1743), `cost_recorder.py` (454), `cost_aggregator.py` (780), `operations.py` (403), `worker_ingester.py` (370), `worker_intent.py` (254), `worker_usage_ingester.py` (186), `ai_usage_middleware.py` (185), `token_tracker.py` (347).
**Sizing: 5-8 engineer-days** — the least tangled of the four concerns, because the cut line is already drawn in the code.

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

**Target mechanism:** K8 consequence 3 names this directly — a generic ingestion API wrapping `record_event`, with `WorkerJSONLIngester` kept as one caller of it (the Claude-Code-specific adapter) rather than the only path in.
The payload schema (`TokenEvent`), the dedup primitive (`_request_id`), and the semantics (`ingest_usage_log`) already exist and are already tested; what is missing is only the transport (an MCP tool or HTTP endpoint) and the attribution binding (`AgentBinding`, `worker_ingester.py:55-86`, is the right shape for that).
The legacy `token_tracker.py`/`ai_usage_middleware.py` pair is a separate decision (delete or extract); deciding that is part of this concern's 5-8 days, and deleting it requires first confirming nothing reads `token_usage.json`.

---

## 5. Complete kernel/extension marking of `task.py`

`src/marcus_mcp/tools/task.py` is 11,134 lines on `develop`: **103 top-level functions, 0 classes.**
This is the complete marking issue #862 asked for, so the eventual module-split's shape is visible from this document.
**TANGLED** marks a function whose body interleaves kernel and extension logic so tightly that it needs its own internal split before any kernel/extension cut is expressible at all — these are the functions to budget the most review time against, independent of the per-concern sizing above.

| Function | Lines | Kernel / Extension / Mixed | One-line why |
| --- | --- | --- | --- |
| `_record_repair_attempt` | 189-214 | Kernel | Claim-lifecycle counter, no domain content |
| `clear_repair_attempts` | 217-230 | Kernel | Claim budget reset |
| `_record_repair_requeue` | 233-248 | Kernel | Agent-blind re-offer bound |
| `_release_task_file_locks` | 251-286 | Kernel | File-lock registry release, path-agnostic |
| `_not_current_assignment_reply` | 289-314 | Kernel | Claim-ownership refusal shape |
| `_agent_still_holds_task` | 317-328 | Kernel | Claim ownership check |
| `_agent_holds_live_lease` | 331-359 | Kernel | Lease/fencing lookup (K1) |
| `_repair_instructions` | 362-400 | Mixed | Handoff record is kernel; the coaching prose is instruction-text policy (K8 consequence 2) |
| `_record_blocker_attempt` | 403-418 | Kernel | Advisory-blocker ceiling |
| `clear_blocker_attempts` | 421-432 | Kernel | Counter reset |
| `_handoff_lock` | 461-479 | Kernel | K7 handoff idempotency mechanism |
| `_reconciliation_lock` | 482-500 | Kernel | D11 fence idempotency |
| `_worktree_path_for_branch` | 503-545 | Extension | git worktree plumbing |
| `_commit_exists` | 548-577 | Extension | git plumbing |
| `_card_commit_range` | 580-630 | Extension | git rev-list over branches |
| `_capture_baseline_commit` | 633-689 | Extension | git HEAD capture at claim time |
| `_baseline_commit_for_task` | 692-755 | Mixed | Durable per-claim lookup is kernel-shaped; the stored value (a git sha) exists only for the extension |
| `_record_missing_verification_attempt` | 758-774 | Extension | Smoke-gate counter |
| `_record_behavior_evidence_attempt` | 777-794 | Mixed | Generic retry-ceiling mechanism, extension-named |
| `_clear_smoke_attempts` | 797-800 | Extension | Smoke-gate counters |
| `_escalate_missing_verifications_response` | 803-843 | Extension | Verification-policy content |
| `_escalate_behavior_evidence_response` | 846-887 | Extension | Verification-policy content |
| `_task_type_requires_typed_evidence` | 902-932 | Kernel | K2 registration lookup + documented fallback |
| `_typed_evidence_rejection` | 941-1001 | Kernel | Schema-keyed refusal, no domain vocabulary |
| `_escalate_typed_evidence_response` | 1004-1043 | Kernel | Terminal-status escalation of a kernel gate |
| `_apply_typed_evidence_gate` | 1046-1137 | Kernel | K2/K4 mechanism itself |
| `_task_type_is_evidence_recorded` | 1151-1178 | Kernel | K2 registration lookup |
| `_apply_approval_gate` | 1181-1241 | Kernel | Principal-based gate on the task graph |
| `_handoff_evidence_to_dependents` | 1244-1318 | Kernel | Board-mediated communication, K7 handoff |
| `_apply_handoff_to_dependent` (>100 lines) | 1320-1491 | Kernel | K7's named implementation |
| `_terminalize_escalated_smoke_task` | 1494-1559 | Mixed | BLOCKED write + release is kernel; exists only to serve the smoke gate's escalation (K5 gap, see §3) |
| `clear_validation_retry` | 1562-1585 | Mixed | Budget reset is kernel; smoke/validation counters it clears are extension |
| `get_project_board_context` | 1588-1654 | Kernel | K6 write-attribution lookup |
| `_response_project_for_task` | 1657-1700 | Kernel | K6 attribution from the task's own project |
| `_planner_cost_context` (>100) | 1703-1807 | Kernel | Cost attribution, provider-agnostic |
| `_get_task_type` | 1810-1829 | Extension | Implementation/design/testing vocabulary |
| `_build_mandatory_workflow_prompt` | 1832-1881 | Extension | Domain duties in instruction text — K8 consequence 2 verbatim |
| `_best_effort_addendum` | 1884-1917 | Mixed | Disclosing degraded upstreams is kernel graph info; wording is extension prose |
| `_is_composition_task` | 1920-1959 | Extension | Composition-synthesis artifact detection |
| `_missing_verifications_for_required_outcomes_response` | 1962-2045 | Extension | Verification-policy content |
| `_missing_coverage_response` | 2048-2146 | Extension | Verification-policy content |
| `_text_contains_render_markup` | 2165-2190 | Extension | HTML keyword heuristic |
| `_behavior_evidence_rejection` | 2193-2285 | Extension | Per-app-type behavior contract |
| `_run_product_smoke_gate` (>100, **TANGLED**) | 2288-2597 | Extension, with a kernel island | See §3 — `_gate_unavailable` inside it is kernel |
| `_parse_contract_metadata` (>100) | 2600-2703 | Extension | Contract-first decomposition artifact |
| `_resolve_scaffold_path` | 2706-2743 | Extension | Source-file scaffolding |
| `_build_checkpoint_duty_prompt` | 2746-2789 | Mixed | Cadence numbers are kernel; duty text is K8 consequence 2 |
| `build_tiered_instructions` (>100, **TANGLED**) | 2792-3355 | Extension, mostly | Kernel islands: recovery handoff, acceptance criteria, dependency/decision-log layers, interleaved |
| `jittered_retry_seconds` | 3374-3400 | Kernel | Poll-cadence decorrelation |
| `calculate_retry_after_seconds` (>100) | 3403-3553 | Kernel | Pull-loop cadence and ETA from the graph |
| `_close_run_on_drain` | 3556-3609 | Kernel | Cost-store run lifecycle on drain |
| `_fire_cost_summary_for_project` | 3612-3647 | Kernel | K9 measurement |
| `_frontier_for_agent` | 3650-3705 | Kernel | K9/#804 frontier reporting |
| `request_next_task` (980 lines, **TANGLED**) | 3708-4687 | Mixed, badly | Kernel: selection/claim/lease/persistence/drain. Extension interleaved: GitHub analyzer, merge-conflict sweep, baseline capture, instruction building |
| `_validate_task_completion` | 4690-4737 | Extension | Verification policy, lazy-imports `src/ai/validation` |
| `_handle_validation_failure` | 4740-4824 | Extension | Retry/escalation over extension evidence |
| `_format_blocker_description` | 4827-4863 | Extension | Verification-policy prose |
| `_verify_agent_has_commits` (>100) | 4866-4969 | Extension | git commit gate |
| `_apply_merge_failure_to_update_data` (>100, **TANGLED**) | 4972-5086 | Mixed | BLOCKED-flip is kernel; git conflict content is extension, same body |
| `_attempt_merge_recovery` (>100) | 5089-5337 | Extension | Worktree/git landing machinery |
| `_sweep_blocked_merge_conflicts` | 5340-5384 | Extension | Exists only for git merge conflicts |
| `_is_ancestor` | 5387-5420 | Extension | git plumbing |
| `_card_footprint` | 5423-5510 | Extension | git log/diff plumbing |
| `_take_tip_paths` | 5513-5558 | Extension | git checkout/rm |
| `_blob_at` | 5561-5584 | Extension | git plumbing |
| `_remove_paths_the_task_deleted` | 5587-5647 | Extension | git plumbing |
| `_apply_footprint_three_way` | 5650-5696 | Extension | git apply --3way |
| `_merge_card_range` (>100) | 5699-6000 | Extension | Entire body is branches/commits/merges |
| `_merge_agent_branch_to_main` (>100) | 6003-6256 | Extension | ADR names the merge machinery as extension |
| `_resolve_completed_task` | 6259-6290 | Kernel | Task-graph lookup across stores |
| `_should_validate_completion` | 6293-6328 | Extension | Delegates to label-vocabulary extension policy |
| `_record_epoch_collision` | 6345-6416 | Kernel | K9/D11 fencing measurement |
| `_store_epoch_collision_detail` | 6419-6465 | Kernel | Event-log provenance |
| `count_epoch_collisions` | 6468-6502 | Kernel | Measurement readout |
| `_board_tasks_for_fence` | 6505-6529 | Kernel | Board read for the fence |
| `_record_refused_attempt` | 6532-6622 | Mixed | Durable refusal record is kernel (D11); hardcodes a git branch name |
| `_task_is_terminal` | 6625-6645 | Kernel | Status predicate |
| `_find_reconciliation_card` | 6648-6671 | Kernel | Fence idempotency marker |
| `_open_reconciliation_card` (>100, **TANGLED**) | 6674-6857 | Mixed | Card mechanics are kernel (D11); card body text is git-specific extension prose |
| `maybe_open_reconciliation_card` | 6860-6943 | Kernel | D11 fence timing |
| `_fence_stale_epoch` (>100, **TANGLED**) | 6946-7080 | Mixed | Fence itself is kernel; refusal message hardcodes a git branch name |
| `_observe_epoch_collision` | 7083-7156 | Kernel | K9 fencing measurement |
| `_live_lease_held_by_other` | 7159-7188 | Kernel | Lease ownership predicate |
| `report_task_progress` | 7191-7279 | Kernel | Board-mediated communication, delegates all gating |
| `_record_progress` (1651 lines, **TANGLED — worst in file**) | 7282-8932 | Mixed, severely | Kernel: fence/guards/lease/persistence/handoff/reconciliation. Extension interleaved: validation gate, smoke gate, commit-verification, merge, GitHub analyzer, stub scanner |
| `_alert_validation_unavailable` | 8935-8998 | Mixed | `operator_needed` channel is kernel; exists only because the extension's gate crashed |
| `_stamp_validation_unavailable` | 9001-9036 | Mixed | Reply-stamping is kernel; the thing stamped is extension-gate health |
| `_build_escalation_error_response` | 9039-9077 | Extension | Validation-escalation payload |
| `request_task_redo` (>100, **TANGLED**) | 9080-9380 | Mixed | Kernel guards/reset/dual-write; extension branch naming and git-range text |
| `request_task_decline` (>100) | 9383-9609 | Kernel | ADR K5: `DECLINED` status, no software content |
| `report_blocker` (>100, **TANGLED**) | 9612-10142 | Mixed | Kernel guards/dedupe/rollback/release; extension AI-suggestion prose, branch naming |
| `_tasks_for_dependency_analysis` | 10148-10173 | Kernel | Project-scoped graph read |
| `_record_task_completion` | 10176-10208 | Kernel | K2's registration roster field |
| `_scope_tasks_to_project` | 10211-10246 | Kernel | Project scoping of the graph |
| `_classify_task_cached` | 10249-10294 | Mixed | Memoization is kernel-neutral; the cached value is extension classification |
| `find_optimal_task_for_agent` | 10297-10325 | Kernel | Self-selection dispatch |
| `apply_board_wide_gates` (>100, **TANGLED**) | 10328-10553 | Extension, with a kernel contract | ADR names the phase logic as extension; the shared-rule contract (O3) is kernel |
| `_find_optimal_task_original_logic` (>100, **TANGLED**) | 10556-10866 | Mixed | Kernel: lock/scoping/filters/reservation; extension: deployment-keyword penalty, AI ranking |
| `find_optimal_task_basic` | 10869-10938 | Mixed | Skill/priority scoring is kernel; deployment-keyword penalty is extension |
| `get_all_board_tasks` | 10941-10991 | Kernel | Pure board read |
| `unassign_task` (>100) | 10994-11134 | Kernel | Claim release across every coordination store |

**Module-level software-specific vocabulary** (constants worth flagging separately, since they travel with whichever function owns them): `EVIDENCE_REQUIRED_TASK_TYPES = frozenset({"find", "verify", "synthesize"})` (line 899, the same hardcoded set ADR-0013's K2 section already names as a root cause); `_RENDER_MARKUP_MARKERS` (2152-2162, an HTML tag list — the most explicitly web-app-specific constant in the file); `deployment_keywords` (defined twice, lines 10804 and 10897); the `"marcus/{agent_id}"` branch-name literal, which appears six times and leaks into otherwise-kernel functions (`_record_refused_attempt`, `_fence_stale_epoch`).

---

## 6. Cross-cutting K8 dependency callouts

Items that cannot fully move until a specific K8 consequence lands, gathered from all four concerns above:

- **K8 consequence 1 (artifacts as content):** `WorkAnalyzer._get_project_root` (§2), the smoke gate's project-root resolution (§3), and `log_artifact` itself (`attachment.py:32-134`, named directly in K8's own acceptance).
  All three assume a server-side absolute path.
- **K8 consequence 2 (no kernel-assembled domain duties in instructions):** `_build_mandatory_workflow_prompt` and `_build_checkpoint_duty_prompt` (§5), and the smoke-gate instruction block at `task.py:2852-2890` (§3).
  All three inject software-specific duty text into every task's prompt, unconditionally.
- **K8 consequence 3 (cost ingestion API):** the entire §4 vendor-coupled half (`worker_ingester.py`, `worker_intent.py`).

None of these three consequences block the *other* three concerns in this document (decomposition, validation, smoke-gate mechanics) from moving — they specifically block the pieces named above, which is why K8's own three-consequence sizing (6-8 engineer-days, ADR-0013 K8) is additive to, not a subset of, this document's 37-57.

## 7. Confirmation

No code was moved, renamed, or behavior-changed by this document.
Every file:line reference above was read directly from `develop` while writing this scoping, not reconstructed from memory or from the issue's own worked example.

## Related

- ADR-0013 (`docs/architecture/adr/0013-kernel-and-extensions.md`), especially K2, K3, K4, K5, K8, K10.
- `docs/architecture/building-an-extension.md`, the practical guide this extraction will eventually let a fifth extension follow without reading the kernel's source.
- Issue #862 (this document's own source), #841/#855 (K10's narrow accept), #798 (K3), #830 (K2's evidence/handoff gap), #742 (K8 consequence 3).
