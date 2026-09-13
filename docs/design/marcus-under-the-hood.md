<!-- Design note, September 2026. Published with figures at https://claude.ai/code/artifact/87b68846-a29f-422d-a616-5773036371cd -->

# Marcus Under the Hood

*How agents find Marcus, who writes what, and the technical checklist from the session model forward*

---

## The one fact everything follows from

Marcus never calls an agent.
Agents call Marcus.

Marcus is a server that speaks MCP, the Model Context Protocol, the standard way an AI agent calls a tool.
To an agent, Marcus looks like a toolbox with seventeen tools in it: `register_agent`, `request_next_task`, `report_task_progress`, `get_task_context`, `log_decision`, `log_artifact`, `report_blocker`, `request_task_redo`, and a few for the person running things.
MCP is request-and-response only: a tool does nothing until an agent invokes it, and there is no channel by which Marcus can reach out and tap an agent on the shoulder.
So every question about "how do the agents know to reach out" has the same root answer: something outside Marcus starts the agent and tells it to ask, and the asking is the agent's job.
Marcus's own design rule says the same thing on purpose, Invariant #1, "agents self-select work; Marcus never pushes."

That one fact settles the five questions you asked.
Here they are, each answered twice: how it works in the code today, and how it works in the ledger the last document described.

---

## Question 1: How do agents know to ask Marcus for work?

**They are told, in writing, before they start.** Every agent is launched with a system prompt (a document that sits in front of everything else it reads) and that document is Marcus's worker instructions.
In the repository it is `prompts/Agent_prompt.md`, about 370 lines.
It tells the agent which tools exist, to register once, to ask for work, what to do with a task when it gets one, when to report progress (25%, 50%, 75%, done), how to log a decision or an artifact, what a blocker is, and what to do when there is no work ("sleep the number of seconds in `retry_after_seconds`, then ask again").
Its first line is the whole contract: "You MUST maintain a continuous work loop.
After completing ANY task, IMMEDIATELY request the next task from Marcus."

How that document reaches the agent depends on the agent.
Coding agents each read a conventions file from the directory they start in (Claude Code reads `CLAUDE.md`, Codex reads `AGENTS.md`, Gemini CLI reads `GEMINI.md`) so the runner copies the worker instructions into each agent's working directory under whichever filename that agent's harness expects (`copy_agent_workflow_to_implementation` in the runner).
For an agent that does not read a file, a person pastes the same text into its system prompt.
Either way, the agent wakes up already knowing the protocol.

A second layer of instruction arrives with each task.
When an agent calls `request_next_task`, Marcus returns not just the task but a nine-step "mandatory workflow" for it, check dependencies, read upstream artifacts, do the work, report at milestones, log decisions, verify before claiming done, report completion, be ready for remediation, "immediately request next task", plus the task's contract, acceptance criteria and the context it needs.
A third layer is the tool descriptions themselves: MCP lets a server describe each tool, and an agent reads those descriptions when it connects, so `report_task_progress`'s description tells it what evidence to include.

**In the ledger,** the same three layers remain and the first one becomes a published document: the session contract.
The difference is only in who has to write it into whose directory, the launcher does it for every harness it starts, and any agent runtime that wants to join a board publishes its own adapter that installs the contract.

---

## Question 2: Are agents on an active loop?

**Today, no, and that is the bug the session model fixes.** The instructions say "continuous loop," but the way Marcus actually runs agents is the opposite: the experiment runner starts one agent process per task, that agent does exactly one task, and the runner kills it.
The loop exists, but it lives in the runner, not in the agent: the runner polls Marcus for how many agents the board could use right now (`get_desired_agent_count`), starts that many, watches them, and starts more as tasks free up.
A second copy of the worker instructions in the runner's own templates says "one task, then EXIT," and that copy wins because the runner uses it.
This is why an agent can receive "immediately request next task" from Marcus and "do not request another task" from its own prompt in the same session, issue #736 caught the contradiction in the transcripts.

That arrangement is also where four of the six worst bugs in the record live: agents multiplying (279 worktrees for nine tasks), a watchdog killing slow-but-healthy agents, an endless respawn of one integration task, and a merge failure that could never recover.
All of them are consequences of Marcus's runner babysitting processes.

**In the session model (the first item on the checklist below) the loop moves into the agent, where the instructions always said it was.** A thin launcher starts N agents and then does nothing else.
Each agent registers once and loops on its own: ask for work; if there is a task, do it, report it, ask again; if there is none, wait the interval Marcus returns and ask again; if the board says it is drained, exit.
Marcus tracks each agent by exactly one thing, the lease it holds, and stays deliberately blind to whether a process is alive.
An agent that goes silent past its lease is presumed gone and its task is returned to the board; if it turns out to have been alive, the ledger notices the late report and refuses to apply it twice.

