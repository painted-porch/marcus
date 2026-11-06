"""
Unit tests for log_artifact's content-addressed path (ADR-0013 K8
consequence 1, issue #868's sibling).

A runtime with no filesystem Marcus can reach -- the Minecraft
harness's own shape, with no project_root at all -- must still be
able to log an artifact. Omitting project_root switches log_artifact
to storing the artifact's own content directly in Marcus's record
(ProjectHistoryPersistence) instead of writing a path, with none of
the filesystem-only guards (path traversal, git-tracked overwrite,
stub-overwrite size) applying, since none of them make sense without
a file to compare against.

Every existing call site always supplies project_root, so this is
purely additive -- see test_log_artifact_guardrail.py and
test_log_artifact_size_guard.py for proof the filesystem path is
unchanged.
"""

from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.core.project_history import ProjectHistoryPersistence
from src.marcus_mcp.tools.attachment import log_artifact

pytestmark = pytest.mark.unit


class _MockState:
    """Minimal state stub with a real, file-backed project history."""

    def __init__(self, tmp_path: Path) -> None:
        self.task_artifacts: Dict[str, List[Dict[str, Any]]] = {}
        self.project_tasks: List[Any] = []
        self.kanban_client: Any = None
        self.current_project_id = "minecraft-project"
        self.current_project_name = "Minecraft run"
        self.current_agent_id = "agent-1"
        self.project_history_persistence = ProjectHistoryPersistence(
            marcus_root=tmp_path
        )


class TestLogArtifactContentAddressed:
    """log_artifact with no project_root stores content, not a path."""

    @pytest.mark.asyncio
    async def test_omitting_project_root_succeeds(self, tmp_path: Path) -> None:
        state = _MockState(tmp_path)

        result = await log_artifact(
            task_id="task-1",
            filename="build-plan.md",
            content="Build the west wall first.",
            artifact_type="design",
            project_root=None,
            state=state,
        )

        assert result["success"] is True
        assert result["data"]["content_stored"] is True
        assert "full_path" not in result["data"]
        assert result["data"]["location"] == "docs/design/build-plan.md"

    @pytest.mark.asyncio
    async def test_no_file_is_written_to_disk(self, tmp_path: Path) -> None:
        """Nothing is written anywhere on disk -- there is no
        project_root to write under in the first place."""
        state = _MockState(tmp_path)

        await log_artifact(
            task_id="task-1",
            filename="build-plan.md",
            content="Build the west wall first.",
            artifact_type="design",
            project_root=None,
            state=state,
        )

        assert not any(tmp_path.rglob("build-plan.md"))

    @pytest.mark.asyncio
    async def test_content_is_durably_recorded_and_readable_back(
        self, tmp_path: Path
    ) -> None:
        """The content itself, not just metadata, round-trips through
        the real ProjectHistoryPersistence."""
        state = _MockState(tmp_path)

        await log_artifact(
            task_id="task-1",
            filename="build-plan.md",
            content="Build the west wall first.",
            artifact_type="design",
            project_root=None,
            state=state,
        )

        artifacts = await state.project_history_persistence.load_artifacts(
            "minecraft-project"
        )
        assert len(artifacts) == 1
        assert artifacts[0].content == "Build the west wall first."
        assert artifacts[0].absolute_path == ""
        assert artifacts[0].sha256_hash is not None
        assert artifacts[0].file_size_bytes == len(
            "Build the west wall first.".encode("utf-8")
        )

    @pytest.mark.asyncio
    async def test_json_content_is_serialized_before_storing(
        self, tmp_path: Path
    ) -> None:
        """A dict/list content argument is JSON-serialized the same
        way the filesystem path already does (issue #595 Fix 1)."""
        state = _MockState(tmp_path)

        result = await log_artifact(
            task_id="task-1",
            filename="config.json",
            content={"spawn": [0, 64, 0]},
            artifact_type="reference",
            project_root=None,
            state=state,
        )

        assert result["success"] is True
        artifacts = await state.project_history_persistence.load_artifacts(
            "minecraft-project"
        )
        assert '"spawn"' in artifacts[0].content

    @pytest.mark.asyncio
    async def test_the_filesystem_only_guards_never_run(self, tmp_path: Path) -> None:
        """No project_root means no path to traverse outside of, no
        git repository to check, and no existing file to compare
        sizes against -- none of those guards can reject a
        content-addressed call."""
        state = _MockState(tmp_path)

        result = await log_artifact(
            task_id="task-1",
            filename="x.md",
            content="short",
            artifact_type="design",
            location="../../etc/passwd",
            project_root=None,
            state=state,
        )

        # The path-traversal guard is filesystem-specific (it exists to
        # stop a write from landing outside project_root); with no
        # filesystem write at all, a caller's location string is just
        # metadata, not a write target, so it is not rejected the way
        # the project_root path rejects it.
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_task_artifacts_tracking_is_unaffected(self, tmp_path: Path) -> None:
        """The in-memory per-task tracker _collect_task_artifacts
        reads from still gets an entry, the same shape as the
        filesystem path produces."""
        state = _MockState(tmp_path)

        await log_artifact(
            task_id="task-1",
            filename="build-plan.md",
            content="Build the west wall first.",
            artifact_type="design",
            project_root=None,
            state=state,
        )

        assert len(state.task_artifacts["task-1"]) == 1
        assert state.task_artifacts["task-1"][0]["filename"] == "build-plan.md"


class TestLogArtifactFilesystemPathContentFieldIsNone:
    """The original (project_root given) path's records carry no
    content field -- only the new content-addressed path does."""

    @pytest.mark.asyncio
    async def test_filesystem_artifact_has_no_content_field_recorded(
        self, tmp_path: Path
    ) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        state = _MockState(tmp_path / "history")

        result = await log_artifact(
            task_id="task-1",
            filename="notes.md",
            content="Some notes.",
            artifact_type="design",
            project_root=str(project_root),
            state=state,
        )

        assert result["success"] is True
        assert result["data"]["full_path"] == str(
            project_root / "docs" / "design" / "notes.md"
        )
        assert "content_stored" not in result["data"]
        artifacts = await state.project_history_persistence.load_artifacts(
            "minecraft-project"
        )
        assert artifacts[0].content is None
        assert (project_root / "docs" / "design" / "notes.md").read_text() == (
            "Some notes."
        )
