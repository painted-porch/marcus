import json
import os
import tempfile
from unittest.mock import patch

import pytest

from src.config.settings import Settings

pytestmark = pytest.mark.unit


class TestSettings:
    def test_default_settings(self):
        """Test loading default settings.

        Settings._load_config falls back to src.config.config_loader's
        ConfigLoader whenever config_path points at a file that doesn't
        exist. ConfigLoader searches several fixed locations, including
        the project root (via __file__), independent of the test's own
        cwd -- so an unpatched ConfigLoader silently picks up whatever
        real config_marcus.json happens to exist on the machine running
        the test (issue #879: this assertion used to read 0.1, matching
        one developer's personal, untracked config_marcus.json, instead
        of the code's real, documented default of 0.7). Patched so this
        test asserts the code's actual default regardless of machine.
        """
        with patch(
            "src.config.settings.get_config",
            side_effect=FileNotFoundError("no config_marcus.json for this test"),
        ):
            settings = Settings(config_path="nonexistent.json")

        # Check default values
        assert settings.get("monitoring_interval") == 900
        assert settings.get("kanban_comments_enabled") is True
        assert settings.get("slack_enabled") is False

        # Check nested values
        assert settings.get("risk_thresholds.high_risk") == 0.8
        assert settings.get("ai_settings.temperature") == 0.7

    def test_load_from_file(self):
        """Test loading settings from file"""
        config_data = {
            "monitoring_interval": 600,
            "slack_enabled": True,
            "custom_setting": "test_value",
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config_data, f)
            temp_path = f.name

        try:
            settings = Settings(config_path=temp_path)

            assert settings.get("monitoring_interval") == 600
            assert settings.get("slack_enabled") is True
            assert settings.get("custom_setting") == "test_value"
            # Default values should still be present
            assert settings.get("kanban_comments_enabled") is True
        finally:
            os.unlink(temp_path)

    def test_environment_overrides(self):
        """Test environment variable overrides"""
        os.environ["MARCUS_MONITORING_INTERVAL"] = "1200"
        os.environ["MARCUS_SLACK_ENABLED"] = "true"

        try:
            settings = Settings(config_path="nonexistent.json")

            assert settings.get("monitoring_interval") == 1200
            assert settings.get("slack_enabled") is True
        finally:
            del os.environ["MARCUS_MONITORING_INTERVAL"]
            del os.environ["MARCUS_SLACK_ENABLED"]

    def test_get_nested_values(self):
        """Test getting nested configuration values"""
        settings = Settings(config_path="nonexistent.json")

        # Test nested get
        assert settings.get("risk_thresholds.medium_risk") == 0.5
        assert settings.get("team_config.default.skills") == []

        # Test non-existent nested key
        assert settings.get("non.existent.key", "default") == "default"

    def test_set_values(self):
        """Test setting configuration values"""
        settings = Settings(config_path="nonexistent.json")

        # Set simple value
        settings.set("new_key", "new_value")
        assert settings.get("new_key") == "new_value"

        # Set nested value
        settings.set("nested.key.value", 42)
        assert settings.get("nested.key.value") == 42

    def test_team_config(self):
        """Test team configuration retrieval"""
        config_data = {
            "team_config": {
                "backend_team": {
                    "skills": ["python", "django"],
                    "work_patterns": {"preferred_hours": "9am-5pm"},
                },
                "default": {
                    "skills": [],
                    "work_patterns": {"preferred_hours": "flexible"},
                },
            }
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config_data, f)
            temp_path = f.name

        try:
            settings = Settings(config_path=temp_path)

            # Get specific team config
            backend_config = settings.get_team_config("backend_team")
            assert "python" in backend_config["skills"]

            # Get default team config
            default_config = settings.get_team_config("nonexistent_team")
            assert default_config["work_patterns"]["preferred_hours"] == "flexible"
        finally:
            os.unlink(temp_path)

    def test_validation(self):
        """Test configuration validation"""
        settings = Settings(config_path="nonexistent.json")

        # Test with very short monitoring interval
        settings.set("monitoring_interval", 30)
        result = settings.validate()
        # Validation should return True but print a warning for short intervals
        assert result is True
