# Marcus Configuration Guide

## Overview

Marcus uses a centralized, type-safe configuration system based on Python dataclasses. This provides:

- **Type Safety**: IDE autocomplete and compile-time type checking
- **Environment Variables**: Override any setting with `${VAR_NAME}` syntax
- **Validation**: Automatic validation on startup
- **Clear Defaults**: Sensible defaults for all settings

## Quick Start

### 1. Create Your Configuration File

Copy the example configuration:

```bash
cp config_marcus.example.json config_marcus.local.json
```

### 2. Set Your Credentials

Edit `config_marcus.local.json` with your API keys and credentials. You can either:

**Option A**: Set values directly in the file:
```json
{
  "ai": {
    "provider": "anthropic",
    "anthropic_api_key": "sk-ant-api01-..."  // pragma: allowlist secret
  }
}
```

**Option B**: Use environment variables:
```json
{
  "ai": {
    "provider": "anthropic",
    "anthropic_api_key": "${CLAUDE_API_KEY}"
  }
}
```

Then set the environment variable:
```bash
export CLAUDE_API_KEY="sk-ant-api01-..."  # pragma: allowlist secret
```

### 3. Access Configuration in Code

```python
from src.config.marcus_config import get_config

config = get_config()
api_key = config.ai.anthropic_api_key
planka_url = config.kanban.planka_base_url
```

## Configuration Sections

### AI Settings

Configure AI providers and models:

```json
{
  "ai": {
    "provider": "anthropic",           // "anthropic", "openai", or "local"
    "anthropic_api_key": "${CLAUDE_API_KEY}",
    "openai_api_key": "${OPENAI_API_KEY}",
    "local_model": "qwen2.5:7b-instruct",  // For Ollama
    "local_url": "http://localhost:11434/v1",
    "local_key": "none",
    "model": "claude-3-haiku-20240307",
    "temperature": 0.7,               // 0.0-1.0
    "max_tokens": 4096,
    "enabled": true
  }
}
```

**Supported Providers**:
- `anthropic`: Claude models (requires `anthropic_api_key`)
- `openai`: GPT models (requires `openai_api_key`)
- `local`: Ollama or other local models (requires `local_model`)

### Kanban Settings

Configure your project management backend:

```json
{
  "kanban": {
    "provider": "planka",              // "planka", "github", or "linear"
    "board_name": "Marcus Todo Demo",

    // Planka settings
    "planka_base_url": "${PLANKA_BASE_URL}",
    "planka_email": "${PLANKA_EMAIL}",
    "planka_password": "${PLANKA_PASSWORD}",

    // GitHub settings
    "github_token": "${GITHUB_TOKEN}",
    "github_owner": "${GITHUB_OWNER}",
    "github_repo": "${GITHUB_REPO}",

    // Linear settings
    "linear_api_key": "${LINEAR_API_KEY}",
    "linear_team_id": "${LINEAR_TEAM_ID}"
  }
}
```

**Supported Providers**:
- `planka`: Self-hosted Planka boards
- `github`: GitHub Projects
- `linear`: Linear.app

### Feature Flags

Enable or disable Marcus features:

```json
{
  "features": {
    "events": true,      // Event system
    "context": true,     // Context tracking
    "memory": false,     // Learning/memory system
    "visibility": false  // Visualization features
  }
}
```

### Memory Settings

Configure the learning/memory system (when `features.memory` is enabled):

```json
{
  "memory": {
    "learning_rate": 0.1,           // How quickly patterns are learned
    "min_samples": 3,               // Minimum samples before pattern recognition
    "use_v2_predictions": false     // Use enhanced prediction algorithm
  }
}
```

### Transport Settings

Configure MCP (Model Context Protocol) transport:

```json
{
  "transport": {
    "type": "http",              // "http" or "stdio"
    "http_host": "0.0.0.0",
    "http_port": 4298,
    "http_path": "/mcp",
    "log_level": "info",         // "debug", "info", "warning", "error"
    "dual_mode": false,          // Enable both stdio and HTTP simultaneously
    "http_enabled": true         // Enable HTTP in dual mode
  }
}
```

### Task Lease Settings

Configure agent task leasing.
Since ADR-0012 D3 a lease has two dials: the silence timeout (is the agent alive?) and the budget ceiling (how long may it hold the task?).
The pre-D3 keys `max_renewals`, `stuck_threshold_renewals`, `renewal_decay_factor` and `silence_multiplier` are still accepted and ignored.

