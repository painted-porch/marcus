"""User-outcome coverage check for intent fidelity (issue #449).

Given a list of :class:`UserOutcome` records (from the extractor) and a
list of :class:`Task` records (from the decomposer), this module answers:

* Which tasks address each outcome? (:func:`compute_coverage`)
* Which in-scope outcomes have no covering task? (:func:`find_gaps`)
* What fraction of in-scope outcomes are covered?
  (:func:`compute_intent_fidelity_score`)
* When gaps exist, what tasks should be added? (:func:`fill_gaps`)

Two coverage strategies are available:

* :func:`compute_coverage` — sync, requires an explicit mapper.  Use
  :func:`keyword_overlap_mapper` for offline / test scenarios.  No
  default mapper is provided because the only sync option produces
  false positives on the snake_game-v31 case (#449's motivation).
* :func:`compute_coverage_with_llm` — async, single LLM call maps
  all outcomes to all tasks.  This is the production path; the LLM
  has the global context to distinguish "supports" from "addresses".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
)

from src.ai.advanced.prd.outcome_extractor import UserOutcome
from src.config.outcome_coverage_config import is_outcome_coverage_enabled
from src.core.models import Priority, Task, TaskStatus
from src.core.task_classification import TASK_TYPE_IMPLEMENTATION, get_task_type
from src.marcus_mcp.coordinator.graph_augmentation import AugmentationResult

if TYPE_CHECKING:
    # TYPE_CHECKING-only to avoid the runtime cycle:
    # advanced_parser imports from this module.
    from src.ai.advanced.prd.advanced_parser import PRDAnalysis

logger = logging.getLogger(__name__)

# Words too common to discriminate between outcome and task.  Stripped
# from the keyword sets before overlap is computed.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "by",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "user",
        "users",
        "can",
        "able",
        "must",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "their",
        "there",
        "when",
        "where",
        "what",
        "who",
        "how",
        "all",
        "any",
        "some",
        "no",
        "not",
        "so",
        "than",
        "then",
        "into",
        "out",
        "up",
        "down",
        "over",
        "under",
        "again",
        "more",
        "most",
        "other",
        "such",
        "own",
        "same",
        "very",
        "will",
        "just",
        "also",
        # signal-domain noise
        "observable",
        "appears",
        "shows",
        "displays",
        "set",
        "works",
        # action verbs that almost always appear in outcomes (don't help
        # discriminate)
        "see",
        "view",
        "use",
        "make",
        "get",
        "go",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")


def _tokens(text: str) -> Set[str]:
    """Lower-cased, stopword-filtered token set for keyword overlap."""
    return {
        match.group().lower()
        for match in _TOKEN_RE.finditer(text)
        if match.group().lower() not in _STOPWORDS and len(match.group()) >= 3
    }


def keyword_overlap_mapper(outcome: UserOutcome, task: Task) -> bool:
    """Map outcome to task via keyword overlap (FALLBACK heuristic only).

    .. warning::

        **Do not use this as the production coverage strategy.**  It is
        provably inadequate on the snake_game-v31 case (the failure
        mode that motivated #449): outcomes and internal logic tasks
        share heavy domain-noun overlap (``snake``, ``food``, ``score``)
        which produces false positives — every logic task is wrongly
        marked as "covering" the play-the-game outcome.

        Use this mapper only when:

        * No LLM client is available (smoke tests, offline tooling)
        * Writing unit tests that exercise :func:`compute_coverage`
          mechanics and need a deterministic, sync mapper

        For production decomposer integration use
        :func:`compute_coverage_with_llm` instead — it issues a single
        LLM call that maps all outcomes to all tasks at once.

    Returns ``True`` when the lower-cased, stopword-filtered token sets
    of ``(outcome.action + outcome.success_signal)`` and
    ``(task.name + task.description)`` share at least one token.

    Parameters
    ----------
    outcome : UserOutcome
        The user-visible capability under check.
    task : Task
        The candidate task.

    Returns
    -------
    bool
        ``True`` if the token sets overlap.  This is necessary but not
        sufficient evidence that the task addresses the outcome — see
        the warning above.
    """
    outcome_tokens = _tokens(outcome.action + " " + outcome.success_signal)
    task_tokens = _tokens(task.name + " " + (task.description or ""))
    return bool(outcome_tokens & task_tokens)


def compute_coverage(
    outcomes: Iterable[UserOutcome],
    tasks: Iterable[Task],
    mapper: Callable[[UserOutcome, Task], bool],
) -> Dict[str, List[str]]:
    """Map every outcome (by id) to the task ids that address it.

    The ``mapper`` argument is **required**.  There is no implicit
    default because the only sync mapper in this module
    (:func:`keyword_overlap_mapper`) produces false positives on the
    snake_game-v31 case the whole module exists to catch — a
    contributor importing :func:`compute_coverage` without thinking
    about which mapper to use would silently re-introduce the v31
    regression.

    Parameters
    ----------
    outcomes : iterable of UserOutcome
        The outcomes extracted from the spec.  Order is preserved in
        the returned dict via insertion order.
    tasks : iterable of Task
        Decomposer-generated tasks to test.
    mapper : callable
        ``(outcome, task) -> bool`` predicate.  Pass
        :func:`keyword_overlap_mapper` for fallback / testing only;
        production callers should use :func:`compute_coverage_with_llm`
        instead, which performs the mapping with a single LLM call.

    Returns
    -------
    dict
        ``{outcome.id: [task.id, ...]}``.  Every outcome appears as a
        key; the list is empty when no task covers it.
    """
    task_list = list(tasks)
    coverage: Dict[str, List[str]] = {}
    for outcome in outcomes:
        coverage[outcome.id] = [task.id for task in task_list if mapper(outcome, task)]
    return coverage


def find_gaps(
    outcomes: Iterable[UserOutcome],
    coverage: Dict[str, List[str]],
) -> List[UserOutcome]:
    """Return in-scope outcomes that have no covering task.

    Out-of-scope outcomes are excluded — they are tracked in the audit
    trail but never count as gaps because the spec said not to build them.

    Parameters
    ----------
    outcomes : iterable of UserOutcome
        Outcomes to check.
    coverage : dict
        Output of :func:`compute_coverage`.

    Returns
    -------
    list of UserOutcome
        Outcomes that are in-scope and uncovered, in original order.
    """
    return [
        outcome
        for outcome in outcomes
        if outcome.scope == "in_scope" and not coverage.get(outcome.id, [])
    ]


def compute_intent_fidelity_score(
    outcomes: Iterable[UserOutcome],
    coverage: Dict[str, List[str]],
) -> float:
    """Fraction of in-scope outcomes that have at least one covering task.

    Returns ``1.0`` when there are no in-scope outcomes — vacuously
    satisfied, since there is nothing to fail.  Other choices (NaN,
    0.0, raise) all surprise consumers; treat absence as full fidelity.

    Parameters
    ----------
    outcomes : iterable of UserOutcome
        All outcomes (in-scope and out-of-scope).
    coverage : dict
        Output of :func:`compute_coverage`.

    Returns
    -------
    float
        Score in ``[0.0, 1.0]``.
    """
    in_scope = [o for o in outcomes if o.scope == "in_scope"]
    if not in_scope:
        return 1.0
    covered = sum(1 for o in in_scope if coverage.get(o.id))
    return covered / len(in_scope)


_LLM_COVERAGE_PROMPT = """\
You are evaluating whether each user-visible outcome is addressed by
at least one task in a decomposed task graph.

A task "addresses" an outcome only when finishing it would contribute
in a user-observable way to the outcome.  Internal logic that only
maintains state, validates input, or supports another task does NOT
address a user outcome unless it produces something the user sees,
hears, or otherwise observes.

Example (the snake_game-v31 case):
- Outcome: "user can play the snake game" (signal: snake visibly
  moves on a board)
- Task "Snake state machine" (track snake body) — does NOT address
  the outcome (no rendering)
- Task "Render snake to canvas" (draw snake/food/score) — DOES
  address the outcome (produces user-visible movement)

DEFAULT TO EMPTY.  The decomposer typically produces only internal
or structural tasks — data models, services, APIs, business logic —
in the first pass.  Most user-visible outcomes will have NO covering
task in this graph.  Returning an empty list for an outcome IS the
expected case, not a failure.  An empty result with score 0.0 is a
healthy honest signal; a falsely-full result with score 1.0 hides
real gaps and breaks downstream gap-fill.

More domain examples (the principle, applied):

  Web app with REST backend (the user is a human in a browser):
    Outcome: "user can sign up"
    - Task "Signup form page that POSTs to /api/users and renders
      success or error" → ADDRESSES (form is visible to the user;
      the rendered result is the user-observable signal)
    - Task "POST /api/users endpoint" → does NOT (backend plumbing —
      without a UI calling it, the user sees nothing; this is
      supporting work for the form task above)
    - Task "User schema validation" → does NOT (internal precondition)

  Developer-facing API product (the user IS an API consumer):
    Outcome: "developer can create a user via the API"
    - Task "POST /api/users endpoint that returns 201 with user JSON"
      → ADDRESSES (the API response IS the product surface for this
      user; the developer reads the JSON directly)
    - Task "User schema validation" → does NOT (internal precondition)

  Same task, different verdict: who the "user" is determines whether
  a backend endpoint is the surface or the plumbing.  Read the spec
  to identify the user.  When the spec says "REST API backend, web
  frontend," the user is the human in the browser and the endpoint
  is plumbing.  When the spec says "REST API for third-party
  developers," the user is a developer and the endpoint is the
  surface.  When uncertain, treat backend-only tasks as plumbing.

  File-handling:
    Outcome: "user can upload a recipe photo"
    - Task "Photo upload form with progress indicator" → ADDRESSES
      (form is shown, progress is observable)
    - Task "Image storage backend" → does NOT (internal plumbing)

  Notification:
    Outcome: "user is alerted when their post is liked"
    - Task "Send like notification to user device" → ADDRESSES
      (device notification is observable)
    - Task "Like event aggregation worker" → does NOT (internal
      counting)

  CLI:
    Outcome: "user sees results of their query"
    - Task "Print query results to stdout" → ADDRESSES (terminal
      output is observable)
    - Task "Query optimizer" → does NOT (internal performance)

The pattern across all of these: a task addresses an outcome when
COMPLETING IT PRODUCES the observable signal the user can sense.
Internal correctness, validation, storage, scheduling, contract
definition, and service plumbing do not — even when their names
sound related to the outcome.  Domains differ; the principle does
not.  Apply the same judgment to whatever domain this project is
in, not just the ones above.

Tiebreaker: when you cannot describe what user-observable evidence
the task produces, return an empty list.  When in doubt, empty
list.  False positives are worse than false negatives because they
hide gaps from the next pipeline stage.

Outcomes:
{outcomes_block}

Tasks:
{tasks_block}

Return strict JSON of the form:

{{
  "coverage": {{
    "<outcome_id>": ["<task_id>", "<task_id>", ...]
  }}
}}

