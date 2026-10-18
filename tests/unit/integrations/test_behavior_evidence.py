"""
Unit tests for the per-app-type behavior evidence contract + judge (#677).

Marcus owns the *evidence contract* — WHAT must be submitted to prove an
outcome actually behaves (a rendered DOM, a pipeline's output, a CLI's
stdout) — and JUDGES the submitted evidence. It stays tool-agnostic: it
does NOT prescribe HOW to capture the evidence (that is the agent's
choice, per the ``VerificationSpec`` "coordination, not a tooling
registry" principle and Invariant #2). The judge is what closes the
"builds-but-renders-empty" gap (#463/#654/#636/#677): empty evidence
fails, regardless of build/serve success.
"""

import pytest

from src.integrations.behavior_evidence import (
    behavior_evidence_contract,
    has_behavior_contract,
    judge_behavior_evidence,
)

pytestmark = pytest.mark.unit


class TestHasBehaviorContract:
    """Clear app types get a behavior contract; fuzzy ones fall back."""

    @pytest.mark.parametrize(
        "category",
        ["web app", "game", "data pipeline", "CLI tool", "library", "API service"],
    )
    def test_known_types_have_contract(self, category: str) -> None:
        assert has_behavior_contract(category) is True

    def test_is_case_insensitive(self) -> None:
        assert has_behavior_contract("Web App") is True
        assert has_behavior_contract("DATA PIPELINE") is True

    @pytest.mark.parametrize("category", ["other", "automation", "unknown", ""])
    def test_fuzzy_types_have_no_contract(self, category: str) -> None:
        # No behavior contract -> caller falls back to legacy exit-0
        # verification, so unclassified projects never regress.
        assert has_behavior_contract(category) is False


class TestEvidenceContractText:
    """The contract text is domain-appropriate and non-web-biased."""

    def test_web_app_asks_for_rendered_dom(self) -> None:
        text = behavior_evidence_contract("web app").lower()
        assert "dom" in text or "render" in text
        assert "console" in text

    def test_data_pipeline_asks_for_output_not_http(self) -> None:
        text = behavior_evidence_contract("data pipeline").lower()
        assert "output" in text or "sample" in text
        assert "curl" not in text and "http" not in text

    def test_cli_asks_for_stdout(self) -> None:
        text = behavior_evidence_contract("CLI tool").lower()
        assert "stdout" in text or "exit" in text

    def test_no_contract_type_returns_empty(self) -> None:
        assert behavior_evidence_contract("other") == ""


class TestJudgeWebApp:
    """A web app must show a non-empty rendered DOM with no console errors."""

    def test_empty_dom_fails(self) -> None:
        passed, reason = judge_behavior_evidence(
            "web app", {"dom": "", "console_errors": []}
        )
        assert passed is False
        assert "dom" in reason.lower() or "empty" in reason.lower()

    def test_missing_dom_fails(self) -> None:
        passed, _ = judge_behavior_evidence("web app", {"console_errors": []})
        assert passed is False

    def test_rendered_dom_no_errors_passes(self) -> None:
        passed, _ = judge_behavior_evidence(
            "web app",
            {
                "dom": "<div id='app'><canvas></canvas><div class='score'>0</div></div>",
                "console_errors": [],
            },
        )
        assert passed is True

    def test_console_errors_fail_even_with_dom(self) -> None:
        passed, reason = judge_behavior_evidence(
            "web app",
            {
                "dom": "<div id='app'><canvas></canvas></div>",
                "console_errors": ["ReferenceError: process is not defined"],
            },
        )
        assert passed is False
        assert "console" in reason.lower() or "error" in reason.lower()


class TestJudgeDataPipeline:
    """A pipeline must produce non-empty output (not just build)."""

    def test_empty_output_fails(self) -> None:
        passed, _ = judge_behavior_evidence("data pipeline", {"output": ""})
        assert passed is False

    def test_nonempty_output_passes(self) -> None:
        passed, _ = judge_behavior_evidence(
            "data pipeline", {"output": "id,score\n1,42\n2,17\n"}
        )
        assert passed is True

    def test_zero_rows_fails(self) -> None:
        passed, _ = judge_behavior_evidence("data pipeline", {"output_rows": 0})
        assert passed is False


class TestJudgeCli:
    """A CLI must exit 0 and produce stdout."""

    def test_exit_zero_with_stdout_passes(self) -> None:
        passed, _ = judge_behavior_evidence(
            "CLI tool", {"exit_code": 0, "stdout": "done: 3 files processed"}
        )
        assert passed is True

    def test_nonzero_exit_fails(self) -> None:
        passed, _ = judge_behavior_evidence(
            "CLI tool", {"exit_code": 1, "stdout": "boom"}
        )
        assert passed is False

    def test_empty_stdout_fails(self) -> None:
        passed, _ = judge_behavior_evidence("CLI tool", {"exit_code": 0, "stdout": ""})
        assert passed is False


class TestJudgeLibrary:
    """A library must import and a public call must return something."""

    def test_import_and_call_passes(self) -> None:
        passed, _ = judge_behavior_evidence(
            "library", {"import_ok": True, "call_result": "4"}
        )
        assert passed is True

    def test_import_failure_fails(self) -> None:
        passed, _ = judge_behavior_evidence(
            "library", {"import_ok": False, "call_result": None}
        )
        assert passed is False


class TestJudgeMl:
    """An ML/AI project must produce a prediction (Codex P2 on #679: this
    branch was missing, so empty predictions wrongly passed as 'not gated')."""

    def test_prediction_present_passes(self) -> None:
        passed, _ = judge_behavior_evidence("ml/ai", {"prediction": "cat (0.92)"})
        assert passed is True

    def test_empty_prediction_fails(self) -> None:
        passed, _ = judge_behavior_evidence("ml/ai", {"prediction": ""})
        assert passed is False

    def test_missing_prediction_fails(self) -> None:
        passed, _ = judge_behavior_evidence("ml/ai", {})
        assert passed is False
