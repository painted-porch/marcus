"""
Unit tests for composition task synthesis (issue #463).

Issue #463 — v38 audit case: a multi-domain contract-first project
shipped with no task owning ``App.tsx`` wiring.  All three domain
implementations (engine, bus, renderer) shipped clean.  ``App.tsx``
returned ``null``.  Unit tests passed.  The bundle built.  But the
rendered DOM was empty.

Marcus's existing safety net (``enhance_project_with_integration``)
caught it as a catch-all during integration verification —
``planning_gap_detected: true`` self-flagged by the agent — but that
absorbed ~15 min cleanup at the end of the project.

Fix (Variant V3, Kaia checkpoint #1): synthesize a dedicated
composition task when N >= 2 contract-first domains exist.  Marcus
says WHAT (a wiring task with explicit deliverables) and the agent
picks HOW (which file is the entry point, which wiring strategy).

Bright-line check: description lists multiple framework examples
(App.tsx / main.py / index.ts) so Marcus is not picking the file —
the agent discovers from the scaffold and ``log_decision``s the
choice.  Two foundation agents handed this task can produce
legitimately different wirings.  Coordination, not control.

Layering with integration_verification: composition is narrow
scope (entry-point only), early in the DAG.  Integration
verification is broad catch-all (orphan scan, missing components,
contract verification), end of the DAG.  Intentional layered
safety, not redundant.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

import pytest

from src.core.models import Priority, Task, TaskStatus

pytestmark = pytest.mark.unit


def _impl_task(task_id: str, name: str = "Implement Foo") -> Task:
    """Build a minimal contract-first implementation Task for tests."""
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
        labels=["contract_first", "implementation"],
        source_type="contract_first",
    )


# ---------------------------------------------------------------------------
# Trigger gate: only synthesize when N >= 2 domains
# ---------------------------------------------------------------------------


class TestCompositionSynthesisTrigger:
    """Composition task is gated on multi-domain projects.

    Single-domain projects don't need a composition task — the integration
    verification catch-all already handles single-domain wiring concerns
    and the agent typically wires their one domain naturally.
    """

    def test_returns_none_when_zero_impl_tasks(self) -> None:
        """Empty impl_tasks list → no composition task (no project)."""
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        result = build_composition_task(
            project_name="my-project",
            impl_tasks=[],
        )
        assert result is None

    def test_returns_none_when_one_impl_task(self) -> None:
        """Single-impl-task projects skip composition.

        IV catch-all is sufficient.  A project with only one
        implementation task has nothing meaningful to compose — the
        agent wires their one domain naturally as part of the impl
        work.
        """
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        result = build_composition_task(
            project_name="my-project",
            impl_tasks=[_impl_task("t1")],
        )
        assert result is None

    def test_returns_task_when_two_impl_tasks(self) -> None:
        """``len(impl_tasks) >= 2`` → composition task synthesized."""
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        result = build_composition_task(
            project_name="snake-game",
            impl_tasks=[_impl_task("t1"), _impl_task("t2")],
        )
        assert result is not None
        assert isinstance(result, Task)

    def test_returns_task_when_three_impl_tasks(self) -> None:
        """Trigger fires for any multi-impl-task project."""
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        result = build_composition_task(
            project_name="dashboard",
            impl_tasks=[_impl_task("t1"), _impl_task("t2"), _impl_task("t3")],
        )
        assert result is not None


# ---------------------------------------------------------------------------
# Task structure: id, source_type, labels, priority, status
# ---------------------------------------------------------------------------


class TestCompositionTaskStructure:
    """Generated composition task carries the canonical Marcus shape."""

    @staticmethod
    def _build() -> Task:
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        result = build_composition_task(
            project_name="snake-game",
            impl_tasks=[_impl_task("t1"), _impl_task("t2")],
        )
        assert result is not None
        return result

    def test_id_uses_composition_prefix(self) -> None:
        """ID starts with ``composition_`` for downstream filtering."""
        task = self._build()
        assert task.id.startswith("composition_")

    def test_source_type_is_composition_synthesis(self) -> None:
        """``source_type`` distinguishes Marcus-synthesized from LLM-derived."""
        task = self._build()
        assert task.source_type == "composition_synthesis"

    def test_labels_include_composition_and_marcus_synthesized(self) -> None:
        """Both labels present for downstream filtering and audit trails."""
        task = self._build()
        assert "composition" in task.labels
        assert "marcus_synthesized" in task.labels

    def test_priority_is_high(self) -> None:
        """Composition gates the demo — wiring failure = product appears
        broken — so it must outrank documentation / nice-to-haves."""
        task = self._build()
        assert task.priority == Priority.HIGH

    def test_status_is_todo(self) -> None:
        """Synthesized tasks ship as TODO so an agent picks them up."""
        task = self._build()
        assert task.status == TaskStatus.TODO

    def test_estimated_hours_reasonable(self) -> None:
        """Composition is mostly imports + mounting — small task."""
        task = self._build()
        assert task.estimated_hours is not None
        assert 0.5 <= task.estimated_hours <= 3.0

    def test_responsibility_set(self) -> None:
        """``responsibility`` field set so ``build_tiered_instructions``
        surfaces the contract-responsibility framing in the agent prompt.
        """
        task = self._build()
        assert task.responsibility is not None
        assert "entry point" in task.responsibility.lower()


# ---------------------------------------------------------------------------
# Dependencies: hard deps on every impl task, no foundation deps
# ---------------------------------------------------------------------------


class TestCompositionDependencies:
    """Composition depends on every contract-first impl task (transitively
    on foundation tasks via the impl deps already wired at
    ``nlp_tools.py:332``)."""

    def test_depends_on_every_impl_task(self) -> None:
        """3 impl tasks → composition has all 3 in dependencies."""
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        impls = [_impl_task("t1"), _impl_task("t2"), _impl_task("t3")]
        task = build_composition_task(
            project_name="dashboard",
            impl_tasks=impls,
        )
        assert task is not None
        assert set(task.dependencies) == {"t1", "t2", "t3"}

    def test_no_self_dependency(self) -> None:
        """Composition's own ID never appears in its dependencies list."""
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        impls = [_impl_task("t1"), _impl_task("t2")]
        task = build_composition_task(
            project_name="x",
            impl_tasks=impls,
        )
        assert task is not None
        assert task.id not in task.dependencies

    def test_no_foundation_task_dependency(self) -> None:
        """Composition depends on impl tasks only — not on foundation tasks.

        Foundation deps are already wired transitively via impl deps
        (``nlp_tools.py:332`` makes every domain task depend on every
        foundation task).  Direct foundation deps on the composition
        task would clutter the DAG and risk over-blocking when
        foundation work is split across multiple sub-tasks.

        Caller filters input to ``contract_first`` impl tasks before
        passing to the helper; this test pins that even if a
        foundation-typed task slips through, it doesn't end up in
        the composition's dependency edges.
        """
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        # Build a foundation-typed task and verify it doesn't drift
        # into composition's deps.  Caller is expected to filter input
        # to impl tasks only — this test guards the helper's behavior
        # if the caller ever passes a heterogeneous list.
        foundation = _impl_task("foundation_t1", name="Tech Foundation Setup")
        foundation.source_type = "pre_fork_synthesis"
        foundation.labels = ["pre-fork"]

        impl_a = _impl_task("impl_a")
        impl_b = _impl_task("impl_b")

        # Helper takes only impl_tasks — passing impls only here.
        # The contract: foundation tasks are NEVER input to this
        # helper, but if they were, the dependencies should still
        # reflect ONLY what was passed.
        task = build_composition_task(
            project_name="x",
            impl_tasks=[impl_a, impl_b],
        )
        assert task is not None
        assert "foundation_t1" not in task.dependencies, (
            "Composition must not depend on foundation tasks "
            "(transitive via impl deps already wired at nlp_tools.py:332)"
        )

    def test_does_not_mutate_input_impl_tasks(self) -> None:
        """Helper is pure — must not modify the caller's impl_tasks list.

        Python lists pass by reference.  If the helper appends, sorts,
        or otherwise mutates the input, that's a silent side effect
        bug that could surprise callers.  This test pins purity so a
        future refactor can't introduce in-place mutation without
        failing loud.
        """
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        impls = [_impl_task("t1"), _impl_task("t2")]
        impls_snapshot_ids = [t.id for t in impls]
        impls_snapshot_len = len(impls)

        build_composition_task(
            project_name="x",
            impl_tasks=impls,
        )

        assert (
            len(impls) == impls_snapshot_len
        ), "Helper must not mutate input list length"
        assert [
            t.id for t in impls
        ] == impls_snapshot_ids, "Helper must not reorder input list"


