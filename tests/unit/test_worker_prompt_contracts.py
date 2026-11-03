"""
Contract tests for the worker prompt (Codex P1 on PR #720).

``prompts/Agent_prompt.md`` is the canonical, user-facing worker spec: the
README tells operators to copy it into every project directory, so it is
the prompt real users' agents run with. The experiment runner's one-task
copy (``dev-tools/experiments/templates/agent_prompt.md``) drifted from it
silently (#719's severity contract was added to one file only) and was
deleted with the spawn-per-task runner (epic #706); the session launcher's
sheet is ``dev-tools/experiments/templates/session_prompt.md``, pinned by
``tests/unit/experiments/test_session_launcher.py``. These tests pin the
behaviour-defining contracts of the canonical prompt, and the list below
is where a second sheet goes if one is ever held to the same contract.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_PROMPT = REPO_ROOT / "prompts" / "Agent_prompt.md"
DELETED_ONE_TASK_PROMPT = (
    REPO_ROOT / "dev-tools" / "experiments" / "templates" / "agent_prompt.md"
)

ALL_PROMPTS = [CANONICAL_PROMPT]


class TestOneTaskCopyStaysDeleted:
    """The "EXACTLY ONE task then exit" sheet must not come back."""

    def test_experiment_one_task_prompt_is_gone(self) -> None:
        """Nothing reads it, and a second sheet is how contracts drift."""
        assert not DELETED_ONE_TASK_PROMPT.exists(), (
            "the one-task worker prompt was deleted with the spawn loop; "
            "extend ALL_PROMPTS instead of reviving it"
        )


@pytest.mark.parametrize("prompt_path", ALL_PROMPTS, ids=lambda p: p.name)
class TestBlockerSeverityContract:
    """Issue #719: severity decides whether the agent keeps the task.

    An agent that does not know this will send ``high`` for an ordinary
    snag and kill its own lane, or send ``medium`` for impossible work
    and hold the lane until the ceiling fires. Both prompts must teach it.
    """

    def test_prompt_exists(self, prompt_path: Path) -> None:
        """The prompt file must exist where the docs/runner expect it."""
        assert prompt_path.is_file(), f"missing worker prompt: {prompt_path}"

    def test_names_advisory_and_terminal(self, prompt_path: Path) -> None:
        """Both severity behaviours must be named explicitly."""
        text = prompt_path.read_text()
        assert "ADVISORY" in text, f"{prompt_path.name} lacks the advisory contract"
        assert "TERMINAL" in text, f"{prompt_path.name} lacks the terminal contract"

    def test_states_advisory_keeps_the_task(self, prompt_path: Path) -> None:
        """The load-bearing fact: low/medium does NOT cost you the task."""
        text = prompt_path.read_text().lower()
        assert "keep" in text and "task" in text
        assert "medium" in text

    def test_scopes_high_to_genuinely_impossible(self, prompt_path: Path) -> None:
        """``high`` must be framed as last resort, or agents overuse it."""
        text = prompt_path.read_text().lower()
        assert "only" in text
        assert "high" in text


@pytest.mark.parametrize("prompt_path", ALL_PROMPTS, ids=lambda p: p.name)
class TestRepairRequeueContract:
    """A terminal blocker is retried by a fresh agent, not instantly fatal.

    Agents must know their diagnostic is read by the next agent, or they
    will write throwaway blocker text ("didn't work") and the repair
    attempt starts blind.
    """

    def test_says_a_fresh_agent_retries(self, prompt_path: Path) -> None:
        """The prompt must say the task goes back for another attempt."""
        text = prompt_path.read_text().lower()
        assert "fresh agent" in text

    def test_asks_for_a_useful_diagnostic(self, prompt_path: Path) -> None:
        """The next agent reads what you wrote — say so."""
        text = prompt_path.read_text().lower()
        assert "next agent reads it" in text


@pytest.mark.parametrize("prompt_path", ALL_PROMPTS, ids=lambda p: p.name)
class TestNoStaleTerminalWarning:
    """Codex P2: no leftover text claiming a terminal blocker is final.

    The pre-repair prompts warned that reporting "high" ends work on the
    task "for the whole project" and leaves the deliverable "missing".
    That is now false — a fresh agent retries — and an agent reading both
    statements may avoid legitimate terminal handoffs.
    """

    def test_no_whole_project_finality_claim(self, prompt_path: Path) -> None:
        """The contradictory warning must not survive anywhere."""
        text = prompt_path.read_text().lower()
        assert "whole project" not in text
        assert "will be missing" not in text


@pytest.mark.parametrize("prompt_path", ALL_PROMPTS, ids=lambda p: p.name)
class TestLeaseEpochContract:
    """ADR-0012 D11: every prompt must teach the token, honestly.

    Marcus records the epoch and does not act on it. The prompt must say
    so. An earlier revision told agents that a stale epoch would come
    back as ``stale_epoch`` with their work preserved on a reconciliation
    card — a response Marcus no longer produces. A prompt that promises
    behaviour the server does not implement is worse than one that says
    nothing, because an agent may branch on it.
    """

    def test_names_the_lease_epoch(self, prompt_path: Path) -> None:
        """The token is named, not merely implied."""
        assert "lease_epoch" in prompt_path.read_text()

    def test_instructs_passing_it_on_every_report(self, prompt_path: Path) -> None:
        """The agent is told to carry it back on reports, not just save it."""
        text = " ".join(prompt_path.read_text().lower().split())
        assert "on every" in text and "report_task_progress call" in text

    def test_is_honest_that_it_is_not_enforced(self, prompt_path: Path) -> None:
        """The prompt must not promise fencing Marcus does not do."""
        text = " ".join(prompt_path.read_text().lower().split())
        assert "records this and does not act on it" in text

    def test_does_not_promise_a_stale_epoch_response(self, prompt_path: Path) -> None:
        """No agent should branch on a response that cannot arrive."""
        text = prompt_path.read_text().lower()
        assert "stale_epoch" not in text
        assert "reconciliation card" not in text


class TestDrainSignalContract:
    """Epic #706: the server's BOARD_DRAINED verdict is the only exit rule.

    The canonical loop prompt must teach the worker to stop on the drain
    signal and never on its own reading of the board.
    """

    def test_names_the_drain_signal(self) -> None:
        """The prompt names BOARD_DRAINED and should_exit."""
        text = CANONICAL_PROMPT.read_text()
        assert "BOARD_DRAINED" in text
        assert "should_exit" in text

    def test_drain_means_exit_without_retry(self) -> None:
        """On the signal the worker exits and does not request again."""
        text = " ".join(CANONICAL_PROMPT.read_text().lower().split())
        assert "do not retry" in text
        assert "exit your work loop" in text

    def test_worker_does_not_self_declare_completion(self) -> None:
        """The old 'total == completed means done' rule is gone."""
        text = " ".join(CANONICAL_PROMPT.read_text().lower().split())
        assert "total_tasks == completed" not in text
        assert "only the board_drained signal ends your loop" in text


class TestGateUnavailableContract:
    """A gate Marcus cannot run is Marcus's fault; the worker must not file a blocker."""

    def test_names_the_parked_status(self) -> None:
        """The canonical prompt names smoke_gate_unavailable."""
        assert "smoke_gate_unavailable" in CANONICAL_PROMPT.read_text()

    def test_says_wait_and_re_report_not_blocker(self) -> None:
        """The instruction is to keep the task, wait, and re-report."""
        text = " ".join(CANONICAL_PROMPT.read_text().lower().split())
        assert "never file a blocker" in text
        assert "re-report" in text


