"""
Unit tests for the evidence handoff replacing an upstream task's own keys.

When a task closes, ``_handoff_evidence_to_dependents`` copies its
evidence into the tasks that depend on it. The rule has been that
existing keys win, so a claim author can never overwrite what the board
seeded: without it a finder could ship its own excerpt and the verifier
would check the attacker's text against the page (Codex P1 on PR #739).

That rule is correct and stays. Its comment stated the assumption that
made it safe, "legitimate chain keys never collide by construction",
which holds only while a task closes once. ``request_task_redo`` resets a
DONE task to TODO, applies to any task type, and clears nothing
downstream, so a second close collides with the first by construction.
First-write-wins then keeps the superseded claim and the verifier passes
a value the author already corrected (#803).

The fix is set replacement rather than a per-key merge: a close by
upstream task T replaces the whole set of keys attributed to T on each
dependent. Keys attributed to nobody, the board-seeded set, are
untouched, and so are keys another upstream task contributed.

## Why the "before" state is read from the kanban board, not the Task object

Three earlier rounds of this fix kept the merge basis (the dependent's
"before" inputs) on the ``Task`` object the caller passed in, protected
by a per-dependent lock. That was wrong in a way Codex found on the
fourth round, and it is the reason for this design rather than a patch:
the ``Task`` object is captured from ``state.project_tasks`` by the
*caller*, BEFORE the lock is ever acquired. ``refresh_project_state``
(server.py, triggered by every ``request_next_task`` poll) can swap the
entire ``project_tasks`` list with fresh objects at any ``await`` point,
including while a handoff holds the lock — it has no idea the lock
exists. A second handoff can then acquire the very same lock, correctly,
and still operate on a stale pre-write snapshot of the dependent, because
the object it captured was fixed before the lock was ever requested.

The fix is to never trust the in-memory object for the merge basis at
all. Inside the lock, ``_apply_handoff_to_dependent`` reads the
dependent's CURRENT ``inputs`` from the kanban board itself
(``get_task_by_id``, a fresh query every call on every provider), merges
against that, and writes back. ``refresh_project_state`` only ever reads
the board and never writes ``inputs``, so once the merge basis is the
board rather than an object a caller might have captured stale, there is
nothing left for a second, uncoordinated writer to race against: the
lock now genuinely owns the one thing both the handoff and a concurrent
refresh touch.

A per-key overwrite would have been wrong in a way worth its own test
too. It covers a key present in both rounds and misses one present in
round one and absent in round two, leaving the dependent holding a mix
of two rounds. That case is reachable because ``_validate_typed_evidence``
checks only that the schema's ``required`` fields are present, so an
agent may send a different set of optional keys each time.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from src.core.models import Priority, Task, TaskStatus

pytestmark = pytest.mark.unit


def _task(task_id: str, deps: Optional[List[str]] = None, **kw: Any) -> Task:
    """Build a task with the fields the handoff touches.

    Parameters
    ----------
    task_id : str
        The task's id.
    deps : Optional[List[str]]
        Ids this task depends on.
    **kw : Any
        Overrides, notably ``inputs``.

    Returns
    -------
    Task
        A task suitable for the handoff.
    """
    task = Task(
        id=task_id,
        name=task_id,
        description="",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=1.0,
        dependencies=deps or [],
        labels=[],
    )
    task.inputs = kw.get("inputs", {})
    return task


class _Persistence:
    """In-memory stand-in for the durable provenance store."""

    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, List[str]]] = {}

    async def record_input_provenance(
        self, dependent_id: str, upstream_task_id: str, keys: List[str]
    ) -> None:
        """Record which keys an upstream task contributed."""
        self.rows.setdefault(dependent_id, {})[upstream_task_id] = list(keys)

    async def get_input_provenance(
        self, dependent_id: str, upstream_task_id: str
    ) -> List[str]:
        """Return the keys that upstream task last contributed."""
        return list(self.rows.get(dependent_id, {}).get(upstream_task_id, []))


class _Kanban:
    """A fake board: the single source of truth for a task's ``inputs``.

    This is the crux of the redesign's test double. ``board_inputs`` is
    the board's own record, independent of any ``Task`` object a test
    constructs — exactly the separation that makes the old bug
    reproducible and this fix's immunity to it provable. A test can hand
    the handoff a ``Task`` whose ``.inputs`` says one thing while the
    board says another, and assert the handoff followed the board.
    """

    def __init__(self, board_inputs: Optional[Dict[str, Dict[str, Any]]] = None):
        self.board_inputs: Dict[str, Dict[str, Any]] = {
            k: dict(v) for k, v in (board_inputs or {}).items()
        }
        self.writes: List[tuple] = []
        self.fail_next_write_for: Optional[str] = None

    async def get_task_by_id(self, task_id: str) -> Optional[Task]:
        """Return a task reflecting the board's CURRENT inputs, fresh."""
        if task_id not in self.board_inputs:
            return None
        return _task(task_id, inputs=dict(self.board_inputs[task_id]))

    async def update_task(self, task_id: str, data: Dict[str, Any]) -> None:
        """Commit a write to the board, unless told to fail this one."""
        if self.fail_next_write_for == task_id:
            self.fail_next_write_for = None
            raise RuntimeError("board unreachable")
        self.writes.append((task_id, data))
        if "inputs" in data:
            self.board_inputs[task_id] = dict(data["inputs"])