```json
{
  "task_lease": {
    "silence_timeout_minutes": 45.0,
    "budget_multiplier": 3.0,
    "checkpoint_cadence_minutes": 20.0,
    "default_hours": 4.0,
    "warning_hours": 0.0167,
    "grace_period_minutes": 1.0,
    "min_lease_hours": 0.0,
    "max_lease_hours": 24.0,
    "enable_adaptive": true,

    "priority_multipliers": {
      "critical": 0.5,    // Critical tasks get a tighter budget ceiling
      "high": 0.75,
      "medium": 1.0,
      "low": 1.5         // Low priority tasks get a looser one
    },

    "complexity_multipliers": {
      "simple": 0.5,
      "complex": 1.5,
      "research": 2.0,
      "epic": 3.0
    }
  }
}
```

### Board Health Settings

Configure board health monitoring:

```json
{
  "board_health": {
    "stale_task_days": 7,        // Days before a task is considered stale
    "max_tasks_per_agent": 3     // Maximum concurrent tasks per agent
  }
}
```

### Multi-Endpoint Settings

Configure separate MCP endpoints for different clients:

```json
{
  "multi_endpoint": {
    "human": {
      "port": 4298,
      "host": "0.0.0.0",
      "path": "/mcp",
      "enabled": true
    },
    "agent": {
      "port": 4299,
      "host": "0.0.0.0",
      "path": "/mcp",
      "enabled": true
    },
    "analytics": {
      "port": 4300,
      "host": "0.0.0.0",
      "path": "/mcp",
      "enabled": true
    }
  }
}
```

### Hybrid Inference Settings

Configure AI-powered dependency detection:

```json
{
  "hybrid_inference": {
    "pattern_confidence_threshold": 0.8,
    "ai_confidence_threshold": 0.7,
    "combined_confidence_boost": 0.15,
    "max_ai_pairs_per_batch": 20,
    "min_shared_keywords": 2,
    "enable_ai_inference": true,
    "cache_ttl_hours": 24,
    "require_component_match": true,
    "max_dependency_chain_length": 10
  }
}
```

### MCP Settings

Configure Model Context Protocol client settings:

```json
{
  "mcp": {
    "kanban_client_path": "~/dev/kanban-mcp/dist/index.js",
    "timeout": 30,
    "retry_attempts": 3
  }
}
```

## Environment Variable Substitution

Any configuration value can reference an environment variable using `${VAR_NAME}` syntax:

```json
{
  "ai": {
    "anthropic_api_key": "${CLAUDE_API_KEY}"
  },
  "kanban": {
    "planka_password": "${PLANKA_PASSWORD}"
  }
}
```

**Complex example with defaults**:
```bash
export PLANKA_BASE_URL="http://localhost:3333"
export PLANKA_EMAIL="admin@example.com"
export PLANKA_PASSWORD="secure_password_123"  # pragma: allowlist secret
```

## Configuration File Locations

Marcus looks for configuration files in this order:

1. `config_marcus.local.json` (local overrides, gitignored)
2. `config_marcus.json` (committed defaults)
3. Built-in defaults from `MarcusConfig` dataclass

**Best Practice**: Keep `config_marcus.local.json` gitignored and use it for your local development settings.

## Validation

Marcus validates configuration on startup:

```python
from src.config.marcus_config import get_config

config = get_config()

# Validate configuration
errors = config.validate()
if errors:
    for error in errors:
        print(f"Configuration error: {error}")
    raise ValueError("Invalid configuration")
```

**Common validation checks**:
- Required API keys are present (based on provider selection)
- Port numbers are valid (1-65535)
- Threshold values are in valid ranges (0.0-1.0)
- File paths exist and are accessible

## Migration from Old Config System

If you're migrating from the old `config_loader` system:

### Old Way (config.get())
```python
from src.config.config_loader import get_config

config = get_config()
api_key = config.get("ai", {}).get("anthropic_api_key")
planka_url = config.get("planka", {}).get("base_url", "http://localhost:3333")
```

### New Way (Type-Safe)
```python
from src.config.marcus_config import get_config

config = get_config()
api_key = config.ai.anthropic_api_key
planka_url = config.kanban.planka_base_url
```

**Benefits**:
- IDE autocomplete
- Type checking
- No more nested `.get()` calls
- Clear defaults
- Validation on startup

