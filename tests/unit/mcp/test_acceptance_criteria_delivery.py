"""
Unit tests for acceptance_criteria delivery to agents (#664).

Marcus's setup-time pipeline writes the concrete, checkable conditions a
task must satisfy into ``Task.acceptance_criteria``. Before #664,
``request_next_task`` delivered ``completion_criteria`` but dropped
``acceptance_criteria`` entirely — so every criterion the pipeline
enriched landed in a field no agent ever read.

These tests verify both halves of the delivery pipe:
1. ``build_tiered_instructions`` surfaces the criteria, framed as the
   verification contract (Layer 1.2).
2. The framing is omitted when there are no acceptance criteria.
"""

from datetime import datetime, timezone

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.task import build_tiered_instructions

pytestmark = pytest.mark.unit


def _make_task(acceptance_criteria=None) -> Task:
    """Build a Task with optional acceptance criteria for testing."""
    return Task(
        id="task_664",
        name="Implement reversal handling",
        description="Snake must ignore a 180-degree reversal",
        status=TaskStatus.TODO,
        priority=Priority.HIGH,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=0.15,
        labels=["implementation"],
        acceptance_criteria=acceptance_criteria or [],
    )


class TestAcceptanceCriteriaDelivery:
    """Test suite for the acceptance-criteria layer in tiered instructions."""

    def test_criteria_surfaced_when_present(self):
        """acceptance_criteria set → criteria appear in the agent's instructions."""
        task = _make_task(
            acceptance_criteria=[
                "Pressing the opposite direction is ignored (no instant death)",
                "Food never spawns on the snake's body",
            ]
        )

        instructions = build_tiered_instructions(
            base_instructions="Do the task",
            task=task,
            context_data=None,
            dependency_awareness=None,
            predictions=None,
        )

        assert "ACCEPTANCE CRITERIA" in instructions
        assert "no instant death" in instructions
        assert "Food never spawns on the snake's body" in instructions

    def test_criteria_framed_as_verification_contract(self):
        """The criteria must be framed as what the work is verified against."""
        task = _make_task(acceptance_criteria=["Reversal is a no-op"])

        instructions = build_tiered_instructions(
            base_instructions="Do the task",
            task=task,
            context_data=None,
            dependency_awareness=None,
            predictions=None,
        )

        assert "verified against" in instructions.lower()

    def test_layer_omitted_when_no_criteria(self):
        """No acceptance criteria → no acceptance-criteria layer."""
        task = _make_task(acceptance_criteria=[])

        instructions = build_tiered_instructions(
            base_instructions="Do the task",
            task=task,
            context_data=None,
            dependency_awareness=None,
            predictions=None,
        )

        assert "ACCEPTANCE CRITERIA" not in instructions
