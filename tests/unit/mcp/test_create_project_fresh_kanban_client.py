"""
Unit tests: ``create_project`` never reuses or mutates the caller's
existing ``state.kanban_client``.

Issue #856, Codex P1 round 1: when the requested provider matched the
currently-active one, ``create_project`` reused ``state.kanban_client``
in place, clearing only its ``project_id``/``board_id`` to force new
project creation. That object is frequently the SAME instance
``ProjectContextManager`` has cached for the active project (selecting
a project stamps ``state.kanban_client`` from that cache). Mutating it
in place to point at a brand-new project corrupted the cached entry:
selecting the OLD project again returned a client now wired to the
NEW project's board, while still carrying the OLD project's registry
id (#854) -- so the old project's agents could see and claim the new
project's work.

The fix: always build a brand-new client instance for ``create_project``,
never reuse or mutate whatever ``state.kanban_client`` already holds.
A fresh, unshared instance can never alias a cached context, so there
is nothing left to corrupt.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _state(old_client: MagicMock) -> MagicMock:
    state = MagicMock()
    state.kanban_client = old_client
    state.log_event = MagicMock()
    state.events = None
    return state


async def _call(
    options: Any, name: str, old_client: MagicMock
) -> tuple[dict, MagicMock, MagicMock]:
    from src.marcus_mcp.tools import nlp as nlp_module

    state = _state(old_client)
    dedup_key = f"{name}:Build a small calculator with a CLI"
    nlp_module._recent_create_project_calls.pop(dedup_key, None)

    new_client = MagicMock(name="fresh_client")
    new_client.provider = "planka"
    new_client.connect = AsyncMock()

    result: dict = {}
    try:
        with (
            patch(
                "src.integrations.kanban_factory.KanbanFactory.create",
                return_value=new_client,
            ),
            patch(
                "src.integrations.nlp_tools.NaturalLanguageProjectCreator",
                side_effect=RuntimeError("stop before the rest of the pipeline"),
            ),
        ):
            try:
                result = await nlp_module.create_project(
                    description="Build a small calculator with a CLI",
                    project_name=name,
                    options=options,
                    state=state,
                )
            except RuntimeError:
                # The client is already built and assigned to
                # state.kanban_client by this point; everything after
                # it is irrelevant to this test.
                result = {"success": False}
    finally:
        nlp_module._recent_create_project_calls.pop(dedup_key, None)
    return result, state, new_client


class TestCreateProjectBuildsAFreshKanbanClient:
    """The old client object is never reused, mutated, or still referenced."""

    @pytest.mark.asyncio
    async def test_a_second_project_on_the_same_provider_gets_a_distinct_client(
        self,
    ) -> None:
        """Same provider as the active project: still a brand-new instance."""
        old_client = MagicMock(name="project_a_client")
        old_client.provider = "planka"
        old_client.project_id = "a-project-id"
        old_client.board_id = "a-board-id"

        result, state, new_client = await _call(
            {"provider": "planka"}, "ProjectB", old_client
        )

        assert result["success"] is False  # stopped early by design
        assert state.kanban_client is new_client
        assert state.kanban_client is not old_client

    @pytest.mark.asyncio
    async def test_the_old_clients_scoping_fields_are_never_touched(self) -> None:
        """The removed reuse path used to null out project_id/board_id on the
        SAME object. That object may still be cached elsewhere (e.g. as the
        active project's ProjectContextManager entry) -- it must come out of
        create_project exactly as it went in."""
        old_client = MagicMock(name="project_a_client")
        old_client.provider = "planka"
        old_client.project_id = "a-project-id"
        old_client.board_id = "a-board-id"

        await _call({"provider": "planka"}, "ProjectC", old_client)

        assert old_client.project_id == "a-project-id"
        assert old_client.board_id == "a-board-id"

    @pytest.mark.asyncio
    async def test_a_cached_context_holding_the_old_client_is_unaffected(
        self,
    ) -> None:
        """Simulates ProjectContextManager's per-project cache: a second
        reference to the SAME old client object, as if project A's context
        were still holding it. Creating project B must leave that cache
        entry resolvable to the untouched original client."""
        old_client = MagicMock(name="project_a_client")
        old_client.provider = "planka"
        old_client.project_id = "a-project-id"
        old_client.board_id = "a-board-id"
        cached_context_for_a = MagicMock()
        cached_context_for_a.kanban_client = old_client

        await _call({"provider": "planka"}, "ProjectD", old_client)

        assert cached_context_for_a.kanban_client is old_client
        assert cached_context_for_a.kanban_client.project_id == "a-project-id"
