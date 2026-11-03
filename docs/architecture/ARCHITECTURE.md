# Marcus Architecture Documentation

**Project:** Multi-Agent Resource Coordination and Understanding System (Marcus)
**Codebase Size:** ~89,500 lines of Python across 202 files
**Python Version:** 3.11+
**Current Branch:** feature/post-project-analysis-phase1

---

## 1. OVERALL ARCHITECTURE PATTERN

Marcus follows a **Layered Architecture with Domain-Driven Design (DDD)** principles, combined with an **event-driven system** for inter-component communication.

### Key Architectural Characteristics:

1. **Layered Organization:**
   - Presentation/Interface Layer (MCP Server, CLI)
   - Application/Orchestration Layer (Workflows, Task Management)
   - Domain/Core Layer (Models, Business Logic)
   - Integration Layer (Kanban providers, AI providers)
   - Infrastructure Layer (Persistence, Events, Logging)

2. **Event-Driven Communication:**
   - Loose coupling between components via event publish/subscribe
   - Event history tracking for audit trails
   - Optional persistence for long-term event storage

3. **Project Management:**
   - Project switching with isolated state (one active project at a time)
   - Project Context Manager for managing multiple project configurations
   - LRU caching for fast project switching

4. **Asynchronous-First Design:**
   - All I/O operations are async
   - Event loop-safe locks for concurrent operations
   - AsyncIO-based scheduling and monitoring

---

## 2. CORE COMPONENTS AND LAYERS

### 2.1 **Entry Points & Infrastructure**

```
marcus (CLI entry point)
├── src.marcus_mcp.server (MCP Server)
│   ├── MarcusServer initialization
│   ├── Tool group registration
│   └── Transport modes (HTTP, stdio, multi-endpoint)
└── src.config.config_loader (Configuration Management)
    └── Supports environment variable overrides
```

**Key Files:**
- `/Users/lwgray/dev/marcus/marcus` - CLI entrypoint
- `/Users/lwgray/dev/marcus/src/marcus_mcp/server.py` - MCP server implementation
- `/Users/lwgray/dev/marcus/src/config/config_loader.py` - Configuration loader
- `/Users/lwgray/dev/marcus/src/config/settings.py` - Settings management

### 2.2 **Core Domain Layer** (`src/core/`)

The heart of Marcus containing fundamental data structures and business logic.

```
src/core/
├── models.py ...................... Domain entities (Task, Agent, ProjectState)
├── context.py ..................... Context system for task assignments
├── events.py ...................... Event distribution system
├── memory.py ...................... Multi-tier memory system
├── persistence.py ................. Storage abstraction (file & SQLite)
├── project_history.py ............. Project execution history (decisions, artifacts)
├── project_context_manager.py ..... Multi-project state management
├── project_registry.py ............ Project discovery and registration
├── service_registry.py ............ Service advertisement for discovery
│
├── error_framework.py ............. Marcus error hierarchy
├── error_strategies.py ............ Retry, circuit breaker, fallback patterns
├── error_responses.py ............. Error serialization for MCP tools
├── error_monitoring.py ............ Error tracking and analytics
│
├── assignment_lease.py ............ Lease-based task assignment with timeout
├── assignment_persistence.py ...... Task assignment storage
├── assignment_reconciliation.py ... Reconcile assignments with Kanban board
│
├── events.py ...................... Event-driven publish/subscribe
├── task_diagnostics.py ............ Task execution debugging
├── task_recovery.py ............... Failed task recovery mechanisms
├── task_graph_validator.py ........ Dependency graph validation
│
├── adaptive_dependencies.py ....... Dynamic dependency resolution
├── phase_dependency_enforcer.py ... Phase-based task ordering
├── workspace.py ................... Agent workspace isolation
├── code_analyzer.py ............... Code quality analysis
└── models/
    └── task_execution_order.py .... Task ordering strategies
```

