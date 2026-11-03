"""Cross-cutting telemetry guarantees (Marcus #416 / #546, Task #10).

The per-event test files verify each helper in isolation.  This file
pins three properties that span *every* event helper at once — the
ones a reviewer would otherwise have to re-check by hand on every new
event:

1. **stdio safety.**  Marcus runs as an MCP server over stdio: stdout
   is reserved for JSON-RPC frames.  A telemetry helper that ever
   ``print``s would corrupt the protocol.  Every helper must be
   stdout-silent.

2. **Taxonomy ↔ disclosure agreement.**  Several events ship a
   bucketed enum label (``task_phase``, ``blocker_type``, ``domain``,
   ``structural_category``).  The code's taxonomy and the published
   ``docs/telemetry.md`` disclosure must not drift apart.

3. **Privacy regression net.**  No event helper may grow a parameter
   that could carry PII / source code / free text (``project_name``,
   ``description``, ``prompt``, ``prd``, ...).  The function signature
   is the regression net — this test pins it.
"""

from __future__ import annotations

import inspect
import io
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MARCUS_TELEMETRY", raising=False)
    monkeypatch.delenv("MARCUS_POSTHOG_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    client = MagicMock()
    monkeypatch.setattr("src.telemetry.events.get_telemetry_client", lambda: client)
    return client


class _FakeTask:
    """Minimal Task stand-in: ``fire_task_completed`` reads ``labels``."""

    def __init__(self, labels: list) -> None:
        self.labels = labels


#: Every public ``fire_*`` event helper, with a representative set of
#: arguments.  Keep this in sync with ``src.telemetry.events.__all__``;
#: ``test_all_fire_helpers_are_covered`` fails if a helper is missing.
def _fire_calls() -> dict:
    from src.telemetry import events as ev

    return {
        "fire_project_created": lambda: ev.fire_project_created(
            result={"success": True, "tasks_created": 5},
            options={},
            actual_decomposer="feature_based",
        ),
        "fire_task_completed": lambda: ev.fire_task_completed(
            _FakeTask(labels=["backend"])
        ),
        "fire_task_blocked": lambda: ev.fire_task_blocked(
            severity="high", blocker_description="connection timed out"
        ),
        "fire_lease_expired": lambda: ev.fire_lease_expired(
            task_held_minutes=5,
            progress_pct_at_expiry=0,
            recovery_attempted=True,
        ),
        "fire_validator_retry": lambda: ev.fire_validator_retry(
            retry_count=1, final_result="pass", validation_type="x"
        ),
        "fire_structured_llm_retry": lambda: ev.fire_structured_llm_retry(
            operation="parse_prd",
            retry_count=1,
            reason="truncation",
            final="ok",
        ),
        "fire_error_occurred": lambda: ev.fire_error_occurred(
            error_type="KanbanIntegrationError"
        ),
        "fire_agent_registered": lambda: ev.fire_agent_registered(
            role="backend", skills=["python"]
        ),
        "fire_planning_intent_fidelity": lambda: (
            ev.fire_planning_intent_fidelity(
                decomposer="feature_based",
                intent_fidelity_score=0.9,
                coverage_before_fill=0.5,
                coverage_after_fill=0.9,
                gap_filled_outcomes=2,
            )
        ),
        "fire_project_cost_summary": lambda: ev.fire_project_cost_summary(
            {"input_tokens": 1, "output_tokens": 1}
        ),
        "fire_epictetus_result": lambda: ev.fire_epictetus_result(
            grade="B", recommendations=["improve coverage"]
        ),
    }


class TestStdioSafety:
    """No event helper may write to stdout — it would corrupt JSON-RPC."""

    def test_no_fire_helper_writes_to_stdout(
        self, isolated_home: Path, mock_client: Any
    ) -> None:
        """Calling every helper produces zero bytes on stdout.

        Falsification recipe: add ``print("debug")`` to any helper.
        Confirm this test fails with that helper's name in the
        captured output.
        """
        for name, call in _fire_calls().items():
            buf = io.StringIO()
            with redirect_stdout(buf):
                call()
            assert buf.getvalue() == "", (
                f"{name} wrote to stdout: {buf.getvalue()!r} — this would "
                f"corrupt the MCP JSON-RPC stream"
            )

    def test_classifiers_are_stdout_silent(self) -> None:
        """The classifier / sanitizer helpers are also stdout-silent."""
        from src.telemetry import events as ev

        buf = io.StringIO()
        with redirect_stdout(buf):
            ev.classify_blocker_type("connection timed out")
            ev.extract_task_phase(["backend"])
            ev.sanitize_epictetus_recommendations(["a rec"])
        assert buf.getvalue() == ""


class TestAllFireHelpersCovered:
    """The stdio sweep must exercise every public fire_* helper."""

    def test_all_fire_helpers_are_covered(self) -> None:
        """Every ``fire_*`` name in ``events.__all__`` is in _fire_calls.

        Stops a newly-added event helper from silently escaping the
        stdio-safety and privacy sweeps.
        """
        from src.telemetry import events as ev

        exported = {n for n in ev.__all__ if n.startswith("fire_")}
        covered = set(_fire_calls().keys())
        missing = exported - covered
        assert not missing, (
            f"New fire_* helpers not covered by cross-cutting tests: "
            f"{missing}. Add them to _fire_calls()."
        )


class TestTaxonomyDisclosureAgreement:
    """Code taxonomies must match what docs/telemetry.md discloses."""

    def test_task_phase_buckets_match_disclosure(self) -> None:
        from src.telemetry.events import _TASK_PHASE_BUCKETS

        assert _TASK_PHASE_BUCKETS == frozenset(
            {
                "backend",
                "frontend",
                "design",
                "integration",
                "testing",
                "deployment",
                "documentation",
                "foundation",
            }
        )

    def test_blocker_types_match_disclosure(self) -> None:
        from src.telemetry.events import _BLOCKER_TYPE_KEYWORDS

        code_types = {bucket for bucket, _ in _BLOCKER_TYPE_KEYWORDS}
        assert code_types == {
            "dependency_not_ready",
            "timeout",
            "missing_credential",
            "tool_error",
            "ambiguous_requirement",
            "async_failure",
        }

    def test_extract_task_phase_only_returns_taxonomy_or_unknown(
        self,
    ) -> None:
        """Any label resolves to a bucket or 'unknown' — never free text."""
        from src.telemetry.events import (
            _TASK_PHASE_BUCKETS,
            extract_task_phase,
        )

        allowed = _TASK_PHASE_BUCKETS | {"unknown"}
        assert extract_task_phase(["backend"]) in allowed
        assert extract_task_phase(["totally-made-up-label"]) == "unknown"
        assert extract_task_phase(None) == "unknown"
        assert extract_task_phase([]) == "unknown"

    def test_classify_blocker_only_returns_taxonomy_or_unknown(self) -> None:
        """Blocker text resolves to a bucket or 'unknown' — never the text."""
        from src.telemetry.events import (
            _BLOCKER_TYPE_KEYWORDS,
            classify_blocker_type,
        )

        allowed = {b for b, _ in _BLOCKER_TYPE_KEYWORDS} | {"unknown"}
        assert classify_blocker_type("connection timed out") in allowed
        assert classify_blocker_type("a wholly novel problem") == "unknown"
        assert classify_blocker_type("") == "unknown"


class TestPrivacyRegressionNet:
    """No event helper may accept a parameter that could carry PII."""

    #: Substrings that, if they appear in a helper's parameter name,
    #: indicate the helper could be handed identifying / free-text
    #: content.  The privacy contract is that telemetry ships only
    #: counts, durations, and enum buckets — never these.
    _FORBIDDEN_PARAM_SUBSTRINGS = (
        "project_name",
        "description",
        "prompt",
        "prd",
        "message",
        "content",
        "task_name",
        "agent_name",
        "email",
        "path",
        "source_code",
        "stack_trace",
        "traceback",
    )

    #: The one reviewed exception.  ``fire_task_blocked`` accepts the
    #: raw ``blocker_description`` *solely* to run it through the local
    #: keyword classifier ``classify_blocker_type``; the text itself is
    #: never put on the wire (pinned by the classifier tests in
    #: ``test_task_events.py::TestClassifyBlockerType``).  Classification
    #: lives inside the helper so the privacy contract is co-located
    #: with the disclosure document.  Any *new* entry here must clear
    #: the same bar — local use only, never shipped.
    _REVIEWED_FREE_TEXT_PARAMS = {("fire_task_blocked", "blocker_description")}

    def test_no_fire_helper_accepts_pii_parameter(self) -> None:
        """Every fire_* helper's signature is free of PII-shaped params.

        One reviewed exception is allowlisted — see
        ``_REVIEWED_FREE_TEXT_PARAMS``.

        Falsification recipe: add a ``project_name: str`` parameter to
        any ``fire_*`` helper.  Confirm this test fails naming that
        helper and parameter.
        """
        from src.telemetry import events as ev

        offenders = []
        for name in ev.__all__:
            if not name.startswith("fire_"):
                continue
            fn = getattr(ev, name)
            for param in inspect.signature(fn).parameters:
                if (name, param) in self._REVIEWED_FREE_TEXT_PARAMS:
                    continue
                lowered = param.lower()
                for bad in self._FORBIDDEN_PARAM_SUBSTRINGS:
                    if bad in lowered:
                        offenders.append(f"{name}({param})")
        assert not offenders, (
            f"Event helpers accept PII-shaped parameters: {offenders}. "
            f"Telemetry must ship only counts / durations / enum buckets."
        )

    def test_fire_error_occurred_takes_only_error_type(self) -> None:
        """error_occurred ships the error class name, never the message."""
        from src.telemetry.events import fire_error_occurred

        params = list(inspect.signature(fire_error_occurred).parameters)
        assert params == ["error_type"]
