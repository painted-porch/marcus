# Onboarding: start here

This page is for a new session, human or model, that knows nothing about Marcus and has limited context.
Read it top to bottom once.
It tells you what the project is, where the live state is, the rules you must not break, how to pick up work, and how to hand it off.
It deliberately holds no status, because status goes stale; the Program board holds status.

Last checked against the repositories on 2026-11-21.

## 1. What Marcus is

Marcus is an open-source coordinator where independent AI agents pull tasks from a shared task board instead of being told what to do by a central scheduler.
The board is the only channel: agents choose their own work, decide for themselves how to do it, and communicate only by what they write to the board.
Marcus is being split into a small **kernel** (the board and its rules) and **extensions** (separate programs that use the kernel for one purpose).

The current program of work is that split.
Its plan is [`MODULARIZATION-PLAN.md`](MODULARIZATION-PLAN.md); read it second.

## 2. Where the live state is

**The Program board is the record of truth:** <https://github.com/orgs/painted-porch/projects/1>.
If anything you remember, or anything in a document, disagrees with the board, the board is right.

```bash
# Every open item, with workstream and state. The default limit is 30 and the board is bigger.
gh project item-list 1 --owner painted-porch --limit 400 --format json \
  --jq '.items[] | select(.state != "Done") | "\(.workstream) | \(.state) | \(.content.number // "draft") | \(.title)"'

# The modularization program in its order, one step per line.
gh project item-list 1 --owner painted-porch --limit 400 --format json \
  --jq '[.items[] | select(.order != null)] | sort_by(.order)[] | "\(.order) | \(.state) | #\(.content.number) | \(.title)"'
```

The program runs one step at a time.
Each step's place is the board's `Order` field, and only the lowest-numbered step that is not Done is at Resume now.

What each state means:

| State | Meaning |
| --- | --- |
| Resume now | Ready. No decision needed. Pick it up. |
| In progress | Someone is on it, or it is an epic still open. |
| Blocked | Sequenced behind something or waiting on something external. The issue body says what. |
| Needs Larry | Waiting on a decision by Lawrence Gray. Do not build on it. |
| Done | Finished. |

Every issue in this program is written to stand alone: what the system is, the problem, what to change, files to read first, an estimate, how to verify, and what it relates to.
Read the issue, not the conversation that produced it.

## 3. Rules you must not break

1. **Never branch, commit, or leave uncommitted work in `~/dev/marcus`.** Other sessions use it as their working directory. Work in a worktree: `git -C ~/dev/marcus worktree add ~/dev/marcus-worktrees/<topic> -b <branch> origin/develop`.
2. **Pull requests target `develop`, never `main`.** `main` is for releases.
3. **No paid model run without an `APPROVED by Larry <date>` line** on the run's board item. Name the cost and wait.
4. **Three gates before any merge, on the same commit:** your own review, a Codex pass (comment `@codex review`; a 👍 or "Didn't find any major issues" is clean, silence is not), and a live check against a real Marcus server. CI alone is not a gate.
5. **Merge pinned to the full commit:** `gh pr merge <n> --squash --match-head-commit <full 40-character SHA>`. A short SHA fails. Stop on any failure.
6. **No Tow Center dataset text, URLs or excerpts in any repository, ever.** Run `python dev-tools/experiments/runners/dataset_leak_scan.py` before pushing anything near the citation verifier.
7. **Hospital case texts, attack instructions and generated advice are never published.**
8. **The numerical verifier's data files are never committed to a repository.**
9. **Decisions belong to Lawrence Gray.** To ask for one, create a draft item on the board with four headings, Problem, Context, Recommendation, Why, set it to Needs Larry, and wait for a line that starts "DECIDED by Larry". A statement of fact by Larry is not a go-ahead to build, merge or spend.
10. **Write code test-first**, with numpy-style docstrings, mypy clean on what you change, and the unit baseline `pytest -m unit` passing. Gate a push on pytest's exit status (`pytest -m unit || exit 1`), never on a line of output read through a pipe.