**Key Data Models:**
- `Task` - Work item with dependencies, priority, and status
- `ProjectState` - Snapshot of entire project health
- `WorkerStatus` - Agent capabilities and performance metrics
- `TaskAssignment` - Task-to-agent binding with instructions
- `BlockerReport` - Impediment tracking with severity
- `ProjectRisk` - Risk assessment and mitigation
- `Decision` - Architectural decisions with rationale
- `Event` - Base event structure with metadata

### 2.3 **Integration Layer** (`src/integrations/`)

Abstracts external systems behind interfaces for maximum flexibility.

```
src/integrations/
├── kanban_interface.py ............ Abstract base for Kanban providers
├── kanban_factory.py .............. Factory for creating Kanban clients
│
├── providers/
│   ├── planka.py .................. Planka Kanban implementation
│   ├── planka_kanban.py ........... Enhanced Planka support
│   ├── github_kanban.py ........... GitHub Projects integration
│   ├── linear_kanban.py ........... Linear integration
│   └── github_mcp_interface.py .... GitHub MCP bridge
│
├── ai_analysis_engine.py .......... AI-powered analysis and suggestions
├── nlp_base.py .................... NLP foundation
├── nlp_tools.py ................... NLP utilities
├── nlp_task_utils.py .............. Task parsing via NLP
├── enhanced_task_classifier.py .... Smart task categorization
├── adaptive_documentation.py ...... Auto-generated documentation
└── project_auto_setup.py .......... Automatic project initialization
```

**Kanban Interface Pattern:**
- Abstract methods for all CRUD operations
- Provider enumeration for easy switching
- Standardized Task/Board representation

### 2.4 **AI/ML Layer** (`src/ai/`)

Intelligent decision-making and analysis.

```
src/ai/
├── types.py ....................... Shared AI data classes
├── core/
│   └── ai_engine.py ............... Base AI integration
│
├── providers/
│   ├── base_provider.py ........... Abstract provider interface
│   ├── anthropic_provider.py ....... Anthropic Claude integration
│   ├── openai_provider.py ......... OpenAI integration
│   ├── local_provider.py .......... Local LLM via Ollama
│   └── llm_abstraction.py ......... Unified LLM interface
│
├── advanced/
│   └── prd/
│       └── parser/ ............... Product requirement parsing
│
├── decisions/ .................... Decision-making logic
├── enrichment/ ................... Data enrichment via AI
├── learning/ ..................... Learning mechanisms
└── core/ ......................... Core AI utilities
```

**AI Abstraction Pattern:**
- Provider-agnostic interface
- Token tracking and cost monitoring
- Fallback strategies for API failures

### 2.5 **Orchestration & Workflow** (`src/orchestration/`, `src/workflow/`)

Coordinates multi-step processes and task execution.

```
src/workflow/
└── project_workflow.py ............ Complete project workflow orchestration

src/orchestration/
└── (hybrid_tools.py) .............. Workflow utilities
```

**Workflow Features:**
- Auto-assignment loops
- Continuous monitoring
- Event-driven status updates

### 2.6 **MCP Server & Tools** (`src/marcus_mcp/`)

The public API exposed to AI agents via Model Context Protocol.

```
src/marcus_mcp/
├── server.py ...................... MCP server implementation
├── handlers/ ...................... Tool call handlers
├── coordinator/ ................... Agent coordination logic
│
└── tools/
    ├── __init__.py ................ Tool registration
    ├── task.py .................... Task management tools
    ├── project_stall_analyzer.py .. Detect project bottlenecks
    ├── agent.py ................... Agent registration and management
    ├── auth.py .................... Authentication tools
    ├── system.py .................. System information tools
    ├── context.py ................. Context retrieval tools
    ├── experiments.py ............. Experiment tracking tools
    ├── analytics.py ............... Analytics and reporting
    ├── predictions.py ............. Predictive analysis
    ├── diagnostics.py ............. Debugging and diagnostics
    ├── board_health.py ............ Board health monitoring
    ├── code_metrics.py ............ Code quality metrics
    ├── nlp.py ..................... NLP-based task creation
    ├── audit_tools.py ............. Audit and compliance
    ├── attachment.py .............. Artifact management
    ├── pipeline.py ................ Pipeline visualization
    └── scheduling.py .............. Task scheduling
```

