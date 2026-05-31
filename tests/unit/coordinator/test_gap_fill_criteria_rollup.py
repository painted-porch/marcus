"""Unit tests for #607 step 4 — gap-fill rollup to completion_criteria.

Step 4 of the decomposition redesign (#607) flips the output channel of
the intent-fidelity coverage check: instead of materializing one new
``Implement <gap>`` task per uncovered outcome (which was atomization
mechanism #1 from the issue), the gap-fill output is appended as a
behavior-criterion string on an existing task's ``completion_criteria``.

Routing precedence per gap (implemented by ``_route_gap_fill_to_criteria``):

1. If ``coverage_after_fill[outcome_id]`` lists a NATIVE task alongside
   the stub, that native task is the anchor (the post-fill LLM judged
   it to co-cover the outcome — the natural home for the criterion).
2. Else if a task labelled "integration" / "verification" exists
   (the integration-verification task), it is the anchor for
   cross-cutting outcomes.
3. Else fall back to the first task in the input list — degraded but
   functional. Only reached when the project has neither a native
   anchor nor an integration-verification task.

The intent-fidelity score, coverage maps, and gap_filled_outcomes are
unchanged — only the OUTPUT FORM is different (criteria on existing
tasks instead of new tasks).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional
from unittest.mock import AsyncMock

import pytest

from src.ai.advanced.prd.advanced_parser import PRDAnalysis
from src.ai.advanced.prd.outcome_extractor import UserOutcome
from src.config.outcome_coverage_config import ENV_VAR_NAME
from src.core.models import Priority, Task, TaskStatus

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _task(
    task_id: str,
    name: str = "Implement Foo",
    labels: Optional[List[str]] = None,
) -> Task:
    """Minimal in-scope native task for the routing tests."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description="...",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=2.0,
        labels=labels or [],
        project_id="proj_1",
        project_name="Test Project",
    )


def _outcome(out_id: str = "outcome_play") -> UserOutcome:
    """In-scope user outcome."""
    return UserOutcome(
        id=out_id,
        action="user plays the game",
        success_signal="game becomes interactive",
        scope="in_scope",
    )


def _bare_analysis(outcomes: List[UserOutcome]) -> PRDAnalysis:
    """Minimal PRDAnalysis carrying the supplied outcomes."""
    return PRDAnalysis(
        functional_requirements=[],
        non_functional_requirements=[],
        technical_constraints=[],
        business_objectives=[],
        user_personas=[],
        success_metrics=[],
        implementation_approach="agile",
        complexity_assessment={},
        risk_factors=[],
        confidence=0.9,
        original_description="build a snake game",
        user_outcomes=outcomes,
    )


def _llm_mock(
    *,
    pre_fill_coverage: str,
    fill_response: str,
    post_fill_coverage: str,
) -> AsyncMock:
    """Build an LLM mock that returns the three coverage-pipeline responses.

    ``apply_outcome_coverage`` calls the LLM three times:
    pre-fill coverage, gap-fill synthesis, post-fill coverage.
    """
    llm = AsyncMock()
    llm.analyze = AsyncMock(
        side_effect=[pre_fill_coverage, fill_response, post_fill_coverage]
    )
    return llm


# ---------------------------------------------------------------------------
# Step 4 — no new tasks; gaps become criteria
# ---------------------------------------------------------------------------


