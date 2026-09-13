<!-- Index of the September 2026 design notes. -->

# Design notes, September 2026

These documents were written between 5 and 13 September 2026 as a full review of Marcus and a proposal for what it becomes next.
They are the context for the `feat/system-of-record` branch and the issue that tracks it.
Read them in the order below; each one builds on the one before it.

| Order | Document | What it is | Published page |
|---|---|---|---|
| 1 | [the-life-of-marcus.md](the-life-of-marcus.md) | The project's history from the source, the issues, and the pull requests, in twelve chapters, with the docs-versus-code reconciliation and the session-model transition | https://claude.ai/code/artifact/ceb5eb1f-a4ac-4159-86e2-d4a73aa01965 |
| 2 | [the-cobbled-together-stack.md](the-cobbled-together-stack.md) | The 2026 production agent stack, the six things every team hand-builds between the layers they buy, and what that means for Marcus | https://claude.ai/code/artifact/731ac0bb-0c9c-4091-90a4-3b136a410dc2 |
| 3 | [what-marcus-becomes.md](what-marcus-becomes.md) | The vision: the ledger of agent work, how the world differs after, the storyboard, the MVP, and how far away each piece is | https://claude.ai/code/artifact/04ad38fa-0a84-433d-b070-8a4fb8e694c0 |
| 4 | [marcus-under-the-hood.md](marcus-under-the-hood.md) | How agents find Marcus, who writes the decomposition and the gate, and the technical checklist from the session model forward | https://claude.ai/code/artifact/87b68846-a29f-422d-a616-5773036371cd |
| 5 | [system-of-record.md](system-of-record.md) | The design this branch implements: where the record sits, what flows through it, a worked case, where it fits in existing workflows, what changes in Marcus, the Tow Center replication protocol, and the patch outline | https://claude.ai/code/artifact/2eed6f40-f3e2-47b0-8e23-8627a80c332f |
| 6 | [the-ninety-days.md](the-ninety-days.md) | The working plan through December 2026; this branch is items 3 to 7 | https://claude.ai/code/artifact/aef1b1b3-356d-41bf-a5a1-d3eaab429106 |

Research notes behind documents 2 and 3 are in [research/](research/): the landscape of vendors, funding, and pricing; production agent-ops findings; and the first research pass on multi-agent coordination.

Figures for document 5 are in [figures/](figures/) as standalone SVG files.

Two conventions apply to every file here, per the repository's `CLAUDE.md` and the author's own rules: one sentence per line, and no em-dashes.
The older documents (1 to 4) were converted mechanically from prose that used em-dashes, so an occasional comma or parenthesis may read slightly rougher than the original; the published pages are the reference text.
