# Design: The Composition–Verification Loop (#677)

## What this is, briefly

Marcus coordinates independent AI agents that each build one piece of a
project. A late task — **composition** — wires those pieces into one runnable
product. Today composition declares itself "done" after `npm run build` exits 0
and `curl` returns HTTP 200. Neither observes whether the assembled product
**actually works**, so a blank-page web app (a client-side `process is not
defined` error) shipped "green." (See `snake-pr667-5`, 2026-05-29.)

## The core correction

Composition and behavior-verification are **one loop, not two slices.** A
composer cannot *know* it wired the pieces correctly without running the
assembled thing and observing it work — and "run it and observe it works" *is*
the verification. So the per-app-type definition of "works" is **not** a
separate thing to author; it is the composer's **stop condition**.

This supersedes the earlier (wrong) framing that split the work into "author the
success criterion" and "fix composition" as two slices.

## The loop

```
Marcus authors the bar (WHAT "works" means, per app type)
        │
        ▼
Composition agent (HOW — owns implementation):
   READ   the assembled implementations + the contract (entry point, wiring)
   WIRE   them into the entry point; fix interface / import / config mismatches
   RUN    it the way appropriate to its type — derived from reading the code,
          so the entry point / port / command are real, not guessed
   OBSERVE the actual behavior and CAPTURE EVIDENCE
   CHECK  the evidence against Marcus's bar
   FIX → repeat (bounded) if it fails
   SUBMIT the evidence when it passes
        │
        ▼
Gate judges the EVIDENCE against Marcus's bar (not exit-0 on an agent command)
```

### Who owns what (Invariant #2 v2)

- **Marcus authors the bar** — the observable success condition, per app type,
  at the outcome level. No file paths, libraries, commands, or ports. Marcus
  already classifies the project (`structural_category`) at setup
  (`advanced_parser.py`), and each in-scope `UserOutcome`
  (`outcome_extractor.py`) already carries an observable `success_signal`.
- **The agent owns the doing** — reads the code, wires it, decides *how* to run
  and observe it, fixes mismatches. Two agents could wire and run it differently
  and both clear the same bar → **passes the Bright Line → coordination, not
  control.**
- **The gate judges the evidence**, not the command. This is the anti-#636
  guard: the agent cannot substitute a weak check (e.g. `curl → 200`) for the
  bar, because Marcus checks the captured evidence against the criterion. The
  agent runs; Marcus grades.

## The per-type "works" bar (domain-general, not web-only)

