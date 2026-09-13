<!-- Design note, September 2026. Published with figures at https://claude.ai/code/artifact/04ad38fa-0a84-433d-b070-8a4fb8e694c0 -->

# What Marcus Becomes

*A vision for the ledger of agent work: what it does, how the world changes when it exists, how far away it is, and why now*

---

## The whole story on one page

Companies are starting to let AI agents do real work: read a customer's account, decide a claim, move money, change a system.
Everything one agent needs to do that can now be rented by the hour.
The moment two agents have to cooperate on one case, six things have to exist that nobody sells: a durable record of the case, a way to hand work between agents, a record of who owns what and what happens when an agent goes silent, a way to route decisions to a person, a way to know what each agent spent, and proof that the outcome is right.
Today every company builds those six things itself, badly, again.

Marcus becomes the product that provides them: a **ledger of agent work**.
It is a board that agents from any vendor pull work from; a contract attached to every task that says what "done" means and how it will be checked; a lease that says who holds the task and when it is presumed abandoned; a gate that refuses completion without evidence; a rung for human approval; and an account of what every agent spent, per task.
It does not run models, host agents, or replace the tools that keep long processes alive.
It sits between them and makes the work coherent, recoverable and provable.

After it exists, an organization can put agents from three vendors on one case and answer, at any moment, five questions it cannot answer today: what is being worked, by whom, what has been decided, what has it cost, and was the outcome verified.
Compliance gets a record instead of chat logs.
Engineering stops rebuilding the plumbing.
Vendors compete on the quality of their agents instead of on lock-in.

Marcus today has four of the six components, built for one domain (software), running agents from Anthropic, OpenAI and Google against one board.
The distance to the vision is not conceptual; it is a runtime built from git parts, a verification gate that is switched off, two missing components, and a maintainer working weekends.
The minimum product is one ledger with two adapters (the existing software one and a new operational one) demonstrated on a synthetic insurance claim with three vendor-different agents, a human approval, an enforced gate, and an exportable record.
At weekend pace that is roughly six months; with a full-time partner, roughly three.

The window is real and it is now: the research has decided in favor of this architecture, the analysts named the category two months ago, the money has gone to the layers on either side, and nobody owns the middle.
That is what makes it exciting, and the question of whether you are the person to do it has an honest answer, which comes last.

---

## Part I: The world Marcus enters

Three things are true in September 2026 that were not true a year ago, and together they make the moment.

First, single-agent work is solved as a product.
Anthropic, Amazon, Google, Microsoft and Salesforce all rent the loop that lets one agent think, act and think again, metered by the hour or the action, and the tools around it (durable execution, gateways, tracing, sandboxes, identity) are mature.
Nobody needs to build a single agent's machinery any more.

Second, multi-agent work is solved as a *pattern* but not as a product.
The companies that run several agents on consequential work all converged on the same shape: a hub hands narrow jobs to specialists, the specialists communicate through the case file rather than with each other, a person holds the money decision, and a separate checker verifies the result.
Research from Google, MIT and Anthropic in 2026 explains why that shape wins and why the alternatives (agents chatting freely, managers delegating to managers) fail.
But each company built the connective tissue for that pattern by hand.

Third, the category has just been named and not yet owned.
Gartner called it "AI Agent Management Platforms" in July; Forrester calls it the "agent control plane" and expects the market to solidify within two years.
Capital has gone to identity, observability, durable execution and finished-outcome agents, and nothing has gone to coordination itself, because nobody has found its price.
Half of enterprise agents run in isolation; 85% of organizations have no one accountable for agent behavior.

That is the opening: a proven pattern, no product, a named category, and a market that pays for everything around it.

---

## Part II: What Marcus becomes, and how it works

Marcus becomes the ledger of agent work: the system of record for work done by agents, from any vendor, on one case, with proof.

The word *ledger* is chosen carefully.
A ledger is a durable record that multiple parties write to, that nobody can quietly rewrite, and that can be audited later.
That is what a case worked by several agents needs, and it is what no runtime provides.
Here is how it works, mechanism by mechanism, each one a direct answer to one of the six things every team builds.

