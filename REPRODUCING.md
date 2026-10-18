# Reproducing "On the Record"

Marcus is open-source software for getting real work out of AI agents and being able to trust what comes back.
"On the Record" is a preregistered replication of Columbia's Tow Center 2025 study of AI search, "AI Search Has a Citation Problem."
We asked four AI search engines the same 200 news questions, where we already knew the correct source, and then ran the same answers a second time through Marcus's verification checkpoint.
This guide reproduces that study end to end.

The live study, case by case, is at https://marcus-ai.dev/on-the-record/.

## The result you should get

| Condition | What it is | Answers wrong | Links fabricated, dead, or wrong source |
|---|---|---|---|
| A | The four engines alone, no board | about 69.5% | about 51.2% |
| B | The same engines run through Marcus | about 12.1% | 0% reach the reader |
| C | The verifier bolted onto A as a post-hoc filter, no board | about 9.5% | 0% |

The honest reading: this is sorting, not fixing.
The engines invent just as many bad links in condition B as in condition A.
Marcus catches them instead of delivering them, so no fabricated link reaches the reader, and every decision is on the record.
A hypothesis set in advance, that fewer than 10% of delivered answers would still be wrong, did not hold: about 1 in 8 delivered answers still had a flaw in the citation, and about 1 in 29 named the wrong article outright.
Both numbers are reported rather than the flattering one alone.

## Before you start

1. **The dataset.** The study uses `GenAISearch_Data.csv`, 200 excerpts across 20 publishers. We do not redistribute it. Get it from the Tow Center and decrypt it locally; the passphrase is in their README. Do not commit the CSV, and do not commit the manifests the runner writes, because both contain the excerpts and answers.
2. **Marcus installed.** Python 3.11 or newer, and a working Marcus checkout. See the project README for setup.
3. **Engine API keys.** The run uses four engines: an OpenAI model, Google Gemini, Perplexity, and Anthropic Claude Haiku. The exact `--engines` keys are defined in `dev-tools/experiments/runners/tow_engines.py`. Grounded engines such as Gemini bill for web search separately from tokens, which standard token logs undercount, so the real cost is higher than a token count suggests.
4. **Cost.** Conditions A and B each make roughly 800 paid engine calls (four engines times 200 questions). Condition C is free, because it only fetches pages. Get a cost estimate approved before running; the Condition A runner refuses to start without `--confirm-paid` for exactly this reason.

## The three conditions, in plain terms

- **Condition A** is each engine answering alone, with no board and no verification. This is the baseline the original study measured.
- **Condition B** is the same engines working inside Marcus, where the board enforces three rules: nothing is accepted without its evidence, every claim is checked by a deterministic script the claim's author never sees, and a human signs off on a sample.
- **Condition C** is the verifier from B applied to Condition A's answers as a plain filter, with no board and no record. It is the control. If C scores like B, the accuracy gain came from the verifier. What B adds on top is that the check cannot be skipped, cannot be done by the author or the author's vendor, cannot be lost, and can be shown to a third party.

## Step by step

Pick an output directory outside the repository, for example `~/marcus-experiments/tow/repro`. Nothing below should be committed.

**1. Condition A (paid).** One request per engine per excerpt; the raw response, the parsed claim, the model, token counts, and cost land in one JSONL row per answer.

```
python dev-tools/experiments/runners/tow_condition_a.py \
    --dataset /path/to/GenAISearch_Data.csv \
    --engines openai gemini perplexity claude-haiku \
    --limit 200 \
    --out ~/marcus-experiments/tow/repro/condition-a.jsonl \
    --confirm-paid
```

**2. Condition B (paid), one board per engine.** The board never assigns work, so attribution comes from environment design: seed one project per finder vendor, and register only that vendor's finder plus a different-vendor verifier. Repeat this for each of the four engines, changing `--project` and `--manifest`.

```
python dev-tools/experiments/runners/tow_runner.py \
    --dataset /path/to/GenAISearch_Data.csv \
    --db ~/marcus-experiments/tow/repro/kanban.db \
    --project tow-b-openai \
    --limit 200 --approval-rate 0.1 --seed 737 \
    --manifest ~/marcus-experiments/tow/repro/manifest-openai.json
```

Then start the Marcus server for that board on port 4777, which is the port the workers and the export command below use. The server defaults to 4298, so set it explicitly:

```
./marcus start --http --port 4777
```