def _state(
    tasks: List[Task],
    board: Optional[_Kanban] = None,
    persistence: Optional[_Persistence] = None,
) -> Any:
    """Assemble the server state the handoff reads.

    Parameters
    ----------
    tasks : List[Task]
        The in-memory ``project_tasks``. May be deliberately stale
        relative to ``board``, to exercise the redesign's guarantee.
    board : Optional[_Kanban]
        The kanban client; built from ``tasks`` if omitted, so the
        common case (no staleness) needs no extra setup.
    persistence : Optional[_Persistence]
        The provenance store.

    Returns
    -------
    Any
        A state object.
    """
    if board is None:
        board = _Kanban({t.id: dict(t.inputs or {}) for t in tasks})
    return SimpleNamespace(
        project_tasks=tasks,
        kanban_client=board,
        assignment_persistence=persistence or _Persistence(),
    )


async def _handoff(task: Task, evidence: Dict[str, Any], state: Any) -> None:
    """Call the function under test."""
    from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

    await _handoff_evidence_to_dependents(task, evidence, state)


class TestARedoneTaskDoesNotLeaveAStaleClaim:
    """The defect in #803, reproduced against the real function."""

    @pytest.mark.asyncio
    async def test_the_second_close_wins(self) -> None:
        """A corrected claim reaches the verifier, not the superseded one."""
        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        state = _state([find, verify])

        await _handoff(find, {"claim_value": "0.862"}, state)
        # request_task_redo resets status and assignee and touches nothing else.
        find.status = TaskStatus.TODO
        find.assigned_to = None
        await _handoff(find, {"claim_value": "0.904"}, state)

        board_now = await state.kanban_client.get_task_by_id("verify-1")
        assert board_now.inputs["claim_value"] == "0.904"

    @pytest.mark.asyncio
    async def test_a_key_dropped_in_the_second_round_is_removed(self) -> None:
        """The gap a per-key overwrite would have left.

        Round one writes three keys, round two writes two. The third
        must not survive: a per-key merge updates the two it sees and
        leaves the third standing, so the dependent holds a mix of two
        rounds with nothing marking the leftover stale.
        """
        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        state = _state([find, verify])

        await _handoff(
            find,
            {"claims_path": "a.json", "report": "r1", "note": "looked odd"},
            state,
        )
        await _handoff(find, {"claims_path": "b.json", "report": "r2"}, state)

        board_now = await state.kanban_client.get_task_by_id("verify-1")
        assert board_now.inputs["claims_path"] == "b.json"
        assert board_now.inputs["report"] == "r2"
        assert "note" not in board_now.inputs

    @pytest.mark.asyncio
    async def test_the_board_write_carries_the_replacement(self) -> None:
        """The kanban row is corrected too, not only whatever the caller held."""
        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        state = _state([find, verify])

        await _handoff(find, {"claim_value": "0.862"}, state)
        await _handoff(find, {"claim_value": "0.904"}, state)

        last_id, last_data = state.kanban_client.writes[-1]
        assert last_id == "verify-1"
        assert last_data["inputs"]["claim_value"] == "0.904"


class TestItDoesNotTrustAStaleInMemoryObject:
    """The guarantee the redesign exists to make: the board wins.

    This is the direct test for the Codex finding: ``refresh_project_state``
    can hand a handoff a ``Task`` object whose ``.inputs`` is stale
    relative to the board. Rather than reproducing the async race with
    real timing (the previous rounds' approach, which is exactly what
    made the bug hard to see), this constructs the stale state directly
    and asserts the merge basis is the board, never the object.
    """

    @pytest.mark.asyncio
    async def test_a_stale_dependent_object_is_ignored_in_favour_of_the_board(
        self,
    ) -> None:
        """The object says one thing; the board says another. Board wins."""
        find = _task("find-1")
        # The board already holds round one's note, perhaps written by a
        # handoff that ran before this dependent object was captured.
        board = _Kanban({"verify-1": {"note": "from an earlier round"}})
        # The in-memory object a caller is holding is stale: it has
        # never seen that write at all.
        stale_dependent = _task("verify-1", deps=["find-1"], inputs={})
        state = _state([find, stale_dependent], board=board)

        await _handoff(find, {"claim_value": "0.904"}, state)

        board_now = await board.get_task_by_id("verify-1")
        # The board's pre-existing key survives (board-seeded-equivalent
        # for this test: nobody contributed "note" in this handoff, and
        # the merge basis is the board, which had it).
        assert board_now.inputs["note"] == "from an earlier round"
        assert board_now.inputs["claim_value"] == "0.904"

    @pytest.mark.asyncio
    async def test_the_stale_objects_own_inputs_never_reach_the_board(self) -> None:
        """If the object's stale data leaked through, this would catch it.

        The stale object here carries a key the board does NOT have.
        If the handoff merged against the object instead of the board,
        that key would resurrect. It must not.
        """
        find = _task("find-1")
        board = _Kanban({"verify-1": {}})
        stale_dependent = _task(
            "verify-1", deps=["find-1"], inputs={"ghost": "from a dead snapshot"}
        )
        state = _state([find, stale_dependent], board=board)

        await _handoff(find, {"claim_value": "0.904"}, state)

        board_now = await board.get_task_by_id("verify-1")
        assert "ghost" not in board_now.inputs


