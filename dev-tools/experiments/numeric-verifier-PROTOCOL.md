# Numeric Report Verification: Proof of Concept

Preregistered protocol.
Written 2026-11-03, before any data was staged or any run was made.

## What this is

Marcus is a coordination layer for AI agents.
Agents never talk to each other.
They claim work from a shared board and report results back to it, and Marcus enforces rules about what counts as done.

This experiment tests one claim: that a report full of numbers is more trustworthy when the check on those numbers is authored and enforced by the harness than when the agent that wrote the report also decides whether it is correct.

The motivating case is a colleague's workflow at a large agricultural equipment company.
He generates a model performance report and slide deck from a machine learning evaluation run, and he added three verification loops to it.
The first checks every numerical result against the source data.
The second is an editorial pass.
The third re-audits the numbers on the slides.
Each loop retries up to three times.

A recomputation of one of his published decks suggested the loops pass per-figure lookups but do not catch relations between figures.
A tier table summed to 14,253 units while two other tables summed to 14,258.
A stated ratio of "~23x" recomputed to 22.1.
A stated "~18.7%" recomputed to 18.6% unless you sum already-rounded percentages.
Those findings are unconfirmed, because the staged source files were not available.
This experiment reproduces the setup on public data where ground truth is known, so the catch rates can be measured rather than inferred.

## Definitions

A **case** is one report.
A **task** is one step in a case, and it has a kind: find (write the report and its claims), verify (check claims you did not make), approve (a person accepts or rejects), synthesize (build the slide deck from accepted claims).
A **claim** is one number in the report, recorded as an identifier, a value, a label, a source file, a source key, a kind, and a tolerance.
A claim's **kind** is one of three.
A **lookup** claim reads a single value from a single staged file.
A **derived** claim is computed from other values, such as a ratio or a percentage.
A **consistency** claim asserts a relation between figures, such as "the tier counts sum to the record count."
**Evidence** is the typed payload a task must return before Marcus will let it close.
**Eligibility** is the rule Marcus applies before offering a task to a worker, which for a verify task means the worker is not the claim's author and not on the author's vendor.
The **audit bundle** is the export of one case: every task, who took it, every round, every verdict, every approval, and the cost.

## Hypotheses

**H1.** Condition B catches wrong figures that Condition A ships.
Specifically, B catches derived-figure errors and consistency violations that A passes, because A's check has no obligation to recompute or cross-check.

**H2.** Condition C scores close to Condition B on catch rate.
This is the honest control.
The accuracy gain comes from the checker, not from Marcus.
What Marcus adds is that the check cannot be skipped, cannot be performed by the author, cannot be lost, and can be shown to a third party.
If C scores far below B, something other than the checker is doing the work and the claim is overstated.

**H3.** Condition A's self-check accepts the author's assertions.
When the checking agent can read the writing agent's reasoning, it will close verify tasks without independently opening the source file at a measurably higher rate than a checker that sees only the claim and the source.

**H4.** Slide figures mapped to accepted claims eliminate a class of error that re-auditing misses.
A deck assembled only from already-accepted claims cannot contain a figure the report verifier rejected.

## Conditions

All three conditions read identical staged data.

**Condition A, the baseline.**
A writer agent produces the report.
A checking agent on the same model, with access to the writer's conversation, verifies it.
Up to three rounds.
No board, no eligibility rule, no declined outcome, no record.
This reproduces the colleague's current setup.

**Condition B, Marcus enforced.**
The same writer agent produces a report and a claims file.
Marcus holds the claims, hands the verify task only the claim and the source, and offers it only to a worker that is neither the author nor on the author's vendor.
The verifier is deterministic code, not a model.
Failed claims re-open the find task with the failures named, up to three rounds, after which the case closes as declined.
A sample of cases, plus every case that needed more than one round, waits for a human approval.
The slide deck is assembled only from accepted claims, and the slide check is a mapping check rather than a re-audit.

