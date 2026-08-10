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
    """ADR-0012 D11: every prompt must teach the fencing token.

    Marcus hands the agent a ``lease_epoch`` on ``request_next_task`` and
    fences reports that carry a superseded one. An agent that never
    learns to send it back is invisible to the fence, so this contract
    has to hold in every prompt copy — the two drift silently otherwise,
    which is the reason this module exists.
    """

    def test_names_the_lease_epoch(self, prompt_path: Path) -> None:
        """The token is named, not merely implied."""
        assert "lease_epoch" in prompt_path.read_text()

    def test_instructs_passing_it_on_every_report(self, prompt_path: Path) -> None:
        """The agent is told to carry it back on reports, not just save it."""
        text = prompt_path.read_text().lower()
        assert "every" in text
        assert "report_task_progress" in text

    def test_states_that_stale_work_is_preserved(self, prompt_path: Path) -> None:
        """Preserve-not-discard must be explicit.

        An agent that believes a stale report destroys its work will
        retry under pressure — the #636 compliance-theater failure mode.
        """
        text = prompt_path.read_text().lower()
        assert "stale_epoch" in text
        assert "not lost" in text

    def test_tells_the_agent_to_stop_rather_than_resolve(
        self, prompt_path: Path
    ) -> None:
        """Reconciliation is a board card, never the agent's to settle."""
        text = prompt_path.read_text().lower()
        assert "do not retry" in text
        assert "reconcile" in text
