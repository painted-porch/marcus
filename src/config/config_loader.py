"""Centralized configuration loader for Marcus.

This module provides a single source of truth for loading configuration
from marcus.config.json with support for environment variable overrides.

Supports both legacy single-project and new multi-project configurations.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from src.config.config_path import resolve_config_candidates

logger = logging.getLogger(__name__)


class ConfigLoader:
    """Singleton configuration loader for Marcus."""

    _instance = None
    _config = None
    _config_path = None

    def __new__(cls) -> "ConfigLoader":
        """Create or return singleton instance."""
        if cls._instance is None:
            cls._instance = super(ConfigLoader, cls).__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """Initialize the config loader."""
        if self._config is None:
            self._load_config()

    def _load_config(self) -> None:
        """Load configuration from marcus.config.json."""
        # Find config file. resolve_config_candidates puts MARCUS_CONFIG
        # first when set (issue #891); the rest of this list is this
        # loader's own fallback order.
        possible_paths = resolve_config_candidates(
            Path.cwd() / "config_marcus.json",  # Current directory
            Path(__file__).parent.parent.parent / "config_marcus.json",  # Project root
            Path.home() / ".marcus" / "config_marcus.json",  # User home
        )

        for path in possible_paths:
            if path.exists():
                self._config_path = path
                break

        if self._config_path is None:
            raise FileNotFoundError(
                f"config_marcus.json not found. Tried: "
                f"{[str(p) for p in possible_paths]}. "
                "Please copy config_marcus.example.json to "
                "config_marcus.json and fill in your settings."
            )

        # Load the config file
        with open(self._config_path, "r") as f:
            self._config = json.load(f)

        # Check if this is a legacy config and migrate if needed
        self._migrate_legacy_config()

        # Apply environment variable overrides
        self._apply_env_overrides()

    def _apply_env_overrides(self) -> None:
        """Apply environment variable overrides to config."""
        # Map of environment variables to config paths
        env_mappings = {
            # Kanban provider
            "MARCUS_KANBAN_PROVIDER": "kanban.provider",
            # Planka
            "MARCUS_KANBAN_PLANKA_BASE_URL": "kanban.planka.base_url",
            "MARCUS_KANBAN_PLANKA_EMAIL": "kanban.planka.email",
            "MARCUS_KANBAN_PLANKA_PASSWORD": (
                "kanban.planka.password"
            ),  # pragma: allowlist secret
            "MARCUS_KANBAN_PLANKA_PROJECT_ID": "kanban.planka.project_id",
            "MARCUS_KANBAN_PLANKA_BOARD_ID": "kanban.planka.board_id",
            # GitHub
            "MARCUS_KANBAN_GITHUB_TOKEN": (
                "kanban.github.token"
            ),  # pragma: allowlist secret
            "MARCUS_KANBAN_GITHUB_OWNER": "kanban.github.owner",
            "MARCUS_KANBAN_GITHUB_REPO": "kanban.github.repo",
            # Linear
            "MARCUS_KANBAN_LINEAR_API_KEY": (
                "kanban.linear.api_key"
            ),  # pragma: allowlist secret
            "MARCUS_KANBAN_LINEAR_TEAM_ID": "kanban.linear.team_id",
            # AI
            "MARCUS_AI_ANTHROPIC_API_KEY": (
                "ai.anthropic_api_key"
            ),  # pragma: allowlist secret
            "MARCUS_AI_OPENAI_API_KEY": (
                "ai.openai_api_key"
            ),  # pragma: allowlist secret
            "MARCUS_AI_MODEL": "ai.model",
            "MARCUS_LLM_PROVIDER": "ai.provider",
            "MARCUS_LOCAL_LLM_PATH": "ai.local_model",
            "MARCUS_LOCAL_LLM_URL": "ai.local_url",
            "MARCUS_LOCAL_LLM_KEY": "ai.local_key",  # pragma: allowlist secret
            "MARCUS_AI_ENABLED": "ai.enabled",
            # Monitoring
            "MARCUS_MONITORING_INTERVAL": "monitoring.interval",
            # Communication
            "MARCUS_SLACK_ENABLED": "communication.slack_enabled",
            "MARCUS_SLACK_WEBHOOK_URL": "communication.slack_webhook_url",
            "MARCUS_EMAIL_ENABLED": "communication.email_enabled",
            # Advanced
            "MARCUS_DEBUG": "advanced.debug",
            "MARCUS_PORT": "advanced.port",
        }

        for env_var, config_path in env_mappings.items():
            if env_var in os.environ:
                self._set_nested_value(config_path, os.environ[env_var])

    def _set_nested_value(self, path: str, value: str) -> None:
        """Set a nested value in the config using dot notation."""
        keys = path.split(".")
        config = self._config

        # Navigate to the parent of the target key
        for key in keys[:-1]:
            if config is None:
                return
            if key not in config:
                config[key] = {}
            config = config[key]

        # Set the value, converting types as needed
        final_key = keys[-1]

        # Type conversion based on current value type
        if config is not None and final_key in config:
            current_value = config[final_key]
            if isinstance(current_value, bool):
                config[final_key] = value.lower() in ("true", "1", "yes", "on")
            elif isinstance(current_value, int):
                config[final_key] = int(value)
            elif isinstance(current_value, float):
                config[final_key] = float(value)
            else:
                config[final_key] = value
        elif config is not None:
            # Default to string if key doesn't exist
            config[final_key] = value

    def get(self, path: str, default: Any = None) -> Any:
        """Get a configuration value using dot notation.

        Parameters
        ----------
        path : str
            Dot-separated path to the config value (e.g., 'kanban.provider')
        default : Any
            Default value if path doesn't exist

        Returns
        -------
        Any
            The configuration value or default
        """
        keys = path.split(".")
        value = self._config

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default

        return value

    def get_feature_config(self, feature: str) -> Dict[str, Any]:
        """Get feature configuration with backward compatibility.

        Supports both old boolean format and new object format:
        - Old: "events": true
        - New: "events": {"enabled": true, "store_history": true}

        Parameters
        ----------
        feature : str
            Feature name (events, context, memory, visibility)

        Returns
        -------
        Dict[str, Any]
            Feature configuration dictionary
        """
        config = self.get(f"features.{feature}")

        if config is None:
            # Feature not configured
            return {"enabled": False}
        elif isinstance(config, bool):
            # Old format - convert to new
            return {"enabled": config}
        elif isinstance(config, dict):
            # New format - ensure 'enabled' field exists
            if "enabled" not in config:
                config["enabled"] = True
            return config
        else:
            # Invalid format
            return {"enabled": False}

    def get_kanban_config(self) -> Dict[str, Any]:
        """Get the complete kanban configuration for the selected provider."""
        provider = self.get("kanban.provider", "planka")
        base_config = {"provider": provider, **self.get(f"kanban.{provider}", {})}
        return base_config

    def get_ai_config(self) -> Dict[str, Any]:
        """Get the complete AI configuration."""
        result = self.get("ai", {})
        return result if isinstance(result, dict) else {}

    def get_monitoring_config(self) -> Dict[str, Any]:
        """Get the complete monitoring configuration."""
        result = self.get("monitoring", {})
        return result if isinstance(result, dict) else {}

    def get_communication_config(self) -> Dict[str, Any]:
        """Get the complete communication configuration."""
        result = self.get("communication", {})
        return result if isinstance(result, dict) else {}

    def get_hybrid_inference_config(self) -> Any:
        """Get the hybrid inference configuration."""
        from src.config.hybrid_inference_config import HybridInferenceConfig

        config_dict = self.get("hybrid_inference", {})
        if not config_dict:
            # Return default config
            return HybridInferenceConfig()

        try:
            config = HybridInferenceConfig.from_dict(config_dict)
            config.validate()
            return config
        except Exception as e:
            logger.warning(f"Invalid hybrid inference config, using defaults: {e}")
            return HybridInferenceConfig()

    def _migrate_legacy_config(self) -> None:
        """Migrate legacy single-project config to multi-project format."""
        # Check if this is a legacy config (has project_id but no projects section)
        if (
            self._config is not None
            and "project_id" in self._config
            and "projects" not in self._config
        ):
            logger.info(
                "Detected legacy configuration format. "
                "Migrating to multi-project format..."
            )

            # Create a default project from legacy config
            import uuid

            default_project_id = str(uuid.uuid4())

            # Determine provider
            provider = (
                self._config.get("kanban", {}).get("provider", "planka")
                if self._config
                else "planka"
            )

            # Extract provider-specific config
            provider_config = {}
            if provider == "planka" and self._config:
                provider_config = {
                    "project_id": self._config.get("project_id"),
                    "board_id": self._config.get("board_id"),
                }
            elif provider == "github" and self._config:
                github_cfg = self._config.get("github", {})
                provider_config = {
                    "owner": github_cfg.get("owner"),
                    "repo": github_cfg.get("repo"),
                    "project_number": github_cfg.get("project_number", 1),
                }
            elif provider == "linear" and self._config:
                linear_cfg = self._config.get("linear", {})
                provider_config = {
                    "team_id": linear_cfg.get("team_id"),
                    "project_id": linear_cfg.get("project_id"),
                }

            # Create projects section
            if self._config:
                self._config["projects"] = {
                    default_project_id: {
                        "name": self._config.get("project_name", "Default Project"),
                        "provider": provider,
                        "config": provider_config,
                        "tags": ["default", "migrated"],
                    }
                }

                # Set active project
                self._config["active_project"] = default_project_id

            # Move provider credentials to providers section
            if self._config and "providers" not in self._config:
                self._config["providers"] = {}

            if self._config and "planka" in self._config:
                self._config["providers"]["planka"] = self._config["planka"]
            if self._config and "github" in self._config:
                self._config["providers"]["github"] = self._config["github"]
            if self._config and "linear" in self._config:
                self._config["providers"]["linear"] = self._config["linear"]

            # IMPORTANT: Preserve ai section during migration
            # The ai config should remain at top level for backward compatibility
            # Don't move or remove it - just leave it in place

            logger.info(
                f"Migration complete. Created default project with ID: "
                f"{default_project_id}"
            )

        # Clean up stale fields from root config (applies to all configs)
        if self._config:
            # Migrate project_name to default_project_name if it exists
            if (
                "project_name" in self._config
                and "default_project_name" not in self._config
            ):
                self._config["default_project_name"] = self._config["project_name"]
                logger.info(
                    f"Migrated project_name to default_project_name: "
                    f"{self._config['project_name']}"
                )

            # Remove stale runtime state fields
            stale_fields = ["project_id", "board_id", "project_name", "board_name"]
            removed_fields = []
            for field in stale_fields:
                if field in self._config:
                    del self._config[field]
                    removed_fields.append(field)

            if removed_fields:
                logger.info(f"Removed stale config fields: {', '.join(removed_fields)}")

    def is_multi_project_mode(self) -> bool:
        """Check if config is in multi-project mode."""
        return bool(self._config is not None and "projects" in self._config)

    def get_projects_config(self) -> Dict[str, Any]:
        """Get all project configurations."""
        if self._config is None:
            return {}
        result = self._config.get("projects", {})
        return result if isinstance(result, dict) else {}

    def get_active_project_id(self) -> Optional[str]:
        """Get the active project ID."""
        if self._config is None:
            return None
        result = self._config.get("active_project")
        return result if isinstance(result, str) or result is None else None

    def get_provider_credentials(self, provider: str) -> Dict[str, Any]:
        """Get credentials for a specific provider."""
        if self._config is None:
            return {}
        providers = self._config.get("providers", {})
        if not isinstance(providers, dict):
            return {}
        result = providers.get(provider, {})
        return result if isinstance(result, dict) else {}

    def reload(self) -> None:
        """Reload the configuration from disk."""
        self._config = None
        self._load_config()

    @property
    def config_path(self) -> Path:
        """Get the path to the loaded config file."""
        if self._config_path is None:
            raise RuntimeError("Config not loaded yet")
        return self._config_path

    def __repr__(self) -> str:
        """Return string representation of ConfigLoader."""
        return f"ConfigLoader(config_path={self._config_path})"


# Global singleton instance
_config_loader = None


def get_config() -> ConfigLoader:
    """Get the global config loader instance."""
    global _config_loader
    if _config_loader is None:
        _config_loader = ConfigLoader()
    return _config_loader


# Convenience functions for common access patterns
def get_config_value(path: str, default: Any = None) -> Any:
    """Get a configuration value using dot notation."""
    return get_config().get(path, default)


def get_kanban_provider() -> str:
    """Get the configured kanban provider."""
    result = get_config().get("kanban.provider", "planka")
    return result if isinstance(result, str) else "planka"


def get_anthropic_api_key() -> Optional[str]:
    """Get the Anthropic API key."""
    result = get_config().get("ai.anthropic_api_key")
    return result if isinstance(result, str) or result is None else None


def get_planka_config() -> Dict[str, Any]:
    """Get Planka configuration."""
    result = get_config().get("kanban.planka", {})
    return result if isinstance(result, dict) else {}