**Condition C, the checker with no record.**
The same deterministic checker from B applied to Condition A's final output as a post-hoc filter.
No board, no eligibility, no held close, no approval, no bundle.

## Dataset

FAOSTAT crop production, the normalized bulk export, which is public and needs no credentials.
Four crops chosen to mirror the colleague's report: maize, soybeans, wheat, and seed cotton.
One row is one country and year, which stands in for one field in his report.
The prediction target is yield.
The model is a small gradient-boosted regressor trained on prior-year yield, harvested area, and region.

Model quality is not the subject of this experiment.
What matters is that the errors are real, that their distribution has a long right tail like the colleague's report, and that the four crops have uneven group sizes like his, where corn had 8,171 fields and cotton had 369.

The staging step writes the same artifact family his workflow produces: an overall metrics file, per-crop metrics, a per-record error table, quality tiers, and size quartiles.
Every artifact carries a content hash and an evaluation timestamp.

The data source is behind one function so a different source can be substituted without touching the rest of the experiment.
USDA county yields were the first choice and remain the better analog, but the agency's interface requires an account key, so FAOSTAT was used instead.

## Ground truth and planted errors

Ground truth is the staged data.
A manifest records the correct value of every figure a report should contain, and the manifest never reaches Marcus.
A worker that can read the answer from the board is not being tested.

Two sources of error are measured.

**Natural errors** are whatever the writer agent produces on its own.
This is what the colleague actually sees.

**Planted traps** mirror the failure modes the deck recomputation suggested.
A stale evaluation directory sits beside the current one with figures that differ by a few percent, so a writer that opens the wrong file produces a confident wrong number.
Tiers are staged with undefined-error records dropped, so the tier total does not equal the record count, which is the 14,253 case.
A ratio is staged where the correct value rounds down and a careless writer would round up.

Trap placement is seeded and recorded in the manifest.

## Metrics

Per condition, the grader reports:

- Wrong figures that shipped, split by claim kind.
- Correct figures declined, which is the false-decline rate.
- Claim coverage, the fraction of numbers in the final text that carry a claim.
- Trap catch rate, per trap type.
- Rounds per report.
- Cost per report.

Grading is label-blind.
Every report from every condition is normalized into one record shape, given a blind identifier, shuffled with a fixed seed, and scored by one function that never sees which condition produced it.
Rates are computed afterward by rejoining on the blind identifier.

## Scale

Thirty cases per condition, ninety reports in total.
About forty claims per report, so roughly 1,200 claims checked per condition.
A rehearsal with a scripted writer that emits known-good and known-bad claims runs first and costs nothing.
A pilot of three cases per condition runs before the full set.

Estimated cost for the full run is under twenty dollars.
The deterministic verifier costs nothing, because it never calls a model.

## What gets built

| Piece | Purpose |
| --- | --- |
| Staging script | Turn FAOSTAT rows into the artifact family a report is written from |
| Trap generator | Plant the three trap types and record them in the manifest |
| Claim schema | The typed shape every number in a report must carry |
| Deterministic checker | Resolve lookups, recompute derived figures, evaluate consistency relations, require a claim for every number in the text |
| Seeding runner | Create one case with its five tasks on a Marcus board |
| Writer worker | A headless agent session that reads staged data and reports a report plus claims |
| Verifier worker | A no-model worker that runs the checker and reports verdicts |
| Slide worker | Assemble a deck from accepted claims only |
| Bounded retry | Re-open a find task with named failures, and close the case as declined at the bound |
| Grader | Label-blind scoring across the three conditions |

## Preregistered cautions

The person who scores a human spot-check must not be the person who wrote the checker's rules.

If Condition C scores far below Condition B, the result is reported as-is rather than explained away.

If the writer agent cannot reliably produce a complete claims file, every case fails on coverage before any interesting failure appears.
The writer is rehearsed alone against one staged directory before being connected to Marcus.

The deck findings that motivated this experiment are unconfirmed.
Nothing in the readout asserts that the colleague's workflow shipped a wrong number.
