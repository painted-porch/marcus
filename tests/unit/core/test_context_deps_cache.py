"""
Unit tests for the ``analyze_dependencies`` cache in :mod:`src.core.context`.

Background
----------
Issue #626 — `request_next_task` was paying 13-15 seconds on every poll
because ``analyze_dependencies`` re-ran the LLM-backed hybrid dependency
inferer on every call, even when the task graph had not changed. This
test module verifies the cache that fixes that regression.

The cache is keyed on a stable hash of ``(task.id, sorted_dependencies)``
for every task in the input list plus the ``infer_implicit`` flag. The
key changes when the graph changes; otherwise the cached entry is
returned within a 60s TTL.
"""

import asyncio
from datetime import datetime, timezone
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.context import (
    _DEPS_CACHE_PRUNE_AGE_SECONDS,
    _DEPS_CACHE_TTL_SECONDS,
    Context,
    _deps_cache,
    _deps_cache_key,
    _deps_cache_locks,
    _prune_stale_deps_cache,
    invalidate_deps_cache,
)
from src.core.models import Priority, Task, TaskStatus


def _make_task(
    task_id: str,
    deps: Optional[List[str]] = None,
    name: Optional[str] = None,
    description: str = "",
    labels: Optional[List[str]] = None,
    provides: Optional[str] = None,
    requires: Optional[str] = None,
) -> Task:
    """Build a minimal Task with the fields required by the constructor.

    All fields that affect the cache key are exposed as kwargs so the
    cache-key tests can vary each one independently:

    - ``id``, ``dependencies`` — graph topology
    - ``name``, ``description``, ``labels`` — inputs to the pattern-
      based ``_infer_dependency`` and the hybrid LLM prompt
      (Codex P2 on PR #631)
    - ``provides``, ``requires`` — inputs to the cross-parent
      contract-wiring system from GH-320 (Kaia P3 follow-up)

    Other Task fields are filled with defensible placeholders.
    """
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name if name is not None else f"Task {task_id}",
        description=description,
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        dependencies=deps or [],
        labels=labels or [],
        provides=provides,
        requires=requires,
    )


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    """Reset the module-level cache and lock map before and after every test.

    Without this, tests would leak cache entries into each other and
    later cases would observe surprising hits. The lock map is also
    cleared so per-key compute locks from a previous test don't
    survive into the next.
    """
    invalidate_deps_cache()
    _deps_cache_locks.clear()
    yield
    invalidate_deps_cache()
    _deps_cache_locks.clear()


@pytest.fixture
def mock_hybrid_inferer():
    """Return a mock ``HybridDependencyInferer`` that counts calls.

    The mock's ``infer_dependencies`` is an ``AsyncMock`` returning a
    stable, non-empty ``adjacency_list`` so callers can compare results.
    """
    inferer = MagicMock()
    dep_graph = MagicMock()
    dep_graph.adjacency_list = {"a": ["b"], "b": []}
    inferer.infer_dependencies = AsyncMock(return_value=dep_graph)
    return inferer


@pytest.fixture
def context_with_inferer(mock_hybrid_inferer):
    """Return a ``Context`` with the mock inferer attached.

    Constructed without auto-creating a real ``HybridDependencyInferer``
    (no ``ai_engine``); the mock is assigned to ``hybrid_inferer``
    directly, mirroring the runtime arrangement in production.
    """
    ctx = Context(use_hybrid_inference=False)
    ctx.hybrid_inferer = mock_hybrid_inferer
    return ctx


# ---------------------------------------------------------------------------
# Cache-key semantics (pure function, no Context needed)
# ---------------------------------------------------------------------------


