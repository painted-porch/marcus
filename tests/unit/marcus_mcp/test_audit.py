"""
Unit tests for src/marcus_mcp/audit.py
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, mock_open, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from src.marcus_mcp.audit import AuditLogger


@pytest.fixture
def logger(tmp_path: Path) -> AuditLogger:
    """Create an AuditLogger writing to a temp directory."""
    return AuditLogger(log_dir=tmp_path)


class TestLogAccessDenied:
    """Test suite for AuditLogger.log_access_denied"""

    @pytest.fixture
    def logger(self, tmp_path: Path) -> AuditLogger:
        """Create an AuditLogger writing to a temp directory."""
        return AuditLogger(log_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_access_denied_entry_contains_numeric_duration_ms(
        self, logger: AuditLogger
    ) -> None:
        """Audit entry written by log_access_denied must include a numeric duration_ms field."""
        # Arrange
        captured: list[str] = []

        original_log_event = logger.log_event

        async def capturing_log_event(**kwargs):  # type: ignore[override]
            captured.append(json.dumps(kwargs.get("details", {})))
            await original_log_event(**kwargs)

        logger.log_event = capturing_log_event  # type: ignore[method-assign]

        # Act
        await logger.log_access_denied(
            client_id="agent-1",
            client_type="agent",
            tool_name="get_next_task",
            reason="insufficient permissions",
            duration_ms=42.5,
        )

        # Assert
        assert len(captured) == 1
        details = json.loads(captured[0])
        assert "duration_ms" in details, "duration_ms missing from audit details"
        assert isinstance(
            details["duration_ms"], (int, float)
        ), f"duration_ms must be numeric, got {type(details['duration_ms'])}"
        assert details["duration_ms"] == 42.5

    @pytest.mark.asyncio
    async def test_access_denied_writes_duration_ms_to_log_file(
        self, logger: AuditLogger
    ) -> None:
        """duration_ms appears in the JSONL file written to disk."""
        # Act
        await logger.log_access_denied(
            client_id="agent-2",
            client_type="agent",
            tool_name="assign_next_task",
            reason="role not allowed",
            duration_ms=7.0,
        )

        # Assert
        log_files = list(logger.log_dir.glob("audit_*.jsonl"))
        assert log_files, "No audit log file was created"

        line = log_files[0].read_text().strip()
        event = json.loads(line)
        assert event["details"]["duration_ms"] == 7.0
        assert isinstance(event["details"]["duration_ms"], (int, float))

    @pytest.mark.asyncio
    async def test_access_denied_includes_task_count_when_provided(
        self, logger: AuditLogger
    ) -> None:
        """task_count appears in the audit entry when passed to log_access_denied."""
        # Act
        await logger.log_access_denied(
            client_id="agent-3",
            client_type="agent",
            tool_name="request_next_task",
            reason="role not allowed",
            duration_ms=5.0,
            task_count=17,
        )

        # Assert
        log_files = list(logger.log_dir.glob("audit_*.jsonl"))
        event = json.loads(log_files[0].read_text().strip())
        assert event["details"]["task_count"] == 17
        assert isinstance(event["details"]["task_count"], int)

    @pytest.mark.asyncio
    async def test_access_denied_omits_task_count_when_not_provided(
        self, logger: AuditLogger
    ) -> None:
        """task_count is absent from the audit entry when not passed."""
        # Act
        await logger.log_access_denied(
            client_id="agent-4",
            client_type="agent",
            tool_name="get_project_status",
            reason="role not allowed",
            duration_ms=3.0,
        )

        # Assert
        log_files = list(logger.log_dir.glob("audit_*.jsonl"))
        event = json.loads(log_files[0].read_text().strip())
        assert "task_count" not in event["details"]


class TestLogToolCall:
    """Test suite for AuditLogger.log_tool_call"""

    @pytest.mark.asyncio
    async def test_tool_call_includes_task_count_when_provided(
        self, logger: AuditLogger
    ) -> None:
        """task_count appears in the audit entry when passed to log_tool_call."""
        # Act
        await logger.log_tool_call(
            client_id="agent-1",
            client_type="agent",
            tool_name="request_next_task",
            arguments={"agent_id": "agent-1"},
            result={"success": True},
            duration_ms=120.0,
            success=True,
            task_count=42,
        )

        # Assert
        log_files = list(logger.log_dir.glob("audit_*.jsonl"))
        event = json.loads(log_files[0].read_text().strip())
        assert event["details"]["task_count"] == 42
        assert isinstance(event["details"]["task_count"], int)

    @pytest.mark.asyncio
    async def test_tool_call_omits_task_count_when_not_provided(
        self, logger: AuditLogger
    ) -> None:
        """task_count is absent from non-request_next_task audit entries."""
        # Act
        await logger.log_tool_call(
            client_id="agent-1",
            client_type="agent",
            tool_name="get_project_status",
            arguments={},
            result={"status": "ok"},
            duration_ms=10.0,
            success=True,
        )

        # Assert
        log_files = list(logger.log_dir.glob("audit_*.jsonl"))
        event = json.loads(log_files[0].read_text().strip())
        assert "task_count" not in event["details"]
