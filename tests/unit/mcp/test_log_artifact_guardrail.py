"""
Unit tests for the log_artifact git-tracked file guardrail (GH-385 fix #7).

When an agent calls log_artifact with a path that resolves to a git-tracked
source file, the tool must reject the write and return a descriptive error.
dashboard-v82 post-mortem: Agent 1 overwrote theme.css and design-tokens.json
via log_artifact and had to restore from git (commit d44dd5a).

Tests:
- tracked source file (src/) → rejected
- tracked artifact file (docs/, tmp/) → allowed (iterative refresh)
- untracked file → write proceeds normally
- git not available on non-artifact path → write proceeds (fail-open)
"""

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.marcus_mcp.tools.attachment import log_artifact

pytestmark = pytest.mark.unit


class _MockState:
    """Minimal state stub for log_artifact calls."""

    def __init__(self) -> None:
        self.task_artifacts: Dict[str, List[Dict[str, Any]]] = {}
        self.project_tasks: List[Any] = []
        self.kanban_client: Any = None


@pytest.fixture()
def state() -> _MockState:
    """Return a fresh MockState for each test."""
    return _MockState()


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """Return a temporary directory to use as project_root."""
    return tmp_path


class TestLogArtifactGitGuard:
    """Tests for the git-tracked file write guard."""

    @pytest.mark.asyncio
    async def test_rejects_git_tracked_file(
        self, project_root: Path, state: _MockState
    ) -> None:
        """
        Verify log_artifact returns an error when the target file is tracked by git.

        The guard calls ``git ls-files --error-unmatch``; returncode 0 means
        tracked. The tool must refuse the write without touching the filesystem.
        """
        tracked_file = project_root / "src" / "widgets" / "theme.css"

        tracked_result = Mock(spec=subprocess.CompletedProcess)
        tracked_result.returncode = 0

        with patch(
            "src.marcus_mcp.tools.attachment.subprocess.run",
            return_value=tracked_result,
        ):
            result = await log_artifact(
                task_id="task-1",
                filename="theme.css",
                content="body { color: red; }",
                artifact_type="design",
                project_root=str(project_root),
                location=str(tracked_file.relative_to(project_root)),
                state=state,
            )

        assert result["success"] is False
        assert "git-tracked" in result["error"]
        assert not tracked_file.exists(), "Guard must not write the file"

    @pytest.mark.asyncio
    async def test_allows_untracked_file(
        self, project_root: Path, state: _MockState
    ) -> None:
        """
        Verify log_artifact proceeds normally when the target is not tracked.

        git ls-files --error-unmatch exits non-zero for untracked paths.
        The tool should write the artifact to docs/ as usual.
        """
        untracked_result = Mock(spec=subprocess.CompletedProcess)
        untracked_result.returncode = 1

        with (
            patch(
                "src.marcus_mcp.tools.attachment.subprocess.run",
                return_value=untracked_result,
            ),
            patch(
                "src.marcus_mcp.tools.attachment._persist_artifact_to_history",
                new_callable=AsyncMock,
            ),
        ):
            result = await log_artifact(
                task_id="task-2",
                filename="design-spec.md",
                content="# Design Spec",
                artifact_type="design",
                project_root=str(project_root),
                state=state,
            )

        assert result["success"] is True
        written = project_root / "docs" / "design" / "design-spec.md"
        assert written.exists()
        assert written.read_text() == "# Design Spec"

    @pytest.mark.asyncio
    async def test_proceeds_when_git_not_available(
        self, project_root: Path, state: _MockState
    ) -> None:
        """
        Verify log_artifact proceeds when git is not installed (non-artifact path).

        The guard must be fail-open: non-git projects and CI environments
        without git should not be blocked from writing artifacts, even when
        the target is outside the standard artifact directories.
        """
        with (
            patch(
                "src.marcus_mcp.tools.attachment.subprocess.run",
                side_effect=FileNotFoundError("git: No such file"),
            ),
            patch(
                "src.marcus_mcp.tools.attachment._persist_artifact_to_history",
                new_callable=AsyncMock,
            ),
        ):
            result = await log_artifact(
                task_id="task-3",
                filename="api-spec.md",
                content="# API",
                artifact_type="api",
                project_root=str(project_root),
                # Use a non-artifact path so the guard actually runs
                location="reports/api-spec.md",
                state=state,
            )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_allows_tracked_file_under_docs(
        self, project_root: Path, state: _MockState
    ) -> None:
        """
        Verify tracked files under docs/ are allowed (iterative artifact refresh).

        Codex P2: the original guard blocked ALL tracked files, including
        legitimate artifact files under docs/ committed in a previous run.
        Iterative refreshes must succeed; the guard only protects source roots.
        """
        tracked_result = Mock(spec=subprocess.CompletedProcess)
        tracked_result.returncode = 0  # would be "tracked" if git ran

        with (
            patch(
                "src.marcus_mcp.tools.attachment.subprocess.run",
                return_value=tracked_result,
            ) as mock_git,
            patch(
                "src.marcus_mcp.tools.attachment._persist_artifact_to_history",
                new_callable=AsyncMock,
            ),
        ):
            result = await log_artifact(
                task_id="task-5",
                filename="design-spec.md",
                content="# Updated Design",
                artifact_type="design",
                project_root=str(project_root),
                # Default location → docs/design/design-spec.md
                state=state,
            )

        assert result["success"] is True, "Tracked docs/ file must be refreshable"
        # Guard must not have run git for a docs/ path
        mock_git.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_tracked_file_under_tmp(
        self, project_root: Path, state: _MockState
    ) -> None:
        """
        Verify tracked files under tmp/ are allowed (artifact output root).
        """
        tracked_result = Mock(spec=subprocess.CompletedProcess)
        tracked_result.returncode = 0

        with (
            patch(
                "src.marcus_mcp.tools.attachment.subprocess.run",
                return_value=tracked_result,
            ) as mock_git,
            patch(
                "src.marcus_mcp.tools.attachment._persist_artifact_to_history",
                new_callable=AsyncMock,
            ),
        ):
            result = await log_artifact(
                task_id="task-6",
                filename="scratch.json",
                content="{}",
                artifact_type="temporary",
                project_root=str(project_root),
                state=state,
            )

        assert result["success"] is True
        mock_git.assert_not_called()

    @pytest.mark.asyncio
    async def test_still_blocks_tracked_source_file(
        self, project_root: Path, state: _MockState
    ) -> None:
        """
        Verify the guard still blocks tracked files outside artifact roots.

        A file under src/ is not an artifact output — if it's tracked, the
        guard must reject the write regardless of the P2 scoping fix.
        """
        tracked_result = Mock(spec=subprocess.CompletedProcess)
        tracked_result.returncode = 0

        with patch(
            "src.marcus_mcp.tools.attachment.subprocess.run",
            return_value=tracked_result,
        ):
            result = await log_artifact(
                task_id="task-7",
                filename="theme.css",
                content="body {}",
                artifact_type="design",
                project_root=str(project_root),
                location="src/widgets/theme.css",
                state=state,
            )

        assert result["success"] is False
        assert "git-tracked" in result["error"]

    @pytest.mark.asyncio
    async def test_error_message_names_the_path(
        self, project_root: Path, state: _MockState
    ) -> None:
        """
        Verify the rejection error message includes the offending relative path.

        Agents need to know WHICH file was rejected, not just that a rejection
        happened, so they can choose an appropriate docs/ location instead.
        """
        tracked_result = Mock(spec=subprocess.CompletedProcess)
        tracked_result.returncode = 0

        with patch(
            "src.marcus_mcp.tools.attachment.subprocess.run",
            return_value=tracked_result,
        ):
            result = await log_artifact(
                task_id="task-4",
                filename="design-tokens.json",
                content='{"color": "blue"}',
                artifact_type="design",
                project_root=str(project_root),
                location="src/styles/design-tokens.json",
                state=state,
            )

        assert result["success"] is False
        assert (
            "design-tokens.json" in result["error"] or "src/styles" in result["error"]
        )

    @pytest.mark.asyncio
    async def test_rejects_path_traversal_bypassing_artifact_root(
        self, project_root: Path, state: _MockState
    ) -> None:
        """
        Verify that "docs/../src/theme.css" is treated as a src/ path, not docs/.

        Codex P1: the artifact-root bypass checked artifact_path.parts[0] on
        the raw string, so "docs/../src/theme.css" appeared to start with
        "docs" and skipped the git guard — even though it resolves to
        src/theme.css. After the fix, the path is normalised before the check.
        A git-tracked file at the resolved src/ location must still be blocked.
        """
        tracked_result = Mock(spec=subprocess.CompletedProcess)
        tracked_result.returncode = 0  # git says: tracked

        with patch(
            "src.marcus_mcp.tools.attachment.subprocess.run",
            return_value=tracked_result,
        ):
            result = await log_artifact(
                task_id="task-traversal",
                filename="theme.css",
                content="body {}",
                artifact_type="design",
                project_root=str(project_root),
                # Traversal: looks like docs/ root but resolves to src/
                location="docs/../src/widgets/theme.css",
                state=state,
            )

        assert (
            result["success"] is False
        ), "Traversal via docs/../src/ must not bypass the git guard"
        # Should be blocked by git guard (not by the path-escape check)
        assert "git-tracked" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_path_escaping_project_root(
        self, project_root: Path, state: _MockState
    ) -> None:
        """
        Verify that a location resolving outside project_root is rejected.

        A path like "../../etc/passwd" escapes the project root entirely.
        The normalisation step must catch this and return an error before
        any filesystem or git operations occur.
        """
        with patch(
            "src.marcus_mcp.tools.attachment.subprocess.run",
        ) as mock_git:
            result = await log_artifact(
                task_id="task-escape",
                filename="passwd",
                content="root:x:0:0",
                artifact_type="design",
                project_root=str(project_root),
                location="../../etc/passwd",
                state=state,
            )

        assert result["success"] is False
        assert "outside project root" in result["error"]
        mock_git.assert_not_called()
