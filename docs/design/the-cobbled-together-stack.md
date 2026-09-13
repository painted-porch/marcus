<!-- Design note, September 2026. Published with figures at https://claude.ai/code/artifact/731ac0bb-0c9c-4091-90a4-3b136a410dc2 -->

# The Cobbled-Together Stack

*What it takes to run AI agents on real business work in 2026, why the hard part is still built by hand, and what that means for Marcus*

---

## The argument in one paragraph

A company that wants an AI agent to do real work (read live business data, decide something, and take an action that has consequences) can now buy every piece that one agent needs, off the shelf, by the hour.
The moment a second agent has to cooperate with the first, six specific things have to exist that no vendor sells and no standard carries: a durable record of the case, a way to hand work from one agent to the next, a record of which agent owns what and what happens if it goes silent, a way to route decisions to a human, a way to know what each agent spent, and proof that the outcome is right.
Every company that runs multiple agents today has built those six things itself.
The companies that succeed all use the same shape to do it, and that shape is the one Marcus was built around.
The money so far has gone to the layers on either side of those six things, not to the things themselves.
That is the gap Scott is pointing at when he says production multi-agent work is "cobbled together." Marcus has four of the six, built for one domain.

Everything below supports that paragraph.
Vendor claims are marked as such.

---

## Three definitions

An **agent** is a program that uses a language model to decide what to do next, calls tools (a database, an email system, an API), looks at the result, and decides again, until the job is done.
A chatbot answers; an agent acts.

A **real agent**, in Scott's phrase, is one whose actions have stakes: it reads a customer's account, approves or denies a claim, moves money, changes a system.
If it is wrong, someone is harmed and the company is accountable.

A **multi-agent system** is several agents with different jobs working on one case.
A claims system might have one agent that checks coverage, one that checks weather records, one that checks for fraud, and one that writes the audit summary.
They are separate programs.
Something has to connect them.
This page is about that something.

---

## Claim 1: Everything a single agent needs can now be bought

In 2026 a team does not need to build the machinery for one agent.
It rents it.
Here is what "the machinery" means, in plain terms, and who sells it.

The **runtime** is the computer the agent runs on plus the loop that lets it think, call a tool, and think again.
Anthropic rents one at $0.08 per hour the agent is active, plus the cost of the words it reads and writes (Managed Agents, public beta since April 2026).
Amazon sells the same thing in smaller pieces: the loop by the CPU-hour, tool calls at half a cent per thousand, memory at a quarter per thousand events, identity checks at a penny per thousand (AgentCore).
Google, Microsoft and Salesforce sell equivalents; Salesforce charges ten cents per action or two dollars per conversation and reports more than $1.5 billion a year from it.

**Durable execution** is a product that remembers exactly where a long-running process was, so that when the computer crashes, the model times out, or a human takes three days to reply, the process resumes where it stopped instead of starting over.
The leading product, Temporal, is worth $5 billion on the strength of exactly this need, and one consultancy's summary of the state of the art is that "LangGraph plus Temporal is the common production stack", a framework for writing the agent, plus Temporal to keep it alive.

A **gateway** is a checkpoint every model call passes through, so the company can cap spending, log requests, and block disallowed tools. **Tracing** records what the agent did step by step. **Sandboxes** are disposable computers where an agent can run untrusted code safely. **Memory services** store what an agent has learned about a user. **Identity** products give an agent its own login and permissions, the way an employee has one; Okta's version shipped in April 2026. **Approval** products let an agent pause and ask a human in Slack or email before doing something risky.

Every one of these is bought and metered: per hour, per action, per token, per seat.
That is the layer Scott means when he says single-agent orchestration is "getting good." It is.

---

## Claim 2: The moment there are two agents, six things have to be built by hand

Buy all of the above and you have one agent that works.
Add a second agent that has to cooperate with it and six new problems appear.
No vendor sells a solution to any of them, no protocol carries them, and every engineering team that has published how it runs multiple agents describes building the same six things.

**The case record.** When two agents work on one claim, there has to be a single, durable file that both can read and write: what the case is, what has been found so far, what has been decided.
This is not the agents' memory (which is per agent) and not the chat transcript (which is per conversation).
Shopify, which runs an agent platform for its own engineers, says the design principle outright: "the session is the thing that must survive," and it built that record as an append-only log in a Postgres database.
Ramp, the finance company, built its own.
LinkedIn reused its internal messaging system.
Every one of them wrote it themselves.

