# The extension contract: what an extension may take from the kernel

## What this is, briefly

Marcus is a coordination kernel: a shared task board that AI agents pull work from, plus the rules that decide what is claimable, what counts as done, and what gets measured.
An *extension* is a separate program that puts work on that board and runs agents against it for one purpose, such as verifying AI citations or building software.
ADR-0013 decided that Marcus is the kernel and everything else is an extension.
This document is the written contract between the two: the exact kernel surface an extension may depend on, what each piece is for, and what the first extension cut taught us.

It was written from the first cut, the AI-citation verifier (`extensions/citation_verifier/`), packaged on 2026-11-07 after Larry chose it as the first extension (ADR-0013 K11).
The numerical-report verifier is next, once its owner rebases it off its unmerged branch.
The software-building extension is the large cut this contract is meant to constrain.

## Glossary

| Term | Meaning |
| --- | --- |
| kernel | Marcus's own code under `src/`: the board, the registry, the MCP server and tools, the lease and evidence rules |
| extension | a package outside `src/` that seeds a board and runs pull-loop agents for one purpose |
| MCP | Model Context Protocol, the wire protocol agents use to call Marcus's tools (`register_agent`, `request_next_task`, `report_task_progress`) |
| seeder | the extension's program that creates the project and its cards before any agent runs |
| worker | the extension's pull-loop agent process; it holds no kernel code, only an MCP client |
| `ProjectConfig` | the project record in `src/core/project_registry.py`, where ADR-0013 K2 says an extension's task-type declaration lives |
| typed-evidence gate | the kernel's check, at close, that a task of a registered type reported the evidence its type requires |
| pull-mode verdict | a verification result that reaches the kernel because an agent pulled a verify task and reported the verdict as evidence, never because the kernel called into the extension |

## The surface, exactly

An extension imports Marcus's code through **one module of its own**, and that module imports **only** the symbols below.
For the citation verifier that module is `extensions/citation_verifier/citation_verifier/kernel.py`, and `tests/unit/extensions/citation_verifier/test_kernel_surface.py` fails if any other module imports `src.` or if `kernel.py` imports anything not on this list.

