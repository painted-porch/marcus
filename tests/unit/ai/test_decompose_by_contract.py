"""
Unit tests for ``AdvancedPRDParser.decompose_by_contract`` (GH-320 PR 2).

Tests the contract-first decomposition path that produces tasks where
each task owns one side of a contract interface. Experiment 1
(2026-04-10) validated the mechanism on a hand-crafted TypeScript
contract; these tests pin the productionized path.

Test strategy
-------------
- Stub ``LLMAbstraction`` at ``AdvancedPRDParser`` construction time
  so Marcus config validation is bypassed. This is the project-wide
  convention for unit tests that instantiate the parser — see
  ``tests/unit/ai/test_advanced_prd_parser.py`` and every other
  existing parser test.
- Mock ``AIAnalysisEngine.generate_structured_response`` to return
  controlled task lists. Never call real LLMs from unit tests.
- Exercise the happy path, the empty-contracts fallback, the
  LLM-failure path, and the Task.responsibility plumbing.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.advanced.prd.advanced_parser import (
    AdvancedPRDParser,
    PRDAnalysis,
    ProjectConstraints,
)
from src.core.models import TaskStatus

pytestmark = pytest.mark.unit


def _make_parser() -> AdvancedPRDParser:
    """
    Instantiate ``AdvancedPRDParser`` with ``LLMAbstraction`` stubbed.

    Matches the project convention for parser unit tests (see
    ``test_advanced_prd_parser.py``). Without the stub, parser
    construction would call ``get_config()`` and fail in environments
    that have no ``config_marcus.json`` or ``CLAUDE_API_KEY``
    (e.g. CI).
    """
    with patch("src.ai.advanced.prd.advanced_parser.LLMAbstraction") as mock_llm_class:
        mock_llm_class.return_value = MagicMock()
        return AdvancedPRDParser()


def _make_prd_analysis() -> PRDAnalysis:
    """Build a minimal PRDAnalysis for tests."""
    return PRDAnalysis(
        functional_requirements=[
            {
                "id": "game_engine",
                "name": "Game Engine",
                "description": "Core game loop and state management",
                "complexity": "coordinated",
            },
            {
                "id": "game_ui",
                "name": "Game UI",
                "description": "Render and handle user input",
                "complexity": "simple",
            },
        ],
        non_functional_requirements=[],
        technical_constraints=[],
        business_objectives=[],
        user_personas=[],
        success_metrics=[],
        implementation_approach="iterative",
        complexity_assessment={"level": "medium"},
        risk_factors=[],
        confidence=0.85,
        original_description="Build a snake game with React and TypeScript",
    )


def _make_contract_artifacts():
    """Build contract artifacts matching _generate_contracts_by_domain output."""
    return {
        "Game Engine": {
            "artifacts": [
                {
                    "filename": "types.ts",
                    "artifact_type": "api",
                    "content": (
                        "export interface GameEngine {\n"
                        "  tick(): GameState;\n"
                        "  reset(): void;\n"
                        "}\n"
                        "export interface GameState {\n"
                        "  snake: Coord[];\n"
                        "  food: Coord;\n"
                        "  score: number;\n"
                        "}\n"
                    ),
                    "description": "Shared interface contract",
                    "relative_path": "docs/api/types.ts",
                },
            ],
            "decisions": [],
        },
        "Game UI": {
            "artifacts": [
                {
                    "filename": "ui-contract.md",
                    "artifact_type": "specification",
                    "content": "# UI contract\n\nConsumes GameState from types.ts\n",
                    "description": "UI consumer contract",
                    "relative_path": "docs/specifications/ui-contract.md",
                },
            ],
            "decisions": [],
        },
    }


class TestDecomposeByContract:
    """Test suite for AdvancedPRDParser.decompose_by_contract."""

    @pytest.fixture(autouse=True)
    def _no_spec_coverage(self) -> Any:
        """Stub out the SpecCoverageAugmenter so these tests stay
        focused on contract decomposition.

        Issue #456 Stage 4 wires ``SpecCoverageAugmenter`` into
        ``decompose_by_contract`` via the augmenter chain.  That
        augmenter calls :func:`check_spec_coverage`, which makes a
        real LLM call to extract spec features — which would both (a)
        contaminate these unit tests with network I/O and (b) inflate
        the returned task count with synthesized spec_gap tasks.
        Tests in this class assert exact contract task shape, so we
        replace ``check_spec_coverage`` with a no-op for the entire
        class.  Tests that need real spec_coverage behavior should
        live in ``test_spec_coverage_augmenter.py``.
        """
        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock:
            yield mock

    @pytest.mark.asyncio
    async def test_happy_path_produces_tasks_with_responsibility(self):
        """
        LLM returns structured tasks → Task objects with responsibility.

        The decomposer builds Task objects whose ``responsibility``
        field names the contract interface each task owns. This is the
        core productionization of experiment 1.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement GameEngine",
                    "description": "Build the game loop and state transitions",
                    "responsibility": "implements GameEngine interface from types.ts",
                    "contract_file": "docs/api/types.ts",
                    "provides": "GameState transitions, tick() implementation",
                    "requires": "None",
                    "estimated_minutes": 10,
                },
                {
                    "name": "Implement Game UI",
                    "description": "Render the board and handle keyboard input",
                    "responsibility": "consumes GameState, implements UI layer",
                    "contract_file": "docs/specifications/ui-contract.md",
                    "provides": "Rendered board, input dispatch",
                    "requires": "GameEngine tick() output",
                    "estimated_minutes": 8,
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
                constraints=ProjectConstraints(complexity_mode="standard"),
            )
            tasks = result.augmented_tasks

        assert len(tasks) == 2

        engine_task = tasks[0]
        assert engine_task.name == "Implement GameEngine"
        assert engine_task.responsibility == (
            "implements GameEngine interface from types.ts"
        )
        assert engine_task.status == TaskStatus.TODO
        assert engine_task.source_type == "contract_first"
        assert engine_task.source_context is not None
        assert engine_task.source_context.get("contract_file") == "docs/api/types.ts"
        assert engine_task.provides == "GameState transitions, tick() implementation"
        # "None" maps to Python None for requires
        assert engine_task.requires is None
        # 10 minutes -> hours
        assert abs(engine_task.estimated_hours - (10 / 60)) < 1e-6
        # Description embeds the contract reference
        assert "docs/api/types.ts" in engine_task.description

        ui_task = tasks[1]
        assert ui_task.responsibility == "consumes GameState, implements UI layer"
        assert ui_task.requires == "GameEngine tick() output"

    @pytest.mark.asyncio
    async def test_empty_contracts_raises_value_error(self):
        """
        All-None contracts → ValueError so caller can fall back.

        When every domain's contract generation produced no artifacts
        (payload=None), the decomposer cannot proceed. The caller in
        NaturalLanguageProjectCreator catches ValueError and falls back
        to feature_based with a visible warning.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()

        empty_contracts = {"Game Engine": None, "Game UI": None}

        with pytest.raises(ValueError, match="no usable domains"):
            await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=empty_contracts,
            )

    @pytest.mark.asyncio
    async def test_contracts_with_empty_artifacts_raises_value_error(self):
        """
        Domains with empty artifact lists are treated as unusable.

        A payload with ``{"artifacts": []}`` is as useless as None —
        the LLM would see no contract content to decompose against.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()

        empty_contracts = {
            "Game Engine": {"artifacts": [], "decisions": []},
        }

        with pytest.raises(ValueError, match="no usable domains"):
            await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=empty_contracts,
            )

    @pytest.mark.asyncio
    async def test_llm_failure_raises_runtime_error(self):
        """LLM exception → RuntimeError so caller can fall back."""
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            side_effect=TimeoutError("LLM call timed out"),
        ):
            with pytest.raises(RuntimeError, match="Contract decomposition LLM"):
                await parser.decompose_by_contract(
                    prd_analysis=prd_analysis,
                    contract_artifacts=contracts,
                )

    @pytest.mark.asyncio
    async def test_empty_task_list_in_response_raises(self):
        """LLM returns ``{"tasks": []}`` → RuntimeError (unusable)."""
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value={"tasks": []},
        ):
            with pytest.raises(RuntimeError, match="contained no tasks"):
                await parser.decompose_by_contract(
                    prd_analysis=prd_analysis,
                    contract_artifacts=contracts,
                )

    @pytest.mark.asyncio
    async def test_labels_include_contract_first(self):
        """Contract-first tasks carry a 'contract_first' label for filtering."""
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement thing",
                    "description": "Do the thing",
                    "responsibility": "implements Thing from contract",
                    "contract_file": "docs/api/types.ts",
                    "provides": "thing",
                    "requires": "None",
                    "estimated_minutes": 5,
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )
            tasks = result.augmented_tasks

        assert "contract_first" in tasks[0].labels
        assert "implementation" in tasks[0].labels

    @pytest.mark.asyncio
    async def test_contract_file_embedded_in_description(self):
        """Task description includes the contract file path for agent discovery."""
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement thing",
                    "description": "Build the thing (no contract ref in LLM output)",
                    "responsibility": "implements Thing interface",
                    "contract_file": "docs/api/types.ts",
                    "provides": "thing",
                    "requires": "None",
                    "estimated_minutes": 5,
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )
            tasks = result.augmented_tasks

        # Even if the LLM didn't embed the contract_file in its own
        # description text, the decomposer augments it so agents can
        # read the contract without hunting through source_context.
        assert "docs/api/types.ts" in tasks[0].description
        assert "implements Thing interface" in tasks[0].description

    @pytest.mark.asyncio
    async def test_malformed_estimated_minutes_coerced_to_default(self):
        """
        LLM returns ``estimated_minutes: null`` → coerced to 8.0 default.

        ``generate_structured_response`` parses JSON but does NOT
        enforce the schema, so nulls can leak through. Must not
        raise TypeError from ``float(None)``. Codex P1 on PR #327.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement thing",
                    "description": "Do the thing",
                    "responsibility": "implements Thing",
                    "contract_file": "docs/api/types.ts",
                    "provides": "thing",
                    "requires": "None",
                    "estimated_minutes": None,  # malformed
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )
            tasks = result.augmented_tasks

        assert len(tasks) == 1
        # Default 8.0 minutes → 8/60 hours
        assert abs(tasks[0].estimated_hours - (8.0 / 60)) < 1e-6

    @pytest.mark.asyncio
    async def test_malformed_description_coerced_to_string(self):
        """
        LLM returns non-string description → coerced to string.

        Must not raise AttributeError from ``.strip()`` on a non-str.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement thing",
                    # LLM returned null for description
                    "description": None,
                    "responsibility": "implements Thing",
                    "contract_file": "docs/api/types.ts",
                    "provides": "thing",
                    "requires": "None",
                    "estimated_minutes": 5,
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )
            tasks = result.augmented_tasks

        assert len(tasks) == 1
        # Empty description + contract metadata marker appended
        assert "MARCUS_CONTRACT_FIRST" in tasks[0].description

    @pytest.mark.asyncio
    async def test_non_dict_task_raises_runtime_error(self):
        """
        LLM returns a task that's not a dict (e.g. a string) → clean
        RuntimeError so the caller's fallback fires.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                "this is not a dict",  # malformed
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            with pytest.raises(RuntimeError, match="not a dict"):
                await parser.decompose_by_contract(
                    prd_analysis=prd_analysis,
                    contract_artifacts=contracts,
                )

    @pytest.mark.asyncio
    async def test_responsibility_persisted_in_source_context(self):
        """
        decompose_by_contract stores responsibility in source_context
        so providers that persist source_context (e.g. SQLite) can
        round-trip contract metadata even though Task.responsibility
        isn't a top-level kanban column. Codex P1 on PR #327.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement thing",
                    "description": "Do the thing",
                    "responsibility": "implements Thing interface",
                    "contract_file": "docs/api/types.ts",
                    "provides": "thing",
                    "requires": "None",
                    "estimated_minutes": 5,
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )
            tasks = result.augmented_tasks

        task = tasks[0]
        assert task.responsibility == "implements Thing interface"
        assert task.source_context is not None
        # Responsibility also stored in source_context as belt-and-suspenders
        assert task.source_context["responsibility"] == "implements Thing interface"
        assert task.source_context["contract_file"] == "docs/api/types.ts"

    @pytest.mark.asyncio
    async def test_description_embeds_marker_for_persistence_fallback(self):
        """
        decompose_by_contract embeds MARCUS_CONTRACT_FIRST marker in
        description so the metadata survives providers that only
        persist description (e.g. Planka).
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement thing",
                    "description": "Build the thing",
                    "responsibility": "implements Thing from types.ts",
                    "contract_file": "docs/api/types.ts",
                    "provides": "thing",
                    "requires": "None",
                    "estimated_minutes": 5,
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )
            tasks = result.augmented_tasks

        desc = tasks[0].description
        assert "<!-- MARCUS_CONTRACT_FIRST" in desc
        assert "responsibility: implements Thing from types.ts" in desc
        assert "contract_file: docs/api/types.ts" in desc
        assert "-->" in desc

    @pytest.mark.asyncio
    async def test_mixed_none_and_usable_contracts(self):
        """Partial contract generation → decomposer uses only usable ones."""
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        # One domain succeeded, one failed (None)
        contracts = {
            "Game Engine": {
                "artifacts": [
                    {
                        "filename": "types.ts",
                        "artifact_type": "api",
                        "content": "export interface GameEngine {}",
                        "description": "contract",
                        "relative_path": "docs/api/types.ts",
                    }
                ],
                "decisions": [],
            },
            "Game UI": None,  # contract generation failed for this domain
        }

        llm_response = {
            "tasks": [
                {
                    "name": "Implement GameEngine",
                    "description": "Build the engine",
                    "responsibility": "implements GameEngine",
                    "contract_file": "docs/api/types.ts",
                    "provides": "engine",
                    "requires": "None",
                    "estimated_minutes": 8,
                }
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ) as mock_llm:
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )
            tasks = result.augmented_tasks

        # Decomposition proceeded with 1 usable contract
        assert len(tasks) == 1
        # Verify the prompt sent to the LLM only included the Game Engine
        # section (the None domain was filtered out).
        call_args = mock_llm.call_args
        prompt = call_args.kwargs["prompt"]
        assert "Game Engine" in prompt
        assert "Game UI" not in prompt

    @pytest.mark.asyncio
    async def test_acceptance_criteria_populated_from_llm_response(self):
        """
        Contract-derived acceptance criteria flow through to Task.

        The decomposition LLM returns ``acceptance_criteria`` in its
        structured output — verifiable outcomes restated from the
        contract. These must land on the Task object so the validator
        has something concrete to check against. Empty criteria was
        the root cause of the Experiment 4 v2/v3 validator blocker
        where agents completed 47 passing tests but the validator
        rejected with "Acceptance Criteria Not Provided".
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement WeatherWidget",
                    "description": "Build the weather display module",
                    "responsibility": (
                        "implements WeatherWidget interface from "
                        "weather-contracts.md"
                    ),
                    "contract_file": "docs/api/weather-contracts.md",
                    "provides": "Weather data display",
                    "requires": "None",
                    "estimated_minutes": 10,
                    "acceptance_criteria": [
                        "Module exposes temperature and conditions "
                        "fields as defined in the contract",
                        "Weather data is fetchable from the " "configured API endpoint",
                        "Module integrates with the Dashboard "
                        "container through the contract boundary",
                    ],
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )
            tasks = result.augmented_tasks

        assert len(tasks) == 1
        task = tasks[0]
        assert len(task.acceptance_criteria) == 3, (
            f"Expected 3 acceptance criteria from LLM response, "
            f"got {len(task.acceptance_criteria)}: "
            f"{task.acceptance_criteria}"
        )
        assert "temperature" in task.acceptance_criteria[0].lower()

    @pytest.mark.asyncio
    async def test_acceptance_criteria_graceful_when_missing(self):
        """
        LLM omits acceptance_criteria → Task gets empty list, not crash.

        Backward compatibility: existing LLM responses that don't
        include the new field should produce tasks with empty
        acceptance_criteria (same as before this change). The
        validator will still struggle, but the decomposer won't
        crash.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement GameEngine",
                    "description": "Build the engine",
                    "responsibility": "implements GameEngine",
                    "contract_file": "docs/api/types.ts",
                    "provides": "engine",
                    "requires": "None",
                    "estimated_minutes": 8,
                    # acceptance_criteria intentionally omitted
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )
            tasks = result.augmented_tasks

        assert len(tasks) == 1
        assert tasks[0].acceptance_criteria == []

    @pytest.mark.asyncio
    async def test_acceptance_criteria_malformed_elements_skipped(self):
        """
        LLM returns non-string elements in acceptance_criteria →
        they are silently skipped (defensive coercion).
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement GameEngine",
                    "description": "Build the engine",
                    "responsibility": "implements GameEngine",
                    "contract_file": "docs/api/types.ts",
                    "provides": "engine",
                    "requires": "None",
                    "estimated_minutes": 8,
                    "acceptance_criteria": [
                        "Valid criterion",
                        None,
                        "",
                        42,
                        "Another valid criterion",
                    ],
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )
            tasks = result.augmented_tasks

        assert len(tasks) == 1
        # None, empty string, and 42 are skipped. "42" coerces
        # to string "42" which is non-empty so it survives.
        criteria = tasks[0].acceptance_criteria
        assert "Valid criterion" in criteria
        assert "Another valid criterion" in criteria

    @pytest.mark.asyncio
    async def test_phase1_product_intent_threaded_into_source_context(self):
        """
        Phase 1 framing (GH-320): when the LLM response carries a
        product_intent field, it must be stored in source_context so
        build_tiered_instructions can surface it as the WHY THIS
        EXISTS section in the agent's task instructions.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement Weather",
                    "description": "Users see current weather on the dashboard",
                    "responsibility": "implements WeatherProvider",
                    "contract_file": "docs/api/weather.md",
                    "product_intent": (
                        "the user checks the weather before leaving the house"
                    ),
                    "provides": "weather data",
                    "requires": "None",
                    "estimated_minutes": 10,
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )
            tasks = result.augmented_tasks

        task = tasks[0]
        assert task.source_context is not None
        assert task.source_context["product_intent"] == (
            "the user checks the weather before leaving the house"
        )
        # Also embedded in the description marker for providers that
        # only round-trip description.
        assert "product_intent:" in task.description
        assert "checks the weather before leaving" in task.description

    @pytest.mark.asyncio
    async def test_phase1_product_intent_missing_defaults_to_empty(self):
        """
        Legacy LLM responses without product_intent must not break
        decomposition — the field defaults to an empty string, and
        downstream the instructions layer skips the WHY THIS EXISTS
        section when empty.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement Widget",
                    "description": "Build the widget",
                    "responsibility": "implements Widget",
                    "contract_file": "docs/api/widget.md",
                    # No product_intent field
                    "provides": "widget",
                    "requires": "None",
                    "estimated_minutes": 5,
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )
            tasks = result.augmented_tasks

        task = tasks[0]
        assert task.source_context is not None
        assert task.source_context["product_intent"] == ""
        # Empty intent is NOT embedded in the marker (keeps legacy
        # marker format terse).
        assert "product_intent:" not in task.description


