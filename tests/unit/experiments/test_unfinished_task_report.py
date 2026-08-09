"""
Unit tests for the end-of-run unfinished-work report.

A run could finish with work silently missing: the last line printed was
``run finished — spawned N ephemeral agents``, and nothing ever said
"task X never got done, so its deliverable is absent". A user had to
watch the live progress line or go inspect the board to discover it, so
a partially-failed run looked exactly like a successful one.

``get_experiment_status`` now reports the unfinished tasks themselves —
id, name, status and the blocker text — so the runner (and any other
consumer) can state plainly what did not get built and why.
"""

from datetime import datetime, timezone
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import Priority, Task, TaskStatus

pytestmark = pytest.mark.unit


def _task(
    task_id: str,
    name: str,
    status: TaskStatus,
    source_context: dict | None = None,
) -> Task:
    now = datetime.now(timezone.utc)
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
        estimated_hours=1.0,
        labels=[],
        dependencies=[],
        source_context=source_context,
    )


def _monitor(tasks: List[Task]) -> Any:
    """Build a LiveExperimentMonitor with a stubbed kanban client."""
    from src.experiments.live_experiment_monitor import LiveExperimentMonitor

    monitor = LiveExperimentMonitor.__new__(LiveExperimentMonitor)
    monitor.is_running = False
    monitor.run_name = "run"
    monitor.experiment_name = "exp"
    monitor.board_id = "board"
    monitor.registered_agents = {}
    monitor.task_assignments = {}
    monitor.task_completions = {}
    monitor.blockers_reported = 0
    monitor.artifacts_created = 0
    monitor.decisions_logged = 0
    monitor.context_requests = 0
    monitor.progress_updates = 0

    client = MagicMock()
    client.get_project_metrics = AsyncMock(
        return_value={
            "total_tasks": len(tasks),
            "completed_tasks": sum(1 for t in tasks if t.status == TaskStatus.DONE),
            "in_progress_tasks": 0,
            "backlog_tasks": 0,
            "blocked_tasks": sum(1 for t in tasks if t.status == TaskStatus.BLOCKED),
        }
    )
    client.get_all_tasks = AsyncMock(return_value=tasks)
    monitor.kanban_client = client
    return monitor


class TestUnfinishedTasksReported:
    """The status payload must name what did not get built."""

    @pytest.mark.asyncio
    async def test_blocked_task_is_reported_with_reason(self) -> None:
        """A dead lane must be named, with the blocker text that killed it."""
        tasks = [
            _task("t1", "Implement Quote Library", TaskStatus.DONE),
            _task(
                "t2",
                "Implement CLI Entry Point",
                TaskStatus.BLOCKED,
                source_context={"blocker": "needs a paid API key"},
            ),
        ]

        status = await _monitor(tasks).get_status()

        unfinished = status["unfinished_tasks"]
        assert len(unfinished) == 1
        assert unfinished[0]["id"] == "t2"
        assert unfinished[0]["name"] == "Implement CLI Entry Point"
        assert unfinished[0]["status"] == "blocked"
        assert unfinished[0]["reason"] == "needs a paid API key"

    @pytest.mark.asyncio
    async def test_never_started_task_is_reported(self) -> None:
        """A task still sitting in TODO at teardown never got built either."""
        tasks = [
            _task("t1", "Done thing", TaskStatus.DONE),
            _task("t2", "Never claimed", TaskStatus.TODO),
        ]

        status = await _monitor(tasks).get_status()

        assert [t["id"] for t in status["unfinished_tasks"]] == ["t2"]
        assert status["unfinished_tasks"][0]["status"] == "todo"

    @pytest.mark.asyncio
    async def test_fully_complete_run_reports_nothing_unfinished(self) -> None:
        """A clean run must not invent warnings."""
        tasks = [
            _task("t1", "A", TaskStatus.DONE),
            _task("t2", "B", TaskStatus.DONE),
        ]

        status = await _monitor(tasks).get_status()

        assert status["unfinished_tasks"] == []

    @pytest.mark.asyncio
    async def test_blocker_reason_falls_back_when_absent(self) -> None:
        """A blocked task with no recorded text still reports honestly."""
        tasks = [_task("t1", "Mystery", TaskStatus.BLOCKED)]

        status = await _monitor(tasks).get_status()

        assert status["unfinished_tasks"][0]["reason"]

    @pytest.mark.asyncio
    async def test_task_fetch_failure_does_not_break_status(self) -> None:
        """Status is load-bearing for the control loop — never let it throw."""
        tasks = [_task("t1", "A", TaskStatus.DONE)]
        monitor = _monitor(tasks)
        monitor.kanban_client.get_all_tasks = AsyncMock(
            side_effect=RuntimeError("board unreachable")
        )

        status = await monitor.get_status()

        assert status["total_tasks"] == 1
        assert "unfinished_tasks" not in status