**Tool Categories:**
- Task/Project Management (20+ tools)
- Agent Coordination (5+ tools)
- Analytics & Monitoring (10+ tools)
- System & Diagnostics (8+ tools)
- Experiment Tracking (MLflow integration)

### 2.7 **Monitoring & Analysis** (`src/monitoring/`, `src/analysis/`)

Real-time system health and performance tracking.

```
src/monitoring/
├── project_monitor.py ............ Project health metrics
├── assignment_monitor.py ......... Task assignment tracking
├── error_predictor.py ............ Predict task failures
└── live_pipeline_monitor.py ...... Real-time pipeline visualization

src/analysis/
├── query_api.py .................. Query interface
└── (various analysis utilities)
```

### 2.8 **Visualization** (`src/visualization/`)

Event-driven visualization and pipeline monitoring.

```
src/visualization/
├── pipeline_manager.py ........... Pipeline flow visualization
├── event_integrated_visualizer.py  Event-based visuals
├── shared_pipeline_events.py ..... Event sharing
├── causal_analyzer.py ............ Causal flow analysis
├── pipeline_flow.py .............. Flow diagrams
├── stall_dashboard.py ............ Bottleneck detection
├── pipeline_replay.py ............ Execution replay
└── conversation_adapter.py ....... Agent conversation bridge
```

### 2.10 **Modes System** (`src/modes/`)

Different operational modes for Marcus.

```
src/modes/
├── adaptive/ ..................... Adaptive mode (learns from execution)
├── creator/ ...................... Creator mode (task generation)
├── enricher/ ..................... Enricher mode (context enhancement)
└── mode_registry.py .............. Mode discovery and loading
```

### 2.11 **Cross-Cutting Concerns**

#### **Logging** (`src/logging/`)
```
src/logging/
├── conversation_logger.py ........ Comprehensive conversation tracking
└── conversation/ ................. Conversation utilities
```
- Tracks all agent-system interactions
- Stores conversation context for learning
- Searchable conversation history

#### **Communication** (`src/communication/`)
```
src/communication/
└── communication_hub.py .......... Inter-component messaging
```
- Central message routing
- Component discovery
- Message serialization

#### **Cost Tracking** (`src/cost_tracking/`)
```
src/cost_tracking/
├── token_tracker.py .............. Token counting and cost calculation
└── ai_usage_middleware.py ........ AI API usage middleware
```
- Per-request token tracking
- Cost attribution by task
- Budget monitoring

#### **Learning & Intelligence** (`src/learning/`, `src/intelligence/`)
```
src/learning/
├── (pattern learning)
└── pattern_learning_init.py

src/intelligence/
└── (dependency inference and smart analysis)
```

### 2.12 **Persistence Layer**

**Dual Persistence Strategy:**

1. **File-based** (`FilePersistence`)
   - JSON storage under `data/marcus_state/`
   - Workspace isolation per project
   - Collection-based organization

2. **SQLite** (Primary for Phase 1)
   - Direct SQLite for structured data
   - ACID transactions
   - Efficient querying

**Storage Locations:**
```
/Users/lwgray/dev/marcus/
├── data/
│   ├── marcus_state/ ............. File-based collections
│   ├── project_history/ .......... Per-project decision/artifact records
│   └── workspaces/ ............... Agent workspace isolation
├── mlruns/ ....................... MLflow experiment tracking
├── logs/ .......................... Application logs
└── marcus.db ..................... SQLite database
```

---

## 3. KEY DOMAIN MODELS

### 3.1 **Task Model**

