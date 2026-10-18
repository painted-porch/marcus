"""
Unit tests for the smoke-gate behavior-evidence retry ceiling (issue #677).

When an integration task's project has a behavior-evidence contract
(web/pipeline/cli/…) and the agent reports completion with no ``evidence``
(or evidence that fails the per-type bar), the product smoke gate rejects
with ``error="behavior_evidence_missing"`` / ``"behavior_evidence_failed"``.

Like the missing-verifications rejection (#676), this is retryable but
loops forever without a ceiling. The observed failure
(snake-composition-verification-loop, 2026-05-30): the agent CAPTURED real
rendered HTML but wrote it into the free-text ``message`` field instead of
``evidence``, so the gate saw nothing and rejected 10 times → gridlock at
81.8%. The fix converts the rejection into a TERMINAL escalation after a
small number of identical attempts so the agent stops and the failure
surfaces with its remediation payload (the exact ``evidence`` payload to
submit) instead of looping.
"""

import pytest

from src.marcus_mcp.tools.task import (
    MAX_SMOKE_BEHAVIOR_EVIDENCE_ATTEMPTS,
    _clear_smoke_attempts,
    _escalate_behavior_evidence_response,
    _record_behavior_evidence_attempt,
    _text_contains_render_markup,
)

pytestmark = pytest.mark.unit


def _behavior_evidence_rejection() -> dict:
    """A representative behavior-evidence rejection from the smoke gate."""
    return {
        "success": False,
        "status": "smoke_verification_failed",
        "error": "behavior_evidence_missing",
        "agent_id": "agent-1",
        "task_id": "task-int",
        "structural_category": "game",
        "blocker": "## Behavior evidence required ...",
        "evidence_misfiled_in_message": True,
    }


class TestBehaviorEvidenceAttemptCounter:
    """The per-task attempt counter increments and clears."""

    def test_record_increments_per_task(self) -> None:
        _clear_smoke_attempts("be-count")
        assert _record_behavior_evidence_attempt("be-count") == 1
        assert _record_behavior_evidence_attempt("be-count") == 2
        assert _record_behavior_evidence_attempt("be-count") == 3

    def test_counters_are_independent_per_task(self) -> None:
        _clear_smoke_attempts("be-a")
        _clear_smoke_attempts("be-b")
        _record_behavior_evidence_attempt("be-a")
        _record_behavior_evidence_attempt("be-a")
        assert _record_behavior_evidence_attempt("be-b") == 1

    def test_clear_resets_count(self) -> None:
        _record_behavior_evidence_attempt("be-clear")
        _clear_smoke_attempts("be-clear")
        assert _record_behavior_evidence_attempt("be-clear") == 1

    def test_clear_resets_both_smoke_counters(self) -> None:
        # _clear_smoke_attempts must reset the behavior counter too, so a
        # recovered/reassigned task gets a fresh set of attempts.
        from src.marcus_mcp.tools.task import _record_missing_verification_attempt

        _record_missing_verification_attempt("be-both")
        _record_behavior_evidence_attempt("be-both")
        _clear_smoke_attempts("be-both")
        assert _record_behavior_evidence_attempt("be-both") == 1
        assert _record_missing_verification_attempt("be-both") == 1


class TestEscalationResponse:
    """The terminal escalation preserves remediation but stops retries."""

    def test_marks_terminal_and_escalated(self) -> None:
        out = _escalate_behavior_evidence_response(_behavior_evidence_rejection())
        assert out["terminal"] is True
        assert out["escalated"] is True
        assert out["success"] is False

    def test_uses_a_distinct_error_so_it_is_not_a_retry_signal(self) -> None:
        out = _escalate_behavior_evidence_response(_behavior_evidence_rejection())
        assert out["error"] != "behavior_evidence_missing"
        assert out["error"] == "behavior_evidence_escalated"

    def test_preserves_remediation_payload(self) -> None:
        out = _escalate_behavior_evidence_response(_behavior_evidence_rejection())
        assert "blocker" in out
        assert out["structural_category"] == "game"
        # original is not mutated
        src = _behavior_evidence_rejection()
        _escalate_behavior_evidence_response(src)
        assert src["error"] == "behavior_evidence_missing"


class TestCeilingDecision:
    """Attempts at/under the ceiling stay retryable; beyond it escalates."""

    def test_attempt_beyond_ceiling_triggers_escalation(self) -> None:
        _clear_smoke_attempts("be-ceil")
        last = 0
        for _ in range(MAX_SMOKE_BEHAVIOR_EVIDENCE_ATTEMPTS + 1):
            last = _record_behavior_evidence_attempt("be-ceil")
        assert last > MAX_SMOKE_BEHAVIOR_EVIDENCE_ATTEMPTS


class TestRenderMarkupDetector:
    """The misfiling detector recognizes HTML/DOM embedded in free text."""

    @pytest.mark.parametrize(
        "text",
        [
            "<div id='app'></div>",
            "The page renders: <canvas></canvas>",
            "<!DOCTYPE html><html><body>hi</body></html>",
            "Output: <svg><rect/></svg>",
        ],
    )
    def test_detects_markup(self, text: str) -> None:
        assert _text_contains_render_markup(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            None,
            "All done, everything works great.",
            "123 tests passed, build succeeded, exit 0",
        ],
    )
    def test_no_false_positive_on_plain_text(self, text: str) -> None:
        assert _text_contains_render_markup(text) is False
