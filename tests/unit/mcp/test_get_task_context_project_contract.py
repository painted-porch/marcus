"""
Unit tests for the project-global foundation contract in get_task_context.

Issue #595 Fix 2: ``get_task_context`` only returned artifacts from a
task's *direct* dependencies, so the shared technical contract produced
by the foundation (pre-fork synthesis) tasks never reached tasks more
than one hop away. Fix 2 adds a project-global ``project_contract`` field
that returns the foundation tasks' artifacts and decisions to *every*
task, regardless of its position in the dependency graph.

Foundation tasks are identified by ``source_type == "pre_fork_synthesis"``
— the exact, Marcus-set marker. Ordinary task artifacts keep their
existing 1-hop dependency scoping.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.marcus_mcp.tools.context import (
    _collect_foundation_contract,
    get_task_context,
)

pytestmark = pytest.mark.unit


class _MockDecision:
    """Minimal architectural-decision stand-in."""

    def __init__(self, task_id: str, what: str) -> None:
        self.task_id = task_id
        self._what = what

    def to_dict(self) -> Dict[str, Any]:
        return {"task_id": self.task_id, "what": self._what}


class _MockContextResult:
    """Result returned by MockContext.get_context."""

    def to_dict(self) -> Dict[str, Any]:
        return {"previous_implementations": {}, "architectural_decisions": []}


class _MockContext:
    """Mock Context subsystem carrying a flat decision list."""

    def __init__(self) -> None:
        self.decisions: List[_MockDecision] = []

    async def get_context(
        self, task_id: str, dependencies: List[str]
    ) -> _MockContextResult:
        return _MockContextResult()


class _MockSubtaskManager:
    """Mock SubtaskManager exposing the surface get_task_context touches."""

    def __init__(self, subtask_id: str, parent_task_id: str) -> None:
        self.subtasks = {subtask_id: object()}
        self._subtask_id = subtask_id
        self._parent_task_id = parent_task_id

    def get_subtask_context(self, task_id: str) -> Dict[str, Any]:
        return {
            "parent_task_id": self._parent_task_id,
            "subtask": {"id": task_id, "name": "Sub"},
            "shared_conventions": {},
            "dependency_artifacts": [],
            "sibling_subtasks": [],
        }

    def get_subtasks(self, parent_task_id: str, project_tasks: List[Task]) -> List[Any]:
        return []


class _MockState:
    """Mock Marcus server state."""

    def __init__(self) -> None:
        self.task_artifacts: Dict[str, List[Dict[str, Any]]] = {}
        self.project_tasks: List[Task] = []
        self.context = _MockContext()
        self.kanban_client = None
        self.events = None
        self.subtask_manager: Any = None


def _task(
    task_id: str,
    *,
    source_type: str = "",
    dependencies: List[str] | None = None,
) -> Task:
    """Build a Task with the fields get_task_context touches."""
    return Task(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=1.0,
        dependencies=dependencies or [],
        source_type=source_type,
    )


@pytest.fixture()
def state() -> _MockState:
    """Provide a fresh mock state per test."""
    return _MockState()


class TestCollectFoundationContract:
    """_collect_foundation_contract gathers only foundation-task output."""

    @pytest.mark.asyncio
    async def test_collects_artifacts_from_foundation_tasks(
        self, state: _MockState
    ) -> None:
        """Artifacts from pre_fork_synthesis tasks are returned."""
        state.project_tasks = [_task("f1", source_type="pre_fork_synthesis")]
        state.task_artifacts["f1"] = [
            {"filename": "tsconfig.json", "artifact_type": "specification"}
        ]

        contract = await _collect_foundation_contract(state)

        assert [a["filename"] for a in contract["artifacts"]] == ["tsconfig.json"]

    @pytest.mark.asyncio
    async def test_collects_decisions_from_foundation_tasks(
        self, state: _MockState
    ) -> None:
        """Decisions made on a foundation task are returned."""
        state.project_tasks = [_task("f1", source_type="pre_fork_synthesis")]
        state.context.decisions = [
            _MockDecision("f1", "Public API surface"),
            _MockDecision("other", "unrelated decision"),
        ]

        contract = await _collect_foundation_contract(state)

        assert [d["what"] for d in contract["decisions"]] == ["Public API surface"]

    @pytest.mark.asyncio
    async def test_excludes_non_foundation_task_artifacts(
        self, state: _MockState
    ) -> None:
        """Artifacts from ordinary (non-foundation) tasks are not global."""
        state.project_tasks = [
            _task("f1", source_type="pre_fork_synthesis"),
            _task("impl1", source_type="pre_fork_synthesis_NOT"),
            _task("design1"),  # no source_type
        ]
        state.task_artifacts["f1"] = [
            {"filename": "package.json", "artifact_type": "specification"}
        ]
        state.task_artifacts["impl1"] = [{"filename": "feature.ts"}]
        state.task_artifacts["design1"] = [{"filename": "design.md"}]

        contract = await _collect_foundation_contract(state)

        assert [a["filename"] for a in contract["artifacts"]] == ["package.json"]

    @pytest.mark.asyncio
    async def test_empty_when_no_foundation_tasks(self, state: _MockState) -> None:
        """No foundation tasks yields empty artifact and decision lists."""
        state.project_tasks = [_task("impl1")]
        state.task_artifacts["impl1"] = [{"filename": "feature.ts"}]

        contract = await _collect_foundation_contract(state)

        assert contract == {"artifacts": [], "decisions": []}


class TestGetTaskContextProjectContract:
    """get_task_context exposes project_contract regardless of DAG position."""

    @pytest.mark.asyncio
    async def test_task_with_no_foundation_dependency_still_gets_contract(
        self, state: _MockState
    ) -> None:
        """
        A task that does NOT depend on the foundation task still receives
        the foundation contract via the global project_contract field.

        This is the core #595 Fix 2 behavior: contract reaches every task,
        not only the foundation task's direct dependents.
        """
        foundation = _task("f1", source_type="pre_fork_synthesis")
        # leaf task depends on nothing — several hops from foundation
        leaf = _task("leaf", dependencies=[])
        state.project_tasks = [foundation, leaf]
        state.task_artifacts["f1"] = [
            {"filename": "tsconfig.json", "artifact_type": "specification"}
        ]
        state.context.decisions = [_MockDecision("f1", "Public API surface")]

        result = await get_task_context("leaf", state)

        assert result["success"] is True
        contract = result["context"]["project_contract"]
        assert [a["filename"] for a in contract["artifacts"]] == ["tsconfig.json"]
        assert [d["what"] for d in contract["decisions"]] == ["Public API surface"]

    @pytest.mark.asyncio
    async def test_project_contract_empty_without_foundation(
        self, state: _MockState
    ) -> None:
        """project_contract is present but empty when no foundation exists."""
        state.project_tasks = [_task("leaf")]

        result = await get_task_context("leaf", state)

        assert result["success"] is True
        assert result["context"]["project_contract"] == {
            "artifacts": [],
            "decisions": [],
        }

    @pytest.mark.asyncio
    async def test_subtask_also_receives_project_contract(
        self, state: _MockState
    ) -> None:
        """
        A subtask receives the foundation contract via project_contract.

        get_task_context has a separate code path for subtasks; Fix 2
        wires project_contract into that path too, so a subtask is not
        cut off from the project-global contract.
        """
        state.project_tasks = [_task("f1", source_type="pre_fork_synthesis")]
        state.task_artifacts["f1"] = [{"filename": "tsconfig.json"}]
        state.subtask_manager = _MockSubtaskManager("sub-1", "parent-1")

        result = await get_task_context("sub-1", state)

        assert result["success"] is True
        assert result["context"]["is_subtask"] is True
        contract = result["context"]["project_contract"]
        assert [a["filename"] for a in contract["artifacts"]] == ["tsconfig.json"]


class TestCodexFixesOnProjectContract:
    """Codex P1 + P2 regression guards on PR #622.

    Two issues flagged on the develop -> main release PR:

    - P1: skipping ``pre_fork_synthesis`` deps in the direct-dependency
      path dropped Kanban-board attachments (not just logged artifacts).
      ``_collect_foundation_contract`` must compensate by reading both
      ``state.task_artifacts`` AND ``state.kanban_client.get_attachments``
      for each foundation task.
    - P2: foundation artifacts must be filtered by
      ``_is_architectural_artifact`` like the other context tiers do,
      since the project_contract is broadcast to every task. Non-
      architectural (temporary / code-like) artifacts would otherwise
      leak project-wide.
    """

    @pytest.mark.asyncio
    async def test_p1_pulls_kanban_attachments_from_foundation_tasks(
        self, state: _MockState
    ) -> None:
        """Board-attached foundation docs must reach project_contract."""
        from unittest.mock import AsyncMock

        state.project_tasks = [_task("f1", source_type="pre_fork_synthesis")]
        state.task_artifacts["f1"] = []  # no logged artifacts

        # Mock kanban_client.get_attachments — return CANONICAL
        # provider shape per ``src/integrations/kanban_interface.py``:
        # ``{"id", "filename", "url", "content_type", "size",
        # "created_at", "created_by"}``. Codex P2 follow-up on PR
        # #623 caught the earlier mock used Planka-style raw keys
        # (``name`` / ``userId`` / ``createdAt``) which masked a
        # real field-mapping bug — the code would have read None
        # against the canonical interface.
        state.kanban_client = AsyncMock()
        state.kanban_client.get_attachments = AsyncMock(
            return_value={
                "success": True,
                "data": [
                    {
                        "id": "att-1",
                        "filename": "foundation-contract.md",
                        "url": "/storage/foundation-contract.md",
                        "content_type": "text/markdown",
                        "size": 1024,
                        "created_by": "user-1",
                        "created_at": "2026-05-23T12:00:00Z",
                    }
                ],
            }
        )

        contract = await _collect_foundation_contract(state)

        # Verify the foundation collector calls ``get_attachments``
        # with the canonical ``task_id=`` keyword — NOT Planka-style
        # ``card_id=`` which would TypeError against the documented
        # interface (Codex P1 follow-up on PR #623).
        state.kanban_client.get_attachments.assert_called_once_with(task_id="f1")

        assert any(
            a.get("filename") == "foundation-contract.md" for a in contract["artifacts"]
        ), (
            "Codex P1: Kanban-board attachments on foundation tasks "
            "must appear in project_contract. Without this, foundation "
            "documents attached on the board (not logged via "
            "log_artifact) are silently dropped."
        )
        # Stamped with the architectural type so future filter changes
        # don't accidentally exclude them.
        attach = next(
            a
            for a in contract["artifacts"]
            if a.get("filename") == "foundation-contract.md"
        )
        assert attach.get("artifact_type") == "reference"
        assert attach.get("storage_type") == "attachment"
        # Provider's canonical ``url`` field is preserved as
        # ``location`` — not overwritten with a synthetic path when
        # the real one is available.
        assert attach.get("location") == "/storage/foundation-contract.md"
        # Canonical-key reads land on the right keys: ``created_by``
        # / ``created_at`` (NOT Planka raw ``userId`` / ``createdAt``).
        assert attach.get("created_by") == "user-1"
        assert attach.get("created_at") == "2026-05-23T12:00:00Z"

    @pytest.mark.asyncio
    async def test_p2_non_architectural_artifacts_are_filtered_out(
        self, state: _MockState
    ) -> None:
        """Temporary / code-like artifacts must NOT reach project_contract.

        Other tiers (dependency, transitive) already filter to
        architectural types via :func:`_is_architectural_artifact`. The
        project_contract is broadcast to every task, so it must apply
        the same filter — otherwise non-architectural artifacts leak
        project-wide.
        """
        state.project_tasks = [_task("f1", source_type="pre_fork_synthesis")]
        state.task_artifacts["f1"] = [
            {"filename": "tsconfig.json", "artifact_type": "specification"},
            {"filename": "scratch.py", "artifact_type": "temporary"},
            {"filename": "snippet.ts", "artifact_type": "source_code"},
        ]

        contract = await _collect_foundation_contract(state)

        filenames = {a["filename"] for a in contract["artifacts"]}
        assert "tsconfig.json" in filenames
        assert "scratch.py" not in filenames, (
            "Codex P2: non-architectural artifact 'temporary' leaked "
            "into project_contract."
        )
        assert "snippet.ts" not in filenames, (
            "Codex P2: non-architectural artifact 'source_code' leaked "
            "into project_contract."
        )

    @pytest.mark.asyncio
    async def test_kanban_attachment_failure_does_not_crash_contract(
        self, state: _MockState
    ) -> None:
        """Graceful degrade when kanban backend errors fetching attachments.

        The context-delivery hot path must NEVER raise on a transient
        kanban error — degrade to logged-artifacts only, log a warning.
        """
        from unittest.mock import AsyncMock

        state.project_tasks = [_task("f1", source_type="pre_fork_synthesis")]
        state.task_artifacts["f1"] = [
            {"filename": "tsconfig.json", "artifact_type": "specification"}
        ]
        state.kanban_client = AsyncMock()
        state.kanban_client.get_attachments = AsyncMock(
            side_effect=RuntimeError("kanban transient error")
        )

        # Must NOT raise.
        contract = await _collect_foundation_contract(state)

        # Logged artifact still delivered; kanban error swallowed.
        assert [a["filename"] for a in contract["artifacts"]] == ["tsconfig.json"]


class TestFoundationContractCaching:
    """v0.3.8.post1 perf hotfix: foundation contract must be cached per
    project so ``request_next_task`` doesn't re-walk SQLite + re-fetch
    kanban attachments on every call.

    Pre-cache observed: context_building took 12-15 seconds per call
    against snake-completion-1 (3 foundation tasks × ~4s kanban attachment
    query). Goal: <100ms after first call.
    """

    @pytest.mark.asyncio
    async def test_cache_hit_skips_kanban_fetch_on_second_call(
        self, state: _MockState
    ) -> None:
        """Same project_id within TTL → cached contract, no second
        kanban fetch."""
        from unittest.mock import AsyncMock

        state.project_tasks = [_task("f1", source_type="pre_fork_synthesis")]
        state.task_artifacts["f1"] = [
            {"filename": "tsconfig.json", "artifact_type": "specification"}
        ]
        state.kanban_client = AsyncMock()
        state.kanban_client.get_attachments = AsyncMock(
            return_value={"success": True, "data": []}
        )
        state.current_project_id = "proj-perf-test-1"

        # First call: computes + caches.
        contract1 = await _collect_foundation_contract(state)
        assert contract1["artifacts"]
        first_call_count = state.kanban_client.get_attachments.call_count

        # Second call: should hit cache, NOT re-call kanban.
        contract2 = await _collect_foundation_contract(state)
        second_call_count = state.kanban_client.get_attachments.call_count

        assert second_call_count == first_call_count, (
            f"Foundation contract cache miss: kanban.get_attachments "
            f"called {second_call_count - first_call_count} extra times "
            f"on second call. Cache MUST hit when project_id is unchanged."
        )
        # Same content returned.
        assert contract1 == contract2

    @pytest.mark.asyncio
    async def test_different_project_id_rebuilds_cache(self, state: _MockState) -> None:
        """Switching project_id triggers a fresh compute."""
        from unittest.mock import AsyncMock

        state.project_tasks = [_task("f1", source_type="pre_fork_synthesis")]
        state.task_artifacts["f1"] = [
            {"filename": "tsconfig.json", "artifact_type": "specification"}
        ]
        state.kanban_client = AsyncMock()
        state.kanban_client.get_attachments = AsyncMock(
            return_value={"success": True, "data": []}
        )
        state.current_project_id = "proj-A"

        await _collect_foundation_contract(state)
        calls_after_A = state.kanban_client.get_attachments.call_count

        # Switch project_id; cache must NOT serve cross-project.
        state.current_project_id = "proj-B"
        await _collect_foundation_contract(state)
        calls_after_B = state.kanban_client.get_attachments.call_count

        assert calls_after_B > calls_after_A, (
            "Foundation contract cache must NOT serve cross-project; "
            "switching project_id should trigger a fresh compute."
        )

    @pytest.mark.asyncio
    async def test_no_project_id_does_not_break(self, state: _MockState) -> None:
        """Defensive: if state has no current_project_id, helper still
        works (no cache, computed fresh every call) — never crashes."""
        from unittest.mock import AsyncMock

        state.project_tasks = [_task("f1", source_type="pre_fork_synthesis")]
        state.task_artifacts["f1"] = [
            {"filename": "tsconfig.json", "artifact_type": "specification"}
        ]
        state.kanban_client = AsyncMock()
        state.kanban_client.get_attachments = AsyncMock(
            return_value={"success": True, "data": []}
        )
        # Explicitly NOT setting state.current_project_id.

        # Both calls should succeed without raising.
        c1 = await _collect_foundation_contract(state)
        c2 = await _collect_foundation_contract(state)
        assert c1["artifacts"] and c2["artifacts"]


class TestCachePostMergeFixes:
    """Three correctness fixes applied post-Kaia-self-review on PR #625:

    1. Cache hit returns a DEEP COPY so caller mutations don't corrupt
       the shared cache entry.
    2. Empty-foundation result is cached too, so projects without
       foundation tasks don't re-scan ``state.project_tasks`` per
       request.
    3. Cache invalidation hook fires on ``state.project_tasks``
       reassignment from the kanban refresh path (verified separately
       in ``test_server_refresh_state.py``; here we only verify the
       cache helper exposes the ``invalidate_foundation_contract_cache``
       API the refresh path calls).
    """

    @pytest.mark.asyncio
    async def test_cache_hit_returns_independent_copy(self, state: _MockState) -> None:
        """Caller mutation of the returned dict must NOT corrupt the
        cached entry (deep-copy on hit)."""
        from unittest.mock import AsyncMock

        state.project_tasks = [_task("f1", source_type="pre_fork_synthesis")]
        state.task_artifacts["f1"] = [
            {"filename": "tsconfig.json", "artifact_type": "specification"}
        ]
        state.kanban_client = AsyncMock()
        state.kanban_client.get_attachments = AsyncMock(
            return_value={"success": True, "data": []}
        )
        state.current_project_id = "proj-copy-1"

        # First call: computes + caches.
        contract1 = await _collect_foundation_contract(state)
        original_artifact_count = len(contract1["artifacts"])

        # Corrupt the returned dict.
        contract1["artifacts"].append({"filename": "MUTATED.txt"})
        contract1["decisions"].append({"task_id": "MUTATED"})

        # Second call: should NOT see the mutations from contract1.
        contract2 = await _collect_foundation_contract(state)
        assert len(contract2["artifacts"]) == original_artifact_count, (
            f"Cache returned a SHARED reference: caller-mutation of "
            f"contract1 leaked into the cached entry. "
            f"contract2 artifacts: {contract2['artifacts']}"
        )
        assert not any(
            a.get("filename") == "MUTATED.txt" for a in contract2["artifacts"]
        )
        assert not any(d.get("task_id") == "MUTATED" for d in contract2["decisions"])

    @pytest.mark.asyncio
    async def test_empty_foundation_result_is_cached(self, state: _MockState) -> None:
        """No-foundation projects must NOT re-scan project_tasks per call.

        Tracked at the helper level by ensuring the second call to
        ``_collect_foundation_contract`` does NOT iterate
        ``state.project_tasks`` again — verified by setting
        ``project_tasks`` to a list that tracks iteration.
        """
        state.current_project_id = "proj-empty-1"

        scan_count = [0]

        class _CountingList(list):
            def __iter__(self):
                scan_count[0] += 1
                return list.__iter__(self)

        state.project_tasks = _CountingList()  # empty, but counts iterations

        # First call: misses cache, scans (empty) project_tasks once.
        await _collect_foundation_contract(state)
        first_scans = scan_count[0]

        # Second call: should hit cache, NOT scan again.
        await _collect_foundation_contract(state)
        second_scans = scan_count[0]

        assert second_scans == first_scans, (
            f"Empty-foundation result was not cached: project_tasks "
            f"got iterated {second_scans - first_scans} extra times "
            f"on second call."
        )

    @pytest.mark.asyncio
    async def test_log_decision_on_foundation_task_invalidates_cache(
        self, state: _MockState
    ) -> None:
        """Codex P2 on PR #625: ``log_decision`` against a foundation
        task must invalidate the foundation-contract cache.

        Without this, decisions logged on foundation tasks are hidden
        from the next ``get_task_context`` for up to
        ``_FOUNDATION_CONTRACT_CACHE_TTL_SECONDS``, and agents building
        against the foundation see stale architecture guidance for that
        window. Mirrors the existing ``log_artifact`` invalidation hook.
        """
        from unittest.mock import AsyncMock

        from src.marcus_mcp.tools.context import log_decision

        foundation = _task("f1", source_type="pre_fork_synthesis")
        state.project_tasks = [foundation]
        state.task_artifacts["f1"] = []
        state.kanban_client = None  # skip kanban-comment path
        state.current_project_id = "proj-decision-invalidation"

        # Pre-cache the foundation contract by calling once.
        await _collect_foundation_contract(state)

        # Mock ``state.context.log_decision`` so we can drive the
        # function without a real Context subsystem.
        class _LoggedDecision:
            decision_id = "dec-1"

            def to_dict(self) -> Dict[str, Any]:
                return {"id": "dec-1"}

        async def fake_log(**kwargs: Any) -> _LoggedDecision:
            # Side-effect: append the decision to state.context.decisions
            # so a recompute would see it.
            state.context.decisions.append(_MockDecision("f1", "use TypeScript"))
            return _LoggedDecision()

        state.context.log_decision = AsyncMock(  # type: ignore[attr-defined]
            side_effect=fake_log
        )

        # Log a decision against the foundation task.
        result = await log_decision("agent-1", "f1", "use TypeScript", state)
        assert result["success"] is True

        # Next call must see the new decision (cache invalidated).
        contract = await _collect_foundation_contract(state)
        assert any(d.get("what") == "use TypeScript" for d in contract["decisions"]), (
            "Codex P2: log_decision on a foundation task must "
            "invalidate the foundation-contract cache so the next "
            "get_task_context sees the new decision."
        )

    @pytest.mark.asyncio
    async def test_log_decision_on_NON_foundation_task_does_not_invalidate(
        self, state: _MockState
    ) -> None:
        """Cache invalidation must be SCOPED to foundation tasks only.

        A decision on a regular implement-task should NOT bust the
        foundation-contract cache; otherwise the cache is useless
        because every agent decision triggers a recompute.
        """
        from unittest.mock import AsyncMock

        from src.marcus_mcp.tools.context import (
            _foundation_contract_cache,
            log_decision,
        )

        # Foundation task + a non-foundation task; only the latter
        # gets the decision.
        foundation = _task("f1", source_type="pre_fork_synthesis")
        regular = _task("impl1")  # no source_type → not foundation
        state.project_tasks = [foundation, regular]
        state.task_artifacts["f1"] = [
            {"filename": "tsconfig.json", "artifact_type": "specification"}
        ]
        state.kanban_client = None
        state.current_project_id = "proj-scope-test"

        # Warm the cache.
        await _collect_foundation_contract(state)
        assert "proj-scope-test" in _foundation_contract_cache

        class _LoggedDecision:
            decision_id = "dec-1"

            def to_dict(self) -> Dict[str, Any]:
                return {"id": "dec-1"}

        async def fake_log(**kwargs: Any) -> _LoggedDecision:
            return _LoggedDecision()

        state.context.log_decision = AsyncMock(  # type: ignore[attr-defined]
            side_effect=fake_log
        )

        # Log a decision against the NON-foundation task.
        await log_decision("agent-1", "impl1", "use vanilla JS", state)

        # Cache entry for proj-scope-test must STILL be present
        # (decision was on regular task, not foundation).
        assert "proj-scope-test" in _foundation_contract_cache, (
            "Cache was invalidated for a non-foundation decision; "
            "invalidation must be scoped to foundation tasks only."
        )

    def test_invalidate_cache_api_is_exposed(self) -> None:
        """The ``invalidate_foundation_contract_cache`` function must
        be importable and callable — the kanban-refresh path in
        ``server.py`` depends on it."""
        from src.marcus_mcp.tools.context import (
            invalidate_foundation_contract_cache,
        )

        # Both signatures must work: with a project_id and without.
        invalidate_foundation_contract_cache("proj-X")
        invalidate_foundation_contract_cache(None)
        invalidate_foundation_contract_cache()  # default = clear all


class TestCacheTTLValue:
    """Pin the cache TTL value (regression guard).

    Background — verify-snake-5 audit (2026-05-25)
    -----------------------------------------------
    The original PR #625 set ``_FOUNDATION_CONTRACT_CACHE_TTL_SECONDS``
    to 60.0 as a conservative safety net.  Empirical measurement on
    test67 (verify-snake-5) showed the 60s TTL hits only ~33% of the
    time because ephemeral agents space their ``request_next_task``
    calls 1-5 minutes apart while working in their worktrees.  6 of 9
    request_next_task calls paid the ~13s context_building penalty.

    The TTL was bumped to 600s (10 minutes).  This is safe because all
    three write surfaces (log_artifact, log_decision, server-level
    project refresh) invalidate explicitly; the TTL only governs
    "missed invalidation" cases which are the same risk at 60s as at
    600s.

    These tests pin both the value (so a future PR doesn't silently
    re-narrow it) and the safety property (TTL > expected agent
    inter-claim gap).
    """

    def test_ttl_value_matches_audit_decision(self) -> None:
        """TTL is 600 seconds post-2026-05-25 audit.

        Regression guard against accidentally re-narrowing back to
        60s (the original conservative value).  Change this assertion
        only with an explicit rationale logged in the constant's
        docstring.
        """
        from src.marcus_mcp.tools.context import (
            _FOUNDATION_CONTRACT_CACHE_TTL_SECONDS,
        )

        assert _FOUNDATION_CONTRACT_CACHE_TTL_SECONDS == 600.0

    def test_ttl_exceeds_typical_agent_inter_claim_gap(self) -> None:
        """TTL must be longer than the typical gap between agent claims.

        Ephemeral-agent lifecycle (PR #600) spaces ``request_next_task``
        calls by however long an agent works on a task — typically
        1-5 minutes for trivial-spec projects, up to 10-15 minutes
        for richer tasks.  A TTL shorter than the inter-claim gap
        means most claims miss the cache.

        The runner's stall watchdog at ``run_experiment.py`` allows
        spawns up to 20 minutes apart.  A TTL of 600s (10 min) covers
        the typical case without artificially constraining the
        ceiling.
        """
        from src.marcus_mcp.tools.context import (
            _FOUNDATION_CONTRACT_CACHE_TTL_SECONDS,
        )

        # Inter-claim gap observed in test67 ranged 30s to >5 min;
        # TTL must comfortably exceed the typical case (5 min).
        assert _FOUNDATION_CONTRACT_CACHE_TTL_SECONDS >= 300.0
