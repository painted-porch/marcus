"""
Unit tests: ``create_project`` relays a human-authored objective to the board.

The stigmergy primer (Simon 390438ee): a project's stated objective is a
primer signal, distinct from the releaser markers agents post with
``give_opinion``. It is written by the project's author, never synthesised
by Marcus, and relayed at ``create_project`` as a project-level ``Decision``
(``task_id=None``, ``kind="objective"``, subject ``project.objective``)
stamped with the new project's id, so an agent can read it before its first
pick through ``query_project_history(..., "opinions", kind="objective")``.

Two properties matter: the relay never fails project creation, and the
primer carries its own ``kind`` so the opinions read (which defaults to
``kind="assessment"``) never counts it as agent corroboration.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

pytestmark = pytest.mark.unit


def _state() -> MagicMock:
    state = MagicMock()
    state.kanban_client = MagicMock()
    state.log_event = MagicMock()
    state.events = None
    return state


def _reached_pipeline(state: MagicMock) -> bool:
    return any(
        call.args and call.args[0] == "create_project_calling"
        for call in state.log_event.call_args_list
    )


async def _call(options: Any, name: str, tmp_path: Path) -> tuple[dict, MagicMock]:
    from src.marcus_mcp.tools import nlp as nlp_module

    state = _state()
    options = {"provider": "sqlite", "project_root": str(tmp_path / "impl"), **options}
    dedup_key = f"{name}:Build a tiny thing"
    nlp_module._recent_create_project_calls.pop(dedup_key, None)
    fake_config = MagicMock()
    fake_config.kanban.provider = "sqlite"
    try:
        with (
            patch("src.config.marcus_config.get_config", return_value=fake_config),
            patch(
                "src.integrations.kanban_factory.KanbanFactory.create",
                side_effect=RuntimeError("stop before any provider work"),
            ),
        ):
            result = await nlp_module.create_project(
                description="Build a tiny thing",
                project_name=name,
                options=options,
                state=state,
            )
    finally:
        nlp_module._recent_create_project_calls.pop(dedup_key, None)
    return result, state


class TestObjectiveOptionValidation:
    """A malformed objective stops at the door; a valid one reaches the pipeline."""

    @pytest.mark.asyncio
    async def test_non_string_objective_is_rejected_before_any_work(
        self, tmp_path: Path
    ) -> None:
        result, state = await _call(
            {"objective": ["not", "a", "string"]}, "obj-bad", tmp_path
        )
        assert result["success"] is False
        assert result["error_code"] == "objective_invalid"
        assert not _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_blank_objective_is_rejected_before_any_work(
        self, tmp_path: Path
    ) -> None:
        result, state = await _call({"objective": "   "}, "obj-blank", tmp_path)
        assert result["success"] is False
        assert result["error_code"] == "objective_invalid"
        assert not _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_valid_objective_reaches_the_pipeline(self, tmp_path: Path) -> None:
        result, state = await _call(
            {"objective": "Build a camp the crew can survive the night in."},
            "obj-ok",
            tmp_path,
        )
        assert result.get("error_code") != "objective_invalid"
        assert _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_without_objective_option_nothing_changes(
        self, tmp_path: Path
    ) -> None:
        result, state = await _call({}, "obj-none", tmp_path)
        assert result.get("error_code") != "objective_invalid"
        assert _reached_pipeline(state)


def _decision(**overrides: Any) -> Mock:
    decision = Mock()
    payload = {
        "decision_id": "dec_1_1.0",
        "task_id": None,
        "agent_id": "marcus",
        "subject": "project.objective",
        "kind": "objective",
        "what": "Build a camp the crew can survive the night in.",
        "why": "stated by the project's author at create_project",
        "impact": "Not specified",
        "project_id": "p-new",
    }
    payload.update(overrides)
    decision.decision_id = payload["decision_id"]
    decision.to_dict = Mock(return_value=payload)
    return decision


def _recording_state() -> Mock:
    state = Mock()
    state.context = Mock()
    state.context.log_decision = AsyncMock(return_value=_decision())
    state.kanban_client = Mock()
    state.kanban_client.add_comment = AsyncMock(return_value={"success": True})
    state.project_tasks = []
    state.current_project_name = "Camp"
    state.current_project_id = "p-new"
    state.events = None
    # The history store the primer is read back from (Codex P2 on PR #820):
    # by default it holds the primer, so the happy path reads back as recorded.
    stored = Mock()
    stored.kind = "objective"
    stored.subject = "project.objective"
    stored.project_id = "p-new"
    state.project_history_persistence = Mock()
    state.project_history_persistence.load_decisions = AsyncMock(return_value=[stored])
    return state


class TestRecordProjectObjective:
    """The relay writes exactly one project-level objective Decision."""

    @pytest.mark.asyncio
    async def test_writes_a_project_level_objective_decision(self) -> None:
        from src.marcus_mcp.tools.nlp import _record_project_objective

        state = _recording_state()
        with patch(
            "src.marcus_mcp.tools.context._persist_decision_to_history",
            new=AsyncMock(),
        ) as persist:
            ok = await _record_project_objective(
                state,
                "p-new",
                "Camp",
                "Build a camp the crew can survive the night in.",
            )

        assert ok is True
        kwargs = state.context.log_decision.call_args.kwargs
        assert kwargs["task_id"] is None
        assert kwargs["kind"] == "objective"
        assert kwargs["subject"] == "project.objective"
        assert kwargs["project_id"] == "p-new"
        assert kwargs["what"] == "Build a camp the crew can survive the night in."
        persist.assert_awaited_once()
        assert persist.call_args.kwargs["project_id"] == "p-new"

    @pytest.mark.asyncio
    async def test_marcus_relays_the_text_verbatim(self) -> None:
        """Marcus never rewrites the author's objective (Invariant 2 v2)."""
        from src.marcus_mcp.tools.nlp import _record_project_objective

        state = _recording_state()
        text = "  Keep every chest within 15 blocks of camp.  "
        with patch(
            "src.marcus_mcp.tools.context._persist_decision_to_history",
            new=AsyncMock(),
        ):
            await _record_project_objective(state, "p-new", "Camp", text)

        assert state.context.log_decision.call_args.kwargs["what"] == text.strip()

    @pytest.mark.asyncio
    async def test_a_failing_relay_reports_false_and_does_not_raise(self) -> None:
        from src.marcus_mcp.tools.nlp import _record_project_objective

        state = _recording_state()
        state.context.log_decision = AsyncMock(side_effect=RuntimeError("db down"))
        ok = await _record_project_objective(state, "p-new", "Camp", "Build a camp.")
        assert ok is False

    @pytest.mark.asyncio
    async def test_no_context_system_reports_false(self) -> None:
        from src.marcus_mcp.tools.nlp import _record_project_objective

        state = _recording_state()
        state.context = None
        ok = await _record_project_objective(state, "p-new", "Camp", "Build a camp.")
        assert ok is False


