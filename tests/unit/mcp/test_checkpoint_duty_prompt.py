"""
Unit tests for the ADR-0012 D8 checkpoint duty in agent prompts.

A dead worker's partial work is salvageable only if it was committed: the
recovery handoff can pass on commits and nothing else. The duty to commit
at a stated cadence is therefore part of the task contract, and Marcus
states it in its own configured numbers rather than leaving an agent to
infer them from a static prompt file.

The duty says *when* to commit and never what to write, so it is a
project-wide discipline (coordination), not an implementation decision.
"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src.core.lease_invariants import (
    DEFAULT_CHECKPOINT_CADENCE_MINUTES,
    DEFAULT_SILENCE_TIMEOUT_MINUTES,
)
from src.core.models import Priority, Task, TaskStatus
from src.core.project_registry import ProjectConfig, ProjectRegistry
from src.marcus_mcp.tools.task import (
    _build_checkpoint_duty_prompt,
    build_tiered_instructions,
)

pytestmark = pytest.mark.unit


def _make_task(labels: list[str] | None = None) -> Task:
    """Build a minimal task for instruction building.

    Parameters
    ----------
    labels : list[str] | None
        Labels to put on the task; implementation by default.

    Returns
    -------
    Task
        A TODO task.
    """
    now = datetime.now(timezone.utc)
    return Task(
        id="task_1",
        name="Implement the parser",
        description="Build the parser",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=2.0,
        labels=labels if labels is not None else ["implementation"],
        dependencies=[],
    )


def _state_with_dials(cadence: float, silence: float) -> Mock:
    """Build a state double whose lease manager carries the two dials.

    Parameters
    ----------
    cadence : float
        Configured checkpoint cadence in minutes.
    silence : float
        Configured silence timeout in minutes.

    Returns
    -------
    Mock
        State with a ``lease_manager``.
    """
    state = Mock()
    state.lease_manager = Mock()
    state.lease_manager.checkpoint_cadence_minutes = cadence
    state.lease_manager.silence_timeout_minutes = silence
    return state


class TestCheckpointDutyText:
    """The duty carries Marcus's live numbers and names the consequence."""

    def test_uses_the_configured_dials(self) -> None:
        """A retuned deployment retunes the contract, not just the server."""
        prompt = _build_checkpoint_duty_prompt(_state_with_dials(7, 21))

        assert "every 7 minutes" in prompt
        assert "21 minutes" in prompt

    def test_falls_back_to_the_adr_defaults(self) -> None:
        """With no lease manager the ADR's own figures are stated."""
        prompt = _build_checkpoint_duty_prompt(None)

        assert f"every {DEFAULT_CHECKPOINT_CADENCE_MINUTES:.0f} minutes" in prompt
        assert f"{DEFAULT_SILENCE_TIMEOUT_MINUTES:.0f} minutes" in prompt

    def test_says_why_uncommitted_work_is_lost(self) -> None:
        """The reason is the salvage path, not tidiness."""
        prompt = _build_checkpoint_duty_prompt(None)

        assert "handed over" in prompt
        assert "uncommitted" in prompt

    def test_does_not_dictate_implementation(self) -> None:
        """Invariant #2: Marcus says when to commit, never what to write."""
        prompt = _build_checkpoint_duty_prompt(None).lower()

        for forbidden in ("library", "framework", "pattern", "class ", "function "):
            assert forbidden not in prompt


