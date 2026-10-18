"""Unit tests for the user-outcome coverage check (issue #449).

Verifies that:

* :func:`compute_coverage` maps outcomes to the tasks that address them
* :func:`find_gaps` returns only in-scope outcomes with no covering task
* :func:`compute_intent_fidelity_score` reports the right ratio
* :func:`fill_gaps` issues a single LLM call to generate replacement tasks

These tests do not exercise the decomposer end-to-end; integration of the
coverage layer with :mod:`src.marcus_mcp.coordinator.decomposer` is covered
by tests in ``tests/unit/coordinator/test_decomposer_outcome_integration.py``
(future).
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.ai.advanced.prd.outcome_extractor import UserOutcome
from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.coordinator.outcome_coverage import (
    SIGNAL_CRITERION_PREFIX,
    STUB_TASK_ID_PREFIX,
    OutcomeCoverageResult,
    _build_recoverage_description,
    _enrich_acceptance_criteria_with_signals,
    _normalize_gap_task_name,
    apply_outcome_coverage,
    compute_coverage,
    compute_coverage_with_llm,
    compute_intent_fidelity_score,
    fill_gaps,
    find_gaps,
    keyword_overlap_mapper,
)

pytestmark = pytest.mark.unit


def _task(task_id: str, name: str, description: str = "") -> Task:
    """Build a minimally-populated :class:`Task` for coverage tests."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description=description,
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
    )


def _outcome(
    out_id: str,
    action: str,
    signal: str,
    scope: str = "in_scope",
) -> UserOutcome:
    return UserOutcome(id=out_id, action=action, success_signal=signal, scope=scope)


class TestComputeCoverage:
    """Coverage maps every outcome (by id) to the task ids that address it.

    All tests pass an explicit mapper.  ``compute_coverage`` deliberately
    has no default mapper — the only sync mapper available
    (:func:`keyword_overlap_mapper`) produces false positives on the
    snake_game-v31 case this whole module exists to catch.  Production
    code should use :func:`compute_coverage_with_llm`.
    """

    def test_keyword_overlap_marks_task_as_covering(self) -> None:
        """keyword_overlap_mapper covers outcome via overlapping nouns."""
        outcomes = [
            _outcome(
                "outcome_play",
                "user can play the snake game",
                "snake moves on a board, food appears, score updates",
            )
        ]
        tasks = [
            _task(
                "t1",
                "Render snake game to canvas",
                "Draw the snake, food, and score on a canvas element.",
            ),
        ]
        coverage = compute_coverage(outcomes, tasks, mapper=keyword_overlap_mapper)
        assert coverage["outcome_play"] == ["t1"]

    def test_no_overlap_yields_empty_coverage(self) -> None:
        """When no task addresses an outcome, the list is empty."""
        outcomes = [
            _outcome(
                "outcome_play",
                "user can play the snake game",
                "snake moves on a board, food appears",
            )
        ]
        tasks = [
            _task(
                "t1", "Build authentication service", "JWT tokens and password hashing"
            ),
        ]
        coverage = compute_coverage(outcomes, tasks, mapper=keyword_overlap_mapper)
        assert coverage["outcome_play"] == []

    def test_multiple_tasks_can_cover_one_outcome(self) -> None:
        """An outcome with broad scope may map to several tasks."""
        outcomes = [
            _outcome(
                "outcome_play", "user can play the snake game", "snake moves on a board"
            )
        ]
        tasks = [
            _task("t1", "Render snake on canvas", "draw snake body"),
            _task("t2", "Snake state machine", "moves snake each tick"),
        ]
        coverage = compute_coverage(outcomes, tasks, mapper=keyword_overlap_mapper)
        assert set(coverage["outcome_play"]) == {"t1", "t2"}

    def test_explicit_mapper_overrides_keyword_default(self) -> None:
        """Tests can inject custom mappers besides keyword_overlap_mapper."""
        outcomes = [_outcome("outcome_x", "user can do X", "X is observable")]
        tasks = [_task("t1", "totally unrelated", "nothing matches")]

        def always_match(_outcome: UserOutcome, _task: Task) -> bool:
            return True

        coverage = compute_coverage(outcomes, tasks, mapper=always_match)
        assert coverage["outcome_x"] == ["t1"]

    def test_every_outcome_appears_in_coverage_even_with_zero_tasks(self) -> None:
        """An empty task list produces a coverage dict with empty lists."""
        outcomes = [
            _outcome("o1", "user can do X", "X observable"),
            _outcome("o2", "user can do Y", "Y observable"),
        ]
        coverage = compute_coverage(outcomes, [], mapper=keyword_overlap_mapper)
        assert coverage == {"o1": [], "o2": []}

    def test_mapper_argument_is_required(self) -> None:
        """compute_coverage refuses to run without an explicit mapper.

        This is the footgun guard: the only sync mapper available is
        keyword_overlap_mapper, which produces false positives on the
        snake_game-v31 case.  A contributor importing compute_coverage
        without thinking about which mapper to use would silently
        re-introduce the v31 regression.  Force the choice.
        """
        outcomes = [_outcome("o1", "user can do X", "X observable")]
        tasks = [_task("t1", "task one", "")]
        with pytest.raises(TypeError):
            compute_coverage(outcomes, tasks)  # type: ignore[call-arg]


class TestComputeCoverageWithLLM:
    """LLM-backed coverage: one call maps all outcomes to all tasks."""

    @pytest.fixture
    def llm_returning(self) -> Any:
        def _build(payload: str) -> AsyncMock:
            mock = AsyncMock()
            mock.analyze = AsyncMock(return_value=payload)
            return mock

        return _build

    @pytest.mark.asyncio
    async def test_returns_coverage_dict_from_llm_response(
        self, llm_returning: Any
    ) -> None:
        outcomes = [
            _outcome("o_play", "user can play snake", "snake visibly moves"),
            _outcome("o_score", "user can see score", "score visible"),
        ]
        tasks = [
            _task("t_state", "Snake state machine", "track snake body"),
            _task("t_render", "Render snake to canvas", "draw snake"),
            _task("t_score", "Score display", "render score in DOM"),
        ]
        llm = llm_returning(
            '{"coverage": {' '"o_play": ["t_render"],' '"o_score": ["t_score"]' "}}"
        )

        coverage = await compute_coverage_with_llm(outcomes, tasks, llm)

        assert coverage == {"o_play": ["t_render"], "o_score": ["t_score"]}

    @pytest.mark.asyncio
    async def test_unknown_task_ids_in_response_are_dropped(
        self, llm_returning: Any
    ) -> None:
        """LLM hallucinations don't corrupt the coverage map."""
        outcomes = [_outcome("o1", "user can play", "snake moves")]
        tasks = [_task("t1", "real task", "real desc")]
        llm = llm_returning('{"coverage": {"o1": ["t1", "t_does_not_exist"]}}')

        coverage = await compute_coverage_with_llm(outcomes, tasks, llm)
        assert coverage == {"o1": ["t1"]}

    @pytest.mark.asyncio
    async def test_unknown_outcome_ids_in_response_are_dropped(
        self, llm_returning: Any
    ) -> None:
        outcomes = [_outcome("o1", "user can play", "moves")]
        tasks = [_task("t1", "render", "draw")]
        llm = llm_returning('{"coverage": {"o1": ["t1"], "o_phantom": ["t1"]}}')

        coverage = await compute_coverage_with_llm(outcomes, tasks, llm)
        assert "o_phantom" not in coverage
        assert coverage == {"o1": ["t1"]}

    @pytest.mark.asyncio
    async def test_missing_outcome_in_response_filled_empty(
        self, llm_returning: Any
    ) -> None:
        """LLM that forgets an outcome gets the empty-list default.

        This mirrors find_gaps' contract — every outcome must have an
        entry, even if the LLM skipped it.  Treating "skipped" as
        "uncovered" surfaces the gap; treating it as "covered" would
        hide it.
        """
        outcomes = [
            _outcome("o1", "user can play", "moves"),
            _outcome("o2", "user can score", "visible"),
        ]
        tasks = [_task("t1", "render", "draw")]
        llm = llm_returning('{"coverage": {"o1": ["t1"]}}')

        coverage = await compute_coverage_with_llm(outcomes, tasks, llm)
        assert coverage == {"o1": ["t1"], "o2": []}

    @pytest.mark.asyncio
    async def test_malformed_json_raises(self, llm_returning: Any) -> None:
        outcomes = [_outcome("o1", "user can play", "moves")]
        tasks = [_task("t1", "render", "draw")]
        llm = llm_returning("not json")
        with pytest.raises(ValueError, match="malformed JSON"):
            await compute_coverage_with_llm(outcomes, tasks, llm)

    @pytest.mark.asyncio
    async def test_missing_coverage_key_raises(self, llm_returning: Any) -> None:
        outcomes = [_outcome("o1", "user can play", "moves")]
        tasks = [_task("t1", "render", "draw")]
        llm = llm_returning('{"wrong_key": {}}')
        with pytest.raises(ValueError, match="coverage"):
            await compute_coverage_with_llm(outcomes, tasks, llm)

    @pytest.mark.asyncio
    async def test_no_tasks_returns_empty_lists_without_llm_call(
        self, llm_returning: Any
    ) -> None:
        """When the task graph is empty, no LLM call fires."""
        outcomes = [_outcome("o1", "user can play", "moves")]
        llm = llm_returning('{"coverage": {}}')
        coverage = await compute_coverage_with_llm(outcomes, [], llm)
        assert coverage == {"o1": []}
        llm.analyze.assert_not_called()


