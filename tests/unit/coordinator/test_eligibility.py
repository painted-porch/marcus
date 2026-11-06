"""
Unit tests for eligibility at assignment (issue #737, step 3).

Eligibility is the rule the board applies before offering a task to a
worker.  The rules: a verify task is never offered to the author of the
claim it depends on, nor to any worker from the author's vendor; an
approve task is offered only to a human principal; every other task
type is offered only to an agent principal, so a person is offered
approve tasks and nothing else.  These tests cover the pure module
only; wiring into the two assignment entry points is reviewed and
tested separately.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import Mock

import pytest

from src.core.models import Priority, Task, TaskStatus, WorkerStatus
from src.core.persistence import MemoryPersistence, Persistence
from src.core.project_registry import ProjectConfig, ProjectRegistry
from src.marcus_mcp.coordinator.eligibility import (
    LEGACY_REFUSALS_COLLECTION,
    drain_refusal_writes,
    filter_eligible_tasks,
    is_eligible,
    refusal_key,
    refusals_collection,
)

pytestmark = pytest.mark.unit


def _make_task(
    task_id: str,
    task_type: str = "implement",
    dependencies: Optional[List[str]] = None,
    status: TaskStatus = TaskStatus.TODO,
    assigned_to: Optional[str] = None,
) -> Task:
    """Build a minimal Task for eligibility checks."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=assigned_to,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        dependencies=dependencies or [],
        task_type=task_type,
    )


def _make_worker(
    worker_id: str,
    vendor: str = "",
    principal: str = "agent",
) -> WorkerStatus:
    """Build a minimal WorkerStatus for eligibility checks."""
    return WorkerStatus(
        worker_id=worker_id,
        name=worker_id,
        role="worker",
        email=None,
        current_tasks=[],
        completed_tasks_count=0,
        capacity=40,
        skills=[],
        availability={},
        vendor=vendor,
        principal=principal,
    )


def _make_state(tasks: List[Task], workers: List[WorkerStatus]) -> Mock:
    """Build a Mock server state holding tasks and registered workers."""
    state = Mock()
    state.project_tasks = tasks
    state.agent_status = {w.worker_id: w for w in workers}
    return state


def _tow_case() -> tuple[Task, Task, Mock]:
    """Build the worked case from the design: closed find, open verify.

    finder-pplx-1 (vendor perplexity) authored the claim; the verify
    task depends on the closed find task.
    """
    find = _make_task(
        "find-1",
        task_type="find",
        status=TaskStatus.DONE,
        assigned_to="finder-pplx-1",
    )
    verify = _make_task("verify-1", task_type="verify", dependencies=["find-1"])
    workers = [
        _make_worker("finder-pplx-1", vendor="perplexity"),
        _make_worker("verifier-pplx-2", vendor="perplexity"),
        _make_worker("verifier-gem-1", vendor="google"),
    ]
    state = _make_state([find, verify], workers)
    return find, verify, state


async def _registered_state(
    tasks: List[Task],
    workers: List[WorkerStatus],
    task_type_registrations: Dict[str, Dict[str, Any]],
) -> Mock:
    """Build a state whose active project carries a K2 registration.

    A bare ``Mock`` auto-creates any attribute, including
    ``project_registry``, so ``is_eligible`` must not trust that
    presence alone -- it checks ``isinstance(..., ProjectRegistry)``.
    This builds a real registry, the thing that check is meant to let
    through.
    """
    registry = ProjectRegistry(Persistence(MemoryPersistence()))
    now = datetime.now(timezone.utc)
    await registry.add_project(
        ProjectConfig(
            id="proj-1",
            name="fifth-extension",
            provider="sqlite",
            provider_config={},
            created_at=now,
            last_used=now,
            task_type_registrations=task_type_registrations,
        )
    )
    state = _make_state(tasks, workers)
    state.project_registry = registry
    return state