```python
@dataclass
class Task:
    id: str                              # Unique task identifier
    name: str                            # Human-readable name
    description: str                     # Detailed description
    status: TaskStatus                   # todo | in_progress | done | blocked
    priority: Priority                   # low | medium | high | urgent
    assigned_to: Optional[str]           # Agent ID
    created_at: datetime                 # Creation timestamp
    updated_at: datetime                 # Last modification
    due_date: Optional[datetime]         # Target completion
    estimated_hours: float               # Time estimate
    actual_hours: float = 0.0            # Actual time spent
    dependencies: List[str] = []         # Task IDs this depends on
    labels: List[str] = []               # Task categories
    project_id: Optional[str] = None     # Project context
    project_name: Optional[str] = None

    # Generalization fields
    source_type: Optional[str] = None    # Where task came from
    source_context: Optional[Dict] = None
    completion_criteria: Optional[Dict] = None
    validation_spec: Optional[str] = None

    # Unified dependency graph
    is_subtask: bool = False             # Hierarchical task relationships
    parent_task_id: Optional[str] = None
    subtask_index: Optional[int] = None

    # Cross-parent dependency wiring
    provides: Optional[str] = None       # Interface this task provides
    requires: Optional[str] = None       # Interface this task needs
```

### 3.2 **Project State Model**

```python
@dataclass
class ProjectState:
    board_id: str                        # Kanban board ID
    project_name: str                    # Project name
    total_tasks: int                     # Total task count
    completed_tasks: int                 # Done count
    in_progress_tasks: int               # Active count
    blocked_tasks: int                   # Blocked count
    progress_percent: float              # Completion percentage
    overdue_tasks: List[Task]            # Late tasks
    team_velocity: float                 # Tasks/time period
    risk_level: RiskLevel                # Overall risk
    last_updated: datetime               # Last sync time
```

### 3.3 **Agent/Worker Model**

```python
@dataclass
class WorkerStatus:
    worker_id: str                       # Agent identifier
    name: str                            # Display name
    role: str                            # Specialization
    email: Optional[str]                 # Contact info
    current_tasks: List[Task]            # Assigned tasks
    completed_tasks_count: int           # Success count
    capacity: int                        # Hours/week available
    skills: List[str]                    # Technical competencies
    availability: Dict[str, bool]        # Schedule
    performance_score: float = 1.0       # Relative performance
```

### 3.4 **Decision & Artifact Models**

```python
@dataclass
class Decision:
    decision_id: str
    task_id: str
    agent_id: str
    timestamp: datetime
    what: str                            # Choice made
    why: str                             # Rationale
    impact: str                          # Effect on system
    affected_tasks: List[str] = []       # Related tasks
    confidence: float = 0.8              # Agent confidence
    kanban_comment_url: Optional[str] = None
    project_id: Optional[str] = None

@dataclass
class ArtifactMetadata:
    artifact_id: str
    task_id: str
    agent_id: str
    timestamp: datetime
    artifact_type: str                   # code | design | documentation
    location: str                        # File path or URL
    description: Optional[str] = None
    size_bytes: Optional[int] = None
```

---

## 4. INTEGRATION POINTS

### 4.1 **Kanban Board Integrations**

**Supported Providers:**
- Planka (self-hosted)
- GitHub Projects
- Linear
- (Extensible pattern for others)

**Kanban Interface Pattern:**

```python
class KanbanInterface(ABC):
    @abstractmethod
    async def connect(self) -> bool: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def get_available_tasks(self) -> List[Task]: ...

    @abstractmethod
    async def get_all_tasks(self) -> List[Task]: ...

    @abstractmethod
    async def assign_task(self, task_id: str, agent_id: str) -> bool: ...

    @abstractmethod
    async def update_task(self, task: Task) -> bool: ...

    @abstractmethod
    async def add_comment(self, task_id: str, comment: str) -> None: ...
```

**Factory Pattern:**
```python
KanbanFactory.create(provider_type, config) -> KanbanInterface
```

### 4.2 **AI/LLM Integrations**

**Supported Providers:**
- Anthropic Claude (Primary)
- OpenAI (ChatGPT, GPT-4)
- Local models via Ollama
- (Extensible pattern for others)

**LLM Interface:**
```python
class BaseProvider(ABC):
    async def generate(self, prompt: str, **kwargs) -> str: ...
    async def analyze_task(self, task: Task) -> AIInsights: ...
    async def suggest_assignment(self, context: AnalysisContext) -> AssignmentDecision: ...
```

**Cost Tracking:**
- Per-request token counting
- Cost attribution by task/agent
- Budget monitoring and alerts

### 4.3 **MCP Protocol Integration**