class TestFindGaps:
    """Gaps are in-scope outcomes that have no covering tasks."""

    def test_in_scope_uncovered_outcomes_are_gaps(self) -> None:
        outcomes = [
            _outcome("o1", "user can play snake", "snake moves"),
            _outcome("o2", "user can see score", "score visible"),
        ]
        coverage = {"o1": ["t1"], "o2": []}
        gaps = find_gaps(outcomes, coverage)
        assert [g.id for g in gaps] == ["o2"]

    def test_out_of_scope_outcomes_never_count_as_gaps(self) -> None:
        """Out-of-scope outcomes are tracked for audit but excluded from gaps."""
        outcomes = [
            _outcome("o1", "user can log in", "session set", scope="out_of_scope"),
        ]
        coverage = {"o1": []}
        gaps = find_gaps(outcomes, coverage)
        assert gaps == []

    def test_no_gaps_when_all_covered(self) -> None:
        outcomes = [_outcome("o1", "user can play", "moves")]
        coverage = {"o1": ["t1"]}
        assert find_gaps(outcomes, coverage) == []


class TestIntentFidelityScore:
    """Score is covered_in_scope / total_in_scope, in [0.0, 1.0]."""

    def test_full_coverage_scores_one(self) -> None:
        outcomes = [
            _outcome("o1", "user can play", "moves"),
            _outcome("o2", "user can score", "score visible"),
        ]
        coverage = {"o1": ["t1"], "o2": ["t2"]}
        assert compute_intent_fidelity_score(outcomes, coverage) == 1.0

    def test_partial_coverage(self) -> None:
        outcomes = [
            _outcome("o1", "user can play", "moves"),
            _outcome("o2", "user can score", "score visible"),
            _outcome("o3", "user can pause", "pause works"),
            _outcome("o4", "user can restart", "restart works"),
        ]
        coverage = {"o1": ["t1"], "o2": [], "o3": ["t3"], "o4": []}
        assert compute_intent_fidelity_score(outcomes, coverage) == 0.5

    def test_out_of_scope_excluded_from_denominator(self) -> None:
        """Out-of-scope outcomes do not count toward score."""
        outcomes = [
            _outcome("o1", "user can play", "moves"),
            _outcome("o2", "user can log in", "auth", scope="out_of_scope"),
        ]
        coverage = {"o1": ["t1"], "o2": []}
        # 1 in-scope outcome, 1 covered → 1.0
        assert compute_intent_fidelity_score(outcomes, coverage) == 1.0

    def test_no_in_scope_outcomes_yields_one(self) -> None:
        """Empty denominator → vacuously satisfied (score = 1.0).

        The alternative (NaN, 0.0, raise) all surprise consumers.  Since
        there are no in-scope outcomes to fail, treat it as full fidelity.
        """
        outcomes = [
            _outcome("o1", "user can x", "x", scope="out_of_scope"),
        ]
        coverage = {"o1": []}
        assert compute_intent_fidelity_score(outcomes, coverage) == 1.0