Run the finder and the verifier pull-loop workers against it (`tow_finder.py` and `tow_verifier.py`, both default to `--url http://localhost:4777/mcp`). The board drains in a fixed order, because the `synthesize` task that renders a case's final answer depends on the human `approve` task. So run the finder and verifier until they idle, sign the sampled approvals with the approvals CLI (`python -m src.cli.approvals`), then run the verifier once more so the now-eligible `synthesize` tasks complete. Skipping that second verifier pass leaves the approved sample without final answers, and the grader scores them as declines.

While the server is still up, export the audit bundles (the refusal log lives in server memory and would be lost after a restart):

```
python dev-tools/experiments/runners/export_bundles.py \
    --url http://localhost:4777/mcp \
    --manifest ~/marcus-experiments/tow/repro/manifest-openai.json \
    --out-dir ~/marcus-experiments/tow/repro/bundles-openai
```

**3. Condition C (free).** The same checks as B, applied to Condition A's answers as a filter. C and B share one fetch cache, which is a preregistered requirement so a page that moves between runs is scored identically in both.

```
python dev-tools/experiments/runners/tow_condition_c.py \
    --a-responses ~/marcus-experiments/tow/repro/condition-a.jsonl \
    --dataset /path/to/GenAISearch_Data.csv \
    --cache-dir ~/marcus-experiments/tow/repro/fetch-cache \
    --out ~/marcus-experiments/tow/repro/condition-c.jsonl
```

**4. Grade, label-blind.** The grader normalizes every answer from every condition into one record shape, assigns a blind id, shuffles with a fixed seed, and scores with one function that never sees the condition or the engine. It reads ground truth only from the manifests, which never touched the board. It emits the preregistered rates: confident-wrong, fabricated-URL, decline, correct, and cost per case. See `dev-tools/experiments/runners/tow_grader.py` for its arguments.

## Where to look in the code

| File | Purpose |
|---|---|
| `dev-tools/experiments/runners/tow_runner.py` | Seeds Condition B cases onto a Marcus board, writes the ground-truth manifest |
| `dev-tools/experiments/runners/tow_condition_a.py` | Condition A: each engine alone |
| `dev-tools/experiments/runners/tow_condition_c.py` | Condition C: verifier-only control |
| `dev-tools/experiments/runners/tow_checks.py` | The five deterministic checks (`decide_verdict`) |
| `dev-tools/experiments/runners/tow_grader.py` | Label-blind grader, emits the preregistered rates |
| `dev-tools/experiments/runners/tow_engines.py` | Engine adapters and the `--engines` keys |
| `dev-tools/experiments/runners/tow_fetch.py` | The shared fetch cache used by B and C |
| `dev-tools/experiments/runners/export_bundles.py` | Exports one audit bundle per case |
| `dev-tools/experiments/runners/tow_finder.py`, `tow_verifier.py` | The finder and verifier pull-loop workers |
| `python -m src.cli.approvals` | The approvals CLI a human uses to sign the sampled cases |

## The rules that keep it honest

- Ground truth never reaches the board. The publisher, date, and source URL live only in the manifest, which only the grader reads. A worker that could read the answer off the board would not be under test.
- Grading is label-blind. One scoring function, a fixed shuffle, no sight of the condition or engine. The person doing any human spot-check must not be the person who wrote the verifier's rules.
- Conditions B and C share one fetch cache, so all three conditions see identical observations of each page.
- One board per finder vendor, and the board never assigns, so vendor attribution is a property of the setup, not of a scheduler.
- The verify task is never offered to the claim's author or the author's vendor, so a worker cannot shape its answer to pass a test it can see.
- A human signs a sampled set of already-verified cases (`--approval-rate 0.1`).
- A fixed seed (`--seed 737`) makes sampling and shuffling deterministic.

## Glossary

- **Case:** one excerpt and the chain of tasks that answers it (find, verify, approve, synthesize).
- **Finder:** the agent that answers a question with a citation.
- **Verifier:** the deterministic script that fetches the cited page and runs the five checks.
- **Manifest:** the file the runner writes holding each case's ground truth and task ids, read only by the grader.
- **Audit bundle:** the exported record of one case: who claimed, what the checks found, the approval or refusal, and the cost.
- **Grounded engine:** a search engine backed by live web search, which bills for that search separately from tokens.

## Data handling

The dataset CSV, the manifests, and the raw audit bundles all contain the Tow excerpts and raw engine output. Never commit any of them. Keep every output under `~/marcus-experiments/`, never inside the repository. If you publish bundles, strip the excerpt text and raw engine prose first.

## Related

- The live study and per-case evidence: https://marcus-ai.dev/on-the-record/
- The detailed protocol lives in the runner docstrings listed above, and the preregistration is in the issue #737 history.
- Issue #737 (On the Record and the System of Record that produced it)
