"""Configuration management for Marcus.

This module provides the Settings class which handles loading, validation,
and management of configuration settings from multiple sources including
JSON files, environment variables, and default values.
"""

import json
import os
from typing import Any, Dict, Optional

from .config_loader import get_config
from .config_path import resolve_config_candidates


class Settings:
    """Configuration management system for Marcus.

    Manages configuration loading from multiple sources with support for
    hierarchical settings, environment variable overrides, and validation.
    Provides specialized getters for different configuration categories.

    Parameters
    ----------
    config_path : Optional[str], default=None
        Path to the configuration file. If None, uses MARCUS_CONFIG
        environment variable or defaults to 'config/marcus_config.json'

    Attributes
    ----------
    config_path : str
        Path to the configuration file
    defaults : Dict[str, Any]
        Default configuration values
    config : Dict[str, Any]
        Current loaded configuration

    Examples
    --------
    >>> settings = Settings()
    >>> monitoring_interval = settings.get('monitoring_interval')
    >>> risk_config = settings.get_risk_thresholds()
    >>> settings.set('custom_setting', 'value')
    >>> settings.save()

    Notes
    -----
    Configuration loading priority (highest to lowest):
    1. Environment variables
    2. Configuration file
    3. Default values
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        # resolve_config_candidates puts MARCUS_CONFIG first when set
        # (issue #891), same as every other config-loading path.
        self.config_path = config_path or str(
            resolve_config_candidates("marcus.config.json")[0]
        )

        # Default configuration
        self.defaults = {
            # Monitoring settings
            "monitoring_interval": 900,  # 15 minutes
            "stall_threshold_hours": 24,
            "health_check_interval": 3600,  # 1 hour
            # Risk thresholds
            "risk_thresholds": {
                "high_risk": 0.8,
                "medium_risk": 0.5,
                "timeline_buffer": 0.2,
            },
            # Escalation rules
            "escalation_rules": {
                "stuck_task_hours": 8,
                "blocker_escalation_hours": 4,
                "critical_path_delay_hours": 2,
            },
            # Communication settings
            "communication_rules": {
                "daily_plan_time": "08:00",
                "progress_check_time": "14:00",
                "end_of_day_summary": "18:00",
            },
            # Integration settings
            "slack_enabled": False,
            "email_enabled": False,
            "kanban_comments_enabled": True,
            # Team configuration
            "team_config": {
                "default": {
                    "skills": [],
                    "work_patterns": {
                        "preferred_hours": "9am-6pm",
                        "deep_work_blocks": "2-4 hours",
                        "context_switch_cost": "medium",
                    },
                    "communication_preferences": {
                        "notification_frequency": "medium",
                        "detailed_instructions": True,
                        "technical_depth": "medium",
                    },
                }
            },
            # Performance settings
            "performance": {
                "max_concurrent_analyses": 5,
                "cache_ttl_seconds": 300,
                "batch_notification_delay": 1.0,
            },
            # AI settings
            "ai_settings": {
                "model": "claude-3-sonnet-20241022",
                "temperature": 0.7,
                "max_tokens": 2000,
                "retry_attempts": 3,
                "retry_delay": 1.0,
            },
            # Feature flags
            "features": {
                # Enable automatic task decomposition into subtasks
                "enable_subtasks": True,
            },
        }

        # Load configuration
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from file and environment variables.

        Loads configuration in priority order: defaults, then file config,
        then environment variable overrides.

        Returns
        -------
        Dict[str, Any]
            Complete merged configuration dictionary

        Notes
        -----
        Uses the centralized ConfigLoader for consistency.
        """
        # Start with defaults
        config = self.defaults.copy()

        # If a specific config path was provided (e.g., in tests), load from it
        if (
            self.config_path
            and os.path.exists(self.config_path)
            and self.config_path != "marcus.config.json"
        ):
            try:
                with open(self.config_path, "r") as f:
                    file_config = json.load(f)
                    config = self._deep_merge(config, file_config)
            except Exception as e:
                # Don't print to stdout - corrupts MCP protocol
                import sys

                print(f"Error loading config file: {e}", file=sys.stderr)
        else:
            try:
                # Get configuration from centralized loader
                config_loader = get_config()

                # Merge relevant settings from marcus.config.json
                monitoring_config = config_loader.get("monitoring", {})
                if monitoring_config:
                    config["monitoring_interval"] = monitoring_config.get(
                        "interval", config["monitoring_interval"]
                    )
                    config["stall_threshold_hours"] = monitoring_config.get(
                        "stall_threshold_hours", config["stall_threshold_hours"]
                    )
                    config["health_check_interval"] = monitoring_config.get(
                        "health_check_interval", config["health_check_interval"]
                    )
                    if "risk_thresholds" in monitoring_config:
                        if isinstance(config["risk_thresholds"], dict) and isinstance(
                            monitoring_config["risk_thresholds"], dict
                        ):
                            config["risk_thresholds"].update(
                                monitoring_config["risk_thresholds"]
                            )

                # Get escalation settings
                escalation_config = config_loader.get("escalation", {})
                if escalation_config and isinstance(config["escalation_rules"], dict):
                    config["escalation_rules"].update(escalation_config)

                # Get communication settings
                comm_config = config_loader.get("communication", {})
                if comm_config:
                    config["slack_enabled"] = comm_config.get(
                        "slack_enabled", config["slack_enabled"]
                    )
                    config["email_enabled"] = comm_config.get(
                        "email_enabled", config["email_enabled"]
                    )
                    config["kanban_comments_enabled"] = comm_config.get(
                        "kanban_comments_enabled", config["kanban_comments_enabled"]
                    )
                    if "rules" in comm_config and isinstance(
                        config["communication_rules"], dict
                    ):
                        config["communication_rules"].update(comm_config["rules"])

                # Get AI settings
                ai_config = config_loader.get("ai", {})
                if ai_config and isinstance(config["ai_settings"], dict):
                    config["ai_settings"]["model"] = ai_config.get(
                        "model", config["ai_settings"]["model"]
                    )
                    config["ai_settings"]["temperature"] = ai_config.get(
                        "temperature", config["ai_settings"]["temperature"]
                    )
                    config["ai_settings"]["max_tokens"] = ai_config.get(
                        "max_tokens", config["ai_settings"]["max_tokens"]
                    )

                # Get feature flags
                features_config = config_loader.get("features", {})
                if features_config and isinstance(config["features"], dict):
                    config["features"]["enable_subtasks"] = features_config.get(
                        "enable_subtasks", config["features"]["enable_subtasks"]
                    )

            except Exception as e:
                # If config loader fails, just use defaults
                # Don't print to stdout - corrupts MCP protocol
                import sys

                print(
                    f"Warning: Could not load from config loader: {e}", file=sys.stderr
                )

        # Environment variables still override everything
        config = self._load_env_overrides(config)

        return config

    def _deep_merge(
        self, base: Dict[str, Any], update: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Recursively merge two dictionaries.

        Performs deep merge where nested dictionaries are merged recursively
        rather than replaced entirely.

        Parameters
        ----------
        base : Dict[str, Any]
            Base dictionary to merge into
        update : Dict[str, Any]
            Dictionary with updates to merge

        Returns
        -------
        Dict[str, Any]
            New dictionary with merged values

        Examples
        --------
        >>> base = {'a': {'x': 1}, 'b': 2}
        >>> update = {'a': {'y': 3}, 'c': 4}
        >>> result = settings._deep_merge(base, update)
        >>> result
        {'a': {'x': 1, 'y': 3}, 'b': 2, 'c': 4}
        """
        result = base.copy()

        for key, value in update.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value

        return result

    def _load_env_overrides(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Load configuration overrides from environment variables.

        Checks for specific environment variables and updates the configuration
        with their values, performing type conversion as needed.

        Parameters
        ----------
        config : Dict[str, Any]
            Base configuration to update

        Returns
        -------
        Dict[str, Any]
            Configuration with environment variable overrides applied

        Notes
        -----
        Supported environment variables:
        - MARCUS_MONITORING_INTERVAL: Monitoring interval in seconds
        - MARCUS_SLACK_ENABLED: Enable/disable Slack (true/false)
        - SLACK_WEBHOOK_URL: Slack webhook URL
        - MARCUS_EMAIL_ENABLED: Enable/disable email (true/false)
        - CLAUDE_API_KEY: API key for Anthropic services. ``ANTHROPIC_API_KEY``
          is intentionally NOT consulted because setting it in the shell
          switches Claude Code from subscription to API billing.
        """
        # Monitoring interval
        if "MARCUS_MONITORING_INTERVAL" in os.environ:
            config["monitoring_interval"] = int(
                os.environ["MARCUS_MONITORING_INTERVAL"]
            )

        # Slack integration
        if "MARCUS_SLACK_ENABLED" in os.environ:
            config["slack_enabled"] = (
                os.environ["MARCUS_SLACK_ENABLED"].lower() == "true"
            )

        if "SLACK_WEBHOOK_URL" in os.environ:
            config["slack_webhook_url"] = os.environ["SLACK_WEBHOOK_URL"]

        # Email integration
        if "MARCUS_EMAIL_ENABLED" in os.environ:
            config["email_enabled"] = (
                os.environ["MARCUS_EMAIL_ENABLED"].lower() == "true"
            )

        # API keys — read CLAUDE_API_KEY (NOT ANTHROPIC_API_KEY) so we
        # don't conflict with Claude Code's subscription auth.
        if "CLAUDE_API_KEY" in os.environ:
            config["anthropic_api_key"] = os.environ["CLAUDE_API_KEY"]

        # Feature flags
        if "MARCUS_ENABLE_SUBTASKS" in os.environ:
            if "features" not in config:
                config["features"] = {}
            config["features"]["enable_subtasks"] = (
                os.environ["MARCUS_ENABLE_SUBTASKS"].lower() == "true"
            )

        return config

    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value using dot notation key.

        Supports nested key access using dot notation
        (e.g., 'risk_thresholds.high_risk').

        Parameters
        ----------
        key : str
            Configuration key, supports dot notation for nested access
        default : Any, default=None
            Default value to return if key is not found

        Returns
        -------
        Any
            Configuration value or default if not found

        Examples
        --------
        >>> value = settings.get('monitoring_interval')
        >>> nested = settings.get('risk_thresholds.high_risk', 0.8)
        >>> missing = settings.get('nonexistent.key', 'fallback')
        """
        keys = key.split(".")
        value = self.config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def set(self, key: str, value: Any) -> None:
        """Set configuration value using dot notation key.

        Creates nested dictionary structure as needed when using dot notation.

        Parameters
        ----------
        key : str
            Configuration key, supports dot notation for nested setting
        value : Any
            Value to set

        Examples
        --------
        >>> settings.set('monitoring_interval', 600)
        >>> settings.set('custom.nested.setting', 'value')

        Notes
        -----
        Setting values does not automatically save to file. Call save() to persist.
        """
        keys = key.split(".")
        config = self.config

        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]

        config[keys[-1]] = value

    def save(self) -> None:
        """Save current configuration to file.

        Writes the current configuration to the configured file path,
        creating directories as needed.

        Raises
        ------
        OSError
            If file cannot be written or directory cannot be created
        json.JSONEncodeError
            If configuration contains non-serializable values

        Examples
        --------
        >>> settings.set('new_setting', 'value')
        >>> settings.save()
        """
        if self.config_path:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)

            with open(self.config_path, "w") as f:
                json.dump(self.config, f, indent=2)

    def get_team_config(self, team_name: str = "default") -> Dict[str, Any]:
        """Get configuration for a specific team.

        Retrieves team-specific configuration including skills, work patterns,
        and communication preferences. Falls back to default team config.

        Parameters
        ----------
        team_name : str, default="default"
            Name of the team to get configuration for

        Returns
        -------
        Dict[str, Any]
            Team configuration containing:
            - skills: List of team skills
            - work_patterns: Work scheduling preferences
            - communication_preferences: Notification settings

        Examples
        --------
        >>> backend_config = settings.get_team_config("backend_team")
        >>> default_config = settings.get_team_config()
        """
        team_configs = self.get("team_config", {})
        if isinstance(team_configs, dict):
            result = team_configs.get(team_name)
            if result is not None and isinstance(result, dict):
                return dict(result)
            default_result = team_configs.get("default")
            if default_result is not None and isinstance(default_result, dict):
                return dict(default_result)
        return {}

    def get_risk_thresholds(self) -> Dict[str, float]:
        """Get risk assessment thresholds for project monitoring.

        Returns threshold values used for risk assessment calculations
        in project monitoring and alerting systems.

        Returns
        -------
        Dict[str, float]
            Risk thresholds containing:
            - high_risk: Threshold for high risk classification (0.0-1.0)
            - medium_risk: Threshold for medium risk classification (0.0-1.0)
            - timeline_buffer: Buffer percentage for timeline calculations

        Examples
        --------
        >>> thresholds = settings.get_risk_thresholds()
        >>> if project_risk > thresholds['high_risk']:
        ...     send_alert()
        """
        risk_thresholds = self.get("risk_thresholds", self.defaults["risk_thresholds"])
        if isinstance(risk_thresholds, dict):
            return risk_thresholds
        # Type assertion - defaults is known to have correct structure
        from typing import cast

        return cast(Dict[str, float], self.defaults["risk_thresholds"])

    def get_escalation_rules(self) -> Dict[str, int]:
        """Get escalation rules for automated issue management.

        Returns timing rules that determine when issues should be
        escalated to higher levels of management.

        Returns
        -------
        Dict[str, int]
            Escalation rules containing:
            - stuck_task_hours: Hours before escalating stuck tasks
            - blocker_escalation_hours: Hours before escalating blockers
            - critical_path_delay_hours: Hours before escalating critical delays

        Examples
        --------
        >>> rules = settings.get_escalation_rules()
        >>> if task_stuck_hours > rules['stuck_task_hours']:
        ...     escalate_task(task)
        """
        escalation_rules = self.get(
            "escalation_rules", self.defaults["escalation_rules"]
        )
        if isinstance(escalation_rules, dict):
            return escalation_rules
        # Type assertion - defaults is known to have correct structure
        from typing import cast

        return cast(Dict[str, int], self.defaults["escalation_rules"])

    def get_communication_rules(self) -> Dict[str, str]:
        """Get communication timing rules for automated messaging.

        Returns scheduling rules that determine when different types
        of automated communications should be sent.

        Returns
        -------
        Dict[str, str]
            Communication timing rules containing:
            - daily_plan_time: Time to send daily work plans (HH:MM format)
            - progress_check_time: Time for progress check messages
            - end_of_day_summary: Time to send daily summaries

        Examples
        --------
        >>> rules = settings.get_communication_rules()
        >>> daily_time = rules['daily_plan_time']  # "08:00"
        >>> schedule_daily_plans(daily_time)
        """
        communication_rules = self.get(
            "communication_rules", self.defaults["communication_rules"]
        )
        if isinstance(communication_rules, dict):
            return communication_rules
        # Type assertion - defaults is known to have correct structure
        from typing import cast

        return cast(Dict[str, str], self.defaults["communication_rules"])

    def get_ai_settings(self) -> Dict[str, Any]:
        """Get AI model configuration settings.

        Returns configuration for AI model integration including
        model selection, parameters, and retry behavior.

        Returns
        -------
        Dict[str, Any]
            AI settings containing:
            - model: Model identifier string
            - temperature: Sampling temperature (0.0-1.0)
            - max_tokens: Maximum response tokens
            - retry_attempts: Number of retry attempts
            - retry_delay: Delay between retries in seconds

        Examples
        --------
        >>> ai_config = settings.get_ai_settings()
        >>> client = AIClient(
        ...     model=ai_config['model'],
        ...     temperature=ai_config['temperature']
        ... )
        """
        ai_settings = self.get("ai_settings", self.defaults["ai_settings"])
        if isinstance(ai_settings, dict):
            return ai_settings
        # Type assertion - defaults is known to have correct structure
        from typing import cast

        return cast(Dict[str, Any], self.defaults["ai_settings"])

    def is_subtasks_enabled(self) -> bool:
        """Check if subtask functionality is enabled.

        Returns whether automatic task decomposition and subtask
        assignment features are enabled.

        Returns
        -------
        bool
            True if subtasks are enabled, False otherwise

        Examples
        --------
        >>> if settings.is_subtasks_enabled():
        ...     decompose_task(task)
        >>> # Can also be set via environment:
        >>> # export MARCUS_ENABLE_SUBTASKS=false

        Notes
        -----
        Can be controlled via:
        - Configuration file: features.enable_subtasks
        - Environment variable: MARCUS_ENABLE_SUBTASKS=true/false
        """
        result = self.get("features.enable_subtasks", True)
        return bool(result)

    def validate(self) -> bool:
        """Validate current configuration for consistency and completeness.

        Performs validation checks on the loaded configuration to ensure
        required settings are present and values are within acceptable ranges.
        Prints warnings for potential issues.

        Returns
        -------
        bool
            True if configuration is valid, False if critical issues found

        Notes
        -----
        Validation checks include:
        - Presence of required API keys
        - Reasonable monitoring intervals
        - At least one communication channel enabled

        Warnings are printed to stdout for non-critical issues.

        Examples
        --------
        >>> if settings.validate():
        ...     start_marcus()
        ... else:
        ...     fix_configuration()
        """
        # Check for API key if AI is being used
        if not os.environ.get("CLAUDE_API_KEY"):
            # Don't print to stdout - corrupts MCP protocol
            import sys

            print("Warning: CLAUDE_API_KEY not found in environment", file=sys.stderr)

        # Validate monitoring interval
        monitoring_interval = self.get("monitoring_interval")
        if monitoring_interval < 60:
            # Don't print to stdout - corrupts MCP protocol
            import sys

            print(
                "Warning: Monitoring interval is very short (< 60 seconds)",
                file=sys.stderr,
            )

        # Validate communication channels
        if not any(
            [
                self.get("slack_enabled"),
                self.get("email_enabled"),
                self.get("kanban_comments_enabled"),
            ]
        ):
            # Don't print to stdout - corrupts MCP protocol
            import sys

            print("Warning: No communication channels enabled", file=sys.stderr)

        return True