class TestFillGaps:
    """Gap-fill issues a single LLM call returning task dicts.

    All tests pass ``existing_tasks`` (required) so the LLM has the
    real task graph to ground its ``requires`` references against.
    Contract-aware tests pass ``contract_artifacts`` as well; the
    feature-based tests omit it.
    """

    @pytest.fixture
    def llm_returning(self) -> Any:
        def _build(payload: str) -> AsyncMock:
            mock = AsyncMock()
            mock.analyze = AsyncMock(return_value=payload)
            return mock

        return _build

    @pytest.mark.asyncio
    async def test_returns_task_dicts_for_each_gap(self, llm_returning: Any) -> None:
        """LLM returns a list of task dicts; one per gap (or more)."""
        gaps = [
            _outcome(
                "outcome_render",
                "user can play the snake game",
                "snake visible on canvas",
            )
        ]
        llm = llm_returning(
            '{"tasks": ['
            '{"name": "Render snake to canvas",'
            ' "description": "Subscribe to game-state events and draw '
            'snake/food/score on a canvas element"}'
            "]}"
        )
        new_tasks = await fill_gaps(
            spec="build a snake game",
            gaps=gaps,
            existing_tasks=[_task("t1", "Snake state machine")],
            llm_client=llm,
        )
        assert len(new_tasks) == 1
        assert new_tasks[0]["name"] == "Render snake to canvas"

    @pytest.mark.asyncio
    async def test_empty_gaps_returns_empty_without_llm_call(
        self, llm_returning: Any
    ) -> None:
        """No gaps means no LLM call — saves cost when coverage is full."""
        llm = llm_returning('{"tasks": []}')
        new_tasks = await fill_gaps(
            spec="anything",
            gaps=[],
            existing_tasks=[_task("t1", "task one")],
            llm_client=llm,
        )
        assert new_tasks == []
        llm.analyze.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_response_raises(self, llm_returning: Any) -> None:
        gaps = [_outcome("o1", "user can do X", "X")]
        llm = llm_returning("not json")
        with pytest.raises(ValueError, match="JSON"):
            await fill_gaps(spec="x", gaps=gaps, existing_tasks=[], llm_client=llm)

    @pytest.mark.asyncio
    async def test_each_returned_task_has_name_and_description(
        self, llm_returning: Any
    ) -> None:
        """A task dict missing required fields is rejected, not silently kept."""
        gaps = [_outcome("o1", "user can do X", "X observable")]
        llm = llm_returning('{"tasks": [{"name": "no description"}]}')
        with pytest.raises(ValueError, match="description"):
            await fill_gaps(spec="x", gaps=gaps, existing_tasks=[], llm_client=llm)

    @pytest.mark.asyncio
    async def test_null_name_field_is_rejected(self, llm_returning: Any) -> None:
        """Null name must raise — must not coerce 'None' through validation."""
        gaps = [_outcome("o1", "user can do X", "X observable")]
        llm = llm_returning(
            '{"tasks": [{"name": null, "description": "valid description"}]}'
        )
        with pytest.raises(ValueError, match=r"'name'.*string"):
            await fill_gaps(spec="x", gaps=gaps, existing_tasks=[], llm_client=llm)

    @pytest.mark.asyncio
    async def test_null_description_field_is_rejected(self, llm_returning: Any) -> None:
        """Null description must raise."""
        gaps = [_outcome("o1", "user can do X", "X observable")]
        llm = llm_returning('{"tasks": [{"name": "valid name", "description": null}]}')
        with pytest.raises(ValueError, match=r"'description'.*string"):
            await fill_gaps(spec="x", gaps=gaps, existing_tasks=[], llm_client=llm)

    @pytest.mark.asyncio
    async def test_non_string_name_field_is_rejected(self, llm_returning: Any) -> None:
        """An integer where a string is expected raises (no silent coercion)."""
        gaps = [_outcome("o1", "user can do X", "X observable")]
        llm = llm_returning(
            '{"tasks": [{"name": 42, "description": "valid description"}]}'
        )
        with pytest.raises(ValueError, match=r"'name'.*string"):
            await fill_gaps(spec="x", gaps=gaps, existing_tasks=[], llm_client=llm)

    # ----- Provides / requires output fields -----

    @pytest.mark.asyncio
    async def test_provides_and_requires_emitted_when_supplied(
        self, llm_returning: Any
    ) -> None:
        """Optional contract fields are surfaced verbatim in the output dict."""
        gaps = [_outcome("o1", "user can play snake", "snake visibly moves")]
        llm = llm_returning(
            '{"tasks": [{'
            '"name": "Render snake to canvas",'
            '"description": "Draw snake/food/score on canvas",'
            '"provides": "RenderingAgent.draw",'
            '"requires": "GameStateUpdate"'
            "}]}"
        )
        new_tasks = await fill_gaps(
            spec="build snake",
            gaps=gaps,
            existing_tasks=[_task("t1", "Engine", "produces GameStateUpdate")],
            llm_client=llm,
        )
        assert new_tasks[0]["provides"] == "RenderingAgent.draw"
        assert new_tasks[0]["requires"] == "GameStateUpdate"

    @pytest.mark.asyncio
    async def test_provides_and_requires_default_to_none(
        self, llm_returning: Any
    ) -> None:
        """Tasks the LLM omits contract fields for get None defaults."""
        gaps = [_outcome("o1", "user can do X", "X observable")]
        llm = llm_returning(
            '{"tasks": [{"name": "Standalone", "description": "no contract"}]}'
        )
        new_tasks = await fill_gaps(
            spec="x", gaps=gaps, existing_tasks=[], llm_client=llm
        )
        assert new_tasks[0]["provides"] is None
        assert new_tasks[0]["requires"] is None

    @pytest.mark.asyncio
    async def test_non_string_optional_field_coerced_not_rejected(
        self, llm_returning: Any
    ) -> None:
        """A list/int for an optional contract field coerces to None, not a raise.

        The LLM commonly returns ``requires`` as a LIST of upstream task
        ids. A hard raise here was stricter than the downstream handling
        (which coerces non-strings to None anyway) and silently no-opped
        the ENTIRE outcome-coverage pass — gap-fill, signal enrichment,
        and #680 gotcha enumeration — for the whole project. So a
        malformed optional field must degrade gracefully, not abort.
        """
        gaps = [_outcome("o1", "user can do X", "X observable")]
        llm = llm_returning(
            '{"tasks": [{'
            '"name": "X", "description": "Y", '
            '"provides": ["bad"], "requires": ["a", "b"]'
            "}]}"
        )
        new_tasks = await fill_gaps(
            spec="x", gaps=gaps, existing_tasks=[], llm_client=llm
        )
        assert new_tasks[0]["provides"] is None
        assert new_tasks[0]["requires"] is None

    # ----- Existing task graph as prompt context -----

    @pytest.mark.asyncio
    async def test_existing_task_names_appear_in_prompt(
        self, llm_returning: Any
    ) -> None:
        """LLM sees existing tasks so requires references can be grounded.

        Without this context, the LLM's ``requires`` strings become
        invented labels disconnected from the real task graph.
        """
        llm = llm_returning('{"tasks": []}')
        await fill_gaps(
            spec="build snake",
            gaps=[_outcome("o1", "user can play", "snake moves")],
            existing_tasks=[
                _task("t_engine", "Game Engine", "tracks snake body"),
                _task("t_input", "Input Handler", "reads keyboard"),
            ],
            llm_client=llm,
        )
        # safe_structured_call passes the prompt as a keyword argument.
        prompt = llm.analyze.call_args.kwargs["prompt"]
        assert "t_engine" in prompt
        assert "Game Engine" in prompt
        assert "t_input" in prompt

    # ----- Contract artifacts: contract-aware path -----

    @pytest.mark.asyncio
    async def test_contract_artifacts_appear_in_prompt(
        self, llm_returning: Any
    ) -> None:
        """When contracts are passed, their content appears in the prompt.

        Without this, the gap-fill LLM emits ungrounded contract names
        (the failure mode that scrapped PR #454).
        """
        llm = llm_returning('{"tasks": []}')
        await fill_gaps(
            spec="build snake",
            gaps=[_outcome("o1", "user can play", "snake moves")],
            existing_tasks=[],
            llm_client=llm,
            contract_artifacts={
                "game_engine": {
                    "artifacts": [
                        {
                            "filename": "GameState.ts",
                            "relative_path": "src/contracts/GameState.ts",
                            "content": (
                                "interface GameStateUpdate " "{ snake: Position[] }"
                            ),
                        }
                    ]
                }
            },
        )
        prompt = llm.analyze.call_args.kwargs["prompt"]
        assert "GameStateUpdate" in prompt
        assert "GameState.ts" in prompt
        # Schema variant with responsibility is selected when contracts present
        assert "responsibility" in prompt

    @pytest.mark.asyncio
    async def test_no_contracts_omits_contract_section(
        self, llm_returning: Any
    ) -> None:
        """Feature-based path: no contract section, no responsibility field."""
        llm = llm_returning('{"tasks": []}')
        await fill_gaps(
            spec="x",
            gaps=[_outcome("o1", "user can do X", "X observable")],
            existing_tasks=[],
            llm_client=llm,
            contract_artifacts=None,
        )
        prompt = llm.analyze.call_args.kwargs["prompt"]
        # No-contract schema explicitly does NOT mention responsibility
        assert "responsibility" not in prompt
        # And the contract-section header does not appear
        assert "Existing contract artifacts" not in prompt

    @pytest.mark.asyncio
    async def test_responsibility_emitted_when_contracts_present(
        self, llm_returning: Any
    ) -> None:
        """Contract-aware gap-fill includes responsibility on output dicts."""
        llm = llm_returning(
            '{"tasks": [{'
            '"name": "Render snake to canvas",'
            '"description": "Draw on canvas",'
            '"provides": "RenderingAgent.draw",'
            '"requires": "GameStateUpdate",'
            '"responsibility": '
            '"implements RenderingAgent from src/contracts/Rendering.ts"'
            "}]}"
        )
        new_tasks = await fill_gaps(
            spec="x",
            gaps=[_outcome("o1", "user can play", "snake visible")],
            existing_tasks=[],
            llm_client=llm,
            contract_artifacts={
                "rendering": {
                    "artifacts": [
                        {
                            "filename": "Rendering.ts",
                            "relative_path": "src/contracts/Rendering.ts",
                            "content": "interface RenderingAgent { draw(state) }",
                        }
                    ]
                }
            },
        )
        assert new_tasks[0]["responsibility"] == (
            "implements RenderingAgent from src/contracts/Rendering.ts"
        )

    @pytest.mark.asyncio
    async def test_responsibility_omitted_when_contracts_absent(
        self, llm_returning: Any
    ) -> None:
        """Feature-based gap-fill output dicts have no responsibility key.

        The dict shape is intentionally narrower in the feature-based
        path so callers downstream don't get false signal that a
        responsibility is set.
        """
        llm = llm_returning(
            '{"tasks": [{'
            '"name": "Standalone",'
            '"description": "no contract",'
            '"responsibility": "would be ignored anyway"'
            "}]}"
        )
        new_tasks = await fill_gaps(
            spec="x",
            gaps=[_outcome("o1", "user can do X", "X observable")],
            existing_tasks=[],
            llm_client=llm,
            contract_artifacts=None,
        )
        assert "responsibility" not in new_tasks[0]

    @pytest.mark.asyncio
    async def test_non_string_responsibility_coerced_not_rejected(
        self, llm_returning: Any
    ) -> None:
        """Non-string responsibility coerces to None (same as provides/requires).

        Tolerant degradation, not a hard raise — a malformed optional
        field must not abort the whole coverage pass (see
        ``test_non_string_optional_field_coerced_not_rejected``).
        """
        llm = llm_returning(
            '{"tasks": [{'
            '"name": "X", "description": "Y",'
            '"responsibility": 42'
            "}]}"
        )
        new_tasks = await fill_gaps(
            spec="x",
            gaps=[_outcome("o1", "user can do X", "X observable")],
            existing_tasks=[],
            llm_client=llm,
            contract_artifacts={"d": {"artifacts": []}},
        )
        assert new_tasks[0]["responsibility"] is None

    @pytest.mark.asyncio
    async def test_existing_tasks_is_required_kwarg(self, llm_returning: Any) -> None:
        """Calling without existing_tasks raises TypeError.

        Locks in the API contract — existing_tasks is required, not
        Optional.  Forces callers to think about which tasks the LLM
        should see when grounding requires references.
        """
        llm = llm_returning('{"tasks": []}')
        with pytest.raises(TypeError):
            await fill_gaps(  # type: ignore[call-arg]
                spec="x",
                gaps=[_outcome("o1", "user can do X", "X observable")],
                llm_client=llm,
            )


