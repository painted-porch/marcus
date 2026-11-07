# Reproducing "On the Record"

Marcus is open-source software for getting real work out of AI agents and being able to trust what comes back.
"On the Record" is a preregistered replication of Columbia's Tow Center 2025 study of AI search, "AI Search Has a Citation Problem."
We asked four AI search engines the same 200 news questions, where we already knew the correct source, and then ran the same answers a second time through Marcus's verification checkpoint.
This guide reproduces that study end to end.

The live study, case by case, is at https://marcus-ai.dev/on-the-record/.

## The result you should get

| Condition | What it is | Answers wrong | Links fabricated, dead, or wrong source | Declined, not delivered |
|---|---|---|---|---|
| A | The four engines alone, no board | about 69.5% | about 51.2% | about 14% |
| B | The same engines run through Marcus | about 12.1% | 0% reach the reader | about 82% |
| C | The verifier bolted onto A as a post-hoc filter, no board | about 9.5% | 0% | about 83% |

**Dated note, 2026-11-05:** the table above is left exactly as published, scored under the rule in force when it was run.
A board decision that day (Marcus Program, OPTION B) adopted a new rule for verifier v2 going forward: a citation confirmed as a faithful republication on a documented sister-site or wire-syndication network now scores as correct, not wrong, provided the claim's own date does not independently contradict the truth (a network match is not proof the cited article is the right one, so a wrong date still fails it).
Re-scoring row B's result under that new rule would move it from about 12.1% to about 9.2% (4 of the 141 delivered answers), not from any change to the published numbers themselves.
Run `tow_grader.py` with `--scoring-version v2` to see the new rule applied; the default (`v1`, what produced the table above) never changes.

The wrong-answer rates are among answers actually delivered.
A declined case is the system refusing to ship an unverifiable claim, not a wrong answer, and every decline carries its fetch receipts.
Across the four engines Marcus delivered 141 of 800 answers and declined the rest, so it ships far fewer answers, and the claim is about the quality of what it ships, not the quantity.

