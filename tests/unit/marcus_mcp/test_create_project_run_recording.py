"""
Tests for ``create_project``'s run + path wiring (Simon ``7ed3074d``).

These tests exercise the two-phase attribution + ``path`` discriminator
the wrapper in ``src/marcus_mcp/tools/nlp.py`` is responsible for:

- Every successful ``create_project`` records exactly one row in the
  ``runs`` table.
- ``runs.run_id`` matches the value cost rows recorded during the
  wrapper's PlannerContext push.
- ``runs.path`` reflects the entry point: ``"direct"`` by default,
  ``"marcus"`` / ``"posidonius"`` when supplied in ``options``.
- Failure leaves the runs table clean (no half-written rows).
- Missing ``cost_store`` attribute degrades gracefully.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_marcus_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a pre-built MarcusConfig stub.

    The wrapper hits ``get_config()`` on the create_project path
    (kanban provider lookup, snapshot writer). On a fresh checkout
    without ``config_marcus.json``, ``MarcusConfig.from_file()``
    falls back to defaults whose ``ai.anthropic_api_key`` is None,
    and ``validate()`` then raises. Setting ``CLAUDE_API_KEY`` in env
    is not sufficient — substitution only fires when a config file is
    actually loaded. Patch the module-level singleton directly so the
    test is hermetic on any developer machine and in CI. (Codex P1)
    """
    from src.config import marcus_config as mc

    stub = mc.MarcusConfig()
    stub.ai.anthropic_api_key = "test-key-for-unit-tests"
    monkeypatch.setattr(mc, "_config", stub)


@pytest.fixture(autouse=True)
def _clear_create_project_dedup_cache() -> Any:
    """Hermetic tests: clear the module-level dedup cache around each test."""
    from src.marcus_mcp.tools import nlp as nlp_module

    nlp_module._recent_create_project_calls.clear()
    yield
    nlp_module._recent_create_project_calls.clear()


def _build_state(cost_store: Any) -> Mock:
    """Mock state pre-wired with a real CostStore so the wrapper writes to disk."""
    from src.integrations.kanban_interface import KanbanProvider

    state = Mock()
    state.log_event = Mock()
    state.kanban_client = Mock()
    state.kanban_client.provider = KanbanProvider.PLANKA
    state.kanban_client.project_id = "1670692878487127607"
    state.kanban_client.board_id = "1670692878621345337"
    state.ai_engine = Mock()
    state.subtask_manager = Mock()
    state.project_registry = Mock()
    state.project_manager = Mock()
    state.cost_store = cost_store
    state.project_registry.add_project = AsyncMock(return_value="abc-123-def-456")
    state.project_registry.get_active_project = AsyncMock(return_value=None)
    state.project_manager.switch_project = AsyncMock()
    state.project_manager.get_kanban_client = AsyncMock(
        return_value=state.kanban_client
    )
    state._subtasks_migrated = False
    return state


@pytest.fixture
def cost_store(tmp_path: Path) -> Any:
    """Real tmp CostStore so we can inspect token_events + runs."""
    from src.cost_tracking.cost_store import CostStore

    return CostStore(db_path=tmp_path / "costs.db")


@pytest.fixture
def recorder(cost_store: Any) -> Any:
    """Enabled cost recorder wired to the tmp store."""
    from src.cost_tracking.cost_recorder import CostRecorder, set_recorder

    rec = CostRecorder(store=cost_store, enabled=True)
    set_recorder(rec)
    yield rec
    set_recorder(None)


def _ok_creator_result() -> Dict[str, Any]:
    """A canned success payload from the underlying creator."""
    return {
        "success": True,
        "project_name": "Flight Simulator",
        "tasks_created": 29,
        "task_ids": ["task-1"],
        "board": {
            "project_name": "Flight Simulator",
            "board_name": "Main Board",
        },
    }


