"""Unit tests for setup-time gotcha enumeration (issue #680).

A "gotcha" is a known failure mode for a user outcome — a behavior a
naive but spec-compliant implementation gets wrong (snake reversing into
itself, food spawning on the snake's body). #680 enumerates these at
decomposition time via one LLM call and writes them into the
``acceptance_criteria`` of every task that covers the outcome, so they
ride the #664 delivery pipe to the agent and the self-verify skeptic.

These tests cover the three units in isolation:
* ``enumerate_gotchas_with_llm`` — the batched LLM call + parse.
* ``_enrich_acceptance_criteria_with_gotchas`` — projection onto tasks.
* idempotency / graceful degradation contracts.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.ai.advanced.prd.outcome_extractor import UserOutcome
from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.coordinator.outcome_coverage import (
    GOTCHA_CRITERION_PREFIX,
    _enrich_acceptance_criteria_with_gotchas,
    enumerate_gotchas_with_llm,
)

pytestmark = pytest.mark.unit


def _task(
    task_id: str,
    name: str,
    acceptance_criteria=None,
    dependencies=None,
) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description="",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        acceptance_criteria=acceptance_criteria or [],
        dependencies=dependencies or [],
    )


def _outcome(out_id, action, signal, scope="in_scope") -> UserOutcome:
    return UserOutcome(id=out_id, action=action, success_signal=signal, scope=scope)


def _llm(payload: str) -> AsyncMock:
    mock = AsyncMock()
    mock.analyze = AsyncMock(return_value=payload)
    return mock


class TestEnumerateGotchasWithLLM:
    """Tests for the batched gotcha-enumeration LLM call."""

    @pytest.mark.asyncio
    async def test_returns_gotchas_per_outcome(self):
        """In-scope outcomes get their LLM-returned failure modes back."""
        outcomes = [
            _outcome("outcome_move", "user can steer the snake", "snake turns"),
        ]
        payload = json.dumps(
            {
                "gotchas": {
                    "outcome_move": [
                        "Pressing the opposite direction reverses into "
                        "the snake and ends the game; it must be ignored.",
                    ]
                }
            }
        )

        result = await enumerate_gotchas_with_llm(
            spec="a snake game", outcomes=outcomes, llm_client=_llm(payload)
        )

        assert "outcome_move" in result
        assert "must be ignored" in result["outcome_move"][0]

    @pytest.mark.asyncio
    async def test_out_of_scope_outcomes_excluded(self):
        """Out-of-scope outcomes are never sent and never enumerated."""
        outcomes = [
            _outcome("o_in", "user plays", "plays", scope="in_scope"),
            _outcome("o_out", "user logs in", "auth", scope="out_of_scope"),
        ]
        llm = _llm(json.dumps({"gotchas": {"o_in": ["g1"]}}))

        result = await enumerate_gotchas_with_llm(
            spec="spec", outcomes=outcomes, llm_client=llm
        )

        prompt = llm.analyze.call_args.kwargs["prompt"]
        assert "o_in" in prompt
        assert "o_out" not in prompt
        assert "o_out" not in result

    @pytest.mark.asyncio
    async def test_no_llm_call_when_no_in_scope_outcomes(self):
        """All-out-of-scope → no LLM call, empty result."""
        outcomes = [_outcome("o", "x", "y", scope="out_of_scope")]
        llm = _llm(json.dumps({"gotchas": {}}))

        result = await enumerate_gotchas_with_llm(
            spec="spec", outcomes=outcomes, llm_client=llm
        )

        assert result == {}
        llm.analyze.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_outcome_ids_dropped(self):
        """LLM hallucinating an id not in the input is ignored."""
        outcomes = [_outcome("real", "x", "y")]
        llm = _llm(json.dumps({"gotchas": {"real": ["g"], "made_up": ["bad"]}}))

        result = await enumerate_gotchas_with_llm(
            spec="spec", outcomes=outcomes, llm_client=llm
        )

        assert set(result) == {"real"}

    @pytest.mark.asyncio
    async def test_malformed_response_raises(self):
        """Missing 'gotchas' key surfaces as ValueError for the caller."""
        outcomes = [_outcome("o", "x", "y")]
        llm = _llm(json.dumps({"wrong_key": {}}))

        with pytest.raises(ValueError):
            await enumerate_gotchas_with_llm(
                spec="spec", outcomes=outcomes, llm_client=llm
            )


class TestEnrichAcceptanceCriteriaWithGotchas:
    """Tests for projecting gotchas onto covering tasks' acceptance_criteria."""

    def test_gotcha_lands_on_covering_task(self):
        """The covering task gains the stamped gotcha criterion."""
        tasks = [_task("t1", "Direction handling"), _task("t2", "Rendering")]
        gotchas = {"o_move": ["reversal is a no-op"]}
        mapping = {"o_move": ["t1"]}

        enriched = _enrich_acceptance_criteria_with_gotchas(
            tasks=tasks, gotchas_by_outcome=gotchas, mapping=mapping
        )

        t1 = next(t for t in enriched if t.id == "t1")
        t2 = next(t for t in enriched if t.id == "t2")
        assert f"{GOTCHA_CRITERION_PREFIX}reversal is a no-op" in t1.acceptance_criteria
        assert t2.acceptance_criteria == []

    def test_idempotent(self):
        """Re-running does not duplicate an already-stamped criterion."""
        tasks = [_task("t1", "x")]
        gotchas = {"o": ["g"]}
        mapping = {"o": ["t1"]}

        once = _enrich_acceptance_criteria_with_gotchas(
            tasks=tasks, gotchas_by_outcome=gotchas, mapping=mapping
        )
        twice = _enrich_acceptance_criteria_with_gotchas(
            tasks=once, gotchas_by_outcome=gotchas, mapping=mapping
        )

        stamped = [
            c
            for c in twice[0].acceptance_criteria
            if c.startswith(GOTCHA_CRITERION_PREFIX)
        ]
        assert len(stamped) == 1

    def test_empty_mapping_or_gotchas_is_noop(self):
        """No mapping or no gotchas returns tasks unchanged (same objects)."""
        tasks = [_task("t1", "x", acceptance_criteria=["existing"])]

        assert (
            _enrich_acceptance_criteria_with_gotchas(
                tasks=tasks, gotchas_by_outcome={"o": ["g"]}, mapping={}
            )
            is tasks
        )
        assert (
            _enrich_acceptance_criteria_with_gotchas(
                tasks=tasks, gotchas_by_outcome={}, mapping={"o": ["t1"]}
            )
            is tasks
        )

    def test_preserves_existing_criteria(self):
        """Existing criteria stay first; gotchas append after."""
        tasks = [_task("t1", "x", acceptance_criteria=["pre-existing signal"])]
        enriched = _enrich_acceptance_criteria_with_gotchas(
            tasks=tasks,
            gotchas_by_outcome={"o": ["g"]},
            mapping={"o": ["t1"]},
        )
        ac = enriched[0].acceptance_criteria
        assert ac[0] == "pre-existing signal"
        assert ac[1] == f"{GOTCHA_CRITERION_PREFIX}g"


