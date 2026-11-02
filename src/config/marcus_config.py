"""Centralized configuration for Marcus.

This module provides type-safe configuration management with:
- Dataclass-based structure for type safety
- Environment variable override support
- Validation on startup
- Clear defaults

Example
-------
>>> from src.config.marcus_config import get_config
>>> config = get_config()
>>> api_key = config.ai.anthropic_api_key
>>> planka_url = config.kanban.planka_base_url
"""

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class MCPSettings:
    """MCP (Model Context Protocol) related configuration.

    Parameters
    ----------
    kanban_client_path : str
        Path to kanban-mcp client (default: ~/dev/kanban-mcp/dist/index.js)
    timeout : int
        Timeout for MCP operations in seconds (default: 30)
    retry_attempts : int
        Number of retry attempts for failed operations (default: 3)
    """

    kanban_client_path: str = "~/dev/kanban-mcp/dist/index.js"
    timeout: int = 30
    retry_attempts: int = 3


@dataclass
class AISettings:
    """AI provider configuration.

    Parameters
    ----------
    provider : str
        AI provider to use: "anthropic", "openai", "local", or "cloud"
    anthropic_api_key : Optional[str]
        Anthropic API key (required if provider="anthropic")
    openai_api_key : Optional[str]
        OpenAI API key (required if provider="openai")
    local_model : Optional[str]
        Local model name (e.g., "qwen2.5:7b-instruct" for Ollama)
    local_url : Optional[str]
        Local LLM API URL (e.g., "http://localhost:11434/v1")
    local_key : Optional[str]
        Local LLM API key (if required)
    cloud_api_key : Optional[str]
        API key for generic cloud provider (required if provider="cloud")
    cloud_url : Optional[str]
        Base URL for generic cloud provider (required if provider="cloud")
    model : Optional[str]
        Model name (e.g., "claude-3-haiku-20240307")
    temperature : float
        Temperature for LLM sampling (0.0-1.0)
    max_tokens : int
        Maximum tokens for LLM responses
    enabled : bool
        Whether AI features are enabled
    """

    provider: str = "anthropic"
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    local_model: Optional[str] = None
    local_url: Optional[str] = None
    local_key: Optional[str] = None
    cloud_api_key: Optional[str] = None
    cloud_url: Optional[str] = None
    model: Optional[str] = "claude-3-haiku-20240307"
    temperature: float = 0.7
    max_tokens: int = 4096
    enabled: bool = True


@dataclass
class KanbanSettings:
    """Kanban provider configuration.

    Parameters
    ----------
    provider : str
        Kanban provider: "planka", "github", "linear", or "sqlite"
    board_name : Optional[str]
        Default board name to use
    planka_base_url : Optional[str]
        Planka server URL (required if provider="planka")
    planka_email : Optional[str]
        Planka user email (required if provider="planka")
    planka_password : Optional[str]
        Planka user password (required if provider="planka")
    github_token : Optional[str]
        GitHub personal access token (required if provider="github")
    github_owner : Optional[str]
        GitHub repository owner
    github_repo : Optional[str]
        GitHub repository name
    linear_api_key : Optional[str]
        Linear API key (required if provider="linear")
    linear_team_id : Optional[str]
        Linear team ID
    sqlite_db_path : Optional[str]
        Path to SQLite database file (required if provider="sqlite")
    sqlite_attachments_dir : Optional[str]
        Directory for attachment file storage (provider="sqlite")
    """

    provider: str = "sqlite"
    board_name: Optional[str] = None
    planka_base_url: Optional[str] = None
    planka_email: Optional[str] = None
    planka_password: Optional[str] = None
    github_token: Optional[str] = None
    github_owner: Optional[str] = None
    github_repo: Optional[str] = None
    linear_api_key: Optional[str] = None
    linear_team_id: Optional[str] = None
    sqlite_db_path: Optional[str] = None
    sqlite_attachments_dir: Optional[str] = None


