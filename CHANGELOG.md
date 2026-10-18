# Changelog

All notable changes to Marcus will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.10] - 2026-10-18

### Fixed
- Corrected the GitHub links the repository move left stale: the README clone command, the Issues, Discussions, and PyCon links, and the `cato` clone now point at `painted-porch`; the docs site's Discord and repository links are fixed. `posidonius` is unchanged, because it did not move.
- Engine cost is no longer undercounted. The Tow runner now captures OpenAI's web-search fee and Gemini's Google-Search grounding fee as `search_fee_usd`, which the token-only logging missed; a clean full run costs about $35, where the earlier token-only figure badly understated the grounded engines.
- `REPRODUCING.md` now states the preregistration correctly (under 5% predicted, 10% the falsification line, 12.1% strict against 4.3% adjudicated), reports delivered-versus-declined coverage, and carries the real cost.

### Added
- `CITATION.cff` so the software and the On the Record study are citable, and a README link to the reproduction guide.

## [0.3.9] - 2026-10-18

### Added
- On the Record: a preregistered replication of the Tow Center AI-citation study across four engines and 200 questions, run a second time through a verification checkpoint. Unchecked, the engines were wrong about 7 in 10 times and 51% of cited links were fabricated, dead, or the wrong source. Through the checkpoint, 0 fabricated links reached the reader. Includes the public site and exportable per-case audit bundles.
- System of Record: typed task contracts, silent eligibility so an author never verifies its own work, deterministic verification, a human sign-off, and exportable audit bundles.

