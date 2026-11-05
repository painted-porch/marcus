"""Unit tests for the Condition A runner.

No real model calls happen here -- every ``claude -p`` invocation is
mocked, so these tests pin the loop's control flow (rounds, retries,
resume wiring, the shared-session requirement H3 depends on) and the
verdict-reading contract, without spending anything.
"""

import json
from unittest.mock import patch

import pytest
from runners.numeric_condition_a import (
    DEFAULT_MODEL,
    read_self_check,
    run_condition_a_case,
)
from runners.numeric_cost_watchdog import StopCheck


@pytest.fixture
def staged(tmp_path):
    """Lay out a minimal staged evaluation directory.

    Returns
    -------
    pathlib.Path
        The staged directory.
    """
    d = tmp_path / "staged"
    d.mkdir()
    (d / "metrics.json").write_text(json.dumps({"n_records": 10}))
    return d


def fake_claude_factory(script):
    """Build a fake ``_run_claude`` that writes files per call, in order.

    Parameters
    ----------
    script : list
        One callable per expected call, taking the workspace Path and
        performing whatever side effect that call should have.

    Returns
    -------
    callable
        A drop-in replacement for ``_run_claude``.
    """
    calls = {"n": 0}

    def fake(prompt, workspace, model, timeout, session_id=None, resume=None):
        i = calls["n"]
        calls["n"] += 1
        if i < len(script):
            script[i](workspace)
        return {"returncode": 0, "seconds": 1.0, "stdout_tail": "", "stderr_tail": ""}

    return fake, calls


def write_report_and_claims(workspace):
    """Simulate a writer pass: report.md + claims.json appear."""
    (workspace / "report.md").write_text("R2 was 0.9.")
    (workspace / "claims.json").write_text(
        json.dumps(
            {"claims": [{"id": "c1", "value": 0.9, "label": "R2", "kind": "lookup"}]}
        )
    )


def accept(workspace):
    """Simulate a self-check pass that accepts."""
    (workspace / "self_check.json").write_text(json.dumps({"verdict": "accept"}))


def reject(reason):
    """Build a self-check side effect that rejects with one reason.

    Returns
    -------
    callable
        A side effect writing a reject verdict.
    """

    def _reject(workspace):
        (workspace / "self_check.json").write_text(
            json.dumps({"verdict": "reject", "reasons": [reason]})
        )

    return _reject


class TestReadSelfCheck:
    """Reading back whatever the self-check actually wrote."""

    @pytest.mark.unit
    def test_missing_file_is_a_reject_not_a_crash(self, tmp_path):
        """A checker that produced nothing does not count as accepted."""
        result = read_self_check(tmp_path)
        assert result["verdict"] == "reject"
        assert "missing" in result["reasons"][0]

    @pytest.mark.unit
    def test_malformed_json_is_a_reject(self, tmp_path):
        """A broken file is treated as a failure to check, not a pass."""
        (tmp_path / "self_check.json").write_text("{not json")
        result = read_self_check(tmp_path)
        assert result["verdict"] == "reject"

    @pytest.mark.unit
    def test_unrecognized_verdict_value_is_a_reject(self, tmp_path):
        """A verdict that isn't accept or reject is not an accept by default."""
        (tmp_path / "self_check.json").write_text(json.dumps({"verdict": "maybe"}))
        result = read_self_check(tmp_path)
        assert result["verdict"] == "reject"

    @pytest.mark.unit
    def test_a_real_accept_is_read_through(self, tmp_path):
        """The happy path passes the value through unchanged."""
        (tmp_path / "self_check.json").write_text(json.dumps({"verdict": "accept"}))
        assert read_self_check(tmp_path)["verdict"] == "accept"


