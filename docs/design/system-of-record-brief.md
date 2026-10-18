<!-- Short companion to system-of-record.md for readers with no context. Full design, protocol, and patch outline live in that file. -->

# Marcus as a System of Record, the short version

*Six hundred words on what the record is, what it enforces, where it sits next to an agent control plane, and the experiment that tests it.*

## The thesis

Every team that runs AI agents on real work ends up hand-building the same thing: a durable, vendor-neutral record of who did what, what evidence came back, who independently checked it, what a person approved, and what it cost.
Control planes, gateways, and tracing tools record what an agent may reach, which model ran, and which calls it made.
None of them records whether the work was accepted, by whom, on what evidence, or why the agent decided what it decided.
Marcus already holds that record by construction, because its board is the only channel agents have.
The work described here makes the board enforce the record instead of trusting the agent.

## What Marcus is

Marcus is an open-source coordination server for AI agents built on the blackboard pattern from classic AI.
Agents do not talk to each other.
They pull their next task from a shared board over MCP, do the work, and report back with evidence and their stated reasons.
Every assignment, completion, decision, and rationale therefore lands in shared state at the moment it happens, attributed to the agent that made it.
Marcus has coordinated coding agents (Claude Code, Codex, and others) on software projects for a year and launched publicly at the PyCon 2026 sprints.

## Definitions

A **case** is one unit of work with a goal and a policy.
A **task** is one step inside a case, and it has a **type**: find (produce a claim with sources), verify (check a claim you did not make), approve (a person accepts or rejects), synthesize (assemble the deliverable), or implement (write software).
A **claim** is what a find task produces: an assertion plus the sources behind it.
**Evidence** is the typed payload a task must return before it can close.
**Eligibility** is the rule the board applies before offering a task to an agent.
An **approval** is a task only a human principal can complete.
The **audit bundle** is the export of a case: every task, who took it and when, the evidence, the verdicts, the approvals, each agent's stated rationale, and the cost.

## What the board enforces

Three rules that Marcus previously left to the agent's honor, plus one it already had.
A task cannot close without the evidence its type requires; a "done" without it is refused with the missing fields named.
A verify task is never offered to the author of the claim or to any agent from the author's vendor, and an approve task is offered only to a person.
Everything downstream of an approval waits for it.
A dead agent's lease expires and its task returns to the board.
One deliberate rule about information flow: a verifier sees the claim and what it needs to check, never the author's reasoning, so it cannot be talked into agreement.
The reasoning is kept in the record for approvers and auditors.

## Where it sits

The record sits one layer above an agent control plane and consumes what the control plane produces.
Identity attributes (who the agent is, which vendor, agent or human) feed the eligibility rules.
Every board transition is emitted as one event into whatever tracing tables the organization already has.
Cost is attributed per decision rather than per request, so "a verified answer costs eleven cents and an unverified one costs four" becomes a query.
A control plane answers what the agent can reach, what changed, and what happened at the level of calls.
The record answers the next question: was the output checked by someone independent, who approved it, and why did the agent decide that.

## The experiment

The first proof is a replication of the Tow Center's 2025 study of how AI search engines cite news, in which eight engines answered wrongly more than sixty percent of the time and almost never declined.
The same two hundred excerpts run three ways: the engine alone, the engine on the record with a different-vendor verifier and a sampled human approval, and the same verifier bolted on with no board.
Expectations and failure thresholds are written down before running.
The board should convert confidently wrong answers into honest declines at about three times the cost per question, and it should tie the bolted-on verifier on accuracy, because accuracy comes from checking; what the record adds is that the check cannot be skipped, cannot be done by the author, cannot be lost, and can be shown to a third party.
A fifty-case pilot runs first.

## Where to read more

The full design, protocol, and patch outline: `docs/design/system-of-record.md`.
The implementation issue with file-level steps and verification procedure: the system-of-record issue in this repository.
Why the market does not sell this layer: `docs/design/the-cobbled-together-stack.md`.