@dataclass
class FeaturesSettings:
    """Feature flags for optional Marcus features.

    Parameters
    ----------
    events : bool
        Enable event logging
    context : bool
        Enable context system
    memory : bool
        Enable memory/learning system
    visibility : bool
        Enable visibility features
    """

    events: bool = True
    context: bool = True
    memory: bool = True
    visibility: bool = False


@dataclass
class MemorySettings:
    """Memory system configuration.

    Parameters
    ----------
    learning_rate : float
        Learning rate for memory system (0.0-1.0)
    min_samples : int
        Minimum samples required for confidence threshold
    use_v2_predictions : bool
        Whether to use enhanced memory v2 predictions
    """

    learning_rate: float = 0.1
    min_samples: int = 3
    use_v2_predictions: bool = False


@dataclass
class TransportSettings:
    """MCP transport configuration.

    Parameters
    ----------
    type : str
        Transport type: "http" or "stdio"
    http_host : str
        HTTP server host
    http_port : int
        HTTP server port
    http_path : str
        HTTP endpoint path
    log_level : str
        Logging level
    dual_mode : bool
        Enable both stdio and HTTP transports simultaneously
    http_enabled : bool
        Enable HTTP transport in dual mode
    """

    type: str = "http"
    http_host: str = "127.0.0.1"
    http_port: int = 4298
    http_path: str = "/mcp"
    log_level: str = "info"
    dual_mode: bool = False
    http_enabled: bool = True


@dataclass
class TaskLeaseSettings:
    """Task lease configuration for agent task management.

    ADR-0012 D3 splits the lease into two dials. The **silence timeout**
    is how long an agent may make no MCP call before Marcus presumes it
    dead and recovers its task. The **budget ceiling** is how long an
    agent may hold a task at all: ``budget_multiplier`` times the task's
    estimate, floored at one silence window and capped at
    ``max_lease_hours``. Heartbeats reset the first and never pass the
    second.

    Parameters
    ----------
    silence_timeout_minutes : float
        Minutes of MCP silence after which the agent is presumed dead
        (default 45). Must be at least twice ``checkpoint_cadence_minutes``.
    budget_multiplier : float
        The budget is this many times the estimate (default 3.0).
    checkpoint_cadence_minutes : float
        How often the session contract obliges an agent to checkpoint
        (default 20); validates the silence timeout.
    default_hours : float
        Estimate used for the budget when a task carries none.
    warning_hours : float
        Hours before expiry to warn.
    grace_period_minutes : float
        Grace period after expiry in minutes.
    min_lease_hours : float
        Lower bound on the budget.
    max_lease_hours : float
        Upper bound on the budget; no task is held longer.
    enable_adaptive : bool
        Scale the budget by estimate, priority, and complexity.
    priority_multipliers : dict[str, float]
        Priority-based budget multipliers
    complexity_multipliers : dict[str, float]
        Complexity-based budget multipliers
    max_renewals, stuck_threshold_renewals, renewal_decay_factor, silence_multiplier
        Pre-D3 knobs. Accepted so existing config files still load;
        ignored.
    """

    silence_timeout_minutes: float = 45.0
    budget_multiplier: float = 3.0
    checkpoint_cadence_minutes: float = 20.0
    default_hours: float = 4.0
    warning_hours: float = 0.0167  # ~60 seconds before expiry
    grace_period_minutes: float = 1.0  # 60 seconds grace
    min_lease_hours: float = 0.0
    max_lease_hours: float = 24.0
    enable_adaptive: bool = True
    # Pre-D3 knobs: accepted, ignored.
    max_renewals: int = 10
    renewal_decay_factor: float = 0.9
    stuck_threshold_renewals: int = 5
    silence_multiplier: float = 1.5
    priority_multipliers: dict[str, float] = field(
        default_factory=lambda: {
            "critical": 0.5,
            "high": 0.75,
            "medium": 1.0,
            "low": 1.5,
        }
    )
    complexity_multipliers: dict[str, float] = field(
        default_factory=lambda: {
            "simple": 0.5,
            "complex": 1.5,
            "research": 2.0,
            "epic": 3.0,
        }
    )


