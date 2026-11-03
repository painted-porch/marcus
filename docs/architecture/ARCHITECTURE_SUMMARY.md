# Marcus Architecture Summary

A quick reference guide to the Marcus architecture for creating additional C4 diagrams and sequence diagrams.

## Key Metrics

- **Codebase Size:** ~89,500 lines of Python
- **Files:** 202 Python modules
- **Python Version:** 3.11+
- **Test Coverage:** Minimum 80%
- **Core Modules:** 33 main packages/subsystems

## Architecture Pattern: Layered + Event-Driven + Domain-Driven

### Layer 1: Presentation (MCP Server)
**Location:** `src/marcus_mcp/`

Exposes 30+ tools via Model Context Protocol:
- HTTP, Stdio, Multi-endpoint transport modes
- Tool handler dispatch and serialization
- Request validation and response formatting
- Authentication middleware

**Key Classes:** `MarcusServer`, Tool handlers

### Layer 2: Application/Orchestration
**Location:** `src/workflow/`, `src/orchestration/`, `src/modes/`

Coordinates multi-step processes:
- Project creation and lifecycle management
- Task assignment workflows
- Auto-assignment loops
- Continuous monitoring and updates
- Three operational modes (adaptive, creator, enricher)

**Key Classes:** `ProjectWorkflowManager`, Mode implementations

### Layer 3: Domain (Core Business Logic)
**Location:** `src/core/`

Rich domain models and business logic:
- **Models:** Task, ProjectState, WorkerStatus, Decision, BlockerReport, ProjectRisk
- **Context System:** Rich context for assignments, previous implementations, dependencies
- **Memory System:** Multi-tier learning from past executions
- **Events:** Publish/subscribe with optional persistence
- **Project Management:** Project switching with isolated state (single active project)
- **Assignment:** Lease-based task assignment with timeout management
- **Error Handling:** Comprehensive error hierarchy with recovery strategies
- **Persistence:** File and SQLite backends

**Key Classes:**
- `Task`, `ProjectState`, `WorkerStatus`
- `Context`, `ProjectContextManager`
- `Memory`, `AgentProfile`
- `Events`, `Event`
- `AssignmentLease`, `AssignmentPersistence`
- `MarcusBaseError` and subclasses

### Layer 4: Integration (External Systems)
**Location:** `src/integrations/`, `src/ai/`

Abstracts external dependencies:
- **Kanban:** Abstract interface with Planka, GitHub, Linear implementations
- **AI/LLM:** Abstract providers for Anthropic, OpenAI, Local (Ollama)
- **NLP:** Task parsing and semantic analysis
- **Cost Tracking:** Token counting and budget management

**Key Classes:**
- `KanbanInterface`, `KanbanFactory`
- `BaseProvider`, provider implementations
- `AIAnalysisEngine`
- `TokenTracker`

### Layer 5: Infrastructure (Persistence, Logging, Events)
**Location:** `src/logging/`, `src/communication/`, `src/cost_tracking/`, `src/persistence/`

System-level services:
- **Persistence:** FilePersistence (JSON), SQLite backend
- **Logging:** Conversation tracking, agent interaction history
- **Communication:** Message routing, component discovery
- **Events:** Event bus with optional disk persistence

**Key Classes:**
- `Persistence`, `FilePersistence`
- `ConversationLogger`
- `CommunicationHub`
- `Events`, `Event`

### Layer 6: Monitoring & Analytics
**Location:** `src/monitoring/`, `src/visualization/`

Real-time system health and learning:
- **Monitoring:** Project health, task assignments, error prediction
- **Experiments:** MLflow-based experiment tracking
- **Visualization:** Event-driven pipeline visualization, bottleneck detection

**Key Classes:**
- `ProjectMonitor`, `AssignmentMonitor`
- `PipelineFlowManager`, visualizers

## Data Flow

### Primary Flows

1. **Project Creation Flow**
   ```
   NLP Parse → Task Generator → Kanban Integration → Project Registry
   ```

2. **Task Assignment Flow**
   ```
   Get Available → Analyze → Score Agents → Create Lease → Build Context → Assign
   ```

3. **Task Completion Flow**
   ```
   Report Status → Update Kanban → Store Artifacts → Update Memory → Monitor Update
   ```

4. **Event Flow**
   ```
   Publish Event → Store History → Notify Subscribers → Broadcast to Visualization
   ```

## Key Patterns

| Pattern | Location | Purpose |
|---------|----------|---------|
| **Factory** | Kanban, AI providers | Create implementations |
| **Strategy** | Error strategies, AI | Pluggable algorithms |
| **Observer** | Events system | Publish/subscribe |
| **Adapter** | Kanban, NLP | System integration |
| **Registry** | Projects, Services | Discovery |
| **LRU** | ProjectContextManager | Resource management |
| **Lease** | Assignment | Concurrency control |
| **Circuit Breaker** | Error strategies | Fault tolerance |

## Domain Models (Critical)

