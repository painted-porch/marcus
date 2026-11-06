"""Unit tests for ``_kanban_init_lock_manager`` in ``create_project`` (issue #461).

Closes the verification gap left by PR #452: the lock that prevents the
807s stall under concurrent ``create_project`` calls had no test
exercising the concurrent path.  Without this test, future refactors of
``nlp.py`` could remove or move the lock without anyone noticing
(Track 2 / #442 will rebuild this whole area).

The 807s stall was caused by N concurrent callers each detecting
``need_new_client=True`` and racing to overwrite ``state.kanban_client``,
leaving earlier callers with orphaned connections and triggering the
dedup-guard loop.  The lock makes "check provider then maybe init"
atomic per event loop.

Regression-net taxonomy
-----------------------
Per Kaia review #7 (Simon ``6c77618f``), only one of these tests is a
true regression net for lock removal.  Empirically verified by
replacing ``async with _kanban_init_lock_manager.get_lock():`` with
``if True:`` and running the suite — only Test 3 fails.

* **Test 3 (``test_no_caller_observes_half_initialized_client``)** is
  THE regression net.  A slow ``connect()`` exposes any caller that
  bypasses the lock and observes ``state.kanban_client`` populated
  but not yet connected.  This is the actual race the lock protects
  against.

* **Tests 1, 2, 4** are invariant assertions, not regression nets for
  the lock itself — they pin behaviour that would hold the same way
  with or without it.  They don't replace Test 3 for catching lock
  removal.

  Issue #856 update: ``create_project`` no longer skips
  ``KanbanFactory.create`` when the existing client's provider
  matches the requested one — it always builds a fresh, unshared
  client, because reusing one risked mutating an instance
  ``ProjectContextManager`` still had cached for another project.
  Tests 1 and 4 now pin "once per call, always", not "once total,
  skipped on a match".

Verification recipe for future hardening
----------------------------------------
To verify a regression net actually catches the bug it claims to:
remove the load-bearing line, run the test suite, expect failure.
If zero tests fail, the suite isn't guarding the property.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _make_state_no_client() -> MagicMock:
    """Build a state mock with kanban_client=None to force init.

    The lock-protected block detects need_new_client=True when
    ``state.kanban_client`` is falsy or its provider doesn't match
    ``requested_provider``.  Setting client to None forces the init
    path on every concurrent caller, which is the regression scenario.
    """
    state = MagicMock()
    state.kanban_client = None
    state.ai_engine = MagicMock()
    state.subtask_manager = None
    state.log_event = MagicMock()
    state.events = None
    return state


def _stub_creator_result(project_id: str) -> dict[str, Any]:
    """Build a minimal create_project return for the NLPC mock."""
    return {
        "success": True,
        "project_id": project_id,
        "tasks_created": 1,
        "recommended_agents": 1,
    }


class TestKanbanInitLockUnderConcurrency:
    """``_kanban_init_lock_manager`` makes "check provider, maybe init"
    atomic per event loop.  These tests pin the contract.
    """

    @pytest.mark.asyncio
    async def test_kanban_factory_create_called_once_per_concurrent_call(
        self, tmp_path: Path
    ) -> None:
        """Invariant assertion (NOT a regression net): three concurrent
        ``create_project`` calls -> ``KanbanFactory.create`` runs
        exactly three times, once per call.

        Issue #856, Codex P1: ``create_project`` used to skip
        ``KanbanFactory.create`` whenever ``state.kanban_client``
        already matched the requested provider, reusing (and
        mutating) that same object. That object is frequently the one
        ``ProjectContextManager`` has cached for the active project,
        so mutating it in place to point at a brand-new project
        corrupted the cache entry and leaked one project's tasks into
        another's view. The fix always builds a fresh, unshared
        client, so under N concurrent calls the factory now runs N
        times, not once.

        Why keep this test: pins the invariant that future refactors
        must preserve -- `create_project` must never again special-case
        a "the client I already have will do" path, because that path
        is exactly what caused #856.
        """
        from src.marcus_mcp.tools import nlp as nlp_module

        state = _make_state_no_client()

        # Track how many times KanbanFactory.create runs.  The lock
        # contract: exactly once across all concurrent callers.
        factory_create_calls = 0

        def _counted_factory_create(provider: str) -> Any:
            nonlocal factory_create_calls
            factory_create_calls += 1
            client = MagicMock()
            client.provider = provider
            client.connect = AsyncMock()
            client.board_id = "board-test"
            return client

        # Stub NaturalLanguageProjectCreator so create_project completes
        # quickly without running real decomposition / LLM / persistence.
        # The lock contract is exercised before NLPC is constructed.
        mock_creator_class = MagicMock()
        mock_creator_instance = MagicMock()
        mock_creator_instance.create_project_from_description = AsyncMock(
            side_effect=lambda **kwargs: _stub_creator_result(
                f"proj-{kwargs['project_name']}"
            )
        )
        mock_creator_class.return_value = mock_creator_instance

        # Stub get_config to bypass marcus_config.json validation
        # (kanban.provider is the only field create_project reads).
        cfg = MagicMock()
        cfg.kanban = MagicMock()
        cfg.kanban.provider = "sqlite"

        with (
            patch(
                "src.config.marcus_config.get_config",
                return_value=cfg,
            ),
            patch(
                "src.integrations.kanban_factory.KanbanFactory.create",
                side_effect=_counted_factory_create,
            ),
            patch(
                "src.integrations.nlp_tools.NaturalLanguageProjectCreator",
                mock_creator_class,
            ),
        ):
            results = await asyncio.gather(
                *[
                    nlp_module.create_project(
                        description=f"description {i}",
                        project_name=f"proj_{i}",
                        options={
                            "provider": "sqlite",
                            "project_root": str(tmp_path / f"impl_{i}"),
                        },
                        state=state,
                    )
                    for i in range(3)
                ]
            )

        # All 3 calls completed
        assert len(results) == 3
        for r in results:
            assert r.get("success") is True

        # KanbanFactory.create ran once per call — every create_project
        # call gets its own, unshared client (issue #856).
        assert factory_create_calls == 3, (
            f"KanbanFactory.create must run once per concurrent "
            f"create_project call; ran {factory_create_calls} times"
        )

    @pytest.mark.asyncio
    async def test_concurrent_calls_complete_within_wall_clock_budget(
        self, tmp_path: Path
    ) -> None:
        """Bounds check (NOT a regression net): 3 concurrent calls finish
        in seconds, not the 807s pre-fix stall.

        Per Kaia review #7 (Simon ``6c77618f``): this is a sanity
        bound, not a lock-removal regression net.  Without the lock,
        the test still passes in milliseconds because subsequent
        callers see ``state.kanban_client`` already set and skip
        init — the 807s stall required the dedup-guard loop downstream
        which isn't exercised in this isolated test.  Test 3 is the
        real regression net.

        Why keep this test: catches order-of-magnitude regression in
        the happy path (e.g., if a future refactor introduces a slow
        operation inside the lock that blocks all concurrent callers
        serially for too long).
        """
        from src.marcus_mcp.tools import nlp as nlp_module

        state = _make_state_no_client()

        def _factory_create(provider: str) -> Any:
            client = MagicMock()
            client.provider = provider
            client.connect = AsyncMock()
            client.board_id = "board-test"
            return client

        mock_creator_class = MagicMock()
        mock_creator_instance = MagicMock()
        mock_creator_instance.create_project_from_description = AsyncMock(
            side_effect=lambda **kwargs: _stub_creator_result(
                f"proj-{kwargs['project_name']}"
            )
        )
        mock_creator_class.return_value = mock_creator_instance

        start = time.monotonic()
        # Stub get_config to bypass marcus_config.json validation
        # (kanban.provider is the only field create_project reads).
        cfg = MagicMock()
        cfg.kanban = MagicMock()
        cfg.kanban.provider = "sqlite"

        with (
            patch(
                "src.config.marcus_config.get_config",
                return_value=cfg,
            ),
            patch(
                "src.integrations.kanban_factory.KanbanFactory.create",
                side_effect=_factory_create,
            ),
            patch(
                "src.integrations.nlp_tools.NaturalLanguageProjectCreator",
                mock_creator_class,
            ),
        ):
            await asyncio.gather(
                *[
                    nlp_module.create_project(
                        description=f"description {i}",
                        project_name=f"budget_test_{i}",
                        options={
                            "provider": "sqlite",
                            "project_root": str(tmp_path / f"impl_{i}"),
                        },
                        state=state,
                    )
                    for i in range(3)
                ]
            )
        elapsed = time.monotonic() - start

        # 5s is generous — the actual happy path is ~10ms with mocks.
        # Catches order-of-magnitude regression (807s stall would be
        # multiple orders over budget).
        assert elapsed < 5.0, (
            f"Concurrent create_project should complete in seconds, "
            f"not stall.  Elapsed: {elapsed:.1f}s"
        )

    @pytest.mark.asyncio
    async def test_no_caller_observes_half_initialized_client(
        self, tmp_path: Path
    ) -> None:
        """**THE regression net for lock removal.**  Every concurrent
        call must see a fully-initialized ``state.kanban_client``.

        Per Kaia review #7 (Simon ``6c77618f``): empirically verified
        — replacing ``async with _kanban_init_lock_manager.get_lock():``
        with ``if True:`` causes this test to fail (and only this
        test).  If you delete or move the lock, this is what catches it.

        Why this test catches the bug while Tests 1, 2, 4 don't: a
        slow ``connect()`` exposes the actual race the lock protects
        against.  Without the lock:

        1. Caller A: ``state.kanban_client = KanbanFactory.create(...)``
           (sync, completes), then ``await connect()`` (slow, yields)
        2. Caller B enters: sees ``state.kanban_client`` populated,
           provider matches, skips init, exits the (lock-removed) block
        3. B proceeds to NLPC while A's ``connect()`` is still running
        4. NLPC observes ``connected_event`` not yet set → records
           "half-init observed" for B

        Verification recipe: ``sed -i 's/async with
        _kanban_init_lock_manager.get_lock():/if True:/'
        src/marcus_mcp/tools/nlp.py && pytest <this file>``.
        Expect this test to fail.  Restore with ``git checkout``.
        """
        from src.marcus_mcp.tools import nlp as nlp_module

        state = _make_state_no_client()

        # Track whether any caller observed a state.kanban_client
        # whose connect() hadn't completed.
        half_init_observed = []

        connected_event = asyncio.Event()

        async def _slow_connect() -> None:
            # Yield to other coroutines so they can race if not locked
            await asyncio.sleep(0.01)
            connected_event.set()

        def _factory_create(provider: str) -> Any:
            client = MagicMock()
            client.provider = provider
            client.connect = AsyncMock(side_effect=_slow_connect)
            client.board_id = "board-test"
            return client

        mock_creator_class = MagicMock()

        async def _check_client_state(**kwargs: Any) -> dict[str, Any]:
            # By the time NLPC runs, state.kanban_client must be fully
            # initialized (connect() finished).  If a half-init slipped
            # through, connected_event would still be unset on a caller
            # that bypassed the lock.
            if not connected_event.is_set():
                half_init_observed.append(kwargs["project_name"])
            return _stub_creator_result(f"proj-{kwargs['project_name']}")

        mock_creator_instance = MagicMock()
        mock_creator_instance.create_project_from_description = AsyncMock(
            side_effect=_check_client_state
        )
        mock_creator_class.return_value = mock_creator_instance

        # Stub get_config to bypass marcus_config.json validation
        # (kanban.provider is the only field create_project reads).
        cfg = MagicMock()
        cfg.kanban = MagicMock()
        cfg.kanban.provider = "sqlite"

        with (
            patch(
                "src.config.marcus_config.get_config",
                return_value=cfg,
            ),
            patch(
                "src.integrations.kanban_factory.KanbanFactory.create",
                side_effect=_factory_create,
            ),
            patch(
                "src.integrations.nlp_tools.NaturalLanguageProjectCreator",
                mock_creator_class,
            ),
        ):
            await asyncio.gather(
                *[
                    nlp_module.create_project(
                        description=f"description {i}",
                        project_name=f"half_init_test_{i}",
                        options={
                            "provider": "sqlite",
                            "project_root": str(tmp_path / f"impl_{i}"),
                        },
                        state=state,
                    )
                    for i in range(3)
                ]
            )

        assert half_init_observed == [], (
            f"No caller should observe a half-initialized kanban_client; "
            f"these callers did: {half_init_observed}"
        )

    @pytest.mark.asyncio
    async def test_existing_matching_client_is_never_reused(
        self, tmp_path: Path
    ) -> None:
        """Issue #856, Codex P1: even when ``state.kanban_client``
        already matches the requested provider, every concurrent
        caller still builds a fresh client via ``KanbanFactory.create``
        rather than reusing the existing one.

        This used to assert the opposite -- that a matching existing
        client made ``KanbanFactory.create`` a no-op. That was the bug:
        reusing (and mutating) a client that may be the SAME object
        ``ProjectContextManager`` has cached for the currently-active
        project corrupted that cache entry when this call's new
        project/board got stamped onto it, leaking one project's tasks
        into another project's view. A fresh instance per call, even
        on a provider match, is what the fix requires.
        """
        from src.integrations.kanban_interface import KanbanProvider
        from src.marcus_mcp.tools import nlp as nlp_module

        state = MagicMock()
        # Pre-populate with a sqlite client so need_new_client is False
        existing_client = MagicMock()
        existing_client.provider = KanbanProvider.SQLITE
        existing_client.board_id = "board-existing"
        state.kanban_client = existing_client
        state.ai_engine = MagicMock()
        state.subtask_manager = None
        state.log_event = MagicMock()
        state.events = None

        factory_create_calls = 0

        def _counted_factory_create(provider: str) -> Any:
            nonlocal factory_create_calls
            factory_create_calls += 1
            client = MagicMock()
            client.provider = provider
            client.connect = AsyncMock()
            client.board_id = "board-test"
            return client

        mock_creator_class = MagicMock()
        mock_creator_instance = MagicMock()
        mock_creator_instance.create_project_from_description = AsyncMock(
            side_effect=lambda **kwargs: _stub_creator_result(
                f"proj-{kwargs['project_name']}"
            )
        )
        mock_creator_class.return_value = mock_creator_instance

        # Stub get_config to bypass marcus_config.json validation
        # (kanban.provider is the only field create_project reads).
        cfg = MagicMock()
        cfg.kanban = MagicMock()
        cfg.kanban.provider = "sqlite"

        with (
            patch(
                "src.config.marcus_config.get_config",
                return_value=cfg,
            ),
            patch(
                "src.integrations.kanban_factory.KanbanFactory.create",
                side_effect=_counted_factory_create,
            ),
            patch(
                "src.integrations.nlp_tools.NaturalLanguageProjectCreator",
                mock_creator_class,
            ),
        ):
            await asyncio.gather(
                *[
                    nlp_module.create_project(
                        description=f"description {i}",
                        project_name=f"existing_client_{i}",
                        options={
                            "provider": "sqlite",
                            "project_root": str(tmp_path / f"impl_{i}"),
                        },
                        state=state,
                    )
                    for i in range(3)
                ]
            )

        assert factory_create_calls == 3, (
            f"KanbanFactory.create must run for every call, even when "
            f"state already has a matching client (issue #856); ran "
            f"{factory_create_calls} times"
        )
