# Roadmap

Marcus is building the coordination layer for the agent era — starting with
orchestration, expanding to domain templates, and scaling to a marketplace.

This roadmap synthesizes our planning documents
([Playbook](docs/Playbook.md), [Development Guide](docs/DEVELOPMENT_GUIDE.md),
[Implementation Plans](docs/implementation/)) and research-driven features from
GitHub issues.

> **Status note (2026-05):** Current shipped release is **v0.3.8** (2026-05-22).
> One-agent-per-task lifecycle (#600) + decomposition redesign (#605, #607)
> shipped this milestone.
>
> The **12-month sprint plan** below (2026-05-23 → 2027-05-23) pulls
> **MFP/federation forward** from the "post-12 months" deferral in the
> earlier Playbook triage. The v0.5.0 flagship is now **MFP v0.1**
> (#476) with Board Rewind (#593) as a feature inside the protocol.
> Federation production (v0.7.0) is a Q3 commitment, not a post-Year-1
> aspiration. Marketplace + Brownfield sit in Sprint 4 (final 3 months).
>
> The arc: **Reliability → Open Protocol (MFP) → Federation → Commercial Surface.**

---

## Phase 1: Foundation — *In Progress*

**Goal:** Core coordination works reliably.

### v0.1.1 (2025-10-13) — Initial Release
Started as "PM Agent" in June 2025, rebranded to Marcus in October.
- [x] MCP protocol support (stdio + HTTP transport)
- [x] Planka kanban integration via kanban-mcp
- [x] Agent registration and task assignment
- [x] Workspace isolation (WorkspaceManager)
- [x] Hybrid intelligent coordination system (Phases 1-4)
- [x] NLP tools for natural language project creation
- [x] AI-powered feature analysis
- [x] Board quality validator

### v0.1.2 (2025-10-16) — Scheduling
- [x] CPM (Critical Path Method) scheduling algorithm
- [x] Unified dependency graphs with parallelism calculation
- [x] Optimal agent count recommendation
- [x] Cross-parent dependency wiring
- [x] Race condition prevention in task assignment
- [x] MLflow trace integration for experiments

### v0.1.3 — v0.1.3.1 (2025-10-18 — 2025-10-20) — Stability
- [x] Sweep-line algorithm for correct parallelism
- [x] Tmux-based multi-agent experiment support
- [x] Phase enforcer for cross-feature dependencies
- [x] Subtask assignment and project creation fixes

### v0.2.0 (2026-03-16) — Validation and Intelligence
115 commits. Major release after 5 months of development.
- [x] AI-powered task completeness validation with retry
- [x] Centralized configuration system with environment overrides
- [x] Composition-aware PRD extraction with specificity detection
- [x] Intelligent task pattern selection (prototype/standard/enterprise)
- [x] Constraint propagation through task generation
- [x] Bundled domain-based design tasks
- [x] Soft vs hard dependency support for parallel work
- [x] Post-project analysis: data persistence + LLM-powered analysis (Phase 1+2)
- [x] MLflow tracking for single-agent experiments
- [x] Smart retry with parallel work prioritization
- [x] Enterprise mode with aggressive task decomposition

### v0.2.1 (2026-03-21) — Reliability
- [x] Progressive timeout system with lease recovery
- [x] Structured RecoveryInfo for agent handoffs
- [x] Configurable LLM temperature
- [x] Validation loop bug fix (zero-issue auto-pass)

### v0.2.x (2026-03) — Shipped
- [x] README redesign — manifesto + accurate onboarding (#202, commit 72aea1c4)
- [x] Docker cleanup — infrastructure-only setup (#202)
- [x] Platform architecture + experiment flow diagrams (commit c1140466)
- [x] CLAUDE_API_KEY rename to preserve Claude Code subscription (commit 4e3014ce)
- [ ] Ollama (free LLM) setup verification — provider exists, docs pending
- [ ] OpenAI provider end-to-end testing — provider exists, full E2E pending

### v0.3.x (2026-04, current shipped — v0.3.6) — Mostly Done
- [x] Agent performance metrics and bottleneck detection
      (Epictetus coordination effectiveness analysis, commit 8d1f8b9e / #263)
- [x] Board health monitoring and alerting
      (Quality dashboard in Cato v0.3.0, commit 5f8e54d)
- [x] Auto-terminate agents when experiment completes (#389, commit 1b331117)
- [x] Validation hardening — eliminate criteria hallucinations + BLOCKED
      deadlocks (#421, commit 8e95ca28)
- [x] Coordination/correctness decoupling (commit 4ef01f3a)
- [ ] ~~Unified Cato dashboard bundled with Marcus~~ **DROPPED 2026-04** —
      Cato stays a sibling product. See `MVP_CATO_ALIGNMENT_EVALUATION.md`
      and the proposed Marcus Studio desktop shell (#443) for any future
      "single install" surface.
- [ ] ~~Single install: `pip install marcus && marcus start`~~ **DROPPED 2026-04** —
      depended on the Cato submodule decision above.
- [ ] ~~Real-time event broadcasting (SSE)~~ **DEFERRED 2026-04** —
      Cato continues reading SQLite + Planka directly. SSE rebuilt only
      when Marcus Studio (#443) needs it.
- [ ] ~~Jira kanban provider~~ **DEFERRED 2026-04** — revisit when a real user asks.
- [ ] ~~Linear kanban provider~~ **DEFERRED 2026-04** — same.
- [ ] ~~Trello kanban provider~~ **DEFERRED 2026-04** — same.

### v0.4.0-dev (on `develop` and `feat/parallel-experiment-platform`) — Parallel Experiment Platform

Major architectural addition not in earlier roadmaps. Five-part sprint
that turns Marcus into a parallel-experiment platform: multiple
independent Marcus instances run side-by-side, Epictetus audits run
reliably after every experiment, and a remote-monitoring channel
(Rufus) reports on long-running batches without a desk.

**Shipped:**
- [x] SQLite parallel experiment isolation (commit 0da35ed3, then v0.4.0
      tag in 3c68a97c — version reverted to 0.3.6 in 0e54bfa0 pending more work)
- [x] Env-var driven MCP URLs (per-instance `MARCUS_URL`)
- [x] Per-instance kanban DBs (`db_path` anchored to repo root)
- [x] Spawn-time pretrust race fix (`~/.claude.json` file lock)
- [x] Epictetus phase reporting (commit 420472d9)
- [x] **Track 1 — `asyncio.Lock` fix in `create_project`** (`src/marcus_mcp/tools/nlp.py`):
      serializes concurrent calls so simultaneous project creations no
      longer stall. Tests cover lock existence + serialization behavior.
      Code in; full end-to-end validation still pending.
- [x] **Epictetus reliability**:
      - timeout raised from 30 min → 2 hr
      - Phase 8.5 in the `/epictetus` skill now checks the `MARCUS_DB`
        env var before attempting `import marcus_mcp` —
        Posidonius-spawned audits previously skipped writing to
        `marcus.db` silently (the 5-agent quality score had been
        manually imported as a workaround)
- [x] **Batch pipeline tests** — 6 new tests in Posidonius confirming
      Epictetus fires after every run in a 3-run batch, the pipeline
      reaches `COMPLETED`, and teardown happens in the right order
- [x] **Posidonius Epictetus UI** — phase indicators render in the
      experiment card; backend `/api/experiments/{name}/events`
      endpoint added

**Pending:**
- [ ] v0.4.0 release once dashboard-v98 fixes settle
- [ ] Documentation guide for parallel multi-instance setup
- [ ] Track 1 end-to-end validation under real concurrent load

---

## 12-Month Sprint Plan — *2026-05-23 → 2027-05-23*

The active 12-month plan, broken into four sprints + a 2-month buffer.
Each sprint maps to specific milestones. The arc:
**Reliability → Open Protocol (MFP) → Federation → Commercial Surface.**

### Sprint 1 — Reliability *(2026-05-23 → 2026-07-23, 2 months)*
**Goal:** A 9-task project completes cleanly end-to-end. No integration
scrambles, no perf tax, no invariant leaks.

Pairs two milestones:
- **v0.3.9** (13 issues) — decomposer correctness (#604, #615, #617,
  #618, #619, #620), perf cache for `analyze_dependencies` (#626),
  `request_task_redo` MCP tool (#627), ephemeral worker worktree
  cleanup (#603), `_collect_task_artifacts` bug (#624),
  `recommended_agents` bug (#462), decomposer auto-select (#382),
  subtask spawning explosion (#628), integration partial-done bug
  (#629), composition test triage (#630).
- **v0.4.0 — Trustworthy Coordination** (15 issues) — observability
  primitives (#447, #196, #284), god-file splits (#422, #423, #424),
  `with_retry` dedup (#448), context system performance (#219, #222,
  #223), CPM autoscale (#465), the integration-verifier-must-fix
  invariant (#296), soft/hard dep support (#156), design/planning
  validation (#205), project deletion UI (#256).

### Sprint 2 — Open Protocol (MFP v0.1) *(2026-07-23 → 2026-09-23, 2 months)*
**Goal:** MFP v0.1 published as a versioned, language-agnostic
coordination protocol. Marcus is the *reference implementation*. Cato
consumes MFP read API instead of filesystem coupling. Conformance test
suite passes.

Milestone: **v0.5.0 — MFP v0.1 + Board Rewind** (8 issues).

Headline: **#476** — MFP v0.1 spec + JSON Schemas + OpenAPI + conformance
test suite. Defines the eight core artifact schemas (`AgentRegistration`,
`Task`, `TaskContext`, `TaskProgress`, `Decision`, `ArtifactMetadata`,
`BlockerReport`, `BoardEvent`) and the seven MCP tool RPC contracts.

Co-flagship: **#593** — Board Rewind. Naturally enabled by MFP's
immutable event log. Reconstruct board state at any prior task, edit
that task's spec, re-execute only the tail. A 27-task run with a bad
task 14 currently costs a full re-run; rewind makes it cost only tasks
14–27.

Also ships in v0.5.0:
- **#414** — SQLite migration (unblocker; the schema must support
  immutable branch lineage and the new `editing`/`pending_review`
  task states from the start, or rewind forces a re-migration)
- **#592** — `pending_review` task state (sibling gate machinery
  for rewind)
- **#299** — Domain-agnostic coordination (remove software-engineering
  assumptions from core protocol; required for MFP scope)
- **#594** — Richer agent capability profiles (MFP's
  `AgentCapability` schema)
- **#298** — Marcus Agent Protocol (MAP); rolled into MFP v0.1

### Sprint 3 — Federation v1 *(2026-09-23 → 2026-12-23, 3 months)*
**Goal:** Two Marcus instances discover each other, exchange
capabilities, delegate tasks across the wire, sync reputation,
auth/identity hardened. Private federation production-ready.

Pairs two milestones:
- **v0.6.0 — The Learning Coordinator** (16 issues) — per-experiment
  outcome→adaptation loop (#176), backprop blame attribution (#255),
  signal persistence for ML forecasting (#546), foundation-task
  decision-compliance metrics (#468, #469, #471), audit-log duration
  metrics (#228), foundations for federated reputation.
  Spec-generation improvements feed off the rewind+edit diffs from
  Sprint 2.
- **v0.7.0 — Federation** (5 issues) — instance discovery, cross-instance
  task delegation, reputation portability, payment routing skeleton,
  cross-instance Build Kit licensing primitives.

### Sprint 4 — Commercial Surface *(2026-12-23 → 2027-03-23, 3 months)*
**Goal:** Brownfield + Build Kits + Stripe + agent hiring marketplace.
Public agent economy launch.

Pairs two milestones:
- **v0.8.0 — Marketplace Foundation** — brownfield project ingestion
  (RAG over existing repos), Build Kit format + 10 seed kits, Stripe
  Connect for creators, Jira kanban provider (#241) for enterprise
  adoption.
- **v1.0.0 — The Agent Economy** — public marketplace launch, agent
  hiring + escrow + reputation + dispute resolution, multi-dimensional
  trust scoring.

### Buffer *(2027-03-23 → 2027-05-23, 2 months)*
Slip absorption, production hardening, polish. If unused, becomes
growth/marketing/PMF time before v1.0.0 GA.

---

## Why the Reordering vs the Earlier Playbook

The Playbook (2026-04 triage) deferred MFP, Federation, and Marketplace
past 12 months in favor of "research-first" milestones. This roadmap
flips that — **MFP earns its keep when federation lands on top, and
federation cannot land cleanly without MFP**. The longer Marcus runs
in production with implicit data shapes, the more expensive MFP
becomes. Ship the protocol first while the surface area is small.

**Marketplace stays in Sprint 4** because payment infrastructure
without paying users is a sunk cost. Build Kits + Stripe Connect ship
only after federation proves a network exists worth monetizing.

**Reliability stays Sprint 1** because nothing compounds if every
completion test still produces an integration scramble. Trust is the
foundation everything else stacks on.

---

## Open Architectural Debt — High Urgency

These surfaced from experiment post-mortems and Kaia architectural
reviews. No fixed order yet; no assigned milestone. Tracked here so
they don't get lost.

### Feature-based contract bleed
`get_task_context` does not label artifacts as `in_scope` vs
`reference_only` at retrieval time. Agents end up consuming the wrong
contracts. Two open design options:
- **Option B** — mode-aware framing (the call site declares its mode;
  the context builder filters by mode)
- **Option C** — explicit `artifact_role` field on artifacts at write
  time

### Foundation task descriptions missing consumption contracts
The design phase produces artifacts, but task descriptions don't tell
downstream agents *how* to consume them. Dashboard-v80 showed agents
correctly built the design system but dependents wired it incorrectly
because the consumption contract wasn't in the task body.

### Decomposer removed integration-wiring tasks
`_create_integration_subtask` was removed citing overhead. The
dashboard-v99 audit proved this causes **hollow products** — each
component passes its unit tests, but the composed result is broken
(no service wiring, no data flow). Restore some form of explicit
integration-wiring step.

---

## Phase 2: Domain Expansion + Build Kits (Months 3-4)

**Goal:** Prove coordination works beyond code. Launch Build Kits.

### Build Kits System
Build Kits (`.mkb` packages) capture completed projects as reusable templates
— source code, architectural decisions, build history, and customization points.

- [ ] `.mkb` file format specification
- [ ] BuildKitPackager — create packages from completed projects
- [ ] BuildKit metadata extractor (auto-detect tech stack)
- [ ] BuildKitCustomizer — intelligent modifications via agents
- [ ] Context injection (Build Kit context -> agent context)
- [ ] CLI: `marcus buildkit create`, `marcus buildkit customize`
- [ ] Build Kit browser in Cato dashboard

### Seed Build Kits
| Kit | Price | Stack |
|-----|-------|-------|
| SaaS Starter | Free | Next.js + Supabase + Stripe |
| E-commerce | Free | React + Node + PostgreSQL |
| Blog/CMS | Free | Markdown-based |
| Todo App | Free | Simple CRUD |
| Dashboard Template | Free | Admin panel |
| API Service | Free | FastAPI REST |
| Real-time Chat | Paid | WebSocket |
| Payment Integration | Paid | Stripe edge cases |
| Multi-tenant B2B | Paid | Tenant isolation |
| Auth System | Paid | OAuth, SSO, MFA |

### Domain Expansion
- [ ] Content creation workflows (podcasts, articles)
- [ ] Research workflows (literature review, data analysis)
- [ ] Marketing workflows (campaigns, copy)

---

## Phase 3: Brownfield + Marketplace Validation (Months 5-6)

**Goal:** Agents work on existing projects. Validate marketplace economics.

### Brownfield Support
- [ ] Project ingestion from existing Git repos
- [ ] RAG-based context retrieval over codebases
- [ ] Safe modification workflows with rollback

### Agent Marketplace
- [ ] Agent registry with capability declarations
- [ ] Agent profile pages with portfolio and reputation
- [ ] Search/filter agents by specialization and rate
- [ ] Hire agent workflow with escrow
- [ ] Star rating and review system

### Payment Infrastructure
- [ ] Stripe Connect integration for creators
- [ ] Checkout flow for Build Kits (one-time, subscription)
- [ ] Escrow system for agent work
- [ ] Platform fee engine (15-20%)
- [ ] Payout processing

---

## Phase 4: Federation Protocol (Months 7-8)

**Goal:** Marcus instances discover each other and share agent networks.

- [ ] Marcus Federation Protocol (MFP) v1.0 specification
- [ ] Instance discovery on network
- [ ] Cross-instance task delegation
- [ ] Reputation portability across instances
- [ ] Payment routing between federated nodes
- [ ] State synchronization protocol

---

## Phase 5: Full Marketplace + Scale (Months 9-12)

**Goal:** Production marketplace with agent labor economy.

- [ ] Full marketplace with agent hiring at scale
- [ ] Enterprise features: RBAC, SSO/SAML, audit logging
- [ ] Multi-project coordination
- [ ] SLA and compliance reporting
- [ ] Enterprise pilot program (10+ organizations)
- [ ] **Plugin architecture with SDK** — *2026-04: rescoped to land much
      earlier, gated on Marcus Studio approval (#443).* Plugin v1
      (manifest-only) in Studio M0–M1; typed SDK in M3+. The Phase 5
      framing here remains as the "GA / public marketplace" milestone
      rather than the introduction of plugins.
- [ ] Horizontal scaling documentation and patterns

---

## Research-Driven Features

These features come from academic research documented in GitHub issues:

| Feature | Issue | Status |
|---------|-------|--------|
| Close the Learning Loop — observation to adaptation | #176 | In progress (Epictetus + research event infra landed) |
| Automated task ordering evaluation framework | #37 | Largely shipped (validation study + #337 hallucination work) |
| Coordination tax experiments (NeurIPS 2026) | n/a | Infrastructure shipped (commit 2801ea6d) |

---

## The Vision

**Open Core Model:**
- Marcus orchestration = **free forever** (MIT)
- Build Kits = mostly free (community-generated)
- Revenue from: cloud hosting, marketplace transactions, enterprise contracts

**Long-term aspirational targets:**
- 1,000+ active users
- 500+ Build Kits
- 100+ specialized agents in marketplace
- 10+ enterprise pilots
- Foundation for the agent labor economy

> *Note (2026-04): These targets remain the long-term commercial vision.
> The internal `docs/Playbook.md` has been rescoped to research-first
> milestones (NeurIPS 2026 coordination-tax submission, validated
> experiments, contributor growth) for the current phase. The
> commercial vision above reactivates when a deliberate commercial
> push begins.*

---

## How to Contribute to the Roadmap

- Join the [Discord](https://discord.com/channels/1409498120739487859/1409498121456848907) to discuss priorities
- Open an [issue](https://github.com/lwgray/marcus/issues) with the
  `enhancement` label
- See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines
- Check [docs/Playbook.md](docs/Playbook.md) for detailed technical specs
  on Build Kits, Marketplace, and Federation
