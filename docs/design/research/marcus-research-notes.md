<!-- Research notes, September 2026. -->

# Marcus research notes: 5 September 2026

Working notes behind "The Life of Marcus" (same project folder).
Source of truth: `painted-porch/marcus` at `develop` a4236573 / `main` a95eb4e9, plus the full `gh issue list` / `gh pr list` export of 2026-09-05 (444 issues, 282 PRs).
Code was treated as truth over docs.

## 1. Corpus profile

- Issues: 444 (285 open / 159 closed), #1–#736, 2025-07-15 → 2026-09-05. All authored by lwgray.
- PRs: 282 (264 merged / 15 closed / 3 open: #734 launcher design, #577, #580). 270 by lwgray; treble37 6, aak540114 4, TheRealGchen 1, CrepuscularIRIS 1.
- Monthly issues opened / PRs merged: Jul-25 6/3 · Aug 11/0 · Sep 30/2 · Oct 35/12 · Nov 29/20 · Dec 7/12 · Jan-26 1/0 · Feb 0/0 · Mar 85/26 · Apr 110/68 · May 109/98 · Jun 9/4 · Jul 1/5 · Aug 9/11 · Sep 2/3.
- Labels: enhancement 166, bug 100, architecture 94, pycon_2026 77, refactor 43, tech-debt 42, build-blocker 26 (19 open), session-migration 2 (#706, #730).
- Milestones: v0.3.9 31 open / 7 closed (was due 2026-07-23); v0.4.0 17 open; v0.5.0 9 open; v0.6.0 17 open; v0.7.0 5 open; v0.8.0 3 open.
- Git: first commit 2025-06-15 ("PM Agent"); 1,113 commits on develop; tags v0.1.1 … v0.3.8 (2026-05-23). No v0.3.3, v0.3.4, v0.3.4.post2 tags. Zero commits Jan–Feb 2026.
- GitHub (2026-09-05): 14 stars, 11 forks. PyPI `marcus-ai`: name registered, zero released files (documented install is clone + editable install, which works).

## 2. Source architecture (develop)

- `pyproject`: name `marcus-ai`, version 0.3.8, Python ≥3.11, MIT, "Beta". Console script `marcus = src.marcus_mcp.server:cli_main` (server only); separate root `./marcus` daemon script (start/stop/status/board; `--version` prints "Marcus 2.0.0").
- `src/`: 239 files, 123,517 lines. `marcus_mcp/` 30.9k (server, handlers, tools, coordinator), `integrations/` 20.8k, `core/` 19.8k, `ai/` 15.1k, `modes/` 4.9k, `cost_tracking/` 4.4k, `analysis/` 4.0k, `intelligence/` 3.1k, `config/` 2.4k … `ttt2/` 1,241 lines of tic-tac-toe committed by an experiment agent.
- God files: `tools/task.py` 6,802; `advanced_parser.py` 5,443; `nlp_tools.py` 4,931; `server.py` 3,396; `outcome_coverage.py` 2,613; `assignment_lease.py` 2,251; `work_analyzer.py` 2,196 … 24 files >1,000 lines. All #422–#441 splits open.
- Transports: stdio (`handlers.py` dispatcher, 13 tools), HTTP FastMCP (default, 17 agent tools), multi (4298 human / 4299 agent / 4300 "Seneca"). Three registration paths already diverge (stdio drops `verifications`/`evidence`).
- Agent tools on default endpoint (17): create_project, end_experiment, get_desired_agent_count, get_experiment_status, get_optimal_agent_count, get_task_context, list_project_history_files, log_artifact, log_decision, ping, query_project_history, register_agent, report_blocker, report_task_progress, request_next_task, request_task_redo, start_experiment. NOT exposed: get_project_status, get_agent_status (which `prompts/Agent_prompt.md` tells workers to call). 14 `pipeline_*`/`what_if_*` names in `tool_groups.py` are phantoms.
- `request_next_task` (`task.py:2180–3040`): lease monitor bootstrap → one-task-per-agent guard → `find_optimal_task_for_agent` (subtasks first, phase enforcer, AI-powered selection) → sweep BLOCKED merge conflicts via rebase → dependency awareness + memory predictions + tiered instructions → file locks → kanban IN_PROGRESS → assignment persisted, git baseline captured, lease + `lease_epoch` returned; no-task path returns constant `retry_after_seconds=30` and `EXPERIMENT_COMPLETE`.
- `report_task_progress` (`task.py:4057–5351`): epoch observed not enforced → stale-completion guard/re-grant (refuses non-holders since #731) → validation gate (WorkAnalyzer on git delta; auto-pass after 3 failures) → product smoke gate for integration tasks (self-verify: evidence optional; `start_command` never executed) → honesty stamp logged only → `_verify_agent_has_commits` → release lease → merge `marcus/<agent_id>` to main (conflict → BLOCKED + `_attempt_merge_recovery`) → memory record.
- Leases: defaults 240 s lease, 180–360 s bounds, 60 s grace; progressive timeouts; circuit breaker 3 silent recoveries → BLOCKED (#707); persisted `assignments.json` + `task_epochs.json` (#729/#731).
- Kanban providers: SQLite (real, tested, default); Planka (needs sibling `~/dev/kanban-mcp` Node project, hard-coded path); GitHub (abstract, 4 methods unimplemented, cannot instantiate); Linear (complete interface, zero tests).
- Cost tracking: `cost_store.py` (`runs`, `token_events`, `model_prices`, `v_event_cost`), `PlannerContext` on every tool call, `worker_ingester.py` (called from Cato, not from Marcus). Maintainer's local `costs.db` (Aug 2026): 971,296 worker rows, ~68.9B tokens, 356 distinct agents in May 2026.
- Memory: `core/memory.py` wired (predictions in request_next_task, outcomes on completion); `server.py:302` imports non-existent `src.core.memory_enhanced`; `src/learning/`, `ai/learning/`, `modes/`, `enhancements/`, `core/adaptive_dependencies.py` dormant.
- Runner: `dev-tools/experiments/runners/run_experiment.py` (live entry) → `spawn_agents.py::AgentSpawner` (2,176 lines; `main()` dead), tmux session, creator pane calls `create_project`, control loop polls `get_desired_agent_count`, spawns one ephemeral agent per task in `worktrees/<agent_id>` on `marcus/<agent_id>`; `spawn_controller.py` StallWatchdog + SpawnThrashDetector; `harness.py` registry {claude, codex, gemini}. Legacy `experiments/` tree (1,616 lines) is dead and functionally broken.
- Tests: 346 files, 4,909 test functions; only 165/301 unit files carry `@pytest.mark.unit`, so CI's `pytest -m unit` skips ~half; `pytest.ini` overrides pyproject's 80% coverage gate; CI red on every branch 2026-08-14 → 2026-09-04 (unpinned mlflow, fixed #733); Claude review action dead since 2026-07-05 (401); Codex connector is the only live reviewer.
- develop vs main: 81 commits by count (50 after v0.3.8 squash), 138 files, +23,418/−2,509. Themes: lease persistence/ownership (#729, #731, #732), blocker ladder + redo (#712, #718, #720, #722, #723), verification (#697, #679, #684), test isolation (#725, #726), ADR 0012 (#708, #710), runner fixes (#702, #704, #711).

## 3. Docs-vs-code reconciliation (headline items)

1. README install path (clone + `pip install -e .`) is correct and works. Stale: CHANGELOG v0.3.0/v0.3.2 claim PyPI publishing (`pip install marcus-ai`) and a `publish.yml` workflow, no such workflow in `.github/workflows/`, PyPI name has zero files; #312 (open) already says pip cannot run projects.
2. `.env.example` overwritten 2026-05-18 with `HANGMAN_*` lines.
3. Two contradictory worker prompts: `prompts/Agent_prompt.md` (continuous loop) vs `dev-tools/experiments/templates/agent_prompt.md` ("one task, then EXIT"); Marcus's injected instructions step 9 says "Immediately request next task."
4. Smoke gate tool description (`server.py:~1446/1683`) says agent MUST declare verifications/start_command and Marcus rejects otherwise; `_run_product_smoke_gate` (`task.py:1240`) says absence never blocks; `start_command` unused.
5. Invariant #2 v2 (CLAUDE.md, 2026-05-24: verification belongs to Marcus) reversed in code by PR #679 (2026-05-30: verification lives with the agent); `independently_verified` flag never set in responses.
6. `marcus status` says 10 tools; endpoint has 17; `get_project_status`/`get_agent_status` absent from agent endpoint.
7. Providers: GitHub uninstantiable; Linear untested; Planka needs undocumented sibling project.
8. Telemetry: doc says opt-in; code default `enabled=True` (opt-out); not mentioned in README/CLI.
9. `marcus --version` → "2.0.0"; pyproject 0.3.8.
10. Retry "60% of ETA" (VALUE_PROPOSITIONS) → constant 30 s.
11. "Same project name → Marcus finds it" (user guide) → `create_project` "ALWAYS creates a new project."
12. `memory.use_v2_predictions` → imports missing `memory_enhanced`; crashes.
13. Learning/pattern systems documented as integrated → dormant.
14. ROADMAP dates passed; v0.5.0 (MFP, rewind, pending_review) has no code; ROADMAP self-contradicts on shipped version; ROADMAP "open debt" item (scope annotation) is actually implemented.
15. Docs tree: 346 pages; 51% untouched since 2025-09; 1 page touched since v0.3.8; 9 Sphinx stubs point at deleted modules; CHANGELOG 0.1.x entries backdated (created 2026-03).
16. Undocumented shipped features: request_task_redo, lease_epoch, blocker severity ladder, repair-requeue, end-of-run report, get_cost_summary, `marcus board --watch`, `MARCUS_DECOMPOSER`.

## 4. Session-model migration status (#706 / ADR 0012)

- Phase 1 complete 2026-08-10 (#667 circuit breaker, #700 bridge fix, #676, #627, #629 ladder, #624). Addendum: #719/#720, #722, #723, #724/#725, #726.
- Phase 2 ADR 0012 accepted 2026-07-05; D11 added 07-06; D11 deferred 08-15 (three failed adversarial passes; contradicts documented lease re-grant compensation; depends on D3).
- PRE-3 gate 1 (call-site inventory) closed 2026-08-10: 495 sites / 31 files; 11 migration changes M1–M11; nine plan-changing findings (M2 has a second entry point on the claim path; retry_after clamp; two prompts; renew_lease ownerless; O4 premise false; ADR pointers stale; M7 may be a rename; M6 is a cross-repo contract with Cato via `worktrees/<id>` path shape; O1 never cleaned up).
- PRE-3 gate 2 (D11 fence) closed as DEFERRED 2026-08-31; PR #731 shipped ownership hardening + epoch recorded-not-enforced.
- Unplanned prerequisites shipped: #729 lease persistence; #732 retry_after pinned.
- New blocker: #730 merge by commit range (blocks D1 and merge re-home; not the launcher).
- Status 2026-08-31: "Zero Phase-3 checklist items started." Targets missed: Phase 3 2026-08-02.
- PR #734 (2026-09-05, open): launcher design. Order proposed: O1 restart fix → launcher from `run_experiment.py` with `--sessions N` → delete one-task prompt, add drained-stop + checkpoint duty → jitter → run 3 workers. Three decisions posed: idle vs go home (lean go home); restart dead workers (lean no); O1 before launcher (lean before).
- Phase 4 items open: #677, #643, #636, #638, #693. Phase 5 re-triage pending seam definition. Phase 6 gate: 5 consecutive unattended Shape-B passes.

## 5. test124 (2026-09-04): #735 / #736

- Run: `/marcus build a snake game`, contract_first, prototype, Haiku 4.5, 13 tasks, 15 workers. Reported 13/13 complete. Dev server renders blank (TS interface re-export as value); 38/265 tests fail; verifier claimed 230 passing.
- Gate chain: self-verify (#679) makes evidence optional; web judge passes any ≥10-char string containing `<`; `independently_verified=False` only logged.
- Cost: 46.0M input tokens, 608 calls, $7.87 Haiku (97.8% cache reads; ≈$45 on Fable 5.1). 11% of tool calls post-completion merge remediation; project-wide criteria on every task; phantom artifact (8 agents hit missing file); contract sent twice; ~1K chars filler per task; runner prompt contradicts instructions; 4 idle spawns; MLflow empty.
- Proposed fixes ranked in #735 (require evidence; served≠rendered; Marcus-run headless browser; surface self-report; isolatedModules tripwire; kill orphaned servers) and #736 (record usage; scope criteria; domain-key boilerplate; contracts reference only main; pin shared config; send contract once; remove contradiction/filler; merge earlier; session model).

## 6. Landscape (Sept 2026): verified vs reported

Verified against primary sources: Cursor "Agent swarms and the new model economics" (2026-07-20: $1,339 hybrid vs $10,565 single frontier; workers 69–90% of tokens; merge conflicts 70k+ → <1,000; split-brain/planner contention/megafiles/ossification).
OpenAI Symphony (Elixir, Apache-2.0, 25.9k stars, Linear-board-driven Codex orchestration, "low-key engineering preview").
CooperBench (Stanford/SAP, Jan 2026: ~25% two-agent success, ~50% drop, ≤20% budget on messaging, 63% expectation misalignment).
Destefanis & Aste, "When Agents Coordinate" (arXiv 2608.16801, 2026-08-17: 1,902 runs; messaging ~quadratic in team size; shared files −42% output tokens at 8 agents; coordinator "no reliable improvement"). painted-porch/marcus: 14 stars.

Reported via secondary coverage (not primary-verified): Claude Code Agent Teams (experimental, Feb 2026; shared task list + mailbox; 4–7× solo token use); Codex app worktrees/subagents/`/goal`; GitHub Agent HQ; Factory Missions (16.5 h, 185 runs, 82 validators); Managed Agents "Outcomes"; Gas Town ~18k / Beads ~27k stars; Paperclip ~80k; Vibe Kanban shutdown (Apr 2026, "couldn't find a business model"); Amazon-led single-vs-multi-agent cost paper; METR horizon 1.1.

Sources list: see Part VI of the documentary and the landscape report URLs (cursor.com/blog/agent-swarm-model-economics; github.com/openai/symphony; cooperbench.com/blog/curse-of-coordination; arxiv.org/abs/2608.16801; github.com/steveyegge/gastown; github.com/steveyegge/beads; github.com/paperclipai/paperclip; github.com/BloopAI/vibe-kanban; github.com/andyrewlee/awesome-agent-orchestrators; factory.ai/news/missions-architecture).

## 7. Recommendation recorded in the documentary

Stay on Phase 3, shortened and reordered: (1) O1 restart-rehydration PR first; (2) launcher from `run_experiment.py` with `--sessions N`, keep `worktrees/<id>` shape, keep AGENTS.md/GEMINI.md writing; (3) delete one-task prompt, add drained-stop + checkpoint duty, fix #736 contradiction; (4) jitter; (5) run 3 workers, restart Marcus mid-run.
Defer merge re-home, D1/#730, D3 retune, D11 until that run exists.
In parallel: #735 fixes 1, 2, 4 (require evidence; served≠rendered; surface self-report).
Independently: an honest-docs release now (strike stale CHANGELOG PyPI claims; `.env.example`; one prompt; truthful gate description; 17 tools; providers marked unsupported).
Time-box ~6 weekends to "three workers, one project, restart mid-run"; then run #529 three-arm coordination-tax experiment and decide product vs research-instrument path.
Shed dead code (legacy `experiments/`, `ttt2/`, dormant modules, duplicate Planka clients, phantom tools); close or merge #577/#580.
