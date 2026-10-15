<!-- Reproduction guide for the On the Record experiment (#737). One sentence per line, written for a reader on their first day. -->

# Reproducing On the Record

## What is this, briefly

Marcus is an open-source coordinator where AI agents pull tasks from a shared kanban board, and this experiment measured what happens when AI search engines must pass their citations through Marcus's enforcement: evidence required, independent verification, human sign-off.
This guide reproduces the whole experiment, or any slice of it, on your own machine.

## What you need

1. Python 3.11 with this repository's dependencies installed (`pip install -e .` from the repo root).
2. The dataset: the Tow Center distributes `GenAISearch_Data.csv` encrypted in their own repository to keep the excerpts away from AI crawlers; follow their README to decrypt it, keep the CSV out of any repository, and point `--dataset` at your local copy.
3. API keys, as environment variable names only: `OPENAI_API_KEY`, `GEMINI_API_KEY`, `PERPLEXITY_API_KEY`, and `CLAUDE_API_KEY` (or `ANTHROPIC_API_KEY`) for the engines you run.
4. Money awareness: at October 2026 list prices the full 200-question run cost about $19, dominated by the Claude Haiku arm at about $8; the three budget engines alone cost about $2.30, and the free rehearsal below costs nothing.

## The free rehearsal (start here)

Every moving part runs end to end with a fake engine and zero API calls.

```bash
EXP=~/marcus-experiments/tow/rehearsal && mkdir -p $EXP/run

# 1. Seed a 4-case board; this also registers it in Marcus's project registry.
python dev-tools/experiments/runners/tow_runner.py \
  --dataset /path/to/GenAISearch_Data.csv \
  --db $EXP/kanban.db --project tow-rehearsal \
  --limit 4 --approval-rate 0.25 --seed 737 --manifest $EXP/manifest.json

# 2. Write a config: copy config_marcus.example.json, set kanban.provider=sqlite,
#    kanban.sqlite_db_path to the db above, default_project_name to
#    "tow-rehearsal - Main Board", ai.enabled=false, features.context=false.

# 3. Start the server FROM THE REPO ROOT (persistence paths are cwd-relative)
#    with every engine key stripped from its environment.
env -u OPENAI_API_KEY -u GEMINI_API_KEY -u PERPLEXITY_API_KEY -u CLAUDE_API_KEY -u ANTHROPIC_API_KEY \
  MARCUS_CONFIG=$EXP/config.json python -m src.marcus_mcp.server --http --all-tools

# 4. In another shell: the fake finder, the verifier, the human approvals.
python dev-tools/experiments/runners/tow_finder.py --url http://localhost:4777/mcp \
  --project tow-rehearsal --agent-id finder-fake-1 --vendor fakevendor --engine fake --idle-exits 3
python dev-tools/experiments/runners/tow_verifier.py --url http://localhost:4777/mcp \
  --project tow-rehearsal --agent-id verifier-fetch-1 --vendor fetchcheck \
  --cache-dir $EXP/fetch-cache --idle-exits 3
python -m src.cli.approvals --url http://localhost:4777/mcp --project tow-rehearsal \
  --reviewer you --decide approved
python dev-tools/experiments/runners/tow_verifier.py --url http://localhost:4777/mcp \
  --project tow-rehearsal --agent-id verifier-fetch-1 --vendor fetchcheck \
  --cache-dir $EXP/fetch-cache --idle-exits 2

# 5. Export the audit bundles while the server is still up.
python dev-tools/experiments/runners/export_bundles.py --url http://localhost:4777/mcp \
  --manifest $EXP/manifest.json --out-dir $EXP/bundles
```

Expected: every task reaches done, and each bundle names the finder, the verifier's checks, and your approval.
Then try to cheat: report a find task completed without evidence and watch the board reject the close, or request work as the finder after its finds are done and watch it be offered nothing.

## The measured conditions

Condition A (engines alone) needs no server.

```bash
python dev-tools/experiments/runners/tow_rot_sweep.py --dataset ... --limit 0 \
  --cache-dir $EXP/fetch-cache --out $EXP/rot.json      # measure link rot FIRST
python dev-tools/experiments/runners/tow_condition_a.py --dataset ... \
  --engines openai gemini perplexity claude-haiku --limit 0 \
  --out $EXP/condition-a.jsonl --confirm-paid
```

Condition B is the rehearsal flow with a real engine (`--engine perplexity --vendor perplexity`, one board per engine vendor) and 200 cases.
Condition C re-filters A's answers through the same verifier, free: `tow_condition_c.py --a-responses ... --cache-dir ...`.
Grade everything blind with `tow_grader.py --manifest ... --a-responses ... --bundles <dir> --b-engine <name> --usage-log <file> ... --c-responses ... --seed 737 --out rates.json`.

## Rules the numbers depend on

1. Run the rot sweep before spending, and read decline rates net of its baseline; against this 2024-25 dataset, 47 percent of ground-truth pages are already dead or closed to robots.
2. Stratify or run all 200: the dataset is publisher-ordered, so a plain first-N slice measures a few publishers' crawler posture, not the study.
3. Keep ground truth out of the board: it lives only in the manifest the grader reads.
4. Keep engine keys out of the server's environment, and the coordinator's own AI disabled (`ai.enabled=false`), so Marcus makes zero model calls.
5. "Wrong among delivered answers" counts answers that reached you; declines are not errors, and every decline carries its fetch receipts.

## What you should see

Alone, engines deliver wrong answers most of the time (69.5 percent pooled in our full run) with roughly half their links fabricated or dead; through Marcus the same engines fall to single digits with zero fabricated links delivered, because the checkpoint converts caught claims into labeled declines.
Your absolute numbers will drift with engine versions and link rot; the gap between the two conditions is the finding.

## Related

The dated readouts beside this file; issue #737 (the preregistration); the `on-the-record-v1` release carrying redacted audit bundles; Jaźwińska & Chandrasekar, "AI Search Has a Citation Problem," Tow Center for Digital Journalism, CJR, March 2025.