@dataclass
class RegistrationSettings:
    """Worker registration roster settings (ADR-0012 obligation O1).

    Registrations are persisted to ``registrations.json`` beside the
    assignment store and rehydrated lazily on the first tool call after
    a restart. A registration that Marcus has not heard from for
    ``ttl_hours`` is evicted so the roster does not grow forever; a
    worker holding a live lease is never evicted.

    Parameters
    ----------
    ttl_hours : float
        Hours of silence after which a registration is evicted (default 24).
    sweep_interval_seconds : float
        How often, at most, the eviction sweep runs (default 3600).
    touch_write_interval_seconds : float
        Minimum spacing between disk writes of ``last_seen`` per worker
        (default 60); every tool call is a touch.
    """

    ttl_hours: float = 24.0
    sweep_interval_seconds: float = 3600.0
    touch_write_interval_seconds: float = 60.0


@dataclass
class BoardHealthSettings:
    """Board health monitoring configuration.

    Parameters
    ----------
    stale_task_days : int
        Days before a task is considered stale
    max_tasks_per_agent : int
        Maximum tasks per agent before warning
    """

    stale_task_days: int = 7
    max_tasks_per_agent: int = 3


@dataclass
class EndpointSettings:
    """Configuration for a single MCP endpoint.

    Parameters
    ----------
    port : int
        Port number for the endpoint
    host : str
        Host address for the endpoint
    path : str
        URL path for the endpoint
    enabled : bool
        Whether this endpoint is enabled
    """

    port: int
    host: str = "127.0.0.1"
    path: str = "/mcp"
    enabled: bool = True


@dataclass
class MultiEndpointSettings:
    """Multi-endpoint configuration for different client types.

    Parameters
    ----------
    human : EndpointSettings
        Endpoint for human clients
    agent : EndpointSettings
        Endpoint for agent clients
    analytics : EndpointSettings
        Endpoint for analytics clients
    """

    human: EndpointSettings = field(
        default_factory=lambda: EndpointSettings(port=4298, host="127.0.0.1")
    )
    agent: EndpointSettings = field(
        default_factory=lambda: EndpointSettings(port=4299, host="127.0.0.1")
    )
    analytics: EndpointSettings = field(
        default_factory=lambda: EndpointSettings(port=4300, host="127.0.0.1")
    )


@dataclass
class HybridInferenceSettings:
    """Hybrid inference configuration for dependency detection.

    Parameters
    ----------
    pattern_confidence_threshold : float
        Confidence threshold for pattern-based inference
    ai_confidence_threshold : float
        Confidence threshold for AI-based inference
    combined_confidence_boost : float
        Boost when both methods agree
    max_ai_pairs_per_batch : int
        Maximum task pairs to send to AI per batch
    min_shared_keywords : int
        Minimum shared keywords for pattern match
    enable_ai_inference : bool
        Whether to enable AI inference
    cache_ttl_hours : int
        Cache TTL in hours
    require_component_match : bool
        Require component match for dependencies
    max_dependency_chain_length : int
        Maximum allowed dependency chain length
    """

    pattern_confidence_threshold: float = 0.8
    ai_confidence_threshold: float = 0.7
    combined_confidence_boost: float = 0.15
    max_ai_pairs_per_batch: int = 20
    min_shared_keywords: int = 2
    enable_ai_inference: bool = True
    cache_ttl_hours: int = 24
    require_component_match: bool = True
    max_dependency_chain_length: int = 10


@dataclass
class LoggingSettings:
    """Logging configuration.

    Parameters
    ----------
    level : str
        Logging level: DEBUG, INFO, WARNING, ERROR, or CRITICAL
    log_dir : str
        Directory for log files
    enable_file_logging : bool
        Whether to enable file logging
    enable_console_logging : bool
        Whether to enable console logging
    """

    level: str = "INFO"
    log_dir: str = "logs"
    enable_file_logging: bool = True
    enable_console_logging: bool = True


