# Architecture Decision Records (ADRs)

This directory contains Architecture Decision Records for the Marcus project. These documents capture the key architectural decisions made during development, including the context, decision, consequences, and alternatives considered.

## What is an ADR?

An Architecture Decision Record (ADR) is a document that captures an important architectural decision made along with its context and consequences. ADRs help teams:

- Understand why decisions were made
- Onboard new team members
- Revisit decisions when circumstances change
- Learn from past choices

## ADR Format

Each ADR follows this structure:

1. **Title**: Brief description of the decision
2. **Status**: Accepted, Deprecated, Superseded, etc.
3. **Date**: When the decision was made
4. **Context**: Problem being solved and requirements
5. **Decision**: The architectural decision and approach
6. **Consequences**: Positive and negative impacts
7. **Implementation Details**: How it's implemented
8. **Alternatives Considered**: Other options and why they weren't chosen
9. **Related Decisions**: Links to related ADRs
10. **References**: External resources

## Marcus ADRs

### Core Architecture

#### [ADR-0001: Layered Architecture with Domain-Driven Design](./0001-layered-architecture-with-ddd.md)
**Status:** Accepted | **Date:** 2024-11

Establishes Marcus's foundational architecture pattern: a six-layer architecture (Presentation, Application, Domain, Integration, Infrastructure, Monitoring) with DDD principles.

**Key Points:**
- Clear separation of concerns across layers
- Rich domain models with encapsulated business logic
- Bounded contexts for different subsystems
- Dependency inversion (outer layers depend on inner)

**Impact:** ✅ High testability, maintainability, extensibility

---

#### [ADR-0002: Event-Driven Communication Between Components](./0002-event-driven-communication.md)
**Status:** Accepted | **Date:** 2024-11

Implements publish/subscribe pattern for loose coupling and audit trail.

**Key Points:**
- Central event bus for component communication
- Domain events (TaskCreated, TaskAssigned, etc.)
- Optional event persistence for audit and analysis
- Multiple subscribers per event type

**Impact:** ✅ Loose coupling, audit trail, extensibility | ⚠️ Debugging complexity

---

#### [ADR-0003: Project Switching with Isolated State Management](./0003-multi-project-support.md)
**Status:** Accepted | **Date:** 2024-11

Enables managing multiple project configurations with fast switching between them. **Only ONE project is active at a time.**

**Key Points:**
- ProjectContextManager with LRU cache (stores up to 10 project contexts)
- Each project has isolated state (loaded when made active)
- Project discovery from Kanban boards and local history
- Fast context switching (~5-10ms)
- Single `active_project_id` variable (not concurrent projects)

**Impact:** ✅ Project management, fast switching, state isolation | ⚠️ Cannot run multiple projects concurrently

---

#### [ADR-0004: Async-First Design with AsyncIO](./0004-async-first-design.md)
**Status:** Accepted | **Date:** 2024-11

All I/O operations use Python's AsyncIO for non-blocking concurrency.

**Key Points:**
- Async throughout the stack (MCP tools → integrations)
- `asyncio.gather()` for parallel operations
- Event loop-safe locks (asyncio.Lock)
- aiosqlite for async database access

**Impact:** ✅ 10x performance improvements, concurrent agents | ⚠️ Learning curve, complexity

---

### Persistence & Data

#### [ADR-0008: SQLite as Primary Persistence Layer](./0008-sqlite-primary-persistence.md)
**Status:** Accepted | **Date:** 2024-11

SQLite as single source of truth, replacing dual SQLite+JSON persistence.

**Key Points:**
- All data in one SQLite database (tasks, assignments, events, conversations, decisions, artifacts)
- WAL mode for better concurrent access
- Comprehensive indices for query performance
- JSON fields for flexible schemas

**Impact:** ✅ Single source of truth, powerful queries, ACID | ⚠️ Write concurrency limits (single writer)

---

### Error Handling & Resilience

#### [ADR-0010: Comprehensive Error Handling Framework](./0010-comprehensive-error-framework.md)
**Status:** Accepted | **Date:** 2024-11

Four-layer error framework: exceptions, strategies, responses, monitoring.

**Key Points:**
- Custom exception hierarchy (MarcusBaseError)
- Error strategies: retry with exponential backoff, circuit breaker, fallback
- Structured error responses for MCP tools
- Error monitoring and spike detection

**Impact:** ✅ Clear errors, automatic recovery, better monitoring | ⚠️ Increased complexity

---

## ADR Index by Topic

### Architecture Patterns
- [ADR-0001: Layered Architecture with DDD](./0001-layered-architecture-with-ddd.md)
- [ADR-0002: Event-Driven Communication](./0002-event-driven-communication.md)

