# The modularization plan

This document describes the plan: its goal, its decisions, its order and its rules.
It does not hold the current state of the work.
The current state is the Program board, <https://github.com/orgs/painted-porch/projects/1>, filtered to Workstream = Modularization.
If this document and the board disagree about what is done, the board is right.

Plan written 2026-11-21 from Lawrence Gray's decisions of the same day.
Umbrella epic: [#938](https://github.com/painted-porch/marcus/issues/938).

## What Marcus is, in three sentences

Marcus is an open-source coordinator where independent AI agents pull tasks from a shared task board instead of being told what to do by a central scheduler.
It is being split into a small **kernel**, meaning the board, task claiming, time limits on claims and the rules for what counts as done, and **extensions**, meaning separate programs that use the kernel for one purpose.
The extensions today verify AI citations, review clinical AI advice, verify numerical reports, referee a war game and run agents in Minecraft, and the original Marcus use, building software, is the fifth.

## The goal

The `painted-porch/marcus` repository holds nothing but the kernel.
Every extension lives in its own repository.
Every extension reaches the kernel the same way: through `marcus-toolkit`, over MCP (Model Context Protocol, the wire protocol for the kernel's tools), against a tagged kernel release.
A kernel change that breaks an extension fails the kernel's own contract tests before it merges.

## Decisions already made

These were decided by Lawrence Gray and are not open for re-decision by a session.

| Date | Decision |
| --- | --- |
| 2026-11-13 | Extensions write their cards to the board over MCP only, never by importing kernel code. |
| 2026-11-13 | Verification is a `verify` card owned by a verifier agent, not an in-process gate. |
| 2026-11-21 | The `marcus` repository holds only core kernel code; every extension moves to its own repository. |
| 2026-11-21 | Repository names: `citation-verifier`, `hospital`, `numeric-verifier` (private), `marcus-software`. |
| 2026-11-21 | The numerical verifier's data files are never committed to a repository. |
| 2026-11-21 | The website moves off the kernel repository into its own, with redirect instructions. |
| 2026-11-21 | The Minecraft deliberation layer resumes after Track B is done. |
| 2026-11-21 | The work runs one step at a time, not in parallel, in the order recorded on the board. Any issue estimated at more than three engineer-days is split into sub-issues of one to three days. |
| 2026-11-21 | The website's repository is `marcus-site`. |
| 2026-11-21 | `citation-verifier` is public. |
| 2026-11-21 | The clinical review's records go to a separate private repository, `hospital-records`, not into `hospital` with the code. |
| 2026-11-22 | `marcus-software` is public. |
| 2026-11-21 | Nothing is done on the numerical verifier's framing or its Condition D run; that decision item is parked. The B3 move stays in the order. |
| 2026-11-21 | The Minecraft item on citing a decision as grounds (Agent Invariant 2) is Blocked, waiting with the deliberation layer. |

## The order

The program is one sequence of 71 steps, done one at a time.
Each step's position is the Program board's `Order` field, a number from 1 to 71.
Only the lowest-numbered step that is not Done is at Resume now; every later step is Blocked until the one before it is Done.
Each step's issue says which step comes before it.

The twelve issues that were too big for one sitting are now parents.
Their work is in their sub-issues, which GitHub shows on the parent and the board shows in the `Parent issue` field.
A parent stays In progress until all its sub-issues close.

A step that turns out bigger than three days while it is being done is split the same way before work continues, and the `Order` numbers after it move down to fit.

## Where everything ends up

| Project | Today | After |
| --- | --- | --- |
| Kernel | `painted-porch/marcus`, mixed with everything below | `painted-porch/marcus`, kernel only, with a contract spec and contract tests |
| AI-citation verifier | `extensions/citation_verifier/` in the kernel repository | `painted-porch/citation-verifier` (public) |
| Clinical review ("The Second Opinion") | `extensions/clinical_review/` in the kernel repository | `painted-porch/hospital` (private), and its records in `painted-porch/hospital-records` (private) |
| Numerical verifier | `dev-tools/experiments/runners/numeric_*.py` plus three unmerged branches | `painted-porch/numeric-verifier` (private), data files never committed |
| War game ("No Hand on the Table") | `painted-porch/no-hand-on-the-table` | unchanged, pins a kernel release |
| Minecraft | `painted-porch/marcus-minecraft` | unchanged, last kernel import removed, pins a kernel release |
| Software building | tangled through the kernel's `src/` | `painted-porch/marcus-software` (public) |
| Website, marcus-ai.dev | `gh-pages` branch of the kernel repository | `painted-porch/marcus-site` |
| Toolkit | `painted-porch/marcus-toolkit`, on PyPI | unchanged, pins a kernel release instead of a commit |

Experiment records stay outside every repository, in `~/marcus-experiments/`.

## The three tracks, in order

### Track A: define and freeze the door

The "door" is everything an extension may rely on: the kernel's MCP tools and their reply shapes, the seed spec, the task-type declaration, and the rules for evidence, handoff and eligibility.
Today none of it is versioned, and two tool registries disagree about what the tools are.
Track A comes first because every project moved in Track B has to pin a kernel release, and there is no meaningful release until the door is written down.

| Issue | Step | Days |
| --- | --- | --- |
| [#939](https://github.com/painted-porch/marcus/issues/939) | A1: write the contract as a versioned spec | 2 to 3 |
| [#940](https://github.com/painted-porch/marcus/issues/940) | A2: a contract test suite, and CI that notices drift | 2 to 3 |
| [#941](https://github.com/painted-porch/marcus/issues/941) | A3: remove the kernel's built-in task-type names | 1 |
| [#942](https://github.com/painted-porch/marcus/issues/942) | A4: cut a release; every project pins releases, not commits | 1 to 2 |

### Track B: move every project out of the kernel repository

Each move follows the five-step procedure below, so nothing that runs today breaks while it moves.

| Issue | Step | Days |
| --- | --- | --- |
| [#943](https://github.com/painted-porch/marcus/issues/943) | B1: citation verifier to `citation-verifier` | 2 to 3 |
| [#944](https://github.com/painted-porch/marcus/issues/944) | B2: clinical review to `hospital`, and give its records a remote | 2 to 3 |
| [#945](https://github.com/painted-porch/marcus/issues/945) | B3: numerical verifier to its own private repository, in seven sub-issues (B3.1 to B3.7) | 8 to 14 |
| [#946](https://github.com/painted-porch/marcus/issues/946) | B4: Minecraft and the war game pin a release; Minecraft's last kernel import goes | 1 to 2 |
| [#947](https://github.com/painted-porch/marcus/issues/947) | B5: the website to its own repository | 1 to 2 |

B1 to B4 wait for Track A's release.
B5 does not depend on Track A, but it takes its place in the single order after B4.

### Track C: extract software building

The software-building code is the original Marcus and is tangled through the kernel.
Its extraction is epic [#924](https://github.com/painted-porch/marcus/issues/924), scoped in `docs/architecture/software-extension-extraction.md`.

| Issue | Step | Days |
| --- | --- | --- |
| [#948](https://github.com/painted-porch/marcus/issues/948) | classify the 18 `src/` packages the scoping never covered | 2 to 3 |
| [#925](https://github.com/painted-porch/marcus/issues/925) | 1: move the vendor-coupled cost modules | 3 to 5 |
| [#926](https://github.com/painted-porch/marcus/issues/926) | 2: move `nlp_task_utils.py` and break the kernel's import of it | 1 plus |
| [#927](https://github.com/painted-porch/marcus/issues/927) | 3: verification as a card | 9 to 14 |
| [#928](https://github.com/painted-porch/marcus/issues/928) | 4: the verifier lands the work; the git plumbing leaves | 6 to 10 |
| [#929](https://github.com/painted-porch/marcus/issues/929) | 5: move the validation gate body | 10 to 15 |
| [#930](https://github.com/painted-porch/marcus/issues/930) | 6: move the smoke gate body | 6 to 9 |
| [#931](https://github.com/painted-porch/marcus/issues/931) | 8a: split `_create_project_inner` | not priced |
| [#932](https://github.com/painted-porch/marcus/issues/932) | 7: move the decomposer | 7 to 11 |
| [#933](https://github.com/painted-porch/marcus/issues/933) | 8b: split `request_next_task` | not priced |
| [#934](https://github.com/painted-porch/marcus/issues/934) | 8c: split `_record_progress` | not priced |
| [#935](https://github.com/painted-porch/marcus/issues/935) | 9: assemble `painted-porch/marcus-software` | not priced |
| [#424](https://github.com/painted-porch/marcus/issues/424) | 10: split `task.py` into modules | not priced |

Every step above except #948 is split into sub-issues numbered after it, so step 3 becomes 3.1 to 3.6, and so on.
Two of them run out of their parent's position on purpose.
Step 9.1, creating the `painted-porch/marcus-software` repository, runs first in Track C, because steps 1, 3, 4, 5, 6 and 7 move code into it.
The move of `nlp_task_utils.py` belongs to step 7.5, not step 2, because the decomposer and four other kernel modules still import it until the decomposer leaves.

Step 3 builds on Track A's contract, because it declares task types over MCP.
Steps that change the door (3.1, 3.2, 4.4, 6.2, 7.1, 7.2, 8b.3) update the contract spec, its version and the contract tests in the same pull request.

### The orderings that are hazards, not preferences

- **Track A before any Track B move.** A moved project has to pin something.
- **Inside Track C, step 4 before steps 5 and 6.** If either gate body leaves the kernel before the landing does, the kernel keeps merging work that nothing has checked.
- **Steps 5 and 6 never run concurrently.** Both rewrite the same region of `report_task_progress` in `src/marcus_mcp/tools/task.py`.
- **Verify cards stay off until a verifier runs.** Step 3.5 adds them behind an option that is off by default, and step 3.6 turns it on only when software runs launch a verifier; otherwise every card downstream of an unclaimed verify card waits forever.

## The five-step move, for every project in Track B

1. **Copy with history.** From a fresh clone, filter the project's paths into its new repository, so its commit history comes with it. Transfer its issues with `gh issue transfer`.
2. **Run from the new repository.** It imports no kernel code, only `marcus-toolkit`. It pins the Track A release. It has its own CI.
3. **Prove it matches.** Its zero-spend rehearsal, run from the new repository, gives the same result it gave inside the kernel repository. The expected result is written in its issue.
4. **Repoint every consumer.** Run scripts in `~/marcus-experiments/`, worktrees, documentation links, and the kernel's own `pytest.ini` and `pyproject.toml` entries that point at the old path.
5. **Delete from the kernel repository** in one pull request, with the three gates. Reverting that pull request restores the old location.

Until step 5 merges, the old location keeps working, so nothing anyone runs today breaks.
Two rules sit on top of the five steps.
No project moves while one of its runs is in flight.
No project moves before Track A's release exists.

## After the program

The Minecraft deliberation layer resumes once Track B is done.
It needs only the door, which Track A defines, so it does not wait for Track C.
Its decision items on the Program board are marked Blocked with that reason.

## How to change this plan

Decisions belong to Lawrence Gray.
A session that thinks the plan is wrong writes a draft item on the Program board in the shape Problem, Context, Recommendation, Why, and waits for a line that starts "DECIDED by Larry".
A decided change is then written into this document in the same pull request that acts on it, so the plan and the work never drift apart.

## Related

- [`ONBOARDING.md`](ONBOARDING.md), the starting point for a new session.
- `docs/architecture/adr/0013-kernel-and-extensions.md`, the kernel-and-extensions decision record.
- `docs/architecture/extension-contract.md` and `docs/architecture/building-an-extension.md`.
- `docs/architecture/software-extension-extraction.md`, the inventory behind Track C.
- Epics [#938](https://github.com/painted-porch/marcus/issues/938) and [#924](https://github.com/painted-porch/marcus/issues/924).
