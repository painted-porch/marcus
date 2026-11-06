"""
Unit tests for ADR-0013 K5's DECLINED status in deps_allow_claim.

K5 (Accepted 2026-11-05): the kernel gains a terminal state for "this
could not be verified", distinct from DONE, from failed, and from a
worker-reported BLOCKED. ``deps_allow_claim`` is where that distinction
has to actually hold: an ordinary (strict-gate) dependent must still
require every upstream DONE, so a DECLINED upstream correctly stays
unclaimable forever, exactly as "distinct from done" demands. An
integration task's best-effort gate (``SETTLED_FOR_BEST_EFFORT``,
which already treats BLOCKED this way) must treat DECLINED the same
way -- settled, so it does not wait on a dependency that will never
become DONE.
"""

from datetime import datetime, timezone

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.core.task_claimability import (
    SETTLED_FOR_BEST_EFFORT,
    deps_allow_claim,
    is_integration_task,
)

pytestmark = pytest.mark.unit


def _task(task_id: str, status: TaskStatus, **kw: object) -> Task:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        name=kw.get("name", f"Task {task_id}"),  # type: ignore[arg-type]
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        labels=kw.get("labels", []),  # type: ignore[arg-type]
        dependencies=kw.get("dependencies", []),  # type: ignore[arg-type]
    )


class TestDeclinedIsSettledForBestEffort:
    """The integration gate must treat DECLINED the way it already
    treats BLOCKED: settled, not a reason to keep waiting."""

    def test_declined_is_in_the_settled_set(self) -> None:
        assert TaskStatus.DECLINED in SETTLED_FOR_BEST_EFFORT

    def test_an_integration_task_proceeds_past_a_declined_upstream(self) -> None:
        """One declined upstream among enough DONE ones to still clear
        BEST_EFFORT_MIN_DONE_FRACTION -- isolates "is DECLINED settled"
        from the separate done-fraction threshold."""
        declined = _task("verify-1", TaskStatus.DECLINED)
        done = [_task(f"impl-{i}", TaskStatus.DONE) for i in range(4)]
        deps = ["verify-1"] + [t.id for t in done]
        integration = _task(
            "int-1",
            TaskStatus.TODO,
            name="Integration verification",
            labels=["integration", "verification", "type:integration"],
            dependencies=deps,
        )
        assert is_integration_task(integration)
        tasks_by_id = {t.id: t for t in ([declined, integration] + done)}

        claimable, degraded = deps_allow_claim(integration, deps, tasks_by_id)

        assert claimable is True
        assert any(
            d["id"] == "verify-1" and d["status"] == "declined" for d in degraded
        )


class TestDeclinedNeverSatisfiesTheStrictGate:
    """An ordinary dependent must stay unclaimable forever behind a
    declined upstream -- the whole point of "distinct from done"."""

    def test_an_ordinary_dependent_stays_unclaimable(self) -> None:
        declined = _task("verify-1", TaskStatus.DECLINED)
        ordinary = _task("synth-1", TaskStatus.TODO, dependencies=["verify-1"])
        tasks_by_id = {t.id: t for t in (declined, ordinary)}

        claimable, degraded = deps_allow_claim(ordinary, ["verify-1"], tasks_by_id)

        assert claimable is False
        assert degraded == []

    def test_this_differs_from_a_done_upstream(self) -> None:
        """Sanity check: the same dependent with a DONE upstream claims fine,
        confirming the refusal above is specifically about DECLINED."""
        done = _task("verify-1", TaskStatus.DONE)
        ordinary = _task("synth-1", TaskStatus.TODO, dependencies=["verify-1"])
        tasks_by_id = {t.id: t for t in (done, ordinary)}

        claimable, _ = deps_allow_claim(ordinary, ["verify-1"], tasks_by_id)

        assert claimable is True