**The handoff.** When agent A finishes and agent B starts, A has to give B what B needs and nothing else.
If A hands over free-form prose, B misreads it.
An MIT study measured this: in a chain of agents relaying a task, accuracy fell from 91% to 41% after two hand-offs when the hand-off was prose, and structured hand-offs (a form with defined fields) lost a third as much per stage.
Practitioners now say "the unit of design is the handoff, not the prompt." No protocol defines a handoff.
The most widely adopted agent protocol, MCP, has no concept of one agent handing work to another; the newer A2A protocol has a task with three states (submitted, working, completed) and nothing more.

**Ownership and what happens when an agent dies.** Which agent holds which task, right now?
If it stops responding, is it dead or just slow?
Who picks the work up, and how do you stop two agents from finishing the same task twice?
One audit of the protocol landscape put it plainly: "no standard mechanism exists for tracking which agent owns a task across protocol boundaries." Surveys show what that costs.
IT leaders report an average of twelve agents per company, half of them running in isolation with no awareness of the others; 85% of organizations say no one is formally accountable for agent behavior, and 54% have already had an incident.

**Approval.** Every serious deployment keeps money decisions with a person: Allianz's payouts, a staffing company's payment files, a security vendor's response plans.
The problem is routing.
If fifty agents each make twenty tool calls an hour and a tenth of those need a human, that is a hundred approvals an hour, and studies of humans supervising AI find that under load the most consistent behavior is agreeing with whatever the AI recommended.
What teams end up building is approval by rule (this kind of decision, above this amount, below this confidence, goes to this person) not approval by prompt.

