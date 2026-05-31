"""
Canonical taxonomy of LLM operations used by Marcus's planner.

Every planner-side LLM call should be tagged with one of the keys in
:data:`OPERATIONS`. The mapping flows two directions:

- **Producer**: call sites pass the key as ``operation=`` to
  :meth:`LLMAbstraction.analyze` (or directly through provider hooks).
  The recorder stamps it onto ``token_events.operation``.
- **Consumer**: the Cato dashboard fetches this catalog via the
  ``/api/cost/operations`` endpoint and renders the human label and
  description next to each operation in the cost breakdown — so users
  can see *what* the planner spent tokens on, not just *that* it did.

Categories
----------
``decomposition``
    Setup-time LLM work that runs during ``create_project``: parsing the
    PRD, extracting outcomes, generating contracts, etc. These are the
    big cache-creation calls.
``runtime``
    Per-task LLM work that runs while agents are executing: blocker
    analysis, dependency inference, task enrichment. Smaller, but more
    of them.
``monitoring``
    LLM work driven by the project monitor loop: health analysis,
    proactive risk detection.
``other``
    Catch-all for ad-hoc analyzers that don't fit the above.

Adding new operations
---------------------
1. Add the key to :data:`OPERATIONS` with a ``label``, ``description``,
   and ``category``.
2. At the call site, pass ``operation="<your_key>"`` to
   :meth:`LLMAbstraction.analyze` (or wrap the call in
   :meth:`CostRecorder.operation_context`).
3. The dashboard will pick it up automatically on next reload.

If a call lands without an operation key, the recorder falls back to
whatever default the provider stamps (usually ``'analyze'``). That still
shows up in the breakdown — just under the generic bucket.
"""

from __future__ import annotations

from typing import Dict, TypedDict


class Operation(TypedDict):
    """Schema for one entry in :data:`OPERATIONS`."""

    label: str
    description: str
    category: str