class TestApplyOutcomeCoverage:
    """End-to-end pipeline that both decomposers call internally.

    LLM call sequence:

    - 0 calls when ``outcomes`` is empty (vacuous full coverage)
    - 1 call when initial graph already covers everything
    - 3 calls when gaps exist: coverage check, gap-fill, recheck on
      augmented graph (the recheck verifies fill_gaps actually
      produced covering tasks rather than assuming success)
    """

    @staticmethod
    def _llm_with_responses(*responses: str) -> AsyncMock:
        """AsyncMock that returns each response in sequence."""
        mock = AsyncMock()
        mock.analyze = AsyncMock(side_effect=list(responses))
        return mock

    @pytest.mark.asyncio
    async def test_no_outcomes_returns_score_one_no_llm_calls(self) -> None:
        """Empty outcome list: vacuous full coverage, no LLM cost."""
        mock = AsyncMock()
        mock.analyze = AsyncMock()

        result = await apply_outcome_coverage(
            spec="anything",
            outcomes=[],
            tasks=[_task("t1", "Task 1")],
            llm_client=mock,
        )

        assert isinstance(result, OutcomeCoverageResult)
        assert result.synthesized_tasks == []
        assert result.intent_fidelity_score == 1.0
        assert result.gaps == []
        mock.analyze.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_coverage_one_llm_call(self) -> None:
        """Coverage check passes — no gap-fill, score is 1.0."""
        outcomes = [_outcome("o1", "user can play snake", "snake visibly moves")]
        tasks = [_task("t_render", "Render snake to canvas", "draw snake")]
        llm = self._llm_with_responses(
            '{"coverage": {"o1": ["t_render"]}}',
        )

        result = await apply_outcome_coverage(
            spec="snake game",
            outcomes=outcomes,
            tasks=tasks,
            llm_client=llm,
        )

        assert result.synthesized_tasks == []
        assert result.intent_fidelity_score == 1.0
        assert result.gaps == []
        assert result.coverage_before_fill == {"o1": ["t_render"]}
        assert llm.analyze.await_count == 1

    @pytest.mark.asyncio
    async def test_gaps_filled_three_llm_calls(self) -> None:
        """Initial gap → gap-fill → post-fill coverage all green."""
        outcomes = [_outcome("o1", "user can play snake", "snake visibly moves")]
        v31_tasks = [_task("t_state", "Snake state machine", "track body")]
        llm = self._llm_with_responses(
            # 1. Initial coverage: gap
            '{"coverage": {"o1": []}}',
            # 2. Gap-fill produces a render task
            (
                '{"tasks": [{'
                '"name": "Render snake to canvas",'
                '"description": "draw snake on canvas",'
                '"provides": "RenderingAgent",'
                '"requires": "GameStateUpdate"'
                "}]}"
            ),
            # 3. Post-fill coverage: now covered
            f'{{"coverage": {{"o1": ["{STUB_TASK_ID_PREFIX}0"]}}}}',
        )

        result = await apply_outcome_coverage(
            spec="snake game",
            outcomes=outcomes,
            tasks=v31_tasks,
            llm_client=llm,
        )

        assert len(result.synthesized_tasks) == 1
        assert result.synthesized_tasks[0]["name"] == "Render snake to canvas"
        assert result.synthesized_tasks[0]["provides"] == "RenderingAgent"
        assert result.synthesized_tasks[0]["requires"] == "GameStateUpdate"
        assert result.intent_fidelity_score == 1.0
        assert len(result.gaps) == 1
        assert result.gaps[0].id == "o1"
        assert result.coverage_before_fill == {"o1": []}
        assert llm.analyze.await_count == 3

    @pytest.mark.asyncio
    async def test_contract_artifacts_yields_responsibility_field(self) -> None:
        """Contract-first path: synthesized dicts carry responsibility."""
        outcomes = [_outcome("o1", "user can play snake", "snake visibly moves")]
        tasks = [_task("t_state", "Snake state machine", "track body")]
        llm = self._llm_with_responses(
            '{"coverage": {"o1": []}}',
            (
                '{"tasks": [{'
                '"name": "Render snake to canvas",'
                '"description": "draw snake on canvas",'
                '"provides": "RenderingAgent",'
                '"requires": "GameStateUpdate",'
                '"responsibility": '
                '"implements RenderingAgent from src/contracts/Render.ts"'
                "}]}"
            ),
            f'{{"coverage": {{"o1": ["{STUB_TASK_ID_PREFIX}0"]}}}}',
        )

        result = await apply_outcome_coverage(
            spec="snake game",
            outcomes=outcomes,
            tasks=tasks,
            llm_client=llm,
            contract_artifacts={
                "rendering": {
                    "artifacts": [
                        {
                            "filename": "Render.ts",
                            "relative_path": "src/contracts/Render.ts",
                            "content": "interface RenderingAgent { draw() }",
                        }
                    ]
                }
            },
        )

        assert "responsibility" in result.synthesized_tasks[0]
        assert result.synthesized_tasks[0]["responsibility"] == (
            "implements RenderingAgent from src/contracts/Render.ts"
        )

    @pytest.mark.asyncio
    async def test_no_contract_artifacts_omits_responsibility(self) -> None:
        """Feature-based path: synthesized dicts have no responsibility key."""
        outcomes = [_outcome("o1", "user can play snake", "snake visibly moves")]
        tasks = [_task("t_state", "Snake state machine", "track body")]
        llm = self._llm_with_responses(
            '{"coverage": {"o1": []}}',
            (
                '{"tasks": [{'
                '"name": "Render snake to canvas",'
                '"description": "draw snake on canvas"'
                "}]}"
            ),
            f'{{"coverage": {{"o1": ["{STUB_TASK_ID_PREFIX}0"]}}}}',
        )

        result = await apply_outcome_coverage(
            spec="snake game",
            outcomes=outcomes,
            tasks=tasks,
            llm_client=llm,
        )

        assert "responsibility" not in result.synthesized_tasks[0]

    @pytest.mark.asyncio
    async def test_score_reflects_post_fill_measured_coverage(self) -> None:
        """Gap-fill produced a task that doesn't actually cover → score < 1.

        This is the key reason we recheck coverage on the augmented
        graph instead of assuming gap-fill succeeded — fill_gaps is
        an LLM call that can produce off-target tasks.  The score must
        reflect MEASURED coverage, not assumed.
        """
        outcomes = [_outcome("o1", "user can play snake", "snake visibly moves")]
        tasks = [_task("t_state", "Snake state machine", "track body")]
        llm = self._llm_with_responses(
            '{"coverage": {"o1": []}}',
            (
                '{"tasks": [{'
                '"name": "Off-target task",'
                '"description": "does not address the outcome"'
                "}]}"
            ),
            # Post-fill coverage: still uncovered (LLM judged the
            # synthesized task didn't help)
            '{"coverage": {"o1": []}}',
        )

        result = await apply_outcome_coverage(
            spec="snake game",
            outcomes=outcomes,
            tasks=tasks,
            llm_client=llm,
        )

        assert len(result.synthesized_tasks) == 1
        assert result.intent_fidelity_score == 0.0

    @pytest.mark.asyncio
    async def test_out_of_scope_outcomes_dont_create_gaps(self) -> None:
        """Out-of-scope outcomes are excluded from the gap-fill input."""
        outcomes = [
            _outcome("o_play", "user can play", "moves"),
            _outcome("o_login", "user can log in", "auth", scope="out_of_scope"),
        ]
        tasks = [_task("t_render", "Render snake", "draw snake")]
        llm = self._llm_with_responses(
            # Only o_play is covered; o_login is uncovered but
            # out-of-scope, so it doesn't trigger gap-fill
            '{"coverage": {"o_play": ["t_render"], "o_login": []}}',
        )

        result = await apply_outcome_coverage(
            spec="snake game (no auth)",
            outcomes=outcomes,
            tasks=tasks,
            llm_client=llm,
        )

        assert result.gaps == []
        assert result.synthesized_tasks == []
        # Score: 1 in-scope outcome, 1 covered → 1.0
        assert result.intent_fidelity_score == 1.0
        assert llm.analyze.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_gap_fill_response_falls_back_to_pre_fill_score(
        self,
    ) -> None:
        """Gap-fill returning [] doesn't poison the score."""
        outcomes = [_outcome("o1", "user can play", "moves")]
        tasks = [_task("t_state", "state machine", "track")]
        llm = self._llm_with_responses(
            '{"coverage": {"o1": []}}',
            '{"tasks": []}',  # LLM returned no tasks
        )

        result = await apply_outcome_coverage(
            spec="snake game",
            outcomes=outcomes,
            tasks=tasks,
            llm_client=llm,
        )

        assert result.synthesized_tasks == []
        # No fill happened → score from coverage_before (still 0)
        assert result.intent_fidelity_score == 0.0
        # No third LLM call (no augmented graph to recheck)
        assert llm.analyze.await_count == 2

    @pytest.mark.asyncio
    async def test_coverage_failure_propagates_for_caller_to_handle(
        self,
    ) -> None:
        """Malformed LLM JSON raises; caller decides whether to suppress.

        Decomposers should catch ValueError and degrade to no-coverage
        rather than fail the project, but apply_outcome_coverage
        itself doesn't bake a degradation policy.  Each caller
        chooses.
        """
        outcomes = [_outcome("o1", "user can play", "moves")]
        tasks = [_task("t1", "task one")]
        # safe_structured_call retries on apparent truncation. "not json"
        # doesn't end with a closing brace so each attempt looks
        # truncated — feed enough responses to exhaust the retry path
        # (initial + max_retries=3 attempts = 4 total) so the test
        # reaches the final raise rather than running out of mocks.
        llm = self._llm_with_responses("not json", "not json", "not json", "not json")

        with pytest.raises(ValueError, match="malformed JSON"):
            await apply_outcome_coverage(
                spec="x",
                outcomes=outcomes,
                tasks=tasks,
                llm_client=llm,
            )

    @pytest.mark.asyncio
    async def test_coverage_after_fill_populated_when_gaps_filled(self) -> None:
        """coverage_after_fill mirrors coverage_before_fill for telemetry.

        Lets callers do diff analysis ("which gap-fill tasks ended up
        actually covering which outcomes?") without recomputing
        coverage themselves.
        """
        outcomes = [_outcome("o1", "user can play snake", "snake visibly moves")]
        tasks = [_task("t_state", "Snake state machine", "track body")]
        llm = self._llm_with_responses(
            '{"coverage": {"o1": []}}',
            (
                '{"tasks": [{'
                '"name": "Render snake to canvas",'
                '"description": "draw snake on canvas",'
                '"provides": "RenderingAgent"'
                "}]}"
            ),
            f'{{"coverage": {{"o1": ["{STUB_TASK_ID_PREFIX}0"]}}}}',
        )

        result = await apply_outcome_coverage(
            spec="snake game",
            outcomes=outcomes,
            tasks=tasks,
            llm_client=llm,
        )

        assert result.coverage_before_fill == {"o1": []}
        assert result.coverage_after_fill == {"o1": [f"{STUB_TASK_ID_PREFIX}0"]}

    @pytest.mark.asyncio
    async def test_coverage_after_fill_is_none_when_no_gaps(self) -> None:
        """No gap-fill ran → no augmented graph → coverage_after_fill is None.

        Distinguishes "we didn't recheck" from "we rechecked and got X."
        """
        outcomes = [_outcome("o1", "user can play snake", "snake visibly moves")]
        tasks = [_task("t_render", "Render snake to canvas", "draw snake")]
        llm = self._llm_with_responses(
            '{"coverage": {"o1": ["t_render"]}}',
        )

        result = await apply_outcome_coverage(
            spec="snake game",
            outcomes=outcomes,
            tasks=tasks,
            llm_client=llm,
        )

        assert result.coverage_after_fill is None
        assert result.coverage_before_fill == {"o1": ["t_render"]}


