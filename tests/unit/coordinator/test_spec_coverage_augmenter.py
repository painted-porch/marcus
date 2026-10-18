"""Unit tests for ``SpecCoverageAugmenter`` (issue #456 — Stage 4).

Stage 4 moves ``check_spec_coverage`` from the post-safety-check call
site at ``nlp_tools.py:1336-1349`` to a chain augmenter that runs
inside the decomposer.  The behavior change: gap tasks now flow
through ``_infer_smart_dependencies`` and foundation wiring instead
of being appended as orphans (``dependencies=[]``) — this is the
v37 orphan-failure-mode fix.

The augmenter delegates to the existing :func:`check_spec_coverage`
function for behavior preservation; only the call-site location
changes.  Stage 5 will sweep the now-unused ``project_name`` parameter.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(task_id: str, name: str = "Implement Foo") -> Task:
    """Minimal Task for augmenter tests."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description="...",
        status=TaskStatus.TODO,
        priority=Priority.HIGH,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=2.0,
    )


def _make_gap_task(task_id: str, name: str = "Implement Auth") -> Task:
    """Synthesized gap task (matches what check_spec_coverage produces)."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description="Spec coverage check found this feature missing",
        status=TaskStatus.TODO,
        priority=Priority.HIGH,
        labels=["spec_gap"],
        dependencies=[],
        estimated_hours=3.0,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
    )


def _make_prd_analysis(description: str = "build a snake game") -> Any:
    """Minimal PRDAnalysis whose ``original_description`` carries the spec."""
    from src.ai.advanced.prd.advanced_parser import PRDAnalysis

    return PRDAnalysis(
        functional_requirements=[],
        non_functional_requirements=[],
        technical_constraints=[],
        business_objectives=[],
        user_personas=[],
        success_metrics=[],
        implementation_approach="iterative",
        complexity_assessment={},
        risk_factors=[],
        confidence=0.9,
        original_description=description,
    )


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


class TestSpecCoverageAugmenterProtocolSatisfaction:
    """Augmenter satisfies the GraphAugmenter Protocol."""

    def test_module_exports_augmenter(self) -> None:
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        assert SpecCoverageAugmenter is not None

    def test_augmenter_is_graph_augmenter(self) -> None:
        """``isinstance(augmenter, GraphAugmenter)`` returns True."""
        from src.marcus_mcp.coordinator.graph_augmentation import (
            GraphAugmenter,
        )
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        augmenter = SpecCoverageAugmenter()
        assert isinstance(augmenter, GraphAugmenter)

    def test_name_is_spec_coverage(self) -> None:
        """Canonical name keys telemetry / log lines."""
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        augmenter = SpecCoverageAugmenter()
        assert augmenter.name == "spec_coverage"

    def test_name_distinct_from_outcome_coverage(self) -> None:
        """Names must be distinct so chain uniqueness check accepts both.

        Stage 4 registers ``[OutcomeCoverageAugmenter,
        SpecCoverageAugmenter]`` together — their names cannot collide
        or the chain rejects the registration at entry.
        """
        from src.marcus_mcp.coordinator.outcome_coverage_augmenter import (
            OutcomeCoverageAugmenter,
        )
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        oc = OutcomeCoverageAugmenter(llm_client=AsyncMock())
        sc = SpecCoverageAugmenter()
        assert oc.name != sc.name


# ---------------------------------------------------------------------------
# Delegation to check_spec_coverage
# ---------------------------------------------------------------------------


class TestDelegation:
    """Augmenter delegates to ``check_spec_coverage`` with correct args."""

    @pytest.mark.asyncio
    async def test_delegates_with_prd_description(self) -> None:
        """``description`` arg comes from ``prd_analysis.original_description``."""
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_check:
            augmenter = SpecCoverageAugmenter()
            await augmenter.augment(
                prd_analysis=_make_prd_analysis("build a weather app"),
                tasks=[_make_task("t1")],
            )

        mock_check.assert_awaited_once()
        call_kwargs = mock_check.call_args.kwargs
        assert call_kwargs["description"] == "build a weather app"

    @pytest.mark.asyncio
    async def test_delegates_with_input_tasks(self) -> None:
        """``tasks`` arg threads through unchanged."""
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        input_tasks = [_make_task("t1"), _make_task("t2")]

        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_check:
            augmenter = SpecCoverageAugmenter()
            await augmenter.augment(
                prd_analysis=_make_prd_analysis(),
                tasks=input_tasks,
            )

        assert mock_check.call_args.kwargs["tasks"] == input_tasks

    @pytest.mark.asyncio
    async def test_ignores_contract_artifacts(self) -> None:
        """spec_coverage operates on spec text, not contracts.

        ``contract_artifacts`` arg is part of the Protocol surface, but
        spec_coverage doesn't read it — feature parity with the
        post-safety-check call site that never had this parameter.
        """
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_check:
            augmenter = SpecCoverageAugmenter()
            # Pass contract_artifacts; augmenter must not crash, must
            # not forward to check_spec_coverage (which has no such param)
            await augmenter.augment(
                prd_analysis=_make_prd_analysis(),
                tasks=[_make_task("t1")],
                contract_artifacts={"Auth": {"artifacts": []}},
            )

        # check_spec_coverage shouldn't receive contract_artifacts
        assert "contract_artifacts" not in mock_check.call_args.kwargs


# ---------------------------------------------------------------------------
# No-gap behavior (passthrough)
# ---------------------------------------------------------------------------


class TestNoGapPassthrough:
    """When check_spec_coverage returns no gaps, augmenter no-ops."""

    @pytest.mark.asyncio
    async def test_empty_gap_list_passthrough_input_tasks(self) -> None:
        """No gaps → augmented_tasks equals input."""
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        input_tasks = [_make_task("t1"), _make_task("t2")]

        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ):
            augmenter = SpecCoverageAugmenter()
            result = await augmenter.augment(
                prd_analysis=_make_prd_analysis(),
                tasks=input_tasks,
            )

        assert result.augmented_tasks == input_tasks
        assert result.synthesized_ids == []

    @pytest.mark.asyncio
    async def test_empty_gap_list_no_telemetry(self) -> None:
        """No gaps → no telemetry slice in chain namespace.

        Empty telemetry dict so the chain orchestrator omits the
        ``spec_coverage`` key entirely (signals "ran, no data" vs
        "didn't run").
        """
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ):
            augmenter = SpecCoverageAugmenter()
            result = await augmenter.augment(
                prd_analysis=_make_prd_analysis(),
                tasks=[_make_task("t1")],
            )

        assert result.telemetry == {}


# ---------------------------------------------------------------------------
# Gap synthesis path
# ---------------------------------------------------------------------------


class TestGapSynthesis:
    """When check_spec_coverage returns gap tasks, augmenter appends them."""

    @pytest.mark.asyncio
    async def test_gap_tasks_appended_to_input(self) -> None:
        """Gap tasks added at the tail of augmented_tasks."""
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        input_tasks = [_make_task("t1")]
        gap = _make_gap_task("gap_abc", "Implement Auth")

        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=[gap],
        ):
            augmenter = SpecCoverageAugmenter()
            result = await augmenter.augment(
                prd_analysis=_make_prd_analysis(),
                tasks=input_tasks,
            )

        assert result.augmented_tasks == input_tasks + [gap]

    @pytest.mark.asyncio
    async def test_synthesized_ids_lists_gap_ids(self) -> None:
        """``synthesized_ids`` carries IDs of all gap tasks."""
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        gap_a = _make_gap_task("gap_a", "Implement A")
        gap_b = _make_gap_task("gap_b", "Implement B")

        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=[gap_a, gap_b],
        ):
            augmenter = SpecCoverageAugmenter()
            result = await augmenter.augment(
                prd_analysis=_make_prd_analysis(),
                tasks=[_make_task("t1")],
            )

        assert result.synthesized_ids == ["gap_a", "gap_b"]

    @pytest.mark.asyncio
    async def test_telemetry_carries_gap_count(self) -> None:
        """``telemetry["spec_gap_count"]`` reports number of gaps filled."""
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        gaps = [
            _make_gap_task("g1", "Implement Auth"),
            _make_gap_task("g2", "Implement Reports"),
            _make_gap_task("g3", "Implement Search"),
        ]

        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=gaps,
        ):
            augmenter = SpecCoverageAugmenter()
            result = await augmenter.augment(
                prd_analysis=_make_prd_analysis(),
                tasks=[_make_task("t1")],
            )

        assert result.telemetry["spec_gap_count"] == 3

    @pytest.mark.asyncio
    async def test_telemetry_carries_gap_features(self) -> None:
        """``telemetry["spec_gap_features"]`` lists synthesized task names.

        Cato consumers can show "spec_coverage filled gaps for: A, B, C"
        without having to introspect the augmented_tasks list.
        """
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        gaps = [
            _make_gap_task("g1", "Implement Auth"),
            _make_gap_task("g2", "Implement Reports"),
        ]

        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=gaps,
        ):
            augmenter = SpecCoverageAugmenter()
            result = await augmenter.augment(
                prd_analysis=_make_prd_analysis(),
                tasks=[_make_task("t1")],
            )

        assert result.telemetry["spec_gap_features"] == [
            "Implement Auth",
            "Implement Reports",
        ]


# ---------------------------------------------------------------------------
# Empty description handling
# ---------------------------------------------------------------------------


class TestEmptyDescription:
    """When ``prd_analysis.original_description`` is empty, augmenter no-ops.

    ``check_spec_coverage`` would extract zero features from an empty
    spec and return [], but skipping the call avoids a needless LLM
    round-trip.
    """

    @pytest.mark.asyncio
    async def test_empty_description_skips_check(self) -> None:
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_check:
            augmenter = SpecCoverageAugmenter()
            await augmenter.augment(
                prd_analysis=_make_prd_analysis(""),
                tasks=[_make_task("t1")],
            )

        mock_check.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_description_returns_passthrough(self) -> None:
        """Empty description → input tasks returned unchanged."""
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        input_tasks = [_make_task("t1"), _make_task("t2")]
        augmenter = SpecCoverageAugmenter()
        result = await augmenter.augment(
            prd_analysis=_make_prd_analysis(""),
            tasks=input_tasks,
        )

        assert result.augmented_tasks == input_tasks
        assert result.synthesized_ids == []
        assert result.telemetry == {}


# ---------------------------------------------------------------------------
# Stage 4 contract: spec_coverage no longer wired post-safety-check
# ---------------------------------------------------------------------------


class TestStage4PostSafetyCheckCallSiteRemoved:
    """Stage 4 removes the call site at ``nlp_tools.py:1336-1349``.

    Before Stage 4: ``check_spec_coverage`` was called from
    ``nlp_tools.py`` *after* safety checks; gap tasks were appended
    with ``dependencies=[]`` (orphans).

    After Stage 4: the augmenter runs *inside* the decomposer chain,
    so gap tasks flow through ``_infer_smart_dependencies`` and
    foundation wiring like first-class graph members.

    Anti-regression: nlp_tools.py no longer imports or calls
    ``check_spec_coverage`` directly.  The function still exists in
    spec_coverage.py (the augmenter delegates to it).
    """

    def test_check_spec_coverage_function_still_exists(self) -> None:
        """The underlying function is preserved (augmenter delegates to it)."""
        from src.integrations.spec_coverage import check_spec_coverage

        assert callable(check_spec_coverage)

    def test_nlp_tools_no_longer_imports_check_spec_coverage(self) -> None:
        """No import of ``check_spec_coverage`` in ``nlp_tools.py``.

        Once the augmenter chain handles spec coverage, the legacy
        post-safety-check call site is gone.  Catching this in a test
        prevents a future regression that re-adds a parallel call.
        """
        from pathlib import Path

        nlp_tools_path = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "integrations"
            / "nlp_tools.py"
        )
        content = nlp_tools_path.read_text()
        assert "from src.integrations.spec_coverage import check_spec_coverage" not in (
            content
        ), (
            "nlp_tools.py must not import check_spec_coverage post-Stage-4 — "
            "the augmenter chain handles spec coverage now."
        )


# ---------------------------------------------------------------------------
# Prototype-mode skip (bug #649 root cause 4)
# ---------------------------------------------------------------------------


class TestPrototypeModeNoLongerSkips:
    """Issue #666: spec_coverage now runs on EVERY complexity mode.

    The old prototype short-circuit (bug #649 root cause 4) silenced
    spec_coverage on prototype runs to cut a redundant-task/cost problem.
    But a *dropped* outcome (e.g. the snake game's restart) is
    mode-independent, and skipping the whole pass meant genuine gaps were
    never caught — the snake game shipped without restart (#666). The skip
    is removed. ``complexity_mode`` is retained as a no-op for chain
    construction compatibility and no longer affects behavior.
    """

    @pytest.mark.asyncio
    async def test_prototype_mode_runs_spec_coverage(self) -> None:
        """Prototype mode now RUNS the coverage check (no short-circuit)."""
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter.check_spec_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_check:
            augmenter = SpecCoverageAugmenter(complexity_mode="prototype")
            tasks = [_make_task("t1"), _make_task("t2")]
            result = await augmenter.augment(
                prd_analysis=_make_prd_analysis("build a snake game"),
                tasks=tasks,
            )

        # Coverage check now runs even on prototype (no skip).
        mock_check.assert_awaited_once()
        # No gaps found -> passthrough unchanged.
        assert result.augmented_tasks == tasks
        assert result.synthesized_ids == []
        assert result.telemetry == {}

    @pytest.mark.asyncio
    async def test_standard_mode_unchanged_behavior(self) -> None:
        """Non-prototype mode still calls ``check_spec_coverage``.

        Regression guard: only prototype mode short-circuits.  Standard
        and enterprise modes (the previous default) keep the existing
        behavior so the fix is opt-in via complexity_mode.
        """
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_check:
            augmenter = SpecCoverageAugmenter(complexity_mode="standard")
            await augmenter.augment(
                prd_analysis=_make_prd_analysis("build a weather app"),
                tasks=[_make_task("t1")],
            )

        mock_check.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_default_complexity_mode_preserves_legacy_behavior(self) -> None:
        """Construction without ``complexity_mode`` keeps the old default.

        Pre-bug-#649 callers constructed ``SpecCoverageAugmenter()`` with
        no arguments.  The new keyword-only ``complexity_mode`` must
        default to ``None`` and behave identically to standard mode for
        backwards compatibility — only when an explicit ``"prototype"``
        is passed does the short-circuit engage.
        """
        from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
            SpecCoverageAugmenter,
        )

        with patch(
            "src.marcus_mcp.coordinator.spec_coverage_augmenter." "check_spec_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_check:
            augmenter = SpecCoverageAugmenter()  # No complexity_mode
            await augmenter.augment(
                prd_analysis=_make_prd_analysis("build a thing"),
                tasks=[_make_task("t1")],
            )

        mock_check.assert_awaited_once()
