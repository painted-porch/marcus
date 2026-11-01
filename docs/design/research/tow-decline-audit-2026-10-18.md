<!-- Decline-audit readout for On the Record (#737). One sentence per line. Written for a reader on their first day. -->

# On the Record: the decline audit (false-decline rate)

*2026-10-18. A human audit of the citations Marcus declined in the full-200 run, measuring the one axis the main readout did not: whether the declines themselves were warranted.*

## What is this, briefly

Marcus is open-source software that makes AI agents prove their work before anyone has to trust it.
In the On the Record study, AI search engines answered 200 news-sourcing questions, and Marcus accepted an answer only if an independent verifier could fetch the cited page and confirm it.
When the verifier could not confirm a citation, Marcus declined it: it withheld the answer and attached the fetch receipts instead of delivering a claim it could not stand behind.

## The question this audit answers

The full-200 readout measured two things: how often a delivered answer was still wrong (precision), and how often Marcus declined (coverage).
It never measured whether the declines were correct.
A decline is the right call when the citation really was bad, but it is a mistake when the citation was good and the verifier refused it anyway.
A good citation wrongly refused is a false decline, and the share of declines that are false is the false-decline rate.
This is a decline-conditioned quantity, the probability that a citation was good given that Marcus declined it, so it is not recall itself; the connection to recall, which also needs the delivered citations, is worked out in the results.
This audit measures that number for the first time.

## Method

From the full-200 Condition B run (four engines, 800 cases, 141 delivered and 659 declined), we drew a blind sample with seed 737.
The sample is 50 declines that carried a citation (the 504 declines where the engine actually gave a source, not the 155 where the engine itself refused) plus 10 delivered-and-verified cases as controls, shuffled together into 60 rows.
The judging sheet showed only the quoted sentence and the engine's own citation (headline, publisher, date, URL).
It deliberately hid what Marcus decided, the verifier's verdict, the verifier's checks, and the true source, so the judgment could not simply echo the machine or collapse into string-matching against the answer key.
For each row the judge opened the cited link and decided, on the merits, whether that page is a source a reader could trust and verify for that sentence: `correct`, `wrong`, or `borderline`.
After all 60 were judged, the calls were joined to a held-back key to compute the false-decline rate and to check the controls.

## Results

The false-decline rate is **10.0% (5 of 50), 95% Wilson interval [4.3%, 21.4%]**.
Counting the 3 borderline cases as possible false declines raises it to 16.0% (8 of 50), interval [8.3%, 28.5%].
Extrapolated to the 504 declines that carried a citation, that is about **50 trustworthy citations wrongly withheld** (interval 22 to 108), roughly 6% of all 800 Condition B cases.

The controls held up.
All 10 delivered answers were correct citations.
Three were marked wrong on first pass only because the article sits behind a paywall the judge could not read, but the engine's URL in each matched the true source exactly and every verifier check had passed, so they are correct citations the reader simply could not open without a subscription, not bad deliveries.
So when Marcus delivers an answer, the human audit agrees it is trustworthy, which corroborates the delivered-wrong figures (12.1% strict, 4.3% adjudicated) and the zero-fabrication result.

## What the false declines actually were

Nearly every false decline was the verifier failing to fetch or match the correct page, not the engine citing a bad source.

- Three (TIME, TechCrunch, and a National Geographic borderline) had a URL that exactly matched the true source, with publisher and date confirmed, but the verifier's quote-on-page check returned false and declined a correct citation.
- One (Mississippi Today) returned HTTP 403 to the verifier's fetcher, so the correct page could not be read.
- One (Philadelphia Tribune) was a Gemini grounding-redirect wrapper URL that the verifier could not follow, though it redirects to the right article.
- The remaining cases were a dead page (link rot, the article is simply gone), a recurring annual column cited at the wrong year, and one syndicated republication the judge accepted as trustworthy.

The pattern is clear: the dominant cause of false declines is the verifier's fetch-and-match brittleness on pages that are genuinely correct, which is exactly the mechanism the methodology review predicted.

## What this changes, and what it does not

It does not change the delivered-answer results, and it corroborates them: the delivered set is trustworthy.
It adds the false-decline measurement the study was missing: the 82% decline rate is not all correct refusals, because about 1 in 10 of the declines with a citation were good answers lost.
That 10% is the false-decline rate, not recall, because recall is the share of all good citations that were delivered rather than withheld, so it also needs the delivered side.
Of the 141 delivered answers, roughly 124 to 135 carried good citations (from the 12.1% strict and 4.3% adjudicated delivered-wrong rates), set against the roughly 50 good citations wrongly withheld, which puts a rough recall near 124 / (124 + 50), about 71%.
That recall figure combines two separate estimates and should be read as illustrative magnitude, not an exact number.
It refines the "sorting, not fixing" framing rather than weakening it, because the sorting errs in both directions: a little wrong gets through, and some right gets blocked.
The same verifier runs in Condition C, but this audit sampled only Condition B's declines; because Condition C reuses Condition A's engine answers while Condition B draws fresh ones, the mix of citations among C's declines can differ, so the 10% cannot be assumed for C without separately sampling C's declines.

## The fix

A more robust fetcher (follow redirects including grounding-redirect wrappers, handle 403s and soft paywalls gracefully, render pages that need JavaScript) and a fuzzier quote match (tolerate paraphrase and truncation) would recover most of the roughly 50 wrongly-withheld citations.
Recovering those five already-adjudicated citations adds no fabrications, because they are known to be correct.
The broader rules are a different matter: looser redirect, fetch, and quote-match logic applies to every future citation, including bad ones that currently fail closed, and a fuzzier quote match could accept a wrong page whose text merely resembles the quote.
So the zero-new-fabrication claim is safe only for the already-adjudicated cases; any such relaxation has to be re-measured against precision and fabrication before it can be said to raise coverage without touching delivered quality.
This is the top item for the verifier's next version (#752), to be evaluated against delivered quality after the change rather than assumed free.

## Honest limitations

The judge was the study's author, not an independent reviewer, which is the independence gap the methodology review named; a second reviewer would strengthen this.
The sample is 50 declines, so the interval is wide ([4.3%, 21.4%]).
The recall figure is derived by combining the delivered-good count with the extrapolated withheld-good count, so it carries the uncertainty of both and is offered only as a rough magnitude.
The "can a reader trust and verify this" standard is a judgment call, which is why the borderline band and both rates are reported.

## Data handling

The per-case judging sheet and answer key live only under `~/marcus-experiments/` because the judging sheet contains the Tow excerpts; they are never committed.
This readout reports only aggregate results and public article URLs, no excerpts.

## Related

The full-200 readout (`tow-full-200-readout-2026-10-16.md`), issue #737 (the preregistration), and `REPRODUCING.md`.