OPERATIONS: Dict[str, Operation] = {
    # -- decomposition (create_project setup) ------------------------------
    "decompose_prd": {
        "label": "Decompose PRD",
        "description": (
            "Parses the project requirements document into structured tasks. "
            "Largest single LLM call per project; benefits heavily from "
            "prompt caching on retries."
        ),
        "category": "decomposition",
    },
    "extract_outcomes": {
        "label": "Extract outcomes",
        "description": (
            "Pulls the user-visible outcomes from the PRD (login works, "
            "checkout completes, etc.) so the task graph can be checked "
            "against them downstream."
        ),
        "category": "decomposition",
    },
    "outcome_coverage_check": {
        "label": "Outcome coverage check",
        "description": (
            "Verifies that the decomposed task graph covers every extracted "
            "outcome. Triggers regeneration of missing tasks when gaps "
            "appear."
        ),
        "category": "decomposition",
    },
    "core_feature_mapping": {
        "label": "Core feature mapping",
        "description": (
            "Maps AI-generated functional requirements to in-scope user "
            "outcomes so the capacity filter keeps core features and trims "
            "only scope-creep (#683 Cause 1)."
        ),
        "category": "decomposition",
    },
    "generate_contracts": {
        "label": "Generate contracts",
        "description": (
            "Synthesizes interface contracts (API schemas, data shapes) "
            "that pre-fork foundation tasks must satisfy, so independent "
            "agents can fork without coordination drift."
        ),
        "category": "decomposition",
    },
    "generate_task_detail": {
        "label": "Generate task detail",
        "description": (
            "Expands a high-level task into per-task acceptance criteria, "
            "scope notes, and artifacts agents will see on the board."
        ),
        "category": "decomposition",
    },
    "decompose_task": {
        "label": "Decompose task",
        "description": (
            "Breaks a single high-level task into ordered subtasks with "
            "explicit hard/soft dependencies, file artifacts, and "
            "provides/requires contracts. Driven by the Marcus "
            "coordinator on user request."
        ),
        "category": "decomposition",
    },
    "analyze_task_redundancy": {
        "label": "Analyze task redundancy",
        "description": (
            "Detects duplicate or overlapping tasks in the decomposed "
            "graph. Used during contract-first decomposition to clean up "
            "the task list before pre-fork synthesis."
        ),
        "category": "decomposition",
    },
    "spec_coverage_check": {
        "label": "Spec coverage check",
        "description": (
            "Cross-checks generated task scope against the provided spec "
            "or contract; flags gaps before tasks are pushed to the board."
        ),
        "category": "decomposition",
    },
    "extract_spec_features": {
        "label": "Extract spec features",
        "description": (
            "First-pass feature extraction from a raw spec, used by "
            "``spec_coverage`` to seed the coverage matrix before the "
            "decomposer runs."
        ),
        "category": "decomposition",
    },
    "spec_coverage_confirm": {
        "label": "Spec coverage confirm",
        "description": (
            "Semantic coverage judgment (issue #666): given the extracted "
            "spec features and the current task list, the LLM decides which "
            "features no task actually delivers, so only genuine gaps are "
            "synthesized into tasks. Replaces the keyword coverage scan, "
            "which mismatched on shared/absent words."
        ),
        "category": "decomposition",
    },
    "outcome_gap_fill": {
        "label": "Outcome gap fill",
        "description": (
            "Synthesizes replacement or supplementary tasks when "
            "``outcome_coverage_check`` flags uncovered user outcomes. "
            "Honors the active contract schema."
        ),
        "category": "decomposition",
    },
    "filter_outcomes": {
        "label": "Filter outcomes",
        "description": (
            "Filters extracted user outcomes to only those that are "
            "verifiable from the artifacts an agent can produce, so the "
            "coverage check has a meaningful ground truth."
        ),
        "category": "decomposition",
    },
    "discover_domains": {
        "label": "Discover domains",
        "description": (
            "Clusters PRD features into domains (auth, payments, etc.) "
            "so the decomposer can produce domain-scoped task lists and "
            "shared contracts."
        ),
        "category": "decomposition",
    },
    "synthesize_foundation_tasks": {
        "label": "Synthesize foundation tasks",
        "description": (
            "Pre-fork synthesis step: generates the shared foundation "
            "work (design system, base components, shared contracts) "
            "that parallel agents need *before* they can safely fork."
        ),
        "category": "decomposition",
    },
    "generate_design_artifact": {
        "label": "Generate design artifact",
        "description": (
            "Produces a design doc or contract for one domain during "
            "the design phase. Runs concurrently across domains, gated "
            "by a semaphore."
        ),
        "category": "decomposition",
    },
    "generate_design_decisions": {
        "label": "Generate design decisions",
        "description": (
            "Generates the explicit cross-domain decisions log for one "
            "domain (routing, ownership boundaries) — the prompt that "
            "implements GH-320 Option A."
        ),
        "category": "decomposition",
    },
    "generate_project_scaffold": {
        "label": "Generate project scaffold",
        "description": (
            "Produces the initial repo file scaffold (directory layout, "
            "manifests, README skeleton) that agents check out before "
            "implementing tasks."
        ),
        "category": "decomposition",
    },
    "post_analysis": {
        "label": "Post-project analysis",
        "description": (
            "Generic bucket for post-mortem analyzers driven by "
            "``AnalysisAIEngine`` (requirement divergence, decision "
            "impact, failure diagnosis, etc.). Sub-type is recorded in "
            "the analyzer's own logs."
        ),
        "category": "monitoring",
    },
    "post_analysis_requirement_divergence": {
        "label": "Post-analysis: requirement divergence",
        "description": (
            "Post-project analyzer that compares delivered work against "
            "the original requirements to detect drift or missing "
            "scope."
        ),
        "category": "monitoring",
    },
    "post_analysis_decision_impact": {
        "label": "Post-analysis: decision impact",
        "description": (
            "Post-project analyzer that traces a logged decision "
            "through to its downstream effects on tasks and outcomes."
        ),
        "category": "monitoring",
    },
    "post_analysis_instruction_quality": {
        "label": "Post-analysis: instruction quality",
        "description": (
            "Scores the clarity and completeness of generated agent "
            "instructions against actual completion behavior."
        ),
        "category": "monitoring",
    },
    "post_analysis_failure_diagnosis": {
        "label": "Post-analysis: failure diagnosis",
        "description": (
            "Diagnoses the root cause of a stalled or failed task by "
            "cross-referencing agent reports, board state, and prior "
            "decisions."
        ),
        "category": "monitoring",
    },
    "post_analysis_task_redundancy": {
        "label": "Post-analysis: task redundancy",
        "description": (
            "Detects duplicate or overlapping tasks across the whole "
            "decomposed project (post-decomposition audit)."
        ),
        "category": "monitoring",
    },
    "post_analysis_overall_assessment": {
        "label": "Post-analysis: overall assessment",
        "description": (
            "Cross-analyzer rollup that synthesizes a single overall "
            "quality verdict from the other post-analysis signals."
        ),
        "category": "monitoring",
    },
    # -- runtime (per-task agent work) -------------------------------------
    "analyze_blocker": {
        "label": "Analyze blocker",
        "description": (
            "Reads an agent's blocker report, suggests resolutions, and "
            "decides whether to re-route or escalate. Fires once per "
            "``report_blocker`` MCP call."
        ),
        "category": "runtime",
    },
    "infer_dependencies": {
        "label": "Infer dependencies",
        "description": (
            "Semantic dependency inference beyond rule-based detection. "
            "Used when the rule engine returns ambiguous edges."
        ),
        "category": "runtime",
    },
    "enrich_task": {
        "label": "Enrich task",
        "description": (
            "Augments a task with implementation hints, related-artifact "
            "links, and clarifying context before an agent picks it up."
        ),
        "category": "runtime",
    },
    "analyze_task_semantics": {
        "label": "Analyze task semantics",
        "description": (
            "Semantic-level analysis of a single task: intent, risks, "
            "and similar-task matches. Used to refine downstream "
            "dependency edges."
        ),
        "category": "runtime",
    },
    "estimate_effort": {
        "label": "Estimate effort",
        "description": (
            "AI-powered effort/hours estimate for a task, blending "
            "historical performance data with task description."
        ),
        "category": "runtime",
    },
    # -- monitoring --------------------------------------------------------
    "analyze_project_health": {
        "label": "Analyze project health",
        "description": (
            "Periodic health check from the monitor loop: detects stalls, "
            "stuck agents, and risks. Cheaper per-call but runs on a "
            "schedule."
        ),
        "category": "monitoring",
    },
    "validate_work": {
        "label": "Validate agent work",
        "description": (
            "Validates that an agent's submitted artifacts satisfy the "
            "task's acceptance criteria. Runs on ``report_task_progress`` "
            "completion events."
        ),
        "category": "monitoring",
    },
    "validate_task_completeness": {
        "label": "Validate task completeness",
        "description": (
            "Pre-board validation that a generated task is well-formed: "
            "has acceptance criteria, file artifacts, and a coherent "
            "description. Catches malformed LLM output before it reaches "
            "agents."
        ),
        "category": "decomposition",
    },
    # -- other -------------------------------------------------------------
    "analyze": {
        "label": "Generic analyze",
        "description": (
            "Fallback bucket for LLM calls that didn't pass a specific "
            "operation tag. If you see meaningful spend here, the call "
            "site needs an explicit ``operation=`` argument."
        ),
        "category": "other",
    },
}


def get_operation(key: str) -> Operation:
    """Look up an operation by key, falling back to the generic bucket.

    Parameters
    ----------
    key : str
        Operation key from ``token_events.operation``.

    Returns
    -------
    Operation
        Entry with ``label``, ``description``, ``category``. If ``key``
        isn't in the catalog, returns a synthesized entry in the
        ``other`` category so the dashboard can still render it.
    """
    if key in OPERATIONS:
        return OPERATIONS[key]
    return Operation(
        label=key.replace("_", " ").title(),
        description=f"Unregistered operation '{key}'. Add it to OPERATIONS.",
        category="other",
    )


def all_operations() -> Dict[str, Operation]:
    """Return a defensive copy of the full catalog."""
    return dict(OPERATIONS)
