# Marcus Telemetry

Marcus collects anonymous usage data to help improve the tool. This page
documents exactly what we collect, what we never collect, where it goes, and
how to disable it.

**Telemetry is opt-in by default.** You can disable it any time with one
command (see [Disabling telemetry](#disabling-telemetry) below). No data is
ever sent until at least one run completes after Marcus is installed.

If you have any privacy concerns this document does not address, please open
an issue at <https://github.com/lwgray/marcus/issues> or email the maintainer
at <privacy@marcus-ai.dev>.

---

## Why we collect telemetry

Marcus is a multi-agent coordination system. We collect anonymous usage data
to answer questions like:

- Which decomposer strategy (`feature_based` vs `contract_first`) produces
  more successful runs?
- What domain (web apps, data pipelines, CLI tools, ML projects) does Marcus
  get used for most?
- Which features are actually used, and which can be deprecated?
- Where do runs stall or fail, so we know what to fix first?
- How does cost-per-project change release-over-release?

Without telemetry, we are guessing. With it, we can improve Marcus based on
how people actually use it, not how we imagined they would.

---

## What we collect

Every event below is sent only if you have telemetry enabled. Every event is
tagged with an **anonymous UUID** generated once on first run and stored at
`~/.marcus/telemetry_id`. This UUID is not linked to any account, email,
hostname, IP address, or other identifier. It exists only so we can answer
"is this the same user as before?" without knowing *who* the user is.

### Session events

**`session_started`** — fired when Marcus starts up.

```json
{
  "marcus_version": "0.3.7",
  "python_version": "3.12",
  "os": "darwin",
  "kanban_provider": "planka",
  "ai_provider": "anthropic",
  "planner_model": "claude-opus-4-7",
  "agent_model": "claude-sonnet-4-6",
  "is_local_llm": false,
  "runner": "mcp_direct"
}
```

`runner` is one of: `mcp_direct` (Claude Desktop / Cursor / direct MCP
connection), `meta_runner` (the `/marcus` skill), `posidonius` (Posidonius
batch runner).

### Project events

**`project_created`** — fired when a new project is decomposed.

```json
{
  "task_count": 14,
  "complexity_mode": "standard",
  "decomposer_strategy": "contract_first",
  "was_fallback": false,
  "structural_category": "web app",
  "domain": "fintech"
}
```

`decomposer_strategy` is the strategy that **actually ran**.
`was_fallback` is `true` when `contract_first` was requested but the run
fell back to `feature_based` — it lets us measure how often contract-first
decomposition fails. Both are fixed labels; no project text is involved.

`structural_category` is one of: `web app`, `data pipeline`, `CLI tool`,
`game`, `API service`, `ML/AI`, `library`, `automation`, `other`.

`domain` is one of: `fintech`, `healthtech`, `edtech`, `ecommerce`, `social`,
`productivity`, `devtools`, `gaming`, `media`, `iot`, `data_analytics`,
`ml_ai`, `enterprise`, `consumer`, `other`.

Both labels are produced by the planner LLM call you are already paying for
when it decomposes your project. We do **not** make a separate LLM call to
produce these labels, and we do **not** ship your project description text.
Only the bucket labels above leave your machine.

**`project_cost_summary`** — fired when a run finishes, summarizing
aggregate token cost.

```json
{
  "input_tokens": 142000,
  "output_tokens": 18500,
  "cache_read_tokens": 89000,
  "cache_creation_tokens": 4200,
  "cost_usd_cents": 47,
  "cost_per_task_cents": 3.4
}
```

### Agent events

**`agent_registered`** — fired when an agent joins a run.

```json
{
  "role": "Backend Developer",
  "skills": ["python", "fastapi", "postgres"],
  "agent_model": "claude-sonnet-4-6"
}
```

`role` and `skills` are values you provide when you register an agent. If you
do not want these to leave your machine, use generic role names like "Agent A"
and an empty skills list — Marcus does not require them to be specific.

### Task events

**`task_completed`** — fired when a task moves to DONE.

```json
{ "duration_minutes": 12, "had_blocker": false, "task_phase": "backend" }
```

`duration_minutes` and `had_blocker` may be `null` in v0.3.7 — the
underlying signals (per-task lifecycle timing and the blocker→task join)
land with [#547](https://github.com/lwgray/marcus/issues/547) and
[#551](https://github.com/lwgray/marcus/issues/551).  Until then,
`task_phase` is the always-populated field; the others are best-effort
and may be absent.

`task_phase` is one of a fixed set: `backend`, `frontend`, `design`,
`integration`, `testing`, `deployment`, `documentation`, `foundation`,
or `unknown` when a task's labels match none of these. The task's
free-text labels are **not** shipped — only the bucket label.

**`task_blocked`** — fired when an agent reports a blocker.

```json
{ "blocker_type": "dependency_not_ready" }
```

The blocker **type** is shipped. The blocker **message** is never shipped.
`blocker_type` is one of a fixed set: `dependency_not_ready`, `timeout`,
`missing_credential`, `tool_error`, `ambiguous_requirement`,
`async_failure`, or `unknown` when the blocker text matches no known
keyword. The blocker text is classified locally by keyword match — no
LLM call, and the text never leaves your machine.

**`lease_expired`** — fired when an agent's task lease expires before they
finish.

```json
{ "task_held_minutes": 45, "progress_pct_at_expiry": 60, "recovery_attempted": true }
```

`recovery_attempted` reports only that Marcus put the task back on the
board for another agent to claim.  Whether the next agent actually
completed it is observed separately via `task_completed` — the lease-
expiry code path cannot see downstream completion.

**`validator_retry`** — fired when a planning validator retries a failed
check.

```json
{ "retry_count": 2, "final_result": "pass", "validation_type": "task_completeness" }
```

### Quality and error events

**`planning_intent_fidelity`** — fired after planning, measures how well
the planner covered your stated intent.

```json
{
  "decomposer": "contract_first",
  "intent_fidelity_score": 0.87,
  "coverage_before_fill": 0.71,
  "coverage_after_fill": 0.94,
  "gap_filled_outcomes": 3
}
```

**`epictetus_result`** — fired only when post-run code grading runs. The
recommendations are sanitized: file paths, absolute paths, code snippets,
and identifying text are stripped before the event leaves your machine.

```json
{
  "grade": "B",
  "recommendations": ["improve test coverage", "add error handling to api layer"]
}
```

**`error_occurred`** — fired when an error reaches the error monitoring
system.

```json
{ "error_type": "KanbanIntegrationError" }
```

The error **type** is shipped. The error **message** and **stack trace** are
never shipped.

---

## What we never collect

Under no circumstances does Marcus ship any of the following:

- **Source code** — never. Not in events, not in attachments, not in
  comments, not in anything.
- **File contents** of any kind — README, configs, artifacts, none of it.
- **API keys, secrets, credentials, tokens** — even by accident, even
  truncated, even hashed.
- **Project names** — the human-readable name you give your project stays
  on your machine. Only the anonymous internal UUID and the bucket-label
  `domain` / `structural_category` ship.
- **Task descriptions** — the free-text task descriptions the planner
  generates never leave your machine.
- **PRD (Product Requirements Document) text** — your project description
  is the most sensitive input to Marcus. It never ships in any form. Only
  derived bucket labels and length statistics ship.
- **IP addresses** — PostHog is instructed not to capture them.
- **Email addresses** — never collected.
- **Hostnames, machine identifiers, MAC addresses** — never collected.
- **Cursor history, MCP transport metadata, conversation history** — never
  collected.
- **Personally identifying information** — by policy, by code, by review.

---

## Where the data goes

- **Service:** [PostHog Cloud](https://posthog.com)
- **Region:** US (us.i.posthog.com)
- **Retention:** Per PostHog's default — 7 years for raw events, summarized
  in dashboards we control.
- **Access:** Only the Marcus maintainer has access to the PostHog project.

We may move to a self-hosted PostHog instance on
[Fly.io](https://fly.io) if costs warrant. Self-hosting changes nothing for
you — the data shipped and the privacy contract are identical.

**If we ever change telemetry vendors** — for example, switch from PostHog to
another analytics provider — we will announce the change in a release note
**before any data ships to the new vendor**, and the first run after the
change will print a one-line notice pointing here. Your opt-out state is
preserved across vendor changes: if you opted out under PostHog, you stay
opted out under any future vendor.

---

## How sending works (technical details)

- Events are sent **asynchronously and fire-and-forget**: if PostHog is
  unreachable, the call is a no-op and never blocks Marcus.
- Events are sent over **HTTPS** to `https://us.i.posthog.com/capture/`.
- No event sending blocks any user-facing operation. If you kill Marcus
  mid-event-send, at most the in-flight event is lost — no corruption.
- The anonymous UUID at `~/.marcus/telemetry_id` is generated once with
  `uuid.uuid4()` and is never re-generated unless you delete the file.
  Deleting the file effectively gives you a fresh anonymous identity.

---

## Disabling telemetry

Telemetry can be disabled three ways, in order of permanence.

### One-time, this session only

Set the environment variable when running Marcus:

```bash
MARCUS_TELEMETRY=off marcus
```

### Permanent, this machine

```bash
marcus telemetry disable
```

This writes `telemetry.enabled: false` to `~/.marcus/config.yaml`. Future
runs respect this until you run `marcus telemetry enable`.

### Permanent and remove identifying ID

```bash
marcus telemetry purge
```

This (a) disables telemetry, (b) deletes `~/.marcus/telemetry_id`, and
(c) deletes any local telemetry log file. Future runs will not send
telemetry and will not have a continuing anonymous identity if you re-enable.

### Check status

```bash
marcus telemetry status
```

Shows whether telemetry is enabled, where the anonymous UUID lives, and
where the local outbound-event log lives (so you can inspect exactly what
was sent).

---

## Inspecting what was sent

Marcus keeps a local copy of every event it sends at
`~/.marcus/telemetry_outbound.jsonl`. You can inspect it any time:

```bash
cat ~/.marcus/telemetry_outbound.jsonl | jq .
```

This file is for your inspection only. Marcus does not read it back, does
not upload it, and does not aggregate from it.

You can delete it at any time without affecting Marcus.

---

## Your data rights

- **Export:** request a copy of every event your anonymous UUID has produced
  by emailing <privacy@marcus-ai.dev> with your `telemetry_id` value.
- **Delete:** request deletion of every event your anonymous UUID has
  produced by emailing the same address.
- **Audit:** read `~/.marcus/telemetry_outbound.jsonl` at any time to see
  exactly what was sent.

---

## When this document changes

If we ever add a new event type, a new field, or change what we collect, we
will:

1. Update this document.
2. Note the change in `CHANGELOG.md`.
3. Print a one-line notice on first run after upgrade, like the original
   first-run notice but for the changed scope.
4. Re-prompt nothing — your existing opt-in / opt-out state is preserved.

This document is the contract. If the code and this document disagree, the
code is wrong.

---

## Related

- The first-run notice that points to this document.
- `marcus telemetry --help` for CLI details.
- GitHub issue [#416](https://github.com/lwgray/marcus/issues/416) — the
  implementation tracking issue.
- GitHub issue [#544](https://github.com/lwgray/marcus/issues/544) — the
  ML forecasting layer this data eventually feeds.