class TestTheSecurityPropertyIsUnchanged:
    """What PR #739's rule exists to stop must still be stopped."""

    @pytest.mark.asyncio
    async def test_a_board_seeded_key_still_wins(self) -> None:
        """A claim author cannot overwrite what the board seeded.

        This is the attack the first-write-wins rule was added for: a
        finder shipping its own excerpt so the verifier checks the
        attacker's text against the page.
        """
        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"], inputs={"excerpt": "the real one"})
        state = _state([find, verify])

        await _handoff(find, {"excerpt": "the attacker's text"}, state)

        board_now = await state.kanban_client.get_task_by_id("verify-1")
        assert board_now.inputs["excerpt"] == "the real one"

    @pytest.mark.asyncio
    async def test_a_board_seeded_key_survives_a_redo(self) -> None:
        """Replacement must not become a way round the rule."""
        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"], inputs={"excerpt": "the real one"})
        state = _state([find, verify])

        await _handoff(find, {"excerpt": "attempt one"}, state)
        await _handoff(find, {"excerpt": "attempt two"}, state)

        board_now = await state.kanban_client.get_task_by_id("verify-1")
        assert board_now.inputs["excerpt"] == "the real one"

    @pytest.mark.asyncio
    async def test_one_upstream_task_cannot_clear_anothers_keys(self) -> None:
        """Required test (numerical-report verifier): two upstreams, one
        dependent, each owns only what it wrote.

        A key present in round one of one upstream and absent from its
        round two is removed; the OTHER upstream's contribution, written
        in between, must survive untouched.
        """
        find = _task("find-1")
        review = _task("review-1")
        synth = _task("synth-1", deps=["find-1", "review-1"])
        state = _state([find, review, synth])

        await _handoff(find, {"claim": "c1", "note": "dropped next round"}, state)
        await _handoff(review, {"verdict": "ok"}, state)
        # find's round two drops "note".
        await _handoff(find, {"claim": "c2"}, state)

        board_now = await state.kanban_client.get_task_by_id("synth-1")
        assert board_now.inputs["claim"] == "c2"
        assert "note" not in board_now.inputs
        assert board_now.inputs["verdict"] == "ok"


class TestConcurrentHandoffsToOneDependent:
    """Two upstreams closing at once must not both own a key.

    With the merge basis now the board rather than a captured object,
    this is a narrower property than the earlier rounds needed to prove:
    the lock still has to serialise the read-merge-write sequence, or
    two concurrent handoffs could each read the board's PRE-either-write
    state and each think they are the sole writer.
    """

    @pytest.mark.asyncio
    async def test_two_concurrent_closes_do_not_lose_either_contribution(
        self,
    ) -> None:
        """Both A's and B's keys are present afterward; neither clobbers
        the other, even though both entered concurrently."""
        import asyncio

        a_in_critical_section = asyncio.Event()
        release_a = asyncio.Event()

        class _SlowBoard(_Kanban):
            async def get_task_by_id(self, task_id: str) -> Optional[Task]:
                if task_id == "dependent-1" and not a_in_critical_section.is_set():
                    a_in_critical_section.set()
                    await release_a.wait()
                return await super().get_task_by_id(task_id)

        a = _task("upstream-a")
        b = _task("upstream-b")
        dependent = _task("dependent-1", deps=["upstream-a", "upstream-b"])
        board = _SlowBoard({"dependent-1": {}})
        state = _state([a, b, dependent], board=board)

        a_task = asyncio.create_task(_handoff(a, {"from_a": "1"}, state))
        await a_in_critical_section.wait()

        # B starts while A is paused inside its read. If the lock did
        # not serialise the whole read-merge-write, B could finish
        # first and A's eventual write would then stomp it (or vice
        # versa, depending on scheduling) — either way one contribution
        # would be lost.
        b_task = asyncio.create_task(_handoff(b, {"from_b": "1"}, state))
        await asyncio.sleep(0)
        release_a.set()
        await asyncio.gather(a_task, b_task)

        board_now = await board.get_task_by_id("dependent-1")
        assert board_now.inputs.get("from_a") == "1"
        assert board_now.inputs.get("from_b") == "1"