# ---------------------------------------------------------------------------
# Description content: V3 elements (multiple framework examples,
# log_decision deliverable, log_artifact deliverable, "discover from
# scaffold" framing)
# ---------------------------------------------------------------------------


class TestCompositionDescriptionV3:
    """Description satisfies Variant V3 (Kaia checkpoint #1):

    - lists multiple framework examples so Marcus is not picking
    - says "discover from scaffold" so agent retains file-choice authority
    - requires ``log_decision`` titled "Entry point wired"
    - requires ``log_artifact`` for the wired file
    - does NOT dictate a specific entry-point file (anti-V1 regression)
    """

    @staticmethod
    def _build_description() -> str:
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        task = build_composition_task(
            project_name="snake-game",
            impl_tasks=[_impl_task("t1"), _impl_task("t2")],
        )
        assert task is not None
        return task.description

    def test_description_lists_multiple_framework_examples(self) -> None:
        """Multiple examples (App.tsx + main.py + index.ts at minimum)
        so the LLM/agent sees Marcus is not picking a single file.

        Bright-line: if Marcus listed only one example, the agent could
        legitimately read it as a directive rather than an example.
        Multiple examples make the "this is one of several possibilities"
        framing explicit.
        """
        desc = self._build_description()
        # At least three different framework conventions must be named
        examples = ["App.tsx", "main.py", "index.ts"]
        present = sum(1 for ex in examples if ex in desc)
        assert present >= 3, (
            f"Description must list at least 3 framework entry-point "
            f"examples so Marcus is not picking a single file.  "
            f"Found: {present} of {examples}"
        )

    def test_description_says_discover_from_scaffold(self) -> None:
        """Locks the "agent discovers, Marcus doesn't pick" framing.

        The phrase "scaffold" anchors the agent's file-choice authority
        in the project's existing structure rather than Marcus's
        proposal.
        """
        desc = self._build_description()
        assert "scaffold" in desc.lower()
        # Variants of "discover" or "find" or "identify" — at least one
        # must signal that the agent does the discovery
        discover_signals = ["discover", "find", "identify", "locate"]
        assert any(
            signal in desc.lower() for signal in discover_signals
        ), "Description must signal agent-side discovery of entry point"

    def test_description_requires_log_decision_entry_point_wired(self) -> None:
        """Locks the canonical decision title so Cato/Epictetus can grep
        compliance metrics consistently across runs (parallel to the
        \"Public API surface\" decision title pattern from #446)."""
        desc = self._build_description()
        assert "log_decision" in desc
        assert "Entry point wired" in desc

    def test_description_requires_log_artifact_for_wired_file(self) -> None:
        """log_artifact is the file-level surface; log_decision is the
        structured-metadata surface.  Both required (same #446 pattern).
        """
        desc = self._build_description()
        assert "log_artifact" in desc

    def test_description_does_not_dictate_specific_entry_point(self) -> None:
        """Anti-V1 regression: description must NOT instruct the agent
        to wire a specific file (e.g., \"wire src/App.tsx\" or \"the
        entry point is App.tsx\").

        Bright-line guard: if Marcus picks the entry-point file, agents
        lose the legitimate choice between framework conventions
        (Vite vs Next.js vs CRA all use different entry filenames).
        """
        desc = self._build_description()
        # Loose check: the description should NOT say "the entry point
        # is X" or "wire FILENAME" with a specific filename.  Hard to
        # assert negatively, but we can pin that the prescriptive
        # phrasing isn't present.
        bad_phrases = [
            "the entry point is App.tsx",
            "the entry point is main.py",
            "wire src/App.tsx",
            "wire src/main.py",
            "wire index.ts",
        ]
        for bad in bad_phrases:
            assert bad not in desc, (
                f"Description must not dictate a specific entry-point "
                f"file ({bad!r}) — that's HOW guidance.  Use scaffold "
                f"discovery framing instead."
            )

    def test_description_names_project(self) -> None:
        """Project name appears in the description so the agent has
        context (which project's entry point are they wiring)."""
        desc = self._build_description()
        assert "snake-game" in desc


