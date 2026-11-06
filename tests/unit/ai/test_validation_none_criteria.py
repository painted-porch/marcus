"""Unit tests for validation with None completion_criteria."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.ai.validation.work_analyzer import WorkAnalyzer
from src.core.models import Priority, Task, TaskStatus

pytestmark = pytest.mark.unit


class TestValidationNoneCriteria:
    """Test suite for validation with None completion_criteria."""

    @pytest.fixture
    def work_analyzer(self):
        """Create WorkAnalyzer instance.

        WorkAnalyzer.__init__ unconditionally constructs a real
        LLMAbstraction(), which calls get_config() and raises if no
        config_marcus.json or CLAUDE_API_KEY is present -- a real
        filesystem/environment dependency this "unit" test must not
        have (the #857 class of bug in a different costume: Codex
        review on PR #874 caught this passing only because a config
        file or an earlier test's cached config happened to be
        present, order-dependent and worktree-dependent). Patched the
        same way test_work_analyzer.py's own fixtures already do.
        """
        with patch("src.ai.validation.work_analyzer.LLMAbstraction"):
            return WorkAnalyzer()

    @pytest.fixture
    def task_with_none_criteria(self):
        """Create task with None completion_criteria."""
        return Task(
            id="test_task_1",
            name="Test Task",
            description="Test task description",
            status=TaskStatus.DONE,
            priority=Priority.MEDIUM,
            assigned_to="agent_1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=2.0,
            completion_criteria=None,  # This is the key issue
        )

    def test_build_validation_prompt_handles_none_criteria(
        self, work_analyzer, task_with_none_criteria
    ):
        """Test _build_validation_prompt doesn't crash with None criteria."""
        # Arrange
        mock_evidence = Mock()
        mock_evidence.source_files = []
        mock_evidence.design_artifacts = []
        mock_evidence.decisions = []
        mock_evidence.has_placeholders = False
        mock_evidence.empty_files = []
        mock_evidence.file_manifest = []

        # Act & Assert - Should not raise TypeError
        try:
            prompt = work_analyzer._build_validation_prompt(
                task_with_none_criteria, mock_evidence
            )
            assert isinstance(prompt, str)
            assert "TASK: Test Task" in prompt
            assert "ACCEPTANCE CRITERIA" in prompt
        except TypeError as e:
            pytest.fail(f"Should handle None criteria without TypeError: {e}")

    def test_build_validation_prompt_handles_empty_list_criteria(
        self, work_analyzer, task_with_none_criteria
    ):
        """Test _build_validation_prompt works with empty list criteria."""
        # Arrange
        task_with_none_criteria.completion_criteria = []
        mock_evidence = Mock()
        mock_evidence.source_files = []
        mock_evidence.design_artifacts = []
        mock_evidence.decisions = []
        mock_evidence.has_placeholders = False
        mock_evidence.empty_files = []
        mock_evidence.file_manifest = []

        # Act
        prompt = work_analyzer._build_validation_prompt(
            task_with_none_criteria, mock_evidence
        )

        # Assert
        assert isinstance(prompt, str)
        assert "TASK: Test Task" in prompt

    def test_build_validation_prompt_includes_criteria_when_present(
        self, work_analyzer, task_with_none_criteria
    ):
        """Test _build_validation_prompt includes criteria when provided."""
        # Arrange
        task_with_none_criteria.completion_criteria = [
            "Code is functional",
            "Tests pass",
        ]
        mock_evidence = Mock()
        mock_evidence.source_files = []
        mock_evidence.design_artifacts = []
        mock_evidence.decisions = []
        mock_evidence.has_placeholders = False
        mock_evidence.empty_files = []
        mock_evidence.file_manifest = []

        # Act
        prompt = work_analyzer._build_validation_prompt(
            task_with_none_criteria, mock_evidence
        )

        # Assert
        assert "1. Code is functional" in prompt
        assert "2. Tests pass" in prompt