**Protocol:** Model Context Protocol (Anthropic)

**Transport Modes:**
- HTTP (default)
- Stdio (for direct terminal integration)
- Multi-endpoint (separate human/agent/analytics endpoints)

**Tool Endpoints:**
- 30+ specialized MCP tools
- Request/response serialization
- Error handling with Marcus framework

---

## 5. DATA FLOW ARCHITECTURE

### 5.1 **Request → Processing → Response Flow**

```
User/Agent Request (MCP)
    ↓
MCP Server (src/marcus_mcp/server.py)
    ↓
Tool Handler (handlers/)
    ↓
Business Logic (core/, integrations/)
    ↓
Kanban API / AI API / Database
    ↓
Response Serialization
    ↓
MCP Response to Client
```

### 5.2 **Project Lifecycle Flow**

```
1. Project Creation
   ├── Parse natural language description
   ├── Generate task breakdown
   ├── Create Kanban board
   └── Register project in registry

2. Task Assignment
   ├── Get available tasks from Kanban
   ├── Analyze task requirements
   ├── Score candidate agents
   ├── Make assignment decision
   └── Update Kanban + Assignment persistence

3. Task Execution
   ├── Agent pulls task with full context
   ├── Executes task
   ├── Logs artifacts and decisions
   ├── Reports progress/blockers
   └── Marks task complete

4. Context Propagation
   ├── Store decision + artifacts
   ├── Update project history
   ├── Notify dependent tasks
   └── Update agent performance profile

5. Project Completion
   ├── Generate project report
   ├── Archive artifacts
   ├── Store execution analytics
   └── Update agent learning profiles
```

### 5.3 **Event Flow**

```
Event Publication
    ↓
Event Subscribers (if any)
    ↓
Optional: Persist to storage
    ↓
Optional: Broadcast to visualization system
```

**Event Types:**
- `task_assigned`
- `task_started`
- `task_completed`
- `blocker_reported`
- `decision_made`
- `artifact_created`
- `context_updated`
- `project_risk_detected`

---

## 6. KEY WORKFLOWS

### 6.1 **Project Creation Workflow**

```
Input: Natural language description
    ↓
NLP Parser (src/integrations/nlp_task_utils.py)
    ├── Extract requirements
    ├── Identify task boundaries
    └── Infer dependencies
    ↓
Task Generator
    ├── Create Task objects
    ├── Set priorities
    └── Define success criteria
    ↓
Kanban Integration
    ├── Create board
    ├── Add tasks
    └── Configure columns
    ↓
Project Registry
    ├── Store project metadata
    └── Mark as active
    ↓
Output: Project ID + Task List
```

### 6.2 **Task Assignment Workflow**

```
Agent Request: "Get next task"
    ↓
Assignment Manager
    ├── Get available tasks
    ├── Filter by agent skills/capacity
    ├── Score task difficulty
    └── Check dependencies
    ↓
AI Analysis Engine (Optional)
    ├── Analyze task semantics
    ├── Identify hidden risks
    └── Suggest best agent
    ↓
Assignment Lease
    ├── Create lease with timeout
    ├── Lock task for agent
    └── Set lease expiration
    ↓
Context Builder
    ├── Gather previous implementations
    ├── Fetch relevant decisions
    ├── Collect dependent tasks
    └── Build rich context
    ↓
Agent Response
    ├── Task with instructions
    ├── Rich context
    └── Workspace path
```

### 6.3 **Agent Coordination Workflow**

```
Agent Completes Task
    ↓
Report Task Status
    ├── Success/Failure
    ├── Actual hours spent
    ├── Blockers encountered
    └── Artifacts produced
    ↓
Update Systems
    ├── Update Kanban board
    ├── Release assignment lease
    ├── Store metrics in memory
    └── Log decision/artifacts
    ↓
Propagate Context
    ├── Notify dependent tasks
    ├── Update project state
    └── Trigger visualization
    ↓
Learning
    ├── Update agent profile
    ├── Adjust task estimates
    └── Identify patterns
```

### 6.4 **Experiment Tracking Workflow**

