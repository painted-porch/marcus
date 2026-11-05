"""
Task Management Tools for Marcus MCP.

This module contains tools for task operations in the Marcus system:
- request_next_task: Get optimal task assignment for an agent
- report_task_progress: Update progress on assigned tasks
- report_blocker: Report blockers with AI-powered suggestions
- unassign_task: Manually unassign a task from an agent
"""

import asyncio
import inspect
import json
import logging
import os
import secrets
import subprocess
import threading
import time
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from src.core.ai_powered_task_assignment import find_optimal_task_for_agent_ai_powered

# Issue #629: claimability rules live in src/core/task_claimability.py so
# the availability filter here and the gridlock detector share one rule
# and cannot drift. Aliased to the module-local names used throughout
# this file.
from src.core.lease_invariants import (
    DEFAULT_CHECKPOINT_CADENCE_MINUTES,
    DEFAULT_SILENCE_TIMEOUT_MINUTES,
)
from src.core.models import (
    DefectType,
    Priority,
    RecoveryInfo,
    Task,
    TaskAssignment,
    TaskStatus,
    WorkerStatus,
)
from src.core.task_claimability import build_slug_to_id
from src.core.task_claimability import deps_allow_claim as _deps_allow_claim
from src.core.task_claimability import is_integration_task as _is_integration_task
from src.core.task_claimability import phase_satisfied_by
from src.core.task_classification import get_task_type
from src.integrations.behavior_evidence import (
    behavior_evidence_contract,
    has_behavior_contract,
    judge_behavior_evidence,
)
from src.logging.agent_events import log_agent_event
from src.logging.conversation_logger import conversation_logger, log_thinking
from src.marcus_mcp.coordinator.drain import compute_board_drain
from src.marcus_mcp.tools.context import give_opinion
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

# Seconds an agent waits before re-reporting a completion that the smoke
# gate could not evaluate because of a Marcus-side misconfiguration (the
# task is parked, not rejected). Long enough for an operator to act, short
# enough that every re-report keeps the lease alive under ADR-0012 D3.
GATE_UNAVAILABLE_RETRY_SECONDS = 300
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


# Redo ceiling (issue #627). ``request_task_redo`` lets a downstream agent
# (typically integration verification) send a DONE task back to the board for
# a fresh implementer instead of rewriting a sibling's lane in place. Without
# a cap, a disagreement between the integration agent and implementers could
# ping-pong a task forever. After this many redos the tool refuses and the
# integration agent falls back to fixing in place.
MAX_TASK_REDO_COUNT = 3


# Advisory-blocker ceiling (issue #719). ``report_blocker`` is a help
# channel: a low/medium-severity report returns AI suggestions and leaves
# the task with the agent so it can try them (the worker prompt's
# ERROR_RECOVERY contract — "Don't panic or abandon the task… Continue
# working"). Without a ceiling an agent could ask for help forever while
# holding the lane, so after this many advisory reports on the same task
# the next one is treated as terminal and the task is handed back.
MAX_ADVISORY_BLOCKER_ATTEMPTS = 3
_advisory_blocker_attempts: Dict[str, int] = {}

# The only severities that keep the task with the agent. Everything else —
# "high", "critical", a typo — is treated as terminal so an unrecognised
# value can never silently strand a blocked lane (Codex P2 on PR #720).
ADVISORY_SEVERITIES = frozenset({"low", "medium"})


# Repair-requeue ceiling (the missing middle rung). Marcus had no step
# between "an agent gives up" and "this lane is dead forever": a terminal
# blocker killed the task on the spot, so the work it was meant to produce
# never happened and the only mitigation left was routing around the hole
# (#629). A terminally-blocked task now goes back on the board carrying the
# blocker text and Marcus's suggestions, so a FRESH agent attempts it with
# the previous agent's diagnostic in hand. Only after this many independent
# agents have given up is the lane declared genuinely dead — two agents
# agreeing is evidence; one agent's bad afternoon is not.
#
# This counter is deliberately NOT cleared by ``clear_validation_retry``
# (which recovery calls): it is the loop guard. Clearing it on requeue
# would let a task ping-pong between TODO and blocked forever.
MAX_BLOCKER_REPAIR_ATTEMPTS = 2
_blocker_repair_agents: Dict[str, Set[str]] = {}
# Issue #769: the distinct-agent ceiling above can never be reached when
# the pool is smaller than it (one persistent agent is handed the same
# task back forever, at an instruction-generation call and a blocker-
# analysis call per loop). This is the second, agent-blind bound: the
# total number of times a task may be re-offered after a give-up,
# whoever gives up. It is deliberately NOT a snapshot of who is
# registered right now, because the spawn-per-task runner also shows a
# single agent at give-up time and a snapshot rule would kill lanes that
# the next spawned agent would have repaired (#722).
MAX_BLOCKER_REPAIR_REQUEUES = 3
_blocker_repair_requeues: Dict[str, int] = {}
# The lease epoch (ADR-0012 D11 fencing token) whose terminal give-up has
# already been accepted, per task. A terminal report is accepted once per
# assignment: concurrent duplicates of the same give-up all pass the live-
# lease check (the lease is released only after the board write), so
# without this four overlapping reports could spend the whole budget on one
# real give-up (Codex P2 on PR #770).
_blocker_repair_consumed_epoch: Dict[str, int] = {}

# Tasks whose completed report is currently inside the validation gate.
# The MCP transport can re-send a slow report_task_progress(completed)
# call after a reconnect, so two reports for the same task reach the gate
# concurrently -- before the first marks the task DONE. The DONE guard in
# the completed branch only catches duplicates that arrive AFTER
# completion; this set catches the ones that OVERLAP an in-progress
# validation. asyncio is single-threaded, so the check-and-add in
# report_task_progress is atomic as long as no await separates them.
_completion_in_flight: Set[str] = set()


def _record_repair_attempt(task_id: str, agent_id: str) -> int:
    """Record that ``agent_id`` gave up on ``task_id``; return distinct count.

    Counts DISTINCT agents, not tool calls (Codex P1 on PR #722). After the
    first requeue clears the lease, the ownership guard only rejects a lease
    held by *someone else* — so a stale agent retrying after a lost response
    could otherwise consume the whole budget and mark a lane dead that no
    fresh agent ever attempted. Recording by agent makes repeat calls
    idempotent and matches the semantic the ceiling claims: the lane is dead
    only when independent agents agree it is.

    Parameters
    ----------
    task_id : str
        The task that was given up on.
    agent_id : str
        The agent giving up.

    Returns
    -------
    int
        Number of distinct agents that have given up on this task.
    """
    agents = _blocker_repair_agents.setdefault(task_id, set())
    agents.add(agent_id)
    return len(agents)


def clear_repair_attempts(task_id: str) -> None:
    """Reset a task's repair budget.

    Called when the task actually completes — NOT on lease recovery, which
    must not reset the loop guard.

    Parameters
    ----------
    task_id : str
        The task whose repair record should be reset.
    """
    _blocker_repair_agents.pop(task_id, None)
    _blocker_repair_requeues.pop(task_id, None)
    _blocker_repair_consumed_epoch.pop(task_id, None)


def _record_repair_requeue(task_id: str) -> int:
    """Count one more re-offer of ``task_id`` after a give-up; return the total.

    Parameters
    ----------
    task_id : str
        The task being requeued for repair.

    Returns
    -------
    int
        Total requeues so far for this task, across all agents.
    """
    total = _blocker_repair_requeues.get(task_id, 0) + 1
    _blocker_repair_requeues[task_id] = total
    return total


async def _release_task_file_locks(state: Any, task_id: str, context: str) -> None:
    """Release the #206 file locks a task holds, best-effort.

    Codex P1 on PR #722: the availability filter skips any TODO task whose
    declared files are locked, with no exemption for locks the task itself
    holds — so a requeue that leaves them in place makes the repair task
    permanently unclaimable and can starve siblings sharing those files.
    Both terminal paths must release, exactly as lease recovery and the
    terminal-progress path already do.

    Parameters
    ----------
    state : Any
        Marcus server state (may lack ``file_lock_registry``).
    task_id : str
        Task whose locks should be dropped.
    context : str
        Short label for the log line (e.g. "repair requeue").

    Notes
    -----
    Best-effort and idempotent: ``release`` returns 0 for a task holding
    nothing, and any failure is logged rather than raised — at worst a lock
    leaks until Marcus restarts, which must never cost us the handoff.
    """
    if not hasattr(state, "file_lock_registry"):
        return
    try:
        await state.file_lock_registry.release(task_id)
    except Exception as release_err:  # noqa: BLE001 - never break the handoff
        logger.warning(
            "[#206] lock release failed for task %s during %s: %s",
            task_id,
            context,
            release_err,
        )


def _repair_instructions(
    blocker_description: str,
    suggestions: Any,
    previous_agent: str,
    attempt: int,
) -> str:
    """Render the handoff a repairing agent receives.

    Parameters
    ----------
    blocker_description : str
        What the previous agent said stopped them.
    suggestions : Any
        Marcus's AI analysis of that blocker.
    previous_agent : str
        Agent that gave up.
    attempt : int
        Which repair attempt this is (1-based).

    Returns
    -------
    str
        Multi-line guidance for the next agent.
    """
    return (
        f"🔧 **REPAIR ATTEMPT {attempt}/{MAX_BLOCKER_REPAIR_ATTEMPTS}** — a "
        f"previous agent ({previous_agent}) could not complete this task and "
        f"handed it back.\n\n"
        f"**What stopped them:**\n{blocker_description}\n\n"
        f"**What Marcus suggested:**\n{suggestions}\n\n"
        "You are a different agent with a fresh perspective. Try a different "
        "approach than the one that failed — do not simply repeat it. If the "
        "obstacle is real but workable, work around it and note the "
        "workaround. If you also conclude the task is genuinely impossible "
        "for anyone (a missing credential, an unavailable service), report a "
        "high-severity blocker saying so and what you tried; after "
        f"{MAX_BLOCKER_REPAIR_ATTEMPTS} independent agents agree, Marcus "
        "records the lane as dead."
    )


def _record_blocker_attempt(task_id: str) -> int:
    """Increment and return the advisory-blocker count for a task.

    Parameters
    ----------
    task_id : str
        The task an agent is asking for help on.

    Returns
    -------
    int
        Cumulative advisory reports for this task (1 on the first call).
    """
    count = _advisory_blocker_attempts.get(task_id, 0) + 1
    _advisory_blocker_attempts[task_id] = count
    return count


def clear_blocker_attempts(task_id: str) -> None:
    """Reset a task's advisory-blocker budget.

    Called when a task is recovered or reassigned so an incoming agent is
    not penalised for the previous holder's requests for help.

    Parameters
    ----------
    task_id : str
        The task whose advisory counter should be reset.
    """
    _advisory_blocker_attempts.pop(task_id, None)


# Stamped into a reconciliation card's description so the fence can find
# the card it already opened for a task (ADR-0012 D11).
_RECONCILIATION_MARKER = "<!-- marcus:reconciles task {task_id} -->"

#: Label that identifies a reconciliation card. Its deliverable is a
#: recorded decision rather than code, so the commit gate skips it.
_RECONCILIATION_LABEL = "reconciliation"

#: One lock per contested task around "find a card, else create one".
#: Two overlapping refusals, or a refusal overlapping the replacement's
#: acceptance hook, would otherwise both find nothing and both create a
#: card, leaving two claimable cards deciding the same thing (Codex P2 on
#: PR #794).
_RECONCILIATION_LOCKS: Dict[str, asyncio.Lock] = {}
_RECONCILIATION_LOCKS_GUARD = threading.Lock()

#: One lock per DEPENDENT around the whole handoff sequence: read the
#: provenance, replace the inputs, write the board or roll back, record
#: the provenance. Without it the replacement is visible in shared
#: memory while the board write is still in flight, so a second upstream
#: closing concurrently can see a key the first has removed but not yet
#: committed, claim it, and leave both owning it (Codex P2 on PR #807).
_HANDOFF_LOCKS: Dict[str, asyncio.Lock] = {}
_HANDOFF_LOCKS_GUARD = threading.Lock()


def _handoff_lock(dependent_id: str) -> asyncio.Lock:
    """Return the lock that serialises handoffs into one dependent.

    Parameters
    ----------
    dependent_id : str
        The task whose inputs are being written.

    Returns
    -------
    asyncio.Lock
        A lock unique to this dependent for the life of the process.
    """
    with _HANDOFF_LOCKS_GUARD:
        lock = _HANDOFF_LOCKS.get(dependent_id)
        if lock is None:
            lock = asyncio.Lock()
            _HANDOFF_LOCKS[dependent_id] = lock
        return lock


def _reconciliation_lock(task_id: str) -> asyncio.Lock:
    """Return the lock that serialises card creation for one task.

    Parameters
    ----------
    task_id : str
        The contested task.

    Returns
    -------
    asyncio.Lock
        A lock unique to this task for the life of the process.
    """
    with _RECONCILIATION_LOCKS_GUARD:
        lock = _RECONCILIATION_LOCKS.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            _RECONCILIATION_LOCKS[task_id] = lock
        return lock


def _worktree_path_for_branch(repo_root: str, branch: str) -> Optional[str]:
    """Find the worktree that currently has ``branch`` checked out.

    The session's worktree path is a launcher convention, so asking git
    is more reliable than rebuilding the path. ``git worktree list
    --porcelain`` prints one block per worktree, with a ``branch
    refs/heads/<name>`` line for any worktree that is not detached.

    Parameters
    ----------
    repo_root : str
        Any directory inside the repository.
    branch : str
        Branch name without the ``refs/heads/`` prefix.

    Returns
    -------
    Optional[str]
        Filesystem path of the worktree holding ``branch``, or None when
        no worktree has it checked out (or git is unavailable).
    """
    try:
        listing = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=5,
        )
        if listing.returncode != 0:
            return None
        current: Optional[str] = None
        for line in listing.stdout.splitlines():
            if line.startswith("worktree "):
                current = line[len("worktree ") :].strip()
            elif line.startswith("branch "):
                ref = line[len("branch ") :].strip()
                if ref == f"refs/heads/{branch}" and current:
                    return current
        return None
    except Exception as exc:  # noqa: BLE001 - never fail a claim over this
        logger.debug(f"Could not list worktrees in {repo_root}: {exc}")
        return None


def _commit_exists(repo: Any, rev: str) -> bool:
    """Report whether ``rev`` resolves to a commit in ``repo``.

    Used before scoping anything to a baseline: a sha that git cannot
    resolve (a stale record, a rewritten branch) must degrade to
    branch-level behaviour rather than silently matching nothing.

    Parameters
    ----------
    repo : Any
        Repository path (str or Path) to run git in.
    rev : str
        Revision to resolve.

    Returns
    -------
    bool
        True when ``rev`` names a commit object in this repository.
    """
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return probe.returncode == 0 and bool(probe.stdout.strip())
    except Exception:  # noqa: BLE001 - treat git trouble as "unknown"
        return False


