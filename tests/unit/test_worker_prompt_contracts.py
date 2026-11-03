"""
Contract tests for the worker prompt files (Codex P1 on PR #720).

Marcus ships more than one worker prompt:

* ``prompts/Agent_prompt.md`` — the CANONICAL, user-facing spec. The
  README tells operators to copy this into every project directory, so
  this is the prompt real users' agents run with.
* ``dev-tools/experiments/templates/agent_prompt.md`` — the prompt the
  experiment runner embeds for spawned agents.

They drift silently: #719's severity contract was added to the
experiment template only, so agents launched via the documented setup
would still send the default ``medium`` for an impossible blocker and
unexpectedly keep the task. These tests pin the behaviour-defining
contracts in BOTH files so a future change to one surfaces the other.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_PROMPT = REPO_ROOT / "prompts" / "Agent_prompt.md"
EXPERIMENT_PROMPT = (
    REPO_ROOT / "dev-tools" / "experiments" / "templates" / "agent_prompt.md"
)

ALL_PROMPTS = [CANONICAL_PROMPT, EXPERIMENT_PROMPT]


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
    signal and never on its own reading of the board; the experiment
    template is the one-task copy slated for deletion and is not held to
    the loop contract.
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