@dataclass
class MarcusConfig:
    """Central configuration for Marcus.

    This is the main configuration class that contains all settings.
    Load it once at startup and use throughout the application.

    Parameters
    ----------
    mcp : MCPSettings
        MCP-related settings
    ai : AISettings
        AI provider settings
    kanban : KanbanSettings
        Kanban provider settings
    features : FeaturesSettings
        Feature flags
    memory : MemorySettings
        Memory system settings
    transport : TransportSettings
        MCP transport settings
    task_lease : TaskLeaseSettings
        Task lease management settings
    registration : RegistrationSettings
        Worker registration roster settings
    board_health : BoardHealthSettings
        Board health monitoring settings
    multi_endpoint : MultiEndpointSettings
        Multi-endpoint configuration
    hybrid_inference : HybridInferenceSettings
        Hybrid inference settings
    logging : LoggingSettings
        Logging configuration
    auto_find_board : bool
        Automatically find board by name
    single_project_mode : bool
        Run in single project mode
    log_level : str
        Global logging level
    data_dir : str
        Data directory path
    cache_dir : str
        Cache directory path
    """

    mcp: MCPSettings = field(default_factory=MCPSettings)
    ai: AISettings = field(default_factory=AISettings)
    kanban: KanbanSettings = field(default_factory=KanbanSettings)
    features: FeaturesSettings = field(default_factory=FeaturesSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    transport: TransportSettings = field(default_factory=TransportSettings)
    task_lease: TaskLeaseSettings = field(default_factory=TaskLeaseSettings)
    registration: RegistrationSettings = field(default_factory=RegistrationSettings)
    board_health: BoardHealthSettings = field(default_factory=BoardHealthSettings)
    multi_endpoint: MultiEndpointSettings = field(default_factory=MultiEndpointSettings)
    hybrid_inference: HybridInferenceSettings = field(
        default_factory=HybridInferenceSettings
    )
    logging: LoggingSettings = field(default_factory=LoggingSettings)

    # Global settings
    auto_find_board: bool = False
    single_project_mode: bool = True
    default_project_name: Optional[str] = None
    log_level: str = "INFO"
    data_dir: str = "./data"
    cache_dir: str = "./cache"

    @classmethod
    def from_file(cls, path: str = "config_marcus.json") -> "MarcusConfig":
        """Load configuration from JSON file.

        This method:
        1. Looks for config file at given path
        2. If not found, returns default configuration
        3. Parses JSON and creates nested dataclass instances
        4. Supports ${ENV_VAR} substitution in values

        Parameters
        ----------
        path : str
            Path to config file (default: "config_marcus.json")

        Returns
        -------
        MarcusConfig
            Loaded configuration

        Examples
        --------
        >>> config = MarcusConfig.from_file("config_marcus.json")
        >>> print(config.ai.provider)
        "anthropic"
        """
        config_path = Path(path).expanduser()

        # If config file doesn't exist, return defaults
        if not config_path.exists():
            import sys

            print(
                f"Warning: Config file not found at {config_path}, using defaults",
                file=sys.stderr,
            )
            return cls()

        # Load JSON
        with open(config_path) as f:
            data = json.load(f)

        # Substitute environment variables
        data = cls._substitute_env_vars(data)

        # Create nested configs
        return cls._from_dict(data)

    @classmethod
    def _substitute_env_vars(cls, data: Any) -> Any:
        """Recursively substitute ${ENV_VAR} with environment variable values.

        Examples
        --------
        "${CLAUDE_API_KEY}" -> actual API key from environment
        """
        if isinstance(data, dict):
            return {k: cls._substitute_env_vars(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [cls._substitute_env_vars(item) for item in data]
        elif isinstance(data, str) and data.startswith("${") and data.endswith("}"):
            # Extract env var name: "${VAR_NAME}" -> "VAR_NAME"
            env_var = data[2:-1]
            # Return None if env var not found - this ensures validation catches it
            value = os.getenv(env_var, None)
            if value is None:
                logger.warning(
                    f"Environment variable {env_var} not set, using None. "
                    f"This may cause validation errors if the variable is required."
                )
            return value
        else:
            return data

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "MarcusConfig":
        """Create MarcusConfig from dictionary.

        Handles nested dataclasses by recursively creating them.
        """
        # Create nested configs
        nested_configs: dict[str, Any] = {}

        if "mcp" in data:
            nested_configs["mcp"] = MCPSettings(**data["mcp"])

        if "ai" in data:
            nested_configs["ai"] = AISettings(**data["ai"])

        if "kanban" in data:
            nested_configs["kanban"] = KanbanSettings(**data["kanban"])

        # Handle old "planka" key for backward compatibility
        if "planka" in data:
            planka_data = data["planka"]
            if "kanban" not in nested_configs:
                nested_configs["kanban"] = KanbanSettings()
            nested_configs["kanban"].planka_base_url = planka_data.get("base_url")
            nested_configs["kanban"].planka_email = planka_data.get("email")
            nested_configs["kanban"].planka_password = planka_data.get("password")

        if "github" in data:
            github_data = data["github"]
            if "kanban" not in nested_configs:
                nested_configs["kanban"] = KanbanSettings()
            nested_configs["kanban"].github_token = github_data.get("token")
            nested_configs["kanban"].github_owner = github_data.get("owner")
            nested_configs["kanban"].github_repo = github_data.get("repo")

        if "linear" in data:
            linear_data = data["linear"]
            if "kanban" not in nested_configs:
                nested_configs["kanban"] = KanbanSettings()
            nested_configs["kanban"].linear_api_key = linear_data.get("api_key")
            nested_configs["kanban"].linear_team_id = linear_data.get("team_id")

        if "features" in data:
            nested_configs["features"] = FeaturesSettings(**data["features"])

        if "memory" in data:
            nested_configs["memory"] = MemorySettings(**data["memory"])

        if "transport" in data:
            transport_data = data["transport"]
            if isinstance(transport_data.get("http"), dict):
                # New format with nested http section
                http_config = transport_data["http"]
                port = http_config.get("port", 4298)
                # Convert string to int if needed (from env var substitution)
                if isinstance(port, str):
                    try:
                        port = int(port)
                    except ValueError:
                        # Invalid port string, use default
                        port = 4298
                nested_configs["transport"] = TransportSettings(
                    type=transport_data.get("type", "http"),
                    http_host=http_config.get("host", "127.0.0.1"),
                    http_port=port,
                    http_path=http_config.get("path", "/mcp"),
                    log_level=http_config.get("log_level", "info"),
                )
            else:
                # Old flat format
                nested_configs["transport"] = TransportSettings(**transport_data)

        if "task_lease" in data:
            nested_configs["task_lease"] = TaskLeaseSettings(**data["task_lease"])

        if "registration" in data:
            nested_configs["registration"] = RegistrationSettings(
                **data["registration"]
            )

        if "board_health" in data:
            nested_configs["board_health"] = BoardHealthSettings(**data["board_health"])

        if "multi_endpoint" in data:
            multi_ep_data = data["multi_endpoint"]
            nested_configs["multi_endpoint"] = MultiEndpointSettings(
                human=(
                    EndpointSettings(**multi_ep_data.get("human", {}))
                    if "human" in multi_ep_data
                    else EndpointSettings(port=4298)
                ),
                agent=(
                    EndpointSettings(**multi_ep_data.get("agent", {}))
                    if "agent" in multi_ep_data
                    else EndpointSettings(port=4299)
                ),
                analytics=(
                    EndpointSettings(**multi_ep_data.get("analytics", {}))
                    if "analytics" in multi_ep_data
                    else EndpointSettings(port=4300)
                ),
            )

        if "hybrid_inference" in data:
            nested_configs["hybrid_inference"] = HybridInferenceSettings(
                **data["hybrid_inference"]
            )

        if "logging" in data:
            nested_configs["logging"] = LoggingSettings(**data["logging"])

        # Extract top-level settings
        top_level = {
            "auto_find_board": data.get("auto_find_board", False),
            "single_project_mode": data.get("single_project_mode", True),
            "default_project_name": data.get("default_project_name", None),
            "log_level": data.get("log_level", "INFO"),
            "data_dir": data.get("data_dir", "./data"),
            "cache_dir": data.get("cache_dir", "./cache"),
        }

        # Combine nested and top-level
        return cls(**nested_configs, **top_level)

    def validate(self) -> None:
        """Validate configuration settings.

        Raises clear error messages for invalid configuration,
        making it easy for users to fix issues.

        Validation checks:
        - AI provider has required API keys
        - Kanban provider has required credentials
        - Ports are valid (1-65535)
        - Paths exist or can be created

        Raises
        ------
        ValueError
            If configuration is invalid, with detailed message

        Examples
        --------
        >>> config = MarcusConfig.from_file("config.json")
        >>> config.validate()  # Raises ValueError if invalid
        """
        errors: list[str] = []

        # Validate AI settings
        if self.ai.enabled:
            if self.ai.provider == "anthropic":
                if not self.ai.anthropic_api_key:
                    errors.append(
                        "AI provider is 'anthropic' but anthropic_api_key is not set. "
                        "Set it in config_marcus.json or environment variable "
                        "CLAUDE_API_KEY"
                    )
            elif self.ai.provider == "openai":
                if not self.ai.openai_api_key:
                    errors.append(
                        "AI provider is 'openai' but openai_api_key is not set. "
                        "Set it in config_marcus.json or environment variable "
                        "OPENAI_API_KEY"
                    )
            elif self.ai.provider == "cloud":
                # Honor env var fallbacks — LLMAbstraction applies them at
                # runtime, so validation must check them too or it rejects
                # valid configs that rely on env-var credentials.
                effective_key = self.ai.cloud_api_key or os.environ.get(
                    "MARCUS_CLOUD_LLM_KEY", ""
                )
                if not effective_key:
                    errors.append(
                        "AI provider is 'cloud' but cloud_api_key is not set. "
                        "Set it in config_marcus.json or environment variable "
                        "MARCUS_CLOUD_LLM_KEY"
                    )
                effective_url = self.ai.cloud_url or os.environ.get(
                    "MARCUS_CLOUD_LLM_URL", ""
                )
                if not effective_url:
                    errors.append(
                        "AI provider is 'cloud' but cloud_url is not set. "
                        "Set it in config_marcus.json or environment variable "
                        "MARCUS_CLOUD_LLM_URL"
                    )
                # Catch the common mistake of leaving the Anthropic default
                # model name when switching to a cloud provider.
                if self.ai.model and self.ai.model.startswith("claude-"):
                    errors.append(
                        f"AI provider is 'cloud' but ai.model is set to an "
                        f"Anthropic model name ('{self.ai.model}'). "
                        "Set ai.model to a model available on your cloud "
                        "endpoint (e.g. "
                        "'accounts/fireworks/models/qwen2p5-coder-7b-instruct')"
                    )

            # Validate temperature
            if not 0.0 <= self.ai.temperature <= 1.0:
                errors.append(
                    f"AI temperature must be between 0.0 and 1.0, "
                    f"got {self.ai.temperature}"
                )

        # Validate Kanban settings
        if self.kanban.provider == "planka":
            if not self.kanban.planka_base_url:
                errors.append(
                    "Kanban provider is 'planka' but planka_base_url is not set"
                )
            if not self.kanban.planka_email:
                errors.append("Kanban provider is 'planka' but planka_email is not set")
            if not self.kanban.planka_password:
                errors.append(
                    "Kanban provider is 'planka' but planka_password is not set"
                )
        elif self.kanban.provider == "github":
            if not self.kanban.github_token:
                errors.append("Kanban provider is 'github' but github_token is not set")
            if not self.kanban.github_owner or not self.kanban.github_repo:
                errors.append(
                    "Kanban provider is 'github' but github_owner or "
                    "github_repo is not set"
                )
        elif self.kanban.provider == "linear":
            if not self.kanban.linear_api_key:
                errors.append(
                    "Kanban provider is 'linear' but linear_api_key is not set"
                )

        # Validate transport settings
        if self.transport.http_port < 1 or self.transport.http_port > 65535:
            errors.append(
                f"Transport HTTP port must be between 1-65535, "
                f"got {self.transport.http_port}"
            )

        # Validate directories can be created
        for dir_name, dir_path in [
            ("data_dir", self.data_dir),
            ("cache_dir", self.cache_dir),
        ]:
            try:
                path = Path(dir_path).expanduser()
                path.mkdir(parents=True, exist_ok=True)
            except (PermissionError, OSError, FileNotFoundError) as e:
                errors.append(f"Cannot create {dir_name} at {dir_path}: {e}")

        # If there are errors, raise with clear message
        if errors:
            error_message = "Configuration validation failed:\n\n"
            for i, error in enumerate(errors, 1):
                error_message += f"{i}. {error}\n"
            error_message += "\nPlease fix these issues in config_marcus.json"
            raise ValueError(error_message)


# Global config instance
_config: Optional[MarcusConfig] = None


def get_config() -> MarcusConfig:
    """Get or create global config instance.

    This function:
    1. Checks if config is already loaded
    2. If not, loads from config_marcus.json
    3. Validates configuration
    4. Returns config instance

    This should be called ONCE at application startup,
    then the result is reused throughout the application.

    Returns
    -------
    MarcusConfig
        The global configuration instance

    Raises
    ------
    ValueError
        If configuration validation fails

    Examples
    --------
    >>> from src.config.marcus_config import get_config
    >>> config = get_config()
    >>> api_key = config.ai.anthropic_api_key
    """
    global _config
    if _config is None:
        # Try to load from MARCUS_CONFIG env var, else default
        config_path = os.getenv("MARCUS_CONFIG", "config_marcus.json")
        _config = MarcusConfig.from_file(config_path)
        _config.validate()
    return _config


def reload_config() -> MarcusConfig:
    """Force reload configuration from file.

    Useful for:
    - Testing (reset config between tests)
    - Hot-reloading config changes

    Returns
    -------
    MarcusConfig
        The reloaded configuration

    Examples
    --------
    >>> config = reload_config()  # Force fresh load from disk
    """
    global _config
    config_path = os.getenv("MARCUS_CONFIG", "config_marcus.json")
    _config = MarcusConfig.from_file(config_path)
    _config.validate()
    return _config


def setup_logging() -> None:
    """Configure Python logging to write to stderr.

    This function configures Python's logging system to write to stderr,
    which the Marcus CLI script redirects to log files. This ensures that
    all logging from Marcus modules appears in the log files.

    The logging format matches the historical format:
    LEVEL:module.name:message

    Notes
    -----
    - Logs go to stderr, which the CLI redirects to logs/marcus_*.log
    - Uses force=True to override any existing configuration
    - Configures the root logger so all Marcus loggers inherit the config

    Examples
    --------
    >>> setup_logging()  # Call once at application startup
    """
    config = get_config()

    # Get log level from config, default to INFO
    log_level_str = config.logging.level.upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    # Simple basicConfig - logs go to stderr, CLI redirects to files
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s:%(name)s:%(message)s",
        stream=sys.stderr,
        force=True,  # Override any existing configuration
    )

    # Targeted DEBUG for the lease-recovery module. The grace-period and
    # candidate-filtering branches in ``check_expired_leases`` log at DEBUG;
    # at INFO they are invisible, which hides why an expired lease is never
    # recovered. Set MARCUS_DEBUG_LEASE=1 to promote ONLY
    # ``src.core.assignment_lease`` to DEBUG without flooding the rest of
    # the logs. basicConfig's root handler is NOTSET, so it emits these
    # records once the logger itself is at DEBUG.
    if os.getenv("MARCUS_DEBUG_LEASE"):
        logging.getLogger("src.core.assignment_lease").setLevel(logging.DEBUG)
        logger.info("MARCUS_DEBUG_LEASE set — src.core.assignment_lease at DEBUG")
