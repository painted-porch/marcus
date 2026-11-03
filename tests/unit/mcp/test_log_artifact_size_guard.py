"""
Unit tests for the log_artifact docs/-overwrite size guard (Fix 1).

When a docs/ file already exists with substantial content (>= 8 KB) and
the incoming content is less than half the existing size, log_artifact
must refuse the write unless force=True is passed.

This prevents an "Implement X" agent from silently replacing a large design
spec with a small stub artifact.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.marcus_mcp.tools.attachment import log_artifact

pytestmark = pytest.mark.unit

_LARGE_CONTENT = "x" * 9_000  # 9 KB — above the 8 KB threshold
_SMALL_CONTENT = "y" * 100  # 100 bytes — less than 50% of 9 KB


class _MockState:
    def __init__(self) -> None:
        self.task_artifacts: Dict[str, List[Dict[str, Any]]] = {}
        self.project_tasks: List[Any] = []
        self.kanban_client: Any = None


@pytest.fixture()
def state() -> _MockState:
    return _MockState()


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def large_docs_file(project_root: Path) -> Path:
    """Pre-write a 9 KB docs/ file to simulate an existing spec."""
    f = project_root / "docs" / "specifications" / "api-spec.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(_LARGE_CONTENT, encoding="utf-8")
    return f


class TestLogArtifactSizeGuard:
    """log_artifact blocks small overwrites of large docs/ files."""

    @pytest.mark.asyncio
    async def test_blocks_small_overwrite_of_large_docs_file(
        self, project_root: Path, state: _MockState, large_docs_file: Path
    ) -> None:
        """
        A small write to an existing large docs/ file is rejected without force.

        Existing file is 9 KB; new content is 100 bytes — well under 50%.
        The tool must refuse and name the existing file size in the error.
        """
        result = await log_artifact(
            task_id="task-sz-1",
            filename="api-spec.md",
            content=_SMALL_CONTENT,
            artifact_type="specification",
            project_root=str(project_root),
            state=state,
        )

        assert result["success"] is False
        assert (
            "overwrite" in result["error"].lower() or "force" in result["error"].lower()
        )
        # Existing file must remain untouched
        assert large_docs_file.read_text() == _LARGE_CONTENT

    @pytest.mark.asyncio
    async def test_force_true_allows_small_overwrite(
        self, project_root: Path, state: _MockState, large_docs_file: Path
    ) -> None:
        """
        force=True bypasses the size guard and writes the smaller content.
        """
        with patch(
            "src.marcus_mcp.tools.attachment._persist_artifact_to_history",
            new_callable=AsyncMock,
        ):
            result = await log_artifact(
                task_id="task-sz-2",
                filename="api-spec.md",
                content=_SMALL_CONTENT,
                artifact_type="specification",
                project_root=str(project_root),
                state=state,
                force=True,
            )

        assert result["success"] is True
        assert large_docs_file.read_text() == _SMALL_CONTENT

    @pytest.mark.asyncio
    async def test_allows_large_overwrite_without_force(
        self, project_root: Path, state: _MockState, large_docs_file: Path
    ) -> None:
        """
        A write that is >= 50% of existing size is not a downgrade — allowed.
        """
        new_content = "z" * 5_000  # 5 KB — more than 50% of 9 KB
        with patch(
            "src.marcus_mcp.tools.attachment._persist_artifact_to_history",
            new_callable=AsyncMock,
        ):
            result = await log_artifact(
                task_id="task-sz-3",
                filename="api-spec.md",
                content=new_content,
                artifact_type="specification",
                project_root=str(project_root),
                state=state,
            )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_allows_small_write_to_new_file(
        self, project_root: Path, state: _MockState
    ) -> None:
        """
        Writing a small file where nothing existed before is always allowed.
        """
        with patch(
            "src.marcus_mcp.tools.attachment._persist_artifact_to_history",
            new_callable=AsyncMock,
        ):
            result = await log_artifact(
                task_id="task-sz-4",
                filename="new-spec.md",
                content=_SMALL_CONTENT,
                artifact_type="specification",
                project_root=str(project_root),
                state=state,
            )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_allows_small_write_to_small_existing_file(
        self, project_root: Path, state: _MockState
    ) -> None:
        """
        If the existing file is under the threshold (< 8 KB), no guard fires.
        """
        small_existing = project_root / "docs" / "specifications" / "notes.md"
        small_existing.parent.mkdir(parents=True, exist_ok=True)
        small_existing.write_text("a" * 100, encoding="utf-8")  # 100 bytes — tiny

        with patch(
            "src.marcus_mcp.tools.attachment._persist_artifact_to_history",
            new_callable=AsyncMock,
        ):
            result = await log_artifact(
                task_id="task-sz-5",
                filename="notes.md",
                content=_SMALL_CONTENT,
                artifact_type="specification",
                project_root=str(project_root),
                state=state,
            )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_allows_small_overwrite_of_large_tmp_file(
        self, project_root: Path, state: _MockState
    ) -> None:
        """
        Size guard does NOT apply to tmp/ artifacts — they are legitimately
        replaced with compact summaries.
        """
        tmp_file = project_root / "tmp" / "artifacts" / "scratch.json"
        tmp_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file.write_text(_LARGE_CONTENT, encoding="utf-8")

        with patch(
            "src.marcus_mcp.tools.attachment._persist_artifact_to_history",
            new_callable=AsyncMock,
        ):
            result = await log_artifact(
                task_id="task-sz-6",
                filename="scratch.json",
                content=_SMALL_CONTENT,
                artifact_type="temporary",
                project_root=str(project_root),
                state=state,
            )

        assert result["success"] is True, "tmp/ files must not be gated by size guard"