class TestDecomposerOverFragmentationGuard:
    """The over-fragmentation guard (snake-decomposer-1 regression fix).

    The contract-decomposition prompt instructs: "Produce exactly one
    task per contract boundary." On 2026-05-26, with snake-decomposer-1,
    the LLM produced 5 tasks for 2 contracts — three of them claimed
    to own the same ``game-core-engine`` contract under "Integrate X"
    / "Initialize Y" framings. Those tasks needed to write to the same
    entry-point file in parallel, merge-conflicted, and BLOCKED the
    whole run.

    The guard enforces the structural invariant the prompt already
    states. ``decompose_by_contract`` dedupes the LLM's task list by
    ``contract_file``, keeping the first task per contract. Tasks
    that violate the rule are dropped with a WARN log so the failure
    surfaces during diagnostics.
    """

    @pytest.fixture(autouse=True)
    def _no_spec_coverage(self) -> Any:
        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock:
            yield mock

    @pytest.mark.asyncio
    async def test_dedupes_extra_tasks_claiming_same_contract(self):
        """Replays the snake-decomposer-1 failure shape.

        LLM emits 5 tasks for 2 contracts. The first task per contract
        wins; the 3 duplicates that claim the same contract_file as an
        earlier task are dropped.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement Game Engine",
                    "description": "Build the engine",
                    "responsibility": "implements Game Engine",
                    "contract_file": "docs/api/types.ts",
                    "provides": "engine",
                    "requires": "None",
                    "estimated_minutes": 10,
                },
                {
                    "name": "Implement Game UI",
                    "description": "Build the UI",
                    "responsibility": "implements UI",
                    "contract_file": "docs/specifications/ui-contract.md",
                    "provides": "ui",
                    "requires": "engine",
                    "estimated_minutes": 8,
                },
                # Duplicate — claims the engine contract again.
                {
                    "name": "Integrate Input Handling with Engine",
                    "description": "Wire input",
                    "responsibility": "wires Game Engine",
                    "contract_file": "docs/api/types.ts",
                    "provides": "input wiring",
                    "requires": "engine",
                    "estimated_minutes": 5,
                },
                # Duplicate — claims the engine contract a THIRD time.
                {
                    "name": "Initialize Game and Wire Startup",
                    "description": "Boot the game",
                    "responsibility": "wires Game Engine",
                    "contract_file": "docs/api/types.ts",
                    "provides": "startup wiring",
                    "requires": "engine",
                    "estimated_minutes": 5,
                },
                # Duplicate — claims the UI contract a SECOND time.
                {
                    "name": "Integrate Game Loop with UI",
                    "description": "Wire game loop",
                    "responsibility": "wires UI",
                    "contract_file": "docs/specifications/ui-contract.md",
                    "provides": "loop wiring",
                    "requires": "engine, ui",
                    "estimated_minutes": 5,
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )

        # Only 2 tasks survive — one per contract.
        tasks = [t for t in result.augmented_tasks if t.source_type == "contract_first"]
        assert (
            len(tasks) == 2
        ), f"Expected 2 tasks (one per contract), got {len(tasks)}: {[t.name for t in tasks]}"

        # First task per contract wins.
        task_names = [t.name for t in tasks]
        assert "Implement Game Engine" in task_names
        assert "Implement Game UI" in task_names

        # The dropped "Integrate"/"Initialize" tasks didn't make it.
        for dropped in (
            "Integrate Input Handling with Engine",
            "Initialize Game and Wire Startup",
            "Integrate Game Loop with UI",
        ):
            assert dropped not in task_names

    @pytest.mark.asyncio
    async def test_guard_inactive_when_count_matches(self):
        """No dedupe runs when the LLM obeys the one-task-per-contract rule.

        Defensive: the guard should be a true no-op for the common
        case so it never silently affects compliant LLM responses.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement Game Engine",
                    "description": "Build the engine",
                    "responsibility": "implements Game Engine",
                    "contract_file": "docs/api/types.ts",
                    "provides": "engine",
                    "requires": "None",
                    "estimated_minutes": 10,
                },
                {
                    "name": "Implement Game UI",
                    "description": "Build the UI",
                    "responsibility": "implements UI",
                    "contract_file": "docs/specifications/ui-contract.md",
                    "provides": "ui",
                    "requires": "engine",
                    "estimated_minutes": 8,
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )

        tasks = [t for t in result.augmented_tasks if t.source_type == "contract_first"]
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_drops_task_with_missing_contract_file(self):
        """A task without ``contract_file`` violates the one-task-per-contract rule.

        The guard only fires when ``len(raw_tasks) > len(contracts)``,
        so we trigger it by adding an extra contract-less task on top
        of compliant per-contract tasks.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement Game Engine",
                    "description": "Build the engine",
                    "responsibility": "implements Game Engine",
                    "contract_file": "docs/api/types.ts",
                    "provides": "engine",
                    "requires": "None",
                    "estimated_minutes": 10,
                },
                {
                    "name": "Implement Game UI",
                    "description": "Build the UI",
                    "responsibility": "implements UI",
                    "contract_file": "docs/specifications/ui-contract.md",
                    "provides": "ui",
                    "requires": "engine",
                    "estimated_minutes": 8,
                },
                # Third task triggers the guard. No contract_file →
                # dropped.
                {
                    "name": "Cross-cutting Setup",
                    "description": "Some setup work",
                    "responsibility": "general setup",
                    "contract_file": "",
                    "provides": "setup",
                    "requires": "None",
                    "estimated_minutes": 3,
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )

        task_names = [
            t.name for t in result.augmented_tasks if t.source_type == "contract_first"
        ]
        assert "Cross-cutting Setup" not in task_names
        # Two compliant tasks survive.
        assert len(task_names) == 2

    @pytest.mark.asyncio
    async def test_dedupes_across_artifact_types_of_same_domain(self):
        """snake-663-658 (2026-05-27) regression replay.

        Tighter LLM circumvention than the snake-decomposer-1 case:
        instead of repeating the same ``contract_file`` string, the
        LLM picks different artifact TYPES of the same domain. All
        three contract_files are distinct strings, but two of them
        name the SAME domain (``game-core-engine``) via different
        artifact types:

          ...game-core-engine-interface-contracts.md
          ...game-presentation-and-feedback-interface-contracts.md
          ...game-core-engine-architecture.md             ← same domain!

        Raw-string dedupe misses this because the filenames differ.
        Domain-slug dedupe catches it because the slug
        ``game-core-engine`` appears in two filenames.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        llm_response = {
            "tasks": [
                {
                    "name": "Implement Game Core Engine",
                    "description": "Build the engine",
                    "responsibility": "implements Game Core Engine",
                    "contract_file": "docs/specifications/game-core-engine-interface-contracts.md",
                    "provides": "engine",
                    "requires": "None",
                    "estimated_minutes": 10,
                },
                {
                    "name": "Implement Game UI",
                    "description": "Build the UI",
                    "responsibility": "implements UI",
                    "contract_file": "docs/specifications/game-presentation-and-feedback-interface-contracts.md",
                    "provides": "ui",
                    "requires": "engine",
                    "estimated_minutes": 8,
                },
                # The circumvention: different artifact type, SAME
                # domain. Old guard saw a unique string and let it
                # through. New guard sees the shared domain slug
                # ``game-core-engine`` and drops it.
                {
                    "name": "Integrate Game Core Engine and Presentation Layer",
                    "description": "Wire engine and UI",
                    "responsibility": "wires Game Core Engine",
                    "contract_file": "docs/architecture/game-core-engine-architecture.md",
                    "provides": "wiring",
                    "requires": "engine, ui",
                    "estimated_minutes": 5,
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )

        # Only the two distinct domains survive.
        tasks = [t for t in result.augmented_tasks if t.source_type == "contract_first"]
        task_names = [t.name for t in tasks]
        assert len(tasks) == 2, (
            f"Expected 2 tasks (one per domain), got {len(tasks)}: " f"{task_names}"
        )
        assert "Implement Game Core Engine" in task_names
        assert "Implement Game UI" in task_names
        # The artifact-type-variant duplicate is gone.
        assert "Integrate Game Core Engine and Presentation Layer" not in task_names

    @pytest.mark.asyncio
    async def test_dedupe_recognizes_all_four_artifact_types(self):
        """All four artifact-type suffixes collapse to the same domain.

        ``_DESIGN_ARTIFACT_SPECS`` defines four artifact types:
        architecture, api-contracts, data-models, interface-contracts.
        The domain-slug extractor must recognize all four suffixes so
        no circumvention shape can slip through.
        """
        parser = _make_parser()
        prd_analysis = _make_prd_analysis()
        contracts = _make_contract_artifacts()

        # All four artifact types of "game-engine" + one task that
        # legitimately belongs to "game-ui" (distinct domain).
        llm_response = {
            "tasks": [
                {
                    "name": "Implement Engine Interface",
                    "description": "Interface impl",
                    "responsibility": "engine interface",
                    "contract_file": "docs/specifications/game-engine-interface-contracts.md",
                    "estimated_minutes": 10,
                    "provides": "x",
                    "requires": "None",
                },
                {
                    "name": "Implement Engine API",
                    "description": "API impl",
                    "responsibility": "engine api",
                    "contract_file": "docs/api/game-engine-api-contracts.md",
                    "estimated_minutes": 8,
                    "provides": "x",
                    "requires": "x",
                },
                {
                    "name": "Implement Engine Data Models",
                    "description": "Data models impl",
                    "responsibility": "engine data",
                    "contract_file": "docs/specifications/game-engine-data-models.md",
                    "estimated_minutes": 5,
                    "provides": "x",
                    "requires": "x",
                },
                {
                    "name": "Implement Engine Architecture",
                    "description": "Architecture impl",
                    "responsibility": "engine architecture",
                    "contract_file": "docs/architecture/game-engine-architecture.md",
                    "estimated_minutes": 5,
                    "provides": "x",
                    "requires": "x",
                },
                {
                    "name": "Implement Game UI",
                    "description": "UI impl",
                    "responsibility": "ui",
                    "contract_file": "docs/specifications/game-ui-interface-contracts.md",
                    "estimated_minutes": 8,
                    "provides": "ui",
                    "requires": "engine",
                },
            ]
        }

        with patch(
            "src.integrations.ai_analysis_engine.AIAnalysisEngine."
            "generate_structured_response",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            result = await parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contracts,
            )

        tasks = [t for t in result.augmented_tasks if t.source_type == "contract_first"]
        # Only TWO tasks — the four "engine" artifacts collapse
        # to one domain, plus the one "game-ui" task.
        assert len(tasks) == 2
        names = {t.name for t in tasks}
        # First engine task survives (FIFO).
        assert "Implement Engine Interface" in names
        assert "Implement Game UI" in names


class TestExtractContractDomainSlug:
    """``_extract_contract_domain_slug`` strips artifact-type suffixes.

    The four artifact types defined in ``_DESIGN_ARTIFACT_SPECS``
    must all collapse to the bare domain slug. This is what makes
    the over-fragmentation guard immune to artifact-type
    circumvention.
    """

    def test_interface_contracts_suffix(self) -> None:
        from src.ai.advanced.prd.advanced_parser import (
            _extract_contract_domain_slug,
        )

        assert (
            _extract_contract_domain_slug(
                "docs/specifications/game-core-engine-interface-contracts.md"
            )
            == "game-core-engine"
        )

    def test_api_contracts_suffix(self) -> None:
        from src.ai.advanced.prd.advanced_parser import (
            _extract_contract_domain_slug,
        )

        assert (
            _extract_contract_domain_slug("docs/api/game-core-engine-api-contracts.md")
            == "game-core-engine"
        )

    def test_data_models_suffix(self) -> None:
        from src.ai.advanced.prd.advanced_parser import (
            _extract_contract_domain_slug,
        )

        assert (
            _extract_contract_domain_slug(
                "docs/specifications/game-core-engine-data-models.md"
            )
            == "game-core-engine"
        )

    def test_architecture_suffix(self) -> None:
        from src.ai.advanced.prd.advanced_parser import (
            _extract_contract_domain_slug,
        )

        assert (
            _extract_contract_domain_slug(
                "docs/architecture/game-core-engine-architecture.md"
            )
            == "game-core-engine"
        )

    def test_multi_word_domain_slug_preserved(self) -> None:
        """Domain slugs with multiple words (joined by ``-``) survive intact."""
        from src.ai.advanced.prd.advanced_parser import (
            _extract_contract_domain_slug,
        )

        assert (
            _extract_contract_domain_slug(
                "docs/specifications/game-presentation-and-feedback-interface-contracts.md"
            )
            == "game-presentation-and-feedback"
        )

    def test_unknown_suffix_returns_bare_stem(self) -> None:
        """Unknown artifact-type suffix → fall back to the bare stem.

        Defensive against future drift in ``_DESIGN_ARTIFACT_SPECS``.
        Two unknown-suffix paths with the same stem still dedupe;
        two with different stems don't (degrades to the pre-domain-
        slug guard rather than silently passing).
        """
        from src.ai.advanced.prd.advanced_parser import (
            _extract_contract_domain_slug,
        )

        result = _extract_contract_domain_slug("docs/random/widget-mystery-suffix.md")
        assert result == "widget-mystery-suffix"

    def test_empty_input_returns_empty(self) -> None:
        from src.ai.advanced.prd.advanced_parser import (
            _extract_contract_domain_slug,
        )

        assert _extract_contract_domain_slug("") == ""