class TestStubTaskBuildingForRecoverage:
    """Stub tasks used internally for the post-fill coverage recheck.

    These three tests lock in invariants Kaia flagged as future-fragility
    risks:

    - Stub IDs use a public, named prefix so test mocks reference
      ``STUB_TASK_ID_PREFIX`` rather than hardcoding the literal.
    - Stub descriptions enrich the gap-fill output with contract
      metadata (provides / requires / responsibility) so the recheck
      LLM has full signal when scoring synthesized tasks.
    """

    def test_stub_id_prefix_is_publicly_exported(self) -> None:
        """The prefix is a module constant — convention is explicit."""
        assert STUB_TASK_ID_PREFIX == "_synth_for_coverage_"

    def test_recoverage_description_passthrough_when_no_contract(self) -> None:
        """No contract fields → description is unchanged."""
        gap_dict = {
            "name": "Standalone task",
            "description": "do the thing",
        }
        assert _build_recoverage_description(gap_dict) == "do the thing"

    def test_recoverage_description_appends_contract_section_when_provided(
        self,
    ) -> None:
        """Contract fields surface explicitly so the recheck LLM sees them."""
        gap_dict = {
            "name": "Render snake to canvas",
            "description": "draw snake on canvas",
            "provides": "RenderingAgent.draw",
            "requires": "GameStateUpdate",
            "responsibility": (
                "implements RenderingAgent from src/contracts/Render.ts"
            ),
        }
        rendered = _build_recoverage_description(gap_dict)
        assert "draw snake on canvas" in rendered
        assert "Contract:" in rendered
        assert "provides=RenderingAgent.draw" in rendered
        assert "requires=GameStateUpdate" in rendered
        assert "responsibility=implements RenderingAgent" in rendered

    def test_recoverage_description_handles_partial_contract_fields(
        self,
    ) -> None:
        """Only provides set → only provides surfaces (not null requires)."""
        gap_dict = {
            "name": "Producer",
            "description": "produce thing",
            "provides": "Thing",
            "requires": None,
        }
        rendered = _build_recoverage_description(gap_dict)
        assert "provides=Thing" in rendered
        # Null requires is omitted entirely (not rendered as "requires=None")
        assert "requires" not in rendered

    def test_recoverage_description_no_contract_section_when_all_fields_null(
        self,
    ) -> None:
        """All contract fields explicitly null → description is unchanged."""
        gap_dict = {
            "name": "Standalone",
            "description": "no contract",
            "provides": None,
            "requires": None,
            "responsibility": None,
        }
        assert _build_recoverage_description(gap_dict) == "no contract"


