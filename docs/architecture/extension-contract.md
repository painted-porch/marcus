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

| Symbol | From | What the extension uses it for |
| --- | --- | --- |
| `Inspector` | `src/worker/inspector.py` | the MCP client the seeder, the workers and the exporter use to talk to a running server; the seeder calls `create_project`, the workers call `register_agent`, `request_next_task` and `report_task_progress`, the exporter calls `export_case` |
| `extract_text_from_result` | `src/worker/inspector.py` | read the text of a tool reply (public since #910; the private name it replaced was the one smell on this table) |

That is the whole surface since 2026-11-13.
Everything else the extension does, it does on its own: fetching pages, deciding verdicts, rendering answers, grading, exporting.
The workers import zero kernel code directly; they reach `Inspector` through the shared shell `worker_common.py`.

### What the surface was before the MCP-only write path

The first cut (2026-11-07) also imported `ProjectRegistry`, `register_sqlite_board`, `SQLiteKanban` and `write_task_metadata_rows`, because the seeder wrote the board in-process.
Those four left the surface when #911 closed the seed-spec gaps below and the seeder moved to one `create_project` call.
`add_project(provider="sqlite", config={project_id, db_path})` (#888) remains the MCP way to register an already-seeded SQLite board; the citation verifier no longer needs it because `create_project` mints the record.

## The K2 question, answered: the MCP write path

ADR-0013 K2 left one implementation question open: does an extension construct its `ProjectConfig` in-process, or write through an MCP path?
Packaging the citation verifier (2026-11-07) answered it with evidence: at that date an extension had to construct in-process, because the MCP write path was missing three things a typed chain needs.

1. **Seeding could not carry the task type.** `create_project(options.tasks)` seed specs had no `task_type` field, so every seeded card would have been an `implement` card and none of the kernel's verification rules would apply.
2. **Seeding could not carry the inputs.** Each `find` card carries the excerpt and its metadata in `inputs`; seed specs had no `inputs` field.
3. **Seeding could not carry a caller-supplied id.** The exporter finds a case's cards by their ids and labels (#844); seed specs had no `id` field.

**Larry decided on 2026-11-13: extensions write their cards over MCP only.**
#911 closed the gaps the same day, so the answer is now settled in code rather than in this document:

| Need | How an extension does it over MCP now |
| --- | --- |
| a card's `task_type`, `inputs` and final `id` | fields on the seed spec, `create_project(options.tasks=[...])`, validated by `validate_seed_specs` in `src/integrations/seeded_tasks.py` and advertised in the tool's schema |
| the project's task-type declaration (K2, #909) | `create_project(options.task_type_registrations={...})`, validated up front by `validate_task_type_registrations` in `src/core/project_registry.py` and stamped on the `ProjectConfig` the call mints |
| the `task_metadata` rows Cato renders | written by the creation path itself for every card, with the card's `task_type`; no separate write exists or is needed |
| registering an already-seeded board | `add_project(provider="sqlite", ...)` (#888) |

A `depends_on` in a seed spec may name another spec by its name or by its caller-supplied `id`.
The full citation study is one call carrying 800 seed specs with dependencies; the cost of the change was the schema, not the transport.

**What the software-building cut must satisfy, therefore:** seed its cards over MCP the same way, and import nothing from `src/` beyond the MCP client.
An extension can run on a different machine from the server.
The in-process door is closed for new extensions; `kernel.py` shows what remains.

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

1. Put every kernel import in one module and copy `test_kernel_surface.py` with your own declared list; today that list is the MCP client and its reply reader, nothing else.
2. Create the project and its cards with one `create_project` call: `options.tasks` carries each card's `task_type`, `inputs` and `id`, and `options.task_type_registrations` declares the types (K2) with their evidence schemas and, for a verify-shaped type, `verifies_task_type`.
   The reply's `project_id` is the registry id your workers register against.
3. Run workers as MCP clients only.
4. Prove it live the way `~/marcus-experiments/citation-extension-live-check/run_rehearsal.sh` does: a real server, the real transport, every card closed through the kernel's gates, zero spend.

## Related

- ADR-0013 K1 (what the kernel is), K2 (how an extension registers), K4 and K8 (the pull model), K11 (verification first): `docs/architecture/adr/0013-kernel-and-extensions.md`.
- The extension guide: `docs/architecture/building-an-extension.md`.
- The software-building extraction scope: `docs/architecture/software-extension-extraction.md`.
- Issues #741, #844 (sanctioned seeding paths), #888 (`add_project` accepts sqlite), #831 (K2 registration field), #909 (the chain declares its types), #910 (public reply reader), #911 (seed-spec gaps closed), #913 (guard docstring).
