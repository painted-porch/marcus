# Marcus: Technical Plan & Execution Playbook

**Version**: 1.0
**Date**: November 2025
**Status**: MASTER VISION DOCUMENT (active focus has shifted — see 2026-04 note)

> ## ⚠ 2026-04 Status Note
>
> This Playbook describes the original 12-month commercial vision (Build
> Kits, Brownfield, Marketplace, Federation, $5M GMV / $100M ARR Year 2).
> The current shipped reality is **v0.3.6** with the v0.4.0 parallel
> experiments work on `develop`.
>
> **The active workstream has pivoted toward research validation.** Effort
> in 2026-Q1/Q2 has gone into:
> - Coordination tax experiments (24-PRD test suite, NeurIPS 2026
>   submission infrastructure — commit 2801ea6d)
> - Parallel multi-instance experiment isolation (commit 0da35ed3)
> - Epictetus coordination-effectiveness audit (#263)
> - Validation hardening (#421, #337)
> - Cato Quality dashboard integration
>
> Months 3–4 (Build Kits / `.mkb` packaging) and Months 5–6 (Brownfield /
> RAG over existing repos) of this Playbook are **not started** and
> should be treated as post-v1.0 features. The 12-month vision below is
> still the long-term direction, but the calendar is no longer accurate.
> Refer to `ROADMAP.md` for the current shipped state and `DEVELOPMENT_GUIDE.md`
> for active milestones.
>
> ## 2026-04 Triage Decisions (Strategic Frame)
>
> Walking the Playbook section by section, the following high-level
> decisions have been recorded:
>
> | Item | Original commitment | Decision | Notes |
> |---|---|---|---|
> | **Commercial outcome targets** ("By Month 12: 1,000+ users · 500+ Build Kits · 100+ agents · $5M GMV · 10+ enterprise pilots · $100M ARR Year 2") | Hard targets in Executive Summary | **RESCOPE** | Swap to research-first targets: NeurIPS 2026 submission with coordination-tax data, validated multi-agent experiments published, growing community of researchers and contributors. The commercial KPIs become a future optionality, not a near-term commitment. |
> | **12-month month-by-month timeline** (Month 1 = November 2025; calendar drives MVP / Build Kits / Brownfield / Federation / Marketplace / Enterprise) | Hard monthly plan | **DEFER** | Strip absolute month numbers. Phases stay (MVP → Build Kits → Brownfield → Federation → Marketplace → Enterprise) but as relative ordering, not a fixed calendar. The current real Month 1 is whatever month a phase actually starts. |
> | **Open Core licensing** (free orchestration; paid Build Kits + cloud hosting + marketplace transactions 15–20% + enterprise contracts) | Strategic monetization frame | **KEEP** | Strategy stays. **Correction:** Playbook's "Apache 2.0" reference is wrong — the project is MIT-licensed (verified in `LICENSE` and `pyproject.toml`). Treat any future references to Apache 2.0 in this document as MIT until separately decided. |
>
> ## 2026-04 Triage Decisions (Product Features)
>
> | Item | Original commitment | Decision | Notes |
> |---|---|---|---|
> | **Build Kits system** (.mkb format, packager, customizer, validator, CLI commands, Cato browser, 10 seed kits) | Months 3–4 | **DEFER** | Spec stays as long-term direction. Strip the Months 3–4 framing — Build Kits start whenever a deliberate product push begins, not on a fixed calendar. The detailed implementation sections (Packager / Validator / Customizer code samples below in this doc) should be read as a future-work appendix, not a near-term plan. |
> | **Marketplace infrastructure** (agent registry, Stripe Connect, escrow, disputes, reputation, discovery, payment routing, reviews, multi-instance hire flow) | Months 5–10 | **KEEP** | Spec preserved as-is for the eventual build. Strategy and shape unchanged. Remains gated on Build Kits proving a creator economy is viable; no near-term commitment to start. |
> | **Federation Protocol (MFP)** (HTTPS + signed JWT, instance discovery, cross-instance task delegation, reputation portability, payment routing, state sync) | Months 7–8 | **DEFER** | Spec stays as-is. Strip the Months 7–8 framing. Active commitment from Studio #443 §13 stands: dormant identity infrastructure (Ed25519 keypair + JWT issuer) lands when Marcus Studio M0 ships, full wire-protocol activation deferred. |
>
> ## 2026-04 Triage Decisions (Business Operations)
>
> | Item | Original commitment | Decision | Notes |
> |---|---|---|---|
> | **Team & hiring plan** (3–4 people Months 1–2 → 5–6 Months 3–6 → 7–9 Months 7–10 → 10–12 Months 11–12) | Calendar-driven hiring schedule | **DEFER** | Roles stay as the staffing model "if/when funded." Strip the calendar. Today this is a self-funded project with PyCon Sprint contributors; the role list is what to hire **when** a fundraising decision is made, not a schedule. |
> | **Funding strategy** (bootstrap $200K → seed $2M–$3M → Series A $10M+; total budget $2.1M–$2.6M for 12 months) | Calendar-tied capital plan | **KEEP** | Path stays as the option if-pursued. Today the project is self-funded and research-first; fundraising is a deliberate future decision, not a near-term commitment. |
> | **Growth Playbook** (acquisition channels, content, community, conferences, partnerships, paid) | ~170 lines of go-to-market tactics | **KEEP** | Stays as written for the eventual commercial phase. Today the active growth surface is community + GitHub + PyCon Sprints + research-community engagement; the broader Growth Playbook activates when there's a paid product to grow. |
> | **Success Metrics & KPIs** (DAU, paid Build Kits sold, GMV, conversion rates, NPS) | Commercial KPI dashboard | **KEEP** | Commercial KPIs stay for the eventual phase. Today the actually-tracked metrics are research-shaped (project success rate %, coordination tax across N agents, experiments published, contributors). The Playbook KPI table activates when the marketplace surface is live. |
> | **Risk Management** (catalog of strategic, technical, market risks) | Risk catalog | **KEEP** | Catalog stays as written. Note for current phase: the live risks are research-shaped (model regression, experiment reproducibility, NeurIPS deadline) plus solo-maintainer-shaped (bus factor, scope creep, time). The commercial-phase risks in the catalog reactivate when the commercial surface is real. |
>
> ## Triage summary
>
> Of the 11 strategic items walked individually:
>
> - **Kept (5):** Open Core licensing, Marketplace infrastructure spec, Funding strategy, Growth Playbook, Success Metrics, Risk Management
> - **Deferred (5):** 12-month timeline framing, Build Kits Months 3–4, Federation Months 7–8, Team & hiring schedule
> - **Rescoped (1):** Commercial outcome targets → research-first targets
> - **Dropped (0):** None
>
> The Playbook's strategy and structure stay intact. The cuts are
> primarily about **stripping dates and near-term commitments** to match
> the current research-first phase. When a commercial phase activates,
> most of this Playbook reactivates with it.

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Technical Architecture](#technical-architecture)
3. [12-Month Development Roadmap](#12-month-development-roadmap)
4. [Build Kits Technical Specification](#build-kits-technical-specification)
5. [Marketplace Infrastructure](#marketplace-infrastructure)
6. [Open Source Strategy](#open-source-strategy)
7. [Growth Playbook](#growth-playbook)
8. [Team & Resources](#team--resources)
9. [Success Metrics & KPIs](#success-metrics--kpis)
10. [Risk Management](#risk-management)

---

## Executive Summary

### The Mission
Build open source infrastructure for the global agent labor economy - starting with coordination, scaling to marketplace, becoming the protocol.

### The Strategy
**Open Core Model:**
- Marcus orchestration = free forever (Apache 2.0)
- Build Kits = mostly free (community-generated)
- Revenue from: Cloud hosting, marketplace transactions, enterprise contracts

### The Timeline
**12 months to validated infrastructure:**
- Months 1-2: MVP foundation (coordination works)
- Months 3-4: Domain expansion + Build Kits launch
- Months 5-6: Brownfield support + marketplace validation
- Months 7-8: Federation protocol
- Months 9-10: Full marketplace with agent labor
- Months 11-12: Scale, polish, enterprise pilots

### The Outcome
By Month 12:
- 1,000+ active users
- 500+ Build Kits (400 free, 100 paid)
- 100+ specialized agents in marketplace
- $5M GMV across Build Kits + agent hiring
- 10+ enterprise pilots
- Foundation for $100M+ ARR in Year 2

---

## Technical Architecture

### System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    Marcus Infrastructure                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Marcus Core (Open Source)                   │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │         Orchestration Engine                      │   │   │
│  │  │  • Task decomposition                             │   │   │
│  │  │  • Dependency management                          │   │   │
│  │  │  • Agent assignment                               │   │   │
│  │  │  • Event system                                   │   │   │
│  │  └──────────────────────────────────────────────────┘   │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │         Workspace Manager                         │   │   │
│  │  │  • Git worktree isolation                         │   │   │
│  │  │  • Branch management                              │   │   │
│  │  │  • Conflict prevention                            │   │   │
│  │  └──────────────────────────────────────────────────┘   │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │         Context Builder                           │   │   │
│  │  │  • Feature context aggregation                    │   │   │
│  │  │  • Artifact tracking                              │   │   │
│  │  │  • Decision logging                               │   │   │
│  │  └──────────────────────────────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                            ↕                                     │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Build Kits System                          │   │
│  │  • Package format (.mkb - Marcus Build Kit)             │   │
│  │  • Metadata (stack, dependencies, architecture)         │   │
│  │  • Replay system (reconstruct with context)             │   │
│  │  • Customization engine (intelligent modifications)     │   │
│  └─────────────────────────────────────────────────────────┘   │
│                            ↕                                     │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Marketplace Infrastructure                 │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │    Agent Registry                                 │   │   │
│  │  │    • Capability declarations                      │   │   │
│  │  │    • Reputation scores                            │   │   │
│  │  │    • Availability & pricing                       │   │   │
│  │  └──────────────────────────────────────────────────┘   │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │    Transaction Engine                             │   │   │
│  │  │    • Escrow system                                │   │   │
│  │  │    • Payment processing                           │   │   │
│  │  │    • Revenue sharing                              │   │   │
│  │  └──────────────────────────────────────────────────┘   │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │    Discovery & Matching                           │   │   │
│  │  │    • Search/filter agents                         │   │   │
│  │  │    • Recommendation engine                        │   │   │
│  │  │    • Availability checking                        │   │   │
│  │  └──────────────────────────────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                            ↕                                     │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Federation Layer                            │   │
│  │  • Marcus instance discovery                             │   │
│  │  • Cross-instance task delegation                        │   │
│  │  • Reputation portability                                │   │
│  │  • Payment routing                                       │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

### Technology Stack

#### Backend
```yaml
Core Engine:
  Language: Python 3.11+
  Framework: FastAPI
  Database: PostgreSQL 15+ (relational), Redis (caching)
  Task Queue: Celery + Redis
  Real-time: WebSockets (FastAPI WebSocket)

MCP Server:
  Protocol: Model Context Protocol
  Port: 4298
  Transport: stdio, HTTP

API Layer:
  REST: FastAPI routers
  GraphQL: Strawberry (optional, for complex queries)
  WebSocket: Real-time events
```

#### Frontend
```yaml
Dashboard (Cato):
  Framework: React 18+ with TypeScript
  UI Library: Material-UI (MUI)
  State Management: Zustand
  Data Fetching: TanStack Query (React Query)
  Visualization: D3.js, Recharts
  Real-time: EventSource (SSE)

CLI:
  Framework: Click (Python)
  Rich terminal output: Rich library
  Configuration: TOML
```

#### Infrastructure
```yaml
Development:
  Package Management: pip, conda
  Version Control: Git + Git worktrees
  Testing: pytest, pytest-cov
  Linting: black, isort, mypy, flake8

Production:
  Hosting: AWS / GCP / Azure
  Containers: Docker, Docker Compose
  Orchestration: Kubernetes (Year 2+)
  CI/CD: GitHub Actions
  Monitoring: Prometheus, Grafana
  Logging: ELK Stack (Elasticsearch, Logstash, Kibana)

Marketplace:
  Payment: Stripe Connect
  Search: Elasticsearch
  File Storage: S3-compatible (Backblaze B2, AWS S3)
  CDN: CloudFlare
```

### Data Models

#### Core Entities

```python
# src/core/models.py

@dataclass
class Project:
    project_id: str
    name: str
    description: str
    repo_path: str
    status: ProjectStatus
    created_at: datetime
    features: List[str]  # feature_ids
    metadata: Dict[str, Any]

@dataclass
class Feature:
    feature_id: str
    feature_name: str
    project_id: str
    design_task_id: Optional[str]
    feature_branch: str
    status: FeatureStatus
    created_at: datetime
    task_ids: List[str]
    context: FeatureContext

@dataclass
class Task:
    task_id: str
    feature_id: str
    title: str
    description: str
    status: TaskStatus
    dependencies: List[str]  # task_ids
    assigned_agent_id: Optional[str]
    workspace_path: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]

@dataclass
class Agent:
    agent_id: str
    name: str
    capabilities: List[str]
    status: AgentStatus
    registered_at: datetime
    reputation: AgentReputation
    pricing: AgentPricing  # NEW for marketplace
```

#### Build Kit Entities

```python
# src/buildkits/models.py

@dataclass
class BuildKit:
    buildkit_id: str
    name: str
    description: str
    creator_id: str
    version: str
    tech_stack: TechStack
    architecture: ArchitecturalDecisions
    pricing: BuildKitPricing  # free, one-time, subscription
    stats: BuildKitStats
    created_at: datetime
    updated_at: datetime

@dataclass
class TechStack:
    languages: List[str]
    frameworks: List[str]
    databases: List[str]
    services: List[str]

@dataclass
class ArchitecturalDecisions:
    decisions: List[Decision]
    trade_offs: Dict[str, str]
    customization_points: List[str]

@dataclass
class BuildKitPackage:
    """The .mkb file format"""
    metadata: BuildKit
    source_code: Dict[str, bytes]  # file_path -> content
    build_state: BuildState  # Marcus build history
    replay_instructions: List[ReplayStep]
    dependencies: List[str]  # other buildkit_ids
```

#### Marketplace Entities

```python
# src/marketplace/models.py

@dataclass
class AgentListing:
    agent_id: str
    specializations: List[str]
    hourly_rate: Optional[Decimal]
    project_rate: Optional[Decimal]
    availability: AgentAvailability
    portfolio: List[ProjectReference]
    certifications: List[Certification]

@dataclass
class AgentReputation:
    overall_rating: float  # 0-5
    total_projects: int
    success_rate: float  # 0-1
    domain_ratings: Dict[str, float]  # domain -> rating
    reviews: List[Review]

@dataclass
class Transaction:
    transaction_id: str
    buyer_id: str
    seller_id: str  # agent_id or buildkit_creator_id
    item_type: str  # "agent_work", "buildkit"
    item_id: str
    amount: Decimal
    platform_fee: Decimal
    status: TransactionStatus
    escrow_released_at: Optional[datetime]
```

### API Design

#### REST API Endpoints

```yaml
# Core Marcus API (existing)
POST   /api/agents                    # Register agent
GET    /api/agents                    # List agents
POST   /api/projects                  # Create project
GET    /api/projects/{id}             # Get project
POST   /api/tasks                     # Create task
GET    /api/tasks/{id}                # Get task
PUT    /api/tasks/{id}/status         # Update task status

# Build Kits API (NEW)
POST   /api/buildkits                 # Publish Build Kit
GET    /api/buildkits                 # Browse Build Kits (search, filter)
GET    /api/buildkits/{id}            # Get Build Kit details
GET    /api/buildkits/{id}/download   # Download Build Kit
POST   /api/buildkits/{id}/customize  # Start customization project
POST   /api/buildkits/{id}/review     # Submit review
GET    /api/buildkits/stats           # Global Build Kit stats

# Marketplace API (NEW)
GET    /api/marketplace/agents        # Browse agents
GET    /api/marketplace/agents/{id}   # Agent profile
POST   /api/marketplace/hire          # Hire agent
GET    /api/marketplace/transactions  # Transaction history
POST   /api/marketplace/reviews       # Submit review
GET    /api/marketplace/search        # Search agents/buildkits

# Payment API (NEW)
POST   /api/payments/connect          # Connect Stripe account
POST   /api/payments/checkout         # Create checkout session
POST   /api/payments/escrow           # Handle escrow
POST   /api/payments/payout           # Process payout
```

#### MCP Tools (existing + new)

```python
# Existing MCP Tools
register_agent()
request_next_task()
submit_artifact()
log_decision()
complete_task()
get_feature_context()

# New MCP Tools for Build Kits
publish_buildkit()        # Agents can publish Build Kits
search_buildkits()        # Agents can search Build Kits
customize_buildkit()      # Agents can customize Build Kits

# New MCP Tools for Marketplace
list_available_agents()   # Find agents for hire
request_agent_hire()      # Request to hire specialist
accept_subcontract()      # Agent accepts subcontract work
```

### File System Structure

```
marcus/
├── src/
│   ├── core/
│   │   ├── models.py              # Core data models
│   │   ├── orchestrator.py        # Main orchestration engine
│   │   └── events.py              # Event system
│   ├── workspace/
│   │   ├── manager.py             # Workspace isolation
│   │   └── git_operations.py     # Git worktree operations
│   ├── context/
│   │   ├── feature_builder.py    # Context aggregation
│   │   └── commit_tracker.py     # Git commit tracking
│   ├── buildkits/                 # NEW
│   │   ├── models.py              # Build Kit data models
│   │   ├── packager.py            # Create .mkb files
│   │   ├── customizer.py          # Intelligent customization
│   │   ├── marketplace.py         # Build Kit marketplace logic
│   │   └── replay.py              # Reconstruct builds
│   ├── marketplace/               # NEW
│   │   ├── models.py              # Marketplace data models
│   │   ├── agent_registry.py     # Agent listings
│   │   ├── discovery.py           # Search/matching
│   │   ├── transactions.py        # Escrow & payments
│   │   ├── reputation.py          # Reputation system
│   │   └── disputes.py            # Dispute resolution
│   ├── federation/                # NEW (Month 7-8)
│   │   ├── protocol.py            # Federation protocol
│   │   ├── discovery.py           # Instance discovery
│   │   └── sync.py                # State synchronization
│   ├── api/
│   │   ├── marcus_routes.py       # Core API routes
│   │   ├── buildkit_routes.py     # NEW: Build Kit routes
│   │   ├── marketplace_routes.py  # NEW: Marketplace routes
│   │   └── payment_routes.py      # NEW: Payment routes
│   ├── cli/
│   │   └── commands/              # CLI commands
│   └── config/
│       └── marcus_config.py       # Configuration system
├── dashboard/                     # Cato (git submodule)
│   ├── backend/
│   └── frontend/
│       └── src/
│           ├── components/
│           │   ├── BuildKits/     # NEW: Build Kit browser
│           │   └── Marketplace/   # NEW: Agent marketplace
│           └── layouts/
│               └── UnifiedDashboard.tsx
├── tests/
│   ├── unit/
│   │   ├── buildkits/             # NEW
│   │   └── marketplace/           # NEW
│   └── integration/
├── docs/
│   ├── api/
│   │   ├── buildkits.md           # NEW
│   │   └── marketplace.md         # NEW
│   └── guides/
│       ├── publishing-buildkits.md # NEW
│       └── hiring-agents.md        # NEW
└── examples/
    ├── buildkits/                  # NEW: Example Build Kits
    │   ├── saas-starter/
    │   ├── ecommerce-template/
    │   └── blog-cms/
    └── agent-integrations/         # NEW: Example agent configs
```

---

## 12-Month Development Roadmap

### Month 1-2: MVP Foundation (Updated 8-Week Plan)
**Goal:** Validate core coordination works + deliver unified dashboard

**Timeline**: Weeks 1-8 (see DEVELOPMENT_GUIDE.md for updated schedule)

**Deliverables:**
- ✅ Configuration system (Week 1 - polish only)
- ✅ Telemetry & CATO API integration (Week 2)
  - Includes REST APIs for Launch tab
  - Includes terminal streaming for Terminals tab
- ✅ Production validations & Docker (Week 3)
- ✅ Unified Cato Dashboard (Weeks 4-7)
  - Git submodule setup (Week 4)
  - Unified installation (Week 5)
  - Unified startup command (Week 6)
  - 6-tab dashboard UI (Week 7)
- ✅ Web Console (Week 8 - optional development tool)

**What Was Removed from Original Plan:**
- Feature entities (not needed for Cato dashboard)
- Git worktrees (deferred - not needed for 5-agent MVP)
- Feature context aggregation (deferred to Build Kits phase)

**Success Metrics:**
- 20+ projects completed successfully
- 80%+ success rate
- 5+ alpha users
- Unified dashboard with all 6 tabs functional

**Team:** 3-4 engineers

---

### Month 3-4: Domain Expansion + Build Kits Launch
**Goal:** Prove coordination works beyond code + seed Build Kits marketplace

**Note**: Build Kits may benefit from Feature entities and Context aggregation - can be implemented in this phase if needed.

#### Week 9-10: Build Kit System Foundation (Formerly Week 9-10, now follows 8-week MVP)

**Build Kit Package Format:**
```python
# src/buildkits/packager.py

class BuildKitPackager:
    def create_buildkit(self, project_id: str, metadata: BuildKitMetadata) -> BuildKit:
        """
        Package a completed project as a Build Kit.

        Captures:
        - Source code
        - Architectural decisions
        - Build history (commits, artifacts, decisions)
        - Tech stack and dependencies
        - Customization points
        """

    def export_mkb(self, buildkit: BuildKit, output_path: str) -> None:
        """Export Build Kit as .mkb file (zip with metadata)."""

    def import_mkb(self, mkb_path: str) -> BuildKit:
        """Import .mkb file and validate."""
```

**Deliverables:**
- `.mkb` file format specification
- BuildKitPackager (create packages from projects)
- BuildKit metadata extractor (auto-detect tech stack)
- Basic Build Kit validation (no malware, valid structure)

**Tests:**
- Package a simple project as Build Kit
- Export/import .mkb file
- Validate Build Kit structure

#### Week 11-12: Build Kit Customization Engine

**Intelligent Customization:**
```python
# src/buildkits/customizer.py

class BuildKitCustomizer:
    def customize(
        self,
        buildkit: BuildKit,
        customization_request: str,
        agent: Agent
    ) -> Project:
        """
        Start a new project based on Build Kit with customizations.

        Process:
        1. Extract Build Kit to new project
        2. Analyze customization request
        3. Create tasks for modifications
        4. Agent(s) execute modifications
        5. Return customized project
        """

    def suggest_customizations(self, buildkit: BuildKit) -> List[str]:
        """Suggest common customizations based on architecture."""
```

**Deliverables:**
- BuildKitCustomizer (intelligent modifications)
- Customization task generator
- Context injection (Build Kit context → agent context)
- CLI command: `marcus buildkit customize <buildkit-id>`

**Tests:**
- Customize SaaS starter (swap Postgres → Supabase)
- Add feature to template (e.g., "add real-time chat")
- Verify architectural coherence maintained

#### Week 13-14: Build Kit Marketplace UI

**Dashboard Integration:**
```typescript
// dashboard/frontend/src/components/BuildKits/BuildKitBrowser.tsx

export const BuildKitBrowser: React.FC = () => {
  // Browse, search, filter Build Kits
  // Preview Build Kit details
  // One-click download/customize
};

// dashboard/frontend/src/components/BuildKits/BuildKitPublisher.tsx

export const BuildKitPublisher: React.FC = () => {
  // Publish completed project as Build Kit
  // Set pricing (free / paid)
  // Add description, screenshots, etc.
};
```

**Deliverables:**
- Build Kit browser (search, filter, preview)
- Build Kit detail page (tech stack, reviews, preview)
- Build Kit publisher (create listing)
- Download/customize flow

**Tests:**
- Browse Build Kits by category
- Search by tech stack
- Download and customize Build Kit
- Publish new Build Kit

#### Week 15-16: Seed Build Kits + Beta Launch

**Create 10 Seed Build Kits:**
1. **SaaS Starter** (Free) - Next.js + Supabase + Stripe
2. **E-commerce** (Free) - React + Node + PostgreSQL
3. **Blog/CMS** (Free) - Markdown-based blog
4. **Todo App** (Free) - Simple CRUD app
5. **Dashboard Template** (Free) - Admin dashboard
6. **API Service** (Free) - FastAPI REST API
7. **Real-time Chat** ($29) - WebSocket chat app
8. **Payment Integration** ($49) - Stripe integration with edge cases
9. **Multi-tenant B2B** ($79) - Tenant isolation, user management
10. **Auth System** ($29) - OAuth, SSO, MFA

**Beta Launch:**
- Invite 20 users to try Build Kits
- Monitor usage and feedback
- Iterate based on real usage

**Success Metrics:**
- 10 seed Build Kits published
- 50+ downloads in first month
- 5+ customization projects started
- 3+ community Build Kits published

**Team:** 4-5 engineers (1 added for marketplace work)

---

### Month 5-6: Brownfield + Marketplace Validation
**Goal:** Prove agents can work on existing projects + validate marketplace economics

#### Week 17-18: Brownfield Support (Existing Plan)
- Project ingestion (Git repos)
- Context retrieval (RAG over codebases)
- Safe modification workflows

#### Week 19-20: Payment Infrastructure

**Stripe Integration:**
```python
# src/marketplace/payments.py

class PaymentProcessor:
    def create_stripe_connect_account(self, user_id: str) -> str:
        """Create Stripe Connect account for creator."""

    def create_checkout_session(
        self,
        buyer_id: str,
        item_id: str,
        item_type: str,
        amount: Decimal
    ) -> CheckoutSession:
        """Create Stripe checkout for Build Kit or agent hire."""

    def handle_webhook(self, event: stripe.Event) -> None:
        """Handle Stripe webhooks (payment success, etc.)."""

    def create_escrow(
        self,
        transaction_id: str,
        amount: Decimal,
        release_conditions: List[str]
    ) -> Escrow:
        """Hold payment in escrow until conditions met."""

    def release_escrow(self, transaction_id: str) -> None:
        """Release payment to seller."""

    def calculate_fees(self, amount: Decimal, item_type: str) -> Decimal:
        """Calculate platform fee (15-20%)."""
```

**Deliverables:**
- Stripe Connect integration
- Checkout flow for Build Kits
- Escrow system for agent work
- Webhook handlers
- Fee calculation engine

**Tests:**
- Purchase Build Kit (test mode)
- Escrow held until project approved
- Fee calculation correct
- Payout to creator

#### Week 21-22: Agent Registry & Hiring

**Agent Marketplace:**
```python
# src/marketplace/agent_registry.py

class AgentRegistry:
    def list_agent(
        self,
        agent_id: str,
        specializations: List[str],
        pricing: AgentPricing,
        portfolio: List[ProjectReference]
    ) -> AgentListing:
        """List agent on marketplace."""

    def search_agents(
        self,
        query: str,
        specializations: List[str],
        max_rate: Optional[Decimal]
    ) -> List[AgentListing]:
        """Search for agents matching criteria."""

    def hire_agent(
        self,
        buyer_id: str,
        agent_id: str,
        project_description: str,
        budget: Decimal
    ) -> Transaction:
        """Initiate agent hiring transaction."""
```

**Deliverables:**
- Agent listing system
- Agent profile pages
- Search/filter agents
- Hire agent workflow
- Basic reputation system (star ratings)

**Tests:**
- List agent with specializations
- Search for "Rust expert"
- Hire agent for project
- Complete transaction and rate agent

#### Week 23-24: Marketplace Validation

**Validation Experiments:**
1. Recruit 10 agent creators
2. Have them list agents with pricing
3. Recruit 20 project owners
4. Facilitate 20 agent hiring transactions
5. Measure success rate and satisfaction

**Success Metrics (Marketplace Validation):**
- 20+ agent hiring transactions
- $10K GMV (gross marketplace value)
- 80%+ project success rate
- 4.0+ average rating
- 50%+ of agents get repeat hires

**If this validates:** Proceed to federation
**If this fails:** Pivot marketplace model or pricing

**Team:** 5-6 engineers (1 added for payment integration)

---

### Month 7-8: Federation Protocol
**Goal:** Enable Marcus instances to federate and share agent networks

#### Week 25-26: Federation Protocol Design

**Protocol Specification:**
```python
# src/federation/protocol.py

class FederationProtocol:
    """
    Marcus Federation Protocol (MFP) v1.0

    Enables multiple Marcus instances to:
    - Discover each other
    - Share agent availability
    - Delegate tasks across instances
    - Sync reputation data
    - Route payments
    """

    def discover_nodes(self) -> List[MarcusNode]:
        """Discover other Marcus instances on network."""

    def announce_capabilities(self, capabilities: List[str]) -> None:
        """Announce what this instance can provide."""

    def request_task_delegation(
        self,
        task: Task,
        target_node: MarcusNode
    ) -> TaskDelegation:
        """Delegate task to agent on another instance."""

    def sync_reputation(
        self,
        agent_id: str,
        reputation_update: ReputationUpdate
    ) -> None:
        """Sync reputation across federated network."""
```

**Deliverables:**
- Federation protocol specification (v1.0)
- Node discovery mechanism
- Task delegation API
- Reputation synchronization
- Payment routing for cross-instance transactions

#### Week 27-28: Federation Implementation

**Deliverables:**
- Federation client/server
- Instance registration
- Cross-instance task assignment
- Reputation portability
- CLI: `marcus federation join <node-url>`

**Tests:**
- Connect two Marcus instances
- Delegate task from Instance A to Instance B
- Agent on Instance B completes task
- Reputation syncs back to Instance A

#### Week 29-30: Build Kits Federation

**Build Kits Across Federation:**
- Build Kit published on Instance A
- Visible on Instance B (federated marketplace)
- User on Instance B downloads Build Kit
- Payment routed through federation

**Deliverables:**
- Federated Build Kit marketplace
- Cross-instance licensing
- Revenue sharing across instances

#### Week 31-32: Enterprise Pilots

**Target:** 5 enterprise pilots testing private federation

**Use case:**
- Company runs private Marcus instance
- Federates with contractor agencies
- Contractors' agents work on company projects
- All tracked, compliant, auditable

**Success Metrics:**
- 5+ enterprise pilots
- 10+ federated instances
- 20+ cross-instance task delegations
- Zero data leaks or security incidents

**Team:** 6-7 engineers (1 added for federation)

---

### Month 9-10: Full Marketplace Launch
**Goal:** Open marketplace to general public, validate economics at scale

#### Week 33-34: Advanced Reputation System

**Multi-dimensional Reputation:**
```python
# src/marketplace/reputation.py

class ReputationSystem:
    def calculate_reputation(self, agent_id: str) -> AgentReputation:
        """
        Calculate reputation across multiple dimensions:
        - Overall rating (0-5 stars)
        - Success rate (% of projects completed successfully)
        - Domain-specific ratings (Rust expert, UX design, etc.)
        - Response time
        - Code quality metrics
        - Client satisfaction
        """

    def calculate_trust_score(self, agent_id: str) -> float:
        """
        Trust score (0-100) based on:
        - Verification status
        - Time on platform
        - Transaction history
        - Dispute rate
        """
```

**Deliverables:**
- Multi-dimensional reputation
- Trust score calculation
- Verified badge system
- Historical performance analytics

#### Week 35-36: Discovery & Recommendations

**Smart Matching:**
```python
# src/marketplace/discovery.py

class AgentDiscovery:
    def recommend_agents(
        self,
        project_description: str,
        budget: Decimal,
        urgency: str
    ) -> List[AgentRecommendation]:
        """
        Recommend best agents for project based on:
        - Specialization match
        - Availability
        - Budget fit
        - Past performance on similar projects
        """

    def predict_success_rate(
        self,
        agent_id: str,
        project_type: str
    ) -> float:
        """Predict likelihood of project success."""
```

**Deliverables:**
- ML-based agent recommendations
- Success rate prediction
- Smart search with filters
- "Best match" badges

#### Week 37-38: Certification Program

**Marcus Certified Agents:**
- Application process
- Skill verification
- Quality benchmarks
- Ongoing monitoring

**Certification Tiers:**
- Bronze: 10 successful projects, 4.0+ rating
- Silver: 50 successful projects, 4.5+ rating, verified skills
- Gold: 100+ successful projects, 4.8+ rating, specialized certification

**Benefits:**
- Certified badge
- Higher visibility in search
- Priority placement
- Can charge 20-30% more

**Deliverables:**
- Certification application system
- Skill verification process
- Certification badges
- Certified agent directory

#### Week 39-40: Marketplace Launch & Growth

**Public Launch:**
- Open marketplace to all users
- Marketing push (see Growth Playbook)
- Creator incentive program
- Quality monitoring

**Success Metrics:**
- 100+ agents listed
- 200+ projects posted
- $50K GMV in first month
- 4.0+ average rating
- <5% dispute rate

**Team:** 7-8 engineers (1 added for ML/recommendations)

---

### Month 11-12: Scale & Polish
**Goal:** Production hardening, performance optimization, enterprise readiness

#### Week 41-42: Performance Optimization
- Database query optimization
- Caching layer (Redis)
- CDN for Build Kit downloads
- API rate limiting
- Load testing (1000+ concurrent users)

#### Week 43-44: Security Hardening
- Penetration testing
- Security audit (third-party)
- SOC2 compliance preparation
- GDPR compliance
- Bug bounty program

#### Week 45-46: Enterprise Features
- RBAC (Role-Based Access Control)
- SSO integration (SAML, OAuth)
- Audit logs
- Custom integrations
- White-label options

#### Week 47-48: Documentation & Onboarding
- Complete API documentation
- Video tutorials
- Creator guides
- Best practices documentation
- Interactive onboarding flow

**Success Metrics (End of Month 12):**
- 1,000+ active users
- 500+ Build Kits published
- 100+ agents in marketplace
- $100K+ GMV
- 99%+ uptime
- 10+ enterprise pilots signed

**Team:** 8-10 engineers (full team)

---

## Build Kits Technical Specification

### The .mkb File Format

**Marcus Build Kit (.mkb) File Structure:**

```
buildkit-name.mkb
├── manifest.json          # Build Kit metadata
├── source/                # Source code files
│   ├── src/
│   ├── tests/
│   └── ...
├── architecture/          # Architectural documentation
│   ├── decisions.json     # Architectural decisions log
│   ├── trade-offs.md      # Trade-offs explained
│   └── diagrams/          # Architecture diagrams
├── buildstate/            # Marcus build history
│   ├── commits.json       # Git commit history
│   ├── artifacts.json     # Artifacts created
│   └── decisions.json     # Decisions logged
├── replay/                # Replay instructions
│   ├── steps.json         # Steps to reconstruct
│   └── context.json       # Context for each step
└── assets/                # Screenshots, videos, etc.
    ├── screenshot.png
    └── demo.mp4
```

**manifest.json Schema:**

```json
{
  "buildkit_id": "saas-starter-v1",
  "name": "SaaS Starter Template",
  "version": "1.0.0",
  "description": "Production-ready SaaS starter with auth, payments, and admin dashboard",
  "creator": {
    "user_id": "creator-123",
    "name": "John Doe"
  },
  "tech_stack": {
    "languages": ["TypeScript", "Python"],
    "frameworks": ["Next.js", "FastAPI"],
    "databases": ["PostgreSQL"],
    "services": ["Stripe", "SendGrid", "AWS S3"]
  },
  "pricing": {
    "type": "one-time",  // or "free", "subscription"
    "amount": 49.00,
    "currency": "USD"
  },
  "customization_points": [
    "Authentication provider (Supabase/Firebase/Custom)",
    "Payment processor (Stripe/PayPal)",
    "Database (PostgreSQL/MySQL/MongoDB)",
    "Deployment target (Vercel/AWS/GCP)"
  ],
  "tags": ["saas", "starter", "nextjs", "stripe"],
  "license": "MIT",
  "created_at": "2025-11-01T00:00:00Z",
  "downloads": 127,
  "rating": 4.8,
  "reviews": 23
}
```

### Build Kit Creation Workflow

**1. Developer completes project using Marcus:**
```bash
$ marcus start
# ... agents coordinate and build project ...
# Project completes successfully
```

**2. Developer packages project as Build Kit:**
```bash
$ marcus buildkit create \
    --project-id my-saas \
    --name "SaaS Starter Template" \
    --description "Production-ready SaaS starter" \
    --price 49 \
    --customization-points "auth,payments,database"

✓ Analyzing project structure...
✓ Extracting architectural decisions...
✓ Capturing build history...
✓ Generating replay instructions...
✓ Creating .mkb package...

Build Kit created: saas-starter-v1.mkb

Next steps:
1. Test your Build Kit: marcus buildkit test saas-starter-v1.mkb
2. Publish to marketplace: marcus buildkit publish saas-starter-v1.mkb
```

**3. Marcus validates Build Kit:**
```python
# src/buildkits/validator.py

class BuildKitValidator:
    def validate(self, buildkit_path: str) -> ValidationResult:
        """
        Validate Build Kit before publishing:
        - Structure valid (all required files present)
        - No malware/security issues
        - Source code compiles/runs
        - Dependencies available
        - Metadata complete
        - License compatible
        """
```

**4. Developer publishes to marketplace:**
```bash
$ marcus buildkit publish saas-starter-v1.mkb

✓ Validating Build Kit...
✓ Uploading to marketplace...
✓ Creating listing...

Published! Your Build Kit is now available:
https://marcus.dev/buildkits/saas-starter-v1

Set up Stripe Connect to receive payments:
marcus payments connect
```

### Build Kit Customization Workflow

**1. User discovers Build Kit:**
```bash
$ marcus buildkit search "saas starter"

Found 5 Build Kits:

1. SaaS Starter Template ($49)
   ⭐ 4.8 (23 reviews) • 127 downloads
   Next.js + FastAPI + PostgreSQL + Stripe

2. Simple SaaS Boilerplate (FREE)
   ⭐ 4.2 (8 reviews) • 43 downloads
   React + Node + MongoDB

...
```

**2. User views Build Kit details:**
```bash
$ marcus buildkit show saas-starter-v1

SaaS Starter Template v1.0.0
By: John Doe (@johndoe)

Description:
Production-ready SaaS starter with auth, payments, and admin dashboard.
Includes user management, subscription billing, and analytics.

Tech Stack:
• Frontend: Next.js 14, TypeScript, Tailwind CSS
• Backend: FastAPI, Python 3.11+
• Database: PostgreSQL 15
• Services: Stripe, SendGrid, AWS S3

Customization Points:
✓ Authentication provider (Supabase/Firebase/Custom)
✓ Payment processor (Stripe/PayPal)
✓ Database (PostgreSQL/MySQL/MongoDB)
✓ Deployment target (Vercel/AWS/GCP)

Reviews: ⭐ 4.8/5 (23 reviews)
Downloads: 127
Price: $49

[Download] [Customize] [Preview]
```

**3. User starts customization:**
```bash
$ marcus buildkit customize saas-starter-v1 \
    --project-name "MyAwesomeApp" \
    --customizations "auth=firebase,database=mongodb,deploy=vercel"

✓ Downloading Build Kit...
✓ Extracting to /projects/MyAwesomeApp...
✓ Analyzing customization request...
✓ Creating customization tasks...

Customization Plan:
1. Swap authentication: Supabase → Firebase
2. Swap database: PostgreSQL → MongoDB
3. Update deployment config: Generic → Vercel
4. Update dependencies
5. Run tests

Start customization? [Y/n]: y

✓ Creating project...
✓ Assigning tasks to agents...

Agent "frontend-pro" assigned: Update auth integration
Agent "backend-expert" assigned: Swap database layer
Agent "devops-specialist" assigned: Configure Vercel deployment

Customization in progress... (view: marcus status)
```

**4. Marcus coordinates agents to customize:**
```python
# src/buildkits/customizer.py

class BuildKitCustomizer:
    async def customize(
        self,
        buildkit: BuildKit,
        customizations: Dict[str, str],
        project_name: str
    ) -> Project:
        # 1. Extract Build Kit to new project
        project = self._create_project_from_buildkit(buildkit, project_name)

        # 2. Analyze customizations and create tasks
        tasks = self._generate_customization_tasks(
            buildkit,
            customizations
        )

        # 3. Assign tasks to agents
        for task in tasks:
            agent = self._find_best_agent(task)
            await self.orchestrator.assign_task(task, agent)

        # 4. Monitor progress
        await self._monitor_customization(project)

        return project
```

**5. User receives customized project:**
```bash
✓ Customization complete!

Project: MyAwesomeApp
Location: /projects/MyAwesomeApp

Changes made:
✓ Authentication: Supabase → Firebase
✓ Database: PostgreSQL → MongoDB
✓ Deployment: Generic → Vercel
✓ Updated 47 files
✓ All tests passing

Next steps:
1. Review changes: cd /projects/MyAwesomeApp
2. Start development: npm run dev
3. Deploy: vercel deploy

Want to publish your customized version as a Build Kit?
marcus buildkit create --based-on saas-starter-v1
```

### Intelligent Customization Engine

**How Marcus maintains architectural coherence:**

```python
# src/buildkits/customizer.py

class ArchitecturalCoherenceEngine:
    def ensure_coherence(
        self,
        original_architecture: ArchitecturalDecisions,
        customization: Dict[str, str]
    ) -> List[Task]:
        """
        Ensure customizations maintain architectural coherence.

        Example: Swapping PostgreSQL → MongoDB
        - Identify all SQL queries
        - Convert to MongoDB queries
        - Update ORM/schema
        - Update migrations
        - Update tests
        - Verify data models still make sense
        """

        tasks = []

        # Analyze impact of each customization
        for component, new_value in customization.items():
            impact = self._analyze_impact(component, new_value)

            # Generate tasks to handle impact
            for affected_area in impact.affected_areas:
                task = self._create_update_task(affected_area, new_value)
                tasks.append(task)

        # Ensure tasks maintain coherence
        tasks = self._add_coherence_checks(tasks, original_architecture)

        return tasks
```

**Example: Swapping Auth Provider**

When user requests "auth=firebase" (replacing Supabase):

1. **Identify affected components:**
   - Login flow
   - Signup flow
   - Session management
   - Protected routes
   - User profile
   - Auth utilities

2. **Generate tasks:**
   - Task 1: Replace Supabase client with Firebase client
   - Task 2: Update login/signup UI components
   - Task 3: Update session management middleware
   - Task 4: Update protected route guards
   - Task 5: Update user profile fetching
   - Task 6: Update environment variables
   - Task 7: Update tests

3. **Maintain coherence:**
   - Ensure auth flow still matches original pattern
   - Verify error handling preserved
   - Check security practices maintained
   - Validate user data structure compatible

---

## Marketplace Infrastructure

### Agent Registry & Discovery

**Agent Profile Schema:**

```python
@dataclass
class AgentProfile:
    agent_id: str
    display_name: str
    bio: str
    avatar_url: str

    # Capabilities
    specializations: List[str]  # ["rust", "performance", "systems"]
    tech_stack: List[str]       # ["Python", "C++", "CUDA"]

    # Pricing
    hourly_rate: Optional[Decimal]
    project_rate_min: Optional[Decimal]
    project_rate_max: Optional[Decimal]

    # Availability
    available: bool
    timezone: str
    response_time: timedelta  # Average response time

    # Portfolio
    portfolio_projects: List[ProjectReference]
    build_kits_published: List[str]

    # Reputation
    reputation: AgentReputation
    certifications: List[Certification]

    # Metadata
    joined_at: datetime
    last_active: datetime
```

**Discovery Algorithm:**

```python
# src/marketplace/discovery.py

class AgentDiscovery:
    def search(
        self,
        query: str,
        filters: SearchFilters
    ) -> List[AgentProfile]:
        """
        Search agents with sophisticated matching:

        1. Text search (bio, specializations)
        2. Skill matching (exact + similar)
        3. Budget filtering
        4. Availability filtering
        5. Reputation filtering
        6. Rank by relevance + reputation
        """

        # Step 1: Full-text search
        candidates = self._text_search(query)

        # Step 2: Apply filters
        candidates = self._apply_filters(candidates, filters)

        # Step 3: Rank by relevance
        ranked = self._rank_by_relevance(
            candidates,
            query,
            filters
        )

        return ranked

    def recommend(
        self,
        project_description: str,
        budget: Decimal
    ) -> List[AgentRecommendation]:
        """
        ML-based recommendations:

        1. Analyze project requirements (NLP)
        2. Extract needed skills
        3. Find agents with those skills
        4. Predict success rate for each agent
        5. Rank by predicted success + reputation
        """

        # Extract requirements using NLP
        requirements = self.nlp_analyzer.extract_requirements(
            project_description
        )

        # Find matching agents
        matches = self._find_skill_matches(requirements.skills)

        # Predict success rate
        predictions = [
            AgentRecommendation(
                agent=agent,
                match_score=self._calculate_match_score(agent, requirements),
                predicted_success_rate=self._predict_success(agent, requirements),
                estimated_cost=self._estimate_cost(agent, requirements)
            )
            for agent in matches
        ]

        # Rank by success rate * reputation / cost
        ranked = sorted(
            predictions,
            key=lambda x: (x.predicted_success_rate * x.agent.reputation.overall_rating) / x.estimated_cost,
            reverse=True
        )

        return ranked[:10]
```

### Transaction & Escrow System

**Transaction Flow:**

```
1. Buyer hires agent
   ↓
2. Payment held in escrow
   ↓
3. Agent completes work
   ↓
4. Buyer reviews & approves
   ↓
5. Escrow released to agent (minus platform fee)
   ↓
6. Both parties rate each other
```

**Escrow Implementation:**

```python
# src/marketplace/escrow.py

class EscrowSystem:
    def create_escrow(
        self,
        transaction: Transaction,
        release_conditions: ReleaseConditions
    ) -> Escrow:
        """
        Create escrow for agent work:

        - Hold payment via Stripe
        - Define release conditions
        - Set timeout (auto-release after 30 days if no disputes)
        """

    def release_escrow(
        self,
        transaction_id: str,
        reason: str
    ) -> None:
        """
        Release payment to agent:

        - Deduct platform fee (20%)
        - Transfer to agent's Stripe Connect account
        - Update transaction status
        - Notify both parties
        """

    def dispute_escrow(
        self,
        transaction_id: str,
        dispute_reason: str,
        evidence: List[str]
    ) -> Dispute:
        """
        Handle dispute:

        - Hold escrow
        - Create dispute case
        - Notify both parties
        - Start resolution process
        """
```

**Dispute Resolution:**

```python
# src/marketplace/disputes.py

class DisputeResolution:
    def create_dispute(
        self,
        transaction_id: str,
        initiator: str,  # buyer or agent
        reason: str,
        evidence: List[Evidence]
    ) -> Dispute:
        """
        Create dispute case.
        """

    def mediate(self, dispute_id: str) -> DisputeResolution:
        """
        Mediation process:

        1. Review evidence from both parties
        2. Check transaction history
        3. Review communication logs
        4. Make determination
        5. Distribute escrow accordingly
        """

    def appeal(self, dispute_id: str, appeal_reason: str) -> Appeal:
        """
        Allow appeals of dispute resolutions.
        """
```

### Reputation System

**Multi-Dimensional Reputation:**

```python
# src/marketplace/reputation.py

@dataclass
class AgentReputation:
    # Overall metrics
    overall_rating: float  # 0-5 stars
    total_projects: int
    success_rate: float  # 0-1

    # Domain-specific ratings
    domain_ratings: Dict[str, DomainRating]  # "rust" -> 4.8, "frontend" -> 4.2

    # Quality metrics
    code_quality_score: float  # 0-100
    communication_score: float  # 0-100
    timeliness_score: float  # 0-100

    # Trust indicators
    verified: bool
    certifications: List[str]
    time_on_platform: timedelta
    dispute_rate: float  # 0-1

    # Reviews
    reviews: List[Review]
    response_rate: float  # % of inquiries responded to
    average_response_time: timedelta

class ReputationCalculator:
    def calculate(self, agent_id: str) -> AgentReputation:
        """
        Calculate comprehensive reputation:

        - Weighted average of ratings (recent = more weight)
        - Success rate (completed / total projects)
        - Domain expertise (skill-specific ratings)
        - Trust score (verification, disputes, tenure)
        """

    def update_after_project(
        self,
        agent_id: str,
        project: Project,
        rating: Rating
    ) -> None:
        """
        Update reputation after project completion:

        - Add new rating (recency-weighted)
        - Update domain-specific ratings
        - Update success rate
        - Update quality metrics
        """
```

**Trust Score Algorithm:**

```python
def calculate_trust_score(agent: Agent) -> float:
    """
    Trust score (0-100) based on multiple factors:

    - Verification status (email, phone, ID) = 20 points
    - Time on platform (>6 months) = 15 points
    - Project history (>10 projects) = 15 points
    - Success rate (>90%) = 20 points
    - Low dispute rate (<5%) = 15 points
    - Certifications = 15 points

    Total: 100 points
    """
    score = 0

    # Verification (20 points)
    if agent.email_verified:
        score += 7
    if agent.phone_verified:
        score += 7
    if agent.identity_verified:
        score += 6

    # Tenure (15 points)
    months = (datetime.now() - agent.joined_at).days / 30
    score += min(months / 6 * 15, 15)

    # Experience (15 points)
    projects = agent.reputation.total_projects
    score += min(projects / 10 * 15, 15)

    # Success (20 points)
    score += agent.reputation.success_rate * 20

    # Low disputes (15 points)
    dispute_rate = agent.reputation.dispute_rate
    score += (1 - dispute_rate / 0.05) * 15 if dispute_rate < 0.05 else 0

    # Certifications (15 points)
    cert_count = len(agent.certifications)
    score += min(cert_count / 3 * 15, 15)

    return min(score, 100)
```

---

## Open Source Strategy

### What's Open, What's Closed

```
┌─────────────────────────────────────────────────────────┐
│                Open Source Core (Apache 2.0)            │
├─────────────────────────────────────────────────────────┤
│                                                           │
│  • Marcus orchestration engine                           │
│  • Task decomposition algorithms                         │
│  • Workspace isolation (git worktrees)                   │
│  • Context aggregation                                   │
│  • MCP server & tools                                    │
│  • CLI                                                   │
│  • Basic dashboard (local)                               │
│  • Build Kit format & packager                           │
│  • Federation protocol specification                     │
│                                                           │
└─────────────────────────────────────────────────────────┘
                          ↕
┌─────────────────────────────────────────────────────────┐
│              Proprietary Services (Closed)              │
├─────────────────────────────────────────────────────────┤
│                                                           │
│  • Marcus Cloud (managed hosting)                        │
│  • Marketplace infrastructure                            │
│    - Payment processing                                  │
│    - Escrow system                                       │
│    - Reputation algorithms                               │
│    - Discovery/recommendation ML models                  │
│  • Enterprise features                                   │
│    - SSO, RBAC, audit logs                              │
│    - SLA guarantees                                      │
│    - Dedicated support                                   │
│  • Advanced analytics                                    │
│                                                           │
└─────────────────────────────────────────────────────────┘
```

### Licensing Strategy

**Core: Apache 2.0**
- Permissive license
- Commercial use allowed
- Patent protection
- No copyleft (can be used in proprietary products)

**Why Apache 2.0:**
- Enterprises comfortable with it
- Encourages adoption
- Allows commercial integrations
- Protects against patent trolls

**Proprietary Services: Closed**
- Hosted service
- Marketplace backend
- Enterprise features
- ML models

### Community Engagement Plan

**GitHub Strategy:**

```yaml
Repository Structure:
  marcus-ai/marcus:
    - Core orchestration engine
    - CLI tools
    - Documentation
    - Examples

  marcus-ai/marcus-dashboard:
    - Basic dashboard (open)
    - Community contributions welcome

  marcus-ai/build-kits:
    - Community Build Kits repository
    - Templates and examples
    - Quality reviewed

  marcus-ai/awesome-marcus:
    - Curated list of resources
    - Integrations
    - Tutorials
```

**Community Programs:**

1. **Contributor Recognition:**
   - Hall of Fame on website
   - Special badges on marketplace
   - Swag for significant contributions
   - Annual contributor awards

2. **Build Kit Creator Program:**
   - Featured Build Kits
   - Creator spotlights
   - Revenue sharing (85/15)
   - Marketing support

3. **Agent Developer Program:**
   - SDK and documentation
   - Integration guides
   - Developer office hours
   - Beta access to new features

4. **Research Partnership:**
   - Collaborate with universities
   - Publish research papers
   - Shared telemetry data (anonymized)
   - Joint conferences/workshops

**Communication Channels:**

- **GitHub Discussions:** Technical questions, feature requests
- **Discord:** Real-time community chat
- **Blog:** Product updates, tutorials, case studies
- **Twitter/X:** Announcements, community highlights
- **YouTube:** Video tutorials, demos, talks
- **Newsletter:** Monthly updates

### Open Source Release Strategy

**Month 1-2: Private Development**
- Build MVP
- Internal testing
- Documentation

**Month 3: Public Alpha**
- Release core on GitHub
- Invite 20 alpha testers
- Gather feedback
- Iterate rapidly

**Month 4: Public Beta**
- Open to everyone
- Start building community
- Accept contributions
- Launch Discord

**Month 6: Version 1.0 Release**
- Stable release
- Complete documentation
- Marketing push
- HackerNews, ProductHunt, Reddit

**Month 9: Marketplace Launch**
- Open marketplace
- Creator program
- Growth acceleration

---

## Growth Playbook

### Phase 1: Foundation (Months 1-3)

**Goal:** Build initial community of early adopters

**Tactics:**

1. **Content Marketing:**
   - Blog posts on agent coordination challenges
   - Technical deep dives (git worktrees, context aggregation)
   - Case studies (first 10 projects)
   - SEO optimization

2. **Developer Outreach:**
   - Post on HackerNews (Show HN: Marcus)
   - r/programming, r/MachineLearning, r/programming
   - Dev.to articles
   - Medium cross-posts

3. **Social Media:**
   - Twitter thread: "We built coordination infrastructure for AI agents"
   - Demo video on YouTube
   - LinkedIn posts targeting CTOs/VPs Engineering

4. **Partnerships:**
   - Reach out to Claude, Cursor, Windsurf teams
   - Explore integrations
   - Guest posts on their blogs

**Metrics:**
- 1,000 GitHub stars
- 100 Discord members
- 20 active users
- 10 contributors

### Phase 2: Growth (Months 4-6)

**Goal:** Achieve product-market fit, grow to 100 users

**Tactics:**

1. **Build Kits Launch:**
   - Seed 10 high-quality Build Kits
   - ProductHunt launch
   - "Show HN: Build Kits for AI agent projects"
   - Demo video viral campaign

2. **Creator Marketing:**
   - Incentivize first 20 creators
   - Spotlight creator interviews
   - Revenue sharing announcements
   - Case studies

3. **Community Building:**
   - Weekly office hours
   - Monthly webinars
   - Contributor highlights
   - Swag for contributors

4. **Paid Advertising (Small Budget):**
   - Google Ads (agent coordination keywords)
   - Reddit ads (r/programming, r/MachineLearning)
   - Twitter ads (targeted at developers)
   - $5K/month budget

**Metrics:**
- 5,000 GitHub stars
- 500 Discord members
- 100 active users
- 50 Build Kits published
- $5K GMV

### Phase 3: Scale (Months 7-9)

**Goal:** Reach 500 users, establish marketplace

**Tactics:**

1. **Marketplace Launch:**
   - Press release
   - TechCrunch pitch
   - VentureBeat coverage
   - Podcast appearances

2. **Agent Creator Program:**
   - Recruit 50 agent developers
   - $1,000 signing bonus for first 10
   - Featured agent spotlights
   - Success stories

3. **Conference Presence:**
   - Speak at AI/ML conferences
   - Booth at developer conferences
   - Sponsor community events
   - Host Marcus meetups

4. **Paid Marketing Scale:**
   - Increase budget to $20K/month
   - LinkedIn ads (B2B targeting)
   - YouTube ads (tutorial content)
   - Retargeting campaigns

**Metrics:**
- 10,000 GitHub stars
- 2,000 Discord members
- 500 active users
- 200 Build Kits published
- 50 agents listed
- $50K GMV

### Phase 4: Expansion (Months 10-12)

**Goal:** Reach 1,000 users, enterprise pilots

**Tactics:**

1. **Enterprise Sales:**
   - Hire first sales rep
   - Outbound to 100 target companies
   - Demo days
   - Case studies

2. **Partnership Ecosystem:**
   - Formal partnerships with AI companies
   - Integration marketplace
   - Co-marketing campaigns
   - Revenue sharing deals

3. **Thought Leadership:**
   - Publish research papers
   - Host MarcusCon (virtual conference)
   - Podcast series
   - Book deal (optional)

4. **Global Expansion:**
   - Multi-language support
   - Regional communities
   - International payment methods
   - Localized marketing

**Metrics:**
- 20,000 GitHub stars
- 5,000 Discord members
- 1,000 active users
- 500 Build Kits published
- 100 agents listed
- 10 enterprise pilots
- $100K GMV

### Growth Channels (Priority Order)

**Tier 1 (Highest ROI):**
1. GitHub/open source community
2. Content marketing (blog, tutorials)
3. Build Kit creators (viral loop)
4. Developer word-of-mouth

**Tier 2 (Medium ROI):**
5. Social media (Twitter, LinkedIn)
6. Partnerships (AI tool companies)
7. Community events (meetups, webinars)
8. Paid advertising (targeted)

**Tier 3 (Experimental):**
9. Influencer partnerships
10. Affiliate program
11. Referral program
12. Conference sponsorships

---

## Team & Resources

### Month 1-2: Founding Team (3-4 people)

```
Team Structure:

1. Tech Lead / Architect (You)
   - Overall architecture
   - Core orchestration
   - Technical decisions

2. Backend Engineer
   - MCP server
   - API development
   - Database design

3. Frontend Engineer
   - Dashboard (Cato)
   - CLI refinement
   - UX/UI

4. DevOps Engineer (Part-time or contractor)
   - Infrastructure setup
   - CI/CD
   - Monitoring
```

**Budget:** $80K-120K/month
- Salaries: $60K-80K
- Infrastructure: $5K-10K
- Tools/services: $5K-10K
- Buffer: $10K-20K

### Month 3-6: Scaling Team (5-6 people)

```
Add:

5. Full-Stack Engineer (Build Kits)
   - Build Kit system
   - Marketplace backend
   - Customization engine

6. Designer (Contract/Part-time)
   - UI/UX design
   - Brand identity
   - Marketing assets
```

**Budget:** $120K-180K/month
- Salaries: $90K-130K
- Infrastructure: $10K-20K
- Tools/services: $10K-15K
- Marketing: $10K-15K

### Month 7-10: Growth Team (7-9 people)

```
Add:

7. Marketplace Engineer
   - Payment integration
   - Escrow system
   - Reputation algorithms

8. ML Engineer (Recommendations)
   - Agent discovery
   - Success prediction
   - Smart matching

9. Developer Advocate / Community Manager
   - Community engagement
   - Documentation
   - Support
```

**Budget:** $180K-250K/month
- Salaries: $130K-180K
- Infrastructure: $20K-30K
- Marketing: $20K-30K
- Community: $10K-10K

### Month 11-12: Enterprise Team (10-12 people)

```
Add:

10. Solutions Engineer
    - Enterprise sales support
    - Custom integrations
    - Technical consulting

11. Technical Writer
    - Documentation
    - Tutorials
    - API reference

12. Customer Success Manager (Optional)
    - Onboarding
    - Training
    - Support
```

**Budget:** $220K-300K/month
- Salaries: $160K-220K
- Infrastructure: $30K-40K
- Marketing: $20K-30K
- Sales: $10K-10K

### 12-Month Total Budget

```
Months 1-2:  $200K-250K   ($100K-125K/mo avg)
Months 3-6:  $600K-750K   ($150K-188K/mo avg)
Months 7-10: $800K-1M     ($200K-250K/mo avg)
Months 11-12: $500K-600K  ($250K-300K/mo avg)

Total: $2.1M-2.6M for 12 months
```

**Funding Strategy:**
- Bootstrap or friends/family: $200K (Months 1-2)
- Seed round: $2M-3M (before Month 3)
- Series A: $10M+ (Month 9-12, optional)

---

## Success Metrics & KPIs

### North Star Metric

**Projects Successfully Completed**
- Ultimate measure of value
- Tracks actual utility
- Leading indicator of retention

### Tier 1 Metrics (Weekly Tracking)

**Adoption:**
- Weekly Active Users (WAU)
- New user signups
- Activation rate (completed first project)
- Retention (week-over-week)

**Engagement:**
- Projects created
- Projects completed
- Average project duration
- Agent coordination events

**Marketplace:**
- Build Kits published
- Build Kits downloaded
- Agent listings
- Transactions initiated
- Gross Marketplace Value (GMV)

**Quality:**
- Project success rate (builds + works)
- User satisfaction (NPS)
- Average rating (Build Kits + agents)
- Dispute rate

### Tier 2 Metrics (Monthly Tracking)

**Community:**
- GitHub stars
- Contributors
- Discord members
- Forum posts/activity

**Revenue:**
- MRR (Monthly Recurring Revenue)
- GMV (Gross Marketplace Value)
- Platform revenue (fees collected)
- Enterprise contracts signed

**Technical:**
- System uptime
- API response time (p50, p95, p99)
- Error rate
- Support tickets

### Milestone Targets

**Month 3:**
- ✅ 20 active users
- ✅ 50 projects completed
- ✅ 80% success rate
- ✅ 1,000 GitHub stars

**Month 6:**
- ✅ 100 active users
- ✅ 300 projects completed
- ✅ 50 Build Kits published
- ✅ $10K GMV
- ✅ 5,000 GitHub stars

**Month 9:**
- ✅ 500 active users
- ✅ 2,000 projects completed
- ✅ 200 Build Kits published
- ✅ 50 agents listed
- ✅ $50K GMV
- ✅ 10,000 GitHub stars

**Month 12:**
- ✅ 1,000 active users
- ✅ 10,000 projects completed
- ✅ 500 Build Kits published
- ✅ 100 agents listed
- ✅ $100K GMV
- ✅ 10 enterprise pilots
- ✅ 20,000 GitHub stars

---

## Risk Management

### Technical Risks

**Risk 1: Git worktrees cause problems at scale**
- **Probability:** Medium
- **Impact:** High
- **Mitigation:**
  - Extensive testing in Month 3
  - Fallback to branch-per-agent
  - Monitor disk usage
  - Cleanup automation
- **Early warning:** Users report workspace errors

**Risk 2: Context aggregation insufficient**
- **Probability:** Medium
- **Impact:** Medium
- **Mitigation:**
  - User feedback loops
  - Iterative improvement
  - Manual context as fallback
- **Early warning:** Agents frequently ask for more context

**Risk 3: Marketplace quality problems**
- **Probability:** High
- **Impact:** High
- **Mitigation:**
  - Quality review process
  - Reputation system
  - User ratings/reviews
  - Ability to remove bad actors
- **Early warning:** Spike in disputes or low ratings

### Market Risks

**Risk 4: Developers don't adopt multi-agent workflows**
- **Probability:** Medium
- **Impact:** Critical
- **Mitigation:**
  - Stage 1 validation tests this
  - Exit ramp after Month 2
  - Pivot to single-agent if needed
- **Early warning:** Low activation rate, high churn

**Risk 5: Incumbents build similar features**
- **Probability:** High
- **Impact:** Medium
- **Mitigation:**
  - Open protocol makes us partner-able
  - Network effects from marketplace
  - Speed of execution
  - Community moat
- **Early warning:** Competitor announcements

**Risk 6: Agent economy doesn't materialize**
- **Probability:** Low
- **Impact:** High
- **Mitigation:**
  - Start with human developers using agents
  - Gradually shift to agent-to-agent
  - Still valuable as coordination tool
- **Early warning:** Agents don't list on marketplace

### Business Risks

**Risk 7: Can't monetize effectively**
- **Probability:** Medium
- **Impact:** High
- **Mitigation:**
  - Multiple revenue streams
  - Test pricing early (Month 6)
  - Enterprise fallback
- **Early warning:** Low conversion rates

**Risk 8: Funding runs out**
- **Probability:** Low
- **Impact:** Critical
- **Mitigation:**
  - Conservative runway planning
  - Revenue generation starts Month 6
  - Exit ramps at each stage
  - Seed round before Month 3
- **Early warning:** Burn rate exceeds plan

**Risk 9: Community doesn't form**
- **Probability:** Low
- **Impact:** High
- **Mitigation:**
  - Heavy community investment
  - Creator incentives
  - Transparent communication
  - Quick response to feedback
- **Early warning:** Low GitHub engagement

### Go/No-Go Decision Points

**Month 2 Decision:**
- **GO IF:** 20+ projects, 80%+ success, 5+ users
- **STOP IF:** Success rate <70%, users abandon

**Month 6 Decision:**
- **GO IF:** 100+ users, 50+ Build Kits, $10K GMV
- **PIVOT IF:** Build Kits don't sell (try different model)

**Month 9 Decision:**
- **GO IF:** 500+ users, 50+ agents, $50K GMV
- **SLOW IF:** Marketplace struggling (focus on Build Kits)

**Month 12 Decision:**
- **SCALE IF:** 1,000+ users, $100K GMV, 10+ enterprise pilots
- **OPTIMIZE IF:** Close but not quite (improve efficiency before scaling)

---

## Conclusion: The Execution Playbook

### The Strategy in One Page

**Vision:** Global coordination infrastructure for AI agents

**Approach:** Open source core + marketplace services

**Timeline:** 12 months to validated infrastructure

**Revenue:** Cloud hosting + marketplace transactions + enterprise contracts

**Moat:** Network effects (agents + Build Kits + reputation)

**Success:** 1,000 users, $100K GMV, 10 enterprise pilots by Month 12

### Next Steps (Week by Week)

**This Week:**
- Review this plan
- Finalize team commitments
- Set up infrastructure
- Start Week 1 (Configuration system)

**Next 4 Weeks:**
- Complete Weeks 1-3 (MVP core: Config, Telemetry, Validations)
- Get to 10 projects completed
- Start recruiting alpha users

**Next 2 Months (8 weeks):**
- Complete MVP + Unified Dashboard (Weeks 1-8)
- All 6 dashboard tabs functional (Launch, Terminals, Kanban, Live, Historical, Global)
- Begin Build Kits development (Weeks 9-10 start)
- Hit 100 users, 20+ projects

**Next 6 Months:**
- Launch Build Kits marketplace (Weeks 9-16 foundation)
- Reach $50K GMV
- 500 users, 50 Build Kits

**Next 12 Months:**
- Federation (Weeks 25-32)
- Scale to enterprise (Weeks 33-48)
- 1,000 users, $100K GMV, enterprise pilots

### Key Success Factors

**Technical Excellence:**
- ✅ Core coordination works reliably
- ✅ Build Kits maintain quality
- ✅ Marketplace is trustworthy
- ✅ System scales smoothly

**Community Building:**
- ✅ Creators feel valued
- ✅ Contributors are engaged
- ✅ Users are evangelists
- ✅ Network effects compound

**Execution Speed:**
- ✅ Ship fast, iterate faster
- ✅ Validate at each stage
- ✅ Learn from users continuously
- ✅ Pivot when data says to

**Resource Management:**
- ✅ Runway sufficient for 18+ months
- ✅ Team scales with validation
- ✅ Exit ramps prevent over-investment
- ✅ Focus on what matters

---

## Appendix: Reference Documents

**Strategic Documents:**
- marcus-validation.md (Staged validation roadmap)
- MARCUS_STRATEGIC_FRAMEWORK.md (Long-term vision)
- MARCUS_EXECUTIVE_SUMMARY.md (One-pager)

**Technical Documents:**
- DEVELOPMENT_GUIDE.md (Implementation guide)
- docs/implementation/WEEK_*_PLAN.md (Week-by-week plans)
- docs/CATO/CATO_MCP_INTEGRATION_PLAN.md (Dashboard integration)

**API Documentation:**
- docs/api/README.md (MCP tools)
- docs/api/buildkits.md (Build Kits API)
- docs/api/marketplace.md (Marketplace API)

---

**Last Updated:** November 2025
**Version:** 1.0
**Status:** READY FOR EXECUTION

**Let's build the infrastructure for the agent economy. 🚀**
