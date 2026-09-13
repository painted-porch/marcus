<!-- Working plan, 12 September 2026. Published as an interactive page at https://claude.ai/code/artifact/aef1b1b3-356d-41bf-a5a1-d3eaab429106 -->

# The Ninety Days

Eight things, in order, from September to December 2026.
The rule for the whole period: nothing irreversible until December.

## 1. Three sends (This week)

Jeni's answers, the two Anthropic applications, one paragraph to Scott.

### What it is

Send Jeni the pre-session answers as they are (the file "Pre-Session Answers for Coach Jeni" in this chat), including the line that you wrote all eight because you could not pick two.
That line is the most useful thing she will read.

For Anthropic, dictate five minutes of raw thinking on three prompts: why Anthropic and not another lab or a startup; one specific story of a room of engineers learning to build with Claude; what you would want to be true about Anthropic's technical instruction a year after you joined.
Anthropic's candidate guidance says the first draft of written answers must be yours; I shape it into the 200 to 400 word "Why Anthropic?" answer and a short note for the optional box.
Then submit Lead Technical Instructor (New York) first and Head of Technical Training (San Francisco) second, each with its own resume PDF from this chat.

Have the form answers ready before you sit down: earliest start date, deadlines, relocation yes or no, your working address or the word "relocating", yes to 25 percent in office, yes to the AI policy, and the visa questions.

Send Scott one paragraph, in your words, close to this: "I've been chewing on your operational-side framing and landed on a narrower claim: the missing piece is the system of record for agent work, the claim-level record of who did what, who checked it, what a person approved, and what it cost, vendor-neutral, with research as the first case type.
Is that the wedge you meant, or a feature inside it?"

### Why it matters

All three are cheap, reversible, and produce evidence.
None of them commits you to anything.
Together they turn three open questions (which coach, whether the lab door is real, whether Scott meant this) into answers that arrive on their own schedule while you build.

### Done when

Jeni has the file, both applications show as submitted in Greenhouse, and Scott's reply is in your inbox or two weeks have passed without one, which is also an answer.

### Learn more

