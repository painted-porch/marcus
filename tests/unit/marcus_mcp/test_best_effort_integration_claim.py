"""
Unit tests for the best-effort integration claim gate (issue #629).

The terminal integration-verification task depends on every implementer
task. The availability filter required every dependency to be strictly
DONE, so one BLOCKED upstream froze the integration task forever — the
project hung at "almost done" with no recovery path (and #627's redo
tool never got a chance to run, because redo requires a claimed caller).

The fix: for the integration task ONLY, dependencies are satisfied when
every upstream is SETTLED (DONE or BLOCKED) and at least
BEST_EFFORT_MIN_DONE_FRACTION of them are DONE. The claim then carries
a ``degraded_upstreams`` record so the agent knows exactly which lanes
never finished and can decide what to do (Invariant #2).
"""

from datetime import datetime, timezone

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.core.task_claimability import BEST_EFFORT_MIN_DONE_FRACTION
from src.marcus_mcp.tools.task import _deps_allow_claim

pytestmark = pytest.mark.unit


def _task(
    task_id: str,
    name: str = "Implement thing",
    status: TaskStatus = TaskStatus.DONE,
    labels: list[str] | None = None,
    dependencies: list[str] | None = None,
) -> Task:
    now = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description=name,
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=2.0,
        labels=labels or [],
        dependencies=dependencies or [],
    )


def _integration(deps: list[str]) -> Task:
    return _task(
        "int-1",
        name="Integration verification for proj",
        status=TaskStatus.TODO,
        labels=["integration", "verification", "type:integration"],
        dependencies=deps,
    )


class TestOrdinaryTasksKeepStrictGate:
    """Non-integration tasks must still require all deps DONE."""

    def test_blocked_dep_keeps_ordinary_task_unclaimable(self) -> None:
        deps = {"a": _task("a"), "b": _task("b", status=TaskStatus.BLOCKED)}
        t = _task("t", status=TaskStatus.TODO, dependencies=["a", "b"])
        claimable, degraded = _deps_allow_claim(t, ["a", "b"], deps)
        assert claimable is False
        assert degraded == []

    def test_all_done_deps_claimable(self) -> None:
        deps = {"a": _task("a"), "b": _task("b")}
        t = _task("t", status=TaskStatus.TODO, dependencies=["a", "b"])
        claimable, degraded = _deps_allow_claim(t, ["a", "b"], deps)
        assert claimable is True
        assert degraded == []


class TestIntegrationBestEffort:
    """The integration task claims over settled-but-degraded upstreams."""

    def test_one_blocked_of_seven_is_claimable_with_degraded_record(self) -> None:
        """6/7 done + 1 blocked (85.7% >= 80%) -> claimable, degraded listed."""
        deps = {f"d{i}": _task(f"d{i}") for i in range(6)}
        deps["d6"] = _task("d6", name="Implement API", status=TaskStatus.BLOCKED)
        dep_ids = list(deps.keys())
        claimable, degraded = _deps_allow_claim(_integration(dep_ids), dep_ids, deps)
        assert claimable is True
        assert [d["id"] for d in degraded] == ["d6"]
        assert degraded[0]["status"] == "blocked"
        assert degraded[0]["name"] == "Implement API"

    def test_all_done_is_claimable_with_no_degraded_record(self) -> None:
        deps = {f"d{i}": _task(f"d{i}") for i in range(4)}
        dep_ids = list(deps.keys())
        claimable, degraded = _deps_allow_claim(_integration(dep_ids), dep_ids, deps)
        assert claimable is True
        assert degraded == []

    def test_below_done_floor_is_not_claimable(self) -> None:
        """1 done + 1 blocked (50% < 80%) -> project too broken to integrate."""
        deps = {
            "a": _task("a"),
            "b": _task("b", status=TaskStatus.BLOCKED),
        }
        claimable, degraded = _deps_allow_claim(
            _integration(["a", "b"]), ["a", "b"], deps
        )
        assert claimable is False
        assert degraded == []

    def test_exactly_at_floor_is_claimable(self) -> None:
        """4 done + 1 blocked = exactly 0.8 -> claimable (floor is inclusive)."""
        assert BEST_EFFORT_MIN_DONE_FRACTION == 0.8
        deps = {f"d{i}": _task(f"d{i}") for i in range(4)}
        deps["d4"] = _task("d4", status=TaskStatus.BLOCKED)
        dep_ids = list(deps.keys())
        claimable, _ = _deps_allow_claim(_integration(dep_ids), dep_ids, deps)
        assert claimable is True

    def test_in_flight_dep_is_not_settled(self) -> None:
        """An IN_PROGRESS upstream means work is live — wait, don't claim."""
        deps = {f"d{i}": _task(f"d{i}") for i in range(5)}
        deps["d5"] = _task("d5", status=TaskStatus.IN_PROGRESS)
        dep_ids = list(deps.keys())
        claimable, _ = _deps_allow_claim(_integration(dep_ids), dep_ids, deps)
        assert claimable is False

    def test_todo_dep_is_not_settled(self) -> None:
        """A recovered (partial-done -> TODO) upstream is claimable by others
        and will be re-attempted — integration must keep waiting for it."""
        deps = {f"d{i}": _task(f"d{i}") for i in range(5)}
        deps["d5"] = _task("d5", status=TaskStatus.TODO)
        dep_ids = list(deps.keys())
        claimable, _ = _deps_allow_claim(_integration(dep_ids), dep_ids, deps)
        assert claimable is False

    def test_unknown_dep_id_is_not_settled(self) -> None:
        """A dep id with no task object cannot be proven settled."""
        deps = {f"d{i}": _task(f"d{i}") for i in range(5)}
        dep_ids = list(deps.keys()) + ["ghost"]
        claimable, _ = _deps_allow_claim(_integration(dep_ids), dep_ids, deps)
        assert claimable is False

    def test_no_deps_is_claimable(self) -> None:
        claimable, degraded = _deps_allow_claim(_integration([]), [], {})
        assert claimable is True
        assert degraded == []


class TestBestEffortAddendum:
    """The assignment instructions must disclose the degraded upstreams."""

    def test_addendum_names_every_degraded_upstream(self) -> None:
        from src.marcus_mcp.tools.task import _best_effort_addendum

        text = _best_effort_addendum(
            [
                {"id": "d6", "name": "Implement API", "status": "blocked"},
                {"id": "d7", "name": "Implement UI", "status": "blocked"},
            ]
        )
        assert "Implement API" in text
        assert "Implement UI" in text
        assert "BEST-EFFORT" in text

    def test_addendum_scopes_redo_away_from_blocked_lanes(self) -> None:
        """request_task_redo targets DONE work — the addendum must say it
        does not apply to these BLOCKED upstreams, or agents will try it."""
        from src.marcus_mcp.tools.task import _best_effort_addendum

        text = _best_effort_addendum(
            [{"id": "d6", "name": "Implement API", "status": "blocked"}]
        ).lower()
        assert "request_task_redo" in text
        assert "not" in text