class TestRegisteredVerifyShapedTypes:
    """Issue #830: self-check exclusion for a project's own type names.

    A fifth extension names its task types ``"write"`` (produces a
    claim) and ``"check"`` (verifies it) instead of reusing
    ``"find"``/``"verify"``. The exclusion must still apply, keyed off
    the project's own ADR-0013 K2 registration rather than a second
    hardcoded literal pair.
    """

    @pytest.mark.asyncio
    async def test_author_is_refused_its_own_registered_check_task(self) -> None:
        write = _make_task(
            "write-1",
            task_type="write",
            status=TaskStatus.DONE,
            assigned_to="author-1",
        )
        check = _make_task("check-1", task_type="check", dependencies=["write-1"])
        author = _make_worker("author-1")
        state = await _registered_state(
            [write, check],
            [author],
            {"check": {"verifies_task_type": "write"}},
        )

        eligible, reason = is_eligible(author, check, state)

        assert eligible is False
        assert "author" in reason

    @pytest.mark.asyncio
    async def test_a_different_agent_is_offered_the_registered_check_task(
        self,
    ) -> None:
        write = _make_task(
            "write-1",
            task_type="write",
            status=TaskStatus.DONE,
            assigned_to="author-1",
        )
        check = _make_task("check-1", task_type="check", dependencies=["write-1"])
        other = _make_worker("checker-1")
        state = await _registered_state(
            [write, check],
            [other],
            {"check": {"verifies_task_type": "write"}},
        )

        eligible, reason = is_eligible(other, check, state)

        assert eligible is True
        assert reason == ""

    @pytest.mark.asyncio
    async def test_an_unregistered_project_still_honors_the_literal_pair(
        self,
    ) -> None:
        """A registry present but with no registration for "verify" at
        all must fall back to the literal "find" producer -- the
        existing find/verify chain must not regress just because a
        real ProjectRegistry is now in play."""
        find = _make_task(
            "find-1",
            task_type="find",
            status=TaskStatus.DONE,
            assigned_to="author-1",
        )
        verify = _make_task("verify-1", task_type="verify", dependencies=["find-1"])
        author = _make_worker("author-1")
        state = await _registered_state([find, verify], [author], {})

        eligible, reason = is_eligible(author, verify, state)

        assert eligible is False
        assert "author" in reason

    @pytest.mark.asyncio
    async def test_a_registered_type_unrelated_to_verification_is_unaffected(
        self,
    ) -> None:
        """Registering a type that is not verify-shaped (no
        verifies_task_type) must not start gating it."""
        task = _make_task("implement-1", task_type="implement")
        worker = _make_worker("worker-1")
        state = await _registered_state(
            [task], [worker], {"implement": {"evidence_schema": {}}}
        )

        eligible, _ = is_eligible(worker, task, state)

        assert eligible is True


async def _two_project_state(
    tasks: List[Task],
    workers: List[WorkerStatus],
    registered_registrations: Dict[str, Dict[str, Any]],
    active_project_id: str,
) -> Mock:
    """Build a state with two projects: "proj-a" (registered) and
    "proj-b" (not), where ``active_project_id`` names whichever one is
    the server's active project -- independent of which project each
    task in ``tasks`` actually belongs to via its own ``project_id``.
    """
    registry = ProjectRegistry(Persistence(MemoryPersistence()))
    now = datetime.now(timezone.utc)
    await registry.add_project(
        ProjectConfig(
            id="proj-a",
            name="project-a",
            provider="sqlite",
            provider_config={},
            created_at=now,
            last_used=now,
            task_type_registrations=registered_registrations,
        )
    )
    await registry.add_project(
        ProjectConfig(
            id="proj-b",
            name="project-b",
            provider="sqlite",
            provider_config={},
            created_at=now,
            last_used=now,
        )
    )
    await registry.set_active_project(active_project_id)
    state = _make_state(tasks, workers)
    state.project_registry = registry
    return state