class TestDepsCacheKey:
    """Verify cache-key hashing of the input task graph."""

    def test_same_tasks_produce_same_key(self):
        """Identical task lists hash to the same cache key."""
        tasks = [_make_task("a", ["b"]), _make_task("b")]
        assert _deps_cache_key(tasks, True) == _deps_cache_key(tasks, True)

    def test_task_added_changes_key(self):
        """Adding a task changes the cache key (graph changed)."""
        tasks = [_make_task("a")]
        before = _deps_cache_key(tasks, True)
        after = _deps_cache_key(tasks + [_make_task("b")], True)
        assert before != after

    def test_task_removed_changes_key(self):
        """Removing a task changes the cache key (graph changed)."""
        tasks = [_make_task("a"), _make_task("b")]
        before = _deps_cache_key(tasks, True)
        after = _deps_cache_key([tasks[0]], True)
        assert before != after

    def test_dependency_edit_changes_key(self):
        """Editing a task's ``dependencies`` field changes the cache key."""
        before = _deps_cache_key([_make_task("a", []), _make_task("b")], True)
        after = _deps_cache_key([_make_task("a", ["b"]), _make_task("b")], True)
        assert before != after

    def test_task_ordering_does_not_change_key(self):
        """Re-ordering the input list must not change the cache key.

        The graph is a set of nodes; presentation order is irrelevant
        and would cause spurious cache misses if it leaked into the key.
        """
        tasks = [_make_task("a", ["b"]), _make_task("b")]
        assert _deps_cache_key(tasks, True) == _deps_cache_key(
            list(reversed(tasks)), True
        )

    def test_dependency_ordering_does_not_change_key(self):
        """Re-ordering an individual task's ``dependencies`` must not change the key."""
        before = _deps_cache_key([_make_task("a", ["b", "c"])], True)
        after = _deps_cache_key([_make_task("a", ["c", "b"])], True)
        assert before == after

    def test_infer_implicit_flag_changes_key(self):
        """``infer_implicit=True`` and ``=False`` must produce different keys.

        The two flags select different code paths inside
        ``analyze_dependencies`` and therefore produce different
        outputs; the cache must not conflate them.
        """
        tasks = [_make_task("a")]
        assert _deps_cache_key(tasks, True) != _deps_cache_key(tasks, False)

    def test_task_name_change_changes_key(self):
        """Renaming a task changes the cache key (Codex P2).

        The pattern-based ``_infer_dependency`` matches against task
        names; the hybrid inferer's LLM prompt is built from names.
        A name change can produce a different dependency map, so the
        cache must miss.
        """
        before = _deps_cache_key([_make_task("a", name="Build API")], True)
        after = _deps_cache_key([_make_task("a", name="Build Frontend")], True)
        assert before != after

    def test_task_description_change_changes_key(self):
        """Editing a task's description changes the cache key (Codex P2).

        The hybrid inferer's LLM prompt includes task descriptions; the
        same id/name with a different description can produce a
        different dependency map.
        """
        before = _deps_cache_key(
            [_make_task("a", description="REST endpoints for users")], True
        )
        after = _deps_cache_key(
            [_make_task("a", description="GraphQL schema for users")], True
        )
        assert before != after

    def test_task_labels_change_changes_key(self):
        """Editing a task's labels changes the cache key (Codex P2).

        ``_infer_dependency`` reads ``task.labels`` to match patterns.
        A label change can produce a different dependency map.
        """
        before = _deps_cache_key([_make_task("a", labels=["api"])], True)
        after = _deps_cache_key([_make_task("a", labels=["frontend"])], True)
        assert before != after

    def test_label_ordering_does_not_change_key(self):
        """Re-ordering an individual task's ``labels`` must not change the key.

        Labels are a set conceptually; presentation order should not
        cause spurious cache misses.
        """
        before = _deps_cache_key([_make_task("a", labels=["api", "backend"])], True)
        after = _deps_cache_key([_make_task("a", labels=["backend", "api"])], True)
        assert before == after

    def test_task_provides_change_changes_key(self):
        """Editing a task's ``provides`` contract changes the cache key.

        The cross-parent contract-wiring system (GH-320) matches one
        task's ``provides`` against another's ``requires`` to derive
        implicit dependency edges. A ``provides`` change can therefore
        change the dependency map, and the cache must miss.
        """
        before = _deps_cache_key(
            [_make_task("a", provides="GameEngine interface")], True
        )
        after = _deps_cache_key(
            [_make_task("a", provides="RenderEngine interface")], True
        )
        assert before != after

    def test_task_requires_change_changes_key(self):
        """Editing a task's ``requires`` contract changes the cache key.

        ``requires`` is the consumer side of the contract-wiring system
        (GH-320). A change can re-route which upstream task this one
        depends on, so the cache must miss.
        """
        before = _deps_cache_key(
            [_make_task("a", requires="GameEngine interface")], True
        )
        after = _deps_cache_key(
            [_make_task("a", requires="StorageEngine interface")], True
        )
        assert before != after

    def test_provides_requires_none_treated_as_empty(self):
        """A task with ``provides=None`` hashes the same as one with ``provides=""``.

        Both representations mean "no provided contract"; treating them
        as distinct would cause spurious cache misses when one Task
        constructor sets the field to ``None`` and another to the empty
        string.
        """
        key_none = _deps_cache_key([_make_task("a", provides=None)], True)
        key_empty = _deps_cache_key([_make_task("a", provides="")], True)
        assert key_none == key_empty


# ---------------------------------------------------------------------------
# End-to-end cache behavior for analyze_dependencies
# ---------------------------------------------------------------------------