The full project rules are in [`CLAUDE.md`](../../CLAUDE.md) at the repository root.

## 4. Where everything lives

| What | Repository | Local copy | Notes |
| --- | --- | --- | --- |
| Kernel | `painted-porch/marcus` (public), branch `develop` | `~/dev/marcus` (shared, read-only for you) | work in `~/dev/marcus-worktrees/<topic>` |
| Toolkit | `painted-porch/marcus-toolkit` (public, PyPI) | `~/dev/marcus-toolkit` | what extensions use instead of kernel code; imports no kernel code |
| AI-citation verifier | `extensions/citation_verifier/` today; moving to `painted-porch/citation-verifier` (#943) | | method in `REPRODUCING.md`; records in `~/marcus-experiments/on-the-record/`, `~/marcus-experiments/tow/` |
| Clinical review | `extensions/clinical_review/` today; moving to `painted-porch/hospital` (#944) | worktrees `~/dev/marcus-worktrees/hospital`, `hospital-site` | records in `~/marcus-experiments/hospital/` (git, no remote yet) |
| Numerical verifier | `dev-tools/experiments/runners/numeric_*.py` and three unmerged branches today; moving to `painted-porch/numeric-verifier`, private (#945) | worktree `~/dev/marcus-worktrees/numeric-verifier` | records in `~/marcus-experiments/numeric-verifier/`, not in git |
| War game | `painted-porch/no-hand-on-the-table` (private) | `~/dev/no-hand-on-the-table` | Marcus-facing code is `plumbing/`; the copy in `~/marcus-experiments/` is a second clone, not the source |
| Minecraft | `painted-porch/marcus-minecraft` (private), a fork of YC's harness | `~/marcus-experiments/minecraft-harness` | push to the `marcus-minecraft` remote, never `origin` (YC's); run `npx prettier --write` before every push |
| Website, marcus-ai.dev | `gh-pages` branch of `painted-porch/marcus` today; moving to its own repository (#947) | | `painted-porch/on-the-record` is a stale copy, not the source |
| Software building | tangled through the kernel's `src/` today; moving to `painted-porch/marcus-software` (#924) | | |
| Experiment records | none, by design | `~/marcus-experiments/<run>/` | durable; each run's `RESULTS.md` is its record |

## 5. Read these next, in this order

| Document | Why |
| --- | --- |
| [`MODULARIZATION-PLAN.md`](MODULARIZATION-PLAN.md) | the goal, the decisions, the three tracks and their order |
| `docs/architecture/adr/0013-kernel-and-extensions.md` | the thirteen decisions that define kernel and extension |
| `docs/architecture/kernel-contract.md` | the door, versioned: every tool an extension may call, its arguments and reply, and the rules behind them |
| `docs/architecture/extension-contract.md` | why the door has that shape, and what the first extension cut taught |
| `docs/architecture/building-an-extension.md` | how an extension is built, with a worked example |
| `docs/architecture/software-extension-extraction.md` | the inventory behind Track C, with every `task.py` function classified |
| `extensions/citation_verifier/` | the first finished extension; the shape the others copy |

Read the one your issue points at first if you are short of context.

## 6. How to pick up work

1. List the program in its order (section 2) and take the one step at **Resume now**. Do not start a later step; they run one at a time.
2. Read its issue in full, then the files its "Where to look first" table names. If the step is a parent with sub-issues, work the sub-issues, not the parent.
3. Set the item to **In progress** on the board so nobody else starts it:

   ```bash
   gh project item-edit --project-id PVT_kwDODj3MGM4BqP6D --id <item id> \
     --field-id PVTSSF_lADODj3MGM4BqP6Dzhon3hg --single-select-option-id 3e9997c3
   ```

4. Make a worktree, write the failing test first, then the change.
5. Run the gates in rule 4, then open a pull request to `develop` written so a first-day reader can follow it.

Board field ids, which do not change:

| Field | Id | Options |
| --- | --- | --- |
| State | `PVTSSF_lADODj3MGM4BqP6Dzhon3hg` | Resume now `6fcc9988`, In progress `3e9997c3`, Blocked `5f98ed78`, Needs Larry `97f2aa51`, Done `a3c843c2` |
| Workstream | `PVTSSF_lADODj3MGM4BqP6Dzhon3gY` | Modularization `e41965e4`, Minecraft deliberation `cf921be3`, Minecraft plumbing `6eb50fba`, Numerical-report verifier `b2668a00`, AI-citation verifier `ced7efd1`, Other `0fabb584` |

## 7. How to hand off

Assume you will be stopped without warning and someone with no memory of you picks up.

- **Commit and push before any long wait**, even unfinished work, on your branch.
- **Comment on the issue** with what is done, what is not, and the exact next step. That comment is the handoff; messages between sessions are often never delivered.
- **Set the board state** to match: Done, Blocked with the reason in the comment, or back to Resume now.
- **When your step is Done, set the next step by `Order` to Resume now.** That is how the next session finds it.
- **If your step turns out bigger than three days,** split it into sub-issues before going on, give them `Order` numbers, and move the numbers after them down.
- **Log decisions and blockers** with the `simon` command (see `CLAUDE.md`, section SIMON_DECISION_LOGGING).
- **After a merge, delete the branch by hand.** `gh pr merge --delete-branch` silently skips the deletion when run from a worktree. Check with `git ls-remote --heads origin <branch>`.

## 8. Live checks

A live check runs real code against a real Marcus server with the model switched off, so it costs nothing.

- Start the server from your worktree with every model key stripped from the environment, and a config whose AI section is `"enabled": false` **and** `"anthropic_api_key": ""`. A placeholder key still fires one real call on startup.
- Give each run its own port and its own `MARCUS_DATA_DIR`.
- An agent must call `register_agent` with a `project_id`, or `request_next_task` returns nothing.
- Every created project gets one extra finished card, `About: <name>`, so expected counts are your cards plus one.
- Never put the word "test" in a project name; it changes Marcus's behavior.
- Read the reply body of every call; do not infer success from a string match.

Existing live checks to copy: `~/marcus-experiments/citation-extension-live-check/run_rehearsal.sh` and `python -m marcus_toolkit.livecheck --checkout <your worktree>`.

## 9. Traps that have cost real time

| Trap | What to do |
| --- | --- |
| `git merge-base --is-ancestor` says a squash-merged branch is not merged | check that the pull request's merge commit is on the base instead |
| `rm` on this machine moves files to the trash | expect it; it is not a permanent delete |
| zsh does not split `$VAR` into words | use arrays, or quote and split explicitly |
| `pgrep -f` matches its own shell | kill the Python process, not its wrapper |
| A pull request in a merge conflict gets no CI runs | "no checks reported" is not green |
| The shared root `~/dev/marcus` may be behind `develop` | read code from your worktree at `origin/develop` |

## 10. Glossary

| Term | Meaning |
| --- | --- |
| Kernel | the part of Marcus every project needs: board, task lifecycle, leases (time-limited claims), eligibility, registration, the MCP server |
| Extension | a program that declares its task types to the kernel and runs its own agents against the board; the kernel never calls into it |
| MCP | Model Context Protocol, the wire protocol agents and extensions use to call the kernel's tools |
| The door | everything an extension may rely on: MCP tools and reply shapes, the seed spec, the task-type declaration, the evidence, handoff and eligibility rules |
| Seed spec | one entry of `create_project(options.tasks)`: a card put on the board exactly as written |
| Task-type declaration | a project's list of the card types it uses and the evidence each must carry, `options.task_type_registrations` |
| Verify card | a card whose close must carry a verdict, claimed by a different agent than the one whose work it checks |
| ADR-0013 | the architecture decision record for the kernel-and-extensions split |
| Cato | the dashboard that reads a kernel's data |
| Simon | Lawrence Gray's decision journal, the `simon` command |
| Codex | the automated reviewer, asked with `@codex review` on a pull request |