Rules:
- Every outcome.id must appear as a key, even if the list is empty.
- Only include a task id when you can describe what user-observable
  evidence completing that task produces (a screen shown, an HTTP
  response, a file written that the user reads, a notification
  delivered).  If the task only produces internal state, return
  an empty list for that outcome.
- Do not invent task ids.  Use exactly the ids from the input.
- Respond with ONLY the JSON object — no preamble, no markdown fences.
"""


async def compute_coverage_with_llm(
    outcomes: Iterable[UserOutcome],
    tasks: Iterable[Task],
    llm_client: Any,
    max_tokens: int = 2000,
) -> Dict[str, List[str]]:
    """Compute outcome → task coverage via a single LLM call.

    Production decomposer integration uses this function rather than
    :func:`compute_coverage` with a sync mapper.  One LLM call evaluates
    all outcomes against all tasks at once — cheaper than per-pair calls,
    and the LLM has the global context to distinguish "supports" from
    "addresses" (the keyword mapper cannot).

    Parameters
    ----------
    outcomes : iterable of UserOutcome
        Outcomes extracted from the spec.
    tasks : iterable of Task
        Tasks produced by the decomposer.
    llm_client : Any
        Async client exposing ``analyze(prompt, context)``.  Mocked in
        tests.
    max_tokens : int, optional
        Token budget for the LLM call.  Larger task graphs may need
        more headroom.

    Returns
    -------
    dict
        ``{outcome.id: [task.id, ...]}`` — same shape as
        :func:`compute_coverage`.  Every outcome appears as a key;
        unknown outcome ids in the LLM response are dropped, missing
        outcome ids are filled with empty lists.

    Raises
    ------
    ValueError
        On malformed JSON or a missing ``coverage`` key.
    """
    outcome_list = list(outcomes)
    task_list = list(tasks)

    if not outcome_list:
        return {}
    if not task_list:
        return {o.id: [] for o in outcome_list}

    outcomes_block = "\n".join(
        f"- {o.id}: {o.action} (signal: {o.success_signal})" for o in outcome_list
    )
    tasks_block = "\n".join(
        f"- {t.id}: {t.name} — {t.description or '(no description)'}" for t in task_list
    )
    prompt = _LLM_COVERAGE_PROMPT.format(
        outcomes_block=outcomes_block, tasks_block=tasks_block
    )

    from src.utils.structured_llm import safe_structured_call

    try:
        payload = await safe_structured_call(
            llm=llm_client,
            prompt=prompt,
            operation="outcome_coverage_check",
            initial_max_tokens=max_tokens,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"LLM coverage: malformed JSON: {exc}") from exc

    raw_coverage = payload.get("coverage")
    if not isinstance(raw_coverage, dict):
        raise ValueError("LLM coverage: response missing 'coverage' object")

    valid_outcome_ids = {o.id for o in outcome_list}
    valid_task_ids = {t.id for t in task_list}
    coverage: Dict[str, List[str]] = {oid: [] for oid in valid_outcome_ids}
    for outcome_id, task_ids in raw_coverage.items():
        if outcome_id not in valid_outcome_ids:
            continue
        if not isinstance(task_ids, list):
            continue
        coverage[outcome_id] = [
            tid for tid in task_ids if isinstance(tid, str) and tid in valid_task_ids
        ]
    return coverage


_GAP_FILL_PROMPT_HEADER = """\
The following user-visible outcomes are required by the specification
but the current task graph does not cover them.  Generate the minimum
set of tasks that would address each gap.

Specification:
{spec}

Uncovered outcomes:
{gap_list}

Existing tasks already in the graph (so you can ground ``requires``
references in real upstream task names instead of inventing labels):

{existing_tasks_block}
"""

_GAP_FILL_PROMPT_CONTRACT_SECTION = """\

Existing contract artifacts already generated for this project.  When
your synthesized task consumes or implements one of these interfaces,
quote the exact field names and contract identifiers from below — do
NOT invent contract names.  This is the same contract context that
the decomposer's original tasks were built against; gap-fill tasks
must integrate with that contract surface.

{contract_block}
"""

_GAP_FILL_PROMPT_SCHEMA_NO_CONTRACT = """\

Return strict JSON of the form:

{{
  "tasks": [
    {{
      "name": "<short task name>",
      "description": "<what to build, including how downstream agents will consume it>",
      "provides": "<interface this task makes available, or null>",
      "requires": "<interface this task consumes from upstream, or null>"
    }}
  ]
}}

Rules:
- One or more tasks per gap is allowed.
- Task names must be concrete (e.g. ``"Render snake to canvas"``) not
  generic (``"implement feature"``).
- Descriptions must say WHAT to build, not HOW.  No library choices.
- ``provides`` names an interface this task makes available to the
  rest of the graph.  Use ``null`` when the task is a pure
  user-visible endpoint with no downstream consumer.
- ``requires`` names an interface this task consumes from an existing
  task in the graph.  Use ``null`` when the task is self-contained.
- Return ONLY the JSON object — no preamble, no markdown fences.
"""

_GAP_FILL_PROMPT_SCHEMA_WITH_CONTRACT = """\

Return strict JSON of the form:

{{
  "tasks": [
    {{
      "name": "<short task name>",
      "description": "<what to build, including how downstream agents will consume it>",
      "provides": "<interface this task makes available, or null>",
      "requires": "<interface this task consumes from upstream, or null>",
      "responsibility": "<contract interface this task OWNS, or null>"
    }}
  ]
}}

Rules:
- One or more tasks per gap is allowed.
- Task names must be concrete (e.g. ``"Render snake to canvas"``) not
  generic (``"implement feature"``).
- Descriptions must say WHAT to build, not HOW.  No library choices.
- ``provides`` and ``requires`` MUST quote names that exist in the
  contract artifacts above when the task consumes or produces a
  contract-defined interface.  Use ``null`` only for tasks with no
  contract-side relationship.
- ``responsibility`` names the specific contract interface this task
  owns (the canonical "build this side of the contract" framing).
  Format: ``"implements <InterfaceName> from <relative_path>"``.  Use
  ``null`` when the task is purely a consumer.