class TestAnalyzeDependenciesCache:
    """Verify the cache wraps ``Context.analyze_dependencies`` correctly."""

    @pytest.mark.asyncio
    async def test_cache_hit_skips_inferer_on_second_call(self, context_with_inferer):
        """Two calls within TTL with same graph: inferer called only once."""
        tasks = [_make_task("a"), _make_task("b")]
        await context_with_inferer.analyze_dependencies(tasks)
        await context_with_inferer.analyze_dependencies(tasks)
        assert context_with_inferer.hybrid_inferer.infer_dependencies.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_hit_returns_equivalent_result(self, context_with_inferer):
        """Both calls return identical dependency maps."""
        tasks = [_make_task("a"), _make_task("b")]
        result1 = await context_with_inferer.analyze_dependencies(tasks)
        result2 = await context_with_inferer.analyze_dependencies(tasks)
        assert result1 == result2

    @pytest.mark.asyncio
    async def test_cache_miss_after_ttl_expiry(self, context_with_inferer):
        """After TTL elapses, the next call re-invokes the inferer."""
        tasks = [_make_task("a")]
        with patch("src.core.context.time.monotonic") as mock_time:
            mock_time.return_value = 0.0
            await context_with_inferer.analyze_dependencies(tasks)
            mock_time.return_value = _DEPS_CACHE_TTL_SECONDS + 1.0
            await context_with_inferer.analyze_dependencies(tasks)
        assert context_with_inferer.hybrid_inferer.infer_dependencies.call_count == 2

    @pytest.mark.asyncio
    async def test_cache_invalidated_when_task_added(self, context_with_inferer):
        """Adding a task produces a new cache key and a fresh inferer call."""
        tasks = [_make_task("a")]
        await context_with_inferer.analyze_dependencies(tasks)
        await context_with_inferer.analyze_dependencies(tasks + [_make_task("b")])
        assert context_with_inferer.hybrid_inferer.infer_dependencies.call_count == 2

    @pytest.mark.asyncio
    async def test_cache_invalidated_when_task_removed(self, context_with_inferer):
        """Removing a task produces a new cache key and a fresh inferer call."""
        tasks = [_make_task("a"), _make_task("b")]
        await context_with_inferer.analyze_dependencies(tasks)
        await context_with_inferer.analyze_dependencies([tasks[0]])
        assert context_with_inferer.hybrid_inferer.infer_dependencies.call_count == 2

    @pytest.mark.asyncio
    async def test_cache_invalidated_when_dependency_edited(self, context_with_inferer):
        """Editing a task's ``dependencies`` field bypasses the cache."""
        await context_with_inferer.analyze_dependencies(
            [_make_task("a", []), _make_task("b")]
        )
        await context_with_inferer.analyze_dependencies(
            [_make_task("a", ["b"]), _make_task("b")]
        )
        assert context_with_inferer.hybrid_inferer.infer_dependencies.call_count == 2

    @pytest.mark.asyncio
    async def test_cache_hit_returns_deep_copy_not_shared_reference(
        self, context_with_inferer
    ):
        """Mutating the returned dict must not corrupt the cached entry.

        Cached values are stored by deep copy and returned by deep
        copy. Without this, an unrelated mutation by one caller would
        appear in subsequent callers' results within the TTL window.
        """
        tasks = [_make_task("a")]
        result1 = await context_with_inferer.analyze_dependencies(tasks)
        result1["polluted_by_caller"] = ["unexpected"]
        result2 = await context_with_inferer.analyze_dependencies(tasks)
        assert "polluted_by_caller" not in result2

    @pytest.mark.asyncio
    async def test_explicit_invalidation_forces_next_call_to_miss(
        self, context_with_inferer
    ):
        """``invalidate_deps_cache()`` forces a fresh inferer call."""
        tasks = [_make_task("a")]
        await context_with_inferer.analyze_dependencies(tasks)
        invalidate_deps_cache()
        await context_with_inferer.analyze_dependencies(tasks)
        assert context_with_inferer.hybrid_inferer.infer_dependencies.call_count == 2

    @pytest.mark.asyncio
    async def test_different_infer_implicit_flags_cache_independently(
        self, context_with_inferer
    ):
        """``infer_implicit=True`` and ``=False`` calls do not share cache.

        With ``infer_implicit=False`` the hybrid inferer is bypassed
        (pattern-based fallback runs instead), but both paths produce
        cacheable output. The two flag values must hash to different
        keys, so a True-cached entry does not satisfy a False call.
        """
        tasks = [_make_task("a", ["b"]), _make_task("b")]
        await context_with_inferer.analyze_dependencies(tasks, infer_implicit=True)
        await context_with_inferer.analyze_dependencies(tasks, infer_implicit=False)
        # The True call invokes the inferer once; the False call falls
        # back to pattern matching and never touches the inferer. So
        # call_count stays at 1 across the two calls.
        assert context_with_inferer.hybrid_inferer.infer_dependencies.call_count == 1

        # Now run True again — should hit cache (still 1 call).
        await context_with_inferer.analyze_dependencies(tasks, infer_implicit=True)
        assert context_with_inferer.hybrid_inferer.infer_dependencies.call_count == 1