class TestImplementationTaskPlacement:
    """#680 placement: gotchas land on implementation tasks, not design/testing."""

    def test_design_only_mapping_routes_to_impl_dependent(self):
        """Outcome mapped only to a design task → gotcha lands on the
        implementation task that depends on that design (tier 2)."""
        design = _task("design_movement", "Design Game Physics and Movement")
        impl = _task(
            "impl_movement",
            "Implement Snake Movement",
            dependencies=["design_movement"],
        )
        enriched = _enrich_acceptance_criteria_with_gotchas(
            tasks=[design, impl],
            gotchas_by_outcome={"o_move": ["reversal is a no-op"]},
            mapping={"o_move": ["design_movement"]},
        )
        by_id = {t.id: t for t in enriched}
        assert by_id["design_movement"].acceptance_criteria == []
        assert (
            f"{GOTCHA_CRITERION_PREFIX}reversal is a no-op"
            in by_id["impl_movement"].acceptance_criteria
        )

    def test_mixed_mapping_excludes_design_task(self):
        """When the mapping covers both a design and an impl task, only the
        impl task is stamped (tier 1 keeps impl, drops design)."""
        design = _task("design_x", "Design X")
        impl = _task("impl_x", "Implement X")
        enriched = _enrich_acceptance_criteria_with_gotchas(
            tasks=[design, impl],
            gotchas_by_outcome={"o": ["g"]},
            mapping={"o": ["design_x", "impl_x"]},
        )
        by_id = {t.id: t for t in enriched}
        assert by_id["design_x"].acceptance_criteria == []
        assert f"{GOTCHA_CRITERION_PREFIX}g" in by_id["impl_x"].acceptance_criteria

    def test_orphaned_outcome_is_dropped_and_logged(self, caplog):
        """Outcome mapped only to a design task with no impl dependent →
        gotcha dropped, logged loudly as a decomposition gap (tier 3)."""
        import logging

        design = _task("design_only", "Design Something")
        with caplog.at_level(logging.WARNING):
            enriched = _enrich_acceptance_criteria_with_gotchas(
                tasks=[design],
                gotchas_by_outcome={"o_orphan": ["g"]},
                mapping={"o_orphan": ["design_only"]},
            )
        assert enriched[0].acceptance_criteria == []
        assert "o_orphan" in caplog.text
        assert "decomposition gap" in caplog.text

    def test_testing_task_not_stamped(self):
        """A testing-typed task covering an outcome is excluded; the gotcha
        routes to the impl task instead."""
        testing = _task("test_x", "Test X feature")
        impl = _task("impl_x", "Implement X feature", dependencies=["test_x"])
        enriched = _enrich_acceptance_criteria_with_gotchas(
            tasks=[testing, impl],
            gotchas_by_outcome={"o": ["g"]},
            mapping={"o": ["test_x"]},
        )
        by_id = {t.id: t for t in enriched}
        assert by_id["test_x"].acceptance_criteria == []
        assert f"{GOTCHA_CRITERION_PREFIX}g" in by_id["impl_x"].acceptance_criteria


