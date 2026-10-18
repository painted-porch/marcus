<!-- Design brief for the slim trust distribution of Marcus. One sentence per line, no em-dashes, written for a reader on their first day. -->

# Trust Kernel design brief

## What is this system, briefly

Marcus is an open-source coordinator where AI agents pull tasks from a shared kanban board, post results back, and communicate through nothing else.
This brief proposes a slim distribution of Marcus, the trust kernel, whose single promise is that an agent's "done" cannot be a lie: claims cannot close without evidence, are never verified by their author, and only humans approve.

## The evidence this brief stands on

A preregistered replication of Columbia's Tow Center citation study (issue #737) measured AI search engines on the same questions, alone and inside Marcus's enforced board.

| Engine | Alone: wrong among delivered answers | Alone: fabricated URLs | On the record: wrong | On the record: fabricated |
|---|---|---|---|---|
| OpenAI gpt-4o-mini | 87.2 percent | 70.2 percent | 0.0 percent | 0.0 percent |
| Gemini 3.6 Flash | 85.4 percent | 63.4 percent | (no board arm) | n/a |
| Perplexity Sonar | 47.6 percent | 28.6 percent | 6.7 percent | 0.0 percent |
| Claude Sonnet 5 | 50.0 percent | 37.5 percent | 6.7 percent | 0.0 percent |
| Claude Haiku 4.5 | 48.8 percent | 24.4 percent | 10.0 percent | 0.0 percent |

The engines did not get smarter on the record; their caught lies arrived as labeled declines with fetch receipts instead of as answers.
The effect was engine-independent and tier-independent, which is the claim the kernel productizes: honesty is procedural, not cognitive.

## The decision (Simon d13fd31e)

Ship the kernel as a profile of Marcus, not a fork, plus a declarative authoring layer for the four domain edges: verifiers, evidence contracts, intake, and approval routing.
Marcus stays the brand; the full coordinator stays the upgrade path.

## What the kernel is

The kernel is Marcus started with everything dark except the enforcement surface, which the #737 pilots proved stands alone (decomposer, planner model, memory, and visibility were all disabled in every run).

| Piece | What it does | Where it lives today |
|---|---|---|
| The board | Holds typed tasks with dependencies; the only channel | `src/integrations/providers/sqlite_kanban.py` |
| Typed-evidence gate | Rejects any close whose evidence misses the task's contract | `src/marcus_mcp/tools/task.py` |
| Eligibility | Never offers a verify task to the claim's author or the author's vendor, silently | `src/marcus_mcp/coordinator/eligibility.py` |
| Human-only approvals | Approve tasks go only to principals declared human | `src/marcus_mcp/tools/task.py`, `src/cli/approvals.py` |
| Audit export | One command returns who claimed, who checked, the evidence, who signed, the cost | `src/marcus_mcp/tools/audit_tools.py` |

A team interacts through five MCP tools (`register_agent`, `request_next_task`, `report_task_progress`, `get_task_context`, `export_case`) plus the approvals CLI.
They point the agents they already run at the endpoint; they do not adopt an orchestrator.

## The authoring layer: one file per claim type

A claim type is one declarative file holding all four edges.

```yaml
claim_type: legal-citation
evidence:
  required: [proposition, citation, pinpoint, quote]
verify:
  - record_exists: {source: courtlistener, id: "{citation}"}
  - text_at: {source: "{citation}", locator: "{pinpoint}", equals: "{quote}"}
approve:
  when: sampled(0.1) or risk == high
  by: role:partner
intake:
  from: [cli, webhook]
```

Each section compiles onto machinery that already exists.
`evidence` becomes the task's `output_schema`, which the typed-evidence gate already enforces.
`verify` composes deterministic check primitives; the Tow verifier (`dev-tools/experiments/runners/tow_checks.py`) decomposes into exactly such primitives (fetch, text-on-page, field-match, date-within, canonical-source), and a script escape hatch (claim plus evidence on stdin, verdict JSON on stdout) covers everything else.
Checks are authored at setup time as part of the task contract, never by the agent, per Invariant 2 v2.
`approve` is policy over the existing human-only gate.
`intake` generalizes the Tow seeder's `build_case_spec`: anything may call `create_case(claim_type, payload)` and get the find, verify, approve chain seeded with dependencies wired.

## Onboarding

`marcus trust init` scaffolds a claim-type file; `marcus trust demo` seeds one sample case so the first thing a visitor watches is their own agent's unverified "done" bouncing off the board.
The public funnel (the Verifier Bench page and the Tow replication report with published audit bundles) feeds this quickstart.

## Red lines

marcus-mini is untouched; the kernel is Marcus wearing less, and mini stays the research instrument.
The three agent invariants hold: agents pull work, agents own implementation, the board is the only channel.
Verifiers stay deterministic scripts; no LLM judges an LLM inside the gate.

## Sequencing

D3 (trustworthy recovery, epic #706 Phase 3) lands first; kernel customers bring long-running agents, and the current single lease dial falsely recovers healthy slow work, which is a trust product's worst bug.
The kernel profile and authoring layer follow, built TDD on the #737 surfaces.

## Glossary

| Term | Meaning |
|---|---|
| Board | The shared kanban task store agents pull from; the only communication channel |
| Claim | A task result that asserts something checkable, carried as typed evidence |
| Evidence contract | The fields a task's close must include (`output_schema`) |
| Eligibility | The assignment rule that hides a verify task from its claim's author and vendor |
| Principal | Whether a registered worker is an agent or a human; approvals are human-only |
| Audit bundle | The exported case record: claimant, checker, evidence, approvals, costs |
| Vendor | The self-declared maker of an agent's model; verification crosses vendors |

## Related

Simon decisions d13fd31e (this direction), 4872ff38 and f0e720ad (packaging insights).
Issue #737 (the replication), its readouts in `docs/design/research/`, and epic #706 Phase 3 (D3 first).