class TestNoNewTasksCreatedByGapFill:
    """After #607 step 4, gap-fill never synthesizes new board tasks.

    The intent-fidelity check still runs and identifies uncovered
    outcomes; its output is routed to existing tasks' criteria instead
    of materialized into ``gap_fill_<uuid>`` Tasks.
    """

    @pytest.mark.asyncio
    async def test_zero_new_tasks_when_gap_synthesized(self, monkeypatch: Any) -> None:
        """Gap synthesized → augmented_tasks count equals input count."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        # Pre-fill: outcome uncovered.  Fill: 1 gap.  Post-fill: stub covers
        # it.
        llm = _llm_mock(
            pre_fill_coverage='{"coverage": {"outcome_play": []}}',
            fill_response=(
                '{"tasks": [{'
                '"name": "Render snake to canvas",'
                '"description": "draw snake on canvas"'
                "}]}"
            ),
            post_fill_coverage=(
                '{"coverage": {"outcome_play": ["_synth_for_coverage_0"]}}'
            ),
        )
        native = _task("t_state", "Implement Snake State")

        result = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=[native],
            llm_client=llm,
        )

        assert len(result.augmented_tasks) == 1, (
            "step 4 must not add new tasks for gap-fill outcomes — "
            "gaps roll up into existing tasks' completion_criteria"
        )
        # No gap_fill_<uuid> task in the augmented list
        assert not any(t.id.startswith("gap_fill_") for t in result.augmented_tasks)
        # synthesized_ids is empty under the rollup model
        assert result.synthesized_ids == []

    @pytest.mark.asyncio
    async def test_zero_new_tasks_when_multiple_gaps_synthesized(
        self, monkeypatch: Any
    ) -> None:
        """Two gaps → still zero new tasks; two criteria appended."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        llm = _llm_mock(
            pre_fill_coverage=(
                '{"coverage": {"outcome_play": [], "outcome_save": []}}'
            ),
            fill_response=(
                '{"tasks": ['
                '{"name": "Render snake to canvas",'
                ' "description": "draw snake on canvas"},'
                '{"name": "Persist high score",'
                ' "description": "write score to disk"}'
                "]}"
            ),
            post_fill_coverage=(
                '{"coverage": {'
                '"outcome_play": ["_synth_for_coverage_0"],'
                '"outcome_save": ["_synth_for_coverage_1"]'
                "}}"
            ),
        )
        native = _task("t_state", "Implement Snake State")

        result = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis(
                [_outcome("outcome_play"), _outcome("outcome_save")]
            ),
            tasks=[native],
            llm_client=llm,
        )

        assert len(result.augmented_tasks) == 1
        assert result.synthesized_ids == []


# ---------------------------------------------------------------------------
# Routing precedence
# ---------------------------------------------------------------------------