### Changed
- request_next_task is fast again on large boards. Task classification on the request path is memoized, and dependent-count and critical-path work is hoisted out of the hot loop (#743, #744).
- The README front-door now explains Marcus in plain English, with the On the Record study as the worked example.

### Fixed
- A range of coordination, lease, recovery, and verification fixes landed since 0.3.8 (#706 through #746), including lease-ownership hardening, a circuit breaker after repeated silent recoveries, and test-data isolation.

## [0.3.8] - 2026-05-23

**Two fundamental architectural shifts ship in this release.** How
agents are spawned changes from a persistent polling pool to ephemeral,
one-agent-per-task; and how a project description becomes a task
graph fundamentally changes via the #607 decomposition redesign. Net
effect: fewer, richer tasks; no idle agents; substantially lower
cost per project.

### Changed (behavior)

- **Ephemeral, one-agent-per-task lifecycle (#600, Fix 3 of #595)**:
  the experiment runner no longer maintains a persistent agent pool
  that polls `request_next_task`. Agents are now **ephemeral** —
  spawned when a layer of the DAG has work, claim a task, execute,
  exit. No idle polling; no agents-without-tasks. Closes the "88%
  idle polls" waste documented in the test18 cost diagnosis. Spawn
  pattern matches the task graph's topology rather than a fixed
  agent count.
- **Decomposition redesign (#607 steps 3 + 4 + 5)** — three
  consolidations of how a project description becomes a task graph:
  - **Test-pair rollup (#608, step 3)**: removed paired
    `Test {feature}` tasks at standard + enterprise complexity.
    Test-coverage criteria now appear as `completion_criteria` on
    the `Implement {feature}` task and the worker prompt mandates
    TDD as a project-wide standard. Empirical: standard board task
    counts dropped 20.3 → 14.3 across the 9-decomp probe.
  - **Gap-fill rollup (#611, step 4)**: outcome-coverage gap-fill
    no longer creates `gap_fill_<uuid>` board tasks. Each gap rolls
    up as a criterion on an existing task — routed by post-fill
    coverage → integration-verification task → first task. Same
    intent-fidelity check; criteria-as-output instead of
    tasks-as-output.
  - **Enterprise prompt tightening (#616, step 5)**: PRD-analysis
    prompt at enterprise complexity targets 8-12 broad feature areas
    (was 15-30+ narrow features) with rich per-feature descriptions
    that bundle related concerns. Empirical: enterprise board task
    counts dropped 66.3 → 23.3 (−65%).
- **Three-tier task context delivery (#605/#606)**: every task
  assignment carries a three-tier context bundle in the
  `request_next_task` response — `project_contract` (global),
  `dependency_artifacts` (one-hop, in-scope), `transitive_context`
  (past one-hop, reference-only). Removes the previously-optional
  `get_task_context` call from the agent's critical path. Agents
  past one hop from the foundation no longer build their task blind.

### Added

- **Foundation contract is project-global (#598)**: the
  pre-fork foundation contract (tech stack, public API surface,
  shared invariants) now reaches every task via the
  `project_contract` field on `get_task_context`, not just direct
  descendants of the foundation task. Closes the test54 finding
  that 4 of 6 agents lacked the contract.

### Fixed

- **CRITICAL: `build_task_data` was silently dropping
  `Task.completion_criteria` (#614)**. PRs #608 and #611 populated
  the field correctly in memory but the Task→task_data conversion
  at `nlp_task_utils.py:217` did not copy it. Result: every gap-fill
  criterion and every test-coverage criterion was dropped before
  reaching the kanban DB. Verified by direct query of `data/kanban.db`:
  **zero tasks across 4 days had non-empty `completion_criteria`
  pre-fix**. With this fix the decomposition redesign actually
  reaches agents in production.
- **CRITICAL: `create_project` race corrupted project boards (#613,
  fixes #610)**. Concurrent calls with different names overwrote each
  other's `project_id` mid-flight via `auto_setup_project`. Silent
  data corruption — two projects' tasks commingled under one
  project_id. Fix: process-wide `threading.Lock` serialization
  (cross-loop), acquired cooperatively via polling so the event loop
  is never blocked.
- **Runtime verification subprocess inherited bad stdin (#602)**:
  `_validate_runtime` spawned Python with `stdin` inherited from
  the parent, causing pytest to wedge in test53. Fix: `stdin=DEVNULL,
  start_new_session=True`.
- **Ephemeral worker worktree cleanup (#600/#595)**: ephemeral
  layered spawning replaces the polling pool; workers now exit
  cleanly and reclaim their git worktree and branch on completion.
- **`log_artifact` accepts JSON dict/list content (#597)**.
- **`create_project` no longer silently skips design auto-completion
  when `options.project_root` is unset (#590/#596)**: now defaults
  `project_root` to `~/.marcus/projects/<id>` instead of skipping
  the design phase.

### Refactor

- `Task.completion_criteria` type hint corrected from
  `Optional[Dict[str, Any]]` to `Optional[List[str]]` (#612). Removes
  the `# type: ignore` tax that compounded across #608 + #611.
- Dead code removed: `_build_feature_gap_fill_task`,
  `_build_contract_gap_fill_task`, `_translate_stub_ids_to_real_ids`
  (no longer reachable post step 4).

### Known issues (tracked for v0.3.9)

- **#615**: `contract_first` decomposer falls back to `feature_based`
  at standard + enterprise complexity due to cross-contract type-
  consistency check failure. The LLM that generates per-domain
  contracts produces inconsistent typings on shared fields across
  domains (e.g. `description: string | null` in one contract,
  `description: string` in another). Pre-existing behavior, not
  introduced by this release. `contract_first` works correctly at
  prototype complexity.
- **#617**: Functional requirements with empty LLM-generated
  descriptions fall through to the infrastructure-task fallback
  (`"Set up and configure {name}"`). Affects ~15% of enterprise
  functional requirements; the other 85% receive rich descriptions
  per step 5.
- **#618 / #619 / #620**: Three small naming / determinism issues in
  the decomposer — doubled "Implement" word from the gap-fill
  normalizer, occasional design-ghost `status=todo` instead of
  auto-completed `status=done`, near-duplicate functional
  requirements at enterprise without fuzzy dedup. Tracked as
  step 6 of #607.

## [0.3.7.post1] - 2026-05-19

**Post-release roll-up of bug fixes, dev-experience improvements, and
the new terminal kanban board.** No new headline features — this
release picks up everything that landed on `develop` since `v0.3.7`
so production users get them without waiting for `v0.3.8`.

### Added

- **Terminal kanban board** (#569): `marcus board` renders the live
  task board in your terminal. `marcus board --watch` refreshes in
  place; `marcus board --list` lists projects.
- **Board simulator demo** (#578): `marcus board --demo` seeds a
  throwaway fake project and animates fake agents moving cards from
  `backlog -> in progress -> done`, then exits on its own. Self-
  contained — no Marcus server, Docker, or real agents required.
  Flags: `--agents N`, `--speed X`, `--seed N`, `--interval SECS`.
- **Experiment stall watchdog** (#564): monitor mode now detects and
  reports stalled experiments instead of silently hanging.
- **Debug logging on `request_next_task`** (#572): per-task debug
  entries on the hot path so stuck assignments are easier to diagnose.

### Fixed

- **`marcus restart`** (#567): now correctly restarts with the
  previous transport configuration.
- **Non-zero tmux `base-index`** (#568): experiment runner no longer
  miscomputes pane indices when tmux is configured with
  `base-index 1`.
- **Validation gate subtask resolution** (#559): validation gate
  resolves subtask IDs to their parent task before checking; removes
  the stale 30-minute lease grace period.
- **Issue #198** (#566): see PR for specifics.

### Changed

- **Repository hygiene** (#574, #575, #576): removed an empty file,
  added `.gitignore` entries for build caches, untracked `mlruns/`,
  and dropped 13 unused LFS assets from the default branch to shrink
  clone size.

## [0.3.7] - 2026-05-17

**Telemetry + cost-forecasting foundation release — opt-in anonymous
telemetry, Phase 0 cost-signal persistence, and experiment-runner
resilience.**

The headline change is opt-in anonymous telemetry (#416): a new
`src/telemetry/` package that ships anonymous, aggregate usage events to
PostHog Cloud so the Marcus team can see which features are used and
where coordination breaks. It is **off until the user opts in**, the
first-run notice explains the choice, and `marcus telemetry
{status,enable,disable,purge}` gives full control. The PostHog *project*
key (a write-only ingest key, safe to embed) ships in source so opted-in
users need zero configuration. Alongside it, Phase 0 (#546) persists 14
cost-forecasting signal columns to the local SQLite cost DB so a future
cost-prediction model has training data.

### Added

- **Opt-in anonymous telemetry (#416)**
  - `src/telemetry/` package — `client`, `config`, `cli`, `events`
  - All event sites hooked per the #416 schema (`project_created`, etc.)
  - `marcus telemetry {status,enable,disable,purge}` CLI, routed before
    MCP setup so opt-out works even when the rest of Marcus is broken
  - First-run notice; `MARCUS_TELEMETRY=off` env override
  - Embedded PostHog project key; `MARCUS_POSTHOG_API_KEY` env override
    for development / self-hosted PostHog
  - `was_fallback` on `project_created` — surfaces when `contract_first`
    decomposition silently fell back to `feature_based`
- **Phase 0 cost-signal persistence (#546)**
  - 14 cost-forecasting columns persisted to `runs` / `token_events`
  - `persist_phase0_run_signals` + `_derive_phase0_close_signals`
- **Experiment-runner monitor stall watchdog (#564)**

### Fixed

- Validation gate resolves subtasks; stale 30-min lease grace removed
  (#557, #559)
- Post-delete experiment fixes — monitor provider, foundation
  serialize, lease debug (#558)
- `marcus` console script reachable via synchronous `cli_main` wrapper
  (Codex P1)
- Phase 0 intent-fidelity columns no longer NULL — fidelity persisted
  after `record_run` creates the row (Kaia review #546)
- `project_created` no longer re-fires on the dedup-cache replay path;
  Phase 0 `is_local_llm` uses the shared local-provider taxonomy
  (Codex P2)

### Changed

- Default LLM `max_tokens` raised so longer planner/decomposer responses
  are not truncated mid-structure.

### Notes

- `run_cost_features` central cost-model training event was deferred to
  #563 — Phase 0 keeps cost signals local-only for now.
- Reinstall (`pip install -e .`) to pick up the `marcus` console script
  and embedded telemetry key.

## [0.3.6.post4] - 2026-05-13

**Cost-tracking foundation release — SQLite cost system shipped end-to-end, multi-provider work, planner resilience.**

The headline change is the cost-tracking system (#409): a SQLite-backed
schema (`runs`, `token_events`, `project_names`, `task_names`,
`project_budgets`, `model_prices`) that captures every LLM call across
planner, workers, and monitor — with per-role, per-operation,
per-provider, per-decomposer aggregation. No production API changes for
callers; cost data is now reliably attributable to (project, task,
agent, operation) tuples for downstream dashboards.

### Added

- **Cost-tracking system (#409 epic, phases 1–5)**
  - SQLite schema + capture + aggregation foundation (#497)
  - Worker JSONL ingester for off-process token consumption (#498)
  - `get_cost_summary` MCP tool (#499)
  - Project-axis aggregation + automatic `PlannerContext` push (#503)
  - `project_summary` aggregator as dashboard precursor (#513)
  - Persistent project names + `create_project` rebind + monitor
    attribution (#515)
  - `project_name` plumbed through `list_projects` (#508)
  - Per-operation drill-down for planner LLM calls (#517)
  - `experiments` table renamed to `runs` + path discriminator (#522)
  - Worker `tool_intent` + `task_id` tracker so worker token use is
    attributable to specific tasks (#534)
  - Token audit + per-role `by_operation` split (#528)
  - Task name snapshot pattern + backfill (#536)
  - Decomposer label stamped on `runs` + `by_decomposer` aggregation
    slice (#539)
- **Outcome-coverage hardening (#449 follow-ups)**
  - Verb whitelist replaced with diverse domain examples (#490)
  - "Task X" LLM artifact promoted to "Implement X" (#509)
  - `success_signal` injected into `acceptance_criteria` (#524, slice A
    of #523)
  - Runtime smoke gate accepts per-outcome verifications (#525, slice B
    of #523)
- **Multi-provider support**
  - Generic cloud LLM provider for OpenAI-compatible APIs (#494)
  - Local LLM `<think>...</think>` reasoning blocks stripped from
    responses (#489)
- **Experiments**
  - Agent model can be set independently of Planner via `--model` flag
    (#540)
- **Planner resilience**
  - `safe_structured_call` helper centralizes structured-LLM calls
    with truncation-retry semantics (#542) at `src/utils/structured_llm.py`
- **Docs**
  - CONTRIBUTING trimmed; local-LLM setup refreshed; Ollama badge added
  - Architecture diagrams added

### Fixed

- **Cost attribution**
  - Project-creation LLM work no longer attributed to the active
    project (#507)
  - `token_events` deduped on `request_id` (#511) with no-op migration
    + `busy_timeout` (#512)
  - Anthropic prices seeded from the official pricing page (#510)
  - 100% planner-cost attribution leak plugged (#516)
  - `runs` rows correctly closed on completion (#538)
  - Design-phase background task preserves the parent context's
    `run_id` instead of dropping it to `'unassigned'`, so
    `create_project` design-phase LLM calls are attributed to the
    project's actual run row in `get_cost_summary` — Codex P2 on PR
    #545
- **AI / Provider**
  - `config.ai.max_tokens` honored on the `LLMAbstraction.analyze` path
  - Provider selection locked to `config.ai.provider` with hard-fail
    on miss (#535)
  - `<think>` strip anchored to leading prefix only — Codex P2 on
    PR #489
- **Coordination / Kanban**
  - `agent_status.current_tasks` cleared on lease recovery (#485)
  - `create_tasks` exempted from `contract_first` fail-fast (#478)
  - Original exception logged before `KanbanIntegrationError` wrap
    (#480)
  - Missing `project_root` fails fast instead of warning after success
    (#478)
  - Slug names normalized from weak LLM gap-fill responses (#479)
  - `contract_first → feature_based` fallback surfaced in result dict
    (#478)
  - Dataclass / Pydantic objects handled in kanban JSON columns (#502)
  - `_normalize_type` loosened to ignore stylistic noise in contract
    validation (#505)
- **Experiments**
  - Completion check isolated from MLflow logging in monitor loop
    (#495)

### Notes for v0.3.7

The next release will ship PostHog telemetry (#416) plus the ML
forecasting Phase 0 data-collection work (persisting already-captured
signals to `runs`). The cost-tracking system landed in this release
provides the substrate those features build on.

## [0.3.6.post3] - 2026-05-01

**Verification release — concurrent-init lock regression net + CoP demo end-to-end smoke confirmed.**

Closes the two verification gaps left by `v0.3.6.post1` and `v0.3.6.post2`.
No production code changes; this release pins behavior we already shipped
so future refactors can't silently regress it.

### Added

- **Concurrent `create_project` test for `_kanban_init_lock_manager` (#461)**
  at `tests/unit/mcp/test_parallel_create_project.py`.  PR #452 added the
  per-event-loop lock that prevents the 807s stall under concurrent
  `create_project` calls; this PR closes the test gap left at the time.
  Four tests: factory-called-once invariant, wall-clock budget,
  half-init detection (the actual regression net for lock removal),
  and provider-match short-circuit.

### Process

- **Regression-net taxonomy in test docstrings** — the
  `test_parallel_create_project.py` module documents which test
  actually catches lock removal (Test 3 — half-init detection) vs
  which tests are invariant assertions that pin the contract but
  pass even without the lock.  Verified empirically by replacing
  `async with _kanban_init_lock_manager.get_lock():` with `if True:`
  and observing only Test 3 fails.

  Heuristic banked for future hardening (Simon `08a896f4`): a test
  that claims to catch removal of a load-bearing line must include
  the recipe to verify it does.  If the docstring doesn't show how
  to falsify the test by removing the protected behavior, the claim
  is unverified.  Two Kaia reviews (#7 + #8) caught and corrected an
  initial overstated regression-net claim before merge.

### Verified

- **CoP demo end-to-end smoke (#460)** — full Marcus + Posidonius +
  Cato pipeline ran cleanly at the John Deere Data Analytics
  Community of Practice on 2026-05-01.  No stray nodes, kanban_client
  initialized once, agents auto-advanced through runs, Epictetus
  audit fired on completion, Cato rendered planning swim lanes and
  quality dashboard.  This confirms the post1 (concurrent-init lock)
  and post2 (GraphAugmenter unification, v37 orphan fix) work
  composed correctly under live demo conditions.

## [0.3.6.post2] - 2026-05-01

**DAG hygiene release — GraphAugmenter Protocol unification, classifier + composition fixes, foundation public-API contracts.**

The headline change is the `GraphAugmenter` Protocol unification (#456), which
collapses the two parallel pre-inference task-synthesis pipelines
(`outcome_coverage` and `spec_coverage`) into a single chain-orchestrator
pattern. Synthesized gap-fill tasks now uniformly participate in dependency
inference, foundation wiring, and safety checks — closing the v37
orphan-failure-mode where `spec_coverage` gap tasks were appended with
`dependencies=[]` and bypassed all downstream wiring.

Plus three smaller DAG-hygiene improvements: a setup-tier classifier fix
(#455) that lets foundation tasks count as valid prereqs of implementation
tasks, a public-API foundation contract enforcement (#446) so foundation
tasks `log_decision` their interface surface, and a mandatory composition
task (#463) for multi-domain contract-first projects to wire entry points
explicitly instead of leaning on integration verification's catch-all.

### Added

- **`GraphAugmenter` Protocol + `AugmentationResult` dataclass (#456)** at
  `src/marcus_mcp/coordinator/graph_augmentation.py`. Structural Protocol
  with `@runtime_checkable` for orchestrator-side `isinstance` validation;
  keyword-only `augment` signature; canonical `(augmented_tasks,
  synthesized_ids, telemetry)` return shape. Augmenter names are
  validated for uniqueness at chain entry.

- **`run_augmenter_chain` orchestrator (#456)** with defense-in-depth
  `try/except` around every augmenter call. A buggy or future augmenter
  that forgets to catch its own exceptions cannot crash the decomposer —
  failures log a warning naming the augmenter and the chain continues
  with prior tasks. Telemetry is namespaced by `augmenter.name` so
  multi-augmenter chains don't collide on event-payload keys.

- **`OutcomeCoverageAugmenter`** as a thin Protocol-satisfying dispatcher
  to two new public module functions:
  `apply_outcome_coverage_to_feature_graph` and
  `apply_outcome_coverage_to_contract_graph` in
  `src/marcus_mcp/coordinator/outcome_coverage.py`. Both return
  `AugmentationResult` directly with the canonical
  `PLANNING_INTENT_FIDELITY` telemetry shape — no parser-side wrapper
  type, no helper-then-translate two-step.

- **`SpecCoverageAugmenter`** wrapping `check_spec_coverage`. Both
  decomposers register `[OutcomeCoverageAugmenter,
  SpecCoverageAugmenter]` via a single helper
  `AdvancedPRDParser._build_augmenter_chain()`. Order is load-bearing:
  `spec_coverage` runs after `outcome_coverage` so it sees outcome
  gap-fill tasks during spec feature scanning.

- **Composition task synthesis for multi-domain contract-first
  projects (#463)**. When `len(impl_tasks) >= 2`, a composition task is
  appended that depends on every implementation task — explicit
  ownership for entry-point wiring instead of relying on integration
  verification's catch-all to flag the v38-style "every domain
  implements cleanly but App.tsx returns null" failure mode.

- **Foundation public-API contract (#446)**. Foundation tasks now
  `log_decision` their public interface surface so domain agents can
  consume the contract with `get_task_context`. Closes the silent
  drift where two domain agents would import the same foundation
  module but disagree on the function signatures.

- **Setup tier in task classifier (#455)**. Foundation tasks (`Set up
  Auth foundation`, etc.) are now valid prerequisites of implementation
  tasks. Word-boundary matching prevents `"install"` from false-matching
  `"uninstall"` in the keyword scan.

- **ADR 0011 — GraphAugmenter Protocol** at
  `docs/architecture/adr/0011-graph-augmenter-protocol.md`. Documents
  the pattern, trade-offs, and a concrete recipe for adding future
  augmenters (NFR coverage, security checks, lint coverage) without
  re-introducing the parallel-pipeline smell.

### Fixed

- **v37 orphan-failure-mode (#456)**. `spec_coverage` gap tasks were
  appended post-safety-check with `dependencies=[]`, making them
  orphans that bypassed `_infer_smart_dependencies` and foundation
  wiring. They now flow through the augmenter chain inside the
  decomposer as first-class graph members and inherit phase
  dependencies, implementation dependencies, and testing dependencies
  via `apply_safety_checks` like every other task.

- **Foundation visibility to spec coverage (Codex P2 on PR #473)**.
  In the contract-first path, foundation tasks synthesized pre-fork
  by `_synthesize_shared_foundation` were appended after
  `decompose_by_contract` returned, so the augmenter chain saw only
  contract tasks during scanning. `spec_coverage` could synthesize
  duplicate `spec_gap` tasks for features that foundation tasks
  already implemented. Fixed by threading `pre_existing_tasks` into
  `decompose_by_contract`; the chain now runs on the combined
  `foundation + contract` list.

- **Composition task false-trigger (Codex P2 on PR #472)**.
  `decompose_result.augmented_tasks` includes outcome-coverage gap-fill
  tasks (`source_type="gap_fill_contract"`). Those address outcome
  gaps, not domain multiplicity, so they must not count toward the
  composition-trigger threshold. Filtered to `source_type="contract_first"`
  before the trigger check.

- **Classifier word-boundary matching (Codex P2 on PR #466)**.
  `"install"` was false-matching `"uninstall"` in the setup-tier
  keyword scan. Fixed with `\b<keyword>\w*` regex.

### Changed

- **`AdvancedPRDParser.decompose_by_contract` return type changed from
  `ParserOutcomeCoverage` to `AugmentationResult`** as part of the
  augmenter unification. Consumers read augmented tasks via
  `result.augmented_tasks` (unchanged) and outcome-coverage telemetry
  via `result.telemetry["outcome_coverage"]` (was
  `result.coverage.intent_fidelity_score` etc.). The `PLANNING_INTENT_FIDELITY`
  event payload emitted to Cato is byte-identical — only the internal
  reading code changed.

### Removed

- `AdvancedPRDParser._apply_outcome_coverage_to_graph` and
  `_apply_outcome_coverage_to_contract_graph` private methods.
  Replaced by public module functions (see Added).
- `AdvancedPRDParser._build_gap_fill_task` and
  `_build_contract_gap_fill_task` private methods. Lifted to module
  scope alongside the apply-helpers.
- `ParserOutcomeCoverage` dataclass. The lifted helpers return the
  canonical `AugmentationResult` directly.
- Post-safety-check `check_spec_coverage` call site at
  `src/integrations/nlp_tools.py:1336`. The chain now handles spec
  coverage inside the decomposer.
- Vestigial `project_name` parameter on `check_spec_coverage`
  (declared but never read in the function body).

### Process

This release was developed via a 5-stage TDD migration with Kaia
review gates after each stage. Three bright-line PR gates verified
on production code at merge time:

```
git grep _apply_outcome_coverage -- src/   = 0
git grep ParserOutcomeCoverage    -- src/   = 0
git grep "from src.integrations.spec_coverage import check_spec_coverage" -- src/ = 1 (only the augmenter)
```

Pattern worth repeating for future architectural cleanups.

## [0.3.6.post1] - 2026-04-29

**Post-release — intent fidelity coverage ON BY DEFAULT, parallel experiment lock, epictetus event progress.**

The headline addition is the user-outcome coverage pipeline (issue #449),
which catches missing user-visible outcomes (e.g. snake_game-v31's silent
absence of a rendering task) at planning time. **The pipeline is enabled by
default** — `MARCUS_OUTCOME_COVERAGE=false` opts out for users who need
the pre-#449 legacy decomposer behavior.

### Added
- **User-outcome coverage pipeline (#449) — ON BY DEFAULT.** Both decomposers
  (`parse_prd_to_tasks` and `decompose_by_contract`) extract user-visible
  outcomes from the spec, run a coverage check against the freshly-decomposed
  task graph, synthesize gap-fill tasks for uncovered outcomes, and emit a
  `PLANNING_INTENT_FIDELITY` event with `intent_fidelity_score` plus coverage
  maps. Synthesized gap-fill tasks participate in dependency inference and
  foundation wiring as first-class graph members (no orphans). Contract-first
  gap-fill tasks carry `<!-- MARCUS_CONTRACT_FIRST -->` description markers so
  contract metadata survives provider round-trip (Planka). New event type
  `EventTypes.PLANNING_INTENT_FIDELITY`. ~3,500 lines of code + 300+ unit tests.
- **Epictetus phase progress events (#450)** — Posidonius pipeline emits
  EPICTETUS_STARTED / EPICTETUS_COMPLETE / EPICTETUS_FAILED to
  `epictetus_progress.jsonl` so Cato can render audit-phase status.
- **Concurrent `create_project` lock (#452)** — `_kanban_init_lock` at module
  level in `nlp.py` serializes concurrent kanban_client initializations,
  preventing the 807s stall observed in batch experiments. Test coverage of
  the concurrent path is tracked in #460 (v0.3.6.post3).

### Fixed
- **Coverage failures never block project creation** — broadened `except
  ValueError` to `except Exception` in three coverage helper sites so
  transient LLM errors (timeouts, API errors, parse errors) downgrade to a
  logged warning instead of crashing PRD analysis. Surfaced by Kaia review
  on PR #457.
- **Contract metadata round-trip on Planka** — gap-fill tasks now embed the
  `MARCUS_CONTRACT_FIRST` marker in their description so reload from
  providers that don't persist `Task.responsibility` or `source_context`
  (Planka) recovers contract ownership via `_parse_contract_metadata`
  priority-3 fallback. Surfaced by Codex review on PR #457.

### Changed
- **Cato integration documentation** — refreshed marcus-ai.dev pages to match
  current product reality.

### Notes
- The default-ON flag means new installs run the coverage pipeline
  automatically. To opt out, set `MARCUS_OUTCOME_COVERAGE=false` (or
  `0` / `no` / `off` / `disabled`) in the marcus_mcp server's environment.
  Marcus does not auto-load `.env`; the var must be exported in the shell
  that launches the server.
- Cost / latency measurement for the new pipeline (and for all experiments)
  is tracked in #409 (high-priority, v0.3.7) — the soak data we expected to
  gate this flag flip is still pending. Decision to ship ON-by-default in
  this post-release was made on positive qualitative signal from
  snake_game-v38; quantitative validation continues via #409.

## [0.3.6] - 2026-04-26

**Patch release — parallel experiment platform (Part 1), board integrity guards, agent auto-termination, and experiment reliability fixes.**

### Added
- **Parallel experiment isolation** — `run_comparison_experiment.py --parallel` now
  gives each Marcus instance its own SQLite kanban DB (`db_path`) and MCP URL baked
  into the spawned shell scripts at generation time. Previously all parallel runs
  shared one MCP endpoint and one board, contaminating each other's task assignments.
- **Agent auto-termination (GH-389)** — Marcus sends a termination signal when the
  experiment completes; agents that have been idle for the grace period exit cleanly
  instead of polling indefinitely across sequential runs.
- **`cpm_override` flag** — `config.yaml` now accepts `cpm_override: false` (default).
  When off, Marcus spawns exactly the configured agent count, preserving the
  experimental independent variable. Set `true` to restore CPM auto-sizing.
- **Stub debt scanner** — integration task acceptance criteria now reports unresolved
  stub debt (placeholder functions, hardcoded returns) as warnings in task completion.
- **`output_paths` on Task/Subtask** — integration-wiring tasks track which files
  each task is expected to produce; orphan scan traces reachability from the entry
  point against this manifest.
- **Platform architecture diagrams** — `docs/` now includes architecture and
  experiment flow diagrams.
- **Code of Conduct** — based on the scikit-learn contributor covenant.

### Fixed
- **`~/.claude.json` pretrust race** — parallel experiments calling `_pretrust_directory`
  simultaneously caused last-writer-wins overwrites; fixed with `fcntl.flock(LOCK_EX)`.
- **tmux env inheritance** — `tmux new-session` does not inherit subprocess env; the
  `${MARCUS_URL:-...}` fallback always fired. Fix: bake the concrete URL into scripts
  at `AgentSpawner` init time.
- **DONE-task reversion (stale agent blocker)** — `report_blocker` and
  `report_task_progress(status="blocked")` had no guard against reverting a DONE task.
  Stale agents (lease expired, task completed by new holder) could call `report_blocker`
  and corrupt the board. Both functions now reject calls on DONE tasks and from agents
  that no longer hold the active lease.
- **Validation worktree path on recovery** — `_validate_task_completion` was reading
  `task.assigned_to` (the recovering agent) to locate the worktree, finding the wrong
  directory. The caller `agent_id` is now threaded through explicitly.
- **Phase 4 lease timeout** — raised from 180 s to 360 s (450 s total with 90 s
  grace) based on empirical evidence that tail-phase activities (tests, build, commit,
  push, conflict resolution) routinely produce 161–215 s gaps between progress reports.
- **Validation gate lease expiry** — `touch_lease` is now called before each
  validation LLM attempt so the lease clock doesn't expire during silent 60–120 s
  validator runs.
- **JSON fence stripping** — `_call_claude()` in `ai_analysis_engine.py` now strips
  markdown fences and surrounding prose before returning, preventing `json.loads()`
  failures when the LLM wraps its response in a code block.
- **Spec-coverage ordering** — `check_spec_coverage` now runs before
  `enhance_project_with_integration`; gap tasks are in the integration task's
  dependency list instead of racing it.
- **Lease/assignment release decoupled from correctness** — `report_blocker` and
  task completion now release the agent's assignment slot and active lease
  independently of validation or merge-conflict outcomes.
- **Lease recreation on DONE tasks** — guard added to prevent re-opening a lease
  on a task that has already reached DONE state.
- **Recovery retry counter** — retry counter is now cleared on lease recovery so
  the new agent starts fresh without inheriting the previous agent's retry budget.
- **`board_id` KeyError in dry-run** — `run_all_projects()` was printing
  `inst['board_id']` unconditionally; fixed to fall back to `db_path`.
- **SQLite `db_path` anchored to repo root** — previously relative to the caller's
  cwd; now resolved via `Path(__file__)` so parallel DBs always land in `data/`.
- **`ANTHROPIC_API_KEY` → `CLAUDE_API_KEY`** — renamed to preserve Claude Code
  subscription credentials alongside API key usage.

## [0.3.5] - 2026-04-22

**Patch release — project-scoped registration, CPM-optimal agent count, integration orphan scan.**

### Added
- **CPM-optimal agent count** — Critical Path Method query is now authoritative
  for how many agents to spawn; config entries are templates (not a cap).
  `max_agents` config key (default 12) is the safety valve. Overflow agents get
  unique IDs (`agent_unicorn_{i+1}`) and `subagents=0`.
- **Integration orphan scan** — integration task acceptance criteria now requires
  tracing reachability from the entry point; every source file must be reachable
  or explicitly documented as intentionally standalone. Validated: v89 → v90
  reduced orphaned lines from 681 to 0.

### Fixed
- **Cross-experiment task theft (GH-388)** — `register_agent` now requires
  `project_id`; agents without one are rejected. `agent_project_map: Dict[str,
  str]` added to server state; `request_next_task` filters candidates to the
  agent's registered project. Legacy tasks with `project_id=None` pass through
  unchanged.
- **Atomic registration (GH-388 P2)** — `project_id` validation now runs before
  any state mutation; failed registrations leave no ghost `WorkerStatus` entries.
- **stdio handler wiring (GH-388 P1)** — `handlers.py` dispatch now extracts
  `project_id` from MCP tool arguments and declares it in the tool schema
  `required[]` list.
- **Overflow agent ID collision** — all overflow agents previously got the same
  ID (`agent_unicorn_1`); now uniquely numbered.
- **Overflow agents inheriting template subagents** — overflow agents now always
  set `subagents=0` regardless of the template they were copied from.
- **CPM failure log** — exception type and message now logged on CPM query failure
  before falling back to template count.

## [0.3.4.post3] - 2026-04-18

**Hotfix release — SQLite startup fix; PROTOCOL.md and README corrections.**

### Fixed
- **SQLite startup blocked by missing kanban-mcp (GH-386)** — `KanbanClient.__init__`
  eagerly called `_get_kanban_mcp_path()` which raised `FileNotFoundError` even for
  SQLite, GitHub, and Linear providers that never use kanban-mcp. Path detection is
  now deferred to first use via a lazy property — non-Planka providers construct
  cleanly; Planka users get the same descriptive error at call time.
- **`ProjectMonitor` instantiated for all providers** — `MarcusServer` now skips
  `ProjectMonitor()` for non-Planka providers since it unconditionally constructs a
  `KanbanClient`. Fixes the README claim "No Docker required" for SQLite.

### Docs
- **PROTOCOL.md** — added `create_project` to Required MCP Tools table; added Two
  Agent Roles section (creator vs worker); removed runner-specific exit logic
  (`get_experiment_status`, Experiment Completion section) from the agent protocol;
  added `log_decision` and `log_artifact` to work loop steps.
- **README.md** — named `config_marcus.json` in provider setup table; fixed agent
  directory guidance (agents run from the project directory, not the Marcus directory);
  clarified `CLAUDE.md` placement.

## [0.3.4.post2] - 2026-04-17

**Patch release — Codex P2 guardrail scope fixes.**

### Fixed
- **`log_artifact` guard too broad (P2-1)** — the git-tracked-file guard
  introduced in `0.3.4.post1` blocked *all* tracked files, including legitimate
  artifact outputs under `docs/` and `tmp/` that were committed in a previous
  run. Iterative artifact refreshes must be allowed. Guard is now scoped to
  paths whose first component is NOT in `{"docs", "tmp"}` — files under those
  artifact output roots skip the check entirely. Source roots (`src/`, `lib/`,
  etc.) are still protected.
- **Smoke gate fail-open on missing workspace state (P2-2)** — `_run_product_smoke_gate`
  previously returned `None` (the "verification passed" sentinel) in three
  infrastructure-failure paths: workspace state load error, `project_root` absent
  from state, and `project_root` not a directory on disk. Integration tasks could
  therefore slip through unverified on misconfigured environments. All three paths
  now return a `smoke_gate_unavailable` rejection dict so the gate is always a
  hard stop when it cannot initialise.

## [0.3.4.post1] - 2026-04-17

**Patch release — dashboard-v82 post-mortem fixes.**

### Fixed
- **`log_artifact` must not overwrite git-tracked source files** — the tool
  now calls `git ls-files --error-unmatch` before writing and rejects the write
  with a descriptive error if the target path is tracked. Fail-open: when git
  is unavailable the guard is skipped so non-git projects are unaffected.
  dashboard-v82 post-mortem: Agent 1 accidentally overwrote `theme.css` and
  `design-tokens.json` via `log_artifact` and had to restore from git
  (commit d44dd5a).
- **Documentation task instructions: read source before documenting** —
  `build_tiered_instructions` Layer 6 now injects a "read the actual source
  file" checklist for any task whose labels include `documentation`, `docs`,
  or `readme`, or whose name contains those keywords. Detection fires on label
  OR name so it works even when labels are absent. The AI prompt template
  (`task_instructions`) gains a matching DOCUMENTATION section so LLM-generated
  base instructions reinforce the same requirement. dashboard-v82 post-mortem:
  Agent 1 documented `WeatherWidget` props from the design spec
  (`location: {lat, lon, name}`) instead of the implementation
  (`defaultLocation: string`), causing silent README/code drift.
- **`build_tiered_instructions` Layer 6 defensive guards** — label list and
  task name now use `or []` / `or ""` fallbacks so the guidance block is safe
  when `task.labels` or `task.name` is `None`.

## [0.3.4] - 2026-04-17

**The Contract-First & Agent-Agnostic release.** Contract-first decomposition
graduates from opt-in flag to default. Pre-fork synthesis, planning observability,
and integration verification close the observability gap. PROTOCOL.md documents
Marcus as an agent-agnostic coordination layer. Lease race conditions patched.

### Added
- **Contract-first decomposition — full GH-320 series** — the decomposer now
  generates domain contracts synchronously inside `create_project`, fully
  populating the board before agents spawn. Eliminates the Phase A race where
  agents grabbed tasks before design artifacts were ready. One task per contract
  boundary; boundaries are determined by interface surface, not by agent count.
  Includes: domain-keyed contract generation, Phase A→B artifact registration,
  gate (Invariant 5 + verb-coverage), Cato synthetic design ghosts, scope clamp,
  upstream intent preservation, framing layer, acceptance criteria, and scope
  annotation (`in_scope` vs `reference_only` at retrieval time). (#320, #322,
  #326, #327, #329, #330, #331, #334, #335, #336, #339, #344, #381)
- **Pre-fork synthesis** (#355, #380) — Marcus generates shared foundation
  artifacts (architecture decisions, shared contracts) as a pre-fork task before
  agents spawn, preventing the most common class of cross-agent interface mismatch.
- **Planning observability** (#355, #380) — task graph, dependency DAG, and
  decomposition rationale logged as structured events for Cato and Simon.
- **Systemic integration verification** (#346) — three-layer verification:
  static boundary analysis, cross-agent contract tracing, and doc-code consistency
  check. Catches field-name mismatches, return-type disagreements, and config
  value divergence at agent boundaries.
- **`recommended_agents` in `create_project` JSON response** — CPM-based optimal
  agent count (from `calculate_optimal_agents`) surfaced in the API response after
  decomposition. API consumers get a data-driven recommendation; no change to the
  `/marcus` skill or human-driven workflows.
- **PROTOCOL.md** — developer-facing agent protocol spec. Documents the
  register → work-loop → exit lifecycle, required MCP tools, Marcus guarantees
  (task isolation, dependency ordering, lease recovery, artifact routing, scope
  annotation), and the Three Agent Invariants. Complements `Agent_prompt.md`
  (which is agent-facing). Marcus is agent-agnostic: any MCP-compatible agent
  (Codex, Gemini CLI, AutoGen, LangGraph…) can participate.
- **Merge-conflict-aware lease extension** (#350) — when an agent's lease is
  about to expire and its worktree has unresolved git merge conflicts, Marcus
  grants a one-shot 5-minute extension (max 2 grants = 10 min total). Truth is
  the worktree git state — agents cannot self-declare or lie about conflict status.

### Changed
- **`contract_first` is now the default decomposer** — previously `feature_based`.
  Set `MARCUS_DECOMPOSER=feature_based` or pass `{"decomposer": "feature_based"}`
  to opt out. The `/marcus` skill default updated to match.
- **`agent_count` removed from contract decomposition LLM prompt** — was being
  used to set `min_tasks`/`max_tasks` in the prompt, which is backwards. Task
  count should be determined by contract boundary count (one task per boundary),
  not by user-supplied agent count. Use `recommended_agents` in the response
  instead of driving decomposition with agent count.
- **README** — `<details>` summaries clarify agent-agnostic positioning; v0.3.4
  row added to News and Milestones tables; PROTOCOL.md linked from setup steps.

### Fixed
- **Lease extension race conditions** (Codex P1 + P2 review, PR #384):
  - **P1** — if `renew_lease` wins the race during the git probe, the lease is
    no longer expired but the merge-conflict extension still fired, burning an
    extension slot against the cap of 2. Fix: re-verify `is_expired` inside the
    Phase 3 lock before mutating; re-check in `check_expired_leases` so a
    concurrently-renewed lease is not added to the recovery list.
  - **P2** — `now` timestamp was captured once at scan start; later candidates
    in a batch received shorter-than-intended extensions. Fix: capture `grant_time`
    inside the lock at the moment of grant so each candidate gets the full 300s.
- **Decomposer contract bleed** (#353, #354) — contract-first framing leaked
  into feature-based artifact generation when both decomposers were active in
  the same process. Isolated artifact context per decomposer path.
- **Integration smoke — `start_command` required** (#351) — smoke test runner now
  requires agents to declare a `start_command`; vacuous passes where no build
  pipeline was exercised are rejected.
- **Product smoke — exec → shell runner** (#352) — killed the v73 vacuous-pass
  class where the smoke script exited 0 without actually running tests.
- **Agent completion signal** (#349) — agents now receive the real completion
  signal (`is_running=false`) rather than timing out on the work loop.
- **Task reviewer-selection deadlock** (#348) — Layer 3 reviewer selection
  removed after dashboard-v72 deadlock traced to it.

## [0.3.2] - 2026-04-10

**The Observability & Attribution release.** Substantive improvements to design
autocomplete speed, cross-agent contract verification, worktree agent resilience,
and the code cleanup that clears the path for contract-first decomposition in v0.3.3.

### Added
- **Cross-agent interface contracts for design and integration** (#313) — the
  integration verification agent now explicitly traces identifiers, data shapes,
  and configuration values across agent boundaries. Catches the class of bug
  where two agents' code works in isolation but collides at the boundary: field
  names that don't match, return types that disagree with caller expectations,
  config values that differ between producer and consumer. The verification step
  uses `git log --format="%an %s"` to find boundaries between agent-authored
  files, then traces data across each boundary with raw command output as
  evidence.
- **PyPI publishing workflow** — `publish.yml` triggers on GitHub release
  published, builds via `python -m build`, verifies `pyproject.toml` version
  matches the tag, and uploads to PyPI via trusted publishing. First release
  to exercise this workflow.

### Changed
- **Design autocomplete parallelized** (#304) — the sequential for-loops in
  `_generate_design_content()` are replaced with two levels of `asyncio.gather`:
  Level 1 across design tasks, Level 2 within each task (4 artifact calls + 1
  decisions call run concurrently). Concurrency cap via `asyncio.Semaphore(10)`,
  retries via `@with_retry(max_attempts=3, base_delay=2.0, jitter=True)` from
  `src/core/resilience.py`.

  **Wall-clock impact:** a 10-task enterprise project went from ~25-33 min
  sequential to ~145s parallel (10-14× speedup, 27% of the 10-min project
  creation budget). The smoke test run caught and transparently recovered from
  15 transient Anthropic API timeouts via the retry layer — all 50 LLM calls
  ultimately succeeded despite a ~30% transient failure rate.

  **Failure semantics change:** previously the design phase would warn-and-
  continue on per-task errors. Now any unrecoverable failure aborts the whole
  design phase (fail-fast). Partial design results are worse than no results
  because they silently corrupt downstream agent work.

### Fixed
- **Worktree agent resilience** (#317) — wired up the resilience system and
  fixed recovery for worktree-based agents. Before this, failed worktree agents
  could leave the recovery system in an inconsistent state.

### Removed
- **Dead `create_project_from_natural_language` function** (#303) — 180 lines
  of orphaned code removed from `src/integrations/nlp_tools.py`. The real MCP
  entry point was `src.marcus_mcp.tools.nlp.create_project`; the old function
  had no callers in `src/` and was only referenced by tests that exercised a
  dead code path. 13 files changed, +357 / −1,899. This cleanup unblocks the
  contract-first decomposition work planned for v0.3.3 (#320).

## [0.3.1] - 2026-04-07

### Fixed
- **Duplicate project creation** — Claude's `--print` mode retried `create_project` after timeout during design phase, creating duplicate projects. Fixed with dedup guard on the MCP tool and background design phase that returns immediately. (GH-314)
- **Missing git init in spawn_agents.py** — multi-agent experiment runner now initializes git repo in `implementation/` directory, preventing first-run failures that triggered Claude Code retries. (GH-314)
- **Design task agent assignment** — design tasks now assigned to Marcus before hitting the kanban board, preventing workers from grabbing them during the background design phase.

### Added
- **Background design phase** — `create_project` returns after tasks are on the board (seconds) instead of waiting for design artifact generation (3-5 min). Workers are blocked by hard dependencies until design tasks reach DONE status. Prevents Claude timeout retries.
- **Design task persistence** — design task outcomes persisted to marcus.db with `agent_id: "Marcus"`, enabling Cato to display them in the Swim Lane planning lane.
- **Epictetus Phase 8.5** — audit reports now persist to marcus.db `quality_assessments` collection for Cato Quality dashboard integration. Best-effort; skips silently if marcus.db not found.
- **`project_id` field** in Epictetus report schema for reliable project-to-report linking.
- **Caller stack logging** on `create_project` MCP tool for debugging duplicate invocations.

## [0.3.0] - 2026-04-03

**The Multi-Agency Foundation release.** First release where two independent agents
produce a working, multi-authored product and deliver it to main. Epictetus audit
of dashboard-v48: Multi-Agency Effective: Yes, Delivery: Delivered, Coordination: B.

### Multi-Agency Infrastructure (NEW)
- **Design autocomplete** — Marcus owns all design tasks. Generates architecture docs, API contracts, data models, and architectural decisions via LLM during `create_project`. Design tasks are born DONE — no agent ever carries design authorship context into implementation. Artifacts and decisions registered via MCP tools (`log_artifact`, `log_decision`) for full observability. (GH-297)
- **Git worktree isolation** — each worker agent gets an isolated git worktree on branch `marcus/{agent_id}`. Agents work in parallel without file conflicts. Merge-on-completion after validation passes. If merge conflicts, agent is sent back to resolve — code is never lost. (GH-250)
- **Project scaffolding** — Marcus generates shared project infrastructure (package manifest, build config, entry point, placeholder files) from the architecture document. Committed to main before worktrees branch. Prevents agents from duplicating scaffolding work. Post-processing filter rejects over-generated files. (GH-300)
- **Per-task visibility** — agents run `git merge main` before each task to see all previously completed work. DAG fan-in dependencies work correctly. (GH-302)
- **Commit attribution** — `GIT_AUTHOR_NAME` and `GIT_COMMITTER_NAME` env vars per agent tmux pane. Enables `git blame` attribution and Epictetus contribution analysis. (GH-308)
- **Design principle**: "No implementation agent should ever be able to reconstruct the full system from what Marcus gave it."
- **Five research findings** on why multi-agent parallelism fails documented in GH-301: decomposition mismatch, context contamination, knowledge leakage, precision-autonomy tradeoff, scaffolding duplication.

### Added
- **SQLite Kanban provider** — local-first, zero-config; no Docker needed for basic use (GH-258)
- **Epictetus evaluation skill** — standardized rubric grading for experiments (GH-258)
- **Coordination Effectiveness analysis** in Epictetus reports (GH-263)
- **Integration verification phase** — validates task outputs fit together (GH-271, GH-286)
- **Dr. Kaia Chen AI architect skill** for architectural advice via `/kaia` (GH-287)
- **`/marcus` Claude Code skill** — one-command experiment launcher (GH-227)
- Cadence-based agent recovery using median progress intervals (GH-247)
- Phase-level timing in `request_next_task` for scheduling insights (GH-248)
- Acceptance criteria stored in SQLite DB and wired through pipeline (GH-268, GH-269)
- Agent event logging on `get_task_context` calls (GH-285, GH-288)
- TERM normalization for tmux panes in non-interactive shells (GH-291)
- Pane readiness polling before injecting commands via `send-keys` (GH-291)
- Auto-confirm Claude trust/permission prompts in tmux panes (GH-218)
- AI model info displayed in `marcus status` output (GH-262)
- Project config snapshots saved at creation time (GH-260)
- Project status metrics sourced from kanban DB (GH-266)
- Brief task instruction generation to reduce context bloat (GH-259)
- PyPI publishing as `marcus-ai` (`pip install marcus-ai`)

### Changed
- **Default provider for experiments is now SQLite** — Planka remains fully supported for existing users
- Docker is now optional infrastructure (Planka + Postgres only)
- README redesigned with News, Architecture, and Comparison sections (GH-202, GH-216)

### Fixed
- **Dual project ID mismatch** — kanban and registry generated separate UUIDs, making decisions/artifacts invisible in Cato. Now uses kanban project ID as single source of truth. (GH-306)
- **Validator worktree awareness** — WorkAnalyzer now validates in the agent's worktree, not the main repo. Prevents false validation failures. (GH-305)
- **Phase B routing** — design artifact/decision registration moved to actual MCP tool entry point (`nlp.py`), not dead code path. (GH-303)
- **Stale .git cleanup** — `run_experiment.py` removes leftover `.git` at experiment root to prevent worktrees branching from wrong repo.
- **Agent prompt updates** — removed "push commits" (no remote), removed "NEVER merge" (agents need `git merge main`), added worktree workflow instructions.
- Display estimated time in minutes instead of hours on board
- Agent startup reliability in detached tmux sessions (GH-289)
- `experiment_complete.json` now written in `end_experiment` (GH-265)
- Task instruction generation constrained to be brief (GH-259)

### Documentation
- System docs: `15-git-worktree-isolation.md`, `16-project-scaffolding.md`, `52-design-autocomplete.md`
- Research findings: GH-301 — five empirical findings on multi-agent parallelism failure
- Vision document: `~/Desktop/new_vision_plan_design_implement_flow.md`

### Notes
- `/marcus`, `/kaia`, and `/epictetus` skills require Claude Code CLI — they are not part of the Python API
- Epictetus is the foundation for a larger backpropagation-style self-learning system (see GH-255, GH-257)
- **Known limitations**: lopsided contributions (73/27 split), scaffold may over-generate types/utils (filter mitigates), CSS conflicts between agents (no shared convention yet). See v0.3.1 milestone for planned fixes.

## [0.2.1] - 2026-03-21
### Added
- Progressive timeout system with lease recovery and handoff (GH-191)
- Structured RecoveryInfo field on Task model for agent handoffs (GH-190)
- Per-lease adaptive grace periods for progressive timeouts
- Recovery info preservation across project state refreshes
- Docker improvements: updated compose config, devcontainer (GH-182)
- LLM temperature now configurable via config
- Experiment infrastructure: reorganized folder structure, project_root handling
- False positive monitoring guide and scripts
- Local development documentation (LOCAL_DEVELOPMENT.md)
- Demo materials updated for April 15 conference talk

### Fixed
- Validation loop bug: LLM returning fail with zero issues now auto-passes
- AIAnalysisEngine UnboundLocalError when config validation fails
- get_task_by_id now searches all tasks, not just available/TODO tasks
- Design tasks exempt from deployment keyword filter
- Handle None completion_criteria in validation prompt builder
- docker-compose env vars preventing Marcus from starting
- .env file loading in marcus startup script
- WorkAnalyzer test failures and test suite updates (GH-187)
- All unit test fixtures updated to mock config/LLM dependencies

## [0.2.0] - 2026-03-16
### Added
- Feature completeness validation system with AI-powered analysis (GH-170)
- Centralized configuration system with validation and environment support (GH-162)
- Composition-aware task completeness validation (GH-160)
- Composition-aware PRD extraction with specificity detection (GH-159)
- AI-powered task completeness validation with automatic retry
- Project management utilities and telemetry planning tools (GH-167)
- Demo materials: presentation decks, scripts, and audio transcriptions
- Single-agent trial runner for controlled experiments
- Enhanced task classifier with weighted signal scoring and confidence levels
- Protection for integration requirements from complexity filtering (GH-163)
- WorkAnalyzer for source code validation
- Pre-completion validation checks to agent workflow
- Hybrid test discovery strategy for validation

### Changed
- Simplified validation to single-tier intent system (GH-166)
- Simplified MCP tool logger to activity tracker (GH-164)
- Task completeness validator is now AI provider-agnostic
- Agent retry wait times reduced for better parallelization (GH-177)
- Integrated Marcus error framework and resilience patterns into validation

### Removed
- Integration requirements extraction from PRD analysis (GH-165)
- Duplicate documentation enhancement call in NLP pipeline

### Fixed
- Task classifier misclassification causing phase enforcement failures (GH-180)
- Strong signals (task name + labels) now override weak signals (description keywords)
- Database connection tasks correctly classified as IMPLEMENTATION vs INFRASTRUCTURE
- Code comment tasks correctly classified as DOCUMENTATION vs IMPLEMENTATION
- Config loading and project-local database path with tilde expansion (GH-171)
- MCP tool logging and workflow enforcement (GH-172)
- Validation retry tracking order to prevent premature blocker creation
- Code review issues from validation refactoring
- Security and code quality issues across codebase

## [0.1.3.1] - 2026-03-09
### Fixed
- Validation retry tracking order to prevent premature blocker creation (GH-170)

## [0.1.3] - 2026-03-08
### Fixed
- Security and code quality issues
- Validation system final updates (GH-170)
- Task label fetching from Kanban to use filtered labels

### Added
- Comprehensive resolution summary for Issue #170
- Pre-completion validation checks to agent workflow
- Runtime test execution to catch configuration issues
- Detailed logging to validation gate

## [0.1.2] - 2026-03-07
### Added
- Hybrid test discovery strategy for validation
- WorkAnalyzer for source code validation (GH-170)
- Phase 2 validation gate integration (GH-170)
- Retry tracking with comprehensive tests

### Changed
- Integrated Marcus error framework and resilience patterns into validation

## [0.1.1] - 2026-03-06
### Added
- Initial validation system framework
- Basic task completion verification

### Fixed
- Various bug fixes in agent coordination

## [0.1.0] - 2026-03-01
### Added
- Initial beta release
- Multi-agent coordination platform
- MCP protocol support
- GitHub and Planka Kanban integrations
- Agent registration and task assignment
- Context sharing through artifacts
- Progress reporting system
- Blocker handling workflow

[0.3.0]: https://github.com/lwgray/marcus/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/lwgray/marcus/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/lwgray/marcus/compare/v0.1.3.1...v0.2.0
[0.1.3.1]: https://github.com/lwgray/marcus/compare/v0.1.3...v0.1.3.1
[0.1.3]: https://github.com/lwgray/marcus/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/lwgray/marcus/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/lwgray/marcus/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/lwgray/marcus/releases/tag/v0.1.0