class TestRunnerReportRendering:
    """The runner must state plainly what did not get built.

    Counts in a scrolling progress line are not a report: a run could end
    with a deliverable missing and the final output still read like
    success. These pin the human-facing text.
    """

    def test_clean_run_says_all_complete(self) -> None:
        from runners.spawn_agents import format_unfinished_report

        lines = format_unfinished_report([], total=9)

        assert len(lines) == 1
        assert "9/9" in lines[0]
        assert "complete" in lines[0].lower()

    def test_unfinished_work_is_named_with_reasons(self) -> None:
        from runners.spawn_agents import format_unfinished_report

        lines = format_unfinished_report(
            [
                {
                    "id": "abc123",
                    "name": "Implement CLI Entry Point",
                    "status": "blocked",
                    "reason": "needs a paid API key",
                },
                {
                    "id": "def456",
                    "name": "Write README",
                    "status": "todo",
                    "reason": "never claimed by an agent",
                },
            ],
            total=10,
        )

        text = "\n".join(lines)
        assert "8/10" in text
        assert "2 task(s) did NOT complete" in text
        assert "Implement CLI Entry Point" in text
        assert "needs a paid API key" in text
        assert "Write README" in text
        assert "never claimed by an agent" in text

    def test_report_warns_deliverables_may_be_missing(self) -> None:
        """The consequence must be stated, not left for the reader to infer."""
        from runners.spawn_agents import format_unfinished_report

        text = "\n".join(
            format_unfinished_report(
                [{"id": "a", "name": "X", "status": "blocked", "reason": "r"}],
                total=2,
            )
        )

        assert "missing" in text.lower()


class TestBlockerTextComesFromTheStore:
    """Codex P1: the blocker text lives in the provider's blocker store.

    ``report_blocker`` sends ``update_task(task_id, {"blocker": text})``
    and the SQLite provider INSERTs that into a separate ``blockers``
    table. Task hydration only reads the tasks row, so
    ``source_context["blocker"]`` is always absent — meaning every
    genuinely blocked task rendered as "blocked (no blocker text
    recorded)", defeating the entire point of the report.
    """

    @pytest.mark.asyncio
    async def test_reason_read_from_provider_blocker_store(self) -> None:
        """The recorded description must reach the report."""
        tasks = [_task("t1", "Implement CLI", TaskStatus.BLOCKED)]
        monitor = _monitor(tasks)
        monitor.kanban_client.get_task_blockers = AsyncMock(
            return_value=[{"description": "needs a paid API key", "severity": "high"}]
        )

        status = await monitor.get_status()

        assert status["unfinished_tasks"][0]["reason"] == "needs a paid API key"

    @pytest.mark.asyncio
    async def test_most_recent_blocker_wins(self) -> None:
        """A task blocked more than once reports the latest reason."""
        tasks = [_task("t1", "Implement CLI", TaskStatus.BLOCKED)]
        monitor = _monitor(tasks)
        monitor.kanban_client.get_task_blockers = AsyncMock(
            return_value=[
                {"description": "first attempt failed"},
                {"description": "second attempt: API key missing"},
            ]
        )

        status = await monitor.get_status()

        assert status["unfinished_tasks"][0]["reason"] == (
            "second attempt: API key missing"
        )

    @pytest.mark.asyncio
    async def test_source_context_still_wins_when_present(self) -> None:
        """Providers that do hydrate the blocker keep working."""
        tasks = [
            _task(
                "t1",
                "Implement CLI",
                TaskStatus.BLOCKED,
                source_context={"blocker": "from source_context"},
            )
        ]
        monitor = _monitor(tasks)
        monitor.kanban_client.get_task_blockers = AsyncMock(return_value=[])

        status = await monitor.get_status()

        assert status["unfinished_tasks"][0]["reason"] == "from source_context"

    @pytest.mark.asyncio
    async def test_blocker_lookup_failure_degrades_to_fallback(self) -> None:
        """A store error must not lose the task from the report."""
        tasks = [_task("t1", "Implement CLI", TaskStatus.BLOCKED)]
        monitor = _monitor(tasks)
        monitor.kanban_client.get_task_blockers = AsyncMock(
            side_effect=RuntimeError("store unreachable")
        )

        status = await monitor.get_status()

        assert len(status["unfinished_tasks"]) == 1
        assert status["unfinished_tasks"][0]["reason"]