class TestRunRecording:
    """Direct-MCP ``create_project`` records a ``runs`` row on success."""

    @pytest.mark.asyncio
    async def test_one_run_row_per_successful_call(
        self, cost_store: Any, recorder: Any
    ) -> None:
        """Exactly one row in ``runs``; path defaults to 'direct'."""
        from src.marcus_mcp.tools.nlp import create_project

        state = _build_state(cost_store)
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as MockCreator:
            MockCreator.return_value.create_project_from_description = AsyncMock(
                return_value=_ok_creator_result()
            )
            result = await create_project(
                description="Build a flight simulator",
                project_name="Flight Simulator",
                options={"provider": "planka"},
                state=state,
            )

        assert result["success"] is True
        rows = list(
            cost_store.conn.execute(
                "SELECT run_id, project_id, project_name, path FROM runs"
            )
        )
        assert len(rows) == 1, f"expected one runs row, got {rows}"
        run_id, pid, name, path = rows[0]
        assert len(run_id) == 32 and all(c in "0123456789abcdef" for c in run_id)
        assert pid == "abc123def456"  # canonical (dashless)
        assert name == "Flight Simulator"
        assert path == "direct", "default path for unmarked options must be 'direct'"

    @pytest.mark.asyncio
    async def test_no_row_on_failure(self, cost_store: Any, recorder: Any) -> None:
        """A failed creator leaves the runs table clean."""
        from src.marcus_mcp.tools.nlp import create_project

        state = _build_state(cost_store)
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as MockCreator:
            MockCreator.return_value.create_project_from_description = AsyncMock(
                return_value={"success": False, "error": "decomposer blew up"}
            )
            result = await create_project(
                description="x",
                project_name="Will Fail",
                options={"provider": "planka"},
                state=state,
            )

        assert result["success"] is False
        rows = list(cost_store.conn.execute("SELECT * FROM runs"))
        assert rows == []

    @pytest.mark.asyncio
    async def test_run_id_matches_token_events(
        self, cost_store: Any, recorder: Any
    ) -> None:
        """The runs.run_id matches what cost rows recorded during the wrapper.

        This is the join the dashboard depends on — if the two don't
        match, the experiment view returns empty and the fix is dead.
        """
        from src.cost_tracking.cost_recorder import get_recorder
        from src.marcus_mcp.tools.nlp import create_project

        state = _build_state(cost_store)

        async def _emits_cost(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
            get_recorder().record_planner_call(
                operation="decompose_prd",
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_tokens=100,
                output_tokens=50,
            )
            return _ok_creator_result()

        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as MockCreator:
            MockCreator.return_value.create_project_from_description = AsyncMock(
                side_effect=_emits_cost
            )
            await create_project(
                description="x",
                project_name="Flight Sim",
                options={"provider": "planka"},
                state=state,
            )

        run_row = cost_store.conn.execute("SELECT run_id FROM runs").fetchone()
        te_row = cost_store.conn.execute(
            "SELECT DISTINCT run_id FROM token_events "
            "WHERE operation = 'decompose_prd'"
        ).fetchone()
        assert run_row is not None and te_row is not None
        assert run_row[0] == te_row[0]
        assert run_row[0] != "unassigned"


class TestCodexP2DedupReplay:
    """Regression: dedup-cached retries must not insert phantom runs rows.

    Codex P2 on PR #522: when ``create_project`` is invoked within the
    10-minute dedup window after a successful first call, the inner
    function returns the cached result without doing planner work. The
    wrapper used to still insert a fresh ``runs`` row on each replay,
    so dashboards and per-run counts grew zero-cost phantom rows for
    timeout/retry storms. Verify exactly one row survives N retries.
    """

    @pytest.mark.asyncio
    async def test_replay_does_not_create_phantom_run_row(
        self, cost_store: Any, recorder: Any
    ) -> None:
        """Three identical calls in a row → still one row in ``runs``."""
        from src.marcus_mcp.tools.nlp import create_project

        state = _build_state(cost_store)
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as MockCreator:
            MockCreator.return_value.create_project_from_description = AsyncMock(
                return_value=_ok_creator_result()
            )
            first = await create_project(
                description="Build a flight simulator",
                project_name="Flight Simulator",
                options={"provider": "planka"},
                state=state,
            )
            # Two retries within the dedup window — both hit the
            # cached-replay path inside _create_project_inner.
            second = await create_project(
                description="Build a flight simulator",
                project_name="Flight Simulator",
                options={"provider": "planka"},
                state=state,
            )
            third = await create_project(
                description="Build a flight simulator",
                project_name="Flight Simulator",
                options={"provider": "planka"},
                state=state,
            )

        # All three reported success to the agent…
        assert first["success"] is True
        assert second["success"] is True
        assert third["success"] is True
        # …but only the original work produced a runs row.
        rows = list(cost_store.conn.execute("SELECT run_id FROM runs"))
        assert len(rows) == 1, f"expected 1 runs row across replays, got {rows}"
        # The internal dedup marker must not leak to the agent.
        assert "_dedup_cached" not in second
        assert "_dedup_cached" not in third

    @pytest.mark.asyncio
    async def test_replay_does_not_refire_project_created(
        self, cost_store: Any, recorder: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dedup-cached retries must not re-emit ``project_created``.

        Codex P2 follow-up: a retry that hits the 10-minute cached-
        success window did not create a new project, so firing
        ``project_created`` again would double-count one creation in
        creation / fallback analytics. The telemetry hook is gated on
        the same ``_dedup_cached`` flag that skips the runs row.

        Falsification recipe: drop the ``not _dedup_cached`` guard on
        the ``fire_project_created`` call — this test fails with 3
        events instead of 1.
        """
        from src.marcus_mcp.tools import nlp as nlp_mod

        fired: list = []
        monkeypatch.setattr(
            "src.telemetry.events.fire_project_created",
            lambda **kw: fired.append(kw),
        )

        state = _build_state(cost_store)
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as MockCreator:
            MockCreator.return_value.create_project_from_description = AsyncMock(
                return_value=_ok_creator_result()
            )
            for _ in range(3):
                await nlp_mod.create_project(
                    description="Build a flight simulator",
                    project_name="Flight Simulator",
                    options={"provider": "planka"},
                    state=state,
                )

        # First call created the project; the two replays did not.
        assert len(fired) == 1, f"expected 1 project_created event, got {len(fired)}"


class TestPhase0LocalProviderTaxonomy:
    """Phase 0 ``is_local_llm`` agrees with the session telemetry taxonomy."""

    @pytest.mark.parametrize(
        "provider,expected_local",
        [
            ("local", True),
            ("ollama", True),  # Codex P2: Ollama is a local provider
            ("anthropic", False),
            ("openai", False),
        ],
    )
    def test_is_local_llm_uses_shared_taxonomy(
        self,
        provider: str,
        expected_local: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_persist_phase0_open_signals`` classifies providers like server.py.

        Codex P2: the Phase 0 cost row used ``provider == "local"``,
        so ``ai.provider = "ollama"`` runs were stamped is_local_llm
        False — disagreeing with the ``session_started`` event, which
        treats both as local. Both must use ``_LOCAL_LLM_PROVIDERS``.

        Falsification recipe: revert to ``provider == "local"`` — the
        ``ollama`` case fails.
        """
        from unittest.mock import MagicMock

        from src.config import marcus_config as mc
        from src.marcus_mcp.tools.nlp import _persist_phase0_open_signals

        mc._config.ai.provider = provider  # type: ignore[union-attr]

        cost_store = MagicMock()
        _persist_phase0_open_signals(
            cost_store,
            run_id="r1",
            description="build something",
            result={"domain": "devtools", "structural_category": "CLI tool"},
        )

        cost_store.persist_phase0_run_signals.assert_called_once()
        kwargs = cost_store.persist_phase0_run_signals.call_args.kwargs
        assert kwargs["is_local_llm"] is expected_local


class TestPathDiscriminator:
    """``options['path']`` flows into ``runs.path``."""

    @pytest.mark.asyncio
    async def test_default_path_is_direct(self, cost_store: Any, recorder: Any) -> None:
        """When options lack a 'path' key, the wrapper stamps 'direct'."""
        from src.marcus_mcp.tools.nlp import create_project

        state = _build_state(cost_store)
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as MockCreator:
            MockCreator.return_value.create_project_from_description = AsyncMock(
                return_value=_ok_creator_result()
            )
            await create_project(
                description="x",
                project_name="P",
                # No 'path' in options — must default to 'direct'.
                options={"provider": "planka"},
                state=state,
            )
        path = cost_store.conn.execute("SELECT path FROM runs").fetchone()[0]
        assert path == "direct"

    @pytest.mark.asyncio
    async def test_explicit_marcus_path_flows_through(
        self, cost_store: Any, recorder: Any
    ) -> None:
        """The session launcher's path='marcus' lands in the runs row."""
        from src.marcus_mcp.tools.nlp import create_project

        state = _build_state(cost_store)
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as MockCreator:
            MockCreator.return_value.create_project_from_description = AsyncMock(
                return_value=_ok_creator_result()
            )
            await create_project(
                description="x",
                project_name="P",
                options={"provider": "planka", "path": "marcus"},
                state=state,
            )
        path = cost_store.conn.execute("SELECT path FROM runs").fetchone()[0]
        assert path == "marcus"

    @pytest.mark.asyncio
    async def test_explicit_posidonius_path_flows_through(
        self, cost_store: Any, recorder: Any
    ) -> None:
        """Posidonius's path='posidonius' lands in the runs row."""
        from src.marcus_mcp.tools.nlp import create_project

        state = _build_state(cost_store)
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as MockCreator:
            MockCreator.return_value.create_project_from_description = AsyncMock(
                return_value=_ok_creator_result()
            )
            await create_project(
                description="x",
                project_name="P",
                options={"provider": "planka", "path": "posidonius"},
                state=state,
            )
        path = cost_store.conn.execute("SELECT path FROM runs").fetchone()[0]
        assert path == "posidonius"


class TestDecomposerDiscriminator:
    """``options['decomposer']`` flows into ``runs.decomposer`` (Marcus #519).

    Stamping the decomposer label on the ``runs`` row at create-time is
    the prerequisite for the dashboard's ``by_decomposer`` slice. Without
    it, feature-based and contract-first projects are indistinguishable
    in the cost data even though they have very different cost shapes.
    """

    @pytest.mark.asyncio
    async def test_feature_based_flows_through(
        self, cost_store: Any, recorder: Any
    ) -> None:
        """Explicit ``decomposer='feature_based'`` lands on the row."""
        from src.marcus_mcp.tools.nlp import create_project

        state = _build_state(cost_store)
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as MockCreator:
            MockCreator.return_value.create_project_from_description = AsyncMock(
                return_value=_ok_creator_result()
            )
            await create_project(
                description="x",
                project_name="P",
                options={"provider": "planka", "decomposer": "feature_based"},
                state=state,
            )
        decomposer = cost_store.conn.execute("SELECT decomposer FROM runs").fetchone()[
            0
        ]
        assert decomposer == "feature_based"

    @pytest.mark.asyncio
    async def test_contract_first_flows_through(
        self, cost_store: Any, recorder: Any
    ) -> None:
        """Explicit ``decomposer='contract_first'`` lands on the row."""
        from src.marcus_mcp.tools.nlp import create_project

        state = _build_state(cost_store)
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as MockCreator:
            MockCreator.return_value.create_project_from_description = AsyncMock(
                return_value=_ok_creator_result()
            )
            await create_project(
                description="x",
                project_name="P",
                options={"provider": "planka", "decomposer": "contract_first"},
                state=state,
            )
        decomposer = cost_store.conn.execute("SELECT decomposer FROM runs").fetchone()[
            0
        ]
        assert decomposer == "contract_first"

    @pytest.mark.asyncio
    async def test_unknown_decomposer_falls_back_to_feature_based(
        self, cost_store: Any, recorder: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Garbage decomposer values resolve to 'feature_based' (matches resolve_decomposer)."""
        from src.marcus_mcp.tools.nlp import create_project

        # Strip the env var so resolve_decomposer's options-then-env-then-default
        # chain reaches the unknown-value branch deterministically.
        monkeypatch.delenv("MARCUS_DECOMPOSER", raising=False)

        state = _build_state(cost_store)
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as MockCreator:
            MockCreator.return_value.create_project_from_description = AsyncMock(
                return_value=_ok_creator_result()
            )
            await create_project(
                description="x",
                project_name="P",
                options={"provider": "planka", "decomposer": "not_a_real_strategy"},
                state=state,
            )
        decomposer = cost_store.conn.execute("SELECT decomposer FROM runs").fetchone()[
            0
        ]
        assert decomposer == "feature_based"

    @pytest.mark.asyncio
    async def test_contract_first_fallback_stamps_actual_not_requested(
        self, cost_store: Any, recorder: Any
    ) -> None:
        """Request-vs-reality: contract_first that falls back stamps 'feature_based'.

        Regression for Kaia's PR #539 review. ``resolve_decomposer``
        returns what the user asked for; the inner function silently
        falls back when contract_first cannot proceed (no project_root,
        weak contracts, empty domains). The row must reflect what RAN,
        not what was requested — otherwise the #529 A/B/C comparison
        data is corrupt.

        Simulate the divergence by having the mocked inner function
        return ``actual_decomposer='feature_based'`` even though the
        caller requested ``contract_first``.
        """
        from src.marcus_mcp.tools.nlp import create_project

        state = _build_state(cost_store)
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as MockCreator:
            MockCreator.return_value.create_project_from_description = AsyncMock(
                return_value={
                    **_ok_creator_result(),
                    "actual_decomposer": "feature_based",
                }
            )
            await create_project(
                description="x",
                project_name="P",
                # Caller asked for contract_first…
                options={"provider": "planka", "decomposer": "contract_first"},
                state=state,
            )
        decomposer = cost_store.conn.execute("SELECT decomposer FROM runs").fetchone()[
            0
        ]
        # …but the row reflects what actually ran.
        assert decomposer == "feature_based"

    @pytest.mark.asyncio
    async def test_missing_actual_decomposer_falls_back_to_requested(
        self, cost_store: Any, recorder: Any
    ) -> None:
        """When the inner function omits actual_decomposer, fall back to requested.

        Defensive: older creator paths (or future ones that don't set
        the marker) must not produce a NULL ``runs.decomposer``. The
        resolve_decomposer value is the safe fallback.
        """
        from src.marcus_mcp.tools.nlp import create_project

        state = _build_state(cost_store)
        with patch(
            "src.integrations.nlp_tools.NaturalLanguageProjectCreator"
        ) as MockCreator:
            # Standard fixture lacks actual_decomposer key.
            MockCreator.return_value.create_project_from_description = AsyncMock(
                return_value=_ok_creator_result()
            )
            await create_project(
                description="x",
                project_name="P",
                options={"provider": "planka", "decomposer": "feature_based"},
                state=state,
            )
        decomposer = cost_store.conn.execute("SELECT decomposer FROM runs").fetchone()[
            0
        ]
        assert decomposer == "feature_based"