**The board is the case record.** A case (a claim, a dispute, a security alert, a software feature) arrives and is broken into tasks on a shared board.
The board is durable: it lives in a database, survives restarts, and is the single place where the current state of the case exists.
Agents do not carry the case in their heads; they read it from the board and write back to it.
Every decision an agent makes and every artifact it produces is logged to the board, so the record is built as the work happens, not reconstructed afterwards.

**The contract is the handoff.** Every task carries a contract: what the task must produce, what it may assume from upstream tasks, what downstream tasks will need from it, and how completion will be verified.
When agent A finishes and agent B starts, B receives the contract and A's artifacts, a structured package, not a conversation.
This is the mechanism the MIT relay study says preserves accuracy across hand-offs, and it is the thing no protocol carries.

**The lease is ownership.** When an agent takes a task it receives a lease: it holds the task until a deadline, extends the lease by reporting progress, and if it goes silent past the deadline, the task returns to the board with everything the agent recorded so far.
A second agent picks it up and continues.
If the first agent was alive after all and reports late, the ledger notices the collision instead of applying the stale result.
Nothing depends on knowing whether a process is dead; ownership is a token on the board, not a fact about a machine.

**The gate is proof.** A task is not done because an agent says so.
The contract specified how it would be verified; the gate runs that verification and refuses completion without evidence.
For software that means the tests pass and the app behaves; for a claim it means the policy rules were applied to the evidence and the result reconciles.
When the gate cannot verify (some outcomes need a person) it stamps the record "accepted on self-report" so that nobody downstream mistakes "done" for "verified."