| `structural_category` | "It works" means (Marcus's bar) | Evidence the agent captures |
|---|---|---|
| web app / game | loads and renders a non-empty UI, no console error | headless DOM snapshot (non-empty) + console log (no errors) |
| data pipeline | run on sample input → output produced + matches expected schema/values | the output file/rows (diff vs expected / schema check) |
| CLI tool | documented command runs → expected stdout / exit / side-effect | captured stdout + exit code |
| library | import + call public API → expected behavior | a tiny driver's output asserting `fn(x) == y` |
| API service | start it → a real request returns a correct-shaped response | the response body (shape/content check), not just 2xx |
| ML / AI | model loads + predicts on sample → plausible output | prediction shape/range on a fixture |
| other / unknown | run the deliverable, confirm the documented behavior | the documented observable side effect; else fall back to today's generic guidance |

The `other`/`unknown` fallback guarantees no regression for unclassified
projects.

## What changes in code

| File:func | Change |
|---|---|
| `src/ai/advanced/prd/advanced_parser.py` / `src/integrations/nlp_tools.py` | thread `structural_category` (already computed) into composition + integration synthesis |
| `src/integrations/composition_synthesis.py:_build_composition_description` | replace "build + `curl` 2xx" with the read→wire→run-for-type→capture-evidence loop, keyed by `structural_category`; state the bar, ask for evidence |
| `src/integrations/integration_verification.py:_render_outcomes_section` | per-outcome guidance becomes "evidence against the bar," domain-keyed (not a lone `curl` example) |
| `src/marcus_mcp/tools/task.py` (composer + product smoke gates) | judge the submitted **evidence** against the bar; for web, check the DOM/console evidence, not `curl → 200`; for pipeline, check the output |
| (folds in #678's deferred pieces) | bounded fix-retries; on persistent failure, write a remediation artifact (what failed against the bar) and block cleanly |

## How to validate

1. **Web (the snake regression):** a build-clean app with a client-side render
   error must **fail** the loop (evidence: empty DOM), and a working one passes.
2. **Non-web (required, proves it's not web-only):** a data pipeline that builds
   but produces wrong/empty output must **fail**; correct output on sample input
   must **pass** — with no web assumptions anywhere in the path.
3. A CLI and a library each verified by running / importing, not by HTTP.

## Resolved decisions (implemented)

1. **Who runs the browser → (a) agent-runs-and-submits.** The agent loads the
   app in a headless browser (its choice of tool), captures a DOM snapshot +
   console log, and submits them in the `evidence` field of
   `report_task_progress`. Marcus's gate asserts a non-empty rendered DOM + no
   console errors. The analogous "agent runs, Marcus checks output" holds for
   non-web types. This keeps the run mechanics with the agent (Invariant #2)
   while Marcus owns and judges the bar. Implemented in
   `src/integrations/behavior_evidence.py` (`judge_behavior_evidence`) and wired
   into `_run_product_smoke_gate`.
2. **Evidence format → a per-type `evidence` dict, judged mechanically.** Keys by
   type: web → `dom` + `console_errors`; data pipeline → `output` /
   `output_rows`; CLI → `exit_code` + `stdout`; library → `import_ok` +
   `call_result`; API → `status` + `body`; ML → `prediction`. The task
   description (composition + integration) states exactly what to submit, keyed
   by `structural_category`. The judge lives in `behavior_evidence.py`.
3. **Tool-agnostic, not a tooling registry.** Marcus owns *what evidence proves
   the outcome* and judges it; it never names a tool, file, port, or library.
   This honors the `VerificationSpec` "coordination, not a tooling registry"
   principle. (Simon thought logged to revisit whether Marcus should later be
   granted tool-running rights for option (b).)
4. **Bounded fix-retries — deferred to the #678 ceiling.** The
   missing-verifications escalation ceiling (PR #678,
   `MAX_SMOKE_MISSING_VERIFICATION_ATTEMPTS`) is the floor that stops gridlock;
   a behavior-evidence-specific retry ceiling is a follow-up.
5. **Non-web guard — covered by tests.** The bar stays outcome-level; tests
   include data-pipeline and CLI cases asserting no web (`dom`/`console`/`curl`)
   assumptions leak into non-web descriptions or judgments
   (`tests/unit/integrations/test_behavior_evidence.py`,
   `test_composition_task_synthesis.py`, `test_integration_verification.py`,
   `tests/unit/marcus_mcp/test_behavior_evidence_gate.py`).

## What shipped

| File | Change |
|---|---|
| `src/integrations/behavior_evidence.py` (new) | per-type evidence contract + judge; tool-agnostic; fuzzy types fall back to legacy |
| `src/integrations/composition_synthesis.py` | `build_composition_task` / `_build_composition_description` take `structural_category`, append the behavior-evidence step, stash the category on `source_context` |
| `src/integrations/integration_verification.py` | `create_integration_task` / `enhance_project_with_integration` take `structural_category`, render `_render_behavior_evidence_section`, stash the category on `source_context` (always) |
| `src/integrations/nlp_tools.py` | thread `self._project_structural_category` into both synthesis call sites (defensive `getattr`) |
| `src/marcus_mcp/tools/task.py` | `_run_product_smoke_gate` + `report_task_progress` accept `evidence`; the gate judges it via `judge_behavior_evidence` before any subprocess; a passing judgment satisfies the gate (no legacy `start_command` required); outcome-bearing tasks still run `verifications` coverage |
| `src/marcus_mcp/server.py` | both `report_task_progress` tool registrations accept + forward `evidence` |

## Related

- #677 — this is its substance (verify behavior, not build).
- #678 — shipped: stops the gridlock when verification can't complete (the floor
  that makes requiring this loop safe).
- #463 — created the composition task for the "builds but renders empty" failure
  this loop finally closes; #654 / #636 — same root (build/serve ≠ behavior).
- Invariant #2 v2 (`CLAUDE.md` MULTIAGENCY_PROCLAMATION) and the Simon decision
  logged for the criterion/mechanics split. Reviewed by Kaia.
