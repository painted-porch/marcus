"""
Unit tests for the kanban attachment contract in context delivery (#624).

``KanbanInterface.get_attachments(task_id: str)`` returns NORMALIZED
records: ``{"id", "filename", "url", "content_type", "size",
"created_at", "created_by"}``. Three collectors in
``src/marcus_mcp/tools/context.py`` instead called it with a
``card_id=`` keyword and read Planka's RAW keys (``name`` / ``userId``
/ ``createdAt``), so against any compliant provider:

1. ``card_id=`` raises ``TypeError``, swallowed by the surrounding
   ``except``, silently dropping every board attachment from context; and
2. even if the keyword were right, ``name``/``userId``/``createdAt``
   would all read as ``None`` and the synthesized location malformed.

PR #623 fixed this in ``_collect_foundation_contract`` and left the
other three tiers (task, dependency, transitive ancestor) tracked as
#624 — these tests cover all of them, because a partial fix leaves
agents silently missing files that exist on the board.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from src.core.models import Priority, Task, TaskStatus

pytestmark = pytest.mark.unit

# A canonical provider response — exactly the shape KanbanInterface
# documents. If a collector reads Planka raw keys, every field below
# reads as None and the test fails loudly.
CANONICAL_ATTACHMENT = {
    "id": "att-1",
    "filename": "api-contract.yaml",
    "url": "docs/api/api-contract.yaml",
    "content_type": "application/yaml",
    "size": 2048,
    "created_at": "2026-08-10T12:00:00+00:00",
    "created_by": "agent_alpha",
}


def _task(
    task_id: str,
    *,
    dependencies: List[str] | None = None,
    labels: List[str] | None = None,
) -> Task:
    """Build a Task with the fields the context collectors touch."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        dependencies=dependencies or [],
        labels=labels or [],
    )


class _StrictKanban:
    """Kanban stub that enforces the documented signature.

    ``get_attachments`` accepts ONLY ``task_id`` — mirroring the real
    providers, where a ``card_id=`` call raises ``TypeError``. Using a
    strict stub is the point: a permissive ``AsyncMock`` would happily
    accept the wrong keyword and hide the bug.
    """

    def __init__(self, attachments_by_task: Dict[str, List[Dict[str, Any]]]):
        self._by_task = attachments_by_task
        self.calls: List[str] = []

    async def get_attachments(self, task_id: str) -> Dict[str, Any]:
        self.calls.append(task_id)
        return {"success": True, "data": self._by_task.get(task_id, [])}


class _State:
    """Minimal Marcus state for the artifact collectors."""

    def __init__(self, kanban: Any = None) -> None:
        self.task_artifacts: Dict[str, List[Dict[str, Any]]] = {}
        self.project_tasks: List[Task] = []
        self.kanban_client = kanban
        self.context = None
        self.events = None
        self.subtask_manager = None


def _attachment_artifacts(artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter to artifacts sourced from board attachments."""
    return [a for a in artifacts if a.get("storage_type") == "attachment"]


class TestTaskTierAttachments:
    """Tier 1: the requesting task's own board attachments."""

    @pytest.mark.asyncio
    async def test_attachment_is_collected_with_canonical_fields(self) -> None:
        """A board attachment must survive with real values, not None."""
        from src.marcus_mcp.tools.context import _collect_task_artifacts

        task = _task("t1")
        state = _State(_StrictKanban({"t1": [CANONICAL_ATTACHMENT]}))

        artifacts = await _collect_task_artifacts("t1", task, state)

        found = _attachment_artifacts(artifacts)
        assert len(found) == 1, "board attachment was dropped from context"
        assert found[0]["filename"] == "api-contract.yaml"
        assert found[0]["created_by"] == "agent_alpha"
        assert found[0]["created_at"] == "2026-08-10T12:00:00+00:00"

    @pytest.mark.asyncio
    async def test_called_with_task_id_keyword(self) -> None:
        """The documented keyword is task_id; card_id raises TypeError."""
        from src.marcus_mcp.tools.context import _collect_task_artifacts

        kanban = _StrictKanban({"t1": [CANONICAL_ATTACHMENT]})
        await _collect_task_artifacts("t1", _task("t1"), _State(kanban))

        assert kanban.calls == ["t1"]

    @pytest.mark.asyncio
    async def test_location_prefers_provider_url(self) -> None:
        """The provider's real path beats a synthesized one."""
        from src.marcus_mcp.tools.context import _collect_task_artifacts

        state = _State(_StrictKanban({"t1": [CANONICAL_ATTACHMENT]}))
        artifacts = await _collect_task_artifacts("t1", _task("t1"), state)

        assert _attachment_artifacts(artifacts)[0]["location"] == (
            "docs/api/api-contract.yaml"
        )

    @pytest.mark.asyncio
    async def test_location_falls_back_when_url_absent(self) -> None:
        """Without url, synthesize ./attachments/<id>/<filename>."""
        from src.marcus_mcp.tools.context import _collect_task_artifacts

        no_url = {k: v for k, v in CANONICAL_ATTACHMENT.items() if k != "url"}
        state = _State(_StrictKanban({"t1": [no_url]}))

        artifacts = await _collect_task_artifacts("t1", _task("t1"), state)

        assert _attachment_artifacts(artifacts)[0]["location"] == (
            "./attachments/att-1/api-contract.yaml"
        )

    @pytest.mark.asyncio
    async def test_kanban_failure_is_non_fatal(self) -> None:
        """A provider error must not fail the whole context call."""
        from src.marcus_mcp.tools.context import _collect_task_artifacts

        class _Broken:
            async def get_attachments(self, task_id: str) -> Dict[str, Any]:
                raise RuntimeError("board unreachable")

        state = _State(_Broken())
        state.task_artifacts["t1"] = [{"filename": "logged.md"}]

        artifacts = await _collect_task_artifacts("t1", _task("t1"), state)

        assert [a["filename"] for a in artifacts] == ["logged.md"]


class TestDependencyTierAttachments:
    """Tier 2: attachments on the requesting task's direct dependencies."""

    @pytest.mark.asyncio
    async def test_dependency_attachment_is_collected(self) -> None:
        """A dependency's board attachment must reach the requester."""
        from src.marcus_mcp.tools.context import _collect_task_artifacts

        dep = _task("dep1")
        task = _task("t1", dependencies=["dep1"])
        state = _State(_StrictKanban({"dep1": [CANONICAL_ATTACHMENT]}))
        state.project_tasks = [task, dep]

        artifacts = await _collect_task_artifacts("t1", task, state)

        from_dep = [
            a
            for a in _attachment_artifacts(artifacts)
            if a.get("dependency_task_id") == "dep1"
        ]
        assert len(from_dep) == 1, "dependency attachment dropped from context"
        assert from_dep[0]["filename"] == "api-contract.yaml"
        assert from_dep[0]["created_by"] == "agent_alpha"
