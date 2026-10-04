"""
Unit tests for evidence required to close and claim handoff (#737, step 4).

A find, verify, or synthesize task cannot close without evidence that
matches its ``output_schema``; the rejection names the missing fields,
and the #677 retry ceiling terminalizes an agent that cannot produce
evidence instead of letting it loop.  When a find task closes, the
board copies its schema-validated evidence minus ``raw`` into the
``inputs`` of every dependent task; that copy is the only route by
which a claim reaches a verifier, and ``get_task_context`` on a verify
task returns those inputs, never the dependency's evidence.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools import task as task_module
from src.marcus_mcp.tools.context import get_task_context

pytestmark = pytest.mark.unit

FIND_SCHEMA = {
    "type": "object",
    "required": ["headline", "publisher", "date", "url"],
}

GOOD_EVIDENCE = {
    "headline": "AI Search Has a Citation Problem",
    "publisher": "Columbia Journalism Review",
    "date": "2025-03-06",
    "url": "https://www.cjr.org/tow_center/example.php",
    "raw": "the engine's full response, kept verbatim",
}


def _make_task(
    task_id: str,
    task_type: str = "find",
    output_schema: Optional[Dict[str, Any]] = None,
    dependencies: Optional[List[str]] = None,
    inputs: Optional[Dict[str, Any]] = None,
    status: TaskStatus = TaskStatus.TODO,
    assigned_to: Optional[str] = None,
) -> Task:
    """Build a minimal Task for gate tests."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=assigned_to,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        dependencies=dependencies or [],
        task_type=task_type,
        output_schema=output_schema,
        inputs=inputs or {},
    )


@pytest.fixture(autouse=True)
def _fresh_attempt_counters() -> Any:
    """Isolate the module-level #677 attempt counters between tests."""
    task_module._smoke_behavior_evidence_attempts.clear()
    yield
    task_module._smoke_behavior_evidence_attempts.clear()


class TestTypedEvidenceGate:
    """Test suite for _apply_typed_evidence_gate."""

    @pytest.mark.asyncio
    async def test_completion_without_evidence_is_rejected(self) -> None:
        """A find task reporting completed with no evidence is refused."""
        task = _make_task("find-1", output_schema=FIND_SCHEMA)

        response = await task_module._apply_typed_evidence_gate(
            task, "finder-pplx-1", None, Mock()
        )

        assert response is not None
        assert response["success"] is False
        assert response["error"] == "typed_evidence_missing"
        assert set(response["missing_fields"]) == set(FIND_SCHEMA["required"])

    @pytest.mark.asyncio
    async def test_rejection_names_exactly_the_missing_fields(self) -> None:
        """Partial evidence is refused and the gap is named precisely."""
        task = _make_task("find-1", output_schema=FIND_SCHEMA)
        partial = {"headline": "x", "publisher": "y"}

        response = await task_module._apply_typed_evidence_gate(
            task, "finder-pplx-1", partial, Mock()
        )

        assert response is not None
        assert response["error"] == "typed_evidence_incomplete"
        assert set(response["missing_fields"]) == {"date", "url"}
        assert "date" in response["blocker"]
        assert "url" in response["blocker"]

    @pytest.mark.asyncio
    async def test_evidence_matching_the_schema_passes(self) -> None:
        """Evidence with every required field lets the completion proceed."""
        task = _make_task("find-1", output_schema=FIND_SCHEMA)

        response = await task_module._apply_typed_evidence_gate(
            task, "finder-pplx-1", GOOD_EVIDENCE, Mock()
        )

        assert response is None

    @pytest.mark.asyncio
    async def test_no_schema_still_requires_some_evidence(self) -> None:
        """Without an output_schema, a typed task still cannot close empty."""
        task = _make_task("verify-1", task_type="verify", output_schema=None)

        rejected = await task_module._apply_typed_evidence_gate(
            task, "verifier-gem-1", {}, Mock()
        )
        accepted = await task_module._apply_typed_evidence_gate(
            task, "verifier-gem-1", {"verdict": "verified"}, Mock()
        )

        assert rejected is not None
        assert rejected["error"] == "typed_evidence_missing"
        assert accepted is None

    @pytest.mark.asyncio
    async def test_implement_tasks_are_not_gated_here(self) -> None:
        """The implement type stays on the existing smoke gate, untouched."""
        task = _make_task("impl-1", task_type="implement")

        response = await task_module._apply_typed_evidence_gate(
            task, "dev-1", None, Mock()
        )

        assert response is None

    @pytest.mark.asyncio
    async def test_ceiling_escalates_and_terminalizes(self) -> None:
        """The third identical failure terminalizes instead of looping.

        Reuses the #677 pattern: MAX_SMOKE_BEHAVIOR_EVIDENCE_ATTEMPTS
        rejections are retryable; the next one escalates, flips the
        board state through _terminalize_escalated_smoke_task, and
        tells the agent not to retry.
        """
        task = _make_task("find-9", output_schema=FIND_SCHEMA)
        state = Mock()

        with patch.object(
            task_module,
            "_terminalize_escalated_smoke_task",
            new=AsyncMock(),
        ) as terminalize:
            first = await task_module._apply_typed_evidence_gate(
                task, "finder-pplx-1", None, state
            )
            second = await task_module._apply_typed_evidence_gate(
                task, "finder-pplx-1", None, state
            )
            third = await task_module._apply_typed_evidence_gate(
                task, "finder-pplx-1", None, state
            )

        assert first is not None and first.get("terminal") is not True
        assert second is not None and second.get("terminal") is not True
        assert third is not None
        assert third["terminal"] is True
        assert third["error"] == "typed_evidence_escalated"
        terminalize.assert_awaited_once()