class TestMapCoreFeatureIds:
    """#683 Cause 1: map AI features to in-scope outcomes (semantic, via LLM)."""

    @pytest.mark.asyncio
    async def test_returns_core_ids_from_llm(self):
        from src.marcus_mcp.coordinator.outcome_coverage import (
            map_core_feature_ids_with_llm,
        )

        reqs = [
            {"id": "f_move", "name": "Movement", "description": "steer snake"},
            {"id": "f_theme", "name": "Dark theme", "description": "color scheme"},
        ]
        outcomes = [_outcome("o_play", "user plays snake", "snake moves")]
        llm = _llm(json.dumps({"core_feature_ids": ["f_move"]}))

        core = await map_core_feature_ids_with_llm(
            requirements=reqs, outcomes=outcomes, llm_client=llm
        )
        assert core == {"f_move"}

    @pytest.mark.asyncio
    async def test_unknown_ids_dropped(self):
        from src.marcus_mcp.coordinator.outcome_coverage import (
            map_core_feature_ids_with_llm,
        )

        reqs = [{"id": "f_move", "name": "Movement", "description": "d"}]
        outcomes = [_outcome("o", "x", "y")]
        llm = _llm(json.dumps({"core_feature_ids": ["f_move", "hallucinated"]}))

        core = await map_core_feature_ids_with_llm(
            requirements=reqs, outcomes=outcomes, llm_client=llm
        )
        assert core == {"f_move"}

    @pytest.mark.asyncio
    async def test_no_in_scope_outcomes_returns_all_ids(self):
        """No in-scope outcomes → caller should keep everything; helper
        returns all ids and makes no LLM call."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            map_core_feature_ids_with_llm,
        )

        reqs = [{"id": "f1", "name": "A", "description": "d"}]
        outcomes = [_outcome("o", "x", "y", scope="out_of_scope")]
        llm = _llm(json.dumps({"core_feature_ids": []}))

        core = await map_core_feature_ids_with_llm(
            requirements=reqs, outcomes=outcomes, llm_client=llm
        )
        assert core == {"f1"}
        llm.analyze.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_response_raises(self):
        from src.marcus_mcp.coordinator.outcome_coverage import (
            map_core_feature_ids_with_llm,
        )

        reqs = [{"id": "f1", "name": "A", "description": "d"}]
        outcomes = [_outcome("o", "x", "y")]
        llm = _llm(json.dumps({"wrong_key": []}))
        with pytest.raises(ValueError):
            await map_core_feature_ids_with_llm(
                requirements=reqs, outcomes=outcomes, llm_client=llm
            )