**The approval rung is the human.** Certain transitions (paying out, changing a customer's access, merging into production) are marked in the contract as requiring a person.
The board routes them by rule: this kind of decision, above this amount, below this confidence, to this role.
The person sees the case narrative and evidence, not a transcript, and their decision is recorded with their identity.

**The account is cost.** Every token every agent spends is attributed to the task it was working on and the agent that spent it, across vendors.
A case has a cost the way a project has a budget, and an organization can see which agent, which model, which task consumed what.

Two properties make Marcus different from anything a single vendor would build.
It is neutral: an agent is anything that speaks the protocol, so a Claude agent, an OpenAI agent, a Google agent, an in-house agent and a human can work the same board.
And it is willing to say no: the coordinator's first decision on any case is whether it needs more than one agent at all, and for most work the honest answer is "no, run it as one agent," which the ledger records just the same.

What Marcus does not do is equally deliberate.
It does not run models or host agents; the vendors do that.
It does not keep long processes alive across crashes; Temporal and its peers do that, and the board's leases sit on top.
It does not issue agent identities or vault credentials; Okta and its peers do that, and the board records which identity did what.
It does not inventory every agent in the enterprise; Microsoft and ServiceNow do that, and the board exports its records to them.
Marcus is the layer between execution and governance: the record of the work.

---

## Part III: Storyboard: one claim, four agents, one person

The clearest way to see the product is to watch one case go through it.
The case is invented but the shape is Allianz's, which is public: a small insurance claim after a storm.

### Panel 1: A case arrives

A policyholder submits a claim: a power outage during Tuesday's storm spoiled the contents of her refrigerator, about $340.
It arrives on the board as one case with the policy number, the address, the date, and her description.

### Panel 2: The coordinator decides whether this needs a team

Marcus reads the case and asks its first question: would one agent do this well in one sitting?
For a $340 claim with four independent checks and one consequential decision, the answer is that the checks are independent, evidence must come from four different systems, and a regulated payout needs an independent checker.
The case is decomposed into five tasks with contracts: confirm coverage, confirm the weather event, screen for fraud, compute the payout, and audit-and-recommend.
The first four can run in parallel; the fifth depends on all of them; the payout itself is marked as requiring a person.

### Panel 3: Agents from three vendors pull the work

Four agents are registered with the board, a coverage specialist running on one vendor's model, a weather-and-fraud pair on another, an auditor on a third.
None of them knows the others exist.
Each asks the board for work; the board hands each a task and a contract; each receives a lease.
The coverage agent's contract says: produce a structured finding with the policy clauses cited and the effective dates, and the way it will be checked is that every cited clause must exist in the policy document.

### Panel 4: Work moves as artifacts

The weather agent queries the outage records and writes a finding to the board: a 6-hour outage at that address, with the utility's record attached.
The fraud agent writes: no prior claims, no anomalies, low risk score, with its reasoning attached.
Neither talks to the other.
The board now holds three findings, each with its evidence, each attributed to the agent that produced it.

### Panel 5: An agent goes silent

The coverage agent stops responding halfway through, its vendor had an outage.
Its lease expires.
The task returns to the board with the partial finding it had already recorded.
A different coverage agent picks it up, reads what was found so far, and finishes.
Ten minutes later the original agent comes back and reports its own completion; the ledger sees that the task has moved on, refuses the stale result, and records the collision.
Nobody had to notice; nothing was done twice.

### Panel 6: The gate checks the work

The auditor's task depends on the four findings.
Its contract says: reconcile the findings against the policy rules and produce a recommendation with every input cited.
When it reports done, the gate runs the verification the contract specified: every cited clause exists, the payout arithmetic matches the coverage limit, the weather record's date matches the claim.
It passes.
Had the auditor cited a clause that does not exist, the gate would have refused and sent the task back with the reason.

### Panel 7: A person decides

The payout transition is marked for human approval.
The claims adjuster opens the case and sees a one-page narrative: what was claimed, what each agent found, what each finding rests on, the recommendation, and the cost so far, 41 cents across four agents.
She approves.
Her identity and the moment are recorded on the case.

### Panel 8: The case closes with its record

The case is closed.
Its record holds every task, every lease, every hand-off, every finding with its evidence, the gate's checks, the approval, and the cost per agent.
Six months later, when a regulator asks how this claim was decided, the answer is a page, not a search through four vendors' logs.

### Panel 9: The operator's view

The operations lead opens the board and sees today's cases: how many closed, how many waiting on a person, which agent vendor is spending the most per task, which contracts keep failing the gate, and one case that has been reassigned twice and needs a look.
The board is the system, and the people watch the board.

---

## Part IV: How the world is different after

The change is easiest to see person by person.

The platform engineer who used to spend months building a session store, a handoff format, a timeout scheme, an approval flow and a cost export before the first agent could safely touch production instead installs a ledger, writes contracts, and registers agents.
The six things arrive as a product.
The engineer's job becomes the interesting part: designing the decomposition and the contracts for the company's cases.

The compliance officer who today cannot answer "which agent did this, on what data, and who approved it" without reading transcripts from three vendors gets a record built as the work happened, with identities and evidence attached, exportable to whatever governance tower the company already runs.
The regulator's question becomes answerable.

The finance lead who today learns the agent budget is gone in April gets cost per case, per agent, per vendor, and a circuit breaker that stops a runaway before it reaches the invoice.

The agent vendor who today wins deals by locking a customer into its runtime instead competes on whether its agent passes more gates for less money.
A customer can put two vendors' agents on the same cases and read the ledger.

The agent itself has a simpler life: register once, ask for work, receive a task with a contract that says what done means, work, report, hand off, ask again, go home when the board is empty.
No chat with other agents, no guessing what the case needs, no holding the whole world in context.

And the organization gets the thing none of the surveys say it has: someone accountable for agent behavior, because the ledger makes accountability a property of the record rather than a policy nobody enforces.

---

## Part V: The evolution beyond

The vision unfolds in three horizons, each of which is a product in its own right.

**Horizon one, the next twelve months: the ledger for mixed fleets.** The first users are teams already running agents from more than one vendor and feeling the six problems.
The nearest such teams are software organizations running Claude Code, Codex and in-house agents together (the same fleets OpenHands began selling governance for in May) and Marcus's runtime already fits them.
The product at this horizon is the ledger with the software adapter: contracts, leases, gates, approvals and cost for coding agents across vendors.
It proves the primitives on real work and produces the evidence the next horizon needs.

**Horizon two, one to three years: the case layer for operations.** The same ledger with operational adapters (claims, disputes, investigations, exceptions) for the five situations where multiple agents demonstrably earn their cost.
This is Scott's market and the larger one.
It requires the runtime to be domain-neutral, an approval rung with real routing, integration with identity products, and a design partner in a regulated industry.
At this horizon the protocol matters more than the server: if the contract, lease and evidence formats are open and adopted by harness vendors, Marcus becomes the record that everyone's agents write to, whoever runs the board.

**Horizon three, beyond: federation.** Cases do not stop at a company's edge.
A claim involves an insurer, a repair vendor and a bank; a supply-chain exception involves a shipper, a carrier and a customs broker.
When each party runs agents, the case needs a ledger that spans organizations: shared where the parties agree, private where they do not, with every party's agents holding leases and passing artifacts through contracts they both signed.
That is the original Marcus ambition of federated coordination, and it is coherent once the single-organization ledger exists.
The research instrument survives all three horizons: the coordination tax (what it costs to split work across agents versus doing it with one) is measured on the ledger, and Marcus remains the reference implementation for the claim that agents coordinate better through shared state than through conversation.

The story of the industry, if this goes right, is that "agent coordination" stops being a thing companies build and becomes a thing they record, the way nobody builds their own version control or their own accounting system.

---

## Part VI: How far away Marcus is

The honest measurement is against the six things, plus the runtime beneath them and the pace above them.

| The six things | Where Marcus stands today | Distance |
|---|---|---|
| The case record | A database-backed board with persisted task leases and a log of every decision and artifact. Built for software projects. | Close. Needs a domain-neutral schema and a case-level view. |
| The handoff | Contracts and scoped artifacts delivered with each task, for code. | Medium. The contract format must be expressed without git, and the payload sent once rather than twice. |
| Ownership and recovery | Leases, progressive timeouts, a circuit breaker for agents that keep going silent, repair-and-retry, and a way to send bad work back; hardened in August. | Close. Long-lived agents must replace spawned-per-task ones (Phase 3), which is designed and not started. |
| Approval | Nothing. Proposed twice, never built. | Far, but small: an approval state on the board, routing rules, and identity of the approver. |
| Cost | Nearly a million agent turns attributed per task and per tool; the reporting side lives in a companion project. | Close. Bring the ingestion inside the ledger and make the export first-class. |
| Proof | A validation gate and a smoke gate exist; evidence is currently optional and the latest run passed a broken app. | Medium. Enforce evidence when a contract specifies it; make the gate's verdict visible in the record. |

Beneath the six things is the runtime, and this is the real distance.
Marcus's runtime is made of software-development parts: git worktrees, merges to main, test runners, a parser that turns a product description into coding tasks.
An insurance claim has none of those.
The board, the leases, the ledger and the gate are general; the parts that create tasks and check them are not.
Generalizing them is a design problem before it is a coding problem, and the design work is the protocol rewrite described in the next steps.

Above the six things is the pace.
The record shows what one person produces: about a hundred merged changes in a good month in spring, four to twelve a month since June.
The session-model migration (the one prerequisite everything else depends on) was targeted for August 2 and has zero of its checklist items started as of September.
Nothing in the vision fails on its merits; all of it fails on calendar if the calendar stays as it is.

Two estimates, both rough.
At weekend pace, the minimum product described next is about six months away.
With a full-time partner and the maintainer at the current pace, about three.

---

## Part VII: The minimum viable product

A minimum viable product is the smallest thing that lets a real person decide whether they want more.
It is not a prototype (which proves a thing can work) and not a beta (which is the product with bugs).
It is the product with the fewest features that still delivers the whole promise to one kind of user.

The Marcus MVP is **one ledger, two adapters, one demo.**

One ledger: the board, contracts, leases, gate, approval rung and cost account, with a domain-neutral protocol that any agent can speak.

Two adapters: the existing software adapter (tasks created from a feature description; verification by tests and behavior), and one new operational adapter (tasks created from a case description; verification by rule checks against evidence).
Two adapters is the minimum that proves the ledger is not a coding tool wearing a costume.

One demo: the storyboard above, run for real.
A synthetic claim on invented data.
Four agents from three vendors registered against one board.
One agent killed mid-task and the work recovered.
One completion refused by the gate for a fabricated citation.
One human approval recorded.
One ledger export that answers the five questions.
Run three times unattended with the same result.

What is deliberately out: any real customer system integration, any identity product beyond recording who approved, any dashboard beyond the board view, any model or agent hosting, any pricing.
Those are horizon-two work and design-partner work, and they are the questions the MVP exists to raise.

Who it is for: a platform engineer at a company running agents from more than one vendor, who watches the demo and asks the only question that matters, "can it run on our cases?" That question, asked by two or three such people, is the MVP's success criterion.
It is also the thing Scott can find and Marcus alone cannot.

Why this MVP and not a software-only one: a software-only ledger is closer, but it proves only that Marcus is a good coding-agent governor, which OpenHands is already selling and which Scott's thesis says the harness vendors will absorb.
The two-adapter MVP costs perhaps six extra weeks and proves the claim that the whole vision rests on.

---

## Part VIII: The next steps, applied to the six things

The order is dictated by dependencies, and the first three steps are the same regardless of which market wins on Wednesday.

**Step one, ownership and recovery: finish the session model.** Replace spawned-per-task agents with long-lived agents that register once, pull work, and go home when the board drains.
The design exists (PR #734), the first item is the restart fix so that a restarted board recognizes its live agents (obligation O1), the launcher is an existing file with the spawn loop removed, and the acceptance test is three agents on one project surviving a board restart mid-run.
This is the prerequisite for every other step and for any demo with an agent that dies.
Roughly four to six weekends.

**Step two, proof: enforce the gate.** When a contract specifies verification, require the evidence and refuse completion without it; reject evidence that shows a page was served but not rendered; print the "accepted on self-report" stamp in the record where a person will see it.
These are the first, second and fourth fixes in issue #735, and they are small.
Do them in parallel with step one.
One weekend.

**Step three, the handoff: write the protocol without git.** Rewrite the agent protocol in the ledger's terms: work item, contract, claim and lease, artifact, evidence, gate verdict, approval, cost.
Name the software specifics (repository, branch, test suite, merge) as one adapter's vocabulary.
This is writing, not code, and it produces the document that harness vendors, Scott, and a design partner can read.
Two to three weeks of evenings.

**Step four, the case record: the operational adapter.** Build the second adapter against the protocol: a case description becomes tasks with contracts; verification is a rule check against attached evidence; the "merge" is a recorded decision.
Use the synthetic claim.
Keep it small: five task types, one rule set, one evidence format.
Three to four weekends.

**Step five, approval: the rung.** Add an approval state to the board, a routing rule (by task type, amount, confidence), the approver's identity on the record, and the one-page case narrative the approver reads.
Two to three weekends.

**Step six, cost: bring the account inside.** Move the token ingestion from the companion project into the ledger, attribute per task and agent across vendors, and add the export.
Two weekends.

**Then the demo**, run three times, and the honest documentation release that makes the repository say what the code does, which should happen at the start, not the end, because it costs a weekend and every conversation with a potential partner begins with the README.

Twelve to fourteen weekends of engineering, plus the writing, is the MVP at weekend pace.
A partner who can take the writing, the demo production and the design-partner search off the engineering path shortens it to the engineering alone.

---

## Part IX: Foreseeable challenges

The challenges are known, and naming them is most of defending against them.

**The runtime is git-shaped.** The biggest technical risk is that generalizing task creation and verification takes longer than estimated because software-specific assumptions are everywhere in the code, a parser that expects product descriptions, gates that expect build commands.
Mitigation: the protocol first, the adapter second, and a ruthless rule that anything the operational adapter cannot express goes into the software adapter, not the core.

**Verification outside software is harder.** Code has tests; a claim has rules and judgment.
The gate can check that cited clauses exist and arithmetic reconciles; it cannot check that a fraud judgment was wise.
The product must be honest about that boundary, verified where it can verify, "self-reported" where it cannot, and a person where the stakes demand one.
A gate that pretends is worse than no gate, and the record of the blank-page snake game is the proof.

**Vendors will absorb the edges.** Anthropic added multi-agent orchestration to its hosted product in May; Microsoft and ServiceNow sell governance of everyone's agents; OpenHands sells a control plane for coding fleets.
Any single feature of the ledger can be copied by someone with more distribution.
The defense is the neutrality no single vendor can offer, the record that spans them, and the protocol, if the contract and evidence formats are open and adopted, the ledger is where everyone's agents write regardless of who owns the harness.

**The middle has no price yet.** Everyone who tried to sell coordination alone has been free or has died.
The ledger must be priced like the things buyers already pay for: per governed agent, per verified task, the way Microsoft and Salesforce price, and it must be sold to the budget holder who feels the pain (platform engineering or compliance) not to the developer who enjoys the tool.

**Enterprise sales is a different skill.** Design partners, security questionnaires, procurement, deployment in a customer's environment: none of it exists in Marcus's record, and none of it is a weekend project.
This is the challenge a partner solves or nobody does.

**One maintainer, weekends, no second reviewer.** The record is unambiguous: the plan has outrun the calendar every quarter, the review bot is dead, and the most careful design work of the summer was spent fencing a bug that could not yet occur.
The mitigations are the ones already learned (write the contract down before the code, time-box against actual weekend output, delete before adding) plus the one the record has never had: a second person.

**The discipline to say no.** A coordination product that recommends a team for every case will be wrong most of the time, because most work is better done by one agent.
The product's credibility rests on the coordinator's first decision being "does this need more than one agent?" and on being willing to answer no.
That is a design principle that must be built in from the first version, because it is the one users will test first.

**The co-founder decision itself.** Scott is looking for a co-founder.
A wedge in this market is a company.
Taking it means a different life from a research instrument with a curriculum; not taking it means the ledger probably stays a reference implementation.
Neither is wrong.
Deciding by default is.

---

## Part X: Why this is exciting, and whether you are the one

It is exciting for a reason that has nothing to do with enthusiasm and everything to do with timing.
Fifteen months ago, "agents should coordinate through shared state, not conversation" was a hypothesis Marcus bet on.
In 2026 it became the finding: CooperBench measured the curse of coordination, MIT measured what hand-offs lose, Google measured what hubs gain, and every company that made multi-agent work in production landed on Marcus's architecture without having heard of Marcus.
The argument is over and Marcus was on the right side of it.
What remains is the product, and the product's category was named eight weeks ago by the two firms whose naming makes markets.
Being early to a category that has just been named, with the architecture the evidence favors, is the position every founder wants and few get.

It is exciting, too, because of what the work is.
The six things are not glamorous; they are the plumbing of trust, who did what, who approved it, what it cost, was it right.
Plumbing is where durable companies are built, because it is what everyone needs and nobody wants to build twice.
And the problem is intellectually alive: the fencing failure in August, the storyboard's silent agent, the gate that must know its own limits, these are the distributed-systems problems of the 1980s and the accountability problems of the 2020s meeting in one product.
A person who likes understanding complex things will not run out of them here.

Now the honest answer to the qualification question, in both directions.

You are unusually qualified for the part that matters most and that almost nobody has.
You built the board-mediated architecture before the research validated it, which means you understand *why* it works, not just that it does.
You have fifteen months and 444 issues of documented evidence about how multi-agent systems fail (single-author verdicts, lying kanbans, respawn storms, phantom artifacts, gates that pass broken apps) which is the deepest failure corpus in the field and the raw material for every contract the ledger will ever carry.
You wrote the invariants that the successful deployments independently rediscovered.
You explain complex systems for a living, to engineers and to graduate students, and the document you praised yesterday is the standard the product's protocol and pitch will need.
You have credibility in open source and in its governance, which is exactly the open-source-to-enterprise path this layer gets bought through.
And you teach production AI inside a large industrial company, which means you have watched real practitioners meet these six problems.

You are not qualified, alone, for the rest, and the record says so plainly.
You have not sold to an enterprise, found a design partner, or run a security review.
You have not worked in claims, disputes or security operations, which is where horizon two lives.
You have one person's weekends, and the plan has outrun the calendar every quarter it has existed.
Those are not character flaws; they are the shape of a co-founder-sized hole, and they happen to be the shape of the person who reached out to you.

So: yes, it is worth being excited, because the thesis won and the market just opened.
And yes, you are uniquely qualified, for half of it.
The other half is the conversation on Wednesday.

---

## Closing

Marcus started as a project manager for agents and became, through failure after documented failure, a system that only trusts the record.
That turns out to be what the world is about to need: not more agents, and not smarter ones, but a ledger that lets an organization put many of them on one piece of consequential work and still know what happened.
The board is the system.
The next step is to make the board say so in words that do not mention git, and to run one claim through it end to end.
