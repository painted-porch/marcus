<!-- Full-200 readout for the On the Record experiment (#737). One sentence per line. Written for a reader on their first day. All four engines are complete through every condition. -->

# On the Record: the full 200-question readout

*2026-10-16. All four engines (OpenAI, Gemini, Perplexity, Claude Haiku) are complete through Conditions A, B, and C.*

## What is this, briefly

Marcus is an open-source coordinator where AI agents pull tasks from a shared kanban board.
This experiment asked AI search engines 200 news-sourcing questions from the Tow Center's 2025 citation study, then measured what happens when the same engines must pass their answers through Marcus's enforcement: evidence required to close, an independent check the author cannot see or run, and a human signature.
The question is not "are the engines good" but "how many wrong answers actually reach a person who will act on them."

## How to read the numbers

Every rate below is **wrong among answers actually delivered** — answers that reached the user.
A declined case is not a wrong answer; it is the system refusing to ship an unverifiable claim, and every decline carries its fetch receipts.
This matters because the whole point of the checkpoint is to convert a caught bad answer into a labeled decline instead of a delivered falsehood.

The three conditions:

- **Condition A** — the engine alone, no board. The baseline.
- **Condition B** — the engine's answers run through Marcus: evidence gate, independent verifier, human approval.
- **Condition C** — the control. The same verifier used as a plain after-the-fact filter on Condition A's answers, with no board, no human, no record. This isolates what the *checker* does from what the *institution* does.

## Headline

Alone, the engines delivered a wrong answer about **7 times in 10** (69.5% pooled), with **roughly half their links fabricated or dead** (51.2%).
Through Marcus, wrong-among-delivered fell to **about 1 in 8** (12.1% pooled across all four engines) and **fabricated links delivered fell to zero** — every engine, every board.
The single sturdiest statement is the fabrication result: **51% of delivered links fabricated or dead, down to 0%.**

Two honest refinements of the headline, both from the category data below:

- Of the delivered answers that were wrong through Marcus, most were *partially* incorrect (a real, on-topic source with a wrong detail), which a link-fetch verifier cannot catch; **flatly wrong answers reaching the user were only 3.5% (about 1 in 29)**, and "any inaccuracy including partial" is the 12.1%.
- The re-pilot of 2026-10-13 measured the same metric (any inaccuracy among delivered answers, partial errors included) at 4.8%: one partially incorrect answer among 21 kept, from two engines on a stratified 50-excerpt slice.
  On those same two engines the full run measures 11.3% (9 of 80 delivered), and across all four engines 12.1%.
  The pilot's figure rests on 21 answers, whose 95% interval runs from about 1% to 23%, so the two runs are consistent; the difference is sample size and the two added engines, not a change in what was counted.

Gemini is the clearest single demonstration: it fabricated the most links when alone (65.6%) and delivered **zero** fabricated links through Marcus, at an 11.1% wrong-delivered rate.

**A scoring-rule change, dated 2026-11-05, does not move the number above.**
On 2026-11-05 a board decision (Marcus Program, OPTION B, decided by Larry) adopted a new rule for verifier v2 going forward: a citation confirmed as a faithful republication on a documented sister-site or wire-syndication network counts as correct, not wrong.
The number in this readout is left exactly as published, under the rule that scored it at the time.
Re-scoring this same full-200 run under the new rule would move the pooled Condition B confident-wrong rate from 12.1% to about 9.2%, about 2.8 percentage points (4 of the 141 delivered answers).
Of the 5 known-network matches identified in the original classification, only 4 pass the rule's own safeguard: a network pairing is not treated as proof that the cited article is the right one, so the promotion additionally requires the claim's date, when present, not to contradict the ground truth (a network match does not excuse a wrong date).
One of the 5 (a States Newsroom sister-site match, publisher correct, date five days off the tolerance) fails that check and stays wrong, which is the honest result of a review finding caught before this shipped, not a relaxed standard afterward.
This is a methodology change stated honestly, not a quiet restatement of a published figure; `extensions/citation_verifier/citation_verifier/grader.py`'s `grade()` keeps reproducing the number above by default (`scoring_version="v1"`) and only applies the new rule when a caller explicitly asks for `scoring_version="v2"`.

## Condition A — engines alone (complete, all four engines)

| Engine | Wrong among delivered | Fabricated links | Decline rate |
|--------|----------------------:|-----------------:|-------------:|
| OpenAI | 86.0% | 71.0% | 7.0% |
| Gemini | 82.8% | 65.6% | 21.5% |
| Perplexity | 57.0% | 34.9% | 14.0% |
| Claude Haiku | 51.8% | 32.9% | 15.0% |
| **Pooled (800)** | **69.5%** | **51.2%** | **14.4%** |

## Condition B — through Marcus

| Engine | Wrong among delivered | Fabricated links | Decline rate | Delivered |
|--------|----------------------:|-----------------:|-------------:|----------:|
| OpenAI | 16.7% | 0% | 91.0% | 18 |
| Gemini | 11.1% | 0% | 91.0% | 18 |
| Perplexity | 9.7% | 0% | 69.0% | 62 |
| Claude Haiku | 14.0% | 0% | 78.5% | 43 |
| **Pooled (800)** | **12.1%** | **0%** | **82.4%** | 141 |

Of the 141 delivered answers, 5 were completely incorrect (**3.5%**) and 12 partially incorrect; the 12.1% is the sum.

### The 10% threshold was crossed, and we report it as rejected

The preregistration set a falsification line: "a B confident-wrong rate above 10 percent means the verifier is being fooled (syndicated copies, quoting pages) and needs to be better."
At 12.1% the full run crosses that line, and H1 (B below five percent) is rejected.
We report both as rejected, because the preregistration committed to doing so, and because the mechanism it named turned out to be exactly right.

### What the 17 wrong-delivered answers actually are

We inspected every one of the 17, attribute by attribute, against both the cited page and the ground truth, and spot-checked a sample of the correct-graded answers for fairness (12 of 12 cited the exact original outlet, so the strict standard is applied evenly in both directions).
In all 17 the verifier's checks passed legitimately: the cited page is real and contains the quoted excerpt.
None is a fabricated or non-supporting link.
The 17 break down as:

- **11 are valid citations under a "can the reader trust and verify this" standard.**
  Eight are faithful news republications: the engine cited a real outlet that genuinely ran the story (States Newsroom sister sites, the USA Today Network, Mississippi Today, a WSJ pickup), excerpt present and correctly attributed, but not Tow's designated *original* outlet.
  Two are the same outlet at a moved or section-variant URL (the San Francisco Chronicle).
  One (tow-0073) is a case where the engine's date matched the page's own declared date and Tow's ground-truth date is the outlier.
- **3 have a valid republished source but a genuinely broken date field.**
  The Claude Haiku adapter emitted reasoning text such as "969 days ago from the search metadata ... approximately 2024" instead of a parseable date (tow-0037, tow-0138, tow-0164).
  The source is a real republication; the date is a real engine defect, and we count these as failures.
- **3 are genuine near-misses:** a story 3.5 months off the original (tow-0060, likely a different article), a citation to an older article on the same topic (tow-0129), and a newsletter that quotes rather than republishes the source (tow-0179).
  Addendum, 2026-11-05 (issue #745's analysis pass): tow-0129 is named here deliberately, not in the moved-URL bucket above, because its claimed and canonical URLs share one article's slug but differ in the date segment of the path -- the same site's own "this story is still being updated" pattern, not a move. The two moved-URL cases above are a different, cleaner pattern: identical slug, identical day, only the section path changed.

### The second, clearly-labeled number

Under the preregistered strict original-source standard, Condition B confident-wrong is **12.1%**.
Under a standard that accepts a faithful republication or a moved URL as a valid citation — which is what matters for whether a delivered answer is trustworthy — the genuine-failure rate is the 3 broken-date answers plus the 3 near-misses, about **6 of 141, or 4.3%**, which meets H1's five-percent bar.
This is a post-hoc analysis and does not replace the preregistered number; both are reported, with the full per-case table in the run's artifacts.
The honest reading is that the preregistered metric is a strict journalism-provenance standard (original source only), and most of what it flags is the verifier accepting legitimate republications, exactly the mechanism the preregistration predicted and issue #745 now tracks.

## Condition C — verifier as a plain filter (the control)

| Engine | Wrong among delivered | Fabricated links | Decline rate | Delivered |
|--------|----------------------:|-----------------:|-------------:|----------:|
| OpenAI | 6.2% | 0% | 92.0% | 16 |
| Gemini | 0.0% | 0% | 92.5% | 15 |
| Perplexity | 13.3% | 0% | 70.0% | 60 |
| Claude Haiku | 8.7% | 0% | 77.0% | 46 |
| **Pooled (800)** | **9.5%** | **0%** | **82.9%** | 137 |

## What Condition C proves

On raw error rate, Condition C (9.5%) is close to Condition B (12.1%) — the plain filter and the full institution end up in the same single-to-low-double-digit range, with both eliminating fabricated links.
That is the expected and intended result: **the checker is what catches the errors, and the checker is about 300 lines of ordinary code.**
If the only goal were a lower error rate, a bolt-on filter would be enough, and this experiment says so plainly.

Marcus's value is everything Condition C cannot have, and the difference does not show up in the error column:

- The check in C can be skipped; the check in B is unskippable, because nothing closes without it.
- The filter in C is run by whoever holds the answers; the check in B is run by someone who is not the author and cannot see the author's task, so it cannot be gamed under pressure.
- C drops bad answers silently; B converts them into labeled declines that carry their fetch receipts.
- C leaves no record; B leaves a complete, exportable audit bundle per case: who claimed, who checked, who signed, what it cost.
- C is measured here in a benign lab where the answers were generated with no knowledge a filter was coming; B's guarantees hold under adversarial pressure, which is the real deployment condition.

So Condition C is the filter's best case, and it still needs the institution around it to be trustworthy rather than merely accurate.

## What the preregistration got right and wrong

A preregistration is only honest if we report where it was wrong, not just where it was right.

**Wrong: the central premise that engines had improved since 2025.**
The preregistration expected Condition A at 15 to 40 percent confident-wrong, explicitly "below the 2025 figures because the engines improved."
Condition A came in at **69.5 percent**, against the 2025 study's aggregate above 60 percent.
The engines did not improve; the AI citation problem is as bad as a year ago, or worse.
This is a finding in its own right, and it recalibrates how to read the rest: every absolute target (B below 5 percent, C under 3 percent) was anchored to a cleaner starting point that never arrived, so the input to the verifier was far dirtier than the preregistration assumed.

**Wrong: H1's absolute bar, which we report as rejected.**
H1 predicted Condition B confident-wrong below 5 percent; the result is 12.1 percent, so H1 is rejected.
We do not move the bar after the fact.
We do note that H1's bar was premised on the improvement assumption above, and that the 17-case inspection shows most of the 12.1 percent is strict-provenance accounting, not untrustworthy citations.

**Right: the comparative claim, which was the actual test.**
H1 was framed "against the study's aggregate above sixty percent and Perplexity's thirty-seven," so the comparison was always the point.
The checkpoint cut confident-wrong from 69.5 percent to 12.1 percent and fabricated links from 51.2 percent to zero, across every engine.
That comparative collapse is confirmed, and it is what determines whether the checkpoint works.

**Right: the predicted failure mechanism.**
The preregistration named "syndicated copies, quoting pages" as how the verifier could be fooled, and that is precisely what the 17-case inspection found.

**Forward, not backward.**
Because the absolute thresholds were calibrated to a 2025-improvement baseline that did not hold, a future run should re-preregister its thresholds against the observed 2026 baseline (Condition A near 70 percent) before running, and against an improved verifier (issue #745).
We do not retrofit this run's thresholds; we design the next run correctly, in the open.

## Honest limitations

Decline rates in Condition B are high (69-91%), so Marcus ships few answers; the claim is about the quality of what it ships, not the quantity.
Delivered counts per engine are small (18-62), so per-engine wrong rates carry real sampling noise; the pooled figure and the fabrication result are the sturdier numbers.
The per-case cost figures are **token-only and badly understate the grounded engines**: OpenAI and especially Gemini bill separately for Google/Bing-style search grounding, which our usage logging never captured.
Measured in practice, the Gemini board's grounding fees ran on the order of **$30+ for the run** against a token-only reading of a few cents, so Gemini's true per-case cost is roughly 15-20x the token figure; cross-engine cost comparisons here are apples-to-oranges and should not be read as a cost ranking.
Absolute rates will drift with engine versions and link rot; the gap between conditions is the finding, not the absolute level.

## Method anchors

200 questions, seed 737, graded blind.
Link-rot baseline measured before spending: 47% of the ground-truth pages are already dead or closed to robots, which is why decline rates are read net of that floor.
Marcus's own AI was disabled in every run (`ai.enabled=false`) and every engine key was stripped from the server's environment, so Marcus made zero model calls; the engine spend happened only in the worker processes.
Full method and reproduction: [REPRODUCING.md](../../../REPRODUCING.md).

## Related

Issue #737 (the preregistration), the `on-the-record-v1` release (redacted audit bundles for all four boards), the dated pilot readouts beside this file, Jaźwińska & Chandrasekar, "AI Search Has a Citation Problem," Tow Center for Digital Journalism, CJR, March 2025.
Coordinator defects surfaced by running this at scale: issues #740, #741, #742, #743 (PR #744).