class TestRoundFlow:
    """The write-then-check loop's control flow."""

    @pytest.mark.unit
    def test_accepted_on_the_first_round_stops_there(self, staged, tmp_path):
        """No retry happens once the self-check accepts."""
        fake, calls = fake_claude_factory([write_report_and_claims, accept])
        with patch("runners.numeric_condition_a._run_claude", fake):
            outcome = run_condition_a_case(staged, tmp_path / "out")
        assert outcome["verdict"] == "accept"
        assert outcome["rounds"] == 1
        assert calls["n"] == 2  # one write, one check

    @pytest.mark.unit
    def test_rejection_triggers_a_second_round(self, staged, tmp_path):
        """A reject on round 1 leads to another write-then-check pass."""
        fake, calls = fake_claude_factory(
            [
                write_report_and_claims,
                reject("c1: wrong"),
                write_report_and_claims,
                accept,
            ]
        )
        with patch("runners.numeric_condition_a._run_claude", fake):
            outcome = run_condition_a_case(staged, tmp_path / "out")
        assert outcome["verdict"] == "accept"
        assert outcome["rounds"] == 2
        assert calls["n"] == 4

    @pytest.mark.unit
    def test_exhausting_every_round_reports_exhausted_not_accept(
        self, staged, tmp_path
    ):
        """Never reaching an accept is a distinct, honest outcome."""
        script = []
        for _ in range(3):
            script += [write_report_and_claims, reject("c1: still wrong")]
        fake, calls = fake_claude_factory(script)
        with patch("runners.numeric_condition_a._run_claude", fake):
            outcome = run_condition_a_case(staged, tmp_path / "out", max_rounds=3)
        assert outcome["verdict"] == "exhausted"
        assert outcome["rounds"] == 3

    @pytest.mark.unit
    def test_round_log_records_every_attempt(self, staged, tmp_path):
        """Each round's outcome is on the record, not just the final one."""
        fake, _ = fake_claude_factory(
            [
                write_report_and_claims,
                reject("c1: bad"),
                write_report_and_claims,
                accept,
            ]
        )
        with patch("runners.numeric_condition_a._run_claude", fake):
            outcome = run_condition_a_case(staged, tmp_path / "out")
        assert len(outcome["round_log"]) == 2
        assert outcome["round_log"][0]["verdict"] == "reject"
        assert outcome["round_log"][1]["verdict"] == "accept"


class TestSharedSession:
    """H3 requires the self-check to continue the writer's own session."""

    @pytest.mark.unit
    def test_every_call_after_the_first_resumes_the_same_session(
        self, staged, tmp_path
    ):
        """The self-check and any retry share one session id throughout."""
        seen_resume = []
        seen_session_id = []

        def fake(prompt, workspace, model, timeout, session_id=None, resume=None):
            seen_session_id.append(session_id)
            seen_resume.append(resume)
            n = len(seen_resume)
            if n == 1:
                write_report_and_claims(workspace)
            elif n == 2:
                accept(workspace)
            return {
                "returncode": 0,
                "seconds": 1.0,
                "stdout_tail": "",
                "stderr_tail": "",
            }

        with patch("runners.numeric_condition_a._run_claude", fake):
            outcome = run_condition_a_case(staged, tmp_path / "out")

        # The first call pins a session id; every later call resumes
        # that exact id, never starts fresh -- a fresh self-check
        # session would not see the writer's reasoning at all.
        assert seen_session_id[0] == outcome["session_id"]
        assert seen_resume[0] is None
        assert seen_resume[1] == outcome["session_id"]

    @pytest.mark.unit
    def test_retry_also_resumes_the_original_session(self, staged, tmp_path):
        """A retry round still shares context, not a fresh conversation."""
        seen_resume = []

        def fake(prompt, workspace, model, timeout, session_id=None, resume=None):
            seen_resume.append(resume)
            n = len(seen_resume)
            if n in (1, 3):
                write_report_and_claims(workspace)
            elif n == 2:
                reject("c1: bad")(workspace)
            elif n == 4:
                accept(workspace)
            return {
                "returncode": 0,
                "seconds": 1.0,
                "stdout_tail": "",
                "stderr_tail": "",
            }

        with patch("runners.numeric_condition_a._run_claude", fake):
            outcome = run_condition_a_case(staged, tmp_path / "out")

        assert all(r == outcome["session_id"] for r in seen_resume[1:])


