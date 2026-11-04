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

A per-key overwrite would have been wrong in a way worth a test of its
own. It covers a key present in both rounds and misses one present in
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
    """Records the writes the handoff makes."""

    def __init__(self) -> None:
        self.writes: List[tuple] = []

    async def update_task(self, task_id: str, data: Dict[str, Any]) -> None:
        """Capture a board write."""
        self.writes.append((task_id, data))


def _state(tasks: List[Task], persistence: Optional[_Persistence] = None) -> Any:
    """Assemble the server state the handoff reads."""
    return SimpleNamespace(
        project_tasks=tasks,
        kanban_client=_Kanban(),
        assignment_persistence=persistence or _Persistence(),
    )


class TestARedoneTaskDoesNotLeaveAStaleClaim:
    """The defect in #803, reproduced against the real function."""

    @pytest.mark.asyncio
    async def test_the_second_close_wins(self) -> None:
        """A corrected claim reaches the verifier, not the superseded one."""
        from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        state = _state([find, verify])

        await _handoff_evidence_to_dependents(find, {"claim_value": "0.862"}, state)
        # request_task_redo resets status and assignee and touches nothing else.
        find.status = TaskStatus.TODO
        find.assigned_to = None
        await _handoff_evidence_to_dependents(find, {"claim_value": "0.904"}, state)

        assert verify.inputs["claim_value"] == "0.904"

    @pytest.mark.asyncio
    async def test_a_key_dropped_in_the_second_round_is_removed(self) -> None:
        """The gap a per-key overwrite would have left.

        Round one writes three keys, round two writes two. The third
        must not survive: a per-key merge updates the two it sees and
        leaves the third standing, so the dependent holds a mix of two
        rounds with nothing marking the leftover stale.
        """
        from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        state = _state([find, verify])

        await _handoff_evidence_to_dependents(
            find,
            {"claims_path": "a.json", "report": "r1", "note": "looked odd"},
            state,
        )
        await _handoff_evidence_to_dependents(
            find, {"claims_path": "b.json", "report": "r2"}, state
        )

        assert verify.inputs["claims_path"] == "b.json"
        assert verify.inputs["report"] == "r2"
        assert "note" not in verify.inputs

    @pytest.mark.asyncio
    async def test_the_board_write_carries_the_replacement(self) -> None:
        """The kanban row is corrected too, not only the memory copy."""
        from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        state = _state([find, verify])

        await _handoff_evidence_to_dependents(find, {"claim_value": "0.862"}, state)
        await _handoff_evidence_to_dependents(find, {"claim_value": "0.904"}, state)

        last_id, last_data = state.kanban_client.writes[-1]
        assert last_id == "verify-1"
        assert last_data["inputs"]["claim_value"] == "0.904"


class TestTheSecurityPropertyIsUnchanged:
    """What PR #739's rule exists to stop must still be stopped."""

    @pytest.mark.asyncio
    async def test_a_board_seeded_key_still_wins(self) -> None:
        """A claim author cannot overwrite what the board seeded.

        This is the attack the first-write-wins rule was added for: a
        finder shipping its own excerpt so the verifier checks the
        attacker's text against the page.
        """
        from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"], inputs={"excerpt": "the real one"})
        state = _state([find, verify])

        await _handoff_evidence_to_dependents(
            find, {"excerpt": "the attacker's text"}, state
        )

        assert verify.inputs["excerpt"] == "the real one"

    @pytest.mark.asyncio
    async def test_a_board_seeded_key_survives_a_redo(self) -> None:
        """Replacement must not become a way round the rule."""
        from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"], inputs={"excerpt": "the real one"})
        state = _state([find, verify])

        await _handoff_evidence_to_dependents(find, {"excerpt": "attempt one"}, state)
        await _handoff_evidence_to_dependents(find, {"excerpt": "attempt two"}, state)

        assert verify.inputs["excerpt"] == "the real one"

    @pytest.mark.asyncio
    async def test_one_upstream_task_cannot_clear_anothers_keys(self) -> None:
        """Replacement is scoped to the closing task's own contribution."""
        from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

        find = _task("find-1")
        review = _task("review-1")
        synth = _task("synth-1", deps=["find-1", "review-1"])
        state = _state([find, review, synth])

        await _handoff_evidence_to_dependents(find, {"claim": "c1"}, state)
        await _handoff_evidence_to_dependents(review, {"verdict": "ok"}, state)
        await _handoff_evidence_to_dependents(find, {"claim": "c2"}, state)

        assert synth.inputs["claim"] == "c2"
        assert synth.inputs["verdict"] == "ok"