class TestClaimHandoff:
    """Test suite for _handoff_evidence_to_dependents."""

    def _board(self) -> tuple[Task, Task, Task, Mock]:
        """A find task with a dependent verify and an unrelated synthesize."""
        find = _make_task(
            "find-1",
            output_schema=FIND_SCHEMA,
            status=TaskStatus.DONE,
            assigned_to="finder-pplx-1",
        )
        verify = _make_task(
            "verify-1",
            task_type="verify",
            dependencies=["find-1"],
            inputs={"excerpt": "Collectively, they provided..."},
        )
        synthesize = _make_task(
            "synth-1", task_type="synthesize", dependencies=["verify-1"]
        )
        state = Mock()
        state.project_tasks = [find, verify, synthesize]
        state.kanban_client = Mock()
        state.kanban_client.update_task = AsyncMock()
        return find, verify, synthesize, state

    @pytest.mark.asyncio
    async def test_dependents_receive_claim_without_raw(self) -> None:
        """The verify task's inputs gain the claim fields, never raw."""
        find, verify, _, state = self._board()

        await task_module._handoff_evidence_to_dependents(find, GOOD_EVIDENCE, state)

        assert verify.inputs["headline"] == GOOD_EVIDENCE["headline"]
        assert verify.inputs["url"] == GOOD_EVIDENCE["url"]
        assert "raw" not in verify.inputs

    @pytest.mark.asyncio
    async def test_existing_inputs_are_preserved(self) -> None:
        """The handoff merges into inputs; the seeded excerpt survives."""
        find, verify, _, state = self._board()

        await task_module._handoff_evidence_to_dependents(find, GOOD_EVIDENCE, state)

        assert verify.inputs["excerpt"] == "Collectively, they provided..."

    @pytest.mark.asyncio
    async def test_non_dependents_are_untouched(self) -> None:
        """Only tasks that depend on the find task receive the claim."""
        find, _, synthesize, state = self._board()

        await task_module._handoff_evidence_to_dependents(find, GOOD_EVIDENCE, state)

        assert synthesize.inputs == {}

    @pytest.mark.asyncio
    async def test_handoff_is_persisted_through_the_kanban(self) -> None:
        """The dependent's new inputs are written to the board, not memory only."""
        find, verify, _, state = self._board()

        await task_module._handoff_evidence_to_dependents(find, GOOD_EVIDENCE, state)

        state.kanban_client.update_task.assert_awaited_once()
        call_args = state.kanban_client.update_task.await_args
        assert call_args.args[0] == verify.id
        assert call_args.args[1]["inputs"]["headline"] == GOOD_EVIDENCE["headline"]
        assert "raw" not in call_args.args[1]["inputs"]


