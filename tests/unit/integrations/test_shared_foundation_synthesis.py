"""
Unit tests for pre-fork shared foundation synthesis (GH-355).

Verifies that ``_synthesize_shared_foundation()`` on
``NaturalLanguageProjectCreator``:

- Returns an empty list when the LLM detects no shared foundation needs
  (conservative, no-op path).
- Returns a list of Task objects when the LLM detects foundation needs.
- Returns an empty list on LLM failure or JSON parse failure
  (never crashes the pipeline).
- Includes domain contract context in the prompt when domains are provided.
- Produces Task objects with correct fields (status TODO, priority HIGH,
  labels contain "pre-fork" and "implementation").
- Feature-based path: foundation tasks are prepended and domain tasks
  depend on them.
- Contract-first path: foundation tasks are prepended and domain tasks
  depend on them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_creator() -> Any:
    """Build a ``NaturalLanguageProjectCreator`` with all dependencies mocked.

    Returns
    -------
    Any
        A ``NaturalLanguageProjectCreator`` instance whose ``prd_parser``,
        ``kanban_client``, and ``ai_engine`` are all mocked, so no real
        I/O happens.
    """
    from src.integrations.nlp_tools import NaturalLanguageProjectCreator

    mock_kanban = MagicMock()
    mock_ai_engine = MagicMock()

    creator = NaturalLanguageProjectCreator(
        kanban_client=mock_kanban,
        ai_engine=mock_ai_engine,
    )

    # Replace the real LLM client on prd_parser with a mock
    mock_llm = MagicMock()
    mock_llm.analyze = AsyncMock()
    creator.prd_parser.llm_client = mock_llm

    return creator


def _make_task(task_id: str, name: str) -> Task:
    """Build a minimal Task for dependency-wiring tests.

    Parameters
    ----------
    task_id : str
        Task identifier.
    name : str
        Task name.

    Returns
    -------
    Task
        Minimal Task with empty dependencies list.
    """
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description="Do the thing",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=4.0,
        dependencies=[],
    )


# ---------------------------------------------------------------------------
# _synthesize_shared_foundation — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestSynthesizeSharedFoundation:
    """Tests for ``NaturalLanguageProjectCreator._synthesize_shared_foundation``."""

    async def test_returns_empty_list_when_llm_says_no_shared_needs(self) -> None:
        """When LLM returns no foundation tasks, result is an empty list.

        This is the conservative no-op path: the caller's task list is
        unchanged and no pre-fork tasks are injected.
        """
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {"foundation_tasks": []}
        )

        result = await creator._synthesize_shared_foundation(
            "Build a simple weather dashboard."
        )

        assert result == []

    async def test_returns_tasks_when_shared_needs_detected(self) -> None:
        """When LLM identifies shared foundation needs, Task objects are returned.

        Three targets are supported: design system, shared components, and
        tech foundation.  The LLM may return any subset.
        """
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {
                "foundation_tasks": [
                    {
                        "name": "Shared Foundation: Design System",
                        "description": "Create shared color tokens and typography.",
                        "estimated_hours": 2.0,
                    },
                    {
                        "name": "Shared Foundation: Shared Components",
                        "description": "Build Card and Button components.",
                        "estimated_hours": 3.0,
                    },
                ]
            }
        )

        result = await creator._synthesize_shared_foundation(
            "Build a multi-widget dashboard with clock and weather panels."
        )

        assert len(result) == 2
        assert all(isinstance(t, Task) for t in result)
        names = {t.name for t in result}
        assert "Shared Foundation: Design System" in names
        assert "Shared Foundation: Shared Components" in names

    async def test_acceptance_criteria_propagate_onto_foundation_tasks(
        self,
    ) -> None:
        """#557: foundation tasks must carry the LLM's acceptance_criteria.

        Without criteria, WorkAnalyzer auto-passes the foundation task
        and its subtasks have nothing to be grounded against.
        """
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {
                "foundation_tasks": [
                    {
                        "name": "Shared Foundation: Game State",
                        "description": "Define the shared game state types.",
                        "estimated_hours": 2.0,
                        "acceptance_criteria": [
                            "GameState interface exported with score/grid/status",
                            "resetGame() returns a fresh GameState",
                        ],
                    }
                ]
            }
        )

        result = await creator._synthesize_shared_foundation(
            "Build a snake game with shared game state."
        )

        assert len(result) == 1
        assert result[0].acceptance_criteria == [
            "GameState interface exported with score/grid/status",
            "resetGame() returns a fresh GameState",
        ]

    async def test_missing_acceptance_criteria_defaults_to_empty_list(
        self,
    ) -> None:
        """A foundation task with no acceptance_criteria key gets [] (no crash)."""
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {
                "foundation_tasks": [
                    {
                        "name": "Shared Foundation: Tech Setup",
                        "description": "Configure the build tooling.",
                        "estimated_hours": 1.0,
                    }
                ]
            }
        )

        result = await creator._synthesize_shared_foundation("Build a simple app.")

        assert len(result) == 1
        assert result[0].acceptance_criteria == []

    async def test_foundation_tasks_are_recognized_as_validatable(self) -> None:
        """#557 / Codex P2: foundation tasks must pass should_validate_task.

        Foundation tasks carry acceptance_criteria, but the validation
        gate only runs for tasks should_validate_task accepts. A
        "pre-fork"-only label set is neither implementation nor
        exclusion, so the filter would skip foundation work entirely.
        The "implementation" label makes them (and their subtasks)
        validatable.
        """
        from src.ai.validation.task_filter import should_validate_task

        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {
                "foundation_tasks": [
                    {
                        "name": "Shared Foundation: Game State",
                        "description": "Define shared game state types.",
                        "estimated_hours": 2.0,
                        "acceptance_criteria": ["GameState interface exported"],
                    }
                ]
            }
        )

        result = await creator._synthesize_shared_foundation("Build a snake game.")

        assert len(result) == 1
        task = result[0]
        assert "implementation" in task.labels
        assert "pre-fork" in task.labels
        # The validation gate must accept this task — otherwise its
        # acceptance_criteria are populated but never checked.
        assert should_validate_task(task) is True

    async def test_is_conservative_on_llm_failure(self) -> None:
        """When the LLM call raises, the method returns [] instead of crashing.

        The pre-fork synthesis is a best-effort enhancement. A failure must
        not prevent project creation.
        """
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.side_effect = RuntimeError(
            "LLM unavailable"
        )

        result = await creator._synthesize_shared_foundation(
            "Build a weather dashboard."
        )

        assert result == []

    async def test_is_conservative_on_invalid_json(self) -> None:
        """When the LLM returns unparseable JSON, the method returns [].

        Malformed LLM output is common — never crash the pipeline.
        """
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = (
            "Sorry, I cannot help with that."
        )

        result = await creator._synthesize_shared_foundation(
            "Build a weather dashboard."
        )

        assert result == []

    async def test_is_conservative_on_missing_foundation_tasks_key(self) -> None:
        """When the LLM returns JSON without ``foundation_tasks`` key, return [].

        Unexpected but valid JSON should degrade gracefully.
        """
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {"tasks": []}  # wrong key
        )

        result = await creator._synthesize_shared_foundation(
            "Build a weather dashboard."
        )

        assert result == []

    async def test_foundation_tasks_have_correct_structure(self) -> None:
        """Each returned Task has the expected fields set for a pre-fork task.

        - status: TODO (not yet started)
        - priority: HIGH (blocking downstream domain tasks)
        - labels: contains "pre-fork" and "implementation"
        - assigned_to: None (self-assigned by whichever agent picks it first)
        - estimated_hours: matches LLM value
        """
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {
                "foundation_tasks": [
                    {
                        "name": "Shared Foundation: Design System",
                        "description": "Create shared tokens.",
                        "estimated_hours": 2.5,
                    }
                ]
            }
        )

        result = await creator._synthesize_shared_foundation("Build a dashboard.")

        assert len(result) == 1
        task = result[0]
        assert task.status == TaskStatus.TODO
        assert task.priority == Priority.HIGH
        assert task.assigned_to is None
        # Foundation tasks are real implementation work — not design ghosts.
        # Only "pre-fork" tag used; source_type is the internal marker.
        assert "pre-fork" in task.labels
        assert "design" not in task.labels
        assert task.estimated_hours == 2.5
        # Description contains the original text plus the workflow reminder.
        assert "Create shared tokens" in task.description
        assert "log_artifact" in task.description
        # ID must be a non-empty string
        assert isinstance(task.id, str) and len(task.id) > 0

    async def test_domain_context_included_in_prompt_when_provided(self) -> None:
        """When domains are provided, the LLM prompt includes domain contract text.

        Contract-first synthesis has higher fidelity than feature-based because
        domain contracts are available.  This test checks that the prompt the
        LLM actually receives contains the domain name.
        """
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {"foundation_tasks": []}
        )

        domains: Dict[str, Any] = {
            "WeatherDomain": {
                "artifacts": [
                    {"content": "interface WeatherService { getTemp(): number }"}
                ]
            }
        }

        await creator._synthesize_shared_foundation(
            "Build a dashboard.", domains=domains
        )

        # Verify the LLM was called with a prompt that mentions the domain
        call_args = creator.prd_parser.llm_client.analyze.call_args
        prompt_text: str = call_args.kwargs.get("prompt") or call_args.args[0]
        assert "WeatherDomain" in prompt_text

    async def test_prompt_omits_domain_section_when_no_domains(self) -> None:
        """When no domains are given (feature_based path), the prompt still works.

        The feature_based path runs on PRD text only (lower fidelity) so the
        prompt should not contain a domain-contract section.
        """
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {"foundation_tasks": []}
        )

        await creator._synthesize_shared_foundation(
            "Build a weather dashboard.", domains=None
        )

        call_args = creator.prd_parser.llm_client.analyze.call_args
        prompt_text: str = call_args.kwargs.get("prompt") or call_args.args[0]
        # Prompt must still contain the PRD description
        assert "weather dashboard" in prompt_text.lower()

    async def test_foundation_task_missing_estimated_hours_uses_default(self) -> None:
        """If LLM omits ``estimated_hours``, a sensible default is used.

        Defensive handling for partial LLM responses.
        """
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {
                "foundation_tasks": [
                    {
                        "name": "Shared Foundation: Tech Setup",
                        "description": "Configure Vite + TypeScript.",
                        # no estimated_hours key
                    }
                ]
            }
        )

        result = await creator._synthesize_shared_foundation("Build a dashboard.")

        assert len(result) == 1
        assert result[0].estimated_hours > 0  # some positive default


# ---------------------------------------------------------------------------
# Public API surface reminder tests (issue #446)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestPublicApiSurfaceReminder:
    """Foundation task descriptions must instruct the agent to log a
    Public API surface decision so downstream consumers can discover it.

    Issue #446 / Kaia review checkpoint #2.  v80 audit showed downstream
    agents inventing import paths (``tokens.json`` vs actual
    ``tokens.css``) because foundation agents had no canonical
    structured way to publish their public API.  ``log_decision`` flow
    already reaches dependent tasks via ``Context.get_context``
    (``core/context.py:334-346``) — these tests pin that the foundation
    task description requires the agent to use it.

    Bright-line check: Marcus says "produce a coordination contract."
    Agent picks the contract shape (paths, names, organization).  Two
    foundation agents produce different contracts and both are valid.
    Coordination, not control.
    """

    async def _foundation_task(self) -> Any:
        """Helper: drive ``_synthesize_shared_foundation`` once and
        return the first generated Task."""
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {
                "foundation_tasks": [
                    {
                        "name": "Design System Setup",
                        "description": "Create shared design tokens.",
                        "estimated_hours": 2.0,
                    }
                ]
            }
        )
        result = await creator._synthesize_shared_foundation("Build a dashboard.")
        assert len(result) == 1
        return result[0]

    async def test_description_includes_log_decision_instruction(self) -> None:
        """Foundation task description must instruct ``log_decision`` use."""
        task = await self._foundation_task()
        assert "log_decision" in task.description

    async def test_description_uses_public_api_surface_title(self) -> None:
        """Description names the canonical decision title for downstream
        consumers to grep / parse.

        Locking the title (``Public API surface``) so a future
        prompt edit doesn't drift to ``Public API``, ``API surface``,
        or any other variant — keeping a stable identifier across
        runs lets Cato / Epictetus measure compliance reliably.
        """
        task = await self._foundation_task()
        assert "Public API surface" in task.description

    async def test_description_lists_required_decision_fields(self) -> None:
        """Description names the four expected decision payload fields.

        Without an explicit field list, agents log free-text decisions
        of varying quality.  These four fields are the minimum
        downstream consumers need:

        - import paths (where to find the artifact)
        - exported symbols (what's importable)
        - config keys (any env / build-tool config consumers must set)
        - usage constraints (preconditions, ordering, etc.)
        """
        task = await self._foundation_task()
        for required_field in (
            "import",
            "symbol",
            "config",
            "constraint",
        ):
            assert required_field in task.description.lower(), (
                f"Foundation task description missing required field reference: "
                f"{required_field!r}"
            )

    async def test_description_explains_skip_consequence(self) -> None:
        """Description must explain why skipping the decision matters.

        v80 audit evidence: downstream agent invented ``tokens.json``
        because no canonical path existed.  Description must motivate
        the agent to actually log the decision — explicit consequence
        framing > polite request.
        """
        task = await self._foundation_task()
        # Loose: must mention either downstream / consumer + invent /
        # miss / wrong / discover so the consequence framing is real.
        desc_lower = task.description.lower()
        consequence_signals = ["downstream", "consumer", "discover"]
        has_audience = any(signal in desc_lower for signal in consequence_signals)
        risk_signals = ["invent", "miss", "wrong", "guess"]
        has_risk = any(signal in desc_lower for signal in risk_signals)
        assert has_audience and has_risk, (
            f"Foundation task description must explain WHO is affected "
            f"(downstream/consumer) AND the RISK if decision is skipped "
            f"(invent/miss/guess paths).  Got: {task.description}"
        )

    async def test_log_artifact_reminder_still_present(self) -> None:
        """Anti-regression: the existing ``log_artifact`` reminder must
        not be removed when adding the ``log_decision`` instruction.

        Both are coordination workflow steps — artifacts track files,
        decisions track structured public-API metadata.  Foundation
        agents should do both.
        """
        task = await self._foundation_task()
        assert "log_artifact" in task.description

    async def test_two_foundation_tasks_both_get_reminder(self) -> None:
        """When LLM returns multiple foundation tasks, every one gets
        the public-API reminder appended.  No drift across tasks."""
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {
                "foundation_tasks": [
                    {
                        "name": "Design System",
                        "description": "Create design tokens.",
                        "estimated_hours": 2.0,
                    },
                    {
                        "name": "Tech Foundation",
                        "description": "Configure TypeScript and build tools.",
                        "estimated_hours": 1.5,
                    },
                ]
            }
        )
        result = await creator._synthesize_shared_foundation("Build a dashboard.")
        assert len(result) == 2
        for task in result:
            assert "log_decision" in task.description
            assert "Public API surface" in task.description
            assert "log_artifact" in task.description


# ---------------------------------------------------------------------------
# Conceptual-domain deduplication rule tests (issue #463)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestConceptualDomainDeduplicationRule:
    """The synthesis prompt must instruct the LLM to merge same-domain
    foundation candidates.

    Issue #463 / Kaia review checkpoint #2 — corrected design.  v38
    audit case (kanban verification): two ``pre_fork_synthesis`` tasks
    (``Game State Data Structure Contract`` and
    ``State Update Event/Message Protocol``) targeted the same
    conceptual domain (game state) and were emitted as parallel
    foundation tasks.  Agent A1 shipped ~530 LOC against the first
    contract, then deleted it during integration verification because
    A2's parallel work made A1's orphaned.  Worker isolation prevented
    detection at agent time.

    Root cause: a single LLM call produced both tasks and didn't
    deduplicate within its own response.  The existing prompt has a
    "Be CONSERVATIVE" rule about WHETHER to emit foundation tasks at
    all, but no rule about MERGING same-domain candidates.

    Fix (Variant B, preventive prompt edit): add a deduplication rule
    to the prompt asking the LLM to merge same-conceptual-domain
    candidates before returning.  Cheaper and smaller than a reactive
    LLM-judge pass; escalates to reactive only if observability shows
    the prompt edit insufficient.

    Bright-line check: Marcus shapes the LLM call to avoid duplicates
    — same authority Marcus already exercises by structuring the
    synthesis prompt's three categories.  Coordination ✓.
    """

    async def _capture_prompt(self) -> str:
        """Drive ``_synthesize_shared_foundation`` once, return the
        prompt text the LLM client received.  Mirrors the capture
        pattern in ``test_domain_context_included_in_prompt_when_provided``.
        """
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {"foundation_tasks": []}
        )
        await creator._synthesize_shared_foundation(
            "Build a snake game with state, rendering, and protocol."
        )
        call_args = creator.prd_parser.llm_client.analyze.call_args
        return call_args.kwargs.get("prompt") or call_args.args[0]

    async def test_prompt_includes_dedup_action_word(self) -> None:
        """Prompt must instruct the LLM to ``merge`` overlapping candidates.

        Locking the action word so a future prompt edit can't drift to
        a vague \"reconsider\" or \"check\" that doesn't mandate a
        resolution.  v38 reproduction: two same-domain tasks shipped
        because the prompt didn't mandate merging.
        """
        prompt_text = await self._capture_prompt()
        assert "merge" in prompt_text.lower()

    async def test_prompt_names_conceptual_domain_as_overlap_signal(
        self,
    ) -> None:
        """Prompt must use the phrase ``conceptual domain`` so the
        LLM understands the merge criterion.

        Locks a stable phrase the prompt uses to identify overlap.
        """
        prompt_text = await self._capture_prompt()
        assert "conceptual domain" in prompt_text.lower()

    async def test_prompt_includes_concrete_overlap_example(self) -> None:
        """Prompt must give the LLM a concrete example of the failure
        mode it should prevent (v38: state-related candidate pair).

        Without an example, the LLM may apply the rule too loosely
        (legitimate parallel work) or too tightly (different domains
        with shared substrings).  The example anchors the LLM to the
        specific failure shape.
        """
        prompt_text = await self._capture_prompt()
        # Loose match: must reference state-shaped or contract-shaped
        # examples so the LLM sees the failure mode it's catching.
        prompt_lower = prompt_text.lower()
        assert (
            "state" in prompt_lower or "contract" in prompt_lower
        ), "Dedup rule must include a concrete example of the failure mode"

    async def test_prompt_includes_negative_overlap_example(self) -> None:
        """Prompt must give the LLM a maximally-different negative
        example so it knows what's NOT same-domain.

        Kaia review checkpoint #3 follow-up: a single positive example
        invites over-merging — the LLM may collapse legitimate parallel
        candidates that share substrings or surface concerns.  The
        negative example anchors the boundary at maximally-different
        domains (backend data infra vs frontend visual design) so the
        LLM has a clear signal of where merging stops.

        ``Database connection pool`` and ``Theme tokens`` were chosen
        precisely because their consumer sets DO NOT overlap (no agent
        reaches for both at the same point), avoiding the borderline
        Theme-tokens-vs-Component-library trap where consumers can
        legitimately consume both.
        """
        prompt_text = await self._capture_prompt()
        prompt_lower = prompt_text.lower()
        assert "database connection pool" in prompt_lower, (
            "Dedup rule must include a maximally-different negative example "
            "(backend infra) so the LLM knows what's NOT same-domain"
        )
        assert "theme tokens" in prompt_lower, (
            "Dedup rule must pair the negative example with frontend "
            "visual concern so the LLM sees both poles"
        )
        # Loose: instruction-side check that "do not merge" framing
        # is present, distinct from the merge instruction above.
        assert "do not merge" in prompt_lower or "not merge" in prompt_lower

    async def test_existing_conservative_rule_still_present(self) -> None:
        """Anti-regression: the existing ``Be CONSERVATIVE`` instruction
        must remain.  It biases against over-creating tasks; the new
        dedup rule biases against double-creating tasks.  The two work
        together — removing the conservative rule would invite the LLM
        to over-emit even after deduplicating.
        """
        prompt_text = await self._capture_prompt()
        assert "CONSERVATIVE" in prompt_text

    async def test_dedup_rule_appears_after_conservative_rule(
        self,
    ) -> None:
        """Ordering check: dedup rule comes after the conservative
        rule in the prompt.

        Both are quality rules; their order signals priority.
        \"Decide whether to emit any tasks at all\" comes first
        (conservative); \"if you do emit, deduplicate\" comes second.
        Locking the order keeps the LLM's reasoning chain consistent
        across runs.
        """
        prompt_text = await self._capture_prompt()
        conservative_pos = prompt_text.find("CONSERVATIVE")
        merge_pos = prompt_text.lower().find("merge")
        assert conservative_pos != -1, "Conservative rule missing"
        assert merge_pos != -1, "Merge instruction missing"
        assert (
            conservative_pos < merge_pos
        ), "CONSERVATIVE rule must come before merge instruction"


# ---------------------------------------------------------------------------
# Feature-based wiring tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestFeatureBasedFoundationWiring:
    """Foundation tasks are prepended and domain tasks depend on them.

    These tests patch ``_synthesize_shared_foundation`` and
    ``parse_prd_to_tasks`` so we can verify only the wiring logic in
    ``process_natural_language`` without running real LLM calls.
    """

    async def test_foundation_tasks_prepended_before_domain_tasks(self) -> None:
        """When synthesis returns tasks, they appear first in the task list."""
        from src.integrations.nlp_tools import NaturalLanguageProjectCreator

        creator = _make_creator()

        now = datetime.now(timezone.utc)
        foundation_task = _make_task("foundation-1", "Shared Foundation: Design System")
        foundation_task.labels = ["foundation", "pre-fork"]

        domain_task_a = _make_task("domain-a", "Build WeatherWidget")
        domain_task_b = _make_task("domain-b", "Build ClockWidget")

        # Stub synthesis
        async def fake_synthesis(description: str, domains: Any = None) -> List[Task]:
            return [foundation_task]

        # Stub prd_parser.parse_prd_to_tasks
        from src.ai.advanced.prd.advanced_parser import TaskGenerationResult

        mock_result = Mock(spec=TaskGenerationResult)
        mock_result.tasks = [domain_task_a, domain_task_b]
        mock_result.dependencies = []
        # Phase 5 (#449): orchestrator reads these before emitting
        # the planning-intent-fidelity event.  Score=None triggers
        # the no-op path in the helper.
        mock_result.intent_fidelity_score = None
        mock_result.coverage_before_fill = {}
        mock_result.coverage_after_fill = None
        mock_result.gap_filled_outcomes = []
        # Phase 0 (#546): the feature-based path reads these off the
        # TaskGenerationResult to stash project classification.
        mock_result.domain = "unknown"
        mock_result.structural_category = "unknown"
        mock_result.detected_tech_stack = []

        creator._synthesize_shared_foundation = fake_synthesis  # type: ignore[method-assign]
        creator.prd_parser.parse_prd_to_tasks = AsyncMock(return_value=mock_result)

        # Stub board_analyzer and context_detector to avoid real I/O
        creator.board_analyzer.analyze_board = AsyncMock()
        from unittest.mock import MagicMock

        mock_ctx = MagicMock()
        from src.detection.context_detector import MarcusMode

        mock_ctx.recommended_mode = MarcusMode.CREATOR
        creator.context_detector.detect_optimal_mode = AsyncMock(return_value=mock_ctx)

        tasks = await creator.process_natural_language(
            "Build a dashboard", project_name="Test"
        )

        assert tasks[0].id == "foundation-1"
        assert tasks[1].id == "domain-a"
        assert tasks[2].id == "domain-b"

    async def test_domain_tasks_depend_on_foundation_tasks(self) -> None:
        """When foundation tasks exist, all domain tasks list them as dependencies."""
        from src.integrations.nlp_tools import NaturalLanguageProjectCreator

        creator = _make_creator()

        foundation_task = _make_task("foundation-1", "Shared Foundation: Design System")
        foundation_task.labels = ["foundation", "pre-fork"]

        domain_task_a = _make_task("domain-a", "Build WeatherWidget")
        domain_task_b = _make_task("domain-b", "Build ClockWidget")

        async def fake_synthesis(description: str, domains: Any = None) -> List[Task]:
            return [foundation_task]

        from src.ai.advanced.prd.advanced_parser import TaskGenerationResult

        mock_result = Mock(spec=TaskGenerationResult)
        mock_result.tasks = [domain_task_a, domain_task_b]
        mock_result.dependencies = []
        # Phase 5 (#449): orchestrator reads these before emitting
        # the planning-intent-fidelity event.  Score=None triggers
        # the no-op path in the helper.
        mock_result.intent_fidelity_score = None
        mock_result.coverage_before_fill = {}
        mock_result.coverage_after_fill = None
        mock_result.gap_filled_outcomes = []
        # Phase 0 (#546): the feature-based path reads these off the
        # TaskGenerationResult to stash project classification.
        mock_result.domain = "unknown"
        mock_result.structural_category = "unknown"
        mock_result.detected_tech_stack = []

        creator._synthesize_shared_foundation = fake_synthesis  # type: ignore[method-assign]
        creator.prd_parser.parse_prd_to_tasks = AsyncMock(return_value=mock_result)

        creator.board_analyzer.analyze_board = AsyncMock()
        mock_ctx = MagicMock()
        from src.detection.context_detector import MarcusMode

        mock_ctx.recommended_mode = MarcusMode.CREATOR
        creator.context_detector.detect_optimal_mode = AsyncMock(return_value=mock_ctx)

        tasks = await creator.process_natural_language(
            "Build a dashboard", project_name="Test"
        )

        # domain tasks must have foundation-1 as a dependency
        domain_tasks = [t for t in tasks if t.id != "foundation-1"]
        for dt in domain_tasks:
            assert (
                "foundation-1" in dt.dependencies
            ), f"Task '{dt.name}' missing dependency on foundation-1"

    async def test_no_foundation_tasks_leaves_domain_tasks_unchanged(self) -> None:
        """When synthesis returns [], domain tasks have no injected dependencies."""
        from src.integrations.nlp_tools import NaturalLanguageProjectCreator

        creator = _make_creator()

        domain_task_a = _make_task("domain-a", "Build WeatherWidget")
        domain_task_b = _make_task("domain-b", "Build ClockWidget")

        async def fake_synthesis(description: str, domains: Any = None) -> List[Task]:
            return []

        from src.ai.advanced.prd.advanced_parser import TaskGenerationResult

        mock_result = Mock(spec=TaskGenerationResult)
        mock_result.tasks = [domain_task_a, domain_task_b]
        mock_result.dependencies = []
        # Phase 5 (#449): orchestrator reads these before emitting
        # the planning-intent-fidelity event.  Score=None triggers
        # the no-op path in the helper.
        mock_result.intent_fidelity_score = None
        mock_result.coverage_before_fill = {}
        mock_result.coverage_after_fill = None
        mock_result.gap_filled_outcomes = []
        # Phase 0 (#546): the feature-based path reads these off the
        # TaskGenerationResult to stash project classification.
        mock_result.domain = "unknown"
        mock_result.structural_category = "unknown"
        mock_result.detected_tech_stack = []

        creator._synthesize_shared_foundation = fake_synthesis  # type: ignore[method-assign]
        creator.prd_parser.parse_prd_to_tasks = AsyncMock(return_value=mock_result)

        creator.board_analyzer.analyze_board = AsyncMock()
        mock_ctx = MagicMock()
        from src.detection.context_detector import MarcusMode

        mock_ctx.recommended_mode = MarcusMode.CREATOR
        creator.context_detector.detect_optimal_mode = AsyncMock(return_value=mock_ctx)

        tasks = await creator.process_natural_language(
            "Build a dashboard", project_name="Test"
        )

        # No foundation injected — task list unchanged, dependencies empty
        assert len(tasks) == 2
        for t in tasks:
            assert t.dependencies == []


class TestArtifactScopeReminder:
    """The artifact reminder must scope log_artifact to reference outputs.

    "Call log_artifact for every file you produce" was wrong on three
    counts: git is already the file channel (an artifact copy of a
    source file goes stale the moment the real one changes — two
    sources of truth, the v80 failure class through a different door);
    artifacts are the coordination channel, so logging every file
    buries the one contract that matters under implementation blobs;
    and every artifact inflates downstream get_task_context payloads —
    coordination tax purchasing nothing git doesn't provide. Artifacts
    are for interface-bearing outputs consumed as references.
    """

    async def _foundation_task(self) -> Any:
        """Helper: drive ``_synthesize_shared_foundation`` once."""
        creator = _make_creator()
        creator.prd_parser.llm_client.analyze.return_value = json.dumps(
            {
                "foundation_tasks": [
                    {
                        "name": "Design System Setup",
                        "description": "Create shared design tokens.",
                        "estimated_hours": 2.0,
                    }
                ]
            }
        )
        result = await creator._synthesize_shared_foundation("Build a dashboard.")
        assert len(result) == 1
        return result[0]

    async def test_every_file_phrasing_removed(self) -> None:
        """The 'every file you produce' instruction must be gone."""
        task = await self._foundation_task()
        assert "every file" not in task.description.lower()

    async def test_artifacts_scoped_to_reference_outputs(self) -> None:
        """log_artifact is scoped to interface-bearing reference outputs."""
        desc = (await self._foundation_task()).description.lower()
        assert "contracts" in desc and "schemas" in desc

    async def test_source_files_travel_via_git(self) -> None:
        """The description says source files are discovered via git."""
        desc = (await self._foundation_task()).description.lower()
        assert "do not log source files" in desc
        assert "git" in desc