class TestUntrustworthyFetchIsNotSuccess:
    """Codex P1: an empty task list can mean "board unreachable".

    The Planka provider catches fetch errors and returns ``[]`` rather
    than raising, so the exception path never fires. Publishing
    ``unfinished_tasks=[]`` from that makes the runner print "all
    complete" for a board it could not read — the precise silent-success
    failure this report exists to prevent.
    """

    @pytest.mark.asyncio
    async def test_empty_list_contradicting_metrics_is_not_published(self) -> None:
        """metrics say 9 tasks, fetch returns 0 -> untrustworthy."""
        tasks = [_task(f"t{i}", f"T{i}", TaskStatus.DONE) for i in range(9)]
        monitor = _monitor(tasks)
        monitor.kanban_client.get_all_tasks = AsyncMock(return_value=[])

        status = await monitor.get_status()

        assert "unfinished_tasks" not in status

    @pytest.mark.asyncio
    async def test_genuinely_empty_board_is_not_published_either(self) -> None:
        """0 tasks total is indistinguishable from a failed fetch."""
        monitor = _monitor([])

        status = await monitor.get_status()

        assert "unfinished_tasks" not in status

    @pytest.mark.asyncio
    async def test_consistent_fetch_is_published(self) -> None:
        """A fetch that matches the metrics total is trustworthy."""
        tasks = [
            _task("t1", "A", TaskStatus.DONE),
            _task("t2", "B", TaskStatus.BLOCKED),
        ]

        status = await _monitor(tasks).get_status()

        assert len(status["unfinished_tasks"]) == 1


class TestRunnerRefusesToClaimSuccessBlind:
    """The runner must never imply success from missing data."""

    def test_absent_report_says_it_could_not_verify(self) -> None:
        from runners.spawn_agents import format_unfinished_report

        lines = format_unfinished_report(None, total=9)

        text = "\n".join(lines).lower()
        assert "could not" in text or "unable" in text
        assert "complete" not in text.split("could not")[0]

    def test_zero_total_does_not_claim_all_complete(self) -> None:
        from runners.spawn_agents import format_unfinished_report

        text = "\n".join(format_unfinished_report([], total=0)).lower()

        assert "all 0/0" not in text


class TestSqliteProviderExposesBlockers:
    """The SQLite provider must offer the read path the report needs.

    It already WRITES blocker descriptions (``update_task`` with a
    ``blocker`` key INSERTs into the ``blockers`` table) but offered no
    way to read them back, so the recorded explanation was unreachable.
    """

    @pytest.mark.asyncio
    async def test_get_task_blockers_returns_written_description(
        self, tmp_path: Any
    ) -> None:
        from src.integrations.providers.sqlite_kanban import SQLiteKanban

        provider = SQLiteKanban({"db_path": str(tmp_path / "k.db")})
        await provider.connect()
        task = await provider.create_task({"name": "Implement CLI", "description": "d"})

        await provider.update_task(task.id, {"blocker": "needs a paid API key"})
        blockers = await provider.get_task_blockers(task.id)

        assert [b["description"] for b in blockers] == ["needs a paid API key"]

    @pytest.mark.asyncio
    async def test_blockers_returned_oldest_first(self, tmp_path: Any) -> None:
        """Order matters: the report shows the most recent (last) one."""
        from src.integrations.providers.sqlite_kanban import SQLiteKanban

        provider = SQLiteKanban({"db_path": str(tmp_path / "k2.db")})
        await provider.connect()
        task = await provider.create_task({"name": "T", "description": "d"})

        await provider.update_task(task.id, {"blocker": "first"})
        await provider.update_task(task.id, {"blocker": "second"})
        blockers = await provider.get_task_blockers(task.id)

        assert [b["description"] for b in blockers] == ["first", "second"]

    @pytest.mark.asyncio
    async def test_no_blockers_returns_empty(self, tmp_path: Any) -> None:
        from src.integrations.providers.sqlite_kanban import SQLiteKanban

        provider = SQLiteKanban({"db_path": str(tmp_path / "k3.db")})
        await provider.connect()
        task = await provider.create_task({"name": "T", "description": "d"})

        assert await provider.get_task_blockers(task.id) == []
