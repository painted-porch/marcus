<!-- As-built reference for the On the Record enforcement machinery (#737). The design is system-of-record.md; this documents what shipped, mapped to code. One sentence per line. -->

# On the Record, as built

*The enforcement machinery that shipped in PR #739: what each rule is, where the code enforces it, and how to watch it refuse you.*

## What is this system, briefly

Marcus is an open-source coordination server where AI agents pull tasks from a shared kanban board over MCP (a standard protocol for tool calls).
"On the Record" is the set of board-enforced rules that turns that board into a system of record: work cannot close without evidence, no claim is checked by its own author, a person signs the result, and every case exports a complete audit bundle.
The design rationale lives in [system-of-record.md](system-of-record.md); the experiment that proves it is in [research/](research/) and [research/REPRODUCING.md](research/REPRODUCING.md).
This document is the map from each rule to the code that enforces it.

## The rules and where they live

| # | Rule | Enforced in | Mechanism |
|---|------|-------------|-----------|
| 1 | Work is pulled, never pushed | `src/marcus_mcp/tools/task.py` (`request_next_task`) | Agents ask; the board offers from what remains after eligibility filtering. |
| 2 | Nothing closes without evidence | `src/marcus_mcp/tools/task.py` → `_apply_typed_evidence_gate` | A find, verify, or synthesize completion must carry every field its task's `output_schema.required` lists; the rejection names the missing fields. |
| 3 | No one checks their own work | `src/marcus_mcp/coordinator/eligibility.py` → `filter_eligible_tasks` | A verify task is never offered to the claim's author, nor to any worker registered under the author's vendor; the refusal is silent, so the gate cannot be probed. |
| 4 | A person holds the final signature | `eligibility.py` (offer time) + `task.py` → `_apply_approval_gate` (close time) | An approve task is offered only to a `human` principal, and its completion is refused from anything else — defense in depth at both ends. |
| 5 | The record is complete by construction | `src/marcus_mcp/tools/audit_tools.py` → `export_case` | The board is the only channel, so exporting the tasks labeled `case:<id>` yields every claim, check, actor, approval, and cost without anyone having remembered to log. |

## A case, start to finish

A **case** is one unit of work (for example, one citation question).
The runner seeds it as a dependency chain of typed tasks: **find** (produce a claim with sources) → **verify** (check a claim you did not make) → **approve** (a person accepts) → **synthesize** (assemble the deliverable).

1. A finder agent calls `request_next_task` and is offered a find task.
2. It reports completion with an `evidence` payload; the typed gate rejects the close if a required field is missing, and after repeated failures terminalizes the task instead of looping (the #677 retry ceiling, reused by #737).
3. The verify task unlocks; eligibility guarantees whoever takes it is neither the author nor the author's vendor, and the evidence handoff seeds the verifier's inputs so that seeded values win over anything a later writer supplies (the anti-gaming merge in `task.py`).
4. The verifier fetches the claimed URL itself — `dev-tools/experiments/runners/tow_fetch.py` enforces an address guard (no private, loopback, link-local, or reserved targets), robots.txt, and a re-check of both on any cross-origin redirect — and closes with its checks as evidence.
5. The approve task is completed by a person through `src/cli/approvals.py`; dependents wait on ordinary dependency resolution until the signature lands.
6. `export_case` (exposed via `dev-tools/experiments/runners/export_bundles.py`) writes the audit bundle; `redact_bundles.py` produces the publishable copy with dataset text stripped.

## Evidence contracts

The gate does not hard-code claim types.
Each task carries an `output_schema`, and the gate requires exactly that schema's `required` fields; a task with no schema passes on any non-empty payload.
This is why the same machinery works for citations today and for code, numbers, or legal claims tomorrow: a new claim type is a new schema plus a new verifier, not a change to the board.
Approve tasks are recorded but not evidence-gated: the person's decision is the approval (`EVIDENCE_RECORDED_TASK_TYPES` in `task.py`).

## Why the refusals are silent

Eligibility filters tasks out of an agent's view before the offer, so the author never learns the verify task exists.
A visible gate gets probed, timed, and gamed — issue #636 documents an agent iterating verification commands until one passed.
An invisible gate cannot be iterated against, which is the structural reason verification here is trustworthy rather than merely present.

## How to verify it works

Run the free rehearsal in [research/REPRODUCING.md](research/REPRODUCING.md) (fake engine, zero API calls), then try to cheat:

1. Report a find task completed without evidence → expect a rejection naming the missing fields.
2. Keep submitting bad evidence → expect terminalization after the retry ceiling, not an infinite loop.
3. Request work as the finder after its finds are done → expect "no task" while verify tasks for its own claims sit unoffered.
4. Complete an approve task as an agent → expect refusal; complete it via `python -m src.cli.approvals` → expect dependents to unlock.
5. Export the case → expect the bundle to name the finder, the verifier's checks, the approver, and the cost.

## Glossary

| Term | Meaning |
|------|---------|
| Case | One unit of work with a goal; its tasks share a `case:<id>` label. |
| Claim | What a find task produces: an assertion plus sources. |
| Evidence | The typed payload a completion must carry to close. |
| Eligibility | The board-side filter deciding which workers may even see a task. |
| Principal | A registered worker's kind: `agent` or `human`. |
| Audit bundle | The JSON export of a case: tasks, actors, evidence, approvals, costs. |
| Silent refusal | Filtering at offer time, so an excluded worker cannot detect the gate. |

## Where to look in the code first

| File | Purpose |
|------|---------|
| `src/marcus_mcp/coordinator/eligibility.py` | All offer-time rules, in one function both assignment paths call. |
| `src/marcus_mcp/tools/task.py` | `_apply_typed_evidence_gate`, `_apply_approval_gate`, the seeded-inputs-win handoff. |
| `src/marcus_mcp/tools/audit_tools.py` | `export_case`: the audit bundle. |
| `src/cli/approvals.py` | The human signature CLI. |
| `dev-tools/experiments/runners/tow_fetch.py` | Fetch safety: address guard, robots, redirect re-check. |
| `dev-tools/experiments/runners/tow_runner.py` | Case seeding, board registration, id prefixing, task metadata. |
| `tests/unit/` (`test_typed_evidence_gate`, eligibility, fetch tests) | The behavior, specified executable. |

## Known gaps

Refusal durability across server restarts is tracked in issue #740.
Full board citizenship for experiment workers is tracked in issue #741.

## Related

Issue #737 (preregistration), PR #739 (the implementation), issues #740 and #741 (architectural debt), [system-of-record.md](system-of-record.md) (design), [research/](research/) (readouts), the `on-the-record-v1` release (redacted bundles).
