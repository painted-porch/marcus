"""Unit tests for the lifecycle event helpers in ``src/telemetry/events``.

Stage 2 of #9.  Three event helpers each callable from the
corresponding MCP tool:

- ``fire_project_created`` (hooked into ``create_project``)

Helpers are small, testable units that build a properties dict and
call ``TelemetryClient.capture(...)``.  Each helper swallows all
exceptions — telemetry must never crash the MCP tool path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test ``~``; clears MARCUS_TELEMETRY / MARCUS_RUNNER."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MARCUS_TELEMETRY", raising=False)
    monkeypatch.delenv("MARCUS_POSTHOG_API_KEY", raising=False)
    monkeypatch.delenv("MARCUS_RUNNER", raising=False)
    return tmp_path


@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch ``get_telemetry_client`` to return a MagicMock."""
    client = MagicMock()
    monkeypatch.setattr("src.telemetry.events.get_telemetry_client", lambda: client)
    return client


class TestFireProjectCreated:
    """``fire_project_created`` emits the ``project_created`` event."""

    def test_calls_capture_with_event_name(
        self, isolated_home: Path, mock_client: Any
    ) -> None:
        """Helper calls ``client.capture("project_created", ...)``."""
        from src.telemetry.events import fire_project_created

        fire_project_created(
            result={"success": True, "tasks_created": 14},
            options={"complexity": "standard"},
            actual_decomposer="contract_first",
        )

        mock_client.capture.assert_called_once()
        args, _ = mock_client.capture.call_args
        assert args[0] == "project_created"

    def test_payload_carries_required_fields(
        self, isolated_home: Path, mock_client: Any
    ) -> None:
        """Every key promised by docs/telemetry.md § project_created.

        Falsification recipe: rename ``task_count`` key in
        ``fire_project_created`` and confirm this test fails on
        the missing-keys assertion.
        """
        from src.telemetry.events import fire_project_created

        fire_project_created(
            result={"success": True, "tasks_created": 14},
            options={"complexity": "standard"},
            actual_decomposer="feature_based",
        )

        _, kwargs = mock_client.capture.call_args
        # Properties are the second positional arg.
        props = mock_client.capture.call_args[0][1]

        required = {
            "task_count",
            "complexity_mode",
            "decomposer_strategy",
            "was_fallback",
            "structural_category",
            "domain",
        }
        missing = required - set(props.keys())
        assert not missing, (
            f"project_created payload missing keys promised in "
            f"docs/telemetry.md: {missing}"
        )

    def test_task_count_from_result(
        self, isolated_home: Path, mock_client: Any
    ) -> None:
        """task_count is read from ``result['tasks_created']``."""
        from src.telemetry.events import fire_project_created

        fire_project_created(
            result={"success": True, "tasks_created": 27},
            options={},
            actual_decomposer="contract_first",
        )
        props = mock_client.capture.call_args[0][1]
        assert props["task_count"] == 27

    def test_complexity_defaults_to_standard(
        self, isolated_home: Path, mock_client: Any
    ) -> None:
        """No complexity in options → defaults to 'standard'."""
        from src.telemetry.events import fire_project_created

        fire_project_created(
            result={"success": True, "tasks_created": 5},
            options={},
            actual_decomposer="contract_first",
        )
        props = mock_client.capture.call_args[0][1]
        assert props["complexity_mode"] == "standard"

    def test_decomposer_from_argument(
        self, isolated_home: Path, mock_client: Any
    ) -> None:
        """The ``actual_decomposer`` arg lands as ``decomposer_strategy``."""
        from src.telemetry.events import fire_project_created

        fire_project_created(
            result={"success": True, "tasks_created": 5},
            options={},
            actual_decomposer="feature_based",
        )
        props = mock_client.capture.call_args[0][1]
        assert props["decomposer_strategy"] == "feature_based"

    def test_was_fallback_true_when_contract_first_fell_back(
        self, isolated_home: Path, mock_client: Any
    ) -> None:
        """contract_first requested + feature_based ran → was_fallback True.

        This is the signal that contract-first decomposition failed.

        Falsification recipe: hard-code ``was_fallback`` to False in
        ``fire_project_created`` — this test fails.
        """
        from src.telemetry.events import fire_project_created

        fire_project_created(
            result={"success": True, "tasks_created": 5},
            options={},
            actual_decomposer="feature_based",
            requested_decomposer="contract_first",
        )
        assert mock_client.capture.call_args[0][1]["was_fallback"] is True

    def test_was_fallback_false_when_decomposer_matches(
        self, isolated_home: Path, mock_client: Any
    ) -> None:
        """Requested == actual → not a fallback."""
        from src.telemetry.events import fire_project_created

        # contract_first ran as requested.
        fire_project_created(
            result={"success": True, "tasks_created": 5},
            options={},
            actual_decomposer="contract_first",
            requested_decomposer="contract_first",
        )
        assert mock_client.capture.call_args[0][1]["was_fallback"] is False

    def test_was_fallback_false_when_feature_based_requested(
        self, isolated_home: Path, mock_client: Any
    ) -> None:
        """feature_based requested + feature_based ran → not a fallback.

        A genuine feature_based request must not be miscounted as a
        contract_first failure.
        """
        from src.telemetry.events import fire_project_created

        fire_project_created(
            result={"success": True, "tasks_created": 5},
            options={},
            actual_decomposer="feature_based",
            requested_decomposer="feature_based",
        )
        assert mock_client.capture.call_args[0][1]["was_fallback"] is False

    def test_domain_and_category_default_to_unknown(
        self, isolated_home: Path, mock_client: Any
    ) -> None:
        """When the result dict omits the classification, both are 'unknown'.

        An early return in the decomposer (e.g. PRD produced no
        tasks) can skip classification entirely.  ``"unknown"`` is
        the honest value for that case.
        """
        from src.telemetry.events import fire_project_created

        fire_project_created(
            result={"success": True, "tasks_created": 5},
            options={},
            actual_decomposer="contract_first",
        )
        props = mock_client.capture.call_args[0][1]
        assert props["domain"] == "unknown"
        assert props["structural_category"] == "unknown"

    def test_domain_and_category_read_from_result(
        self, isolated_home: Path, mock_client: Any
    ) -> None:
        """Planner-supplied classification is forwarded to the event.

        Falsification recipe: revert ``fire_project_created`` to the
        hard-coded ``"unknown"`` placeholders.  Confirm this test
        fails because the planner labels no longer reach PostHog.
        """
        from src.telemetry.events import fire_project_created

        fire_project_created(
            result={
                "success": True,
                "tasks_created": 12,
                "domain": "fintech",
                "structural_category": "web app",
            },
            options={},
            actual_decomposer="feature_based",
        )
        props = mock_client.capture.call_args[0][1]
        assert props["domain"] == "fintech"
        assert props["structural_category"] == "web app"

    def test_does_not_fire_on_failed_create(
        self, isolated_home: Path, mock_client: Any
    ) -> None:
        """Failed create_project (success=False) does NOT fire telemetry.

        Privacy / signal-quality: a failed create is not a created
        project.  The event measures successful project creation,
        not call attempts.
        """
        from src.telemetry.events import fire_project_created

        fire_project_created(
            result={"success": False, "error": "boom"},
            options={},
            actual_decomposer="contract_first",
        )

        mock_client.capture.assert_not_called()

    def test_swallows_exceptions(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken client cannot crash the create_project path."""
        from src.telemetry.events import fire_project_created

        broken = MagicMock()
        broken.capture.side_effect = RuntimeError("simulated")
        monkeypatch.setattr("src.telemetry.events.get_telemetry_client", lambda: broken)

        # Must not raise.
        fire_project_created(
            result={"success": True, "tasks_created": 1},
            options={},
            actual_decomposer="contract_first",
        )

    def test_no_secrets_in_payload(self, isolated_home: Path, mock_client: Any) -> None:
        """Project name, descriptions, secrets MUST NOT ship."""
        from src.telemetry.events import fire_project_created

        fire_project_created(
            result={
                "success": True,
                "tasks_created": 1,
                "project_name": "Secret Customer Project — DO NOT SHARE",
                "description": "Build the launch-codes API",
                "api_key": "sk-ant-secret-key",
            },
            options={
                "complexity": "standard",
                "anthropic_api_key": "sk-ant-also-secret",
            },
            actual_decomposer="contract_first",
        )
        props = mock_client.capture.call_args[0][1]
        flat = " ".join(str(v) for v in props.values()).lower()
        assert "secret" not in flat
        assert "launch-codes" not in flat
        assert "api_key" not in props
        assert "anthropic_api_key" not in props