- Return ONLY the JSON object — no preamble, no markdown fences.
"""


def _format_existing_tasks_block(existing_tasks: List[Task]) -> str:
    """Render the existing-tasks portion of the gap-fill prompt."""
    if not existing_tasks:
        return "(no tasks in the graph yet)"
    return "\n".join(
        f"- {t.id}: {t.name} — {t.description or '(no description)'}"
        for t in existing_tasks
    )


def _format_contract_block(contract_artifacts: Dict[str, Any]) -> str:
    """Render the contract-artifacts portion of the gap-fill prompt.

    ``contract_artifacts`` is the structure produced by
    ``_generate_contracts_by_domain`` in ``advanced_parser.py``: a
    domain-keyed dict where each value has an ``"artifacts"`` list,
    each artifact having ``filename`` / ``content`` /
    ``relative_path``.
    """
    sections: List[str] = []
    for domain, payload in contract_artifacts.items():
        if not payload:
            continue
        artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
        if not isinstance(artifacts, list):
            continue
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            filename = (
                artifact.get("filename") or artifact.get("relative_path") or "<unknown>"
            )
            content = artifact.get("content") or ""
            sections.append(
                f"### Domain: {domain}\n### File: {filename}\n```\n{content}\n```"
            )
    if not sections:
        return "(no usable contract artifacts)"
    return "\n\n".join(sections)


async def fill_gaps(
    spec: str,
    gaps: List[UserOutcome],
    existing_tasks: List[Task],
    llm_client: Any,
    *,
    contract_artifacts: Optional[Dict[str, Any]] = None,
    max_tokens: int = 2000,
) -> List[Dict[str, Any]]:
    """Generate replacement tasks for uncovered outcomes via a single LLM call.

    The LLM sees the existing task graph (so it can ground its
    ``requires`` references in real task names) and, when available,
    the contract artifacts the decomposer used (so its ``provides`` /
    ``requires`` / ``responsibility`` fields name real contract
    interfaces rather than inventing labels).  This is what fixes the
    PR #454-era failure mode where gap-fill tasks shipped with
    ungrounded contract metadata that nothing else in the graph could
    consume.

    No call is made when ``gaps`` is empty.

    Parameters
    ----------
    spec : str
        Project specification, included verbatim so the LLM can
        respect scope language.
    gaps : list of UserOutcome
        Outcomes the decomposer missed.  Out-of-scope outcomes have
        already been filtered out by the caller.
    existing_tasks : list of Task
        Tasks already in the graph.  Required — the LLM uses these to
        ground its ``requires`` references in real task names instead
        of inventing labels.  Pass an empty list only when the graph
        truly has no tasks yet (rare; coverage check would not have
        produced gaps in that case).
    llm_client : Any
        Async client exposing ``analyze(prompt, context)``.
    contract_artifacts : Optional[Dict[str, Any]], keyword-only
        The contract artifacts produced by
        ``_generate_contracts_by_domain``.  When provided, the prompt
        includes a contract section and asks for a ``responsibility``
        field on each task; when ``None`` (feature-based path), the
        contract section and responsibility field are omitted.
    max_tokens : int, keyword-only, default 2000
        Token budget for the LLM call.

    Returns
    -------
    list of dict
        Each dict has:

        - ``name`` (str): short task name
        - ``description`` (str): what to build
        - ``provides`` (str | None): interface this task exposes
        - ``requires`` (str | None): interface this task consumes
        - ``responsibility`` (str | None): contract interface this
          task owns (only present when ``contract_artifacts`` was
          supplied; ``None`` for purely-consumer tasks)

    Raises
    ------
    ValueError
        On malformed JSON, missing ``tasks`` key, missing required
        ``name`` / ``description``, or any contract field with a
        non-string-or-null value.
    """
    if not gaps:
        return []

    gap_lines = "\n".join(
        f"- {gap.id}: {gap.action} (success: {gap.success_signal})" for gap in gaps
    )
    existing_block = _format_existing_tasks_block(existing_tasks)

    contracts_present = contract_artifacts is not None and bool(contract_artifacts)
    if contracts_present:
        contract_block = _format_contract_block(contract_artifacts or {})
        contract_section = _GAP_FILL_PROMPT_CONTRACT_SECTION.format(
            contract_block=contract_block
        )
        schema_section = _GAP_FILL_PROMPT_SCHEMA_WITH_CONTRACT
    else:
        contract_section = ""
        schema_section = _GAP_FILL_PROMPT_SCHEMA_NO_CONTRACT

    prompt = (
        _GAP_FILL_PROMPT_HEADER.format(
            spec=spec, gap_list=gap_lines, existing_tasks_block=existing_block
        )
        + contract_section
        + schema_section
    )

    from src.utils.structured_llm import safe_structured_call

    try:
        payload = await safe_structured_call(
            llm=llm_client,
            prompt=prompt,
            operation="outcome_gap_fill",
            initial_max_tokens=max_tokens,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Outcome gap-fill: LLM returned malformed JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(
            "Outcome gap-fill: LLM JSON must be an object with 'tasks' key"
        )
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValueError("Outcome gap-fill: 'tasks' must be a JSON array")

    # Optional contract fields.  ``responsibility`` is only meaningful
    # when contracts were supplied — for feature-based gap-fill, it is
    # omitted from the output entirely.
    optional_contract_fields: tuple[str, ...] = ("provides", "requires")
    if contracts_present:
        optional_contract_fields = optional_contract_fields + ("responsibility",)

    validated: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            raise ValueError(f"Outcome gap-fill: task at index {idx} is not an object")

        # Required fields: name, description must be strings.  Reject
        # null / non-string rather than coerce — str(None) becomes the
        # literal string "None" which would silently pass the
        # empty-string checks below.
        for required_field in ("name", "description"):
            value = item.get(required_field)
            if not isinstance(value, str):
                raise ValueError(
                    f"Outcome gap-fill: task at index {idx} field "
                    f"{required_field!r} must be a string, got "
                    f"{type(value).__name__}: {value!r}"
                )

        # Optional contract fields should be string or null. When the
        # LLM returns something else (commonly ``requires`` as a LIST of
        # upstream task ids), do NOT abort the whole coverage pass — the
        # handler below already coerces any non-string to ``None``, so a
        # hard raise here was stricter than the handling and silently
        # no-opped gap-fill, signal enrichment, AND gotcha enumeration
        # (#680) for the entire project. Log and tolerate instead.
        for optional_field in optional_contract_fields:
            if optional_field in item:
                value = item[optional_field]
                if value is not None and not isinstance(value, str):
                    logger.debug(
                        "Outcome gap-fill: task at index %d field %r was "
                        "%s, not a string; coercing to null",
                        idx,
                        optional_field,
                        type(value).__name__,
                    )

        name = item["name"].strip()
        description = item["description"].strip()
        if not name:
            raise ValueError(f"Outcome gap-fill: task at index {idx} missing 'name'")
        if not description:
            raise ValueError(
                f"Outcome gap-fill: task at index {idx} missing 'description'"
            )

        provides_raw = item.get("provides")
        requires_raw = item.get("requires")
        provides = provides_raw.strip() if isinstance(provides_raw, str) else None
        requires = requires_raw.strip() if isinstance(requires_raw, str) else None

        result_dict: Dict[str, Any] = {
            "name": name,
            "description": description,
            "provides": provides if provides else None,
            "requires": requires if requires else None,
        }

        if contracts_present:
            responsibility_raw = item.get("responsibility")
            responsibility = (
                responsibility_raw.strip()
                if isinstance(responsibility_raw, str)
                else None
            )
            result_dict["responsibility"] = responsibility if responsibility else None

        validated.append(result_dict)

    return validated


@dataclass
class OutcomeCoverageResult:
    """Result of running the full outcome coverage pipeline.

    Returned by :func:`apply_outcome_coverage`.  Each decomposer
    converts ``synthesized_tasks`` (gap-fill output dicts) into proper
    :class:`Task` objects in its own conventions — feature-based
    tasks get default fields; contract-first tasks get
    ``responsibility`` set from the dict.

    Attributes
    ----------
    synthesized_tasks : list of dict
        Gap-fill output dicts (same shape as :func:`fill_gaps`
        returns).  Empty when coverage was full or when no outcomes
        were supplied.
    intent_fidelity_score : float
        Final score on the augmented graph (post gap-fill).  In
        ``[0.0, 1.0]``.  ``1.0`` when there are no in-scope outcomes
        (vacuously satisfied).
    coverage_before_fill : dict
        ``{outcome.id: [task.id, ...]}`` from the initial coverage
        check, before any gap-fill ran.  Useful for logging /
        debugging the original task graph's coverage.
    gaps : list of UserOutcome
        In-scope outcomes that had no covering task in the initial
        graph.  These are the inputs to :func:`fill_gaps`.  Empty
        when coverage was full.
    """

    synthesized_tasks: List[Dict[str, Any]]
    intent_fidelity_score: float
    coverage_before_fill: Dict[str, List[str]] = field(default_factory=dict)
    coverage_after_fill: Optional[Dict[str, List[str]]] = None
    gaps: List[UserOutcome] = field(default_factory=list)


# Public so tests don't have to hardcode the literal in mock LLM
# responses.  The prefix marks task IDs we synthesize internally for
# the recoverage check; the caller's task_factory replaces them with
# proper IDs (gap_fill_<uuid> or kanban-backed) before the augmented
# graph reaches downstream consumers.
STUB_TASK_ID_PREFIX: str = "_synth_for_coverage_"


def _normalize_gap_task_name(name: str) -> str:
    """Convert ``snake_case`` LLM slug to a readable task name.

    Weak local models occasionally ignore the prompt instruction to
    produce concrete human-readable names (e.g. ``"Render snake to
    canvas"``) and instead return Python-style slugs such as
    ``"render_snake_to_canvas"``.  This normalizer detects the slug
    pattern — underscores present, no spaces — and converts it to
    Title Case.

    Also promotes the ``Task X`` artifact prefix to ``Implement X``.
    Haiku and qwen-class models pattern-match the literal word
    ``task`` out of the gap-fill schema's ``"<short task name>"``
    field description and stamp it as a name prefix.  The result was
    a board with ``Task Signup Form`` / ``Task Login Form`` cards
    from gap_fill_contract synthesis — information-free on a kanban
    board (everything is a task) and inconsistent with the
    feature_based decomposer's ``Implement {feature_name}`` convention
    at ``advanced_parser.py:2980``.  Promoting to ``Implement`` gives
    the board one verb for the same semantic role.  This is a
    normalization of LLM artifact noise, not a HOW prescription — it
    doesn't tell agents how to implement anything.

    Already-readable names (and names already starting with
    ``Implement``) pass through unchanged.

    Parameters
    ----------
    name : str
        Raw name from the gap-fill LLM response.

    Returns
    -------
    str
        Human-readable name; empty string when input is blank.

    Examples
    --------
    >>> _normalize_gap_task_name("render_game_board")
    'Render Game Board'
    >>> _normalize_gap_task_name("Task Signup Form")
    'Implement Signup Form'
    >>> _normalize_gap_task_name("task_signup_form")
    'Implement Signup Form'
    >>> _normalize_gap_task_name("Render game board")
    'Render game board'
    >>> _normalize_gap_task_name("")
    ''
    """
    name = name.strip()
    if not name:
        return name
    # Slug detection: underscores present AND no spaces.  Track
    # whether the slug pass fired so the prefix promotion below can
    # use it as a signal that the input was an LLM artifact rather
    # than a human-readable name.
    was_slug = False
    if "_" in name and " " not in name:
        name = " ".join(word.capitalize() for word in name.split("_"))
        was_slug = True
    # Promote ``Task X`` / ``Task: X`` to ``Implement X`` ONLY when
    # the input was a slug.  Codex P2 on PR #509: an unconditional
    # rewrite would mangle legitimate domain nouns like
    # ``Task Creation Form`` / ``Task Assignment Rules`` in a
    # task-management product, where ``Task`` IS the domain term.
    # The slug-converted path is the actual LLM artifact (Haiku /
    # qwen pattern-match the literal ``task`` out of the schema's
    # ``"<short task name>"`` and emit ``task_signup_form``);
    # human-readable ``Task X`` names from the LLM are trusted as
    # intentional.  Bare ``"Task"`` with no payload is left alone —
    # it signals upstream prompt failure that we shouldn't silently
    # rewrite into ``"Implement "``.
    if was_slug:
        for prefix in ("Task: ", "Task- ", "Task "):
            if name.startswith(prefix):
                remainder = name[len(prefix) :].strip()
                if remainder:
                    return f"Implement {remainder}"
                break
    return name


def _build_recoverage_description(gap_dict: Dict[str, Any]) -> str:
    """Render a description that surfaces contract metadata for the recheck.

    The post-fill coverage check is an LLM call that scores tasks by
    name + description only.  When gap-fill emits a task with
    ``provides`` / ``requires`` / ``responsibility``, those fields
    carry semantic load — the LLM should see them when judging
    whether the synthesized task actually covers an outcome.

    For example, a task whose description says only "draw on canvas"
    is ambiguous; appending ``(provides=RenderingAgent.draw)`` makes
    the contract surface visible to the recheck LLM, raising the
    odds it scores the task as covering the play-the-game outcome.
    """
    base = str(gap_dict["description"])
    parts: List[str] = []
    provides = gap_dict.get("provides")
    requires = gap_dict.get("requires")
    responsibility = gap_dict.get("responsibility")
    if provides:
        parts.append(f"provides={provides}")
    if requires:
        parts.append(f"requires={requires}")
    if responsibility:
        parts.append(f"responsibility={responsibility}")
    if not parts:
        return base
    return f"{base}\n\nContract: " + ", ".join(parts)


def _make_stub_task_for_coverage(idx: int, gap_dict: Dict[str, Any]) -> Task:
    """Build a stub :class:`Task` for the post-fill coverage recheck.

    :func:`compute_coverage_with_llm` only reads ``id`` / ``name`` /
    ``description`` from each task when rendering its prompt, so the
    other Task fields can be filler.  The description is enriched
    with contract metadata via :func:`_build_recoverage_description`
    so the recheck LLM has full signal when scoring.

    Real task construction (with sibling-inherited estimated_hours,
    project_id, contract responsibility, etc.) happens in the
    caller's task_factory after :func:`apply_outcome_coverage`
    returns.
    """
    now = datetime.now(timezone.utc)
    return Task(
        id=f"{STUB_TASK_ID_PREFIX}{idx}",
        name=_normalize_gap_task_name(gap_dict["name"]),
        description=_build_recoverage_description(gap_dict),
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
    )


async def apply_outcome_coverage(
    *,
    spec: str,
    outcomes: List[UserOutcome],
    tasks: List[Task],
    llm_client: Any,
    contract_artifacts: Optional[Dict[str, Any]] = None,
) -> OutcomeCoverageResult:
    """Run the full intent-fidelity coverage pipeline.

    Both decomposers (``parse_prd_to_tasks`` and
    ``decompose_by_contract``) call this internally after producing
    their initial task graph.  Each decomposer then converts
    ``result.synthesized_tasks`` into proper :class:`Task` objects in
    its own conventions and appends to its output.

    Pipeline:

    1. Coverage check on the initial task graph (1 LLM call)
    2. Identify gaps among in-scope outcomes
    3. If gaps exist:
       a. Gap-fill with full context (1 LLM call) — sees the
          existing task graph and, when supplied, the contract
          artifacts so its provides/requires/responsibility quote
          real interface names
       b. Recompute coverage on the augmented graph (1 LLM call) —
          gap-fill is an LLM call that might produce tasks that
          don't actually cover; the score reflects measured coverage
          on the augmented graph, not assumed
    4. Score = covered_in_scope / total_in_scope from the final coverage

    Total LLM cost: 1 call when no gaps; 3 calls when gaps exist.

    Parameters
    ----------
    spec : str
        Project specification, passed to :func:`fill_gaps` for
        context.
    outcomes : list of UserOutcome
        Outcomes extracted from the spec.  Empty list returns a
        vacuous-success result with no LLM calls.
    tasks : list of Task
        The initial task graph the decomposer just produced.
    llm_client : Any
        Async client exposing ``analyze(prompt, context)``.
    contract_artifacts : Optional[Dict[str, Any]], keyword-only
        Contract artifacts from ``_generate_contracts_by_domain``.
        When provided, gap-fill emits a ``responsibility`` field
        per synthesized task (contract-first path).  When ``None``,
        gap-fill omits ``responsibility`` (feature-based path).

    Returns
    -------
    OutcomeCoverageResult
        Synthesized tasks + score + audit-trail fields.

    Raises
    ------
    ValueError
        From :func:`compute_coverage_with_llm` or :func:`fill_gaps`
        when an LLM call returns malformed JSON.  Callers
        (decomposers) should catch and downgrade to a warning rather
        than fail the whole project — the legacy "no coverage check"
        path is the always-available fallback.
    """
    if not outcomes:
        return OutcomeCoverageResult(
            synthesized_tasks=[],
            intent_fidelity_score=1.0,
        )

    coverage_before = await compute_coverage_with_llm(
        outcomes=outcomes,
        tasks=tasks,
        llm_client=llm_client,
    )
    gaps = find_gaps(outcomes, coverage_before)

    if not gaps:
        # No gap-fill needed.  Score from the initial coverage map.
        return OutcomeCoverageResult(
            synthesized_tasks=[],
            intent_fidelity_score=compute_intent_fidelity_score(
                outcomes, coverage_before
            ),
            coverage_before_fill=coverage_before,
            gaps=[],
        )

    synthesized_dicts = await fill_gaps(
        spec=spec,
        gaps=gaps,
        existing_tasks=tasks,
        llm_client=llm_client,
        contract_artifacts=contract_artifacts,
    )

    if not synthesized_dicts:
        # Gap-fill produced nothing — degraded LLM behavior, but the
        # function did not raise.  Score from coverage_before since
        # the graph is unchanged.
        return OutcomeCoverageResult(
            synthesized_tasks=[],
            intent_fidelity_score=compute_intent_fidelity_score(
                outcomes, coverage_before
            ),
            coverage_before_fill=coverage_before,
            gaps=gaps,
        )

    # Build stubs for the recoverage check; the real Task
    # construction (with proper hours / project_id / responsibility)
    # happens in the caller's task_factory after this returns.
    stubs = [
        _make_stub_task_for_coverage(idx, d) for idx, d in enumerate(synthesized_dicts)
    ]
    augmented_tasks = list(tasks) + stubs
    coverage_after = await compute_coverage_with_llm(
        outcomes=outcomes,
        tasks=augmented_tasks,
        llm_client=llm_client,
    )

    return OutcomeCoverageResult(
        synthesized_tasks=synthesized_dicts,
        intent_fidelity_score=compute_intent_fidelity_score(outcomes, coverage_after),
        coverage_before_fill=coverage_before,
        coverage_after_fill=coverage_after,
        gaps=gaps,
    )


# ---------------------------------------------------------------------------
# Graph-level adapters (issue #456 Stage 5)
#
# These functions take/return Task graphs (not just outcome data) and
# adapt the lower-level :func:`apply_outcome_coverage` into the
# :class:`AugmentationResult` shape that the augmenter chain consumes.
#
# Pre-#456-Stage-5 these lived as private methods on
# ``AdvancedPRDParser``.  Lifting them to public module functions
# decouples ``OutcomeCoverageAugmenter`` from the parser's private API
# (Kaia review #3, Simon ``4453bd2c``) — the augmenter now constructs
# with ``llm_client`` only and dispatches to these by name.
# ---------------------------------------------------------------------------


def _coverage_to_telemetry(
    coverage: OutcomeCoverageResult,
) -> Dict[str, Any]:
    """Flatten an OutcomeCoverageResult into PLANNING_INTENT_FIDELITY shape.

    Keys are pinned to the event payload at
    ``src/integrations/nlp_tools.py:396-399``:
    ``intent_fidelity_score``, ``coverage_before_fill``,
    ``coverage_after_fill``, ``gap_filled_outcomes``.  Cato consumers
    read this slice via ``result.telemetry["outcome_coverage"]``.
    """
    return {
        "intent_fidelity_score": coverage.intent_fidelity_score,
        "coverage_before_fill": coverage.coverage_before_fill,
        "coverage_after_fill": coverage.coverage_after_fill,
        "gap_filled_outcomes": [g.id for g in coverage.gaps],
    }


#: Prefix stamped on every acceptance-criterion appended by
#: :func:`_enrich_acceptance_criteria_with_signals`.  Stable string so
#: downstream consumers (WorkAnalyzer, log readers, audit tooling) can
#: tell user-outcome signal criteria apart from the LLM-emitted or
#: template-generated criteria already on the task, and so re-running
#: enrichment is idempotent without parsing the success_signal text.
SIGNAL_CRITERION_PREFIX: str = "User outcome verifiable: "

#: Prefix stamped on every gap-fill-derived ``completion_criteria``
#: string. Mirrors :data:`SIGNAL_CRITERION_PREFIX` in spirit: lets the
#: rollup pass be idempotent (re-runs see the prefix and skip) and lets
#: audits identify which criteria came from the #607-step-4 rollup vs
#: which were authored by the decomposer's per-task path.
OUTCOME_GAP_CRITERION_PREFIX: str = "Implementation must cover: "

#: Prefix stamped on every acceptance-criterion appended by the #680
#: gotcha-enumeration pass. Distinct from :data:`SIGNAL_CRITERION_PREFIX`
#: so audits / WorkAnalyzer / the self-verify skeptic can tell a known
#: failure mode ("reversal is a no-op") apart from a user-outcome signal
#: ("snake moves when arrow pressed"). The prefix also keeps the pass
#: idempotent: a re-run sees the stamp and skips the duplicate.
GOTCHA_CRITERION_PREFIX: str = "Known failure mode to handle: "


def _gap_dict_to_criterion(gap_dict: Dict[str, Any]) -> str:
    """Convert a gap-fill output dict to a single criterion string.

    Issue #607 step 4 helper. The criterion names the user-outcome
    behavior that the anchor task's implementation must cover; it does
    NOT prescribe HOW the behavior is implemented (no framework, no
    pattern, no internal code structure). Two agents reading the same
    criterion are free to write legitimately different code.

    Contract-first gap dicts carry an additional ``responsibility``
    field ("implements <Iface> from <path>") that the pre-step-4
    ``_build_contract_gap_fill_task`` projected onto
    ``Task.responsibility`` / contract source_context. The rollup path
    no longer materializes that Task field, so when ``responsibility``
    is present it is appended to the criterion text — the only channel
    that surfaces contract ownership for gap-driven contract work
    after the rollup (Codex P2 on PR #611). Agents reading the
    criterion still see the contract framing even though Task-field
    rendering (Layer 1.3 of ``build_tiered_instructions``) no longer
    fires for these gaps.

    Parameters
    ----------
    gap_dict : dict
        One entry from :attr:`OutcomeCoverageResult.synthesized_tasks`,
        shape ``{"name": str, "description": str,
        "responsibility"?: str, ...}``.

    Returns
    -------
    str
        Criterion string starting with
        :data:`OUTCOME_GAP_CRITERION_PREFIX`. Idempotent re-runs find
        this stamp and skip duplicates.
    """
    name = (gap_dict.get("name") or "").strip()
    description = (gap_dict.get("description") or "").strip()
    responsibility = (gap_dict.get("responsibility") or "").strip()
    if name and description:
        body = f"{name} — {description}"
    else:
        body = name or description
    if responsibility:
        body = f"{body} (contract: {responsibility})"
    return f"{OUTCOME_GAP_CRITERION_PREFIX}{body}"


#: Cap on implementation tasks synthesized from core gaps in a single
#: run (#683 Cause 2). #607-step-4 moved away from synthesizing a task
#: per gap to fight ATOMIZATION (too many tiny tasks). We reinstate
#: synthesis only for *core* gaps with no implementation home, and cap
#: the count so a pathological gap list cannot re-atomize the graph —
#: excess gaps beyond the cap fall back to the completion_criteria
#: rollup and are logged.
GAP_SYNTH_CAP: int = 8


def _gap_contract_round_trip(
    gap: Dict[str, Any], description: str
) -> Tuple[str, Dict[str, Any]]:
    """Make a synthesized gap task's contract ownership survive the board.

    Codex P1 on PR #687. A contract-first gap carries ``responsibility``.
    Setting it only on the top-level ``Task.responsibility`` field is not
    enough: that field is the SQLite-native column, but the kanban
    conversion path (``TaskBuilder.build_task_data``) persists
    ``source_context`` and the description, NOT arbitrary top-level
    fields. So on the production round-trip the synthesized task loses
    its contract ownership before ``request_next_task`` and the
    contract-responsibility instruction layer never fires for exactly
    these tasks.

    ``_parse_contract_metadata`` in ``marcus_mcp/tools/task.py`` reads
    contract metadata from three sources in priority order: the native
    attribute, ``source_context["responsibility"]``, then the
    ``<!-- MARCUS_CONTRACT_FIRST: responsibility | contract_file -->``
    description marker. We populate sources 2 and 3 here so the metadata
    round-trips through every provider (source_context for JSON-capable
    providers, the marker as the universal fallback for ones like Planka
    that drop arbitrary fields).

    Returns ``(description_with_marker, source_context)``. When the gap
    has no ``responsibility`` both are returned unchanged/empty.
    """
    responsibility = gap.get("responsibility")
    if not responsibility:
        return description, {}
    contract_file = gap.get("contract_file") or ""
    source_context: Dict[str, Any] = {"responsibility": responsibility}
    if contract_file:
        source_context["contract_file"] = contract_file
    marker = f"<!-- MARCUS_CONTRACT_FIRST: {responsibility} | {contract_file} -->"
    description = f"{description}\n\n{marker}" if description else marker
    return description, source_context


def _build_impl_task_from_gap(gap: Dict[str, Any]) -> Optional[Task]:
    """Materialize one gap-fill dict into an implementation Task (#683).

    Mirrors :func:`_materialize_gap_dicts_as_rescue_tasks` but labels the
    task ``"implementation"`` (not ``"rescue"``) and is used on the
    NORMAL (non-empty-graph) path for core gaps that have no existing
    implementation task to host them. The normalized name (``Implement
    …`` / a readable verb phrase) classifies as ``implementation`` via
    :func:`~src.core.task_classification.get_task_type`, so #680 gotcha
    placement will treat it as a valid host.

    Returns ``None`` when the gap has no usable name.
    """
    from uuid import uuid4

    name = _normalize_gap_task_name(gap.get("name") or "")
    if not name:
        return None
    now = datetime.now(timezone.utc)
    responsibility = gap.get("responsibility")
    labels = ["gap_fill", "intent_fidelity", "implementation"]
    if responsibility:
        labels.append("contract")
    # Round-trip contract ownership via source_context + description
    # marker so it survives the kanban conversion (Codex P1 on PR #687).
    description, source_context = _gap_contract_round_trip(
        gap, gap.get("description", "")
    )
    return Task(
        id=f"gap_fill_{uuid4().hex[:12]}",
        name=name,
        description=description,
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=4.0,
        labels=labels,
        provides=gap.get("provides"),
        requires=gap.get("requires"),
        responsibility=responsibility,
        source_context=source_context or None,
    )


def _materialize_gap_dicts_as_rescue_tasks(
    gap_dicts: List[Dict[str, Any]],
) -> List[Task]:
    """Materialize gap-fill dicts into Tasks for the empty-graph case.

    Codex P1 on PR #611. ``_create_detailed_tasks`` can return ``[]``
    when AI decomposition fails entirely; before step 4 the gap-fill
    output rescued those projects by materializing tasks even with no
    siblings. Step 4's rollup needs an anchor task to route criteria
    onto — with zero native tasks there is no anchor, so gaps would be
    silently dropped. This helper restores the pre-step-4 rescue path
    scoped narrowly to the empty-native-graph case.

    The atomization concern that motivated #607 step 4 does not apply
    here: there are no native tasks to atomize against, the gap-fill
    output IS the entire graph.

    Parameters
    ----------
    gap_dicts
        :attr:`OutcomeCoverageResult.synthesized_tasks` — the gap-fill
        LLM's output dicts.

    Returns
    -------
    list of Task
        One :class:`Task` per gap dict, labeled ``["gap_fill",
        "intent_fidelity", "rescue"]`` so audits can identify rescue-
        path synthesized tasks distinctly from the (now-removed)
        atomization-path synthesized tasks. The ``"rescue"`` label
        marks the empty-graph fallback.
    """
    from uuid import uuid4

    now = datetime.now(timezone.utc)
    tasks: List[Task] = []
    for gap in gap_dicts:
        name = _normalize_gap_task_name(gap.get("name") or "")
        if not name:
            continue
        responsibility = gap.get("responsibility")
        labels = ["gap_fill", "intent_fidelity", "rescue"]
        if responsibility:
            labels.append("contract")
        # Round-trip contract ownership via source_context + description
        # marker so it survives the kanban conversion (Codex P1 on PR
        # #687) — the top-level responsibility field alone is dropped by
        # TaskBuilder.build_task_data on non-SQLite providers.
        description, source_context = _gap_contract_round_trip(
            gap, gap.get("description", "")
        )
        tasks.append(
            Task(
                id=f"gap_fill_{uuid4().hex[:12]}",
                name=name,
                description=description,
                status=TaskStatus.TODO,
                priority=Priority.MEDIUM,
                assigned_to=None,
                created_at=now,
                updated_at=now,
                due_date=None,
                # 4.0h matches the pre-step-4 sibling-median fallback
                # when sibling list was empty.
                estimated_hours=4.0,
                labels=labels,
                provides=gap.get("provides"),
                requires=gap.get("requires"),
                responsibility=responsibility,
                source_context=source_context or None,
            )
        )
    return tasks


def _find_integration_verification_task_id(
    tasks: List[Task],
) -> Optional[str]:
    """Return the id of an integration-verification task, or ``None``.

    Issue #607 step 4 helper. Used as the routing anchor for cross-
    cutting gaps (outcomes with no native task that almost-covered
    them pre-fill). Detection precedence:

    1. Task carrying any of the canonical integration labels
       (``"integration"``, ``"verification"``, ``"type:integration"``)
       — these are stamped by ``integration_verification.py`` when the
       task is generated.
    2. Task whose name begins with ``"Integration verification"`` —
       fallback for older task generators that label-skipped.
    """
    for t in tasks:
        labels = set(t.labels or [])
        if labels & {"integration", "verification", "type:integration"}:
            return t.id
    for t in tasks:
        if (t.name or "").startswith("Integration verification"):
            return t.id
    return None


def _resolve_gap_anchor_ids(
    *,
    tasks: List[Task],
    gap_dicts: List[Dict[str, Any]],
    coverage_after_fill: Optional[Dict[str, List[str]]],
) -> Dict[str, str]:
    """Map each gap stub id to its routed anchor task id.

    Issue #607 step 4 helper. Encapsulates the routing precedence so
    both the criterion-rollup path and the signal-enrichment path can
    address the same anchor.

    Returns
    -------
    Dict[str, str]
        ``{stub_id: anchor_task_id}`` for every gap_dict (by index).
        Empty when ``gap_dicts`` is empty or no anchor can be found.
        ``stub_id`` is ``f"{STUB_TASK_ID_PREFIX}{idx}"`` matching
        ``coverage_after_fill`` convention.
    """
    if not gap_dicts:
        return {}

    integration_anchor_id = _find_integration_verification_task_id(tasks)
    fallback_anchor_id = tasks[0].id if tasks else None
    task_by_id: Dict[str, Task] = {t.id: t for t in tasks}

    stub_for_idx: Dict[str, int] = {
        f"{STUB_TASK_ID_PREFIX}{idx}": idx for idx in range(len(gap_dicts))
    }
    gap_to_outcomes: Dict[int, List[str]] = {}
    if coverage_after_fill:
        for outcome_id, covering_task_ids in coverage_after_fill.items():
            for tid in covering_task_ids:
                idx = stub_for_idx.get(tid)
                if idx is not None:
                    gap_to_outcomes.setdefault(idx, []).append(outcome_id)

    stub_to_anchor: Dict[str, str] = {}
    for idx in range(len(gap_dicts)):
        anchor_id: Optional[str] = None
        if coverage_after_fill:
            for outcome_id in gap_to_outcomes.get(idx, []):
                for tid in coverage_after_fill.get(outcome_id, []):
                    if tid in task_by_id:
                        anchor_id = tid
                        break
                if anchor_id:
                    break
        if not anchor_id:
            anchor_id = integration_anchor_id
        if not anchor_id:
            anchor_id = fallback_anchor_id
        if anchor_id:
            stub_to_anchor[f"{STUB_TASK_ID_PREFIX}{idx}"] = anchor_id

    return stub_to_anchor


def _translate_stub_ids_to_anchor_ids(
    mapping: Optional[Dict[str, List[str]]],
    stub_to_anchor: Dict[str, str],
) -> Optional[Dict[str, List[str]]]:
    """Rewrite stub IDs in a coverage mapping to their routed anchor IDs.

    Issue #607 step 4. Mirrors :func:`_translate_stub_ids_to_real_ids`
    (which existed for the pre-step-4 model where stubs became real
    ``gap_fill_<uuid>`` tasks). Under step 4 stubs never materialize;
    instead each stub's outcome rolls up onto an anchor task. To keep
    :func:`_enrich_acceptance_criteria_with_signals` working for the
    cross-cutting case (mapping entries with only a stub id), we
    rewrite stub ids to the routed anchor id so the
    ``success_signal`` text follows the criterion to the same task.

    Duplicate anchor ids that arise from multiple stubs routing to one
    task are deduplicated per outcome — the enricher itself is
    idempotent, but reducing duplication here keeps the mapping shape
    clean for downstream consumers.
    """
    if mapping is None:
        return None
    if not stub_to_anchor:
        return mapping

    translated: Dict[str, List[str]] = {}
    for outcome_id, task_ids in mapping.items():
        rewritten: List[str] = []
        seen: Set[str] = set()
        for tid in task_ids:
            new_tid = stub_to_anchor.get(tid, tid)
            if new_tid in seen:
                continue
            rewritten.append(new_tid)
            seen.add(new_tid)
        translated[outcome_id] = rewritten
    return translated


def _route_gap_fill_to_criteria(
    *,
    tasks: List[Task],
    gap_dicts: List[Dict[str, Any]],
    coverage_before_fill: Dict[str, List[str]],
    coverage_after_fill: Optional[Dict[str, List[str]]],
) -> Tuple[List[Task], List[Task], Dict[int, str]]:
    """Route gap-fill outcomes to existing tasks or synthesized impl tasks.

    Issue #607 step 4 — replaces the previous behavior of materializing
    one ``gap_fill_<uuid>`` :class:`Task` per uncovered outcome (which
    was atomization mechanism #1 from #607). The intent-fidelity check
    in :func:`apply_outcome_coverage` still runs and its score /
    coverage maps are unchanged; only the OUTPUT FORM is different —
    gaps become criteria on existing tasks.

    Routing precedence per gap (by ``gap_dicts`` index, which matches
    ``_synth_for_coverage_<idx>`` in ``coverage_after_fill``):

    1. Look at the outcomes this gap stub covers in
       ``coverage_after_fill``. For each such outcome,
       ``coverage_after_fill[outcome]`` may also list NATIVE task IDs
       alongside the stub — that means the post-fill LLM judged those
       native tasks to co-cover the outcome with the new gap-fill
       task. Pick the first such native co-anchor — that's the
       natural home for the gap's criterion.
    2. If no native co-anchor (the post-fill LLM thinks only the gap
       task covers this outcome — i.e., truly cross-cutting), find an
       integration-verification task. Cross-cutting behaviors
       (end-to-end flows, performance, accessibility) belong on the
       project's verification surface, not on a single feature
       implementation.
    3. Else, fall back to the first task in the input list — degraded
       but functional. Only reached when the project has neither a
       native co-anchor nor an integration-verification task.

    Note that ``coverage_before_fill`` is accepted in the signature
    for parity with the OutcomeCoverageResult contract and future
    routing heuristics (e.g., picking the anchor with highest pre-fill
    coverage strength), but it is intentionally NOT consulted in this
    precedence: outcomes that produce a gap have empty
    ``coverage_before_fill`` entries by definition (that is what
    ``find_gaps`` means), so anchor selection has to read from
    post-fill.

    Parameters
    ----------
    tasks : list of Task
        Native task list (post-decomposition, pre-rollup). Not mutated;
        tasks that gain criteria are returned as new ``Task`` copies.
    gap_dicts : list of dict
        :attr:`OutcomeCoverageResult.synthesized_tasks` — the
        gap-fill LLM's output dicts.
    coverage_before_fill : dict
        ``{outcome_id: [task_id, ...]}`` from the pre-fill coverage
        pass. Drives anchor selection in precedence step 1.
    coverage_after_fill : dict or None
        ``{outcome_id: [task_id, ...]}`` from the post-fill coverage
        pass. Includes stub IDs that key which gap covers which
        outcome. ``None`` is treated as "no coverage attribution"; the
        rollup still runs and falls through to anchor steps 2-3.

    Returns
    -------
    tuple of (list of Task, list of Task, dict)
        ``(augmented_tasks, synthesized_tasks, stub_idx_to_synth_id)``:

        * ``augmented_tasks`` — the input tasks (anchor tasks replaced
          with copies whose ``completion_criteria`` gained gap-fill
          criterion strings) followed by any synthesized impl tasks.
          Idempotent on the :data:`OUTCOME_GAP_CRITERION_PREFIX` stamp.
        * ``synthesized_tasks`` — implementation tasks materialized for
          core gaps whose resolved anchor was a non-implementation task
          (#683 Cause 2); empty when no synthesis fired.
        * ``stub_idx_to_synth_id`` — ``{gap_index: synth_task_id}`` so
          the caller can route the gap's outcome coverage (and thus the
          signal + #680 gotcha enrichment) onto the synthesized task.
    """
    if not gap_dicts:
        return tasks, [], {}

    tasks_by_id: Dict[str, Task] = {t.id: t for t in tasks}
    integration_id = _find_integration_verification_task_id(tasks)

    # Shared anchor-resolution logic so the signal-enrichment path
    # routes outcomes the same way.
    stub_to_anchor = _resolve_gap_anchor_ids(
        tasks=tasks,
        gap_dicts=gap_dicts,
        coverage_after_fill=coverage_after_fill,
    )

    # #683 Cause 2: per gap, decide between rolling the criterion onto an
    # existing anchor (the #607-step-4 behavior) and SYNTHESIZING an
    # implementation task. A gotcha (#680) is a failure mode in running
    # code, so the outcome's work must live on an implementation task. We
    # synthesize ONLY when the resolved anchor is a non-implementation
    # (design/testing) task — the snake-run failure where collision /
    # game-over / restart rolled onto "Design Game Core Mechanics" and
    # never got built. When the anchor is already an implementation task
    # (or the integration-verification task for cross-cutting outcomes),
    # we keep the rollup — that is the anti-atomization guard: no new task
    # is created when an existing one can host the criterion.
    criteria_per_task: Dict[str, List[str]] = {}
    synthesized: List[Task] = []
    stub_idx_to_synth_id: Dict[int, str] = {}
    n_over_cap = 0
    for idx, gap in enumerate(gap_dicts):
        anchor_id = stub_to_anchor.get(f"{STUB_TASK_ID_PREFIX}{idx}")
        anchor = tasks_by_id.get(anchor_id) if anchor_id else None

        anchor_is_non_impl = (
            anchor is not None
            and anchor_id != integration_id
            and get_task_type(anchor) != TASK_TYPE_IMPLEMENTATION
        )

        if anchor_is_non_impl:
            # Core gap with no implementation home → synthesize one impl
            # task (capped to bound atomization). Over the cap, fall back
            # to the rollup on the resolved (design) anchor and log it.
            if len(synthesized) < GAP_SYNTH_CAP:
                synth = _build_impl_task_from_gap(gap)
                if synth is not None:
                    synthesized.append(synth)
                    stub_idx_to_synth_id[idx] = synth.id
                    continue
            n_over_cap += 1

        if anchor_id:
            criteria_per_task.setdefault(anchor_id, []).append(
                _gap_dict_to_criterion(gap)
            )

    # Rebuild the task list, replacing anchor tasks with copies that
    # carry the new criteria. Idempotent on the criterion-prefix stamp.
    n_tasks_enriched = 0
    n_criteria_added = 0
    enriched: List[Task] = []
    for task in tasks:
        gained = criteria_per_task.get(task.id)
        if not gained:
            enriched.append(task)
            continue
        existing_criteria: List[str] = list(task.completion_criteria or [])
        existing_set: Set[str] = set(existing_criteria)
        new_criteria: List[str] = list(existing_criteria)
        for criterion in gained:
            if criterion in existing_set:
                continue
            new_criteria.append(criterion)
            existing_set.add(criterion)
        if len(new_criteria) == len(existing_criteria):
            enriched.append(task)
            continue
        n_tasks_enriched += 1
        n_criteria_added += len(new_criteria) - len(existing_criteria)
        enriched.append(dataclass_replace(task, completion_criteria=new_criteria))

    if n_tasks_enriched > 0:
        logger.info(
            "Gap-fill rollup (#607 step 4): %d task(s) gained %d "
            "outcome-coverage criterion(s) from %d gap(s)",
            n_tasks_enriched,
            n_criteria_added,
            len(gap_dicts),
        )
    if synthesized:
        logger.info(
            "Gap-fill synthesis (#683 Cause 2): materialized %d "
            "implementation task(s) for core gaps with no implementation "
            "home (cap=%d)",
            len(synthesized),
            GAP_SYNTH_CAP,
        )
    if n_over_cap:
        logger.warning(
            "Gap-fill synthesis (#683 Cause 2): %d core gap(s) exceeded "
            "the synthesis cap of %d and fell back to the criteria rollup",
            n_over_cap,
            GAP_SYNTH_CAP,
        )

    return enriched + synthesized, synthesized, stub_idx_to_synth_id


def _enrich_acceptance_criteria_with_signals(
    *,
    tasks: List[Task],
    outcomes: List[UserOutcome],
    mapping: Optional[Dict[str, List[str]]],
) -> List[Task]:
    """Append ``success_signal`` text to acceptance_criteria of mapped tasks.

    Issue #523 Slice A (static layer).  Takes the outcome→task mapping
    that :class:`OutcomeCoverageResult` already produces and projects
    each in-scope outcome's ``success_signal`` into the
    ``acceptance_criteria`` of every task the mapping says covers it.
    The existing :class:`~src.ai.validation.work_analyzer.WorkAnalyzer`
    LLM validator reads ``acceptance_criteria`` at task-completion time,
    so this gives that validator the user-observable signal text to
    judge against, without changing WorkAnalyzer itself.

    Parameters
    ----------
    tasks
        Current task list (post-gap-fill in the caller).  Not mutated.
        Tasks whose ``id`` does not appear in any mapping entry pass
        through unchanged.
    outcomes
        Extracted user outcomes.  Out-of-scope outcomes are skipped —
        they were retained for audit-trail purposes but must not gate
        completion.
    mapping
        ``Dict[outcome_id, List[task_id]]`` produced by
        :class:`OutcomeCoverageResult.coverage_after_fill` (preferred,
        includes synthesized gap-fill tasks) or
        ``coverage_before_fill``.  ``None`` or empty → tasks returned
        unchanged.

    Returns
    -------
    list of Task
        Same-length task list, in input order, with new ``Task``
        copies (via :func:`dataclasses.replace`) for tasks that
        gained at least one criterion.  Tasks with no signals added
        are passed through by reference.

    Notes
    -----
    Idempotent: criteria are stamped with :data:`SIGNAL_CRITERION_PREFIX`
    and deduplicated against the task's existing criteria, so re-running
    enrichment (or running it on a task that already carries the signal
    from a prior pass) does not produce duplicates.

    Multiple coverage: a task that covers N outcomes gains N criteria,
    one per signal, in mapping-iteration order.  An outcome that maps
    to M tasks contributes its signal to all M.  Order within
    ``acceptance_criteria`` is: existing criteria first, then the
    appended signal criteria.
    """
    if not mapping:
        return tasks

    # Outcomes are scoped — only in-scope outcomes should gate or
    # decorate completion.  Out-of-scope outcomes were retained in
    # the extractor output for audit but must not appear as criteria.
    signal_by_id: Dict[str, str] = {
        o.id: o.success_signal for o in outcomes if o.scope == "in_scope"
    }
    if not signal_by_id:
        return tasks

    # Invert the mapping into ``task_id -> [signals]`` so the task
    # rebuild pass below is a single lookup per task.  Preserve order
    # of mapping-iteration so the resulting criteria list is stable
    # across runs (useful for tests and audit-log diffing).
    signals_per_task: Dict[str, List[str]] = {}
    for outcome_id, task_ids in mapping.items():
        signal = signal_by_id.get(outcome_id)
        if not signal:
            continue
        for tid in task_ids:
            signals_per_task.setdefault(tid, []).append(signal)

    if not signals_per_task:
        return tasks

    enriched: List[Task] = []
    n_tasks_enriched = 0
    n_criteria_added = 0
    for task in tasks:
        signals = signals_per_task.get(task.id)
        if not signals:
            enriched.append(task)
            continue

        existing_criteria: List[str] = list(task.acceptance_criteria or [])
        existing_set: Set[str] = set(existing_criteria)
        new_criteria: List[str] = list(existing_criteria)
        for signal in signals:
            criterion = f"{SIGNAL_CRITERION_PREFIX}{signal}"
            if criterion in existing_set:
                continue
            new_criteria.append(criterion)
            existing_set.add(criterion)

        # No-op if every signal was already present (idempotent
        # re-run).  Avoid the unnecessary Task allocation in that case.
        if len(new_criteria) == len(existing_criteria):
            enriched.append(task)
            continue

        n_tasks_enriched += 1
        n_criteria_added += len(new_criteria) - len(existing_criteria)
        enriched.append(dataclass_replace(task, acceptance_criteria=new_criteria))

    # Single line summarising the pass.  Silent on no-op so steady-state
    # idempotent re-runs don't spam logs; fires whenever the pass
    # actually changes the task list so production debugging of "did the
    # signal land?" reads from logs alone.  Sibling to the existing
    # "Outcome coverage: score=..." line emitted by the wrapping
    # ``apply_outcome_coverage_to_*_graph`` helpers — together they let
    # operators correlate coverage score with enrichment effect for a
    # given run.
    if n_tasks_enriched > 0:
        logger.info(
            "Signal enrichment: %d task(s) gained %d signal criterion(s) "
            "from %d in-scope outcome(s)",
            n_tasks_enriched,
            n_criteria_added,
            len(signal_by_id),
        )

    return enriched


_GOTCHA_PROMPT = """\
You are a senior QA engineer reviewing a project specification before \
the build starts. Your job is to enumerate the KNOWN FAILURE MODES \
("gotchas") for each user-visible outcome: behaviors that a naive but \
spec-compliant implementation gets WRONG.

A gotcha is NOT a restatement of the outcome. It is the specific, \
checkable thing that breaks the user experience even though the literal \
task ("handle direction", "spawn food") was done. Examples for a snake \
game:
- "Pressing the direction opposite to current travel reverses the snake \
into itself and ends the game instantly — it must be ignored instead."
- "Food spawns on a cell already occupied by the snake's body, so it is \
unreachable or auto-eaten."

Specification:
{spec}

User outcomes (enumerate gotchas for the in-scope ones):
{outcomes_block}

Rules:
- Each gotcha must be a concrete, observable, checkable failure — not \
generic advice ("handle errors", "test thoroughly").
- Describe WHAT must be true, never HOW to implement it. Do not name \
frameworks, libraries, patterns, or code structure.
- 0 to 4 gotchas per outcome. Return an empty list for outcomes with no \
non-obvious failure mode. Do not pad.
- Use exactly the outcome ids from the input. Do not invent ids.

Return strict JSON, no preamble, no markdown fences:

{{
  "gotchas": {{
    "<outcome_id>": ["<failure mode 1>", "<failure mode 2>"]
  }}
}}
"""


async def enumerate_gotchas_with_llm(
    *,
    spec: str,
    outcomes: Iterable[UserOutcome],
    llm_client: Any,
    max_tokens: int = 2000,
) -> Dict[str, List[str]]:
    """Enumerate known failure modes per in-scope outcome via one LLM call.

    Issue #680. A single batched call (not per-outcome) keeps the added
    latency negligible — the same cost profile as
    :func:`compute_coverage_with_llm`. Out-of-scope outcomes are
    excluded: they do not gate completion, so spending tokens
    enumerating their gotchas would be wasted.

    Parameters
    ----------
    spec : str
        The original project description, for grounding. May be empty.
    outcomes : iterable of UserOutcome
        Extracted outcomes. Only ``scope == "in_scope"`` are sent.
    llm_client : Any
        Async client exposing ``analyze(...)`` — same client the
        coverage check uses. Mocked in tests.
    max_tokens : int, optional
        Token budget for the call.

    Returns
    -------
    dict
        ``{outcome_id: [gotcha_text, ...]}`` for in-scope outcomes that
        the LLM returned at least one gotcha for. Outcomes with no
        gotchas are omitted. Unknown ids in the response are dropped.

    Raises
    ------
    ValueError
        On malformed JSON or a missing ``gotchas`` key. Callers wrap
        this in graceful degradation (the enumeration step must never
        block project creation).
    """
    in_scope = [o for o in outcomes if o.scope == "in_scope"]
    if not in_scope:
        return {}

    outcomes_block = "\n".join(
        f"- {o.id}: {o.action} (signal: {o.success_signal})" for o in in_scope
    )
    prompt = _GOTCHA_PROMPT.format(
        spec=spec or "(no specification text provided)",
        outcomes_block=outcomes_block,
    )

    from src.utils.structured_llm import safe_structured_call

    try:
        payload = await safe_structured_call(
            llm=llm_client,
            prompt=prompt,
            operation="gotcha_enumeration",
            initial_max_tokens=max_tokens,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Gotcha enumeration: malformed JSON: {exc}") from exc

    raw = payload.get("gotchas")
    if not isinstance(raw, dict):
        raise ValueError("Gotcha enumeration: response missing 'gotchas' object")

    valid_ids = {o.id for o in in_scope}
    gotchas: Dict[str, List[str]] = {}
    for outcome_id, items in raw.items():
        if outcome_id not in valid_ids:
            continue
        if not isinstance(items, list):
            continue
        cleaned = [g.strip() for g in items if isinstance(g, str) and g.strip()]
        if cleaned:
            gotchas[outcome_id] = cleaned
    return gotchas


def _resolve_implementation_targets(
    *,
    outcome_id: str,
    mapped_task_ids: List[str],
    tasks_by_id: Dict[str, Task],
    impl_dependents: Dict[str, List[str]],
) -> List[str]:
    """Resolve an outcome's covering tasks to implementation tasks (#680).

    A gotcha is a known failure mode in the *running code*, so it must
    land on the task whose code can break the outcome — an
    implementation task — not on a design/testing task that produces a
    document the skeptic verifies separately. The LLM coverage mapping
    does not respect this distinction and routes outcomes onto broad
    design tasks, so we remap here. Three tiers, in order:

    1. Keep the mapped tasks that are themselves implementation tasks.
       This is the primary path and needs no dependency information.
    2. If none are, walk each mapped (non-implementation) task's
       dependents and take the implementation tasks that depend on it —
       the design→implementation edge is "who builds what this design
       describes." This tier only fires when ``task.dependencies`` is
       already populated at call time. NOTE: in the feature-based
       decomposition path it is NOT — dependencies are wired downstream
       in ``nlp_tools`` after ``parse_prd_to_tasks`` returns, and
       ``_infer_smart_dependencies`` deliberately strips design→impl
       edges (GH-129), so this tier is effectively a no-op there and
       such outcomes fall to tier 3. It is useful for callers that pass
       a pre-wired graph (e.g. contract-first / subtask graphs with
       explicit dependencies).
    3. If still none, the outcome has no implementation home; the caller
       logs it as a decomposition gap (tracked in #683) rather than
       dropping it silently.

    Parameters
    ----------
    outcome_id
        The outcome being placed (for the caller's orphan logging).
    mapped_task_ids
        Task ids the coverage mapping assigned to this outcome.
    tasks_by_id
        Lookup of every task in the graph by id.
    impl_dependents
        Precomputed ``{task_id: [implementation_task_id, ...]}`` of the
        implementation tasks that directly depend on each task.

    Returns
    -------
    list of str
        Implementation task ids to stamp. Empty when the outcome is
        orphaned (tier 3) — the caller decides what to do.
    """
    # Tier 1: mapped tasks that are themselves implementation tasks.
    impl_targets = [
        tid
        for tid in mapped_task_ids
        if tid in tasks_by_id
        and get_task_type(tasks_by_id[tid]) == TASK_TYPE_IMPLEMENTATION
    ]
    if impl_targets:
        return impl_targets

    # Tier 2: walk the design→implementation dependency edge.
    fallback: List[str] = []
    seen: Set[str] = set()
    for tid in mapped_task_ids:
        for dependent_id in impl_dependents.get(tid, []):
            if dependent_id not in seen:
                seen.add(dependent_id)
                fallback.append(dependent_id)
    if fallback:
        logger.info(
            "Gotcha placement (#680): outcome %r mapped only to "
            "non-implementation task(s); routed to %d downstream "
            "implementation task(s) via dependency edge",
            outcome_id,
            len(fallback),
        )
    return fallback


def _enrich_acceptance_criteria_with_gotchas(
    *,
    tasks: List[Task],
    gotchas_by_outcome: Dict[str, List[str]],
    mapping: Optional[Dict[str, List[str]]],
) -> List[Task]:
    """Append enumerated gotchas to acceptance_criteria of impl tasks.

    Issue #680. Each in-scope outcome's enumerated failure modes are
    written into the ``acceptance_criteria`` of the IMPLEMENTATION tasks
    that cover it (resolved via :func:`_resolve_implementation_targets`),
    not wherever the raw LLM coverage mapping happens to point. Because
    ``request_next_task`` delivers ``acceptance_criteria`` to the agent
    (#664) and the self-verify skeptic reads the same field, the gotcha
    reaches both the builder of the code that can break it and the
    verifier.

    Parameters
    ----------
    tasks
        Current task list. Not mutated; tasks that gain criteria are
        returned as new :func:`dataclasses.replace` copies.
    gotchas_by_outcome
        ``{outcome_id: [gotcha_text, ...]}`` from
        :func:`enumerate_gotchas_with_llm`.
    mapping
        ``{outcome_id: [task_id, ...]}`` coverage mapping (already
        stub-id-translated by the caller). ``None`` / empty → tasks
        unchanged.

    Returns
    -------
    list of Task
        Same-length list, input order. Idempotent via
        :data:`GOTCHA_CRITERION_PREFIX` dedup.
    """
    if not mapping or not gotchas_by_outcome:
        return tasks

    tasks_by_id: Dict[str, Task] = {t.id: t for t in tasks}

    # Precompute implementation dependents: for each task id, the
    # implementation tasks that directly depend on it. Used for the
    # tier-2 design→implementation fallback in target resolution.
    impl_dependents: Dict[str, List[str]] = {}
    for t in tasks:
        if get_task_type(t) != TASK_TYPE_IMPLEMENTATION:
            continue
        for dep_id in getattr(t, "dependencies", []) or []:
            impl_dependents.setdefault(dep_id, []).append(t.id)

    # Invert into task_id -> [gotcha criteria], preserving order so the
    # resulting list is stable across runs (tests, audit diffs). Each
    # outcome's mapped tasks are first remapped to implementation tasks;
    # outcomes with no implementation home are logged, never dropped.
    gotchas_per_task: Dict[str, List[str]] = {}
    orphaned_outcomes: List[str] = []
    for outcome_id, task_ids in mapping.items():
        gotchas = gotchas_by_outcome.get(outcome_id)
        if not gotchas:
            continue
        impl_targets = _resolve_implementation_targets(
            outcome_id=outcome_id,
            mapped_task_ids=task_ids,
            tasks_by_id=tasks_by_id,
            impl_dependents=impl_dependents,
        )
        if not impl_targets:
            orphaned_outcomes.append(outcome_id)
            continue
        for tid in impl_targets:
            bucket = gotchas_per_task.setdefault(tid, [])
            for g in gotchas:
                bucket.append(f"{GOTCHA_CRITERION_PREFIX}{g}")

    if orphaned_outcomes:
        # Decomposition gap: an in-scope outcome with enumerated failure
        # modes has no implementation task to enforce them. Surface it
        # loudly — a silently-dropped gotcha is the opposite of the
        # transparency/correctness the feature exists for. Root cause and
        # the proposed fix (a required outcome with no implementation task)
        # are tracked in #683.
        logger.warning(
            "Gotcha placement (#680): %d outcome(s) had gotchas but NO "
            "implementation task to host them (decomposition gap, "
            "criteria dropped; see #683): %s",
            len(orphaned_outcomes),
            ", ".join(orphaned_outcomes),
        )

    if not gotchas_per_task:
        return tasks

    enriched: List[Task] = []
    n_tasks_enriched = 0
    n_criteria_added = 0
    for task in tasks:
        new_for_task = gotchas_per_task.get(task.id)
        if not new_for_task:
            enriched.append(task)
            continue

        existing_criteria: List[str] = list(task.acceptance_criteria or [])
        existing_set: Set[str] = set(existing_criteria)
        new_criteria: List[str] = list(existing_criteria)
        for criterion in new_for_task:
            if criterion in existing_set:
                continue
            new_criteria.append(criterion)
            existing_set.add(criterion)

        if len(new_criteria) == len(existing_criteria):
            enriched.append(task)
            continue

        n_tasks_enriched += 1
        n_criteria_added += len(new_criteria) - len(existing_criteria)
        enriched.append(dataclass_replace(task, acceptance_criteria=new_criteria))

    if n_tasks_enriched > 0:
        logger.info(
            "Gotcha enrichment (#680): %d task(s) gained %d gotcha "
            "criterion(s) from %d outcome(s)",
            n_tasks_enriched,
            n_criteria_added,
            len(gotchas_by_outcome),
        )

    return enriched


async def _apply_gotcha_enrichment(
    *,
    spec: str,
    outcomes: List[UserOutcome],
    tasks: List[Task],
    mapping: Optional[Dict[str, List[str]]],
    llm_client: Any,
) -> List[Task]:
    """Run the #680 enumeration + enrichment, degrading gracefully.

    Shared by the feature-based and contract-first graph paths so the
    LLM call and error handling live in one place. Only reached from
    inside the outcome-coverage augmenters, which already no-op when the
    coverage pipeline is off or no outcomes were extracted — so this is
    permanently on wherever coverage runs and needs no flag of its own.
    Returns ``tasks`` unchanged on an empty mapping or any LLM/parse
    error — the gotcha step must never block project creation.
    """
    if not mapping:
        return tasks
    try:
        gotchas_by_outcome = await enumerate_gotchas_with_llm(
            spec=spec,
            outcomes=outcomes,
            llm_client=llm_client,
        )
    except Exception as exc:  # noqa: BLE001 — broad by design (graceful degrade)
        logger.warning("Gotcha enumeration failed; criteria unchanged: %s", exc)
        return tasks
    return _enrich_acceptance_criteria_with_gotchas(
        tasks=tasks,
        gotchas_by_outcome=gotchas_by_outcome,
        mapping=mapping,
    )


async def apply_outcome_coverage_to_feature_graph(
    *,
    prd_analysis: "PRDAnalysis",
    tasks: List[Task],
    llm_client: Any,
) -> AugmentationResult:
    """Run the outcome-coverage check on a feature-based task graph.

    Issue #449 + #456.  Returns the canonical
    :class:`AugmentationResult` shape so the chain orchestrator can
    consume it directly without a parser-side wrapper.

    Returns an :class:`AugmentationResult` with empty telemetry when:

    - The flag is off (``MARCUS_OUTCOME_COVERAGE`` unset/false)
    - No outcomes were extracted (extraction failed earlier or no
      in-scope outcomes in the PRD)
    - The coverage pipeline raised (LLM error)

    Otherwise carries the augmented task list (originals + any
    synthesized gap-fill tasks), ``synthesized_ids`` of the new tasks,
    and ``telemetry`` with the canonical event-payload keys.

    Failures degrade gracefully — the input task list is returned
    unchanged so a project is never blocked by an outcome-coverage
    failure.
    """
    if not is_outcome_coverage_enabled():
        return AugmentationResult(augmented_tasks=list(tasks))
    if not prd_analysis.user_outcomes:
        return AugmentationResult(augmented_tasks=list(tasks))

    try:
        result = await apply_outcome_coverage(
            spec=prd_analysis.original_description or "",
            outcomes=prd_analysis.user_outcomes,
            tasks=tasks,
            llm_client=llm_client,
            contract_artifacts=None,
        )
    except Exception as exc:
        # Catch broadly: timeouts, API errors, parse errors all
        # downgrade to a logged warning.  Coverage failures must
        # never block project creation; the decomposer's pre-#449
        # behavior is the always-available fallback.
        logger.warning("Outcome coverage failed; task graph unchanged: %s", exc)
        return AugmentationResult(augmented_tasks=list(tasks))

    # Issue #607 step 4: gap-fill output rolls up onto existing tasks'
    # completion_criteria instead of synthesizing one new
    # ``gap_fill_<uuid>`` task per uncovered outcome (the previous
    # atomization mechanism #1). The intent-fidelity check above still
    # ran identically; only the OUTPUT FORM differs — every gap now
    # becomes one criterion text on an anchor task picked via the
    # routing precedence in ``_route_gap_fill_to_criteria``.
    #
    # Codex P1 on PR #611: when the native graph is empty (AI
    # decomposer failure path), the rollup has no anchor and gaps
    # would be silently dropped. Materialize the gaps as rescue tasks
    # instead — pre-step-4 safety net for the empty-graph case, scoped
    # narrowly so the atomization mechanism stays dead for the normal
    # path.
    synthesized_ids: List[str] = []
    # ``gap_stub_to_synth`` maps a gap's stub index to the real id of the
    # task it was materialized into, so the downstream signal + #680
    # gotcha enrichment land on that task. Both the empty-graph rescue
    # path and the #683 Cause-2 synthesis path populate it.
    gap_stub_to_synth: Dict[int, str] = {}
    if not tasks:
        rescued = _materialize_gap_dicts_as_rescue_tasks(result.synthesized_tasks)
        augmented = rescued
        synthesized_ids = [t.id for t in rescued]
        gap_stub_to_synth = {idx: rid for idx, rid in enumerate(synthesized_ids)}
    else:
        augmented, gap_synth_tasks, gap_stub_to_synth = _route_gap_fill_to_criteria(
            tasks=list(tasks),
            gap_dicts=result.synthesized_tasks,
            coverage_before_fill=result.coverage_before_fill,
            coverage_after_fill=result.coverage_after_fill,
        )
        synthesized_ids = [t.id for t in gap_synth_tasks]

    # Issue #523 Slice A: project each in-scope outcome's
    # ``success_signal`` into the ``acceptance_criteria`` of every task
    # the mapping says covers it. Stub IDs in the coverage mapping
    # (cross-cutting outcomes that only the gap-fill task covers) are
    # rewritten to the routed anchor id so the signal text follows the
    # criterion to the same task — see
    # ``_translate_stub_ids_to_anchor_ids``. Gaps materialized into tasks
    # (empty-graph rescue OR #683 Cause-2 synthesis) map their stub id to
    # the materialized task's real id so enrichment lands there.
    stub_to_anchor = _resolve_gap_anchor_ids(
        tasks=augmented,
        gap_dicts=result.synthesized_tasks,
        coverage_after_fill=result.coverage_after_fill,
    )
    for idx, real_id in gap_stub_to_synth.items():
        stub_to_anchor[f"{STUB_TASK_ID_PREFIX}{idx}"] = real_id
    coverage_mapping = _translate_stub_ids_to_anchor_ids(
        result.coverage_after_fill or result.coverage_before_fill,
        stub_to_anchor,
    )
    augmented = _enrich_acceptance_criteria_with_signals(
        tasks=augmented,
        outcomes=prd_analysis.user_outcomes,
        mapping=coverage_mapping,
    )

    # Issue #680: enumerate known failure modes per outcome and write
    # them into the acceptance_criteria of every covering task. Reuses
    # the same translated coverage mapping as the signal pass so a
    # gotcha lands on the same task the signal did. Delivered to the
    # agent by #664 and read by the self-verify skeptic.
    augmented = await _apply_gotcha_enrichment(
        spec=prd_analysis.original_description or "",
        outcomes=prd_analysis.user_outcomes,
        tasks=augmented,
        mapping=coverage_mapping,
        llm_client=llm_client,
    )

    logger.info(
        "Outcome coverage: score=%.2f, %d outcome(s), %d gap(s) "
        "rolled up onto existing tasks' completion_criteria "
        "(#607 step 4)",
        result.intent_fidelity_score,
        len(prd_analysis.user_outcomes),
        len(result.gaps),
    )

    return AugmentationResult(
        augmented_tasks=augmented,
        synthesized_ids=synthesized_ids,
        telemetry=_coverage_to_telemetry(result),
    )


async def apply_outcome_coverage_to_contract_graph(
    *,
    prd_analysis: "PRDAnalysis",
    tasks: List[Task],
    contract_artifacts: Dict[str, Optional[Dict[str, Any]]],
    llm_client: Any,
) -> AugmentationResult:
    """Run outcome-coverage against a contract-first task graph.

    Issue #449 + #456.  Returns :class:`AugmentationResult` so the
    chain orchestrator can consume it directly.

    Distinct from :func:`apply_outcome_coverage_to_feature_graph`
    because contract-first tasks need ``responsibility`` set from the
    gap-fill ``responsibility`` field (which the contract-aware
    ``fill_gaps`` prompt asks the LLM for).

    Returns an empty-telemetry :class:`AugmentationResult` (input
    tasks unchanged) on flag off / no outcomes / LLM error — same
    graceful-degradation contract as the feature-based path.
    """
    if not is_outcome_coverage_enabled():
        return AugmentationResult(augmented_tasks=list(tasks))
    if not prd_analysis.user_outcomes:
        return AugmentationResult(augmented_tasks=list(tasks))

    # Filter to contract artifacts that actually have content;
    # apply_outcome_coverage's prompt-builder expects a dict with
    # populated ``artifacts`` lists, not the wrapper shape that
    # decompose_by_contract receives (which can have None payloads).
    usable_contracts: Dict[str, Any] = {
        domain: payload
        for domain, payload in contract_artifacts.items()
        if payload and payload.get("artifacts")
    }

    try:
        result = await apply_outcome_coverage(
            spec=prd_analysis.original_description or "",
            outcomes=prd_analysis.user_outcomes,
            tasks=tasks,
            llm_client=llm_client,
            contract_artifacts=usable_contracts or None,
        )
    except Exception as exc:
        logger.warning(
            "Outcome coverage (contract-first) failed; task graph unchanged: %s",
            exc,
        )
        return AugmentationResult(augmented_tasks=list(tasks))

    # Issue #607 step 4: gap-fill output rolls up onto existing tasks'
    # completion_criteria instead of synthesizing new contract gap_fill
    # tasks. The contract-first ``responsibility`` field that the
    # pre-step-4 ``_build_contract_gap_fill_task`` path projected onto
    # ``Task.responsibility`` is now embedded in the criterion text via
    # ``_gap_dict_to_criterion`` (Codex P2 on PR #611) — the only
    # post-step-4 channel that surfaces contract ownership for gap-
    # driven contract work.
    #
    # Codex P1 on PR #611: when the native contract graph is empty,
    # the rollup has no anchor. Materialize gaps as rescue tasks
    # instead — pre-step-4 safety net scoped narrowly.
    synthesized_ids: List[str] = []
    gap_stub_to_synth: Dict[int, str] = {}
    if not tasks:
        rescued = _materialize_gap_dicts_as_rescue_tasks(result.synthesized_tasks)
        augmented = rescued
        synthesized_ids = [t.id for t in rescued]
        gap_stub_to_synth = {idx: rid for idx, rid in enumerate(synthesized_ids)}
    else:
        augmented, gap_synth_tasks, gap_stub_to_synth = _route_gap_fill_to_criteria(
            tasks=list(tasks),
            gap_dicts=result.synthesized_tasks,
            coverage_before_fill=result.coverage_before_fill,
            coverage_after_fill=result.coverage_after_fill,
        )
        synthesized_ids = [t.id for t in gap_synth_tasks]

    # Issue #523 Slice A: mirror the feature-based path — inject each
    # in-scope outcome's ``success_signal`` into the
    # ``acceptance_criteria`` of every covering task so the existing
    # ``WorkAnalyzer`` static gate validates against the user's stated
    # signal at task completion. Post-#607-step-4: stub IDs in the
    # mapping are rewritten to the routed anchor id so the signal
    # follows the criterion to the same task. Gaps materialized into
    # tasks (empty-graph rescue OR #683 Cause-2 synthesis) map their
    # stub id to the materialized task's real id.
    stub_to_anchor = _resolve_gap_anchor_ids(
        tasks=augmented,
        gap_dicts=result.synthesized_tasks,
        coverage_after_fill=result.coverage_after_fill,
    )
    for idx, real_id in gap_stub_to_synth.items():
        stub_to_anchor[f"{STUB_TASK_ID_PREFIX}{idx}"] = real_id
    coverage_mapping = _translate_stub_ids_to_anchor_ids(
        result.coverage_after_fill or result.coverage_before_fill,
        stub_to_anchor,
    )
    augmented = _enrich_acceptance_criteria_with_signals(
        tasks=augmented,
        outcomes=prd_analysis.user_outcomes,
        mapping=coverage_mapping,
    )

    # Issue #680: gotcha enumeration, mirrored from the feature-based
    # path. Same translated coverage mapping so failure modes land on
    # the covering task and ride the #664 delivery pipe to the agent
    # and the self-verify skeptic.
    augmented = await _apply_gotcha_enrichment(
        spec=prd_analysis.original_description or "",
        outcomes=prd_analysis.user_outcomes,
        tasks=augmented,
        mapping=coverage_mapping,
        llm_client=llm_client,
    )

    logger.info(
        "Outcome coverage (contract-first): score=%.2f, %d outcome(s), "
        "%d gap(s) rolled up onto existing tasks' completion_criteria "
        "(#607 step 4)",
        result.intent_fidelity_score,
        len(prd_analysis.user_outcomes),
        len(result.gaps),
    )

    return AugmentationResult(
        augmented_tasks=augmented,
        synthesized_ids=synthesized_ids,
        telemetry=_coverage_to_telemetry(result),
    )
