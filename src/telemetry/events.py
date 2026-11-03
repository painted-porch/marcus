"""Marcus telemetry event helpers (Marcus #416, Stage 2 of #9).

Each public function in this module corresponds to one event in the
schema documented at ``docs/telemetry.md``.  The function gathers
properties (with NO PII / source code / secrets), then forwards to
:class:`src.telemetry.client.TelemetryClient.capture`.

Every helper is wrapped in a top-level ``try/except Exception``
that swallows errors at debug log level — telemetry must never
raise into the MCP tool path.

These helpers live in the telemetry package (not the tool modules)
so the privacy contract is co-located with the disclosure document.
A single grep over ``src/telemetry/events.py`` answers "what do we
actually ship?" — the contract surface is one file.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from src.telemetry import get_telemetry_client

__all__ = [
    "classify_blocker_type",
    "extract_task_phase",
    "fire_agent_registered",
    "fire_epictetus_result",
    "fire_error_occurred",
    "fire_lease_expired",
    "fire_planning_intent_fidelity",
    "fire_project_cost_summary",
    "fire_project_created",
    "fire_structured_llm_retry",
    "fire_task_blocked",
    "fire_task_completed",
    "fire_validator_retry",
    "sanitize_epictetus_recommendations",
]


logger = logging.getLogger(__name__)


#: Runner default used by both ``session_started`` and ``experiment_started``.
#: Kept in sync with the value in ``src/marcus_mcp/server.py``.
_RUNNER_DEFAULT: str = "mcp_direct"


#: Fixed task-phase taxonomy.  Tasks carry labels (free-form strings);
#: we map each label to one of these buckets and ship only the bucket
#: label.  Unknown labels collapse to ``"unknown"``.
_TASK_PHASE_BUCKETS: frozenset[str] = frozenset(
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


#: Fixed blocker-type taxonomy.  See :func:`classify_blocker_type`.
#: The keyword tuples are checked in order — first match wins.  Adjust
#: the order if a tighter prefix should win over a looser keyword (e.g.
#: ``timeout`` is more specific than ``timed out``).
_BLOCKER_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("dependency_not_ready", ("depend", "blocked by", "waiting for")),
    ("timeout", ("timeout", "timed out")),
    ("missing_credential", ("api key", "api_key", "credential", "auth")),
    ("tool_error", ("tool", "command failed", "permission denied", "exit code")),
    ("ambiguous_requirement", ("unclear", "ambiguous", "don't know", "do not know")),
    ("async_failure", ("async", "race condition")),
)


# -- project_created ----------------------------------------------------------


def fire_project_created(
    result: Dict[str, Any],
    options: Optional[Dict[str, Any]],
    actual_decomposer: Optional[str],
    requested_decomposer: Optional[str] = None,
) -> None:
    """Emit the ``project_created`` event after a successful ``create_project``.

    Best-effort: all errors are swallowed at debug-log level.  Does
    NOT fire when ``result["success"]`` is falsy — failed creates
    are not creates.

    Parameters
    ----------
    result : dict
        The ``create_project`` return value.  Reads ``success``,
        ``tasks_created``, ``domain`` and ``structural_category``.
        The last two are taxonomy-bucketed by the PRD planner (see
        ``src/ai/advanced/prd/advanced_parser.py``) — they are never
        free text, so ``project_created`` cannot leak project detail.
    options : dict, optional
        The ``options`` arg passed to ``create_project``.  Reads
        ``complexity`` only; never reads keys that could carry
        secrets (``anthropic_api_key``, etc.).
    actual_decomposer : str, optional
        The decomposer strategy that actually ran.
    requested_decomposer : str, optional
        The decomposer strategy the caller asked for.  Used only to
        derive ``was_fallback`` — see below.  Not shipped directly.

    Notes
    -----
    ``was_fallback`` is ``True`` when the caller requested
    ``contract_first`` but the run actually used ``feature_based`` —
    i.e. contract-first decomposition failed (no project root, weak
    contracts, empty domains) and silently fell back.  Without this
    flag a fallback is indistinguishable from a genuine feature_based
    request in telemetry, hiding how often contract_first is failing.
    """
    try:
        if not result or not result.get("success"):
            return

        options = options or {}
        was_fallback = (
            requested_decomposer == "contract_first"
            and actual_decomposer == "feature_based"
        )
        properties = {
            "task_count": result.get("tasks_created"),
            "complexity_mode": options.get("complexity", "standard"),
            "decomposer_strategy": actual_decomposer or "unknown",
            # True when contract_first was requested but fell back to
            # feature_based — surfaces contract_first failure rate.
            "was_fallback": was_fallback,
            # Taxonomy-bucketed by the PRD planner (#546 Phase 0).
            # "unknown" when the planner omitted the label or an early
            # return skipped decomposition.
            "structural_category": result.get("structural_category", "unknown"),
            "domain": result.get("domain", "unknown"),
        }
        get_telemetry_client().capture("project_created", properties)
    except Exception as exc:  # noqa: BLE001 - never crash the tool path
        logger.debug("fire_project_created failed: %s", exc)


# -- task_completed -----------------------------------------------------------


def extract_task_phase(labels: Optional[List[Any]]) -> str:
    """Map a task's labels to one of the fixed :data:`_TASK_PHASE_BUCKETS`.

    First matching label wins.  Comparison is case-insensitive.
    Returns ``"unknown"`` if no label is in the taxonomy.

    Parameters
    ----------
    labels : list of str, optional
        The ``task.labels`` list.  May be ``None`` or empty.

    Returns
    -------
    str
        One of the bucket names, or ``"unknown"``.
    """
    if not labels:
        return "unknown"
    for label in labels:
        if not isinstance(label, str):
            continue
        normalized = label.lower()
        if normalized in _TASK_PHASE_BUCKETS:
            return normalized
    return "unknown"


def fire_task_completed(task: Any) -> None:
    """Emit the ``task_completed`` event when a task moves to DONE.

    Reads only the task's ``labels`` field for the phase bucket.
    Task ``name``, ``description``, and any other free-text fields
    are NOT touched — they never leave the machine.

    Best-effort.  Errors swallowed at debug-log level.

    Parameters
    ----------
    task : Task-like
        Marcus Task object.  Must expose ``labels``.  Other
        attributes (``id``, ``name``) are intentionally ignored.

    Notes
    -----
    ``had_blocker`` and ``duration_minutes`` are part of the
    ``docs/telemetry.md`` § task_completed schema but their
    capture sites do not exist yet (they need the
    ``task_lifecycle`` table from Sub-issue A #547 and the
    ``blockers`` table from Sub-issue E #551).  We ship ``None``
    for them today — honest placeholder; populated in v0.3.8+.
    """
    try:
        labels = getattr(task, "labels", None) if task is not None else None
        properties = {
            "task_phase": extract_task_phase(labels),
            # Populated in v0.3.8 once Sub-issues A (#547) and
            # E (#551) land — task_lifecycle table + blockers table.
            "had_blocker": None,
            "duration_minutes": None,
        }
        get_telemetry_client().capture("task_completed", properties)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fire_task_completed failed: %s", exc)


# -- task_blocked -------------------------------------------------------------


def classify_blocker_type(description: str) -> str:
    """Bucket a free-text blocker description into a fixed type.

    Local keyword bucketing — the description text itself never
    leaves the machine.  Only the returned bucket label ships in
    the ``task_blocked`` event.  See ``docs/telemetry.md`` §
    task_blocked: "The blocker type is shipped. The blocker
    message is never shipped."

    Parameters
    ----------
    description : str
        Free-text blocker description from ``report_blocker``.
        May be empty or ``None``.

    Returns
    -------
    str
        One of: ``dependency_not_ready``, ``timeout``,
        ``missing_credential``, ``tool_error``,
        ``ambiguous_requirement``, ``async_failure``, ``unknown``.

    Notes
    -----
    Keyword order in :data:`_BLOCKER_TYPE_KEYWORDS` is load-bearing.
    More specific buckets should appear before more general ones
    so the first match is the most-precise classification.
    """
    text = (description or "").lower()
    if not text:
        return "unknown"
    for bucket, keywords in _BLOCKER_TYPE_KEYWORDS:
        if any(k in text for k in keywords):
            return bucket
    return "unknown"


def fire_task_blocked(severity: str, blocker_description: str) -> None:
    """Emit the ``task_blocked`` event.

    Ships ``blocker_type`` (keyword-classified from the description)
    and ``severity`` (passthrough).  Does NOT ship the description
    itself.

    Best-effort.  Errors swallowed at debug-log level.

    Parameters
    ----------
    severity : str
        ``"low"``, ``"medium"``, or ``"high"`` from ``report_blocker``.
    blocker_description : str
        The free-text description.  Used only by
        :func:`classify_blocker_type` to derive a bucket label; the
        text itself is never put on the wire.
    """
    try:
        properties = {
            "blocker_type": classify_blocker_type(blocker_description),
            "severity": severity,
        }
        get_telemetry_client().capture("task_blocked", properties)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fire_task_blocked failed: %s", exc)


# -- lease_expired ------------------------------------------------------------


def fire_lease_expired(
    task_held_minutes: int,
    progress_pct_at_expiry: int,
    recovery_attempted: bool,
) -> None:
    """Emit ``lease_expired`` when an agent's task lease expires.

    Best-effort.  Errors swallowed.

    Parameters
    ----------
    task_held_minutes : int
        How long the lease was held before expiring.
    progress_pct_at_expiry : int
        Last reported progress percentage on the task at the moment
        of expiry (0-100).
    recovery_attempted : bool
        True if Marcus put the task back on the board for another
        agent to claim; False if it was abandoned outright.  Named
        ``recovery_attempted`` (not ``recovered``) because the lease-
        expiry code path cannot observe downstream completion — only
        whether recovery was initiated.  Whether the next agent
        succeeded is a separate signal carried by ``task_completed``.
    """
    try:
        properties = {
            "task_held_minutes": task_held_minutes,
            "progress_pct_at_expiry": progress_pct_at_expiry,
            "recovery_attempted": recovery_attempted,
        }
        get_telemetry_client().capture("lease_expired", properties)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fire_lease_expired failed: %s", exc)


# -- validator_retry ----------------------------------------------------------


def fire_validator_retry(
    retry_count: int,
    final_result: str,
    validation_type: str,
) -> None:
    """Emit ``validator_retry`` when a planner validator re-runs a check.

    Best-effort.  Errors swallowed.

    Parameters
    ----------
    retry_count : int
        How many retries the validator attempted before reaching the
        final result.
    final_result : str
        ``"pass"`` or ``"fail"`` — what the validator concluded after
        all retries.
    validation_type : str
        Which validator ran (e.g. ``"task_completeness"``).  Free-text
        label, but callers should stick to a small enum so PostHog
        cardinality stays manageable.
    """
    try:
        properties = {
            "retry_count": retry_count,
            "final_result": final_result,
            "validation_type": validation_type,
        }
        get_telemetry_client().capture("validator_retry", properties)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fire_validator_retry failed: %s", exc)


# -- structured_llm_retry -----------------------------------------------------


def fire_structured_llm_retry(
    operation: str,
    retry_count: int,
    reason: str,
    final: str,
) -> None:
    """Emit ``structured_llm_retry`` for the ``safe_structured_call`` helper.

    Per PR #542 — the truncation-retry helper centralizes structured
    LLM calls.  This event tags each retry so the dashboard can
    answer "is the retry helper firing in the wild and is it
    succeeding?".

    Best-effort.  Errors swallowed.

    Parameters
    ----------
    operation : str
        Cost-event operation key (e.g. ``"parse_prd"``).
    retry_count : int
        Which attempt this was (1 = first retry, 2 = second, ...).
    reason : str
        Why the retry fired.  Conventional values: ``"truncation"``,
        ``"rate_limit"``, ``"timeout"``, ``"validation_fail"``.
    final : str
        Outcome of the retry.  ``"ok"`` if the retry parsed, ``"fail"``
        if the retry also failed.
    """
    try:
        properties = {
            "operation": operation,
            "retry_count": retry_count,
            "reason": reason,
            "final": final,
        }
        get_telemetry_client().capture("structured_llm_retry", properties)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fire_structured_llm_retry failed: %s", exc)


# -- error_occurred -----------------------------------------------------------


def fire_error_occurred(error_type: str) -> None:
    """Emit ``error_occurred`` when an error reaches Marcus error monitoring.

    **Privacy contract**: only the error *type* (class name) is shipped.
    The error *message* and stack trace are never shipped.  The
    function signature accepts only ``error_type`` — there is no
    way for a caller to pass a message even by accident.

    Best-effort.  Errors swallowed (an error in the error event
    handler would be... unfortunate).

    Parameters
    ----------
    error_type : str
        The class name of the error (e.g. ``"KanbanIntegrationError"``).
        Callers extract via ``type(err).__name__``; the bare class name
        is the bucket.
    """
    try:
        get_telemetry_client().capture("error_occurred", {"error_type": error_type})
    except Exception as exc:  # noqa: BLE001
        logger.debug("fire_error_occurred failed: %s", exc)


# -- agent_registered ---------------------------------------------------------


def fire_agent_registered(
    role: str, skills: List[Any], agent_model: str = "unknown"
) -> None:
    """Emit ``agent_registered`` when ``register_agent`` MCP tool succeeds.

    Ships ``role``, ``skills``, ``agent_model`` — the user-controlled
    labels documented in ``docs/telemetry.md`` § agent_registered.
    Notably does NOT accept agent display name or agent_id (those
    can identify a human).

    Best-effort.  Errors swallowed.

    Parameters
    ----------
    role : str
        Agent's role string as passed to ``register_agent``.
    skills : list of str
        Agent's skills list as passed to ``register_agent``.
    agent_model : str, default ``"unknown"``
        Model identifier (e.g. ``"claude-sonnet-4-6"``).  Optional
        per #416 plan; defaults to ``"unknown"`` until runners pass
        it through register_agent.
    """
    try:
        properties = {
            "role": role,
            "skills": list(skills or []),
            "agent_model": agent_model,
        }
        get_telemetry_client().capture("agent_registered", properties)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fire_agent_registered failed: %s", exc)


# -- planning_intent_fidelity -------------------------------------------------


def fire_planning_intent_fidelity(
    decomposer: str,
    intent_fidelity_score: float,
    coverage_before_fill: float,
    coverage_after_fill: float,
    gap_filled_outcomes: int,
) -> None:
    """Forward the internal ``PLANNING_INTENT_FIDELITY`` event to telemetry.

    The internal event (emitted by
    ``src.integrations.nlp_tools._emit_planning_intent_fidelity_event``)
    carries ``project_name`` for Cato.  This forwarder accepts
    everything BUT ``project_name`` — the function signature is the
    regression net that prevents accidentally shipping the name.

    Best-effort.  Errors swallowed.
    """
    try:
        properties = {
            "decomposer": decomposer,
            "intent_fidelity_score": intent_fidelity_score,
            "coverage_before_fill": coverage_before_fill,
            "coverage_after_fill": coverage_after_fill,
            "gap_filled_outcomes": gap_filled_outcomes,
        }
        get_telemetry_client().capture("planning_intent_fidelity", properties)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fire_planning_intent_fidelity failed: %s", exc)


# -- project_cost_summary -----------------------------------------------------


#: Keys from ``cost_aggregator.project_summary`` that are safe to ship.
#: Anything not on this list is dropped at the forwarder — explicit
#: allowlist is the privacy regression net.  See docs/telemetry.md
#: § project_cost_summary for the contract.
_COST_SUMMARY_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cost_usd_cents",
    }
)


def fire_project_cost_summary(summary: Dict[str, Any]) -> None:
    """Forward the cost-aggregator's project summary to telemetry.

    Allowlist filter: only the keys in
    :data:`_COST_SUMMARY_ALLOWED_KEYS` make it onto the wire.  The
    aggregator's internal fields (``project_id``, ``project_name``,
    breakdowns by role/operation) stay local.

    Computes ``cost_per_task_cents`` from ``cost_usd_cents`` and
    ``task_count`` (if both present and task_count > 0).

    Best-effort.  Errors swallowed.

    Parameters
    ----------
    summary : dict
        A **flat** cost summary with the numeric keys in
        :data:`_COST_SUMMARY_ALLOWED_KEYS` plus optional
        ``cost_usd_cents`` / ``task_count``.  Note this is NOT the
        raw ``cost_aggregator.project_summary()`` return value — that
        is a nested object whose totals live under a ``summary`` key.
        Callers must flatten it first.  May contain extra identifying
        fields; the allowlist filter drops them.
    """
    try:
        filtered: Dict[str, Any] = {
            k: summary[k] for k in _COST_SUMMARY_ALLOWED_KEYS if k in summary
        }

        # Derived metric — cost per task.  Avoid divide-by-zero.
        task_count = summary.get("task_count") or 0
        cost_cents = summary.get("cost_usd_cents") or 0
        filtered["cost_per_task_cents"] = cost_cents / task_count if task_count else 0

        get_telemetry_client().capture("project_cost_summary", filtered)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fire_project_cost_summary failed: %s", exc)


# -- epictetus_result + sanitizer ---------------------------------------------


#: Max characters per recommendation after truncation.  Per Kaia
#: review 2026-05-13: bound the text size so a runaway auditor
#: cannot blow PostHog's payload limits + a hard upper limit on
#: how much identifying text can leak per rec.
_EPICTETUS_REC_MAX_CHARS: int = 200

#: Max number of recommendations shipped per event.  Same Kaia
#: review.  Auditors that produce 30+ recs ship only the first 5.
_EPICTETUS_REC_MAX_COUNT: int = 5

#: Regex patterns scrubbed from each recommendation BEFORE truncation.
#: Order matters — code blocks (which may contain paths) are stripped
#: before path matching so we don't end up with ``<path><code>`` salad.
#: Each tuple is ``(pattern, replacement)``.
_EPICTETUS_SCRUB_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Triple-backtick fenced code blocks (DOTALL — spans newlines).
    (re.compile(r"```[\s\S]*?```", re.MULTILINE), "<code>"),
    # Single-backtick inline code.
    (re.compile(r"`[^`\n]+`"), "<code>"),
    # Absolute Unix paths starting with `/`.  Must run before the
    # home-relative pattern because `/Users/...` shouldn't collapse
    # to `~/...`.
    (re.compile(r"(?:/[A-Za-z0-9._-]+){2,}\.[A-Za-z0-9]+"), "<path>"),
    (re.compile(r"(?:/[A-Za-z0-9._-]+){2,}"), "<path>"),
    # Home-relative paths.
    (re.compile(r"~[/\\][A-Za-z0-9._\-/\\]+"), "<path>"),
    # Windows paths (drive letter + backslash chains).
    (re.compile(r"[A-Z]:[\\/][A-Za-z0-9._\\\-/]+"), "<path>"),
)


def sanitize_epictetus_recommendations(
    recommendations: Optional[List[Any]],
) -> List[str]:
    """Scrub identifying content from auditor recommendations.

    Pipeline per item:

    1. Coerce to string (drop ``None`` entries).
    2. Apply :data:`_EPICTETUS_SCRUB_PATTERNS` — fenced code blocks
       → ``<code>``, inline code → ``<code>``, paths → ``<path>``.
    3. Truncate to :data:`_EPICTETUS_REC_MAX_CHARS`.
    4. Drop empty results.

    Whole list is clipped to :data:`_EPICTETUS_REC_MAX_COUNT`.

    Parameters
    ----------
    recommendations : list, optional
        Free-text auditor recommendations.  ``None`` is treated as
        an empty list.  Non-string items are coerced to ``str()``
        before scrubbing; items that coerce to empty after scrubbing
        are dropped.

    Returns
    -------
    list of str
        Sanitized recommendations, at most
        :data:`_EPICTETUS_REC_MAX_COUNT` items.

    Notes
    -----
    Known limitations of the regex sweep:

    * **URL false positives.**  The absolute-Unix-path patterns will
      match the path portion of a URL — e.g. ``https://example.com/
      blog/post.html`` becomes ``https:<path>``.  We accept this
      because (a) URLs in auditor recommendations are rare, (b)
      over-scrubbing is the privacy-safe failure mode, and (c) the
      receiver (PostHog dashboard) cares about the *guidance* in the
      rec, not the source URL.  If a URL must survive, the auditor
      can omit the path or use a shortlink.

    * **Pattern ordering matters.**  Code blocks are stripped before
      paths so that a path embedded inside a code block becomes
      ``<code>`` (which is correct — the whole block was identifying
      content), not ``<path>`` inside otherwise-leaked code.  The
      paired test ``test_path_inside_code_block`` pins this ordering.
    """
    if not recommendations:
        return []

    sanitized: List[str] = []
    for item in recommendations[:_EPICTETUS_REC_MAX_COUNT]:
        if item is None:
            continue
        text = str(item)
        for pattern, replacement in _EPICTETUS_SCRUB_PATTERNS:
            text = pattern.sub(replacement, text)
        if len(text) > _EPICTETUS_REC_MAX_CHARS:
            text = text[: _EPICTETUS_REC_MAX_CHARS - 1] + "…"
        text = text.strip()
        if text:
            sanitized.append(text)
    return sanitized


def fire_epictetus_result(grade: str, recommendations: List[Any]) -> None:
    """Emit ``epictetus_result`` after a post-run code grade.

    Submitted by runners that explicitly invoke the Epictetus
    auditor — typically ``/marcus`` skill and Posidonius.  Marcus
    itself does not automatically run Epictetus.

    Applies :func:`sanitize_epictetus_recommendations` before the
    payload reaches the client.  The sanitized list ships to
    PostHog.

    Best-effort.  Errors swallowed.

    Parameters
    ----------
    grade : str
        Letter grade (e.g. ``"B+"``).
    recommendations : list
        Free-text recommendations from the auditor.  Will be
        sanitized before shipping.
    """
    try:
        properties = {
            "grade": grade,
            "recommendations": sanitize_epictetus_recommendations(recommendations),
        }
        get_telemetry_client().capture("epictetus_result", properties)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fire_epictetus_result failed: %s", exc)
