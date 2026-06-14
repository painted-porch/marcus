"""Context Tools for Marcus MCP.

This module contains tools for context management:
- log_decision: Log architectural decisions
- get_task_context: Get context for a specific task
"""

import copy
import logging
import time
from typing import Any, Dict, List, Optional, Set

from src.core.project_history import Decision as HistoryDecision
from src.core.project_history import (
    ProjectHistoryPersistence,
)
from src.logging.agent_events import log_agent_event

logger = logging.getLogger(__name__)


async def log_decision(
    agent_id: str, task_id: str, decision: str, state: Any
) -> Dict[str, Any]:
    """
    Log an architectural decision made during task implementation.

    Agents use this to document important technical choices that might
    affect other tasks. Decisions are automatically cross-referenced
    to dependent tasks.

    Parameters
    ----------
    agent_id : str
        The agent making the decision
    task_id : str
        Current task ID
    decision : str
        Natural language description of the decision
    state : Any
        Marcus server state instance

    Returns
    -------
    Dict[str, Any]
        Dict with success status and decision details
    """
    try:
        # Check if Context system is available
        if not hasattr(state, "context") or not state.context:
            return {"success": False, "error": "Context system not enabled"}

        # Parse decision from natural language
        # Expected format: "I chose X because Y. This affects Z."
        parts = decision.split(".", 2)

        what = decision  # Default to full decision
        why = "Not specified"
        impact = "May affect dependent tasks"

        # Try to parse structured format
        if len(parts) >= 1 and "because" in parts[0]:
            what_parts = parts[0].split("because", 1)
            what = what_parts[0].strip()
            if len(what_parts) > 1:
                why = what_parts[1].strip()

        if len(parts) >= 2 and any(
            word in parts[1].lower() for word in ["affect", "impact", "require"]
        ):
            impact = parts[1].strip()

        # Log the decision
        logged_decision = await state.context.log_decision(
            agent_id=agent_id, task_id=task_id, what=what, why=why, impact=impact
        )

        # v0.3.8.post1 (Codex P2 on PR #625): invalidate the
        # foundation-contract cache when a decision is logged against
        # a foundation task. The cached contract includes
        # foundation-task decisions; without this invalidation an
        # agent calling ``get_task_context`` right after a foundation
        # decision lands would see the stale pre-decision contract
        # for up to ``_FOUNDATION_CONTRACT_CACHE_TTL_SECONDS`` and
        # build the wrong architecture guidance into its work.
        # Best-effort: never break log_decision on cache-miss.
        try:
            task = next(
                (t for t in (state.project_tasks or []) if t.id == task_id),
                None,
            )
            if (
                task is not None
                and getattr(task, "source_type", None) == "pre_fork_synthesis"
            ):
                pid = getattr(state, "current_project_id", None)
                invalidate_foundation_contract_cache(str(pid) if pid else None)
        except Exception:  # noqa: BLE001 - never break log_decision on cache miss
            pass

        # Add comment to task if kanban is available
        if state.kanban_client:
            try:
                comment = f"🏗️ ARCHITECTURAL DECISION by {agent_id}\\n"
                comment += f"Decision: {what}\\n"
                comment += f"Reasoning: {why}\\n"
                comment += f"Impact: {impact}"

                await state.kanban_client.add_comment(task_id, comment)
            except Exception as e:
                # Don't fail if kanban comment fails - decision is still logged
                logger.warning(f"Failed to add kanban comment for decision: {e}")

        # Log event (non-blocking)
        if hasattr(state, "events") and state.events:
            await state.events.publish_nowait(
                "decision_logged", agent_id, logged_decision.to_dict()
            )

        # Record in active experiment if one is running
        from src.experiments.live_experiment_monitor import get_active_monitor

        monitor = get_active_monitor()
        if monitor and monitor.is_running:
            monitor.record_decision(
                agent_id=agent_id, task_id=task_id, decision=decision
            )

        # Persist to project history for post-project analysis
        await _persist_decision_to_history(logged_decision, state)

        return {
            "success": True,
            "decision_id": logged_decision.decision_id,
            "message": "Decision logged and cross-referenced to dependent tasks",
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


async def get_task_context(task_id: str, state: Any) -> Dict[str, Any]:
    """
    Get the full context for a specific task.

    This is useful for agents who want to understand the broader
    context of their work or review decisions made on dependencies.

    For subtasks, includes parent task context and shared conventions.

    Parameters
    ----------
    task_id : str
        The task to get context for (can be regular task or subtask ID)
    state : Any
        Marcus server state instance

    Returns
    -------
    Dict[str, Any]
        Dict with task context including implementations, dependencies, and
        decisions. For subtasks, includes parent_task, shared_conventions,
        and dependency_artifacts.
    """
    # Log to agent events — proof the agent called get_task_context
    # Resolve agent_id: try authenticated client first, then task assignment
    agent_id = getattr(state, "_current_client_id", None)
    if not agent_id and hasattr(state, "project_tasks"):
        for t in state.project_tasks:
            if t.id == task_id and t.assigned_to:
                agent_id = t.assigned_to
                break
    agent_id = agent_id or "unknown"
    project_name = getattr(state, "current_project_name", None) or "unknown"
    log_agent_event(
        "context_requested",
        {
            "agent_id": agent_id,
            "task_id": task_id,
            "project": project_name,
        },
    )

    # Record in the active experiment if one is running (mirrors
    # log_decision above). Without this, context_requests stays 0 and the
    # runner's spawn-thrash detector cannot see a claimed agent pulling
    # context as a sign of forward progress.
    from src.experiments.live_experiment_monitor import get_active_monitor

    _monitor = get_active_monitor()
    if _monitor and _monitor.is_running:
        _monitor.record_context_request(agent_id=agent_id, task_id=task_id)

    try:
        # Check if this is a subtask
        if hasattr(state, "subtask_manager") and state.subtask_manager:
            # Check if task_id is a subtask
            if task_id in state.subtask_manager.subtasks:
                # Get subtask context
                subtask_context = state.subtask_manager.get_subtask_context(task_id)

                # Get parent task context
                parent_task_id = subtask_context["parent_task_id"]
                parent_task = None
                for t in state.project_tasks:
                    if t.id == parent_task_id:
                        parent_task = t
                        break

                # Build enriched context
                context_dict = {
                    "is_subtask": True,
                    "subtask_info": subtask_context["subtask"],
                    "parent_task": (
                        {
                            "id": parent_task_id,
                            "name": parent_task.name if parent_task else "Unknown",
                            "description": (
                                parent_task.description if parent_task else ""
                            ),
                            "labels": parent_task.labels if parent_task else [],
                        }
                        if parent_task
                        else {"id": parent_task_id}
                    ),
                    "shared_conventions": subtask_context["shared_conventions"],
                    "dependency_artifacts": subtask_context["dependency_artifacts"],
                    "sibling_subtasks": subtask_context["sibling_subtasks"],
                }

                # CRITICAL: Add artifacts and decisions from sibling subtasks
                # This allows subtasks to see what their siblings have produced
                # Optimized: Single pass through siblings collecting both
                sibling_context = await _collect_sibling_subtask_context(
                    task_id, parent_task_id, state
                )
                context_dict["sibling_artifacts"] = sibling_context["artifacts"]
                context_dict["sibling_decisions"] = sibling_context["decisions"]

                # #595 Fix 2: the foundation contract is project-global —
                # every task, including subtasks, receives it regardless
                # of dependency-graph position.
                context_dict["project_contract"] = await _collect_foundation_contract(
                    state
                )

                # Add parent task's context if Context system is available
                if hasattr(state, "context") and state.context and parent_task:
                    parent_context = await state.context.get_context(
                        parent_task_id, parent_task.dependencies or []
                    )
                    context_dict["parent_context"] = parent_context.to_dict()

                return {"success": True, "context": context_dict}

        # Standard task context (not a subtask)
        # Check if Context system is available
        if not hasattr(state, "context") or not state.context:
            return {"success": False, "error": "Context system not enabled"}

        # Find the task
        task = None
        for t in state.project_tasks:
            if t.id == task_id:
                task = t
                break

        if not task:
            return {"success": False, "error": f"Task {task_id} not found"}

        # Get context
        context = await state.context.get_context(task_id, task.dependencies or [])
        context_dict = context.to_dict()
        context_dict["is_subtask"] = False

        # Add artifact information
        artifacts = await _collect_task_artifacts(task_id, task, state)
        context_dict["artifacts"] = artifacts

        # #595 Fix 2: the foundation contract is project-global. Unlike
        # `artifacts` (scoped to direct dependencies), it is returned to
        # every task regardless of dependency-graph position.
        context_dict["project_contract"] = await _collect_foundation_contract(state)

        # Add recovery information if present
        if task.recovery_info:
            context_dict["recovery_info"] = task.recovery_info.to_dict()

        return {"success": True, "context": context_dict}

    except Exception as e:
        return {"success": False, "error": str(e)}


# Issue #605: context carries architectural artifacts only — never source
# code. These are the artifact types whose canonical home is under ``docs/``
# (see ``ARTIFACT_PATHS`` in ``attachment.py``). Any artifact whose
# ``artifact_type`` is not in this set (e.g. ``temporary`` or a custom type
# pointing at real code) is excluded from delivered context.
ARCHITECTURAL_ARTIFACT_TYPES = frozenset(
    {
        "specification",
        "api",
        "design",
        "architecture",
        "documentation",
        "reference",
    }
)


def _is_architectural_artifact(artifact: Dict[str, Any]) -> bool:
    """
    Return ``True`` when an artifact is architectural (safe to deliver).

    Issue #605: delivered context carries decisions and architectural
    artifacts only, never source code. An artifact qualifies when its
    ``artifact_type`` is one of :data:`ARCHITECTURAL_ARTIFACT_TYPES`.
    Artifacts with no ``artifact_type`` are treated as architectural —
    Kanban attachments, for example, are stamped ``artifact_type:
    "reference"`` already, and missing the field should not silently
    drop a doc.

    Parameters
    ----------
    artifact : Dict[str, Any]
        An artifact dict from ``state.task_artifacts`` or a Kanban
        attachment record.

    Returns
    -------
    bool
        ``True`` when the artifact may be delivered as context.
    """
    artifact_type = artifact.get("artifact_type")
    if artifact_type is None:
        return True
    return artifact_type in ARCHITECTURAL_ARTIFACT_TYPES


_COORDINATION_REFERENCE_GUIDANCE = (
    "COORDINATION REFERENCE: This artifact defines the interface boundary "
    "between domains. Build the complete, working feature implementation. "
    "Treat this as a constraint on what your code must expose at integration "
    "points — not as your implementation spec."
)

_IMPLEMENTATION_GUIDE_GUIDANCE = (
    "IMPLEMENTATION GUIDE: This artifact provides data shapes and design "
    "patterns. Use it to understand the expected structure and patterns — "
    "adapt as needed for your complete feature."
)

_FOUNDATION_USAGE_GUIDANCE = (
    "SHARED FOUNDATION: This artifact was produced by a shared setup task "
    "that completed before parallel work began. Use it directly — import it, "
    "consume its exports, and extend it if needed. Do not recreate or "
    "duplicate what it already provides."
)


#: Per-project cache for ``_collect_foundation_contract`` output.
#:
#: v0.3.8.post1 perf hotfix. Pre-cache the helper was invoked on every
#: ``request_next_task`` call. With 3 foundation tasks per project,
#: each call re-walked ``state.task_artifacts`` AND made 3 fresh
#: kanban ``get_attachments`` queries — context_building dominated by
#: that work at ~12 seconds per call against the snake-completion-1
#: probe board. Multiplied by ephemeral agents each polling, the
#: server appears hung.
#:
#: Cache is keyed by ``state.current_project_id`` and TTL-bound. The
#: foundation contract is intentionally cheap to invalidate: it only
#: changes when (a) the active project changes, (b) a new artifact is
#: logged to a foundation task, or (c) a board attachment is added.
#: We bound staleness with a TTL rather than tracking change events,
#: because the cost of a stale read (one cycle of slightly-old
#: foundation data) is much smaller than the cost of a missed
#: invalidation (agents permanently miss new foundation content).
#:
#: TTL history
#: -----------
#: v0.3.8.post1 (PR #625) set TTL to 60s as a conservative safety
#: net under the assumption that explicit invalidation might miss
#: some mutation case (e.g. ``state.project_tasks`` reassignment in
#: ``server.py`` — the Kaia self-review on PR #625 flagged this).
#: PR #625 then added explicit invalidation at that site
#: (``server.py:1003``).  Together with the existing invalidations
#: on ``log_artifact`` to foundation tasks (``attachment.py:313``)
#: and ``log_decision`` to foundation tasks (``context.py:97``), the
#: explicit invalidation paths now cover every write surface that
#: mutates the foundation contract.
#:
#: 2026-05-25 (verify-snake-5 audit): empirical measurement showed
#: the 60s TTL hits only ~33% of the time because ephemeral agent
#: lifecycle (#600) spaces ``request_next_task`` calls 1-5 minutes
#: apart while agents work in their worktrees.  6 of 9 calls in
#: test67 missed the cache and paid the ~13s context_building
#: penalty.
#:
#: Bumping to 600s (10 minutes) is safe because:
#: 1. All three write surfaces invalidate explicitly, so the cache
#:    only goes stale on bug-cases where invalidation is missed —
#:    and 60s vs 600s is the same correctness exposure (both are
#:    "until something invalidates").
#: 2. Ephemeral agent cadence is bounded by the runner's stall
#:    watchdog (20-min spawn ceiling per ``run_experiment.py``),
#:    so a 10-min TTL fits comfortably inside one run cycle.
#: 3. Per-run wall-clock saving is ~78s (~5%) at trivial-spec
#:    complexity; higher savings expected for richer projects with
#:    more task assignments.
#:
#: Shape: ``{project_id: (computed_at_monotonic, contract_dict)}``.
_FOUNDATION_CONTRACT_CACHE_TTL_SECONDS: float = 600.0
_foundation_contract_cache: Dict[str, tuple[float, Dict[str, List[Dict[str, Any]]]]] = (
    {}
)


def _foundation_contract_cache_key(state: Any) -> Optional[str]:
    """Return the cache key for the current project, or None to skip caching.

    Returns ``None`` when ``state.current_project_id`` is missing —
    the caller will compute fresh every time, never crashing. This is
    the safe degrade path for unit-test mocks or partially-initialized
    state.
    """
    pid = getattr(state, "current_project_id", None)
    return str(pid) if pid else None


def invalidate_foundation_contract_cache(project_id: Optional[str] = None) -> None:
    """Drop the foundation-contract cache entry for one project (or all).

    Call when an artifact is logged to a foundation task or an attachment
    is added — the cache MUST be invalidated so the next
    ``_collect_foundation_contract`` call sees the new content.

    Parameters
    ----------
    project_id : Optional[str]
        Drop only this project's entry. When ``None``, drop everything
        (used during full state resets / project switches).
    """
    if project_id is None:
        _foundation_contract_cache.clear()
    else:
        _foundation_contract_cache.pop(project_id, None)


def _inject_usage_guidance(
    artifact: Dict[str, Any],
    is_design_dep: bool,
    is_contract_first_ghost: bool,
) -> None:
    """
    Inject usage_guidance into a dependency artifact dict in-place.

    Priority (highest to lowest):

    1. ``artifact_role`` field — role-aware guidance (Option C).
    2. ``is_design_dep`` label-based fallback for feature_based design
       artifacts that predate the ``artifact_role`` field (Option B).

    Foundation (pre-fork synthesis) artifacts are no longer handled here.
    They are delivered project-globally via ``project_contract`` and
    carry ``_FOUNDATION_USAGE_GUIDANCE`` from :func:`_collect_foundation_contract`
    (issue #595 Fix 2).

    Parameters
    ----------
    artifact : Dict[str, Any]
        Artifact dict from ``state.task_artifacts``.  Modified in-place.
    is_design_dep : bool
        True when the dependency task has ``"design"`` in its labels.
    is_contract_first_ghost : bool
        True when the dependency task also has ``"auto_completed"`` in its
        labels (contract_first ghost tasks).  These already get framing from
        ``build_tiered_instructions`` and must not be overridden here.
    """
    role = artifact.get("artifact_role")
    if role == "interface_contract":
        artifact.setdefault("usage_guidance", _COORDINATION_REFERENCE_GUIDANCE)
    elif role == "implementation_spec":
        artifact.setdefault("usage_guidance", _IMPLEMENTATION_GUIDE_GUIDANCE)
    elif is_design_dep and not is_contract_first_ghost:
        # Option B label-based fallback: feature_based design artifacts
        artifact.setdefault("usage_guidance", _COORDINATION_REFERENCE_GUIDANCE)


async def _collect_foundation_contract(
    state: Any,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Collect the project-global foundation contract (issue #595 Fix 2).

    The foundation (pre-fork synthesis) tasks establish the shared
    technical contract — language, build config, test harness, public
    API surface — that every task must build against, regardless of its
    position in the dependency graph. Unlike ordinary task artifacts,
    which :func:`_collect_task_artifacts` scopes to a task's *direct*
    dependencies, this contract is project-global and is returned to
    every task by :func:`get_task_context`.

    Foundation tasks are identified by
    ``source_type == "pre_fork_synthesis"`` — the exact, Marcus-set
    marker. This deliberately excludes domain-specific design artifacts,
    whose 1-hop dependency scoping is correct.

    Sources of artifacts (Codex P1 on PR #622):

    1. ``state.task_artifacts[fid]`` — artifacts logged via
       :func:`log_artifact`.
    2. Kanban-board attachments on each foundation task — fetched via
       ``state.kanban_client.get_attachments(card_id=...)``. Without
       this second source, foundation documents attached to the board
       directly (not logged through ``log_artifact``) would be silently
       dropped, because the dependency-traversal path also skips
       ``pre_fork_synthesis`` tasks.

    All artifacts from both sources are filtered through
    :func:`_is_architectural_artifact` (Codex P2 on PR #622). The
    project_contract is broadcast to every task, so non-architectural
    artifacts (temporary or code-like) would be project-wide noise and
    cross the bright line by leaking implementation HOW into a
    coordination channel. Other tiers (dependency / transitive) already
    enforce this filter; foundation now matches.

    Parameters
    ----------
    state : Any
        Marcus server state. ``project_tasks``, ``task_artifacts``,
        ``kanban_client`` and ``context`` are each consulted defensively
        and treated as optional, so the helper degrades to empty results
        rather than raising when a subsystem is unavailable.

    Returns
    -------
    Dict[str, List[Dict[str, Any]]]
        ``{"artifacts": [...], "decisions": [...]}`` — the union of
        architectural artifacts and decisions produced by every
        foundation task in the project. Both lists are empty when there
        is no foundation work or the backing state is unavailable.

    Caching (v0.3.8.post1 perf hotfix). The result is cached per
    ``state.current_project_id`` for
    :data:`_FOUNDATION_CONTRACT_CACHE_TTL_SECONDS`. Cache invalidation
    is TTL-only — the cost of a 60-second-stale read is negligible
    relative to the per-call latency this avoids (12+ seconds on
    multi-foundation projects). Callers that need to invalidate
    explicitly (e.g. when a new artifact is logged to a foundation
    task) should call :func:`invalidate_foundation_contract_cache`.
    """
    # Cache check — return cached entry within TTL.
    #
    # Deep-copy the cached contract on read so callers can mutate
    # their returned dict without corrupting the shared cache entry
    # for subsequent readers. The contract is small (typically <10
    # artifacts), so the copy cost is negligible vs the ~13s of work
    # this saves on a cache hit.
    cache_key = _foundation_contract_cache_key(state)
    if cache_key is not None:
        cached = _foundation_contract_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_contract = cached
            if (time.monotonic() - cached_at) < _FOUNDATION_CONTRACT_CACHE_TTL_SECONDS:
                return copy.deepcopy(cached_contract)

    foundation_tasks = [
        t
        for t in (getattr(state, "project_tasks", None) or [])
        if getattr(t, "source_type", None) == "pre_fork_synthesis"
    ]
    if not foundation_tasks:
        # Cache the empty-foundation result too. Otherwise projects
        # without foundation tasks (prototypes that don't pre-fork,
        # NFR-only projects) re-scan ``state.project_tasks`` on every
        # ``request_next_task`` call.
        empty_contract: Dict[str, List[Dict[str, Any]]] = {
            "artifacts": [],
            "decisions": [],
        }
        if cache_key is not None:
            _foundation_contract_cache[cache_key] = (
                time.monotonic(),
                empty_contract,
            )
        return copy.deepcopy(empty_contract)

    foundation_task_ids = {t.id for t in foundation_tasks}

    artifacts: List[Dict[str, Any]] = []
    task_artifacts = getattr(state, "task_artifacts", None) or {}

    # Source 1: artifacts logged via log_artifact, filtered to
    # architectural types only.
    for fid in foundation_task_ids:
        for raw in task_artifacts.get(fid, []):
            if not _is_architectural_artifact(raw):
                continue
            artifact = dict(raw)
            artifact.setdefault("usage_guidance", _FOUNDATION_USAGE_GUIDANCE)
            artifacts.append(artifact)

    # Source 2: Kanban-board attachments on each foundation task.
    # Without this, foundation documents attached to the board (not
    # logged through log_artifact) are silently dropped (Codex P1 on
    # PR #622). Kanban attachments are stamped
    # ``artifact_type: "reference"`` which is architectural, so they
    # pass the filter — but apply the filter anyway for symmetry with
    # the logged-artifact path and to future-proof against attachments
    # being stamped with a non-architectural type.
    #
    # Interface contract per ``src/integrations/kanban_interface.py``:
    # ``get_attachments(task_id: str)`` returns
    # ``{"success": bool, "data": [{"id", "filename", "url",
    # "content_type", "size", "created_at", "created_by"}, ...]}``.
    # The keys are normalized; do NOT use Planka-style raw keys
    # (``name`` / ``userId`` / ``createdAt``) — they would all read
    # as ``None`` against the canonical provider output (Codex P2
    # follow-up on PR #623). The same bug exists in
    # ``_collect_task_artifacts`` and is tracked separately; this
    # fix is foundation-collector only.
    kanban_client = getattr(state, "kanban_client", None)
    if kanban_client is not None:
        for t in foundation_tasks:
            try:
                result = await kanban_client.get_attachments(task_id=t.id)
            except Exception as exc:  # noqa: BLE001
                # Don't fail the whole context-delivery path if the
                # kanban backend is unavailable or transient-errors.
                logger.warning(
                    "Foundation contract: failed to fetch attachments "
                    "for task %s: %s",
                    t.id,
                    exc,
                )
                continue
            if not result.get("success", False):
                continue
            for attachment in result.get("data") or []:
                filename = attachment.get("filename")
                # Prefer the provider's canonical ``url`` (real
                # filepath / served URL); fall back to a synthetic
                # ``./attachments/<id>/<filename>`` only when the
                # provider didn't populate ``url``.
                location = attachment.get("url") or (
                    f"./attachments/{attachment.get('id')}/{filename}"
                    if filename
                    else None
                )
                attach_artifact: Dict[str, Any] = {
                    "filename": filename,
                    "location": location,
                    "storage_type": "attachment",
                    "artifact_type": "reference",
                    "created_by": attachment.get("created_by"),
                    "created_at": attachment.get("created_at"),
                    "description": (f"Foundation attachment from task {t.id}"),
                    "usage_guidance": _FOUNDATION_USAGE_GUIDANCE,
                }
                if _is_architectural_artifact(attach_artifact):
                    artifacts.append(attach_artifact)

    decisions: List[Dict[str, Any]] = []
    context = getattr(state, "context", None)
    if context is not None:
        for decision in getattr(context, "decisions", None) or []:
            if getattr(decision, "task_id", None) in foundation_task_ids:
                decisions.append(decision.to_dict())

    contract = {"artifacts": artifacts, "decisions": decisions}

    # Write to cache when a key is available. Store a deep copy so
    # caller-side mutations of the returned dict don't corrupt the
    # cache. Skip caching when no current_project_id is set — that
    # keeps the helper degrade-safe for partially-initialized state
    # and unit-test mocks.
    if cache_key is not None:
        _foundation_contract_cache[cache_key] = (
            time.monotonic(),
            copy.deepcopy(contract),
        )

    return contract


async def _collect_task_artifacts(
    task_id: str, task: Any, state: Any
) -> List[Dict[str, Any]]:
    """
    Collect all artifacts available for this task from tracked sources.

    Returns artifacts from:
    1. Artifacts logged via log_artifact for this task (in state.task_artifacts)
    2. Kanban attachments for this task
    3. Artifacts from dependency tasks (both logged and attached)

    Does NOT scan filesystem - only returns explicitly tracked artifacts.
    """
    artifacts = []

    try:
        # 1. Get artifacts logged via log_artifact
        if hasattr(state, "task_artifacts") and task_id in state.task_artifacts:
            artifacts.extend(state.task_artifacts[task_id].copy())

        # 2. Get Kanban attachments for this task
        if state.kanban_client:
            try:
                card_id = getattr(task, "kanban_card_id", None) or task.id
                result = await state.kanban_client.get_attachments(card_id=card_id)
                if result.get("success", False):
                    attachments = result.get("data", [])
                    for attachment in attachments:
                        artifacts.append(
                            {
                                "filename": attachment.get("name"),
                                "location": (
                                    f"./attachments/{attachment.get('id')}/"
                                    f"{attachment.get('name')}"
                                ),
                                "storage_type": "attachment",
                                "artifact_type": "reference",
                                "created_by": attachment.get("userId"),
                                "created_at": attachment.get("createdAt"),
                                "description": f"Attachment from task {task_id}",
                            }
                        )
            except Exception as e:
                # Don't fail the whole operation if kanban is unavailable
                print(
                    f"Warning: Failed to get kanban attachments for task {task_id}: {e}"
                )

        # 3. Get artifacts from dependency tasks
        #
        # GH-356 scope annotation: determine which domain the requesting
        # task owns by extracting its ``domain:X`` labels.  These labels
        # are added by the contract-first path in nlp_tools for both
        # ghost (design) and implementation tasks.  Feature-based tasks
        # carry no ``domain:`` labels, so ``requesting_domain_labels``
        # will be empty for that path.
        requesting_domain_labels = {
            label
            for label in (getattr(task, "labels", []) or [])
            if label.startswith("domain:")
        }
        if task.dependencies:
            for dep_id in task.dependencies:
                dep_task = next(
                    (t for t in state.project_tasks if t.id == dep_id), None
                )
                if dep_task:
                    # #595 Fix 2: foundation (pre-fork synthesis) output is
                    # delivered project-globally via `project_contract`.
                    # Skip foundation deps entirely here — both logged
                    # artifacts and Kanban attachments — so that channel
                    # stays the single source and direct dependents are
                    # not handed foundation data the 1-hop way.
                    if getattr(dep_task, "source_type", None) == "pre_fork_synthesis":
                        continue
                    # Logged artifacts from dependency
                    if (
                        hasattr(state, "task_artifacts")
                        and dep_id in state.task_artifacts
                    ):
                        # P1 fix (GH-356 Codex review): deep-copy each
                        # artifact dict so scope_annotation mutations
                        # don't bleed back into state.task_artifacts.
                        # The list .copy() was shallow — dict objects
                        # were shared, so A's annotation persisted into
                        # B's stored artifact for subsequent callers.
                        dep_artifacts = [dict(a) for a in state.task_artifacts[dep_id]]
                        dep_labels = getattr(dep_task, "labels", []) or []
                        is_design_dep = "design" in dep_labels
                        # contract_first ghost tasks carry both "design" and
                        # "auto_completed" labels.  They already receive framing
                        # from the contract_notice layer in
                        # build_tiered_instructions, so we skip guidance here.
                        is_contract_first_ghost = "auto_completed" in dep_labels
                        # GH-356: ``domain:`` labels on the dep task for
                        # scope_annotation comparison.
                        dep_domain_labels = {
                            label for label in dep_labels if label.startswith("domain:")
                        }
                        for artifact in dep_artifacts:
                            artifact["dependency_task_id"] = dep_id
                            artifact["dependency_task_name"] = dep_task.name
                            artifact["description"] = (
                                f"{artifact.get('description', '')} "
                                f"(from dependency: {dep_task.name})"
                            )
                            # Option C: artifact_role field takes precedence.
                            # Option B: fall back to label-based detection.
                            _inject_usage_guidance(
                                artifact, is_design_dep, is_contract_first_ghost
                            )
                            # GH-356: scope annotation at retrieval time.
                            # The same artifact is in_scope for one agent and
                            # reference_only for another — annotation cannot be
                            # set at generation time.
                            #
                            # contract_first (requesting task has domain: labels):
                            #   same domain as dep → in_scope (agent owns it)
                            #   different domain   → reference_only (coordinate only)
                            #
                            # feature_based (no domain: labels):
                            #   all deps → reference_only (no domain ownership)
                            if requesting_domain_labels:
                                if requesting_domain_labels & dep_domain_labels:
                                    artifact["scope_annotation"] = "in_scope"
                                else:
                                    artifact["scope_annotation"] = "reference_only"
                            else:
                                artifact["scope_annotation"] = "reference_only"
                        artifacts.extend(dep_artifacts)

                    # Kanban attachments from dependency
                    if state.kanban_client:
                        try:
                            dep_card_id = (
                                getattr(dep_task, "kanban_card_id", None) or dep_task.id
                            )
                            result = await state.kanban_client.get_attachments(
                                card_id=dep_card_id
                            )
                            if result.get("success", False):
                                attachments = result.get("data", [])
                                for attachment in attachments:
                                    # P2 fix (GH-356 Codex review):
                                    # Kanban dep attachments also need
                                    # scope_annotation so the data layer
                                    # matches the prompt contract.
                                    dep_attachment_labels = (
                                        getattr(dep_task, "labels", []) or []
                                    )
                                    dep_attachment_domains = {
                                        lbl
                                        for lbl in dep_attachment_labels
                                        if lbl.startswith("domain:")
                                    }
                                    if requesting_domain_labels:
                                        attachment_scope = (
                                            "in_scope"
                                            if requesting_domain_labels
                                            & dep_attachment_domains
                                            else "reference_only"
                                        )
                                    else:
                                        attachment_scope = "reference_only"
                                    artifacts.append(
                                        {
                                            "filename": attachment.get("name"),
                                            "location": (
                                                f"./attachments/{attachment.get('id')}/"
                                                f"{attachment.get('name')}"
                                            ),
                                            "storage_type": "attachment",
                                            "artifact_type": "reference",
                                            "created_by": attachment.get("userId"),
                                            "created_at": attachment.get("createdAt"),
                                            "dependency_task_id": dep_id,
                                            "dependency_task_name": dep_task.name,
                                            "description": (
                                                f"Attachment from dependency: "
                                                f"{dep_task.name}"
                                            ),
                                            "scope_annotation": attachment_scope,
                                        }
                                    )
                        except Exception as e:
                            # Don't fail if kanban is unavailable
                            print(
                                f"Warning: Failed to get kanban attachments "
                                f"for dependency {dep_id}: {e}"
                            )

    except Exception as e:
        # Don't fail the whole context operation if artifact collection fails
        print(f"Warning: Artifact collection encountered an error: {e}")

    return artifacts


async def _collect_transitive_context(
    task_id: str, task: Any, state: Any
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Collect ambient context from a task's *transitive* ancestors.

    Issue #605 tier 3: beyond the foundation contract (project-global)
    and direct-dependency artifacts (one hop, ``in_scope``), an agent
    also benefits from architectural artifacts and decisions produced
    further upstream. This helper walks the dependency graph past the
    first hop and gathers that ambient reference material.

    Everything returned is labelled ``scope_annotation: "reference_only"``
    — it is context to coordinate against, not work the requesting agent
    owns. Direct (one-hop) dependencies are excluded here; those are
    handled by :func:`_collect_task_artifacts` with proper ``in_scope`` /
    ``reference_only`` annotation. Foundation (pre-fork synthesis) tasks
    are also excluded — their output rides the project-global contract.

    Only architectural artifacts are returned (see
    :func:`_is_architectural_artifact`); source-code artifacts are never
    delivered as context.

    Decisions propagate fully: every decision made on any transitive
    ancestor is returned, because architectural decisions are small and
    cross-cutting and every descendant should see them.

    Parameters
    ----------
    task_id : str
        The requesting task's ID.
    task : Any
        The requesting task. Its ``dependencies`` list seeds the walk.
    state : Any
        Marcus server state. ``project_tasks``, ``task_artifacts`` and
        ``context`` are each consulted defensively.

    Returns
    -------
    Dict[str, List[Dict[str, Any]]]
        ``{"artifacts": [...], "decisions": [...]}`` — transitive
        ancestor artifacts and decisions, each ``reference_only``. Both
        lists are empty when there is no transitive ancestry.
    """
    project_tasks = getattr(state, "project_tasks", None) or []
    tasks_by_id = {t.id: t for t in project_tasks}
    task_artifacts = getattr(state, "task_artifacts", None) or {}

    direct_deps = set(getattr(task, "dependencies", None) or [])
    foundation_ids = {
        t.id
        for t in project_tasks
        if getattr(t, "source_type", None) == "pre_fork_synthesis"
    }

    # Breadth-first walk of all ancestors. ``visited`` guards against
    # cycles in a malformed dependency graph.
    ancestors: Set[str] = set()
    visited: Set[str] = {task_id}
    frontier = list(direct_deps)
    while frontier:
        dep_id = frontier.pop()
        if dep_id in visited:
            continue
        visited.add(dep_id)
        ancestors.add(dep_id)
        dep_task = tasks_by_id.get(dep_id)
        if dep_task is not None:
            for upstream in getattr(dep_task, "dependencies", None) or []:
                if upstream not in visited:
                    frontier.append(upstream)

    # Transitive-only = ancestors minus direct deps minus foundation.
    transitive_ids = ancestors - direct_deps - foundation_ids

    artifacts: List[Dict[str, Any]] = []
    for anc_id in transitive_ids:
        anc_task = tasks_by_id.get(anc_id)
        anc_name = getattr(anc_task, "name", anc_id) if anc_task else anc_id
        for raw in task_artifacts.get(anc_id, []):
            if not _is_architectural_artifact(raw):
                continue
            artifact = dict(raw)
            artifact["scope_annotation"] = "reference_only"
            artifact["dependency_task_id"] = anc_id
            artifact["dependency_task_name"] = anc_name
            artifact.setdefault("usage_guidance", _COORDINATION_REFERENCE_GUIDANCE)
            artifacts.append(artifact)

        # PR #606 review P2: _collect_task_artifacts pulls Kanban
        # attachments for direct deps; the transitive walk has to do
        # the same or architectural docs that were attached (but not
        # logged via ``log_artifact``) silently disappear past the
        # first hop.
        kanban = getattr(state, "kanban_client", None)
        if kanban is not None and anc_task is not None:
            try:
                card_id = getattr(anc_task, "kanban_card_id", None) or anc_id
                kanban_result = await kanban.get_attachments(card_id=card_id)
                if kanban_result.get("success", False):
                    for attachment in kanban_result.get("data", []) or []:
                        attach_artifact: Dict[str, Any] = {
                            "filename": attachment.get("name"),
                            "location": (
                                f"./attachments/{attachment.get('id')}/"
                                f"{attachment.get('name')}"
                            ),
                            "storage_type": "attachment",
                            "artifact_type": "reference",
                            "created_by": attachment.get("userId"),
                            "created_at": attachment.get("createdAt"),
                            "dependency_task_id": anc_id,
                            "dependency_task_name": anc_name,
                            "description": (
                                f"Attachment from transitive ancestor: " f"{anc_name}"
                            ),
                            "scope_annotation": "reference_only",
                            "usage_guidance": _COORDINATION_REFERENCE_GUIDANCE,
                        }
                        if _is_architectural_artifact(attach_artifact):
                            artifacts.append(attach_artifact)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to get Kanban attachments for transitive "
                    "ancestor %s: %s",
                    anc_id,
                    exc,
                )

    decisions: List[Dict[str, Any]] = []
    context = getattr(state, "context", None)
    if context is not None:
        for decision in getattr(context, "decisions", None) or []:
            if getattr(decision, "task_id", None) in transitive_ids:
                decision_dict = decision.to_dict()
                decision_dict["scope_annotation"] = "reference_only"
                decisions.append(decision_dict)

    return {"artifacts": artifacts, "decisions": decisions}


async def assemble_task_context(task_id: str, task: Any, state: Any) -> Dict[str, Any]:
    """
    Assemble the full delivered context for a freshly assigned task.

    Issue #605: context must be delivered *with* the task in the
    ``request_next_task`` response, not left to the optional
    ``get_task_context`` call. This helper builds the three-tier context
    bundle so :func:`request_next_task` can attach it directly.

    The three tiers, each scope-labelled:

    1. ``project_contract`` — the project-global foundation contract
       (:func:`_collect_foundation_contract`). Reaches every task.
    2. ``dependency_artifacts`` — direct-dependency artifacts, filtered
       to architectural types, carrying the ``in_scope`` /
       ``reference_only`` annotation set by :func:`_collect_task_artifacts`.
    3. ``transitive_context`` — transitive ancestor artifacts and all
       upstream decisions (:func:`_collect_transitive_context`), every
       entry ``reference_only``.

    Parameters
    ----------
    task_id : str
        The assigned task's ID.
    task : Any
        The assigned task object.
    state : Any
        Marcus server state.

    Returns
    -------
    Dict[str, Any]
        ``{"project_contract": {...}, "dependency_artifacts": [...],
        "transitive_context": {...}}``. Each field degrades to an empty
        value rather than raising when a subsystem is unavailable.
    """
    project_contract = await _collect_foundation_contract(state)

    direct_artifacts = await _collect_task_artifacts(task_id, task, state)
    # PR #606 review P2: _collect_task_artifacts also returns the
    # requesting task's *own* logged artifacts and Kanban attachments,
    # which lack ``dependency_task_id``. Filter to dependency-sourced
    # entries so ``dependency_artifacts`` is what its name claims and
    # downstream consumers that rely on the scope/coordination shape
    # never see the task's own artifacts mis-tagged as deps.
    dependency_artifacts = [
        a
        for a in direct_artifacts
        if a.get("dependency_task_id") and _is_architectural_artifact(a)
    ]

    transitive_context = await _collect_transitive_context(task_id, task, state)

    return {
        "project_contract": project_contract,
        "dependency_artifacts": dependency_artifacts,
        "transitive_context": transitive_context,
    }


async def _collect_sibling_subtask_context(
    current_subtask_id: str, parent_task_id: str, state: Any
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Collect both artifacts AND decisions from sibling subtasks in one pass.

    OPTIMIZED: Single loop through siblings collecting both artifacts and
    decisions, reducing from 2 separate calls to 1.

    When Task B's subtask 1 calls get_task_context, it should see
    artifacts and decisions produced by Task B's subtask 2, 3, etc.

    Parameters
    ----------
    current_subtask_id : str
        The current subtask requesting context
    parent_task_id : str
        The parent task ID
    state : Any
        Marcus server state

    Returns
    -------
    Dict[str, List[Dict[str, Any]]]
        Dict with "artifacts" and "decisions" keys containing sibling context
    """
    sibling_artifacts: List[Dict[str, Any]] = []
    sibling_decisions: List[Dict[str, Any]] = []

    if not hasattr(state, "subtask_manager") or not state.subtask_manager:
        return {"artifacts": sibling_artifacts, "decisions": sibling_decisions}

    # Get all subtasks for this parent from unified storage
    subtasks = state.subtask_manager.get_subtasks(parent_task_id, state.project_tasks)

    # Single pass: collect both artifacts and decisions from siblings
    for subtask in subtasks:
        if subtask.id == current_subtask_id:
            continue  # Skip current subtask

        # Collect artifacts from this sibling
        if hasattr(state, "task_artifacts") and subtask.id in state.task_artifacts:
            for artifact in state.task_artifacts[subtask.id]:
                # Add sibling context to artifact
                sibling_artifact = artifact.copy()
                sibling_artifact["from_sibling_subtask"] = subtask.id
                sibling_artifact["from_sibling_subtask_name"] = subtask.name
                sibling_artifact["description"] = (
                    f"[From sibling subtask: {subtask.name}] "
                    f"{sibling_artifact.get('description', '')}"
                )
                sibling_artifacts.append(sibling_artifact)

        # Collect decisions from this sibling
        if hasattr(state, "context") and state.context:
            subtask_decisions = [
                d.to_dict() for d in state.context.decisions if d.task_id == subtask.id
            ]

            for decision in subtask_decisions:
                # Add sibling context
                decision["from_sibling_subtask"] = subtask.id
                decision["from_sibling_subtask_name"] = subtask.name
                decision["what"] = f"[From sibling: {subtask.name}] {decision['what']}"
                sibling_decisions.append(decision)

    return {"artifacts": sibling_artifacts, "decisions": sibling_decisions}


async def _persist_decision_to_history(logged_decision: Any, state: Any) -> None:
    """
    Persist decision to project history for post-project analysis.

    Converts the Context system's Decision to ProjectHistory's Decision format
    and stores it persistently.

    Parameters
    ----------
    logged_decision : Decision
        The decision from the Context system
    state : Any
        Marcus server state

    Notes
    -----
    Fails gracefully - errors are logged but don't interrupt the main flow.
    """
    try:
        # Get project info from state
        if not hasattr(state, "current_project_id") or not state.current_project_id:
            logger.debug("No active project - skipping project history persistence")
            return

        project_id = state.current_project_id
        project_name = getattr(state, "current_project_name", project_id)

        # Initialize project history persistence if not already done
        if not hasattr(state, "project_history_persistence"):
            state.project_history_persistence = ProjectHistoryPersistence()

        # Get kanban comment URL if decision was posted to kanban
        kanban_comment_url = None
        if hasattr(state, "last_kanban_comment_url"):
            kanban_comment_url = state.last_kanban_comment_url

        # Find affected tasks by checking task dependencies
        affected_tasks: list[str] = []
        if hasattr(state, "project_tasks"):
            for task in state.project_tasks:
                if (
                    hasattr(task, "dependencies")
                    and task.dependencies
                    and logged_decision.task_id in task.dependencies
                ):
                    affected_tasks.append(task.id)

        # Convert Context Decision to ProjectHistory Decision
        history_decision = HistoryDecision(
            decision_id=logged_decision.decision_id,
            task_id=logged_decision.task_id,
            agent_id=logged_decision.agent_id,
            timestamp=logged_decision.timestamp,
            what=logged_decision.what,
            why=logged_decision.why,
            impact=logged_decision.impact,
            affected_tasks=affected_tasks,
            confidence=0.8,  # Default confidence
            kanban_comment_url=kanban_comment_url,
            project_id=project_id,
        )

        # Persist to project history
        await state.project_history_persistence.append_decision(
            project_id, project_name, history_decision
        )

        logger.info(
            f"Persisted decision {logged_decision.decision_id} "
            f"to project history for {project_id}"
        )

    except Exception as e:
        # Graceful degradation - log but don't fail
        logger.warning(f"Failed to persist decision to project history: {e}")