def _state_with_declared_project(
    has_commit_based_recovery: bool, cadence: float = 20, silence: float = 90
) -> Mock:
    """A state double whose project registry declares the K8/#868 flag.

    Uses ``MagicMock(spec=ProjectRegistry)`` rather than a bare
    ``Mock()``, the same pattern ``test_phase_enforcement_requires_
    declaration.py`` already established: ``spec=`` makes
    ``isinstance(registry, ProjectRegistry)`` pass, which
    ``project_has_commit_based_recovery`` requires before trusting
    anything on it.
    """
    project = ProjectConfig(
        id="minecraft-project",
        name="Minecraft run",
        provider="sqlite",
        provider_config={},
        has_commit_based_recovery=has_commit_based_recovery,
    )
    registry = Mock(spec=ProjectRegistry)
    registry.get_project_sync = Mock(return_value=project)
    registry.get_active_project_sync = Mock(return_value=project)

    state = Mock()
    state.project_registry = registry
    state.lease_manager = Mock()
    state.lease_manager.checkpoint_cadence_minutes = cadence
    state.lease_manager.silence_timeout_minutes = silence
    return state


class TestCheckpointDutyDeclaration:
    """ADR-0013 K8 consequence 2 (issue #868): a project may declare it
    has no commit-based recovery story, skipping this instruction
    layer entirely."""

    def test_a_project_with_no_commit_based_recovery_gets_no_duty_text(
        self,
    ) -> None:
        """A Minecraft-shaped project (no git branch at all) gets an
        empty string, not instructions it cannot act on."""
        state = _state_with_declared_project(has_commit_based_recovery=False)

        prompt = _build_checkpoint_duty_prompt(state, "minecraft-project")

        assert prompt == ""

    def test_the_default_declared_project_still_gets_the_duty(self) -> None:
        """A project that explicitly declares has_commit_based_recovery=True
        (the default) is unaffected."""
        state = _state_with_declared_project(has_commit_based_recovery=True)

        prompt = _build_checkpoint_duty_prompt(state, "minecraft-project")

        assert "CHECKPOINT DUTY" in prompt

    def test_an_undeclared_project_still_gets_the_duty(self) -> None:
        """No registry at all (today's existing tests' shape) behaves
        exactly as before this issue: the duty is still included."""
        prompt = _build_checkpoint_duty_prompt(_state_with_dials(20, 90), None)

        assert "CHECKPOINT DUTY" in prompt

    def test_build_tiered_instructions_omits_the_layer_for_a_declared_project(
        self,
    ) -> None:
        """The full instruction-assembly path, not just the helper in
        isolation, drops the layer for a declared project."""
        state = _state_with_declared_project(has_commit_based_recovery=False)
        task = _make_task()
        task.project_id = "minecraft-project"

        instructions = build_tiered_instructions(
            base_instructions="Base",
            task=task,
            context_data=None,
            dependency_awareness=None,
            predictions=None,
            state=state,
        )

        assert "CHECKPOINT DUTY" not in instructions


class TestCheckpointDutyLayer:
    """Every task carries the duty, whatever its type."""

    def test_implementation_task_gets_the_duty(self) -> None:
        """An implementation task is the main salvage case."""
        instructions = build_tiered_instructions(
            base_instructions="Base",
            task=_make_task(),
            context_data=None,
            dependency_awareness=None,
            predictions=None,
            state=_state_with_dials(11, 33),
        )

        assert "CHECKPOINT DUTY" in instructions
        assert "every 11 minutes" in instructions

    def test_design_task_gets_the_duty_too(self) -> None:
        """A design task produces artifacts that also die uncommitted."""
        instructions = build_tiered_instructions(
            base_instructions="Base",
            task=_make_task(labels=["design"]),
            context_data=None,
            dependency_awareness=None,
            predictions=None,
            state=None,
        )

        assert "CHECKPOINT DUTY" in instructions

    def test_the_duty_appears_before_the_task_body(self) -> None:
        """An agent reads the duty before it starts, not after."""
        instructions = build_tiered_instructions(
            base_instructions="UNIQUE_BASE_MARKER",
            task=_make_task(),
            context_data=None,
            dependency_awareness=None,
            predictions=None,
            state=None,
        )

        assert instructions.index("CHECKPOINT DUTY") < instructions.index(
            "UNIQUE_BASE_MARKER"
        )