| Symbol | From | What the extension uses it for | MCP equivalent today |
| --- | --- | --- | --- |
| `ProjectRegistry`, `register_sqlite_board` | `src/core/project_registry.py` | register a directly seeded SQLite board as a project, idempotently on `(project_id, db_path)`, with the legacy UUID-key migration (issue #741, GH-306) | `add_project(provider="sqlite", config={project_id, db_path})` (#888); honours the caller's `project_id` (#844); no migration of pre-GH-306 entries |
| `SQLiteKanban` | `src/integrations/providers/sqlite_kanban.py` | create the seeded cards in one in-process pass, each with its final id, `task_type`, `inputs`, labels and dependencies (up to 800 cards for a full run) | `create_project(options.tasks=[...])` seed specs (`src/integrations/seeded_tasks.py`), which carry `name`, `labels`, `depends_on`, `estimated_hours`, `priority`, but **not** `task_type`, `inputs` or a caller-supplied `id` |
| `write_task_metadata_rows` | `src/core/persistence.py` | the sanctioned write of the `task_metadata` rows Cato renders (issue #741 step 3) | none |
| `Inspector` | `src/worker/inspector.py` | the MCP client the workers and the exporter use to talk to a running server | it *is* the MCP client; the workers call `register_agent`, `request_next_task`, `report_task_progress` and `export_case` through it |
| `_extract_text_from_result` | `src/worker/inspector.py` | read the text of a tool reply in the exporter | a private helper; a smell, named here rather than hidden; the client should expose it publicly |

Everything else the extension does, it does on its own: fetching pages, deciding verdicts, rendering answers, grading, exporting.
The workers import zero kernel code directly; they reach `Inspector` through the shared shell `worker_common.py`.

## The K2 question, answered: in-process record, or MCP write path?

ADR-0013 K2 left one implementation question open: does an extension construct its `ProjectConfig` in-process, or write through an MCP path?
Packaging the citation verifier answers it with evidence rather than preference.

**Today an extension must construct in-process, because the MCP write path is incomplete in three specific places.**

1. **Seeding cannot carry the task type.** The citation chain's whole design is four task types (`find`, `verify`, `approve`, `synthesize`) with per-type evidence rules and offer-time eligibility.
   `create_project(options.tasks)` seed specs have no `task_type` field, so every seeded card would be an `implement` card and none of the kernel's verification rules would apply.
2. **Seeding cannot carry the inputs.** Each `find` card carries the excerpt and its metadata in `inputs`; the verify card's evidence is computed from them.
   Seed specs have no `inputs` field.
3. **Seeding cannot carry a caller-supplied id, and metadata rows have no tool.**
   The exporter finds a case's cards by their ids and labels (#844), and Cato renders cards from `task_metadata` rows that only `write_task_metadata_rows` writes.

Registration alone is already covered: `add_project(provider="sqlite", config={project_id, db_path})` does what `register_sqlite_board` does for a new registry, and the one thing it lacks (migrating a pre-GH-306 UUID-keyed entry) matters only to registries created before 2026-11-05.

**What the software-building cut must satisfy, therefore:** either it keeps a one-module in-process surface like `kernel.py` (acceptable, auditable, the pattern this cut proves), or the kernel first closes the three MCP gaps above: `task_type`, `inputs` and `id` on seed specs, and a tool or a `create_project` option that writes `task_metadata` rows.
Closing them is small (the fields exist on `SQLiteKanban.create_task` already; the gap is the seed-spec schema and validator), and would let an extension run on a different machine from the server with no kernel import at all.
That is a decision for Larry, recorded here as the two options with their consequences; it is not made by this document.

## What packaging exercised, and what it did not

The extraction scope for software-building (`docs/architecture/software-extension-extraction.md`) carries an optional branch of 8 to 12 engineer-days for a "pull-mode verdict return path" that, it says, does not exist anywhere in Marcus today.
Packaging the citation verifier is evidence about that branch.

**Exercised, on every task close, in the live rehearsal:** the kernel's typed-evidence gate.
Every `report_task_progress` the finder and verifier send carries `evidence`, the seeded cards are typed `find`, `verify` and `synthesize`, and those are exactly the types the gate checks at close (`_apply_typed_evidence_gate`, `src/marcus_mcp/tools/task.py`).
The `approve` card went through the human approval gate (`src/cli/approvals`).
Thirteen of thirteen cards closed through those gates with zero kernel callbacks into extension code.

**So the pull-mode verdict path does exist, for verification done by an agent.**
The citation chain's verifier is itself a pull-loop agent that claims a `verify` card, computes the verdict, and reports it as evidence; the kernel never calls the verifier, it only checks the shape of what came back.
That is the pull model ADR-0013 K4 and K8 require, running in production since October.

**Not exercised:** the software-building validation gate (`src/ai/validation/`), which today runs in-process at completion and never has a card of its own.
Packaging the citation verifier therefore does not tell us that gate can move behind a shim; it tells us the alternative is already built and proven: make software-building's verification a `verify` card owned by a verifier agent, as the citation chain does, and the "missing mechanism" is not missing.
Whether software-building keeps an in-process gate behind a shim or adopts verification-as-a-card is the choice behind the 8 to 12 days, and it is Larry's.

## How a second extension uses this contract

1. Put every kernel import in one module and copy `test_kernel_surface.py` with your own declared list.
2. Register the project with `register_sqlite_board` (or `add_project` when the three gaps above are closed).
3. Seed cards with their `task_type` and `inputs`, and declare the types on `ProjectConfig.task_type_registrations` (K2); the citation verifier does not yet declare them and relies on the kernel's literal fallback for `find`, `verify` and `synthesize`, which is a follow-up, not a precondition.
4. Run workers as MCP clients only.
5. Prove it live the way `~/marcus-experiments/citation-extension-live-check/run_rehearsal.sh` does: a real server, the real transport, every card closed through the kernel's gates, zero spend.

## Related

- ADR-0013 K1 (what the kernel is), K2 (how an extension registers), K4 and K8 (the pull model), K11 (verification first): `docs/architecture/adr/0013-kernel-and-extensions.md`.
- The extension guide: `docs/architecture/building-an-extension.md`.
- The software-building extraction scope: `docs/architecture/software-extension-extraction.md`.
- Issues #741, #844 (sanctioned seeding paths), #888 (`add_project` accepts sqlite), #831 (K2 registration field).