**Cost.** An agent working on a task spends five to thirty times what a chatbot answer costs, and multi-agent systems multiply that: Anthropic measured its own research system at fifteen times a normal chat.
No observability product today attributes cost to a whole workflow across several agents; the tracing standard everyone is adopting (OpenTelemetry's conventions for AI) has no fields for it.
Uber's technology chief said the company's AI budget for the year was spent by April.
The one documented production control is Cox Automotive's: an automatic stop when a single turn costs more than a threshold.

**Proof.** Did the case actually get handled correctly, or did an agent say it did?
A benchmark built by Salesforce on its own customer-service data found frontier models succeed 58% of the time on a single-step task and 35% once the task takes several steps.
A study that deliberately injected errors into multi-agent pipelines found that tool failures recover fully, ambiguous hand-offs recover a third of the time, and quiet semantic errors (the agent confidently doing the wrong thing) recover never.
Companies that solve this build a separate checking agent: Capital One's simulates the outcome of a proposed action against policy before allowing it; Anthropic now sells "Outcomes" grader agents for the same purpose.

(The stack figure, bought layers versus the hand-built glue, is on the published page.)

Those six (case record, handoff, ownership, approval, cost, proof) are the cobble.
They sit between the bought layers, they are rebuilt by every team, they are unpriced by every vendor, and they are uncarried by every protocol.

---

## Claim 3: The companies that make multi-agent work all use one shape, and it is Marcus's shape

I found fourteen documented deployments of cooperating agents on real business work.
They share an architecture, and it is worth walking through one before naming the pattern.

Allianz Australia settles small food-spoilage insurance claims after storms, a refrigerator loses power, a family loses its groceries, the claim is a few hundred dollars.
The system has seven agents.
One is a planner that decides the order of work.
The others each check one thing: is the policy in force, was there a storm at that address on that date, are there signs of fraud, what is the payout.
A final agent writes the audit summary.
The planner sequences the specialists; the specialists never talk to each other; a human makes every payout decision.
Settlement time fell about 80% and the system was built in under a hundred days, by the company's own account.

That is the pattern, and it repeats.
A hub agent hands narrow jobs to specialist agents.
The specialists communicate through the case file, not with each other.
A person holds the consequential decision.
Capital One's car-dealer assistant is four agents where one plans, one checks the plan against policy before anything happens, one explains.
Deutsche Telekom runs customer service in ten countries with billing, contract and sales agents kept in separate compartments and a router in front deciding which one handles a request; about 30% of conversations that involve changing a customer's account go to a human.
Gradient Labs, which handles fraud disputes for financial companies, runs each dispute as a case that can live for days with an inbound agent, a back-office agent, a checking agent and "ask a human" available as a tool.
BNY, the bank, gives each of its 130-plus agents a login, an email address, limited permissions and a human manager, and logs every action with the reason for it.
C.H.
Robinson, the freight broker, reports thirty-plus agents each handling one step of a shipment.
Cox Automotive runs a hub over sales and service agents with the cost stop mentioned above.

The pattern that failed is the one that sounds most like a human team: agents talking to each other freely, or managers delegating to sub-managers.
Shopify's engineers advise avoiding multi-agent designs early.
Walmart built many agents and then collapsed them into four because customers and staff could not tell which one to use.
A Google study across 180 configurations found that a hub coordinating parallel work improved results by about 80%, while every multi-agent arrangement made sequential reasoning worse by 39–70%, and that agents without a hub amplified each other's errors four times more than agents with one.
Salesforce's own research describes agents "echoing", two accommodating agents talking themselves into an agreement that serves neither party, in one test a customer keeping the shoes and paying a restocking fee.

Hub, narrow specialists, communication through the case file, a human on the money decision, and a separate checker.
That is Marcus's architecture, stated as three invariants in its own design documents in April 2026: agents pull their own work, the board is the only channel, and verification belongs to the coordinator, not the agent.
Marcus arrived at it by watching its own agents fail; the companies above arrived at it the same way.

---

## Claim 4: Multi-agent is only worth it in five situations

The evidence is also clear about when *not* to use several agents, and a coordination product that cannot say "don't" is not trustworthy.

Multiple cooperating agents demonstrably earn their cost in five situations.
When evidence must be gathered broadly and each specialist adds something new, research, security investigations, claims validation.
When a regulated decision needs an independent checker, so that the agent doing the work is not the one grading it.
When a case runs for days and waits on outside events, a customer who has not replied, a document that has not arrived.
When different teams own different agents for organizational reasons, and the system must respect that boundary.
And when robots do the structured steps and agents handle only the exceptions.

In everything else (sequential reasoning, most coding, any early-stage product) one agent is faster, cheaper and more accurate, and the research says so consistently.

---

## Claim 5: The money has gone to the edges, not to the middle

Where investors and acquirers have put money in the last eighteen months tells you what buyers will pay for.

They pay for **identity and governance**: who is this agent, what may it touch, can we shut it off.
Companies selling that raised $120 million, $125 million and $85 million in single rounds this year, and more than $30 billion of acquisitions in the identity space closed, including Palo Alto's purchase of CyberArk and Cisco's of Astrix.
They pay for **observability**: what did the agent do.
They pay for **durable execution**: Temporal's $5 billion.
And they pay, most of all, for **finished outcomes**: Sierra and Decagon, which sell resolved customer-service conversations rather than tools, are worth $15.8 billion and $4.5 billion on revenue of roughly $200 million and $100 million.

They have not, so far, paid for coordination itself.
The best-known open-source multi-agent framework, CrewAI, has raised $18 million against an estimated $3 million in revenue.
The most popular open-source "company of agents" tool, Paperclip, is free.
OpenAI's board-driven orchestrator, Symphony, is free and labeled an engineering preview.
The best-known "kanban board for agents," Vibe Kanban, shut down in April because "the vast majority are free users and we couldn't find a business model."

The analysts have just named the category.
Gartner called it "AI Agent Management Platforms" in July 2026 and predicts such platforms will mediate half of all agent-to-agent interactions by 2030; the thirty products it lists have almost no customer reviews.
Forrester calls it the "agent control plane," defines it as software that "inventories, governs, orchestrates, and assures heterogeneous AI agents across vendors," and expects the market to solidify in twelve to twenty-four months.
Gartner also still stands by its 2025 prediction that more than 40% of agent projects will be cancelled by the end of 2027, for cost, unclear value, or inadequate controls.

The five existing products closest to a neutral coordination layer each stop short of it.
Temporal keeps processes alive but has no notion of a task's owner, its verification, or its approval.
OpenHands' Enterprise Agent Control Plane, launched in May, is the nearest feature match (parallel work, policies, per-user audit, cost with budgets, any model) but only for software-engineering agents.
Lyzr's control plane, launched in July, checks agents before they are deployed, not while they work.
ServiceNow's Control Tower watches and can kill agents from any vendor but does not assign work, and requires ServiceNow.
Paperclip does coordinate tasks with budgets and audit trails and has no company behind it.

So the middle is open, and it is open because nobody has yet found the price for it, not because nobody wants it.
Being early is a different problem from being late.

---

## Claim 6: Enterprises want neutrality in the middle and one front door

Scott's strongest claim is that big companies will not accept a coordination layer that belongs to OpenAI or Amazon, because they know they will run agents across many models and vendors.
The evidence supports half of it.

They do run many models: 37% of chief information officers have five or more in production, and three-quarters of engineering teams surveyed use more than one.
Sierra fails over across fifteen.
Deutsche Telekom partitions by country for data-sovereignty reasons.
But 88% of enterprise spending on models goes to three providers, tuned prompts create real switching costs, and Walmart's lesson was to consolidate entry points, not multiply them.
What enterprises want is neutrality at the model and tool layer, and a single control point above it.
A vendor-neutral coordination layer fits that; a vendor-neutral everything does not.

---

## What this means for Marcus

Marcus is an open-source server that decomposes a project into tasks on a shared board, lets AI coding agents from any vendor pull tasks, work in isolation, and report back, and checks their work before accepting it.
Measured against the six hand-built things, here is where it stands.

| The six things every team builds | Does Marcus have it? |
|---|---|
| A durable case record agents pull work from | Yes, a database-backed board with persisted task leases and a log of every decision and artifact. Built for software projects. |
| A structured handoff between agents | Yes, for code, contracts and scoped artifacts delivered with each task. Not yet expressed in domain-neutral terms. |
| Ownership, liveness, recovery | Yes, leases, timeouts, a circuit breaker for agents that keep going silent, and a way to send bad work back. Hardened in August. |
| Approval routing to a human | No. Proposed twice, never built. |
| Cost per agent and per task | Yes, nearly a million recorded agent turns attributed per task; the reporting side lives in a companion project. |
| Proof that the outcome is right | Partly, verification gates exist but are currently optional; the most recent run passed a broken app. Must be enforced. |

Four of six, in software-shaped form.
Approval and identity are missing, and they are also the two areas with the most funded competition, which argues for integrating an Okta or a Permit.io rather than building.
Marcus's rarest property is one the table does not show: it already runs agents from Anthropic, OpenAI and Google against one board.
That is the neutrality Claim 6 says enterprises want and no single vendor will build.

The largest gap is not a missing component.
It is that Marcus's runtime is built from software-development parts (git branches, merges, test suites) and an insurance claim has none of those.
The *shape* transfers; the *implementation* does not, yet.

That leaves two credible first moves, and they differ exactly where Scott and the evidence disagree.
The first stays in software: sell the ledger (ownership, handoff, approval, evidence, cost) for companies running *mixed* fleets of coding agents from several vendors.
OpenHands proved in May that someone will buy that; the research base already exists; Marcus's runtime already fits.
Scott would say the coding-agent vendors will absorb it; the counter is that governance *across* vendors is the one thing no single vendor will build.
The second is Scott's: pick one of the five situations from Claim 4 (claims validation, disputes, security investigations) and build the ledger there.
It is the larger market, and it requires rebuilding the runtime and finding customers Marcus has no way to reach alone.
Either way, the next three engineering steps are identical: long-lived agents that pull from the board instead of being spawned per task, verification that is enforced rather than optional, and a description of the six things in words that do not mention git.

---

## What to find out on Wednesday

Scott has been thinking about this space and is deciding whether to build in it.
The useful questions are about what he has seen and what he would do, not reactions to Marcus's story.

Which of the six hand-built things do the teams he knows curse most, and what did they build it with?
Which of the surrounding layers would he treat as partners (Temporal below, ServiceNow and Microsoft above, Okta beside) because that decides where the product's edges are?
Who buys, in his experience: the platform-engineering team, the compliance team, or the application team, and what triggers the purchase (an incident, an audit, a cost overrun?
Would he start with a platform or with one vertical, and which?
What would he bring) customers, go-to-market, capital, a first design partner, and what does he expect from the technical side?
And would he push back on the one conclusion this research reaches: that the sellable thing is a ledger of agent work with ownership, approval, evidence and cost, priced per governed agent or per verified task, not a "coordination platform."

One question is not for him.
A wedge in this space is a company (design partners, security reviews, a sales motion, a co-founder) and Scott is looking for one.
The same coordination model can serve a company or a research instrument with a curriculum attached; the research says the middle of this market is open and early, which means the opportunity is real and the commitment is large.
That is the decision on the table, and it is worth knowing your answer before he asks.

---

*Principal sources: Anthropic Managed Agents pricing and Agent SDK documentation; AWS AgentCore pricing; Google Vertex Agent Engine announcements; Microsoft Agent Framework and Agent 365 materials; Salesforce FY27 Q2 results; ServiceNow AI Control Tower releases; Temporal Series D coverage and Gradient Labs case study; MCP 2026-07-28 specification post; Linux Foundation A2A announcements; Okta for AI Agents release; Shopify "Under the River"; Ramp platform coverage (InfoQ); LinkedIn multi-agent engineering; Allianz Project Nemo; Capital One coverage (VentureBeat); Deutsche Telekom LMOS (InfoWorld); C.H. Robinson Q2 2026 materials; Forrester on BNY and on the agent control plane; Cox Automotive re:Invent notes; Google Research "Towards a science of scaling agent systems"; MIT relay-chain study (arXiv 2603.26993); OrchestraBench (arXiv 2608.05263); Anthropic multi-agent research system; Salesforce CRMArena-Pro; Menlo, a16z, McKinsey, Camunda, MuleSoft, Gravitee and LangChain surveys; Gartner AI Agent Management Platforms overview and cancellation prediction; funding and acquisition coverage from GeekWire, SiliconANGLE, BusinessWire, TechCrunch and company releases. Vendor-reported figures are identified as such; full source lists are in the research notes saved with this page.*