class TestCriterionRouting:
    """Routing picks the right anchor task for each gap."""

    @pytest.mark.asyncio
    async def test_routes_to_native_task_named_in_pre_fill_coverage(
        self, monkeypatch: Any
    ) -> None:
        """When pre-fill coverage maps the outcome to a native task,
        criterion lands on that task."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        # outcome_play pre-fill-covered by t_state (i.e. it almost covers
        # but the coverage check still wants a gap-fill criterion).
        llm = _llm_mock(
            pre_fill_coverage='{"coverage": {"outcome_play": []}}',
            fill_response=(
                '{"tasks": [{'
                '"name": "Render snake to canvas",'
                '"description": "draw snake on canvas"'
                "}]}"
            ),
            post_fill_coverage=(
                '{"coverage": {"outcome_play": ["_synth_for_coverage_0", "t_state"]}}'
            ),
        )
        t_state = _task("t_state", "Implement Snake State")
        t_other = _task("t_other", "Implement High Score")

        result = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=[t_state, t_other],
            llm_client=llm,
        )

        anchor = next(t for t in result.augmented_tasks if t.id == "t_state")
        non_anchor = next(t for t in result.augmented_tasks if t.id == "t_other")

        criteria = list(anchor.completion_criteria or [])
        assert any(
            "Render snake to canvas" in c for c in criteria
        ), f"Gap-fill criterion not appended to anchor: {criteria}"
        # The non-anchor task is untouched
        assert not (non_anchor.completion_criteria or [])

    @pytest.mark.asyncio
    async def test_routes_cross_cutting_gap_to_integration_verification(
        self, monkeypatch: Any
    ) -> None:
        """When no pre-fill anchor exists, criterion lands on integration-
        verification."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        # outcome_play is uncovered pre-fill → no native anchor.
        llm = _llm_mock(
            pre_fill_coverage='{"coverage": {"outcome_play": []}}',
            fill_response=(
                '{"tasks": [{'
                '"name": "End-to-end snake gameplay",'
                '"description": "user can play a full snake round"'
                "}]}"
            ),
            post_fill_coverage=(
                '{"coverage": {"outcome_play": ["_synth_for_coverage_0"]}}'
            ),
        )
        t_state = _task("t_state", "Implement Snake State")
        # Integration-verification task — labelled with the canonical
        # markers Marcus stamps on these.
        t_int = _task(
            "t_int",
            "Integration verification for snake",
            labels=["integration", "verification", "type:integration"],
        )

        result = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=[t_state, t_int],
            llm_client=llm,
        )

        anchor = next(t for t in result.augmented_tasks if t.id == "t_int")
        non_anchor = next(t for t in result.augmented_tasks if t.id == "t_state")

        criteria = list(anchor.completion_criteria or [])
        assert any("End-to-end snake gameplay" in c for c in criteria), (
            f"Cross-cutting criterion not routed to integration-verification: "
            f"{criteria}"
        )
        assert not (non_anchor.completion_criteria or [])

    @pytest.mark.asyncio
    async def test_falls_back_to_first_task_when_no_anchor_or_integration(
        self, monkeypatch: Any
    ) -> None:
        """Last-resort fallback: gap with no anchor and no integration-
        verification lands on the first task."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        llm = _llm_mock(
            pre_fill_coverage='{"coverage": {"outcome_play": []}}',
            fill_response=(
                '{"tasks": [{'
                '"name": "Render snake to canvas",'
                '"description": "draw snake on canvas"'
                "}]}"
            ),
            post_fill_coverage=(
                '{"coverage": {"outcome_play": ["_synth_for_coverage_0"]}}'
            ),
        )
        # Only one task, no integration-verification.
        t_first = _task("t_first", "Implement Snake State")

        result = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=[t_first],
            llm_client=llm,
        )

        # Degraded but functional: criterion lands on the only task.
        assert len(result.augmented_tasks) == 1
        criteria = list(result.augmented_tasks[0].completion_criteria or [])
        assert any(
            "Render snake to canvas" in c for c in criteria
        ), f"Fallback criterion not appended to first task: {criteria}"


# ---------------------------------------------------------------------------
# Criterion text shape
# ---------------------------------------------------------------------------


class TestCriterionText:
    """Criteria name behaviors, not implementation HOW.

    Critical for #607 bright-line: gap-fill criteria must say WHAT must
    be covered (the user-outcome behavior), not HOW to implement it.
    Two agents reading the same criterion must be free to write
    legitimately different code.
    """

    @pytest.mark.asyncio
    async def test_criterion_includes_gap_name_and_description(
        self, monkeypatch: Any
    ) -> None:
        """Criterion mentions both the gap-fill name and the description."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        llm = _llm_mock(
            pre_fill_coverage='{"coverage": {"outcome_play": []}}',
            fill_response=(
                '{"tasks": [{'
                '"name": "Render snake to canvas",'
                '"description": "draw snake on canvas each tick"'
                "}]}"
            ),
            post_fill_coverage=(
                '{"coverage": {"outcome_play": ["_synth_for_coverage_0", "t_state"]}}'
            ),
        )
        t_state = _task("t_state", "Implement Snake State")

        result = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=[t_state],
            llm_client=llm,
        )

        criteria = " ".join(result.augmented_tasks[0].completion_criteria or [])
        assert "Render snake to canvas" in criteria
        assert "draw snake on canvas each tick" in criteria

    @pytest.mark.asyncio
    async def test_criterion_does_not_name_framework_or_pattern(
        self, monkeypatch: Any
    ) -> None:
        """Criterion is bright-line clean: no framework / pattern names."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        llm = _llm_mock(
            pre_fill_coverage='{"coverage": {"outcome_play": []}}',
            fill_response=(
                '{"tasks": [{'
                '"name": "Render snake to canvas",'
                '"description": "draw snake on canvas"'
                "}]}"
            ),
            post_fill_coverage=(
                '{"coverage": {"outcome_play": ["_synth_for_coverage_0", "t_state"]}}'
            ),
        )
        t_state = _task("t_state", "Implement Snake State")

        result = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=[t_state],
            llm_client=llm,
        )

        joined = " ".join(result.augmented_tasks[0].completion_criteria or []).lower()
        forbidden_hows = [
            "pytest",
            "unittest",
            "jest",
            "react",
            "django",
            "tkinter",
        ]
        for token in forbidden_hows:
            assert token not in joined, (
                f"Criterion accidentally prescribes framework '{token}': " f"{joined}"
            )


# ---------------------------------------------------------------------------
# Idempotence + telemetry
# ---------------------------------------------------------------------------


class TestRolloupIdempotenceAndTelemetry:
    """Re-runs add no duplicates; telemetry shape is unchanged."""

    @pytest.mark.asyncio
    async def test_rerun_does_not_duplicate_criterion(self, monkeypatch: Any) -> None:
        """Idempotent rollup: same gap → criterion stamped once."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")

        def build_llm() -> AsyncMock:
            return _llm_mock(
                pre_fill_coverage='{"coverage": {"outcome_play": []}}',
                fill_response=(
                    '{"tasks": [{'
                    '"name": "Render snake to canvas",'
                    '"description": "draw snake on canvas"'
                    "}]}"
                ),
                post_fill_coverage=(
                    '{"coverage": {"outcome_play": ['
                    '"_synth_for_coverage_0", "t_state"]}}'
                ),
            )

        t_state = _task("t_state", "Implement Snake State")

        result1 = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=[t_state],
            llm_client=build_llm(),
        )
        first_task = result1.augmented_tasks[0]
        criteria_after_first = list(first_task.completion_criteria or [])

        # Re-run on the augmented output — the criterion is already there.
        result2 = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=result1.augmented_tasks,
            llm_client=build_llm(),
        )
        criteria_after_second = list(
            result2.augmented_tasks[0].completion_criteria or []
        )

        # Some criteria from result1 may also be added by signal
        # enrichment; the rollup criterion specifically must not
        # duplicate.
        gap_criteria_1 = [
            c for c in criteria_after_first if "Render snake to canvas" in c
        ]
        gap_criteria_2 = [
            c for c in criteria_after_second if "Render snake to canvas" in c
        ]
        assert len(gap_criteria_1) == 1
        assert len(gap_criteria_2) == 1, (
            "Idempotent rollup: re-running must not append a duplicate "
            "gap-fill criterion"
        )

    @pytest.mark.asyncio
    async def test_empty_native_graph_falls_back_to_materializing_gap_tasks(
        self, monkeypatch: Any
    ) -> None:
        """Codex P1 on #611: when ``tasks=[]`` the rollup has no anchor;
        without a fallback, gaps would be silently dropped and the
        project would end up empty.

        Pre-step-4 behavior: ``_create_detailed_tasks`` returning ``[]``
        (AI decomposer failure) could still be rescued by gap-fill
        materializing tasks. Step 4 must preserve that safety net by
        materializing gap-fill tasks ONLY when the native graph is
        empty — the atomization concern that motivated #607 step 4
        doesn't apply with zero native tasks (there's nothing to
        atomize alongside).
        """
        from src.marcus_mcp.coordinator.outcome_coverage import (
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        # NOTE: when ``tasks=[]``, ``compute_coverage_with_llm`` short-
        # circuits without an LLM call (returns ``{outcome_id: []}``
        # directly), so the mock only needs the fill + post-fill
        # responses — not the pre-fill one.
        llm = AsyncMock()
        llm.analyze = AsyncMock(
            side_effect=[
                # fill_gaps:
                (
                    '{"tasks": [{'
                    '"name": "Render snake to canvas",'
                    '"description": "draw snake on canvas"'
                    "}]}"
                ),
                # post-fill coverage:
                '{"coverage": {"outcome_play": ["_synth_for_coverage_0"]}}',
            ]
        )

        # Empty native graph — the AI-failure rescue case.
        result = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=[],
            llm_client=llm,
        )

        # The gap survives — materialized as a task on the previously
        # empty graph. synthesized_ids surfaces it for downstream.
        assert len(result.augmented_tasks) == 1, (
            "Empty native graph: gap-fill must be materialized as a "
            "rescue task so coverage is preserved (Codex P1 on #611)"
        )
        materialized = result.augmented_tasks[0]
        assert materialized.id.startswith("gap_fill_")
        assert "Render snake to canvas" in materialized.name
        assert len(result.synthesized_ids) == 1
        assert result.synthesized_ids[0] == materialized.id

    @pytest.mark.asyncio
    async def test_contract_responsibility_preserved_in_criterion(
        self, monkeypatch: Any
    ) -> None:
        """Codex P2 on #611: contract-first gap dicts carry a
        ``responsibility`` field ("implements <Iface> from <path>") that
        the pre-step-4 ``_build_contract_gap_fill_task`` projected onto
        ``Task.responsibility`` / contract labels / source_context.

        Step 4 routes contract gaps to existing tasks' criteria instead
        of new Tasks. The criterion text must carry the contract
        ownership so the agent reading the criterion sees the contract
        framing, not just the name+description.
        """
        from datetime import datetime, timezone

        from src.marcus_mcp.coordinator.outcome_coverage import (
            OUTCOME_GAP_CRITERION_PREFIX,
            apply_outcome_coverage_to_contract_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        llm = AsyncMock()
        llm.analyze = AsyncMock(
            side_effect=[
                '{"coverage": {"outcome_play": []}}',
                (
                    '{"tasks": [{'
                    '"name": "Render snake to canvas",'
                    '"description": "draw snake on canvas",'
                    '"responsibility": "implements RenderingAgent from '
                    'contracts/rendering.ts"'
                    "}]}"
                ),
                (
                    '{"coverage": {"outcome_play": ['
                    '"_synth_for_coverage_0", "t_contract"]}}'
                ),
            ]
        )
        now = datetime.now(timezone.utc)
        t_contract = Task(
            id="t_contract",
            name="Implement Snake State",
            description="Snake state machine.",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=now,
            updated_at=now,
            due_date=None,
            estimated_hours=2.0,
            project_id="proj_1",
            project_name="Snake",
            responsibility="implements SnakeState from contracts/state.ts",
        )

        result = await apply_outcome_coverage_to_contract_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=[t_contract],
            contract_artifacts={
                "rendering": {"artifacts": [{"name": "RenderingAgent"}]},
            },
            llm_client=llm,
        )

        anchor = result.augmented_tasks[0]
        rollup = [
            c
            for c in (anchor.completion_criteria or [])
            if c.startswith(OUTCOME_GAP_CRITERION_PREFIX)
        ]
        assert len(rollup) == 1, f"Expected one rollup criterion; got {rollup}"
        # The criterion must surface the contract ownership from
        # gap-fill's ``responsibility`` field, not just name/desc.
        assert "implements RenderingAgent" in rollup[0], (
            f"Codex P2: contract responsibility from gap-fill dropped "
            f"by rollup; criterion text: {rollup[0]!r}"
        )

    @pytest.mark.asyncio
    async def test_telemetry_preserved_after_rollup(self, monkeypatch: Any) -> None:
        """intent_fidelity_score and coverage_after_fill survive the rollup."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        llm = _llm_mock(
            pre_fill_coverage='{"coverage": {"outcome_play": []}}',
            fill_response=(
                '{"tasks": [{'
                '"name": "Render snake to canvas",'
                '"description": "draw snake on canvas"'
                "}]}"
            ),
            post_fill_coverage=(
                '{"coverage": {"outcome_play": ['
                '"_synth_for_coverage_0", "t_state"]}}'
            ),
        )
        t_state = _task("t_state", "Implement Snake State")

        result = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=[t_state],
            llm_client=llm,
        )

        assert "intent_fidelity_score" in result.telemetry
        assert "coverage_before_fill" in result.telemetry
        assert "coverage_after_fill" in result.telemetry
        assert "gap_filled_outcomes" in result.telemetry


# ---------------------------------------------------------------------------
# #683 Cause 2 — core gaps with no implementation home synthesize impl tasks
# ---------------------------------------------------------------------------


class TestCoreGapSynthesizesImplTask:
    """A gap whose resolved anchor is a non-implementation (design) task is
    materialized into a real implementation task (#683 Cause 2), instead of
    rolling its criterion onto a design task where no code lives.
    """

    @pytest.mark.asyncio
    async def test_design_anchor_gap_synthesizes_impl_task(
        self, monkeypatch: Any
    ) -> None:
        """Gap co-anchored only to a design task → new impl task; design
        task does NOT gain the criterion."""
        from src.core.task_classification import get_task_type
        from src.marcus_mcp.coordinator.outcome_coverage import (
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        llm = _llm_mock(
            pre_fill_coverage='{"coverage": {"outcome_play": []}}',
            fill_response=(
                '{"tasks": [{'
                '"name": "Implement collision detection",'
                '"description": "end the game on wall or self collision"'
                "}]}"
            ),
            # Post-fill maps the outcome to the DESIGN task + the stub.
            post_fill_coverage=(
                '{"coverage": {"outcome_play": '
                '["t_design", "_synth_for_coverage_0"]}}'
            ),
        )
        design = _task("t_design", "Design Game Core Mechanics", labels=["design"])

        result = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=[design],
            llm_client=llm,
        )

        # A new implementation task was synthesized.
        synth = [t for t in result.augmented_tasks if t.id.startswith("gap_fill_")]
        assert len(synth) == 1, "core gap on a design anchor must synthesize a task"
        assert get_task_type(synth[0]) == "implementation"
        assert result.synthesized_ids == [synth[0].id]

        # The design task did NOT absorb the gap criterion.
        design_out = next(t for t in result.augmented_tasks if t.id == "t_design")
        assert not (design_out.completion_criteria or [])

    @pytest.mark.asyncio
    async def test_impl_anchor_gap_does_not_synthesize(self, monkeypatch: Any) -> None:
        """When the co-anchor is already an implementation task, no new task
        is created — the criterion rolls onto it (anti-atomization guard)."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        llm = _llm_mock(
            pre_fill_coverage='{"coverage": {"outcome_play": []}}',
            fill_response=(
                '{"tasks": [{'
                '"name": "Implement collision detection",'
                '"description": "end the game on collision"'
                "}]}"
            ),
            post_fill_coverage=(
                '{"coverage": {"outcome_play": ' '["t_impl", "_synth_for_coverage_0"]}}'
            ),
        )
        impl = _task("t_impl", "Implement Snake State")

        result = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=[impl],
            llm_client=llm,
        )

        assert not any(t.id.startswith("gap_fill_") for t in result.augmented_tasks)
        assert result.synthesized_ids == []
        impl_out = next(t for t in result.augmented_tasks if t.id == "t_impl")
        assert any(
            "collision" in c.lower() for c in (impl_out.completion_criteria or [])
        )

    @pytest.mark.asyncio
    async def test_gotcha_lands_on_synthesized_task(self, monkeypatch: Any) -> None:
        """End-to-end #683: a #680 gotcha for the gap's outcome is attached to
        the synthesized implementation task (not dropped as an orphan)."""
        from src.marcus_mcp.coordinator.outcome_coverage import (
            GOTCHA_CRITERION_PREFIX,
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        monkeypatch.setenv("MARCUS_GOTCHA_ENUMERATION", "true")
        # 4 LLM calls: pre-fill, fill, post-fill, gotcha enumeration.
        llm = AsyncMock()
        llm.analyze = AsyncMock(
            side_effect=[
                '{"coverage": {"outcome_play": []}}',
                (
                    '{"tasks": [{'
                    '"name": "Implement collision detection",'
                    '"description": "end the game on collision"'
                    "}]}"
                ),
                '{"coverage": {"outcome_play": ["t_design", "_synth_for_coverage_0"]}}',
                (
                    '{"gotchas": {"outcome_play": '
                    '["the snake passes through its own body instead of dying"]}}'
                ),
            ]
        )
        design = _task("t_design", "Design Game Core Mechanics", labels=["design"])

        result = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis([_outcome()]),
            tasks=[design],
            llm_client=llm,
        )

        synth = next(t for t in result.augmented_tasks if t.id.startswith("gap_fill_"))
        assert any(
            c.startswith(GOTCHA_CRITERION_PREFIX)
            for c in (synth.acceptance_criteria or [])
        ), "gotcha must land on the synthesized implementation task"

    @pytest.mark.asyncio
    async def test_synthesis_capped(self, monkeypatch: Any) -> None:
        """No more than GAP_SYNTH_CAP impl tasks are synthesized; the rest
        fall back to the design anchor's completion_criteria."""
        import src.marcus_mcp.coordinator.outcome_coverage as oc
        from src.marcus_mcp.coordinator.outcome_coverage import (
            apply_outcome_coverage_to_feature_graph,
        )

        monkeypatch.setenv(ENV_VAR_NAME, "true")
        monkeypatch.setattr(oc, "GAP_SYNTH_CAP", 2)

        n_gaps = 4
        outcomes = [_outcome(f"o{i}") for i in range(n_gaps)]
        gap_tasks = ",".join(
            f'{{"name": "Implement feature {i}", "description": "d{i}"}}'
            for i in range(n_gaps)
        )
        post_cov = ",".join(
            f'"o{i}": ["t_design", "_synth_for_coverage_{i}"]' for i in range(n_gaps)
        )
        pre_cov = ",".join(f'"o{i}": []' for i in range(n_gaps))
        llm = _llm_mock(
            pre_fill_coverage=f'{{"coverage": {{{pre_cov}}}}}',
            fill_response=f'{{"tasks": [{gap_tasks}]}}',
            post_fill_coverage=f'{{"coverage": {{{post_cov}}}}}',
        )
        design = _task("t_design", "Design Game Core Mechanics", labels=["design"])

        result = await apply_outcome_coverage_to_feature_graph(
            prd_analysis=_bare_analysis(outcomes),
            tasks=[design],
            llm_client=llm,
        )

        synth = [t for t in result.augmented_tasks if t.id.startswith("gap_fill_")]
        assert len(synth) == 2, "synthesis must respect the cap"
        # The 2 over-cap gaps fell back onto the design task's criteria.
        design_out = next(t for t in result.augmented_tasks if t.id == "t_design")
        assert len(design_out.completion_criteria or []) == 2