class TestRegistrationIsScopedToTheCandidateTasksOwnProject:
    """Codex P2 on PR #836.

    A worker can request tasks for a project other than the server's
    active one. Looking up the registration from whatever is active
    applied the wrong project's rules to a candidate from a different
    project.
    """

    @pytest.mark.asyncio
    async def test_the_candidates_own_project_registration_applies(self) -> None:
        """Project A registers check/write; project B (active) does
        not. A candidate task belonging to A must still be gated by
        A's registration, not B's absence of one."""
        write = _make_task(
            "write-1",
            task_type="write",
            status=TaskStatus.DONE,
            assigned_to="author-1",
        )
        write.project_id = "proj-a"
        check = _make_task("check-1", task_type="check", dependencies=["write-1"])
        check.project_id = "proj-a"
        author = _make_worker("author-1")
        state = await _two_project_state(
            [write, check],
            [author],
            {"check": {"verifies_task_type": "write"}},
            active_project_id="proj-b",
        )

        eligible, reason = is_eligible(author, check, state)

        assert eligible is False
        assert "author" in reason

    @pytest.mark.asyncio
    async def test_a_legacy_task_with_no_project_id_falls_back_to_active(
        self,
    ) -> None:
        """A task with no project_id at all (pre-dates per-task project
        scoping) still resolves against the active project, matching
        pre-#836 behavior."""
        write = _make_task(
            "write-1",
            task_type="write",
            status=TaskStatus.DONE,
            assigned_to="author-1",
        )
        check = _make_task("check-1", task_type="check", dependencies=["write-1"])
        author = _make_worker("author-1")
        state = await _two_project_state(
            [write, check],
            [author],
            {"check": {"verifies_task_type": "write"}},
            active_project_id="proj-a",
        )

        eligible, reason = is_eligible(author, check, state)

        assert eligible is False
        assert "author" in reason


class TestVerifyEligibility:
    """Test suite for the verify-task rules: never author, never author's vendor."""

    def test_author_is_refused_its_own_verify_task(self) -> None:
        """The claim's author is never offered the dependent verify task."""
        _, verify, state = _tow_case()
        author = state.agent_status["finder-pplx-1"]

        eligible, reason = is_eligible(author, verify, state)

        assert eligible is False
        assert "author" in reason

    def test_same_vendor_worker_is_refused(self) -> None:
        """A worker from the author's vendor is never offered the verify task."""
        _, verify, state = _tow_case()
        same_vendor = state.agent_status["verifier-pplx-2"]

        eligible, reason = is_eligible(same_vendor, verify, state)

        assert eligible is False
        assert "vendor" in reason

    def test_different_vendor_worker_is_offered(self) -> None:
        """A worker from a different vendor is eligible for the verify task."""
        _, verify, state = _tow_case()
        different_vendor = state.agent_status["verifier-gem-1"]

        eligible, reason = is_eligible(different_vendor, verify, state)

        assert eligible is True
        assert reason == ""

    def test_verify_without_done_find_dependency_passes(self) -> None:
        """With no closed find dependency there is no author to protect.

        Dependency resolution holds the verify task until the find task
        closes, so this only matters for hand-seeded or unusual boards;
        eligibility must not crash or refuse everyone.
        """
        verify = _make_task("verify-1", task_type="verify", dependencies=["find-1"])
        worker = _make_worker("verifier-gem-1", vendor="google")
        state = _make_state([verify], [worker])

        eligible, reason = is_eligible(worker, verify, state)

        assert eligible is True

    def test_empty_vendor_does_not_match_empty_vendor(self) -> None:
        """Two workers with no declared vendor are not 'the same vendor'.

        The author-refusal still applies; the vendor rule must not
        collapse every vendorless worker into one blocked pool.
        """
        find = _make_task(
            "find-1",
            task_type="find",
            status=TaskStatus.DONE,
            assigned_to="finder-a",
        )
        verify = _make_task("verify-1", task_type="verify", dependencies=["find-1"])
        author = _make_worker("finder-a", vendor="")
        other = _make_worker("verifier-b", vendor="")
        state = _make_state([find, verify], [author, other])

        eligible, _ = is_eligible(other, verify, state)

        assert eligible is True


class TestPrincipalEligibility:
    """Test suite for the principal rules: approve is human-only, the rest agent-only."""

    def test_agent_is_refused_an_approve_task(self) -> None:
        """An agent principal is never offered an approve task."""
        approve = _make_task("approve-1", task_type="approve")
        agent = _make_worker("verifier-gem-1", vendor="google", principal="agent")
        state = _make_state([approve], [agent])

        eligible, reason = is_eligible(agent, approve, state)

        assert eligible is False
        assert "human" in reason

    def test_human_is_offered_an_approve_task(self) -> None:
        """A human principal is eligible for an approve task."""
        approve = _make_task("approve-1", task_type="approve")
        human = _make_worker("larry", principal="human")
        state = _make_state([approve], [human])

        eligible, reason = is_eligible(human, approve, state)

        assert eligible is True
        assert reason == ""

    @pytest.mark.parametrize("task_type", ["find", "verify", "synthesize", "implement"])
    def test_human_is_refused_every_other_task_type(self, task_type: str) -> None:
        """A human principal is offered approve tasks and nothing else."""
        task = _make_task("t-1", task_type=task_type)
        human = _make_worker("larry", principal="human")
        state = _make_state([task], [human])

        eligible, reason = is_eligible(human, task, state)

        assert eligible is False
        assert "agent" in reason

    @pytest.mark.parametrize("task_type", ["find", "synthesize", "implement"])
    def test_agent_passes_non_verify_non_approve_types(self, task_type: str) -> None:
        """An agent principal is eligible for the non-gated task types."""
        task = _make_task("t-1", task_type=task_type)
        agent = _make_worker("finder-pplx-1", vendor="perplexity")
        state = _make_state([task], [agent])

        eligible, reason = is_eligible(agent, task, state)

        assert eligible is True
        assert reason == ""