class TestGeneralizedHandoff:
    """Test suite for the handoff at verify and approve closes.

    Step 7 review decision (Simon 16c9a4b1): every evidence-recorded
    close hands its evidence minus ``raw`` to dependents, so the
    approve reviewer sees claim plus verdict in inputs and the
    synthesize worker sees everything it renders.
    """

    def _chain(self) -> tuple[Task, Task, Task, Mock]:
        """verify -> approve -> synthesize, verify holding the claim."""
        verify = _make_task(
            "verify-1",
            task_type="verify",
            status=TaskStatus.DONE,
            assigned_to="verifier-gem-1",
            inputs={"excerpt": "...", "url": GOOD_EVIDENCE["url"]},
        )
        approve = _make_task(
            "approve-1", task_type="approve", dependencies=["verify-1"]
        )
        synthesize = _make_task(
            "synth-1", task_type="synthesize", dependencies=["approve-1"]
        )
        state = Mock()
        state.project_tasks = [verify, approve, synthesize]
        state.kanban_client = Mock()
        state.kanban_client.update_task = AsyncMock()
        return verify, approve, synthesize, state

    @pytest.mark.asyncio
    async def test_verify_close_hands_verdict_to_the_approve_task(self) -> None:
        """The reviewer's approve task receives the verdict in inputs."""
        verify, approve, _, state = self._chain()
        verdict_evidence = {
            "verdict": "verified",
            "checks": {"excerpt_found": True},
            "claim": {"url": GOOD_EVIDENCE["url"]},
        }

        await task_module._handoff_evidence_to_dependents(
            verify, verdict_evidence, state
        )

        assert approve.inputs["verdict"] == "verified"
        assert approve.inputs["claim"] == {"url": GOOD_EVIDENCE["url"]}

    @pytest.mark.asyncio
    async def test_approve_close_hands_decision_to_synthesize(self) -> None:
        """The synthesize task receives the decision in inputs."""
        _, approve, synthesize, state = self._chain()
        approve.status = TaskStatus.DONE

        await task_module._handoff_evidence_to_dependents(
            approve,
            {"decision": "approved", "approved_by": "reviewer"},
            state,
        )

        assert synthesize.inputs["decision"] == "approved"
        assert synthesize.inputs["approved_by"] == "reviewer"

    @pytest.mark.asyncio
    async def test_handoff_accumulates_case_state_down_the_chain(self) -> None:
        """Each close hands down its inputs plus its evidence.

        A sampled case's synthesize task depends on approve, not
        verify, so without accumulation it would see the decision but
        never the claim or the verdict it must render.
        """
        _, approve, synthesize, state = self._chain()
        approve.status = TaskStatus.DONE
        approve.inputs = {
            "verdict": "verified",
            "claim": {"url": GOOD_EVIDENCE["url"]},
        }

        await task_module._handoff_evidence_to_dependents(
            approve, {"decision": "approved"}, state
        )

        assert synthesize.inputs["decision"] == "approved"
        assert synthesize.inputs["verdict"] == "verified"
        assert synthesize.inputs["claim"] == {"url": GOOD_EVIDENCE["url"]}

    @pytest.mark.asyncio
    async def test_raw_is_stripped_at_every_handoff(self) -> None:
        """The raw field never crosses a handoff, whatever the close."""
        verify, approve, _, state = self._chain()

        await task_module._handoff_evidence_to_dependents(
            verify,
            {"verdict": "verified", "raw": "verifier scratch output"},
            state,
        )

        assert "raw" not in approve.inputs


class TestStdioDispatchCarriesEvidence:
    """Test suite for the stdio path's evidence plumbing (#735 divergence).

    The typed-evidence gate reads the ``evidence`` argument, so the
    stdio tool schema must declare it and the stdio dispatcher must
    forward it; otherwise a stdio agent's evidence silently becomes
    None and every typed completion is rejected until terminalized.
    """

    def test_schema_declares_evidence_and_verifications(self) -> None:
        """MCP clients only send fields the tool schema declares."""
        from src.marcus_mcp.handlers import get_tool_definitions

        tools = {t.name: t for t in get_tool_definitions("agent")}
        properties = tools["report_task_progress"].inputSchema["properties"]

        assert "evidence" in properties
        assert "verifications" in properties

    @pytest.mark.asyncio
    async def test_dispatcher_forwards_evidence_and_verifications(self) -> None:
        """The stdio dispatch passes both fields through to the tool."""
        from src.marcus_mcp import handlers

        state = Mock()
        state._current_client_id = None
        state._registered_clients = {}
        verifications = [{"signal_id": "s1", "command": "true"}]

        with (
            patch.object(
                handlers,
                "report_task_progress",
                new=AsyncMock(return_value={"success": True}),
            ) as impl,
            patch.object(handlers, "get_client_tools", return_value=["*"]),
        ):
            await handlers.handle_tool_call(
                name="report_task_progress",
                arguments={
                    "agent_id": "finder-pplx-1",
                    "task_id": "find-1",
                    "status": "completed",
                    "evidence": GOOD_EVIDENCE,
                    "verifications": verifications,
                },
                state=state,
            )

        impl.assert_awaited_once()
        kwargs = impl.await_args.kwargs
        assert kwargs["evidence"] == GOOD_EVIDENCE
        assert kwargs["verifications"] == verifications


class TestVerifyTaskContext:
    """Test suite for the verifier's view through get_task_context."""

    @pytest.mark.asyncio
    async def test_verify_context_is_inputs_only_with_no_raw(self) -> None:
        """A verify task's context carries the claim fields and no raw.

        This pins the independence invariant: the verifier sees what
        the board wrote into inputs, and the finder's raw response or
        reasoning can never reach it through the context tool.
        """
        claim = {k: v for k, v in GOOD_EVIDENCE.items() if k != "raw"}
        verify = _make_task(
            "verify-1",
            task_type="verify",
            dependencies=["find-1"],
            inputs={"excerpt": "Collectively...", **claim},
        )
        state = Mock()
        state.subtask_manager = None
        state.project_tasks = [verify]
        state.context = None

        with (
            patch("src.marcus_mcp.tools.context.log_agent_event"),
            patch(
                "src.experiments.live_experiment_monitor.get_active_monitor",
                return_value=None,
            ),
        ):
            result = await get_task_context("verify-1", state)

        assert result["success"] is True
        context = result["context"]
        assert context["task_type"] == "verify"
        assert context["inputs"]["url"] == claim["url"]
        assert "raw" not in json.dumps(result)