The honest reading: this is sorting, not fixing.
The engines invent just as many bad links in condition B as in condition A.
Marcus catches them instead of delivering them, so no fabricated link reaches the reader, and every decision is on the record.
The preregistration (issue #737) predicted that fewer than 5 percent of delivered answers would still be wrong, and set a separate line that a rate above 10 percent meant the verifier was being fooled and needed a second version.
At 12.1 percent strict, both are crossed, so the hypothesis is reported as rejected.
Inspecting all 17 wrong-delivered answers one by one found that most were legitimate republications the strict original-source standard rejects, not untrustworthy citations.
Under a "can the reader trust and verify this" standard the genuine-failure rate is about 4.3 percent, and both numbers are reported, the strict 12.1 percent and the adjudicated 4.3 percent, rather than the flattering one alone.

Two caveats on the numbers.
Per-engine delivered counts are small, 18 to 62 of 200 for each engine, so per-engine rates carry real sampling noise, and the pooled figure and the zero-fabrication result are the sturdier numbers.
Cross-engine cost is not comparable, because each provider counts the web content its search pulls back as input tokens very differently, which the matching Condition A and B prompt sizes confirm is an accounting difference, not unequal context.

## Before you start

1. **The dataset.** The study uses `GenAISearch_Data.csv`, 200 excerpts across 20 publishers. We do not redistribute it. Get it from the Tow Center and decrypt it locally; the passphrase is in their README. Do not commit the CSV, and do not commit the manifests the runner writes, because both contain the excerpts and answers.
2. **Marcus installed.** Python 3.11 or newer, and a working Marcus checkout. See the project README for setup.
3. **Engine API keys.** The run uses four engines: an OpenAI model, Google Gemini, Perplexity, and Anthropic Claude Haiku. The exact `--engines` keys are defined in `extensions/citation_verifier/citation_verifier/engines.py`. Grounded engines bill for web search or grounding separately from tokens; the runner now records those fees as `search_fee_usd` on every answer, so the cost log no longer undercounts OpenAI's search or Gemini's grounding the way the first full run did.
4. **Cost.** A clean run of the full study costs about $35, roughly $0.18 per question across the four engines and the two paid conditions; our own run cost about $65 all-in, because pilots and re-running a rate-limit-flaky Gemini added overhead a clean run avoids. Gemini's grounding dominated, about $44 of the all-in total against a few cents of tokens, so grounded-engine fees are the thing to budget for. Conditions A and B each make roughly 800 paid engine calls (four engines times 200 questions); Condition C is free, because it only fetches pages. Get a cost estimate approved before running; the Condition A runner refuses to start without `--confirm-paid` for exactly this reason.

## The free rehearsal (start here)

The citation verifier is a Marcus extension packaged under `extensions/citation_verifier/` (ADR-0013 K11).
Every command below runs it by package name: either `pip install -e extensions/citation_verifier` once, or export `PYTHONPATH=extensions/citation_verifier` from the repository root.
The console scripts `tow-seed`, `tow-finder`, `tow-verifier`, `tow-export`, `tow-redact`, `tow-grade`, `tow-condition-a`, `tow-condition-c` and `tow-rot-sweep` are the same entry points after the install.

Every moving part runs end to end with a fake engine and zero API calls, so you can see the board enforce its rules before spending anything.
The commands below were run as written on 2026-11-01: four cases, thirteen tasks, every one reached done, and each exported bundle named the finder, the verifier's checks, the refusals, and the human approval.

```bash
EXP=~/marcus-experiments/tow/rehearsal && mkdir -p $EXP/run

# 1. Seed a 4-case board; this also registers it in Marcus's project registry.
python -m citation_verifier.runner \
  --dataset /path/to/GenAISearch_Data.csv \
  --db $EXP/kanban.db --project tow-rehearsal \
  --limit 4 --approval-rate 0.25 --seed 737 --manifest $EXP/manifest.json

# 2. Write $EXP/config.json: copy config_marcus.example.json and set
#    kanban.provider = "sqlite", kanban.sqlite_db_path = "$EXP/kanban.db",
#    default_project_name = "tow-rehearsal - Main Board", project_name = "tow-rehearsal",
#    ai.enabled = false, features.context = false, transport.http_port = 4777.

# 3. Start the server FROM THE REPO ROOT (persistence paths are cwd-relative)
#    with every engine key stripped from its environment.
env -u OPENAI_API_KEY -u GEMINI_API_KEY -u PERPLEXITY_API_KEY -u CLAUDE_API_KEY -u ANTHROPIC_API_KEY \
  MARCUS_CONFIG=$EXP/config.json python -m src.marcus_mcp.server --http --all-tools

# 4. In another shell (a new shell starts without EXP, so set it again): the fake
#    finder, the verifier, the human approval, the verifier again.
EXP=~/marcus-experiments/tow/rehearsal
python -m citation_verifier.finder --url http://localhost:4777/mcp \
  --project tow-rehearsal --agent-id finder-fake-1 --vendor fakevendor --engine fake --idle-exits 3
python -m citation_verifier.verifier --url http://localhost:4777/mcp \
  --project tow-rehearsal --agent-id verifier-fetch-1 --vendor fetchcheck \
  --cache-dir $EXP/fetch-cache --idle-exits 3
python -m src.cli.approvals --url http://localhost:4777/mcp --project tow-rehearsal \
  --reviewer you --decide approved
python -m citation_verifier.verifier --url http://localhost:4777/mcp \
  --project tow-rehearsal --agent-id verifier-fetch-1 --vendor fetchcheck \
  --cache-dir $EXP/fetch-cache --idle-exits 2

# 5. Export the audit bundles.
python -m citation_verifier.export_bundles --url http://localhost:4777/mcp \
  --manifest $EXP/manifest.json --out-dir $EXP/bundles
```

Expected: the finder reports four finds and is then offered nothing (its refusals are in the bundles), the verifier closes the four verify tasks as `unverifiable` (the fake engine cites invented pages) and the synthesize tasks that do not wait on a person, the approval command records one decision, the second verifier pass closes the last synthesize task, and `export_bundles.py` writes four bundles.
Then try to cheat: report a find task completed without evidence and watch the board reject the close, or request work as the finder after its finds are done and watch it be offered nothing.
The manifest and the board hold the excerpts, so keep `$EXP` outside the repository.

## The three conditions, in plain terms

- **Condition A** is each engine answering alone, with no board and no verification. This is the baseline the original study measured.
- **Condition B** is the same engines working inside Marcus, where the board enforces three rules: nothing is accepted without its evidence, every claim is checked by a deterministic script the claim's author never sees, and a human signs off on a sample.
- **Condition C** is the verifier from B applied to Condition A's answers as a plain filter, with no board and no record. It is the control. If C scores like B, the accuracy gain came from the verifier. What B adds on top is that the check cannot be skipped, cannot be done by the author or the author's vendor, cannot be lost, and can be shown to a third party.

## Step by step

Pick an output directory outside the repository, for example `~/marcus-experiments/tow/repro`. Nothing below should be committed.

**1. Condition A (paid).** One request per engine per excerpt; the raw response, the parsed claim, the model, token counts, and cost land in one JSONL row per answer.

```
python -m citation_verifier.condition_a \
    --dataset /path/to/GenAISearch_Data.csv \
    --engines openai gemini perplexity claude-haiku \
    --limit 200 \
    --out ~/marcus-experiments/tow/repro/condition-a.jsonl \
    --confirm-paid
```

**2. Condition B (paid), one board per engine.** The board never assigns work, so attribution comes from environment design: seed one project per finder vendor, and register only that vendor's finder plus a different-vendor verifier. Repeat this for each of the four engines, changing `--project` and `--manifest`.

```
python -m citation_verifier.runner \
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

Run the finder and the verifier pull-loop workers against it (`citation_verifier.finder` and `citation_verifier.verifier`, both default to `--url http://localhost:4777/mcp`). The board drains in a fixed order, because the `synthesize` task that renders a case's final answer depends on the human `approve` task. So run the finder and verifier until they idle, sign the sampled approvals with the approvals CLI (`python -m src.cli.approvals`), then run the verifier once more so the now-eligible `synthesize` tasks complete. Skipping that second verifier pass leaves the approved sample without final answers, and the grader scores them as declines.

Export the audit bundles (the refusal log is written through to `data/marcus.db` as it happens, so a restart between the run and the export no longer empties it; the October 2026 full-200 bundles predate that fix and were exported with empty refusal lists):

```
python -m citation_verifier.export_bundles \
    --url http://localhost:4777/mcp \
    --manifest ~/marcus-experiments/tow/repro/manifest-openai.json \
    --out-dir ~/marcus-experiments/tow/repro/bundles-openai
```

**3. Condition C (free).** The same checks as B, applied to Condition A's answers as a filter. C and B share one fetch cache, which is a preregistered requirement so a page that moves between runs is scored identically in both.

```
python -m citation_verifier.condition_c \
    --a-responses ~/marcus-experiments/tow/repro/condition-a.jsonl \
    --dataset /path/to/GenAISearch_Data.csv \
    --cache-dir ~/marcus-experiments/tow/repro/fetch-cache \
    --out ~/marcus-experiments/tow/repro/condition-c.jsonl
```

**4. Grade, label-blind.** The grader normalizes every answer from every condition into one record shape, assigns a blind id, shuffles with a fixed seed, and scores with one function that never sees the condition or the engine. It reads ground truth only from the manifests, which never touched the board. It emits the preregistered rates: confident-wrong, fabricated-URL, decline, correct, and cost per case. See `extensions/citation_verifier/citation_verifier/grader.py` for its arguments.

## Where to look in the code

| File | Purpose |
|---|---|
| `extensions/citation_verifier/citation_verifier/runner.py` | Seeds Condition B cases onto a Marcus board, writes the ground-truth manifest |
| `extensions/citation_verifier/citation_verifier/condition_a.py` | Condition A: each engine alone |
| `extensions/citation_verifier/citation_verifier/condition_c.py` | Condition C: verifier-only control |
| `extensions/citation_verifier/citation_verifier/checks.py` | The five deterministic checks (`decide_verdict`) |
| `extensions/citation_verifier/citation_verifier/grader.py` | Label-blind grader, emits the preregistered rates |
| `extensions/citation_verifier/citation_verifier/engines.py` | Engine adapters and the `--engines` keys |
| `extensions/citation_verifier/citation_verifier/fetch.py` | The shared fetch cache used by B and C |
| `extensions/citation_verifier/citation_verifier/export_bundles.py` | Exports one audit bundle per case |
| `extensions/citation_verifier/citation_verifier/finder.py`, `tow_verifier.py` | The finder and verifier pull-loop workers |
| `python -m src.cli.approvals` | The approvals CLI a human uses to sign the sampled cases |

## The rules that keep it honest

- Ground truth never reaches the board. The publisher, date, and source URL live only in the manifest, which only the grader reads. A worker that could read the answer off the board would not be under test.
- Grading is deterministic and label-blind: one scoring function, a fixed-seed shuffle, no sight of the condition or engine. Because the scorer is deterministic and never sees the label, the shuffle changes none of the automated rates; it is a tidiness measure, and the rigor that matters is the manifest isolation in the bullet above, not the shuffle. The person doing any human spot-check must not be the person who wrote the verifier's rules.
- Conditions B and C share one fetch cache, so all three conditions see identical observations of each page.
- One board per finder vendor, and the board never assigns, so vendor attribution is a property of the setup, not of a scheduler.
- The verify task is never offered to the claim's author or the author's vendor, so a worker cannot shape its answer to pass a test it can see.
- A human signs a sampled set of already-verified cases (`--approval-rate 0.1`).
- A fixed seed (`--seed 737`) makes sampling and shuffling deterministic.
- Do not read the small gap between Condition B (12.1%) and Condition C (9.5%) as a board effect. Condition B draws fresh engine answers and Condition C reuses Condition A's, so the gap is an independent re-draw plus approval rejections, not the board making answers more accurate; what B adds over C is verification that cannot be skipped, cannot be run by the author, and is recorded, not a lower error rate.
- If you run fewer than 200 cases, pass the same `--stratify` to Condition A and to the Condition B seeding step, or the two will select different excerpts and stop being comparable; at the full 200 this is moot.

## Glossary

- **Case:** one excerpt and the chain of tasks that answers it (find, verify, approve, synthesize).
- **Decline reason:** every verify verdict records `decline_reason` in its checks: `opaque_redirect` (a search-grounding redirect wrapper whose robots policy forbids every agent), `robots_blocked`, `no_url` (the engine cited nothing), `address_blocked` (a URL the fetcher refuses on policy: non-http scheme, private or loopback address), `publisher_blocked` (HTTP 401, 403, 429, 451), `dead_link` (404, 410), `http_error`, `fetch_error` (no response), `empty_page` (a 2xx with no body), `excerpt_missing`, `syndicated_copy`, `publisher_mismatch`, `date_mismatch`, or `None` when verified.
  Read declines net of `publisher_blocked`, `dead_link`, `opaque_redirect`, `no_url` and `address_blocked` when you want the verifier's own error rate rather than the web's or the engine's.
- **Excerpt coverage:** the verify checks also record `excerpt_coverage` (share of the excerpt's words found on the page, in order) and `excerpt_longest_run`; the excerpt counts as present at coverage 0.85 with a run of eight words (half the excerpt when shorter), measured inside windows around the quoted run with punctuation ignored.
- **Finder:** the agent that answers a question with a citation.
- **Verifier:** the deterministic script that fetches the cited page and runs the five checks.
- **Manifest:** the file the runner writes holding each case's ground truth and task ids, read only by the grader.
- **Audit bundle:** the exported record of one case: who claimed, what the checks found, the approval or refusal, and the cost.
- **Grounded engine:** a search engine backed by live web search, which bills for that search separately from tokens.

## Data handling

The dataset CSV, the manifests, and the raw audit bundles all contain the Tow excerpts and raw engine output. Never commit any of them. Keep every output under `~/marcus-experiments/`, never inside the repository. If you publish bundles, strip the excerpt text and raw engine prose first.
The same rule covers test fixtures, documents, and web pages: not even a fragment of an excerpt, and not a ground-truth URL, because an agent that reads the source can decode anything a page encodes.
Before committing, run `python dev-tools/experiments/runners/dataset_leak_scan.py` against your local dataset; it scans for excerpt fragments, encoded payloads, split URLs, and headlines, and reports file names only.
The pre-commit hook `tow-dataset-leak-scan` runs it automatically whenever the dataset is present on your machine.

## Related

- The live study and per-case evidence: https://marcus-ai.dev/on-the-record/
- The detailed protocol lives in the runner docstrings listed above, and the preregistration is in the issue #737 history.
- Issue #737 (On the Record and the System of Record that produced it)