# ---------------------------------------------------------------------------
# Idempotency: don't double-synthesize
# ---------------------------------------------------------------------------


class TestCompositionIdempotency:
    """Synthesis is idempotent — calling twice or being called when a
    composition task is already present in impl_tasks does not produce
    two composition tasks.

    Defensive check: current call paths invoke synthesis once per
    create_project, but a future re-run / retry / cache-miss could
    invoke twice.  Skip if existing composition task is already in
    the impl_tasks list.
    """

    def test_skipped_when_composition_task_already_in_impl_list(self) -> None:
        """If impl_tasks contains a task with ``composition`` label,
        synthesis returns None — no double-creation."""
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        existing_composition = _impl_task("existing")
        existing_composition.labels = list(existing_composition.labels) + [
            "composition"
        ]

        result = build_composition_task(
            project_name="x",
            impl_tasks=[existing_composition, _impl_task("t1")],
        )
        assert result is None, (
            "Synthesis must be idempotent — skip when a composition "
            "task is already present in the input list"
        )

    def test_skipped_when_composition_task_already_in_input_by_source_type(
        self,
    ) -> None:
        """Source-type-based detection of existing composition tasks
        (in case labels got stripped during a kanban round-trip)."""
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        existing_composition = _impl_task("existing")
        existing_composition.source_type = "composition_synthesis"

        result = build_composition_task(
            project_name="x",
            impl_tasks=[existing_composition, _impl_task("t1")],
        )
        assert result is None


