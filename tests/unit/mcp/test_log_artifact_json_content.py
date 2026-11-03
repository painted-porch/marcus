"""
Unit tests for log_artifact accepting structured (JSON) content.

Issue #595 Fix 1: ``log_artifact``'s ``content`` parameter was typed
``str``. When an agent passed a parsed JSON object (a ``dict``) — for
example the contents of ``tsconfig.json`` — the call was rejected and the
artifact was never stored. ``log_artifact`` must accept ``dict`` / ``list``
content and serialize it to a JSON string before writing, while leaving
plain ``str`` content unchanged.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.marcus_mcp.tools.attachment import log_artifact

pytestmark = pytest.mark.unit


class _MockState:
    """Minimal stand-in for Marcus server state."""

    def __init__(self) -> None:
        self.task_artifacts: Dict[str, List[Dict[str, Any]]] = {}
        self.project_tasks: List[Any] = []
        self.kanban_client: Any = None


@pytest.fixture()
def state() -> _MockState:
    """Provide a fresh mock state per test."""
    return _MockState()


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """Use pytest's tmp_path as an absolute, existing project root."""
    return tmp_path


def _patch_history() -> Any:
    """Patch out project-history persistence (external write path)."""
    return patch(
        "src.marcus_mcp.tools.attachment._persist_artifact_to_history",
        new_callable=AsyncMock,
    )


class TestLogArtifactJsonContent:
    """log_artifact accepts dict/list content and stores it as JSON."""

    @pytest.mark.asyncio
    async def test_dict_content_is_serialized_to_json(
        self, project_root: Path, state: _MockState
    ) -> None:
        """A dict passed as content is written as a pretty-printed JSON string."""
        tsconfig = {"compilerOptions": {"strict": True, "target": "ES2020"}}

        with _patch_history():
            result = await log_artifact(
                task_id="task-json-1",
                filename="tsconfig.json",
                content=tsconfig,
                artifact_type="specification",
                project_root=str(project_root),
                state=state,
            )

        assert result["success"] is True
        written = (project_root / result["data"]["location"]).read_text()
        assert written == json.dumps(tsconfig, indent=2)
        assert json.loads(written) == tsconfig

    @pytest.mark.asyncio
    async def test_list_content_is_serialized_to_json(
        self, project_root: Path, state: _MockState
    ) -> None:
        """A list passed as content is written as a JSON string."""
        dependencies = ["react", "typescript", "vite"]

        with _patch_history():
            result = await log_artifact(
                task_id="task-json-2",
                filename="deps.json",
                content=dependencies,
                artifact_type="specification",
                project_root=str(project_root),
                state=state,
            )

        assert result["success"] is True
        written = (project_root / result["data"]["location"]).read_text()
        assert json.loads(written) == dependencies

    @pytest.mark.asyncio
    async def test_str_content_is_unchanged(
        self, project_root: Path, state: _MockState
    ) -> None:
        """Plain string content is written verbatim (regression guard)."""
        text = "# Design notes\n\nPlain markdown, not JSON."

        with _patch_history():
            result = await log_artifact(
                task_id="task-json-3",
                filename="notes.md",
                content=text,
                artifact_type="documentation",
                project_root=str(project_root),
                state=state,
            )

        assert result["success"] is True
        written = (project_root / result["data"]["location"]).read_text()
        assert written == text

    @pytest.mark.asyncio
    async def test_dict_content_overwriting_existing_docs_file(
        self, project_root: Path, state: _MockState
    ) -> None:
        """
        Dict content survives the docs/ size-guard path.

        The size guard calls ``content.encode()``, which only works once
        the dict has been coerced to a string. Overwriting an existing
        small docs/ file exercises that code path.
        """
        existing = project_root / "docs" / "specifications" / "config.json"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text('{"old": true}', encoding="utf-8")

        with _patch_history():
            result = await log_artifact(
                task_id="task-json-4",
                filename="config.json",
                content={"new": True, "nested": {"value": 1}},
                artifact_type="specification",
                project_root=str(project_root),
                state=state,
            )

        assert result["success"] is True
        written = (project_root / result["data"]["location"]).read_text()
        assert json.loads(written) == {"new": True, "nested": {"value": 1}}


class TestLogArtifactToolSchema:
    """The registered MCP tool schema accepts structured content."""

    @pytest.mark.asyncio
    async def test_tool_schema_content_accepts_object_and_array(self) -> None:
        """
        The generated log_artifact MCP schema accepts objects and arrays.

        The original #595 bug lived at the MCP argument-validation layer:
        a ``content: str`` parameter made FastMCP emit a string-only
        schema, so a dict was rejected before the implementation ran. The
        impl-level tests above call the function directly and would stay
        green even if the server wrapper were narrowed back to ``str`` —
        this test closes that gap by inspecting the registered schema.
        """
        from mcp.server.fastmcp import FastMCP

        from src.marcus_mcp.server import MarcusServer

        app = FastMCP("schema-test")
        # Registration only introspects function signatures; the tool
        # closures capture ``self`` but are never invoked here, so a bare
        # object stands in for a fully constructed server.
        MarcusServer._register_endpoint_tools(object(), app, "agent")

        tools = await app.list_tools()
        log_artifact_tool = next(t for t in tools if t.name == "log_artifact")
        content_schema = log_artifact_tool.inputSchema["properties"]["content"]

        assert (
            "anyOf" in content_schema
        ), f"log_artifact `content` must accept multiple types; got {content_schema}"
        accepted = {variant.get("type") for variant in content_schema["anyOf"]}
        assert {"string", "object", "array"} <= accepted