```
Experiment Start (MLflow)
    ├── Create run with parameters
    ├── Set tags (agents, complexity)
    └── Initialize metric collectors
    ↓
During Execution
    ├── Track task completion times
    ├── Monitor blocker frequency
    ├── Count artifacts produced
    ├── Record decision made
    └── Log agent performance
    ↓
Experiment End
    ├── Finalize run
    ├── Calculate aggregate metrics
    ├── Generate report
    └── Update learning profiles
```

---

## 7. TECHNOLOGY STACK

### 7.1 **Core Technologies**

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Language** | Python 3.11+ | Application logic |
| **Async** | asyncio | Concurrent operations |
| **Type Safety** | mypy (strict mode) | Type checking at build time |
| **Web** | aiohttp, httpx | HTTP clients/servers |
| **MCP** | mcp-sdk | Model Context Protocol |
| **Database** | SQLite | Structured data persistence |
| **File Storage** | aiofiles, JSON | Document storage |
| **AI Integration** | anthropic, openai | LLM APIs |
| **CLI** | typer, argparse | Command-line interface |
| **Logging** | logging module | Structured logging |
| **Testing** | pytest, pytest-asyncio | Testing framework |
| **Code Quality** | black, isort, ruff | Code formatting |
| **Monitoring** | MLflow | Experiment tracking |
| **Config** | JSON, pydantic | Configuration management |

### 7.2 **Async Patterns**

**Event Loop Compatibility:**
- Custom `EventLoopLockManager` for safe lock creation
- All I/O operations use `async`/`await`
- Background tasks via `asyncio.create_task()`
- Concurrent operations with `asyncio.gather()`

### 7.3 **Testing Approach**

**Test Organization:**
```
tests/
├── unit/                    # Fast, isolated tests
│   ├── core/               # Domain logic
│   ├── ai/                 # AI integration
│   ├── integrations/       # Kanban clients
│   ├── marcus_mcp/         # MCP server
│   └── ... (15+ more)
├── integration/            # Requires external services
│   ├── e2e/               # End-to-end workflows
│   ├── api/               # API endpoints
│   └── external/          # External services
├── future_features/        # TDD tests for planned features
└── performance/           # Performance benchmarks
```

**Coverage Requirements:**
- Minimum 80% code coverage
- Unit tests < 100ms
- Mock all external dependencies
- Async test support with `@pytest.mark.asyncio`

---

## 8. ERROR HANDLING FRAMEWORK

### 8.1 **Error Hierarchy**

```
MarcusBaseError (All Marcus user-facing errors)
├── TransientError (Retry-able)
│   ├── TimeoutError
│   ├── ServiceUnavailableError
│   └── RateLimitError
├── IntegrationError (External service failures)
│   ├── KanbanIntegrationError
│   ├── AIProviderError
│   └── APIError
├── BusinessLogicError (Workflow violations)
│   ├── TaskConflictError
│   ├── DependencyViolationError
│   └── AssignmentError
├── ConfigurationError (Setup issues)
│   ├── MissingCredentialsError
│   ├── InvalidConfigError
│   └── ServiceNotFoundError
├── ResourceError (Resource constraints)
│   ├── MemoryExhaustedError
│   └── DatabaseError
└── SecurityError (Permission/access)
    ├── UnauthorizedAccessError
    └── PermissionDeniedError
```

### 8.2 **Error Strategies**

1. **Retry with Backoff** - Transient errors
2. **Circuit Breaker** - Prevent cascading failures
3. **Fallback** - Use cached/alternative data
4. **Context Injection** - Automatic error metadata
5. **Monitoring** - Error tracking and alerting

---

## 9. PROJECT MANAGEMENT (Single Active Project)

**IMPORTANT:** Marcus operates on **ONE active project at a time**. While multiple project configurations can be stored and managed, only a single project is actively being worked on at any moment. Agents cannot work on different projects simultaneously.

### 9.1 **Project Registry**

Stores and manages multiple project configurations, but enforces single active project:

```
ProjectRegistry
├── discover_projects() - Find all Kanban boards
├── create_project() - Register new project configuration
├── get_active_project() - Get THE active project (only one)
├── set_active_project() - Set which project is active (replaces previous)
└── list_projects() - List all stored project configs
```

**Key:** `_active_project_id` is a **single value**, not a collection.

### 9.2 **Project Context Manager**

Manages switching between projects with LRU caching:

```
ProjectContextManager
├── contexts: OrderedDict[project_id -> ProjectContext]  # LRU cache
├── active_project_id: str  # SINGLE active project
├── active_project_name: str
└── Methods:
    ├── initialize() - Load project registry
    ├── switch_project(id) - REPLACE active project with new one
    ├── get_kanban_client() - Get client for ACTIVE project
    ├── get_context() - Get context of ACTIVE project
    └── cleanup() - Evict inactive projects from cache (LRU)
```

**Key:** `switch_project()` **replaces** the active project, not adds to it.

### 9.3 **Project Context (Cached State)**

Each project has isolated state, loaded into context when made active:

```
ProjectContext
├── project_id
├── kanban_client (loaded when project is active)
├── context (task context for this project)
├── events (project-specific events)
├── project_state (snapshot of project state)
├── assignment_persistence (task assignments for this project)
└── last_accessed (for LRU eviction)
```

**Key:** Contexts are cached for **fast switching**, not for **concurrent execution**.

### 9.4 **Project Switching Flow**

```
User calls: switch_project("project-2")
  ↓
1. Save current project state (if any)
   - Persist project-1 state
   - Close project-1 kanban connection
  ↓
2. Load new project context
   - Get ProjectContext for project-2 (from cache or create)
   - Initialize kanban client for project-2
   - Restore project-2 state
  ↓
3. Update active project
   - self.active_project_id = "project-2"  # REPLACES project-1
   - All subsequent operations use project-2
  ↓
4. LRU cache management
   - Mark project-2 as recently used
   - Evict least recently used projects if cache full
```

### 9.5 **Limitations**

❌ **Cannot do:**
- Run multiple projects simultaneously
- Have Agent A on Project 1 while Agent B works on Project 2
- Execute tasks from different projects concurrently

✅ **Can do:**
- Store multiple project configurations
- Switch between projects without restart (~5-10ms)
- Keep recent projects cached for fast switching
- Isolate project state to prevent cross-contamination

---

## 10. DEPLOYMENT ARCHITECTURE

### 10.1 **Docker Deployment**

```
docker-compose
├── postgres ..................... Database
├── planka ...................... Kanban board
└── marcus ...................... Application server
```

### 10.2 **Transport Modes**

1. **HTTP** (default)
   - Port: 4298
   - Suitable for network communication
   - Standard REST-like interface

2. **Stdio**
   - Direct stdin/stdout
   - No network overhead
   - Suitable for local CLI integration

3. **Multi-Endpoint** (default for v0.1)
   - Human endpoint (port 4298)
   - Agent endpoint (port 4299)
   - Analytics endpoint (port 4300)
   - Separate concerns for different clients

### 10.3 **Process Management**

- CLI daemon management
- PID file tracking
- Log file rotation
- Service discovery registry
- Health monitoring

---

## 11. LEARNING & ADAPTATION

### 11.1 **Multi-Tier Memory System**

```
Memory Tiers:
├── Working Memory (current execution)
├── Episodic Memory (past events)
├── Semantic Memory (patterns & knowledge)
└── Procedural Memory (learned processes)
```

### 11.2 **Learning Mechanisms**

1. **Agent Profiling**
   - Track success rates per agent
   - Identify skill strengths
   - Detect performance trends

2. **Task Pattern Learning**
   - Learn typical duration for task types
   - Identify common blockers
   - Recognize complexity patterns

3. **Dependency Inference**
   - Semantic dependency detection
   - Pattern-based inference
   - Hybrid inference (AI-assisted)

4. **Optimization**
   - Adjust time estimates
   - Improve agent assignments
   - Optimize task sequencing

---

## 12. VISUALIZATION & MONITORING

### 12.1 **Real-Time Dashboards**

- Pipeline flow visualization
- Stall/bottleneck detection
- Causal flow analysis
- Agent workload tracking
- Project health metrics