- [Lead Technical Instructor posting](https://job-boards.greenhouse.io/anthropic/jobs/5415537008)
- [Head of Technical Training posting](https://job-boards.greenhouse.io/anthropic/jobs/5415529008)
- [Anthropic's guidance on candidates' AI use](https://www.anthropic.com/candidate-ai-guidance)
- Why the system of record is my framing and not Scott's (chat, 12 Sep)

## 2. The rule (Ninety days)

No money raised, no quitting, no agreements signed, until December.

### What it is

For ninety days you make no irreversible commitment: no investment taken, no resignation, no co-founder or advisor agreement, no term sheet.
Everything else is allowed and encouraged: applying, building, publishing, talking to anyone.

The rule is not caution for its own sake.
The decision that feels like it will define the rest of your life does not exist yet.
It only exists when a specific offer, term sheet, or agreement is on the table, and then you evaluate that one thing with more evidence than you have today.
Until then, every path is reversible, and the rule makes that true by construction rather than by willpower.

December is the review date, not a deadline to have chosen a life.
It is when the list of five options gets shorter because the probes (the applications, the experiment, Scott's answer, the coaching) have returned.

### Why it matters

It removes the weight from the ninety days without removing the work.
You told Jeni you want a process and a date; this is the date, and the rule is what lets you work without re-deciding every morning.

### Done when

It is done every day you keep it.
Write it on the first page of whatever notebook the coach has you keep.

### Learn more

- The five options you are choosing from (chat, 12 Sep)
- [What Marcus Becomes: how far away each piece is](https://claude.ai/code/artifact/04ad38fa-0a84-433d-b070-8a4fb8e694c0#part-vi-how-far-away-marcus-is)

## 3. The product change (Weekend one)

Patch steps 1 to 5 on develop, tests first: task type, principals, eligibility, evidence, approval.

### What it is

Step 1, task type.
In src/core/models.py add kind (or task_type, your call), inputs, and output_schema to Task, and persist them in src/integrations/providers/sqlite_kanban.py with a default of implement so every existing board loads unchanged.

Step 2, principals.
Extend register_agent in src/marcus_mcp/tools/agent.py with vendor and principal (agent or human), stored on WorkerStatus, and declare them in the tool's input schema.

Step 3, eligibility.
New module src/marcus_mcp/coordinator/eligibility.py with is_eligible(agent, task, state).
A verify task is never offered to the claim's author or the author's vendor; an approve task only to a human principal.
Apply it in both assignment entry points: _find_optimal_task_original_logic in tools/task.py and the coordinator's find_optimal_task_with_subtasks.

Step 4, evidence to close.
In report_task_progress, when status is completed and the task is a find, verify, or synthesize, validate evidence against output_schema and reject with the missing fields named; reuse the retry ceiling from issue #677 so a stuck agent terminalizes instead of looping.
Leave the implement kind on the existing smoke gate.

Step 5, approval.
An approve task closes only when the caller is a human principal.
Dependents already wait through dependency resolution.
Add the smallest human interface: a CLI that lists open approvals and completes one.

Write the tests before each step, per the repository's own rules: a finder is never offered its own verify task; a same-vendor verifier is refused; a different-vendor verifier is offered; a human is offered approve tasks and nothing else; a completion without evidence is rejected.

### Why it matters

These five steps are the whole difference between a board that trusts agents and a board that enforces.
None of them touches the git runtime, and none crosses the Bright Line: eligibility is the board declining to offer, evidence is the contract Marcus already authors under Invariant #2 v2, approval is a task only a person can take.

### Done when

The five test groups pass, and a three-task case seeded by hand on SQLite runs end to end with the eligibility refusal visible in the log.

### Learn more

- [What has to change in Marcus (keep, add, strip table)](https://claude.ai/code/artifact/2eed6f40-f3e2-47b0-8e23-8627a80c332f#what-has-to-change-in-marcus)
- [Patch outline, steps 1 to 9](https://claude.ai/code/artifact/2eed6f40-f3e2-47b0-8e23-8627a80c332f#patch-outline)
- [Marcus Under the Hood: the session-model checklist](https://claude.ai/code/artifact/87b68846-a29f-422d-a616-5773036371cd#the-technical-checklist)
- [The Marcus repository](https://github.com/painted-porch/marcus)

## 4. The experiment (Weekend two)

Runner, two workers, export, grader; then the fifty-excerpt pilot across A, B, and C.

### What it is

Step 6, the runner. dev-tools/experiments/runners/tow_runner.py reads the Tow dataset and creates, per excerpt, a find task, a verify task depending on it, a sampled approve task, and a close task, through the kanban interface, with no decomposer and no planner model.

Step 7, the workers.
Two pull-loop programs that use the HTTP MCP tools exactly as a coding agent would: a finder that wraps one engine (Perplexity's Agent API first; OpenAI with web search, Gemini with grounding, and Grok after) and reports the claim; a verifier that fetches the URL respecting robots.txt, matches the excerpt, checks publisher and date, and reports the verdict.

Step 8, the export. export_case(case_id) beside src/marcus_mcp/tools/audit_tools.py: tasks, assignments and lease history, evidence, approvals, and cost rows keyed by agent_id.

Step 9, the grader.
Applies the six Tow categories, label-blind, to Condition A responses, Condition B case answers, and Condition C (the same verifier as a filter with no board), and emits confident-wrong rate, fabricated-URL rate, decline rate, correct rate, and cost per case.

Then the pilot: fifty excerpts.
Condition A across the engines you have keys for, Condition B with two finder vendors, Condition C as the filter.

### Why it matters

This is the proof-shaped work.
Your confidence came from proving something the first time; this is the second something.
It also doubles as the first session-model acceptance run with no git in the loop.

### Done when

Fifty cases closed in Condition B with an audit bundle each, and A and C scored with the same grader.

### Learn more

- [The protocol, conditions A, B, and C](https://claude.ai/code/artifact/2eed6f40-f3e2-47b0-8e23-8627a80c332f#protocol-replicating-the-tow-center-study-with-a-record)
- [One case walked end to end, with the values the record holds](https://claude.ai/code/artifact/2eed6f40-f3e2-47b0-8e23-8627a80c332f#what-flows-in-what-comes-out)
- [The Tow Center study (methodology and public dataset)](https://www.cjr.org/tow_center/we-compared-eight-ai-search-engines-theyre-all-bad-at-citing-news.php)
- [Perplexity developer APIs](https://docs.perplexity.ai/)

## 5. Read the numbers (After the pilot)

Three thresholds decide whether to run the full two hundred or fix the verifier first.

### What it is

Go if the decline rate is under 50 percent, Condition B's confident-wrong rate is under 10 percent, and B's cost is under five times A's.
Then run all two hundred excerpts.

If B's confident-wrong rate is above 10 percent, the verifier is being fooled, most likely by syndicated copies on other domains or by pages that quote the excerpt.
Fix the verifier (stricter domain match, canonical URL check) before spending the rest.
This is a verifier problem, not a record problem.

If the decline rate is above 50 percent, too many publishers block a well-behaved fetcher; report it honestly rather than relaxing the robots rule, and note that fetch-based verification is impractical for this domain.

If cost is above five times A, the record is only defensible for high-stakes, low-volume work; that is where you would sell it anyway, but the number must be known.

Expected: A's confident-wrong rate 15 to 40 percent; C and B under 3 percent among kept answers, with declines of 20 to 45 percent; B and C scoring alike, which is the expected result, not a failure.

### Why it matters

A preregistered threshold is what separates an experiment from a demo.
Writing the go and no-go conditions before the data is what will make the result credible to Scott, to a program officer, and to you.

### Done when

A one-page go or no-go note, dated, with the pilot's five numbers next to the expected ranges.

### Learn more

- [Expected results and what falsifies what](https://claude.ai/code/artifact/2eed6f40-f3e2-47b0-8e23-8627a80c332f#protocol-replicating-the-tow-center-study-with-a-record)
- [Why Marcus is not the verifier, and where the record stops being overkill](https://claude.ai/code/artifact/2eed6f40-f3e2-47b0-8e23-8627a80c332f#what-this-does-not-do)

## 6. Write it up (After the full run)

The Tow tables with two rows added, the audit bundles published beside them, the expectations checked.

### What it is

Reproduce the study's tables and add two rows per engine: the engine alone, and the engine on the record.
Show Condition C next to B and say plainly that the accuracy is the verifier's; the record's contribution is enforcement, independence, and the trail, demonstrated by the bundles and the refusal log, not by the score.

Publish every case's audit bundle alongside the report, so anyone who doubts a number can open the case and check.
That is the product demonstrating itself.

Check each preregistered expectation against the data and report the misses as misses.

Keep it short: four to six pages, one figure per condition, one table, a link to the bundles and the code.

### Why it matters

The write-up is the object that turns four documents of argument into one piece of evidence.
It is also the first thing you will have shown anyone since the PyCon sprint that runs and measures rather than describes.

### Done when

A report with the tables and the bundle link, and a tagged commit on the repository that reproduces it.

### Learn more

- [The protocol's deliverable section](https://claude.ai/code/artifact/2eed6f40-f3e2-47b0-8e23-8627a80c332f#protocol-replicating-the-tow-center-study-with-a-record)
- [The Cobbled-Together Stack: the six things every team builds](https://claude.ai/code/artifact/731ac0bb-0c9c-4091-90a4-3b136a410dc2)

## 7. Use it three ways (Then)

Show Scott, put it in the STTR, demo the engineering version to your own team.

### What it is

Scott: send the report with one question, whether this is the wedge he meant or a feature inside it, and ask directly what he wants from you: advisor, co-founder, investor, or interested friend.
His answer to that is worth more than any further document.

STTR: the replication is a preliminary result for the provenance thread, and the audit bundle is the provenance model in the concrete.
It gives the PI outreach something to react to instead of a description.

Your team: the engineering-workflow version, one ticket through the three interjection points (assignment, done-with-evidence, human merge), using the same patched Marcus.
That is the demo for your own people, in the tools they already use, and the likeliest first paying use.

### Why it matters

One artifact, three audiences, three different questions answered: is the market real (Scott), is the research fundable (STTR), does it help engineers this week (your team).
You do not have to guess which of the five options is right; the three responses tell you.

### Done when

Three conversations held, each with a written note of what the other side said.

### Learn more

- [Where Marcus fits when it is not the whole process (the engineering and claims-desk examples)](https://claude.ai/code/artifact/2eed6f40-f3e2-47b0-8e23-8627a80c332f#where-marcus-fits-when-it-is-not-the-whole-process)
- [How teams engineer it into their workflows (three patterns)](https://claude.ai/code/artifact/2eed6f40-f3e2-47b0-8e23-8627a80c332f#how-teams-engineer-it-into-their-workflows)

## 8. Decide which game (December)

With the result, the application outcomes, Scott's answer, and the coach's process in hand.

### What it is

The five options, stated once: stay the course with Marcus as credential and authority; go inside a frontier lab; build a company on Marcus (venture-scale, small open-source business, or mom-and-pop); fund Marcus as a research instrument through the STTR while employed; treat Marcus as finished and redirect.
They combine and exclude in known ways: 1 pairs with 4 and 5; 2 excludes 3 and 4; 3 eventually replaces 1.

The inputs by December: whether Condition B held up and what it cost; whether Anthropic advanced you and for which role; what Scott said he wants; whether a PI engaged on the STTR; and what the coaching surfaced about the two things you are least willing to trade, which your own words already rank as family and intellectual challenge, with living up to your potential folded in.

Decide by elimination, the way you decide everything: remove the options the evidence closed, then pick among what is left with the two non-negotiables as the filter.

### Why it matters

This is the only item on the page that is a life decision, and it is deliberately last, after four months of evidence.
The fear you felt in September came from trying to make this decision without any of the inputs above.
In December you will have them.

### Done when

A decision written down with its reasons, and the first irreversible step, if there is one, scheduled for January rather than taken in a December mood.

### Learn more

- [What Marcus Becomes: the whole story on one page](https://claude.ai/code/artifact/04ad38fa-0a84-433d-b070-8a4fb8e694c0#the-whole-story-on-one-page)
- [The Life of Marcus](https://claude.ai/code/artifact/ceb5eb1f-a4ac-4159-86e2-d4a73aa01965)
- Your answers to Jeni's three questions (the file in this chat)