class TestTheProvenanceIsDurable:
    """ "The keys T wrote" has to survive a restart."""

    @pytest.mark.asyncio
    async def test_the_contributed_keys_are_recorded(self) -> None:
        """A close records what it contributed, per dependent."""
        from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"], inputs={"excerpt": "seeded"})
        persistence = _Persistence()
        state = _state([find, verify], persistence)

        await _handoff_evidence_to_dependents(find, {"claim": "c1"}, state)

        recorded = await persistence.get_input_provenance("verify-1", "find-1")
        assert "claim" in recorded
        assert "excerpt" not in recorded

    @pytest.mark.asyncio
    async def test_a_restart_does_not_lose_the_replacement(self) -> None:
        """A fresh in-memory state still clears the first round's keys.

        The store is the only thing carrying the attribution across a
        restart, since the board row holds the merged inputs and nothing
        about who wrote which key.
        """
        from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

        persistence = _Persistence()
        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        await _handoff_evidence_to_dependents(
            find, {"claim": "c1", "note": "n1"}, _state([find, verify], persistence)
        )

        # Restart: new task objects, inputs reloaded from the board.
        find_again = _task("find-1")
        verify_again = _task("verify-1", deps=["find-1"], inputs=dict(verify.inputs))
        await _handoff_evidence_to_dependents(
            find_again, {"claim": "c2"}, _state([find_again, verify_again], persistence)
        )

        assert verify_again.inputs["claim"] == "c2"
        assert "note" not in verify_again.inputs


class TestAFailedBoardWriteDoesNotStrandAKey:
    """Provenance must not narrow before the board write succeeds.

    Codex P2 on PR #807. The provenance write used to happen first, so a
    re-close that dropped an optional key and then failed to reach the
    board recorded the narrower set anyway. The board still held the
    dropped key, ``refresh_project_state`` reloaded it, and because the
    narrowed provenance no longer attributed it to anyone, every later
    close treated the resurrected key as board-seeded and could never
    clear it.
    """

    @pytest.mark.asyncio
    async def test_a_key_survives_an_outage_and_is_still_cleared(self) -> None:
        """The exact sequence from the finding, asserting its invariant."""
        from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

        class _Flaky(_Kanban):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next = False

            async def update_task(self, task_id: str, data: Dict[str, Any]) -> None:
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("board unreachable")
                await super().update_task(task_id, data)

        persistence = _Persistence()
        board = _Flaky()
        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        state = SimpleNamespace(
            project_tasks=[find, verify],
            kanban_client=board,
            assignment_persistence=persistence,
        )

        await _handoff_evidence_to_dependents(
            find, {"claim": "c1", "note": "n1"}, state
        )
        # Round two drops ``note`` and never reaches the board.
        board.fail_next = True
        await _handoff_evidence_to_dependents(find, {"claim": "c2"}, state)

        # refresh_project_state reloads the board's copy, which still has
        # the key round two meant to drop.
        last_written = board.writes[-1][1]["inputs"]
        verify.inputs = dict(last_written)
        assert verify.inputs["note"] == "n1"

        await _handoff_evidence_to_dependents(find, {"claim": "c3"}, state)

        assert verify.inputs["claim"] == "c3"
        assert "note" not in verify.inputs


