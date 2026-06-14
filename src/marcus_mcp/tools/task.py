"""
Task Management Tools for Marcus MCP.

This module contains tools for task operations in the Marcus system:
- request_next_task: Get optimal task assignment for an agent
- report_task_progress: Update progress on assigned tasks
- report_blocker: Report blockers with AI-powered suggestions
- unassign_task: Manually unassign a task from an agent
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.core.ai_powered_task_assignment import find_optimal_task_for_agent_ai_powered
from src.core.models import Priority, Task, TaskAssignment, TaskStatus
from src.core.task_classification import get_task_type
from src.integrations.behavior_evidence import (
    behavior_evidence_contract,
    has_behavior_contract,
    judge_behavior_evidence,
)
from src.logging.agent_events import log_agent_event
from src.logging.conversation_logger import conversation_logger, log_thinking
from src.marcus_mcp.utils import serialize_for_mcp

logger = logging.getLogger(__name__)

# Module-level singletons for validation system (initialized lazily)
_work_analyzer: Optional[Any] = None
_retry_tracker: Optional[Any] = None


# Retry ceiling: after this many validation failures on the same
# task, stop blocking and route the task through the normal
# completion path with an escalation annotation. See
# ``report_task_progress`` for the inline check — the ceiling must
# be evaluated BEFORE calling ``_handle_validation_failure`` so the
# escalated task still goes through the kanban-completion, memory
# recording, and branch-merge steps. Codex P1 on PR #337: returning
# a success response from the failure path left tasks incomplete
# in kanban and reintroduced the workflow stall the ceiling was
# meant to prevent.
MAX_VALIDATION_RETRIES = 3
_singleton_lock = threading.Lock()  # Thread-safe initialization


# Smoke-gate missing-verifications retry ceiling (issue #676). The product
# smoke gate rejects an integration completion that declares no
# ``verifications`` for its in-scope outcomes. That rejection is retryable
# (the agent can add verifications and re-report), but with NO ceiling an
# agent that keeps re-sending the same incomplete report loops forever ->
# lease expiry -> BLOCKED -> project gridlock (snake-pr667-5 hit 6 identical
# rejections). After this many identical rejections the rejection is
# converted to a TERMINAL escalation so the agent stops retrying and the
# failure is surfaced with its remediation payload instead of looping.
MAX_SMOKE_MISSING_VERIFICATION_ATTEMPTS = 2
_smoke_missing_verification_attempts: Dict[str, int] = {}


# Smoke-gate behavior-evidence retry ceiling (issue #677). The product smoke
# gate rejects an integration completion whose project has a behavior-evidence
# contract (web/pipeline/cli/…) when the agent submits no ``evidence`` or
# evidence that fails the per-type bar. Like the missing-verifications
# rejection above, this is retryable but loops forever without a ceiling.
# Observed failure (snake-composition-verification-loop, 2026-05-30): the agent
# CAPTURED real rendered HTML but wrote it into the free-text ``message`` field
# instead of ``evidence``, so the gate saw nothing and rejected 10 times ->
# gridlock at 81.8%. After this many identical rejections the rejection becomes
# a TERMINAL escalation so the agent stops and the failure surfaces with its
# remediation payload.
MAX_SMOKE_BEHAVIOR_EVIDENCE_ATTEMPTS = 2
_smoke_behavior_evidence_attempts: Dict[str, int] = {}


def _record_missing_verification_attempt(task_id: str) -> int:
    """Increment and return the missing-verifications rejection count.

    Parameters
    ----------
    task_id : str
        The integration task being rejected for missing verifications.

    Returns
    -------
    int
        The cumulative number of missing-verifications rejections seen for
        this task (1 on the first call).
    """
    count = _smoke_missing_verification_attempts.get(task_id, 0) + 1
    _smoke_missing_verification_attempts[task_id] = count
    return count


def _record_behavior_evidence_attempt(task_id: str) -> int:
    """Increment and return the behavior-evidence rejection count.

    Parameters
    ----------
    task_id : str
        The integration/composition task being rejected for missing or
        failing behavior evidence (issue #677).

    Returns
    -------
    int
        The cumulative number of behavior-evidence rejections seen for
        this task (1 on the first call).
    """
    count = _smoke_behavior_evidence_attempts.get(task_id, 0) + 1
    _smoke_behavior_evidence_attempts[task_id] = count
    return count


def _clear_smoke_attempts(task_id: str) -> None:
    """Reset the missing-verifications and behavior-evidence counts for a task."""
    _smoke_missing_verification_attempts.pop(task_id, None)
    _smoke_behavior_evidence_attempts.pop(task_id, None)


def _escalate_missing_verifications_response(
    smoke_response: Dict[str, Any],
) -> Dict[str, Any]:
    """Convert a repeatable missing-verifications rejection into a terminal one.

    After ``MAX_SMOKE_MISSING_VERIFICATION_ATTEMPTS`` identical rejections the
    agent is plainly not going to add verifications by re-sending the same
    call, so we stop the loop (issue #676): re-tag the rejection with a
    distinct, non-retryable ``error`` plus ``terminal``/``escalated`` flags
    while preserving the remediation payload (``blocker``,
    ``missing_outcome_ids``) so the failure is actionable rather than a silent
    gridlock. The input dict is not mutated.

    Parameters
    ----------
    smoke_response : Dict[str, Any]
        The ``verifications_required_but_missing`` rejection to escalate.

    Returns
    -------
    Dict[str, Any]
        A new dict tagged terminal/escalated, remediation fields intact.
    """
    escalated = dict(smoke_response)
    escalated["status"] = "smoke_verification_escalated"
    escalated["error"] = "verifications_required_escalated"
    escalated["terminal"] = True
    escalated["escalated"] = True
    escalated["message"] = (
        "Marcus has rejected this completion "
        f"{MAX_SMOKE_MISSING_VERIFICATION_ATTEMPTS} times for the same "
        "reason: no ``verifications`` for the task's required in-scope "
        "outcomes. Do NOT retry the same call. This integration task is "
        "escalated for remediation -- the outcome IDs that need coverage are "
        "in ``missing_outcome_ids`` and the fix is in ``blocker``."
    )
    return escalated


def _escalate_behavior_evidence_response(
    smoke_response: Dict[str, Any],
) -> Dict[str, Any]:
    """Convert a repeatable behavior-evidence rejection into a terminal one.

    After ``MAX_SMOKE_BEHAVIOR_EVIDENCE_ATTEMPTS`` identical rejections the
    agent is plainly not going to satisfy the behavior-evidence bar by
    re-sending the same call (issue #677), so we stop the loop: re-tag the
    rejection with a distinct, non-retryable ``error`` plus
    ``terminal``/``escalated`` flags while preserving the remediation payload
    (``blocker``, ``structural_category``) so the failure surfaces with its
    fix instead of gridlocking the project. The input dict is not mutated.

    Parameters
    ----------
    smoke_response : Dict[str, Any]
        The ``behavior_evidence_missing`` / ``behavior_evidence_failed``
        rejection to escalate.

    Returns
    -------
    Dict[str, Any]
        A new dict tagged terminal/escalated, remediation fields intact.
    """
    escalated = dict(smoke_response)
    escalated["status"] = "smoke_verification_escalated"
    escalated["error"] = "behavior_evidence_escalated"
    escalated["terminal"] = True
    escalated["escalated"] = True
    escalated["message"] = (
        "Marcus has rejected this completion "
        f"{MAX_SMOKE_BEHAVIOR_EVIDENCE_ATTEMPTS} times for the same reason: "
        "the submitted behavior evidence is missing or does not meet the bar. "
        "Do NOT retry the same call. This task is escalated for remediation -- "
        "the fix (and the exact ``evidence`` payload to submit) is in "
        "``blocker``."
    )
    return escalated


async def _terminalize_escalated_smoke_task(
    state: Any, task_id: str, agent_id: str, blocker: str
) -> None:
    """Move an escalated smoke-gate task to a terminal BOARD state.

    Issue #676 (Codex P1 on PR #678): the escalation helpers above only re-tag
    the MCP *response* — no code in the repo consumes the ``*_escalated`` error
    to change kanban state.  So after the ceiling fires, an agent that obeys
    "do NOT retry" simply stops calling back and the task stays IN_PROGRESS
    with its lease held until timeout — the project idles on the active task
    instead of surfacing a terminal board state immediately, the exact gridlock
    these ceilings set out to end.

    This mirrors ``report_blocker``: mark the kanban task BLOCKED and release
    all coordination state (agent assignment + lease) so the board reflects the
    terminal state at once and the agent's slot is freed.  Best-effort — any
    failure is logged, never raised, so escalation reporting is never lost.

    Parameters
    ----------
    state : Any
        Marcus server state (``kanban_client``, ``lease_manager``, …).
    task_id : str
        The escalated task.
    agent_id : str
        Agent that held the task.
    blocker : str
        Blocker text recorded on the board.
    """
    try:
        if getattr(state, "kanban_client", None):
            await state.kanban_client.update_task(
                task_id, {"status": TaskStatus.BLOCKED, "blocker": blocker}
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "Smoke-gate escalation: failed to mark task %s BLOCKED: %s",
            task_id,
            e,
        )

    # Release coordination state — same decoupling as report_blocker (a
    # terminal status must free the agent's slot independently of any
    # correctness check, Simon decision 011b3fad).
    try:
        if getattr(state, "agent_status", None) and agent_id in state.agent_status:
            state.agent_status[agent_id].current_tasks = []
        if getattr(state, "agent_tasks", None) and agent_id in state.agent_tasks:
            del state.agent_tasks[agent_id]
        if getattr(state, "assignment_persistence", None):
            await state.assignment_persistence.remove_assignment(agent_id)
        if getattr(state, "lease_manager", None):
            if task_id in state.lease_manager.active_leases:
                del state.lease_manager.active_leases[task_id]
                logger.info(
                    "Released lease for escalated (terminal) task %s (agent %s)",
                    task_id,
                    agent_id,
                )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "Smoke-gate escalation: failed to release coordination state "
            "for task %s: %s",
            task_id,
            e,
        )


def clear_validation_retry(task_id: str) -> None:
    """Clear the validation retry history for a task.

    Parameters
    ----------
    task_id : str
        The task whose retry counter should be reset.

    Notes
    -----
    Called when a task lease is recovered and reassigned to a new agent
    so the incoming agent is not penalised for the previous agent's
    validation failures.  Safe to call before the validation system is
    initialised (no-op in that case).
    """
    if _retry_tracker is not None:
        _retry_tracker.clear_task(task_id)
        logger.debug("Cleared validation retry history for recovered task %s", task_id)
    # Issue #676: also reset the smoke-gate missing-verifications counter so a
    # recovered/reassigned task gets a fresh set of attempts.
    _clear_smoke_attempts(task_id)


async def get_project_board_context(state: Any) -> Dict[str, Optional[str]]:
    """
    Extract project and board context from state.

    Parameters
    ----------
    state : Any
        Marcus server state instance

    Returns
    -------
    Dict[str, Optional[str]]
        Dictionary with project_id, project_name, board_id, board_name
        (values are None if not available)
    """
    context: Dict[str, Optional[str]] = {
        "project_id": None,
        "project_name": None,
        "board_id": None,
        "board_name": None,
    }

    try:
        # Get active project from registry
        if hasattr(state, "project_registry") and state.project_registry:
            active_project = await state.project_registry.get_active_project()
            if active_project:
                context["project_id"] = active_project.id
                context["project_name"] = active_project.name

                # Extract board_id from provider_config
                provider_config = active_project.provider_config or {}

                if active_project.provider == "planka":
                    context["board_id"] = provider_config.get("board_id")
                    # Board name might not be available, we could fetch it
                    # from kanban client if needed
                elif active_project.provider == "github":
                    # GitHub uses repo as the "board"
                    context["board_id"] = provider_config.get("repo")
                    context["board_name"] = provider_config.get("repo")
                elif active_project.provider == "linear":
                    # Linear uses team_id and project_id
                    context["board_id"] = provider_config.get("team_id")
                    context["board_name"] = provider_config.get("team_id")

    except Exception as e:
        logger.debug(f"Error extracting project/board context: {e}")

    return context


def _get_task_type(task: Task) -> str:
    """Determine task type ("implementation" / "design" / "testing").

    Thin wrapper over the shared :func:`src.core.task_classification.
    get_task_type` so instruction layering here and #680 gotcha
    placement in the coordinator read from one source of truth. Kept as
    a module-level name because existing callers and tests import
    ``_get_task_type`` from this module.

    Parameters
    ----------
    task : Task
        Task to classify.

    Returns
    -------
    str
        ``"implementation"``, ``"design"``, or ``"testing"``.
    """
    return get_task_type(task)


def _build_mandatory_workflow_prompt(task: Task) -> str:
    """
    Build mandatory workflow prompt that agents MUST follow.

    This prompt creates a forcing function by requiring agents to
    write a todo list with enumerated workflow steps.

    Parameters
    ----------
    task : Task
        Task being assigned (used to insert task description)

    Returns
    -------
    str
        Formatted mandatory workflow prompt

    Notes
    -----
    This addresses Issue #168: Agents not following CLAUDE.md workflow.
    The todo list requirement makes workflow visible and trackable.
    """
    workflow_prompt = f"""🔴 MANDATORY WORKFLOW 🔴

Before starting work, you MUST write a todo list with these steps:

1. Call get_task_context to check dependencies and artifacts
2. Read artifacts from dependency tasks
3. [TASK WORK: {task.name}]
   {task.description}
4. Report progress at 25%, 50%, 75% milestones
5. Log decisions (log_decision) and artifacts (log_artifact) as needed
6. BEFORE reporting "completed", verify:
   - Does your code actually run without errors?
   - Do all the tests for this task pass?
7. Report completion with implementation summary
8. Be prepared for remediation work if validation fails and resubmit progress
9. Immediately request next task

⚠️ CRITICAL BEHAVIORS:
- Check dependencies with get_task_context BEFORE starting work
- Read artifacts from dependency tasks to understand prior work
- Report progress at each milestone (not just at completion)
- Log decisions as they're made (not after task completion)
- VERIFY code runs and tests pass BEFORE reporting completed
- Be ready to address validation feedback and resubmit

This workflow ensures coordination with other agents and prevents
incomplete implementations."""
    return workflow_prompt


def _is_integration_task(task: Task) -> bool:
    """Detect whether a task is an integration verification task.

    Integration verification tasks are produced by
    ``IntegrationTaskGenerator.create_integration_task`` and carry
    the ``"type:integration"`` label as a stable type marker. They
    are NOT validated by the citation-based LLM validator (which
    only runs on implementation tasks) — instead they are gated by
    the product smoke verifier, which runs subprocess-level checks
    that the assembled product builds.

    Parameters
    ----------
    task : Task
        Task to inspect.

    Returns
    -------
    bool
        True if the task carries ``type:integration`` in its labels.
        Defensive: returns False if labels is missing or not a
        sequence.
    """
    labels = getattr(task, "labels", None)
    if not labels:
        return False
    try:
        return "type:integration" in labels
    except TypeError:
        return False


def _is_composition_task(task: Task) -> bool:
    """Detect whether a task is a Marcus-synthesized composition task.

    Composition tasks are produced by
    :func:`src.integrations.composition_synthesis.build_composition_task`
    when a multi-domain contract-first project needs an explicit
    entry-point wiring step.  They carry the ``"composition"`` label
    and ``source_type == "composition_synthesis"``.

    Detection accepts either signal so kanban round-trips that strip
    labels (preserving ``source_type`` in ``source_context``) still
    surface as composition tasks.  Mirror of the existing
    ``_has_existing_composition_task`` detection in
    ``src/integrations/composition_synthesis.py``.

    Parameters
    ----------
    task : Task
        Task to inspect.

    Returns
    -------
    bool
        True if the task is a composition task.

    Notes
    -----
    Used by ``report_task_progress`` to stamp composition-task
    completions as self-reported (issue #677 self-verify mode: Marcus
    no longer runs an independent build check on these — verification
    lives with the agent; see the honesty stamp at the completion
    site).
    """
    labels = getattr(task, "labels", None) or []
    try:
        if "composition" in labels:
            return True
    except TypeError:
        pass
    return getattr(task, "source_type", None) == "composition_synthesis"


def _missing_verifications_for_required_outcomes_response(
    *,
    task: Task,
    agent_id: str,
    required_outcome_ids: List[str],
) -> Dict[str, Any]:
    """Reject completion when the task has outcomes but agent sent no specs.

    Slice B (#523) escape-hatch closure (Kaia review on PR #525).

    The smoke gate's existing coverage check fires only inside the
    ``if verifications:`` branch.  Without this preliminary check an
    agent could send ``verifications=None`` (or empty list) plus a
    valid legacy ``start_command``, fall through to
    ``verify_deliverable``, and ship a project whose user-observable
    outcomes were never verified — the exact failure mode Slice B
    targets.

    Rejection shape mirrors :func:`_missing_coverage_response` so
    Cato / log analyzers can treat both as the same "missing
    coverage" category, distinguished by ``error`` ("verifications_
    required_but_missing" here vs "verifications_missing_coverage"
    for partial coverage inside the verifications branch).

    Parameters
    ----------
    task
        Integration task being completed.
    agent_id
        Agent reporting completion.
    required_outcome_ids
        In-scope outcome IDs the task carries on
        ``source_context["in_scope_outcome_ids"]``.  Listed in the
        blocker so the agent knows the full set they must cover.
    """
    blocker = (
        "## Verifications required for this integration task\n\n"
        "Marcus rejected this completion because the integration task "
        "was created with in-scope user outcomes, but the agent did "
        "not declare any ``verifications``.  When user outcomes are "
        "in scope, the legacy ``start_command`` alone is not "
        "sufficient — Marcus cannot prove the deliverable produced "
        "each outcome's ``success_signal`` without per-outcome "
        "verification commands.\n\n"
        f"**In-scope outcomes** ({len(required_outcome_ids)}, all "
        "required):\n\n"
        + "\n".join(f"- `{oid}`" for oid in required_outcome_ids)
        + "\n\n"
        "**What to do**: look up each outcome's ``action`` and "
        "``success_signal`` in the integration task description's "
        "*Verifications required* section.  Write a shell command "
        "whose exit code reflects whether the signal is observable "
        "in the running deliverable (pick the tool that fits — "
        "`curl`, `playwright`, `pytest`, etc.).  Call "
        "``report_task_progress`` again with a ``verifications`` "
        "list containing one entry per outcome above.\n\n"
        "Retrying with the same call (omitting ``verifications``) "
        "will fail with the same rejection — the legacy "
        "``start_command`` path is closed for tasks that carry "
        "declared outcomes."
    )
    return {
        "success": False,
        "status": "smoke_verification_failed",
        "error": "verifications_required_but_missing",
        "agent_id": agent_id,
        "task_id": task.id,
        "failure_summary": (
            f"integration task carries {len(required_outcome_ids)} "
            "in-scope outcome(s) but the agent did not declare a "
            "verifications list"
        ),
        "blocker": blocker,
        "missing_outcome_ids": list(required_outcome_ids),
        "required_outcome_ids": list(required_outcome_ids),
        "declared_signal_ids": [],
        "message": (
            "Marcus rejected this integration-task completion: the "
            "task carries declared in-scope user outcomes and the "
            "agent did not provide a ``verifications`` list.  See "
            "the ``blocker`` field for the outcome IDs that need "
            "coverage."
        ),
    }


def _missing_coverage_response(
    *,
    task: Task,
    agent_id: str,
    required_outcome_ids: List[str],
    declared_signal_ids: List[str],
    missing_outcome_ids: List[str],
) -> Dict[str, Any]:
    """Build the smoke-gate rejection for missing verification coverage.

    Slice B (#523) acceptance criterion: when an integration task has
    declared in-scope user outcomes, every outcome must be covered by
    at least one ``VerificationSpec`` whose ``signal_id`` matches.  The
    rejection blocker lists which outcomes lacked a matching spec so
    the agent knows exactly what to add to their next
    ``report_task_progress`` call — they do not have to re-derive the
    coverage map themselves.

    The fix is structural, not retry-friendly: re-running the same
    completion with the same incomplete ``verifications`` list will
    fail again.  The agent must add the missing entries.

    Parameters
    ----------
    task
        Integration task being completed (for the rejection's
        ``task_id`` field).
    agent_id
        Agent reporting completion (for the rejection's ``agent_id``
        field).
    required_outcome_ids
        All in-scope outcome IDs the task carries on
        ``source_context["in_scope_outcome_ids"]``.  Listed for the
        agent's full reference.
    declared_signal_ids
        ``signal_id`` values the agent did declare (sorted for stable
        rendering).  Listed so the agent can see what they HAD
        provided and diff against the required set.
    missing_outcome_ids
        The subset of ``required_outcome_ids`` with no matching
        ``signal_id`` in the declared list.  Drives the
        ``failure_summary`` field and is the primary fix target.
    """
    blocker = (
        "## Verification coverage FAILED\n\n"
        "Marcus rejected this integration-task completion because at "
        "least one in-scope user outcome has no matching "
        "``VerificationSpec`` in the ``verifications`` list.  Every "
        "in-scope outcome must be covered by at least one spec whose "
        "``signal_id`` matches the outcome's ``id``.\n\n"
        f"**Missing coverage** ({len(missing_outcome_ids)} outcome(s)):\n\n"
        + "\n".join(f"- `{oid}`" for oid in missing_outcome_ids)
        + "\n\n"
        f"**Required in-scope outcomes** ({len(required_outcome_ids)} "
        "from the task's source_context):\n\n"
        + "\n".join(f"- `{oid}`" for oid in required_outcome_ids)
        + "\n\n"
        f"**Verifications you declared** "
        f"({len(declared_signal_ids)} signal_id(s)):\n\n"
        + (
            "\n".join(f"- `{sid}`" for sid in declared_signal_ids)
            if declared_signal_ids
            else "- (none)"
        )
        + "\n\n"
        "**What to do**: for each missing outcome, look up its "
        "``action`` and ``success_signal`` in the integration task "
        "description's *Verifications required* section.  Write a "
        "shell command whose exit code reflects whether the signal "
        "is observable in the running deliverable (pick the tool that "
        "fits — `curl`, `playwright`, `pytest`, etc.).  Add one entry "
        "per missing outcome to the ``verifications`` list and "
        "re-call ``report_task_progress`` with the complete set.\n\n"
        "Retrying with the same incomplete list will fail with the "
        "same rejection — this is a structural coverage check, not a "
        "subprocess-level failure."
    )
    return {
        "success": False,
        "status": "smoke_verification_failed",
        "error": "verifications_missing_coverage",
        "agent_id": agent_id,
        "task_id": task.id,
        "failure_summary": (
            f"verifications missing coverage for "
            f"{len(missing_outcome_ids)} outcome(s): "
            f"{', '.join(missing_outcome_ids)}"
        ),
        "blocker": blocker,
        "missing_outcome_ids": missing_outcome_ids,
        "required_outcome_ids": required_outcome_ids,
        "declared_signal_ids": declared_signal_ids,
        "message": (
            "Marcus rejected this integration-task completion: the "
            "``verifications`` list is missing entries for one or more "
            "in-scope user outcomes.  See the ``blocker`` field for "
            "the specific outcome IDs that need coverage."
        ),
    }


# Markers that strongly indicate rendered HTML / a captured DOM is present in
# a free-text string (used to detect the "agent narrated the proof into
# ``message`` instead of submitting ``evidence``" misfiling, issue #677).
_RENDER_MARKUP_MARKERS = (
    "<!doctype",
    "<html",
    "<body",
    "<div",
    "<canvas",
    "<span",
    "<svg",
    "<main",
    "<section",
)


def _text_contains_render_markup(text: Optional[str]) -> bool:
    """Return True if free text appears to embed rendered HTML / a DOM.

    Heuristic used only to make the behavior-evidence rejection *diagnostic*
    (issue #677): when the agent captured render proof but put it in the
    free-text ``message`` field instead of the structured ``evidence`` field
    the gate reads, the rejection can tell it precisely what went wrong
    instead of repeating a generic "submit evidence" that it has already
    ignored.  This does NOT accept the markup as evidence — it only steers
    the correction (the structured ``evidence`` field stays the only judged
    channel, keeping the gate ungameable).

    Parameters
    ----------
    text : Optional[str]
        The agent-supplied free text (typically ``message``).

    Returns
    -------
    bool
        ``True`` if at least one HTML element marker is present.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _RENDER_MARKUP_MARKERS)


def _behavior_evidence_rejection(
    *,
    task: Task,
    agent_id: str,
    structural_category: str,
    reason: str,
    evidence_missing: bool,
    misfiled_in_message: bool = False,
) -> Dict[str, Any]:
    """Build a smoke-gate rejection for failing/absent behavior evidence.

    Issue #677: for app types with a behavior-evidence contract, Marcus
    judges the *evidence the agent captured by running the assembled
    product* — not a build exit code or an HTTP 200.  This helper renders
    the rejection the agent receives when the evidence is missing or does
    not meet the per-type bar, telling it exactly what evidence to capture
    and resubmit in the ``evidence`` field of ``report_task_progress``.

    Parameters
    ----------
    task : Task
        The integration/composition task being completed.
    agent_id : str
        Agent reporting completion.
    structural_category : str
        Marcus's setup-time classification, used to surface the
        per-type contract text in the blocker message.
    reason : str
        Human-readable reason the evidence failed (from the judge) or a
        "no evidence submitted" message.
    evidence_missing : bool
        ``True`` when no ``evidence`` payload was submitted at all (vs.
        submitted-but-failed-the-bar).  Distinguishes the two error
        strings so the agent knows whether to start capturing evidence
        or to fix the product.
    misfiled_in_message : bool
        ``True`` when no ``evidence`` was submitted but the agent's
        free-text ``message`` appears to contain the render proof
        (issue #677 forcing function).  When set, the blocker LEADS with a
        targeted correction — "you put your proof in ``message``; I only
        read ``evidence``; re-send it as ``evidence={...}``" — because a
        generic "submit evidence" has demonstrably been ignored (the agent
        narrated real HTML into ``message`` 10× in one run).

    Returns
    -------
    Dict[str, Any]
        A rejection dict the caller returns directly to the agent.
    """
    contract = behavior_evidence_contract(structural_category)
    error = (
        "behavior_evidence_missing" if evidence_missing else "behavior_evidence_failed"
    )
    # Forcing function (issue #677): when the proof is sitting in ``message``,
    # lead the blocker with the exact correction so the captured HTML lands in
    # the field the gate actually judges.  A worked instruction ("move it to
    # evidence={...}") beats repeating the generic ask that was ignored.
    misfile_prefix = ""
    if misfiled_in_message:
        misfile_prefix = (
            "STOP — you put your render proof (HTML/output) in the ``message`` "
            "field. Marcus does NOT read ``message`` for verification; it only "
            "judges the structured ``evidence`` field, which you left empty. "
            "Do not re-describe the proof in prose. Re-call report_task_progress "
            "with the SAME proof in ``evidence`` instead, e.g. "
            '``evidence={"dom": "<paste the rendered HTML you captured>", '
            '"console_errors": []}``. '
        )
    return {
        "success": False,
        "status": "smoke_verification_failed",
        "error": error,
        "agent_id": agent_id,
        "task_id": task.id,
        "structural_category": structural_category,
        "failure_summary": reason,
        "evidence_misfiled_in_message": misfiled_in_message,
        "blocker": (
            f"{misfile_prefix}"
            f"Marcus rejected this completion: the product did not pass the "
            f"behavior-evidence bar for a {structural_category!r} project. "
            f"{reason}. A build that exits 0 and a server that returns 200 "
            f"do NOT prove the product behaves. {contract} Capture this "
            f"evidence by actually running the assembled product and submit "
            f"it in the ``evidence`` field of report_task_progress."
        ),
        "message": (
            f"Marcus rejected this integration-task completion: behavior "
            f"evidence "
            f"{'was not submitted' if evidence_missing else 'did not meet the bar'}"
            f". {reason}. See the ``blocker`` field for what to capture."
        ),
    }


async def _run_product_smoke_gate(
    task: Task,
    agent_id: str,
    state: Any,
    start_command: Optional[str],
    readiness_probe: Optional[str],
    verifications: Optional[List[Dict[str, Any]]] = None,
    evidence: Optional[Dict[str, Any]] = None,
    message: str = "",
) -> Optional[Dict[str, Any]]:
    """Run deliverable verification for a completing integration task.

    Resolves the project root from the kanban workspace state, runs
    the agent-declared verification command(s) as subprocess(es), and
    returns either ``None`` (verification passed — completion may
    proceed) or a rejection dict that the caller returns directly to
    the agent.

    **Strict enforcement**: integration tasks MUST declare either
    ``verifications`` OR ``start_command``.  Completions that omit
    both are rejected with the missing-declaration blocker message.
    Locked in Simon decision 967555f6 (no fallback auto-detection;
    agents own stack knowledge) and extended in #523 Slice B.

    Parameters
    ----------
    task : Task
        The integration verification task being completed.
    agent_id : str
        Agent reporting completion.
    state : Any
        Marcus server state (provides kanban_client for workspace
        state lookup).
    start_command : Optional[str]
        Legacy single-command field.  When ``verifications`` is also
        provided, ``start_command`` is ignored (the list is canonical).
        When ``verifications`` is ``None``, ``start_command`` is
        wrapped as a single legacy :class:`VerificationSpec` and the
        gate runs in 1:1 backward-compat mode.  Either path produces
        the same rejection behavior on failure.
    readiness_probe : Optional[str]
        Companion to legacy ``start_command``.  Ignored when
        ``verifications`` is provided (each verification carries its
        own ``readiness_probe`` field).
    verifications : Optional[List[Dict[str, Any]]]
        Issue #523 Slice B: agent-declared verifications, one per
        in-scope user outcome.  Each dict carries ``signal_id``,
        ``command``, optional ``description``, and optional
        ``readiness_probe``.  Marcus runs every declared command via
        the same subprocess primitive ``start_command`` uses — exit 0
        means the outcome's success_signal was observed.  Empty list
        is rejected upstream (caller should pass ``None`` for legacy
        mode); non-empty list takes precedence over ``start_command``.
    evidence : Optional[Dict[str, Any]]
        Issue #677: behavior evidence the agent captured by running the
        assembled product.  When ``task.source_context`` carries a
        ``structural_category`` with a behavior contract
        (:func:`has_behavior_contract`), this payload is judged against
        the per-type bar via :func:`judge_behavior_evidence` before any
        subprocess runs.  Missing or failing evidence rejects the
        completion.  A passing behavior judgment satisfies the gate on
        its own (no legacy ``start_command`` additionally required),
        while outcome-bearing tasks still run their ``verifications``
        coverage path.  Ignored for app types with no behavior contract.

    Returns
    -------
    Optional[Dict[str, Any]]
        ``None`` if verification passed. A rejection dict with
        ``success=False``, ``status="smoke_verification_failed"``,
        and a ``blocker`` payload if verification failed or both
        ``verifications`` and ``start_command`` were missing.

    Notes
    -----
    Verification system errors (programmer errors in the runner
    itself, not deliverable failures) propagate as exceptions so the
    caller can log-and-continue. Deliverable failures return a
    rejection dict.
    """
    from pathlib import Path as _Path

    from src.integrations.product_smoke import (
        VerificationSpec,
        verify_verification_specs,
    )

    # Resolve project root via the kanban client's workspace state.
    # This is the same source of truth used by
    # ``_merge_agent_branch_to_main`` so the smoke gate sees the
    # same project root that the agent's git operations target.
    # Helper: build a rejection dict for smoke-gate infrastructure failures.
    # Integration tasks MUST be verifiable — "cannot run the gate" is a
    # hard rejection, not a pass.  The caller's log-and-continue except
    # clause is intentionally reserved for exceptions (programmer errors in
    # the verification system), not for resolvable configuration failures
    # such as a missing or misconfigured workspace state.
    def _gate_unavailable(reason: str) -> Dict[str, Any]:
        return {
            "success": False,
            "status": "smoke_verification_failed",
            "error": "smoke_gate_unavailable",
            "agent_id": agent_id,
            "task_id": task.id,
            "failure_summary": reason,
            "blocker": (
                f"Marcus could not run the product smoke gate: {reason}. "
                "Ensure the experiment runner has written workspace state "
                "with a valid project_root before the integration task runs."
            ),
            "message": (
                f"Marcus rejected this integration-task completion because "
                f"the smoke gate could not be initialised: {reason}. "
                "Fix the workspace state and re-report completion."
            ),
        }

    project_root: Optional[str] = None
    if hasattr(state, "kanban_client") and state.kanban_client:
        try:
            ws_state = state.kanban_client._load_workspace_state()
            if ws_state and "project_root" in ws_state:
                project_root = ws_state["project_root"]
        except Exception as ws_err:
            logger.warning(
                f"PRODUCT SMOKE GATE: Failed to load workspace state "
                f"for {task.id}: {ws_err}. Rejecting completion."
            )
            return _gate_unavailable(f"workspace state load failed: {ws_err}")

    if not project_root:
        logger.warning(
            f"PRODUCT SMOKE GATE: No project_root resolved for "
            f"{task.id}. Rejecting completion."
        )
        return _gate_unavailable("project_root not found in workspace state")

    project_root_path = _Path(project_root)
    if not project_root_path.is_dir():
        logger.warning(
            f"PRODUCT SMOKE GATE: project_root {project_root!r} is "
            f"not a directory. Rejecting completion for {task.id}."
        )
        return _gate_unavailable(f"project_root {project_root!r} is not a directory")

    # ---- Self-verify-mode gate (issue #677 rework) ----
    # Marcus is NOT a tool-using agent harness — its only executor is this
    # Python process (subprocess), and it has no browser.  We deliberately do
    # NOT run our own build here: a Marcus-authored build command is BOTH
    # tech-specific (``npm run build`` etc. — Marcus owning a language ontology
    # it should not) AND less capable than the agent (the snake-skeptic-1 run
    # gridlocked because Marcus's fixed ``npm install`` failed on a peer-dep
    # conflict the agent had already worked around with ``--legacy-peer-deps``).
    # A floor that is tech-specific, weaker than the agent, and uncorrelated
    # with "the product works" (every shipped-broken run still *built*) is a
    # net-negative tripwire.  So verification lives with the agent: it runs the
    # assembled product with whatever tools it needs and fixes until the
    # outcomes work (see ``_generate_integration_description``).  Marcus only
    # checks the agent-VOLUNTEERED proof if any is submitted, and stamps the
    # completion as self-reported (caller surfaces ``independently_verified``).
    # The eventual objective check is a separate, stack-discovering "borrow
    # hands" verifier agent — not a hardcoded build command bolted on here.
    structural_category_raw = (task.source_context or {}).get("structural_category")
    structural_category = (
        structural_category_raw
        if isinstance(structural_category_raw, str)
        else "unknown"
    )

    # Optional agent-volunteered checks (NOT required in self-verify mode).
    # If the agent chose to declare verification commands or capture behavior
    # evidence, Marcus still runs/judges them as a bonus catch — but their
    # ABSENCE never blocks completion, because the agent's own run is the
    # outcome verification for now.
    if evidence and has_behavior_contract(structural_category):
        passed, judge_reason = judge_behavior_evidence(structural_category, evidence)
        if not passed:
            logger.warning(
                "PRODUCT SMOKE GATE: volunteered behavior evidence FAILED for "
                "%s (%s): %s. Rejecting.",
                task.id,
                structural_category,
                judge_reason,
            )
            return _behavior_evidence_rejection(
                task=task,
                agent_id=agent_id,
                structural_category=structural_category,
                reason=judge_reason,
                evidence_missing=False,
            )
        logger.info(
            "PRODUCT SMOKE GATE: volunteered behavior evidence PASSED for " "%s (%s).",
            task.id,
            structural_category,
        )

    if verifications:
        specs = [
            VerificationSpec(
                signal_id=str(v.get("signal_id", "")),
                command=str(v.get("command", "")),
                description=str(v.get("description", "") or ""),
                readiness_probe=(
                    str(v["readiness_probe"]) if v.get("readiness_probe") else None
                ),
            )
            for v in verifications
        ]
        logger.info(
            "PRODUCT SMOKE GATE: running %d volunteered verification(s) for %s",
            len(specs),
            task.id,
        )
        specs_result = await verify_verification_specs(
            specs=specs, cwd=project_root_path
        )
        if not specs_result.success:
            logger.warning(
                "PRODUCT SMOKE GATE: volunteered verification FAILED for %s: "
                "%s. Rejecting.",
                task.id,
                specs_result.failure_summary,
            )
            return {
                "success": False,
                "status": "smoke_verification_failed",
                "error": "verifications_failed",
                "agent_id": agent_id,
                "task_id": task.id,
                "failure_summary": specs_result.failure_summary,
                "blocker": specs_result.blocker_message,
                "smoke_result": specs_result.to_dict(),
                "message": (
                    "Marcus rejected this integration-task completion: a "
                    "verification command you declared did not pass. See the "
                    "``blocker`` field. Fix the deliverable and re-report."
                ),
            }

    logger.info(
        "PRODUCT SMOKE GATE: %s passed (build floor + self-verify; "
        "outcome behavior verified by the agent's own run).",
        task.id,
    )
    return None


def _parse_contract_metadata(task: Task) -> Dict[str, str]:
    """
    Resolve contract-first metadata from a task across storage layers.

    GH-320 PR 2 introduces the ``Task.responsibility`` field and
    stores ``contract_file`` in ``Task.source_context``. Some kanban
    providers don't persist these fields through their round-trip:

    - SQLite (``src/integrations/providers/sqlite_kanban.py``) persists
      ``source_context`` but not ``responsibility`` as a top-level
      column.
    - Planka (``src/integrations/kanban_client.py``) persists neither
      — the provider's ``_card_to_task`` mapping constructs ``Task``
      with a minimal field set.

    To make the CONTRACT RESPONSIBILITY layer work for tasks reloaded
    from any kanban provider, ``decompose_by_contract`` also embeds
    the metadata as a structured HTML comment marker in the task
    description (which every provider round-trips as the core field).

    This helper looks in three places, in priority order:

    1. ``task.responsibility`` + ``task.source_context["contract_file"]``
       / ``["product_intent"]`` (set by fresh decomposer output, best
       signal)
    2. ``task.source_context["responsibility"]`` + ``["contract_file"]``
       / ``["product_intent"]`` (belt-and-suspenders for SQLite)
    3. Parsed from the ``MARCUS_CONTRACT_FIRST`` marker in
       ``task.description`` — last-resort fallback for providers that
       only persist description, like Planka. The marker carries
       responsibility, contract_file, and (when present) product_intent.

    Parameters
    ----------
    task : Task
        The task to inspect.

    Returns
    -------
    Dict[str, str]
        Dict with keys ``"responsibility"``, ``"contract_file"``, and
        ``"product_intent"``. Values are empty strings when not found.
        Callers can treat an empty ``responsibility`` as "this task is
        not contract-first" without needing a separate boolean flag.

    Notes
    -----
    Codex caught the persistence gap on PR #327 review. Without this
    helper, the CONTRACT RESPONSIBILITY layer would silently never
    fire for tasks reloaded from the board even though the task was
    born contract-first, defeating the whole point of PR 2.

    Phase 1 (GH-320 framing layer) added ``product_intent`` to the
    metadata so the agent prompt can surface WHY the task exists
    from the user's perspective alongside WHAT the contract
    boundary is.
    """
    # Priority 1: direct Task.responsibility field
    responsibility = getattr(task, "responsibility", None)
    source_context = getattr(task, "source_context", None) or {}

    if responsibility:
        return {
            "responsibility": str(responsibility),
            "contract_file": str(source_context.get("contract_file", "") or ""),
            "product_intent": str(source_context.get("product_intent", "") or ""),
        }

    # Priority 2: responsibility stored in source_context
    sc_responsibility = source_context.get("responsibility")
    if sc_responsibility:
        return {
            "responsibility": str(sc_responsibility),
            "contract_file": str(source_context.get("contract_file", "") or ""),
            "product_intent": str(source_context.get("product_intent", "") or ""),
        }

    # Priority 3: parse HTML comment marker from description
    description = getattr(task, "description", "") or ""
    marker_start = description.find("<!-- MARCUS_CONTRACT_FIRST")
    if marker_start == -1:
        return {"responsibility": "", "contract_file": "", "product_intent": ""}
    marker_end = description.find("-->", marker_start)
    if marker_end == -1:
        return {"responsibility": "", "contract_file": "", "product_intent": ""}

    marker_body = description[marker_start:marker_end]
    parsed_resp = ""
    parsed_file = ""
    parsed_intent = ""
    for line in marker_body.splitlines():
        line = line.strip()
        if line.startswith("responsibility:"):
            parsed_resp = line[len("responsibility:") :].strip()
        elif line.startswith("contract_file:"):
            parsed_file = line[len("contract_file:") :].strip()
        elif line.startswith("product_intent:"):
            parsed_intent = line[len("product_intent:") :].strip()

    return {
        "responsibility": parsed_resp,
        "contract_file": parsed_file,
        "product_intent": parsed_intent,
    }


def _resolve_scaffold_path(task: Task) -> str:
    """Return the scaffold anchor path for ``task``, or empty string.

    Priority order, mirroring the contract-metadata resolver above so
    the path survives any kanban provider:

    1. ``task.source_context["scaffold_path"]`` — set by SQLite's
       ``update_task`` source_context-merge code (#660). The default
       SQLite provider persists this directly.
    2. ``MARCUS_SCAFFOLD_PATH`` marker in the task description —
       fallback for non-SQLite providers (Planka, GitHub, Linear)
       that round-trip the description verbatim but don't have a
       native ``source_context`` column. Same approach
       ``_parse_contract_metadata`` uses for contract data.

    Returns an empty string when neither source carries a path —
    legacy / pre-#659 tasks render normally without the anchor section.
    """
    source_context = getattr(task, "source_context", None) or {}
    if isinstance(source_context, dict):
        sc_path = source_context.get("scaffold_path")
        if sc_path:
            return str(sc_path).strip()

    description = getattr(task, "description", "") or ""
    marker_start = description.find("<!-- MARCUS_SCAFFOLD_PATH:")
    if marker_start == -1:
        return ""
    marker_end = description.find("-->", marker_start)
    if marker_end == -1:
        return ""

    marker_body = description[marker_start:marker_end]
    # Marker body shape: ``<!-- MARCUS_SCAFFOLD_PATH: src/foo.js``
    # Strip the prefix and any whitespace.
    prefix = "<!-- MARCUS_SCAFFOLD_PATH:"
    raw = marker_body[len(prefix) :].strip()
    return raw


def build_tiered_instructions(
    base_instructions: str,
    task: Task,
    context_data: Optional[Dict[str, Any]],
    dependency_awareness: Optional[str],
    predictions: Optional[Dict[str, Any]],
    state: Optional[Any] = None,
) -> str:
    """
    Build tiered instructions based on task context and complexity.

    Parameters
    ----------
    base_instructions : str
        Base instructions always included
    task : Task
        Task to build instructions for
    context_data : Optional[Dict[str, Any]]
        Context data including previous implementations
    dependency_awareness : Optional[str]
        Dependency awareness message if task has dependents
    predictions : Optional[Dict[str, Any]]
        AI predictions and warnings if available
    state : Optional[Any]
        Marcus server state.  When provided, Layer 1.4 can look up
        dependency tasks to detect feature-based design deps and add
        up-front framing so agents know to build the complete feature
        rather than just implementing the interface contract.  Callers
        that omit ``state`` silently skip Layer 1.4 (backward compat).

    Returns
    -------
    str
        Tiered instructions with appropriate layers

    Notes
    -----
    Instruction layers:
    0. Mandatory workflow (ONLY for implementation tasks - Issue #168)
    1. Base instructions (always included)
    1.1. Recovery handoff (if task was recovered from another agent)
    1.3. Contract responsibility (contract_first tasks only - GH-320)
    1.4. Feature-based design artifact framing (feature_based tasks with
         design deps — mirrors 1.3 but for the coordination-reference role)
    2. Subtask context (if this is a subtask)
    3. Implementation context (if previous work exists)
    4. Dependency awareness (if task has dependents)
    5. Decision logging (if task affects others)
    6. Predictions and warnings (if available)
    7. Task-specific guidance (based on labels)
    """
    instructions_parts = []

    # Layer 0: Mandatory Workflow (ONLY for implementation tasks)
    # Use same task type logic as AI instruction generation
    task_type = _get_task_type(task)
    if task_type == "implementation":
        workflow_prompt = _build_mandatory_workflow_prompt(task)
        instructions_parts.append(workflow_prompt)

    # Layer 0.1: Integration task smoke gate reminder.
    # Integration tasks must declare start_command (and readiness_probe
    # for long-running servers) when calling report_task_progress with
    # status="completed". Without start_command, Marcus rejects the
    # completion with smoke_verification_failed and the agent is stuck
    # retrying until its lease expires.
    if _is_integration_task(task):
        instructions_parts.append(
            "\n\n⚠️ INTEGRATION TASK — SMOKE GATE REQUIRED ⚠️\n\n"
            "When calling report_task_progress with status='completed' you "
            "MUST include:\n\n"
            "  start_command: the shell command that starts (or builds) the "
            "deliverable.\n"
            "    • One-shot (build/type-check): e.g. 'npm run build' or "
            "'tsc --noEmit'\n"
            "    • Long-running server: e.g. 'npm run dev' (pair with "
            "readiness_probe)\n\n"
            "  readiness_probe (if server): a curl/wget command that exits 0 "
            "when the server is ready.\n"
            "    • e.g. 'curl -f http://localhost:5173' or "
            "'curl -f http://localhost:3000'\n\n"
            "Marcus will run start_command as a subprocess and reject your "
            "completion if it fails or if start_command is missing.\n\n"
            "If the smoke gate cannot be satisfied (e.g. the product is "
            "genuinely broken), call report_blocker instead of retrying "
            "report_task_progress — retrying without fixing the issue will "
            "cause your lease to expire."
        )

    # Layer 1: Base instructions
    instructions_parts.append(base_instructions)

    # Layer 1.2: Acceptance criteria (#664).
    # Marcus's setup-time pipeline (outcome-coverage enrichment, and
    # later the #680 gotcha-enumeration step) writes the concrete,
    # checkable conditions a task must satisfy into
    # ``Task.acceptance_criteria``. Until this layer existed,
    # ``request_next_task`` delivered ``completion_criteria`` but
    # dropped ``acceptance_criteria`` entirely, so every criterion the
    # pipeline enriched landed in a field no agent ever read. Surface
    # the criteria explicitly and frame them as the contract the
    # agent's work will be VERIFIED against — not optional guidance.
    # These are part of the task contract (authored by Marcus at
    # setup), distinct from the implementation HOW the agent owns.
    criteria = getattr(task, "acceptance_criteria", None) or []
    if criteria:
        criteria_lines = "\n".join(f"  - {c}" for c in criteria)
        instructions_parts.append(
            "\n\n✅ ACCEPTANCE CRITERIA (you will be verified against these):\n"
            f"{criteria_lines}\n\n"
            "These are the concrete conditions your work MUST satisfy. "
            "Marcus's verifier checks them when you report completion — "
            "treat them as the definition of done, not suggestions. They "
            "constrain WHAT must be true, not HOW you build it; the "
            "implementation is still yours to design."
        )

    # Layer 1.1: Recovery Handoff (if task was recovered from another agent)
    recovery = getattr(task, "recovery_info", None)
    if recovery is not None:
        # Check if recovery info has expired (stale after 24h)
        is_expired = (
            recovery.recovery_expires_at is not None
            and datetime.now(timezone.utc) > recovery.recovery_expires_at
        )
        if not is_expired:
            instructions_parts.append(
                f"\n\n🔄 RECOVERY HANDOFF:\n"
                f"{recovery.instructions}\n"
                f"- Time spent by previous agent: "
                f"{recovery.time_spent_minutes:.0f} minutes\n"
                f"- Previous progress: {recovery.previous_progress}%\n"
                f"- Recovery reason: {recovery.recovery_reason}"
            )

    # Layer 1.3: Contract Responsibility (GH-320 PR 2)
    # When a task owns a contract interface (contract-first
    # decomposition), surface that ownership with high signal. This
    # layer fires BEFORE subtask context because contract ownership
    # is structural — the agent must understand the contract before
    # considering the task's subtask-vs-standalone status.
    #
    # Metadata is resolved via ``_parse_contract_metadata`` which
    # checks Task.responsibility, Task.source_context, and the
    # MARCUS_CONTRACT_FIRST description marker in priority order.
    # This makes the layer robust to kanban providers that don't
    # round-trip Task.responsibility or source_context — Codex P1
    # from PR #327 review.
    contract_meta = _parse_contract_metadata(task)
    responsibility = contract_meta["responsibility"]
    if responsibility:
        contract_file = contract_meta["contract_file"]
        product_intent = contract_meta.get("product_intent", "")
        contract_notice = (
            f"\n\n📜 CONTRACT RESPONSIBILITY (contract-first decomposition):\n"
            f"You OWN: {responsibility}\n"
        )
        # Phase 1 framing layer (GH-320). When the decomposer
        # supplied a product_intent string, surface it ABOVE the
        # contract-file details so the agent reads the user-facing
        # reason the task exists before reading the interface
        # boundary. The autonomy directive that follows reframes
        # the contract as a coordination guardrail rather than a
        # prescriptive spec — agents working contract-first should
        # use professional judgment for everything the contract
        # doesn't explicitly govern (UI, error handling, helper
        # methods, internal structure). Without this layer the
        # contract dominates the prompt and agents lose the
        # "build a real product" instinct, which is what
        # dashboard-v70's "forgot what a dashboard was" regression
        # was tracking.
        if product_intent:
            contract_notice += (
                f"\n🎯 WHY THIS EXISTS: {product_intent}\n"
                f"\nUse judgment. The contract is a COORDINATION "
                f"BOUNDARY, not a build spec. You choose the "
                f"implementation, helper methods, UI details, error "
                f"handling, loading states, styling, and anything "
                f"else the contract doesn't explicitly govern. Build "
                f"it like a normal engineer building this feature — "
                f"the contract just keeps you from colliding with "
                f"other agents on the shared surface.\n"
            )
        if contract_file:
            contract_notice += (
                f"\nContract file: {contract_file}\n\n"
                f"BEFORE writing any code, Read() the contract file at "
                f"{contract_file}. The contract defines the interface "
                f"boundary your implementation must satisfy. Other agents "
                f"are implementing other sides of this same contract in "
                f"parallel — if you diverge from its data shapes or "
                f"method signatures, the integration fails.\n\n"
                f"Conform to the contract at the boundary. Everything "
                f"else is yours to design. If the contract is missing "
                f"something you need, report a blocker — do NOT silently "
                f"modify the contract file."
            )
        else:
            contract_notice += (
                "\nRead the shared contract artifacts in docs/ before "
                "writing code. Conform to them at the boundary; design "
                "everything else yourself."
            )

        # Stay-in-scope boundary instruction. Without it, contract-first
        # impl agents routinely reach into shared infrastructure files
        # (entry points, manifests, build configs) to make their module
        # "callable" or "integrated" — and collide on those files when
        # another impl agent does the same thing. Observed across
        # snake-baton-1, snake-scaffold-2, snake-decomposer-1, and
        # snake-overfrag-1: every run produced BLOCKED tasks from
        # parallel writes to the project entry-point file.
        #
        # Stack-agnostic by design — no file names, no package
        # manifests, no language assumptions. The contract names the
        # scope; the agent stays in it. Integration into the broader
        # system is a separate downstream task (the Compose step on
        # the contract-first path, or whatever the DAG provides).
        #
        # IMPORTANT: skip this layer for composition tasks (Codex P1).
        # ``build_composition_task()`` in
        # ``src/integrations/composition_synthesis.py`` sets
        # ``source_type="composition_synthesis"`` and a non-empty
        # responsibility ("Wires the application entry point"), so
        # ``_parse_contract_metadata`` reports it as a
        # responsibility-bearing task too. The composition agent's
        # entire JOB is the wiring — telling it "integration is not
        # yours; stop after logging an artifact" would block the run.
        if not _is_composition_task(task):
            contract_notice += (
                "\n\n🎯 STAY IN YOUR CONTRACT'S SCOPE:\n"
                "Your contract IS the coordination surface. Implement what "
                "your contract specifies; do NOT modify code outside your "
                "contract's scope to make your module 'callable' or "
                "'integrated' with other parts of the system.\n\n"
                "Integration is a separate, downstream concern. If your "
                "work cannot be invoked by other code without modifying "
                "their files, that is an integration concern handled by a "
                "later task — not yours. Log a decision or artifact "
                "describing the integration point you would have wired, "
                "then stop. The downstream integration agent will pick it "
                "up from your artifact.\n\n"
                "Reaching outside your contract's scope to wire your "
                "module into shared infrastructure is the #1 cause of "
                "merge conflicts in contract-first runs — the next "
                "agent's branch touched the same file you did. Stay in "
                "your lane and let the integration step do its job."
            )

        # GH-356: surface scope_annotation semantics so agents know
        # how to interpret the field on each dependency artifact.
        contract_notice += (
            "\n\nEach dependency artifact in your context carries a "
            "``scope_annotation`` field:\n"
            "- ``in_scope``      — you OWN this interface, implement it "
            "completely\n"
            "- ``reference_only`` — coordination boundary only; do NOT "
            "implement this interface"
        )
        # Keep-alive reminder (experiment 66 fix): contract-first
        # agents go heads-down implementing and skip the logging /
        # progress guidance from the earlier workflow layer. The
        # lease system treats silence as abandonment and reassigns
        # the task. Terse reminder adjacent to the contract
        # instructions so it survives the agent's attention budget.
        contract_notice += (
            "\n\n⏱️  KEEP THE LEASE ALIVE:\n"
            "Silence >3 min = task reclaimed and reassigned.\n"
            "- report_task_progress at 25/50/75%\n"
            "- log_decision for each architectural choice "
            "(as you make it, not after)\n"
            "- log_artifact for each intermediate output "
            "other agents might consume"
        )
        instructions_parts.append(contract_notice)

    # Layer 1.35: Scaffold-path anchor (#659). When Marcus's pre-fork
    # scaffold generated a placeholder file for this task, surface the
    # exact path so the agent fills the scaffold instead of inventing a
    # sibling path (the ``src/core/gameEngine.js`` orphan failure
    # observed in ``snake-baton-1``). This is a STANDALONE layer, NOT
    # gated on ``responsibility`` — scaffolds are generated for both the
    # contract-first AND feature-based decomposition paths
    # (``_run_design_phase`` runs whenever the project has design tasks
    # and a project_root), but the anchor previously lived inside the
    # contract-first ``if responsibility:`` block, so feature-based
    # agents got the path persisted on their task yet never saw it —
    # exactly the invent-your-own-path failure #659 is about, just on
    # the other decomposer. ``_resolve_scaffold_path`` checks
    # ``source_context`` first (SQLite has a native column) then the
    # ``MARCUS_SCAFFOLD_PATH`` description marker that round-trips
    # through Planka / GitHub / Linear. Naming the path is coordination
    # (it is the public import address downstream agents use), not
    # control — the agent still owns HOW the file is filled.
    scaffold_path_raw = _resolve_scaffold_path(task)
    if scaffold_path_raw:
        instructions_parts.append(
            f"\n\n📂 IMPLEMENTATION FILE: {scaffold_path_raw}\n"
            f"Marcus pre-created a placeholder at this path. Fill it with "
            f"your implementation — do NOT create a sibling file "
            f"elsewhere. Other agents will import from this exact path. "
            f"If the file is missing when you start, the scaffold step may "
            f"have failed; create the file at this path."
        )

    # Layer 1.4: Feature-based design artifact framing
    # Mirrors Layer 1.3 for the opposite decomposer mode.  When a
    # feature_based task depends on a design task (has "design" label
    # but NOT "auto_completed"), agents need up-front guidance that
    # those design artifacts are coordination references — not their
    # implementation spec.  Without this, agents read the interface
    # contract and build a stub that satisfies the interface but
    # ignores the user-visible feature requirement (v76 failure mode).
    #
    # Skipped when:
    # - ``responsibility`` is set (Layer 1.3 already covers contract_first)
    # - ``state`` is None (backward compat for callers without state)
    # - task has no dependencies (nothing to look up)
    if not responsibility and state is not None and task.dependencies:
        dep_tasks = getattr(state, "project_tasks", []) or []
        has_feature_based_design_dep = any(
            dep.id in task.dependencies
            and "design" in (getattr(dep, "labels", []) or [])
            and "auto_completed" not in (getattr(dep, "labels", []) or [])
            for dep in dep_tasks
        )
        if has_feature_based_design_dep:
            instructions_parts.append(
                "\n\n📐 DESIGN ARTIFACTS IN YOUR DEPENDENCIES:\n"
                "Your dependency tasks produced design artifacts (interface\n"
                "contracts, data models, API shapes). Read them before\n"
                "writing code to understand the boundary you must expose.\n\n"
                "Your job is to build the COMPLETE, WORKING FEATURE — not\n"
                "just implement the interface. Contracts are coordination\n"
                "constraints on what your code must expose at integration\n"
                "points. Everything else — UI details, error handling,\n"
                "internal structure, helper methods — is yours to design.\n"
                "Build it like a normal engineer building this feature.\n\n"
                "Each dependency artifact carries a ``scope_annotation`` field:\n"
                "- ``reference_only`` — coordination boundary; read it, do not "
                "reimplement it"
            )

    # Layer 1.5: Subtask Context (if this is a subtask)
    if hasattr(task, "_is_subtask") and task._is_subtask:
        parent_name = getattr(task, "_parent_task_name", "parent task")

        # Calculate realistic time budget for subtask
        # estimated_hours is already in reality-based format (minutes/60)
        # Guard against None to prevent TypeError
        estimated_minutes = (task.estimated_hours or 0) * 60

        # Get complexity level (default to standard if not set)
        complexity = getattr(task, "_complexity", "standard")

        # Complexity-specific guidance (generic for all task types)
        COMPLEXITY_GUIDANCE = {
            "prototype": {
                "scope": "Minimal - core functionality only",
                "quality": "Works for the happy path",
                "effort": "Quick implementation",
            },
            "standard": {
                "scope": "Complete - production-ready",
                "quality": "Handles errors, maintainable",
                "effort": "Thorough implementation",
            },
            "enterprise": {
                "scope": "Comprehensive - all edge cases",
                "quality": "Production-grade, extensively validated",
                "effort": "Complete with reviews",
            },
        }

        guidance = COMPLEXITY_GUIDANCE.get(complexity, COMPLEXITY_GUIDANCE["standard"])

        instructions_parts.append(
            f"\n\n⏱️ TIME BUDGET: {estimated_minutes:.0f} MINUTES\n"
            f"Complexity Mode: {complexity.upper()}\n\n"
            f"This is a SUBTASK - complete it in ~{estimated_minutes:.0f} minutes.\n\n"
            f"{complexity.upper()} MODE EXPECTATIONS:\n"
            f"- Scope: {guidance['scope']}\n"
            f"- Quality: {guidance['quality']}\n"
            f"- Effort: {guidance['effort']}\n\n"
            f"📋 SUBTASK CONTEXT:\n"
            f"This is a SUBTASK of the larger task: '{parent_name}'\n\n"
            f"FOCUS ONLY on completing this specific subtask:\n"
            f"  Task: {task.name}\n"
            f"  Description: {task.description}\n\n"
            f"Do NOT work on the full parent task - only complete this "
            f"specific subtask. Other agents will handle the remaining subtasks."
        )

    # Layer 2: Implementation Context
    if context_data and context_data.get("previous_implementations"):
        impl_count = len(context_data["previous_implementations"])
        instructions_parts.append(
            f"\n\n📚 IMPLEMENTATION CONTEXT:\n{impl_count} relevant "
            "implementations found. Use these patterns and interfaces to "
            "maintain consistency."
        )

    # Layer 3: Dependency Awareness
    if dependency_awareness:
        instructions_parts.append(
            f"\n\n🔗 DEPENDENCY AWARENESS:\n{dependency_awareness}\n\n"
            "Consider these future needs when making implementation decisions. "
            "Your choices will directly impact these dependent tasks."
        )

    # Layer 4: Decision Logging Prompt
    if context_data and len(context_data.get("dependent_tasks", [])) > 2:
        # High-impact task with many dependents
        instructions_parts.append(
            "\n\n📝 ARCHITECTURAL DECISIONS:\n"
            "This task has significant downstream impact. When making "
            "technical choices that affect other tasks:\n"
            "Use: 'Marcus, log decision: I chose [WHAT] because [WHY]. "
            "This affects [IMPACT].'\n"
            "Examples:\n"
            "- 'I chose JWT tokens because mobile apps need stateless "
            "auth. This affects all API endpoints.'\n"
            "- 'I chose PostgreSQL because we need ACID compliance. "
            "This affects all data models.'"
        )

    # Layer 5: Predictions and Warnings
    if predictions:
        risk_parts = []

        # Success probability warning
        if predictions.get("success_probability", 1.0) < 0.6:
            risk_parts.append(
                f"⚠️ Success probability: "
                f"{predictions['success_probability']:.0%} - Extra care needed"
            )

        # Enhanced completion time prediction
        if predictions.get("completion_time"):
            ct = predictions["completion_time"]
            risk_parts.append(
                f"⏱️ Expected duration: {ct['expected_hours']:.1f} hours "
                + f"({ct['confidence_interval']['lower']:.1f}-"
                + f"{ct['confidence_interval']['upper']:.1f} hours)"
            )
            if ct.get("factors"):
                risk_parts.append("   Time factors: " + "; ".join(ct["factors"][:2]))

        # Detailed blockage analysis
        if predictions.get("blockage_analysis"):
            ba = predictions["blockage_analysis"]
            if ba["overall_risk"] > 0.5:
                risk_parts.append(f"⚠️ High blockage risk: {ba['overall_risk']:.0%}")
                # Show top blockers
                if ba.get("risk_breakdown"):
                    top_risks = sorted(
                        ba["risk_breakdown"].items(), key=lambda x: x[1], reverse=True
                    )[:2]
                    for risk_type, probability in top_risks:
                        risk_parts.append(f"   • {risk_type}: {probability:.0%} chance")
                # Add preventive measures
                if ba.get("preventive_measures"):
                    risk_parts.append("💡 Prevention tips:")
                    for measure in ba["preventive_measures"][:2]:
                        risk_parts.append(f"   • {measure}")

        # Cascade effects warning
        if predictions.get("cascade_effects") and predictions["cascade_effects"].get(
            "critical_path_impact"
        ):
            ce = predictions["cascade_effects"]
            risk_parts.append(
                f"🌊 CASCADE WARNING: Delays will impact "
                f"{len(ce['affected_tasks'])} dependent tasks"
            )
            if ce.get("mitigation_options"):
                risk_parts.append(f"   Mitigation: {ce['mitigation_options'][0]}")

        # Performance trajectory insights
        if predictions.get("performance_trajectory"):
            pt = predictions["performance_trajectory"]
            if pt.get("improving_skills"):
                skill_names = list(pt["improving_skills"].keys())[:1]
                if skill_names:
                    risk_parts.append(
                        f"📈 You're improving in {skill_names[0]} - "
                        "great opportunity to excel!"
                    )
            if pt.get("recommendations"):
                risk_parts.append(f"💡 {pt['recommendations'][0]}")

        if risk_parts:
            instructions_parts.append(
                "\n\n⚡ PREDICTIONS & INSIGHTS:\n" + "\n".join(risk_parts)
            )

    # Layer 6: Task-specific guidance based on labels and task name.
    # Guards use ``or []`` / ``or ""`` so the block runs safely even when
    # task.labels is None or task.name is None.
    _labels = [lbl.lower() for lbl in (task.labels or [])]
    _name = (task.name or "").lower()
    guidance_parts = []

    # API tasks
    if any(lbl in ["api", "endpoint", "rest"] for lbl in _labels):
        guidance_parts.append(
            "🌐 API Guidelines: Follow RESTful conventions, "
            "include proper error handling, document response formats"
        )

    # Frontend tasks
    if any(lbl in ["frontend", "ui", "react", "vue"] for lbl in _labels):
        guidance_parts.append(
            "🎨 Frontend Guidelines: Ensure responsive design, "
            "follow component patterns, handle loading/error states"
        )

    # Database tasks
    if any(lbl in ["database", "migration", "schema"] for lbl in _labels):
        guidance_parts.append(
            "🗄️ Database Guidelines: Include rollback migrations, "
            "test with sample data, document schema changes"
        )

    # Security tasks
    if any(lbl in ["security", "auth", "authentication"] for lbl in _labels):
        guidance_parts.append(
            "🔒 Security Guidelines: Follow OWASP best practices, "
            "implement proper validation, use secure defaults"
        )

    # Documentation tasks — fire on label OR task name.
    # dashboard-v82 post-mortem: Agent 1 documented WeatherWidget props
    # from the design spec instead of the actual implementation, causing
    # README/code drift. Instruction-level reminder to read source first.
    _doc_labels = {"documentation", "docs", "readme"}
    _doc_name_keywords = ("readme", "document", "docs")
    if any(lbl in _doc_labels for lbl in _labels) or any(
        kw in _name for kw in _doc_name_keywords
    ):
        guidance_parts.append(
            "📖 Documentation Guidelines: For every component, function, "
            "or API you document — read the actual source file first. "
            "Verify every prop name, parameter name, type, and default "
            "value against the implementation, not the design spec. "
            "Document the API that exists in the code, not the intended "
            "one. Design specs and implementations diverge; code is truth."
        )

    if guidance_parts:
        instructions_parts.append(
            "\n\n💡 TASK-SPECIFIC GUIDANCE:\n" + "\n".join(guidance_parts)
        )

    return "\n".join(instructions_parts)


async def calculate_retry_after_seconds(state: Any) -> Dict[str, Any]:
    """
    Calculate intelligent wait time before next task request.

    Strategy:
    - Prioritizes tasks that unlock parallel work for idle agents
    - Uses 60% of ETA for early completion detection
    - Caps at 5 minutes for regular re-polling

    Uses:
    - Current progress of IN_PROGRESS tasks
    - Historical median task duration
    - Task dependencies and parallel work potential
    - Idle agent capacity

    Parameters
    ----------
    state : Any
        Marcus server state instance

    Returns
    -------
    Dict[str, Any]
        Dictionary with:
        - retry_after_seconds: int (wait time in seconds, max 300s)
        - reason: str (explanation for the wait time)
        - blocking_task: Optional[Dict] (task that's blocking progress)
    """
    # Get all IN_PROGRESS tasks with their assignments
    in_progress_tasks = []
    for agent_id, assignment in state.agent_tasks.items():
        task = next(
            (t for t in state.project_tasks if t.id == assignment.task_id), None
        )
        if task and task.status == TaskStatus.IN_PROGRESS:
            in_progress_tasks.append({"task": task, "assignment": assignment})

    # If no tasks in progress, use default wait time
    if not in_progress_tasks:
        return {
            "retry_after_seconds": 30,
            "reason": "No tasks currently in progress - check back soon",
            "blocking_task": None,
        }

    # Get historical median duration
    global_median_hours = 1.0  # Default fallback
    if hasattr(state, "memory") and state.memory:
        global_median_hours = await state.memory.get_global_median_duration()

    # Calculate ETA for each in-progress task
    completion_estimates = []
    now = datetime.now(timezone.utc)

    for item in in_progress_tasks:
        task = item["task"]
        assignment = item["assignment"]

        # Get current progress (0-100)
        progress = getattr(task, "progress", 0) or 0

        # Calculate elapsed time in seconds
        # Ensure assignment.assigned_at is timezone-aware
        assigned_at = assignment.assigned_at
        if assigned_at.tzinfo is None:
            # Make naive datetime timezone-aware (assume UTC)
            assigned_at = assigned_at.replace(tzinfo=timezone.utc)
        elapsed_seconds = (now - assigned_at).total_seconds()

        # Estimate remaining time
        if progress > 0 and progress < 100:
            # Use actual progress to estimate
            estimated_total_seconds = (elapsed_seconds / progress) * 100
            remaining_seconds = estimated_total_seconds - elapsed_seconds
        else:
            # Fall back to historical median
            remaining_seconds = global_median_hours * 3600  # Convert hours to seconds

        # Ensure non-negative
        remaining_seconds = max(0, remaining_seconds)

        # Check how many tasks this will unblock
        dependent_task_ids = [
            t.id for t in state.project_tasks if task.id in (t.dependencies or [])
        ]

        completion_estimates.append(
            {
                "task_id": task.id,
                "task_name": task.name,
                "progress": progress,
                "eta_seconds": remaining_seconds,
                "unlocks_count": len(dependent_task_ids),
            }
        )

    # Count idle agents (registered but not currently working)
    total_agents = len(state.agent_status)
    busy_agents = len([a for a in state.agent_tasks.values() if a.task_id])
    idle_agents = max(0, total_agents - busy_agents)

    # Prioritize tasks that unlock parallel work for idle agents
    # This prevents waking up for sequential work that current workers can handle
    high_value_tasks = [
        est for est in completion_estimates if est["unlocks_count"] >= idle_agents
    ]

    # If we have tasks that unlock enough parallel work, prioritize those
    # Otherwise fall back to any task completion (sequential work is still work)
    candidate_tasks = high_value_tasks if high_value_tasks else completion_estimates

    # Sort candidates by ETA (soonest first)
    candidate_tasks.sort(key=lambda x: x["eta_seconds"])

    # Get the best task to wait for
    target_task = candidate_tasks[0]

    # Use 60% of ETA for retry to catch early completions
    # This accounts for tasks finishing faster than estimated
    retry_after = int(target_task["eta_seconds"] * 0.6)

    # Minimum 30 seconds to avoid excessive polling
    retry_after = max(30, retry_after)

    # Cap at 30 seconds for re-polling (catch unexpected early completions)
    retry_after = min(retry_after, 30)

    # Format reason
    eta_minutes = int(target_task["eta_seconds"] / 60)
    unlocks_info = (
        f" (unlocks {target_task['unlocks_count']} tasks)"
        if target_task["unlocks_count"] > 0
        else ""
    )
    reason = (
        f"Waiting for '{target_task['task_name']}' to complete "
        f"(~{eta_minutes} min, {target_task['progress']}% done){unlocks_info}"
    )

    return {
        "retry_after_seconds": retry_after,
        "reason": reason,
        "blocking_task": {
            "id": target_task["task_id"],
            "name": target_task["task_name"],
            "progress": target_task["progress"],
            "eta_seconds": int(target_task["eta_seconds"]),
        },
    }


async def request_next_task(agent_id: str, state: Any) -> Any:
    """
    Agents call this to request their next optimal task.

    Uses AI-powered task matching to find the best task based on:
    - Agent skills and experience
    - Task priority and dependencies
    - Current workload distribution

    Parameters
    ----------
    agent_id : str
        The requesting agent's ID
    state : Any
        Marcus server state instance

    Returns
    -------
    Any
        Dict with task details and instructions if successful
    """
    # Ensure lease monitor is running on the active event loop
    # (In HTTP mode, setup runs on a temporary loop that's abandoned)
    if hasattr(state, "ensure_lease_monitor_running"):
        await state.ensure_lease_monitor_running()

    # Get project/board context
    project_context = await get_project_board_context(state)

    # Log task request
    conversation_logger.log_worker_message(
        agent_id,
        "to_pm",
        "Requesting next task",
        {"worker_info": f"Worker {agent_id} requesting task", **project_context},
    )

    try:
        # Phase timing for performance monitoring (GH-228)
        _perf_start = time.perf_counter()
        _perf_marks: Dict[str, float] = {}

        def _mark(name: str) -> None:
            _perf_marks[name] = (time.perf_counter() - _perf_start) * 1000

        # Log the task request immediately
        state.log_event(
            "task_request",
            {"worker_id": agent_id, "source": agent_id, "target": "marcus"},
        )

        # Log conversation event for visualization
        log_agent_event("task_request", {"worker_id": agent_id})

        # Initialize kanban if needed
        await state.initialize_kanban()

        # Log Marcus thinking about refreshing state
        log_thinking("marcus", "Need to check current project state")

        # Get current project state
        await state.refresh_project_state()
        _mark("state_refresh")

        # Log thinking about finding task
        agent = state.agent_status.get(agent_id)
        if agent:
            log_thinking(
                "marcus",
                f"Finding optimal task for {agent.name}",
                {
                    "agent_skills": agent.skills,
                    "current_workload": len(agent.current_tasks),
                },
            )

            # CRITICAL: Enforce one-task-per-agent rule
            if agent.current_tasks:
                logger.warning(
                    f"Agent {agent_id} ({agent.name}) already has "
                    f"{len(agent.current_tasks)} task(s): "
                    f"{[t.name for t in agent.current_tasks]}. "
                    "Rejecting new task request."
                )
                conversation_logger.log_worker_message(
                    agent_id,
                    "from_pm",
                    "Task request denied - complete current task first",
                    {
                        "current_tasks": [t.id for t in agent.current_tasks],
                        "reason": "one_task_per_agent_rule",
                        **project_context,
                    },
                )
                return {
                    "success": False,
                    "error": (
                        "You already have a task assigned. Please complete "
                        "or report blocker on current task before "
                        "requesting another."
                    ),
                    "current_task": {
                        "id": agent.current_tasks[0].id,
                        "name": agent.current_tasks[0].name,
                        "status": agent.current_tasks[0].status.value,
                    },
                }

        # Find optimal task for this agent
        optimal_task = await find_optimal_task_for_agent(agent_id, state)
        _mark("task_selection")

        # Bug #651 follow-up — before declaring "no task ready" and
        # letting the runner spawn another ephemeral agent that will
        # rediscover the same state, sweep any BLOCKED-by-merge-conflict
        # tasks and try to recover them via rebase.  If recovery
        # unblocks a downstream task, the CURRENT agent can claim it
        # instead of forcing a fresh spawn-poll cycle.
        #
        # Eliminates verify-snake-8's spawn-thrash failure mode where
        # 49 ephemeral agents spawned chasing an unrecoverable BLOCKED
        # task before the 20-min stall watchdog fired (~$25-50 wasted
        # spawn cost from one recoverable conflict).
        if not optimal_task:
            recovered_count = await _sweep_blocked_merge_conflicts(state)
            if recovered_count > 0:
                logger.info(
                    "[recovery] sweep recovered %d task(s); refreshing state "
                    "and re-selecting for agent %s",
                    recovered_count,
                    agent_id,
                )
                await state.refresh_project_state()
                optimal_task = await find_optimal_task_for_agent(agent_id, state)
                _mark("task_selection_after_recovery")

        if optimal_task:
            try:
                # Get implementation context if using GitHub
                previous_implementations = None
                if state.provider == "github" and state.code_analyzer:
                    owner = os.getenv("GITHUB_OWNER")
                    repo = os.getenv("GITHUB_REPO")
                    impl_details = await state.code_analyzer.get_implementation_details(
                        optimal_task.dependencies, owner, repo
                    )
                    if impl_details:
                        previous_implementations = impl_details

                # Get enhanced context if Context system is available
                context_data = None
                dependency_awareness = None

                # Issue #605: context is delivered IN this response, so it
                # must never be skipped. The old ">5 TODO tasks" guard
                # dropped context exactly when a large backlog made
                # coordination matter most — it has been removed.
                build_context = hasattr(state, "context") and state.context

                if build_context:
                    # Add any GitHub implementations to context first
                    if previous_implementations:
                        await state.context.add_implementation(
                            optimal_task.id, previous_implementations
                        )

                    # Analyze dependencies for this project
                    if state.project_tasks:
                        dep_map = await state.context.analyze_dependencies(
                            state.project_tasks
                        )
                        if optimal_task.id in dep_map:
                            # Add dependent tasks to context
                            for dep_task_id in dep_map[optimal_task.id]:
                                dep_task = next(
                                    (
                                        t
                                        for t in state.project_tasks
                                        if t.id == dep_task_id
                                    ),
                                    None,
                                )
                                if dep_task:
                                    from src.core.context import DependentTask

                                    # Infer what the dependent task needs
                                    expected_interface = (
                                        state.context.infer_needed_interface(
                                            dep_task, optimal_task.id
                                        )
                                    )

                                    state.context.add_dependency(
                                        optimal_task.id,
                                        DependentTask(
                                            task_id=dep_task.id,
                                            task_name=dep_task.name,
                                            expected_interface=expected_interface,
                                        ),
                                    )

                    # Now get full context including the dependent tasks we just added
                    task_context = await state.context.get_context(
                        optimal_task.id, optimal_task.dependencies or []
                    )

                    # Format context for response
                    context_data = task_context.to_dict()

                    # Create dependency awareness message
                    if task_context.dependent_tasks:
                        dep_count = len(task_context.dependent_tasks)
                        dep_list = "\n".join(
                            [
                                f"- {dt['task_name']} "
                                f"(needs: {dt['expected_interface']})"
                                for dt in task_context.dependent_tasks[:3]
                            ]
                        )
                        dependency_awareness = (
                            f"{dep_count} future tasks depend on your work:\n{dep_list}"
                        )

                _mark("context_building")

                # Get predictions if Memory system is available
                predictions = None
                if hasattr(state, "memory") and state.memory:
                    # Get basic task outcome prediction
                    basic_prediction = await state.memory.predict_task_outcome(
                        agent_id, optimal_task
                    )

                    # Get enhanced predictions
                    completion_time = await state.memory.predict_completion_time(
                        agent_id, optimal_task
                    )
                    blockage_analysis = await state.memory.predict_blockage_probability(
                        agent_id, optimal_task
                    )

                    # Check for cascade effects if task has dependents
                    cascade_effects = None
                    if context_data and context_data.get("dependent_tasks"):
                        # Estimate potential delay based on complexity
                        potential_delay = (
                            completion_time.get("expected_hours", 0) * 0.2
                        )  # 20% buffer
                        cascade_effects = await state.memory.predict_cascade_effects(
                            optimal_task.id, potential_delay
                        )

                    # Get agent performance trajectory
                    performance_trajectory = (
                        await state.memory.calculate_agent_performance_trajectory(
                            agent_id
                        )
                    )

                    # Combine all predictions
                    predictions = {
                        **basic_prediction,
                        "completion_time": completion_time,
                        "blockage_analysis": blockage_analysis,
                        "cascade_effects": cascade_effects,
                        "performance_trajectory": performance_trajectory,
                    }

                    # Record task start in memory
                    await state.memory.record_task_start(agent_id, optimal_task)

                _mark("memory_predictions")

                # Generate detailed instructions with AI
                try:
                    base_instructions = (
                        await state.ai_engine.generate_task_instructions(
                            optimal_task, state.agent_status.get(agent_id)
                        )
                    )

                    # Build tiered instructions based on context
                    instructions = build_tiered_instructions(
                        base_instructions,
                        optimal_task,
                        context_data,
                        dependency_awareness,
                        predictions,
                        state=state,
                    )
                except KeyError as e:
                    # Log the specific KeyError for debugging
                    logger.error(f"KeyError in generate_task_instructions: {e}")
                    logger.error(f"Task: {optimal_task.name}, ID: {optimal_task.id}")
                    logger.error(
                        "Task labels: %s", getattr(optimal_task, "labels", "No labels")
                    )
                    raise
                except Exception as e:
                    logger.error(f"Error generating task instructions: {e}")
                    raise

                _mark("instruction_generation")

                # Log decision process
                conversation_logger.log_pm_decision(
                    decision=f"Assign task '{optimal_task.name}' to {agent_id}",
                    rationale="Best skill match and highest priority",
                    alternatives_considered=[
                        {"task": "Other Task 1", "score": 0.7},
                        {"task": "Other Task 2", "score": 0.6},
                    ],
                    confidence_score=0.85,
                    decision_factors={
                        "skill_match": 0.9,
                        "priority": optimal_task.priority.value,
                        "dependencies_clear": len(optimal_task.dependencies) == 0,
                    },
                )

                # Create assignment
                assignment = TaskAssignment(
                    task_id=optimal_task.id,
                    task_name=optimal_task.name,
                    description=optimal_task.description,
                    instructions=instructions,
                    estimated_hours=optimal_task.estimated_hours,
                    priority=optimal_task.priority,
                    dependencies=optimal_task.dependencies,
                    assigned_to=agent_id,
                    assigned_at=datetime.now(timezone.utc),
                    due_date=optimal_task.due_date,
                )

                # #206 MVP: atomically claim file locks JUST BEFORE
                # the kanban write commits the assignment. The earlier
                # filter in ``_find_optimal_task_original_logic``
                # prevents most contention; this acquire catches the
                # race where two agents passed the filter on the same
                # poll and both reached the commit. On failure we bail
                # before kanban write — the task goes back to TODO and
                # the agent receives the standard no-task response
                # shape so its existing 30s polling re-asks shortly.
                # On success the locks are released in
                # ``report_task_progress`` when the task reaches DONE
                # or BLOCKED (Phase 4).
                declared_files: List[str] = []
                _acquire_project_id: str = (
                    getattr(state, "agent_project_map", {}).get(agent_id, "")
                    if hasattr(state, "agent_project_map")
                    else ""
                )
                if hasattr(state, "file_lock_registry"):
                    declared_files = list(
                        (optimal_task.source_context or {}).get("declared_files", [])
                        or []
                    )
                    if declared_files:
                        acquire_result = await state.file_lock_registry.try_acquire(
                            task_id=optimal_task.id,
                            agent_id=agent_id,
                            files=declared_files,
                            project_id=_acquire_project_id,
                        )
                        if not acquire_result.success:
                            blocker = acquire_result.blocker
                            logger.info(
                                "[#206] Acquire race lost for task %s "
                                "(agent %s) — file %s (project %s) just "
                                "claimed by task %s (agent %s); returning "
                                "no_task_ready",
                                optimal_task.id,
                                agent_id,
                                acquire_result.blocker_file,
                                _acquire_project_id or "<default>",
                                blocker.task_id if blocker else "?",
                                blocker.agent_id if blocker else "?",
                            )
                            state.tasks_being_assigned.discard(optimal_task.id)
                            # Match the standard no-task response shape
                            # so the agent's existing polling loop
                            # interprets this the same as any other
                            # transient unavailability (Kaia review).
                            return {
                                "success": False,
                                "message": (
                                    "Task was claimed by another agent — "
                                    "retry shortly."
                                ),
                                "retry_after_seconds": 30,
                                "retry_reason": "file_lock_race",
                            }

                # Update kanban FIRST (fail fast if kanban is down)
                try:
                    await state.kanban_client.update_task(
                        optimal_task.id,
                        {
                            "status": TaskStatus.IN_PROGRESS,
                            "assigned_to": agent_id,
                        },
                    )
                except Exception:
                    # Kanban write failed AFTER we acquired file locks.
                    # Release them so the task doesn't stay locked by a
                    # phantom assignment that never actually committed.
                    if declared_files and hasattr(state, "file_lock_registry"):
                        await state.file_lock_registry.release(optimal_task.id)
                    raise

                _mark("kanban_update")

                # Sync subtask manager's assigned_to so Cato/analytics
                # see attribution immediately on pickup, not only at
                # completion. Without this, Subtask.assigned_to stays
                # None until report_task_progress(IN_PROGRESS) is called.
                if (
                    hasattr(state, "subtask_manager")
                    and state.subtask_manager
                    and optimal_task.id in state.subtask_manager.subtasks
                ):
                    state.subtask_manager.update_subtask_status(
                        optimal_task.id,
                        TaskStatus.IN_PROGRESS,
                        state.project_tasks,
                        assigned_to=agent_id,
                    )

                # If kanban update succeeded, track assignment
                state.agent_tasks[agent_id] = assignment
                agent = state.agent_status[agent_id]
                agent.current_tasks = [optimal_task]

                # Persist assignment
                await state.assignment_persistence.save_assignment(
                    agent_id,
                    optimal_task.id,
                    {
                        "name": optimal_task.name,
                        "priority": optimal_task.priority.value,
                        "estimated_hours": optimal_task.estimated_hours,
                    },
                )

                # Create lease for this assignment if lease manager available
                if hasattr(state, "lease_manager") and state.lease_manager:
                    lease = await state.lease_manager.create_lease(
                        optimal_task.id, agent_id, optimal_task
                    )
                    logger.info(
                        f"Created lease for task {optimal_task.id} "
                        f"(expires: {lease.lease_expires.isoformat()})"
                    )

                # Remove from pending assignments
                state.tasks_being_assigned.discard(optimal_task.id)

                # Track in server for cleanup on disconnect
                if hasattr(state, "_active_operations"):
                    state._active_operations.discard(
                        f"task_assignment_{optimal_task.id}"
                    )

                # Log task assignment
                conversation_logger.log_worker_message(
                    agent_id,
                    "from_pm",
                    f"Assigned task: {optimal_task.name}",
                    {
                        "task_id": optimal_task.id,
                        "instructions": instructions,
                        "priority": optimal_task.priority.value,
                        **project_context,
                    },
                )

                # Log conversation event for visualization
                log_agent_event(
                    "task_assignment",
                    {
                        "worker_id": agent_id,
                        "task": {
                            "id": optimal_task.id,
                            "name": optimal_task.name,
                            "priority": optimal_task.priority.value,
                            "estimated_hours": optimal_task.estimated_hours,
                        },
                    },
                )

                # Add project context if available
                active_project = None
                if hasattr(state, "project_registry") and state.project_registry:
                    active_project = await state.project_registry.get_active_project()

                # Serialize the response properly
                response: Dict[str, Any] = {
                    "success": True,
                    "task": {
                        "id": optimal_task.id,
                        "name": optimal_task.name,
                        "description": optimal_task.description,
                        "instructions": instructions,
                        "priority": optimal_task.priority.value,
                        "implementation_context": previous_implementations,
                        "project_id": active_project.id if active_project else None,
                        "project_name": active_project.name if active_project else None,
                        "labels": (
                            optimal_task.labels
                            if hasattr(optimal_task, "labels")
                            else []
                        ),
                        "completion_criteria": (
                            optimal_task.completion_criteria
                            if hasattr(optimal_task, "completion_criteria")
                            else []
                        ),
                        # #664: deliver acceptance_criteria to the agent.
                        # This is the checkable contract the agent's work is
                        # verified against; previously omitted, stranding every
                        # criterion the setup-time pipeline enriched.
                        "acceptance_criteria": (
                            optimal_task.acceptance_criteria
                            if hasattr(optimal_task, "acceptance_criteria")
                            else []
                        ),
                    },
                }

                # Add enhanced context if available
                if dependency_awareness:
                    response["task"]["dependency_awareness"] = dependency_awareness
                if context_data:
                    response["task"]["full_context"] = context_data
                if predictions:
                    response["task"]["predictions"] = predictions

                # Issue #605: deliver task context IN this response. The
                # agent pulls a task; the three-tier context bundle —
                # project_contract (project-global), dependency_artifacts
                # (direct deps, in_scope), and transitive_context
                # (ancestor artifacts + all upstream decisions,
                # reference_only) — comes with it. This is always
                # attached, never skipped, so an agent that never calls
                # the optional get_task_context tool still has full
                # context to build against.
                try:
                    from src.marcus_mcp.tools.context import assemble_task_context

                    delivered_context = await assemble_task_context(
                        optimal_task.id, optimal_task, state
                    )
                    response["task"]["project_contract"] = delivered_context[
                        "project_contract"
                    ]
                    response["task"]["dependency_artifacts"] = delivered_context[
                        "dependency_artifacts"
                    ]
                    response["task"]["transitive_context"] = delivered_context[
                        "transitive_context"
                    ]
                except Exception as ctx_err:
                    # Context delivery must never break task assignment.
                    logger.warning(
                        "Failed to assemble delivered context for task "
                        f"{optimal_task.id}: {ctx_err}"
                    )

                # Log task assignment to conversation (CRITICAL for debugging)
                conversation_logger.log_worker_message(
                    agent_id,
                    "from_pm",
                    f"Task assigned: {optimal_task.name}",
                    {
                        "task_id": optimal_task.id,
                        "task_name": optimal_task.name,
                        "priority": optimal_task.priority.value,
                        "estimated_hours": optimal_task.estimated_hours,
                        **project_context,
                    },
                )

                # Log as structured event for analysis
                state.log_event(
                    "task_assignment",
                    {
                        "agent_id": agent_id,
                        "task_id": optimal_task.id,
                        "task_name": optimal_task.name,
                        "priority": optimal_task.priority.value,
                        "source": "marcus",
                        "target": agent_id,
                    },
                )

                # Emit event if Events system is available (non-blocking)
                if hasattr(state, "events") and state.events:
                    await state.events.publish_nowait(
                        "task_assigned",
                        "marcus",
                        {
                            "agent_id": agent_id,
                            "task_id": optimal_task.id,
                            "task_name": optimal_task.name,
                            "has_context": context_data is not None,
                            "has_dependencies": dependency_awareness is not None,
                        },
                    )

                # Log phase timings (GH-228)
                _mark("total")
                task_count = len(state.project_tasks) if state.project_tasks else 0
                # total_ms is the cumulative wall-clock time
                total_ms = round(_perf_marks["total"], 2)
                # Convert cumulative marks to per-phase deltas
                _phases = list(_perf_marks.items())
                _phase_durations: Dict[str, float] = {}
                for i, (name, cumulative_ms) in enumerate(_phases):
                    prev_ms = _phases[i - 1][1] if i > 0 else 0.0
                    _phase_durations[name] = round(cumulative_ms - prev_ms, 2)
                logger.info(
                    "request_next_task timing: "
                    f"agent={agent_id} "
                    f"task={optimal_task.name!r} "
                    f"task_count={task_count} "
                    f"total_ms={total_ms} "
                    f"phases={_phase_durations}"
                )

                return serialize_for_mcp(response)

            except Exception as e:
                # If anything fails, rollback the reservation
                state.tasks_being_assigned.discard(optimal_task.id)

                conversation_logger.log_worker_message(
                    agent_id,
                    "from_pm",
                    f"Failed to assign task: {str(e)}",
                    {"error": str(e), **project_context},
                )

                return {"success": False, "error": f"Failed to assign task: {str(e)}"}

        else:
            # Record no-task response for gridlock detection
            if hasattr(state, "gridlock_detector") and state.gridlock_detector:
                state.gridlock_detector.record_no_task_response(agent_id)

                # Check for gridlock
                gridlock_result = state.gridlock_detector.check_for_gridlock(
                    state.project_tasks
                )

                if gridlock_result["is_gridlock"] and gridlock_result["should_alert"]:
                    # CRITICAL: Project is gridlocked!
                    logger.critical("🚨 PROJECT GRIDLOCK DETECTED!")
                    logger.critical(gridlock_result["diagnosis"])

                    # Log to conversation for visibility
                    conversation_logger.log_pm_thinking(
                        "🚨 PROJECT GRIDLOCK DETECTED",
                        {
                            "severity": "critical",
                            "metrics": gridlock_result["metrics"],
                            "diagnosis": gridlock_result["diagnosis"],
                        },
                    )

            # Check if the experiment has ended — return terminal signal so
            # agents exit cleanly instead of retrying indefinitely.
            #
            # Guard: match the monitor's project_id to the agent's registered
            # project.  Without this, a stale monitor from a previous
            # experiment (auto-stopped, set_active_monitor not yet cleared)
            # would fire EXPERIMENT_COMPLETE for agents of a new experiment
            # that starts before the Marcus server is restarted.
            from src.experiments.live_experiment_monitor import get_active_monitor

            _monitor = get_active_monitor()
            _agent_project_id = (
                state.agent_project_map.get(agent_id, "")
                if hasattr(state, "agent_project_map")
                else ""
            )
            # Only fire if the agent's project is known AND matches the monitor.
            # An empty _agent_project_id means the agent hasn't registered yet;
            # treat that as "no match" so unregistered agents are never
            # incorrectly terminated by a stale/finished monitor.
            _project_id_matches = bool(_agent_project_id) and (
                _monitor is not None
                and hasattr(_monitor, "project_id")
                and _monitor.project_id == _agent_project_id
            )
            if (
                _monitor is not None
                and _monitor.was_started
                and not _monitor.is_running
                and _project_id_matches
            ):
                sep = "=" * 70
                return {
                    "success": False,
                    "status": "EXPERIMENT_COMPLETE",
                    "message": (
                        f"\n\n{sep}\n"
                        "EXPERIMENT COMPLETE\n"
                        f"{sep}\n\n"
                        "The experiment has ended. All project work is done.\n\n"
                        "REQUIRED ACTION:\n"
                        "1. Print a brief summary of your contributions\n"
                        "2. Exit — do NOT retry or request more tasks\n\n"
                        f"{sep}\n"
                    ),
                    "should_exit": True,
                }

            # Check if there are any TODO tasks remaining
            # Only run diagnostics for LOGGING if tasks exist but can't be assigned
            # DO NOT send diagnostics to agents - they interpret them as reasons to stop
            todo_tasks = [t for t in state.project_tasks if t.status == TaskStatus.TODO]

            if todo_tasks:
                # Tasks exist but can't be assigned - run diagnostics FOR LOGGING ONLY
                logger.warning(
                    f"No tasks assignable but {len(todo_tasks)} TODO tasks exist - "
                    "running diagnostics for logs"
                )

                from src.core.task_diagnostics import (
                    format_diagnostic_report,
                    run_automatic_diagnostics,
                )

                # Get completed task IDs for diagnostics
                completed_task_ids = {
                    t.id for t in state.project_tasks if t.status == TaskStatus.DONE
                }
                assigned_task_ids = {a.task_id for a in state.agent_tasks.values()}

                # Run diagnostics FOR LOGGING ONLY - don't send to agents
                try:
                    diagnostic_report = await run_automatic_diagnostics(
                        project_tasks=state.project_tasks,
                        completed_task_ids=completed_task_ids,
                        assigned_task_ids=assigned_task_ids,
                    )

                    # Format report for logging (operators can see this)
                    formatted_report = format_diagnostic_report(diagnostic_report)
                    logger.info(
                        f"Diagnostic Report (for operators):\n{formatted_report}"
                    )

                except Exception as diag_error:
                    logger.error(
                        f"Diagnostic system error: {diag_error}", exc_info=True
                    )
            else:
                # No TODO tasks remaining - all tasks are done or in progress
                logger.info("No TODO tasks remaining - project may be complete")

            # Calculate intelligent retry time
            retry_info = await calculate_retry_after_seconds(state)

            conversation_logger.log_worker_message(
                agent_id,
                "from_pm",
                "No suitable tasks available at this time",
                {
                    "reason": "no_matching_tasks",
                    "retry_after_seconds": retry_info["retry_after_seconds"],
                    **project_context,
                },
            )

            # Build explicit instructions to prevent agent termination
            retry_seconds = retry_info["retry_after_seconds"]
            instructions = (
                f"\n\n{'='*70}\n"
                "NO TASKS CURRENTLY AVAILABLE\n"
                f"{'='*70}\n\n"
                "⚠️  CRITICAL: You MUST remain active and operational.\n\n"
                "Instructions:\n"
                f"1. DO NOT terminate or stop execution\n"
                f"2. Sleep/wait for exactly {retry_seconds} seconds\n"
                f"3. After sleeping, immediately request a new task\n"
                f"4. Repeat this process until you receive a task OR are "
                "notified all work is complete\n\n"
                f"REQUIRED ACTION: Sleep {retry_seconds} seconds → "
                "Request task → Repeat\n\n"
                "⚠️  You must continue this retry loop. Stopping or terminating "
                "will make you unresponsive and unable to receive future tasks. "
                "Stay alive and keep retrying.\n"
                f"{'='*70}\n"
            )

            # Return ONLY the essential retry information
            # DO NOT include diagnostics or blocking_task info - agents interpret
            # these as reasons to stop working instead of retrying
            response = {
                "success": False,
                "message": instructions,
                "retry_after_seconds": retry_seconds,
                "retry_reason": retry_info["reason"],
            }

            return response

    except Exception as e:
        return {"success": False, "error": str(e)}


async def _validate_task_completion(task: Task, agent_id: str, state: Any) -> Any:
    """Validate task completion using WorkAnalyzer.

    Parameters
    ----------
    task : Task
        Task to validate
    agent_id : str
        Agent ID for logging
    state : Any
        Marcus server state

    Returns
    -------
    ValidationResult
        Validation result with pass/fail and issues
    """
    global _work_analyzer, _retry_tracker

    # Lazy imports to avoid circular dependency
    from src.ai.validation.retry_tracker import RetryTracker
    from src.ai.validation.work_analyzer import WorkAnalyzer

    # Initialize singletons with double-checked locking pattern
    if _work_analyzer is None or _retry_tracker is None:
        with _singleton_lock:
            if _work_analyzer is None:
                _work_analyzer = WorkAnalyzer()
            if _retry_tracker is None:
                _retry_tracker = RetryTracker()

    # Pass agent_id explicitly so worktree resolution uses the authoritative
    # caller ID rather than task.assigned_to, which names the recovering agent
    # after task recovery and would point to the wrong worktree.
    validation_result = await _work_analyzer.validate_implementation_task(
        task, state, agent_id=agent_id
    )

    return validation_result


async def _handle_validation_failure(
    task: Task, agent_id: str, validation_result: Any, state: Any
) -> Dict[str, Any]:
    """Handle validation failure with hybrid remediation.

    First failure: Return response (task stays IN_PROGRESS, agent can retry)
    Retry with same issues: Create blocker

    The retry ceiling is evaluated by the caller BEFORE this function
    is invoked — when the ceiling is hit, the caller routes the task
    through the normal completion path with an escalation annotation
    rather than calling this function. See Codex P1 on PR #337 for
    the rationale.

    Parameters
    ----------
    task : Task
        Task that failed validation
    agent_id : str
        Agent ID
    validation_result : ValidationResult
        Validation result with issues
    state : Any
        Marcus server state

    Returns
    -------
    Dict[str, Any]
        Response indicating validation failure
    """
    current_attempts = (
        _retry_tracker.get_attempt_count(task.id) if _retry_tracker is not None else 0
    )

    # Check if this is a retry with same issues BEFORE recording
    is_retry_with_same_issues = False
    if _retry_tracker is not None:
        is_retry_with_same_issues = _retry_tracker.is_retry_with_same_issues(
            task.id, validation_result
        )

    # Format issues for response
    issues_list = [issue.to_dict() for issue in validation_result.issues]

    if is_retry_with_same_issues:
        # Validator is repeating the same issues — escalate instead of
        # blocking.  Creating a BLOCKED task here produced 64 permanent
        # deadlocks (health report 2026-04-26): BLOCKED tasks are never
        # picked up by request_next_task (TODO-only) and never recovered
        # by lease expiry (skips terminal status).  Escalation is the
        # correct exit: let the task auto-pass and log the persistent
        # issues for human review rather than parking the agent forever.
        logger.warning(
            f"VALIDATION ESCALATION (same-issue repeat): task "
            f"{task.id} ({task.name}) — validator returned identical "
            f"issues on retry. Escalating to auto-pass. Issues: "
            f"{[i.issue[:80] for i in validation_result.issues]}"
        )
        if _retry_tracker is not None:
            _retry_tracker.record_attempt(task.id, validation_result)

        # Do NOT include "success" or "message" here — this dict
        # becomes escalation_payload and is merged into the final
        # completion response as **escalation_payload.  Including
        # success=False would override the explicit "success": True
        # in that response, making a finalized DONE task appear
        # failed to the caller (Codex P1, PR #421).
        return {
            "status": "validation_escalated",
            "escalated": True,
            "issues": issues_list,
            "attempt_count": current_attempts + 1,
        }
    else:
        # First failure or different issues - record attempt and return response
        if _retry_tracker is not None:
            _retry_tracker.record_attempt(task.id, validation_result)

        return {
            "success": False,
            "status": "validation_failed",
            "issues": issues_list,
            "attempt_count": current_attempts + 1,
            "message": "Task did not pass validation. Fix issues and retry completion.",
        }


def _format_blocker_description(validation_result: Any) -> str:
    """Format validation issues as blocker description.

    Parameters
    ----------
    validation_result : ValidationResult
        Validation result with issues

    Returns
    -------
    str
        Formatted blocker description
    """
    lines = [
        "🚫 VALIDATION BLOCKER - REPEATED FAILURES",
        "",
        "You've attempted to complete this task multiple times with the same issues.",
        "This suggests you may be stuck. Please carefully review the issues below:",
        "",
    ]

    for i, issue in enumerate(validation_result.issues, 1):
        lines.append(f"{i}. ❌ {issue.issue}")
        lines.append(f"   SEVERITY: {issue.severity.value.upper()}")
        lines.append(f"   EVIDENCE: {issue.evidence}")
        lines.append(f"   REMEDIATION: {issue.remediation}")
        lines.append(f"   CRITERION: {issue.criterion}")
        lines.append("")

    lines.append("IMPORTANT:")
    lines.append("- READ the remediation carefully - it tells you EXACTLY what to fix")
    lines.append("- Don't rebuild everything - fix the SPECIFIC issues listed above")
    lines.append("- If you're unsure, ask for help understanding the requirements")
    lines.append("")
    lines.append("Once you've fixed the issues, report progress to unblock the task.")

    return "\n".join(lines)


def _verify_agent_has_commits(
    agent_id: str,
    project_root: str,
) -> Optional[bool]:
    """
    Check whether the agent's worktree branch has commits ahead of main.

    Used to detect false task completions where an agent calls
    report_task_progress(status='completed') without having committed
    any implementation code (dashboard-v88 post-mortem: agent_unicorn_3
    reported two tasks done with zero commits; UD2 had to rescue both).

    Parameters
    ----------
    agent_id : str
        Agent identifier. Branch name is ``marcus/{agent_id}``.
    project_root : str
        Absolute path to the implementation git repository.

    Returns
    -------
    Optional[bool]
        True  — branch exists and has commits ahead of main.
        False — branch exists but has NO commits ahead of main.
        None  — branch does not exist (agent worked on main directly)
                or git is unavailable; caller should skip the check.
    """
    import subprocess as _sp
    from pathlib import Path

    repo = Path(project_root)
    branch = f"marcus/{agent_id}"

    try:
        # Verify it's a git repo
        git_check = _sp.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if git_check.returncode != 0:
            return None

        # Check if agent's worktree branch exists
        branch_check = _sp.run(
            ["git", "branch", "--list", branch],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if not branch_check.stdout.strip():
            return None  # no worktree branch — skip check

        # Count commits on branch not yet on main
        log_result = _sp.run(
            ["git", "log", f"main..{branch}", "--oneline"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        has_commits = bool(log_result.stdout.strip())
        return has_commits

    except Exception:
        return None  # git unavailable or unexpected error — skip check


def _apply_merge_failure_to_update_data(
    *,
    update_data: Dict[str, Any],
    task: Task,
    merge_result: Dict[str, Any],
    agent_id: str,
) -> Dict[str, Any]:
    """Translate a merge-conflict result into a BLOCKED kanban state.

    Helper that converts a failure dict from
    :func:`_merge_agent_branch_to_main` into the kanban-update
    mutations and structured failure response the caller needs.

    Bug #651: previously a merge conflict left ``update_data["status"]``
    as :data:`TaskStatus.DONE` (set unconditionally by the
    ``status == "completed"`` branch at ``report_task_progress`` line
    3397), and the failure was "deferred" for return after the kanban
    update.  Under the ephemeral one-agent-per-task lifecycle (PR #600)
    the agent that would have received the deferred message has already
    exited, so the kanban records DONE while the filesystem is missing
    the work.

    This helper closes that divergence: the kanban status flips to
    BLOCKED, ``completed_at`` is cleared (the task is not actually
    done), and a ``merge_conflict`` record is stamped on
    ``source_context`` carrying the info a recovery surface needs to
    fix the conflict — which branch, which agent, the git stderr, and
    a timestamp.

    Parameters
    ----------
    update_data : Dict[str, Any]
        The kanban update dict being built inside
        ``report_task_progress``.  Mutated in place: status flips to
        BLOCKED, ``completed_at`` is removed, ``source_context`` gains
        a ``merge_conflict`` entry (existing keys preserved).
    task : Task
        The completing task, used to read any pre-existing
        ``source_context`` so this helper merges into it rather than
        overwriting.
    merge_result : Dict[str, Any]
        The failure dict returned by :func:`_merge_agent_branch_to_main`.
        Not mutated.  Carries the underlying git error in ``message``.
    agent_id : str
        Id of the agent whose branch failed to merge.  Used to derive
        ``branch=marcus/{agent_id}`` and to surface in the response.

    Returns
    -------
    Dict[str, Any]
        Failure response the caller surfaces to its caller.  Shape:
        ``{"success": False, "status": "merge_conflict",
        "task_id": ..., "agent_id": ..., "blocker": ..., "message": ...}``.

    Notes
    -----
    Recovery surface (separate work): a CLI command, follow-up
    "Resolve merge conflict" task, or human-in-the-loop console takes
    the ``merge_conflict`` source_context entry and re-attempts the
    merge after the conflict is resolved in the worktree.  This helper
    does not implement recovery — it makes the BLOCKED state
    actionable downstream.

    See Also
    --------
    _merge_agent_branch_to_main : The merge primitive whose failure
        result this helper translates.
    """
    # Flip status: DONE was set optimistically at line 3397; merge
    # failure means the task is NOT actually done.
    update_data["status"] = TaskStatus.BLOCKED
    # Clear completed_at — leaving it populated alongside BLOCKED
    # would corrupt downstream telemetry (Cato completion latency).
    update_data.pop("completed_at", None)

    # Build the merge_conflict record stamped onto source_context.
    branch = f"marcus/{agent_id}"
    conflict_record = {
        "agent_id": agent_id,
        "branch": branch,
        "conflict_stderr": str(merge_result.get("message", "")),
        "blocked_at": datetime.now(timezone.utc).isoformat(),
    }

    # Merge into existing source_context.  Tasks already carry
    # in_scope_outcome_ids (#523), responsibility (contract-first),
    # and other meaningful fields — preserve them.  A task with
    # source_context=None initializes to a fresh dict.
    existing_ctx = task.source_context if task.source_context is not None else {}
    new_ctx = {**existing_ctx, **(update_data.get("source_context") or {})}
    new_ctx["merge_conflict"] = conflict_record
    update_data["source_context"] = new_ctx

    blocker_message = (
        f"Branch {branch} failed to merge to main: "
        f"{merge_result.get('message', 'merge_conflict')}. "
        "Task marked BLOCKED — resolve the conflict in the worktree "
        "(git merge main; resolve; commit) and re-run completion, or "
        "use `marcus resolve-conflict` (when available)."
    )

    return {
        "success": False,
        "status": "merge_conflict",
        "task_id": task.id,
        "agent_id": agent_id,
        "branch": branch,
        "blocker": blocker_message,
        "message": (
            f"Marcus did not mark task {task.id} done because branch "
            f"{branch} could not be merged to main.  The work is still "
            "on the branch; the kanban shows BLOCKED so the conflict is "
            "visible rather than silently dropped (bug #651)."
        ),
    }


async def _attempt_merge_recovery(
    *,
    task: Task,
    state: Any,
) -> Optional[Dict[str, Any]]:
    """Recover a BLOCKED-by-merge-conflict task via worktree rebase.

    Bug #651 follow-up.  PR #653 marked tasks BLOCKED on merge fail
    (closed the kanban/filesystem divergence) but left them
    unrecoverable.  verify-snake-8 (test71, 2026-05-25) hit one
    such conflict and the runner spawned 49 ephemeral agents
    chasing the unclaimed downstream task before the 20-minute
    stall watchdog gave up — roughly $25-50 of wasted spawn cost.

    Most "conflicts" in DAG-based parallel work are stale-base
    false conflicts: another agent merged main while this agent's
    worktree was on the old base; the actual code changes don't
    overlap.  Rebase resolves these mechanically.  Real content
    conflicts (overlapping line edits) abort the rebase and leave
    the task BLOCKED for human intervention.

    Recovery flow:

    1. Read ``source_context.merge_conflict`` from the BLOCKED task.
    2. Resolve the worktree path
       (``<project_root>/../worktrees/<agent_id>``, the Marcus
       convention set by ``spawn_agents.py``).
    3. Run ``git rebase main`` in the worktree.
    4. On clean rebase: ``git checkout main`` in the main repo,
       ``git merge --ff-only <branch>``, transition task to DONE
       on the kanban, clear ``merge_conflict`` from source_context.
    5. On rebase conflict: ``git rebase --abort`` to clean state;
       leave task BLOCKED; return None.

    Parameters
    ----------
    task : Task
        The BLOCKED task whose source_context carries the
        ``merge_conflict`` record from
        :func:`_apply_merge_failure_to_update_data`.
    state : Any
        Marcus server state.  ``kanban_client._load_workspace_state()``
        provides the ``project_root``; ``kanban_client.update_task``
        applies the BLOCKED→DONE transition on success.

    Returns
    -------
    Optional[Dict[str, Any]]
        Success dict ``{"success": True, "task_id": ...,
        "method": "rebase"}`` when the task recovered.  ``None`` for
        any failure path.  Callers treat ``None`` as "task stays
        BLOCKED."

    Notes
    -----
    Bright-line: Marcus rebasing the agent's branch onto main is
    environment shaping, not HOW dictation.  Same primitive as
    ``git pull --rebase`` that any developer runs daily.
    """
    import subprocess as _sp
    from pathlib import Path as _Path

    ctx = task.source_context or {}
    merge_conflict = ctx.get("merge_conflict")
    if not isinstance(merge_conflict, dict):
        return None

    agent_id = merge_conflict.get("agent_id")
    branch = merge_conflict.get("branch") or (
        f"marcus/{agent_id}" if agent_id else None
    )
    if not agent_id or not branch:
        return None

    project_root: Optional[str] = None
    if hasattr(state, "kanban_client") and state.kanban_client:
        load_state = getattr(state.kanban_client, "_load_workspace_state", None)
        if callable(load_state):
            try:
                ws = load_state()
                if isinstance(ws, dict):
                    project_root = ws.get("project_root")
            except Exception:
                project_root = None
    if not project_root:
        return None

    repo = _Path(project_root)
    if not repo.is_dir():
        return None

    worktree = repo.parent / "worktrees" / agent_id
    if not worktree.is_dir():
        logger.info(
            "[recovery] %s: worktree %s missing; cannot auto-recover",
            task.id,
            worktree,
        )
        return None

    logger.info(
        "[recovery] %s: attempting rebase of %s onto main in %s",
        task.id,
        branch,
        worktree,
    )
    try:
        rebase = _sp.run(
            ["git", "rebase", "main"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (_sp.SubprocessError, OSError) as exc:
        logger.warning("[recovery] %s: rebase subprocess error: %s", task.id, exc)
        return None

    if rebase.returncode != 0:
        logger.warning(
            "[recovery] %s: rebase failed (real content conflict?): %s",
            task.id,
            rebase.stderr.strip() if rebase.stderr else "(no stderr)",
        )
        try:
            _sp.run(
                ["git", "rebase", "--abort"],
                cwd=worktree,
                capture_output=True,
                timeout=30,
            )
        except (_sp.SubprocessError, OSError):
            pass
        return None

    try:
        _sp.run(
            ["git", "checkout", "main"],
            cwd=repo,
            check=True,
            capture_output=True,
            timeout=30,
        )
        _sp.run(
            ["git", "reset", "--hard", "HEAD"],
            cwd=repo,
            capture_output=True,
            timeout=30,
        )
        merge = _sp.run(
            ["git", "merge", "--ff-only", branch],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if merge.returncode != 0:
            logger.warning(
                "[recovery] %s: post-rebase fast-forward failed: %s",
                task.id,
                merge.stderr.strip() if merge.stderr else "(no stderr)",
            )
            return None
    except (_sp.SubprocessError, OSError) as exc:
        logger.warning("[recovery] %s: post-rebase merge error: %s", task.id, exc)
        return None

    new_ctx = dict(ctx)
    new_ctx.pop("merge_conflict", None)
    update_data: Dict[str, Any] = {
        "status": TaskStatus.DONE,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source_context": new_ctx,
    }
    try:
        await state.kanban_client.update_task(task.id, update_data)
    except Exception as exc:
        logger.warning(
            "[recovery] %s: kanban update failed after rebase+merge: %s",
            task.id,
            exc,
        )
        return None

    logger.info(
        "[recovery] %s: recovered via rebase; branch %s merged to main",
        task.id,
        branch,
    )
    return {
        "success": True,
        "task_id": task.id,
        "branch": branch,
        "method": "rebase",
    }


async def _sweep_blocked_merge_conflicts(state: Any) -> int:
    """Sweep BLOCKED-by-merge-conflict tasks and attempt recovery on each.

    Called from :func:`request_next_task` before returning
    ``no_task_ready``.  Iterates :attr:`state.project_tasks`, picks
    out tasks that are BLOCKED with a ``merge_conflict`` record in
    ``source_context``, and runs :func:`_attempt_merge_recovery` on
    each.  Returns the count of successfully recovered tasks so
    the caller can decide whether to refresh project state and
    retry task selection.

    Bug #651 follow-up — eliminates verify-snake-8's spawn-thrash
    failure mode where 49 ephemeral agents spawned chasing an
    unrecoverable BLOCKED task before the 20-min stall watchdog
    fired.

    Parameters
    ----------
    state : Any
        Marcus server state.  Needs ``project_tasks`` and
        ``kanban_client``.

    Returns
    -------
    int
        Number of BLOCKED tasks recovered to DONE.
    """
    project_tasks = getattr(state, "project_tasks", None) or []
    recovered = 0
    for task in project_tasks:
        if getattr(task, "status", None) != TaskStatus.BLOCKED:
            continue
        ctx = getattr(task, "source_context", None) or {}
        if not isinstance(ctx, dict) or "merge_conflict" not in ctx:
            continue
        try:
            result = await _attempt_merge_recovery(task=task, state=state)
        except (
            Exception
        ) as exc:  # noqa: BLE001 - never let one bad task block the sweep
            logger.warning("[recovery] sweep: unexpected error on %s: %s", task.id, exc)
            continue
        if result and result.get("success"):
            recovered += 1
    return recovered


async def _merge_agent_branch_to_main(
    agent_id: str,
    task_id: str,
    state: Any,
) -> Optional[Dict[str, Any]]:
    """
    Merge agent's worktree branch to main after task completion.

    Convention: if branch marcus/{agent_id} exists, the agent used
    a worktree. Merge it to main so dependent tasks can see the code.

    If merge conflicts, abort and return failure — the agent must
    resolve conflicts and report completion again.

    Returns None if no merge needed, success dict if merged,
    or failure dict if conflicts.

    See: https://github.com/lwgray/marcus/issues/250
    """
    import subprocess as _sp
    from pathlib import Path

    # Find the main repo (implementation/ directory)
    # Use workspace state file to get project_root, then use
    # git rev-parse to find the actual repo root regardless
    # of whether we're in a worktree or the main repo.
    project_root = None
    if hasattr(state, "kanban_client") and state.kanban_client:
        ws_state = state.kanban_client._load_workspace_state()
        if ws_state and "project_root" in ws_state:
            project_root = ws_state["project_root"]

    if not project_root:
        return None

    # project_root points to implementation/ (main repo).
    # Use it directly — git commands run here.
    repo = Path(project_root)
    if not repo.exists():
        return None

    # Verify it's a git repo (or inside one)
    try:
        check = _sp.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            return None
    except Exception:
        return None

    branch = f"marcus/{agent_id}"

    # Check if this agent's branch exists
    try:
        result = _sp.run(
            ["git", "branch", "--list", branch],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if not result.stdout.strip():
            # No worktree branch — agent worked on main directly
            return None
    except Exception:
        return None

    logger.info(
        f"[worktree] Merging {branch} to main " f"after task {task_id} completion"
    )

    try:
        # Checkout main
        _sp.run(
            ["git", "checkout", "main"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        # Bug #651 — defensive working-tree cleanup before merge.
        #
        # The verify-snake-4 run (test66, 2026-05-24) surfaced that
        # the composer smoke gate ran ``npm install --silent && npm
        # run build`` directly in ``project_root``.  The ``npm
        # install`` step writes to ``package-lock.json``, leaving
        # the main repo's working tree dirty.  The subsequent merge
        # then fails with:
        #
        #     error: Your local changes to the following files would
        #     be overwritten by merge: package-lock.json
        #     Aborting
        #
        # Two consecutive merges (composer task ``a7d5bbe7`` and
        # integration verifier ``8ff99c5b``) hit this exact pattern,
        # dropping the composer's wiring and the integration
        # verifier's renderer implementation despite both tasks
        # reporting build-verified.  The kanban marked DONE, the
        # filesystem missed the work, and the user saw a blank
        # snake game.
        #
        # The proper architectural fix is #652 (gates run in agent
        # worktree or scratch dir, never in ``project_root``).  This
        # defensive reset is the surgical safety net: discard any
        # uncommitted changes in ``main``'s working tree before
        # attempting the merge.  Safe by design — merging only
        # cares about committed state in ``main``; any uncommitted
        # changes in ``project_root`` are gate-test side effects
        # that should not influence merge outcomes.
        try:
            _sp.run(
                ["git", "reset", "--hard", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
        except _sp.CalledProcessError as reset_err:
            # Rare — git reset failed.  Log and proceed to merge
            # attempt anyway; the merge will surface the underlying
            # problem more concretely than a half-recovered reset.
            logger.warning(
                "[worktree] Pre-merge reset --hard failed for %s: %s. "
                "Continuing to merge attempt — the merge will surface "
                "any concrete blocker.",
                branch,
                reset_err.stderr,
            )

        # Attempt merge
        merge = _sp.run(
            [
                "git",
                "merge",
                branch,
                "--no-ff",
                "-m",
                f"Merge {branch} (task {task_id} by {agent_id})",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )

        if merge.returncode == 0:
            logger.info(f"[worktree] Successfully merged {branch} to main")
            return {"success": True}
        else:
            # Merge conflict — abort and send agent back
            _sp.run(
                ["git", "merge", "--abort"],
                cwd=repo,
                capture_output=True,
            )
            logger.warning(
                f"[worktree] Merge conflict for {branch}: " f"{merge.stderr}"
            )
            return {
                "success": False,
                "error": "merge_conflict",
                "message": (
                    f"Your task passed validation but merging "
                    f"your branch ({branch}) to main has "
                    f"conflicts. Please resolve them:\n"
                    f"  git merge main\n"
                    f"  (resolve conflicts in your editor)\n"
                    f"  git add . && git commit\n"
                    f"Then report completion again."
                ),
            }

    except Exception as e:
        logger.warning(f"[worktree] Merge failed: {e}")
        # Don't block completion if git is unavailable
        return None


def _resolve_completed_task(
    task_id: str,
    board_tasks: List[Task],
    project_tasks: List[Task],
) -> Optional[Task]:
    """Resolve the ``Task`` object for a completed task id (issue #557).

    The kanban board only stores parent tasks, so ``get_all_tasks()``
    misses subtasks. Subtask ``Task`` objects (``is_subtask=True``) live
    in ``state.project_tasks``. When the board lookup misses, fall back
    there so a subtask completion is not silently dropped — leaving the
    validation gate to skip every subtask.

    Parameters
    ----------
    task_id : str
        Id of the completed task or subtask.
    board_tasks : List[Task]
        Tasks from ``kanban_client.get_all_tasks()`` (parent tasks only).
    project_tasks : List[Task]
        ``state.project_tasks`` — the unified store that also holds
        subtask ``Task`` objects.

    Returns
    -------
    Optional[Task]
        The resolved task, or ``None`` if the id matches neither store.
    """
    task = next((t for t in board_tasks if t.id == task_id), None)
    if task is None:
        task = next((t for t in project_tasks if t.id == task_id), None)
    return task


def _should_validate_completion(task: Task, board_tasks: List[Task]) -> bool:
    """Decide whether a completed task needs validation (issue #557).

    ``should_validate_task`` is label-based. Decomposed subtasks may not
    carry implementation labels of their own, which would make every
    subtask skip validation. A subtask of an implementation parent IS
    implementation work, so the decision uses the parent task's labels.

    Fail toward validating: when ``task`` is a subtask but its parent
    cannot be resolved, return ``True``. A subtask exists only because
    its parent was a decomposable implementation task (design tasks are
    not decomposed), so it is implementation work — skipping validation
    on a parent-lookup miss would silently reopen the #557 gap.

    Parameters
    ----------
    task : Task
        The resolved completed task (may be a subtask).
    board_tasks : List[Task]
        Parent tasks from the kanban board, used to find the parent.

    Returns
    -------
    bool
        True if the completed work should be validated.
    """
    # Lazy import to avoid a circular dependency.
    from src.ai.validation.task_filter import should_validate_task

    if getattr(task, "is_subtask", False) and getattr(task, "parent_task_id", None):
        parent = next((t for t in board_tasks if t.id == task.parent_task_id), None)
        if parent is None:
            # Parent unresolved — validate anyway rather than skip.
            return True
        return should_validate_task(parent)
    return should_validate_task(task)


async def report_task_progress(
    agent_id: str,
    task_id: str,
    status: str,
    progress: int,
    message: str,
    state: Any,
    start_command: Optional[str] = None,
    readiness_probe: Optional[str] = None,
    verifications: Optional[List[Dict[str, Any]]] = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Agents report their task progress.

    Updates task status, progress percentage, and handles completion.
    Includes code analysis for GitHub projects.

    Parameters
    ----------
    agent_id : str
        The reporting agent's ID
    task_id : str
        ID of the task being updated
    status : str
        Task status (in_progress, completed, blocked)
    progress : int
        Progress percentage (0-100)
    message : str
        Progress update message
    state : Any
        Marcus server state instance
    start_command : Optional[str]
        Shell-style command that Marcus should run to verify the
        deliverable actually starts. REQUIRED when completing an
        integration verification task (``"type:integration"`` label);
        ignored on all other tasks. Examples:

        - ``"npm run build"`` for a Node build
        - ``"python -m mypackage --help"`` for a Python CLI
        - ``"tsc --noEmit"`` for a TypeScript type check
        - ``"uvicorn main:app --port 8000"`` for a long-running server
          (pair with a ``readiness_probe``)

        When the agent declares this on an integration task, Marcus
        runs it as a subprocess with a 60s timeout (one-shot mode) or
        15s readiness window (server mode). Missing on an integration
        task completion → the completion is rejected.
    readiness_probe : Optional[str]
        Optional shell-style command that Marcus polls to detect when
        a long-running server is ready. When provided alongside
        ``start_command``, Marcus starts the command in the background
        and polls the probe every 1s for up to 15s. Pass requires the
        probe to return exit 0 at least once within that window.
        Marcus always kills the background process before accepting
        the completion. Examples:

        - ``"curl -f http://localhost:8000/health"``
        - ``"curl -f http://localhost:3000/"``

        Absent ``readiness_probe``, Marcus treats the start_command as
        a one-shot that must exit 0 within 60s.
    verifications : Optional[List[Dict[str, Any]]]
        Issue #523 Slice B: per-outcome verification list.  When
        completing an integration task with declared user outcomes,
        pass one entry per in-scope ``UserOutcome`` so Marcus can
        verify each outcome's ``success_signal`` was observed in the
        running deliverable.  Each entry is a dict:

        - ``signal_id`` (str, required) — the ``UserOutcome.id`` this
          command verifies
        - ``command`` (str, required) — shell command Marcus runs as a
          subprocess; exit 0 means the signal was observed
        - ``description`` (str, optional) — short human label
        - ``readiness_probe`` (str, optional) — per-spec probe for
          server-mode verifications

        Takes precedence over ``start_command`` when present:
        Marcus runs every declared verification (in input order) via
        the same subprocess primitive ``start_command`` uses.  Any
        non-zero exit rejects the completion; the agent-facing
        blocker names the failing ``signal_id``, ``description``,
        ``command``, exit code, and stderr tail.

        Backward compat: when ``verifications`` is ``None`` and only
        ``start_command`` is provided, the legacy single-command
        smoke gate runs unchanged.  When both are provided,
        ``verifications`` wins and ``start_command`` is ignored.
    evidence : Optional[Dict[str, Any]]
        Issue #677: behavior evidence captured by actually RUNNING the
        assembled product.  Required when completing an integration or
        composition task whose project Marcus classified with a
        behavior-evidence contract (web, data pipeline, CLI tool,
        library, API service, ML/AI).  The expected keys depend on the
        app type and are stated in the task description:

        - web app / game → ``dom`` (rendered HTML of the body) +
          ``console_errors`` (list)
        - data pipeline → ``output`` (produced output) or
          ``output_rows`` (int)
        - CLI tool → ``exit_code`` (int) + ``stdout`` (str)
        - library → ``import_ok`` (bool) + ``call_result``
        - API service → ``status`` (int) + ``body``
        - ML/AI → ``prediction``

        Marcus judges this evidence against the per-type bar — a build
        that exits 0 and a server that returns 200 do not pass.  Unlike
        ``verifications`` (where the agent chooses the command and
        Marcus trusts exit 0), Marcus judges the *evidence itself*,
        which is what keeps the behavior check ungameable.  App types
        with no behavior contract (``other``/``automation``/unknown)
        ignore this field and fall back to ``verifications``/
        ``start_command``.

    Returns
    -------
    Dict[str, Any]
        Dict with success status
    """
    # Phase timing for performance monitoring (issue: where does the
    # 4-min-per-task cost come from?). Mirrors the inline _mark
    # pattern in ``request_next_task`` so all Marcus timing log lines
    # can be grepped together. Marks fire on the success path only —
    # error returns skip the log, matching ``request_next_task``.
    from src.core.perf_instrumentation import PhaseTimer

    _timer = PhaseTimer()

    # Get project/board context
    project_context = await get_project_board_context(state)
    _timer.mark("ctx_setup")

    # Log progress update
    conversation_logger.log_worker_message(
        agent_id,
        "to_pm",
        f"Progress update: {message} ({progress}%)",
        {"task_id": task_id, "status": status, "progress": progress, **project_context},
    )

    # Log conversation event for visualization
    log_agent_event(
        "progress_update",
        {
            "agent_id": agent_id,
            "task_id": task_id,
            "status": status,
            "progress": progress,
            "message": message,
        },
    )

    # Record the progress heartbeat in the active experiment monitor so
    # the runner's spawn-thrash detector sees a claimed task advancing
    # (report_task_progress at 25/50/75%) as real progress, not only
    # completions. Counted on every report — a report arriving at all
    # proves the agent is alive and working its task.
    from src.experiments.live_experiment_monitor import get_active_monitor

    _progress_monitor = get_active_monitor()
    if _progress_monitor and _progress_monitor.is_running:
        _progress_monitor.record_progress(
            agent_id=agent_id,
            task_id=task_id,
            progress=progress,
            status=status,
        )

    # Escalation flag — set when the retry ceiling is hit during
    # validation. The task is routed through the normal completion
    # path (kanban update, lease cleanup, branch merge) but the
    # final response surfaces the escalation so the caller and any
    # downstream observers know the validator's complaints were
    # overridden. See MAX_VALIDATION_RETRIES constant above.
    validation_escalated: bool = False
    escalation_payload: Optional[Dict[str, Any]] = None

    # #206 MVP Phase 4 (Kaia review fix): flag-driven release so EVERY
    # early-return path that puts the task in a terminal state (DONE or
    # BLOCKED in kanban) frees its file locks. The function has several
    # early returns inside the outer ``try:`` — gate-rejection paths
    # (smoke / composer / validation failure) leave the task IN_PROGRESS
    # and must NOT release; terminal-state paths (deferred merge
    # failure, validation-escalated success, stub-warning success,
    # normal success) MUST release. The ``finally:`` at the bottom of
    # the function reads this flag and releases under ``asyncio`` safely.
    _release_locks_on_exit: bool = False

    try:
        # Initialize kanban if needed
        await state.initialize_kanban()
        _timer.mark("kanban_init")

        # Log Marcus thinking
        log_thinking(
            "marcus",
            f"Processing progress update from {agent_id}",
            {"task_id": task_id, "status": status, "progress": progress},
        )

        # Stale-completion guard for recovered tasks (Issue #343).
        # When a task's lease expires and is recovered to another
        # agent, the original agent's in-memory assignment is
        # cleared by ``on_recovery_callback`` (see server.py:699).
        # If that original agent keeps working locally and later
        # reports completion (unaware their assignment was revoked),
        # Marcus must reject the stale completion — otherwise we
        # accept a second completion on a task another agent is
        # actively working on, and the two implementations collide
        # at merge time. Dashboard-v70 produced 341 lines of ghost
        # source + 506 lines of ghost tests this way.
        #
        # The guard fires on ``status == "completed"`` only. Agents
        # can still report intermediate progress even if their
        # assignment was cleared — that path recreates the lease
        # (see "No active lease" fallback below) rather than
        # producing a persistent kanban mutation. Completions, by
        # contrast, write DONE to kanban and trigger branch merges
        # that cannot be undone cleanly.
        if status == "completed":
            current_assignment = state.agent_tasks.get(agent_id)
            assignment_task_id = (
                getattr(current_assignment, "task_id", None)
                if current_assignment is not None
                else None
            )
            if assignment_task_id != task_id:
                # Cold-cache fallback (Codex P1 review on PR #345).
                # ``state.agent_tasks`` is in-memory only and starts
                # empty on Marcus restart; ``MarcusServer.__init__``
                # never rehydrates it from ``assignment_persistence``.
                # The lease manager, by contrast, IS rebuilt from
                # persistence on startup (see
                # ``AssignmentLeaseManager`` setup in server.py),
                # which makes it the authoritative source for
                # "who currently holds this task" across a restart.
                #
                # Without this fallback, every legitimate post-
                # restart completion would be rejected as stale
                # and the task would stay stuck in progress until
                # manual intervention. We only declare the
                # completion stale when BOTH in-memory cache AND
                # the lease manager say this agent isn't the holder.
                lease_holder_matches = False
                if hasattr(state, "lease_manager") and state.lease_manager is not None:
                    try:
                        lease = state.lease_manager.active_leases.get(task_id)
                        if (
                            lease is not None
                            and getattr(lease, "agent_id", None) == agent_id
                        ):
                            lease_holder_matches = True
                            logger.info(
                                f"Stale completion guard: in-memory "
                                f"agent_tasks cache miss for "
                                f"{agent_id}/{task_id}, but lease "
                                f"manager confirms this agent holds "
                                f"the task — allowing completion "
                                f"(cold-cache recovery, Codex P1 on "
                                f"PR #345)"
                            )
                    except Exception as lease_err:
                        # Lease lookup failed for some reason.
                        # Don't crash the completion path; fall
                        # through to the rejection. Worst case the
                        # agent retries and we eventually rebuild
                        # the cache.
                        logger.warning(
                            f"Stale completion guard: lease lookup "
                            f"raised for {task_id}: {lease_err}. "
                            f"Falling through to default rejection."
                        )

                # Issue #667 Fix 2: transactional late-completion accept.
                #
                # Before the final rejection, check whether the task is
                # actually uncontested — no replacement lease, no other
                # agent's assignment, no in-flight assignment. If
                # nothing else is touching the task, the late
                # completion is safe to accept and the agent's work
                # (verification artifacts, remediation, etc.) is
                # preserved instead of discarded.
                #
                # This preserves Issue #343's two-agent-race protection
                # for the cases where ANY replacement signal exists
                # (in-flight assignment, another agent's
                # ``agent_tasks`` entry, a new active lease).
                #
                # Three replacement signals checked in order of
                # cheapness:
                #
                # 1. ``active_leases.get(task_id)`` exists — a new
                #    lease has been created. If the lease holder is
                #    NOT this agent (already verified above), reject.
                # 2. Any other agent in ``state.agent_tasks``
                #    references this task — a new assignment has
                #    landed. Reject.
                # 3. ``tasks_being_assigned`` contains the task —
                #    Marcus is mid-assignment. Reject (race case).
                #
                # If all three are clear, accept by flipping
                # ``lease_holder_matches`` to True. The completion
                # path proceeds as if the original lease was still
                # valid.
                if not lease_holder_matches:
                    uncontested = False
                    try:
                        no_active_lease = True
                        if (
                            hasattr(state, "lease_manager")
                            and state.lease_manager is not None
                        ):
                            no_active_lease = (
                                state.lease_manager.active_leases.get(task_id) is None
                            )

                        no_other_agent = all(
                            getattr(a, "task_id", None) != task_id
                            for a in state.agent_tasks.values()
                        )

                        no_in_flight = task_id not in getattr(
                            state, "tasks_being_assigned", set()
                        )

                        if no_active_lease and no_other_agent and no_in_flight:
                            uncontested = True
                    except Exception as race_err:
                        # Defensive: if the uncontested check raises
                        # (corrupt state, mid-mutation race), fall
                        # through to the existing rejection. Safer
                        # to reject than to accept under uncertainty.
                        logger.warning(
                            f"Issue #667 Fix 2: uncontested-task check "
                            f"raised for {task_id}: {race_err}. "
                            f"Falling through to default rejection."
                        )

                    if uncontested:
                        lease_holder_matches = True
                        logger.info(
                            f"Issue #667 Fix 2: accepting late "
                            f"completion from {agent_id} on task "
                            f"{task_id} — no replacement lease, no "
                            f"other agent assignment, no in-flight "
                            f"assignment. Agent's work preserved "
                            f"instead of discarded."
                        )

                if not lease_holder_matches:
                    logger.warning(
                        f"Rejecting stale completion: agent {agent_id} "
                        f"tried to complete task {task_id} but is no "
                        f"longer assigned to it (current assignment: "
                        f"{assignment_task_id!r}, lease holder check "
                        f"failed). This usually means the task's "
                        f"lease expired and was recovered to another "
                        f"agent while the original agent was still "
                        f"working on it. Issue #343."
                    )
                    return {
                        "success": False,
                        "status": "stale_completion",
                        "error": "task_recovered",
                        "message": (
                            f"Cannot complete task {task_id}: your "
                            f"assignment was revoked because the "
                            f"lease expired and another agent took "
                            f"ownership. Your work on branch "
                            f"marcus/{agent_id} is preserved in git "
                            f"— request your next task to continue."
                        ),
                    }

        _timer.mark("stale_guard")

        # Issue #667 Fix 1: extend the lease into the validation
        # window BEFORE the validation/smoke gates run. Codex P1 on
        # PR #668: if this fires after the gates, the smoke gate has
        # already run under the prior lease (which expires mid-gate
        # for slow integration tasks), and on a successful completion
        # the lease has been deleted by the time we'd attempt to
        # extend it. Both Fix 1 paths must be anchored before the
        # downstream completion machinery so the entire
        # validation + remediation cycle runs under the validation
        # window.
        if (
            status == "completed"
            and hasattr(state, "lease_manager")
            and state.lease_manager
        ):
            extended = await state.lease_manager.extend_for_validation(task_id)
            if extended:
                logger.info(
                    f"Extended lease for task {task_id} into "
                    f"validation window before smoke gate "
                    f"(expires: {extended.lease_expires.isoformat()})"
                )
            # If extension returned None, the lease was already
            # expired or missing — Fix 2's transactional
            # late-accept path handles that case separately.

        # Update task in kanban
        update_data: Dict[str, Any] = {"progress": progress}

        # VALIDATION GATE: Check if implementation task needs validation
        if status == "completed":
            # CRITICAL: Fetch fresh task from Kanban to get current labels
            # state.project_tasks has stale data from project initialization
            # Labels are added AFTER task creation, so we need fresh data.
            # #557: get_all_tasks() returns only parent tasks; subtasks
            # are resolved from state.project_tasks by the helper below.
            fresh_tasks = await state.kanban_client.get_all_tasks()
            task = _resolve_completed_task(
                task_id, fresh_tasks, getattr(state, "project_tasks", []) or []
            )

            logger.info(
                f"VALIDATION GATE: Task {task_id} completed, "
                f"found task object: {task is not None}"
            )

            if task:
                task_labels = task.labels if hasattr(task, "labels") else None
                # #557: a subtask's validate/skip decision uses its
                # parent's labels — see _should_validate_completion.
                should_validate = _should_validate_completion(task, fresh_tasks)
                logger.info(
                    f"VALIDATION GATE: Task {task_id} ({task.name}) - "
                    f"labels={task_labels}, should_validate={should_validate}"
                )

                if should_validate:
                    try:
                        logger.info(
                            f"VALIDATION GATE: Starting validation for {task_id}"
                        )
                        # Touch lease before validation: LLM calls can take
                        # 60-120s per retry and the agent goes silent here.
                        # touch_lease extends without regressing progress %.
                        if hasattr(state, "lease_manager") and state.lease_manager:
                            await state.lease_manager.touch_lease(agent_id)
                        # Run validation
                        validation_result = await _validate_task_completion(
                            task, agent_id, state
                        )

                        # Handle failure
                        if not validation_result.passed:
                            # Retry ceiling check (Codex P1 on PR #337).
                            # Must run INLINE, before the failure
                            # handler, so escalated tasks fall through
                            # to the normal completion path below —
                            # otherwise the escalation response
                            # short-circuits kanban completion and
                            # leaves the task stuck IN_PROGRESS.
                            current_attempts = (
                                _retry_tracker.get_attempt_count(task.id)
                                if _retry_tracker is not None
                                else 0
                            )
                            if current_attempts >= MAX_VALIDATION_RETRIES:
                                logger.warning(
                                    f"VALIDATION ESCALATION: task "
                                    f"{task.id} ({task.name}) hit "
                                    f"{MAX_VALIDATION_RETRIES} validation "
                                    f"failures. Routing through normal "
                                    f"completion path with escalation "
                                    f"annotation. Final issues: "
                                    f"{[i.issue[:80] for i in validation_result.issues]}"  # noqa: E501
                                )
                                # Record this attempt so history is
                                # complete.
                                if _retry_tracker is not None:
                                    _retry_tracker.record_attempt(
                                        task.id, validation_result
                                    )
                                validation_escalated = True
                                escalation_payload = {
                                    "status": "validation_escalated",
                                    "escalated": True,
                                    "attempt_count": current_attempts + 1,
                                    "issues": [
                                        issue.to_dict()
                                        for issue in validation_result.issues
                                    ],
                                    "message": (
                                        f"Validation escalated after "
                                        f"{MAX_VALIDATION_RETRIES} failed "
                                        f"attempts. Task auto-passed via "
                                        f"the completion pipeline; "
                                        f"validator complaints logged for "
                                        f"review. This usually means the "
                                        f"validator is hallucinating or "
                                        f"the criteria need refinement."
                                    ),
                                }
                                # Fall through to the completion path.
                            else:
                                failure_response = await _handle_validation_failure(
                                    task, agent_id, validation_result, state
                                )
                                # Escalation: same-issue repeat auto-passes
                                # through the completion path below.
                                if failure_response.get("escalated"):
                                    validation_escalated = True
                                    escalation_payload = failure_response
                                    # fall through to completion path
                                else:
                                    return failure_response
                    except Exception as e:
                        # Validation system failed - log and allow completion
                        logger.error(f"Validation system error: {e}")
                        logger.exception("Validation exception details:")
                else:
                    logger.info(
                        f"VALIDATION GATE: Skipping validation for {task_id} "
                        f"(not an implementation task)"
                    )

            # PRODUCT SMOKE GATE (Layer 1 of the systemic
            # integration-verification fix). Integration verification
            # tasks have label "type:integration" and are NOT
            # validated by the citation-based LLM validator above
            # (they aren't implementation tasks). Their entire job
            # is to assemble the deliverable and prove it works —
            # so the right gate for them is a deterministic
            # Marcus-side check that the declared start_command
            # actually runs. This catches the dashboard-v71 class
            # of bug where the integration agent self-reports
            # success but the assembled deliverable fails to boot
            # (e.g. missing public/index.html).
            #
            # The smoke gate runs ``verify_deliverable`` as a
            # subprocess-level check against the agent-declared
            # start_command + optional readiness_probe. If the
            # start_command is missing (strict enforcement) or the
            # declared command fails, the task is rejected. This is
            # the same machine-beats-prompt pattern as PR #337's
            # runtime tests overriding LLM validation opinions.
            if task is not None and _is_integration_task(task):
                try:
                    smoke_response = await _run_product_smoke_gate(
                        task=task,
                        agent_id=agent_id,
                        state=state,
                        start_command=start_command,
                        readiness_probe=readiness_probe,
                        verifications=verifications,
                        evidence=evidence,
                        message=message,
                    )
                    if smoke_response is not None:
                        # Self-verify mode (#677 rework) no longer rejects for
                        # missing agent-authored proof, so the original
                        # gridlock sources (verifications_required_but_missing,
                        # behavior_evidence_missing) can't occur. The remaining
                        # rejections — ``build_failed`` and a failed
                        # agent-VOLUNTEERED check — are all fixable by the agent
                        # (fix the build / fix the deliverable), so they stay
                        # retryable; persistent failure is handled by the lease.
                        # We keep one ceiling: a volunteered behavior-evidence
                        # check that keeps failing escalates cleanly rather than
                        # looping to lease expiry.
                        smoke_error = smoke_response.get("error")
                        if smoke_error == "behavior_evidence_failed":
                            attempts = _record_behavior_evidence_attempt(task_id)
                            if attempts > MAX_SMOKE_BEHAVIOR_EVIDENCE_ATTEMPTS:
                                logger.warning(
                                    "Smoke gate: task %s rejected for behavior "
                                    "evidence %d times; escalating to a "
                                    "terminal response (issue #677).",
                                    task_id,
                                    attempts,
                                )
                                escalated = _escalate_behavior_evidence_response(
                                    smoke_response
                                )
                                # Codex P1 (#678): terminalize the board + release
                                # the lease, so an agent that obeys "do NOT retry"
                                # doesn't leave the task IN_PROGRESS idling until
                                # lease timeout.
                                await _terminalize_escalated_smoke_task(
                                    state,
                                    task_id,
                                    agent_id,
                                    str(
                                        escalated.get("blocker")
                                        or escalated.get("message")
                                        or "Smoke gate escalated: behavior "
                                        "evidence never met the bar."
                                    ),
                                )
                                _clear_smoke_attempts(task_id)
                                return escalated
                        return smoke_response
                except Exception as smoke_err:
                    # Smoke verification system error (not a smoke
                    # failure — those return a response above). Log
                    # and allow completion to proceed rather than
                    # blocking on infrastructure problems we don't
                    # understand. This matches the validation-error
                    # fallthrough policy above.
                    logger.error(
                        f"Product smoke verification system error "
                        f"for task {task_id}: {smoke_err}"
                    )
                    logger.exception("Smoke verification exception details:")

            # Self-verify-mode honesty stamp (#677, Kaia review).  Marcus no
            # longer runs an independent build/behavior check on composition or
            # integration tasks — the old composer build gate was tech-specific
            # (``npm run build``: Marcus owning a language ontology), weaker than
            # the agent (it gridlocked snake-skeptic-1 on a peer-dep conflict the
            # agent had already worked around), and uncorrelated with "the
            # product works" (every shipped-broken run still built).  So
            # verification lives with the agent.  We accept the completion on the
            # agent's self-report, but we do NOT let "done" silently read as
            # "independently verified" — that would erode the observability
            # Marcus competes on.  Stamp it: a WARNING log + a response flag the
            # caller propagates.  The objective check, when we add it, is a
            # separate stack-discovering verifier agent ("borrow hands").
            if task is not None and (
                _is_composition_task(task) or _is_integration_task(task)
            ):
                logger.warning(
                    "ACCEPTED ON SELF-REPORT: task %s (%s) completed with NO "
                    "independent Marcus verification — the agent ran and "
                    "verified the product; Marcus did not (self-verify mode, "
                    "#677). independently_verified=False.",
                    task_id,
                    ("composition" if _is_composition_task(task) else "integration"),
                )

        _timer.mark("validation")

        # Sentinel: holds a failed merge result to be returned AFTER the
        # kanban update. The task must always be finalized on the board
        # (DONE) before returning a merge-conflict error so it is never
        # left IN_PROGRESS with no owner (Codex review P1).
        _deferred_merge_failure: dict[str, object] | None = None

        if status == "completed":
            update_data["status"] = TaskStatus.DONE
            update_data["completed_at"] = datetime.now(timezone.utc).isoformat()

            # Handle subtask completion
            if hasattr(state, "subtask_manager") and state.subtask_manager:
                if task_id in state.subtask_manager.subtasks:
                    # This is a subtask - handle subtask-specific completion
                    from src.marcus_mcp.coordinator.subtask_assignment import (
                        check_and_complete_parent_task,
                        update_subtask_progress_in_parent,
                    )

                    # Update subtask status in unified storage
                    state.subtask_manager.update_subtask_status(
                        task_id,
                        TaskStatus.DONE,
                        state.project_tasks,
                        assigned_to=agent_id,
                    )

                    # Get parent task ID
                    subtask = state.subtask_manager.subtasks[task_id]
                    parent_task_id = subtask.parent_task_id

                    # Update parent task progress
                    await update_subtask_progress_in_parent(
                        parent_task_id,
                        task_id,
                        state.subtask_manager,
                        state.kanban_client,
                    )

                    # Check if parent should be auto-completed
                    parent_completed = await check_and_complete_parent_task(
                        parent_task_id,
                        state.subtask_manager,
                        state.kanban_client,
                        state,  # CRITICAL: Pass state for artifact/decision rollup
                        completing_agent_id=agent_id,
                    )

                    if parent_completed:
                        logger.info(
                            f"Parent task {parent_task_id} auto-completed "
                            f"after subtask {task_id} completion"
                        )

            # Calculate actual hours for experiment tracking
            task_assignment = state.agent_tasks.get(agent_id)
            if task_assignment:
                start_time = task_assignment.assigned_at
                # Ensure start_time is timezone-aware
                if start_time.tzinfo is None:
                    start_time = start_time.replace(tzinfo=timezone.utc)
                now_utc = datetime.now(timezone.utc)
                actual_hours = (now_utc - start_time).total_seconds() / 3600
                duration_seconds = (now_utc - start_time).total_seconds()
            else:
                actual_hours = 1.0  # Default if no assignment found
                duration_seconds = 3600.0  # 1 hour default

            # Record in active experiment if one is running
            from src.experiments.live_experiment_monitor import get_active_monitor

            monitor = get_active_monitor()
            if monitor and monitor.is_running:
                monitor.record_task_completion(
                    task_id=task_id,
                    agent_id=agent_id,
                    duration_seconds=duration_seconds,
                )

            # Memory recording moved to post-merge for #651 honesty
            # (Kaia review on PR #653).  Pre-fix, this block recorded
            # ``success=True`` before the merge had been attempted —
            # so merge-failed tasks (now marked BLOCKED, not DONE)
            # would have memory falsely showing success.  The
            # ``actual_hours`` value is computed above and stays in
            # scope; the recording itself happens in the merge
            # outcome branches below (success branch records
            # success=True; failure branch records success=False
            # with the merge-conflict blocker text).

            # Commit verification gate (dashboard-v88 post-mortem):
            # Reject completions from agents whose worktree branch has
            # zero commits ahead of main. An empty branch means the
            # agent reported done without implementing anything.
            # Only fires when a worktree branch exists (returns None
            # when no branch → agent worked on main → skip check).
            _ws_state = None
            if hasattr(state, "kanban_client") and state.kanban_client:
                _ws_state = state.kanban_client._load_workspace_state()
            _project_root = _ws_state.get("project_root") if _ws_state else None
            if _project_root:
                _commit_ok = _verify_agent_has_commits(agent_id, _project_root)
                if _commit_ok is False:
                    logger.warning(
                        f"[commit_gate] {agent_id} reported task "
                        f"{task_id} complete but branch "
                        f"marcus/{agent_id} has no commits ahead of "
                        f"main. Rejecting false completion."
                    )
                    return {
                        "success": False,
                        "status": "no_commits",
                        "message": (
                            f"Task {task_id} completion rejected: "
                            f"branch marcus/{agent_id} has no commits "
                            f"ahead of main. Commit your implementation "
                            f"before reporting task complete."
                        ),
                    }

            # Release coordination state BEFORE the worktree merge.
            #
            # Coordination state (lease, assignment) is released here,
            # before the merge, so a merge conflict never leaves the
            # lease ticking on a task that has no active work to protect
            # (snake_game-v1 cascade, Simon decision 011b3fad).
            #
            # completed_tasks_count is intentionally NOT incremented
            # here — it moves to after the merge so a failed merge does
            # not inflate the counter (Codex review P2).
            if agent_id in state.agent_status:
                agent = state.agent_status[agent_id]
                agent.current_tasks = []

                # Remove task assignment from state and persistence
                if agent_id in state.agent_tasks:
                    del state.agent_tasks[agent_id]

                # Remove from persistent storage
                await state.assignment_persistence.remove_assignment(agent_id)

                # Remove lease for completed task
                if hasattr(state, "lease_manager") and state.lease_manager:
                    if task_id in state.lease_manager.active_leases:
                        del state.lease_manager.active_leases[task_id]
                        logger.info(f"Removed lease for completed task {task_id}")

            # Merge agent's worktree branch to main (GH-250).
            # Do NOT return early on conflict — the kanban card must
            # still be finalized before this function exits so the
            # task is never left orphaned as IN_PROGRESS with no
            # owner (Codex review P1).
            #
            # Bug #651 update (2026-05-24): a conflict no longer
            # marks the kanban DONE.  The completed-status
            # update_data is rewritten in place by
            # ``_apply_merge_failure_to_update_data`` to BLOCKED with
            # the conflict info stamped on ``source_context``.  The
            # later ``state.kanban_client.update_task(task_id,
            # update_data)`` call (line ~3772) therefore records
            # BLOCKED rather than DONE, closing the
            # filesystem/kanban divergence.  Deferred-failure
            # response is still returned to the caller at the end
            # of the function so the runner sees the merge_conflict
            # error explicitly.
            merge_result = await _merge_agent_branch_to_main(agent_id, task_id, state)
            if merge_result and not merge_result.get("success"):
                # Bug #651: don't mark the kanban DONE on merge fail.
                # Helper flips update_data["status"] from DONE to
                # BLOCKED, removes ``completed_at``, and stamps
                # ``source_context.merge_conflict`` so a recovery
                # surface (CLI command, follow-up agent, human-in-
                # the-loop) can resolve.  Returned response is
                # propagated to the caller via
                # ``_deferred_merge_failure`` at the end of this
                # function so the runner sees BLOCKED rather than
                # interpreting silence as success.
                if task is not None:
                    _deferred_merge_failure = _apply_merge_failure_to_update_data(
                        update_data=update_data,
                        task=task,
                        merge_result=merge_result,
                        agent_id=agent_id,
                    )
                else:
                    # Defensive: task lookup at line 3308 should have
                    # populated ``task`` for any status="completed"
                    # path.  If it didn't, preserve the legacy
                    # deferred-failure behavior so the runner still
                    # sees an error rather than nothing.
                    _deferred_merge_failure = merge_result

            # completed_tasks_count tracks AGENT WORK OUTPUT, not git
            # outcomes — increment after the merge attempt regardless
            # of whether the merge succeeded (Codex P2 from
            # ``f2286c21``; locked by ``TestCompletionReleasesLeaseEven
            # OnMergeFailure.test_completed_tasks_count_incremented_
            # after_merge_not_before``).  PR #653's earlier attempt
            # to gate this on merge success violated the existing
            # invariant; restored here.
            if agent_id in state.agent_status:
                agent = state.agent_status[agent_id]
                agent.completed_tasks_count += 1

            # Memory recording, however, DOES reflect merge outcome.
            # Pre-#651, this recorded ``success=True`` regardless of
            # merge result, falsely inflating the agent's success
            # rate in the learned profile (Kaia review concern #2 on
            # PR #653).  Branched on merge outcome below so the
            # learned profile gets honest feedback.
            if hasattr(state, "memory") and state.memory:
                if merge_result and not merge_result.get("success"):
                    _merge_blocker = (
                        merge_result.get("message")
                        or merge_result.get("error")
                        or "merge_conflict"
                    )
                    await state.memory.record_task_completion(
                        agent_id=agent_id,
                        task_id=task_id,
                        success=False,
                        actual_hours=actual_hours,
                        blockers=[f"merge_conflict: {_merge_blocker}"],
                    )
                else:
                    await state.memory.record_task_completion(
                        agent_id=agent_id,
                        task_id=task_id,
                        success=True,
                        actual_hours=actual_hours,
                        blockers=[],
                    )

            # Code analysis runs ONLY on real merge success — analyzing
            # work that didn't land in main is meaningless (the code
            # the analyzer would inspect doesn't exist on main yet).
            if not (merge_result and not merge_result.get("success")):
                if agent_id in state.agent_status:
                    agent = state.agent_status[agent_id]

                    # Code analysis for GitHub
                    if state.provider == "github" and state.code_analyzer:
                        owner = os.getenv("GITHUB_OWNER")
                        repo = os.getenv("GITHUB_REPO")

                        # Get task details
                        task = await state.kanban_client.get_task_by_id(task_id)

                        # Analyze completed work
                        analysis = await state.code_analyzer.analyze_task_completion(
                            task, agent, owner, repo
                        )

                        if analysis and analysis.get("findings"):
                            # Store findings for future tasks
                            findings_str = json.dumps(analysis["findings"], indent=2)
                            await state.kanban_client.add_comment(
                                task_id,
                                f"🤖 Code Analysis:\n{findings_str}",
                            )

        elif status == "in_progress":
            update_data["status"] = TaskStatus.IN_PROGRESS
            # Include assigned_to for Planka provider compatibility
            if agent_id:
                update_data["assigned_to"] = agent_id

            # Handle subtask status update in unified storage
            if hasattr(state, "subtask_manager") and state.subtask_manager:
                if task_id in state.subtask_manager.subtasks:
                    state.subtask_manager.update_subtask_status(
                        task_id,
                        TaskStatus.IN_PROGRESS,
                        state.project_tasks,
                        assigned_to=agent_id,
                    )

        elif status == "blocked":
            # Guard 1: DONE tasks are immutable — cannot revert to BLOCKED.
            # Mirrors the check in report_blocker (same bug vector).
            _current = await state.kanban_client.get_task_by_id(task_id)
            if _current is not None:
                _cur_status = _current.status
                if _cur_status in {TaskStatus.DONE, "done", "completed"}:
                    logger.warning(
                        f"report_task_progress(blocked) rejected: agent "
                        f"{agent_id} tried to block task {task_id} which "
                        f"is already {_cur_status!r}. DONE tasks are immutable."
                    )
                    return {
                        "success": False,
                        "status": "task_already_complete",
                        "message": (
                            f"Task {task_id} is already complete — cannot "
                            "mark it blocked. Request your next task."
                        ),
                    }

            # Guard 2: Reject from agents who no longer hold the lease.
            if hasattr(state, "lease_manager") and state.lease_manager:
                _lease = state.lease_manager.active_leases.get(task_id)
                if _lease is not None and _lease.agent_id != agent_id:
                    logger.warning(
                        f"report_task_progress(blocked) rejected: agent "
                        f"{agent_id} does not hold lease for {task_id} "
                        f"(holder: {_lease.agent_id})."
                    )
                    return {
                        "success": False,
                        "status": "not_task_holder",
                        "message": (
                            f"Task {task_id} is held by {_lease.agent_id}, "
                            f"not {agent_id}. Your lease expired — request "
                            "your next task."
                        ),
                    }

            update_data["status"] = TaskStatus.BLOCKED

            # Record blocker in Memory if available
            if hasattr(state, "memory") and state.memory and message:
                # Try to get current task assignment
                task_assignment = state.agent_tasks.get(agent_id)
                if task_assignment:
                    start_time = task_assignment.assigned_at
                    # Ensure start_time is timezone-aware
                    if start_time.tzinfo is None:
                        start_time = start_time.replace(tzinfo=timezone.utc)
                    actual_hours = (
                        datetime.now(timezone.utc) - start_time
                    ).total_seconds() / 3600
                else:
                    actual_hours = 1.0

                await state.memory.record_task_completion(
                    agent_id=agent_id,
                    task_id=task_id,
                    success=False,
                    actual_hours=actual_hours,
                    blockers=[message],
                )

        # Update Kanban card
        await state.kanban_client.update_task(task_id, update_data)
        _timer.mark("kanban_update")

        # Update task progress (including checklist items).
        #
        # Bug #651 (Codex P1 on PR #653): when the merge failed,
        # ``update_data["status"]`` was just rewritten to BLOCKED by
        # ``_apply_merge_failure_to_update_data``.  But this
        # ``update_task_progress`` call sends the LOCAL ``status``
        # variable — which is still ``"completed"`` (the agent's
        # original report).  Every kanban provider's
        # ``update_task_progress`` reads ``status`` and re-applies
        # it (sqlite_kanban:963-970, github_kanban:444-446,
        # linear_kanban:355-357, planka_kanban:600-601), so without
        # the override below the second call would clobber the
        # BLOCKED state and re-introduce the DONE/merge-conflict
        # divergence this PR is fixing.
        _effective_status = "blocked" if _deferred_merge_failure is not None else status
        _effective_message = message
        if _deferred_merge_failure is not None:
            _effective_message = (
                f"Marcus blocked completion: branch merge failed. "
                f"{_deferred_merge_failure.get('blocker', '')}".strip()
            )
        await state.kanban_client.update_task_progress(
            task_id,
            {
                "progress": progress,
                "status": _effective_status,
                "message": _effective_message,
            },
        )

        # Lease renewal on progress update.
        #
        # ``status == "completed"`` — handled earlier (right after
        # the stale-completion guard) by ``extend_for_validation``
        # so the validation/smoke gates and any rejection-driven
        # remediation run under lease coverage. Codex P1 on PR #668:
        # this used to live here, after the smoke gate had already
        # run and ``active_leases[task_id]`` had been deleted by the
        # completion path. Moved upstream so the window actually
        # covers the work it's meant to cover.
        #
        # ``status != "completed"`` — normal progress milestone.
        # Call ``renew_lease`` which uses the progressive-timeout
        # curve based on progress%.
        if (
            hasattr(state, "lease_manager")
            and state.lease_manager
            and status != "completed"
        ):
            renewed_lease = await state.lease_manager.renew_lease(
                task_id, progress, message
            )
            if renewed_lease:
                logger.info(
                    f"Renewed lease for task {task_id} "
                    f"(expires: {renewed_lease.lease_expires.isoformat()})"
                )
            else:
                # No active lease — check whether the task is already DONE
                # before recreating.  A stale agent reporting intermediate
                # progress after another agent completed the task must not
                # reopen the lease; doing so creates an orphaned watchdog
                # that expires and turns the finished task into a zombie.
                task_obj = next(
                    (t for t in state.project_tasks if t.id == task_id),
                    None,
                )
                if task_obj is not None and task_obj.status in {
                    "done",
                    "completed",
                }:
                    logger.warning(
                        f"Stale agent {agent_id} reported progress on "
                        f"task {task_id} which is already DONE. "
                        "Skipping lease recreation to prevent zombie."
                    )
                else:
                    new_lease = await state.lease_manager.create_lease(
                        task_id, agent_id, task_obj
                    )
                    logger.info(
                        f"Recreated lease for task {task_id} "
                        f"after recovery (expires: "
                        f"{new_lease.lease_expires.isoformat()})"
                    )

        # Log response
        conversation_logger.log_worker_message(
            agent_id,
            "from_pm",
            f"Progress update received: {status} at {progress}%",
            {"acknowledged": True, **project_context},
        )

        # DON'T refresh after task updates - causes race condition with Kanban
        # Parent task completion updates Kanban asynchronously, refresh may
        # fetch stale data and overwrite in-memory DONE status, causing tasks
        # to revert to TODO. Let refresh happen on next request_next_task
        # instead. NOTE: This may affect README documentation task visibility -
        # monitor for that

        # Return deferred merge conflict now that kanban is finalized (P1 fix).
        # All state updates (kanban DONE, lease cleared, memory recorded) have
        # completed — safe to surface the merge error to the caller.
        if _deferred_merge_failure is not None:
            # PR #653 path — the task was just marked BLOCKED in kanban
            # because the worktree merge to main failed. Release file
            # locks so a future task can claim the same file.
            _release_locks_on_exit = True
            return _deferred_merge_failure

        # If the validation retry ceiling was hit, surface the
        # escalation details on the success response. The task has
        # already been routed through the full completion path
        # (kanban DONE, memory recorded, lease cleared, branch
        # merged) so the agent can move on, but the escalation
        # annotation tells observers the validator's complaints
        # were overridden.
        if validation_escalated and escalation_payload is not None:
            # Task is DONE in kanban (escalation routes through the
            # full completion path). Release file locks.
            _release_locks_on_exit = True
            return {
                "success": True,
                "message": ("Progress updated successfully (validation escalated)"),
                **escalation_payload,
            }

        # Stub debt check: warn when completed task's output files still
        # contain placeholder markers (data-stub=, // REPLACE this stub).
        # Non-blocking — completion proceeds; agents must resolve warnings.
        if status == "completed" and _project_root:
            _task_obj = next((t for t in state.project_tasks if t.id == task_id), None)
            if _task_obj and getattr(_task_obj, "output_paths", None):
                from pathlib import Path as _ScanPath

                from src.marcus_mcp.coordinator.stub_scanner import scan_output_paths

                _stub_findings = scan_output_paths(
                    _task_obj.output_paths, _ScanPath(_project_root)
                )
                if _stub_findings:
                    # Task is DONE in kanban — warnings are non-blocking.
                    # Release file locks.
                    _release_locks_on_exit = True
                    return {
                        "success": True,
                        "message": "Progress updated successfully",
                        "stub_warnings": _stub_findings,
                        "stub_warning_message": (
                            f"Task completed but "
                            f"{len(_stub_findings)} output file(s) still "
                            "contain stub markers (data-stub= or "
                            "// REPLACE this stub). Replace each placeholder "
                            "with a real implementation."
                        ),
                    }

        # Emit task_completed telemetry only when this report was a
        # completion (Marcus #416, Stage 3 of #9).  Look up the task
        # object so the helper can read its labels for the phase
        # bucket.  Helper swallows all errors internally — no extra
        # try/except needed.
        if status == "completed":
            from src.telemetry.events import fire_task_completed

            completed_task = next(
                (
                    t
                    for t in (getattr(state, "project_tasks", None) or [])
                    if getattr(t, "id", None) == task_id
                ),
                None,
            )
            fire_task_completed(completed_task)

        # #206 MVP Phase 4: normal success path. ``status == "completed"``
        # routes through the full completion pipeline (kanban DONE);
        # ``"blocked"`` writes BLOCKED to kanban earlier in the
        # function. Either way the task is terminal and locks should
        # release. Actual release is in the ``finally:`` block so the
        # SAME path covers early returns (deferred merge failure,
        # validation escalation, stub warnings) and the outer except.
        if status in ("completed", "blocked"):
            _release_locks_on_exit = True

        _timer.mark("completion_processing")
        _timer.mark("total")
        logger.info(
            "report_task_progress timing: "
            f"agent={agent_id} task_id={task_id} status={status} "
            f"progress={progress} "
            f"total_ms={_timer.total_ms()} "
            f"phases={_timer.to_phase_durations()}"
        )
        return {"success": True, "message": "Progress updated successfully"}

    except Exception as e:
        # Atomicity guarantee for the escalation path (review of
        # PR #337 post-Codex): if the retry ceiling was hit during
        # validation and the completion pipeline raised partway
        # through, the escalation signal would otherwise be lost
        # from the response. Preserve escalation context so the
        # caller can distinguish "validation escalated AND pipeline
        # failed" from "generic completion error." The retry
        # tracker has already recorded the escalation attempt, so
        # the next completion request will hit the ceiling again
        # and re-run the pipeline.

        # #206 MVP Phase 4 (Kaia review fix): if the agent reported a
        # terminal status and we got an exception partway through,
        # release the locks. A briefly-orphaned lock is a worse trade
        # than a permanently-leaked one — the task is already in an
        # ambiguous kanban state and a future caller can re-acquire
        # if needed. For non-terminal statuses (in_progress) keep
        # locks held; the agent will retry.
        if status in ("completed", "blocked"):
            _release_locks_on_exit = True

        if validation_escalated and escalation_payload is not None:
            logger.error(
                f"Completion pipeline error AFTER escalation for "
                f"task {task_id}: {e}. Escalation was recorded; "
                f"task may be in partially-updated kanban state. "
                f"Next completion attempt will re-run the pipeline."
            )
            return _build_escalation_error_response(e, escalation_payload)
        return {"success": False, "error": str(e)}
    finally:
        # #206 MVP Phase 4 (Kaia review fix): single release site for
        # ALL exits — normal success, early returns at terminal-state
        # points, and the outer except. ``_release_locks_on_exit`` is
        # set True at each path where the task is now in DONE or
        # BLOCKED state in kanban; gate-rejection paths (smoke /
        # composer / validation rejection) leave the flag False so
        # the agent's in-progress lock holdings are preserved across
        # the retry. Release is idempotent (FileLockRegistry.release
        # returns 0 for a task that held nothing) and wrapped in a
        # try/except so a release failure can never break completion
        # reporting — at worst a lock leaks until Marcus restarts.
        if _release_locks_on_exit and hasattr(state, "file_lock_registry"):
            try:
                await state.file_lock_registry.release(task_id)
            except Exception as _release_err:  # noqa: BLE001
                logger.warning(
                    "[#206] release failed for task %s: %s",
                    task_id,
                    _release_err,
                )


def _build_escalation_error_response(
    exc: BaseException, escalation_payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge escalation context into an error response.

    Used when the validation retry ceiling was hit (escalation
    decided) but the completion pipeline subsequently raised. The
    returned dict carries both failure context and the escalation
    annotation so downstream consumers can tell the two states
    apart and so the agent knows the task was escalated even
    though the pipeline didn't finish. See the escalation
    atomicity guarantee in ``report_task_progress``.

    Parameters
    ----------
    exc : BaseException
        The exception raised by the completion pipeline.
    escalation_payload : Dict[str, Any]
        The escalation payload assembled at retry-ceiling time.

    Returns
    -------
    Dict[str, Any]
        Error response with escalation context merged in.
    """
    return {
        "success": False,
        "error": str(exc),
        "validation_escalated": True,
        "escalation_pipeline_error": True,
        **escalation_payload,
        "message": (
            f"Validation was escalated after "
            f"{MAX_VALIDATION_RETRIES} failed attempts, but the "
            f"completion pipeline failed: {exc}. Task state may "
            f"be inconsistent — retry completion to re-run the "
            f"pipeline."
        ),
    }


async def report_blocker(
    agent_id: str,
    task_id: str,
    blocker_description: str,
    severity: str,
    state: Any,
    skip_ai_analysis: bool = False,
) -> Dict[str, Any]:
    """
    Report a blocker on a task with AI-powered analysis.

    Uses AI to analyze the blocker and provide actionable suggestions.
    Updates task status and adds detailed documentation.

    Parameters
    ----------
    agent_id : str
        The reporting agent's ID
    task_id : str
        ID of the blocked task
    blocker_description : str
        Detailed description of the blocker
    severity : str
        Blocker severity (low, medium, high)
    state : Any
        Marcus server state instance
    skip_ai_analysis : bool, default=False
        If True, skip Marcus's AI analysis (use when blocker_description
        already contains WorkAnalyzer's validation advice)

    Returns
    -------
    Dict[str, Any]
        Dict with AI suggestions and success status
    """
    # Get project/board context
    project_context = await get_project_board_context(state)

    # Log blocker report
    conversation_logger.log_worker_message(
        agent_id,
        "to_pm",
        f"Reporting blocker: {blocker_description}",
        {"task_id": task_id, "severity": severity, **project_context},
    )

    try:
        # Initialize kanban if needed
        await state.initialize_kanban()

        # Guard 1: DONE tasks are immutable — stale agents cannot revert them.
        # This fixes the snake_game-v10 bug where a lease-expired agent called
        # report_blocker after the new holder already completed the task.
        current_task = await state.kanban_client.get_task_by_id(task_id)
        if current_task is not None:
            task_status_val = current_task.status
            if task_status_val in {TaskStatus.DONE, "done", "completed"}:
                logger.warning(
                    f"report_blocker rejected: agent {agent_id} tried to block "
                    f"task {task_id} which is already {task_status_val!r}. "
                    "DONE tasks are immutable."
                )
                return {
                    "success": False,
                    "status": "task_already_complete",
                    "message": (
                        f"Task {task_id} is already complete — cannot report "
                        "a blocker on a finished task. Request your next task."
                    ),
                }

        # Guard 2: Reject blockers from agents who no longer hold the lease.
        # A lease-expired agent must not corrupt work done by the recovery holder.
        if hasattr(state, "lease_manager") and state.lease_manager:
            active_lease = state.lease_manager.active_leases.get(task_id)
            if active_lease is not None and active_lease.agent_id != agent_id:
                logger.warning(
                    f"report_blocker rejected: agent {agent_id} does not hold "
                    f"the lease for task {task_id} (current holder: "
                    f"{active_lease.agent_id}). Request your next task."
                )
                return {
                    "success": False,
                    "status": "not_task_holder",
                    "message": (
                        f"Task {task_id} is held by {active_lease.agent_id}, "
                        f"not {agent_id}. Your lease expired — request your "
                        "next task to continue contributing."
                    ),
                }

        # Log Marcus thinking
        log_thinking(
            "marcus",
            f"Analyzing blocker from {agent_id}",
            {
                "task_id": task_id,
                "severity": severity,
                "description": blocker_description,
            },
        )

        # Use AI to analyze the blocker and suggest solutions
        # (skip if WorkAnalyzer already provided validation advice)
        if skip_ai_analysis:
            suggestions = blocker_description  # Use WorkAnalyzer's advice directly
        else:
            agent = state.agent_status.get(agent_id)
            task = current_task  # already fetched above

            suggestions = await state.ai_engine.analyze_blocker(
                task_id, blocker_description, severity, agent, task
            )

        # Update task status
        await state.kanban_client.update_task(
            task_id, {"status": TaskStatus.BLOCKED, "blocker": blocker_description}
        )

        # Release coordination state — a blocked task is terminal for the
        # current agent. Without this, the agent stays bound to a task it
        # cannot make progress on, eating its assignment slot and accruing
        # lease renewals against work it has explicitly disclaimed.
        # Status changes (DONE/BLOCKED) must always release coordination
        # state independently of correctness checks (decoupling per
        # Simon decision 011b3fad).
        if agent_id in state.agent_status:
            agent = state.agent_status[agent_id]
            agent.current_tasks = []
            if agent_id in state.agent_tasks:
                del state.agent_tasks[agent_id]
            if hasattr(state, "assignment_persistence"):
                await state.assignment_persistence.remove_assignment(agent_id)
            if hasattr(state, "lease_manager") and state.lease_manager:
                if task_id in state.lease_manager.active_leases:
                    del state.lease_manager.active_leases[task_id]
                    logger.info(
                        f"Released lease for blocked task {task_id} "
                        f"(agent {agent_id})"
                    )

        # Record in active experiment if one is running
        from src.experiments.live_experiment_monitor import get_active_monitor

        monitor = get_active_monitor()
        if monitor and monitor.is_running:
            monitor.record_blocker(
                agent_id=agent_id,
                task_id=task_id,
                description=blocker_description,
                severity=severity,
            )

        # Add detailed comment
        comment = f"🚫 BLOCKER ({severity.upper()})\n"
        comment += f"Reported by: {agent_id}\n"
        comment += f"Description: {blocker_description}\n\n"
        comment += f"📋 AI Suggestions:\n{suggestions}"

        await state.kanban_client.add_comment(task_id, comment)

        # Log Marcus decision
        conversation_logger.log_pm_decision(
            decision="Acknowledge blocker and provide suggestions",
            rationale="Help agent overcome the blocker with AI guidance",
            confidence_score=0.8,
            decision_factors={
                "severity": severity,
                "has_suggestions": bool(suggestions),
            },
        )

        # Log response
        conversation_logger.log_worker_message(
            agent_id,
            "from_pm",
            "Blocker acknowledged. Suggestions provided.",
            {"suggestions": suggestions, "severity": severity, **project_context},
        )

        # Emit task_blocked telemetry (Marcus #416, Stage 3 of #9).
        # Ships severity + classified type only — the description
        # text never leaves the machine.
        from src.telemetry.events import fire_task_blocked

        fire_task_blocked(severity=severity, blocker_description=blocker_description)

        return {
            "success": True,
            "suggestions": suggestions,
            "message": "Blocker reported and suggestions provided",
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


# Helper functions for task assignment


def _scope_tasks_to_project(tasks: List[Task], project_id: str) -> List[Task]:
    """
    Filter a task list to only those belonging to the given project.

    Agents are ephemeral — one project, then terminated (GH-389).  A missing
    ``project_id`` is a misconfiguration and raises immediately so the bug is
    visible during development rather than silently producing "no tasks".

    Tasks whose own ``project_id`` is ``None`` are legacy rows created before
    per-project scoping and are always included so pre-existing single-project
    boards remain functional.

    Parameters
    ----------
    tasks : List[Task]
        Full list of project tasks from server state.
    project_id : str
        The project scope to enforce.  Must be non-empty — raises
        ``ValueError`` if falsy.

    Returns
    -------
    List[Task]
        Tasks whose ``project_id`` matches or is ``None`` (legacy).

    Raises
    ------
    ValueError
        When ``project_id`` is empty or ``None``.
    """
    if not project_id:
        raise ValueError(
            "Agent has no project_id — agents are ephemeral and must register "
            "with a project. This is a misconfiguration, not a runtime condition."
        )
    return [t for t in tasks if t.project_id is None or t.project_id == project_id]


async def find_optimal_task_for_agent(agent_id: str, state: Any) -> Optional[Task]:
    """
    Find the best task for an agent, prioritizing subtasks.

    Checks for available subtasks first, then falls back to regular task
    assignment if no subtasks are available.

    Parameters
    ----------
    agent_id : str
        The requesting agent's ID
    state : Any
        Marcus server state instance

    Returns
    -------
    Optional[Task]
        The best task for the agent (may be a subtask converted to Task),
        or None if no suitable task found
    """
    # Import subtask integration helper
    from src.marcus_mcp.coordinator.task_assignment_integration import (
        find_optimal_task_with_subtasks,
    )

    # Use integrated finder that checks subtasks first
    return await find_optimal_task_with_subtasks(
        agent_id, state, _find_optimal_task_original_logic
    )


async def _find_optimal_task_original_logic(
    agent_id: str, state: Any
) -> Optional[Task]:
    """
    Original task assignment logic (used as fallback when no subtasks available).

    Find the best task for an agent using AI-powered analysis.

    Parameters
    ----------
    agent_id : str
        The requesting agent's ID
    state : Any
        Marcus server state instance

    Returns
    -------
    Optional[Task]
        The best task for the agent, or None if no suitable task found
    """
    # Get lock with proper event loop binding
    lock = state.assignment_lock  # This property creates lock if needed
    async with lock:
        # GH-388: Scope tasks to agent's registered project so lingering
        # agents from completed experiments cannot steal tasks from a newly
        # created project.  _scope_tasks_to_project is a pure filter; it
        # does NOT modify state.project_tasks (shared state).
        # Raises ValueError if the agent has no project_id — misconfiguration.
        agent_project_id: str = getattr(state, "agent_project_map", {}).get(
            agent_id, ""
        )
        scoped_tasks = _scope_tasks_to_project(
            state.project_tasks if state.project_tasks else [], agent_project_id
        )

        # Initialize detailed tracking
        filtering_stats = {
            "total_tasks": len(scoped_tasks),
            "todo_status": 0,
            "already_assigned": 0,
            "board_assigned": 0,
            "incomplete_dependencies": 0,
            "project_success_filtered": 0,
            "phase_restrictions": 0,
            "deployment_deprioritized": 0,
            "ai_safety_filtered": 0,
            "skills_mismatch": 0,
            "final_available": 0,
        }
        agent = state.agent_status.get(agent_id)

        if not agent:
            return None

        if not state.project_state:
            return None

        # Get available tasks
        assigned_task_ids = [a.task_id for a in state.agent_tasks.values()]
        persisted_assigned_ids = (
            await state.assignment_persistence.get_all_assigned_task_ids()
        )
        all_assigned_ids = (
            set(assigned_task_ids) | persisted_assigned_ids | state.tasks_being_assigned
        )

        # Get completed task IDs for dependency checking
        completed_task_ids = {t.id for t in scoped_tasks if t.status == TaskStatus.DONE}

        # Build slug-to-ID mapping for dependency resolution
        # Bundled design tasks are created with slug IDs like
        # "design_productivity_tools" but get replaced with numeric Planka IDs
        # when synced to the board. Dependencies still reference the slug, so
        # we need to map slug → numeric ID.
        slug_to_id: dict[str, str] = {}
        for t in scoped_tasks:
            # Look for Design tasks - they have labels like:
            # ['design', 'architecture', 'productivity tools']
            if (
                t.name
                and "Design" in t.name
                and hasattr(t, "labels")
                and t.labels
                and len(t.labels) >= 3
            ):
                # Extract domain from labels
                # (last non-design/architecture label)
                domain_labels = [
                    label
                    for label in t.labels
                    if label.lower() not in ["design", "architecture"]
                ]
                if domain_labels:
                    # Create slug from domain label:
                    # "productivity tools" → "design_productivity_tools"
                    domain = domain_labels[-1]
                    slug = f"design_{domain.lower().replace(' ', '_')}"
                    slug_to_id[slug] = str(t.id)
                    logger.debug(f"Mapped slug '{slug}' → task ID {t.id} ('{t.name}')")

        logger.info(
            f"Built slug-to-ID mapping with {len(slug_to_id)} entries: "
            f"{dict(list(slug_to_id.items())[:5])}"  # Show first 5
        )

        # Filter tasks: TODO, not assigned, and all dependencies completed
        available_tasks = []
        for t in scoped_tasks:
            if t.status != TaskStatus.TODO:
                filtering_stats["todo_status"] += 1
                continue
            if t.id in all_assigned_ids:
                filtering_stats["already_assigned"] += 1
                continue

            # Skip tasks owned on the board (assigned_to is set).
            # Design tasks are assigned to "Marcus" and handled
            # internally. Agent tasks get assigned_to set on
            # assignment. Recovery clears assigned_to to release
            # tasks back to the pool.
            if t.assigned_to:
                filtering_stats["board_assigned"] += 1
                logger.debug(
                    f"Skipping '{t.name}' — assigned to " f"'{t.assigned_to}' on board"
                )
                continue

            # GH-XX: Skip parent tasks that have subtasks
            # Parent tasks should not be assigned - only their subtasks
            if hasattr(state, "subtask_manager") and state.subtask_manager:
                has_subtasks_result = state.subtask_manager.has_subtasks(
                    t.id, state.project_tasks
                )

                # CRITICAL DEBUG: Log subtask check for every task
                logger.debug(
                    f"🔍 SUBTASK CHECK for task '{t.name}' (ID: {t.id}): "
                    f"has_subtasks={has_subtasks_result}"
                )

                if has_subtasks_result:
                    logger.debug(
                        f"Skipping parent task '{t.name}' - "
                        "has subtasks that should be assigned instead"
                    )
                    continue

            # Check dependencies - resolve slugs to actual IDs first
            deps = t.dependencies or []
            resolved_deps = []
            for dep_id in deps:
                # If it's a slug, try to resolve it; otherwise use as-is
                if dep_id in slug_to_id:
                    resolved_deps.append(slug_to_id[dep_id])
                else:
                    resolved_deps.append(dep_id)

            all_deps_complete = all(
                dep_id in completed_task_ids for dep_id in resolved_deps
            )

            if not all_deps_complete:
                filtering_stats["incomplete_dependencies"] += 1
                # Log which dependencies are not complete
                incomplete_deps = [
                    dep_id
                    for dep_id in resolved_deps
                    if dep_id not in completed_task_ids
                ]
                logger.info(
                    f"Task '{t.name}' has incomplete dependencies: {incomplete_deps} "
                    f"(original: {deps}, resolved: {resolved_deps})"
                )
                continue

            # CRITICAL: If this is a subtask, also check parent's dependencies
            # Subtasks should not start until their parent's dependencies are met
            if t.is_subtask and t.parent_task_id:
                parent_task = next(
                    (p for p in state.project_tasks if p.id == t.parent_task_id), None
                )
                if parent_task:
                    parent_deps = parent_task.dependencies or []
                    parent_resolved_deps = []
                    for dep_id in parent_deps:
                        if dep_id in slug_to_id:
                            parent_resolved_deps.append(slug_to_id[dep_id])
                        else:
                            parent_resolved_deps.append(dep_id)

                    parent_deps_complete = all(
                        dep_id in completed_task_ids for dep_id in parent_resolved_deps
                    )

                    if not parent_deps_complete:
                        incomplete_parent_deps = [
                            dep_id
                            for dep_id in parent_resolved_deps
                            if dep_id not in completed_task_ids
                        ]
                        logger.info(
                            f"Subtask '{t.name}' blocked: parent task "
                            f"'{parent_task.name}' has incomplete dependencies: "
                            f"{incomplete_parent_deps}"
                        )
                        filtering_stats["incomplete_dependencies"] += 1
                        continue

            # #206 MVP: skip tasks whose declared write files are
            # currently held by another in-progress task. The
            # registry's ``any_held`` is read-only and lock-free —
            # used here as a pre-filter so the more expensive
            # instruction generation and ``try_acquire`` step only
            # runs for tasks that look claimable. The final
            # commit-time ``try_acquire`` (see request_next_task)
            # serves as the atomic claim against a race with another
            # agent who passed the same filter on the same poll.
            if hasattr(state, "file_lock_registry"):
                declared = (t.source_context or {}).get("declared_files", []) or []
                if declared and state.file_lock_registry.any_held(
                    declared, project_id=agent_project_id
                ):
                    blocker_file = next(
                        (
                            f
                            for f in declared
                            if state.file_lock_registry.held_by(
                                f, project_id=agent_project_id
                            )
                            is not None
                        ),
                        None,
                    )
                    if blocker_file:
                        holder = state.file_lock_registry.held_by(
                            blocker_file, project_id=agent_project_id
                        )
                        if holder is not None:
                            logger.info(
                                "[#206] Skipping task %s for %s — file %s "
                                "(project %s) held by task %s (agent %s)",
                                t.id,
                                agent_id,
                                blocker_file,
                                agent_project_id or "<default>",
                                holder.task_id,
                                holder.agent_id,
                            )
                    filtering_stats["file_lock_blocked"] = (
                        filtering_stats.get("file_lock_blocked", 0) + 1
                    )
                    continue

            available_tasks.append(t)

        # Special handling for README documentation
        # Calculate project completion percentage
        # Exclude README documentation tasks from the calculation since they should only
        # be assigned after other tasks are complete
        total_non_doc_tasks = len(
            [
                t
                for t in scoped_tasks
                if "README" not in t.name
                and not any(
                    label in (t.labels or [])
                    for label in ["documentation", "final", "verification"]
                )
            ]
        )
        completed_non_doc_tasks = len(
            [
                t
                for t in scoped_tasks
                if t.status == TaskStatus.DONE
                and "README" not in t.name
                and not any(
                    label in (t.labels or [])
                    for label in ["documentation", "final", "verification"]
                )
            ]
        )

        # Special case: If README documentation is the only task left, make it available
        readme_doc_tasks = [t for t in available_tasks if "README" in t.name]
        if readme_doc_tasks and len(available_tasks) == len(readme_doc_tasks):
            # All available tasks are README documentation tasks, don't filter them
            logger.debug(
                "README documentation is the only available task - making it assignable"
            )
        elif total_non_doc_tasks > 0:
            completion_percentage = (
                completed_non_doc_tasks / total_non_doc_tasks
            ) * 100

            # Filter out README documentation tasks if not nearly complete
            # Using 90% threshold since some tasks might be blocked
            if completion_percentage < 90:
                available_tasks = [t for t in available_tasks if "README" not in t.name]
                logger.debug(
                    f"Filtering out README documentation tasks - project only "
                    f"{completion_percentage:.1f}% complete"
                )

        # Apply phase-based task filtering
        from src.core.phase_dependency_enforcer import PhaseDependencyEnforcer
        from src.integrations.enhanced_task_classifier import EnhancedTaskClassifier

        phase_enforcer = PhaseDependencyEnforcer()
        classifier = EnhancedTaskClassifier()

        # System/metadata labels that Marcus adds internally (e.g. for Cato
        # visualisation).  These are NOT feature identifiers and must not be
        # used to group tasks into the same phase-enforcement feature group.
        # Without this exclusion, all pre-fork foundation tasks (which share
        # labels=["pre-fork"]) would be treated as one feature and serialised
        # by the Design→Infrastructure phase rule, preventing second agents
        # from receiving any foundation tasks.  (GH: v82 swim-lane bug)
        _SYSTEM_LABELS: frozenset[str] = frozenset(
            {"pre-fork", "foundation", "pre_fork_synthesis"}
        )

        # Get in-progress tasks to check phase constraints
        in_progress_task_ids = {
            t.id for t in scoped_tasks if t.status == TaskStatus.IN_PROGRESS
        }

        # Further filter available tasks based on phase constraints
        phase_eligible_tasks = []
        for task in available_tasks:
            task_type = classifier.classify(task)
            task_phase = phase_enforcer._get_task_phase(task_type)

            # Check if this phase is allowed given current in-progress tasks
            phase_allowed = True

            # First check against in-progress tasks
            for ip_task_id in in_progress_task_ids:
                ip_task = next((t for t in scoped_tasks if t.id == ip_task_id), None)
                if ip_task:
                    ip_type = classifier.classify(ip_task)
                    ip_phase = phase_enforcer._get_task_phase(ip_type)

                    # Check if tasks share the same FEATURE (by labels).
                    # Exclude system labels so "pre-fork" and similar internal
                    # tags do not create false feature groupings.
                    if task.labels and ip_task.labels:
                        task_feature_labels = set(task.labels) - _SYSTEM_LABELS
                        ip_feature_labels = set(ip_task.labels) - _SYSTEM_LABELS
                        shared_labels = task_feature_labels & ip_feature_labels
                        if shared_labels:
                            # Same feature - check phase order
                            if phase_enforcer._should_depend_on_phase(
                                task_phase, ip_phase
                            ):
                                # This task's phase should wait for
                                # in-progress phase
                                phase_allowed = False
                                logger.debug(
                                    f"Task '{task.name}' "
                                    f"({task_phase.value}) blocked by "
                                    f"in-progress task '{ip_task.name}' "
                                    f"({ip_phase.value}) in same feature"
                                )
                                break

            # Also check if all required earlier phases have been completed
            if phase_allowed and task.labels:
                # Only consider non-system labels as feature identifiers
                task_feature_labels = set(task.labels) - _SYSTEM_LABELS
                if task_feature_labels:
                    # Get all completed tasks in the same feature
                    feature_completed_tasks = [
                        t
                        for t in scoped_tasks
                        if t.status == TaskStatus.DONE
                        and t.labels
                        and (set(t.labels) - _SYSTEM_LABELS) & task_feature_labels
                    ]

                    # Check which phases have been completed
                    completed_phases = set()
                    for comp_task in feature_completed_tasks:
                        comp_type = classifier.classify(comp_task)
                        comp_phase = phase_enforcer._get_task_phase(comp_type)
                        completed_phases.add(comp_phase)

                    # Check if all required earlier phases are complete
                    required_phases = [
                        p
                        for p in phase_enforcer.PHASE_ORDER
                        if p.value < task_phase.value
                    ]
                    for req_phase in required_phases:
                        if req_phase not in completed_phases:
                            # Check if there are any tasks of this phase
                            phase_exists = any(
                                phase_enforcer._get_task_phase(classifier.classify(t))
                                == req_phase
                                for t in scoped_tasks
                                if t.labels
                                and (set(t.labels) - _SYSTEM_LABELS)
                                & task_feature_labels
                            )

                            if phase_exists:
                                phase_allowed = False
                                logger.info(
                                    f"Task '{task.name}' "
                                    f"({task_phase.value}) blocked - waiting "
                                    f"for {req_phase.name} phase to complete "
                                    f"in same feature. Task labels: "
                                    f"{task.labels}, Required phase: "
                                    f"{req_phase.name}"
                                )
                                break

            if phase_allowed:
                phase_eligible_tasks.append(task)

        # Log filtering results
        if len(available_tasks) != len(phase_eligible_tasks):
            logger.info(
                f"Phase enforcement filtered tasks: {len(available_tasks)} -> "
                f"{len(phase_eligible_tasks)} eligible"
            )

        available_tasks = phase_eligible_tasks

        # Further filter to deprioritize deployment tasks
        # Separate deployment and non-deployment tasks
        deployment_keywords = ["deploy", "release", "production", "launch", "rollout"]
        non_deployment_tasks = []
        deployment_tasks = []

        for task in available_tasks:
            task_name_lower = task.name.lower()
            task_labels_lower = [label.lower() for label in (task.labels or [])]

            is_deployment = any(
                keyword in task_name_lower or keyword in " ".join(task_labels_lower)
                for keyword in deployment_keywords
            )

            if is_deployment:
                deployment_tasks.append(task)
            else:
                non_deployment_tasks.append(task)

        # Prefer non-deployment tasks; only use deployment if nothing else available
        available_tasks = (
            non_deployment_tasks if non_deployment_tasks else deployment_tasks
        )

        if not available_tasks:
            return None

        # Use AI-powered task selection if AI engine is available
        if state.ai_engine:
            try:
                optimal_task = await find_optimal_task_for_agent_ai_powered(
                    agent_id=agent_id,
                    agent_status=agent.__dict__,
                    project_tasks=scoped_tasks,
                    available_tasks=available_tasks,
                    assigned_task_ids=all_assigned_ids,
                    ai_engine=state.ai_engine,
                )

                if optimal_task:
                    state.tasks_being_assigned.add(optimal_task.id)
                    # Track in server for cleanup on disconnect
                    if hasattr(state, "_active_operations"):
                        state._active_operations.add(
                            f"task_assignment_{optimal_task.id}"
                        )
                    return optimal_task
            except Exception as e:
                # Log error using log_pm_thinking instead
                conversation_logger.log_pm_thinking(
                    f"AI task assignment failed, falling back to basic: {e}"
                )

        # Fallback to basic assignment if AI fails
        return await find_optimal_task_basic(agent_id, available_tasks, state)


async def find_optimal_task_basic(
    agent_id: str, available_tasks: List[Task], state: Any
) -> Optional[Task]:
    """
    Find optimal task using basic assignment logic (fallback).

    Parameters
    ----------
    agent_id : str
        The requesting agent's ID
    available_tasks : List[Task]
        List of available tasks to choose from
    state : Any
        Marcus server state instance

    Returns
    -------
    Optional[Task]
        The best task for the agent, or None if no suitable task found
    """
    agent = state.agent_status.get(agent_id)
    if not agent:
        return None

    best_task = None
    best_score: float = -1.0

    # Check if this is a deployment task
    deployment_keywords = ["deploy", "release", "production", "launch", "rollout"]

    for task in available_tasks:
        # Calculate skill match score
        skill_score: float = 0.0
        if agent.skills and task.labels:
            matching_skills = set(agent.skills) & set(task.labels)
            skill_score = len(matching_skills) / len(task.labels) if task.labels else 0

        # Priority score
        priority_score = {
            Priority.URGENT: 1.0,
            Priority.HIGH: 0.8,
            Priority.MEDIUM: 0.5,
            Priority.LOW: 0.2,
        }.get(task.priority, 0.5)

        # Deployment penalty - reduce score for deployment tasks
        task_name_lower = task.name.lower()
        task_labels_lower = [label.lower() for label in (task.labels or [])]
        is_deployment = any(
            keyword in task_name_lower or keyword in " ".join(task_labels_lower)
            for keyword in deployment_keywords
        )
        deployment_penalty = 0.5 if is_deployment else 1.0

        # Combined score with deployment penalty
        total_score = (
            (skill_score * 0.6) + (priority_score * 0.4)
        ) * deployment_penalty

        if total_score > best_score:
            best_score = total_score
            best_task = task

    if best_task:
        state.tasks_being_assigned.add(best_task.id)
        # Track in server for cleanup on disconnect
        if hasattr(state, "_active_operations"):
            state._active_operations.add(f"task_assignment_{best_task.id}")

    return best_task


async def get_all_board_tasks(
    board_id: str, project_id: str, state: Any
) -> Dict[str, Any]:
    """
    Get all tasks from a specific Planka board.

    This tool fetches all tasks directly from a Planka board,
    useful for validation and inspection purposes.

    Parameters
    ----------
    board_id : str
        The Planka board ID to fetch tasks from
    project_id : str
        The Planka project ID
    state : Any
        Marcus server state instance

    Returns
    -------
    Dict[str, Any]
        Dictionary containing:
        - success: bool
        - tasks: List of task dictionaries from Planka
        - count: Number of tasks retrieved
    """
    try:
        from src.integrations.providers.planka_kanban import PlankaKanban

        provider = PlankaKanban(config={})
        await provider.connect()

        tasks = await provider.get_all_tasks()

        return {
            "success": True,
            "tasks": tasks,
            "count": len(tasks),
            "board_id": board_id,
            "project_id": project_id,
        }

    except Exception as e:
        logger.error(f"Error fetching board tasks: {e}")
        return {"success": False, "error": str(e), "tasks": [], "count": 0}


async def unassign_task(
    task_id: str, agent_id: Optional[str], state: Any
) -> Dict[str, Any]:
    """
    Unassign a task from an agent and reset it to TODO status.

    This tool manually breaks task assignments, useful for recovering from
    stuck assignments when agents crash, disconnect, or get stuck.

    Parameters
    ----------
    task_id : str
        The task ID to unassign
    agent_id : Optional[str]
        The agent ID (optional - will be auto-detected if not provided)
    state : Any
        Marcus server state instance

    Returns
    -------
    Dict[str, Any]
        Dictionary containing:
        - success: bool
        - message: str
        - agent_id: str (the agent it was unassigned from)
        - task_id: str
    """
    # Get project/board context
    project_context = await get_project_board_context(state)

    try:
        # Initialize kanban if needed
        await state.initialize_kanban()

        # Find which agent has this task if not provided
        if not agent_id:
            # Check in-memory assignments
            for a_id, assignment in state.agent_tasks.items():
                if assignment.task_id == task_id:
                    agent_id = a_id
                    break

            # Check agent current_tasks
            if not agent_id:
                for a_id, agent in state.agent_status.items():
                    if agent.current_tasks and any(
                        t.id == task_id for t in agent.current_tasks
                    ):
                        agent_id = a_id
                        break

        if not agent_id:
            # Task not currently assigned
            logger.warning(f"Task {task_id} is not currently assigned to any agent")
            return {
                "success": False,
                "error": f"Task {task_id} is not currently assigned",
                "task_id": task_id,
            }

        # Log the unassignment request
        conversation_logger.log_pm_decision(
            decision=f"Manually unassign task {task_id} from {agent_id}",
            rationale="Manual intervention to unstick assignment",
            confidence_score=1.0,
            decision_factors={"manual_override": True},
        )

        logger.info(f"Unassigning task {task_id} from agent {agent_id}")

        # 1. Remove from state.agent_tasks
        if agent_id in state.agent_tasks:
            del state.agent_tasks[agent_id]
            logger.debug(f"Removed task from state.agent_tasks for {agent_id}")

        # 2. Clear agent's current_tasks
        if agent_id in state.agent_status:
            agent = state.agent_status[agent_id]
            agent.current_tasks = []
            logger.debug(f"Cleared current_tasks for agent {agent_id}")

        # 3. Remove from assignment_persistence
        await state.assignment_persistence.remove_assignment(agent_id)
        logger.debug(f"Removed from assignment_persistence for {agent_id}")

        # 4. Remove from tasks_being_assigned
        state.tasks_being_assigned.discard(task_id)
        logger.debug(f"Removed task {task_id} from tasks_being_assigned")

        # 5. Remove from active_operations
        if hasattr(state, "_active_operations"):
            state._active_operations.discard(f"task_assignment_{task_id}")
            logger.debug("Removed from _active_operations")

        # 6. Delete lease if exists
        if hasattr(state, "lease_manager") and state.lease_manager:
            if task_id in state.lease_manager.active_leases:
                del state.lease_manager.active_leases[task_id]
                logger.info(f"Removed lease for task {task_id}")

        # 7. Update Kanban: Set status back to TODO, clear assigned_to
        await state.kanban_client.update_task(
            task_id,
            {"status": TaskStatus.TODO, "assigned_to": None, "progress": 0},
        )
        logger.info(f"Updated kanban status to TODO for task {task_id}")

        # Refresh project state
        await state.refresh_project_state()

        # Log conversation event
        conversation_logger.log_worker_message(
            agent_id,
            "from_pm",
            f"Task {task_id} unassigned - reset to TODO",
            {"task_id": task_id, "manual_unassignment": True, **project_context},
        )

        # Log event
        state.log_event(
            "task_unassigned",
            {
                "agent_id": agent_id,
                "task_id": task_id,
                "source": "manual_intervention",
                "target": "marcus",
            },
        )

        return {
            "success": True,
            "message": f"Task {task_id} successfully unassigned from {agent_id}",
            "agent_id": agent_id,
            "task_id": task_id,
        }

    except Exception as e:
        logger.error(f"Error unassigning task {task_id}: {e}", exc_info=True)
        return {"success": False, "error": str(e), "task_id": task_id}
