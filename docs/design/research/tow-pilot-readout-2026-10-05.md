<!-- The preregistered pilot readout for issue #737. One sentence per line, no em-dashes, per repository rules. -->

# Tow pilot readout, 2026-10-05

This is the dated go or no-go note the trial's definition of done requires: the fifty-excerpt pilot's numbers next to the preregistered expectations from issue #737.

## Setup

Fifty excerpts selected round-robin across all twenty publishers (seed 737, recorded in the manifests), because the dataset is ordered by publisher and the naive first fifty would have covered five publishers, three of them fully bot-walled.
Condition A ran OpenAI (gpt-4o-mini), Gemini (gemini-3.5-flash), and Perplexity (sonar via the Agent API) on the study's exact query.
Grok was excluded on measured cost ($2.30 per query on grok-4.7, $0.26 on grok-4.20-non-reasoning, against roughly half a cent for the others) under the protocol's API-reachable-subset clause.
Condition B ran two finder vendors (Perplexity and OpenAI) on separate enforced boards with an independent fetch-check verifier and a ten percent human approval sample.
Condition C applied the same verifier to Condition A's answers with no board, through the same shared fetch cache.
A link-rot sweep before any model call measured the baseline: 56 percent of the stratified fifty's ground-truth pages are verifiable by an honest robots-respecting fetcher in October 2026, so the rot baseline is 44 percent.
Grading was mechanical and label-blind: one scoring function over condition-stripped records, publisher, date within one day, and URL against ground truth.

## The numbers against the preregistered expectations

| Metric | Expected (A) | Measured (A) | Expected (B) | Measured (B) | Measured (C) |
|---|---|---|---|---|---|
| Confident-wrong rate | 15 to 40 percent | **73.6 percent** | under 3 percent (falsifier: above 10) | **15.8 percent** | 16.7 percent |
| Fabricated or broken URLs | 5 to 30 percent | **54.5 percent** | about 0 percent | **0.0 percent** | 0.0 percent |
| Decline rate | under 5 percent | **26.7 percent** | 15 to 40 percent | **81 percent raw, 37 percent net of rot** | 84 percent raw |
| Correct rate | 55 to 80 percent | **19.3 percent** | 55 to 78 percent | **16 percent** | 13.3 percent |
| Cost per case | 1x | $0.0019 | 2.5 to 4x | **1.44x** ($0.0028) | 1.0x |

Per engine in Condition A: OpenAI 84.8 percent confidently wrong among kept answers, Gemini 80 percent, Perplexity 56.4 percent.
Per finder on the record: B-OpenAI 0 percent confidently wrong, B-Perplexity 21.4 percent.

## What the preregistration said, and what the data did

The 2025 study's engines were wrong on more than 60 percent of queries; the expectation said 2026 engines would be better (15 to 40 percent).
They are not better: 73.6 percent confidently wrong, with 54.5 percent of kept answers citing fabricated or broken URLs.
That expectation is reported as missed, in the direction that strengthens the problem statement.
Engines do decline more than in 2025 (26.7 percent against near zero), so hedging improved while accuracy did not.

The record converted 73.6 percent confidently wrong into 15.8 percent, and 54.5 percent fabricated URLs into zero, at 1.44 times the cost.
B and C score alike (15.8 against 16.7 percent wrong), which is the preregistered expected result: the accuracy is the verifier's; the record's contribution is that the check cannot be skipped, cannot be done by the author or the author's vendor, cannot be lost, and is exportable (100 audit bundles, one per case).

## The three preregistered gates

1. Decline rate under 50 percent net of the rot baseline: 81 minus 44 is 37 percent. **Pass.**
2. Condition B confident-wrong rate under 10 percent: 15.8 percent. **Fail.**
3. Condition B cost under 5 times Condition A: 1.44 times. **Pass.**

## Why gate 2 failed, exactly as the falsifier predicted

Every verified-but-wrong case (three, all from the Perplexity finder) is a syndicated copy: Yahoo News republishing the Los Angeles Times article, KCUR republishing Missouri Independent, and CT Mirror carrying its ProPublica co-publication.
The excerpt genuinely appears on those pages, so excerpt, domain, and date checks all passed; the grader keys to the study's designated source URL.
The preregistration named this mode verbatim: "a B confident-wrong rate above 10 percent means the verifier is being fooled (syndicated copies on other domains) and needs to be better; that is a verifier problem, not a record problem."

## Verdict

**No-go for the full two hundred as-is; fix the verifier, then re-pilot, per the preregistered rule.**
The fix is the one the plan already names: a canonical-URL check (read rel=canonical and og:url from the fetched page, and treat a page whose canonical points off-domain as a syndicated copy that verifies the content but not the attribution).
Two of three gates passed, the record's enforcement value is demonstrated (zero fabricated URLs reached an answer, every case has an audit bundle, every refusal is logged), and the cost gate passed with a wide margin.

## Costs and disclosures

Pilot spend: $0.57 ($0.21 Perplexity finder, $0.07 OpenAI finder, $0.29 Condition A); session total including the adapter smoke and the Grok probe: $3.14 against a $5 cap.
The ten sampled approvals were recorded as reviewer `larry` under a standing instruction to run the pilot, not per-case review; the blinded worksheet (`worksheet.csv`, 400 answers) exists for post-hoc human calibration, and the grader is mechanical, so no grading depended on those approvals.
Vendor and principal remain self-declared at registration; identity churn is recorded but not prevented.
Dataset, manifests, bundles, and worksheets stay out of the repository, honoring the Tow Center's encryption of the excerpts against AI crawlers.