class TestAFailedWriteDoesNotFreeAKeyForReassignment:
    """A failed board write must not let another upstream claim the key.

    Codex P2 on PR #807, second round, and a consequence of the fix for
    the first. The in-memory copy was still mutated when the board write
    failed, so the key vanished from memory while the board kept it and
    its owner kept the record. Another upstream then contributed the
    same key, successfully, and both records claimed it. The first
    upstream's next close deleted the second's persisted value, breaking
    the guarantee that one upstream cannot clear another's contribution.
    """

    @pytest.mark.asyncio
    async def test_a_failed_removal_does_not_hand_the_key_to_another_task(
        self,
    ) -> None:
        """The exact sequence from the finding."""
        from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

        class _Flaky(_Kanban):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next = False

            async def update_task(self, task_id: str, data: Dict[str, Any]) -> None:
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("board unreachable")
                await super().update_task(task_id, data)

        persistence = _Persistence()
        board = _Flaky()
        a = _task("upstream-a")
        b = _task("upstream-b")
        dependent = _task("dependent-1", deps=["upstream-a", "upstream-b"])
        state = SimpleNamespace(
            project_tasks=[a, b, dependent],
            kanban_client=board,
            assignment_persistence=persistence,
        )

        await _handoff_evidence_to_dependents(a, {"note": "from-a"}, state)
        assert dependent.inputs["note"] == "from-a"

        # A re-closes without ``note`` and the board write fails.
        board.fail_next = True
        await _handoff_evidence_to_dependents(a, {}, state)

        # B now contributes its own ``note``.
        await _handoff_evidence_to_dependents(b, {"note": "from-b"}, state)

        # The invariant: the key has ONE owner. A still holds it, because
        # its removal never reached the board, so B's value does not land
        # and B is not recorded as the author. Two records claiming one
        # key is the state that let A delete B's value.
        a_owns = "note" in await persistence.get_input_provenance(
            "dependent-1", "upstream-a"
        )
        b_owns = "note" in await persistence.get_input_provenance(
            "dependent-1", "upstream-b"
        )
        assert not (a_owns and b_owns)
        assert dependent.inputs["note"] == "from-a"

        # A closes again and clears the key it does own, which is correct:
        # there is no second contribution to destroy.
        await _handoff_evidence_to_dependents(a, {}, state)

        assert "note" not in dependent.inputs

    @pytest.mark.asyncio
    async def test_a_failed_write_leaves_memory_matching_the_board(self) -> None:
        """Memory and board must not diverge, which is what frees the key."""
        from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

        class _Flaky(_Kanban):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next = False

            async def update_task(self, task_id: str, data: Dict[str, Any]) -> None:
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("board unreachable")
                await super().update_task(task_id, data)

        board = _Flaky()
        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        state = SimpleNamespace(
            project_tasks=[find, verify],
            kanban_client=board,
            assignment_persistence=_Persistence(),
        )

        await _handoff_evidence_to_dependents(find, {"claim": "c1"}, state)
        on_board = dict(board.writes[-1][1]["inputs"])

        board.fail_next = True
        await _handoff_evidence_to_dependents(find, {"claim": "c2"}, state)

        assert verify.inputs == on_board


class TestItDegradesRatherThanFailing:
    """The handoff must never break a completion."""

    @pytest.mark.asyncio
    async def test_a_state_without_the_store_still_hands_off(self) -> None:
        """Older callers and tests pass a state with no persistence."""
        from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        state = SimpleNamespace(project_tasks=[find, verify], kanban_client=_Kanban())

        await _handoff_evidence_to_dependents(find, {"claim": "c1"}, state)

        assert verify.inputs["claim"] == "c1"

    @pytest.mark.asyncio
    async def test_a_failed_store_write_does_not_stop_the_handoff(self) -> None:
        """A bookkeeping failure must not cost the dependent its inputs."""
        from src.marcus_mcp.tools.task import _handoff_evidence_to_dependents

        class _Broken(_Persistence):
            async def record_input_provenance(
                self, dependent_id: str, upstream_task_id: str, keys: List[str]
            ) -> None:
                raise RuntimeError("disk full")

        find = _task("find-1")
        verify = _task("verify-1", deps=["find-1"])
        state = _state([find, verify], _Broken())

        await _handoff_evidence_to_dependents(find, {"claim": "c1"}, state)

        assert verify.inputs["claim"] == "c1"