class TestCoverageToTelemetry:
    """``_coverage_to_telemetry`` flattens OutcomeCoverageResult into the
    canonical PLANNING_INTENT_FIDELITY event payload shape.

    Issue #456 Stage 5 follow-up (Kaia review #6, Simon ``1efc9406``):
    the four telemetry keys are load-bearing for Cato consumers — any
    drift breaks the wire.  An explicit unit test here makes the
    contract grep-able alongside the source, replacing the Stage 2
    ``TestTelemetryKeyPinning`` class that was retired with the
    wrapper rewrite.
    """

    def test_canonical_keys_pinned_exactly(self) -> None:
        """Telemetry has exactly four keys; no more, no less."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            OutcomeCoverageResult,
            _coverage_to_telemetry,
        )

        coverage = OutcomeCoverageResult(
            synthesized_tasks=[],
            intent_fidelity_score=0.75,
            coverage_before_fill={"o1": ["t1"]},
            coverage_after_fill={"o1": ["t1", "gap"]},
            gaps=[
                UserOutcome(
                    id="o2",
                    action="x",
                    success_signal="y",
                    scope="in_scope",
                )
            ],
        )
        telemetry = _coverage_to_telemetry(coverage)

        # The four canonical keys, no extras (PLANNING_INTENT_FIDELITY
        # event payload at nlp_tools.py:396-399).
        assert set(telemetry.keys()) == {
            "intent_fidelity_score",
            "coverage_before_fill",
            "coverage_after_fill",
            "gap_filled_outcomes",
        }
        assert telemetry["intent_fidelity_score"] == 0.75
        assert telemetry["coverage_before_fill"] == {"o1": ["t1"]}
        assert telemetry["coverage_after_fill"] == {"o1": ["t1", "gap"]}
        # gap_filled_outcomes is the list of UserOutcome IDs (strings),
        # not the full UserOutcome objects — Cato consumes IDs.
        assert telemetry["gap_filled_outcomes"] == ["o2"]


class TestNormalizeGapTaskName:
    """Tests for ``_normalize_gap_task_name`` (issue #479).

    Weak LLMs (local models) sometimes return gap task names as
    ``snake_case`` slugs instead of the human-readable strings the
    prompt requests.  The normalizer detects and converts slugs so
    structural scaffolding tasks show readable names on the board.
    """

    def test_snake_case_slug_is_converted_to_title_case(self) -> None:
        """Classic Python-slug form must become readable title case."""
        assert _normalize_gap_task_name("implement_snake_movement") == (
            "Implement Snake Movement"
        )

    def test_single_word_without_underscores_passes_through(self) -> None:
        """A single lowercase word with no underscores is not a slug — pass through."""
        assert _normalize_gap_task_name("render") == "render"

    def test_already_readable_name_is_unchanged(self) -> None:
        """Human-readable names must pass through without modification."""
        assert _normalize_gap_task_name("Render snake to canvas") == (
            "Render snake to canvas"
        )

    def test_mixed_case_readable_name_is_unchanged(self) -> None:
        """CamelWord name must pass through without modification."""
        assert _normalize_gap_task_name("Implement SnakeGame core loop") == (
            "Implement SnakeGame core loop"
        )

    def test_empty_string_returns_empty_string(self) -> None:
        """Edge case: empty string must not raise."""
        assert _normalize_gap_task_name("") == ""

    def test_whitespace_only_is_stripped(self) -> None:
        """Whitespace-only input is treated as empty after strip."""
        assert _normalize_gap_task_name("   ") == ""

    def test_slug_with_numbers_is_converted(self) -> None:
        """Numeric tokens in a slug must be preserved in title case output."""
        assert _normalize_gap_task_name("setup_player2_controls") == (
            "Setup Player2 Controls"
        )

    def test_render_game_board_slug(self) -> None:
        """Regression: specific slug seen in test2-qwen25-instruct logs."""
        assert _normalize_gap_task_name("render_game_board") == "Render Game Board"

    def test_task_underscore_slug_is_promoted(self) -> None:
        """``task_signup_form`` slug must be promoted to ``Implement Signup Form``.

        Haiku / qwen pattern-match the literal word "task" out of the
        gap-fill schema's ``"<short task name>"`` and emit
        ``task_signup_form``-style slugs.  After slug conversion to
        ``Task Signup Form``, promote to ``Implement Signup Form`` so
        the board has one verb for the same semantic role as
        feature_based's ``Implement {feature_name}`` convention
        (advanced_parser.py:2980).
        """
        assert _normalize_gap_task_name("task_signup_form") == "Implement Signup Form"

    def test_task_underscore_slug_with_multi_token_payload_is_promoted(self) -> None:
        """Multi-token slug payload composes correctly through promotion."""
        assert (
            _normalize_gap_task_name("task_recipe_search") == "Implement Recipe Search"
        )

    def test_direct_task_prefix_is_preserved_when_not_a_slug(self) -> None:
        """Human-readable ``Task X`` from the LLM is trusted as intentional.

        Codex P2 on PR #509: an unconditional ``Task `` → ``Implement ``
        rewrite would mangle legitimate domain nouns in a task-management
        product (``Task Creation Form``, ``Task Assignment Rules``,
        ``Task Queue``) where ``Task`` IS the domain term.  Only the
        slug-converted path is the known LLM artifact; preserve
        human-readable names as the LLM emitted them.
        """
        assert _normalize_gap_task_name("Task Creation Form") == "Task Creation Form"
        assert (
            _normalize_gap_task_name("Task Assignment Rules") == "Task Assignment Rules"
        )
        assert _normalize_gap_task_name("Task Queue") == "Task Queue"

    def test_task_alone_is_not_promoted(self) -> None:
        """Bare ``Task`` with no payload after it is not a real task name.

        Strip + bail rather than producing ``Implement ``.  This is a
        defensive case: an LLM that returns the literal string "Task"
        signals upstream prompt failure; we don't want to mask it with
        a misleadingly-prefixed empty cleanup.
        """
        assert _normalize_gap_task_name("Task") == "Task"

    def test_implement_prefix_passes_through_unchanged(self) -> None:
        """Names already starting with Implement must not be re-prefixed."""
        assert (
            _normalize_gap_task_name("Implement Signup Form") == "Implement Signup Form"
        )


class TestCoveragePromptAntiBiasGuidance:
    """Lock the prompt edits that fight false-positive coverage from weak LLMs.

    Trial 11/12/14 (recipe PRD on local 7B models) all reported
    ``score=1.00, 0 gap(s)`` from ``compute_coverage_with_llm`` while the
    deterministic ``spec_coverage`` augmenter caught 5-8 real uncovered
    features — the LLM was generously mapping internal/structural tasks
    to user outcomes, hiding gaps from the gap-fill pipeline.

    The fix has two pillars:

    1. **Disposition** — the prompt explicitly anchors "empty" as the
       expected/healthy default.  This counters weak models' bias toward
       agreement.

    2. **Examples across domains** — the prompt teaches the concept
       through positive/negative pairs in multiple domains (REST, file,
       notification, CLI) rather than enumerating verbs.  Enumeration is
       the wrong altitude: domain space is unbounded and a static verb
       list would silently degrade coverage on domains we didn't
       anticipate.  Examples teach the principle; the LLM generalizes.

    These tests pin both pillars so a future edit doesn't quietly
    regress to the verb-list or no-disposition state.
    """

    def test_prompt_anchors_empty_as_expected_default(self) -> None:
        """The prompt must explicitly tell weak LLMs that empty is healthy."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            _LLM_COVERAGE_PROMPT,
        )

        assert "DEFAULT TO EMPTY" in _LLM_COVERAGE_PROMPT, (
            "Prompt must include the strong default-empty anchor that "
            "fights false-positive bias in weak local models."
        )

    def test_prompt_keeps_response_format_strictness(self) -> None:
        """JSON-only response constraint must survive any prompt rewrite."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            _LLM_COVERAGE_PROMPT,
        )

        assert "ONLY the JSON object" in _LLM_COVERAGE_PROMPT, (
            "Prompt must require JSON-only output. Without this, weak "
            "models prepend prose that breaks the parser."
        )

    def test_prompt_teaches_via_diverse_domain_examples(self) -> None:
        """
        The prompt must teach 'addresses' across multiple domains, not
        just rendering / game UI.

        Earlier versions of this fix tried a verb whitelist (render,
        display, submit, ...) which silently failed on backend-heavy
        domains where natural verbs were missing from the list.
        Examples across domains are the right altitude — they teach the
        principle without enumerating cases.

        At minimum the prompt must cover a non-rendering domain so weak
        models don't conclude "user-observable means rendering."
        """
        from src.marcus_mcp.coordinator.outcome_coverage import (
            _LLM_COVERAGE_PROMPT,
        )

        prompt_lower = _LLM_COVERAGE_PROMPT.lower()

        # At least one rendering / UI example.
        assert "render" in prompt_lower, (
            "Prompt must include a UI-rendering example to anchor the "
            "'visible movement' style of user-observable evidence."
        )

        # At least one non-UI domain — backend, file, notification, or CLI.
        non_ui_signals = (
            "http response",
            "stdout",
            "notification",
            "upload",
            "endpoint",
        )
        matches = [s for s in non_ui_signals if s in prompt_lower]
        assert matches, (
            f"Prompt must include at least one non-UI domain example so "
            f"weak models don't conclude user-observable means rendering. "
            f"None of {non_ui_signals} found in prompt."
        )

    def test_prompt_distinguishes_api_product_from_api_plumbing(self) -> None:
        """
        Regression for Codex P1 review on PR #490.

        The first version of the REST-API example showed a bare
        ``POST /api/users`` task as ADDRESSING the outcome ``user can
        sign up`` — but that's only true when the API IS the product
        surface (developer-facing API).  For the more common case (web
        app with REST backend), the same task is backend plumbing and
        the user-observable surface is the frontend form.

        The prompt must teach BOTH surfaces so weak models don't mark
        backend-only tasks as covering frontend outcomes for UI
        products — exactly the false-positive mode this prompt exists
        to prevent.
        """
        from src.marcus_mcp.coordinator.outcome_coverage import (
            _LLM_COVERAGE_PROMPT,
        )

        prompt_lower = _LLM_COVERAGE_PROMPT.lower()

        # Web app with backend case must be present and must mark the
        # bare endpoint as plumbing (not addressing the user outcome).
        assert "web app" in prompt_lower or "browser" in prompt_lower, (
            "Prompt must include the web-app-with-backend product case "
            "so the LLM doesn't mark bare endpoints as covering UI "
            "outcomes."
        )
        assert "plumbing" in prompt_lower, (
            "Prompt must explicitly call out backend tasks as plumbing "
            "for UI products."
        )

        # Developer-facing API case must also be present so we don't
        # over-correct in the other direction.
        assert "developer" in prompt_lower or "api consumer" in prompt_lower, (
            "Prompt must include the developer-facing API case so "
            "API-as-product outcomes still resolve correctly."
        )

    def test_prompt_states_principle_not_verb_list(self) -> None:
        """
        The prompt must teach the principle (completing a task produces
        observable evidence) rather than enumerate a verb whitelist.

        Verb whitelists silently degrade on unanticipated domains.  This
        test prevents future maintainers from quietly reintroducing the
        whitelist crutch.
        """
        from src.marcus_mcp.coordinator.outcome_coverage import (
            _LLM_COVERAGE_PROMPT,
        )

        # The principle statement must be present.
        assert "produces" in _LLM_COVERAGE_PROMPT.lower(), (
            "Prompt must state the principle: completing the task "
            "PRODUCES observable evidence."
        )

        # Whitelist phrasing must NOT be present (anti-pattern guard).
        anti_patterns = (
            "from the list above",
            "user-observable verb from the list",
            "must contain a verb",
        )
        for pat in anti_patterns:
            assert pat not in _LLM_COVERAGE_PROMPT.lower(), (
                f"Prompt regressed to verb-whitelist phrasing: "
                f"'{pat}'.  Use diverse examples and the principle "
                f"statement instead."
            )


class TestEnrichAcceptanceCriteriaWithSignals:
    """:func:`_enrich_acceptance_criteria_with_signals` projects each
    in-scope outcome's ``success_signal`` into the ``acceptance_criteria``
    of every task the coverage mapping says addresses it.

    Issue #523 Slice A.  This is the static-layer wire: the existing
    :class:`~src.ai.validation.work_analyzer.WorkAnalyzer` LLM gate
    validates source code against ``acceptance_criteria`` at task
    completion, so adding the user's success_signal to that field
    teaches the validator what user-observable outcome the task must
    satisfy without changing WorkAnalyzer itself.
    """

    def _signal_criteria(self, task: Task) -> list[str]:
        """Subset of acceptance_criteria stamped by the enricher."""
        return [
            c
            for c in (task.acceptance_criteria or [])
            if c.startswith(SIGNAL_CRITERION_PREFIX)
        ]

    def test_appends_signal_to_mapped_task(self) -> None:
        outcomes = [
            _outcome(
                "outcome_play",
                "user can play the snake game",
                "snake visibly moves on a board, arrow keys steer",
            )
        ]
        task = _task("t_render", "Render snake game", "draw snake on canvas")
        task.acceptance_criteria = ["Renderer draws snake"]
        mapping = {"outcome_play": ["t_render"]}

        result = _enrich_acceptance_criteria_with_signals(
            tasks=[task], outcomes=outcomes, mapping=mapping
        )

        assert len(result) == 1
        signals = self._signal_criteria(result[0])
        assert signals == [
            f"{SIGNAL_CRITERION_PREFIX}"
            "snake visibly moves on a board, arrow keys steer"
        ]
        # Existing criterion preserved
        assert "Renderer draws snake" in result[0].acceptance_criteria

    def test_pure_function_does_not_mutate_inputs(self) -> None:
        outcomes = [_outcome("o1", "user can do X", "X is visible")]
        task = _task("t1", "do X task", "")
        task.acceptance_criteria = ["pre-existing criterion"]
        original_criteria = list(task.acceptance_criteria)
        mapping = {"o1": ["t1"]}

        _enrich_acceptance_criteria_with_signals(
            tasks=[task], outcomes=outcomes, mapping=mapping
        )

        # Original task object's criteria are untouched.
        assert task.acceptance_criteria == original_criteria

    def test_task_not_in_mapping_passes_through_by_reference(self) -> None:
        """Unaffected tasks are returned by identity, not copied."""
        outcomes = [_outcome("o1", "user can play", "snake moves")]
        t_covered = _task("t_render", "render snake", "")
        t_uncovered = _task("t_unrelated", "unrelated task", "")
        mapping = {"o1": ["t_render"]}

        result = _enrich_acceptance_criteria_with_signals(
            tasks=[t_covered, t_uncovered], outcomes=outcomes, mapping=mapping
        )

        # t_uncovered is the exact same object (no allocation).
        assert result[1] is t_uncovered
        # t_covered was copied (replace produces a new instance).
        assert result[0] is not t_covered

    def test_out_of_scope_outcomes_are_skipped(self) -> None:
        """Out-of-scope outcomes must not gate or decorate completion."""
        outcomes = [
            _outcome(
                "o_oos",
                "user can authenticate",
                "login form visible",
                scope="out_of_scope",
            ),
            _outcome("o_play", "user can play", "snake moves", scope="in_scope"),
        ]
        t1 = _task("t1", "auth task", "")
        t2 = _task("t2", "renderer task", "")
        mapping = {"o_oos": ["t1"], "o_play": ["t2"]}

        result = _enrich_acceptance_criteria_with_signals(
            tasks=[t1, t2], outcomes=outcomes, mapping=mapping
        )

        # t1 unchanged — out-of-scope outcome contributes nothing.
        assert self._signal_criteria(result[0]) == []
        assert result[0] is t1
        # t2 gained the in-scope signal.
        assert self._signal_criteria(result[1]) == [
            f"{SIGNAL_CRITERION_PREFIX}snake moves"
        ]

    def test_multiple_outcomes_on_one_task_each_become_a_criterion(self) -> None:
        outcomes = [
            _outcome("o1", "user can play", "snake moves on board"),
            _outcome("o2", "user can see score", "score updates in DOM"),
        ]
        task = _task("t_all", "monolith task", "")
        mapping = {"o1": ["t_all"], "o2": ["t_all"]}

        result = _enrich_acceptance_criteria_with_signals(
            tasks=[task], outcomes=outcomes, mapping=mapping
        )

        signals = self._signal_criteria(result[0])
        assert set(signals) == {
            f"{SIGNAL_CRITERION_PREFIX}snake moves on board",
            f"{SIGNAL_CRITERION_PREFIX}score updates in DOM",
        }

    def test_one_outcome_across_many_tasks_decorates_all(self) -> None:
        outcomes = [_outcome("o1", "user can play", "snake moves")]
        t_render = _task("t_render", "render", "")
        t_input = _task("t_input", "handle input", "")
        mapping = {"o1": ["t_render", "t_input"]}

        result = _enrich_acceptance_criteria_with_signals(
            tasks=[t_render, t_input], outcomes=outcomes, mapping=mapping
        )

        for enriched in result:
            assert self._signal_criteria(enriched) == [
                f"{SIGNAL_CRITERION_PREFIX}snake moves"
            ]

    def test_idempotent_on_re_run(self) -> None:
        """Re-running enrichment must not produce duplicate criteria."""
        outcomes = [_outcome("o1", "user can play", "snake moves")]
        task = _task("t1", "render", "")
        mapping = {"o1": ["t1"]}

        first = _enrich_acceptance_criteria_with_signals(
            tasks=[task], outcomes=outcomes, mapping=mapping
        )
        second = _enrich_acceptance_criteria_with_signals(
            tasks=first, outcomes=outcomes, mapping=mapping
        )

        # Second pass adds nothing; identity passthrough on the no-op.
        assert second[0] is first[0]
        assert (
            len(
                [
                    c
                    for c in second[0].acceptance_criteria
                    if SIGNAL_CRITERION_PREFIX in c
                ]
            )
            == 1
        )

    def test_empty_mapping_returns_input_unchanged(self) -> None:
        outcomes = [_outcome("o1", "user can play", "snake moves")]
        tasks = [_task("t1", "task one", "")]
        result = _enrich_acceptance_criteria_with_signals(
            tasks=tasks, outcomes=outcomes, mapping={}
        )
        assert result is tasks or result == tasks  # passthrough acceptable

    def test_none_mapping_returns_input_unchanged(self) -> None:
        outcomes = [_outcome("o1", "user can play", "snake moves")]
        tasks = [_task("t1", "task one", "")]
        result = _enrich_acceptance_criteria_with_signals(
            tasks=tasks, outcomes=outcomes, mapping=None
        )
        assert result is tasks

    def test_no_outcomes_returns_input_unchanged(self) -> None:
        tasks = [_task("t1", "task one", "")]
        result = _enrich_acceptance_criteria_with_signals(
            tasks=tasks, outcomes=[], mapping={"o1": ["t1"]}
        )
        assert result is tasks

    def test_only_out_of_scope_outcomes_returns_input_unchanged(self) -> None:
        outcomes = [_outcome("o_oos", "user can X", "X visible", scope="out_of_scope")]
        tasks = [_task("t1", "task one", "")]
        result = _enrich_acceptance_criteria_with_signals(
            tasks=tasks, outcomes=outcomes, mapping={"o_oos": ["t1"]}
        )
        assert result is tasks

    def test_mapping_to_unknown_task_id_is_ignored(self) -> None:
        """Coverage entries for task ids not in the list don't crash."""
        outcomes = [_outcome("o1", "user can play", "snake moves")]
        tasks = [_task("t_real", "real task", "")]
        # mapping references a task id that doesn't exist in tasks
        result = _enrich_acceptance_criteria_with_signals(
            tasks=tasks, outcomes=outcomes, mapping={"o1": ["t_ghost"]}
        )
        assert self._signal_criteria(result[0]) == []
        assert result[0] is tasks[0]

    def test_task_with_empty_acceptance_criteria_gets_first_signal(self) -> None:
        """The acceptance_criteria default-factory is an empty list; the
        enricher must handle that without losing the signal."""
        outcomes = [_outcome("o1", "user can play", "snake moves")]
        task = _task("t1", "task", "")
        # Default factory leaves acceptance_criteria == [] — exercise it.
        assert task.acceptance_criteria == []
        mapping = {"o1": ["t1"]}

        result = _enrich_acceptance_criteria_with_signals(
            tasks=[task], outcomes=outcomes, mapping=mapping
        )

        assert result[0].acceptance_criteria == [
            f"{SIGNAL_CRITERION_PREFIX}snake moves"
        ]

    def test_emits_info_log_when_tasks_are_enriched(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One-line summary at INFO when the pass actually changes anything.

        Lets operators debug "did the signal land?" from logs alone,
        without inspecting the task graph directly.  Sibling to the
        ``Outcome coverage: score=...`` line emitted by the wrapping
        graph helpers.
        """
        outcomes = [
            _outcome("o1", "user can play", "snake moves"),
            _outcome("o2", "user can see score", "score updates"),
        ]
        tasks = [_task("t_render", "render", ""), _task("t_score", "score", "")]
        mapping = {"o1": ["t_render"], "o2": ["t_score"]}

        with caplog.at_level(
            "INFO", logger="src.marcus_mcp.coordinator.outcome_coverage"
        ):
            _enrich_acceptance_criteria_with_signals(
                tasks=tasks, outcomes=outcomes, mapping=mapping
            )

        msgs = [r.getMessage() for r in caplog.records]
        enrichment_logs = [m for m in msgs if "Signal enrichment" in m]
        assert len(enrichment_logs) == 1
        assert "2 task(s)" in enrichment_logs[0]
        assert "2 signal criterion" in enrichment_logs[0]
        assert "2 in-scope outcome(s)" in enrichment_logs[0]

    def test_silent_on_noop_idempotent_rerun(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No log line when nothing changed.

        Keeps steady-state idempotent re-runs from spamming logs.
        First pass enriches and logs; second pass on the same tasks
        is a no-op and must stay silent.
        """
        outcomes = [_outcome("o1", "user can play", "snake moves")]
        tasks = [_task("t1", "render", "")]
        mapping = {"o1": ["t1"]}

        first = _enrich_acceptance_criteria_with_signals(
            tasks=tasks, outcomes=outcomes, mapping=mapping
        )

        # caplog accumulates across calls within a test — clear so the
        # first-pass log doesn't pollute the no-op assertion.
        caplog.clear()
        with caplog.at_level(
            "INFO", logger="src.marcus_mcp.coordinator.outcome_coverage"
        ):
            _enrich_acceptance_criteria_with_signals(
                tasks=first, outcomes=outcomes, mapping=mapping
            )

        msgs = [r.getMessage() for r in caplog.records]
        assert not any(
            "Signal enrichment" in m for m in msgs
        ), f"No-op re-runs must not emit the enrichment log line — got: {msgs}"


# ``TestTranslateStubIdsToRealIds`` removed in #607 step 4: the
# rewrite-stub-to-real path no longer exists because gap-fill no
# longer materializes real ``gap_fill_<uuid>`` tasks. Stub→anchor
# routing for criterion + signal placement is tested directly in
# ``tests/unit/coordinator/test_gap_fill_criteria_rollup.py``.
