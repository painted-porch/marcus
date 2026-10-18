<!-- The re-pilot readout for issue #737, after the canonical-URL fix. One sentence per line, no em-dashes, per repository rules. -->

# Tow re-pilot readout, 2026-10-13: all three gates pass

The 2026-10-05 pilot failed one of three preregistered gates: Condition B's confident-wrong rate was 15.8 percent against the 10 percent falsifier, caused entirely by syndicated copies, exactly the fooling mode the preregistration named with its prescribed remedy.
The verifier gained the canonical-URL check (a page whose declared canonical lives on another domain is a republication: the content verifies, the attribution does not), and the fifty-excerpt pilot re-ran in full.
This note is the dated go or no-go readout for the re-pilot.

## Setup differences from 2026-10-05

Same stratified fifty excerpts (seed 737, all twenty publishers), same three engines in Condition A (OpenAI gpt-4o-mini, Gemini gemini-3.5-flash, Perplexity sonar; Grok still excluded on measured cost), same two finder vendors in Condition B, same verifier and grader code paths plus the canonical-URL check.
All artifacts now live in durable storage (`~/marcus-experiments/tow/pilot-2/`); the first pilot's raw files were lost to a temp-directory purge, a storage mistake now fixed and recorded.
A fresh rot sweep re-measured the baseline: 53 percent of all two hundred ground-truth pages verifiable (down one point in eight days), stratified-fifty baseline unverifiable 46 percent.
The board servers ran with the Context feature disabled after the first board's finder phase measured 95 of 103 minutes spent in request-path dependency inference (216 full-board runs); these boards' dependencies are explicitly wired, so the machinery was pure overhead, logged as a Marcus defect to fix properly (Simon 44db8b8b).

## The numbers

| Metric | A: engines alone | B: on the record | C: verifier, no board |
|---|---|---|---|
| Confident-wrong rate | **73.8 percent** | **4.8 percent** | 7.7 percent |
| Fabricated or broken URLs | **54.6 percent** | **0.0 percent** | 0.0 percent |
| Decline rate | 13.3 percent | 79 percent raw, **33 percent net of rot** | 82.7 percent raw |
| Correct rate | 22 percent | 20 percent | 16 percent |
| Cost per case | $0.0019 | $0.0027 (**1.42x**) | $0.0019 |

Condition A replicated the first pilot almost exactly (73.8 against 73.6 percent confidently wrong, 54.6 against 54.5 percent fabricated URLs), which is strong evidence the measurement is stable.
Per engine in A: OpenAI 87.2 percent confidently wrong among kept answers, Gemini 85.4 percent, Perplexity 47.6 percent.
On the record: B-OpenAI 0 percent confidently wrong, B-Perplexity 6.7 percent (one partially incorrect case among fifteen kept).
B and C score alike (4.8 against 7.7 percent), the preregistered expected result.

## The three preregistered gates

1. Decline rate under 50 percent net of the rot baseline: 79 minus 46 is 33 percent. **Pass.**
2. Condition B confident-wrong rate under 10 percent: 4.8 percent. **Pass** (was 15.8; the canonical-URL check removed every syndication miss).
3. Condition B cost under 5 times Condition A: 1.42 times. **Pass.**

## Verdict

**Go.**
All three preregistered gates pass; per the plan, the full two hundred excerpts may run.
Projected full-run cost at measured rates: roughly $2.30 (Condition A three engines at 600 calls, Condition B two finder vendors at 400 finder calls, everything else free), which exceeds the original $5 session cap's remaining headroom ($1.29) and therefore needs a fresh budget approval.

## Costs and disclosures

Re-pilot spend: $0.56 ($0.21 Perplexity finder, $0.07 OpenAI finder, $0.29 Condition A).
Session total across both pilots, the adapter smoke, and the Grok probe: $3.71 against the $5 cap.
The ten sampled approvals were again recorded as reviewer `larry` under the standing instruction, not per-case review; the blinded worksheet (400 answers) supports post-hoc calibration.
Vendor and principal remain self-declared; identity churn is recorded, not prevented.
Dataset, manifests, bundles, and worksheets stay out of the repository, in `~/marcus-experiments/tow/`.
