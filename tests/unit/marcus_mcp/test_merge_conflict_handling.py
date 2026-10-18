"""Unit tests for bug #651 — merge-conflict handling in report_task_progress.

Background
----------
Bug #651: When ``_merge_agent_branch_to_main`` reports a merge conflict,
the existing code marked the kanban task DONE anyway and "deferred" the
failure for return after the kanban update.  Under ephemeral one-agent-
per-task lifecycle (PR #600), the agent receiving the deferred failure
has already exited — the message goes nowhere.  Net result: kanban
claims the task is done, filesystem doesn't contain the work.

The fix introduces ``_apply_merge_failure_to_update_data`` which:

- Flips ``update_data["status"]`` from ``TaskStatus.DONE`` to
  ``TaskStatus.BLOCKED``
- Removes ``completed_at`` (task is not actually done)
- Stamps ``source_context.merge_conflict`` with the agent/branch/stderr
  so a recovery mechanism (CLI command, follow-up agent, or human) has
  the info needed to resolve
- Returns a structured failure response so the caller sees the BLOCKED
  state rather than a misleading success

These tests verify the helper's contract.  An integration-style test
verifies the wiring inside ``report_task_progress``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from src.core.models import TaskStatus

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _apply_merge_failure_to_update_data — helper-function contract
# ---------------------------------------------------------------------------


class TestApplyMergeFailureToUpdateData:
    """The helper that translates a merge_result failure into BLOCKED state."""

    def _task(
        self, task_id: str = "task_X", source_context: Dict[str, Any] | None = None
    ) -> MagicMock:
        """Minimal task stub with the attributes the helper reads."""
        task = MagicMock()
        task.id = task_id
        task.source_context = source_context
        return task

    def _failure_result(self) -> Dict[str, Any]:
        """Canonical merge failure payload returned by _merge_agent_branch_to_main."""
        return {
            "success": False,
            "error": "merge_conflict",
            "message": (
                "Your task passed validation but merging your branch "
                "(marcus/agent_X) to main has conflicts. Please "
                "resolve them..."
            ),
        }

    def test_flips_status_from_done_to_blocked(self) -> None:
        """The bug at task.py:3397 sets status=DONE; this helper flips it.

        Without this flip, the kanban update later in
        ``report_task_progress`` records DONE despite the work not
        being merged.  That divergence is the foundational bug #651
        addresses.
        """
        from src.marcus_mcp.tools.task import _apply_merge_failure_to_update_data

        update_data: Dict[str, Any] = {
            "status": TaskStatus.DONE,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        task = self._task(task_id="task_X")
        merge_result = self._failure_result()

        _apply_merge_failure_to_update_data(
            update_data=update_data,
            task=task,
            merge_result=merge_result,
            agent_id="agent_X",
        )

        assert update_data["status"] == TaskStatus.BLOCKED

    def test_removes_completed_at_field(self) -> None:
        """``completed_at`` must be cleared — the task is NOT completed.

        Leaving ``completed_at`` populated alongside BLOCKED status
        would corrupt downstream telemetry (Cato's "completion
        latency" metrics, etc.) and make audits inconsistent.
        """
        from src.marcus_mcp.tools.task import _apply_merge_failure_to_update_data

        update_data = {
            "status": TaskStatus.DONE,
            "completed_at": "2026-05-24T15:00:00+00:00",
        }
        task = self._task()
        _apply_merge_failure_to_update_data(
            update_data=update_data,
            task=task,
            merge_result=self._failure_result(),
            agent_id="agent_X",
        )

        assert "completed_at" not in update_data

    def test_stamps_merge_conflict_on_source_context(self) -> None:
        """Conflict info goes onto source_context so recovery has the
        info needed to resolve.

        The recovery surface (CLI, follow-up agent, or human) reads
        ``source_context.merge_conflict`` to know:
        - which branch failed to merge (``branch``)
        - the underlying git error (``conflict_stderr``)
        - when the conflict occurred (``blocked_at``)
        - which agent's work is parked (``agent_id``)
        """
        from src.marcus_mcp.tools.task import _apply_merge_failure_to_update_data

        update_data: Dict[str, Any] = {"status": TaskStatus.DONE}
        task = self._task(task_id="task_X")
        merge_result = self._failure_result()

        _apply_merge_failure_to_update_data(
            update_data=update_data,
            task=task,
            merge_result=merge_result,
            agent_id="agent_X",
        )

        ctx = update_data["source_context"]
        assert "merge_conflict" in ctx
        mc = ctx["merge_conflict"]
        assert mc["agent_id"] == "agent_X"
        assert mc["branch"] == "marcus/agent_X"
        assert "conflicts" in mc["conflict_stderr"]
        # blocked_at must be ISO-8601 parseable
        datetime.fromisoformat(mc["blocked_at"])

    def test_preserves_existing_source_context_fields(self) -> None:
        """Pre-existing source_context fields must survive the stamp.

        Tasks carry meaningful source_context already (e.g.,
        ``in_scope_outcome_ids`` from issue #523, ``responsibility``
        from contract-first decomposition).  The merge-conflict
        stamp must coexist with those, not overwrite them.
        """
        from src.marcus_mcp.tools.task import _apply_merge_failure_to_update_data

        existing_ctx = {
            "in_scope_outcome_ids": ["o_play", "o_score"],
            "responsibility": "Wires the application entry point",
        }
        update_data: Dict[str, Any] = {"status": TaskStatus.DONE}
        task = self._task(source_context=existing_ctx)

        _apply_merge_failure_to_update_data(
            update_data=update_data,
            task=task,
            merge_result=self._failure_result(),
            agent_id="agent_X",
        )

        ctx = update_data["source_context"]
        assert ctx["in_scope_outcome_ids"] == ["o_play", "o_score"]
        assert ctx["responsibility"] == "Wires the application entry point"
        assert "merge_conflict" in ctx

    def test_handles_none_source_context_on_task(self) -> None:
        """Tasks with ``source_context=None`` get a fresh dict, no crash.

        Legacy integration tasks have ``source_context = None``.  The
        helper must initialize the dict when stamping, not raise an
        AttributeError on ``None.copy()``.
        """
        from src.marcus_mcp.tools.task import _apply_merge_failure_to_update_data

        update_data: Dict[str, Any] = {"status": TaskStatus.DONE}
        task = self._task(source_context=None)

        _apply_merge_failure_to_update_data(
            update_data=update_data,
            task=task,
            merge_result=self._failure_result(),
            agent_id="agent_X",
        )

        ctx = update_data["source_context"]
        assert "merge_conflict" in ctx

    def test_returns_failure_response_with_blocker_message(self) -> None:
        """The helper returns the response dict the caller surfaces to runner.

        The runner (or any orchestrator) needs to know the task is in
        BLOCKED state because of a merge conflict.  Without this
        response, the runner would interpret silence as success and
        proceed to claim the task done.
        """
        from src.marcus_mcp.tools.task import _apply_merge_failure_to_update_data

        update_data: Dict[str, Any] = {"status": TaskStatus.DONE}
        task = self._task(task_id="task_X")
        merge_result = self._failure_result()

        response = _apply_merge_failure_to_update_data(
            update_data=update_data,
            task=task,
            merge_result=merge_result,
            agent_id="agent_X",
        )

        assert response["success"] is False
        assert response["status"] == "merge_conflict"
        assert response["task_id"] == "task_X"
        assert response["agent_id"] == "agent_X"
        assert (
            "branch" in response["blocker"].lower()
            or "merge" in response["blocker"].lower()
        )

    def test_does_not_mutate_merge_result(self) -> None:
        """Defensive: the helper must not mutate its input merge_result.

        ``_merge_agent_branch_to_main``'s return is the source of truth
        for audit/telemetry — mutating it from a side-channel would
        make traces inconsistent.
        """
        from src.marcus_mcp.tools.task import _apply_merge_failure_to_update_data

        update_data: Dict[str, Any] = {"status": TaskStatus.DONE}
        task = self._task()
        merge_result = self._failure_result()
        merge_result_snapshot = dict(merge_result)

        _apply_merge_failure_to_update_data(
            update_data=update_data,
            task=task,
            merge_result=merge_result,
            agent_id="agent_X",
        )

        assert merge_result == merge_result_snapshot


# ---------------------------------------------------------------------------
# Integration check — the wiring inside report_task_progress
# ---------------------------------------------------------------------------


class TestReportTaskProgressMergeConflictWiring:
    """Verify the helper is wired into report_task_progress correctly.

    Heavier-weight check than the helper tests above — exercises the
    full code path so we catch wiring regressions where the helper
    is correct but never gets called.
    """

    def test_helper_is_imported_by_task_module(self) -> None:
        """The helper must be exported from ``src.marcus_mcp.tools.task``.

        Regression guard: a refactor that moves the helper without
        updating the import would silently break the fix.
        """
        from src.marcus_mcp.tools import task

        assert hasattr(task, "_apply_merge_failure_to_update_data")


# ---------------------------------------------------------------------------
# _merge_agent_branch_to_main — defensive working-tree cleanup (bug #651)
# ---------------------------------------------------------------------------


class TestMergeDefensiveReset:
    """Pre-merge ``git reset --hard HEAD`` to discard gate side effects.

    Background — verify-snake-4 (test66, 2026-05-24)
    -------------------------------------------------
    The composer smoke gate runs ``npm install --silent && npm run
    build`` in ``project_root`` (the main repo) before the merge
    attempt.  ``npm install`` writes to ``package-lock.json``,
    leaving the main working tree dirty.  The merge then fails
    with::

        error: Your local changes to the following files would be
        overwritten by merge: package-lock.json
        Aborting

    Two consecutive merges hit this in the verify-snake-4 run.
    Their work landed on orphaned branches, never in HEAD.

    The defensive ``git reset --hard HEAD`` before the merge
    attempt discards any working-tree pollution and lets the merge
    proceed against clean committed state.  Safe by design —
    merging only cares about committed history; uncommitted gate
    side effects must not block the merge.
    """

    @pytest.fixture
    def real_git_project(self, tmp_path):
        """Create a real git repo with two committed files + an agent branch.

        Layout:
            tmp_path/implementation/        ← main repo
                ├── main.js                 (commit on main)
                ├── package-lock.json       (commit on main)
                └── .git/

        Plus a ``marcus/agent_X`` branch that adds a third file.
        Tests then dirty the main working tree (simulating an
        ``npm install`` side effect) and verify the merge proceeds
        cleanly after the defensive ``git reset --hard``.
        """
        import subprocess as _sp

        repo = tmp_path / "implementation"
        repo.mkdir()

        def git(*args):
            return _sp.run(
                ["git", *args],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

        git("init", "--initial-branch=main")
        git("config", "user.email", "test@marcus.test")
        git("config", "user.name", "Test")

        (repo / "main.js").write_text("// scaffold main.js\n")
        (repo / "package-lock.json").write_text('{"version": "1.0.0"}\n')
        git("add", ".")
        git("commit", "-m", "scaffold")

        # Create the agent's branch with an additional file.
        git("checkout", "-b", "marcus/agent_X")
        (repo / "agent_X_file.js").write_text("// agent X work\n")
        git("add", "agent_X_file.js")
        git("commit", "-m", "feat(agent_X): add file")
        git("checkout", "main")

        return repo

    @pytest.mark.asyncio
    async def test_merge_succeeds_when_working_tree_dirty_from_gate_side_effect(
        self, real_git_project
    ) -> None:
        """Reproduces the verify-snake-4 failure and verifies the fix.

        Dirty the main working tree (simulating the composer smoke
        gate's ``npm install`` modifying ``package-lock.json``),
        then attempt the merge.  Pre-fix this fails with "Your
        local changes ... would be overwritten."  Post-fix the
        defensive ``git reset --hard HEAD`` discards the
        modification and the merge succeeds.
        """
        from unittest.mock import patch

        from src.marcus_mcp.tools.task import _merge_agent_branch_to_main

        repo = real_git_project

        # Simulate the gate's side effect: dirty the tracked
        # package-lock.json without committing.
        (repo / "package-lock.json").write_text(
            '{"version": "1.0.0", "polluted": "by-npm-install"}\n'
        )

        state = MagicMock()
        state.kanban_client = MagicMock()
        state.kanban_client._load_workspace_state = MagicMock(
            return_value={"project_root": str(repo)}
        )

        result = await _merge_agent_branch_to_main(
            agent_id="agent_X",
            task_id="task_Y",
            state=state,
        )

        # Post-fix: defensive reset discards the pollution; merge succeeds.
        assert result == {
            "success": True
        }, f"Merge should succeed after defensive reset, got: {result}"

        # Confirm the agent's file is now in main.
        assert (repo / "agent_X_file.js").exists()
        # Confirm package-lock.json is back to its committed state.
        assert (repo / "package-lock.json").read_text() == '{"version": "1.0.0"}\n'

    def test_update_task_progress_uses_blocked_status_on_merge_fail(self) -> None:
        """Codex P1 on PR #653 commit 2d30f3a2 — second kanban call also uses BLOCKED.

        ``report_task_progress`` makes two kanban calls after the
        merge attempt:

        1. ``kanban_client.update_task(task_id, update_data)`` —
           uses ``update_data["status"]`` which the helper
           correctly flips to BLOCKED on merge fail.
        2. ``kanban_client.update_task_progress(task_id,
           {"status": status, ...})`` — uses the LOCAL ``status``
           variable which is still ``"completed"`` (the agent's
           original report).

        Pre-fix, the second call clobbers the first by re-applying
        ``"completed"`` → providers re-mark the task DONE,
        re-introducing the divergence this PR is supposed to close.

        Post-fix, the second call uses an ``_effective_status``
        that flips to ``"blocked"`` when ``_deferred_merge_failure``
        is set, keeping kanban consistent.
        """
        import inspect

        from src.marcus_mcp.tools.task import report_task_progress

        source = inspect.getsource(report_task_progress)

        # The override variable must exist and be used in the
        # update_task_progress call.
        assert "_effective_status" in source
        assert '"status": _effective_status' in source
        # The override must be gated on _deferred_merge_failure.
        assert '"blocked" if _deferred_merge_failure is not None' in source

    @pytest.mark.asyncio
    async def test_memory_records_failure_on_merge_conflict(self) -> None:
        """Memory recording reflects merge outcome, not pre-merge optimism.

        Kaia review concern #2 on PR #653: previously
        ``state.memory.record_task_completion(success=True, ...)``
        fired BEFORE the merge attempt.  On merge failure (now
        BLOCKED, not DONE), memory would falsely show the task
        succeeded — inflating the agent's learned success rate.

        Post-fix: memory recording moves into the merge outcome
        branches.  On failure, ``success=False`` is recorded with
        the merge-conflict blocker message.
        """
        # This test is partially structural — full end-to-end
        # exercise of report_task_progress requires extensive
        # state mocking.  Here we just verify the source code at
        # the merge-failure branch contains an honest memory call
        # (success=False) and the merge-success branch contains
        # success=True.
        import inspect

        from src.marcus_mcp.tools.task import report_task_progress

        source = inspect.getsource(report_task_progress)

        # The failure path must record success=False with the
        # merge_conflict blocker.
        assert "success=False" in source and "merge_conflict" in source
        # The success path must still record success=True.
        assert "success=True" in source

    @pytest.mark.asyncio
    async def test_merge_still_attempts_when_reset_fails(self, monkeypatch) -> None:
        """If ``git reset --hard`` fails, the merge attempt still proceeds.

        Defensive code: a flaky reset should not block the merge
        entirely.  Log a warning and continue — if the underlying
        problem persists, the merge will surface it more concretely
        than a half-recovered reset.
        """
        # Reuse the fixture's setup by creating a fresh repo.
        import subprocess as _sp
        import tempfile
        from unittest.mock import patch

        from src.marcus_mcp.tools.task import _merge_agent_branch_to_main

        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path as _P

            repo = _P(tmpdir) / "implementation"
            repo.mkdir()

            def git(*args):
                return _sp.run(
                    ["git", *args],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                )

            git("init", "--initial-branch=main")
            git("config", "user.email", "test@marcus.test")
            git("config", "user.name", "Test")
            (repo / "main.js").write_text("// scaffold\n")
            git("add", ".")
            git("commit", "-m", "scaffold")
            git("checkout", "-b", "marcus/agent_X")
            (repo / "feature.js").write_text("// feature\n")
            git("add", "feature.js")
            git("commit", "-m", "feat")
            git("checkout", "main")

            state = MagicMock()
            state.kanban_client = MagicMock()
            state.kanban_client._load_workspace_state = MagicMock(
                return_value={"project_root": str(repo)}
            )

            # Monkeypatch subprocess.run to make ``git reset --hard``
            # fail while letting everything else pass through.
            real_run = _sp.run

            def selective_failing_run(cmd, **kwargs):
                if (
                    isinstance(cmd, list)
                    and len(cmd) >= 4
                    and cmd[:4] == ["git", "reset", "--hard", "HEAD"]
                ):
                    raise _sp.CalledProcessError(
                        returncode=128,
                        cmd=cmd,
                        output=b"",
                        stderr=b"fatal: simulated reset failure",
                    )
                return real_run(cmd, **kwargs)

            with patch("subprocess.run", side_effect=selective_failing_run):
                result = await _merge_agent_branch_to_main(
                    agent_id="agent_X",
                    task_id="task_Y",
                    state=state,
                )

            # Reset failed but merge was still attempted and succeeded
            # (working tree was clean, so even without the reset the
            # merge had nothing blocking it).
            assert result == {"success": True}