### 12.2 **Event-Driven Visualization**

- Events trigger visualization updates
- Shared event bus for all visualizers
- Replay capability for debugging
- Conversation integration for context

---

## 13. SECURITY & ISOLATION

### 13.1 **Workspace Isolation**

```
Agent Workspace
├── Allowed paths (for read/write)
├── Forbidden paths (enforced access control)
└── Project-specific directory structure
```

### 13.2 **Access Control**

- Authentication via API keys
- Authorization via role/project
- Audit logging of all operations
- Security error handling (no retry)

---

## 14. CONFIGURATION MANAGEMENT

### 14.1 **Configuration Sources**

1. **Config File** (json)
   - Primary configuration
   - Project-specific settings
   - Provider credentials

2. **Environment Variables**
   - Override config file
   - CI/CD integration
   - Secret management

3. **Runtime Defaults**
   - Sensible defaults
   - Fallback values

### 14.2 **Project Management Configuration**

Store multiple project configurations, with one active at a time:

```json
{
  "projects": [
    {
      "id": "project-1",
      "name": "My App",
      "board_id": "123",
      "provider": "planka"
    },
    {
      "id": "project-2",
      "name": "Other App",
      "board_id": "456",
      "provider": "planka"
    }
  ],
  "active_project": "project-1",  // SINGLE active project at a time
  "providers": {
    "planka": {
      "url": "http://localhost:3000",
      "username": "user",
      "password": "pass"  // pragma: allowlist secret
    }
  },
  "ai": {
    "provider": "anthropic",
    "api_key": "${ANTHROPIC_API_KEY}"
  }
}
```

**Note:** While multiple projects can be stored, only `active_project` is worked on at any moment. Use `switch_project` to change which project is active.

---

## 15. ARCHITECTURAL PRINCIPLES & PATTERNS

### 15.1 **Design Principles**

1. **Separation of Concerns**
   - Domain logic separate from infrastructure
   - Integration layer abstracts external systems
   - Clear layer boundaries

2. **Dependency Injection**
   - Components receive dependencies
   - Easier testing and composition
   - Service registry for discovery

3. **Event-Driven Communication**
   - Loose coupling between systems
   - Publish/subscribe pattern
   - Optional event persistence

4. **Provider Pattern**
   - Multiple Kanban implementations
   - Multiple AI providers
   - Factory for instantiation
   - Easy to add new providers

5. **Async-First Design**
   - All I/O is non-blocking
   - Better resource utilization
   - Handles high concurrency

6. **Domain-Driven Design**
   - Rich domain models (Task, Project, Decision)
   - Domain logic in core layer
   - Bounded contexts per project

### 15.2 **Common Patterns**

| Pattern | Location | Purpose |
|---------|----------|---------|
| **Factory** | KanbanFactory, LLMFactory | Create implementations |
| **Strategy** | Error strategies, AI providers | Pluggable algorithms |
| **Observer** | Events system | Pub/sub communication |
| **Adapter** | Kanban interface, NLP tools | System integration |
| **Decorator** | Error wrapping, middleware | Add behavior |
| **Registry** | ProjectRegistry, ServiceRegistry | Discovery |
| **LRU Cache** | ProjectContextManager | Resource management |
| **Circuit Breaker** | error_strategies | Fault tolerance |
| **Lease/Lock** | assignment_lease | Concurrency control |

---

## SUMMARY

Marcus is a sophisticated multi-agent coordination platform with:
- **Layered architecture** for clear separation of concerns
- **Event-driven design** for loose coupling
- **Pluggable providers** for flexibility (Kanban, AI)
- **Rich domain models** for complex workflows
- **Comprehensive error handling** with retry strategies
- **Project management** with switching and isolated state (single active project)
- **Learning & optimization** from past executions
- **Real-time monitoring** and visualization
- **Type-safe Python** with strict mypy checking
- **Test-driven development** with 80%+ coverage

The architecture supports domain-agnostic coordination of AI agents for any multi-step project while maintaining strong boundaries, error resilience, and learning capabilities.