SESSION_HEADER = (
    REPO_ROOT / "dev-tools" / "experiments" / "templates" / "session_prompt.md"
)


class TestLoopContract:
    """Epic #706: the canonical sheet is the one worker contract.

    The deleted runner copy carried clauses the canonical sheet lacked; a
    worker started by the documented setup never saw them. They live here
    now, and the launcher's header only points at this file.
    """

    def test_registration_is_idempotent_and_repeated(self) -> None:
        """Re-registering with the same agent_id is expected on every fresh start."""
        text = " ".join(CANONICAL_PROMPT.read_text().lower().split())
        assert (
            "registering again with the same agent_id is expected and harmless" in text
        )

    def test_already_assigned_task_is_resumed(self) -> None:
        """A worker that stopped mid-task continues it instead of asking anew."""
        text = CANONICAL_PROMPT.read_text()
        assert "ALREADY have a task assigned" in text

    def test_redo_tool_is_on_the_allowlist(self) -> None:
        """request_task_redo is a tool every worker may use."""
        text = CANONICAL_PROMPT.read_text()
        allow = text[text.index("You can only use these Marcus tools") :]
        allow = allow[: allow.index("- You CANNOT")]
        assert "request_task_redo" in allow

    def test_redo_guidance_draws_the_glue_line(self) -> None:
        """Redo is for another agent's wrong DONE task; glue is your own work."""
        text = CANONICAL_PROMPT.read_text()
        assert "REDO VS BLOCKER VS FIX-IN-PLACE" in text
        assert "request_task_redo(agent_id, task_id, reason)" in text
        assert "composition work, not a redo" in text
        assert "fix it in place" in text

    def test_redone_work_is_merged_before_reverification(self) -> None:
        """Marcus merges the redo into main; the waiter must pull main first."""
        text = " ".join(CANONICAL_PROMPT.read_text().split())
        redo = text[text.index("REDO VS BLOCKER") :]
        assert "git merge main --no-edit" in redo[: redo.index("Mechanical GLUE")]
        assert "BEFORE you re-verify" in redo

    def test_tdd_is_a_project_standard_not_a_how(self) -> None:
        """Tests first, watched to fail, framework left to the agent."""
        text = CANONICAL_PROMPT.read_text()
        assert "PROJECT STANDARD: TDD" in text
        assert "Write the tests FIRST" in text
        assert "WATCH THEM FAIL" in text
        assert "you pick the framework" in text

    def test_both_harness_modes_are_named(self) -> None:
        """The sheet works for a self-looping worker and a one-chore harness."""
        text = CANONICAL_PROMPT.read_text()
        assert "HARNESS MODES" in text
        assert "ONE CHORE" in text
        assert "request next task (go to a)" in text


class TestSessionHeaderDefersToContract:
    """The launcher's sheet is a header, not a second contract."""

    def test_header_points_at_the_contract_files(self) -> None:
        """Workers are told the contract is the instruction file in their cwd."""
        text = SESSION_HEADER.read_text()
        assert "CLAUDE.md" in text and "AGENTS.md" in text and "GEMINI.md" in text
        assert "YOUR CONTRACT" in text

    def test_header_does_not_restate_the_blocker_contract(self) -> None:
        """Severity semantics live in one place; a copy here would drift again."""
        text = SESSION_HEADER.read_text()
        assert "ADVISORY" not in text
        assert "TERMINAL" not in text
        assert "fresh agent" not in text.lower()

    def test_header_keeps_only_the_chore_mechanics(self) -> None:
        """Identity, one chore, the two markers, and the exit rules."""
        text = SESSION_HEADER.read_text()
        assert "ONE CHORE" in text
        assert ".marcus_session_done" in text
        assert ".marcus_chore_done" in text
        assert "{agent_id}" in text and "{workspace}" in text