## Common Configurations

### Local Development with Planka

```json
{
  "single_project_mode": true,
  "ai": {
    "provider": "anthropic",
    "anthropic_api_key": "${CLAUDE_API_KEY}",
    "model": "claude-3-haiku-20240307"
  },
  "kanban": {
    "provider": "planka",
    "planka_base_url": "http://localhost:3333",
    "planka_email": "admin@example.com",
    "planka_password": "${PLANKA_PASSWORD}"
  },
  "features": {
    "events": true,
    "context": true,
    "memory": false
  }
}
```

### Production with GitHub Projects

```json
{
  "single_project_mode": false,
  "ai": {
    "provider": "anthropic",
    "anthropic_api_key": "${CLAUDE_API_KEY}",
    "model": "claude-3-opus-20240229"
  },
  "kanban": {
    "provider": "github",
    "github_token": "${GITHUB_TOKEN}",
    "github_owner": "myorg",
    "github_repo": "myproject"
  },
  "features": {
    "events": true,
    "context": true,
    "memory": true
  },
  "transport": {
    "type": "http",
    "http_host": "0.0.0.0",
    "http_port": 4298
  }
}
```

### Local AI with Ollama

```json
{
  "ai": {
    "provider": "local",
    "local_model": "qwen2.5:7b-instruct",
    "local_url": "http://localhost:11434/v1",
    "local_key": "none",
    "enabled": true
  },
  "kanban": {
    "provider": "planka",
    "planka_base_url": "http://localhost:3333",
    "planka_email": "admin@example.com",
    "planka_password": "${PLANKA_PASSWORD}"
  }
}
```

## Troubleshooting

### Configuration Not Loading

**Problem**: Configuration changes aren't being picked up

**Solution**: Check file locations:
```bash
ls -la config_marcus*.json
```

Ensure `config_marcus.local.json` exists and has proper JSON syntax.

### Environment Variables Not Substituted

**Problem**: Seeing literal `${VAR_NAME}` in values

**Solution**: Check that environment variables are set:
```bash
echo $CLAUDE_API_KEY
```

Set them in your shell:
```bash
export CLAUDE_API_KEY="sk-ant-..."  # pragma: allowlist secret
```

### API Key Not Found

**Problem**: `ValueError: Anthropic API key not found`

**Solution**: Ensure API key is set in config or environment:
```json
{
  "ai": {
    "provider": "anthropic",
    "anthropic_api_key": "${CLAUDE_API_KEY}"
  }
}
```

And:
```bash
export CLAUDE_API_KEY="sk-ant-..."  # pragma: allowlist secret
```

### Type Errors

**Problem**: `AttributeError: 'MarcusConfig' object has no attribute 'xyz'`

**Solution**: Check the attribute name matches the dataclass definition in `src/config/marcus_config.py`. Common mistakes:
- `config.planka.base_url` → `config.kanban.planka_base_url`
- `config.ai.api_key` → `config.ai.anthropic_api_key`

## Advanced Usage

### Custom Configuration Location

```python
from src.config.marcus_config import MarcusConfig

config = MarcusConfig.from_file("/path/to/custom/config.json")
```

### Programmatic Configuration

```python
from src.config.marcus_config import MarcusConfig, AISettings

config = MarcusConfig(
    ai=AISettings(
        provider="anthropic",
        anthropic_api_key="sk-ant-...",  # pragma: allowlist secret
        model="claude-3-haiku-20240307"
    )
)
```

### Override Specific Settings

```python
from src.config.marcus_config import get_config

config = get_config()

# Override at runtime
config.ai.temperature = 0.5
config.transport.log_level = "debug"
```

## Configuration Schema Reference

For the complete configuration schema, see:
- Source code: `src/config/marcus_config.py`
- Example: `config_marcus.example.json`
- Tests: `tests/unit/config/test_marcus_config*.py`

## Security Best Practices

1. **Never commit API keys**: Use environment variables
2. **Use .gitignore**: Keep `config_marcus.local.json` out of git
3. **Rotate credentials**: Regularly update API keys
4. **Limit permissions**: Use least-privilege API tokens
5. **Validate inputs**: Marcus validates config on startup

## Support

For configuration issues:
- Check the example: `config_marcus.example.json`
- Review tests: `tests/unit/config/`
- File an issue: https://github.com/lwgray/marcus/issues
