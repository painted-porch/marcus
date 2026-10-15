<!-- The Claude-arm readout for issue #737, extending the 2026-10-13 re-pilot. One sentence per line, no em-dashes, per repository rules. -->

# Tow Claude-arm readout, 2026-10-14: the pattern holds across model tiers

The 2026-10-13 re-pilot measured three budget engines (gpt-4o-mini, gemini-3.6-flash, perplexity sonar) and passed all three preregistered gates.
At the user's direction, two Claude arms were added on the same stratified fifty excerpts: Sonnet 5 as the consumer-default tier, and Haiku 4.5 as the price-tier match for the other engines.
Haiku 4.5 is also the model Marcus's planner is configured with, so the arm is an explicit, documented exception to the adapter's guard; Marcus's AI was fully disabled in every board run, making the overlap nominal only.

## The numbers, per Claude arm

| Metric | Sonnet 5 alone | Sonnet 5 on the record | Haiku 4.5 alone | Haiku 4.5 on the record |
|---|---|---|---|---|
| Wrong, among answers delivered | 50.0 percent | **6.7 percent** | 48.8 percent | **10.0 percent** |
| Fabricated or dead URLs | 37.5 percent | **0.0 percent** | 24.4 percent | **0.0 percent** |
| Correct rate | 32 percent | 28 percent | 26 percent | 18 percent |
| Decline rate | 4 percent | 70 percent raw | 18 percent | 80 percent raw |
| Cost per case | $0.096 | $0.095 | $0.040 | $0.042 |

Pooled across all five engine arms, Condition A is now 63.9 percent wrong among delivered answers with 45.2 percent fabricated or dead URLs (250 answers).
Pooled across all four boards, Condition B is 6.5 percent wrong among delivered answers with zero fabricated URLs (200 cases).
Condition C (verifier without the board) pools to 8.7 percent, the preregistered B-approximately-C expectation again.

## What the Claude arms add to the finding

The frontier model was the most accurate engine tested and still delivered wrong answers half the time it answered, with 37.5 percent of its citations failing to hold.
The budget Claude matched the frontier Claude on wrongness (48.8 against 50.0 percent) at 42 percent of the price, so intelligence tier bought accuracy but not citation integrity.
On the record, both Claude tiers landed with the other engines: every one of the four boards is at or under 10 percent wrong among delivered answers, and every one is at zero fabricated URLs.
The record's effect is engine-independent and tier-independent, which is the strongest version of the procedural claim.

## Operational notes and disclosures

The engines are exactly as wrong on the record as alone; the metric counts wrong answers among answers actually delivered, and the caught claims arrive as labeled declines with fetch receipts.
Sonnet's on-the-record run paused at 45 of 50 finder calls when the Anthropic console balance ran out; the board held the remaining tasks and the same finder identity resumed them cleanly after a top-up, including the task it had been holding mid-failure.
The Anthropic Messages API's newest web-search tool variant stalled indefinitely on buffered requests in the live smoke; the adapter pins the basic variant.
Claude search results bill as input tokens, so per-case costs (Sonnet $0.096, Haiku $0.040) sit far above the token-only estimates recorded for OpenAI and Gemini; the full report must add every provider's search fees before cost columns are compared.
Claude-arm spend: $4.81 Sonnet Condition A, $4.73 Sonnet finder, $2.01 Haiku Condition A, $2.09 Haiku finder, $13.65 total plus roughly $0.25 of smokes and probes.
Exact models per provider response: claude-sonnet-5 and claude-haiku-4-5-20251001.
All artifacts live in `~/marcus-experiments/tow/pilot-2/` (condition files, usage logs, kanban databases, and fifty audit bundles per board under `bundles-claude/` and `bundles-claude-haiku/`); the registry now lists all four boards after the retro-registration.