### Scalability & Performance
- [ADR-0003: Project Switching](./0003-multi-project-support.md)
- [ADR-0004: Async-First Design](./0004-async-first-design.md)

### Data & Persistence
- [ADR-0008: SQLite as Primary Persistence](./0008-sqlite-primary-persistence.md)

### Reliability & Error Handling
- [ADR-0010: Comprehensive Error Framework](./0010-comprehensive-error-framework.md)

### Task Graph & Coordination
- [ADR-0011: Graph Augmenter Protocol](./0011-graph-augmenter-protocol.md)
- [ADR-0012: Session-Model Migration](./0012-session-model-migration.md)
- [ADR-0013: The Coordination Kernel and Its Extensions](./0013-kernel-and-extensions.md)

### Future ADRs (Planned)

The following topics are referenced in existing documentation but not yet written.
Numbers are assigned when an ADR is written, not reserved in advance: the list below
previously assigned 0011, 0012 and 0013 to topics that were never written under those
numbers, which collided with the ADRs that were.

- NLP pipeline architecture
- Kanban provider abstraction (Planka, GitHub, Linear)
- AI/LLM provider abstraction (OpenAI, Anthropic, Ollama)
- Post-project analysis system
- Memory system architecture (multi-tier caching)
- Task dependency graph management
- Lease-based task assignment (partly decided in ADR-0012 D3 and D11)
- Agent workspace isolation (partly decided in ADR-0012 D1)
- MCP protocol integration

## How to Use This Documentation

### For New Contributors

1. **Start with** [ADR-0001 (Layered Architecture)](./0001-layered-architecture-with-ddd.md) to understand the overall structure
2. **Read** [ADR-0002 (Event-Driven)](./0002-event-driven-communication.md) to understand component communication
3. **Browse** other ADRs based on the area you're working on

### For Feature Development

Before implementing a feature:

1. Check if related ADRs exist
2. Understand the constraints and patterns
3. Follow established patterns
4. Consider if your feature needs a new ADR

### For Debugging

1. Check ADRs for the subsystem you're debugging
2. Understand the intended behavior
3. Review "Consequences" sections for known limitations
4. Check "Common Pitfalls" sections

### For Architecture Reviews

1. Review all ADRs in the affected area
2. Check if the change aligns with existing decisions
3. Consider if alternatives in ADRs are now more suitable
4. Document new decisions as ADRs

## Creating New ADRs

### When to Create an ADR

Create an ADR when making decisions about:

- System architecture or structure
- Technology choices (frameworks, libraries, databases)
- Integration approaches
- Performance/scalability strategies
- Security patterns
- Major refactorings

### ADR Template

```markdown
# ADR XXXX: [Decision Title]

**Status:** [Proposed/Accepted/Deprecated/Superseded]

**Date:** YYYY-MM

**Deciders:** [Team/Individual]

---

## Context

[What is the problem? What are the requirements? What constraints exist?]

---

## Decision

[What is the architectural decision? How will it work?]

---

## Consequences

### Positive

✅ [Benefits]

### Negative

⚠️ [Drawbacks and trade-offs]

---

## Implementation Details

[How is this implemented? Code examples? Patterns?]

---

## Alternatives Considered

### 1. [Alternative Name]
**Rejected** because: [reasons]

**When to Reconsider:** [conditions]

---

## Related Decisions

- [Links to related ADRs]

---

## References

- [External links, books, articles]

---

## Notes

[Additional context, metrics, lessons learned]
```

### ADR Numbering

- Core architecture: 0001-0010
- Integrations: 0011-0020
- Features: 0021-0030
- Infrastructure: 0031-0040
- Reserved: 0041+

## Contributing

When updating ADRs:

1. **Never delete ADRs** - Mark as deprecated/superseded instead
2. **Update status** when decisions change
3. **Add "Superseded by"** links when replacing
4. **Keep historical context** - don't rewrite history
5. **Link related ADRs** - create a web of knowledge

## References

- [ADR GitHub Organization](https://adr.github.io/)
- [Michael Nygard's ADR Article](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
- [ADR Tools](https://github.com/npryce/adr-tools)

---

## Metrics

- **Total ADRs:** 6 (5 core + 1 planned batch)
- **Acceptance Rate:** 100%
- **Average Age:** 1-2 months
- **Active Areas:** Architecture (2), Performance (2), Data (1), Reliability (1)

---

**Note:** This is a living document. As Marcus evolves, new ADRs will be added and existing ones may be superseded. Always check the status before relying on a decision.
