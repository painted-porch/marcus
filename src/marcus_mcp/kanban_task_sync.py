"""ADR-0013 K13: update_task leaves the caller's in-memory view consistent.

Three unrelated bugs, found independently in one day (#807, #816,
#814), had the identical shape: something wrote a task's new status or
assignee to the kanban board, and something else in the *same request*
read ``state.project_tasks`` expecting that write to already be
visible there. It was not — nothing besides the next scheduled
``refresh_project_state`` call ever synced it, and that call runs on
its own schedule, not this one's.

This module is the fix, applied once rather than swept per call site.
``TaskSyncingKanbanClient`` wraps a real kanban client so that every
``update_task`` call also updates the matching ``Task`` object in
``state.project_tasks``, immediately, as part of the same call. Every
current and future caller of ``state.kanban_client.update_task(...)``
gets this for free.

The wrapper lives here, outside ``src/integrations/``, on purpose.
``KanbanInterface`` and its four providers have no reference to a
``MarcusServer`` or to ``state.project_tasks`` today, and several are
constructed directly by standalone tooling (seed scripts, live
checks) with no ``MarcusServer`` in the process at all. Teaching the
provider classes about ``state.project_tasks`` would couple a board
adapter to one server's in-memory cache and break that reuse. Wrapping
at the point ``state.kanban_client`` is assigned keeps every provider
exactly as decoupled as it is today.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, overload

from src.core.models import Task, TaskStatus

logger = logging.getLogger(__name__)


def _coerce(key: str, value: Any) -> Any:
    """Normalize a value before it lands on the in-memory ``Task``.

    Every current ``update_task`` caller passes ``status`` as a
    ``TaskStatus`` enum, except one that passed the plain string
    ``"done"`` (found by this module's own sweep and fixed at the
    source). This is the belt to that fix's suspenders: a raw string
    status is coerced to the matching enum member rather than landing
    as a plain string the rest of the codebase never compares against
    (``task.status == TaskStatus.DONE`` would silently read False
    forever for a task whose in-memory ``status`` is the string
    ``"done"``). An unrecognized string is left as-is and logged
    elsewhere by nothing in particular — it will simply fail the same
    way it already would have without this wrapper.
    """
    if key == "status" and isinstance(value, str):
        try:
            return TaskStatus(value)
        except ValueError:
            return value
    return value


def _apply(task: Task, key: str, value: Any) -> None:
    """Land one updated field onto the in-memory ``Task``.

    ``source_context`` is special: ``sqlite_kanban.update_task`` merges
    a dict value into the existing column rather than replacing it
    (Codex P1 on PR #660) — a caller passing a partial update like
    ``{"scaffold_path": ...}`` expects the board's other keys
    (``contract_file``, ``product_intent``, ...) to survive. A bare
    ``setattr`` here would replace the whole dict and make
    ``state.project_tasks`` disagree with the board it was just
    supposed to agree with (Codex P2 on PR #821). Every other field
    sqlite treats as a plain replace, which plain ``setattr`` already
    is; ``inputs`` is also a replace by the same source's own comment
    (issue #737) and needs no special case here.
    """
    if key == "source_context":
        if value is None:
            task.source_context = None
            return
        if isinstance(value, dict):
            if not value:
                return  # empty dict is a no-op, matching sqlite's own rule
            existing = getattr(task, "source_context", None) or {}
            task.source_context = {**existing, **value}
            return
    setattr(task, key, _coerce(key, value))


class TaskSyncingKanbanClient:
    """Wraps a kanban client so ``update_task`` also syncs in-memory tasks.

    Every other attribute and method delegates straight to the wrapped
    client via ``__getattr__``, so this is transparent to any caller
    that does not specifically care about the sync.
    """

    def __init__(
        self,
        inner: Any,
        tasks_source: Callable[[], Optional[List[Task]]],
    ) -> None:
        """
        Wrap a kanban client with the K13 sync guarantee.

        Parameters
        ----------
        inner : Any
            The real kanban client (any ``KanbanInterface`` implementation).
        tasks_source : Callable[[], Optional[List[Task]]]
            Returns the CURRENT ``state.project_tasks`` list. A callable,
            not a captured list, because ``refresh_project_state``
            reassigns that list wholesale; capturing it once would sync
            against a list nothing reads any more.
        """
        # Routed through object.__setattr__ directly: __setattr__ below
        # delegates everything else to self._inner, which does not exist
        # on the first of these two lines yet.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_tasks_source", tasks_source)

    def __getattr__(self, name: str) -> Any:
        """Delegate anything not defined here to the wrapped client."""
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        """Delegate attribute writes to the wrapped client too.

        Codex P1 on PR #821: callers that read-then-write a provider
        attribute directly (``create_project``'s SQLite reuse path
        clears and later re-sets ``kanban_client.project_id`` and
        ``board_id``) need that write to land on the real client. Only
        delegating reads (via ``__getattr__``) let a bare ``setattr``
        create a same-named attribute on this wrapper instead, which
        then shadowed the real client's value forever — the wrapper
        kept returning its own stale ``None`` no matter what
        ``auto_setup_project`` set on the inner provider afterward, and
        a project registered with that null id served every project's
        tasks on the next switch.
        """
        if name in ("_inner", "_tasks_source"):
            object.__setattr__(self, name, value)
            return
        setattr(self._inner, name, value)

    def __eq__(self, other: object) -> bool:
        """Compare by the wrapped client's identity, not the wrapper's.

        A caller that stashed the raw client and later compares it
        against ``state.kanban_client`` (common in tests) must still
        find them equal.
        """
        if isinstance(other, TaskSyncingKanbanClient):
            return bool(self._inner == other._inner)
        return bool(self._inner == other)

    def __repr__(self) -> str:
        """Report the wrapped client, not this adapter."""
        return repr(self._inner)

    def __bool__(self) -> bool:
        """Truthy exactly when the wrapped client is."""
        return bool(self._inner)

    async def update_task(self, task_id: str, updates: Dict[str, Any]) -> Any:
        """Write the board, then sync the matching in-memory ``Task``.

        If the board write raises, nothing is synced and the exception
        propagates unchanged — the invariant is "if the write landed,
        memory agrees with it," not "memory agrees regardless."

        Only the fields the caller actually passed are copied onto the
        in-memory object, and only onto attributes the ``Task`` model
        actually has (``hasattr``-guarded): several call sites pass
        board-only keys (``blocker``, ``progress``) that are not ``Task``
        attributes at all, and copying those via a bare ``setattr``
        would create incorrect attributes rather than updating real ones.

        A sync failure (unexpected list shape, attribute error) is
        logged and swallowed — matching every other handoff-adjacent
        failure path in this codebase (#737, #667 Fix 2, #807): a
        bookkeeping miss must never fail the write that already landed
        on the board.
        """
        result = await self._inner.update_task(task_id, updates)
        try:
            tasks = self._tasks_source() or []
            for task in tasks:
                if task.id == task_id:
                    for key, value in updates.items():
                        if hasattr(task, key):
                            _apply(task, key, value)
                    break
        except Exception as sync_err:  # noqa: BLE001 - the write already landed
            logger.warning(
                "[K13] Could not sync task %s's in-memory object after a "
                "successful board write (%s); state.project_tasks may read "
                "stale until the next refresh_project_state.",
                task_id,
                sync_err,
            )
        return result


@overload
def sync_on_write(
    client: None, tasks_source: Callable[[], Optional[List[Task]]]
) -> None: ...


@overload
def sync_on_write(
    client: Any, tasks_source: Callable[[], Optional[List[Task]]]
) -> "TaskSyncingKanbanClient": ...


def sync_on_write(
    client: Optional[Any],
    tasks_source: Callable[[], Optional[List[Task]]],
) -> Optional["TaskSyncingKanbanClient"]:
    """Wrap ``client`` for the K13 guarantee, unless there is nothing to wrap.

    ``None`` passes through unchanged (no client yet), and an already-
    wrapped client is returned as-is rather than double-wrapped.

    Parameters
    ----------
    client : Optional[Any]
        The kanban client to wrap, or ``None``.
    tasks_source : Callable[[], Optional[List[Task]]]
        See :class:`TaskSyncingKanbanClient`.

    Returns
    -------
    Optional[Any]
        The wrapped client, ``None``, or the already-wrapped input.
    """
    if client is None or isinstance(client, TaskSyncingKanbanClient):
        return client
    return TaskSyncingKanbanClient(client, tasks_source)