def _card_commit_range(
    repo: Any, branch: str, baseline_commit: str
) -> Optional[List[str]]:
    """List the commits this card added to ``branch``, oldest first.

    The range is "reachable from the branch, but from neither the
    baseline nor ``main``". Excluding ``main`` keeps already-merged work
    out of the replay; excluding the baseline keeps every card the
    session worked on **before** this one out of it, which is the whole
    point of issue #730.

    Parameters
    ----------
    repo : Any
        Repository path (str or Path) to run git in.
    branch : str
        The session's branch, ``marcus/{agent_id}``.
    baseline_commit : str
        Branch tip captured when the card was claimed.

    Returns
    -------
    Optional[List[str]]
        Commit shas in apply order, possibly empty. None means the range
        could not be computed (unresolvable baseline or a git failure),
        so the caller should fall back to a branch-level merge.
    """
    if not _commit_exists(repo, baseline_commit):
        return None
    try:
        listing = subprocess.run(
            [
                "git",
                "rev-list",
                "--reverse",
                "--topo-order",
                branch,
                "--not",
                baseline_commit,
                "main",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if listing.returncode != 0:
            return None
        return listing.stdout.split()
    except Exception:  # noqa: BLE001 - caller falls back
        return None


def _capture_baseline_commit(
    assignment: "TaskAssignment",
    project_root: str,
    agent_id: Optional[str] = None,
) -> None:
    """Snapshot the session's branch tip into the assignment.

    The baseline marks where this card's work starts. It scopes two
    things: the ``git diff <baseline>..HEAD`` delta ``WorkAnalyzer``
    validates (issue #696) and, since issue #730, the commit range
    Marcus merges into ``main`` on completion.

    The snapshot is taken **in the agent's own worktree** when one
    exists. ``main`` moves whenever another session's card merges, so a
    baseline read from the main repo can point at a commit that is not
    on this agent's branch at all — which would make the card's merge
    range include other sessions' work or nothing at all.

    Parameters
    ----------
    assignment : TaskAssignment
        The assignment object to update in-place.
    project_root : str
        Any directory inside the git repo to run git commands from.
    agent_id : Optional[str]
        Agent claiming the task. Its branch is ``marcus/{agent_id}``;
        when that branch is checked out in a worktree, HEAD is read
        there instead of in ``project_root``.
    """
    baseline_cwd = project_root
    if agent_id:
        worktree = _worktree_path_for_branch(project_root, f"marcus/{agent_id}")
        if worktree:
            baseline_cwd = worktree
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=baseline_cwd,
            timeout=5,
        )
        if result.returncode == 0:
            assignment.baseline_commit = result.stdout.strip()
            logger.debug(
                f"Captured baseline commit {assignment.baseline_commit[:8]} "
                f"for task {assignment.task_id}"
            )
        else:
            logger.warning(
                f"git rev-parse HEAD failed for task {assignment.task_id}: "
                f"{result.stderr.strip()}"
            )
    except Exception as exc:
        logger.warning(
            f"Could not capture baseline commit for task {assignment.task_id}: {exc}"
        )


async def _baseline_commit_for_task(
    state: Any,
    agent_id: str,
    task_id: str,
) -> Optional[str]:
    """Recover the baseline commit captured when this card was claimed.

    Three sources, in order of authority:

    1. the in-memory assignment, while it exists;
    2. the persisted assignment row, which survives a Marcus restart;
    3. the per-task baseline store, which survives the **assignment
       release** a completion performs before it merges. Without this
       third source, an agent sent back to resolve a merge conflict
       would report again with no baseline and have its whole branch
       merged — the very thing issue #730 closes (Codex P1 on PR #790).

    A record pointing at a different task is ignored: a baseline from
    another card would merge the wrong range.

    Parameters
    ----------
    state : Any
        Marcus server state.
    agent_id : str
        Agent reporting completion.
    task_id : str
        The card being completed.

    Returns
    -------
    Optional[str]
        The baseline commit sha, or None when it cannot be recovered —
        in which case callers fall back to branch-level behaviour.
    """
    assignment = getattr(state, "agent_tasks", {}).get(agent_id)
    if assignment is not None and getattr(assignment, "task_id", None) == task_id:
        baseline = getattr(assignment, "baseline_commit", None)
        if baseline:
            return str(baseline)

    persistence = getattr(state, "assignment_persistence", None)
    if persistence is None:
        return None
    try:
        record = await persistence.get_assignment(agent_id)
    except Exception as exc:  # noqa: BLE001 - never fail a completion
        logger.debug(f"Could not read persisted assignment for {agent_id}: {exc}")
        record = None
    if record and record.get("task_id") == task_id:
        baseline = (record.get("task_data") or {}).get("baseline_commit")
        if baseline:
            return str(baseline)

    # The per-task store, which outlives the assignment row.
    get_task_baseline = getattr(persistence, "get_task_baseline", None)
    if get_task_baseline is None:
        return None
    try:
        recorded = await get_task_baseline(task_id, agent_id)
    except Exception as exc:  # noqa: BLE001 - never fail a completion
        logger.debug(f"Could not read the baseline for task {task_id}: {exc}")
        return None
    return str(recorded) if recorded else None


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
    # Set success explicitly rather than inheriting it from the copied
    # input: an escalation is always a refusal, and a caller that passes
    # a payload lacking ``success`` must still get an explicit flag.
    escalated["success"] = False
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
    # Set success explicitly rather than inheriting it from the copied
    # input: an escalation is always a refusal, and a caller that passes
    # a payload lacking ``success`` must still get an explicit flag.
    escalated["success"] = False
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


# Task types whose completion requires typed evidence (issue #737).
# The implement type is deliberately absent: it stays on the existing
# smoke gate and behavior-evidence machinery above.
EVIDENCE_REQUIRED_TASK_TYPES = frozenset({"find", "verify", "synthesize"})

# The one evidence field the board never hands to dependents: the
# upstream agent's raw output. The audit bundle keeps it; the
# verifier's view never contains it.
RAW_EVIDENCE_FIELD = "raw"


def _typed_evidence_rejection(
    *,
    task: Task,
    agent_id: str,
    missing_fields: List[str],
    evidence_missing: bool,
) -> Dict[str, Any]:
    """Build the rejection for a typed completion without valid evidence.

    Issue #737: a find, verify, or synthesize task cannot close
    without the typed payload its ``output_schema`` requires. This is
    the structured refusal the agent receives, naming exactly the
    fields that are missing so the correction is mechanical.

    Parameters
    ----------
    task : Task
        The typed task being completed.
    agent_id : str
        Agent reporting completion.
    missing_fields : List[str]
        The required fields absent from the submitted evidence.
    evidence_missing : bool
        True when no evidence payload was submitted at all.

    Returns
    -------
    Dict[str, Any]
        A rejection dict the caller returns directly to the agent.
    """
    error = (
        "typed_evidence_missing" if evidence_missing else "typed_evidence_incomplete"
    )
    required = sorted(missing_fields)
    return {
        "success": False,
        "status": "evidence_rejected",
        "error": error,
        "agent_id": agent_id,
        "task_id": task.id,
        "task_type": task.task_type,
        "missing_fields": required,
        "blocker": (
            f"Marcus rejected this completion: a {task.task_type!r} task "
            f"cannot close without evidence. "
            + (
                "No ``evidence`` payload was submitted. "
                if evidence_missing
                else "The submitted ``evidence`` is missing required fields. "
            )
            + f"Missing fields: {', '.join(required) if required else '(none)'}. "
            f"Re-call report_task_progress with the SAME status and an "
            f"``evidence`` dict containing every required field."
        ),
        "message": (
            f"Completion rejected: evidence "
            f"{'was not submitted' if evidence_missing else 'is incomplete'} "
            f"for {task.task_type} task {task.id}. "
            f"Missing fields: {', '.join(required) if required else '(none)'}."
        ),
    }


def _escalate_typed_evidence_response(
    rejection: Dict[str, Any],
) -> Dict[str, Any]:
    """Convert a repeated typed-evidence rejection into a terminal one.

    Mirrors :func:`_escalate_behavior_evidence_response` (issue #677):
    after ``MAX_SMOKE_BEHAVIOR_EVIDENCE_ATTEMPTS`` identical rejections
    the agent is plainly not going to produce the evidence by
    re-sending the same call, so the loop stops with a non-retryable
    error while the remediation payload is preserved.

    Parameters
    ----------
    rejection : Dict[str, Any]
        The ``typed_evidence_missing`` / ``typed_evidence_incomplete``
        rejection to escalate.

    Returns
    -------
    Dict[str, Any]
        A new dict tagged terminal/escalated, remediation intact.
    """
    escalated = dict(rejection)
    # Set success explicitly rather than inheriting it from the copied
    # input: an escalation is always a refusal, and a caller that passes
    # a payload lacking ``success`` must still get an explicit flag.
    escalated["success"] = False
    escalated["status"] = "evidence_rejection_escalated"
    escalated["error"] = "typed_evidence_escalated"
    escalated["terminal"] = True
    escalated["escalated"] = True
    escalated["message"] = (
        "Marcus has rejected this completion "
        f"{MAX_SMOKE_BEHAVIOR_EVIDENCE_ATTEMPTS} times for the same reason: "
        "the typed evidence this task must return is missing or incomplete. "
        "Do NOT retry the same call. This task is escalated for remediation "
        "-- the required fields are listed in ``missing_fields`` and the fix "
        "is in ``blocker``."
    )
    return escalated


async def _apply_typed_evidence_gate(
    task: Task,
    agent_id: str,
    evidence: Optional[Dict[str, Any]],
    state: Any,
) -> Optional[Dict[str, Any]]:
    """Refuse a typed completion whose evidence fails its contract.

    Issue #737, step 4: when a find, verify, or synthesize task
    reports ``completed``, its ``evidence`` must contain every field
    the task's ``output_schema`` requires (with no schema, any
    non-empty dict passes). The rejection names the missing fields.
    Repeated failure reuses the #677 retry ceiling
    (:func:`_record_behavior_evidence_attempt`): after
    ``MAX_SMOKE_BEHAVIOR_EVIDENCE_ATTEMPTS`` rejections the next one
    terminalizes the task instead of looping to lease expiry.

    Parameters
    ----------
    task : Task
        The task being completed.
    agent_id : str
        Agent reporting completion.
    evidence : Optional[Dict[str, Any]]
        The typed payload submitted with the completion.
    state : Any
        Marcus server state (used only when terminalizing).

    Returns
    -------
    Optional[Dict[str, Any]]
        None when the completion may proceed; otherwise the rejection
        or terminal escalation dict to return to the agent.
    """
    if task.task_type not in EVIDENCE_REQUIRED_TASK_TYPES:
        return None

    required = list((task.output_schema or {}).get("required", []))
    evidence_missing = not evidence
    if evidence_missing:
        missing = required
    else:
        assert evidence is not None  # for mypy; guarded above
        missing = [f for f in required if f not in evidence]
    if not evidence_missing and not missing:
        return None

    rejection = _typed_evidence_rejection(
        task=task,
        agent_id=agent_id,
        missing_fields=missing,
        evidence_missing=evidence_missing,
    )
    attempts = _record_behavior_evidence_attempt(task.id)
    if attempts > MAX_SMOKE_BEHAVIOR_EVIDENCE_ATTEMPTS:
        logger.warning(
            "Typed-evidence gate: task %s rejected %d times; escalating "
            "to a terminal response (issue #737, #677 ceiling).",
            task.id,
            attempts,
        )
        escalated = _escalate_typed_evidence_response(rejection)
        await _terminalize_escalated_smoke_task(
            state,
            task.id,
            agent_id,
            str(
                escalated.get("blocker")
                or escalated.get("message")
                or "Typed-evidence gate escalated: evidence never met the schema."
            ),
        )
        _clear_smoke_attempts(task.id)
        return escalated
    return rejection


APPROVE_TASK_TYPE = "approve"

# Task types whose accepted evidence is written to the record (the
# audit bundle reads it from the completed task's source_context).
# Approve is recorded but not evidence-gated: the human's decision is
# the approval; a payload is kept when provided.
EVIDENCE_RECORDED_TASK_TYPES = EVIDENCE_REQUIRED_TASK_TYPES | {APPROVE_TASK_TYPE}


def _apply_approval_gate(
    task: Task, agent_id: str, state: Any
) -> Optional[Dict[str, Any]]:
    """Refuse an approve-task completion from anyone but a person.

    Issue #737, step 5: an approve task closes only when
    ``report_task_progress`` is called by a registered ``human``
    principal. There is no new task status; dependents wait through
    ordinary dependency resolution until the approval closes.
    Eligibility already keeps approve tasks out of agents' hands at
    offer time, so this gate is defense in depth at the close.

    Parameters
    ----------
    task : Task
        The task being completed.
    agent_id : str
        The caller reporting completion.
    state : Any
        Marcus server state; ``agent_status`` supplies the caller's
        registered principal.

    Returns
    -------
    Optional[Dict[str, Any]]
        None when the completion may proceed; otherwise the rejection
        dict to return to the caller.
    """
    if task.task_type != APPROVE_TASK_TYPE:
        return None

    caller = getattr(state, "agent_status", {}).get(agent_id)
    principal = caller.principal if isinstance(caller, WorkerStatus) else "unregistered"
    if principal == "human":
        return None

    logger.info(
        "[#737] Approval gate: refused completion of approve task %s by "
        "%s (principal: %s).",
        task.id,
        agent_id,
        principal,
    )
    return {
        "success": False,
        "status": "approval_rejected",
        "error": "approval_requires_human",
        "agent_id": agent_id,
        "task_id": task.id,
        "task_type": task.task_type,
        "blocker": (
            "Marcus rejected this completion: an approve task can only be "
            "completed by a human principal, and this caller is registered "
            f"as {principal!r}. The approval waits for a person; dependent "
            "tasks are held until it closes."
        ),
        "message": (
            f"Completion rejected: approve task {task.id} requires a human "
            f"principal; caller {agent_id} is {principal!r}."
        ),
    }


async def _handoff_evidence_to_dependents(
    task: Task, evidence: Dict[str, Any], state: Any
) -> None:
    """Copy a closed task's evidence minus ``raw`` into dependents' inputs.

    Issue #737, step 4, generalized at the step 7 review (Simon
    16c9a4b1): every evidence-recorded close hands off. This copy is
    the ONLY route by which upstream results reach a downstream
    worker: the verifier receives the find task's claim, the approve
    reviewer receives the verdict beside the claim, and the
    synthesize worker receives everything it renders. The ``raw``
    field (an agent's full model output) is stripped at every
    handoff, so no downstream party ever sees upstream reasoning; the
    audit bundle keeps ``raw`` via the completed task's own record.
    Both the in-memory tasks and the kanban rows are updated, so the
    handoff survives a restart.

    Parameters
    ----------
    task : Task
        The task that just closed.
    evidence : Dict[str, Any]
        Its schema-validated evidence payload.
    state : Any
        Marcus server state with ``project_tasks`` and
        ``kanban_client``.
    """
    # Accumulative: the closing task's own inputs ride along with its
    # evidence, so case state builds down the chain (a sampled case's
    # synthesize task depends on approve, not verify, and still needs
    # the claim and the verdict). ``raw`` is stripped at every hop.
    payload = {
        k: v
        for k, v in {**(task.inputs or {}), **evidence}.items()
        if k != RAW_EVIDENCE_FIELD
    }
    persistence = getattr(state, "assignment_persistence", None)
    for dependent in getattr(state, "project_tasks", None) or []:
        if task.id not in (dependent.dependencies or []):
            continue
        # SET REPLACEMENT, not a per-key merge (issue #803).
        #
        # Existing keys win (Codex P1 on PR #739): a dependent's
        # board-seeded inputs must never be overwritten by a claim
        # author's evidence, or a finder could ship its own excerpt and
        # the verifier would check the attacker's text against the page.
        # That rule stands, and everything below preserves it.
        #
        # What it rested on does not: its comment said "legitimate chain
        # keys never collide by construction", which holds only while a
        # task closes once. ``request_task_redo`` re-opens a DONE task
        # and clears nothing downstream, so a second close collides with
        # the first by construction, first-write-wins keeps the
        # superseded claim, and the verifier passes a value the author
        # already corrected.
        #
        # So this close clears the keys THIS task contributed last time
        # and writes its new set. Keys attributed to nobody (the
        # board-seeded set) and keys another upstream task contributed
        # are untouched, which is what keeps the security property
        # exact. Replacing the set rather than overwriting key by key is
        # what makes it correct when the evidence shape varies between
        # rounds: a key present in the first close and absent from the
        # second is removed rather than left standing, and the schema
        # only pins the REQUIRED fields, so that case is reachable.
        # Everything from here to the provenance write is one critical
        # section per dependent (Codex P2 on PR #807). The steps are not
        # individually unsafe; what is unsafe is another handoff
        # observing the gap between the in-memory replacement and the
        # board write that commits it.
        async with _handoff_lock(dependent.id):
            await _apply_handoff_to_dependent(
                task, dependent, payload, persistence, state
            )


async def _apply_handoff_to_dependent(
    task: Task,
    dependent: Task,
    payload: Dict[str, Any],
    persistence: Any,
    state: Any,
) -> None:
    """Replace one dependent's keys from a closing task's payload.

    Held under that dependent's handoff lock by the only caller.

    REDESIGNED (Codex P2 on PR #807, fourth round). Three earlier rounds
    kept the merge basis — the dependent's "before" inputs — on the
    ``dependent`` object the caller passed in, which is captured from
    ``state.project_tasks`` BEFORE this lock is ever acquired.
    ``refresh_project_state`` (server.py, run on every
    ``request_next_task`` poll) can swap that whole list with fresh
    objects at any ``await`` point, including while this lock is held —
    it has no idea the lock exists. A second handoff can then acquire
    the very same lock, correctly, and still merge against a stale
    pre-write snapshot of the dependent, because the object it was given
    was fixed before the lock was ever requested. The lock serialised
    *order*, not *data freshness*.

    The fix is to never trust the in-memory object for the merge basis
    at all. The dependent's current ``inputs`` are read fresh from the
    kanban board itself, inside the lock, immediately before computing
    and writing the replacement. ``refresh_project_state`` only ever
    reads the board and never writes ``inputs``, so once the merge basis
    is the board rather than an object a caller might be holding stale,
    there is nothing left for a second, uncoordinated writer to race
    against: the lock now owns the one thing both this function and a
    concurrent refresh actually touch.

    One consequence worth naming: there is no longer anything to roll
    back on a failed write, because nothing is written to the in-memory
    object speculatively before the board write succeeds. A failure
    simply means the attempted change never reached the board; nothing
    is mutated, and — matching every other handoff failure path in this
    codebase (#737, #667 Fix 2) — it is logged and swallowed rather than
    raised, so it can never strand or eject the agent whose completion
    triggered it.

    Codex P2, fifth round: treating the board as the ONLY merge basis
    broke on Planka, GitHub and Linear, none of which round-trip
    ``inputs`` at all — their converters drop the field on both create
    and update, so ``get_task_by_id`` always comes back empty on those
    providers. The merge basis is therefore the union of the board's
    current row (authoritative on sqlite, and the only source of
    board-seeded inputs anywhere) and Marcus's own durable
    ``committed_inputs`` snapshot (authoritative regardless of provider,
    since nothing else in the codebase ever writes a dependent's
    ``inputs``). The snapshot wins on a key both sources have, since the
    board's agreement with it is no guarantee on these three providers.

    Parameters
    ----------
    task : Task
        The task that just closed.
    dependent : Task
        The task whose inputs are being written. Used only for its
        ``id``; its ``.inputs`` is never read, which is the point.
    payload : Dict[str, Any]
        The closing task's inputs and evidence, minus ``raw``.
    persistence : Any
        The provenance store, or ``None`` when the caller has none.
    state : Any
        Marcus server state, for the kanban client.
    """
    prior_keys: List[str] = []
    if persistence is not None:
        try:
            prior_keys = await persistence.get_input_provenance(dependent.id, task.id)
        except Exception as prov_err:  # noqa: BLE001 - degrade to additive
            logger.warning(
                "[#803] Handoff provenance unreadable for dependent %s "
                "(%s); this close adds keys and clears none, so a "
                "superseded value may survive.",
                dependent.id,
                prov_err,
            )

    # The merge basis: the board's CURRENT row, fetched now, inside the
    # lock — never the ``dependent`` object the caller captured before
    # acquiring it. A missing board row degrades to "nothing to keep,"
    # which is the behaviour an empty dependent would have given anyway.
    try:
        current = await state.kanban_client.get_task_by_id(dependent.id)
        board_inputs = dict((current.inputs if current else {}) or {})
    except Exception as read_err:  # noqa: BLE001 - never fail a close
        logger.warning(
            "[#807] Could not read dependent %s from the board (%s); "
            "this close is skipped for it rather than risking a write "
            "against an unknown prior state.",
            dependent.id,
            read_err,
        )
        return

    # Codex P2, fifth round: the board alone is not authoritative on
    # Planka, GitHub or Linear (see the docstring). Marcus's own durable
    # snapshot of what it last committed fills that gap and wins ties,
    # since those three providers can echo back nothing to disagree with.
    committed_inputs: Dict[str, Any] = {}
    if persistence is not None:
        try:
            committed_inputs = await persistence.get_committed_inputs(dependent.id)
        except Exception as snap_err:  # noqa: BLE001 - degrade to board-only
            logger.warning(
                "[#807] Committed-inputs snapshot unreadable for "
                "dependent %s (%s); falling back to the board alone, "
                "which loses prior contributions on providers that "
                "don't round-trip inputs.",
                dependent.id,
                snap_err,
            )

    before = {**board_inputs, **committed_inputs}
    kept = {k: v for k, v in before.items() if k not in prior_keys}
    new_inputs = {**payload, **kept}
    # Record what this close actually WROTE, not what it offered. A
    # key the board seeded, or another upstream task contributed,
    # beats this payload and stays in ``kept``; attributing it here
    # would let the next close of this task clear a value it never
    # owned, which would turn the #803 fix into a way around the
    # very rule it preserves.
    contributed = [k for k in payload if k not in kept]

    try:
        await state.kanban_client.update_task(dependent.id, {"inputs": new_inputs})
    except Exception as handoff_err:  # noqa: BLE001 - never fail a close
        logger.warning(
            "[#737] Claim handoff: kanban write failed for dependent "
            "%s (%s); nothing was written, so the board and the "
            "in-memory copy stay in agreement.",
            dependent.id,
            handoff_err,
        )
        return

    # The in-memory copy is refreshed as a courtesy for anything in
    # THIS request that reads it synchronously afterward. It is no
    # longer load-bearing for correctness: the next handoff into this
    # dependent re-reads the board under the lock regardless of what
    # this object says by then.
    dependent.inputs = new_inputs

    if persistence is not None:
        try:
            await persistence.record_input_provenance(
                dependent.id, task.id, contributed
            )
        except Exception as prov_err:  # noqa: BLE001 - never fail a close
            logger.warning(
                "[#803] Handoff provenance write failed for dependent "
                "%s (%s); a later re-close of %s may leave one of "
                "these keys standing.",
                dependent.id,
                prov_err,
                task.id,
            )
        try:
            await persistence.record_committed_inputs(dependent.id, new_inputs)
        except Exception as snap_err:  # noqa: BLE001 - never fail a close
            logger.warning(
                "[#807] Committed-inputs snapshot write failed for "
                "dependent %s (%s); a provider that doesn't round-trip "
                "inputs may lose this close's contribution on the next "
                "handoff into this dependent.",
                dependent.id,
                snap_err,
            )


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
                await state.lease_manager.release_lease(task_id, reason="escalated")
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
    # Issue #719: and the advisory-blocker budget — the incoming agent has
    # not asked for help yet.
    clear_blocker_attempts(task_id)


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


async def _planner_cost_context(
    state: Any,
    *,
    task: Optional[Task] = None,
    agent_id: Optional[str] = None,
) -> AbstractContextManager[Any]:
    """Build a cost-attribution context for a run-time planner LLM call (#768).

    Marcus makes several AI calls *while a project is running* -- it
    generates a task's instructions when an agent requests work, analyzes
    a reported blocker, and validates completed work. Each of those calls
    spends tokens that must appear on the Cato cost dashboard attributed
    to the right project and run. The attribution is carried by a
    :class:`~src.cost_tracking.cost_recorder.PlannerContext`, which the
    recorder reads when it writes a ``token_events`` row. This helper
    resolves that context and returns it ready to use in a ``with``
    block; the three run-time call sites wrap their AI call in it.

    The project id is resolved by
    :func:`src.marcus_mcp.handlers._resolve_project_for_cost`, the same
    resolver the MCP request path uses, so a run-time call can never
    attribute to a different project than its handler does. It checks, in
    order, the task's own ``project_id``, the agent's registered project
    (``state.agent_project_map``), then the server's selected / active
    project.

    The run id is the project's open setup run -- the row
    ``create_project`` opened in the ``runs`` table and left open for the
    duration of the project (see
    :meth:`~src.cost_tracking.cost_store.CostStore.get_latest_open_run_for_project`).
    Using that run means run-time spend lands on the same run as the
    setup/decomposition spend, which is what Cato groups by. If a project
    is resolvable but its open run is not (an edge case, e.g. the run was
    already closed), the call is still attributed to the project with the
    run id ``"unassigned"`` so the project total stays correct.

    Parameters
    ----------
    state : Any
        Marcus server state. Supplies ``cost_store`` and the fields the
        shared cost resolver reads (``agent_project_map`` and the
        selected / active project).
    task : Optional[Task]
        The task the AI call is about, if any. Its ``project_id`` and
        ``id`` seed the attribution.
    agent_id : Optional[str]
        The agent the call is being made on behalf of, if any.

    Returns
    -------
    contextlib.AbstractContextManager
        A context manager that pushes the resolved ``PlannerContext`` for
        the duration of the ``with`` block, or a no-op
        :func:`contextlib.nullcontext` when no project can be resolved
        (the call then falls through to the ``'unassigned'`` bucket,
        which is the honest default).
    """
    from src.cost_tracking.cost_recorder import PlannerContext, get_recorder

    # 1. Resolve the project id. Reuse the one cost-attribution resolver the
    #    MCP handler uses (explicit project_id -> agent_project_map ->
    #    selected/active project) so this run-time context cannot drift from
    #    the handler's (#768 review). Imported here, not at module scope,
    #    to avoid a circular import: handlers imports this module's tools.
    from src.marcus_mcp.handlers import _resolve_project_for_cost

    project_id: Optional[str] = _resolve_project_for_cost(
        {
            "project_id": (
                getattr(task, "project_id", None) if task is not None else None
            ),
            "agent_id": agent_id,
        },
        state,
    )
    if not project_id:
        # No project to attribute against -- fall through to 'unassigned'
        # so the attribution gap stays visible rather than being
        # silently misassigned.
        return nullcontext()

    # 2. Resolve the run id -- the project's open setup run.
    run_id = "unassigned"
    cost_store = getattr(state, "cost_store", None)
    if cost_store is not None:
        try:
            resolved_run = cost_store.get_latest_open_run_for_project(str(project_id))
        except Exception:  # pragma: no cover - defensive, never break caller
            resolved_run = None
        if resolved_run:
            run_id = resolved_run

    # PlannerContext normalizes project_id to the canonical dashless form
    # on construction, matching how the decomposition path records.
    return get_recorder().planner_context(
        PlannerContext(
            run_id=run_id,
            project_id=str(project_id),
            # PlannerContext.agent_id is non-optional and defaults to
            # 'planner' (Marcus's own LLM calls); keep that default when
            # no agent is in scope.
            agent_id=agent_id or "planner",
            task_id=getattr(task, "id", None) if task is not None else None,
        )
    )


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


def _best_effort_addendum(degraded_upstreams: List[Dict[str, str]]) -> str:
    """Render the assignment-instructions addendum for a best-effort claim.

    Issue #629: when the integration task claims over settled-but-BLOCKED
    upstreams, the agent must know which lanes never finished — and that
    ``request_task_redo`` does NOT apply to them (redo sends back
    *completed* work; these lanes are terminally blocked, so the gaps are
    the integration agent's to account for).

    Parameters
    ----------
    degraded_upstreams : List[Dict[str, str]]
        ``{"id", "name", "status"}`` records for each non-DONE upstream.

    Returns
    -------
    str
        Markdown addendum appended to the assignment instructions.
    """
    lines = "\n".join(
        f"- {d['name']} (id {d['id']}): {d['status']}" for d in degraded_upstreams
    )
    return (
        f"\n\n⚠️ **BEST-EFFORT INTEGRATION (issue #629)** — "
        f"{len(degraded_upstreams)} upstream task(s) never completed and are "
        f"terminally BLOCKED on the board:\n{lines}\n\n"
        "Their deliverables may be missing or partial. Verify what actually "
        "exists before assuming any of it works. The gaps are yours to "
        "account for: build the minimal missing pieces yourself where the "
        "outcomes require them (these lanes are terminal — no one else will), "
        "or report_blocker if the product cannot work without something you "
        "cannot supply. Note: request_task_redo does NOT apply here — it "
        "sends back completed work, and these tasks never completed."
    )


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
        ``verifications`` and ``start_command`` were missing. When the
        gate itself cannot run (no usable ``project_root``), a parked
        response with ``status="smoke_gate_unavailable"``,
        ``operator_needed=True`` and ``retry_after_seconds``: the task
        stays with the agent and an operator is alerted.

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
    # Helper: PARK the task when the gate itself cannot run. Integration
    # tasks MUST be verifiable, so "cannot run the gate" is never a pass
    # (Codex P2-2), but it is not the agent's failure either: under
    # Invariant #2 (v2) verification is Marcus's job, and a gate Marcus
    # cannot run is Marcus's misconfiguration. The old response told the
    # agent to "fix the workspace state" (it cannot) and the per-task
    # instructions told it to file a blocker, so in the 2026-11-02
    # calculator run two high blockers spent two of the lane's three repair
    # requeues on a missing project_root. Now: the completion is refused
    # with its own status, the task stays IN_PROGRESS with the lease
    # intact, the agent is told to wait and re-report, and an operator is
    # alerted with the exact key to fix. The caller's log-and-continue
    # except clause stays reserved for exceptions (programmer errors in the
    # verification system), not for resolvable configuration failures.
    async def _gate_unavailable(reason: str) -> Dict[str, Any]:
        project_id = getattr(getattr(state, "kanban_client", None), "project_id", None)
        # Hold the lease outside the D3 budget while the operator acts:
        # the wait is Marcus's time, not the agent's (Codex P2 on PR #775).
        lease_manager = getattr(state, "lease_manager", None)
        if lease_manager is not None:
            try:
                parked = lease_manager.park_for_operator(
                    task.id, window_seconds=2 * GATE_UNAVAILABLE_RETRY_SECONDS
                )
                if inspect.isawaitable(parked):
                    await parked
            except Exception as park_error:  # noqa: BLE001 - never break the reply
                logger.warning(
                    f"Could not park the lease for {task.id} while the gate is "
                    f"unavailable: {park_error}"
                )
        operator_action = (
            f"Set provider_config.project_root for project {project_id or '?'} "
            "in the project registry (data/marcus_state/projects.json, or the "
            "project_root option of create_project) to the absolute path of "
            "the workspace, then restart or re-select the project. Until then "
            f"task {task.id} is parked with agent {agent_id}."
        )
        logger.error(
            f"OPERATOR NEEDED: smoke gate unavailable for task {task.id} "
            f"({reason}). {operator_action}"
        )
        log_event = getattr(state, "log_event", None)
        if callable(log_event):
            try:
                log_event(
                    "operator_needed",
                    {
                        "task_id": task.id,
                        "agent_id": agent_id,
                        "project_id": project_id,
                        "reason": reason,
                        "operator_action": operator_action,
                        "source": "smoke_gate",
                    },
                )
            except Exception:  # noqa: BLE001 - an alert must not break the reply
                pass
        return {
            "success": False,
            "status": "smoke_gate_unavailable",
            "error": "smoke_gate_unavailable",
            "operator_needed": True,
            "retry_after_seconds": GATE_UNAVAILABLE_RETRY_SECONDS,
            "agent_id": agent_id,
            "task_id": task.id,
            "failure_summary": reason,
            "operator_action": operator_action,
            "blocker": (
                f"Marcus could not run the product smoke gate: {reason}. "
                "This is a Marcus-side misconfiguration, not a defect in the "
                "deliverable. " + operator_action
            ),
            "message": (
                "Marcus could not run its own verification gate for this "
                f"integration task: {reason}. This is NOT a problem with your "
                "work and there is nothing in the workspace for you to fix. "
                "Do NOT file a blocker and do NOT give the task up. Keep the "
                f"task, wait {GATE_UNAVAILABLE_RETRY_SECONDS} seconds, and "
                "re-report completion with the same evidence; an operator has "
                "been alerted to fix the configuration."
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
            return await _gate_unavailable(f"workspace state load failed: {ws_err}")

    if not project_root:
        logger.warning(
            f"PRODUCT SMOKE GATE: No project_root resolved for "
            f"{task.id}. Rejecting completion."
        )
        return await _gate_unavailable("project_root not found in workspace state")

    project_root_path = _Path(project_root)
    if not project_root_path.is_dir():
        logger.warning(
            f"PRODUCT SMOKE GATE: project_root {project_root!r} is "
            f"not a directory. Rejecting completion for {task.id}."
        )
        return await _gate_unavailable(
            f"project_root {project_root!r} is not a directory"
        )

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


def _build_checkpoint_duty_prompt(state: Optional[Any]) -> str:
    """State the ADR-0012 D8 checkpoint duty in Marcus's configured numbers.

    The duty is what makes a dead worker's partial work salvageable: the
    recovery handoff can only pass on commits, so work that was never
    committed dies with the worker. Marcus therefore names the cadence it
    expects and the silence it reads as death, rather than leaving an
    agent to guess from a static prompt file.

    This is a project-wide discipline, not an implementation decision:
    it says *when* to commit, never what to write.

    The numbers come from the live lease manager so a deployment that
    retunes them retunes the contract too. Without one, the ADR defaults
    are used.

    Parameters
    ----------
    state : Optional[Any]
        Marcus server state. Read for ``lease_manager``.

    Returns
    -------
    str
        The instruction layer, ready to append.
    """
    cadence = DEFAULT_CHECKPOINT_CADENCE_MINUTES
    silence = DEFAULT_SILENCE_TIMEOUT_MINUTES
    lease_manager = getattr(state, "lease_manager", None) if state else None
    if lease_manager is not None:
        cadence = getattr(lease_manager, "checkpoint_cadence_minutes", None) or cadence
        silence = getattr(lease_manager, "silence_timeout_minutes", None) or silence
    return (
        "\n\n🕐 CHECKPOINT DUTY\n\n"
        f"Commit your work at least every {cadence:.0f} minutes, and log a "
        f"decision or artifact as you go.\n"
        f"Marcus reads {silence:.0f} minutes with no tool call from you as a "
        f"dead worker and hands this task to another agent.\n"
        "Only committed work can be handed over. Anything uncommitted dies "
        "with you, and the next agent starts again from nothing.\n"
        "A checkpoint is an ordinary commit on your branch; when Marcus "
        "accepts your completion it merges only the commits you made for "
        "THIS task."
    )


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
    0.2. Checkpoint duty (always included — ADR-0012 D8)
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
            "cause your lease to expire.\n\n"
            "If instead Marcus replies with status 'smoke_gate_unavailable', "
            "the gate itself could not run (a Marcus-side misconfiguration, "
            "not your work): do NOT file a blocker, keep the task, wait the "
            "returned retry_after_seconds and re-report the same completion."
        )

    # Layer 0.2: the ADR-0012 D8 checkpoint duty, in Marcus's own numbers.
    # Partial work is only salvageable if it was committed, so the cadence
    # is part of the task contract rather than a hope.
    instructions_parts.append(_build_checkpoint_duty_prompt(state))
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


# How long an agent waits before asking for work again when none is
# available. A deliberate constant, not an ETA-derived value: PR #177
# ("reduce agent retry wait times from 300s to 30s for better
# parallelization") found that long waits left idle agents missing tasks
# as they became ready. The blocking task's ETA is still reported for
# humans; it just does not drive the cadence.
RETRY_AFTER_SECONDS = 30
# Spread applied to every retry interval, as a fraction of it. Workers that
# register together are otherwise all told to wait the same 30 seconds and
# poll in lockstep for the whole run: N requests arrive in the same second
# every half minute, and N-1 of them find the task the first one just
# claimed (the file-lock race below). One jittered wait decorrelates them,
# and the mean stays at the constant PR #177 chose.
RETRY_JITTER_FRACTION = 0.33


def jittered_retry_seconds(base: int = RETRY_AFTER_SECONDS) -> int:
    """Return a retry interval around ``base`` with the jitter spread applied.

    Parameters
    ----------
    base : int
        The interval to spread, in seconds. Defaults to
        :data:`RETRY_AFTER_SECONDS`.

    Returns
    -------
    int
        A whole number of seconds drawn uniformly from
        ``base ± base * RETRY_JITTER_FRACTION``, never below one. The
        distribution is symmetric, so the expected wait is ``base``: this
        decorrelates pollers without changing the cadence Marcus
        documents or lengthening the wait of any one worker on average.

    Notes
    -----
    The operator-facing waits are deliberately not jittered: a parked
    task's ``GATE_UNAVAILABLE_RETRY_SECONDS`` is a stated interval an
    operator reads, not a poll cadence to spread.
    """
    spread = base * RETRY_JITTER_FRACTION
    drawn = secrets.SystemRandom().uniform(base - spread, base + spread)
    return max(1, round(drawn))


async def calculate_retry_after_seconds(state: Any) -> Dict[str, Any]:
    """
    Calculate intelligent wait time before next task request.

    Strategy:
    - Prioritizes tasks that unlock parallel work for idle agents, and
      reports the soonest such task as ``blocking_task``
    - Returns a wait of ``RETRY_AFTER_SECONDS`` with jitter applied
      (:func:`jittered_retry_seconds`). The wait is deliberately not
      ETA-proportional; see the constant's comment. The jitter keeps
      workers that registered together from polling in lockstep.

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
        - retry_after_seconds: int (``RETRY_AFTER_SECONDS`` ± the
          jitter spread; never ETA-derived)
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
            "retry_after_seconds": jittered_retry_seconds(),
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

    # The wait is the constant plus jitter. It previously computed 60% of
    # the ETA, floored it at 30 and then ceilinged it at 30 —
    # arithmetically the constant, with the ETA discarded. The dead
    # arithmetic read as though the wait scaled with the task, which is
    # the opposite of the PR #177 decision it implemented. The jitter
    # spreads pollers; it does not reintroduce ETA scaling.
    retry_after = jittered_retry_seconds()

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


def _close_run_on_drain(project_id: Optional[str], verdict: Any) -> Optional[str]:
    """Close the project's current cost-store run when the board drains.

    The ``runs`` table (``src/cost_tracking/cost_store.py``) opens a row per
    ``create_project`` and used to close it only when a model called the
    ``end_experiment`` tool. That tool is gone (ADR-0012 D10: a run is not a
    core concept), so the board's own verdict closes the row: the first
    drained ``request_next_task`` for a project stamps ``ended_at`` and the
    final counts, and emits the ``project_cost_summary`` telemetry event
    that used to ride on ``end_experiment``. Every later drain for the same
    project is a no-op, because the close targets the *current* run and
    refuses an already-closed one (so older unclosed rows are never
    touched). Best effort: a cost-store failure never blocks the drain
    response.

    Parameters
    ----------
    project_id : Optional[str]
        The agent's project.
    verdict : Any
        The :class:`DrainVerdict` that ended the run.

    Returns
    -------
    Optional[str]
        The closed ``run_id``, or None when nothing was open or the store
        was unavailable.
    """
    if not project_id:
        return None
    try:
        from src.cost_tracking.cost_recorder import get_recorder

        store = get_recorder().store
        closed = store.close_current_run_for_project(
            project_id=project_id,
            ended_at=datetime.now(timezone.utc),
            total_tasks=verdict.tasks_total,
            completed_tasks=verdict.tasks_done,
            blocked_tasks=verdict.tasks_blocked,
        )
    except Exception as exc:  # noqa: BLE001 - never fail the drain on bookkeeping
        logger.warning("Could not close the run for project %s: %s", project_id, exc)
        return None
    if not closed:
        return None
    logger.info(
        "Closed run %s for project %s on BOARD_DRAINED (%s)",
        closed,
        project_id,
        verdict.reason,
    )
    _fire_cost_summary_for_project(project_id, store)
    return str(closed)


def _fire_cost_summary_for_project(project_id: str, store: Any) -> None:
    """Emit ``project_cost_summary`` once for a run that just closed.

    ``CostAggregator.project_summary`` returns a nested object (totals under
    ``summary``, cost in dollars); the telemetry helper wants a flat dict
    with ``cost_usd_cents`` and ``task_count``, so it is flattened here.
    Best effort; telemetry never affects the drain.

    Parameters
    ----------
    project_id : str
        The project whose run closed.
    store : Any
        The cost store the aggregator reads.
    """
    try:
        from src.cost_tracking.cost_aggregator import CostAggregator
        from src.telemetry.events import fire_project_cost_summary

        summary = CostAggregator(store).project_summary(project_id)
        if not summary:
            return
        totals = summary.get("summary") or {}
        cost_usd = totals.get("total_cost_usd") or 0
        fire_project_cost_summary(
            {
                "input_tokens": totals.get("input_tokens") or 0,
                "output_tokens": totals.get("output_tokens") or 0,
                "cache_read_tokens": totals.get("cache_read_tokens") or 0,
                "cache_creation_tokens": totals.get("cache_creation_tokens") or 0,
                "cost_usd_cents": round(cost_usd * 100),
                "task_count": len(summary.get("by_task") or []),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("project_cost_summary not emitted for %s: %s", project_id, exc)


async def _frontier_for_agent(agent_id: str, state: Any) -> Optional[Dict[str, int]]:
    """Report the board's claimable-work count for the agent's project.

    Issue #804: ``compute_board_drain`` used to run only when
    ``request_next_task`` had no task to hand out, so the count of
    claimable tasks — the frontier width, the measure of whether a plan
    is actually parallel — was discarded exactly when the board is
    healthy and nobody starves. A consumer could only see it collapse,
    never see it wide; one session had to sample the board file from
    outside Marcus and reimplement this function's own rules to get a
    time series at all.

    This is the single rule both callers share (compute_board_drain
    itself), so the number here and the one on the no-work path never
    diverge.

    Resolved from the AGENT's own registration, never the server's
    active project — the same source ``#805`` established for
    decisions, and for the same reason: the active project need not be
    the calling agent's on a multi-project server.

    Parameters
    ----------
    agent_id : str
        The agent whose project scopes the count.
    state : Any
        Marcus server state (``agent_project_map`` and whatever
        ``compute_board_drain`` itself needs).

    Returns
    -------
    Optional[Dict[str, int]]
        ``tasks_claimable`` and ``tasks_in_flight``, or ``None`` when the
        agent has no registered project or the count could not be
        computed. Observational only: a failure here must never affect
        whether the assignment itself succeeds.
    """
    project_id = (
        state.agent_project_map.get(agent_id, "")
        if hasattr(state, "agent_project_map")
        else ""
    )
    if not project_id:
        return None
    try:
        verdict = await compute_board_drain(state, project_id)
    except Exception as error:  # noqa: BLE001 - observational, never blocking
        logger.warning(
            f"[#804] Frontier count failed for {agent_id} ({error}); "
            "the caller proceeds without it."
        )
        return None
    return {
        "tasks_claimable": verdict.tasks_claimable,
        "tasks_in_flight": verdict.tasks_in_flight,
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

        # ADR-0012 O1: the first poll after a restart rebuilds the worker
        # roster from disk, and every poll reconciles a rehydrated worker's
        # current task with the live leases. This runs AFTER
        # refresh_project_state on purpose: the reconciliation needs the
        # board's tasks, and the one-task-per-agent check below needs the
        # reconciliation (Codex P1 on PR #772).
        from src.marcus_mcp.heartbeat import rehydrate_on_tool_call

        await rehydrate_on_tool_call(state, {"agent_id": agent_id})

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

                    # Analyze dependencies for THIS project only. Dependencies
                    # never cross projects, and on a shared board the whole
                    # file was 834 tasks where the project had 10 (Simon
                    # 25f0bcc3); the cache key is the task list, so scoping
                    # also keeps the cache stable per project.
                    if state.project_tasks:
                        dep_map = await state.context.analyze_dependencies(
                            _tasks_for_dependency_analysis(state, agent_id)
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

                # Generate detailed instructions with AI. Attribute the
                # planner LLM cost to the task's project and open run so it
                # reaches Cato instead of 'unassigned' (#768).
                try:
                    _instr_cost_ctx = await _planner_cost_context(
                        state, task=optimal_task, agent_id=agent_id
                    )
                    with _instr_cost_ctx:
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

                # Issue #629: a best-effort integration claim must disclose
                # exactly which upstream lanes never finished.
                degraded_upstreams = (optimal_task.source_context or {}).get(
                    "degraded_upstreams"
                ) or []
                if degraded_upstreams:
                    instructions = instructions + _best_effort_addendum(
                        degraded_upstreams
                    )

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
                                "retry_after_seconds": jittered_retry_seconds(),
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

                # Capture git HEAD as baseline for validation delta scoping
                # (issue #696). Allows WorkAnalyzer to later compute
                # git diff <baseline>..HEAD --name-only and load only the
                # files this agent changed, not the entire merged codebase.
                _project_root_for_baseline: Optional[str] = None
                if hasattr(state, "kanban_client") and state.kanban_client:
                    _ws = state.kanban_client._load_workspace_state()
                    if _ws and "project_root" in _ws:
                        _project_root_for_baseline = _ws["project_root"]
                if not _project_root_for_baseline and (
                    hasattr(state, "workspace_manager")
                    and state.workspace_manager
                    and hasattr(state.workspace_manager, "project_config")
                    and state.workspace_manager.project_config
                ):
                    _project_root_for_baseline = (
                        str(state.workspace_manager.project_config.main_workspace or "")
                        or None
                    )
                if _project_root_for_baseline:
                    _capture_baseline_commit(
                        assignment, _project_root_for_baseline, agent_id=agent_id
                    )

                agent = state.agent_status[agent_id]
                agent.current_tasks = [optimal_task]

                # Persist assignment. ``baseline_commit`` rides along
                # (issue #730) because the completion path merges only
                # ``baseline..HEAD`` for this card. A Marcus restart
                # mid-card clears ``state.agent_tasks``, and without the
                # persisted copy the merge would silently widen back to
                # the whole session branch — carrying any card this
                # session had refused into ``main``.
                await state.assignment_persistence.save_assignment(
                    agent_id,
                    optimal_task.id,
                    {
                        "name": optimal_task.name,
                        "priority": optimal_task.priority.value,
                        "estimated_hours": optimal_task.estimated_hours,
                        "baseline_commit": assignment.baseline_commit,
                    },
                )
                # And again keyed by TASK, in a store the completion does
                # not delete. A completion releases the assignment before
                # it merges, so an agent sent back to resolve a merge
                # conflict would otherwise report again with no baseline
                # and have its whole branch merged (Codex P1 on PR #790).
                if assignment.baseline_commit:
                    await state.assignment_persistence.record_task_baseline(
                        optimal_task.id, agent_id, assignment.baseline_commit
                    )

                # ADR-0012 D11 fencing token. 0 means "no epoch" and never
                # compares equal to a real claim, so a deployment without a
                # lease manager simply never fences.
                assigned_lease_epoch = 0

                # Create lease for this assignment if lease manager available
                if not (hasattr(state, "lease_manager") and state.lease_manager):
                    # Issue #763: a deployment without a lease manager
                    # cannot recover a dead agent's task. Say so, once per
                    # assignment, instead of failing silently.
                    logger.warning(
                        f"No lease manager: task {optimal_task.id} assigned to "
                        f"{agent_id} without a lease; if this agent dies its "
                        f"task will not be recovered (issue #763)."
                    )
                if hasattr(state, "lease_manager") and state.lease_manager:
                    lease = await state.lease_manager.create_lease(
                        optimal_task.id, agent_id, optimal_task
                    )
                    logger.info(
                        f"Created lease for task {optimal_task.id} "
                        f"(expires: {lease.lease_expires.isoformat()}, "
                        f"epoch: {lease.lease_epoch})"
                    )
                    # ADR-0012 D11: hand the fencing token to the agent.
                    # It carries this back on every report for this task,
                    # which is how Marcus tells its reports apart from a
                    # replacement claimant's after a false recovery.
                    assigned_lease_epoch = lease.lease_epoch

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
                    # ADR-0012 D11: the agent carries this back on every
                    # report_task_progress call for this task.
                    "lease_epoch": assigned_lease_epoch,
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
                        # #737: typed tasks dispatch on task_type, work
                        # from the board-written inputs (e.g. the claim
                        # a verify task checks), and must satisfy
                        # output_schema to close — all three belong in
                        # the assignment itself, not behind a second
                        # context call.
                        "task_type": getattr(optimal_task, "task_type", "implement"),
                        "inputs": dict(getattr(optimal_task, "inputs", {}) or {}),
                        "output_schema": getattr(optimal_task, "output_schema", None),
                    },
                }

                # Frontier width on every reply, not only the no-work one
                # (#804): see ``_frontier_for_agent``.
                _frontier = await _frontier_for_agent(agent_id, state)
                if _frontier is not None:
                    response["frontier"] = _frontier
                _mark("frontier")

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
            # The drain signal (epic #706, launcher design piece 4): decided
            # by the server from the board, not by a model calling a tool.
            # A project with no claimable task and no work in flight tells
            # the worker to go home; anything else falls through to the
            # retry below. A failure here must neither strand nor eject a
            # worker, so it degrades to the retry.
            _drain_project_id = (
                state.agent_project_map.get(agent_id, "")
                if hasattr(state, "agent_project_map")
                else ""
            )
            _drain_verdict = None
            if _drain_project_id:
                try:
                    _drain_verdict = await compute_board_drain(state, _drain_project_id)
                except Exception as _drain_error:  # noqa: BLE001 - degrade to retry
                    logger.error(f"Drain verdict failed for {agent_id}: {_drain_error}")
            if _drain_verdict is not None and _drain_verdict.drained:
                sep = "=" * 70
                if _drain_verdict.reason == "complete":
                    headline = "BOARD DRAINED: all tasks are done."
                    detail = (
                        f"All {_drain_verdict.tasks_total} tasks in your project "
                        "are complete and no work is in flight."
                    )
                else:
                    remaining = _drain_verdict.tasks_total - _drain_verdict.tasks_done
                    headline = "BOARD DRAINED: the remaining work cannot proceed."
                    detail = (
                        f"{_drain_verdict.tasks_done} of {_drain_verdict.tasks_total} "
                        f"tasks are done; {remaining} remain but none can be "
                        f"claimed ({_drain_verdict.tasks_blocked} blocked, "
                        f"{_drain_verdict.tasks_unclaimable} waiting on blocked "
                        "work) and nothing is in flight. An operator must "
                        "intervene; there is nothing more for you to do."
                    )
                logger.info(
                    f"BOARD DRAINED for {agent_id} (project {_drain_project_id}): "
                    f"{_drain_verdict.to_dict()}"
                )
                conversation_logger.log_worker_message(
                    agent_id,
                    "from_pm",
                    headline,
                    {"drain": _drain_verdict.to_dict(), **project_context},
                )
                _close_run_on_drain(_drain_project_id, _drain_verdict)
                return {
                    "success": False,
                    "status": "BOARD_DRAINED",
                    "should_exit": True,
                    "drain": _drain_verdict.to_dict(),
                    "message": (
                        f"\n\n{sep}\n{headline}\n{sep}\n\n{detail}\n\n"
                        "REQUIRED ACTION:\n"
                        "1. Print a brief summary of your contributions\n"
                        "2. Exit. Do NOT retry or request more tasks.\n\n"
                        f"{sep}\n"
                    ),
                }

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
            if _drain_verdict is not None:
                # Why the board is not drained yet, for the harness and logs.
                response["drain"] = _drain_verdict.to_dict()

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
    #
    # Attribute the validation LLM cost to the task's project and open run
    # so it reaches Cato instead of 'unassigned' (#768). WorkAnalyzer calls
    # the model through LLMAbstraction, which already records a
    # token_events row; this context is what gives that row a project/run.
    _validate_cost_ctx = await _planner_cost_context(
        state, task=task, agent_id=agent_id
    )
    with _validate_cost_ctx:
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
    baseline_commit: Optional[str] = None,
) -> Optional[bool]:
    """
    Check whether the agent committed code for the card it is completing.

    Used to detect false task completions where an agent calls
    report_task_progress(status='completed') without having committed
    any implementation code (dashboard-v88 post-mortem: agent_unicorn_3
    reported two tasks done with zero commits; UD2 had to rescue both).

    With a ``baseline_commit`` the question is per card: did the branch
    gain commits **of its own** since this card was claimed? Commits the
    agent merged in from ``main`` are excluded, or an agent could satisfy
    the gate by integrating another session's work and writing nothing.
    Without a baseline it falls back to the branch-level question, "is
    the branch ahead of main".

    That fallback goes vacuous under ADR-0012 D1 (issue #730): one
    session branch spans many cards, so once any earlier commit is left
    unmerged the branch stays ahead of main and every later card passes
    the gate without the agent writing a line. Always pass the baseline
    when it is known.

    Parameters
    ----------
    agent_id : str
        Agent identifier. Branch name is ``marcus/{agent_id}``.
    project_root : str
        Absolute path to the implementation git repository.
    baseline_commit : Optional[str]
        The branch tip captured when this card was claimed. When set and
        resolvable, the check counts commits in ``baseline..branch``.

    Returns
    -------
    Optional[bool]
        True  — the agent has commits for this card (or, without a
                baseline, the branch is ahead of main).
        False — the branch exists but holds no commits for this card.
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

        # Count commits for this card when the baseline is known,
        # otherwise fall back to the branch-level comparison.
        scoped_args = ["main.." + branch]
        if baseline_commit and _commit_exists(repo, baseline_commit):
            # Reachable from the branch but from neither the baseline nor
            # main. Excluding main matters: an agent that merges main as
            # its contract asks and then commits nothing of its own would
            # otherwise pass this gate on main's commits (Codex P2 on PR
            # #790). It is the same exclusion the footprint's range uses.
            scoped_args = [branch, "--not", baseline_commit, "main"]
        elif baseline_commit:
            logger.warning(
                "[commit_gate] baseline %s for %s is not a commit in %s; "
                "falling back to the branch-level check (issue #730)",
                baseline_commit[:8],
                branch,
                repo,
            )

        log_result = _sp.run(
            ["git", "rev-list", *scoped_args],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if log_result.returncode != 0:
            return None
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
    """Retry the per-task landing of a BLOCKED-by-merge-conflict task.

    Bug #651 follow-up. PR #653 marked tasks BLOCKED on merge failure,
    which closed the kanban/filesystem divergence but left them
    unrecoverable: verify-snake-8 (test71, 2026-05-25) hit one such
    conflict and the old runner spawned 49 ephemeral agents chasing the
    unclaimed downstream task before its stall watchdog gave up.

    **What this does, since issue #796.** It asks the landing again, and
    nothing else. If the branch has moved on since the failure, the
    task's work lands and the task goes DONE. If the conflict is still
    unresolved, the landing fails again and the task stays BLOCKED,
    which is the honest answer.

    The branch moving on is the expected case rather than a lucky one:
    the blocker message tells the agent to resolve the conflict in its
    own worktree and report again, and this sweep rescues the task when
    the agent did the first part and went home without the second.

    **What it no longer does.** It used to rebase the branch onto
    ``main`` and fast-forward the whole branch in. That merged every
    commit on the branch, so a task whose completion Marcus had refused
    reached ``main`` through the rescue, which is issue #730's defect on
    a path #730 did not touch. It also broke ADR-0012 D2's rule that the
    mechanical path never resolves, rebases or recovers. The rebase
    existed to clear stale-base false conflicts, and the footprint
    landing (#790) already merges three-way against the common ancestor,
    which is what those conflicts needed.

    A consequence worth stating: **nothing in the agent's worktree is
    touched any more**. There is no stash, no reset and no rebase, so
    this sweep cannot destroy an agent's uncommitted work however it
    runs. The one guard that remains is to skip a worktree with a merge
    in progress, because its branch tip is still the commit that failed
    and retrying would only churn.

    Recovery flow:

    1. Read ``source_context.merge_conflict`` from the BLOCKED task.
    2. Resolve the worktree path
       (``<project_root>/../worktrees/<agent_id>``, the Marcus
       convention set by the session launcher), and skip it if a merge
       is in progress there.
    3. Recover the task's claim-time baseline and call
       :func:`_merge_agent_branch_to_main` with it, exactly as the
       completion path does. Without a baseline, stop: the landing would
       fall back to merging the whole branch, which is right when an
       agent is waiting for its work and wrong here, where nobody is.
    4. On success: transition the task to DONE on the kanban, clear
       ``merge_conflict`` from ``source_context``, and open any pending
       reconciliation card (ADR-0012 D11).
    5. On failure: leave the task BLOCKED; return None.

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
        ``{"success": True, "task_id": ..., "branch": ...,
        "method": "task_scoped_landing"}`` when the task recovered.
        ``None`` on every failure path, which callers treat as "the task
        stays BLOCKED".

    Notes
    -----
    Best effort and never raises: a task that cannot be landed stays
    BLOCKED, which is where it already was.
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

    # The blocker message asks the agent to resolve the conflict in this
    # same worktree (``git merge main``, edit, ``git add``, ``git
    # commit``). This sweep runs on any other agent's no-task poll, so a
    # resolution in progress must be left alone: its branch tip is still
    # the commit that failed, so retrying now would fail again and churn.
    try:
        merge_in_progress = _sp.run(
            ["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (_sp.SubprocessError, OSError) as exc:
        logger.warning(
            "[recovery] %s: could not check for a merge in progress: %s; "
            "skipping auto-recovery to be safe",
            task.id,
            exc,
        )
        return None
    if merge_in_progress.returncode == 0:
        logger.info(
            "[recovery] %s: manual merge resolution in progress in %s; "
            "leaving BLOCKED so the agent's work is preserved",
            task.id,
            worktree,
        )
        return None

    # Issue #796: retry the same per-task landing the completion path
    # uses, and nothing else.
    #
    # This used to rebase the branch onto ``main`` and fast-forward the
    # whole branch in. That merged every commit on the branch, so a task
    # whose completion Marcus had refused reached ``main`` through the
    # rescue — issue #730's defect on a path #730 did not touch. It also
    # broke ADR-0012 D2's rule that the mechanical path never resolves,
    # rebases or recovers, which a rebase is the clearest case of.
    #
    # The rebase existed to clear stale-base false conflicts, and the
    # footprint landing (#790) already merges three-way against the
    # common ancestor, which is what those conflicts needed. So the
    # rescue is now: ask the landing again. It succeeds when the branch
    # has moved on since the failure, which is exactly what the blocker
    # message told the agent to do and what it may have done before
    # going home without re-reporting. A genuine unresolved conflict
    # fails again and the task stays BLOCKED, which is the honest answer.
    #
    # Nothing in the agent's worktree is touched, so there is no stash,
    # no reset and no state for this sweep to destroy.
    baseline = await _baseline_commit_for_task(state, agent_id, task.id)
    if not baseline:
        # The landing falls back to merging the whole branch when it has
        # no baseline, which is right on the completion path (an agent is
        # waiting and its work should not be dropped) and wrong here:
        # nobody is waiting, and widening is the defect this path exists
        # to stop. Leave it BLOCKED for an operator instead.
        logger.warning(
            "[recovery] %s: no baseline recorded for %s, so the landing "
            "could only be widened to the whole branch; leaving BLOCKED "
            "(issue #796)",
            task.id,
            agent_id,
        )
        return None

    logger.info(
        "[recovery] %s: retrying the per-task landing of %s from baseline %s",
        task.id,
        branch,
        baseline[:8],
    )
    landed = await _merge_agent_branch_to_main(
        agent_id, task.id, state, baseline_commit=baseline
    )
    if landed is None:
        logger.info(
            "[recovery] %s: nothing to land for %s (no branch or no repo); "
            "leaving BLOCKED",
            task.id,
            branch,
        )
        return None
    if not landed.get("success"):
        logger.info(
            "[recovery] %s: %s still does not land (%s); leaving BLOCKED",
            task.id,
            branch,
            landed.get("error"),
        )
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
            "[recovery] %s: kanban update failed after landing: %s",
            task.id,
            exc,
        )
        return None

    logger.info(
        "[recovery] %s: recovered; this task's work from %s landed on main",
        task.id,
        branch,
    )
    # ADR-0012 D11: this path marks the task DONE without going through
    # ``report_task_progress``, so a displaced session's refusal recorded
    # while this replacement was conflicted would never get its card
    # (Codex P2 on PR #794). Both implementations exist now.
    await maybe_open_reconciliation_card(state, task, task.id)
    return {
        "success": True,
        "task_id": task.id,
        "branch": branch,
        "method": "task_scoped_landing",
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


def _is_ancestor(repo: Any, candidate: str, descendant: str) -> bool:
    """Report whether ``candidate`` is an ancestor of ``descendant``.

    Parameters
    ----------
    repo : Any
        Repository path (str or Path) to run git in.
    candidate : str
        The possible ancestor.
    descendant : str
        The ref it may be an ancestor of.

    Returns
    -------
    bool
        True only when git confirms the ancestry. A git failure answers
        False, which takes the cautious branch rather than one Marcus
        cannot justify.
    """
    try:
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", candidate, descendant],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - answer "not an ancestor"
        logger.debug(
            f"Could not test ancestry of {candidate[:8]} against "
            f"{descendant} in {repo}: {exc}"
        )
        return False
    return ancestry.returncode == 0


def _card_footprint(repo: Any, baseline_commit: str, tip: str) -> Optional[List[str]]:
    """List the files this task touched, from its baseline to its tip.

    The footprint, not the commit list, is what gets landed. Working from
    the files rather than replaying commits is what makes a resolution the
    agent already made survive: the content of the agent's tree is taken
    as it stands, and no intermediate state is reapplied.

    Two details that an endpoint-to-endpoint diff gets wrong, both found
    in review of PR #790:

    * **Renames.** Git's rename detection reports only the destination,
      so ``old.py -> new.py`` would land as two files with the source
      still present. ``--no-renames`` makes both endpoints appear.
    * **A deliberate revert.** An agent that integrates ``main``, then
      decides a file should go back to the content it had at claim time,
      leaves ``baseline..tip`` empty for that file while ``main`` still
      holds the content it is reverting. Collecting the paths each commit
      in the range touched catches it; the endpoint diff cannot.

    Paths that only ``main``'s own merged-in commits touched can come
    along, which is harmless: taking the tip's version of such a path
    copies the content the agent already integrated, and in the
    three-way case the patch for it is empty.

    Parameters
    ----------
    repo : Any
        Repository path (str or Path) to run git in.
    baseline_commit : str
        The branch tip captured when the task was claimed.
    tip : str
        The branch tip now.

    Returns
    -------
    Optional[List[str]]
        Paths the task touched, possibly empty. None when git could not
        answer, which the caller treats as a refusal rather than widening
        the merge.
    """
    paths: List[str] = []
    seen: Set[str] = set()
    for command in (
        # Every path any commit in the range touched, which includes a
        # path reverted to its baseline content.
        [
            "git",
            "log",
            "--name-only",
            "--no-renames",
            "--format=",
            "-z",
            f"{baseline_commit}..{tip}",
        ],
        # And the endpoint difference, which covers a path changed with
        # no commit of its own in the range (an amended or squashed tip).
        [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            f"{baseline_commit}..{tip}",
        ],
    ):
        try:
            listing = subprocess.run(
                command,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001 - caller refuses
            logger.warning("[worktree] Could not read the task's footprint: %s", exc)
            return None
        if listing.returncode != 0:
            logger.warning(
                "[worktree] Could not read the task's footprint: %s",
                listing.stderr.strip(),
            )
            return None
        for path in listing.stdout.split("\0"):
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _take_tip_paths(repo: Any, tip: str, paths: List[str]) -> Optional[str]:
    """Copy ``tip``'s version of ``paths`` into the current tree.

    Used when the agent has already integrated the current ``main``: its
    tree is then strictly newer for everything it touched, so there is
    nothing to merge and no conflict to have. A path the task deleted is
    deleted here too.

    Parameters
    ----------
    repo : Any
        Repository path (str or Path), checked out on the merge target.
    tip : str
        The agent's branch tip.
    paths : List[str]
        The task's footprint.

    Returns
    -------
    Optional[str]
        None on success, or the first error message.
    """
    for path in paths:
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{tip}:{path}"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if exists.returncode == 0:
            taken = subprocess.run(
                ["git", "checkout", tip, "--", path],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            if taken.returncode != 0:
                return f"could not take {path}: {taken.stderr.strip()}"
        else:
            subprocess.run(
                ["git", "rm", "-q", "--ignore-unmatch", "--", path],
                cwd=repo,
                capture_output=True,
                text=True,
            )
    return None


def _blob_at(repo: Any, rev: str, path: str) -> Optional[str]:
    """Return the blob sha of ``path`` at ``rev``, or None when absent.

    Parameters
    ----------
    repo : Any
        Repository path (str or Path) to run git in.
    rev : str
        Revision to read.
    path : str
        Path inside that tree.

    Returns
    -------
    Optional[str]
        The blob sha, or None when the path does not exist there.
    """
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{rev}:{path}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return probe.stdout.strip() or None


def _remove_paths_the_task_deleted(
    repo: Any,
    baseline_commit: str,
    tip: str,
    paths: List[str],
) -> None:
    """Delete footprint paths the task removed, and only those.

    A three-way patch cannot always express a deletion: when the common
    ancestor predates the file, the diff has no hunk for it and the file
    survives on the merge target. A rename is the ordinary case, where
    the task removed ``old.py`` and added ``new.py``.

    The deletion has to be established against the **task's baseline**,
    not merely "absent from the tip". A path the task added and then
    removed inside its own range is absent from the tip too, and if
    another session independently added that same path to the merge
    target meanwhile, treating absence as a deletion wipes their file
    and commits it as a success (Codex P1 on PR #790).

    A path the task deleted that the target has since **changed** is left
    in place: removing it would discard a concurrent edit silently, and
    deciding between a deletion and an edit is a judgement call, not
    something the mechanical path settles.

    Parameters
    ----------
    repo : Any
        Repository path (str or Path), checked out on the merge target.
    baseline_commit : str
        The branch tip captured when the task was claimed.
    tip : str
        The agent's branch tip.
    paths : List[str]
        The task's footprint.
    """
    for path in paths:
        if _blob_at(repo, tip, path) is not None:
            continue
        at_baseline = _blob_at(repo, baseline_commit, path)
        if at_baseline is None:
            # The task never started with this path, so its absence from
            # the tip is not a deletion the task made.
            continue
        on_target = _blob_at(repo, "HEAD", path)
        if on_target is None:
            continue
        if on_target != at_baseline:
            logger.warning(
                "[worktree] Task deleted %s but the merge target has changed "
                "it since; leaving it in place rather than discarding that "
                "change",
                path,
            )
            continue
        subprocess.run(
            ["git", "rm", "-q", "--ignore-unmatch", "--", path],
            cwd=repo,
            capture_output=True,
            text=True,
        )


def _apply_footprint_three_way(
    repo: Any, base: str, tip: str, paths: List[str]
) -> Optional[str]:
    """Merge ``tip``'s footprint into the current tree against ``base``.

    Used when ``main`` has moved since the agent last integrated it, so
    there are genuinely concurrent changes. The patch is generated with
    ``--binary`` so an image or other binary asset in the footprint
    transfers rather than failing to apply, and it is applied with a
    three-way merge so a concurrent edit elsewhere in the same file is
    preserved instead of clobbered.

    Parameters
    ----------
    repo : Any
        Repository path (str or Path), checked out on the merge target.
    base : str
        Common ancestor to merge against.
    tip : str
        The agent's branch tip.
    paths : List[str]
        The task's footprint.

    Returns
    -------
    Optional[str]
        None on success, or the conflict/error detail.
    """
    patch = subprocess.run(
        ["git", "diff", "--binary", "--no-renames", f"{base}..{tip}", "--", *paths],
        cwd=repo,
        capture_output=True,
    )
    if patch.returncode != 0:
        return (patch.stderr or b"").decode("utf-8", "replace").strip()
    if not patch.stdout.strip():
        return None
    applied = subprocess.run(
        ["git", "apply", "--3way", "--index"],
        cwd=repo,
        input=patch.stdout,
        capture_output=True,
    )
    if applied.returncode != 0:
        detail = (applied.stderr or applied.stdout or b"").decode("utf-8", "replace")
        return detail.strip()
    return None


def _merge_card_range(
    *,
    repo: Any,
    branch: str,
    agent_id: str,
    task_id: str,
    baseline_commit: str,
    card_commits: List[str],
    pre_merge_sha: str,
) -> Dict[str, Any]:
    """Land one task's changes on the merge target, all or nothing.

    What gets landed is the task's **footprint**: the files it changed
    between the baseline captured at claim and its branch tip. Working
    from files rather than replaying commits is deliberate — a replay
    reapplies intermediate states that a later commit in the same range
    already fixed, so a task the agent had finished resolving would
    conflict on its own history.

    Two cases, decided by whether the agent has integrated the current
    merge target:

    * **It has** (the target is an ancestor of the branch tip, which is
      the normal case because the harness pulls before each task and the
      contract asks the agent to as well). The agent's tree is then
      strictly newer for everything it touched, so its version of the
      footprint is taken as it stands. No merge, no conflict, and any
      resolution the agent made is preserved exactly.
    * **It has not** (the target moved after the agent's last
      integration). The footprint is merged three-way against their
      common ancestor, with a binary-capable patch, so a concurrent
      change elsewhere in the same file survives. A real conflict rolls
      the target back and hands the task to the agent, whose own
      ``git merge main`` then moves it into the first case.

    Work from a task whose completion Marcus refused stays out either
    way, because it is not in this task's footprint — unless it sits in
    the same file as this task's own changes, in which case the two are
    inseparable in that file's content. That residual is named in
    ADR-0012 D2 rather than guessed at.

    Parameters
    ----------
    repo : Any
        Repository path (str or Path), already checked out on the merge
        target with a clean working tree.
    branch : str
        The session branch, ``marcus/{agent_id}``.
    agent_id : str
        Completing agent, named in the landing commit and the failure.
    task_id : str
        The task being completed.
    baseline_commit : str
        Branch tip captured when the task was claimed.
    card_commits : List[str]
        The task's commits, oldest first, used for provenance. May be
        empty.
    pre_merge_sha : str
        Where the target stood before this call, used to roll back.

    Returns
    -------
    Dict[str, Any]
        ``{"success": True, "commits_merged": n, ...}`` when the task
        landed, otherwise a failure dict carrying ``error`` and
        ``message`` with the target restored to ``pre_merge_sha``.
    """
    tip_read = subprocess.run(
        ["git", "rev-parse", branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    tip = tip_read.stdout.strip() if tip_read.returncode == 0 else ""

    def _rollback(paths: Optional[List[str]] = None) -> None:
        """Restore the merge target to where it stood before this call.

        Cleanup is restricted to the paths this landing touched. A
        repository-wide ``git clean -fd`` would delete untracked files
        that were already there and have nothing to do with the task,
        while the refusal says main was left unchanged (Codex P1 on PR
        #790).
        """
        subprocess.run(
            ["git", "reset", "--hard", pre_merge_sha],
            cwd=repo,
            capture_output=True,
        )
        if paths:
            subprocess.run(
                ["git", "clean", "-qfd", "--", *paths],
                cwd=repo,
                capture_output=True,
            )

    def _refuse(
        error: str,
        detail: str,
        message: str,
        paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Roll back and build the refusal."""
        _rollback(paths)
        return {
            "success": False,
            "error": error,
            "commits_merged": 0,
            "detail": detail,
            "message": message,
        }

    footprint = _card_footprint(repo, baseline_commit, tip) if tip else None
    if footprint is None:
        logger.error(
            "[worktree] Could not scope task %s on %s; refusing rather than "
            "widening the merge (issue #730)",
            task_id,
            branch,
        )
        return _refuse(
            "range_merge_failed",
            "footprint unreadable",
            "Your task passed validation but Marcus could not work out "
            "which files it changed, so it merged nothing. Main was left "
            "unchanged. This needs an operator, not another attempt.",
        )

    if not footprint:
        logger.info(
            "[worktree] Task %s changed no files on %s; nothing to merge",
            task_id,
            branch,
        )
        return {
            "success": True,
            "commits_merged": len(card_commits),
            "paths_landed": 0,
        }

    if _is_ancestor(repo, pre_merge_sha, tip):
        mode = "take-tip"
        failure = _take_tip_paths(repo, tip, footprint)
        if failure is not None:
            logger.error(
                "[worktree] Could not take task %s's files from %s: %s",
                task_id,
                branch,
                failure,
            )
            return _refuse(
                "range_merge_failed",
                failure,
                f"Your task passed validation but Marcus could not copy its "
                f"files onto main: {failure}. Main was left unchanged.",
                paths=footprint,
            )
    else:
        mode = "three-way"
        base_read = subprocess.run(
            ["git", "merge-base", pre_merge_sha, tip],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if base_read.returncode != 0:
            return _refuse(
                "range_merge_failed",
                base_read.stderr.strip(),
                "Your task passed validation but Marcus could not find a "
                "common ancestor between your branch and main, so it merged "
                "nothing. Main was left unchanged.",
                paths=footprint,
            )
        conflict = _apply_footprint_three_way(
            repo, base_read.stdout.strip(), tip, footprint
        )
        if conflict is None:
            # A deletion the patch could not carry, because the common
            # ancestor predates the file. A rename is the ordinary case.
            _remove_paths_the_task_deleted(repo, baseline_commit, tip, footprint)
        if conflict is not None:
            logger.warning(
                "[worktree] Conflict landing task %s from %s: %s",
                task_id,
                branch,
                conflict,
            )
            return _refuse(
                "merge_conflict",
                conflict,
                f"Your task passed validation but its changes conflict with "
                f"main:\n{conflict}\nMain was left unchanged. Please "
                f"resolve:\n"
                f"  git merge main\n"
                f"  (resolve conflicts in your editor)\n"
                f"  git add . && git commit\n"
                f"Then report completion again.",
                paths=footprint,
            )

    logger.info(
        "[worktree] Landing %d file(s) for task %s from %s (%s, base %s)",
        len(footprint),
        task_id,
        branch,
        mode,
        baseline_commit[:8],
    )

    subjects = []
    commit_times: List[int] = []
    for sha in card_commits:
        described = subprocess.run(
            ["git", "log", "-1", "--format=%ct%x00%s", sha],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout.strip()
        when, _, subject = described.partition("\x00")
        subjects.append(f"- {sha[:8]} {subject}")
        if when.isdigit():
            commit_times.append(int(when))
    body = (
        f"Range {baseline_commit[:8]}..{tip[:8]} on {branch}, "
        f"{len(footprint)} file(s), {len(card_commits)} commit(s):\n"
        + "\n".join(subjects)
    )

    subprocess.run(
        ["git", "add", "-A", "--", *footprint], cwd=repo, capture_output=True
    )
    commit = subprocess.run(
        [
            "git",
            "commit",
            "-m",
            f"Land task {task_id} from {branch} (agent {agent_id})",
            "-m",
            body,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        combined = f"{commit.stdout}\n{commit.stderr}"
        if "nothing to commit" in combined or "no changes added" in combined:
            # The task's files are already byte-identical on main (another
            # session landed the same content, or the work was a no-op).
            logger.info(
                "[worktree] Task %s brought no net change to main",
                task_id,
            )
            _rollback(footprint)
            return {
                "success": True,
                "commits_merged": len(card_commits),
                "paths_landed": 0,
                "net_change": False,
            }
        logger.error(
            "[worktree] Could not commit the landing for task %s: %s",
            task_id,
            combined.strip(),
        )
        return _refuse(
            "range_merge_failed",
            combined.strip(),
            f"Your task passed validation but Marcus could not commit it "
            f"onto main: {combined.strip()}. Main was left unchanged.",
            paths=footprint,
        )

    # ADR-0012 D8: how well the checkpoint duty was kept on this task.
    # Measured, never gated — a gate on self-reported virtue invites
    # compliance theatre (D4). The gap from claim to the first commit is
    # deliberately not counted here: the silence timeout already answers
    # that, and the baseline commit's own timestamp can be much older
    # than the claim.
    max_gap_minutes: Optional[float] = None
    if len(commit_times) > 1:
        ordered = sorted(commit_times)
        max_gap_minutes = round(
            max(b - a for a, b in zip(ordered, ordered[1:])) / 60.0, 1
        )
    logger.info(
        "[worktree] Landed %d file(s) for task %s (agent %s) on main; "
        "longest gap between checkpoints: %s",
        len(footprint),
        task_id,
        agent_id,
        f"{max_gap_minutes} min" if max_gap_minutes is not None else "n/a",
    )
    return {
        "success": True,
        "commits_merged": len(card_commits),
        "paths_landed": len(footprint),
        "mode": mode,
        "checkpoint_commits": len(card_commits),
        "max_checkpoint_gap_minutes": max_gap_minutes,
    }


async def _merge_agent_branch_to_main(
    agent_id: str,
    task_id: str,
    state: Any,
    baseline_commit: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Land the completing card's commits on main.

    Convention: if branch marcus/{agent_id} exists, the agent used a
    worktree, and its work has to reach ``main`` for dependent tasks to
    build on it.

    **What gets merged (issue #730).** With a ``baseline_commit`` —
    the branch tip captured when this card was claimed — only the
    changes this card added are merged, as one commit on ``main``.
    Without a baseline the whole branch is merged, which is the
    pre-#730 behaviour.

    The difference matters once a branch outlives a single card
    (ADR-0012 D1). A card whose completion Marcus **refused** leaves
    its commits on the session branch; a whole-branch merge on the next
    accepted card would carry that refused work into ``main``, which
    nobody decided. Merging per card keeps Marcus's accept/reject
    decision and the contents of ``main`` in agreement.

    If the replay conflicts, ``main`` is restored to exactly where it
    was and a failure is returned — the agent resolves the conflict and
    reports completion again.

    Parameters
    ----------
    agent_id : str
        Completing agent. Its branch is ``marcus/{agent_id}``.
    task_id : str
        The card being completed, used in the log and commit trailers.
    state : Any
        Marcus server state, read for the workspace's ``project_root``.
    baseline_commit : Optional[str]
        Branch tip captured at claim time. When absent or unresolvable,
        the merge falls back to the whole branch.

    Returns
    -------
    Optional[Dict[str, Any]]
        None when no merge is needed (no branch, no repo, git
        unavailable). ``{"success": True, ...}`` when the work landed.
        ``{"success": False, "error": "merge_conflict", ...}`` when it
        did not, with ``main`` untouched.

    See: https://github.com/lwgray/marcus/issues/250,
    https://github.com/lwgray/marcus/issues/730
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

        # Where main stands before anything is applied. A failed replay
        # resets here, so a conflict can never leave main half-merged.
        main_before = _sp.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        pre_merge_sha = (
            main_before.stdout.strip() if main_before.returncode == 0 else None
        )

        # Per-card range (issue #730), when the baseline is known.
        card_commits: Optional[List[str]] = None
        if baseline_commit:
            card_commits = _card_commit_range(repo, branch, baseline_commit)
            if card_commits is None:
                logger.warning(
                    "[worktree] Could not scope task %s to baseline %s on %s; "
                    "merging the whole branch instead (issue #730)",
                    task_id,
                    baseline_commit[:8],
                    branch,
                )

        if card_commits is not None and pre_merge_sha and baseline_commit:
            return _merge_card_range(
                repo=repo,
                branch=branch,
                agent_id=agent_id,
                task_id=task_id,
                baseline_commit=baseline_commit,
                card_commits=card_commits,
                pre_merge_sha=pre_merge_sha,
            )

        # Fall back: merge the whole branch. Correct while a branch holds
        # exactly one card's commits; see #730 for why that stops being
        # true under session-scoped branches.
        logger.info(
            "[worktree] No baseline for task %s; merging all of %s",
            task_id,
            branch,
        )
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


EPOCH_COLLISION_COLLECTION = "epoch_collisions"
# One row per task, holding the epoch pairs already counted for it and
# their number. The count is read from here, by key, so no read has to
# scan the detail rows: a scan would truncate, because both persistence
# backends apply their limit around the filter (Codex P2 on PR #789).
EPOCH_COLLISION_COUNT_COLLECTION = "epoch_collision_counts"
# Serializes the read-modify-write of a task's row. Two superseded holders
# can report the same task at once, and the persistence backends execute
# each operation independently, so without this both would read the same
# row and one increment would be lost. The path is rare, so a single lock
# costs nothing (Codex P2 on PR #789).
_EPOCH_COLLISION_LOCK = asyncio.Lock()


async def _record_epoch_collision(
    state: Any, task_id: str, agent_id: str, lease_epoch: int, highest: int
) -> bool:
    """Record one observed epoch collision against its task.

    The count is what sizes D11's fence (ADR-0012): with D3's liveness
    dials a collision should be rare, and "rare" has to be a number, not
    an impression.

    The task's row carries the epoch pairs already counted, so the whole
    operation is one idempotent read-modify-write of a single row: a pair
    that is already listed is a no-op, an interrupted attempt leaves the
    row untouched and the next report repairs it, and the count can never
    drift from the pairs it is derived from. The detail row is written
    afterwards and purely for provenance, so losing it cannot lose a
    count.

    Parameters
    ----------
    state : Any
        Server state, providing ``persistence`` and the board in
        ``project_tasks``.
    task_id : str
        The task whose completion arrived under a superseded epoch.
    agent_id : str
        The reporting agent.
    lease_epoch : int
        The epoch the agent carried.
    highest : int
        The newest epoch for the task.

    Returns
    -------
    bool
        True when this observation was new and has been counted. False
        when the same epoch pair was already counted, when there is no
        persistence, or when the write failed; a failure is logged and
        never propagated, because bookkeeping must not fail a completion.
    """
    persistence = getattr(state, "persistence", None)
    if persistence is None:
        return False
    pair = f"{lease_epoch}:{highest}"
    async with _EPOCH_COLLISION_LOCK:
        try:
            row = await persistence.retrieve(EPOCH_COLLISION_COUNT_COLLECTION, task_id)
            listed = (row or {}).get("pairs")
            pairs = (
                [p for p in listed if isinstance(p, str)]
                if isinstance(listed, list)
                else []
            )
            if pair in pairs:
                return False
            pairs.append(pair)
            await persistence.store(
                EPOCH_COLLISION_COUNT_COLLECTION,
                task_id,
                {
                    "task_id": task_id,
                    "pairs": pairs,
                    "count": len(pairs),
                    "last_observed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:  # noqa: BLE001 - never break the report path
            logger.warning(f"Could not count the epoch collision for {task_id}: {exc}")
            return False
    await _store_epoch_collision_detail(
        state, persistence, task_id, agent_id, lease_epoch, highest, pair
    )
    return True


async def _store_epoch_collision_detail(
    state: Any,
    persistence: Any,
    task_id: str,
    agent_id: str,
    lease_epoch: int,
    highest: int,
    pair: str,
) -> None:
    """Write the provenance row for a counted collision, best effort.

    Separate from the count so a failure here cannot lose a count, and so
    the count's critical section stays one row. The project id is
    provenance taken from the task row, because
    ``state.active_project_id`` is not always set when a completion
    arrives (it was None in the live check for PR #789) and a project id
    comes in two spellings.
    """
    task = next(
        (
            t
            for t in getattr(state, "project_tasks", None) or []
            if getattr(t, "id", None) == task_id
        ),
        None,
    )
    project_id = getattr(task, "project_id", None) or getattr(
        state, "active_project_id", None
    )
    try:
        await persistence.store(
            EPOCH_COLLISION_COLLECTION,
            f"{task_id}:{pair}",
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "lease_epoch": lease_epoch,
                "highest_epoch": highest,
                "project_id": project_id,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:  # noqa: BLE001 - provenance only
        logger.warning(
            f"Counted the epoch collision for {task_id} but could not record "
            f"its detail row: {exc}"
        )


async def count_epoch_collisions(state: Any, task_ids: Set[str]) -> int:
    """Return how many epoch collisions were counted on the given tasks.

    Reads one row per task, by key: counting by task makes the figure
    right even when a detail row's project stamp is missing, and reading
    by key means no scan can truncate it.

    Parameters
    ----------
    state : Any
        Server state, providing ``persistence``.
    task_ids : Set[str]
        The tasks to count, normally every task on the project's board.

    Returns
    -------
    int
        The number of distinct observations across those tasks, or 0 when
        there is no persistence. A task whose row cannot be read is
        skipped with a warning rather than failing the caller.
    """
    persistence = getattr(state, "persistence", None)
    if persistence is None or not task_ids:
        return 0
    total = 0
    for task_id in task_ids:
        try:
            row = await persistence.retrieve(EPOCH_COLLISION_COUNT_COLLECTION, task_id)
        except Exception as exc:  # noqa: BLE001 - a count must never fail a read
            logger.warning(f"Could not read epoch collisions for {task_id}: {exc}")
            continue
        counted = (row or {}).get("count")
        if isinstance(counted, int):
            total += counted
    return total


async def _board_tasks_for_fence(state: Any) -> List[Task]:
    """Read the board's tasks for the fence, tolerating a board failure.

    The fence needs the task only to name it on the reconciliation card,
    so a board read that fails must not turn into a failed completion.

    Parameters
    ----------
    state : Any
        Marcus server state.

    Returns
    -------
    List[Task]
        The board's tasks, or an empty list.
    """
    kanban_client = getattr(state, "kanban_client", None)
    if kanban_client is None:
        return []
    try:
        tasks = await kanban_client.get_all_tasks()
    except Exception as exc:  # noqa: BLE001 - naming the card is optional
        logger.debug("[fence] could not read the board: %s", exc)
        return []
    return list(tasks or [])


async def _record_refused_attempt(
    state: Any,
    task_id: str,
    task: Optional[Task],
    agent_id: str,
    lease_epoch: Optional[int],
) -> List[Dict[str, Any]]:
    """Stamp a refused completion on the task and return every one so far.

    The refusal itself is not the whole of D11 part 2: the work has to
    stay findable. A fenced session's commits are on its branch, and this
    record is what tells a later reader the branch exists and why it was
    refused — including when the task is recovered twice and several
    sessions are displaced.

    Parameters
    ----------
    state : Any
        Marcus server state.
    task_id : str
        The contested task.
    task : Optional[Task]
        The task object, when resolvable; its ``source_context`` is read
        so existing records are preserved.
    agent_id : str
        The session whose completion was refused.
    lease_epoch : Optional[int]
        The epoch it reported under.

    Returns
    -------
    List[Dict[str, Any]]
        Every refused attempt recorded for this task, oldest first.
    """
    attempt = {
        "agent_id": agent_id,
        "branch": f"marcus/{agent_id}",
        "lease_epoch": lease_epoch,
        "refused_at": datetime.now(timezone.utc).isoformat(),
    }

    # Marcus's own store is authoritative. Only the sqlite provider
    # round-trips ``source_context``, and a board refresh replaces cached
    # task objects, so a board-only record is lost on Planka, GitHub and
    # Linear — and the card may be opened much later, when the
    # replacement finally completes (Codex P2 on PR #794).
    attempts: List[Dict[str, Any]] = []
    persistence = getattr(state, "assignment_persistence", None)
    recorder = getattr(persistence, "record_refused_attempt", None)
    if recorder is not None:
        try:
            attempts = list(await recorder(task_id, attempt))
        except Exception as exc:  # noqa: BLE001 - fall back to the board stamp
            logger.error(
                "[fence] Could not persist the refused attempt for task %s: "
                "%s. Falling back to the board record, which some providers "
                "do not keep.",
                task_id,
                exc,
            )

    context = dict(getattr(task, "source_context", None) or {})
    if not attempts:
        attempts = [dict(a) for a in (context.get("refused_attempts") or [])]
        if not any(
            a.get("agent_id") == agent_id and a.get("lease_epoch") == lease_epoch
            for a in attempts
        ):
            attempts.append(attempt)
    context["refused_attempts"] = attempts
    if task is not None:
        task.source_context = context
    # And on the cached object, which can be a different instance: a
    # provider like sqlite hydrates fresh Task objects in
    # ``get_all_tasks()``, so stamping the board's copy never reaches
    # ``state.project_tasks`` — and that is the copy the acceptance hook
    # reads (Codex P2 on PR #794).
    for cached in getattr(state, "project_tasks", None) or []:
        if str(getattr(cached, "id", "")) == task_id and cached is not task:
            cached.source_context = context
    kanban_client = getattr(state, "kanban_client", None)
    if kanban_client is not None:
        try:
            await kanban_client.update_task(task_id, {"source_context": context})
        except Exception as exc:  # noqa: BLE001 - the in-memory stamp still holds
            logger.warning(
                "[fence] Could not persist the refused attempt for task %s: %s",
                task_id,
                exc,
            )
    return attempts


def _task_is_terminal(task: Optional[Task]) -> bool:
    """Report whether the replacement attempt has finished.

    The reconciliation card asks an agent to compare two implementations.
    While the replacement is still working there is only one, and `main`
    holds nothing for the task — a card opened then would send an agent to
    reconcile against an implementation that does not exist yet, and the
    replacement could land afterwards and overwrite the decision (Codex P1
    on PR #794). So the card waits for the task to reach a terminal state.

    Parameters
    ----------
    task : Optional[Task]
        The contested task.

    Returns
    -------
    bool
        True when the task is DONE.
    """
    return getattr(task, "status", None) == TaskStatus.DONE


async def _find_reconciliation_card(state: Any, task_id: str) -> Optional[str]:
    """Return the id of an open reconciliation card for ``task_id``, if any.

    Keeps the fence idempotent: a displaced session can report the same
    completion repeatedly, and each report must not add another card.

    Parameters
    ----------
    state : Any
        Marcus server state.
    task_id : str
        The contested task.

    Returns
    -------
    Optional[str]
        The existing card's id, or None when there is none.
    """
    marker = _RECONCILIATION_MARKER.format(task_id=task_id)
    for candidate in getattr(state, "project_tasks", None) or []:
        if marker in (getattr(candidate, "description", "") or ""):
            if getattr(candidate, "status", None) != TaskStatus.DONE:
                return str(getattr(candidate, "id", "")) or None
    return None


async def _open_reconciliation_card(
    state: Any,
    task: Optional[Task],
    task_id: str,
    attempts: List[Dict[str, Any]],
) -> Optional[str]:
    """Put the contested completions on the board instead of discarding them.

    ADR-0012 D11 part 2: a refused completion is **preserved and
    reconciled**, never thrown away. Two or more sessions have now built
    the same task, each on its own branch. Deciding which implementation
    survives is a judgement call, so it belongs to an agent on a board
    card, not to Marcus (Invariant #2).

    The card states what to compare and what to record, never how to
    decide. It is created at URGENT priority with no dependencies so it is
    claimable at once: until it is resolved, ``main`` holds one of several
    implementations and nobody has looked at the others.

    When a card already exists for this task — a task can be recovered
    more than once, displacing several sessions — it is **updated** to
    name the newly refused attempt rather than left as it was, or the
    reconciling agent would never hear about that branch (Codex P2 on PR
    #794).

    Parameters
    ----------
    state : Any
        Marcus server state.
    task : Optional[Task]
        The contested task, when it could be resolved. Used for its name
        and the accepted agent's id.
    task_id : str
        The contested task's id.
    attempts : List[Dict[str, Any]]
        Every refused attempt recorded for this task, oldest first.

    Returns
    -------
    Optional[str]
        The card's id, or None when the board write failed.
    """
    task_name = getattr(task, "name", None) or task_id
    accepted_agent = getattr(task, "assigned_to", None) or "another session"
    marker = _RECONCILIATION_MARKER.format(task_id=task_id)
    refused_lines = "\n".join(
        f"- `{a.get('branch')}` (agent {a.get('agent_id')}, "
        f"epoch {a.get('lease_epoch')}), refused {a.get('refused_at')}"
        for a in attempts
    )
    first_branch = (attempts[0].get("branch") if attempts else None) or "the branch"
    description = (
        f"More than one session completed this task. Decide which work "
        f"survives.\n\n"
        f"{marker}\n\n"
        f"**The task:** {task_name} (`{task_id}`)\n"
        f"**Accepted:** {accepted_agent}, whose work is already on `main`.\n"
        f"**Refused, and NOT merged:**\n{refused_lines}\n\n"
        f"**What to do:**\n"
        f"1. Read what is on `main` for this task, then read each refused "
        f"branch: `git log main..<branch>` and `git diff main..<branch>`.\n"
        f"2. Decide what the task should end up with: keep what is on "
        f"`main`, take a refused branch's work instead, or combine them.\n"
        f"3. Leave `main` holding that result, with the tests passing.\n"
        f"4. Call `log_decision` with what you kept, what you dropped, and "
        f"why. That record is the point of this card.\n\n"
        f"Nothing has been deleted. Every branch above is intact "
        f"(start with `{first_branch}`)."
    )

    kanban_client = getattr(state, "kanban_client", None)
    if kanban_client is None:
        return None

    # Nothing left to decide: every attempt in hand is already covered by
    # a card. A displaced session repeating its report after that card was
    # completed must not re-open a settled decision, while a genuinely new
    # refusal is uncovered and still gets one (Codex P2 on PR #794).
    pending = [a for a in attempts if not a.get("reconciled_by")]
    if not pending:
        previous = next(
            (
                a.get("reconciled_by")
                for a in reversed(attempts)
                if a.get("reconciled_by")
            ),
            None,
        )
        logger.info(
            "[fence] Every refused attempt on task %s is already covered by "
            "reconciliation card %s; not opening another",
            task_id,
            previous,
        )
        return str(previous) if previous else None

    async def _mark(card: str) -> None:
        """Record which attempts this card now covers."""
        persistence = getattr(state, "assignment_persistence", None)
        marker = getattr(persistence, "mark_refused_attempts_reconciled", None)
        if marker is None:
            return
        try:
            await marker(task_id, card, pending)
        except Exception as exc:  # noqa: BLE001 - the card still exists
            logger.warning(
                "[fence] Could not note that card %s covers task %s's "
                "refused attempts: %s",
                card,
                task_id,
                exc,
            )

    # One card per contested task: two overlapping refusals, or a refusal
    # overlapping the replacement's acceptance hook, would otherwise both
    # find nothing here and both create one.
    async with _reconciliation_lock(task_id):
        existing = await _find_reconciliation_card(state, task_id)
        if existing:
            try:
                await kanban_client.update_task(existing, {"description": description})
            except Exception as exc:  # noqa: BLE001 - never fail a report
                logger.warning(
                    "[fence] Could not update reconciliation card %s for "
                    "task %s: %s",
                    existing,
                    task_id,
                    exc,
                )
                return existing
            for candidate in getattr(state, "project_tasks", None) or []:
                if str(getattr(candidate, "id", "")) == existing:
                    candidate.description = description
            logger.info(
                "[fence] Updated reconciliation card %s for task %s: %d "
                "refused attempt(s) now named",
                existing,
                task_id,
                len(attempts),
            )
            await _mark(existing)
            return existing

        try:
            card = await kanban_client.create_task(
                {
                    "name": f"Reconcile two completions of: {task_name}",
                    "description": description,
                    "status": "todo",
                    "priority": "urgent",
                    # No implementation label: the deliverable is a
                    # decision, and one permitted outcome is "keep what is
                    # on main", which changes no files. Labelling it
                    # implementation would put it in front of the commit
                    # gate and the validation gate, neither of which it can
                    # satisfy (Codex P2 on PR #794).
                    "labels": [_RECONCILIATION_LABEL],
                    "dependencies": [],
                    "estimated_hours": 1.0,
                }
            )
        except Exception as exc:  # noqa: BLE001 - never fail a report
            logger.error(
                "[fence] Could not open a reconciliation card for task %s: "
                "%s. The refused work is still on its branch.",
                task_id,
                exc,
            )
            return None

        card_id = str(getattr(card, "id", "") or "")
        # Make it claimable without waiting for the next board refresh.
        if card_id and isinstance(getattr(state, "project_tasks", None), list):
            state.project_tasks.append(card)
        logger.info(
            "[fence] Opened reconciliation card %s for task %s (%d refused "
            "attempt(s))",
            card_id,
            task_id,
            len(attempts),
        )
        if card_id:
            await _mark(card_id)
        return card_id or None


async def maybe_open_reconciliation_card(
    state: Any,
    task: Optional[Task],
    task_id: str,
) -> Optional[str]:
    """Open the reconciliation card once a completion has been accepted.

    The other half of the fence's timing: a card opened while the
    replacement was still working would ask an agent to compare against
    an implementation that was not on ``main`` yet. So when a session is
    displaced mid-flight the refusal is recorded on the task, and the
    card is opened here — at the moment a completion for that task is
    accepted and both implementations exist.

    Does nothing when no attempt was refused, which is every ordinary
    completion.

    Parameters
    ----------
    state : Any
        Marcus server state.
    task : Optional[Task]
        The task whose completion was just accepted.
    task_id : str
        Its id.

    Returns
    -------
    Optional[str]
        The card's id when one was opened or updated, else None.
    """
    attempts = [
        dict(a)
        for a in (
            (getattr(task, "source_context", None) or {}).get("refused_attempts") or []
        )
    ]
    if not attempts:
        # Marcus's own store, which every provider shares.
        persistence = getattr(state, "assignment_persistence", None)
        reader = getattr(persistence, "get_refused_attempts", None)
        if reader is not None:
            try:
                attempts = [dict(a) for a in await reader(task_id)]
            except Exception as exc:  # noqa: BLE001 - try the board next
                logger.debug(
                    "[fence] Could not read refused attempts for task %s: %s",
                    task_id,
                    exc,
                )
    if not attempts:
        # The cached task can be a different object from the one the
        # refusal stamped: a provider like sqlite hydrates fresh Task
        # objects in ``get_all_tasks()``, and the record is persisted on
        # the board. Look there before concluding there was no contest
        # (Codex P2 on PR #794).
        board_task = next(
            (
                candidate
                for candidate in await _board_tasks_for_fence(state)
                if str(getattr(candidate, "id", "")) == task_id
            ),
            None,
        )
        attempts = [
            dict(a)
            for a in (
                (getattr(board_task, "source_context", None) or {}).get(
                    "refused_attempts"
                )
                or []
            )
        ]
        if attempts and board_task is not None:
            task = board_task
    if not attempts:
        return None
    logger.info(
        "[fence] Task %s was completed with %d refused attempt(s) on "
        "record; opening the reconciliation card now",
        task_id,
        len(attempts),
    )
    return await _open_reconciliation_card(state, task, task_id, attempts)


async def _fence_stale_epoch(
    state: Any,
    task: Optional[Task],
    task_id: str,
    agent_id: str,
    lease_epoch: Optional[int],
) -> Optional[Dict[str, Any]]:
    """Refuse a completion whose claim has been superseded (ADR-0012 D11).

    The fence sits where the resources are: this runs before the board's
    DONE write and before the merge into ``main``, and it decides from the
    **durable epoch high-water mark** rather than from in-memory ownership.
    Marcus's three in-memory ownership buckets have disagreed before
    (issue #485); the epoch sequence survives release and restart
    precisely so it can arbitrate.

    A report carrying no epoch cannot be fenced by token — it falls
    through to the ownership guard, which is the pre-D11 behaviour.

    Parameters
    ----------
    state : Any
        Marcus server state.
    task : Optional[Task]
        The completing task, when resolvable; used for the card.
    task_id : str
        The task being reported complete.
    agent_id : str
        The reporting session.
    lease_epoch : Optional[int]
        The epoch that session was issued at claim. ``None`` or 0 means
        the report carried no token.

    Returns
    -------
    Optional[Dict[str, Any]]
        None when the report may proceed. Otherwise the refusal to
        return to the caller, naming the reconciliation card.
    """
    if not lease_epoch:
        return None
    lease_manager = getattr(state, "lease_manager", None)
    if lease_manager is None:
        return None
    try:
        highest = await lease_manager.get_highest_epoch(task_id)
    except Exception as exc:  # noqa: BLE001 - never fail a report over this
        logger.warning(
            "[fence] Could not read the epoch high-water mark for task %s: "
            "%s. Falling through to the ownership guard.",
            task_id,
            exc,
        )
        return None
    if not highest or lease_epoch >= highest:
        return None

    logger.warning(
        "[fence] Refusing completion of task %s from %s: epoch %s is "
        "superseded by %s (ADR-0012 D11). The work stays on branch "
        "marcus/%s.",
        task_id,
        agent_id,
        lease_epoch,
        highest,
        agent_id,
    )
    attempts = await _record_refused_attempt(
        state, task_id, task, agent_id, lease_epoch
    )
    # The status in hand was read before the epoch lookup was awaited, so
    # the replacement may have finished in between — and its own
    # acceptance hook may have run before this refusal was recorded,
    # finding nothing. Re-read the board, or both reports finish with the
    # refusal stored and no card scheduled (Codex P2 on PR #794).
    if not _task_is_terminal(task):
        fresh = next(
            (
                candidate
                for candidate in await _board_tasks_for_fence(state)
                if str(getattr(candidate, "id", "")) == task_id
            ),
            None,
        )
        if fresh is not None and _task_is_terminal(fresh):
            logger.info(
                "[fence] Task %s finished while this refusal was being "
                "recorded; opening the reconciliation card now",
                task_id,
            )
            task = fresh
    # The card waits for the replacement attempt to finish. Opened while
    # that session is still working, it would send an agent to reconcile
    # against an implementation that is not on main yet, and the
    # replacement could land afterwards and overwrite the decision (Codex
    # P1 on PR #794). The refused attempt is on record either way, and
    # ``maybe_open_reconciliation_card`` opens the card at the moment a
    # completion for this task is accepted.
    card_id: Optional[str] = None
    if _task_is_terminal(task):
        card_id = await _open_reconciliation_card(state, task, task_id, attempts)
    else:
        logger.info(
            "[fence] Task %s is not finished yet (%s); the refused attempt "
            "from %s is recorded and the reconciliation card opens when a "
            "completion is accepted.",
            task_id,
            getattr(task, "status", None),
            agent_id,
        )
    return {
        "success": False,
        "status": "fenced_stale_epoch",
        "error": "superseded_claim",
        "task_id": task_id,
        "agent_id": agent_id,
        "lease_epoch": lease_epoch,
        "current_lease_epoch": highest,
        "reconciliation_task_id": card_id,
        "message": (
            f"Cannot complete task {task_id}: your claim (epoch "
            f"{lease_epoch}) was superseded by a later one (epoch "
            f"{highest}), so another session has been working this task. "
            f"Nothing of yours was deleted — your commits are on branch "
            f"marcus/{agent_id}"
            + (
                f", and reconciliation card {card_id} now asks an agent to "
                f"compare the attempts and keep the better work"
                if card_id
                else ", and it is recorded on the task so a reconciliation "
                "card names it once this task is finished"
            )
            + ". Request your next task to continue."
        ),
    }


async def _observe_epoch_collision(
    state: Any, task_id: str, agent_id: str, lease_epoch: Optional[int]
) -> bool:
    """
    Record — but do not act on — a superseded-claim completion.

    ADR-0012 D11 specifies fencing such completions. That is deliberately
    NOT implemented yet. D3 (the two liveness dials) made recovery
    trustworthy and deleted the re-grant compensation that used to
    contradict a fence; the fence itself lands in Milestone B of the
    session-model migration, enforced at the resource (the DONE write
    and the merge) and sized to the collision rate this function
    records.

    Meanwhile this gives us the number nobody has: how often a completion
    actually arrives under a superseded epoch. Note the failure mode
    cannot occur at all under spawn-per-task, where recovery kills the
    agent — so a nonzero count here is itself informative.

    Parameters
    ----------
    state : Any
        Server state, providing ``lease_manager``.
    task_id : str
        The task being completed.
    agent_id : str
        The reporting agent.
    lease_epoch : Optional[int]
        The epoch the agent carried, if any.

    Returns
    -------
    bool
        True when a collision was observed. The caller ignores this; it
        exists so tests can assert on the observation without parsing
        logs.

    Notes
    -----
    Each observation is also recorded in the ``epoch_collisions``
    persistence collection, keyed by task and epoch pair so a repeated
    report does not inflate the count, and counted into
    ``state.epoch_collision_count`` for the current process.
    ``get_project_status`` reports the project's count so the figure that
    sizes D11's fence can be read off a run instead of grepped out of a
    log. Recording is best effort: it never breaks the report path.
    """
    lease_manager = getattr(state, "lease_manager", None)
    if lease_manager is None or not lease_epoch:
        return False

    try:
        highest = await lease_manager.get_highest_epoch(task_id)
    except Exception:  # pragma: no cover - never break the report path
        return False

    if not highest or lease_epoch == highest:
        return False

    # Per-process tally beside the durable record. A value that is not an
    # int (an unset attribute on a stand-in state, a stale write) counts as
    # absent: this is a diagnostic and must not raise on a completion path.
    _seen = getattr(state, "epoch_collision_count", 0)
    state.epoch_collision_count = (_seen if isinstance(_seen, int) else 0) + 1
    await _record_epoch_collision(state, task_id, agent_id, lease_epoch, highest)
    logger.warning(
        f"D11 epoch collision: agent {agent_id} completed task {task_id} "
        f"carrying epoch {lease_epoch} while the newest claim is epoch "
        f"{highest}. Two workers held this task at once — under "
        f"spawn-per-task that was impossible, because recovery killed the "
        f"agent. Recorded here; the fence decides what happens to the "
        f"completion (ADR-0012 D11)."
    )
    return True


def _live_lease_held_by_other(state: Any, task_id: str, agent_id: str) -> bool:
    """
    Report whether a live lease on this task belongs to a different agent.

    Used by the ``in_progress`` ownership guard, which is a second
    claim site with no holder check of its own. Applies regardless of
    whether the reporter supplied a D11 epoch, so epoch-less agents get
    the same ownership protection as migrated ones.

    Parameters
    ----------
    state : Any
        Server state, providing ``lease_manager``.
    task_id : str
        The task being reported on.
    agent_id : str
        The reporting agent.

    Returns
    -------
    bool
        True only when a lease exists and is held by someone else.
    """
    lease_manager = getattr(state, "lease_manager", None)
    if lease_manager is None:
        return False
    lease = lease_manager.active_leases.get(task_id)
    if lease is None:
        return False
    return bool(getattr(lease, "agent_id", None) != agent_id)


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
    lease_epoch: Optional[int] = None,
    subject: Optional[str] = None,
    observation: Optional[str] = None,
) -> Dict[str, Any]:
    """Report progress, and leave a counted marker on the board with it.

    Everything about the progress report itself is unchanged; see
    :func:`_record_progress`, which this delegates to.

    The marker is the deliberation layer's write path, moved onto the
    work. In Grasse's stigmergy a termite does not decide to inform
    anyone: the pellet it deposits IS the stimulus the next termite
    responds to, so the signal is a byproduct of the work and costs
    nothing extra. An opinion posted through its own tool is the
    opposite, a civic act with its own cost, and in 30 agent-minutes of
    real Minecraft runs agents posted zero of them while writing the
    same observations into progress messages, where nothing could count
    them (record: ~/marcus-experiments/minecraft-plumbing/RESULTS.md).

    So ``subject`` names what the observation is about and the message
    already written becomes the opinion, unless ``observation`` says the
    crew-relevant part separately. The marker never decides the fate of
    the report: a report the board rejected records nothing, and a
    marker that fails or raises is reported beside a progress report
    that still succeeded.

    Parameters
    ----------
    subject : Optional[str]
        Short slug naming what the observation is about (``wood.scarce``).
        Marcus normalises it, and opinions on the same subject from
        different agents are counted together.
    observation : Optional[str]
        The crew-relevant observation; defaults to ``message``.

    Returns
    -------
    Dict[str, Any]
        The progress report's own result, plus ``marker`` when a subject
        was given.
    """
    result = await _record_progress(
        agent_id=agent_id,
        task_id=task_id,
        status=status,
        progress=progress,
        message=message,
        state=state,
        start_command=start_command,
        readiness_probe=readiness_probe,
        verifications=verifications,
        evidence=evidence,
        lease_epoch=lease_epoch,
    )
    if not subject or not str(subject).strip():
        return result
    if isinstance(result, dict) and result.get("success") is False:
        # Nothing is recorded for work the board did not accept.
        return result
    what = (observation or message or "").strip()
    if not what:
        return result
    try:
        marker = await give_opinion(
            agent_id=agent_id,
            subject=subject,
            what=what,
            state=state,
            why="",
            impact="",
            task_id=task_id,
        )
    except Exception as error:  # noqa: BLE001 - the work is the point
        logger.warning("Marker on %s from %s failed: %s", task_id, agent_id, error)
        marker = {"success": False, "error": str(error)}
    if isinstance(result, dict):
        result["marker"] = marker
    return result


async def _record_progress(
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
    lease_epoch: Optional[int] = None,
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

    # Escalation flag — set when the retry ceiling is hit during
    # validation. The task is routed through the normal completion
    # path (kanban update, lease cleanup, branch merge) but the
    # final response surfaces the escalation so the caller and any
    # downstream observers know the validator's complaints were
    # overridden. See MAX_VALIDATION_RETRIES constant above.
    validation_escalated: bool = False
    escalation_payload: Optional[Dict[str, Any]] = None

    # #795: set when the validation gate itself raised. The policy stays
    # log-and-continue (a gate crash is Marcus's fault, not the agent's,
    # and parking the task on a programmer error has no resolution path
    # the agent can act on), but it is no longer silent: every reply the
    # completion exits through carries the stamp, and an operator_needed
    # event is logged, the way smoke_gate_unavailable is handled.
    unavailable_payload: Optional[Dict[str, Any]] = None

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

    # True once THIS call has registered the task in _completion_in_flight,
    # so the finally releases the marker only for the call that acquired it
    # (a rejected duplicate must not clear the in-progress call's marker).
    _completion_inflight_acquired: bool = False

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
        # The guard fires on ``status == "completed"`` only. An
        # intermediate report with no live lease is refused up front
        # (ADR-0012 D3, see the in_progress branch). Completions
        # write DONE to kanban and trigger branch merges that cannot
        # be undone cleanly, so they get the stricter check here.
        if status == "completed":
            # ADR-0012 D11: the epoch is recorded, and then enforced.
            #
            # Recording stays: it is how the collision rate is measured
            # rather than assumed. The fence then refuses a report whose
            # claim is provably superseded, before either resource this
            # function touches — the board's DONE write and the merge into
            # main. It decides from the durable epoch high-water mark, not
            # from in-memory ownership, because Marcus's in-memory buckets
            # have disagreed with each other before (issue #485).
            #
            # A refused completion is preserved and reconciled on the
            # board, never discarded (D11 part 2).
            await _observe_epoch_collision(state, task_id, agent_id, lease_epoch)
            _fenced = await _fence_stale_epoch(
                state,
                _resolve_completed_task(
                    task_id,
                    await _board_tasks_for_fence(state),
                    getattr(state, "project_tasks", None) or [],
                ),
                task_id,
                agent_id,
                lease_epoch,
            )
            if _fenced is not None:
                return _fenced

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

            # Guard: an already-DONE *parent* task is refused before the
            # validation gate. A re-sent completion -- a duplicate report,
            # or a second agent on a task that already closed -- must not
            # re-run the typed-evidence, approval, LLM or smoke pipeline
            # on work that was already accepted. Mirrors the ``blocked``
            # branch below and report_blocker, which short-circuit a DONE
            # task the same way.
            #
            # Subtasks are excluded: a subtask's status is set to DONE by
            # update_subtask_status() BEFORE the commit-verification gate
            # accepts the completion, so a subtask can be DONE in memory
            # while its completion was rejected (e.g. ``no_commits``).
            # Refusing here would permanently block the legitimate retry
            # after the agent commits (Codex P1 on PR #780). The in-flight
            # guard below still dedupes overlapping subtask retries; making
            # the subtask DONE mutation reflect acceptance is the deeper
            # fix, owned by the subtask-completion lifecycle.
            if (
                task is not None
                and not getattr(task, "is_subtask", False)
                and task.status
                in {
                    TaskStatus.DONE,
                    "done",
                    "completed",
                }
            ):
                logger.warning(
                    f"report_task_progress(completed) rejected: agent "
                    f"{agent_id} reported completion on task {task_id} "
                    f"which is already {task.status!r}. DONE tasks are "
                    "immutable; skipping the validation gate."
                )
                return {
                    "success": False,
                    "status": "task_already_complete",
                    "message": (
                        f"Task {task_id} is already complete -- no need to "
                        "report it again. Request your next task."
                    ),
                }

            # In-flight guard (transport-retry dedupe). The MCP transport
            # can re-send this slow completed call after a reconnect, so a
            # second report for the same task can reach the gate while the
            # first is still validating -- before the first marks the task
            # DONE, so the DONE guard above does not catch it. Reject the
            # overlapping duplicate here. asyncio is single-threaded, so
            # this check-and-add is atomic (no await between them): only
            # one of two overlapping calls can pass. The marker is released
            # in the finally, on every exit path.
            if task_id in _completion_in_flight:
                logger.warning(
                    f"report_task_progress(completed) deferred: a completion "
                    f"for task {task_id} is already inside the validation "
                    f"gate (agent {agent_id}). Duplicate from a transport "
                    "retry; returning a pending, retryable response."
                )
                # Do NOT return task_already_complete here: the in-flight
                # original may still REJECT (no_commits or a validation
                # failure), so telling this caller to move on would make it
                # miss that rejection and its remediation, while
                # request_next_task refuses a new task that is still active
                # (Codex P2 on PR #780). Return an explicitly pending,
                # retryable outcome instead; a re-send once the original
                # settles gets the real result -- DONE (via the DONE guard
                # above) or the rejection with its remediation steps.
                return {
                    "success": False,
                    "status": "completion_in_progress",
                    "retryable": True,
                    "message": (
                        f"A completion for task {task_id} is already being "
                        "validated. Do not request a new task; re-send this "
                        "same completion shortly to receive the final result "
                        "(it may still be accepted, or rejected with "
                        "remediation steps)."
                    ),
                }
            _completion_in_flight.add(task_id)
            _completion_inflight_acquired = True

            # TYPED-EVIDENCE GATE (issue #737, step 4). A find, verify,
            # or synthesize task cannot close without the evidence its
            # output_schema requires; the rejection names the missing
            # fields and the #677 ceiling terminalizes a stuck agent.
            # Runs BEFORE the LLM validation and smoke gates: those are
            # the implement type's machinery and never see typed tasks.
            if task is not None:
                typed_gate_response = await _apply_typed_evidence_gate(
                    task, agent_id, evidence, state
                )
                if typed_gate_response is not None:
                    return typed_gate_response

            # APPROVAL GATE (issue #737, step 5). An approve task
            # closes only for a registered human principal; everyone
            # else is refused here even if eligibility was somehow
            # bypassed at offer time.
            if task is not None:
                approval_response = _apply_approval_gate(task, agent_id, state)
                if approval_response is not None:
                    return approval_response

            if task:
                task_labels = task.labels if hasattr(task, "labels") else None
                # #557: a subtask's validate/skip decision uses its
                # parent's labels — see _should_validate_completion.
                # #737: only the implement type goes through the LLM
                # validator; typed tasks were judged by their schema
                # in the typed-evidence gate above.
                should_validate = task.task_type == "implement" and (
                    _should_validate_completion(task, fresh_tasks)
                )
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
                        # Validation system failed: log, alert an operator,
                        # and allow the completion with the fault stamped
                        # on it (#795). Before this, a crashed gate was
                        # indistinguishable from a passed one.
                        logger.error(f"Validation system error: {e}")
                        logger.exception("Validation exception details:")
                        unavailable_payload = _alert_validation_unavailable(
                            task, agent_id, e, state
                        )
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
            # The repair budget is the loop guard for give-ups, so recovery
            # must not reset it — but a genuine completion ends the cycle:
            # if this task is ever reopened (e.g. request_task_redo), it
            # starts with a fresh budget rather than a stale one.
            clear_repair_attempts(task_id)

            # Issue #737, step 4: the accepted evidence lands on the
            # record (the audit bundle reads it, raw included, from the
            # task's source_context), and every evidence-recorded close
            # hands its evidence minus ``raw`` to its dependents — the
            # only route by which upstream results reach a downstream
            # worker (generalized at the step 7 review, Simon 16c9a4b1).
            if (
                task is not None
                and task.task_type in EVIDENCE_RECORDED_TASK_TYPES
                and evidence
            ):
                update_data["source_context"] = {"evidence": evidence}
                _clear_smoke_attempts(task_id)
                await _handoff_evidence_to_dependents(task, evidence, state)

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

            # Calculate actual hours for the memory record below
            task_assignment = state.agent_tasks.get(agent_id)
            if task_assignment:
                start_time = task_assignment.assigned_at
                # Ensure start_time is timezone-aware
                if start_time.tzinfo is None:
                    start_time = start_time.replace(tzinfo=timezone.utc)
                now_utc = datetime.now(timezone.utc)
                actual_hours = (now_utc - start_time).total_seconds() / 3600
            else:
                actual_hours = 1.0  # Default if no assignment found

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
            #
            # Both the gate and the merge below are scoped to this
            # card's commit range (issue #730), so the baseline is
            # resolved once here. It survives a Marcus restart because
            # the claim persists it with the assignment.
            _card_baseline = await _baseline_commit_for_task(state, agent_id, task_id)
            _ws_state = None
            if hasattr(state, "kanban_client") and state.kanban_client:
                _ws_state = state.kanban_client._load_workspace_state()
            _project_root = _ws_state.get("project_root") if _ws_state else None
            _is_reconciliation = _RECONCILIATION_LABEL in (
                getattr(task, "labels", None) or []
            )
            if _is_reconciliation:
                logger.info(
                    "[commit_gate] Task %s is a reconciliation card; its "
                    "deliverable is a recorded decision, and keeping the "
                    "work already on main is a permitted outcome, so the "
                    "commit check does not apply (ADR-0012 D11).",
                    task_id,
                )
            if _project_root and not _is_reconciliation:
                _commit_ok = _verify_agent_has_commits(
                    agent_id, _project_root, baseline_commit=_card_baseline
                )
                if _commit_ok is False:
                    _scope = (
                        f"since this task was claimed "
                        f"(baseline {_card_baseline[:8]})"
                        if _card_baseline
                        else "ahead of main"
                    )
                    logger.warning(
                        f"[commit_gate] {agent_id} reported task "
                        f"{task_id} complete but branch "
                        f"marcus/{agent_id} has no commits {_scope}. "
                        f"Rejecting false completion."
                    )
                    return {
                        "success": False,
                        "status": "no_commits",
                        "message": (
                            f"Task {task_id} completion rejected: "
                            f"branch marcus/{agent_id} has no commits "
                            f"{_scope}. Commit your implementation "
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
                        await state.lease_manager.release_lease(
                            task_id, reason="completed"
                        )
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
            merge_result = await _merge_agent_branch_to_main(
                agent_id, task_id, state, baseline_commit=_card_baseline
            )
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
            await _record_task_completion(state, agent_id)

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
            # ADR-0012 D3: no live lease means this agent's claim was
            # recovered (or it never held the card). The old branch at
            # the end of this function re-granted a lease here, Marcus's
            # apology for false recovery. D3 makes recovery trustworthy,
            # so the apology is gone: refuse before anything reaches the
            # board, and tell the agent what to do instead.
            if (
                hasattr(state, "lease_manager")
                and state.lease_manager
                and state.lease_manager.active_leases.get(task_id) is None
            ):
                logger.warning(
                    f"report_task_progress(in_progress) refused: agent "
                    f"{agent_id} holds no live lease on {task_id}; the task "
                    f"was recovered or never claimed by this agent."
                )
                return {
                    "success": False,
                    "status": "lease_recovered",
                    "message": (
                        f"Task {task_id} is no longer yours: its lease was "
                        "recovered after the silence timeout or budget "
                        "ceiling passed. Do not continue it. Request your "
                        "next task with request_next_task."
                    ),
                }
            update_data["status"] = TaskStatus.IN_PROGRESS
            # Include assigned_to for Planka provider compatibility.
            #
            # ADR-0012 D11: a displaced agent must not rewrite the board's
            # assignee. The epoch fence sits on the completion path only,
            # so without this a session carrying a superseded token still
            # took the card's name on the board away from the live holder
            # — visually stealing work it is not allowed to finish.
            # Progress itself is still accepted; only the ownership field
            # is withheld.
            if agent_id and not _live_lease_held_by_other(state, task_id, agent_id):
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
        # ``renew_lease`` resets the D3 silence clock (ADR-0012).
        if (
            hasattr(state, "lease_manager")
            and state.lease_manager
            and status != "completed"
        ):
            renewed_lease = await state.lease_manager.renew_lease(
                task_id, progress, message, agent_id=agent_id
            )
            if renewed_lease:
                logger.info(
                    f"Renewed lease for task {task_id} "
                    f"(expires: {renewed_lease.lease_expires.isoformat()})"
                )
            else:
                # ADR-0012 D3: nothing is re-granted. ``renew_lease``
                # returns None when the lease lapsed past grace (the
                # monitor owns recovery) or when the caller is not the
                # holder; a missing lease was already refused above.
                logger.info(
                    f"No lease renewed for task {task_id} on report from "
                    f"{agent_id}: lapsed past grace or not the holder; "
                    f"recovery belongs to the lease monitor."
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

        # ADR-0012 D11: if a displaced session's completion was refused
        # while this one was still working, both implementations exist
        # only now, so this is when the reconciliation card can honestly
        # ask for a comparison. It runs here, after the completion is
        # finalized and before every success return, because validation
        # escalation and stub warnings return early — and the refused
        # session has been told to request its next task, so no later
        # report of its own would open the card for it (Codex P2 on PR
        # #794). A no-op for every ordinary completion.
        if status == "completed" and _deferred_merge_failure is None:
            _task_for_card = next(
                (
                    candidate
                    for candidate in (getattr(state, "project_tasks", None) or [])
                    if getattr(candidate, "id", None) == task_id
                ),
                None,
            )
            await maybe_open_reconciliation_card(state, _task_for_card, task_id)

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
                    return _stamp_validation_unavailable(
                        {
                            "success": True,
                            "message": "Progress updated successfully",
                            "stub_warnings": _stub_findings,
                            "stub_warning_message": (
                                f"Task completed but "
                                f"{len(_stub_findings)} output file(s) still "
                                "contain stub markers (data-stub= or "
                                "// REPLACE this stub). Replace each "
                                "placeholder with a real implementation."
                            ),
                        },
                        unavailable_payload,
                    )

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
        return _stamp_validation_unavailable(
            {"success": True, "message": "Progress updated successfully"},
            unavailable_payload,
        )

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
        return _stamp_validation_unavailable(
            {"success": False, "error": str(e)}, unavailable_payload
        )
    finally:
        # Release the transport-dedupe marker for the completed report
        # this call acquired (see _completion_in_flight). Runs on every
        # exit -- success, early return, or exception -- so a failed
        # completion never leaves the task permanently un-completable.
        if _completion_inflight_acquired:
            _completion_in_flight.discard(task_id)
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


def _alert_validation_unavailable(
    task: Any, agent_id: str, error: BaseException, state: Any
) -> Dict[str, Any]:
    """Record that the validation gate raised instead of judging (#795).

    Logs an ``OPERATOR NEEDED`` line, fires an ``operator_needed`` event
    with ``source="validation_gate"`` (the same channel the smoke gate
    uses when it cannot run), and returns the annotation that
    ``_stamp_validation_unavailable`` merges into the completion reply.
    The completion itself still goes through: the fault is Marcus's,
    the agent cannot fix it, and parking the task on a programmer error
    would only idle the agent until someone fixed the code.

    Parameters
    ----------
    task : Any
        The task whose completion was being validated.
    agent_id : str
        The reporting agent.
    error : BaseException
        The exception the gate raised.
    state : Any
        Marcus server state; ``log_event`` is used when present.

    Returns
    -------
    Dict[str, Any]
        The annotation to merge into the reply: ``validation_unavailable``,
        ``operator_needed``, ``validation_error`` and ``operator_action``.
    """
    project_id = getattr(getattr(state, "kanban_client", None), "project_id", None)
    reason = f"{type(error).__name__}: {error}"
    operator_action = (
        f"Marcus's validation gate raised while judging task {task.id}; the "
        "completion was accepted UNVALIDATED. Look for 'Validation exception "
        "details' in the server log, fix the gate, and review the task's "
        "output by hand if it matters."
    )
    logger.error(
        f"OPERATOR NEEDED: validation gate unavailable for task {task.id} "
        f"({reason}). {operator_action}"
    )
    log_event = getattr(state, "log_event", None)
    if callable(log_event):
        try:
            log_event(
                "operator_needed",
                {
                    "task_id": task.id,
                    "agent_id": agent_id,
                    "project_id": project_id,
                    "reason": reason,
                    "operator_action": operator_action,
                    "source": "validation_gate",
                },
            )
        except Exception:  # noqa: BLE001 - an alert must not break the reply
            pass
    return {
        "validation_unavailable": True,
        "operator_needed": True,
        "validation_error": reason,
        "operator_action": operator_action,
    }


def _stamp_validation_unavailable(
    response: Dict[str, Any], unavailable_payload: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Merge the #795 validation-unavailable annotation into a reply.

    Applied at every exit a completion can take after the gate raised
    (plain success, stub-warning success, pipeline error) so the stamp
    is never dropped. A successful reply also gets
    ``status="validation_unavailable"``; an error reply keeps its error
    as the thing the agent acts on and carries the stamp beside it.

    Parameters
    ----------
    response : Dict[str, Any]
        The reply the completion path built.
    unavailable_payload : Optional[Dict[str, Any]]
        The annotation from ``_alert_validation_unavailable``, or
        ``None`` when the gate ran normally.

    Returns
    -------
    Dict[str, Any]
        ``response`` unchanged when there is nothing to stamp, else a
        copy with the annotation merged in.
    """
    if not unavailable_payload:
        return response
    stamped: Dict[str, Any] = {**response, **unavailable_payload}
    if response.get("success"):
        stamped["status"] = "validation_unavailable"
        stamped["message"] = (
            f"{response.get('message', '')} (validation unavailable: Marcus's "
            "validation gate raised and the completion was accepted "
            "unvalidated; an operator was alerted. Nothing for you to do.)"
        ).strip()
    return stamped


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


async def request_task_redo(
    agent_id: str,
    task_id: str,
    reason: str,
    state: Any,
    defect_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send a completed task back to the board for a fresh agent to redo.

    Issue #627: when integration verification finds that an implementer
    shipped substantively wrong work (wrong response shape, missing
    behavior, logic bug), the integration agent should not rewrite the
    sibling's lane in place (Invariant #2 leak) nor abandon the project.
    This tool resets the DONE task to TODO with a redo diagnostic in
    ``RecoveryInfo``; the next agent claims it through the normal
    ``request_next_task`` cycle (Invariant #1) and decides HOW to fix it
    (Invariant #2) — reuse the previous worktree or start fresh.

    Issue #809: a rejection can be one of three things (arXiv 2609.12039)
    — the implementation was wrong, the acceptance criteria were wrong,
    or the verifier's model of the deployment environment was wrong.
    This tool only ever performs the first repair (try again, same
    criteria), but ``defect_type`` now lets the caller record which kind
    this rejection actually was, so that data exists for a later repair
    path to route on. It does not change what this call does today.

    Parameters
    ----------
    agent_id : str
        The requesting agent's ID (must currently hold a claimed task).
    task_id : str
        ID of the DONE task to send back.
    reason : str
        Non-empty diagnostic for the next agent (what is wrong and how it
        was observed — e.g. the contract clause the output violates).
    state : Any
        Marcus server state instance.
    defect_type : Optional[str]
        Which of the three defect kinds this rejection was
        (``DefectType``: ``"implementation_gap"``, ``"requirement_gap"``,
        ``"model_gap"``). Defaults to ``"implementation_gap"`` when
        omitted, matching this tool's only repair today. An unrecognized
        value is rejected rather than silently stored.

    Returns
    -------
    Dict[str, Any]
        ``{"success": True, "task_id", "redo_count", "recovery_info"}``
        on success; ``{"success": False, "error": ...}`` with error codes
        ``reason_required`` / ``invalid_defect_type`` / ``task_not_found``
        / ``task_not_done`` / ``caller_not_active`` /
        ``max_redo_count_exceeded`` otherwise. After
        ``max_redo_count_exceeded`` the caller's fallback is an in-place
        fix.
    """
    if not reason or not reason.strip():
        return {
            "success": False,
            "error": "reason_required",
            "message": (
                "A non-empty reason is required — the next agent needs a "
                "diagnostic to act on. Describe what is wrong and how you "
                "observed it."
            ),
        }

    if defect_type is None:
        defect_type = DefectType.IMPLEMENTATION_GAP.value
    elif defect_type not in {dt.value for dt in DefectType}:
        return {
            "success": False,
            "error": "invalid_defect_type",
            "message": (
                f"{defect_type!r} is not a recognized defect_type. Use "
                f"one of: {', '.join(sorted(dt.value for dt in DefectType))}."
            ),
        }

    task = next(
        (t for t in (getattr(state, "project_tasks", None) or []) if t.id == task_id),
        None,
    )
    if task is None:
        return {
            "success": False,
            "error": "task_not_found",
            "message": f"Task {task_id} not found in the current project.",
        }

    # #816: the in-memory ``task`` pulled from ``state.project_tasks`` above
    # is only as fresh as the last ``refresh_project_state`` call — nothing
    # in the completion path (``report_task_progress``) sets this object's
    # ``.status`` to DONE; it only writes DONE into the board. Checking
    # ``task.status`` directly can therefore reject a genuinely-completed
    # task as ``task_not_done`` if no refresh landed between the close and
    # this call. Read the board fresh instead, mirroring ``report_blocker``'s
    # Guard 1 a few hundred lines down. A board read failure (or a provider
    # that returns ``None``) degrades to the in-memory status rather than
    # blocking the redo outright — stale-but-functional beats broken.
    board_status: Any = None
    try:
        current_task = await state.kanban_client.get_task_by_id(task_id)
        board_status = (
            getattr(current_task, "status", None) if current_task is not None else None
        )
    except Exception as board_err:  # noqa: BLE001 - degrade to the stale status
        logger.warning(
            "request_task_redo: could not read %s from the board (%s); "
            "falling back to the in-memory status.",
            task_id,
            board_err,
        )
    effective_status = board_status if board_status is not None else task.status
    if effective_status not in {TaskStatus.DONE, "done", "completed"}:
        return {
            "success": False,
            "error": "task_not_done",
            "message": (
                f"Task {task_id} is {effective_status!r}, not 'done' — only "
                "completed work can be sent back for redo. For an in-flight "
                "task, report a blocker instead."
            ),
        }

    if agent_id not in (getattr(state, "agent_tasks", None) or {}):
        return {
            "success": False,
            "error": "caller_not_active",
            "message": (
                f"Agent {agent_id} does not hold a claimed task — only an "
                "active agent (typically integration verification) may "
                "request a redo."
            ),
        }

    previous_redo_count = task.recovery_info.redo_count if task.recovery_info else 0
    if previous_redo_count >= MAX_TASK_REDO_COUNT:
        return {
            "success": False,
            "error": "max_redo_count_exceeded",
            "redo_count": previous_redo_count,
            "message": (
                f"Task {task_id} has already been redone "
                f"{previous_redo_count} times (cap "
                f"{MAX_TASK_REDO_COUNT}). Fix the remaining problem in "
                "place and document the cross-lane edit in your progress "
                "report."
            ),
        }

    previous_agent = task.assigned_to or (
        task.recovery_info.recovered_from_agent if task.recovery_info else None
    )
    previous_branch = f"marcus/{previous_agent}" if previous_agent else None

    # Issue #730: the previous attempt's commits are a range on that
    # branch, not the whole branch. The baseline recorded at claim time
    # is usually gone by now (completion removes the assignment row), in
    # which case the branch alone is the best reference available.
    previous_range: Optional[str] = None
    if previous_agent and previous_branch:
        previous_baseline = await _baseline_commit_for_task(
            state, previous_agent, task_id
        )
        previous_range = (
            f"{previous_baseline[:12]}..{previous_branch}"
            if previous_baseline
            else previous_branch
        )

    # Best-effort worktree resolution via the session launcher's convention
    # ``<project_root>/../worktrees/<agent_id>``. The worktree may already
    # be cleaned up — the branch (merged work) is the durable handle.
    previous_worktree_path: Optional[str] = None
    if previous_agent:
        try:
            ws_state = state.kanban_client._load_workspace_state()
            project_root = (ws_state or {}).get("project_root")
            if project_root:
                candidate = os.path.normpath(
                    os.path.join(project_root, "..", "worktrees", previous_agent)
                )
                previous_worktree_path = candidate
        except Exception as ws_err:
            logger.debug(
                "request_task_redo: could not resolve worktree for %s: %s",
                task_id,
                ws_err,
            )

    now = datetime.now(timezone.utc)
    redo_count = previous_redo_count + 1
    recovery_info = RecoveryInfo(
        recovered_at=now,
        recovered_from_agent=previous_agent or "unknown",
        previous_progress=100,
        time_spent_minutes=0.0,
        recovery_reason="redo_requested",
        previous_agent_branch=previous_branch,
        redo_reason=reason,
        requested_by=agent_id,
        previous_worktree_path=previous_worktree_path,
        redo_count=redo_count,
        defect_type=defect_type,
        recovery_expires_at=now + timedelta(hours=24),
        instructions=(
            f"🔁 **REDO REQUEST** — this task was completed by "
            f"{previous_agent or 'a previous agent'} and sent back by "
            f"{agent_id} (redo {redo_count}/{MAX_TASK_REDO_COUNT}).\n\n"
            f"**What is wrong (from the requesting agent):**\n{reason}\n\n"
            "**Start from the previous attempt, don't restart:**\n"
            # The previous attempt completed, so Marcus already landed
            # its commits on main — the work is in this worktree
            # already. Issue #730: do NOT tell the claimant to merge
            # the previous branch. Under ADR-0012 D1 that branch is
            # scoped to the session and carries its other cards.
            + "- The previous attempt is already on main; Marcus landed "
            "it when the task completed. Read it before changing it.\n"
            + (
                f"- Previous attempt's commits, for reference: "
                f"`git log {previous_range}`\n"
                if previous_range
                else ""
            )
            + (
                f"- Previous worktree (may still exist): "
                f"`{previous_worktree_path}`\n"
                if previous_worktree_path
                else ""
            )
            + "- Fix the reported problem within THIS task's scope, then "
            "complete the original task requirements.\n"
        ),
    )

    # 1. Task model is the source of truth (mirrors lease-expiry recovery).
    task.recovery_info = recovery_info
    task.status = TaskStatus.TODO
    task.assigned_to = None
    # Redone work gates a waiting integration agent — surface it first.
    task.priority = Priority.URGENT
    task.updated_at = now

    # 2. Board dual-write + audit comment, best-effort: the model update
    # above is what request_next_task assigns from; a kanban miss must not
    # fail the redo.
    try:
        await state.kanban_client.update_task(
            task_id,
            {
                "status": TaskStatus.TODO,
                "assigned_to": None,
                # In-memory only would be lost: request_next_task refreshes
                # project state from the board (Tier-1 live finding).
                "priority": Priority.URGENT,
            },
        )
    except Exception as e:
        logger.warning("request_task_redo: kanban update failed for %s: %s", task_id, e)
    try:
        await state.kanban_client.add_comment(
            task_id,
            f"🔁 REDO requested by {agent_id} "
            f"(redo {redo_count}/{MAX_TASK_REDO_COUNT})\n\n"
            f"Reason: {reason}\n\n"
            f"Previous attempt: {previous_agent or 'unknown'}"
            + (f" on branch {previous_branch}" if previous_branch else ""),
        )
    except Exception as e:
        logger.warning(
            "request_task_redo: kanban comment failed for %s: %s", task_id, e
        )

    # 3. Fresh validation/smoke attempt budget for the incoming agent.
    clear_validation_retry(task_id)

    log_agent_event(
        "task_redo_requested",
        {
            "task_id": task_id,
            "requested_by": agent_id,
            "previous_agent": previous_agent,
            "redo_count": redo_count,
            "reason": reason,
            "defect_type": defect_type,
        },
    )

    return {
        "success": True,
        "task_id": task_id,
        "redo_count": redo_count,
        "recovery_info": recovery_info.to_dict(),
        "message": (
            f"Task {task_id} is back on the board (redo "
            f"{redo_count}/{MAX_TASK_REDO_COUNT}). A fresh agent will "
            "claim it via request_next_task and see your diagnostic in "
            "recovery_info."
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

            # Attribute the blocker-analysis LLM cost to the task's
            # project and open run so it reaches Cato instead of
            # 'unassigned' (#768).
            _blocker_cost_ctx = await _planner_cost_context(
                state, task=task, agent_id=agent_id
            )
            with _blocker_cost_ctx:
                suggestions = await state.ai_engine.analyze_blocker(
                    task_id, blocker_description, severity, agent, task
                )

        # Issue #719: decide advisory vs terminal. ``report_blocker`` is a
        # help channel — the worker prompt's ERROR_RECOVERY contract is
        # "Don't panic or abandon the task… Continue working". Marking the
        # task BLOCKED and releasing the agent on every report handed the
        # agent suggestions at the exact moment it lost the task, and (being
        # ephemeral) it then exited: one dead end killed the lane with zero
        # retries, bypassing the lease-recovery → #667 circuit-breaker
        # ladder built for genuine failure. ``severity`` — already in the
        # API, already sent by agents, previously ignored — is the switch:
        # low/medium keep the task, high hands it back. A ceiling stops an
        # agent looping on help while holding the lane.
        # Codex P2: ``severity`` is an unrestricted string on the wire, so
        # "HIGH", " high ", or "critical" all arrive here. Normalise, and
        # fail SAFE — only an explicit low/medium is advisory. Anything
        # unrecognised hands the task back rather than silently holding a
        # genuinely blocked lane until the ceiling or lease expiry. A blank
        # value follows the schema default ("medium").
        severity_normalized = (severity or "medium").strip().lower()
        is_terminal = severity_normalized not in ADVISORY_SEVERITIES
        if not is_terminal:
            attempts = _record_blocker_attempt(task_id)
            if attempts > MAX_ADVISORY_BLOCKER_ATTEMPTS:
                is_terminal = True
                logger.warning(
                    "Advisory blocker ceiling hit for task %s (%d reports); "
                    "escalating to terminal (issue #719).",
                    task_id,
                    attempts,
                )

        requeued_for_repair = False
        if is_terminal:
            # The missing middle rung: a first give-up returns the work to
            # the board with the diagnostic attached so a FRESH agent can
            # attempt it, rather than killing the lane on one agent's word.
            # Only after MAX_BLOCKER_REPAIR_ATTEMPTS independent agents have
            # given up is the lane recorded as genuinely dead.
            # Issue #769, guard 3: a terminal give-up must come from the
            # agent that currently holds the task. After a requeue the lease
            # is gone, so a stale duplicate of the same report (a retry after
            # a lost response) arrives with no live lease; counting it would
            # burn the repair budget on a give-up nobody made. Under ADR-0012
            # D3 "no lease, no report" is the rule everywhere; here it also
            # keeps the budget honest. Deployments without a lease manager
            # skip the check, as they do everywhere else.
            _epoch = 0
            if hasattr(state, "lease_manager") and state.lease_manager:
                _live = state.lease_manager.active_leases.get(task_id)
                if _live is None:
                    logger.warning(
                        f"report_blocker ignored: agent {agent_id} holds no live "
                        f"lease on {task_id}; the task was already requeued or "
                        f"recovered, so this give-up is not counted."
                    )
                    return {
                        "success": False,
                        "status": "lease_recovered",
                        "message": (
                            f"Task {task_id} is no longer yours: it was already "
                            "handed back after a give-up or recovered. Request "
                            "your next task with request_next_task."
                        ),
                    }
                # Accept one terminal give-up per assignment. The mark is
                # taken here, before the first await, so overlapping
                # duplicates on the same lease see it and are refused
                # without touching either bound (Codex P2 on PR #770).
                _epoch = getattr(_live, "lease_epoch", 0) or 0
                if _epoch:
                    if _blocker_repair_consumed_epoch.get(task_id) == _epoch:
                        logger.warning(
                            f"report_blocker ignored: duplicate terminal give-up "
                            f"from {agent_id} on {task_id} (lease epoch {_epoch}) "
                            f"is already being processed."
                        )
                        return {
                            "success": False,
                            "status": "lease_recovered",
                            "duplicate": True,
                            "message": (
                                f"Your give-up on task {task_id} was already "
                                "received and is being handled. Request your "
                                "next task with request_next_task."
                            ),
                        }
                    _blocker_repair_consumed_epoch[task_id] = _epoch

            # Snapshot the counters so a handoff that never reaches the board
            # can be undone. If update_task raises, the outer handler returns
            # an error and the lease stays held; without a rollback the mark
            # above would refuse the agent's retry as a duplicate and the
            # counters would show a requeue that never happened (Codex P2 on
            # PR #770). Only a failed BOARD write rolls back; once the card
            # says TODO or BLOCKED the give-up has happened.
            _prior_agents = set(_blocker_repair_agents.get(task_id, set()))
            _prior_requeues = _blocker_repair_requeues.get(task_id, 0)

            def _rollback_acceptance() -> None:
                if _epoch and _blocker_repair_consumed_epoch.get(task_id) == _epoch:
                    _blocker_repair_consumed_epoch.pop(task_id, None)
                if _prior_agents:
                    _blocker_repair_agents[task_id] = set(_prior_agents)
                else:
                    _blocker_repair_agents.pop(task_id, None)
                if _prior_requeues:
                    _blocker_repair_requeues[task_id] = _prior_requeues
                else:
                    _blocker_repair_requeues.pop(task_id, None)

            repair_attempt = _record_repair_attempt(task_id, agent_id)
            total_requeues = _blocker_repair_requeues.get(task_id, 0)

            # Two bounds, either ends the lane: enough DISTINCT agents gave
            # up (the ceiling), or the task has been re-offered enough times
            # in total, whoever gave up (the budget, issue #769). The budget
            # is what terminates a single persistent agent's loop; the
            # ceiling is what a multi-agent board normally hits first; the
            # spawn-per-task runner, which also looks like one agent at
            # give-up time, keeps its repair rung (#722) because neither
            # bound reads who is registered right now.
            under_ceiling = repair_attempt <= MAX_BLOCKER_REPAIR_ATTEMPTS
            under_budget = total_requeues < MAX_BLOCKER_REPAIR_REQUEUES
            requeued_for_repair = under_ceiling and under_budget

            if requeued_for_repair:
                total_requeues = _record_repair_requeue(task_id)
                now = datetime.now(timezone.utc)
                recovery_info = RecoveryInfo(
                    recovered_at=now,
                    recovered_from_agent=agent_id,
                    previous_progress=0,
                    time_spent_minutes=0.0,
                    recovery_reason="blocker_repair",
                    previous_agent_branch=f"marcus/{agent_id}",
                    recovery_expires_at=now + timedelta(hours=24),
                    # #809: a blocker-repair requeue is always the agent
                    # itself admitting it got stuck on execution, never a
                    # verifier disputing the requirement or the
                    # environment model — so unlike request_task_redo,
                    # there is no caller-supplied classification to
                    # thread through here. Tagged for the same reason
                    # request_task_redo defaults to it: consistency with
                    # the one repair this tool actually performs.
                    defect_type=DefectType.IMPLEMENTATION_GAP.value,
                    instructions=_repair_instructions(
                        blocker_description,
                        suggestions,
                        agent_id,
                        repair_attempt,
                    ),
                )
                task_obj = next(
                    (
                        t
                        for t in (getattr(state, "project_tasks", None) or [])
                        if t.id == task_id
                    ),
                    None,
                )
                _prior_status = task_obj.status if task_obj is not None else None
                _prior_recovery = (
                    task_obj.recovery_info if task_obj is not None else None
                )
                if task_obj is not None:
                    task_obj.recovery_info = recovery_info
                    task_obj.status = TaskStatus.TODO
                    task_obj.assigned_to = None
                try:
                    await state.kanban_client.update_task(
                        task_id,
                        {
                            "status": TaskStatus.TODO,
                            "assigned_to": None,
                            "blocker": blocker_description,
                        },
                    )
                except Exception:
                    _rollback_acceptance()
                    if task_obj is not None:
                        task_obj.status = _prior_status
                        task_obj.assigned_to = agent_id
                        task_obj.recovery_info = _prior_recovery
                    raise
                await _release_task_file_locks(state, task_id, "repair requeue")
                logger.warning(
                    "REPAIR REQUEUE: task %s returned to the board after "
                    "%s gave up (%d/%d distinct agents); next agent gets "
                    "the blocker diagnostic.",
                    task_id,
                    agent_id,
                    repair_attempt,
                    MAX_BLOCKER_REPAIR_ATTEMPTS,
                )
            else:
                # Lane death. Record which bound ended it so the log and the
                # agent reply are accurate for both.
                lane_death_reason = (
                    f"{MAX_BLOCKER_REPAIR_ATTEMPTS} independent agents gave up on it"
                    if not under_ceiling
                    else (
                        f"it has been re-offered {MAX_BLOCKER_REPAIR_REQUEUES} "
                        "times after give-ups (#769 repair budget)"
                    )
                )
                # The terminal write has to reach every place the task
                # lives, or the lane is dead on the board and alive in
                # memory (ultrareview on PR #770): the in-memory task, the
                # subtask store (a subtask has no card of its own, so
                # without this it stays IN_PROGRESS and its dependents
                # starve, #595), the card, and the provider's blocked
                # column (GitHub and Linear do not persist BLOCKED through
                # update_task; the circuit breaker learned this on PR #707).
                _dead_task = next(
                    (
                        t
                        for t in (getattr(state, "project_tasks", None) or [])
                        if t.id == task_id
                    ),
                    None,
                )
                if _dead_task is not None:
                    _dead_task.status = TaskStatus.BLOCKED
                    _dead_task.assigned_to = None
                try:
                    await state.kanban_client.update_task(
                        task_id,
                        {
                            "status": TaskStatus.BLOCKED,
                            "assigned_to": None,
                            "blocker": blocker_description,
                        },
                    )
                except Exception:
                    _rollback_acceptance()
                    if _dead_task is not None:
                        _dead_task.status = TaskStatus.IN_PROGRESS
                        _dead_task.assigned_to = agent_id
                    raise
                # The subtask store persists to disk on write, so it is
                # updated only after the board write succeeded; otherwise a
                # failed handoff would leave BLOCKED on disk for the next
                # reload to resurrect (Codex P2 on PR #770).
                _subtasks = getattr(
                    getattr(state, "subtask_manager", None), "subtasks", None
                )
                if isinstance(_subtasks, dict) and task_id in _subtasks:
                    state.subtask_manager.update_subtask_status(
                        task_id, TaskStatus.BLOCKED, state.project_tasks
                    )
                if hasattr(state.kanban_client, "move_task_to_column"):
                    try:
                        await state.kanban_client.move_task_to_column(
                            task_id, "blocked"
                        )
                    except Exception as _move_err:  # noqa: BLE001 - best effort
                        logger.warning(
                            f"move_task_to_column('blocked') failed for "
                            f"{task_id}: {_move_err}"
                        )
                await _release_task_file_locks(state, task_id, "lane death")
                logger.warning(
                    "LANE DEAD: task %s blocked because %s (distinct give-ups: "
                    "%d, requeues so far: %d).",
                    task_id,
                    lane_death_reason,
                    repair_attempt,
                    total_requeues,
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
                        await state.lease_manager.release_lease(
                            task_id, reason="blocked"
                        )
                        logger.info(
                            f"Released lease for blocked task {task_id} "
                            f"(agent {agent_id})"
                        )
            clear_blocker_attempts(task_id)
        else:
            logger.info(
                "ADVISORY BLOCKER (issue #719): task %s stays with agent %s "
                "(severity=%s); suggestions returned for the agent to apply.",
                task_id,
                agent_id,
                severity,
            )

        # Add detailed comment. Both kinds are recorded — an advisory
        # blocker is real coordination history even though the lane lives on.
        kind = "BLOCKER" if is_terminal else "ADVISORY BLOCKER"
        icon = "🚫" if is_terminal else "💬"
        comment = f"{icon} {kind} ({severity_normalized.upper()})\n"
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

        fire_task_blocked(
            severity=severity_normalized, blocker_description=blocker_description
        )

        if is_terminal:
            if requeued_for_repair:
                return {
                    "success": True,
                    "advisory": False,
                    "requeued_for_repair": True,
                    "suggestions": suggestions,
                    "message": (
                        f"Blocker recorded. Task {task_id} has been returned "
                        "to the board so a fresh agent can attempt it with "
                        "your diagnostic. You no longer hold it — request "
                        "your next task."
                    ),
                }
            lane_death_detail = (
                f"{MAX_BLOCKER_REPAIR_ATTEMPTS + 1} independent agents have now "
                "given up"
                if not under_ceiling
                else (
                    f"it has already been handed back {MAX_BLOCKER_REPAIR_REQUEUES} "
                    "times after give-ups"
                )
            )
            return {
                "success": True,
                "advisory": False,
                "requeued_for_repair": False,
                "suggestions": suggestions,
                "message": (
                    f"Blocker recorded and task {task_id} handed back "
                    f"(status BLOCKED) — {lane_death_detail}, so the lane is "
                    "recorded as dead. You no longer hold it — request your "
                    "next task."
                ),
            }
        return {
            "success": True,
            "advisory": True,
            "suggestions": suggestions,
            "message": (
                f"Suggestions provided — you STILL OWN task {task_id} and it "
                "is still in progress. Apply the suggestions and keep "
                "working; report progress as normal. Only report severity="
                "'high' if you truly cannot proceed and are handing the task "
                "back."
            ),
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


# Helper functions for task assignment


def _tasks_for_dependency_analysis(state: Any, agent_id: str) -> List[Task]:
    """Return the tasks dependency analysis should see for ``agent_id``.

    The agent's registered project plus legacy tasks with no project
    (the same scoping the assignment filter applies). An unregistered
    agent, which has no project to scope by, gets the whole list, as
    before.

    Parameters
    ----------
    state : Any
        Marcus server state (``project_tasks``, ``agent_project_map``).
    agent_id : str
        The requesting agent.

    Returns
    -------
    List[Task]
        The scoped task list.
    """
    tasks: List[Task] = list(getattr(state, "project_tasks", None) or [])
    project_map = getattr(state, "agent_project_map", None) or {}
    project_id = project_map.get(agent_id, "") if isinstance(project_map, dict) else ""
    if not project_id:
        return tasks
    return [t for t in tasks if t.project_id is None or t.project_id == project_id]


async def _record_task_completion(state: Any, agent_id: str) -> None:
    """Count one completed task for ``agent_id`` and persist the counter.

    ``completed_tasks_count`` tracks agent work output, not git outcomes,
    so the caller invokes this after the merge attempt whatever its
    result. The count is part of the worker's registration record
    (ADR-0012 O1), so it is written through to the roster file; a write
    failure is logged and never fails the completion.

    Parameters
    ----------
    state : Any
        Marcus server state.
    agent_id : str
        The agent that completed a task.
    """
    agent = state.agent_status.get(agent_id) if state.agent_status else None
    if agent is None:
        return
    agent.completed_tasks_count += 1
    store = getattr(state, "registration_persistence", None)
    if store is None:
        return
    try:
        pending = store.update_fields(
            agent_id, completed_tasks_count=agent.completed_tasks_count
        )
        if inspect.isawaitable(pending):
            await pending
    except Exception as error:  # noqa: BLE001 - best effort by design
        logger.warning(
            f"Could not persist completed_tasks_count for {agent_id}: {error}"
        )


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


def _classify_task_cached(
    task: Task,
    classifier: Any,
    cache: Dict[str, Tuple[Tuple[str, str, Tuple[str, ...]], Any]],
) -> Any:
    """Return a task's TaskType, memoized by task content (issue #743).

    Classification is a pure function of a task's name, description, and
    labels, none of which change after the task is created. The
    assignment path nonetheless reclassified every available task on
    every ``request_next_task`` call, and classifying a task with long
    text (the Tow experiment embeds excerpts and answers in task text)
    costs on the order of a second, so on a large board this dominated
    request latency and froze the single-threaded server. Memoizing
    classifies each distinct task at most once while still reclassifying
    a task whose text actually changed.

    The cache is keyed by ``task.id`` and stores ``(signature, result)``
    so it holds exactly one entry per task: editing a task replaces its
    entry rather than accumulating a new one, which keeps the cache
    bounded on a long-lived, multi-project server (Codex review on PR
    #744). A stale entry is detected by comparing the current content
    signature and recomputed when it differs.

    Parameters
    ----------
    task : Task
        The task to classify.
    classifier : Any
        An object exposing ``classify(task) -> TaskType``.
    cache : Dict[str, Tuple[Tuple[str, str, Tuple[str, ...]], Any]]
        Mutable cache mapping ``task.id`` to ``(signature, result)``;
        callers persist it (e.g. on server state) across requests.

    Returns
    -------
    Any
        The ``TaskType`` the classifier assigns to the task.
    """
    signature = (task.name, task.description or "", tuple(task.labels or []))
    entry = cache.get(task.id)
    if entry is not None and entry[0] == signature:
        return entry[1]
    result = classifier.classify(task)
    cache[task.id] = (signature, result)
    return result


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


def apply_board_wide_gates(
    available_tasks: List[Task],
    scoped_tasks: List[Task],
    tasks_by_id: Dict[str, Task],
    state: Any,
) -> List[Task]:
    """Apply the gates that depend on the board, not on the asking agent.

    Two gates run after the dependency filter and before any agent-specific
    choice: README documentation waits until the project is 90% complete
    (unless it is the only thing left), and a task may not start while an
    earlier phase of the same feature has no completed task, or while a
    same-feature task of an earlier phase is in progress. Because neither
    gate can be satisfied by choosing a different worker, the drain verdict
    (``src/marcus_mcp/coordinator/drain.py``) applies exactly this function
    too; the two must never disagree (Codex P2 on PR #773).

    Parameters
    ----------
    available_tasks : List[Task]
        Tasks that passed status, ownership and dependency filtering.
    scoped_tasks : List[Task]
        Every task in the project, for completion and phase bookkeeping.
    tasks_by_id : Dict[str, Task]
        ``scoped_tasks`` indexed by id.
    state : Any
        Marcus server state; only its classification cache is touched.

    Returns
    -------
    List[Task]
        The subset of ``available_tasks`` the board allows right now.
    """
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
        completion_percentage = (completed_non_doc_tasks / total_non_doc_tasks) * 100

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

    # Further filter available tasks based on phase constraints.
    # Classification is memoized per task content across requests
    # (issue #743): it is a pure function of task text that never
    # changes after creation, and recomputing it for every task on
    # every request dominated request latency on large boards.
    # isinstance(dict) rather than ``is None`` so a test double whose
    # attribute access auto-creates a Mock still yields a real dict.
    classify_cache = getattr(state, "_task_type_cache", None)
    if not isinstance(classify_cache, dict):
        classify_cache = {}
        try:
            state._task_type_cache = classify_cache
        except Exception:
            pass
    phase_eligible_tasks = []
    for task in available_tasks:
        task_type = _classify_task_cached(task, classifier, classify_cache)
        task_phase = phase_enforcer._get_task_phase(task_type)

        # Check if this phase is allowed given current in-progress tasks
        phase_allowed = True

        # First check against in-progress tasks
        for ip_task_id in in_progress_task_ids:
            ip_task = tasks_by_id.get(ip_task_id)
            if ip_task:
                ip_type = _classify_task_cached(ip_task, classifier, classify_cache)
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
                        if phase_enforcer._should_depend_on_phase(task_phase, ip_phase):
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
                # Get all settled tasks in the same feature. Issue #629
                # / Codex P1 on PR #718: for the terminal integration
                # task a BLOCKED earlier-phase task counts as settled,
                # matching the dependency gate. Counting only DONE here
                # reintroduced the hang through a second door — the
                # integration task stayed phase-ineligible forever while
                # the gridlock detector (sharing deps_allow_claim)
                # reported no gridlock, so the run hung with the alarm
                # switched off. Ordinary tasks keep the strict DONE rule.
                satisfying_statuses = phase_satisfied_by(task)
                feature_completed_tasks = [
                    t
                    for t in scoped_tasks
                    if t.status in satisfying_statuses
                    and t.labels
                    and (set(t.labels) - _SYSTEM_LABELS) & task_feature_labels
                ]

                # Check which phases have been completed
                completed_phases = set()
                for comp_task in feature_completed_tasks:
                    comp_type = _classify_task_cached(
                        comp_task, classifier, classify_cache
                    )
                    comp_phase = phase_enforcer._get_task_phase(comp_type)
                    completed_phases.add(comp_phase)

                # Check if all required earlier phases are complete
                required_phases = [
                    p for p in phase_enforcer.PHASE_ORDER if p.value < task_phase.value
                ]
                for req_phase in required_phases:
                    if req_phase not in completed_phases:
                        # Check if there are any tasks of this phase
                        phase_exists = any(
                            phase_enforcer._get_task_phase(
                                _classify_task_cached(t, classifier, classify_cache)
                            )
                            == req_phase
                            for t in scoped_tasks
                            if t.labels
                            and (set(t.labels) - _SYSTEM_LABELS) & task_feature_labels
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
    return available_tasks


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
        # Issue #629: full task index so the dependency gate can reason about
        # settled-but-not-done upstreams (best-effort integration claim).
        tasks_by_id = {t.id: t for t in scoped_tasks}

        # Design slugs resolve to board ids before the dependency gate; the
        # drain verdict uses the same helper so both see the same graph.
        slug_to_id = build_slug_to_id(scoped_tasks)
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

            # Issue #629: ordinary tasks require all deps DONE; the terminal
            # integration task may claim best-effort over settled-but-BLOCKED
            # upstreams (>=80% done) so one dead lane cannot hang the project.
            claimable, degraded_upstreams = _deps_allow_claim(
                t, resolved_deps, tasks_by_id
            )
            if not claimable:
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
            if degraded_upstreams:
                sc = dict(t.source_context or {})
                sc["degraded_upstreams"] = degraded_upstreams
                t.source_context = sc
                logger.warning(
                    "BEST-EFFORT CLAIM (issue #629): integration task '%s' "
                    "claimable with non-done upstreams: %s",
                    t.name,
                    [d["id"] for d in degraded_upstreams],
                )

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

        available_tasks = apply_board_wide_gates(
            available_tasks, scoped_tasks, tasks_by_id, state
        )

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

        # Issue #737: eligibility is the LAST filter before ranking, so
        # the refusal log records only tasks this worker would actually
        # have been offered. A refused worker gets the ordinary no-task
        # response; the reason stays server-side (never tell an author
        # why it was not offered its own verify task).
        from src.marcus_mcp.coordinator.eligibility import filter_eligible_tasks

        available_tasks = filter_eligible_tasks(agent, available_tasks, state)

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
                await state.lease_manager.release_lease(task_id, reason="unassigned")
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
