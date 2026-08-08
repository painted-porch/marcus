"""
Natural Language MCP Tools for Marcus (Refactored).

These tools expose Marcus's AI capabilities for:
1. Creating projects from natural language descriptions
2. Adding features to existing projects

This refactored version eliminates code duplication by using base classes and utilities.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from src.ai.advanced.prd.advanced_parser import (  # noqa: E402
    AdvancedPRDParser,
    ProjectConstraints,
)
from src.config.decomposer_config import is_contract_first  # noqa: E402
from src.core.events import EventTypes  # noqa: E402
from src.core.models import Priority, Task, TaskStatus  # noqa: E402
from src.core.resilience import RetryConfig, with_retry  # noqa: E402
from src.detection.board_analyzer import BoardAnalyzer  # noqa: E402
from src.detection.context_detector import ContextDetector, MarcusMode  # noqa: E402
from src.integrations.enhanced_task_classifier import (  # noqa: E402
    EnhancedTaskClassifier,
)

# Import refactored base classes and utilities
from src.integrations.nlp_base import NaturalLanguageTaskCreator  # noqa: E402
from src.integrations.nlp_task_utils import TaskType  # noqa: E402
from src.logging.agent_events import log_agent_event  # noqa: E402
from src.marcus_mcp.coordinator.scheduler import (  # noqa: E402
    calculate_optimal_agents,
)
from src.modes.adaptive.basic_adaptive import BasicAdaptiveMode  # noqa: E402

logger = logging.getLogger(__name__)


def _resolve_project_root(
    options: Optional[Dict[str, Any]],
    project_id: Optional[str],
) -> Optional[str]:
    """
    Resolve the on-disk project root for design artifact generation.

    Returns the caller-supplied ``options["project_root"]`` when set.
    When the caller did not supply one and a stable ``project_id`` is
    available, defaults to ``~/.marcus/projects/<project_id>`` and
    ensures the directory exists on disk so downstream artifact
    writers don't hit ``FileNotFoundError``.

    Parameters
    ----------
    options : Optional[Dict[str, Any]]
        Options dict passed to ``create_project``. May be ``None`` or
        omit the ``project_root`` key.
    project_id : Optional[str]
        Stable project identifier (set by the kanban provider before
        this is called).

    Returns
    -------
    Optional[str]
        Absolute path to the project root, or ``None`` when no default
        can be derived (no ``project_id``) or the default cannot be
        created on disk (read-only home, restricted container). When
        ``None`` is returned for a fixable reason, a warning is logged
        so the deadlock root cause is visible.

    Notes
    -----
    GH-588: the design auto-completion phase (``_run_design_phase``)
    is gated on this being truthy. Before #588, callers that omitted
    ``project_root`` got ``None`` here, which silently skipped design
    auto-completion and deadlocked any project whose feature tasks
    depended on design tasks.

    Scope: this helper is intentionally wired into the design-phase
    gate only. Two other call sites read ``options["project_root"]``
    directly and must remain caller-explicit: (1) the contract-first
    decomposer, which warns loudly when the caller omits a root
    (see ``_build_decomposer_warning``); and (2) the SQLite kanban
    ``auto_setup_project`` call. GH-589 tracks the deeper refactor
    that would eliminate the need to default ``project_root`` here
    at all by splitting board-state design completion from on-disk
    scaffold generation.
    """
    explicit = options.get("project_root") if options else None
    if explicit:
        return str(explicit)
    if not project_id:
        return None
    try:
        default_root = Path.home() / ".marcus" / "projects" / project_id
        default_root.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError) as exc:
        # Degrade gracefully rather than killing ``create_project``.
        # ``OSError`` covers read-only home, NFS quota, restricted
        # container; ``RuntimeError`` covers ``Path.home()`` raising
        # when ``$HOME`` (or the platform equivalent) is unset in
        # containerized/service environments. Returning ``None``
        # preserves the pre-#588 behavior (design auto-completion
        # skipped) but surfaces the cause in the log so the caller
        # can see why hello-world-style projects deadlock on this
        # host. ``default_root`` is referenced only when bound; if
        # ``Path.home()`` itself raised, the log falls back to a
        # path-less message.
        location = locals().get("default_root", "<unresolved home>")
        logger.warning(
            f"[create_project] failed to create default project root "
            f"{location}: {exc}; design auto-completion will be "
            f"skipped for project_id={project_id}"
        )
        return None
    return str(default_root)


def _task_type_breakdown(
    tasks: List[Task],
    classifier: EnhancedTaskClassifier,
) -> Dict[str, int]:
    """
    Compute a task-type histogram from a list of tasks.

    Uses the :class:`EnhancedTaskClassifier` instead of reading a
    non-existent ``task_type`` attribute off the ``Task`` dataclass,
    which was the pre-existing bug that made every breakdown log
    show ``{'unknown': N}`` regardless of the underlying decomposer.

    Parameters
    ----------
    tasks : List[Task]
        Tasks to classify.
    classifier : EnhancedTaskClassifier
        Classifier instance (keyword + label scoring, no LLM calls).

    Returns
    -------
    Dict[str, int]
        Mapping of ``TaskType`` value (e.g. ``"implementation"``) to
        the count of tasks that classified as that type. Tasks that
        the classifier rejects fall into the ``"other"`` bucket —
        ``"unknown"`` never appears in the output.
    """
    breakdown: Dict[str, int] = {}
    for task in tasks:
        task_type = classifier.classify(task).value
        breakdown[task_type] = breakdown.get(task_type, 0) + 1
    return breakdown


def _build_decomposer_warning(
    options: Optional[Dict[str, Any]],
) -> Optional[str]:
    """
    Return a warning string when contract_first is active but project_root absent.

    ``contract_first`` decomposition requires a ``project_root`` path so the
    decomposer can write interface-contract files to disk.  When the strategy is
    ``contract_first`` (by default or explicit config) but ``project_root`` is
    not provided, the system silently falls back to ``feature_based`` and the
    caller receives no structural scaffolding tasks.  This helper detects that
    mismatch so callers can see it in the result dict rather than hunting through
    server logs.

    Parameters
    ----------
    options : Optional[Dict[str, Any]]
        Options dict passed to ``create_project``.  If ``None``, the default
        strategy (``contract_first``) is assumed.

    Returns
    -------
    Optional[str]
        A descriptive warning string when the mismatch is detected; ``None``
        when no action is required (correct config or feature_based chosen).

    Examples
    --------
    >>> _build_decomposer_warning(None)  # default = contract_first, no root
    'contract_first strategy ...'
    >>> _build_decomposer_warning({"project_root": "/home/agent/projects/x"})  # OK
    None
    >>> _build_decomposer_warning({"decomposer": "feature_based"})  # intentional
    None
    """
    from src.config.decomposer_config import is_contract_first

    if not is_contract_first(options):
        return None
    project_root = (options or {}).get("project_root")
    if project_root:
        return None
    return (
        "contract_first strategy requires 'project_root' in options but none was "
        "provided; fell back to feature_based — structural scaffolding tasks will "
        "not be generated. Pass 'project_root' in options to enable full "
        "contract-first decomposition with structural scaffolding."
    )


# ---------------------------------------------------------------------------
# Design autocomplete parallelism helpers (GH-304)
#
# Concurrency cap: ``_DESIGN_LLM_CONCURRENCY`` limits in-flight ``llm.analyze``
# calls across all design tasks in a single ``_generate_design_content()``
# invocation. For a 10-task enterprise project with 5 LLM calls per task (4
# artifacts + 1 decisions), uncapped parallelism would burst up to 50 calls
# simultaneously and risk tripping Anthropic's per-minute rate limit. 10 is a
# safe ceiling for Claude Sonnet at paid-tier rate limits while preserving
# most of the wall-clock speedup.
# ---------------------------------------------------------------------------
_DESIGN_LLM_CONCURRENCY = 10


@with_retry(RetryConfig(max_attempts=3, base_delay=2.0, jitter=True))
async def _bounded_llm_analyze(
    llm: Any,
    prompt: str,
    context: Any,
    semaphore: asyncio.Semaphore,
    *,
    operation: Optional[str] = None,
) -> str:
    """Call ``llm.analyze`` under a concurrency cap with retry + backoff.

    Wraps a single LLM call so that:

    - at most ``semaphore._value`` calls run concurrently (rate-limit guard),
    - transient failures retry up to 3 times with jittered exponential
      backoff (``RetryConfig(max_attempts=3, base_delay=2.0, jitter=True)``).

    If all retries fail the last exception propagates to the caller, which
    aborts the surrounding ``asyncio.gather`` and fails the whole design
    autocomplete phase (hard-fail semantics — see GH-304).

    Parameters
    ----------
    llm : Any
        An LLM client exposing an async ``analyze(prompt, context)`` method.
    prompt : str
        Prompt text passed through unchanged.
    context : Any
        Context object passed through unchanged (typically provides
        ``max_tokens`` and similar knobs).
    semaphore : asyncio.Semaphore
        Concurrency guard. Must be created inside a running event loop so
        it binds to the correct loop (created per-call in
        :func:`_generate_design_content`).
    operation : str, optional
        Operation key forwarded to ``llm.analyze`` for cost-event
        tagging. See :mod:`src.cost_tracking.operations` for the
        catalog.

    Returns
    -------
    str
        Raw LLM response text.

    Raises
    ------
    Exception
        Re-raises the last exception from ``llm.analyze`` after retries are
        exhausted.
    """
    async with semaphore:
        response: str = await llm.analyze(
            prompt=prompt, context=context, operation=operation
        )
        return response


class NaturalLanguageProjectCreator(NaturalLanguageTaskCreator):
    """
    Handles creation of projects from natural language descriptions.

    Refactored to use base class and eliminate code duplication.
    """

    def __init__(
        self,
        kanban_client: Any,
        ai_engine: Any,
        subtask_manager: Any = None,
        complexity: str = "standard",
        state: Any = None,
    ) -> None:
        """Initialize the natural language project creator.

        Parameters
        ----------
        kanban_client : Any
            Kanban board client for task creation.
        ai_engine : Any
            AI engine for PRD parsing and task generation.
        subtask_manager : Any, optional
            Subtask manager for decomposed task tracking.
        complexity : str, default="standard"
            Project complexity mode: prototype / standard / enterprise.
        state : Any, optional
            Marcus MCP server state. Required for the Phase A → Phase B
            design artifact registration chain (GH-320): when present,
            the background design phase closure calls
            ``_register_design_via_mcp`` with this state so design
            contract artifacts reach ``state.task_artifacts`` and
            become discoverable to downstream implementation tasks via
            ``get_task_context``. When ``None``, design artifacts are
            still written to disk but are not registered in MCP state
            (legacy behavior).
        """
        super().__init__(kanban_client, ai_engine, subtask_manager, complexity)
        self.prd_parser = AdvancedPRDParser()
        self.board_analyzer = BoardAnalyzer()
        self.context_detector = ContextDetector(self.board_analyzer)
        self.state = state

    async def process_natural_language(
        self,
        description: str,
        **kwargs: Any,
    ) -> List[Task]:
        """
        Process project description into tasks.

        Implementation of abstract method from base class.

        Decomposer strategy
        -------------------
        The active decomposer strategy is resolved from
        ``options["decomposer"]`` or the ``MARCUS_DECOMPOSER``
        environment variable via
        :func:`src.config.decomposer_config.resolve_decomposer`:

        - ``feature_based`` (default): calls
          :meth:`AdvancedPRDParser.parse_prd_to_tasks`. Tasks shaped by
          functional requirements.
        - ``contract_first``: discovers domains from the PRD analysis,
          generates contract artifacts via
          :func:`_generate_contracts_by_domain`, and calls
          :meth:`AdvancedPRDParser.decompose_by_contract` to produce
          tasks where each task owns a contract interface (GH-320 PR 2).

        If contract-first is selected but contract generation or
        decomposition fails (weak contracts, LLM failure, empty
        domains), the caller falls back to feature-based with a loud
        warning — never a silent fallback.
        """
        # Extract arguments from kwargs
        project_name = kwargs.get("project_name") or "Unnamed Project"
        options = kwargs.get("options")
        project_root = options.get("project_root") if options else None

        # Reset the actual-decomposer marker before picking a path.
        # Set at the moment the path is finalized so the cost layer can
        # stamp ``runs.decomposer`` with what RAN rather than what was
        # requested (Marcus #519 fallback-label fix). The two values
        # diverge whenever contract_first falls back to feature_based —
        # see the visible fallback at the end of this if-block.
        self._actual_decomposer: Optional[str] = None

        # Coarse project classification produced by the PRD-analysis
        # LLM call (Marcus #546 Phase 0).  Set the moment a decomposer
        # path produces its PRD analysis, then read into the
        # ``create_project`` result dict so Phase 0 persistence and the
        # ``project_created`` telemetry event can ship taxonomy-bucketed
        # labels without re-reading the PRD analysis.  Default
        # ``"unknown"`` if no path runs (early return on empty tasks).
        self._project_domain: str = "unknown"
        self._project_structural_category: str = "unknown"
        # Technology labels the planner detected (#546 Phase 0).
        # Local-only — persisted to the cost DB, never telemetry.
        self._project_detected_tech_stack: List[str] = []

        # Planner intent-fidelity signals (#546 Phase 0).  Stashed by
        # ``_emit_intent_fidelity_event`` during decomposition; the
        # create_project wrapper persists them to the cost DB AFTER
        # ``record_run`` creates the row (a write during decomposition
        # would UPDATE zero rows — the row does not exist yet).
        # ``None`` when outcome coverage did not run.
        self._project_intent_fidelity_score: Optional[float] = None
        self._project_coverage_before_fill: Optional[float] = None
        self._project_coverage_after_fill: Optional[float] = None

        # Detect context (Phase 1)
        await self.board_analyzer.analyze_board("default", [])
        context = await self.context_detector.detect_optimal_mode(
            user_id="system", board_id="default", tasks=[]
        )

        if context.recommended_mode != MarcusMode.CREATOR:
            logger.warning(f"Expected creator mode but got {context.recommended_mode}")

        # Parse PRD with AI (Phase 4)
        constraints = self._build_constraints(options)
        logger.info(f"Parsing PRD with constraints: {constraints}")

        # Contract-first decomposition path (GH-320 PR 2)
        if is_contract_first(options):
            logger.info(
                "[decomposer] contract_first strategy selected — "
                "attempting contract-first decomposition"
            )
            contract_tasks = await self._try_contract_first_decomposition(
                description=description,
                project_name=project_name,
                project_root=project_root,
                constraints=constraints,
                options=options,
            )
            if contract_tasks is not None:
                logger.info(
                    f"[decomposer] contract_first produced "
                    f"{len(contract_tasks)} task(s)"
                )
                self._actual_decomposer = "contract_first"
                return contract_tasks
            # Fell through to feature-based fallback
            logger.warning(
                "[decomposer] contract_first decomposition failed or "
                "produced weak contracts; falling back to feature_based "
                "(this is a visible fallback — the user asked for "
                "contract_first and is not getting it)"
            )

        # Pre-fork synthesis (GH-355): detect shared foundation needs
        # before domain tasks are created.  Feature-based path runs on
        # PRD text only (lower fidelity than contract_first).
        # Conservative: returns [] on LLM failure — never blocks creation.
        foundation_tasks = await self._synthesize_shared_foundation(description)

        # Feature-based decomposition path (default + fallback target)  # noqa: E501
        prd_result = await self.prd_parser.parse_prd_to_tasks(description, constraints)

        task_count = len(prd_result.tasks) if prd_result.tasks else 0
        logger.info(f"PRD parser returned {task_count} tasks")
        if not prd_result.tasks:
            logger.warning("PRD parser returned no tasks!")
            logger.debug(f"PRD result: {prd_result}")
            return []

        # Phase 5: emit intent-fidelity event for Cato telemetry.
        # TaskGenerationResult flattens the coverage fields at the top
        # level (vs contract-first's nested .coverage object) — see
        # the type asymmetry note in _emit_intent_fidelity_event's
        # docstring.  Helper no-ops when intent_fidelity_score is None.
        await self._emit_intent_fidelity_event(
            project_name=project_name,
            decomposer="feature_based",
            intent_fidelity_score=prd_result.intent_fidelity_score,
            coverage_before_fill=prd_result.coverage_before_fill,
            coverage_after_fill=prd_result.coverage_after_fill,
            gap_filled_outcomes=prd_result.gap_filled_outcomes,
        )

        # Apply the inferred dependencies to the task objects
        if prd_result.dependencies:
            dep_count = len(prd_result.dependencies)
            logger.info(f"Applying {dep_count} inferred dependencies to tasks")

            # Create a mapping of task IDs to tasks for quick lookup
            task_map = {task.id: task for task in prd_result.tasks}

            # Apply each dependency
            for dep in prd_result.dependencies:
                dependent_task_id = dep.get("dependent_task_id")
                dependency_task_id = dep.get("dependency_task_id")

                if dependent_task_id in task_map and dependency_task_id in task_map:
                    dependent_task = task_map[dependent_task_id]

                    # Add the dependency if not already present
                    if dependency_task_id not in dependent_task.dependencies:
                        dependent_task.dependencies.append(dependency_task_id)
                        logger.debug(
                            f"Added dependency: {task_map[dependent_task_id].name} "
                            f"depends on {task_map[dependency_task_id].name} "
                            f"(reason: {dep.get('reasoning', 'inferred')})"
                        )
                else:
                    logger.warning(
                        f"Could not apply dependency: "
                        f"{dependent_task_id} -> {dependency_task_id} "
                        f"(task not found in task map)"
                    )
        else:
            logger.info("No dependencies returned from PRD parser")

        # Wire domain tasks to depend on pre-fork foundation tasks (GH-355).
        # Foundation tasks must complete before any domain-specific work
        # begins — this is the board-state guarantee for visual/structural
        # coherence across parallel agents.
        domain_tasks = prd_result.tasks
        # Reached the feature-based return path — either by default or by
        # falling back from a failed contract_first attempt. Either way,
        # the work that actually produced these tasks was feature_based.
        self._actual_decomposer = "feature_based"
        self._project_domain = prd_result.domain
        self._project_structural_category = prd_result.structural_category
        self._project_detected_tech_stack = prd_result.detected_tech_stack
        if foundation_tasks:
            foundation_ids = [t.id for t in foundation_tasks]
            for task in domain_tasks:
                for fid in foundation_ids:
                    if fid not in task.dependencies:
                        task.dependencies.append(fid)
            return foundation_tasks + domain_tasks

        return domain_tasks

    async def _emit_intent_fidelity_event(
        self,
        *,
        project_name: str,
        decomposer: str,
        intent_fidelity_score: Optional[float],
        coverage_before_fill: Dict[str, List[str]],
        coverage_after_fill: Optional[Dict[str, List[str]]],
        gap_filled_outcomes: List[str],
    ) -> None:
        """Emit a PLANNING_INTENT_FIDELITY event for Cato telemetry.

        Issue #449 — Phase 5.  Both decomposer paths call this after
        producing tasks so Cato can render intent-fidelity alongside
        the planning-phase swim lanes.  No-ops when the score is
        ``None`` (coverage didn't run) or when the state object has
        no event system attached.

        Type asymmetry note: feature-based path reads these fields
        from ``TaskGenerationResult`` (top-level fields), while
        contract-first reads them from
        ``decompose_result.telemetry["outcome_coverage"]`` — the
        chain's namespaced telemetry slice (issue #456 Stage 3).
        The two paths flatten into the same event payload here, so
        downstream consumers see one uniform shape regardless of
        which decomposer ran.

        Parameters
        ----------
        project_name : str
            For tagging the event with project context.
        decomposer : str
            ``"feature_based"`` or ``"contract_first"`` — lets
            consumers compare fidelity across decomposers.
        intent_fidelity_score : Optional[float]
            Final score on the augmented graph; ``None`` means
            coverage didn't run.
        coverage_before_fill : Dict[str, List[str]]
            Initial coverage map (outcome.id → covering task ids).
        coverage_after_fill : Optional[Dict[str, List[str]]]
            Post-fill coverage; ``None`` when no gap-fill ran.
        gap_filled_outcomes : List[str]
            IDs of outcomes that were uncovered initially and
            received synthesized tasks.
        """
        if intent_fidelity_score is None:
            return
        if not (hasattr(self.state, "events") and self.state.events):
            return
        await self.state.events.publish_nowait(
            EventTypes.PLANNING_INTENT_FIDELITY,
            "nlp_orchestrator",
            {
                "project_name": project_name,
                "decomposer": decomposer,
                "intent_fidelity_score": intent_fidelity_score,
                "coverage_before_fill": coverage_before_fill,
                "coverage_after_fill": coverage_after_fill,
                "gap_filled_outcomes": gap_filled_outcomes,
            },
        )

        # The internal event carries coverage as MAPS
        # (outcome.id -> covering task ids); both the telemetry event
        # and the Phase 0 cost columns want scalar coverage RATIOS per
        # docs/telemetry.md.  Convert here: ratio = fraction of
        # outcomes with at least one covering task.  ``coverage_after_
        # fill`` is None when no gap-fill ran — then "after" == "before".
        def _coverage_ratio(cov_map: Dict[str, List[str]]) -> float:
            if not cov_map:
                return 0.0
            covered = sum(1 for tasks in cov_map.values() if tasks)
            return round(covered / len(cov_map), 4)

        before_ratio = _coverage_ratio(coverage_before_fill)
        after_ratio = (
            _coverage_ratio(coverage_after_fill)
            if coverage_after_fill is not None
            else before_ratio
        )

        # Stash the fidelity signals for Phase 0 cost persistence
        # (Marcus #546).  They are NOT written to the cost DB here:
        # this method runs during decomposition, but the ``runs`` row
        # is only INSERTed by ``record_run`` AFTER create_project's
        # inner work returns — a write now would UPDATE zero rows.
        # The create_project wrapper reads these off the result dict
        # and persists them after ``record_run`` (see
        # ``_persist_phase0_open_signals`` in marcus_mcp/tools/nlp.py).
        self._project_intent_fidelity_score = intent_fidelity_score
        self._project_coverage_before_fill = before_ratio
        self._project_coverage_after_fill = after_ratio

        # Forward to PostHog telemetry (Marcus #416, Stage 5A of #9).
        # The forwarder explicitly drops project_name — its signature
        # is the privacy regression net.  Helper swallows its errors.
        try:
            from src.telemetry.events import fire_planning_intent_fidelity

            fire_planning_intent_fidelity(
                decomposer=decomposer,
                intent_fidelity_score=intent_fidelity_score,
                coverage_before_fill=before_ratio,
                coverage_after_fill=after_ratio,
                gap_filled_outcomes=len(gap_filled_outcomes),
            )
        except Exception:  # noqa: BLE001
            pass

    async def _try_contract_first_decomposition(
        self,
        description: str,
        project_name: str,
        project_root: Optional[str],
        constraints: ProjectConstraints,
        options: Optional[Dict[str, Any]],
    ) -> Optional[List[Task]]:
        """
        Attempt contract-first decomposition (GH-320 PR 2).

        Runs the full contract-first pipeline:

        1. Deep PRD analysis to get functional requirements.
        2. Discover natural domain groupings from the requirements.
        3. Generate contract artifacts per domain via
           :func:`_generate_contracts_by_domain`.
        4. Call :meth:`AdvancedPRDParser.decompose_by_contract` with
           the generated contracts to produce contract-owned tasks.

        The method returns ``None`` on any failure so the caller can
        fall back to feature-based decomposition with a visible
        warning. No silent fallbacks.

        Parameters
        ----------
        description : str
            Project description (from the user).
        project_name : str
            Project name (used in contract artifact metadata).
        project_root : Optional[str]
            Where contract artifacts are written. If ``None``,
            contract-first cannot proceed (artifacts have nowhere to
            land) and the method returns None.
        constraints : ProjectConstraints
            Project constraints from the caller. Forwarded to the
            PRD parser and the decomposer.
        options : Optional[Dict[str, Any]]
            Project options dict. Used for the agent count hint.

        Returns
        -------
        Optional[List[Task]]
            A list of contract-owned tasks on success, or ``None``
            on any failure. Caller falls back to feature_based when
            None is returned.

        Notes
        -----
        Domain discovery and contract generation are the two
        pre-existing weak points — they rely on LLM quality. If the
        LLM produces a single undifferentiated "General" domain or
        empty contract artifacts for every domain, contract-first
        decomposition cannot proceed and we fall back.

        GH-320 : Contract-first task decomposition.
        Experiment 4 (pending) : LLM-generated contract validation gate.
        """
        if project_root is None:
            logger.warning(
                "[decomposer] contract_first requires project_root in "
                "options; falling back to feature_based"
            )
            return None

        try:
            prd_analysis = await self.prd_parser._analyze_prd_deeply(
                description, constraints
            )
        except Exception as e:
            logger.warning(
                f"[decomposer] contract_first PRD analysis failed: {e}; "
                f"falling back to feature_based"
            )
            return None

        # Stash project classification (#546 Phase 0).  Set here even
        # though contract_first may still fall back below — if it does,
        # the feature-based path overwrites these from its own
        # prd_result, so the values always reflect the path that ran.
        self._project_domain = prd_analysis.domain
        self._project_structural_category = prd_analysis.structural_category
        self._project_detected_tech_stack = prd_analysis.detected_tech_stack

        functional_reqs = prd_analysis.functional_requirements or []
        if not functional_reqs:
            logger.warning(
                "[decomposer] contract_first: PRD produced no functional "
                "requirements; falling back to feature_based"
            )
            return None

        try:
            domain_groups = await self.prd_parser._discover_domains(functional_reqs)
        except Exception as e:
            logger.warning(
                f"[decomposer] contract_first domain discovery failed: {e}; "
                f"falling back to feature_based"
            )
            return None

        if not domain_groups:
            logger.warning(
                "[decomposer] contract_first: no domains discovered; "
                "falling back to feature_based"
            )
            return None

        # Build ``{domain_name: description}`` mapping that
        # ``_generate_contracts_by_domain`` expects. Each domain
        # description is a bulleted list of its features — enough
        # context for the LLM to generate a meaningful contract.
        domains_for_contracts: Dict[str, str] = {}
        feature_map = {
            req.get("id", f"feature_{idx}"): req
            for idx, req in enumerate(functional_reqs, start=1)
        }
        for domain_name, feature_ids in domain_groups.items():
            bullets = []
            req_bullets = []
            for feature_id in feature_ids:
                feature = feature_map.get(feature_id)
                if feature is None:
                    continue
                name = feature.get("name", feature_id)
                feature_desc = feature.get("description", "")
                bullets.append(f"- {name}: {feature_desc}")
                # Preserve the requirement name as a user-facing
                # behavior the domain must support (GH-320 #64).
                req_bullets.append(f"- {name}")
            # Intent preservation (GH-320 #64): include the user-
            # facing requirements in the domain description so the
            # LLM sees them as constraints on the contract design,
            # not just technical context. When the requirement says
            # "Display weather temperature", the LLM should generate
            # a contract that includes a rendering interface — not
            # just an API endpoint. This was the root cause of the
            # "locally rigorous, globally amnesiac" failure mode in
            # Experiment 4 v2 where user-facing verbs were dropped
            # across the requirements → domains → contracts chain.
            req_section = ""
            if req_bullets:
                req_section = (
                    "\n\nUser-Facing Requirements (the user expects "
                    "to SEE or EXPERIENCE these behaviors — your "
                    "contracts must define interfaces that make them "
                    "visible, not just back-end plumbing):\n" + "\n".join(req_bullets)
                )
            domains_for_contracts[domain_name] = (
                f"Domain: {domain_name}\n\nFeatures:\n"
                + "\n".join(bullets)
                + req_section
            )

        try:
            contract_artifacts = await _generate_contracts_by_domain(
                domains=domains_for_contracts,
                project_description=description,
                project_name=project_name,
                project_root=project_root,
            )
        except Exception as e:
            logger.warning(
                f"[decomposer] contract_first contract generation failed: "
                f"{e}; falling back to feature_based"
            )
            return None

        # Check contract completeness threshold: at least one domain
        # must produce usable artifacts.
        usable_contracts = {
            domain: payload
            for domain, payload in contract_artifacts.items()
            if payload is not None and payload.get("artifacts")
        }
        if not usable_contracts:
            logger.warning(
                "[decomposer] contract_first: contract generation "
                "produced no usable artifacts for any domain; falling "
                "back to feature_based"
            )
            return None

        # Decomposition gate, check 1: cross-contract type consistency.
        # Catches the WidgetPosition class of bug from Experiment 4 v2
        # where two contracts defined the same field name with
        # different types. Fall back to feature_based when contracts
        # disagree — agents would otherwise build incompatible code.
        from src.integrations.contract_validation import (
            check_contract_cross_file_consistency,
        )

        consistency = check_contract_cross_file_consistency(usable_contracts)
        if not consistency["pass"]:
            contradiction_summary = ", ".join(
                f"{c['field']} ({'/'.join(c['types_by_file'].values())})"
                for c in consistency["contradictions"]
            )
            logger.warning(
                f"[decomposer] contract_first: cross-contract type "
                f"consistency check failed — "
                f"{len(consistency['contradictions'])} field(s) "
                f"defined with different types across contracts: "
                f"{contradiction_summary}. Falling back to "
                f"feature_based to avoid silent agent integration "
                f"failures."
            )
            return None

        # Pre-fork synthesis (GH-355): detect shared foundation using
        # domain contracts.  Higher fidelity than feature_based because
        # the full contract set is available here.  Conservative:
        # returns [] on LLM failure so the pipeline never stalls.
        foundation_tasks = await self._synthesize_shared_foundation(
            description, domains=usable_contracts
        )

        try:
            decompose_result = await self.prd_parser.decompose_by_contract(
                prd_analysis=prd_analysis,
                contract_artifacts=contract_artifacts,
                constraints=constraints,
                # Codex P2 on PR #473: thread foundation tasks into the
                # decomposer so the augmenter chain sees them during
                # coverage scanning.  Without this, spec_coverage would
                # only see contract tasks and could synthesize duplicate
                # spec_gap tasks for features that foundation tasks
                # already implement.
                pre_existing_tasks=foundation_tasks,
            )
            # Issue #456 Stage 3: decompose_by_contract returns
            # AugmentationResult.  ``augmented_tasks`` carries
            # foundation + contract tasks plus any synthesized
            # gap-fill tasks; ``telemetry`` is namespaced by augmenter
            # name so each augmenter's payload sits in its own dict.
            tasks = decompose_result.augmented_tasks

            # Phase 5: emit intent-fidelity event for Cato telemetry.
            # The outcome_coverage augmenter contributes its slice
            # under the ``outcome_coverage`` key.  When the augmenter
            # no-opped (flag off / no outcomes / LLM error), the slice
            # is absent — skip emission for the same reason as before.
            oc_telemetry: Dict[str, Any] = decompose_result.telemetry.get(
                "outcome_coverage", {}
            )
            if oc_telemetry:
                await self._emit_intent_fidelity_event(
                    project_name=project_name,
                    decomposer="contract_first",
                    intent_fidelity_score=oc_telemetry["intent_fidelity_score"],
                    coverage_before_fill=oc_telemetry.get("coverage_before_fill", {}),
                    coverage_after_fill=oc_telemetry.get("coverage_after_fill"),
                    gap_filled_outcomes=oc_telemetry.get("gap_filled_outcomes", []),
                )
        except Exception as e:
            # Catch broadly so the fallback path is bulletproof. The
            # advertised behavior of this helper is "return None on any
            # decomposition failure so the caller falls back to
            # feature_based". Narrowing to ValueError/RuntimeError
            # leaked TypeError/AttributeError from malformed-but-
            # parseable LLM JSON (e.g. ``estimated_minutes: null``
            # causing ``float(None)`` → TypeError, or non-string
            # description breaking ``.strip()``) — those should
            # trigger fallback, not crash project creation. Codex
            # caught this on PR #327 review. ``decompose_by_contract``
            # also now coerces its inputs defensively so most of these
            # never fire, but the broad catch is the last line of
            # defense for unknown shape drift in LLM output.
            logger.warning(
                f"[decomposer] contract_first decomposer failed "
                f"({type(e).__name__}): {e}; falling back to "
                f"feature_based"
            )
            return None

        if not tasks:
            logger.warning(
                "[decomposer] contract_first decomposer returned no "
                "tasks; falling back to feature_based"
            )
            return None

        # NOTE: Verb-coverage gate was here but removed per Larry's
        # review. A 6-verb hard-coded checklist was too brittle to
        # serve as a decomposition gate — it would have thrown away
        # contract-first's 55/45 coordination win over one missing
        # task. The right fix is structural: task #64 will thread
        # ALL functional requirements through the contract generation
        # prompt and synthesize gap tasks for any still uncovered.
        # That's additive (keep contract-first + add missing tasks),
        # not destructive (fall back entirely).
        #
        # ``check_requirement_coverage`` still exists in
        # ``src/integrations/contract_validation.py`` for future use
        # as an advisory diagnostic in #64.

        # Cato retrofit (GH-320 PR after #333): synthesize one DONE
        # design task per usable domain so the existing feature-based
        # observability infrastructure fires for contract-first runs.
        #
        # Why this is necessary: contract_artifacts is generated in
        # Phase A (above) and consumed by ``decompose_by_contract`` to
        # build implementation task descriptions, then thrown away.
        # No ``log_artifact`` call, no ``log_decision`` call, no
        # structural task in marcus.db. Cato's display_role classifier
        # at ``cato_src/core/aggregator.py`` only marks tasks as
        # ``"structural"`` when they have ``"design"`` in labels — so
        # without these ghosts, contract-first projects show up as
        # all-work, no design phase, no decisions, no artifacts.
        #
        # The ghosts are honest: they represent "the design phase that
        # produced this domain's contracts". They are born DONE because
        # the work has already happened. The existing background
        # ``_run_design_phase`` will pick them up via ``_is_design_task``
        # and route the stashed contract content through Phase B
        # (``_register_design_via_mcp``), which calls ``log_artifact``
        # and ``log_decision`` against each ghost's kanban UUID.
        usable_contracts = {
            domain: payload
            for domain, payload in contract_artifacts.items()
            if payload is not None and payload.get("artifacts")
        }

        ghost_tasks: List[Task] = []
        # Rekeyed dict that ``_run_design_phase`` will receive in its
        # ``pre_generated_content`` parameter — keyed by ghost task
        # name so the existing Phase B name-join works unchanged.
        stashed_design_content: Dict[str, Dict[str, Any]] = {}
        now = datetime.now(timezone.utc)
        import uuid as _uuid

        for domain_name, payload in usable_contracts.items():
            ghost_id = f"design_contract_{_uuid.uuid4().hex[:12]}"
            ghost_name = f"Design {domain_name}"

            # Pick the interface-contracts artifact as the canonical
            # contract file for this domain. Match by FILENAME, not
            # by artifact_type — the live generator emits interface
            # contracts with artifact_type="specification" (shared
            # with data_models), not "interface_contracts". Same fix
            # as Codex P1 on PR #335 for the consistency gate.
            # Falls back to the first artifact if no interface-
            # contracts file exists.
            artifacts = payload.get("artifacts", [])
            interface_artifact = next(
                (
                    a
                    for a in artifacts
                    if "interface-contracts" in a.get("filename", "")
                ),
                artifacts[0] if artifacts else {},
            )
            contract_file = interface_artifact.get("relative_path", "")

            ghost = Task(
                id=ghost_id,
                name=ghost_name,
                description=(
                    f"Contract-first design phase for domain "
                    f"'{domain_name}'. Generated {len(artifacts)} "
                    f"contract artifact(s). This task is born DONE — "
                    f"the design work has already completed by the "
                    f"time it lands on the kanban board."
                ),
                status=TaskStatus.DONE,
                priority=Priority.HIGH,
                assigned_to="Marcus",
                created_at=now,
                updated_at=now,
                due_date=None,
                estimated_hours=0.0,
                # Codex P2 on PR #334: do NOT add a "contract_first"
                # label here. ``SafetyChecker._find_related_tasks``
                # treats any non-prefixed shared label as a relation,
                # and ``apply_implementation_dependencies`` would link
                # every contract-first impl task (which carries the
                # "contract_first" label) to every ghost (which would
                # also carry it), undoing the per-domain wiring below.
                # Provenance lives on ``source_type`` instead, which
                # is not consulted by the safety check.
                # Add ``domain:`` label for dependency wiring.
                # ``SafetyChecker._find_related_tasks`` matches tasks
                # by label overlap (priority 3). This is how impl
                # tasks find their matching design ghost — both carry
                # the same ``domain:`` label. Keyword matching
                # (priority 4) fails because compound names like
                # "WeatherWidget" don't split to match "Weather
                # Information System" (needs ≥2 matching words).
                labels=[
                    "design",
                    "auto_completed",
                    f"domain:{domain_name.lower().replace(' ', '-')}",
                ],
                source_type="contract_first_design",
                source_context={
                    "contract_file": contract_file,
                    "domain": domain_name,
                },
                dependencies=[],
            )
            ghost_tasks.append(ghost)
            stashed_design_content[ghost_name] = payload

        # Add ``domain:`` labels to impl tasks so the safety checker
        # can match them to their design ghosts. The match key is the
        # domain label, not the contract_file (which differs between
        # artifact types within the same domain).
        #
        # Build contract_file → domain mapping from the usable
        # contracts dict so each impl task's contract_file resolves
        # to its domain name.
        contract_file_to_domain: Dict[str, str] = {}
        for d_name, d_payload in usable_contracts.items():
            for art in d_payload.get("artifacts", []):
                rp = art.get("relative_path", "")
                if rp:
                    contract_file_to_domain[rp] = d_name

        for task in tasks:
            ctx = getattr(task, "source_context", None) or {}
            cf = ctx.get("contract_file", "")
            domain = contract_file_to_domain.get(cf)
            if domain:
                domain_label = f"domain:{domain.lower().replace(' ', '-')}"
                if domain_label not in task.labels:
                    task.labels.append(domain_label)

        # Dependency wiring between ghost and impl tasks is handled
        # by ``SafetyChecker.apply_implementation_dependencies``
        # (called from ``apply_safety_checks``). It matches tasks by
        # the shared ``domain:`` label (priority 3 in
        # ``_find_related_tasks``). After kanban creation,
        # ``_remap_dependencies`` converts the slug IDs to real UUIDs
        # and persists them to the kanban.

        # Wire impl tasks to depend on pre-fork foundation tasks (GH-355).
        # Foundation tasks are TODO and must complete before domain agents
        # begin.  Ghost tasks (DONE) are excluded — they represent design
        # work that already happened and need not wait for foundation.
        # Codex P2 on PR #473: ``tasks`` now includes foundation tasks
        # themselves (threaded through decompose_by_contract via
        # ``pre_existing_tasks``).  Skip foundation tasks in the loop so
        # we don't add a foundation task as its own dependency.
        if foundation_tasks:
            foundation_id_set = {t.id for t in foundation_tasks}
            for task in tasks:
                if task.id in foundation_id_set:
                    continue
                for fid in foundation_id_set:
                    if fid not in task.dependencies:
                        task.dependencies.append(fid)

        # Stash the rekeyed design content on the creator instance.
        # The background design phase scheduler at the end of
        # ``create_tasks_from_description`` reads this attribute via
        # ``getattr`` and forwards it to ``_run_design_phase`` as
        # ``pre_generated_content``, which causes Phase A to be
        # skipped and Phase B to use the pre-generated content.
        self._contract_first_design_content = stashed_design_content

        # Intent preservation (GH-320 task #64): stash the functional
        # requirements so ``create_project_from_description`` can
        # forward them to ``enhance_project_with_integration``, which
        # appends them as acceptance_criteria on the integration task.
        self._contract_first_requirements = functional_reqs

        # Issue #523 Slice B: stash the extracted user outcomes
        # (from ``_analyze_prd_deeply``) alongside the requirements
        # so the same enhance_project_with_integration call site can
        # forward them.  Used to grow the integration task description
        # with a "Verifications required" section and to stash
        # ``in_scope_outcome_ids`` on the task for the smoke gate's
        # coverage check at completion.
        self._contract_first_user_outcomes = list(prd_analysis.user_outcomes or [])

        logger.info(
            f"[decomposer] contract_first: synthesized "
            f"{len(ghost_tasks)} design ghost(s) for "
            f"{len(usable_contracts)} domain(s); "
            f"{len(foundation_tasks)} foundation task(s) pre-fork; "
            f"contract content stashed for background Phase B"
        )

        # Issue #463: synthesize a composition task when len(impl_tasks)
        # >= 2.  Multi-domain contract-first projects can ship with no
        # task owning entry-point wiring — every domain implements
        # cleanly but App.tsx returns null.  v38 audit case caught this
        # via integration verification's catch-all at the cost of
        # ~15 min cleanup absorbed by an agent.  Composition makes the
        # wiring an explicit deliverable with explicit ownership.
        # Layered safety with enhance_project_with_integration's
        # orphan scan (composition = narrow + early; IV = broad + late).
        #
        # Codex P2 (PR #472): ``tasks`` is
        # ``decompose_result.augmented_tasks`` which can include
        # outcome-coverage gap-fill tasks (source_type="gap_fill_contract"
        # synthesized by the contract-first outcome-coverage augmenter).
        # The composition trigger is "multi-domain wiring needed" —
        # gap-fill tasks address outcome coverage gaps, not domain
        # multiplicity, so they must NOT count toward the trigger.
        # Filter to source_type="contract_first" before passing.  IV's
        # broad catch-all still covers any wiring gaps in gap-fill tasks.
        from src.integrations.composition_synthesis import (
            build_composition_task,
        )

        contract_first_impl_tasks = [
            t for t in tasks if t.source_type == "contract_first"
        ]
        composition_task = build_composition_task(
            project_name=project_name,
            impl_tasks=contract_first_impl_tasks,
            # Defensive getattr matches the pattern at the create_project
            # result assembly below: the attribute is set lazily by the
            # decomposer path and may be absent on early/feature-based
            # flows.  Falls back to "unknown" (no behavior contract).
            structural_category=getattr(
                self, "_project_structural_category", "unknown"
            ),
        )

        # Design ghosts come first so the integration task's dependency
        # walk picks them up alongside impl tasks.  ``tasks`` already
        # contains foundation + contract + augmenter-synthesized tasks
        # because foundation_tasks were threaded into
        # decompose_by_contract via ``pre_existing_tasks`` (Codex P2 on
        # PR #473).  Composition (when synthesized) comes last because
        # it depends on every impl task.
        result_tasks: List[Task] = ghost_tasks + tasks
        if composition_task is not None:
            result_tasks.append(composition_task)
            logger.info(
                f"[decomposer] contract_first: synthesized composition "
                f"task '{composition_task.name}' with "
                f"{len(composition_task.dependencies)} impl-task dep(s)"
            )
        return result_tasks

    async def _synthesize_shared_foundation(
        self,
        description: str,
        domains: Optional[Dict[str, Any]] = None,
    ) -> List[Task]:
        """
        Detect shared foundation needs before parallel agents spawn (GH-355).

        Makes a single LLM call to analyse whether parallel development
        agents need any shared foundation built before starting domain-
        specific work.  Three synthesis targets are checked:

        - **Design System**: shared visual tokens (colors, typography,
          spacing, themes) — needed when multiple UI features must look
          visually consistent.
        - **Shared Components**: reusable UI or logic components (Card,
          Button, API client) — needed when ≥2 domains use the same
          component.
        - **Tech Foundation**: shared configuration (TypeScript config,
          routing setup, test harness) — needed when agents would
          duplicate this setup independently.

        This method is **conservative**: it returns an empty list on any
        LLM or parse failure so project creation is never blocked.

        Parameters
        ----------
        description : str
            Project description (PRD text). Used as the primary input.
        domains : Optional[Dict[str, Any]], optional
            Domain contracts from contract-first decomposition.  When
            provided, the prompt gains higher-fidelity context about
            what each domain builds.  ``None`` for the feature-based
            path (lower-fidelity, PRD text only).

        Returns
        -------
        List[Task]
            Pre-fork foundation tasks (status TODO, priority HIGH,
            labels ``["foundation", "pre-fork"]``).  Empty list when no
            shared foundation is needed or when the LLM call fails.

        Notes
        -----
        GH-355 : Pre-fork synthesis — shared foundation tasks before
        parallel agents start.
        """
        import uuid as _uuid

        from src.utils.structured_llm import safe_structured_call

        # Appended to every synthesized description so foundation tasks
        # carry the same Marcus workflow reminder as any implementation
        # task.  Scope matters: log_artifact is for INTERFACE-BEARING
        # reference outputs (contracts, schemas, API specs, design
        # docs), NOT source files.  Git is already the file channel —
        # an artifact copy of a source file goes stale the moment the
        # real one changes (two sources of truth, the v80 failure class
        # through a different door), it buries the one contract that
        # matters under implementation blobs, and every artifact
        # inflates downstream get_task_context payloads (coordination
        # tax buying nothing git doesn't provide).  This is not HOW
        # guidance; it is standard Marcus workflow for tasks that
        # produce reference artifacts consumed by other tasks.
        #
        # Issue #446 (Kaia review checkpoint #2): foundation agents must
        # also call log_decision titled "Public API surface" so the
        # structured public-API metadata (import paths, exported
        # symbols, config keys, usage constraints) flows downstream
        # via Context.get_context (core/context.py:334-346 already
        # pulls dependency decisions into architectural_decisions).
        # Without this decision, downstream agents have no canonical
        # structured way to discover the foundation surface and may
        # invent paths — v80 audit case (agent_unicorn_2 read
        # tokens.json instead of tokens.css because no canonical
        # decision existed).  The agent picks the actual paths /
        # names / module organization — Marcus only requires that
        # the chosen surface be published.  Bright-line: coordination
        # contract requirement, not implementation guidance.
        _WORKFLOW_REMINDER = (
            " Call log_artifact for interface-bearing outputs that "
            "downstream agents must consume as references — contracts, "
            "schemas, API specs, design docs — so they can discover "
            "them via get_task_context. Do NOT log source files: they "
            "are discovered via git once your work merges. Also call "
            "log_decision titled "
            "'Public API surface' listing the exact import paths, "
            "exported symbols, config keys, and usage constraints "
            "downstream consumers must coordinate against — without "
            "this decision, downstream agents may invent paths and "
            "miss your work."
        )

        # Build optional domain-contract section for higher-fidelity
        # contract_first synthesis.  Feature-based path omits this.
        domain_section = ""
        if domains:
            lines: List[str] = []
            for domain_name, payload in domains.items():
                artifacts = (
                    payload.get("artifacts", []) if isinstance(payload, dict) else []
                )
                preview = ""
                if artifacts:
                    raw_content = artifacts[0].get("content", "")
                    preview = str(raw_content)[:300].strip()
                lines.append(f"Domain '{domain_name}':\n{preview}")
            domain_section = (
                "\n\nDomain Contracts (for higher-fidelity analysis):\n"
                + "\n---\n".join(lines)
            )

        prompt = (
            "You are analysing a software project to determine whether "
            "parallel development agents need a shared foundation before "
            "their independent work begins.\n\n"
            f"PRD Description:\n{description}"
            f"{domain_section}\n\n"
            "Analyse whether parallel agents working on this project need "
            "ANY of these shared foundations BEFORE starting domain-specific "
            "work:\n\n"
            "1. Design System: shared visual tokens (colors, typography, "
            "spacing, themes) — needed when multiple UI features must look "
            "visually consistent.\n"
            "2. Shared Components: reusable UI or logic components (Card, "
            "Button, API client) — needed when ≥2 domains will use the "
            "same component.\n"
            "3. Tech Foundation: shared build/tooling configuration "
            "matching the project's stated tech stack (e.g., the build "
            "tool config, router setup, test harness for whatever "
            "language the spec asks for) — needed when agents would "
            "duplicate this setup independently. Do NOT assume "
            "TypeScript; honor the language the spec actually states "
            "(bug #649 root cause 1).\n\n"
            "Be CONSERVATIVE. Return foundation tasks ONLY when agents "
            "would DEFINITELY produce incompatible implementations without "
            "them.  When uncertain, return an empty list.\n\n"
            # Issue #463 (Kaia review checkpoint #2 corrected design):
            # snake-game-v38 audit showed the LLM emitting two
            # foundation tasks targeting the same conceptual domain
            # ("Game State Data Structure Contract" and "State Update
            # Event/Message Protocol" — both about game state).  A1
            # shipped 530 LOC against the first, deleted it during
            # integration verification because A2's parallel work
            # made A1's orphaned.  This rule asks the LLM to merge
            # same-conceptual-domain candidates within its own
            # response.  Bright-line: Marcus shapes the prompt to
            # avoid duplicates; agents still pick HOW each merged
            # task is implemented.  Coordination, not control.
            "DEDUPLICATE within your own response: if two of your "
            "candidates target the same conceptual domain, MERGE them "
            "into a single foundation task before returning.  Two "
            "candidates that both produce shared types for game state "
            "(e.g. one named 'X State Data Structure' and another "
            "'X State Update Protocol') target the same conceptual "
            "domain and should be ONE task, not two.\n\n"
            "By contrast, 'Database connection pool' and 'Theme "
            "tokens' are different conceptual domains — backend data "
            "infrastructure vs frontend visual design.  Their "
            "consumer sets do not overlap (no agent reaches for "
            "both at the same point in their work).  Do NOT merge "
            "those.\n\n"
            "Same conceptual domain means: any agent consuming one "
            "task's output would also reasonably consume the other's.  "
            "When uncertain whether two candidates overlap, prefer "
            "merging — false merges are recoverable; parallel "
            "duplicates orphan real agent work.\n\n"
            "Each foundation task MUST include acceptance_criteria: a "
            "list of concrete, checkable statements describing what "
            "'done' means for that task (e.g. 'GameState interface "
            "exported with score/grid/status fields'). Marcus validates "
            "the completed work — and the work of its subtasks — against "
            "these criteria; a foundation task with no criteria "
            "auto-passes validation, letting stub or placeholder code "
            "through.\n\n"
            "Return ONLY valid JSON with this exact structure:\n"
            '{"foundation_tasks": ['
            '{"name": "<plain task name, e.g. Design System Setup or '
            'Shared Widget Components — no category prefix>", '
            '"description": "<what to build and why parallel agents '
            'need it done first>", '
            '"estimated_hours": <positive number>, '
            '"acceptance_criteria": ["<concrete checkable statement>", '
            '"<another>"]}'
            "]}\n\n"
            'If no shared foundation is needed: {"foundation_tasks": []}'
        )

        try:
            parsed = await safe_structured_call(
                llm=self.prd_parser.llm_client,
                prompt=prompt,
                operation="synthesize_foundation_tasks",
                # Foundation synthesis output is small (typically 3-6
                # task stubs). Start tight; helper escalates on
                # truncation if a project ever needs more.
                initial_max_tokens=2048,
            )
            raw_tasks = parsed.get("foundation_tasks")
            if not isinstance(raw_tasks, list) or not raw_tasks:
                return []
        except Exception as exc:
            logger.warning(
                f"[pre-fork synthesis] structured LLM call failed "
                f"({exc}); no foundation tasks injected"
            )
            return []

        now = datetime.now(timezone.utc)
        foundation_tasks: List[Task] = []
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "Shared Foundation"))
            desc = str(item.get("description", "Shared foundation setup.")).rstrip(".")
            desc = desc + "." + _WORKFLOW_REMINDER
            try:
                hours = float(item.get("estimated_hours", 2.0))
            except (TypeError, ValueError):
                hours = 2.0
            if hours <= 0:
                hours = 2.0

            # #557: foundation tasks must carry acceptance_criteria so
            # their subtasks can be grounded against them (and so the
            # foundation task itself is not auto-passed by WorkAnalyzer).
            raw_criteria = item.get("acceptance_criteria", [])
            acceptance_criteria = (
                [str(c) for c in raw_criteria if str(c).strip()]
                if isinstance(raw_criteria, list)
                else []
            )

            task = Task(
                id=f"foundation_{_uuid.uuid4().hex[:12]}",
                name=name,
                description=desc,
                status=TaskStatus.TODO,
                priority=Priority.HIGH,
                assigned_to=None,
                created_at=now,
                updated_at=now,
                due_date=None,
                estimated_hours=hours,
                dependencies=[],
                acceptance_criteria=acceptance_criteria,
                # Foundation tasks are real implementation work done by
                # agents — not Marcus design ghosts. The "implementation"
                # label makes should_validate_task recognize them (and
                # their subtasks, which decide via the parent's labels)
                # as validatable. Without it the validation gate skips
                # foundation work entirely despite its acceptance_criteria
                # being populated — pre-fork alone is neither an
                # implementation nor exclusion label, so the filter
                # defaults to "skip" (#557 / Codex P2 on PR #559).
                # "pre-fork" stays so Cato can surface the pre-domain
                # distinction; no "design"/"foundation" label.
                labels=["pre-fork", "implementation"],
                source_type="pre_fork_synthesis",
            )
            foundation_tasks.append(task)

        # Serialize the foundation phase: chain each foundation task to
        # depend on the previous one so they run one at a time, not in
        # parallel. Foundation tasks all write the shared layer
        # (src/types, the export barrel, build config) and have no
        # file-ownership boundary between them — running two concurrently
        # produces worktree merge conflicts. Each task therefore starts
        # from a main branch that already contains the prior foundation
        # task's output. Proper file-partitioned parallelization is
        # tracked as a follow-up (see GitHub issue on foundation-phase
        # parallelization).
        for prev_task, next_task in zip(foundation_tasks, foundation_tasks[1:]):
            if prev_task.id not in next_task.dependencies:
                next_task.dependencies.append(prev_task.id)

        if foundation_tasks:
            logger.info(
                f"[pre-fork synthesis] Injecting {len(foundation_tasks)} "
                f"foundation task(s) (serialized): "
                + ", ".join(t.name for t in foundation_tasks)
            )

        return foundation_tasks

    async def create_project_from_description(
        self,
        description: str,
        project_name: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Create a complete project from natural language description.

        Uses the base class implementation for common functionality.
        """
        # Phase timing for performance monitoring (where does the
        # 30-60s project-creation cost come from?). Mirrors the inline
        # _mark pattern in ``src/marcus_mcp/tools/task.py`` so all
        # Marcus timing log lines can be grepped together. Marks fire
        # on the success path only — error returns skip the log.
        from src.core.perf_instrumentation import PhaseTimer

        _timer = PhaseTimer()
        try:
            _planning_started_at = datetime.now(timezone.utc)
            # Initialise deferred-write state so the except block can write
            # a failure outcome if project creation fails after planning ends.
            _planning_id: str | None = None
            _planning_persistence: object | None = None
            _planning_outcome_key: str | None = None
            _planning_outcome: dict[str, object] | None = None
            _planning_hours_val: float = 0.0
            log_agent_event(
                "planning_start",
                {
                    "project_name": project_name,
                    "description_length": len(description),
                },
            )
            logger.info(f"Creating project '{project_name}' from natural language")
            logger.debug(f"Description: {description[:200]}...")
            logger.debug(f"Options: {options}")

            # When contract_first is active but project_root is absent, the
            # decomposer falls back to feature_based. Project creation still
            # proceeds — log the fallback so the degraded strategy is visible
            # rather than aborting the whole run.
            _missing_root_msg = _build_decomposer_warning(options)
            if _missing_root_msg:
                logger.warning(_missing_root_msg)

            # Create a new project/board for each create_project call
            # Clear any existing project/board IDs to force new project creation
            if self.kanban_client:
                logger.info(
                    f"Creating new project '{project_name}' "
                    f"(clearing any existing project/board IDs)"
                )

                # Default from config, experiment options can override
                from src.config.marcus_config import get_config

                default_provider = get_config().kanban.provider or "sqlite"
                provider = (
                    options.get("provider", default_provider)
                    if options
                    else default_provider
                )

                if provider == "sqlite":
                    # SQLite: create a project scope (unique IDs)
                    # so this experiment's tasks are isolated
                    if hasattr(self.kanban_client, "auto_setup_project"):
                        await self.kanban_client.auto_setup_project(
                            project_name=project_name,
                            board_name="Main Board",
                            project_root=(
                                options.get("project_root") if options else None
                            ),
                        )
                    elif hasattr(self.kanban_client, "connect"):
                        await self.kanban_client.connect()
                    # Set active_project_id so task_metadata in marcus.db
                    # gets the correct project_id for Cato filtering.
                    # Only set if not already assigned (caller may have
                    # set it to the Marcus registry ID).
                    if not self.active_project_id:
                        self.active_project_id = getattr(
                            self.kanban_client, "project_id", None
                        )
                    logger.info(
                        f"Using SQLite provider for project "
                        f"'{project_name}' — "
                        f"project_id={self.active_project_id}"
                    )
                else:
                    # Clear IDs on non-SQLite providers (Planka, etc.)
                    if hasattr(self.kanban_client, "client"):
                        self.kanban_client.client.project_id = None
                        self.kanban_client.client.board_id = None
                    elif hasattr(self.kanban_client, "project_id"):
                        self.kanban_client.project_id = None
                        self.kanban_client.board_id = None

                    # Create project/board via provider-specific setup
                    from src.integrations.project_auto_setup import (
                        ProjectAutoSetup,
                    )

                    auto_setup = ProjectAutoSetup()
                    try:
                        client_to_use = (
                            self.kanban_client.client
                            if hasattr(self.kanban_client, "client")
                            else self.kanban_client
                        )
                        project_config = await auto_setup.setup_new_project(
                            kanban_client=client_to_use,
                            provider=provider,
                            project_name=project_name,
                            options=options,
                        )
                        proj_id = project_config.provider_config.get("project_id")
                        bd_id = project_config.provider_config.get("board_id")
                        logger.info(
                            f"Created new {provider} project: "
                            f"project_id={proj_id}, board_id={bd_id}"
                        )
                    except Exception as e:
                        logger.error(f"Failed to create new project: {e}")
                        raise

            _timer.mark("kanban_setup")

            # Parse tasks
            from src.core.error_framework import ErrorContext, error_context

            with error_context(
                "task_parsing",
                custom_context={
                    "project_name": project_name,
                    "description_length": len(description),
                },
            ):
                tasks = await self.process_natural_language(
                    description, project_name=project_name, options=options
                )
                logger.info(f"process_natural_language returned {len(tasks)} tasks")

                # Log detailed task breakdown for debugging. Uses the
                # EnhancedTaskClassifier (via self.task_classifier,
                # inherited from NaturalLanguageTaskCreator) rather
                # than reading a non-existent ``task_type`` attribute
                # off the Task dataclass — the pre-existing bug that
                # made every breakdown show ``{'unknown': N}``.
                if tasks:
                    task_types = _task_type_breakdown(tasks, self.task_classifier)
                    logger.info(f"Task type breakdown: {task_types}")
                else:
                    desc_len = len(description)
                    logger.warning(
                        f"No tasks generated for project '{project_name}' "
                        f"with description length {desc_len}"
                    )

            if not tasks:
                from src.core.error_framework import (  # noqa: F811
                    BusinessLogicError,
                    ErrorContext,
                )

                logger.warning("No tasks generated from natural language processing!")

                error_msg = (
                    f"Failed to generate any tasks from project description. "
                    f"The description may be too vague, missing key details, "
                    f"or not match expected patterns. "
                    f"Description: '{description[:200]}...'"
                )
                raise BusinessLogicError(
                    error_msg,
                    context=ErrorContext(
                        operation="create_project",
                        integration_name="nlp_tools",
                        custom_context={
                            "project_name": project_name,
                            "description_length": len(description),
                        },
                    ),
                )

            _timer.mark("natural_language_processing")

            # Apply safety checks using base class
            with error_context(
                "safety_checks",
                custom_context={
                    "project_name": project_name,
                    "original_task_count": len(tasks),
                },
            ):
                safe_tasks = await self.apply_safety_checks(tasks)
                logger.info(f"Safety checks passed, {len(safe_tasks)} tasks ready")

                # Spec coverage moved to the augmenter chain inside the
                # decomposer (issue #456 Stage 4).  Gap tasks
                # synthesized by spec_coverage now flow through
                # ``_infer_smart_dependencies`` and foundation wiring
                # like first-class graph members instead of being
                # appended here as orphans (``dependencies=[]``).  This
                # is the v37 orphan-failure-mode fix.
                #
                # Order property preserved: the augmenter chain runs
                # before the decomposer returns, so by the time
                # enhance_project_with_integration / documentation see
                # "all existing tasks", the spec_gap tasks are in the
                # pool and become dependencies of the integration /
                # documentation tasks naturally.

                # Add integration verification task if appropriate
                # Must be added BEFORE documentation so doc task
                # depends on integration task.
                #
                # For contract-first decomposition (GH-320 PR 2),
                # pass the shared contract file path so the
                # integration task description instructs the
                # verification agent to treat it as authoritative
                # (fix implementations, not the contract).
                from src.integrations.integration_verification import (
                    enhance_project_with_integration,
                )

                contract_file_for_integration: Optional[str] = None
                for task in safe_tasks:
                    ctx = getattr(task, "source_context", None) or {}
                    candidate = ctx.get("contract_file")
                    if candidate:
                        contract_file_for_integration = candidate
                        break

                # Intent preservation (GH-320 task #64): pass
                # functional requirements to the integration task
                # so it verifies the user's original ask. The
                # requirements were stashed by
                # _try_contract_first_decomposition; for the
                # feature-based path the attribute is absent and
                # defaults to None (no enrichment).
                stashed_requirements = getattr(
                    self, "_contract_first_requirements", None
                )
                # Issue #523 Slice B: same stash pattern for the
                # extracted user outcomes.  The contract-first path
                # populates them at decomposition time; the
                # feature-based path leaves the attribute absent
                # (defaults to None, no outcomes section).  Feature-
                # based wiring is a follow-up — the snake-game class
                # of failure ships via contract_first, the default.
                stashed_outcomes = getattr(self, "_contract_first_user_outcomes", None)
                safe_tasks = enhance_project_with_integration(
                    safe_tasks,
                    description,
                    project_name,
                    contract_file=contract_file_for_integration,
                    functional_requirements=stashed_requirements,
                    outcomes=stashed_outcomes,
                    structural_category=getattr(
                        self, "_project_structural_category", "unknown"
                    ),
                )
                logger.info(
                    "After integration enhancement: " f"{len(safe_tasks)} tasks"
                )

                # Add documentation task if appropriate
                from src.integrations.documentation_tasks import (
                    enhance_project_with_documentation,
                )

                safe_tasks = enhance_project_with_documentation(
                    safe_tasks, description, project_name
                )
                logger.info(
                    "After documentation enhancement: " f"{len(safe_tasks)} tasks"
                )

                # Log safety check impact
                added_tasks = len(safe_tasks) - len(tasks)
                if added_tasks > 0:
                    logger.info(f"Safety checks added {added_tasks} dependency tasks")

            # Design tasks go on the board as TODO but assigned to
            # Marcus so workers can't grab them. Phase A (design
            # artifact generation) runs in the background AFTER the
            # response is returned. Workers can't grab implementation
            # tasks because _are_dependencies_satisfied() checks that
            # design (hard) dependencies are DONE. Once the background
            # task completes, it marks design tasks DONE on the board
            # and workers' next request_next_task call will succeed.
            # GH-588: default to ``~/.marcus/projects/<project_id>`` when
            # the caller omits ``project_root`` so the design
            # auto-completion phase always runs. Skipping it leaves
            # design tasks in TODO and deadlocks every dependent
            # feature task.
            #
            # TODO(GH-589): decouple board-state design completion from
            # on-disk scaffold generation. ``_run_design_phase`` is
            # currently gated as a single unit on ``project_root``, but
            # marking design tasks DONE is pure board-state mutation and
            # does not need a filesystem root. Splitting these would
            # eliminate the need to default ``project_root`` here at all.
            project_root = _resolve_project_root(options, self.active_project_id)
            for task in safe_tasks:
                if _is_design_task(task):
                    task.assigned_to = "Marcus"

            _timer.mark("augmentation")

            # Create tasks on board using base class (this also triggers decomposition)
            with error_context(
                "kanban_task_creation",
                custom_context={
                    "project_name": project_name,
                    "task_count": len(safe_tasks),
                },
            ):
                created_tasks = await self.create_tasks_on_board(safe_tasks)
                logger.info(f"Created {len(created_tasks)} tasks on board")
                _timer.mark("kanban_persist")

                # Planning phase ends: board is populated, agents can now
                # self-select work.  Log JSONL event (debugging) and persist
                # to marcus.db so Cato renders a "Marcus Planning" swim lane
                # bar attributed to "Marcus", spanning setup duration.
                _planning_ended_at = datetime.now(timezone.utc)
                log_agent_event(
                    "planning_end",
                    {
                        "project_name": project_name,
                        "task_count": len(created_tasks),
                    },
                )
                try:
                    import uuid as _uuid_mod
                    from pathlib import Path

                    from src.core.persistence import SQLitePersistence

                    _marcus_root = Path(__file__).parent.parent.parent
                    _db_path = _marcus_root / "data" / "marcus.db"
                    _planning_persistence = SQLitePersistence(db_path=_db_path)

                    _planning_id = f"planning_{_uuid_mod.uuid4().hex[:12]}"
                    _planning_start_iso = _planning_started_at.isoformat()
                    _planning_end_iso = _planning_ended_at.isoformat()
                    _planning_hours = (
                        _planning_ended_at - _planning_started_at
                    ).total_seconds() / 3600
                    _planning_hours_val = _planning_hours

                    # Stage metadata write — executed after full success below.
                    _planning_metadata = {
                        "task_id": _planning_id,
                        "name": f"Marcus Planning: {project_name}",
                        "description": (
                            "LLM synthesis, task decomposition, and board "
                            "setup before agents begin parallel work."
                        ),
                        "priority": "high",
                        "estimated_hours": 0.0,
                        "labels": ["planning", "coordination"],
                        "dependencies": [],
                        "project_id": self.active_project_id,
                        "created_at": _planning_start_iso,
                    }
                    _planning_outcome_key = f"{_planning_id}_Marcus_{_planning_end_iso}"
                    _planning_outcome = {
                        "task_id": _planning_id,
                        "agent_id": "Marcus",
                        "task_name": f"Marcus Planning: {project_name}",
                        "estimated_hours": 0.0,
                        "actual_hours": _planning_hours,
                        # success/blockers filled in after full method completes
                        "started_at": _planning_start_iso,
                        "completed_at": _planning_end_iso,
                    }
                    # task_metadata is written now (it is independent of
                    # success/failure); task_outcomes is written after the
                    # full method completes so success reflects the real result.
                    await _planning_persistence.store(
                        "task_metadata",
                        _planning_id,
                        _planning_metadata,
                    )
                except Exception as _plan_err:
                    logger.warning(
                        f"Failed to stage planning phase metadata: {_plan_err}"
                    )

                # NOW create About task AFTER decomposition with real task IDs.
                # Map created tasks to original tasks to preserve details.
                # Task-name snapshotting for the cost dashboard happens in
                # the shared ``create_tasks_on_board`` (nlp_base.py) so
                # both this flow and the feature-adder flow get covered.
                tasks_with_real_ids = []
                for i, created in enumerate(created_tasks):
                    if i < len(safe_tasks):
                        original = safe_tasks[i]
                        task_with_id = Task(
                            id=created.id,  # Real Planka/Kanban ID
                            name=original.name,
                            description=original.description,
                            status=original.status,
                            priority=original.priority,
                            assigned_to=original.assigned_to,
                            created_at=original.created_at,
                            updated_at=original.updated_at,
                            due_date=original.due_date,
                            estimated_hours=original.estimated_hours,
                            dependencies=original.dependencies,
                            labels=original.labels,
                        )
                        tasks_with_real_ids.append(task_with_id)

                # Create About task with hierarchical subtask information
                about_kanban_task = None
                about_task = self._create_about_task(
                    description, project_name, tasks_with_real_ids
                )

                # Add About task to board at the beginning
                about_task_data = self.task_builder.build_task_data(about_task)
                about_kanban_task = await self.kanban_client.create_task(
                    about_task_data
                )
                logger.info(
                    f"Created 'About' task card with ID: {about_kanban_task.id}"
                )

                # Persist About task metadata and outcome to marcus.db
                # so Cato can see it (same pattern as nlp_base.py)
                try:
                    from pathlib import Path

                    from src.core.persistence import SQLitePersistence

                    marcus_root = Path(__file__).parent.parent.parent
                    db_path = marcus_root / "data" / "marcus.db"
                    persistence = SQLitePersistence(db_path=db_path)

                    about_id = about_kanban_task.id
                    if about_id:
                        now_iso = datetime.now(timezone.utc).isoformat()
                        await persistence.store(
                            "task_metadata",
                            str(about_id),
                            {
                                "task_id": str(about_id),
                                "name": about_task.name,
                                "description": about_task.description,
                                "priority": "low",
                                "estimated_hours": 0.0,
                                "labels": about_task.labels,
                                "dependencies": [],
                                "project_id": self.active_project_id,
                                "created_at": now_iso,
                            },
                        )
                        # About task is created as done, so add outcome
                        await persistence.store(
                            "task_outcomes",
                            f"{about_id}_system_{now_iso}",
                            {
                                "task_id": str(about_id),
                                "agent_id": "system",
                                "task_name": about_task.name,
                                "estimated_hours": 0.0,
                                "actual_hours": 0.0,
                                "success": True,
                                "blockers": [],
                                "started_at": now_iso,
                                "completed_at": now_iso,
                            },
                        )
                except Exception as about_log_err:
                    logger.warning(
                        f"Failed to persist About task metadata: " f"{about_log_err}"
                    )

                # Persist design task metadata and outcomes to marcus.db
                # so Cato can show them in Swim Lane (same pattern as About task)
                try:
                    for task_with_id in tasks_with_real_ids:
                        if not _is_design_task(task_with_id):
                            continue
                        if task_with_id.status != TaskStatus.DONE:
                            continue
                        design_id = str(task_with_id.id)
                        now_iso = datetime.now(timezone.utc).isoformat()
                        await persistence.store(
                            "task_metadata",
                            design_id,
                            {
                                "task_id": design_id,
                                "name": task_with_id.name,
                                "description": task_with_id.description,
                                "priority": getattr(task_with_id, "priority", "medium"),
                                "estimated_hours": getattr(
                                    task_with_id, "estimated_hours", 0.0
                                ),
                                "labels": getattr(task_with_id, "labels", []),
                                "dependencies": getattr(
                                    task_with_id, "dependencies", []
                                ),
                                "project_id": self.active_project_id,
                                "created_at": now_iso,
                            },
                        )
                        await persistence.store(
                            "task_outcomes",
                            f"{design_id}_Marcus_{now_iso}",
                            {
                                "task_id": design_id,
                                "agent_id": "Marcus",
                                "task_name": task_with_id.name,
                                "estimated_hours": getattr(
                                    task_with_id, "estimated_hours", 0.0
                                ),
                                "actual_hours": 0.0,
                                "success": True,
                                "blockers": [],
                                "started_at": now_iso,
                                "completed_at": now_iso,
                            },
                        )
                        logger.info(
                            f"Persisted design task outcome: "
                            f"{task_with_id.name} (id={design_id})"
                        )
                except Exception as design_log_err:
                    logger.warning(
                        f"Failed to persist design task metadata: " f"{design_log_err}"
                    )

                # Include About task in created list
                if about_kanban_task and hasattr(about_kanban_task, "id"):
                    created_tasks.append(about_kanban_task)

                # Log creation success rate
                success_rate = (
                    (len(created_tasks) / len(safe_tasks)) * 100 if safe_tasks else 0
                )
                logger.info(f"Task creation success rate: {success_rate:.1f}%")

            # Skip classification for dictionaries - just count them
            # created_tasks are dictionaries from kanban API, not Task objects
            task_breakdown = {"total": len(created_tasks)}

            # Add breakdown by original task types if available
            if safe_tasks:
                classified_original = self.classify_tasks(safe_tasks)
                for task_type, tasks in classified_original.items():
                    if tasks:
                        task_breakdown[task_type.value] = len(tasks)

            # Collect task IDs for backfill
            task_ids = []
            for ct in created_tasks:
                if isinstance(ct, dict):
                    tid = ct.get("id")
                elif hasattr(ct, "id"):
                    tid = ct.id
                else:
                    tid = None
                if tid:
                    task_ids.append(str(tid))

            try:
                schedule = calculate_optimal_agents(safe_tasks)
                recommended_agents = schedule.optimal_agents
            except Exception as _cpm_err:
                logger.warning(
                    f"[scheduling] CPM failed, omitting recommended_agents: "
                    f"{_cpm_err}"
                )
                recommended_agents = 0

            result = {
                "success": True,
                "project_name": project_name,
                "tasks_created": len(created_tasks),
                "task_ids": task_ids,
                "task_breakdown": task_breakdown,
                "phases": self._extract_phases(safe_tasks),
                "estimated_days": self._estimate_duration(safe_tasks),
                "dependencies_mapped": self._count_dependencies(safe_tasks),
                "risk_level": self._assess_risk_by_count(len(created_tasks)),
                "recommended_agents": recommended_agents,
                "confidence": 0.85,
                "created_at": datetime.now(timezone.utc).isoformat(),
                # Which decomposer ACTUALLY ran (Marcus #519). Diverges
                # from options["decomposer"] whenever contract_first
                # silently falls back to feature_based (no project_root,
                # weak contracts, empty domains, etc. — see fallback
                # paths in process_natural_language and
                # _try_contract_first_decomposition). The cost layer
                # prefers this over the requested value so
                # ``runs.decomposer`` reflects what produced the cost.
                "actual_decomposer": getattr(self, "_actual_decomposer", None),
                # Coarse project classification (#546 Phase 0).  Always
                # taxonomy-bucketed; "unknown" if the planner omitted it.
                "domain": getattr(self, "_project_domain", "unknown"),
                "structural_category": getattr(
                    self, "_project_structural_category", "unknown"
                ),
                # Detected tech labels (#546 Phase 0) — local-only.
                "detected_tech_stack": getattr(
                    self, "_project_detected_tech_stack", []
                ),
                # Planner intent-fidelity signals (#546 Phase 0).  None
                # when outcome coverage did not run.  Persisted to the
                # cost DB by the create_project wrapper after record_run.
                "intent_fidelity_score": getattr(
                    self, "_project_intent_fidelity_score", None
                ),
                "coverage_before_fill": getattr(
                    self, "_project_coverage_before_fill", None
                ),
                "coverage_after_fill": getattr(
                    self, "_project_coverage_after_fill", None
                ),
            }

            logger.info(f"Successfully created project with {len(created_tasks)} tasks")

            # Deferred planning-phase outcome write: project creation fully
            # succeeded, so record success=True. Writing here (not mid-method)
            # ensures the outcome reflects the real overall result (Codex P2).
            if (
                _planning_id is not None
                and _planning_persistence is not None
                and _planning_outcome_key is not None
                and _planning_outcome is not None
            ):
                try:
                    _planning_outcome["success"] = True
                    _planning_outcome["blockers"] = []
                    await _planning_persistence.store(  # type: ignore[attr-defined]
                        "task_outcomes",
                        _planning_outcome_key,
                        _planning_outcome,
                    )
                    logger.info(
                        f"[planning_observability] Persisted planning bar "
                        f"({_planning_hours_val * 60:.1f} min) to marcus.db"
                    )
                except Exception as _plan_write_err:
                    logger.warning(
                        f"Failed to persist planning outcome: {_plan_write_err}"
                    )

            # Phase A+B (background): Generate design artifacts,
            # register via MCP, mark design tasks DONE, generate
            # scaffold. Runs AFTER response is returned so Claude
            # doesn't timeout on long LLM calls. Workers are blocked
            # by hard dependencies until design tasks reach DONE
            # status on the kanban board.
            has_design_tasks = any(_is_design_task(t) for t in safe_tasks)
            if project_root and has_design_tasks:
                import asyncio as _aio

                # Contract-first Cato retrofit: when
                # ``_try_contract_first_decomposition`` synthesizes
                # design ghost tasks, it stashes the pre-generated
                # contract content on the creator instance. Pass it
                # through to ``_run_design_phase`` so Phase A is
                # skipped (artifacts already generated upstream) and
                # Phase B uses the stashed content directly. For the
                # feature-based path this attribute is absent and
                # ``pre_generated_content`` defaults to None, which
                # runs Phase A normally.
                pre_generated = getattr(self, "_contract_first_design_content", None)
                _aio.ensure_future(
                    _run_design_phase(
                        state=self.state,
                        kanban_client=self.kanban_client,
                        safe_tasks=safe_tasks,
                        created_tasks=created_tasks,
                        description=description,
                        project_name=project_name,
                        project_root=project_root,
                        pre_generated_content=pre_generated,
                    )
                )
                # Clear the stash so a subsequent feature-based
                # project on the same creator instance doesn't pick
                # up stale content. ``delattr`` matches the read
                # pattern (``getattr(..., None)``) without confusing
                # mypy about the attribute's type.
                if hasattr(self, "_contract_first_design_content"):
                    delattr(self, "_contract_first_design_content")
                # Same cleanup for stashed requirements (Codex P1
                # on PR #336: prevent stale requirements leaking
                # into subsequent feature-based runs).
                if hasattr(self, "_contract_first_requirements"):
                    delattr(self, "_contract_first_requirements")
                # Slice B (#523): same cleanup for stashed user outcomes.
                if hasattr(self, "_contract_first_user_outcomes"):
                    delattr(self, "_contract_first_user_outcomes")
                logger.info(
                    "[design_autocomplete] Phase A scheduled as " "background task"
                )

            # Run cleanup synchronously with a short timeout
            # This ensures resources are cleaned up without hanging
            import asyncio

            try:
                await asyncio.wait_for(self._cleanup_background(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Cleanup timed out after 5.0s, continuing anyway")

            _timer.mark("finalize")
            _timer.mark("total")
            logger.info(
                "create_project_from_description timing: "
                f"project_name={project_name!r} "
                f"task_count={len(safe_tasks) if 'safe_tasks' in locals() else 0} "
                f"total_ms={_timer.total_ms()} "
                f"phases={_timer.to_phase_durations()}"
            )
            return result

        except Exception as e:
            # Planning phase completed but project creation failed downstream.
            # Write task_outcomes with success=False so Cato shows the real
            # outcome instead of a ghost "success" bar (Codex P2 fix).
            if (
                _planning_id is not None
                and _planning_persistence is not None
                and _planning_outcome_key is not None
                and _planning_outcome is not None
            ):
                try:
                    _planning_outcome["success"] = False
                    _planning_outcome["blockers"] = [str(e)]
                    await _planning_persistence.store(  # type: ignore[attr-defined]
                        "task_outcomes",
                        _planning_outcome_key,
                        _planning_outcome,
                    )
                except Exception as _plan_fail_err:
                    logger.warning(
                        f"Failed to persist failed planning outcome: "
                        f"{_plan_fail_err}"
                    )

            from src.core.error_framework import MarcusBaseError
            from src.core.error_responses import handle_mcp_tool_error

            # If it's already a Marcus error, let it propagate properly
            if isinstance(e, MarcusBaseError):
                logger.error(f"Marcus error during project creation: {e}")
                # Return proper MCP error response
                return handle_mcp_tool_error(
                    e,
                    "create_project",
                    {
                        "description": description,
                        "project_name": project_name,
                        "options": options,
                    },
                )
            else:
                # Convert other exceptions to proper Marcus errors
                from src.core.error_framework import BusinessLogicError, ErrorContext

                unexpected_error = BusinessLogicError(
                    f"Unexpected error during project creation: {str(e)}",
                    context=ErrorContext(
                        operation="create_project",
                        integration_name="nlp_tools",
                        custom_context={"project_name": project_name},
                    ),
                )

                logger.error(f"Unexpected error creating project: {unexpected_error}")
                return handle_mcp_tool_error(
                    unexpected_error,
                    "create_project",
                    {
                        "description": description,
                        "project_name": project_name,
                        "options": options,
                    },
                )

    async def _cleanup_background(self) -> None:
        """Cleanup AI engine after response is sent."""
        try:
            # Only cleanup AI engine, skip task cancellation
            # Task cancellation was causing issues
            if hasattr(self.ai_engine, "cleanup"):
                try:
                    await self.ai_engine.cleanup()
                except Exception as cleanup_error:
                    logger.warning(f"AI engine cleanup failed: {cleanup_error}")
        except Exception as e:
            logger.warning(f"Background cleanup failed: {e}")

    def _build_constraints(
        self, options: Optional[Dict[str, Any]]
    ) -> ProjectConstraints:
        """Build project constraints from options."""
        if not options:
            return ProjectConstraints()

        # Get complexity and deployment options (backwards compatible)
        complexity = options.get("complexity", options.get("project_size", "standard"))
        deployment = options.get("deployment", options.get("deployment_target", "none"))

        # Map new complexity levels to appropriate defaults
        complexity_defaults = {
            "prototype": {"team_size": 1, "deployment_target": "local"},
            "standard": {"team_size": 3, "deployment_target": "dev"},
            "enterprise": {"team_size": 5, "deployment_target": "prod"},
            # Legacy mappings for backwards compatibility
            "mvp": {"team_size": 1, "deployment_target": "local"},
            "small": {"team_size": 2, "deployment_target": "local"},
            "medium": {"team_size": 3, "deployment_target": "dev"},
            "large": {"team_size": 5, "deployment_target": "prod"},
        }

        # Map new deployment options to legacy deployment_target values
        deployment_mapping = {
            "none": "local",
            "internal": "dev",
            "production": "prod",
            # Keep legacy values for backwards compatibility
            "local": "local",
            "dev": "dev",
            "prod": "prod",
            "remote": "prod",
        }

        defaults = complexity_defaults.get(complexity, complexity_defaults["standard"])
        mapped_deployment = deployment_mapping.get(deployment, "local")

        constraints = ProjectConstraints(
            team_size=options.get("team_size", defaults["team_size"]),
            available_skills=options.get("tech_stack", []),
            technology_constraints=options.get("tech_stack", []),
            deployment_target=mapped_deployment,
            complexity_mode=self.complexity,  # Pass explicit complexity mode
        )

        # Pass complexity info via quality_requirements for parser to use
        constraints.quality_requirements = {
            "project_size": complexity,  # Parser still uses project_size internally
            "complexity": (
                "simple" if complexity in ["prototype", "mvp"] else "moderate"
            ),
        }

        if "deadline" in options:
            try:
                constraints.deadline = datetime.fromisoformat(options["deadline"])
            except (ValueError, TypeError):
                # Invalid date format, use default (no deadline)
                pass  # nosec B110

        return constraints

    def _extract_phases(self, tasks: List[Task]) -> List[str]:
        """Extract project phases from tasks."""
        phases = set()
        for task in tasks:
            for label in task.labels:
                if label in [
                    "infrastructure",
                    "backend",
                    "frontend",
                    "testing",
                    "deployment",
                ]:
                    phases.add(label)
        return sorted(phases)

    def _estimate_duration(self, tasks: List[Task]) -> int:
        """Estimate project duration in days."""
        total_hours = sum(
            task.estimated_hours for task in tasks if task.estimated_hours
        )
        # Assume 8 hours per day, with some parallelization factor
        return int(total_hours / (8 * 2))  # 2 developers working in parallel

    def _count_dependencies(self, tasks: List[Task]) -> int:
        """Count total dependencies."""
        return sum(len(task.dependencies) for task in tasks)

    def _assess_risk(self, classified_tasks: Dict[TaskType, List[Task]]) -> str:
        """Assess project risk level."""
        # Create a list to avoid modification during iteration
        total_tasks = sum(len(tasks) for tasks in list(classified_tasks.values()))

        if total_tasks > 50:
            return "high"
        elif total_tasks > 20:
            return "medium"
        else:
            return "low"

    def _assess_risk_by_count(self, task_count: int) -> str:
        """Assess project risk level by task count."""
        if task_count > 50:
            return "high"
        elif task_count > 20:
            return "medium"
        else:
            return "low"

    def _create_about_task(
        self, description: str, project_name: str, tasks: List[Task]
    ) -> Task:
        """
        Create an 'About' task card that documents the project.

        Supports hierarchical formatting when subtasks are present.
        Tasks with subtasks show their children indented underneath.

        Parameters
        ----------
        description : str
            Original user description of the project
        project_name : str
            Name of the project
        tasks : List[Task]
            List of tasks generated for the project

        Returns
        -------
        Task
            About task card with project documentation
        """
        # Get subtask manager if available
        subtask_manager = getattr(self, "subtask_manager", None)

        # Format task list with hierarchical structure
        task_list_md = "## Generated Tasks\n\n"
        for idx, task in enumerate(tasks, 1):
            # Format parent/standalone task
            task_list_md += f"### {idx}. {task.name}\n"
            task_list_md += f"**Description:** {task.description}\n"
            task_list_md += f"**Estimated Hours:** {task.estimated_hours}\n"
            task_list_md += f"**Labels:** {', '.join(task.labels)}\n"

            # Add subtasks if they exist (using legacy storage since
            # we don't have project_tasks here)
            if subtask_manager and subtask_manager.has_subtasks(task.id, None):
                subtasks = subtask_manager.get_subtasks(task.id, None)
                if subtasks:
                    task_list_md += "\n**Subtasks:**\n"
                    for sub_idx, subtask in enumerate(subtasks, 1):
                        task_list_md += f"  {idx}.{sub_idx}. {subtask.name}\n"
                        task_list_md += f"     - {subtask.description}\n"
                        task_list_md += (
                            f"     - Estimated: {subtask.estimated_hours}h\n"
                        )

            task_list_md += "\n"

        # Create the About card description
        about_description = f"""# {project_name} - Project Overview

## Original Description

{description}

{task_list_md}

---
*This card provides an overview of the project and is not assignable to agents.*
"""

        # Create the About task
        about_task = Task(
            id="about_project",
            name=f"About: {project_name}",
            description=about_description,
            status=TaskStatus.DONE,  # Mark as completed
            priority=Priority.LOW,
            assigned_to=None,  # Not assignable
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=0,  # No time estimate
            dependencies=[],
            labels=["documentation"],  # Documentation label
            source_type="project_about",
            source_context={"project_name": project_name},
        )

        return about_task


class NaturalLanguageFeatureAdder(NaturalLanguageTaskCreator):
    """
    Handles adding features to existing projects using natural language.

    Refactored to use base class and eliminate code duplication.
    """

    def __init__(self, kanban_client: Any, ai_engine: Any, project_tasks: Any) -> None:
        super().__init__(kanban_client, ai_engine)
        self.project_tasks = project_tasks
        self.adaptive_mode = BasicAdaptiveMode()
        from src.modes.enricher.basic_enricher import BasicEnricher

        self.enricher = BasicEnricher()

    async def process_natural_language(
        self, description: str, integration_point: str = "auto_detect", **kwargs: Any
    ) -> List[Task]:
        """
        Process feature description into tasks.

        Implementation of abstract method from base class.
        """
        # Parse feature into tasks
        feature_tasks = await self._parse_feature_to_tasks(description)

        # Enrich the parsed tasks
        for i, task in enumerate(feature_tasks):
            feature_tasks[i] = self.enricher.enrich_task(task)

        # Detect integration points
        if integration_point == "auto_detect":
            integration_info = await self._detect_integration_points(
                feature_tasks, self.project_tasks
            )
        else:
            integration_info = {"tasks": [], "phase": integration_point}

        # Map dependencies to existing tasks
        for feature_task in feature_tasks:
            for integration_task_id in integration_info.get("tasks", []):
                if integration_task_id not in feature_task.dependencies:
                    feature_task.dependencies.append(integration_task_id)

        # Store integration info for later use
        self._integration_info = integration_info

        return feature_tasks

    async def add_feature_from_description(
        self, feature_description: str, integration_point: str = "auto_detect"
    ) -> Dict[str, Any]:
        """
        Add a feature to existing project from natural language.

        Uses the base class implementation for common functionality.
        """
        try:
            logger.info(f"Adding feature: {feature_description}")

            # Parse and process tasks
            tasks = await self.process_natural_language(
                feature_description, integration_point=integration_point
            )

            # Apply safety checks using base class
            safe_tasks = await self.apply_safety_checks(tasks)

            # Create tasks on board using base class
            created_tasks = await self.create_tasks_on_board(safe_tasks)

            # Skip classification for dictionaries - just count them
            # created_tasks are dictionaries from kanban API, not Task objects
            task_breakdown = {"total": len(created_tasks)}

            # Add breakdown by original task types if available
            if safe_tasks:
                classified_original = self.classify_tasks(safe_tasks)
                for task_type, tasks in classified_original.items():
                    if tasks:
                        task_breakdown[task_type.value] = len(tasks)

            result = {
                "success": True,
                "tasks_created": len(created_tasks),
                "task_breakdown": task_breakdown,
                "integration_points": self._integration_info.get("tasks", []),
                "integration_detected": integration_point == "auto_detect",
                "confidence": self._integration_info.get("confidence", 0.8),
                "feature_phase": self._integration_info.get("phase", "current"),
                "complexity": self._calculate_complexity(created_tasks),
            }

            logger.info(f"Successfully added feature with {len(created_tasks)} tasks")
            return result

        except Exception as e:
            logger.error(f"Error adding feature: {str(e)}")
            return {"success": False, "error": str(e)}

    async def _parse_feature_to_tasks(self, feature_description: str) -> List[Task]:
        """Parse feature description into tasks using AI."""
        try:
            # Use AI engine to analyze the feature request
            feature_analysis = await self.ai_engine.analyze_feature_request(
                feature_description
            )
        except Exception as e:
            logger.warning(f"AI analysis failed, using fallback: {str(e)}")
            feature_analysis = self._generate_fallback_tasks(feature_description)

        # Generate tasks based on analysis
        tasks = []
        task_id_counter = len(self.project_tasks) + 1

        for task_info in feature_analysis.get("required_tasks", []):
            task = Task(
                id=str(task_id_counter),
                name=task_info["name"],
                description=task_info.get("description", ""),
                status=TaskStatus.TODO,
                priority=(
                    Priority.HIGH if task_info.get("critical") else Priority.MEDIUM
                ),
                labels=task_info.get("labels", ["feature"]),
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                estimated_hours=task_info.get("estimated_hours", 8),
                dependencies=[],
                due_date=None,
            )
            tasks.append(task)
            task_id_counter += 1

        return tasks

    async def _detect_integration_points(
        self, feature_tasks: List[Task], existing_tasks: List[Task]
    ) -> Dict[str, Any]:
        """Detect where feature should integrate with existing project."""
        try:
            # Use AI engine to analyze integration points
            integration_analysis = await self.ai_engine.analyze_integration_points(
                feature_tasks, existing_tasks
            )
        except Exception as e:
            logger.warning(f"AI integration analysis failed, using fallback: {str(e)}")
            integration_analysis = self._analyze_integration_fallback(
                feature_tasks, existing_tasks
            )

        # Use AI-detected dependencies or fall back to label matching
        if "dependent_task_ids" in integration_analysis:
            integration_tasks = integration_analysis["dependent_task_ids"]
        else:
            # Use utility to find related tasks
            integration_tasks = []
            for existing_task in existing_tasks:
                related_features = self.safety_checker._find_related_tasks(
                    existing_task, feature_tasks
                )
                if related_features:
                    integration_tasks.append(existing_task.id)

        return {
            "tasks": integration_tasks,
            "phase": integration_analysis.get("suggested_phase", "current"),
            "confidence": integration_analysis.get("confidence", 0.8),
            "complexity": integration_analysis.get("integration_complexity", "medium"),
            "risks": integration_analysis.get("integration_risks", []),
        }

    def _calculate_complexity(self, tasks: List[Task]) -> str:
        """Calculate feature complexity based on tasks."""
        total_hours = sum(
            task.estimated_hours for task in tasks if task.estimated_hours
        )

        if total_hours > 40:
            return "high"
        elif total_hours > 20:
            return "medium"
        else:
            return "low"

    def _analyze_integration_fallback(
        self, feature_tasks: List[Task], existing_tasks: List[Task]
    ) -> Dict[str, Any]:
        """Analyze integration points without AI."""
        # Determine project phase based on existing tasks
        completed_tasks = [t for t in existing_tasks if t.status == TaskStatus.DONE]
        [t for t in existing_tasks if t.status == TaskStatus.IN_PROGRESS]

        # Classify existing tasks
        classified_existing = self.classify_tasks(existing_tasks)

        # Determine phase based on task types
        if classified_existing[TaskType.DEPLOYMENT]:
            phase = "post-deployment"
            confidence = 0.8
        elif classified_existing[TaskType.TESTING]:
            phase = "testing"
            confidence = 0.85
        elif classified_existing[TaskType.IMPLEMENTATION]:
            phase = "development"
            confidence = 0.85
        else:
            phase = "initial"
            confidence = 0.9

        return {
            "suggested_phase": phase,
            "confidence": confidence,
            "project_maturity": (
                len(completed_tasks) / len(existing_tasks) if existing_tasks else 0
            ),
        }

    def _generate_fallback_tasks(self, feature_description: str) -> Dict[str, Any]:
        """Generate intelligent fallback tasks based on feature description keywords."""
        feature_lower = feature_description.lower()
        tasks = []

        # Analyze feature type
        feature_types = {
            "api": ["api", "endpoint", "rest", "graphql"],
            "ui": ["ui", "interface", "screen", "page", "component", "frontend"],
            "auth": ["auth", "login", "user", "permission", "security"],
            "data": ["database", "model", "schema", "data", "storage"],
            "integration": ["integrate", "connect", "sync", "webhook"],
        }

        detected_types = []
        for ftype, keywords in list(feature_types.items()):
            if any(word in feature_lower for word in keywords):
                detected_types.append(ftype)

        # Always start with design/planning task
        tasks.append(
            {
                "name": f"Design {feature_description}",
                "description": (
                    f"Create technical design and plan for implementing "
                    f"{feature_description}"
                ),
                "estimated_hours": 4,
                "labels": ["feature", "design", "planning"],
                "critical": False,
            }
        )

        # Add type-specific tasks
        task_templates = {
            "data": {
                "name": f"Create database schema for {feature_description}",
                "description": "Design and implement database models and migrations",
                "estimated_hours": 6,
                "labels": ["feature", "database", "backend"],
                "critical": True,
            },
            "api": {
                "name": f"Implement backend for {feature_description}",
                "description": "Create backend services, APIs, and business logic",
                "estimated_hours": 12,
                "labels": ["feature", "backend", "api"],
                "critical": True,
            },
            "ui": {
                "name": f"Build UI components for {feature_description}",
                "description": "Create frontend components and user interface",
                "estimated_hours": 10,
                "labels": ["feature", "frontend", "ui"],
                "critical": True,
            },
            "auth": {
                "name": f"Implement security for {feature_description}",
                "description": (
                    "Add authentication, authorization, and security measures"
                ),
                "estimated_hours": 8,
                "labels": ["feature", "security", "auth"],
                "critical": True,
            },
            "integration": {
                "name": f"Build integration layer for {feature_description}",
                "description": "Implement integration points and data synchronization",
                "estimated_hours": 8,
                "labels": ["feature", "integration", "backend"],
                "critical": True,
            },
        }

        # Add tasks for detected types
        for dtype in detected_types:
            if dtype in task_templates:
                tasks.append(task_templates[dtype])

        # If no specific type detected, add generic implementation
        if not detected_types:
            tasks.append(task_templates["api"])  # Default to backend

        # Always add testing and documentation
        tasks.extend(
            [
                {
                    "name": f"Test {feature_description}",
                    "description": (
                        "Write unit tests, integration tests, and perform QA"
                    ),
                    "estimated_hours": 6,
                    "labels": ["feature", "testing", "qa"],
                    "critical": False,
                },
                {
                    "name": f"Document {feature_description}",
                    "description": "Create user documentation and API documentation",
                    "estimated_hours": 3,
                    "labels": ["feature", "documentation"],
                    "critical": False,
                },
            ]
        )

        return {"required_tasks": tasks}


# --- Design Task Auto-Completion (GH-297) ---
#
# Two-phase approach to prevent context contamination:
#
# Phase A (_generate_design_content): Runs BEFORE create_tasks_on_board.
#   Calls LLM, writes artifact files to disk, sets task status=DONE.
#   Design tasks hit the board already DONE — no agent can grab them.
#
# Phase B (_register_design_via_mcp): Runs AFTER refresh_project_state.
#   Registers artifacts + decisions through MCP tools (log_artifact,
#   log_decision) so state.task_artifacts and state.context.decisions
#   are populated for get_task_context. Workers discover everything.

# Each artifact is generated by a separate LLM call, just like an
# agent would create separate documents via separate log_artifact calls.
# This avoids the giant-JSON-blob problem that caused truncation failures.

_ARTIFACT_PROMPT = """\
You are a senior software architect working on: {project_name}

## Project Description (for context only)
{project_description}
{sibling_domains_block}
## Your Design Task
{task_description}

## Your Current Assignment
Generate the {artifact_label} document for the **{domain_name}** domain \
specifically.

## CRITICAL SCOPE CONSTRAINT (GH-320)

This document describes ONLY the **{domain_name}** domain. You are one of \
several architects producing contracts for this project, each scoped to \
a single domain. The sibling domains section above names the OTHER \
architects' domains explicitly — any field, type, or interface owned by \
a sibling domain MUST NOT appear in this document, because that sibling \
architect will produce their own authoritative definition for it and the \
two definitions will contradict each other at integration time.

**What to include**: fields, types, identifiers, configuration values, \
API endpoints, and interfaces OWNED BY the {domain_name} domain. Things \
the {domain_name} module produces or stores internally.

**What to exclude**: fields, types, or interfaces owned by other domains. \
If the {domain_name} domain must interact with another domain's data, \
reference that data BY NAME ONLY and point to the other domain's contract \
file. Never redefine another domain's fields, never guess at their types, \
never include their interface definitions in this file.

**Good cross-domain reference** (blockquote — each line starts with `>`):
> "When a user selects a timezone, the {domain_name} domain receives a
> TimezoneIdentifier from the TimeWidget domain (see time-widget-system
> contracts for its shape)."

**Bad cross-domain leak**:
> "TimezoneIdentifier (string, IANA format, e.g., 'America/New_York') —
> defined by TimeWidget domain."
> (Do NOT redefine another domain's types. Refer to them by name only.)

**Test before you write each field**: "Is this field owned by the \
{domain_name} domain, or by another domain?" Only describe fields where \
the answer is **{domain_name}**.

## Content Guidelines

Describe WHAT each component does and HOW components connect to each \
other. Focus on behavior, responsibilities, data flow, and integration \
boundaries.

Do NOT specify file names, function signatures, prop interfaces, class \
names, or internal implementation details. The implementing developer \
decides those. Your job is to define the WHAT and WHY, not the HOW.

However, you MUST be concrete and specific about any identifier, name, \
or value that the {domain_name} domain owns and that will be shared \
across module boundaries — field names in data models, storage keys, \
event names, environment variable names, port numbers, API response \
shapes, and status/enum values. When multiple modules must agree on a \
name or value to interoperate, that name or value is an interface \
contract, not an implementation detail. State it explicitly — **but \
only if the {domain_name} domain is the owner/producer of that \
identifier**.

Good: "The time display updates every second using the browser's \
Date API and supports timezone conversion."
Bad: "TimeWidget (src/components/TimeWidget.tsx) takes props \
timeFormat: '24h' | '12h' and uses setInterval(1000)."

Good: "The todo entity fields are: id (string), title (string), \
description (string|null), completed (boolean), created_at \
(ISO 8601 timestamp). All modules that produce or consume todo \
data must use these exact field names."
Good: "Auth tokens are stored under the key `auth_token`. Both \
the auth module and any module making authenticated requests must \
use this key."
Good: "The API server listens on port 3001 (configurable via \
PORT environment variable)."

Respond with ONLY the document content in markdown format. \
No JSON wrapping, no code fences around the whole response. \
Just the markdown document starting with a # heading.
"""

_DECISIONS_PROMPT = """\
You are a senior software architect working on: {project_name}

## Project Description (for context only)
{project_description}
{sibling_domains_block}
## Design Task
{task_description}

## Your Current Assignment
List the key architectural decisions for the **{domain_name}** domain \
specifically. Focus on technology choices, patterns, and boundaries \
that the {domain_name} domain is responsible for — not implementation \
details like file names or function signatures.

## SCOPE CONSTRAINT (GH-320)

Only list decisions owned by the **{domain_name}** domain. The sibling \
domains section above names the OTHER architects' domains — decisions \
they own (technology choices, patterns, data shapes for those domains) \
belong in their decisions lists, not yours. Cross-domain decisions \
(e.g. "the app uses React") belong in a project-wide document. Include \
a cross-cutting decision only if the **{domain_name}** domain is the \
primary driver.

Good: "Use browser Date API for time, not a library — reduces bundle size."
Bad: "Use setInterval(1000) in useCurrentTime.ts hook."
Bad (out of scope): "Use OpenWeatherMap API" (if this isn't the weather domain)

Respond with ONLY a JSON array (no wrapping object, no markdown fences):
[{{"what":"Chose X over Y","why":"Because of Z","impact":"Affects A and B"}}]
"""

# Standard artifacts a design task produces, matching what the task
# description requests in advanced_parser.py:912-927
_DESIGN_ARTIFACT_SPECS = [
    {
        "artifact_type": "architecture",
        "artifact_role": "design_guide",
        "label": "architecture",
        "filename_template": "{domain_slug}-architecture.md",
        "description_template": ("Component boundaries and data flows for {domain}"),
    },
    {
        "artifact_type": "api",
        "artifact_role": "interface_contract",
        "label": "API contracts",
        "filename_template": "{domain_slug}-api-contracts.md",
        "description_template": (
            "Endpoint definitions and request/response schemas " "for {domain}"
        ),
    },
    {
        "artifact_type": "specification",
        "artifact_role": "implementation_spec",
        "label": "data models",
        "filename_template": "{domain_slug}-data-models.md",
        "description_template": (
            "Database schemas and entity relationships for {domain}"
        ),
    },
    {
        "artifact_type": "specification",
        "artifact_role": "interface_contract",
        "label": "interface contracts",
        "filename_template": "{domain_slug}-interface-contracts.md",
        "description_template": (
            "Shared identifiers and values that must be consistent "
            "across all modules in {domain}"
        ),
    },
]

_INTERFACE_CONTRACTS_PROMPT = """\
You are a senior software architect working on: {project_name}

## Project Description (for context only)
{project_description}
{sibling_domains_block}
## Your Design Task
{task_description}

## Your Current Assignment
Generate the interface contracts document for the **{domain_name}** \
domain specifically.

## CRITICAL SCOPE CONSTRAINT (GH-320)

This document defines interface contracts OWNED BY the **{domain_name}** \
domain. You are one of several architects producing contracts for this \
project — each scoped to a single domain. The sibling domains section \
above names the OTHER architects' domains explicitly — any identifier, \
key, type, or shape owned by a sibling domain MUST NOT be redefined \
here, because that sibling architect will produce their own \
authoritative definition and the two definitions will contradict each \
other at integration time, breaking cross-agent coordination.

**Include**: identifiers, keys, types, and shapes that the \
{domain_name} domain produces, stores internally, or exposes as its \
public surface to other domains.

**Exclude**: identifiers, keys, types, or shapes owned by other \
domains. If the {domain_name} domain must consume data from another \
domain, reference it by name only and point to the other domain's \
contract file.

## FORBIDDEN PATTERNS (must not appear in this document)

Each of these is a scope leak. If you write any of them, stop and \
delete. They are what caused the GH-320 scope bug.

1. **Tables that include fields owned by other domains.** Do not
   write a "summary of shared boundaries" table, a "module boundary
   matrix", or any other tabular structure that lists fields from
   multiple domains. Your table rows describe ONLY {domain_name}-owned
   fields. Other domains' fields belong in their own files.

2. **Re-declaring another domain's field with type information.**
   If the {domain_name} domain reads a field called `foo` owned by
   the Bar domain, DO NOT write `foo (string)` or `foo: number`
   anywhere in this document. The correct pattern is:
   "reads a `foo` value from the Bar domain (see
   bar-system-interface-contracts.md for its authoritative type
   definition)."

3. **Describing another domain's props interface.** If Dashboard
   passes props to {domain_name}, describe the props Dashboard
   hands TO you (those ARE your interface). Do NOT describe the
   props Dashboard passes to OTHER domains.

4. **"For reference" or "for clarity" type definitions of external
   fields.** Do not write things like "for reference, here are the
   time fields this widget consumes: currentTime (string),
   timezone (string)...". That is a redefinition. Link to the
   other domain's file instead.

## EXAMPLES

**Good cross-domain reference** (three-line blockquote, each line
starts with `>`):
> "The {domain_name} domain receives a `TimeEntity` as input from
> the TimeWidget domain. See `time-widget-system-interface-contracts.md`
> for the authoritative definition of TimeEntity fields and types."

**Bad (forbidden) cross-domain leak**:
> "TimeEntity fields:
>   - currentTime (ISO 8601 string) — current time
>   - timezone (string) — IANA timezone identifier"
> (Do NOT redefine TimeEntity here if {domain_name} is not TimeWidget.)

**Bad (forbidden) summary table**:
> | Field | Owner | Consumer | Type |
> |-------|-------|----------|------|
> | lastUpdated | WeatherWidget | Dashboard | ISO 8601 string |
> (Do NOT put another domain's fields in your tables, even as rows.)

**Self-check before you write each field**: "Is this field produced \
or owned by the **{domain_name}** domain?" If not, reference it by \
name only and stop. If you catch yourself writing a table that \
includes multiple domains' fields, delete the rows that aren't \
{domain_name}'s.

## Content Guidelines

Interface contracts define the EXACT identifiers, names, values, and \
shapes that multiple modules must agree on to interoperate. These are \
NOT implementation details — they are coordination constraints. Each \
implementing agent independently decides HOW to build their module, \
but they MUST use these exact names and shapes at module boundaries.

List every shared boundary OWNED BY this domain explicitly. For each \
one, specify:
- The exact identifier/key/name that must be used
- The data type or shape
- Which modules produce it and which consume it

Categories to cover (restricted to things the {domain_name} domain \
owns):

### Data Entity Fields
For every shared data entity (user, todo, session, etc.), list the \
exact field names and types that all modules must use when producing \
or consuming that entity. Example:
- `todo.id` (string) — unique identifier
- `todo.title` (string) — display title
- `todo.completed` (boolean) — completion status

### Storage Keys
For any value stored in a shared medium (localStorage, cookies, \
environment variables, database, cache, message queue), specify \
the exact key. Example:
- Auth token stored under key: `auth_token`
- User session stored under key: `session_id`

### Configuration Values
For any value referenced by multiple modules (ports, hostnames, \
base URLs, timeouts), specify the canonical value and how to \
override it. Example:
- API server port: `3001` (override via `PORT` env var)
- API base URL: `/api`

### API Response Shapes
For every endpoint that returns data consumed by another module, \
specify the exact response structure. Example:
- `GET /api/todos` returns: `{{ "status": "success", "data": {{ \
"todos": [...], "total": number, "limit": number, "offset": number }} }}`

### Status/Enum Values
For any status field, category, or enum used across modules, \
specify the exact valid values. Example:
- Todo status filter values: `all`, `active`, `completed`

Respond with ONLY the document content in markdown format. \
No JSON wrapping, no code fences around the whole response. \
Just the markdown document starting with a # heading.
"""


def _build_sibling_domains_block(
    current_domain: str, all_domains: Dict[str, str]
) -> str:
    """Render the Sibling Domains block injected into contract prompts.

    When the contract generator is called for a single domain, the LLM
    is told not to leak other domains' fields — but without knowing
    WHO the other domains are, the instruction has no referent and
    the LLM silently redefines fields it thinks "nobody else will
    cover." Naming the sibling domains explicitly gives the
    instruction concrete force: the LLM can now answer "does this
    field belong to a sibling?" before writing it.

    Root cause of the GH-320 cross-file type contradiction bug: the
    prompts said "stay in your lane" without saying which lanes
    existed. Fixed in the Option A prompt clamp (2026-04-13).

    Parameters
    ----------
    current_domain : str
        The domain being generated right now. Excluded from the
        sibling list — the LLM obviously owns its own fields.
    all_domains : Dict[str, str]
        Map of ``{domain_name: domain_description}`` for every
        domain in the project. Typically the project's full
        PRDAnalysis.domains dict (contract-first path) or the
        derived ``{task_domain_name: task.description}`` map
        (feature-based path).

    Returns
    -------
    str
        Rendered markdown block ready to substitute into a prompt
        ``{sibling_domains_block}`` placeholder. Empty string when
        there are no siblings (single-domain project) — callers
        substitute an empty value and the prompt reads cleanly
        without a stray heading.
    """
    siblings = {
        name: desc for name, desc in all_domains.items() if name != current_domain
    }
    if not siblings:
        return ""

    lines = [
        "",
        "## Sibling Domains (DO NOT define fields owned by these)",
        "",
        "These OTHER domains are being designed in parallel by OTHER",
        "architects. Any field, type, key, or interface owned by a",
        "sibling domain belongs in their contract file, NOT yours.",
        "Reference them by name only — never redefine their types.",
        "",
    ]
    for name, desc in siblings.items():
        # Defensive coercion before whitespace normalization.
        # Upstream callers may pass ``Task.description`` values
        # that are ``None`` or non-string — the Task model
        # tolerates it, and the feature-based path in
        # ``_generate_design_content`` stores raw ``task.description``
        # into ``all_domains`` without validating the type. Before
        # this helper existed, non-string descriptions were silently
        # tolerated because they went straight into f-string
        # formatting. Calling ``desc.split()`` on ``None`` would
        # raise ``AttributeError`` mid-iteration and take down the
        # entire fail-fast design-generation batch (@chatgpt-codex
        # P2 on PR #344).
        desc_str = str(desc) if desc is not None else ""
        # Collapse the description to a single line of <=100 chars so
        # the sibling list stays scannable. The LLM only needs enough
        # context to recognize the domain's scope, not its full spec.
        short = " ".join(desc_str.split())
        if len(short) > 100:
            short = short[:97] + "..."
        lines.append(f"- **{name}**: {short}")
    lines.extend(
        [
            "",
            'BEFORE WRITING ANY FIELD, ASK: "Does this field belong to',
            'any sibling domain above?" If yes, STOP — do not define',
            "its type here. Reference the sibling's contract file by",
            "name instead. The smoke-test Invariant 5 will catch",
            "cross-file type contradictions and fail the build.",
            "",
        ]
    )
    return "\n".join(lines)


def _is_design_task(task: Any) -> bool:
    """Check if a task is a bundled design task."""
    labels = getattr(task, "labels", []) or []
    name = getattr(task, "name", "")
    return "design" in labels and name.lower().startswith("design")


def _domain_slug(task_name: str) -> str:
    """Extract a slug from a design task name like 'Design Authentication'."""
    name = task_name.lower()
    if name.startswith("design "):
        name = name[7:]
    return name.strip().replace(" ", "-")


async def _generate_single_artifact(
    llm: Any,
    spec: Dict[str, Any],
    domain_name: str,
    domain_description: str,
    project_name: str,
    project_description: str,
    project_root_path: Any,
    context: Any,
    semaphore: asyncio.Semaphore,
    all_domains: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """Generate one design artifact document via a single bounded LLM call.

    Builds the artifact prompt from the domain description, calls the LLM
    under the concurrency cap (see :func:`_bounded_llm_analyze`), writes
    the response to disk under ``project_root_path``, and returns the
    artifact metadata dict for later registration in Phase B.

    When ``all_domains`` is provided, the prompt is rendered with a
    Sibling Domains block that names the OTHER domains being designed
    in parallel. This is the GH-320 Option A scope clamp — it gives
    the LLM concrete referents for "don't leak other domains' fields"
    so it stops redefining fields it thinks nobody else is covering.
    When ``all_domains`` is None or has only the current domain, the
    block renders empty and the prompt reads cleanly.

    Returns ``None`` (and logs a warning) when the LLM response is empty
    or shorter than 20 characters — the caller treats that as "no
    artifact produced for this spec" without aborting the domain.

    This helper is domain-keyed, not task-keyed, so it can be called
    both from the feature-based path (where the caller derives
    ``domain_name`` / ``domain_description`` from a Task object via
    :func:`_generate_design_content`) and from the contract-first path
    (where the caller has a ``PRDAnalysis`` domains dict and never
    materializes Task objects — see GH-320 PR 1).

    Parameters
    ----------
    llm : Any
        LLM client instance.
    spec : Dict[str, Any]
        One entry from ``_DESIGN_ARTIFACT_SPECS`` describing the
        filename template, artifact type, and prompt label.
    domain_name : str
        Human-readable domain name without the ``"Design "`` prefix
        (e.g. ``"Authentication"``, not ``"Design Authentication"``).
        Used for the filename slug and the artifact description.
    domain_description : str
        Detailed description of the domain for the LLM prompt. Same
        shape as the task description generated by
        ``_create_bundled_design_tasks``.
    project_name : str
        Project name for prompt templating.
    project_description : str
        Full project description for prompt templating.
    project_root_path : pathlib.Path
        Absolute path to the project implementation directory.
    context : Any
        Context object passed to ``llm.analyze``.
    semaphore : asyncio.Semaphore
        Concurrency guard shared across all domain coroutines in the
        current invocation.

    Returns
    -------
    Optional[Dict[str, Any]]
        Artifact metadata dict with keys ``filename``, ``artifact_type``,
        ``content``, ``description``, ``relative_path`` — or ``None`` if
        the LLM returned an empty/short response.

    Raises
    ------
    Exception
        Propagates any unrecoverable LLM error after ``_bounded_llm_analyze``
        exhausts its retries. Also propagates disk I/O failures.
    """
    from pathlib import Path

    from src.marcus_mcp.tools.attachment import ARTIFACT_PATHS

    # Defensive validation of domain_name before it goes into a
    # ``str.format()`` template (PR #330 review P1). The domain
    # name comes from Marcus's domain discovery (LLM-generated or
    # PRD parser output) and flows into a templated prompt. If it
    # ever contains ``{`` or ``}`` characters, ``.format()`` would
    # either raise ``KeyError`` mid-request or silently consume the
    # curly braces and produce a malformed prompt. Block both
    # cases at the call site with a clear error message.
    if not domain_name or not domain_name.strip():
        raise ValueError(
            "_generate_single_artifact: domain_name must be a " "non-empty string"
        )
    if "{" in domain_name or "}" in domain_name:
        raise ValueError(
            f"_generate_single_artifact: domain_name must not contain "
            f"'{{' or '}}' characters (would corrupt format template); "
            f"got: {domain_name!r}"
        )

    domain = _domain_slug(domain_name)
    fname = spec["filename_template"].format(domain_slug=domain)
    desc = spec["description_template"].format(domain=domain_name)

    # Build the Sibling Domains block (GH-320 Option A). When the
    # caller provides the full ``all_domains`` map, the block names
    # every OTHER domain explicitly so the LLM has concrete referents
    # for the "stay in your lane" instruction. When no map is
    # provided or the project has only one domain, the block is
    # empty and the prompt renders normally.
    sibling_domains_block = (
        _build_sibling_domains_block(domain_name, all_domains) if all_domains else ""
    )

    if spec["label"] == "interface contracts":
        prompt = _INTERFACE_CONTRACTS_PROMPT.format(
            project_name=project_name,
            project_description=project_description,
            task_description=domain_description,
            domain_name=domain_name,
            sibling_domains_block=sibling_domains_block,
        )
    else:
        prompt = _ARTIFACT_PROMPT.format(
            project_name=project_name,
            project_description=project_description,
            task_description=domain_description,
            artifact_label=spec["label"],
            domain_name=domain_name,
            sibling_domains_block=sibling_domains_block,
        )

    response = await _bounded_llm_analyze(
        llm, prompt, context, semaphore, operation="generate_design_artifact"
    )

    if not response or len(response.strip()) < 20:
        logger.warning(
            f"[design_autocomplete] Phase A: "
            f"empty/short response for "
            f"'{domain_name}' {spec['label']}"
        )
        return None

    # Write document to disk
    atype = spec["artifact_type"]
    base = ARTIFACT_PATHS.get(atype, "docs/artifacts")
    rel_path = Path(base) / fname
    full_path = project_root_path / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(response.strip(), encoding="utf-8")

    logger.info(f"[design_autocomplete] Phase A: wrote {rel_path}")

    return {
        "filename": fname,
        "artifact_type": atype,
        "content": response.strip(),
        "description": desc,
        "relative_path": str(rel_path),
        "artifact_role": spec.get("artifact_role"),
    }


async def _generate_single_decisions(
    llm: Any,
    domain_name: str,
    domain_description: str,
    project_name: str,
    project_description: str,
    context: Any,
    semaphore: asyncio.Semaphore,
    all_domains: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Generate the decisions list for one domain via one LLM call.

    The decisions prompt is self-contained: it depends only on the
    domain description and project metadata, not on the artifact
    outputs, which is why it can run in parallel with the artifact
    calls (see GH-304).

    Domain-keyed like :func:`_generate_single_artifact` so the
    contract-first decomposition path (GH-320 PR 2) can call it
    directly with a ``PRDAnalysis`` domains dict, without needing
    Task objects.

    Parameters
    ----------
    llm : Any
        LLM client instance.
    domain_name : str
        Human-readable domain name, used only for log messages.
    domain_description : str
        Detailed description of the domain for the LLM prompt.
    project_name : str
        Project name for prompt templating.
    project_description : str
        Full project description for prompt templating.
    context : Any
        Context object passed to ``llm.analyze``.
    semaphore : asyncio.Semaphore
        Shared concurrency guard for the current invocation.

    Returns
    -------
    List[Dict[str, Any]]
        List of decision dicts, each with at least ``what``, ``why``,
        and ``impact`` keys. Empty list if the response could not be
        parsed as JSON (a warning is logged in that case).

    Raises
    ------
    Exception
        Propagates any unrecoverable LLM error after retries exhaust.
    """
    import json

    # Same defensive validation as _generate_single_artifact
    # (PR #330 review P1). domain_name must be a non-empty string
    # with no ``{`` or ``}`` characters or the format template
    # silently corrupts.
    if not domain_name or not domain_name.strip():
        raise ValueError(
            "_generate_single_decisions: domain_name must be a " "non-empty string"
        )
    if "{" in domain_name or "}" in domain_name:
        raise ValueError(
            f"_generate_single_decisions: domain_name must not contain "
            f"'{{' or '}}' characters (would corrupt format template); "
            f"got: {domain_name!r}"
        )

    # Build the Sibling Domains block for the decisions prompt too,
    # so cross-domain decisions are routed to the domain that owns
    # them (GH-320 Option A).
    sibling_domains_block = (
        _build_sibling_domains_block(domain_name, all_domains) if all_domains else ""
    )

    dec_prompt = _DECISIONS_PROMPT.format(
        project_name=project_name,
        project_description=project_description,
        task_description=domain_description,
        domain_name=domain_name,
        sibling_domains_block=sibling_domains_block,
    )

    dec_response = await _bounded_llm_analyze(
        llm, dec_prompt, context, semaphore, operation="generate_design_decisions"
    )

    if not dec_response:
        return []

    try:
        from src.utils.json_parser import clean_json_response

        cleaned = clean_json_response(dec_response)
        parsed = json.loads(cleaned)
        # Response could be a list or {"decisions": [...]}
        if isinstance(parsed, list):
            dec_list = parsed
        elif isinstance(parsed, dict):
            dec_list = parsed.get("decisions", [])
        else:
            dec_list = []

        # Guard against malformed list elements (mixed types from the LLM:
        # strings, numbers, None) BEFORE checking for required keys.
        # Without the isinstance check, ``"what" in d`` raises TypeError on
        # non-iterables (or matches a substring on a string), which under
        # the GH-304 fail-fast gather would abort the entire design phase
        # for a recoverable formatting issue. See PR #319 Codex review.
        logged_decisions: List[Dict[str, Any]] = [
            d
            for d in dec_list
            if isinstance(d, dict) and all(k in d for k in ("what", "why", "impact"))
        ]

        logger.info(
            f"[design_autocomplete] Phase A: "
            f"{len(logged_decisions)} decision(s) "
            f"for '{domain_name}'"
        )
        return logged_decisions

    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(
            f"[design_autocomplete] Phase A: "
            f"could not parse decisions for "
            f"'{domain_name}': {e}"
        )
        return []


async def _process_design_domain(
    llm: Any,
    domain_name: str,
    domain_description: str,
    project_name: str,
    project_description: str,
    project_root_path: Any,
    context: Any,
    semaphore: asyncio.Semaphore,
    all_domains: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """Run all LLM calls for one domain concurrently (Level 2).

    Kicks off 4 artifact coroutines plus 1 decisions coroutine under a
    single ``asyncio.gather``, so all 5 LLM calls for a domain run in
    parallel, capped by the shared semaphore. Returns ``None`` when no
    artifacts were produced (the corresponding design task — if any —
    should stay TODO in that case).

    This helper is domain-keyed so it can be invoked both from the
    task-centric ``_generate_design_content`` (which converts tasks to
    domains before delegating here) and from the contract-first
    decomposition path (GH-320 PR 2) which has domain information from
    the PRD analysis before any tasks exist.

    Parameters
    ----------
    llm : Any
        LLM client instance.
    domain_name : str
        Human-readable domain name without the ``"Design "`` prefix.
    domain_description : str
        Detailed description of the domain for the LLM prompts.
    project_name : str
        Project name for prompt templating.
    project_description : str
        Full project description for prompt templating.
    project_root_path : pathlib.Path
        Absolute path to the project implementation directory.
    context : Any
        Context object passed through to each LLM call.
    semaphore : asyncio.Semaphore
        Shared concurrency guard across all domains.

    Returns
    -------
    Optional[Dict[str, Any]]
        ``{"artifacts": [...], "decisions": [...]}`` on success, or
        ``None`` if no artifacts survived the empty/short filter.

    Raises
    ------
    Exception
        Propagates the first unrecoverable LLM error (after retries)
        from any of the 5 parallel calls. Fail-fast: the caller aborts
        the whole design phase. See GH-304 decision rationale.
    """
    artifact_coros = [
        _generate_single_artifact(
            llm=llm,
            spec=spec,
            domain_name=domain_name,
            domain_description=domain_description,
            project_name=project_name,
            project_description=project_description,
            project_root_path=project_root_path,
            context=context,
            semaphore=semaphore,
            all_domains=all_domains,
        )
        for spec in _DESIGN_ARTIFACT_SPECS
    ]
    decisions_coro = _generate_single_decisions(
        llm=llm,
        domain_name=domain_name,
        domain_description=domain_description,
        project_name=project_name,
        project_description=project_description,
        context=context,
        semaphore=semaphore,
        all_domains=all_domains,
    )

    # Level 2 parallelism: 4 artifact calls + 1 decisions call all in flight
    # at once for this domain. The nested ``asyncio.gather`` shape lets mypy
    # infer the two result slots (list of artifacts, list of decisions)
    # without a ``cast``. gather() propagates the first exception (after
    # retries exhaust) and cancels the remaining coroutines.
    artifact_results, decisions = await asyncio.gather(
        asyncio.gather(*artifact_coros),
        decisions_coro,
    )

    written_artifacts: List[Dict[str, Any]] = [
        a for a in artifact_results if a is not None
    ]

    if not written_artifacts:
        logger.warning(
            f"[design_autocomplete] Phase A: no "
            f"artifacts for '{domain_name}' — "
            f"stays TODO"
        )
        return None

    n_a = len(written_artifacts)
    n_d = len(decisions)
    logger.info(
        f"[design_autocomplete] Phase A: "
        f"'{domain_name}' → {n_a} artifact(s), "
        f"{n_d} decision(s)"
    )

    return {"artifacts": written_artifacts, "decisions": decisions}


async def _generate_contracts_by_domain(
    domains: Dict[str, str],
    project_description: str,
    project_name: str,
    project_root: str,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Generate contract artifacts for each domain in parallel.

    Standalone, task-free entry point for the Phase A design content
    generation. Takes a ``{domain_name: domain_description}`` mapping
    and produces one set of contract artifacts (architecture, API
    contracts, data models, interface contracts) plus a decisions log
    per domain.

    This is the domain-keyed sibling of :func:`_generate_design_content`:

    - :func:`_generate_design_content` starts from a list of Task
      objects, filters the design tasks, derives domains from their
      names/descriptions, and mutates the task list in-place to mark
      design tasks DONE on success (the feature-based path).
    - :func:`_generate_contracts_by_domain` starts from a domains dict
      directly and returns results keyed by domain name. It does not
      know about tasks and does not mutate any task state. This is
      what the contract-first decomposition path (GH-320 PR 2) will
      call once it has discovered domains from the PRD analysis but
      before any tasks exist.

    Both paths share the same inner Level 1 / Level 2 parallelism,
    the same ``Semaphore(10)`` concurrency cap, and the same
    ``@with_retry`` retry layer via the shared helpers
    :func:`_process_design_domain`, :func:`_generate_single_artifact`,
    and :func:`_generate_single_decisions`.

    Parameters
    ----------
    domains : Dict[str, str]
        Mapping of domain name -> detailed description. Domain names
        should NOT include a ``"Design "`` prefix — e.g. use
        ``"Authentication"``, not ``"Design Authentication"``. The
        descriptions are passed verbatim to the LLM prompts and
        should contain enough context for the model to produce a
        coherent contract.
    project_description : str
        Full project description for LLM context.
    project_name : str
        Project name.
    project_root : str
        Absolute path to project implementation directory. Artifact
        files are written under ``{project_root}/docs/...``.

    Returns
    -------
    Dict[str, Optional[Dict[str, Any]]]
        Mapping of domain name -> ``{"artifacts": [...], "decisions":
        [...]}`` on success, or ``None`` for domains where no artifacts
        were produced (the empty/short response path in
        :func:`_generate_single_artifact`). Domains that produced at
        least one artifact are non-None.

    Raises
    ------
    Exception
        Any unrecoverable LLM or I/O error from the parallel contract
        generation (fail-fast semantics — see GH-304). Disk side
        effects (partial artifact files under ``project_root``) may
        still be present on failure — this matches the
        :func:`_generate_design_content` behavior.

    See Also
    --------
    GH-297 : Phase A / Phase B design autocomplete design.
    GH-304 : Parallelization decision.
    GH-320 : Contract-first task decomposition (consumer in PR 2).
    """
    from pathlib import Path

    from src.ai.providers.llm_abstraction import LLMAbstraction

    project_root_path = Path(project_root)

    if not domains:
        return {}

    logger.info(
        f"[design_autocomplete] Phase A: generating contracts "
        f"for {len(domains)} domain(s) "
        f"(concurrency cap={_DESIGN_LLM_CONCURRENCY})"
    )

    llm = LLMAbstraction()

    class _Ctx:
        max_tokens = 4000

    # Create the semaphore inside the function so it binds to the
    # currently running event loop. See the comment in
    # :func:`_generate_design_content` for the rationale.
    semaphore = asyncio.Semaphore(_DESIGN_LLM_CONCURRENCY)

    domain_items = list(domains.items())

    # Pass the full domains map into each coroutine so every
    # domain's contract generator can render a Sibling Domains
    # block naming the OTHER domains (GH-320 Option A scope clamp).
    # Each call still receives its own domain_name, so
    # _build_sibling_domains_block filters itself out.
    domain_coros = [
        _process_design_domain(
            llm=llm,
            domain_name=domain_name,
            domain_description=domain_description,
            project_name=project_name,
            project_description=project_description,
            project_root_path=project_root_path,
            context=_Ctx(),
            semaphore=semaphore,
            all_domains=domains,
        )
        for domain_name, domain_description in domain_items
    ]

    # Level 1 parallelism. return_exceptions=False so the first
    # unrecoverable failure propagates immediately (fail-fast semantics
    # per GH-304). The thin try/except adds the batch size and domain
    # names to the error log so failures are diagnosable from logs
    # alone — the @with_retry layer only knows about a single failing
    # call, not the surrounding batch.
    try:
        domain_results = await asyncio.gather(*domain_coros)
    except Exception as exc:
        domain_names = ", ".join(repr(name) for name, _ in domain_items)
        logger.error(
            f"[design_autocomplete] Phase A: aborted batch of "
            f"{len(domain_items)} domain(s) due to "
            f"{type(exc).__name__}: {exc}. "
            f"domains in batch: {domain_names}"
        )
        raise

    return {
        domain_name: result
        for (domain_name, _), result in zip(domain_items, domain_results)
    }


async def _generate_design_content(
    tasks: List[Any],
    project_description: str,
    project_name: str,
    project_root: str,
) -> Dict[str, Dict[str, Any]]:
    """
    Phase A: Generate design artifacts + decisions and set status to DONE.

    Makes separate LLM calls per artifact document, just like an agent
    would make separate ``log_artifact`` calls. Writes files to disk and
    marks design tasks as DONE. Runs BEFORE ``create_tasks_on_board`` so
    design tasks are born DONE on the kanban board.

    Parallelism (GH-304)
    --------------------
    Level 1 — across design tasks: all design tasks run concurrently via
    ``asyncio.gather``.
    Level 2 — within each task: the 4 artifact LLM calls and the 1
    decisions LLM call run concurrently for that task.

    Combined with the per-invocation ``asyncio.Semaphore`` cap of
    ``_DESIGN_LLM_CONCURRENCY`` (10), this brings a 10-task enterprise
    project from ~25–33 min (sequential) down to ~1–3 min wall-clock
    without exceeding rate limits.

    Failure semantics
    -----------------
    Fail-fast. Each LLM call is wrapped with
    ``@with_retry(RetryConfig(max_attempts=3, base_delay=2.0, jitter=True))``
    via :func:`_bounded_llm_analyze`. If retries exhaust for any single
    call, the exception propagates out of the inner ``gather``, aborts
    the outer ``gather``, and this function raises — no task state
    mutations happen, and the caller must retry project creation.

    This is a behavior change from pre-GH-304 code, which warned and
    continued on a per-task basis. Rationale: partial design results
    (some tasks designed, some not) silently corrupt downstream agent
    work. Hard-fail surfaces the problem immediately.

    Parameters
    ----------
    tasks : List[Any]
        All tasks (design + implementation). Design tasks are modified
        in-place on success: status set to DONE, ``assigned_to="Marcus"``,
        and ``"auto_completed"`` label added. No mutations happen on
        failure — state updates are atomic across all design tasks.
    project_description : str
        Full project description for LLM context.
    project_name : str
        Project name.
    project_root : str
        Absolute path to project implementation directory.

    Returns
    -------
    Dict[str, Dict[str, Any]]
        Mapping of task name -> ``{"artifacts": [...], "decisions": [...]}``
        for Phase B.

    Raises
    ------
    Exception
        Any unrecoverable LLM or I/O error from the parallel design calls
        (fail-fast). Disk side effects (partial artifact files under
        ``project_root_path``) may still be present on failure — this
        matches pre-GH-304 behavior.

    See Also
    --------
    GH-297 : Phase A / Phase B design autocomplete design.
    GH-304 : Parallelization decision and Kaia architectural review.
    """
    from pathlib import Path

    from src.ai.providers.llm_abstraction import LLMAbstraction

    project_root_path = Path(project_root)

    design_tasks = [t for t in tasks if _is_design_task(t)]
    if not design_tasks:
        return {}

    logger.info(
        f"[design_autocomplete] Phase A: generating content "
        f"for {len(design_tasks)} design task(s) "
        f"(concurrency cap={_DESIGN_LLM_CONCURRENCY})"
    )

    llm = LLMAbstraction()

    class _Ctx:
        max_tokens = 4000

    # Create the semaphore inside the function so it binds to the
    # currently running event loop. A module-level semaphore leaks
    # across pytest-asyncio function-scoped loops and causes confusing
    # test failures; binding per-call is cheap and guarantees
    # correctness.
    semaphore = asyncio.Semaphore(_DESIGN_LLM_CONCURRENCY)

    # Iterate per-task instead of collapsing into a ``{domain: task}``
    # dict. If two design tasks happen to share the same stripped
    # domain name, dict-keying would silently drop one of them — only
    # the second task would get its LLM calls made and its state
    # mutated. Per-task iteration preserves the pre-#320 behavior
    # exactly: each task gets its own LLM calls, each task gets its
    # own ``status=DONE`` mutation, and the task-keyed
    # ``design_content`` dict has whatever write-order semantics the
    # old code had. The contract-first decomposer in PR 2 uses
    # ``_generate_contracts_by_domain`` directly instead — that path
    # operates on a ``Dict[str, str]`` where uniqueness is a dict
    # invariant, so the collision case can't arise. See the Codex
    # review on PR #322.
    #
    # Build the ``all_domains`` map FIRST by iterating once to
    # collect ``{domain_name: task_description}`` for the sibling
    # block (GH-320 Option A scope clamp). Collisions on stripped
    # domain name are tolerated: the last task wins as the sibling
    # description, but each task still runs its own LLM calls via
    # the per-task iteration below. This gives the LLM a scope
    # referent even in the rare feature-based collision case — it's
    # strictly better than having no sibling block at all.
    all_domains: Dict[str, str] = {}
    for task in design_tasks:
        task_name = task.name
        if task_name.startswith("Design "):
            domain_name = task_name[len("Design ") :]
        else:
            domain_name = task_name
        # Use a short summary instead of the full description so the
        # sibling block stays scannable. The helper truncates to 100
        # chars anyway; pre-shortening keeps the map small.
        all_domains[domain_name] = task.description

    task_coros = []
    for task in design_tasks:
        task_name = task.name
        if task_name.startswith("Design "):
            domain_name = task_name[len("Design ") :]
        else:
            domain_name = task_name
        task_coros.append(
            _process_design_domain(
                llm=llm,
                domain_name=domain_name,
                domain_description=task.description,
                project_name=project_name,
                project_description=project_description,
                project_root_path=project_root_path,
                context=_Ctx(),
                semaphore=semaphore,
                all_domains=all_domains,
            )
        )

    # Level 1 parallelism. return_exceptions=False so the first
    # unrecoverable failure propagates immediately (fail-fast semantics
    # per GH-304). The thin try/except adds the batch size and task
    # names to the error log so failures are diagnosable from logs
    # alone — the @with_retry layer only knows about a single failing
    # call, not the surrounding batch.
    try:
        task_results = await asyncio.gather(*task_coros)
    except Exception as exc:
        task_names = ", ".join(repr(t.name) for t in design_tasks)
        logger.error(
            f"[design_autocomplete] Phase A: aborted batch of "
            f"{len(design_tasks)} design task(s) due to "
            f"{type(exc).__name__}: {exc}. "
            f"tasks in batch: {task_names}"
        )
        raise

    # All tasks finished without raising. Atomically update task state
    # on the board-bound Task objects and assemble the design_content
    # mapping for Phase B. The task-keyed shape of design_content is
    # preserved exactly, including the overwrite-on-collision semantics
    # of the pre-#320 code (if two tasks share a name, the second one
    # wins the dict slot, but both have their LLM calls made and both
    # are marked DONE).
    design_content: Dict[str, Dict[str, Any]] = {}
    for task, result in zip(design_tasks, task_results):
        if result is None:
            # No artifacts produced — task stays TODO, warning already
            # logged inside ``_process_design_domain``. Skip state
            # mutation for this task.
            continue

        design_content[task.name] = result

        # Mark task as DONE before it hits the board
        task.status = TaskStatus.DONE
        task.assigned_to = "Marcus"
        if not hasattr(task, "labels") or task.labels is None:
            task.labels = []
        if "auto_completed" not in task.labels:
            task.labels.append("auto_completed")

        logger.info(f"[design_autocomplete] Phase A: " f"'{task.name}' status=DONE")

    return design_content


_SCAFFOLD_PROMPT = """\
You are a senior software architect. Generate the project scaffold \
for the following project.

## Project
{project_name}: {project_description}

## Architecture Document
{architecture_content}

## Implementation Tasks
{impl_task_list}

## Instructions
Generate ONLY the shared build/tooling infrastructure. The implementing \
agents decide everything about the application code.

LANGUAGE / TECH-STACK CONSTRAINTS (bug #649 root cause 1):
Read the project description above for any explicit language, framework, \
or "no <X>" constraint (e.g., "vanilla JavaScript", "plain Python", \
"no TypeScript", "Flask only"). HONOR THOSE CONSTRAINTS EXACTLY:
- If the spec says "vanilla JavaScript", produce .js files and do NOT \
generate tsconfig.json, main.tsx, App.tsx, or any TypeScript artifact.
- If the spec says "plain Python", do not introduce frameworks or \
type-checking config the spec did not ask for.
- Pick file extensions and config filenames to match the stated stack. \
The architecture document above is authoritative for the tech stack \
choice — follow it instead of any example below.

ALLOWED files (generate these — file names/extensions match stated stack):
- Package manifest (e.g., package.json, pyproject.toml, Cargo.toml)
- Build configuration (e.g., vite.config.js / vite.config.ts / \
pyproject build settings — pick the form that matches the language)
- Entry point (e.g., main.js, main.ts, index.js, main.py — pick the \
extension that matches the language stated in the spec)
- App shell (the entry module wires up components; for vanilla-JS \
projects this is just main.js, not App.tsx)
- Tooling config (.gitignore, .env.example)
- ONE placeholder file per implementation task (see below)

FORBIDDEN — do NOT generate these:
- Type definitions or data-model files (those are the agents' work)
- Utility functions, helpers, or service implementations
- CSS files, stylesheets, or design tokens
- Test files or test configuration
- Any file with more than 3 lines of actual code (configs excepted)
- Files in a language the spec did NOT ask for (no .ts when the spec \
says vanilla JS; no Python config when the spec says JavaScript)

Placeholder files must contain EXACTLY one comment line using the \
language's native comment syntax (// for JS/TS, # for Python, etc.):
// TimeWidget — implementation task for agent

The .gitignore MUST include the build artifacts appropriate for the \
project's stated tech stack (e.g., node_modules/ and dist/ for JS/TS, \
__pycache__/ and *.pyc for Python). Do NOT add "*.js in src/" to the \
gitignore when the project's source language IS JavaScript — that \
would ignore the user's actual source files.

TASK ANCHORING (issue #659): for each placeholder file you generate \
for an implementation task, you MUST include a ``task_name`` field \
that EXACTLY matches the task name from the Implementation Tasks \
list above (e.g., ``"Implement Game Core Engine"``). This binds the \
placeholder to its owning task so Marcus can surface the path to the \
implementing agent. Files that are NOT per-task placeholders (config, \
manifests, entry points, .gitignore) MUST omit the ``task_name`` \
field — they are shared infrastructure, not owned by any single task.

Respond with ONLY a JSON array of files. No markdown fencing.
Example shape (your actual extensions must match the stated stack):
[{{"path": "package.json", "content": "..."}}, \
{{"path": "src/<entry-file>", "content": "..."}}, \
{{"path": "src/<placeholder>", "content": "// ...", \
"task_name": "Implement <FeatureName>"}}]
"""


async def _generate_project_scaffold(
    tasks: List[Any],
    project_description: str,
    project_name: str,
    project_root: str,
    design_content: Dict[str, Dict[str, Any]],
) -> Tuple[bool, Dict[str, str]]:
    """
    Generate project scaffold and write to disk on main.

    Reads the architecture doc from design_content, generates
    shared infrastructure files (package manifest, config, entry
    point) and empty placeholder files per implementation task.
    Written to project_root so worktrees inherit them.

    Issue #659 — task anchoring
    ---------------------------
    Each per-task placeholder the LLM emits now carries a
    ``task_name`` field binding it to the owning implementation
    task. The returned mapping ``{task_name: scaffold_path}`` lets
    the caller persist the scaffold path on the corresponding
    kanban task's ``source_context``. Agents then read this
    anchor from their task instructions instead of inventing a
    sibling path and orphaning the scaffold (the
    ``src/core/gameEngine.js`` failure observed in
    ``snake-baton-1``).

    Parameters
    ----------
    tasks : List[Any]
        All tasks including implementation tasks.
    project_description : str
        Project description.
    project_name : str
        Project name.
    project_root : str
        Path to implementation/ directory on main.
    design_content : Dict[str, Dict]
        From _generate_design_content, contains architecture doc.

    Returns
    -------
    Tuple[bool, Dict[str, str]]
        ``(success, task_to_path)`` where ``success`` is True iff
        the scaffold wrote at least one file and ``task_to_path``
        maps each implementation task name the LLM bound to a
        placeholder file → that placeholder's relative path
        within ``project_root``. Config/entry-point files are
        absent from the mapping (they have no owning task). The
        mapping is empty when scaffold generation is skipped or
        all placeholders were rejected.

    See: https://github.com/lwgray/marcus/issues/300
    """
    import json
    from pathlib import Path

    from src.ai.providers.llm_abstraction import LLMAbstraction

    project_root_path = Path(project_root)
    # #659: filled in below from LLM output; returned to caller so
    # the scaffold path can be persisted on the owning task.
    task_to_path: Dict[str, str] = {}

    # Get the architecture doc content from design_content
    arch_content = ""
    for task_name, content in design_content.items():
        for art in content.get("artifacts", []):
            if art.get("artifact_type") == "architecture":
                arch_content = art.get("content", "")
                break
        if arch_content:
            break

    if not arch_content:
        logger.warning(
            "[scaffold] No architecture doc found — " "skipping scaffold generation"
        )
        return False, task_to_path

    # Build implementation task list
    impl_tasks = [
        t
        for t in tasks
        if not _is_design_task(t)
        and getattr(t, "name", "").lower().startswith("implement")
    ]
    impl_task_list = "\n".join(
        f"- {t.name}: {(t.description or '')[:100]}" for t in impl_tasks
    )

    if not impl_task_list:
        logger.warning(
            "[scaffold] No implementation tasks found — " "skipping scaffold generation"
        )
        return False, task_to_path

    llm = LLMAbstraction()

    class _Ctx:
        max_tokens = 4000

    prompt = _SCAFFOLD_PROMPT.format(
        project_name=project_name,
        project_description=project_description,
        architecture_content=arch_content,
        impl_task_list=impl_task_list,
    )

    try:
        response = await llm.analyze(
            prompt=prompt, context=_Ctx(), operation="generate_project_scaffold"
        )

        if not response:
            logger.warning("[scaffold] Empty LLM response")
            return False, task_to_path

        # Parse JSON array of files
        from src.utils.json_parser import clean_json_response

        cleaned = clean_json_response(response)
        files = json.loads(cleaned)

        if not isinstance(files, list):
            logger.warning("[scaffold] Expected JSON array")
            return False, task_to_path

        # Filter out over-generated files (GH-307)
        # Config files can be any length. Non-config files must be
        # ≤3 lines (placeholder comment only). This prevents the LLM
        # from generating types, utils, CSS, or implementation code.
        config_extensions = {
            ".json",
            ".toml",
            ".yaml",
            ".yml",
            ".cjs",
            ".mjs",
            ".config.ts",
            ".config.js",
        }
        config_names = {
            ".gitignore",
            ".env.example",
            ".eslintrc",
            "tsconfig.json",
            "tsconfig.node.json",
            "vite.config.ts",
            "vite.config.js",
        }

        # Build the set of impl task names so we can validate the
        # LLM-emitted ``task_name`` field (#659). Anything not in this
        # set is silently dropped from the mapping — the scaffold file
        # still gets written, but no task ends up anchored to it.
        impl_task_names = {getattr(t, "name", "") for t in impl_tasks}

        # Write each file to disk
        written = 0
        rejected = 0
        for f in files:
            fpath = f.get("path")
            fcontent = f.get("content")
            if not fpath or fcontent is None:
                continue

            # Check if this is a config/tooling file
            fname = Path(fpath).name
            is_config = (
                any(fname.endswith(ext) for ext in config_extensions)
                or fname in config_names
                or fname == "index.html"
                or "main." in fname
                or "App." in fname
            )

            # Non-config files must be ≤3 lines
            if not is_config:
                line_count = len(fcontent.strip().splitlines())
                if line_count > 3:
                    logger.info(
                        f"[scaffold] Rejected {fpath} "
                        f"({line_count} lines — over limit)"
                    )
                    rejected += 1
                    continue

            full_path = project_root_path / fpath
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(fcontent, encoding="utf-8")
            written += 1

            # #659: bind the placeholder to its owning implementation
            # task so the caller can stamp ``scaffold_path`` on the
            # task's ``source_context``. Only honor ``task_name`` when
            # it names a real impl task — defensive against the LLM
            # inventing a name (or attaching it to a config file).
            raw_task_name = f.get("task_name")
            if (
                raw_task_name
                and isinstance(raw_task_name, str)
                and raw_task_name in impl_task_names
                and not is_config
            ):
                if raw_task_name in task_to_path:
                    # Two placeholders bound to the same task — keep
                    # the first one, warn so we can investigate the
                    # prompt drift if it recurs.
                    logger.warning(
                        "[scaffold] task '%s' bound to multiple "
                        "placeholders: keeping %s, ignoring %s",
                        raw_task_name,
                        task_to_path[raw_task_name],
                        fpath,
                    )
                else:
                    task_to_path[raw_task_name] = fpath

        if rejected > 0:
            logger.info(f"[scaffold] Rejected {rejected} over-generated " f"file(s)")

        logger.info(
            f"[scaffold] Wrote {written} scaffold file(s) " f"to {project_root}"
        )

        # Commit scaffold to main so worktrees inherit it
        import subprocess

        subprocess.run(
            ["git", "add", "-A"],
            cwd=project_root,
            capture_output=True,
        )
        result = subprocess.run(
            [
                "git",
                "commit",
                "-m",
                "scaffold: project infrastructure (Marcus)",
            ],
            cwd=project_root,
            capture_output=True,
        )
        if result.returncode == 0:
            logger.info("[scaffold] Committed scaffold to main")
        else:
            logger.warning(f"[scaffold] Commit failed: " f"{result.stderr.decode()}")

        return written > 0, task_to_path

    except Exception as e:
        logger.warning(f"[scaffold] Failed: {e}")
        return False, task_to_path


async def _register_design_via_mcp(
    state: Any,
    design_content: Dict[str, Dict[str, Any]],
    project_root: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Phase B: Register pre-generated artifacts + decisions via MCP tools.

    Runs AFTER state.refresh_project_state() so we have real kanban
    UUIDs and the MCP state object. Calls log_artifact and log_decision
    through the proper codepaths so state.task_artifacts and
    state.context.decisions are populated for get_task_context.

    Parameters
    ----------
    state : Any
        MCP server state (fully initialized after refresh).
    design_content : Dict[str, Dict]
        From Phase A: task name → {artifacts, decisions}.
    project_root : Optional[str]
        Project root for log_artifact.

    Returns
    -------
    Dict[str, Any]
        Summary counts.
    """
    from src.marcus_mcp.tools.attachment import log_artifact
    from src.marcus_mcp.tools.context import log_decision

    result = {
        "tasks_completed": 0,
        "artifacts_registered": 0,
        "decisions_logged": 0,
    }

    if not design_content or not state.project_tasks:
        return result

    # Match state tasks to Phase A content by name
    for task in state.project_tasks:
        name = getattr(task, "name", "")
        if name not in design_content:
            continue

        content = design_content[name]
        task_id = task.id  # Real kanban UUID

        # Register artifacts via MCP tool
        if project_root:
            for art in content.get("artifacts", []):
                art_result = await log_artifact(
                    task_id=task_id,
                    filename=art["filename"],
                    content=art["content"],
                    artifact_type=art["artifact_type"],
                    project_root=project_root,
                    description=art.get("description", ""),
                    artifact_role=art.get("artifact_role"),
                    state=state,
                )
                if art_result.get("success"):
                    result["artifacts_registered"] += 1
                    loc = art_result["data"]["location"]
                    logger.info(f"[design_autocomplete] Phase B: " f"registered {loc}")

        # Register decisions via MCP tool
        for dec in content.get("decisions", []):
            required = ("what", "why", "impact")
            if not all(k in dec for k in required):
                continue
            dec_text = (
                f"{dec['what']} because {dec['why']}. " f"This affects {dec['impact']}"
            )
            dec_result = await log_decision(
                agent_id="Marcus",
                task_id=task_id,
                decision=dec_text,
                state=state,
            )
            if dec_result.get("success"):
                result["decisions_logged"] += 1
                logger.info(
                    f"[design_autocomplete] Phase B: " f"decision \"{dec['what']}\""
                )

        result["tasks_completed"] += 1

    return result


async def _run_design_phase(
    state: Any,
    kanban_client: Any,
    safe_tasks: List[Task],
    created_tasks: List[Task],
    description: str,
    project_name: str,
    project_root: str,
    pre_generated_content: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    """
    Run the full background design phase: Phase A + Phase B + kanban + scaffold.

    This orchestrates four steps in a fixed order:

    1. **Phase A** — :func:`_generate_design_content` produces design
       artifacts and decisions for each design task via parallel LLM
       calls (GH-297, GH-304). Fail-fast: any unrecoverable LLM error
       aborts the entire phase and no downstream steps run.
    2. **Phase B** — :func:`_register_design_via_mcp` registers the
       generated artifacts into ``state.task_artifacts`` keyed by the
       real kanban UUIDs, so downstream implementation tasks discover
       them via the dependency walk in
       :func:`_collect_task_artifacts` (GH-320).
    3. **Kanban DONE update** — mark design task cards as done,
       which unblocks implementation tasks from hard dependencies.
    4. **Scaffold generation** — :func:`_generate_project_scaffold`
       writes initial project scaffolding files.

    Ordering is load-bearing
    ------------------------
    Phase B (step 2) MUST run before the kanban DONE update (step 3).
    The DONE update is what unblocks implementation tasks from hard
    dependencies — if Phase B runs after, there is a window where:

    1. Design task marked DONE on kanban
    2. Implementation task unblocked
    3. Agent requests implementation task
    4. :func:`_collect_task_artifacts` walks dependencies, finds
       empty ``state.task_artifacts[design_task_id]``
    5. Agent receives no contracts

    The window is sub-second but races don't care about narrow
    windows. The ordering pinned here and tested by
    ``TestRunDesignPhaseHandoff`` prevents it entirely.

    Regression history
    ------------------
    Before GH-314 (commit 1c5c7f7, April 6, 2026), Phase A was
    synchronous inside ``create_project_from_description`` and its
    output was stored in ``result["design_content"]``. Phase B ran
    separately inside ``src/marcus_mcp/tools/nlp.py`` after
    ``refresh_project_state``, reading ``design_content`` out of the
    result dict.

    GH-314 moved Phase A to a background closure and deleted the
    line ``result["design_content"] = design_content``. This
    orphaned the Phase B call in nlp.py, which continued to read
    ``result.get("design_content", {})`` — an empty dict forever.
    The Phase B handoff was dead for 5 days until GH-320 caught it.

    This function is the fix: Phase A and Phase B run together in
    the same closure, so the handoff cannot be broken by refactoring
    one without touching the other. The dead Phase B block in
    nlp.py was removed as part of the same fix.

    Parameters
    ----------
    state : Any
        Marcus MCP server state. Required for Phase B — used to
        populate ``state.task_artifacts`` via ``log_artifact``. If
        ``None``, Phase B is skipped with a warning (legacy behavior
        preserved for callers that don't pass state).
    kanban_client : Any
        Kanban client for marking design tasks DONE.
    safe_tasks : List[Any]
        Pre-board-creation task objects used to derive design task
        names and descriptions.
    created_tasks : List[Any]
        Post-board-creation task objects with real kanban UUIDs. The
        index must align with ``safe_tasks``.
    description : str
        Original project description (passed to Phase A LLM calls).
    project_name : str
        Project name (passed to Phase A and scaffold).
    project_root : str
        Absolute path where artifacts will be written.
    pre_generated_content : Optional[Dict[str, Dict[str, Any]]]
        Pre-generated Phase A output. When provided, Phase A
        (``_generate_design_content``) is **skipped** entirely and
        the supplied dict is used directly as ``design_content`` for
        Phase B and the kanban DONE update. Used by the contract-first
        decomposer (GH-320 PR after #333), which generates contract
        artifacts upstream in ``_generate_contracts_by_domain`` and
        synthesizes design ghost tasks to carry them through the
        existing observability infrastructure. The dict must be
        keyed by ghost task name (``f"Design {{domain_name}}"``) so
        :func:`_register_design_via_mcp` can join it to
        ``state.project_tasks``. When ``None`` (default), Phase A
        runs normally.

    Returns
    -------
    None
        This is a background task — callers fire-and-forget via
        :func:`asyncio.ensure_future`. All failures are logged and
        non-fatal by design.

    See Also
    --------
    _generate_design_content : Phase A implementation.
    _register_design_via_mcp : Phase B implementation.
    tests.unit.integrations.test_design_autocomplete.TestRunDesignPhaseHandoff :
        Regression guard for the ordering invariant.

    Notes
    -----
    GH-297 : Original two-phase design autocomplete.
    GH-304 : Parallelization (Phase A 25-33min → 1-3min).
    GH-314 : Moved Phase A to background (accidentally orphaned Phase B).
    GH-320 : Reconnected Phase A → Phase B handoff (this function).
    GH-320 PR after #333 : ``pre_generated_content`` parameter for
        contract-first Cato retrofit — Phase A skipped when contracts
        are already generated upstream.
    """
    # Codex P1 on PR #516 — placeholder context leaks into background
    # design phase. ``create_project`` wraps ``_create_project_inner``
    # in a ``planner_context`` carrying a synthetic ``pending:<uuid>``
    # project_id and runs a one-shot ``rebind_project_id`` after the
    # inner call returns. But this function is spawned via
    # ``asyncio.ensure_future`` *during* the inner call, so it
    # inherits the placeholder via the parent task's ContextVar
    # state copy. After the wrapper's one-shot rebind fires, the
    # design phase keeps writing rows under the placeholder — those
    # rows are never rebound and stay hidden as ``pending:*``.
    #
    # Fix: as the very first action, push a fresh PlannerContext
    # carrying the real project_id (resolved from the kanban_client)
    # so the placeholder is shadowed for the rest of this task's
    # lifetime. The asyncio task isolation means the main task's
    # stack is unaffected — the wrapper's rebind still catches the
    # synchronous rows under the placeholder, and the design
    # phase's rows are attributed to the real project from the
    # start.
    #
    # If ``kanban_client.project_id`` is unavailable (shouldn't
    # happen post-board-creation), fall through with the inherited
    # context — best-effort, no worse than the bug we're fixing.
    from contextlib import nullcontext as _nullcontext

    from src.cost_tracking.cost_recorder import PlannerContext as _PlannerContext
    from src.cost_tracking.cost_recorder import (
        canonical_project_id as _canonical_project_id,
    )
    from src.cost_tracking.cost_recorder import get_recorder as _get_recorder

    _raw_pid = getattr(kanban_client, "project_id", None)
    # Defensive isinstance check: tests often use ``MagicMock``-backed
    # kanban clients whose ``project_id`` resolves to another
    # ``MagicMock`` rather than ``None``. Without the check we'd push
    # a context with a non-string project_id and the recorder would
    # silently swallow the SQL error.
    _real_pid = _canonical_project_id(_raw_pid) if isinstance(_raw_pid, str) else None
    if _real_pid:
        # Codex P2 on PR #545 — preserve the parent context's run_id
        # so design-phase LLM calls are attributed to the
        # ``create_project`` run rather than falling through to
        # ``'unassigned'``. The parent context pushed in
        # ``create_project`` (``src/marcus_mcp/tools/nlp.py:218``)
        # carries the real generated run_id alongside the placeholder
        # project_id; we replace only the project_id here, not the
        # run_id. Falling back to ``'unassigned'`` when no parent
        # context exists matches the prior behavior.
        _recorder = _get_recorder()
        _parent_ctx = _recorder.current()
        _inherited_run_id = (
            _parent_ctx.run_id if _parent_ctx is not None else "unassigned"
        )
        _design_ctx_cm: Any = _recorder.planner_context(
            _PlannerContext(
                run_id=_inherited_run_id,
                project_id=_real_pid,
                project_name=project_name,
            )
        )
    else:
        _design_ctx_cm = _nullcontext()

    with _design_ctx_cm:
        await _run_design_phase_body(
            state=state,
            kanban_client=kanban_client,
            safe_tasks=safe_tasks,
            created_tasks=created_tasks,
            description=description,
            project_name=project_name,
            project_root=project_root,
            pre_generated_content=pre_generated_content,
        )


async def _run_design_phase_body(
    state: Any,
    kanban_client: Any,
    safe_tasks: List[Task],
    created_tasks: List[Task],
    description: str,
    project_name: str,
    project_root: str,
    pre_generated_content: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    """Body of the background design phase (Phase A + Phase B + DONE + scaffold).

    Split out from :func:`_run_design_phase` so that the wrapper can
    push a fresh ``PlannerContext`` (Codex P1 on PR #516) without
    forcing the entire body into an extra indentation level. See
    :func:`_run_design_phase` for the full documentation of behavior,
    ordering invariants, and regression history.
    """
    design_content: Dict[str, Any] = {}
    if pre_generated_content is not None:
        # Contract-first Cato retrofit path. Phase A artifacts and
        # decisions were already generated upstream by
        # ``_generate_contracts_by_domain`` in
        # ``_try_contract_first_decomposition``. Use them directly
        # without re-running Phase A.
        design_content = pre_generated_content
        logger.info(
            f"[design_autocomplete] Skipping Phase A — using "
            f"{len(design_content)} pre-generated design entries "
            f"(contract-first path)"
        )
    else:
        try:
            design_content = await _generate_design_content(
                tasks=safe_tasks,
                project_description=description,
                project_name=project_name,
                project_root=project_root,
            )
            logger.info("[design_autocomplete] Background Phase A complete")
        except Exception as e:
            logger.warning(
                f"[design_autocomplete] Background Phase A failed (non-fatal): {e}"
            )
            # Fail-fast semantics: partial design outputs silently
            # corrupt downstream agent work (#304). Return early so no
            # Phase B, no kanban DONE updates, no scaffold.
            return

    if not design_content:
        logger.info(
            "[design_autocomplete] Phase A produced no content, skipping Phase B"
        )
        return

    # Phase B — MUST run before kanban DONE update. See docstring
    # "Ordering is load-bearing" section for why.
    #
    # Race avoidance: Phase B matches ``state.project_tasks`` to
    # ``design_content`` by task name. Because this closure runs as a
    # background task, ``state.project_tasks`` may still be stale at
    # the moment Phase B fires — the MCP tool caller's
    # ``refresh_project_state()`` might not have completed yet. To
    # close that race, refresh state ourselves before Phase B so the
    # name match sees current kanban UUIDs. Codex review on PR #326
    # caught the original hole: without this refresh, Phase B could
    # silently register zero artifacts against an empty task list
    # and the closure would still mark design tasks DONE, leaving
    # impl tasks to unblock without contracts — the exact silent
    # failure mode this function is supposed to eliminate.
    phase_b_registered = 0
    if state is not None:
        if hasattr(state, "refresh_project_state"):
            try:
                await state.refresh_project_state()
            except Exception as e:
                logger.warning(
                    f"[design_autocomplete] Pre-Phase-B state refresh "
                    f"failed: {e}. Phase B may see stale project_tasks."
                )
        try:
            phase_b_result = await _register_design_via_mcp(
                state=state,
                design_content=design_content,
                project_root=project_root,
            )
            phase_b_registered = int(phase_b_result.get("artifacts_registered", 0))
            logger.info(
                f"[design_autocomplete] Phase B: registered "
                f"{phase_b_registered} "
                f"artifact(s), "
                f"{phase_b_result.get('decisions_logged', 0)} "
                f"decision(s) via MCP tools"
            )
        except Exception as e:
            logger.warning(f"[design_autocomplete] Phase B failed (non-fatal): {e}")
    else:
        logger.warning(
            "[design_autocomplete] Phase B skipped: state is None "
            "(design artifacts written to disk but not registered in "
            "state.task_artifacts; downstream tasks will not discover "
            "them via get_task_context)"
        )

    # Zero-registration guard (Codex review on PR #326).
    #
    # When ``state`` is provided and Phase A produced design content,
    # Phase B MUST have registered at least one artifact for the
    # kanban DONE update to be safe. If it registered zero, something
    # is badly wrong — most likely ``state.project_tasks`` was still
    # empty or none of the names matched. Marking design tasks DONE
    # now would unblock impl tasks and they would walk dependencies
    # into empty ``state.task_artifacts`` entries, reintroducing the
    # silent-failure mode PR #326 was designed to eliminate.
    #
    # Skipping the DONE updates means impl tasks stay blocked on
    # their design deps. That's the correct degenerate state: the
    # user sees "design tasks never completed" in the kanban and can
    # investigate, rather than seeing "implementation agents
    # produced code that doesn't integrate and nobody knows why."
    # Loud failure > silent corruption.
    #
    # The ``state is None`` path is exempt from this guard because
    # legacy callers explicitly opt out of Phase B entirely — for
    # them, the DONE updates are the only thing moving design tasks
    # to the next state and skipping them would hang the project.
    if state is not None and design_content and phase_b_registered == 0:
        logger.error(
            "[design_autocomplete] Phase B registered 0 artifacts "
            "despite Phase A producing design_content. Refusing to "
            "mark design tasks DONE — impl tasks would unblock "
            "without contracts (exact silent failure mode from "
            "#314). Design tasks will stay TODO; investigate why "
            "state.project_tasks did not match design_content names. "
            f"design_content keys: {list(design_content.keys())}"
        )
        return

    # Kanban DONE update — unblocks implementation tasks. Runs
    # AFTER Phase B so that state.task_artifacts is already
    # populated when dependents walk the dependency graph.
    #
    # ``created_tasks`` and ``safe_tasks`` are index-aligned by
    # construction in ``create_project_from_description``: each
    # ``created_tasks[i]`` is the kanban-created counterpart of
    # ``safe_tasks[i]``. ``zip`` is O(n) and handles the shorter-list
    # case gracefully if the two arrays ever get out of sync.
    for ct, orig in zip(created_tasks, safe_tasks):
        if _is_design_task(orig) and orig.name in design_content:
            try:
                await kanban_client.update_task(
                    ct.id,
                    {"status": "done"},
                )
                logger.info(
                    f"[design_autocomplete] Marked '{orig.name}' " f"DONE on board"
                )
            except Exception as e:
                logger.warning(
                    f"[design_autocomplete] Failed to update board "
                    f"for '{orig.name}': {e}"
                )

    # Scaffold generation — best effort, non-fatal on failure
    try:
        scaffold_ok, scaffold_task_to_path = await _generate_project_scaffold(
            tasks=safe_tasks,
            project_description=description,
            project_name=project_name,
            project_root=project_root,
            design_content=design_content,
        )

        # #659: persist the scaffold path on the owning task's
        # ``source_context`` so the agent prompt (Layer 1.3 in
        # ``build_tiered_instructions``) can show it as the canonical
        # implementation address. Without this, agents pick a sibling
        # path by accident — the ``src/core/gameEngine.js`` orphan
        # observed in ``snake-baton-1`` (commit 0ddc6c0 wrote at
        # ``src/game/gameEngine.js`` while the scaffold sat at
        # ``src/core/gameEngine.js``).
        #
        # ``safe_tasks`` and ``created_tasks`` are index-aligned by
        # construction (same ``zip`` invariant used for the design-DONE
        # update above), so we match the LLM-emitted ``task_name``
        # against the ORIGINAL task name and use the kanban-side task
        # ID to issue the update.
        # Cross-provider persistence: SQLite merges ``source_context``
        # into its JSON column; Planka / GitHub / Linear don't have a
        # ``source_context`` column and would otherwise silently drop
        # the anchor. The single ``update_task`` call below also writes
        # a ``MARCUS_SCAFFOLD_PATH`` marker into the description,
        # mirroring the ``MARCUS_CONTRACT_FIRST`` pattern. Every
        # provider's ``update_task`` round-trips description.
        # ``_resolve_scaffold_path`` in ``task.py`` parses the marker
        # as the fallback path source when ``source_context`` is empty
        # — cross-provider parity intact.
        if scaffold_ok and scaffold_task_to_path:
            name_to_pair = {
                orig.name: (ct.id, orig) for ct, orig in zip(created_tasks, safe_tasks)
            }
            for task_name, scaffold_path in scaffold_task_to_path.items():
                pair = name_to_pair.get(task_name)
                if not pair:
                    logger.warning(
                        "[scaffold] LLM bound placeholder to task "
                        "'%s' but no kanban task matches that name; "
                        "skipping anchor",
                        task_name,
                    )
                    continue
                kanban_id, orig_task = pair
                # Idempotent marker append: skip when the marker is
                # already present so re-invocation doesn't multiply
                # the comment.
                existing_desc = getattr(orig_task, "description", "") or ""
                if "<!-- MARCUS_SCAFFOLD_PATH:" in existing_desc:
                    new_desc = existing_desc
                else:
                    marker = f"<!-- MARCUS_SCAFFOLD_PATH: {scaffold_path} -->"
                    new_desc = (
                        f"{existing_desc}\n\n{marker}" if existing_desc else marker
                    )
                try:
                    # #206 + #659 lock-target refinement: when we
                    # anchor a task to a scaffold path, we ALSO update
                    # ``declared_files`` to point at that path.
                    # Previously the lock layer (#658) declared
                    # ``[contract_file]`` — but contract_file points at
                    # a docs/specifications/*.md artifact agents read
                    # but never write. Locking on the doc path is a
                    # no-op against the real merge conflicts on the
                    # implementation file. After this update, the lock
                    # filter/acquire in ``task.py`` sees the scaffold
                    # path as the declared write target and serializes
                    # tasks correctly when two impl tasks both touch
                    # the same shared file.
                    await kanban_client.update_task(
                        kanban_id,
                        {
                            "description": new_desc,
                            "source_context": {
                                "scaffold_path": scaffold_path,
                                "declared_files": [scaffold_path],
                            },
                        },
                    )
                    logger.info(
                        "[scaffold] Anchored task '%s' (%s) to %s "
                        "(lock target = scaffold path)",
                        task_name,
                        kanban_id,
                        scaffold_path,
                    )
                except Exception as anchor_err:
                    logger.warning(
                        "[scaffold] Failed to anchor task '%s' to %s: %s",
                        task_name,
                        scaffold_path,
                        anchor_err,
                    )

        logger.info("[scaffold] Background generation complete")
    except Exception as e:
        logger.warning(f"[scaffold] Background generation failed (non-fatal): {e}")


async def add_feature_natural_language(
    feature_description: str, integration_point: str = "auto_detect", state: Any = None
) -> Dict[str, Any]:
    """
    MCP tool to add a feature to existing project using natural language.

    This is the main entry point that Claude will call.
    """
    try:
        # Validate required parameters
        if not feature_description or not feature_description.strip():
            return {
                "success": False,
                "error": "Feature description is required and cannot be empty",
            }

        # Check if state was provided
        if state is None:
            raise ValueError("State parameter is required")

        # Initialize kanban client if needed
        if not state.kanban_client:
            try:
                await state.initialize_kanban()
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to initialize kanban client: {str(e)}",
                }

        # Verify kanban client supports create_task
        if not hasattr(state.kanban_client, "create_task"):
            return {
                "success": False,
                "error": (
                    "Kanban client does not support task creation. "
                    "Please ensure KanbanClientWithCreate is being used."
                ),
            }

        # Check if there are existing tasks (required for feature addition)
        if not state.project_tasks or len(state.project_tasks) == 0:
            return {
                "success": False,
                "error": (
                    "No existing project found. "
                    "Please create a project first before adding features."
                ),
            }

        # Initialize feature adder
        adder = NaturalLanguageFeatureAdder(
            kanban_client=state.kanban_client,
            ai_engine=state.ai_engine,
            project_tasks=state.project_tasks,
        )

        # Add feature
        result = await adder.add_feature_from_description(
            feature_description=feature_description, integration_point=integration_point
        )

        # Update Marcus state if successful
        if result.get("success"):
            try:
                await state.refresh_project_state()
            except Exception as e:
                # Log but don't fail the operation
                logger.warning(f"Failed to refresh project state: {str(e)}")

        return result

    except Exception as e:
        logger.error(f"Unexpected error in add_feature_natural_language: {str(e)}")
        return {"success": False, "error": f"Unexpected error: {str(e)}"}
