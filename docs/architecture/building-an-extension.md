# Building a Marcus extension

## What is this system, briefly

Marcus coordinates a team of AI agents through a shared kanban board — a database of work items that every agent and the Marcus server itself can read and write.
Agents pull work from the board, do it, and report back.
Marcus is being re-architected into a thin **coordination kernel** (the board, the rules every agent follows, and the guarantees Marcus makes about them) with separate **extensions** snapped on top for each domain — software building, verifying a set of claims against sources, verifying a numerical report, coordinating bots in a Minecraft world.

This guide is for someone building a **fifth** extension.
It assumes no prior knowledge of Marcus's internals.
Everything it says is backed by a file path you can read yourself, or a decision recorded in `docs/architecture/adr/0013-kernel-and-extensions.md` (ADR-0013, Marcus's Phase 4 architecture decision record).

This guide depends on ADR-0013's K2 decision ("how an extension registers").
If K2 is not yet marked **Accepted** at the top of ADR-0013, parts of this guide describing registration are still provisional — check there first.

## Glossary

| Term | Meaning |
|---|---|
| **kernel** | Marcus's coordination core: the board, the Three Agent Invariants (below), and the guarantees every extension can rely on |
| **extension** | A snap-on piece that adds one domain's own verification policy, decomposition, or runner on top of the kernel |
| **task** | One work item on the board |
| **task type** | A string on a task (e.g. `find`, `verify`, `implement`) that tells an agent what kind of work it is |
| **evidence** | The structured data an agent submits when it closes a task, checked against that task type's required shape |
| **registration** | An extension declaring, for one project, which task types it owns and what evidence they need (ADR-0013 K2) |
| **MCP** | Model Context Protocol — the mechanism an agent uses to call Marcus's tools (`register_agent`, `request_next_task`, `report_task_progress`, and so on) |
| **pull loop** | An agent's own loop: ask for a task, do it, report it, ask again — never being handed work it didn't ask for |
| **the board** | The shared kanban database; the only channel agents and Marcus communicate through |

## What the kernel guarantees unconditionally

These are not things your extension can turn off or work around.
They are documented in `CLAUDE.md`'s `MULTIAGENCY_PROCLAMATION` section as the Three Agent Invariants, and every decision in ADR-0013 is required to respect them.

1. **Agents self-select work.**
   An agent calls `request_next_task` to ask for work.
   The kernel never pushes a task onto an agent, and never assigns a specific agent to a specific task.
   Your extension's own worker is an agent like any other: it asks, it does not get told.

2. **Agents make all implementation decisions.**
   The kernel says *what* needs doing and *why*.
   It never dictates *how* — no library choices, no internal code structure — with one exception: **how a task's completion is checked is part of the task's contract, not the agent's choice.**
   If your extension owns verification for a task type, your verifier's check is part of that contract, authored at setup time, the same way the kernel authors its own.

3. **Agents communicate only through the board.**
   There is no agent-to-agent channel, and no channel from the kernel to an agent outside a task assignment.
   If your extension needs to tell its own verifier something, write it to the board (a task's `inputs`, or a comment) — never call out to it directly.

The practical consequence for an extension: **your extension's own code never gets called by the kernel, and the kernel is never called by your extension's code.**
Everything flows through the board, both directions.
See "The pull model" below for what this looks like in practice.

## What an extension must declare (ADR-0013 K2)

An extension declares, **per project**, a named set of task types it owns, and for each one:

- the evidence it requires (what fields a worker closing that task type must submit)
- whether the kernel may inject required checks on top of what the worker submits (ADR-0013 K4)

This declaration is **board state, not configuration** — it is written once, when the project is created, and lives in the project's own record (a `ProjectConfig`, `src/core/project_registry.py`) rather than in a server config file.
It does **not** include a reference to your verifier's code.
See the next section for why.

### Today's state

As of this writing, K2's registration is a decided Recommendation; its field on `ProjectConfig` has not shipped yet.
The two extensions that exist today (the AI-citation verifier and the numerical-report verifier) both construct their project's record directly in Python and call `registry.add_project()` — the same mechanism, used in-process rather than through a tool call.
Check ADR-0013's K2 section for the current implementation status before you build against it.

## What an extension must never do: the pull model (ADR-0013 K4, K8)

The single most common mistake building against Marcus is assuming the kernel calls into your extension's code — the way a plugin system usually works.
**It does not, and it must not.**
Calling into extension code from the kernel would break Invariant #1 (the kernel never pushes work into a process) and K8 (the kernel assumes nothing about an extension's runtime — it may not even be Python).

Instead:

1. The kernel gates a task of your registered task type and offers it on the board, the same as any other task.
2. **Your extension's own agent** — a worker process you run, registered to your project — pulls that task through the ordinary `request_next_task` loop.
3. Your agent computes its own verdict, however it wants (a model call, a deterministic script, a comparison against staged data — the kernel does not care).
4. Your agent reports that verdict as evidence through `report_task_progress`, the same tool every other agent uses to close a task.
5. The kernel's handoff mechanism (ADR-0013 K7, K13) carries that evidence to whatever task depends on it.

Nothing in this sequence calls into your code.
Your code calls into the kernel's tools, exactly like every other agent.

## Worked walkthrough: the AI-citation verifier

The AI-citation verifier ("On the Record" / Tow) is the one extension that ships today with no decomposer and no planner model — the simplest real example to learn from.
It runs a four-step chain of task types: `find` → `verify` → `approve` → `synthesize`.

### Seeding the chain

`dev-tools/experiments/runners/tow_runner.py` seeds the board directly, writing each task with its `task_type` and an `output_schema` describing the evidence it expects:

```python
# dev-tools/experiments/runners/tow_runner.py, the "find" task
{
    "name": f"Find the source of {case}",
    "task_type": "find",
    "inputs": {"case_id": case, "query": ...},
    "output_schema": {
        "type": "object",
        "required": ["headline", "publisher", "date", "url"],
    },
}
```

This is the registration step, today's version of it: the task type and its evidence shape are declared directly on the board, task by task, rather than once per project.
(K2's own field on `ProjectConfig` is the planned replacement for doing this per-project instead of per-task — check ADR-0013's K2 section for its current status.)

### The worker's pull loop

`dev-tools/experiments/runners/tow_worker_common.py`'s `work_loop` is the agent side — the part your own extension's worker would mirror:

```python
while idle < cfg.idle_exits:
    response = await client.request_next_task(cfg.agent_id)
    task = response.get("task")
    if not task:
        ...  # nothing to do yet; wait and ask again
        continue

    task_type = str(task.get("task_type") or "")
    handler = handlers.get(task_type)   # your extension's own dispatch, in YOUR code
    ...
    evidence = handler(task)   # synchronous in this example; your own handlers decide
    await client.report_task_progress(
        agent_id=cfg.agent_id, task_id=task_id,
        status="completed", evidence=evidence,
    )
```

Notice what is *not* here: no callback the kernel invokes, no import of kernel code that runs your verifier.
`handlers` is a plain dictionary your own extension builds and owns; the kernel never sees it.
The kernel only ever sees the `task_type` string on the board and the `evidence` your agent reports back.

### Why this matters: a documented counter-example

The numerical-report verifier's own proof-of-concept did **not** follow this model, because K2's registration did not exist yet when it was built.
Its code imports a verification function by name directly into the kernel's own source tree and calls it unconditionally for every task matching the kernel's hardcoded task-type list — with no project scoping at all, so a second such extension on the same server would collide with it.
This is exactly the failure mode K2 exists to close.
If you find yourself writing an `import` of your own verification code into anything under `src/marcus_mcp/`, stop — that is the sign you need a registration, not a shortcut around one.

## No one checks their own work (ADR-0013 K12) — decided generally, implemented narrowly

K12's decision is general: a verification extension needs the kernel to exclude an artifact's producer, and anyone who shares its vendor, from the task that checks that artifact.
**Its current implementation is not general yet.**
`src/marcus_mcp/coordinator/eligibility.py`'s `is_eligible` only applies this exclusion to a task whose `task_type` is the literal string `"verify"`, and `_claim_authors` only discovers the producer by walking that task's direct dependencies for ones typed exactly `"find"`.

If your extension's task types are named anything else — `"check"` instead of `"verify"`, say, or a producer type other than `"find"` — **this guarantee does not apply to you today**, even though K12 itself does not depend on those specific names.
Until the implementation generalizes, either reuse the `find`/`verify` names, or treat this as still your own responsibility and say so explicitly in your own extension's documentation.
This is enforced, where it applies, at the point the kernel decides what to offer (`filter_eligible_tasks`) — not something your extension calls — and the refusal is silent, so a worker cannot probe around it.

## Where the kernel/extension boundary sits in the code

| File | What it is |
|---|---|
| `docs/architecture/adr/0013-kernel-and-extensions.md` | The architecture decisions this guide documents in practice — read K1 through K13 for the full reasoning |
| `CLAUDE.md`, `MULTIAGENCY_PROCLAMATION` section | The Three Agent Invariants and the bright-line test for coordination vs. control |
| `src/core/project_registry.py` | `ProjectConfig`, the project record K2's registration will live on |
| `src/marcus_mcp/coordinator/eligibility.py` | `filter_eligible_tasks` — K12's "no one checks their own work" guarantee |
| `src/marcus_mcp/tools/task.py` | `request_next_task`, `report_task_progress`, `request_task_redo` — the tools every agent, including your extension's, calls |
| `src/marcus_mcp/kanban_task_sync.py` | K13's guarantee that a board write is immediately visible to the kernel's own in-memory state — not something your extension needs to call, just something it can now rely on |
| `dev-tools/experiments/runners/tow_runner.py` | A real, shipping extension's board-seeding code |
| `dev-tools/experiments/runners/tow_worker_common.py` | A real, shipping extension's pull-loop worker |

## How to verify your extension is built correctly

1. Register your project's task types and evidence requirements through K2's mechanism (check ADR-0013's K2 section for whether that is a `ProjectConfig` field you write directly, or an MCP tool, as of when you read this).
2. Confirm your extension's worker calls `request_next_task` and never receives a task pushed to it outside that call.
3. Confirm nothing under `src/marcus_mcp/` imports or calls a function defined in your extension's own code. If it does, you have built the kernel-calls-extension model this guide rules out.
4. Confirm your verifier's check, if it has one, is declared at task-creation time (part of the contract), not authored by the same agent whose work it checks (ADR-0013 K4).
5. Run your extension's full chain against a real Marcus server and read back every reply body — do not infer success from a board state alone. (This is the same live-check discipline every fix in this repository is held to; see the `~/marcus-experiments/` write-ups referenced throughout ADR-0013 for examples of the level of detail expected.)

## Related

- ADR-0013 — the architecture decisions this guide documents, particularly K1 (what the kernel is), K2 (how an extension registers), K4 (check authorship), K7 (handoff), K8 (runtime neutrality), K12 (offer-time eligibility), K13 (write consistency)
- `CLAUDE.md`'s `MULTIAGENCY_PROCLAMATION` section — the Three Agent Invariants in full
- #818 — the issue this guide answers
- The AI-citation verifier (`dev-tools/experiments/runners/tow_runner.py`, `tow_worker_common.py`) — this guide's worked example
- The numerical-report verifier's proof-of-concept — this guide's counter-example of what registration replaces