### Task
- Represents work item with dependencies
- Status: todo, in_progress, done, blocked
- Priority: low, medium, high, urgent
- Supports hierarchical (subtask) and cross-parent dependencies

### ProjectState
- Snapshot of project health
- Tracks velocity, risk level, overdue tasks
- Primary metrics for monitoring

### WorkerStatus
- Agent capabilities and performance
- Learned profiles from execution history
- Skill tracking and success rates

### Decision
- Architectural decisions with rationale
- Links to affected tasks
- Stores for knowledge base

### Event
- Base structure for all system events
- Pub/sub distribution
- Optional persistence

## Error Handling Strategy

**Marcus Error Framework:**
- User-facing errors inherit from `MarcusBaseError`
- Transient errors use retry with backoff
- Integration errors use circuit breaker
- Configuration errors fail fast (no retry)
- Security errors bypass retry
- Context injection for automatic metadata

## Project Management (Single Active Project)

**IMPORTANT:** Only ONE project active at a time. Switching replaces the active project.

**ProjectContextManager:**
- LRU cache (max 10 projects) for fast switching
- Auto-cleanup on idle (30 minutes)
- Isolated state per project (loaded when active)
- Automatic kanban client lifecycle
- Async-safe context switching (~5-10ms)
- Single `active_project_id` variable

## Testing Strategy

**Organization:**
- Unit tests: `tests/unit/` (fast, isolated)
- Integration tests: `tests/integration/` (requires services)
- Future features: `tests/future_features/` (TDD)
- Performance: `tests/performance/`

**Requirements:**
- 80% minimum coverage
- Unit tests < 100ms
- Mock all external dependencies
- Async test support with `@pytest.mark.asyncio`

## Configuration Management

**Sources (priority order):**
1. Environment variables (MARCUS_CONFIG env var)
2. Config file in current directory
3. Config file in project root
4. Config file in user home (~/.marcus/)

**Format:**
- JSON-based
- Multiple project configs (one active at a time)
- Provider-specific settings
- AI provider credentials
- Kanban board configuration

## Deployment Options

**Docker Compose (Recommended):**
- PostgreSQL (database)
- Planka (Kanban board)
- Marcus app

**Transport Modes:**
- HTTP (default, port 4298)
- Stdio (direct terminal)
- Multi-endpoint (separate human/agent/analytics)

## Technology Stack

**Core:**
- Python 3.11+
- asyncio (all I/O)
- mypy strict mode
- pydantic (validation)

**Web & Protocol:**
- aiohttp, httpx
- MCP SDK (Anthropic)

**AI Integration:**
- anthropic-sdk
- openai
- (local via Ollama)

**Data:**
- SQLite
- aiofiles (async file I/O)
- JSON

**Tools:**
- black, isort, ruff (code quality)
- pytest, pytest-asyncio (testing)
- mlflow (experiment tracking)

## Key Files by Purpose

### Entry Points
- `/marcus` - CLI entrypoint
- `src/marcus_mcp/server.py` - MCP server

### Core Domain
- `src/core/models.py` - Data models
- `src/core/context.py` - Context system
- `src/core/events.py` - Event system
- `src/core/memory.py` - Learning system
- `src/core/error_framework.py` - Error handling

### Integration
- `src/integrations/kanban_interface.py` - Kanban abstraction
- `src/ai/providers/` - AI provider implementations
- `src/integrations/ai_analysis_engine.py` - AI analysis

### Configuration
- `src/config/config_loader.py` - Configuration loading
- `src/config/settings.py` - Settings management

### Tools
- `src/marcus_mcp/tools/task.py` - Task management tools
- `src/marcus_mcp/tools/agent.py` - Agent coordination
- `src/marcus_mcp/tools/analytics.py` - Analytics tools

### Persistence
- `src/core/persistence.py` - Storage abstraction
- `src/core/project_history.py` - Decision/artifact storage
- `src/logging/conversation_logger.py` - Conversation history

### Monitoring
- `src/monitoring/project_monitor.py` - Project health

## Architectural Principles

1. **Separation of Concerns** - Clear layer boundaries
2. **Dependency Injection** - Components receive dependencies
3. **Event-Driven** - Loose coupling via pub/sub
4. **Provider Pattern** - Easy addition of new implementations
5. **Async-First** - All I/O non-blocking
6. **Domain-Driven** - Rich domain models
7. **Multi-Tenant Ready** - Per-project isolation
8. **Error Resilience** - Retry strategies and recovery

## Critical Dependencies

- **asyncio** - Foundation of async architecture
- **aiofiles** - Async file I/O
- **SQLite** - Primary persistence
- **pydantic** - Validation and serialization
- **mcp-sdk** - Model Context Protocol support
- **anthropic-sdk** - Claude API integration

## Security Considerations

- API key management via environment variables
- Workspace isolation per agent
- Forbidden path enforcement
- Audit logging of operations
- Authentication middleware in MCP server

---

This summary should be sufficient for creating additional architecture diagrams.
