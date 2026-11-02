# Configuration Options Reference

Complete reference for all configuration options in `config_marcus.json`.

## Configuration File Location

Marcus searches for `config_marcus.json` in the following order (highest priority first):

1. Path specified in `MARCUS_CONFIG` environment variable
2. Current working directory (`./config_marcus.json`)
3. Project root directory
4. User home directory (`~/.marcus/config_marcus.json`)

## Configuration Structure

| Section | Description |
|---------|-------------|
| [Project Settings](#project-settings) | Legacy single-project configuration |
| [Multi-Project Settings](#multi-project-settings) | Multi-project management |
| [Kanban Integration](#kanban-integration) | Task board provider configuration |
| [AI Configuration](#ai-configuration) | AI/LLM provider settings |
| [Features](#features) | Enable/disable system features |
| [Hybrid Inference](#hybrid-inference) | Dependency inference tuning |
| [Task Lease](#task-lease) | Task assignment lease settings |
| [Board Health](#board-health) | Board health monitoring |
| [Transport](#transport) | MCP transport configuration |
| [Multi-Endpoint](#multi-endpoint) | Multiple MCP endpoints |
| [Monitoring](#monitoring) | System monitoring settings |
| [Communication](#communication) | Slack/Email notifications |
| [Advanced](#advanced) | Debug and advanced options |

---

## Project Settings

**Legacy single-project mode configuration. Automatically migrated to multi-project format.**

| Option | Type | Default | Env Override | Description |
|--------|------|---------|--------------|-------------|
| `project_id` | string | - | - | Planka project ID (legacy) |
| `board_id` | string | - | - | Planka board ID (legacy) |
| `project_name` | string | - | - | Project name (legacy) |
| `board_name` | string | - | - | Board name (legacy) |
| `auto_find_board` | boolean | false | - | Auto-discover board by name |
| `single_project_mode` | boolean | true | - | Legacy single-project mode |

---

## Multi-Project Settings

**Modern multi-project configuration format.**

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `projects` | object | {} | Dictionary of project configurations keyed by project ID |
| `projects.<id>.name` | string | - | Human-readable project name |
| `projects.<id>.provider` | string | - | Provider for this project (planka/github/linear) |
| `projects.<id>.config` | object | {} | Provider-specific configuration |
| `projects.<id>.tags` | array | [] | Tags for project categorization |
| `active_project` | string | - | Currently active project ID |
| `providers` | object | {} | Shared provider credentials |

### Provider Credentials

**Shared credentials for each provider.**

| Provider | Credentials | Description |
|----------|-------------|-------------|
| `providers.planka` | object | Planka API credentials |
| `providers.github` | object | GitHub API credentials |
| `providers.linear` | object | Linear API credentials |

---

## Kanban Integration

**Configure which kanban/project management provider to use.**

| Option | Type | Default | Env Override | Description |
|--------|------|---------|--------------|-------------|
| `kanban.provider` | string | "sqlite" | `MARCUS_KANBAN_PROVIDER` | Provider: sqlite, planka, github, or linear |
| `kanban.board_name` | string | - | - | Default board name to use |

### SQLite Provider (Zero Infrastructure)

The simplest way to get started. No Docker, no external services.

| Option | Type | Default | Env Override | Description |
|--------|------|---------|--------------|-------------|
| `kanban.sqlite_db_path` | string | "./data/kanban.db" | `SQLITE_KANBAN_DB_PATH` | Path to SQLite database file |
| `kanban.sqlite_attachments_dir` | string | "./data/attachments" | `SQLITE_KANBAN_ATTACHMENTS_DIR` | Directory for file attachments |

```json
{
  "kanban": {
    "provider": "sqlite",
    "sqlite_db_path": "./data/kanban.db",
    "sqlite_attachments_dir": "./data/attachments"
  }
}
```

### Planka Provider

| Option | Type | Default | Env Override | Description |
|--------|------|---------|--------------|-------------|
| `kanban.planka.base_url` | string | - | `MARCUS_KANBAN_PLANKA_BASE_URL` | Planka server URL |
| `kanban.planka.email` | string | - | `MARCUS_KANBAN_PLANKA_EMAIL` | Planka login email |
| `kanban.planka.password` | string | - | `MARCUS_KANBAN_PLANKA_PASSWORD` | Planka login password |
| `kanban.planka.project_id` | string | - | `MARCUS_KANBAN_PLANKA_PROJECT_ID` | Planka project ID |
| `kanban.planka.board_id` | string | - | `MARCUS_KANBAN_PLANKA_BOARD_ID` | Planka board ID |

### GitHub Provider

| Option | Type | Default | Env Override | Description |
|--------|------|---------|--------------|-------------|
| `kanban.github.token` | string | - | `MARCUS_KANBAN_GITHUB_TOKEN` | GitHub personal access token |
| `kanban.github.owner` | string | - | `MARCUS_KANBAN_GITHUB_OWNER` | GitHub repository owner |
| `kanban.github.repo` | string | - | `MARCUS_KANBAN_GITHUB_REPO` | GitHub repository name |

### Linear Provider

| Option | Type | Default | Env Override | Description |
|--------|------|---------|--------------|-------------|
| `kanban.linear.api_key` | string | - | `MARCUS_KANBAN_LINEAR_API_KEY` | Linear API key |
| `kanban.linear.team_id` | string | - | `MARCUS_KANBAN_LINEAR_TEAM_ID` | Linear team ID |

---

## AI Configuration

**Configure AI/LLM providers for intelligent features.**

| Option | Type | Default | Env Override | Description |
|--------|------|---------|--------------|-------------|
| `ai.provider` | string | "openai" | `MARCUS_LLM_PROVIDER` | Provider: openai, anthropic, or local |
| `ai.enabled` | boolean | true | `MARCUS_AI_ENABLED` | Enable/disable AI features globally |
| `ai.model` | string | - | `MARCUS_AI_MODEL` | Default model name |
| `ai.anthropic_api_key` | string | - | `MARCUS_AI_ANTHROPIC_API_KEY` | Anthropic API key |
| `ai.openai_api_key` | string | - | `MARCUS_AI_OPENAI_API_KEY` | OpenAI API key |
| `ai.local_model` | string | - | `MARCUS_LOCAL_LLM_PATH` | Local model name (e.g., "qwen2.5:7b") |
| `ai.local_url` | string | - | `MARCUS_LOCAL_LLM_URL` | Local LLM server URL (e.g., Ollama) |
| `ai.local_key` | string | "none" | `MARCUS_LOCAL_LLM_KEY` | Local LLM API key (if required) |

### Supported Providers

- **openai**: OpenAI GPT models (requires `openai_api_key`)
- **anthropic**: Anthropic Claude models (requires `anthropic_api_key`)
- **local**: Local LLM via Ollama or similar (requires `local_url` and `local_model`)

---

## Features

**Enable or disable specific Marcus features.**

Features can be configured as boolean (simple) or object (advanced):

```json
// Simple format
"features": {
  "events": true,
  "context": true
}

// Advanced format
"features": {
  "events": {
    "enabled": true,
    "store_history": true
  }
}
```

| Feature | Type | Default | Description |
|---------|------|---------|-------------|
| `features.events` | boolean/object | false | Event system for tracking pipeline execution |
| `features.context` | boolean/object | false | Context tracking for agent awareness |
| `features.memory` | boolean/object | false | Long-term memory for learning patterns |
| `features.visibility` | boolean/object | false | Enhanced visibility dashboards |

---

## Hybrid Inference

**Configure AI-assisted dependency inference. See [Hybrid Inference Config](../developer/hybrid-inference.md) for details.**

| Option | Type | Default | Valid Range | Description |
|--------|------|---------|-------------|-------------|
| `hybrid_inference.pattern_confidence_threshold` | float | 0.8 | 0.0-1.0 | Pattern match confidence threshold |
| `hybrid_inference.ai_confidence_threshold` | float | 0.7 | 0.0-1.0 | Minimum AI confidence to accept dependency |
| `hybrid_inference.combined_confidence_boost` | float | 0.15 | 0.0-0.3 | Boost when pattern and AI agree |
| `hybrid_inference.max_ai_pairs_per_batch` | integer | 20 | ≥1 | Max task pairs per AI request |
| `hybrid_inference.min_shared_keywords` | integer | 2 | ≥1 | Min keywords to consider tasks related |
| `hybrid_inference.enable_ai_inference` | boolean | true | - | Master switch for AI inference |
| `hybrid_inference.cache_ttl_hours` | integer | 24 | ≥0 | Cache duration for AI results |
| `hybrid_inference.require_component_match` | boolean | true | - | Require shared component for patterns |
| `hybrid_inference.max_dependency_chain_length` | integer | 10 | ≥1 | Max dependency chain depth |

### Presets

Use preset configurations:
- **conservative**: High accuracy, more AI calls
- **balanced**: Default settings (recommended)
- **aggressive**: More dependencies, fewer AI calls
- **cost_optimized**: Minimize API usage
- **pattern_only**: No AI calls

---

## Task Lease

**Configure task assignment lease system to prevent task abandonment.**

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `task_lease.silence_timeout_minutes` | float | 45.0 | Minutes of MCP silence after which the agent is presumed dead (ADR-0012 D3) |
| `task_lease.budget_multiplier` | float | 3.0 | Budget ceiling as a multiple of the task's estimate |
| `task_lease.checkpoint_cadence_minutes` | float | 20.0 | Checkpoint interval; the silence timeout must be at least twice it |
| `task_lease.default_hours` | float | 4.0 | Estimate used for the budget when a task has none |
| `task_lease.warning_hours` | float | 0.0167 | Hours before expiry to show warning |
| `task_lease.grace_period_minutes` | float | 1.0 | Grace period after expiry |
| `task_lease.min_lease_hours` | float | 0.0 | Lower bound on the budget |
| `task_lease.max_lease_hours` | float | 24.0 | Upper bound on the budget |
| `task_lease.enable_adaptive` | boolean | true | Scale the budget by estimate, priority and complexity |
| `task_lease.max_renewals`, `stuck_threshold_renewals`, `renewal_decay_factor`, `silence_multiplier` | | | Pre-D3 keys; accepted and ignored |

### Priority Multipliers

Adjust lease duration based on task priority:

| Priority | Default Multiplier | Effect |
|----------|-------------------|--------|
| `task_lease.priority_multipliers.critical` | 0.5 | 2× shorter lease (urgent) |
| `task_lease.priority_multipliers.high` | 0.75 | 1.33× shorter lease |
| `task_lease.priority_multipliers.medium` | 1.0 | Default lease |
| `task_lease.priority_multipliers.low` | 1.5 | 1.5× longer lease |

### Complexity Multipliers

Adjust lease duration based on task complexity:

| Complexity | Default Multiplier | Effect |
|------------|-------------------|--------|
| `task_lease.complexity_multipliers.simple` | 0.5 | 2× shorter lease |
| `task_lease.complexity_multipliers.complex` | 1.5 | 1.5× longer lease |
| `task_lease.complexity_multipliers.research` | 2.0 | 2× longer lease |
| `task_lease.complexity_multipliers.epic` | 3.0 | 3× longer lease |

---

## Registration

**Keep the worker roster across restarts and bounded (ADR-0012 O1).**

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `registration.ttl_hours` | float | 24.0 | Hours of silence after which a worker's registration is evicted (never while it holds a live lease) |
| `registration.sweep_interval_seconds` | float | 3600.0 | How often, at most, the eviction sweep runs |
| `registration.touch_write_interval_seconds` | float | 60.0 | Minimum spacing between disk writes of `last_seen` per worker; every tool call is a touch |

---

## Board Health

**Monitor and maintain board health.**

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `board_health.stale_task_days` | integer | 7 | Days before task is considered stale |
| `board_health.max_tasks_per_agent` | integer | 3 | Maximum concurrent tasks per agent |

---

## Transport

**Configure MCP transport layer.**

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `transport.type` | string | "http" | Transport type: http or stdio |
| `transport.http.host` | string | "0.0.0.0" | HTTP server host |
| `transport.http.port` | integer | 4298 | HTTP server port |
| `transport.http.path` | string | "/mcp" | HTTP endpoint path |
| `transport.http.log_level` | string | "info" | Logging level |

---

## Multi-Endpoint

**Configure multiple MCP endpoints for different audiences.**

| Endpoint | Default Port | Description |
|----------|-------------|-------------|
| `multi_endpoint.human` | 4298 | Human-facing tools (UI, management) |
| `multi_endpoint.agent` | 4299 | Agent-facing tools (task operations) |
| `multi_endpoint.analytics` | 4300 | Analytics and reporting tools |

### Endpoint Configuration

Each endpoint has the same structure:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `multi_endpoint.<name>.port` | integer | - | Port number |
| `multi_endpoint.<name>.host` | string | "0.0.0.0" | Host address |
| `multi_endpoint.<name>.path` | string | "/mcp" | Endpoint path |
| `multi_endpoint.<name>.enabled` | boolean | true | Enable/disable endpoint |

---

## Monitoring

**System monitoring configuration.**

| Option | Type | Default | Env Override | Description |
|--------|------|---------|--------------|-------------|
| `monitoring.interval` | integer | 60 | `MARCUS_MONITORING_INTERVAL` | Monitoring interval in seconds |

---

## Communication

**Configure notification channels.**

| Option | Type | Default | Env Override | Description |
|--------|------|---------|--------------|-------------|
| `communication.slack_enabled` | boolean | false | `MARCUS_SLACK_ENABLED` | Enable Slack notifications |
| `communication.slack_webhook_url` | string | - | `MARCUS_SLACK_WEBHOOK_URL` | Slack webhook URL |
| `communication.email_enabled` | boolean | false | `MARCUS_EMAIL_ENABLED` | Enable email notifications |

---

## Advanced

**Advanced and debugging options.**

| Option | Type | Default | Env Override | Description |
|--------|------|---------|--------------|-------------|
| `advanced.debug` | boolean | false | `MARCUS_DEBUG` | Enable debug logging |
| `advanced.port` | integer | 4298 | `MARCUS_PORT` | Default server port |

---

## Environment Variable Overrides

All environment variables follow the pattern `MARCUS_<SECTION>_<OPTION>`:

```bash
# Kanban provider
export MARCUS_KANBAN_PROVIDER=github
export MARCUS_KANBAN_GITHUB_TOKEN=ghp_xxx

# AI provider
export MARCUS_LLM_PROVIDER=local
export MARCUS_LOCAL_LLM_URL=http://localhost:11434/v1

# Debug mode
export MARCUS_DEBUG=true
```

**Priority**: Environment variables override `config_marcus.json` values.

---

## Configuration Migration

Marcus automatically migrates legacy single-project configs to multi-project format:

**Before (Legacy)**:
```json
{
  "project_id": "123",
  "board_id": "456",
  "planka": {
    "base_url": "http://planka:1337"
  }
}
```

**After (Migrated)**:
```json
{
  "projects": {
    "uuid-generated": {
      "name": "Default Project",
      "provider": "planka",
      "config": {
        "project_id": "123",
        "board_id": "456"
      },
      "tags": ["default", "migrated"]
    }
  },
  "active_project": "uuid-generated",
  "providers": {
    "planka": {
      "base_url": "http://planka:1337"
    }
  }
}
```

---

## Example Configurations

### Minimal Local Development

```json
{
  "kanban": {
    "provider": "planka"
  },
  "planka": {
    "base_url": "http://localhost:1337",
    "email": "admin@localhost",
    "password": "admin"  # pragma: allowlist secret
  },
  "ai": {
    "provider": "local",
    "local_model": "qwen2.5:7b-instruct",
    "local_url": "http://localhost:11434/v1"
  }
}
```

### Production Multi-Project

```json
{
  "projects": {
    "proj-frontend": {
      "name": "Frontend App",
      "provider": "github",
      "config": {
        "owner": "myorg",
        "repo": "frontend"
      }
    },
    "proj-backend": {
      "name": "Backend API",
      "provider": "linear",
      "config": {
        "team_id": "team-123"
      }
    }
  },
  "active_project": "proj-frontend",
  "providers": {
    "github": {
      "token": "ghp_xxx"
    },
    "linear": {
      "api_key": "lin_xxx"  # pragma: allowlist secret
    }
  },
  "ai": {
    "provider": "anthropic",
    "anthropic_api_key": "sk-ant-xxx"  # pragma: allowlist secret
  },
  "features": {
    "events": true,
    "context": true,
    "memory": true
  }
}
```

---

## See Also

- [Configuration Management](../systems/infrastructure/28-configuration-management.md)
- [Setup Local LLM](../getting-started/setup-local-llm.md)
- [Development Workflow](../developer/development-workflow.md)