# ---------------------------------------------------------------------------
# Thundering-herd protection (per-key compute lock)
# ---------------------------------------------------------------------------


class TestThunderingHerd:
    """Verify the per-key compute lock prevents concurrent LLM calls.

    Marcus's primary workload is N ephemeral agents spawning together
    and each calling ``request_next_task`` near-simultaneously. Without
    the lock, all N calls would miss the cache on cold start and each
    fire its own LLM call — paying Nx the API cost for the same work.
    """

    @pytest.mark.asyncio
    async def test_concurrent_cache_miss_fires_inferer_exactly_once(self):
        """Six concurrent calls with same task graph: inferer called once.

        Models Marcus's six-agent spawn pattern. The inferer is made
        artificially slow so all six coroutines pile up behind the
        compute lock; if the lock were missing, all six would proceed
        in parallel and the assertion would fail.
        """
        ctx = Context(use_hybrid_inference=False)

        call_count = 0

        async def slow_inferer(_tasks):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            graph = MagicMock()
            graph.adjacency_list = {"a": ["b"], "b": []}
            return graph

        ctx.hybrid_inferer = MagicMock()
        ctx.hybrid_inferer.infer_dependencies = AsyncMock(side_effect=slow_inferer)

        tasks = [_make_task("a"), _make_task("b")]
        results = await asyncio.gather(
            *[ctx.analyze_dependencies(tasks) for _ in range(6)]
        )

        # All concurrent callers received an equivalent result.
        for r in results[1:]:
            assert r == results[0]

        # And the expensive compute ran exactly once.
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_calls_for_different_graphs_run_in_parallel(self):
        """Two concurrent calls with different task graphs both compute.

        The lock is per-key, so unrelated task graphs do not serialize
        through each other. Each graph fires its own compute exactly
        once; together they should fire twice in total.
        """
        ctx = Context(use_hybrid_inference=False)

        call_count = 0

        async def slow_inferer(_tasks):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.02)
            graph = MagicMock()
            graph.adjacency_list = {}
            return graph

        ctx.hybrid_inferer = MagicMock()
        ctx.hybrid_inferer.infer_dependencies = AsyncMock(side_effect=slow_inferer)

        tasks_a = [_make_task("a"), _make_task("b")]
        tasks_b = [_make_task("c"), _make_task("d")]

        await asyncio.gather(
            ctx.analyze_dependencies(tasks_a),
            ctx.analyze_dependencies(tasks_b),
        )

        # Each distinct task graph produces exactly one compute.
        assert call_count == 2


# ---------------------------------------------------------------------------
# Edge cases on the cache-key function and the prune helper
# ---------------------------------------------------------------------------


class TestCacheEdgeCases:
    """Edge cases that production code may exercise."""

    def test_empty_task_list_produces_valid_cache_key(self):
        """The cache-key function handles an empty task list without error.

        ``_compute_dependencies`` returns an empty dep_map for an empty
        task list; the cache must be able to store that result, which
        requires a valid hash for the empty list.
        """
        key = _deps_cache_key([], True)
        assert isinstance(key, str)
        # SHA-256 hex digest is always 64 hex chars.
        assert len(key) == 64

    def test_empty_and_nonempty_task_lists_hash_differently(self):
        """Empty and non-empty task lists must produce different cache keys."""
        empty_key = _deps_cache_key([], True)
        nonempty_key = _deps_cache_key([_make_task("a")], True)
        assert empty_key != nonempty_key

    def test_prune_drops_entries_older_than_prune_age(self):
        """``_prune_stale_deps_cache`` removes entries older than the prune age.

        Writes one fresh and one stale entry directly into the
        module-level cache, then runs the prune. The fresh entry must
        survive; the stale entry must be gone.
        """
        with patch("src.core.context.time.monotonic") as mock_time:
            mock_time.return_value = 1000.0
            # Fresh: just written.
            _deps_cache["fresh"] = (mock_time.return_value, {"a": []})
            # Stale: older than the prune-age threshold.
            stale_age = _DEPS_CACHE_PRUNE_AGE_SECONDS + 1.0
            _deps_cache["stale"] = (
                mock_time.return_value - stale_age,
                {"b": []},
            )

            _prune_stale_deps_cache()

            assert "fresh" in _deps_cache
            assert "stale" not in _deps_cache