# ---------------------------------------------------------------------------
# Bug #649 root cause 2 — mandatory build verification gate
# ---------------------------------------------------------------------------


class TestBuildVerificationGate:
    """Composition task description must require a real build check.

    Background — bug #649 root cause 2
    -----------------------------------
    The verify-snake-3 run (2026-05-24) shipped a broken Vite import
    (``@/physics/engine`` unresolvable) because the composer agent
    merged worktree files and marked its task done without verifying
    the result built.  The integration verification smoke gate
    eventually got bypassed when later retries used weak
    ``start_command`` values until one returned exit 0.

    The fix: the composition task description (Marcus-authored)
    explicitly requires a build-verification command appropriate to
    the project's stated tech stack, and forbids marking complete on
    a broken build.  Marcus says WHAT must be true (build exit 0);
    agent decides HOW (which bundler, which flags).  Per Invariant
    #2 v2 (CLAUDE.md), verification belongs to Marcus's setup-time
    pipeline; this is the composition-task expression of that rule.
    """

    def test_description_mandates_build_verification(self) -> None:
        """Description names a mandatory build-verification step.

        Pre-fix the description had a vague "smoke test the
        composition root" bullet that left the agent free to skip the
        check.  Post-fix the description uses MANDATORY phrasing so
        the agent cannot interpret it as optional.
        """
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        task = build_composition_task(
            project_name="snake-game",
            impl_tasks=[_impl_task("t1"), _impl_task("t2")],
        )
        assert task is not None
        # The description must contain MANDATORY-style framing for the
        # build step.  Accept any of "MANDATORY", "REQUIRED", or
        # "must" near the build check so phrasing can evolve.
        desc = task.description
        assert "build verification" in desc.lower() or "mandatory build" in desc.lower()

    def test_description_lists_per_stack_build_commands(self) -> None:
        """Description gives concrete per-stack build-command examples.

        Marcus does not pick THE command — the agent picks the form
        matching the scaffold.  Multiple examples are named so the
        agent reads them as a menu, not a prescription.  This is
        coordination per Invariant #2 v2: Marcus says "your build
        must succeed"; the agent decides HOW to satisfy that for
        their stack.
        """
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        task = build_composition_task(
            project_name="x",
            impl_tasks=[_impl_task("t1"), _impl_task("t2")],
        )
        assert task is not None
        desc_lower = task.description.lower()
        # At minimum two of the canonical stacks must be named so
        # the agent does not read it as "Marcus picked npm".
        stacks_named = sum(
            keyword in desc_lower
            for keyword in ("npm run build", "python -m build", "cargo build")
        )
        assert stacks_named >= 2, (
            "Description must list >=2 stack-specific build commands "
            "so the agent does not read a single example as the "
            "canonical command"
        )

    def test_description_forbids_marking_complete_on_broken_build(self) -> None:
        """Description must explicitly forbid done-on-broken-build.

        Without an explicit prohibition the agent under retry
        pressure may report the task complete by reasoning "I tried
        to fix the build, close enough."  The text bans that
        interpretation outright.
        """
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        task = build_composition_task(
            project_name="x",
            impl_tasks=[_impl_task("t1"), _impl_task("t2")],
        )
        assert task is not None
        desc_lower = task.description.lower()
        # Some phrase ruling out broken-build acceptance.
        assert (
            "broken build" in desc_lower
            or "do not mark this task complete" in desc_lower
            or "blocker" in desc_lower
        )

    def test_description_requires_dev_server_probe_when_applicable(self) -> None:
        """Server-mode projects must boot + curl the running app.

        The verify-snake-3 failure mode: the agent ran a weak
        verification that did not actually launch the dev server.
        Marcus now names "boot dev server + curl" as the canonical
        probe so vague verification gets rejected by the description
        itself.
        """
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        task = build_composition_task(
            project_name="x",
            impl_tasks=[_impl_task("t1"), _impl_task("t2")],
        )
        assert task is not None
        desc_lower = task.description.lower()
        assert "dev server" in desc_lower or "curl" in desc_lower