class TestSynthesizedTaskContractRoundTrip:
    """#687 Codex P1: a synthesized contract gap task must carry its
    responsibility in source_context + the description marker, not only the
    SQLite-native top-level field, so it survives the kanban round-trip and
    the contract-responsibility instruction layer fires."""

    def test_responsibility_persisted_for_round_trip(self) -> None:
        from src.marcus_mcp.coordinator.outcome_coverage import (
            _build_impl_task_from_gap,
        )

        task = _build_impl_task_from_gap(
            {
                "name": "Implement GameEngine",
                "description": "build the loop",
                "responsibility": "implements GameEngine from engine.ts",
                "contract_file": "docs/engine.ts",
            }
        )
        assert task is not None
        # Native field (SQLite)
        assert task.responsibility == "implements GameEngine from engine.ts"
        # source_context (JSON-capable providers, via build_task_data)
        assert task.source_context is not None
        assert (
            task.source_context["responsibility"]
            == "implements GameEngine from engine.ts"
        )
        assert task.source_context["contract_file"] == "docs/engine.ts"
        # Description marker (universal fallback, e.g. Planka)
        assert "<!-- MARCUS_CONTRACT_FIRST:" in task.description
        assert "implements GameEngine from engine.ts" in task.description

    def test_non_contract_gap_has_no_marker_or_source_context(self) -> None:
        from src.marcus_mcp.coordinator.outcome_coverage import (
            _build_impl_task_from_gap,
        )

        task = _build_impl_task_from_gap(
            {"name": "Implement collision", "description": "end on collision"}
        )
        assert task is not None
        assert task.responsibility is None
        assert "MARCUS_CONTRACT_FIRST" not in task.description
        assert not (task.source_context or {})

    def test_parse_contract_metadata_recovers_after_field_loss(self) -> None:
        """Simulate the kanban round-trip dropping the native field: metadata
        must still resolve from source_context (Codex P1 scenario)."""
        from dataclasses import replace

        from src.marcus_mcp.coordinator.outcome_coverage import (
            _build_impl_task_from_gap,
        )
        from src.marcus_mcp.tools.task import _parse_contract_metadata

        task = _build_impl_task_from_gap(
            {
                "name": "Implement GameEngine",
                "description": "build the loop",
                "responsibility": "implements GameEngine from engine.ts",
            }
        )
        assert task is not None
        # Drop the SQLite-native field to mimic a non-SQLite provider.
        round_tripped = replace(task, responsibility=None)
        meta = _parse_contract_metadata(round_tripped)
        assert meta["responsibility"] == "implements GameEngine from engine.ts"