class TestModelParameter:
    """The writer/checker model is a flag, not a code edit."""

    @pytest.mark.unit
    def test_default_model_is_used_when_not_specified(self, staged, tmp_path):
        """Omitting --model falls back to the project default."""
        seen_models = []

        def fake(prompt, workspace, model, timeout, session_id=None, resume=None):
            seen_models.append(model)
            if len(seen_models) == 1:
                write_report_and_claims(workspace)
            else:
                accept(workspace)
            return {
                "returncode": 0,
                "seconds": 1.0,
                "stdout_tail": "",
                "stderr_tail": "",
            }

        with patch("runners.numeric_condition_a._run_claude", fake):
            run_condition_a_case(staged, tmp_path / "out")
        assert all(m == DEFAULT_MODEL for m in seen_models)

    @pytest.mark.unit
    def test_a_different_model_is_passed_through_unchanged(self, staged, tmp_path):
        """Pointing at Sonnet needs no edit to this module, only the flag."""
        seen_models = []

        def fake(prompt, workspace, model, timeout, session_id=None, resume=None):
            seen_models.append(model)
            if len(seen_models) == 1:
                write_report_and_claims(workspace)
            else:
                accept(workspace)
            return {
                "returncode": 0,
                "seconds": 1.0,
                "stdout_tail": "",
                "stderr_tail": "",
            }

        with patch("runners.numeric_condition_a._run_claude", fake):
            run_condition_a_case(staged, tmp_path / "out", model="claude-sonnet-5")
        assert all(m == "claude-sonnet-5" for m in seen_models)

    @pytest.mark.unit
    def test_writer_and_self_check_use_the_same_model(self, staged, tmp_path):
        """The protocol calls for one model on both sides of the check."""
        seen_models = []

        def fake(prompt, workspace, model, timeout, session_id=None, resume=None):
            seen_models.append(model)
            if len(seen_models) == 1:
                write_report_and_claims(workspace)
            else:
                accept(workspace)
            return {
                "returncode": 0,
                "seconds": 1.0,
                "stdout_tail": "",
                "stderr_tail": "",
            }

        with patch("runners.numeric_condition_a._run_claude", fake):
            run_condition_a_case(staged, tmp_path / "out", model="claude-haiku-4-5")
        assert len(set(seen_models)) == 1


class TestCodexFindingCostWatchdogWiring:
    """check_before_next_pass existed but nothing in this runner called it.

    numeric_cost_watchdog.check_before_next_pass is documented as
    mandatory before every claude -p invocation in this exact loop,
    but the only caller in the whole codebase was an uncommitted,
    never-launched scratch orchestrator. A real run had no way to
    actually stop at an approved cost ceiling. stop_line is opt-in
    (None, the default, checks nothing, so every existing caller is
    unaffected) but when given, it must be enforced before each pass.
    """

    @pytest.mark.unit
    def test_omitting_stop_line_never_checks_cost(self, staged, tmp_path):
        """No stop_line means no watchdog call at all, as before."""
        fake, _ = fake_claude_factory([write_report_and_claims, accept])
        with (
            patch("runners.numeric_condition_a._run_claude", fake),
            patch("runners.numeric_condition_a.check_before_next_pass") as mock_check,
        ):
            run_condition_a_case(staged, tmp_path / "out")
        mock_check.assert_not_called()

    @pytest.mark.unit
    def test_already_over_the_line_stops_before_any_pass_runs(self, staged, tmp_path):
        """Nothing is launched if the line was already reached."""
        fake, calls = fake_claude_factory([write_report_and_claims, accept])
        with (
            patch("runners.numeric_condition_a._run_claude", fake),
            patch(
                "runners.numeric_condition_a.check_before_next_pass",
                return_value=StopCheck(spent=25.0, stop_line=20.0, should_stop=True),
            ),
        ):
            outcome = run_condition_a_case(staged, tmp_path / "out", stop_line=20.0)
        assert calls["n"] == 0
        assert outcome["verdict"] == "stopped_cost_limit"
        assert outcome["rounds"] == 0

    @pytest.mark.unit
    def test_reaching_the_line_after_the_write_pass_skips_self_check(
        self, staged, tmp_path
    ):
        """A line crossed mid-round still halts before the next pass."""
        fake, calls = fake_claude_factory([write_report_and_claims])
        checks = [
            StopCheck(spent=10.0, stop_line=20.0, should_stop=False),
            StopCheck(spent=25.0, stop_line=20.0, should_stop=True),
        ]
        with (
            patch("runners.numeric_condition_a._run_claude", fake),
            patch(
                "runners.numeric_condition_a.check_before_next_pass",
                side_effect=checks,
            ),
        ):
            outcome = run_condition_a_case(staged, tmp_path / "out", stop_line=20.0)
        assert calls["n"] == 1  # the write pass ran, the self-check did not
        assert outcome["verdict"] == "stopped_cost_limit"

    @pytest.mark.unit
    def test_staying_under_the_line_runs_the_case_normally(self, staged, tmp_path):
        """A stop_line that is never reached changes nothing else."""
        fake, calls = fake_claude_factory([write_report_and_claims, accept])
        with (
            patch("runners.numeric_condition_a._run_claude", fake),
            patch(
                "runners.numeric_condition_a.check_before_next_pass",
                return_value=StopCheck(spent=1.0, stop_line=20.0, should_stop=False),
            ),
        ):
            outcome = run_condition_a_case(staged, tmp_path / "out", stop_line=20.0)
        assert outcome["verdict"] == "accept"
        assert calls["n"] == 2