class TestItDegradesRatherThanFailing:
    """The handoff must never break a completion, and a failed write
    must leave nothing half-done."""

    @pytest.mark.asyncio
    async def test_a_state_without_persistence_still_hands_off(self) -> None:
        """Older callers and tests pass a state with no provenance store."""
        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        board = _Kanban({"verify-1": {}})
        state = SimpleNamespace(project_tasks=[find, verify], kanban_client=board)

        await _handoff(find, {"claim": "c1"}, state)

        board_now = await board.get_task_by_id("verify-1")
        assert board_now.inputs["claim"] == "c1"

    @pytest.mark.asyncio
    async def test_a_failed_board_write_commits_nothing_and_does_not_block_the_close(
        self,
    ) -> None:
        """A failure leaves the board untouched and never reaches the
        caller. Nothing to roll back, because nothing was written
        speculatively in the first place — the redesign's simplification
        over the earlier rounds. Matching the rest of this codebase
        (#737, #667 Fix 2): a handoff failure must neither strand nor
        eject the agent whose completion triggered it."""
        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        board = _Kanban({"verify-1": {"note": "untouched"}})
        board.fail_next_write_for = "verify-1"
        state = _state([find, verify], board=board)

        await _handoff(find, {"claim": "c1"}, state)  # must not raise

        board_now = await board.get_task_by_id("verify-1")
        assert board_now.inputs == {"note": "untouched"}

    @pytest.mark.asyncio
    async def test_a_failed_write_records_no_provenance(self) -> None:
        """So a later, successful close is not blamed for a key it never
        actually wrote."""
        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        board = _Kanban({"verify-1": {}})
        board.fail_next_write_for = "verify-1"
        persistence = _Persistence()
        state = _state([find, verify], board=board, persistence=persistence)

        await _handoff(find, {"claim": "c1"}, state)  # must not raise

        recorded = await persistence.get_input_provenance("verify-1", "find-1")
        assert recorded == []

    @pytest.mark.asyncio
    async def test_a_failed_provenance_read_degrades_to_additive(self) -> None:
        """A broken provenance store must not block the handoff."""

        class _Broken(_Persistence):
            async def get_input_provenance(
                self, dependent_id: str, upstream_task_id: str
            ) -> List[str]:
                raise RuntimeError("store unavailable")

        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"], inputs={"existing": "x"})
        state = _state([find, verify], persistence=_Broken())

        await _handoff(find, {"claim": "c1"}, state)

        board_now = await state.kanban_client.get_task_by_id("verify-1")
        assert board_now.inputs["claim"] == "c1"
        assert board_now.inputs["existing"] == "x"

    @pytest.mark.asyncio
    async def test_a_failed_provenance_write_does_not_fail_the_close(self) -> None:
        """The close already committed to the board; bookkeeping after
        that must be best-effort."""

        class _Broken(_Persistence):
            async def record_input_provenance(
                self, dependent_id: str, upstream_task_id: str, keys: List[str]
            ) -> None:
                raise RuntimeError("disk full")

        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        state = _state([find, verify], persistence=_Broken())

        await _handoff(find, {"claim": "c1"}, state)

        board_now = await state.kanban_client.get_task_by_id("verify-1")
        assert board_now.inputs["claim"] == "c1"


class TestProvenanceIsRecordedForWhatActuallyLanded:
    """Attribution must match reality, not the offer."""

    @pytest.mark.asyncio
    async def test_a_losing_key_is_not_attributed_to_its_offerer(self) -> None:
        """A key the board seeded beats the payload and stays; attributing
        it to this close would let the NEXT close clear a value it never
        owned — turning the fix into a way around the rule it preserves.
        """
        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"], inputs={"excerpt": "real"})
        persistence = _Persistence()
        state = _state([find, verify], persistence=persistence)

        await _handoff(find, {"excerpt": "attacker"}, state)

        recorded = await persistence.get_input_provenance("verify-1", "find-1")
        assert "excerpt" not in recorded

    @pytest.mark.asyncio
    async def test_a_winning_key_is_attributed_to_its_author(self) -> None:
        """What actually lands is recorded, so a future close can clear it."""
        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        persistence = _Persistence()
        state = _state([find, verify], persistence=persistence)

        await _handoff(find, {"claim": "c1"}, state)

        recorded = await persistence.get_input_provenance("verify-1", "find-1")
        assert recorded == ["claim"]
