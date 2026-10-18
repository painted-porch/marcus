"""
Unit tests for the redo extension of RecoveryInfo (issue #627).

RecoveryInfo already carries lease-expiry recovery context between agent
attempts. Issue #627 extends it with redo-specific fields so an
integration agent can send a completed task back to the board
(``request_task_redo``) and the next implementer sees who requested the
redo, why, where the previous attempt lives, and how many redos have
already happened.
"""

from datetime import datetime, timezone

import pytest

from src.core.models import RecoveryInfo

pytestmark = pytest.mark.unit


class TestRecoveryInfoRedoFields:
    """Test suite for the redo fields added to RecoveryInfo."""

    def _make_redo_info(self) -> RecoveryInfo:
        """Create a RecoveryInfo populated the way request_task_redo does."""
        return RecoveryInfo(
            recovered_at=datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc),
            recovered_from_agent="agent_unicorn_1_3",
            previous_progress=100,
            time_spent_minutes=42.0,
            recovery_reason="redo_requested",
            instructions="Fix the API response shape per contract.",
            previous_agent_branch="marcus/agent_unicorn_1_3",
            redo_reason="API response missing required 'data.task' wrapper",
            requested_by="agent_unicorn_2_6",
            previous_worktree_path="/srv/marcus/worktrees/agent_unicorn_1_3",
            redo_count=1,
        )

    def test_redo_fields_default_to_unset(self) -> None:
        """Existing constructors without redo fields keep working unchanged."""
        info = RecoveryInfo(
            recovered_at=datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc),
            recovered_from_agent="agent-1",
            previous_progress=50,
            time_spent_minutes=10.0,
            recovery_reason="lease_expired",
            instructions="continue",
        )
        assert info.redo_reason is None
        assert info.requested_by is None
        assert info.previous_worktree_path is None
        assert info.redo_count == 0

    def test_to_dict_includes_redo_fields(self) -> None:
        """to_dict must serialize the redo fields for kanban/context delivery."""
        data = self._make_redo_info().to_dict()
        assert data["redo_reason"] == (
            "API response missing required 'data.task' wrapper"
        )
        assert data["requested_by"] == "agent_unicorn_2_6"
        assert (
            data["previous_worktree_path"] == "/srv/marcus/worktrees/agent_unicorn_1_3"
        )
        assert data["redo_count"] == 1

    def test_from_dict_round_trips_redo_fields(self) -> None:
        """from_dict(to_dict(x)) must preserve the redo fields."""
        original = self._make_redo_info()
        restored = RecoveryInfo.from_dict(original.to_dict())
        assert restored.redo_reason == original.redo_reason
        assert restored.requested_by == original.requested_by
        assert restored.previous_worktree_path == original.previous_worktree_path
        assert restored.redo_count == original.redo_count

    def test_from_dict_tolerates_legacy_payloads(self) -> None:
        """Persisted pre-#627 payloads (no redo keys) must still deserialize."""
        legacy = {
            "recovered_at": "2026-08-04T12:00:00+00:00",
            "recovered_from_agent": "agent-1",
            "previous_progress": 50,
            "time_spent_minutes": 10.0,
            "recovery_reason": "lease_expired",
            "instructions": "continue",
            "previous_agent_branch": None,
            "recovery_expires_at": None,
        }
        info = RecoveryInfo.from_dict(legacy)
        assert info.redo_count == 0
        assert info.redo_reason is None