class TestObjectiveRecordedMeansReadable:
    """``objective_recorded`` is a read-back, not a hope (Codex P2 on PR #820).

    ``_persist_decision_to_history`` swallows its own failures and
    ``Context.log_decision`` tolerates a failed persistence, so a primer can
    exist only in memory while the history read the agents use returns
    nothing. The relay therefore reads the primer back from the history
    store before claiming success.
    """

    @staticmethod
    def _history(decisions: list) -> Mock:
        history = Mock()
        history.load_decisions = AsyncMock(return_value=decisions)
        return history

    @pytest.mark.asyncio
    async def test_reports_false_when_the_history_store_has_no_primer(self) -> None:
        from src.marcus_mcp.tools.nlp import _record_project_objective

        state = _recording_state()
        state.project_history_persistence = self._history([])
        with patch(
            "src.marcus_mcp.tools.context._persist_decision_to_history",
            new=AsyncMock(),  # "succeeds" without writing anything
        ):
            ok = await _record_project_objective(
                state, "p-new", "Camp", "Build a camp."
            )
        assert ok is False

    @pytest.mark.asyncio
    async def test_reports_true_only_when_the_primer_reads_back(self) -> None:
        from src.marcus_mcp.tools.nlp import _record_project_objective

        stored = Mock()
        stored.kind = "objective"
        stored.subject = "project.objective"
        stored.project_id = "p-new"
        state = _recording_state()
        state.project_history_persistence = self._history([stored])
        with patch(
            "src.marcus_mcp.tools.context._persist_decision_to_history",
            new=AsyncMock(),
        ):
            ok = await _record_project_objective(
                state, "p-new", "Camp", "Build a camp."
            )
        assert ok is True