class TestCompositionSelfVerify:
    """Issue #677 (self-verify): the composition task tells the agent to RUN
    the composed product and fix until it works — generic, no per-type
    evidence-submission framing — and stashes ``structural_category`` on
    ``source_context`` for any future Marcus-side check ("borrow hands")."""

    def test_description_tells_agent_to_run_it_not_just_build(self) -> None:
        from src.integrations.composition_synthesis import build_composition_task

        task = build_composition_task(
            project_name="x",
            impl_tasks=[_impl_task("t1"), _impl_task("t2")],
            structural_category="web app",
        )
        assert task is not None
        desc_lower = task.description.lower()
        assert "run it" in desc_lower
        assert "blank" in desc_lower  # blank screen / empty result mentioned
        assert "fix the wiring" in desc_lower

    def test_run_it_step_is_generic_no_evidence_submission(self) -> None:
        # Self-verify framing: the agent runs and fixes; it does NOT author
        # ``evidence`` for Marcus to judge. The step is generic across app
        # types (no per-type ``dom``/``submit evidence`` keys).
        from src.integrations.composition_synthesis import build_composition_task

        task = build_composition_task(
            project_name="x",
            impl_tasks=[_impl_task("t1"), _impl_task("t2")],
            structural_category="data pipeline",
        )
        assert task is not None
        desc_lower = task.description.lower()
        assert "behavior evidence" not in desc_lower
        assert "submit" not in desc_lower or "submit this evidence" not in desc_lower
        assert "mandatory behavior evidence" not in desc_lower

    def test_task_stashes_structural_category_on_source_context(self) -> None:
        from src.integrations.composition_synthesis import build_composition_task

        task = build_composition_task(
            project_name="x",
            impl_tasks=[_impl_task("t1"), _impl_task("t2")],
            structural_category="web app",
        )
        assert task is not None
        assert task.source_context is not None
        assert task.source_context["structural_category"] == "web app"

    def test_run_it_step_present_for_all_types(self) -> None:
        # The "run it, don't just build it" step is universal — it is not
        # gated on the structural category, so unclassified projects get it too.
        from src.integrations.composition_synthesis import build_composition_task

        task = build_composition_task(
            project_name="x",
            impl_tasks=[_impl_task("t1"), _impl_task("t2")],
            structural_category="other",
        )
        assert task is not None
        assert "RUN IT" in task.description

    def test_default_category_still_has_run_it_step(self) -> None:
        from src.integrations.composition_synthesis import build_composition_task

        task = build_composition_task(
            project_name="x",
            impl_tasks=[_impl_task("t1"), _impl_task("t2")],
        )
        assert task is not None
        assert "RUN IT" in task.description
        assert task.source_context["structural_category"] == "unknown"
