"""Unit tests for the behavior-evidence judge in the product smoke gate (#677).

The product smoke gate historically proved a deliverable *builds* and
*serves* (exit 0 / HTTP 200), never that it *behaves*.  Issue #677 adds a
per-app-type behavior-evidence judge: when ``Task.source_context`` carries a
``structural_category`` with a behavior contract (web/pipeline/cli/library/
api/ml), the agent must submit ``evidence`` captured by actually RUNNING the
assembled product, and Marcus judges that evidence against the per-type bar
(web=non-empty rendered DOM + no console errors, pipeline=non-empty output,
etc.) BEFORE running any verification subprocess.

These tests prove:

* the judge fires for behavior-contract types and rejects missing / failing
  evidence,
* passing evidence satisfies the gate without a legacy ``start_command``,
* a passing web judgment proves what ``curl`` 200 cannot (the snake-pr667-5 /
  #463 / #636 blank-page regression),
* NON-web types are judged on their own bar (no web assumptions leak in),
* fuzzy types (``other``) with no contract are unaffected (no regression).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.task import _run_product_smoke_gate

pytestmark = pytest.mark.unit


def _make_task(
    structural_category: str,
    *,
    in_scope_outcome_ids: Optional[list] = None,
    task_id: str = "int-677",
) -> Task:
    """Build an integration task carrying a structural category.

    Parameters
    ----------
    structural_category : str
        Marcus's setup-time classification stashed on ``source_context``
        (e.g. ``"web app"``, ``"data pipeline"``).
    in_scope_outcome_ids : Optional[list]
        When provided, also stashes outcome IDs so the verifications
        coverage path applies.
    task_id : str
        Task identifier.

    Returns
    -------
    Task
        Integration task with the given category on ``source_context``.
    """
    source_context: Dict[str, Any] = {"structural_category": structural_category}
    if in_scope_outcome_ids is not None:
        source_context["in_scope_outcome_ids"] = in_scope_outcome_ids
    return Task(
        id=task_id,
        name="Integration verification for Test Project",
        description="Build and verify",
        status=TaskStatus.IN_PROGRESS,
        priority=Priority.HIGH,
        assigned_to="agent-001",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=1.0,
        labels=["integration", "verification", "type:integration"],
        source_context=source_context,
    )


def _make_state(project_root: str) -> Mock:
    """Build a Mock state whose workspace resolves to ``project_root``."""
    state = Mock()
    state.kanban_client = Mock()
    state.kanban_client._load_workspace_state = Mock(
        return_value={"project_root": project_root}
    )
    return state


_GOOD_WEB_DOM = "<div id='app'><canvas></canvas><div class='score'>0</div></div>"


class TestWebBehaviorEvidence:
    """A web app must submit a non-empty rendered DOM with no console errors."""

    @pytest.mark.asyncio
    async def test_missing_evidence_allowed_in_self_verify_mode(
        self, tmp_path: Path
    ) -> None:
        # Self-verify mode (#677 rework): Marcus no longer REQUIRES the agent
        # to submit evidence. The agent verifies its own run; the gate's hard
        # floor is the build (absent here — no manifest in tmp_path). So a
        # completion with no evidence passes the gate rather than gridlocking.
        task = _make_task("web app")
        state = _make_state(str(tmp_path))

        response = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-001",
            state=state,
            start_command=None,
            readiness_probe=None,
            verifications=None,
            evidence=None,
        )

        assert response is None

    @pytest.mark.asyncio
    async def test_empty_dom_rejected(self, tmp_path: Path) -> None:
        # If the agent VOLUNTEERS evidence, Marcus still judges it — empty DOM
        # (the snake-pr667-5 blank-page signature) is rejected.
        task = _make_task("web app")
        state = _make_state(str(tmp_path))

        response = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-001",
            state=state,
            start_command=None,
            readiness_probe=None,
            verifications=None,
            evidence={"dom": "", "console_errors": []},
        )

        assert response is not None
        assert response["error"] == "behavior_evidence_failed"

    @pytest.mark.asyncio
    async def test_console_errors_rejected(self, tmp_path: Path) -> None:
        task = _make_task("web app")
        state = _make_state(str(tmp_path))

        response = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-001",
            state=state,
            start_command=None,
            readiness_probe=None,
            verifications=None,
            evidence={
                "dom": _GOOD_WEB_DOM,
                "console_errors": ["ReferenceError: process is not defined"],
            },
        )

        assert response is not None
        assert response["error"] == "behavior_evidence_failed"

    @pytest.mark.asyncio
    async def test_rendered_dom_passes_without_start_command(
        self, tmp_path: Path
    ) -> None:
        # A passing behavior judgment satisfies the gate on its own — the
        # agent is NOT additionally required to declare a legacy
        # ``start_command`` (the rendered DOM proves more than curl 200).
        task = _make_task("web app")
        state = _make_state(str(tmp_path))

        response = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-001",
            state=state,
            start_command=None,
            readiness_probe=None,
            verifications=None,
            evidence={"dom": _GOOD_WEB_DOM, "console_errors": []},
        )

        assert response is None


class TestNonWebBehaviorEvidence:
    """Non-web types are judged on their own bar — no web assumptions leak."""

    @pytest.mark.asyncio
    async def test_pipeline_empty_output_rejected(self, tmp_path: Path) -> None:
        task = _make_task("data pipeline")
        state = _make_state(str(tmp_path))

        response = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-001",
            state=state,
            start_command=None,
            readiness_probe=None,
            verifications=None,
            evidence={"output": ""},
        )

        assert response is not None
        assert response["error"] == "behavior_evidence_failed"

    @pytest.mark.asyncio
    async def test_pipeline_nonempty_output_passes(self, tmp_path: Path) -> None:
        task = _make_task("data pipeline")
        state = _make_state(str(tmp_path))

        response = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-001",
            state=state,
            start_command=None,
            readiness_probe=None,
            verifications=None,
            evidence={"output": "id,score\n1,42\n2,17\n"},
        )

        assert response is None

    @pytest.mark.asyncio
    async def test_cli_exit_zero_with_stdout_passes(self, tmp_path: Path) -> None:
        task = _make_task("CLI tool")
        state = _make_state(str(tmp_path))

        response = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-001",
            state=state,
            start_command=None,
            readiness_probe=None,
            verifications=None,
            evidence={"exit_code": 0, "stdout": "done: 3 files processed"},
        )

        assert response is None

    @pytest.mark.asyncio
    async def test_cli_nonzero_exit_rejected(self, tmp_path: Path) -> None:
        task = _make_task("CLI tool")
        state = _make_state(str(tmp_path))

        response = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-001",
            state=state,
            start_command=None,
            readiness_probe=None,
            verifications=None,
            evidence={"exit_code": 1, "stdout": "boom"},
        )

        assert response is not None
        assert response["error"] == "behavior_evidence_failed"


class TestNoContractTypeUnaffected:
    """Fuzzy types with no behavior contract pass when the build floor passes."""

    @pytest.mark.asyncio
    async def test_other_type_with_no_build_floor_passes(self, tmp_path: Path) -> None:
        # ``other`` has no behavior contract and tmp_path has no build
        # manifest, so there is no build floor and no proof is required →
        # self-verify mode passes the gate.
        task = _make_task("other")
        state = _make_state(str(tmp_path))

        response = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-001",
            state=state,
            start_command=None,
            readiness_probe=None,
            verifications=None,
            evidence=None,
        )

        assert response is None


class TestOutcomeBearingWebTask:
    """A web task with in-scope outcomes no longer requires agent-authored proof."""

    @pytest.mark.asyncio
    async def test_outcomes_no_verifications_passes_in_self_verify_mode(
        self, tmp_path: Path
    ) -> None:
        # Self-verify mode: declaring in-scope outcomes no longer forces the
        # agent to author ``verifications``. With no build manifest and no
        # volunteered proof, the gate passes — the agent's own run is the
        # outcome verification.
        task = _make_task("web app", in_scope_outcome_ids=["o_play"])
        state = _make_state(str(tmp_path))

        response = await _run_product_smoke_gate(
            task=task,
            agent_id="agent-001",
            state=state,
            start_command=None,
            readiness_probe=None,
            verifications=None,
            evidence=None,
        )

        assert response is None
