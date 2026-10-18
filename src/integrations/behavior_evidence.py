"""Per-app-type behavior evidence contract + judge (issue #677).

The composition / integration gates historically proved a project *builds*
(`npm run build` exit 0) and *serves* (`curl` returns 2xx), never that it
*behaves*. A web app with a client-side error serves 200 with a blank page
and passed (the snake-pr667-5 / #463 / #654 / #636 failure mode).

This module closes that gap **without** turning Marcus into a tooling
registry. Marcus owns two things, per app type:

1. The **evidence contract** — WHAT must be submitted to prove an outcome
   actually behaves (a rendered DOM, a pipeline's output, a CLI's stdout).
2. The **judge** — given the submitted evidence, does it satisfy the bar?

Marcus stays tool-agnostic: it does NOT prescribe HOW to capture the
evidence (headless browser, pytest, a script — the agent's choice, per the
``VerificationSpec`` "coordination, not a tooling registry" principle and
Invariant #2). The agent runs whatever tool produces the evidence and
submits it; Marcus judges the *evidence*, not an agent-chosen exit code —
which is what keeps it ungameable (the agent cannot pass by declaring a
weak command). See ``docs/design/677-composition-verification-loop.md``.

Fuzzy / unclassified app types (``other``, ``automation``) have NO behavior
contract here and fall back to the legacy exit-0 verification, so
unclassified projects never regress.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

# Marcus's setup-time ``structural_category`` (advanced_parser.py) -> the
# internal behavior "kind" whose contract/judge applies. Categories not in
# this map (``other``, ``automation``, unknown) have no behavior contract.
_CATEGORY_TO_KIND: Dict[str, str] = {
    "web app": "web",
    "game": "web",
    "data pipeline": "pipeline",
    "cli tool": "cli",
    "library": "library",
    "api service": "api",
    "ml/ai": "ml",
}


def _kind(structural_category: str) -> str:
    """Map a structural category to a behavior kind, or '' if none."""
    return _CATEGORY_TO_KIND.get((structural_category or "").strip().lower(), "")


def has_behavior_contract(structural_category: str) -> bool:
    """Whether this app type is gated by a behavior-evidence contract.

    Parameters
    ----------
    structural_category : str
        Marcus's setup-time classification (e.g. ``"web app"``,
        ``"data pipeline"``).

    Returns
    -------
    bool
        ``True`` if a per-type behavior contract/judge applies; ``False``
        for fuzzy/unclassified types (which fall back to legacy exit-0
        verification).
    """
    return _kind(structural_category) != ""


_CONTRACTS: Dict[str, str] = {
    "web": (
        "Submit behavior evidence that the app RENDERS. Load the assembled "
        "app (headless is fine -- you choose the tool) and submit `dom` (the "
        "rendered HTML of the page body) and `console_errors` (any errors "
        "logged during load). Marcus passes only when the DOM is non-empty "
        "AND there are no console errors. A server returning 200 with a blank "
        "page does NOT pass."
    ),
    "pipeline": (
        "Submit behavior evidence that the pipeline PRODUCES OUTPUT. Run it on "
        "representative sample input and submit `output` (the produced output) "
        "or `output_rows` (the row count). Marcus passes only when the output "
        "is non-empty."
    ),
    "cli": (
        "Submit behavior evidence that the command WORKS. Run the documented "
        "command and submit `exit_code` and `stdout`. Marcus passes only on "
        "exit 0 with non-empty stdout."
    ),
    "library": (
        "Submit behavior evidence that the public API WORKS. Import the library "
        "and call a documented public function; submit `import_ok` (bool) and "
        "`call_result` (the returned value). Marcus passes only when the import "
        "succeeds and the call returns a value."
    ),
    "api": (
        "Submit behavior evidence that the service RESPONDS CORRECTLY. Start it "
        "and make a real request; submit `status` and `body`. Marcus passes "
        "only on a 2xx status WITH a non-empty body -- not just 2xx."
    ),
    "ml": (
        "Submit behavior evidence that the model PREDICTS. Load it and run "
        "inference on a sample; submit `prediction`. Marcus passes only when a "
        "prediction is produced."
    ),
}


def behavior_evidence_contract(structural_category: str) -> str:
    """Return the evidence the agent must submit for this app type.

    This is the WHAT (outcome-level, tool-agnostic) that Marcus authors for
    the composition/integration task description. It never names a tool.

    Parameters
    ----------
    structural_category : str
        Marcus's setup-time classification.

    Returns
    -------
    str
        The contract instruction text, or ``""`` for types with no behavior
        contract (caller falls back to legacy verification).
    """
    return _CONTRACTS.get(_kind(structural_category), "")


def _nonempty_str(value: Any, minimum: int = 1) -> bool:
    """Return True if value is a string with ≥ ``minimum`` non-space chars."""
    return isinstance(value, str) and len(value.strip()) >= minimum


def judge_behavior_evidence(
    structural_category: str, evidence: Dict[str, Any]
) -> Tuple[bool, str]:
    """Judge submitted behavior evidence against the per-type bar.

    Marcus judges the *evidence the agent captured*, not an exit code the
    agent chose -- this is the ungameable check that proves the deliverable
    behaves rather than merely builds.

    Parameters
    ----------
    structural_category : str
        Marcus's setup-time classification.
    evidence : Dict[str, Any]
        The agent-submitted evidence payload (shape depends on app type;
        see :func:`behavior_evidence_contract`).

    Returns
    -------
    Tuple[bool, str]
        ``(passed, reason)``. For app types with no behavior contract,
        returns ``(True, ...)`` -- the behavior judge does not gate them
        (the caller should check :func:`has_behavior_contract` first).
    """
    kind = _kind(structural_category)
    ev = evidence or {}

    if kind == "web":
        dom = ev.get("dom")
        console_errors = ev.get("console_errors") or []
        if not _nonempty_str(dom, minimum=10) or "<" not in str(dom):
            return False, "rendered DOM is empty or missing -- the app did not render"
        if console_errors:
            sample = "; ".join(str(e) for e in list(console_errors)[:3])
            return False, f"console errors during render: {sample}"
        return True, "rendered a non-empty DOM with no console errors"

    if kind == "pipeline":
        if "output_rows" in ev:
            rows = ev.get("output_rows")
            ok = isinstance(rows, int) and rows > 0
            return ok, (
                f"pipeline produced {rows} output row(s)"
                if ok
                else "pipeline produced no output rows"
            )
        output = ev.get("output")
        if output is None or (isinstance(output, str) and not output.strip()):
            return False, "pipeline produced no output"
        if isinstance(output, (list, dict)) and len(output) == 0:
            return False, "pipeline produced empty output"
        return True, "pipeline produced non-empty output"

    if kind == "cli":
        exit_code = ev.get("exit_code")
        stdout = ev.get("stdout")
        if exit_code != 0:
            return False, f"command exited non-zero (exit_code={exit_code!r})"
        if not _nonempty_str(stdout):
            return False, "command produced no stdout"
        return True, "command exited 0 with stdout"

    if kind == "library":
        if ev.get("import_ok") is not True:
            return False, "library failed to import"
        if ev.get("call_result") is None:
            return False, "public API call returned nothing"
        return True, "library imported and a public call returned a value"

    if kind == "api":
        status = ev.get("status")
        body = ev.get("body")
        if not (isinstance(status, int) and 200 <= status < 300):
            return False, f"service did not return a 2xx status (status={status!r})"
        if body is None or not str(body).strip():
            return False, "service returned an empty body"
        return True, "service returned a 2xx status with a non-empty body"

    if kind == "ml":
        # ``ml/ai`` maps to kind ``ml`` and the contract asks for a produced
        # ``prediction``; without this branch malformed volunteered evidence
        # (e.g. an empty prediction) would fall through to "not gated" and be
        # wrongly accepted (Codex P2 on #679).
        prediction = ev.get("prediction")
        if prediction is None or (
            isinstance(prediction, str) and not prediction.strip()
        ):
            return False, "model produced no prediction"
        if isinstance(prediction, (list, dict)) and len(prediction) == 0:
            return False, "model produced an empty prediction"
        return True, "model produced a prediction"

    # No behavior contract for this app type: the behavior judge does not
    # gate it (caller falls back to legacy verification).
    return True, "no behavior contract for this app type; not gated by evidence"
