"""
Unit tests: ``create_project`` accepts ``options.task_type_registrations``.

Issue #911 (ADR-0013 K2, the MCP-only write path): an extension seeding a
typed chain over MCP has to declare its task types on the project record
in the same call that mints the record, or the kernel's typed-evidence gate
and offer-time eligibility fall back to the literal ``find``/``verify``
names (#909). A malformed declaration is refused before any state is
touched, with the same shape ``options.tasks`` and ``options.objective``
use, and the kernel's own registration validator
(``src/core/project_registry.py``) decides what malformed means.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

GOOD = {
    "find": {"evidence_schema": {"required": ["headline", "url"]}},
    "verify": {
        "evidence_schema": {"required": ["verdict", "checks"]},
        "verifies_task_type": "find",
    },
}


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


class TestTaskTypeRegistrationsOptionValidation:
    """Malformed declarations stop at the door; valid ones reach the pipeline."""

    @pytest.mark.asyncio
    async def test_a_non_mapping_is_rejected_before_any_work(
        self, tmp_path: Path
    ) -> None:
        """The option must be a mapping of task type to registration."""
        result, state = await _call(
            {"task_type_registrations": ["find", "verify"]}, "reg-bad-1", tmp_path
        )
        assert result["success"] is False
        assert result["error_code"] == "task_type_registrations_invalid"
        assert "must be a mapping" in result["error"]
        assert not _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_an_entry_that_is_not_a_mapping_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """Each task type's entry is itself a mapping."""
        result, state = await _call(
            {"task_type_registrations": {"find": "evidence please"}},
            "reg-bad-2",
            tmp_path,
        )
        assert result["error_code"] == "task_type_registrations_invalid"
        assert "'find'" in result["error"]
        assert not _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_the_kernels_own_k4_rule_is_applied(self, tmp_path: Path) -> None:
        """injected_checks without checks_injectable is the registry's rule, applied here."""
        result, state = await _call(
            {
                "task_type_registrations": {
                    "verify": {"injected_checks": [{"name": "fetched_ok"}]}
                }
            },
            "reg-bad-3",
            tmp_path,
        )
        assert result["error_code"] == "task_type_registrations_invalid"
        assert "checks_injectable" in result["error"]
        assert not _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_a_valid_declaration_reaches_the_pipeline(
        self, tmp_path: Path
    ) -> None:
        """A well-formed declaration passes (the pipeline is stubbed to stop)."""
        result, state = await _call(
            {"task_type_registrations": GOOD}, "reg-ok", tmp_path
        )
        assert result.get("error_code") != "task_type_registrations_invalid"
        assert _reached_pipeline(state)

    @pytest.mark.asyncio
    async def test_without_the_option_nothing_changes(self, tmp_path: Path) -> None:
        """No declaration: the validator is a no-op."""
        result, state = await _call({}, "reg-none", tmp_path)
        assert result.get("error_code") != "task_type_registrations_invalid"
        assert _reached_pipeline(state)