class TestFilterEligibleTasks:
    """Test suite for the shared filter both assignment entry points draw from."""

    def test_filter_removes_ineligible_and_keeps_eligible(self) -> None:
        """The filter drops the tasks the worker may not take."""
        find_done, verify, state = _tow_case()
        implement = _make_task("impl-1", task_type="implement")
        state.project_tasks = [find_done, verify, implement]
        author = state.agent_status["finder-pplx-1"]

        result = filter_eligible_tasks(author, [verify, implement], state)

        assert result == [implement]

    def test_filter_logs_each_refusal_with_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Every refusal is logged with the worker, task, and reason."""
        _, verify, state = _tow_case()
        author = state.agent_status["finder-pplx-1"]

        with caplog.at_level(logging.INFO):
            filter_eligible_tasks(author, [verify], state)

        assert any(
            "finder-pplx-1" in r.message
            and "verify-1" in r.message
            and "author" in r.message
            for r in caplog.records
        )

    def test_filter_records_refusals_on_state_for_the_audit_bundle(self) -> None:
        """Refusals are kept on state so export_case (step 8) can show them."""
        _, verify, state = _tow_case()
        state.eligibility_refusals = []
        same_vendor = state.agent_status["verifier-pplx-2"]

        filter_eligible_tasks(same_vendor, [verify], state)

        assert len(state.eligibility_refusals) == 1
        refusal = state.eligibility_refusals[0]
        assert refusal["agent_id"] == "verifier-pplx-2"
        assert refusal["task_id"] == "verify-1"
        assert "vendor" in refusal["reason"]
        assert refusal["count"] == 1
        assert "first_refused_at" in refusal
        assert "last_refused_at" in refusal

    def test_repeated_refusal_dedups_into_one_entry_with_count(self) -> None:
        """Polling workers refuse the same way every ~30s; dedup bounds state.

        The same (agent, task, reason) triple updates the existing
        entry's count and last_refused_at instead of appending, so an
        hour of polling is one row, not ~120.
        """
        _, verify, state = _tow_case()
        state.eligibility_refusals = []
        author = state.agent_status["finder-pplx-1"]

        filter_eligible_tasks(author, [verify], state)
        filter_eligible_tasks(author, [verify], state)

        assert len(state.eligibility_refusals) == 1
        refusal = state.eligibility_refusals[0]
        assert refusal["count"] == 2
        assert refusal["last_refused_at"] >= refusal["first_refused_at"]

    def test_refusals_by_different_agents_stay_separate_entries(self) -> None:
        """Dedup keys on (agent, task, reason); other agents get own rows."""
        _, verify, state = _tow_case()
        state.eligibility_refusals = []
        author = state.agent_status["finder-pplx-1"]
        same_vendor = state.agent_status["verifier-pplx-2"]

        filter_eligible_tasks(author, [verify], state)
        filter_eligible_tasks(same_vendor, [verify], state)

        assert len(state.eligibility_refusals) == 2

    def test_filter_passes_through_for_a_non_worker_status_agent(self) -> None:
        """A non-WorkerStatus identity is not judged, only passed through.

        Registration always stores WorkerStatus; test doubles and
        legacy callers must keep their pre-#737 behavior instead of
        being refused on attributes that are not real.
        """
        verify = _make_task("verify-1", task_type="verify")
        state = _make_state([verify], [])

        result = filter_eligible_tasks(Mock(), [verify], state)

        assert result == [verify]

    def test_filter_passes_everything_for_an_unregistered_worker(self) -> None:
        """A worker object with plain defaults filters like any agent.

        The filter never raises on missing state entries; eligibility
        is a refusal to offer, not an exception path.
        """
        implement = _make_task("impl-1", task_type="implement")
        worker = _make_worker("unknown-agent")
        state = _make_state([implement], [])

        result = filter_eligible_tasks(worker, [implement], state)

        assert result == [implement]


class _GatedPersistence:
    """A persistence layer whose reads can be held open or made to fail once.

    Wraps a memory backend so tests can reproduce the two orderings that
    matter: a slow carry read while more refusals arrive, and a first
    write that fails before the carry completed.
    """

    def __init__(self) -> None:
        self.inner = Persistence(backend=MemoryPersistence())
        self.gate = asyncio.Event()
        self.gate.set()
        self.fail_retrieve_once = False
        self.store_calls = 0

    async def store(self, collection: str, key: str, data: dict) -> None:
        self.store_calls += 1
        await self.inner.store(collection, key, data)

    async def retrieve(self, collection: str, key: str) -> Any:
        await self.gate.wait()
        if self.fail_retrieve_once:
            self.fail_retrieve_once = False
            raise RuntimeError("database is locked")
        return await self.inner.retrieve(collection, key)

    async def query(
        self, collection: str, filter_func: Any = None, limit: int = 100
    ) -> Any:
        return await self.inner.query(collection, filter_func, limit)

    async def delete(self, collection: str, key: str) -> None:
        await self.inner.delete(collection, key)


class TestRefusalPersistence:
    """Test suite for refusals surviving a server restart (issue #740).

    Refusals are written through the persistence layer by one serialized
    writer per server state: writes for a record never interleave, the
    prior process's count is carried in exactly once and only after a
    successful read, repeats coalesce into the latest count, and a
    failed write is retried rather than silently dropped.
    """

    def _verify_setup(self) -> Tuple[WorkerStatus, Task, Mock]:
        """Build an author worker, a verify task it may not take, and state.

        Returns
        -------
        Tuple[WorkerStatus, Task, Mock]
            The author, the verify task, and a state with an empty
            in-memory refusal list and a gated persistence layer.
        """
        find = _make_task(
            "find-1", "find", status=TaskStatus.DONE, assigned_to="finder-pplx-1"
        )
        verify = _make_task("verify-1", "verify", dependencies=["find-1"])
        author = _make_worker("finder-pplx-1", vendor="perplexity")
        state = _make_state([find, verify], [author])
        state.eligibility_refusals = []
        state.persistence = _GatedPersistence()
        return author, verify, state

    async def _rows(
        self, state: Mock, task_id: str = "verify-1"
    ) -> List[Dict[str, Any]]:
        """Read the persisted refusals for one task."""
        rows: List[Dict[str, Any]] = await state.persistence.query(
            refusals_collection(task_id), limit=10
        )
        return rows

    @pytest.mark.asyncio
    async def test_first_refusal_is_written_through_persistence(self) -> None:
        """A new refusal lands in the task's collection with its fields."""
        author, verify, state = self._verify_setup()

        filter_eligible_tasks(author, [verify], state)
        await drain_refusal_writes(state)

        rows = await self._rows(state)
        assert len(rows) == 1
        row = rows[0]
        assert row["agent_id"] == "finder-pplx-1"
        assert row["task_id"] == "verify-1"
        assert row["task_type"] == "verify"
        assert "author" in row["reason"]
        assert row["count"] == 1
        assert row["first_refused_at"] <= row["last_refused_at"]

    @pytest.mark.asyncio
    async def test_repeat_refusals_coalesce_into_the_latest_count(self) -> None:
        """Polling again updates one persisted record to the latest count."""
        author, verify, state = self._verify_setup()

        filter_eligible_tasks(author, [verify], state)
        filter_eligible_tasks(author, [verify], state)
        filter_eligible_tasks(author, [verify], state)
        await drain_refusal_writes(state)

        rows = await self._rows(state)
        assert len(rows) == 1
        assert rows[0]["count"] == 3

    @pytest.mark.asyncio
    async def test_restart_continues_the_count_from_persistence(self) -> None:
        """A fresh process with an empty in-memory list resumes the persisted count."""
        author, verify, state = self._verify_setup()
        filter_eligible_tasks(author, [verify], state)
        filter_eligible_tasks(author, [verify], state)
        await drain_refusal_writes(state)
        persisted_first = (await self._rows(state))[0]["first_refused_at"]

        restarted = _make_state(state.project_tasks, [author])
        restarted.eligibility_refusals = []
        restarted.persistence = state.persistence  # same database, new process
        filter_eligible_tasks(author, [verify], restarted)
        await drain_refusal_writes(restarted)

        rows = await self._rows(restarted)
        assert len(rows) == 1
        assert rows[0]["count"] == 3
        assert rows[0]["first_refused_at"] == persisted_first
        assert restarted.eligibility_refusals[0]["count"] == 3

    @pytest.mark.asyncio
    async def test_refusals_arriving_during_a_slow_carry_keep_the_prior_count(
        self,
    ) -> None:
        """Codex finding on PR #757: a second poll during the carry read must not win.

        With a persisted count of 100 and two new refusals while the first
        read is still pending, the record must end at 102 with the original
        first-seen time, not at 2.
        """
        author, verify, state = self._verify_setup()
        reason_probe_state = _make_state(state.project_tasks, [author])
        reason_probe_state.eligibility_refusals = []
        filter_eligible_tasks(author, [verify], reason_probe_state)
        reason = reason_probe_state.eligibility_refusals[0]["reason"]
        key = refusal_key(author.worker_id, verify.id, reason)
        await state.persistence.inner.store(
            refusals_collection(verify.id),
            key,
            {
                "agent_id": author.worker_id,
                "task_id": verify.id,
                "task_type": "verify",
                "reason": reason,
                "count": 100,
                "first_refused_at": "2026-10-14T01:00:00+00:00",
                "last_refused_at": "2026-10-14T01:30:00+00:00",
            },
        )
        state.persistence.gate.clear()  # hold every read open

        filter_eligible_tasks(author, [verify], state)
        await asyncio.sleep(0)  # let the writer start and park on the read
        filter_eligible_tasks(author, [verify], state)
        state.persistence.gate.set()
        await drain_refusal_writes(state)

        rows = await self._rows(state)
        assert rows[0]["count"] == 102
        assert rows[0]["first_refused_at"] == "2026-10-14T01:00:00+00:00"
        assert state.eligibility_refusals[0]["count"] == 102

    @pytest.mark.asyncio
    async def test_failed_first_write_retries_the_carry(self) -> None:
        """A failed read before the carry must not disable the carry.

        If the first attempt fails, the next attempt still reads the prior
        process's count; otherwise one transient error overwrites 5 with 1.
        """
        author, verify, state = self._verify_setup()
        probe = _make_state(state.project_tasks, [author])
        probe.eligibility_refusals = []
        filter_eligible_tasks(author, [verify], probe)
        reason = probe.eligibility_refusals[0]["reason"]
        await state.persistence.inner.store(
            refusals_collection(verify.id),
            refusal_key(author.worker_id, verify.id, reason),
            {
                "agent_id": author.worker_id,
                "task_id": verify.id,
                "task_type": "verify",
                "reason": reason,
                "count": 5,
                "first_refused_at": "2026-10-14T01:00:00+00:00",
                "last_refused_at": "2026-10-14T01:30:00+00:00",
            },
        )
        state.persistence.fail_retrieve_once = True

        filter_eligible_tasks(author, [verify], state)
        await drain_refusal_writes(state)

        rows = await self._rows(state)
        assert rows[0]["count"] == 6
        assert rows[0]["first_refused_at"] == "2026-10-14T01:00:00+00:00"

    @pytest.mark.asyncio
    async def test_legacy_flat_collection_row_is_carried_and_migrated(self) -> None:
        """Codex finding on PR #758: rows written by the first version still count.

        The first version kept every refusal in one flat collection. A server
        upgraded in place must continue that history: the carry reads the
        legacy row when the per-task collection has none, and the legacy row
        is removed once the per-task record is written.
        """
        author, verify, state = self._verify_setup()
        probe = _make_state(state.project_tasks, [author])
        probe.eligibility_refusals = []
        filter_eligible_tasks(author, [verify], probe)
        reason = probe.eligibility_refusals[0]["reason"]
        key = refusal_key(author.worker_id, verify.id, reason)
        await state.persistence.inner.store(
            LEGACY_REFUSALS_COLLECTION,
            key,
            {
                "agent_id": author.worker_id,
                "task_id": verify.id,
                "task_type": "verify",
                "reason": reason,
                "count": 5,
                "first_refused_at": "2026-10-14T01:00:00+00:00",
                "last_refused_at": "2026-10-14T01:30:00+00:00",
            },
        )

        filter_eligible_tasks(author, [verify], state)
        await drain_refusal_writes(state)

        rows = await self._rows(state)
        assert rows[0]["count"] == 6
        assert rows[0]["first_refused_at"] == "2026-10-14T01:00:00+00:00"
        assert (
            await state.persistence.inner.retrieve(LEGACY_REFUSALS_COLLECTION, key)
            is None
        )

    @pytest.mark.asyncio
    async def test_null_first_seen_in_the_prior_row_is_ignored(self) -> None:
        """A prior row with no first-seen time must not produce the string 'None'."""
        author, verify, state = self._verify_setup()
        probe = _make_state(state.project_tasks, [author])
        probe.eligibility_refusals = []
        filter_eligible_tasks(author, [verify], probe)
        reason = probe.eligibility_refusals[0]["reason"]
        await state.persistence.inner.store(
            refusals_collection(verify.id),
            refusal_key(author.worker_id, verify.id, reason),
            {
                "agent_id": author.worker_id,
                "task_id": verify.id,
                "task_type": "verify",
                "reason": reason,
                "count": 2,
                "first_refused_at": None,
                "last_refused_at": None,
            },
        )

        filter_eligible_tasks(author, [verify], state)
        await drain_refusal_writes(state)

        rows = await self._rows(state)
        assert rows[0]["first_refused_at"].startswith("20")
        assert rows[0]["count"] == 3

    @pytest.mark.asyncio
    async def test_in_memory_record_carries_no_private_bookkeeping(self) -> None:
        """The audit record stays a pure fact; writer state lives elsewhere."""
        author, verify, state = self._verify_setup()

        filter_eligible_tasks(author, [verify], state)
        await drain_refusal_writes(state)

        assert not any(str(k).startswith("_") for k in state.eligibility_refusals[0])

    @pytest.mark.asyncio
    async def test_different_reasons_persist_as_separate_records(self) -> None:
        """Author refusal and same-vendor refusal are different facts."""
        author, verify, state = self._verify_setup()
        same_vendor = _make_worker("verifier-pplx-2", vendor="perplexity")
        state.agent_status[same_vendor.worker_id] = same_vendor

        filter_eligible_tasks(author, [verify], state)
        filter_eligible_tasks(same_vendor, [verify], state)
        await drain_refusal_writes(state)

        rows = await self._rows(state)
        assert {r["agent_id"] for r in rows} == {"finder-pplx-1", "verifier-pplx-2"}

    @pytest.mark.asyncio
    async def test_drain_with_timeout_returns_while_a_write_is_stuck(self) -> None:
        """An orderly shutdown waits a bounded time, never forever."""
        author, verify, state = self._verify_setup()
        state.persistence.gate.clear()

        filter_eligible_tasks(author, [verify], state)
        await drain_refusal_writes(state, timeout=0.05)

        state.persistence.gate.set()
        await drain_refusal_writes(state)
        assert (await self._rows(state))[0]["count"] == 1

    def test_no_persistence_on_state_still_records_in_memory(self) -> None:
        """A state without a persistence layer keeps today's behaviour."""
        author, verify, state = self._verify_setup()
        state.persistence = None

        filter_eligible_tasks(author, [verify], state)

        assert len(state.eligibility_refusals) == 1

    def test_no_running_loop_does_not_raise(self) -> None:
        """Called outside an event loop, recording stays in-memory and quiet."""
        author, verify, state = self._verify_setup()

        filter_eligible_tasks(author, [verify], state)

        assert len(state.eligibility_refusals) == 1

    @pytest.mark.asyncio
    async def test_mock_state_without_real_persistence_is_tolerated(self) -> None:
        """A bare Mock state (as other suites use) must not schedule a non-coroutine."""
        find = _make_task(
            "find-1", "find", status=TaskStatus.DONE, assigned_to="finder-pplx-1"
        )
        verify = _make_task("verify-1", "verify", dependencies=["find-1"])
        author = _make_worker("finder-pplx-1", vendor="perplexity")
        state = _make_state([find, verify], [author])  # Mock(): persistence is a Mock

        filter_eligible_tasks(author, [verify], state)
        await drain_refusal_writes(state)

        assert len(state.eligibility_refusals) == 1