Two details make this work and are both on the checklist.
The "no work" response must carry a real interval, not the constant 30 seconds it carries today, and a little randomness, or five agents started together will all wake in the same second forever.
And "the board is drained" must be something the board computes (no claimable work and nothing in flight) rather than, as today, a signal that depends on an AI agent remembering to call `end_experiment`.

---

## Question 3: Who builds Marcus's decomposition prompt?

**Today, Marcus's maintainer does, and it lives inside Marcus.** Decomposition is what happens when someone calls `create_project` with a plain-English description.
Marcus sends that description to its *own* language model (the "planner," configured separately from the agents' models by `ai.provider` and a key in `.env`) through a sequence of prompts that are hard-coded in the server: analyze the description (`_analyze_prd_deeply`), generate a task hierarchy (`_generate_task_hierarchy`), or, on the contract-first path, generate interface contracts per domain first (`_generate_contracts_by_domain`) and shape tasks around them, then run "augmenters" that check the tasks cover the stated outcomes and add acceptance criteria, then append a final integration-verification task (`enhance_project_with_integration`).
None of this is visible to the agents; they receive the resulting tasks.
None of it is configurable by a user beyond a handful of switches, which decomposer, what complexity level.

So there are two language models in every Marcus run, and it matters to keep them apart.
The planner is Marcus's brain: it decomposes, generates contracts, judges evidence.
The agents are whatever the operator brought (Claude Code, Codex, Gemini, a local model) and they do the work.
An operator can run a strong planner with cheap agents, which is exactly the economics Cursor's swarm paper found.

**In the ledger, the decomposition prompt becomes part of an adapter, and the adapter is Marcus's product.** An adapter is everything that is specific to a kind of case: how a description becomes tasks with contracts, what the contract schema looks like, which verifiers exist, which transitions need a person.
The software adapter is the one that exists today.
A claims adapter would have its own decomposition (this kind of claim becomes coverage, event, fraud, payout and audit tasks) and its own contract fields.
Marcus's makers write the adapters, because writing a decomposition that produces good contracts is the hard, evidence-driven work the last fifteen months were spent on.
What a customer supplies is the inputs the adapter consumes: the policy rulebook, the thresholds, the systems of record, the approval routes.
Over time an adapter can expose templates a customer tunes, but the core "how to cut a case into tasks" stays with the adapter, for the same reason a database vendor writes the query planner and the customer writes the queries.

---

## Question 4: Who writes the gating mechanism?

**Today, Marcus's code does, and the rule about it was written in May.** The gate is the part of `report_task_progress` that decides whether "done" is accepted.
For ordinary implementation tasks it runs the project's own tests against only the files the agent changed and has the planner model review the evidence with a check that every cited file and line exists (`WorkAnalyzer`); after three failures it accepts with an annotation.
For the final integration task there is a second gate that is supposed to build and run the product and judge behavior evidence, a rendered page, a passing command.

The rule is Invariant #2, version 2, amended on 24 May 2026 after an agent shipped an unplayable game by rewriting its own verification commands until one passed: *how the work will be checked is part of the task contract, authored by Marcus's setup pipeline, not by the agent.* The agent owns the implementation; the contract owns the test.
That is the correct rule, and the code currently violates it, six days after the rule was written, a change made behavior evidence optional, and the most recent run passed a blank page with a log line nobody reads.
Re-enforcing it is the second item on the checklist and the smallest.

**In the ledger, the gate is generic and the verifiers are adapter-specific, and neither is written by the agent.** The generic part: every contract names its verifiers; completion is refused without their evidence; a verdict is stamped on the record; when no verifier can decide, the record says "accepted on self-report" in letters a person will see.
The adapter part: the software adapter's verifiers are tests, a build, and a browser loading the documented run command; a claims adapter's verifiers are "every cited clause exists in the policy," "the payout arithmetic reconciles with the coverage limit," "the event date matches the claim date." Marcus ships the verifier types; the customer's rulebook is their input; a person is itself a verifier type, invoked by the approval rung when the contract says so.
The one thing the design must never do is let the agent author the check it will be graded by, that is the whole lesson of the blank-page game.

---

## Question 5: Is Marcus set up per case type?

**Today it is set up for one case type, software, with knobs.** The knobs are a complexity level (prototype, standard, enterprise) that changes how many tasks are produced, and a decomposer strategy (feature-based or contract-first) that changes how they are shaped.
Everything else (the parser that expects a product description, the worktree per task, the merge to main, the tests as verification, the integration task at the end) assumes software.

**In the ledger, yes: Marcus is configured per adapter, and per customer within an adapter.** An adapter is chosen when a board is created (this board handles storm-damage claims).
The adapter fixes the task types, the contract schema, the verifiers and the approval points.
The customer's configuration fills in what the adapter needs to know about *their* claims: the rulebook, the evidence sources, the amounts above which a person signs, who that person is.
Two insurers running the claims adapter share the decomposition logic and differ in their rulebooks and routes.
That split (adapter logic owned by Marcus, policy inputs owned by the customer) is what keeps the product general without pretending one prompt fits every business.

---

## Question 6: Does Marcus need to know which agents exist and what each can do?

**Today it knows a little and uses less.** When an agent registers it declares an id, a name, a role and a list of skills.
When it asks for work, Marcus scores the available tasks: a skill-match score (agent skills intersected with the task's labels), the task's priority, a penalty for deployment tasks, and an AI-ranked choice among candidates when that is enabled.
In practice the runner registers every agent as a generalist, and the worker instructions say so in as many words: "You CANNOT refuse tasks based on perceived skill mismatch, you are a unicorn developer." Skill matching exists in the code and is nearly inert in use, which one issue (#406) already flagged.

**In the ledger, Marcus needs to know two things about an agent and refuses to know a third.** It needs a *capability profile* at registration (what kinds of task this agent may take, and under what permissions) so that the board can filter: a payout task is claimable only by an agent with the finance permission, or by a person.
And it needs an *identity* it can put on the record, so the ledger can say which agent did what.
What it refuses to do is assign: it never picks an agent for a task.
It marks tasks claimable-by-whom and lets eligible agents pull; that is Invariant #1 and it is also what makes vendor neutrality real, because the board does not care whether the agent that qualified is Anthropic's, OpenAI's or a person.
Which agents exist at all (how many, from which vendor, on which model) is the operator's decision, expressed through the launcher (`--sessions N`, `--harness`) and, later, per-role model choices.
Marcus observes that fleet; it does not manage it.

---

## The picture, in one figure

The figure below is the loop as it will run after the session model lands.
Read the left column top to bottom for the agent's life; read the right column for what Marcus does at each step.
Nothing crosses from right to left except as a reply.

<div class="loop">
<div class="col agent"><h4>The agent's side</h4>
<ol>
<li><b>Started by the launcher</b> with a working directory, its conventions file (the session contract), and Marcus's address.</li>
<li><b>Registers once</b> (id, name, capabilities, project) via <code>register_agent</code>.</li>
<li><b>Asks for work</b> via <code>request_next_task</code>.</li>
<li><b>Works the task</b> in its own environment; calls <code>get_task_context</code>, <code>log_decision</code>, <code>log_artifact</code> as it goes; reports at 25 / 50 / 75%.</li>
<li><b>Reports done</b> with evidence via <code>report_task_progress</code>.</li>
<li><b>Asks again.</b> On "no work," sleeps the returned interval; on "drained," exits.</li>
</ol></div>
<div class="col marcus"><h4>Marcus's side</h4>
<ol>
<li>Earlier, someone created the board: the planner model decomposed a case into tasks with contracts (the adapter's job).</li>
<li>Records the registration; issues an identity for the ledger.</li>
<li>Picks the best claimable task for this agent, issues a <b>lease</b> and an epoch, returns task + contract + context + workflow instructions.</li>
<li>Extends the lease on each report; logs decisions and artifacts to the case record; attributes the agent's spend to the task.</li>
<li>Runs the <b>gate</b> the contract specified; refuses without evidence; routes marked transitions to a person; records the verdict; releases the lease; makes downstream tasks claimable.</li>
<li>Computes "no claimable work and nothing in flight" from the board itself; tells the agent to go home.</li>
</ol></div>
</div>

---

## The technical checklist

Ordered by dependency.
The first item is the session model because nothing else can be demonstrated without long-lived agents.
Each item names where it lives and what "done" means.
Estimates are weekends at the pace the record shows.

### 0. Truth and weight (one weekend, do first)

- [ ] Make the repository say what the code does: strike the CHANGELOG's PyPI claims until a wheel ships; restore `.env.example`; state 17 agent tools, not 10; mark GitHub and Linear providers unsupported; fix `./marcus --version` (prints 2.0.0); document that telemetry is on by default; fix the smoke-gate tool description in `server.py` (~lines 1446 and 1683) to say what the gate actually does.
- [ ] Delete dead weight: the legacy `experiments/` tree (1,616 lines, functionally broken prompt), `src/ttt2/`, dormant learning and mode modules, the duplicate Planka clients, the 14 phantom `pipeline_*`/`what_if_*` names in `tool_groups.py`.
- [ ] Keep CI green: mlflow is now pinned (#733); mark the ~136 unit test files that lack `@pytest.mark.unit` so `pytest -m unit` runs the whole suite; either fix the Claude review action's 401 or remove the workflow.
- [ ] Close or merge the two sprint PRs open since May (#577, #580).

### 1. The session model (Phase 3 of #706; four to six weekends)

- [ ] **O1, registration survives a restart.** Persist `agent_status` and `agent_project_map` and rehydrate them on the *lazy* path (the first `request_next_task` after a restart), because the startup path never runs in HTTP mode. Add eviction/unregister so the roster is neither empty after a restart nor inflated forever. Stop the runner registering `{agent_id}_sub{i}` phantom subagents. Done when: restart Marcus under three live workers and they keep pulling work.
- [ ] **The launcher.** `dev-tools/experiments/runners/run_experiment.py` minus the spawn loop, plus `--sessions N`. It starts N sessions and does nothing else: no restart of dead workers, no watchdog. Keep the `worktrees/<agent_id>` path shape (Cato's cost ingestion reads it; rename and cost tracking silently stops). Keep writing `CLAUDE.md`/`AGENTS.md`/`GEMINI.md` via each harness's `workflow_files`, or Codex and Gemini workers start with no instructions. Invert `--print` mode: sessions must stay alive after their first turn.
- [ ] **One session contract.** Delete the "one task then EXIT" copy in `dev-tools/experiments/templates/agent_prompt.md`; make `prompts/Agent_prompt.md` the only worker prompt. Add: the drained-stop signal, the checkpoint duty (commit and `log_decision` at least every 20 minutes), lease-epoch awareness, idempotent re-registration, `request_task_redo` in the tool allowlist (it is absent today, so no operator-run agent can use it), and the TDD-as-standard block that only the runner copy carries. Extend `tests/unit/test_worker_prompt_contracts.py` to pin the loop contract, not just blocker semantics.
- [ ] **A real drained signal.** Replace `EXPERIMENT_COMPLETE` (which fires only if an AI agent calls `end_experiment`) with a board-computed condition: no claimable tasks and no live leases. The launcher, not a model, decides that a run has ended.
- [ ] **`retry_after` with jitter.** #732 pinned the interval and recorded the jitter question; add randomized backoff once the loop exists to test it against.
- [ ] **Retire the babysitters.** Remove `StallWatchdog` and `SpawnThrashDetector` from `spawn_controller.py`, but preserve their one good idea: the tally of MCP activity (completions, progress reports, context requests, artifacts, decisions, blockers) as the definition of "not silent" for liveness.
- [ ] **Worktree teardown.** Nothing removes a worktree today; add removal when a session goes home.
- [ ] Close #628 and #703 as deleted-by-design; retire the spawn-backoff half of #667.
- [ ] **Acceptance run:** three sessions, one small project. Kill one mid-task; its lease expires; another session claims and finishes. Restart Marcus mid-run; all three keep working. Board drains; all three exit on their own. `token_events` rows attribute to the right agents.

### 2. Merge unit and liveness (after the loop runs; three to four weekends)

- [ ] **#730, merge by commit range, not by branch.** Under long-lived sessions one branch spans many tasks; merging "the branch" re-merges rejected work. Persist the baseline commit with the assignment, capture it in the session worktree, merge `baseline..HEAD` only, scope `_verify_agent_has_commits` to that range, and fix the recovery handoff wording that still says "merge the agent's branch."
- [ ] **Merge execution out of `report_task_progress`**, and out of the second entry point gate 1 found on the *claim* path (`_sweep_blocked_merge_conflicts`, called from `request_next_task`). Keep merge evidence; a clean merge fast-forwards; a conflict opens an integrator card for an agent to claim. Move the smoke gate's working directory to the session worktree (#652), or the gate verifies a `main` that provably lacks the work.
- [ ] **D3, split liveness from lease duration.** A silence timeout (default 45 minutes, ≥ 2× the checkpoint cadence as a config invariant) plus a generous budget ceiling. Delete `max_renewals` and `stuck_task_threshold_renewals` (one is warn-only, the other is read by nothing) and then address the mechanism that actually reclaims healthy long tasks: the progressive-timeout curve and the second renewal-duration curve in `assignment_lease.py`.
- [ ] **Decide the lease re-grant's fate, then D11.** The documented "the agent's next report recreates the lease" compensation and the epoch fence cannot both be authoritative, that contradiction is what failed three review passes. Once D3 makes recovery trustworthy, delete the re-grant and enforce the epoch where it matters: at the DONE write and at the merge (#730 is the same defect from the other end). Read `_observe_epoch_collision` counts first; size the design to the measured rate.
- [ ] **Cost binding and the run concept.** Record session→agent/project at registration and claim (M6); keep the Cato path contract or change both repos together; evaluate renaming `run_id` to `session_id` rather than deleting it (M7), and decide explicitly whether by-decomposer cost slicing is being retired.

### 3. Proof: enforce the gate (in parallel with section 1; one weekend)

- [ ] Require behavior evidence when a task category has a behavior contract (`has_behavior_contract`); reject through the existing `_behavior_evidence_rejection(evidence_missing=True)` path, which is currently unreachable; keep the two-rejection ceiling so gridlock cannot return.
- [ ] Make the web judge tell served from rendered: reject a DOM equal to the static `index.html` shell or with an empty root container; require a marker only JavaScript produces.
- [ ] Put `independently_verified` where people look: the `report_task_progress` response, `get_experiment_status`, and the runner's `RESULT` line ("13/13 complete, integration SELF-REPORTED").
- [ ] When a headless browser is available, have Marcus run the documented start command and load the page itself, failing on any page error or empty root; Playwright's `webServer` also kills the orphaned dev servers #735 found.
- [ ] For TypeScript-plus-bundler scaffolds, require `isolatedModules` in the type-check config as an acceptance criterion; it turns the blank-page class into a red build.
- [ ] Add #735's two regression fixtures; forward `verifications`/`evidence` on the stdio dispatcher too (three registration paths currently diverge).

### 4. The handoff: the protocol without git (two to three weeks of evenings)

- [ ] Rewrite `PROTOCOL.md` and the session protocol draft in ledger terms: work item, contract, claim and lease, artifact, evidence, gate verdict, approval, cost account. Name repository, branch, test suite and merge as the software adapter's vocabulary. Version the contract schema (#646).
- [ ] Fix what #736 measured: send the contract once (drop the duplicate payload from `get_task_context`); scope acceptance criteria to the task, with project-wide outcomes on the integration task only; key boilerplate criteria to the task's structural category; let contracts reference only artifacts that exist on `main`; pin shared configuration in the scaffold contract; remove the filler block and the instruction that contradicts the prompt.

### 5. The case record: the operational adapter (three to four weekends)

- [ ] Define the adapter interface: task creation (a case becomes tasks with contracts), verifier types, evidence formats, and what "integration" means when there is no merge (a recorded decision). Move git-specific code behind the software adapter; anything the operational adapter cannot express goes there, not into the core.
- [ ] Build the claims adapter on synthetic data: five task types (coverage, event, fraud, payout, audit), one rulebook, evidence as attached records; three verifiers (cited clause exists, arithmetic reconciles, dates match).
- [ ] A case-level view on the board and a ledger export that answers the five questions: what was worked, by whom, what was decided, what it cost, was it verified.

### 6. The approval rung (two to three weekends)

- [ ] A `pending_review` task state (#592, proposed May 2026) with routing rules by task type, amount and confidence; the approver's identity on the record; a one-page case narrative generated from the ledger for the approver to read; approval as a transition the gate requires when the contract marks it.

### 7. The cost account (two weekends)

- [ ] Bring worker token ingestion inside Marcus (or make the Cato contract explicit and tested); attribute per task, agent and vendor; stamp role and model tier on sessions (#717); log Marcus's own planner usage (#736 found none recorded); add a per-run budget circuit breaker in the style of Cox Automotive's cost-per-turn cap.

### 8. The demo, and the measurement

- [ ] The storyboard, run for real: four agents from three vendors on a synthetic claim; one killed mid-task and recovered; one completion refused for a fabricated citation; one human approval; one export. Three unattended runs, same result.
- [ ] Then the three-arm coordination-tax experiment (#529): one agent alone, one Marcus worker, N Marcus workers, same spec, same model, cost and behavior both measured, the number the product's honesty depends on.

Twelve to fourteen weekends of engineering plus the writing, in the order above.
Sections 0, 1 and 3 do not depend on which market wins on Wednesday; everything from section 5 on does.
