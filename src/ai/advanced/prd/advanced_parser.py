"""
Advanced PRD Parser for Marcus Phase 4.

Transform natural language requirements into actionable tasks with
deep understanding, intelligent task breakdown, and risk assessment.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from src.ai.advanced.prd.outcome_extractor import (
    UserOutcome,
    extract_user_outcomes,
)
from src.ai.providers.llm_abstraction import LLMAbstraction
from src.config.hybrid_inference_config import HybridInferenceConfig
from src.config.outcome_coverage_config import is_outcome_coverage_enabled
from src.core.models import Priority, Task, TaskStatus
from src.integrations.ai_analysis_engine import AIAnalysisEngine
from src.intelligence.dependency_inferer_hybrid import HybridDependencyInferer
from src.marcus_mcp.coordinator.graph_augmentation import (
    AugmentationResult,
    GraphAugmenter,
    run_augmenter_chain,
)
from src.marcus_mcp.coordinator.outcome_coverage_augmenter import (
    OutcomeCoverageAugmenter,
)
from src.marcus_mcp.coordinator.spec_coverage_augmenter import (
    SpecCoverageAugmenter,
)

logger = logging.getLogger(__name__)


def _normalize_declared_file_path(raw: str) -> str:
    r"""Return the canonical form of a declared file path.

    The registry uses the path string as part of its lookup key, so
    two tasks declaring the same file under different surface forms
    (``./src/foo.py`` vs ``src/foo.py`` vs ``src\\foo.py``) would
    otherwise miss each other and skip the conflict check — exactly
    the regression Kaia's #658 review flagged.

    Normalizes by:

    - Stripping surrounding whitespace.
    - Resolving leading ``./`` and embedded ``./`` segments via
      ``os.path.normpath``.
    - Converting Windows-style backslash separators to POSIX ``/``
      so Marcus's cross-platform agent fleet sees one form.
    - NOT resolving symlinks or making the path absolute — the
      contract file is repo-relative and Marcus has no business
      knowing the agent's worktree root at decomposition time.

    Parameters
    ----------
    raw : str
        Raw path string (typically from the LLM-produced contract
        descriptor).

    Returns
    -------
    str
        Canonical POSIX-style relative path. Empty string if ``raw``
        is empty after stripping.
    """
    import os

    stripped = raw.strip()
    if not stripped:
        return ""
    # Replace backslashes BEFORE normpath so Linux normpath treats
    # the path as POSIX (normpath('a\\b') on Linux returns 'a\\b').
    posix_form = stripped.replace("\\", "/")
    return os.path.normpath(posix_form).replace("\\", "/")


def _extract_contract_domain_slug(contract_file: str) -> str:
    """Return the domain slug from a contract artifact path.

    The contract-generation pipeline emits four artifact types per
    domain, all sharing a filename template
    ``{domain_slug}-{artifact_type}.md`` and one of four artifact
    suffixes: ``-architecture``, ``-api-contracts``, ``-data-models``,
    or ``-interface-contracts`` (see ``_DESIGN_ARTIFACT_SPECS`` in
    ``src/integrations/nlp_tools.py``). The over-fragmentation guard
    in ``decompose_by_contract`` needs to detect when the LLM
    produced multiple tasks claiming the SAME domain via different
    artifact types — naive contract_file-string dedupe misses this
    because the filenames differ.

    On the ``snake-663-658`` run (2026-05-27) the LLM produced three
    tasks for two domains by using three different artifact types of
    those two domains:

      Implement Game Core Engine
        → ...game-core-engine-interface-contracts.md
      Implement Game Presentation
        → ...game-presentation-and-feedback-interface-contracts.md
      Integrate Engine + Presentation
        → ...game-core-engine-architecture.md  ← same domain!

    All three contract_files are unique strings, but two of them
    point at the SAME domain (``game-core-engine``). Dedupe by
    domain slug to catch this.

    Parameters
    ----------
    contract_file : str
        A contract artifact path, typically
        ``docs/{artifact_dir}/{domain_slug}-{artifact_type}.md``.
        May be empty / unset — returns ``""`` then.

    Returns
    -------
    str
        The domain slug (e.g. ``game-core-engine``) — the part of
        the basename before the artifact-type suffix. Returns the
        whole stem if no recognized artifact suffix matches.
    """
    import os

    if not contract_file:
        return ""
    # Strip directory + extension.
    base = os.path.basename(contract_file.strip())
    if base.endswith(".md"):
        base = base[:-3]
    elif "." in base:
        base = base.rsplit(".", 1)[0]
    # Strip the artifact-type suffix. Order matters: longer
    # suffixes first so ``-interface-contracts`` matches before
    # ``-contracts`` (defensive — current spec has no bare
    # ``-contracts`` but future drift is cheap to guard against).
    artifact_suffixes = (
        "-interface-contracts",
        "-api-contracts",
        "-data-models",
        "-architecture",
    )
    for suffix in artifact_suffixes:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    # Unknown shape — return the bare stem so dedupe still works
    # on identical paths, just without the artifact-type
    # cross-matching.
    return base


def _extract_declared_files(
    responsibility: Optional[str],
    contract_file: Optional[str],
    contract_artifacts: Optional[Dict[str, Any]],
) -> List[str]:
    """Return the list of file paths a contract task intends to write.

    Populated into ``task.source_context["declared_files"]`` by
    :meth:`AdvancedPRDParser.decompose_by_contract`. Later consulted
    by ``request_next_task`` to skip tasks whose declared files are
    currently held by another in-progress task (#206 MVP, Phase 3).

    MVP scope — **conservative**: the only declared write target is
    the task's own ``contract_file``. Contract artifacts (foundation
    files read by every implementation task) are NOT included because
    the registry only locks writes — agents read freely. Inferred
    implementation files beyond ``contract_file`` are also excluded
    to avoid over-blocking before we have empirical contention data.

    Parameters
    ----------
    responsibility : str, optional
        The task's contract responsibility text. Reserved for future
        inference heuristics; currently unused so MVP behavior stays
        deterministic.
    contract_file : str, optional
        Path of the contract interface this task owns (e.g.,
        ``src/types/engine.ts``). May be empty / None / whitespace
        — in which case the task declares nothing and passes through
        the registry filter untouched.
    contract_artifacts : dict, optional
        Foundation contract artifacts shared across all tasks.
        Accepted for signature stability with future heuristics; the
        MVP does not consult them (reads are free, never locked).

    Returns
    -------
    list of str
        The declared write targets. Empty when ``contract_file`` is
        missing, None, or whitespace-only.
    """
    # Defensive normalization: callers in the parser already coerce
    # ``contract_file`` to ``str(raw or "")`` but the helper must be
    # robust on its own — it's also called from tests with None.
    if contract_file is None:
        return []
    normalized = _normalize_declared_file_path(contract_file)
    if not normalized:
        return []
    return [normalized]


#: Fixed project-classification taxonomies (Marcus #546 Phase 0).
#:
#: The PRD-analysis LLM call is asked to bucket each project into one
#: of these labels.  Telemetry (``project_created`` event) ships only
#: the bucket — never the project description.  Keeping the taxonomy
#: here, next to the parser that produces it, means the disclosure
#: document (``docs/telemetry.md``) and the code share one source of
#: truth.  Any LLM answer outside the set collapses to ``"other"``;
#: a missing answer collapses to ``"unknown"``.
DOMAIN_BUCKETS: frozenset[str] = frozenset(
    {
        "fintech",
        "healthtech",
        "edtech",
        "ecommerce",
        "social",
        "productivity",
        "devtools",
        "gaming",
        "media",
        "iot",
        "data_analytics",
        "ml_ai",
        "enterprise",
        "consumer",
        "other",
    }
)

STRUCTURAL_CATEGORY_BUCKETS: frozenset[str] = frozenset(
    {
        "web app",
        "data pipeline",
        "CLI tool",
        "game",
        "API service",
        "ML/AI",
        "library",
        "automation",
        "other",
    }
)


def _bucket_label(raw: Any, taxonomy: frozenset[str]) -> str:
    """Collapse an LLM-supplied label to a known taxonomy bucket.

    Parameters
    ----------
    raw : Any
        The value the LLM returned for ``domain`` or
        ``structuralCategory``.  May be ``None``, a non-string, or a
        string with stray casing/whitespace.
    taxonomy : frozenset of str
        The allowed bucket set — :data:`DOMAIN_BUCKETS` or
        :data:`STRUCTURAL_CATEGORY_BUCKETS`.

    Returns
    -------
    str
        ``"unknown"`` when ``raw`` is missing/blank, an exact taxonomy
        member when it matches case-insensitively, otherwise
        ``"other"``.  Never returns free text — this is the privacy
        guard that stops an LLM hallucination from leaking project
        detail through the ``project_created`` telemetry event.
    """
    if raw is None:
        return "unknown"
    text = str(raw).strip()
    if not text:
        return "unknown"
    lowered = text.lower()
    for bucket in taxonomy:
        if bucket.lower() == lowered:
            return bucket
    return "other"


#: Fixed technology-stack taxonomy (Marcus #546 Phase 0).
#:
#: The PRD planner detects technologies named in the project
#: description; :func:`_normalize_tech_stack` collapses each one to a
#: member of this set.  Bucketing is the privacy guard — a free-text
#: label the LLM hallucinated (which could echo a project name)
#: collapses to ``"other"`` rather than being stored verbatim.  The
#: result is local-only today (Phase 0 cost DB); a future telemetry
#: event would ship the buckets safely (deferred — see #563).
#: Kept deliberately coarse: the cost-forecasting model needs broad
#: tech-family signal, not an exhaustive framework catalogue.
TECH_STACK_BUCKETS: frozenset[str] = frozenset(
    {
        # Languages
        "python",
        "javascript",
        "typescript",
        "go",
        "rust",
        "java",
        "ruby",
        "php",
        "csharp",
        "cpp",
        "c",
        "swift",
        "kotlin",
        # Frontend
        "react",
        "vue",
        "angular",
        "svelte",
        "nextjs",
        "html_css",
        # Backend frameworks
        "django",
        "flask",
        "fastapi",
        "express",
        "node",
        "rails",
        "spring",
        "dotnet",
        "laravel",
        # Datastores
        "postgres",
        "mysql",
        "sqlite",
        "mongodb",
        "redis",
        # Infra / devops
        "docker",
        "kubernetes",
        "aws",
        "gcp",
        "azure",
        "terraform",
        # Mobile
        "react_native",
        "flutter",
        "ios",
        "android",
        # ML / data
        "pytorch",
        "tensorflow",
        "pandas",
        # Catch-all
        "other",
    }
)

#: Common aliases mapped to their canonical :data:`TECH_STACK_BUCKETS`
#: member.  Keys are already lower-cased + separator-collapsed (see
#: :func:`_normalize_tech_stack`); only entries that differ from the
#: canonical spelling need to appear here.
_TECH_STACK_ALIASES: Dict[str, str] = {
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "golang": "go",
    "cplusplus": "cpp",
    "c#": "csharp",
    "csharpdotnet": "csharp",
    "c++": "cpp",
    ".net": "dotnet",
    "dotnetcore": "dotnet",
    "nodejs": "node",
    "node.js": "node",
    "postgresql": "postgres",
    "psql": "postgres",
    "mongo": "mongodb",
    "k8s": "kubernetes",
    "next": "nextjs",
    "next.js": "nextjs",
    "nuxt": "vue",
    "html": "html_css",
    "css": "html_css",
    "tailwind": "html_css",
    "rn": "react_native",
    "reactnative": "react_native",
    "htmlcss": "html_css",
    "tf": "tensorflow",
    "amazonwebservices": "aws",
    "googlecloud": "gcp",
}

#: Upper bound on how many distinct tech buckets are kept per project.
#: There are only ~45 buckets so this is a defensive cap, not a
#: realistic limit.
_MAX_TECH_STACK_LABELS: int = 20


def _normalize_tech_stack(raw: Any) -> List[str]:
    """Bucket the LLM's ``detectedTechStack`` answer to known tech labels.

    Each raw label is lower-cased, separator-collapsed, run through
    :data:`_TECH_STACK_ALIASES`, then matched against
    :data:`TECH_STACK_BUCKETS`.  Anything off-taxonomy collapses to
    ``"other"`` — the privacy guard so the result could ship in a
    future telemetry event without risking a free-text leak (#563).

    Parameters
    ----------
    raw : Any
        Whatever the LLM returned for ``detectedTechStack``.  Expected
        to be a list of short strings, but may be ``None``, a bare
        string, or a list with non-string / junk entries.

    Returns
    -------
    list of str
        De-duplicated taxonomy buckets, in first-seen order, at most
        :data:`_MAX_TECH_STACK_LABELS`.  Empty list when ``raw`` is
        missing or contains nothing usable.  Every element is a member
        of :data:`TECH_STACK_BUCKETS` — never free text.
    """
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        # Collapse spaces / underscores / hyphens so "React Native",
        # "react-native" and "react_native" all key the same.
        key = item.strip().lower().replace(" ", "").replace("-", "")
        key = key.replace("_", "")
        if not key:
            continue
        canonical = _TECH_STACK_ALIASES.get(key, key)
        # The bucket set uses an underscore in ``html_css`` /
        # ``react_native``; the separator-collapsed key would miss it,
        # so the alias map carries those forms explicitly.
        bucket = canonical if canonical in TECH_STACK_BUCKETS else "other"
        if bucket in seen:
            continue
        seen.add(bucket)
        out.append(bucket)
        if len(out) >= _MAX_TECH_STACK_LABELS:
            break
    return out


@dataclass
class PRDAnalysis:
    """Deep analysis of a PRD document."""

    functional_requirements: List[Dict[str, Any]]
    non_functional_requirements: List[Dict[str, Any]]
    technical_constraints: List[str]
    business_objectives: List[str]
    user_personas: List[Dict[str, Any]]
    success_metrics: List[str]
    implementation_approach: str
    complexity_assessment: Dict[str, Any]
    risk_factors: List[Dict[str, Any]]
    confidence: float
    original_description: str = ""  # NEW: Preserve original user description
    integration_requirements: List[Dict[str, Any]] = field(default_factory=list)
    # Coarse project classification produced by the same PRD-analysis
    # LLM call (Marcus #546 Phase 0).  Bucketed against fixed
    # taxonomies so telemetry never ships a free-text label — see
    # ``DOMAIN_BUCKETS`` / ``STRUCTURAL_CATEGORY_BUCKETS``.  Default
    # ``"unknown"`` when the LLM omits the field or returns junk.
    domain: str = "unknown"
    structural_category: str = "unknown"
    # Technologies the planner detected in the PRD, taxonomy-bucketed
    # to :data:`TECH_STACK_BUCKETS` (e.g. ["python", "react",
    # "postgres"]).  Persisted to the cost DB for Phase 0 forecasting
    # groundwork — local-only today; central reporting is deferred
    # (#563).  Every element is a fixed bucket, never free text.
    detected_tech_stack: List[str] = field(default_factory=list)
    # User-visible outcomes the product must satisfy (issue #449).
    # Populated by ``extract_user_outcomes`` when
    # MARCUS_OUTCOME_COVERAGE is on; otherwise an empty list.  Both
    # decomposer paths read this field and feed it through
    # ``apply_outcome_coverage`` after task generation.
    user_outcomes: List[UserOutcome] = field(default_factory=list)


@dataclass
class TaskGenerationResult:
    """Result of PRD-to-tasks conversion."""

    tasks: List[Task]
    task_hierarchy: Dict[str, List[str]]  # parent_id -> [child_ids]
    dependencies: List[Dict[str, Any]]
    risk_assessment: Dict[str, Any]
    estimated_timeline: Dict[str, Any]
    resource_requirements: Dict[str, Any]
    success_criteria: List[str]
    generation_confidence: float
    # Intent fidelity (issue #449).  ``intent_fidelity_score`` is the
    # final score on the augmented task graph after gap-fill (or
    # initial-coverage score when no gaps).  ``None`` when outcome
    # coverage was disabled or no outcomes were extracted.  The
    # coverage maps are exposed for telemetry / logging.
    intent_fidelity_score: Optional[float] = None
    coverage_before_fill: Dict[str, List[str]] = field(default_factory=dict)
    coverage_after_fill: Optional[Dict[str, List[str]]] = None
    gap_filled_outcomes: List[str] = field(default_factory=list)
    # Coarse project classification copied from ``PRDAnalysis`` so
    # callers that only see the task-generation result (e.g. the
    # project creator) can forward it to Phase 0 persistence and the
    # ``project_created`` telemetry event without re-reading the PRD
    # analysis object.  Always taxonomy-bucketed (#546 Phase 0).
    domain: str = "unknown"
    structural_category: str = "unknown"
    # Detected technology labels, copied from ``PRDAnalysis`` (#546).
    detected_tech_stack: List[str] = field(default_factory=list)


@dataclass
class ProjectConstraints:
    """Constraints for task generation."""

    deadline: Optional[datetime] = None
    budget_limit: Optional[float] = None
    team_size: int = 3
    available_skills: Optional[List[str]] = None
    technology_constraints: Optional[List[str]] = None
    quality_requirements: Optional[Dict[str, Any]] = None
    deployment_target: str = "local"  # local, dev, prod, remote
    complexity_mode: str = "standard"  # prototype, standard, enterprise

    def __post_init__(self) -> None:
        """Initialize post-creation."""
        if self.available_skills is None:
            self.available_skills = []
        if self.technology_constraints is None:
            self.technology_constraints = []
        if self.quality_requirements is None:
            self.quality_requirements = {}
        # Validate deployment target
        if self.deployment_target not in ["local", "dev", "prod", "remote"]:
            self.deployment_target = "local"


class AdvancedPRDParser:
    """
    Advanced PRD parser that converts natural language requirements.

    Converts requirements into complete task breakdown with intelligent
    dependencies and risk assessment.
    """

    def __init__(
        self,
        hybrid_config: Optional[HybridInferenceConfig] = None,
        memory: Optional[Any] = None,
    ):
        self.llm_client = LLMAbstraction()
        self.memory = memory  # Store memory system for learning durations

        # Set up hybrid dependency inference with configurable thresholds
        ai_engine = (
            AIAnalysisEngine()
            if hybrid_config and hybrid_config.enable_ai_inference
            else None
        )
        self.dependency_inferer = HybridDependencyInferer(ai_engine, hybrid_config)

        # PRD parsing configuration
        self.max_tasks_per_epic = 8
        self.min_task_complexity_hours = 1
        self.max_task_complexity_hours = 40

        # Task pattern constants
        self.TASK_TYPE_DESIGN = "design"
        self.TASK_TYPE_IMPLEMENTATION = "implementation"
        self.TASK_TYPE_TESTING = "testing"

        # Complexity mode constants
        self.VALID_COMPLEXITY_MODES = ["prototype", "standard", "enterprise"]
        self.VALID_COMPLEXITIES = ["atomic", "simple", "coordinated", "distributed"]

        # Standard project phases for task organization
        self.standard_phases = [
            "research_and_planning",
            "design_and_architecture",
            "setup_and_configuration",
            "core_development",
            "integration_and_testing",
            "deployment_and_launch",
            "monitoring_and_optimization",
        ]

        # Risk assessment categories
        self.risk_categories = [
            "technical_complexity",
            "integration_challenges",
            "performance_requirements",
            "security_concerns",
            "scalability_needs",
            "external_dependencies",
            "timeline_pressure",
            "resource_constraints",
        ]

        logger.info("Advanced PRD parser initialized")

    def _get_learned_task_duration(
        self, task_type: str, default_minutes: float = 6.0
    ) -> float:
        """
        Get median task duration from historical data.

        Uses memory system to query learned median completion times.
        Falls back to default if no historical data available.

        Parameters
        ----------
        task_type : str
            Task type: "design", "implement", "test", etc.
        default_minutes : float
            Default duration in minutes if no learned data available

        Returns
        -------
        float
            Estimated duration in minutes
        """
        try:
            if self.memory:
                # Query memory system for median duration
                median_hours = self.memory.get_median_duration_by_type(task_type)
                if median_hours is not None:
                    # Convert hours to minutes
                    learned_minutes: float = float(median_hours) * 60
                    logger.info(
                        f"Using learned duration for {task_type}: "
                        f"{learned_minutes:.1f} minutes "
                        f"(from {median_hours:.3f} hours)"
                    )
                    return learned_minutes
                else:
                    logger.debug(
                        f"No learned duration for {task_type}, "
                        f"using default: {default_minutes} minutes"
                    )
        except Exception as e:
            logger.warning(
                f"Failed to get learned duration for {task_type}: {e}. "
                f"Using default: {default_minutes} minutes"
            )

        # Fallback to default
        return default_minutes

    def _build_augmenter_chain(
        self, complexity_mode: Optional[str] = None
    ) -> Sequence[GraphAugmenter]:
        """Build the canonical pre-inference augmenter chain.

        Single source of truth for the chain order, used by both
        :meth:`parse_prd_to_tasks` (feature-based) and
        :meth:`decompose_by_contract` (contract-first).  Order is
        load-bearing: ``SpecCoverageAugmenter`` runs after
        ``OutcomeCoverageAugmenter`` so it sees any outcome gap-fill
        tasks when scoring spec feature coverage (locked by
        ``test_second_augmenter_sees_first_augmenter_tasks``).

        Parameters
        ----------
        complexity_mode : Optional[str]
            Forwarded to :class:`SpecCoverageAugmenter`, where it is now a
            no-op: issue #666 removed the prototype skip so spec_coverage
            runs on every mode.  Retained for call-site compatibility and
            removable in a follow-up.

        Returns
        -------
        Sequence[GraphAugmenter]
            ``[OutcomeCoverageAugmenter, SpecCoverageAugmenter]`` —
            the production chain.  Future augmenters (NFR coverage,
            security checks) can join here behind the same single
            registration site.
        """
        return [
            OutcomeCoverageAugmenter(llm_client=self.llm_client),
            SpecCoverageAugmenter(complexity_mode=complexity_mode),
        ]

    async def parse_prd_to_tasks(
        self, prd_content: str, constraints: ProjectConstraints
    ) -> TaskGenerationResult:
        """
        Convert PRD into complete task breakdown with dependencies.

        Args
        ----
            prd_content: Full PRD document content
            constraints: Project constraints and limitations

        Returns
        -------
            Complete task generation result with breakdown and analysis
        """
        logger.info("Starting advanced PRD parsing and task generation")

        # Step 1: Deep PRD analysis (with complexity-aware enrichment)
        prd_analysis = await self._analyze_prd_deeply(prd_content, constraints)

        # Step 2: Generate task hierarchy
        req_count = len(prd_analysis.functional_requirements)
        req_ids = [
            req.get("id", req.get("name", "unknown"))
            for req in prd_analysis.functional_requirements
        ]
        logger.info(
            f"PRD analysis found {req_count} functional requirements: {req_ids}"
        )
        task_hierarchy = await self._generate_task_hierarchy(prd_analysis, constraints)

        # Step 3: Create detailed tasks
        logger.info(
            f"Creating detailed tasks from hierarchy with {len(task_hierarchy)} epics"
        )
        tasks = await self._create_detailed_tasks(
            task_hierarchy, prd_analysis, constraints
        )
        logger.info(f"Created {len(tasks)} detailed tasks")

        # Step 3.5: Run the augmenter chain (issue #456) BEFORE
        # dependency inference so synthesized gap-fill tasks are
        # treated as first-class members of the graph by
        # ``_infer_smart_dependencies``.  Chain order is load-bearing:
        # ``SpecCoverageAugmenter`` runs after ``OutcomeCoverageAugmenter``
        # so it sees any outcome gap-fill tasks when scoring spec
        # feature coverage.  ``contract_artifacts=None`` selects the
        # feature-based outcome-coverage path; spec_coverage ignores
        # the parameter (operates on spec text, not contracts).
        augmenter_result = await run_augmenter_chain(
            self._build_augmenter_chain(complexity_mode=constraints.complexity_mode),
            prd_analysis=prd_analysis,
            tasks=tasks,
            contract_artifacts=None,
        )
        tasks = augmenter_result.augmented_tasks
        # Telemetry is namespaced by augmenter name; pull the
        # outcome_coverage slice for the legacy
        # ``TaskGenerationResult`` fields below.  Empty dict when
        # the augmenter no-opped (flag off / no outcomes / LLM error).
        oc_telemetry: Dict[str, Any] = augmenter_result.telemetry.get(
            "outcome_coverage", {}
        )

        # Step 4: AI-powered dependency inference
        dependencies = await self._infer_smart_dependencies(tasks, prd_analysis)

        # Step 5: Risk assessment and timeline prediction
        risk_assessment = await self._assess_implementation_risks(
            tasks, prd_analysis, constraints
        )
        timeline_prediction = await self._predict_timeline(
            tasks, dependencies, constraints
        )

        # Step 6: Resource requirement analysis
        resource_requirements = await self._analyze_resource_requirements(
            tasks, prd_analysis, constraints
        )

        # Step 7: Generate success criteria
        success_criteria = await self._generate_success_criteria(prd_analysis, tasks)

        return TaskGenerationResult(
            tasks=tasks,
            task_hierarchy=task_hierarchy,
            dependencies=dependencies,
            risk_assessment=risk_assessment,
            estimated_timeline=timeline_prediction,
            resource_requirements=resource_requirements,
            success_criteria=success_criteria,
            # Telemetry pulled from the chain's outcome_coverage
            # namespace (or empty dict if the augmenter no-opped).
            # The wrapper has already extracted .id from each gap,
            # flattened to the canonical event-payload shape, and
            # pinned the four keys against PLANNING_INTENT_FIDELITY.
            intent_fidelity_score=oc_telemetry.get("intent_fidelity_score"),
            coverage_before_fill=oc_telemetry.get("coverage_before_fill", {}),
            coverage_after_fill=oc_telemetry.get("coverage_after_fill"),
            gap_filled_outcomes=oc_telemetry.get("gap_filled_outcomes", []),
            domain=prd_analysis.domain,
            structural_category=prd_analysis.structural_category,
            detected_tech_stack=prd_analysis.detected_tech_stack,
            generation_confidence=self._calculate_generation_confidence(
                prd_analysis, tasks
            ),
        )

    async def decompose_by_contract(
        self,
        prd_analysis: PRDAnalysis,
        contract_artifacts: Dict[str, Optional[Dict[str, Any]]],
        constraints: Optional[ProjectConstraints] = None,
        pre_existing_tasks: Optional[List[Task]] = None,
    ) -> AugmentationResult:
        """
        Contract-first task decomposition (GH-320 PR 2).

        Given a PRD analysis and pre-generated contract artifacts (one per
        domain), produce a list of Task objects where each task owns exactly
        one side of a contract interface. This is the productionized
        mechanism that experiment 1 (2026-04-10, hand-crafted TypeScript
        contract) validated for tightly-coupled problems.

        The key difference from ``parse_prd_to_tasks``:

        - Feature-based (``parse_prd_to_tasks``): tasks are shaped by
          functional requirements. Multiple tasks can end up editing the
          same file when features are tightly coupled — one agent absorbs
          the work and produces a Single-Author Product.
        - Contract-first (``decompose_by_contract``): tasks are shaped by
          contract interfaces. Each task carries a ``responsibility`` field
          naming the interface it owns from the shared contract artifact.
          The agent prompt frames the task as ownership of that interface,
          and the agent reads the contract before writing code. File
          ownership emerges naturally from contract ownership — two agents
          implementing two sides of a contract land in different files
          because the contract interface was already the boundary.

        The caller is responsible for running contract generation
        (``_generate_contracts_by_domain``) before calling this method.
        This method trusts the contracts as input and builds tasks around
        them — it does not regenerate contracts.

        Parameters
        ----------
        prd_analysis : PRDAnalysis
            Deep PRD analysis produced by ``_analyze_prd_deeply``. Used
            for project description, complexity assessment, and NFR
            awareness.
        contract_artifacts : Dict[str, Dict[str, Any]]
            Output of ``_generate_contracts_by_domain``: a mapping of
            ``domain_name -> {"artifacts": [...], "decisions": [...]}``.
            Each artifact has ``filename``, ``content``, ``artifact_type``,
            ``relative_path``. Domains where contract generation produced
            no output map to ``None``.
        constraints : Optional[ProjectConstraints]
            Project constraints (complexity mode, deployment target, etc.).
            If omitted, defaults are used.

        Returns
        -------
        AugmentationResult
            ``augmented_tasks`` is the contract-owned tasks (with
            ``responsibility`` field set), in the order produced by
            the LLM, plus any synthesized gap-fill tasks when the
            outcome-coverage augmenter ran.  ``telemetry`` is
            namespaced by augmenter name (e.g.
            ``{"outcome_coverage": {"intent_fidelity_score": ...}}``)
            when an augmenter ran, empty otherwise.  Dependencies
            between tasks use the ``provides``/``requires``
            cross-parent wiring mechanism.  The caller is responsible
            for assigning kanban-backed IDs and creating cards.

        Raises
        ------
        ValueError
            If ``contract_artifacts`` is empty, or if every domain produced
            None. This indicates contract generation was a complete
            failure and the caller should fall back to feature-based
            decomposition with a visible warning.
        RuntimeError
            If the LLM call fails after retries or returns a response
            that cannot be parsed as a valid task list.

        Notes
        -----
        The LLM call uses structured output with a JSON schema. See
        ``_build_contract_decomposition_prompt`` for the prompt template.

        This method does not handle the fallback itself — the caller (in
        ``NaturalLanguageProjectCreator``) decides whether to fall back to
        feature-based decomposition based on whether this method raises
        and whether the contract quality threshold is met.

        See Also
        --------
        _generate_contracts_by_domain : Upstream contract generation.
        parse_prd_to_tasks : Feature-based decomposition (default path).
        _build_contract_decomposition_prompt : LLM prompt template.

        References
        ----------
        GH-320 : Contract-first task decomposition.
        Experiment 1 (2026-04-10) : Validated mechanism on hand-crafted
            TypeScript contract with snake game (30/70 split, clean merge).
        """
        # Drop empty domain results so the LLM sees only real contracts.
        usable_contracts = {
            domain: payload
            for domain, payload in contract_artifacts.items()
            if payload is not None and payload.get("artifacts")
        }

        if not usable_contracts:
            raise ValueError(
                "contract_artifacts has no usable domains — contract "
                "generation produced no artifacts for any domain. "
                "Caller should fall back to feature-based decomposition."
            )

        logger.info(
            f"[decompose_by_contract] Decomposing with "
            f"{len(usable_contracts)} contract domain(s)"
        )

        prompt = self._build_contract_decomposition_prompt(
            prd_analysis=prd_analysis,
            contract_artifacts=usable_contracts,
        )

        response_format = {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": (
                                    "Short imperative task name, e.g. "
                                    "'Implement GameEngine'"
                                ),
                            },
                            "description": {
                                "type": "string",
                                "description": (
                                    "Full task description explaining "
                                    "what to build, reading from the "
                                    "contract."
                                ),
                            },
                            "responsibility": {
                                "type": "string",
                                "description": (
                                    "Contract interface or module this "
                                    "task owns, e.g. 'implements "
                                    "GameEngine interface from "
                                    "src/types.ts'. Must reference an "
                                    "actual interface from the provided "
                                    "contract artifacts."
                                ),
                            },
                            "contract_file": {
                                "type": "string",
                                "description": (
                                    "Relative path to the contract "
                                    "artifact this task reads from."
                                ),
                            },
                            "product_intent": {
                                "type": "string",
                                "description": (
                                    "One-sentence plain-language "
                                    "statement of WHY this task exists "
                                    "from the user's perspective. "
                                    "Phase 1 framing layer — surfaced "
                                    "in the agent's task instructions "
                                    "alongside the contract "
                                    "responsibility so agents treat the "
                                    "contract as a boundary, not a "
                                    "spec."
                                ),
                            },
                            "provides": {
                                "type": "string",
                                "description": (
                                    "Semantic description of what this "
                                    "task delivers to downstream tasks."
                                ),
                            },
                            "requires": {
                                "type": "string",
                                "description": (
                                    "Semantic description of what this "
                                    "task needs from upstream tasks. "
                                    "'None' if no prerequisites."
                                ),
                            },
                            "estimated_minutes": {
                                "type": "number",
                                "description": (
                                    "Reality-based estimate in minutes "
                                    "(typically 4-15 for contract-first "
                                    "tasks)."
                                ),
                            },
                            "acceptance_criteria": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Verifiable outcomes restated from "
                                    "the contract. Only include what "
                                    "the contract actually specifies — "
                                    "do not add requirements beyond "
                                    "the contract. Do not prescribe "
                                    "implementation approach, method "
                                    "names, or code patterns. Focus "
                                    "on observable outcomes the "
                                    "validator can check: what the "
                                    "module exposes, what data it "
                                    "produces, what boundary it "
                                    "satisfies. The contract is the "
                                    "source of truth."
                                ),
                            },
                        },
                        "required": [
                            "name",
                            "description",
                            "responsibility",
                            "contract_file",
                            "product_intent",
                            "provides",
                            "requires",
                            "estimated_minutes",
                            "acceptance_criteria",
                        ],
                    },
                },
            },
            "required": ["tasks"],
        }

        system_prompt = (
            "You are a software architect decomposing a project into "
            "agent-owned tasks based on shared interface contracts. "
            "Each task you produce MUST own exactly one contract "
            "interface. You are NOT telling agents which files to edit — "
            "you are telling them which interface they own. File "
            "ownership emerges naturally from contract ownership. "
            "Respond with structured JSON matching the provided schema."
        )

        # Use AIAnalysisEngine for structured output. LLMAbstraction
        # only exposes ``analyze`` which returns unstructured text.
        ai_engine = AIAnalysisEngine()
        try:
            response = await ai_engine.generate_structured_response(
                prompt=prompt,
                system_prompt=system_prompt,
                response_format=response_format,
                operation="generate_contracts",
            )
        except Exception as e:
            raise RuntimeError(f"Contract decomposition LLM call failed: {e}") from e

        # generate_structured_response returns a Dict per its type
        # signature.
        response_data: Dict[str, Any] = response

        raw_tasks = response_data.get("tasks", [])
        if not raw_tasks:
            raise RuntimeError("Contract decomposition LLM response contained no tasks")

        # Decomposer over-fragmentation guard (#658 follow-up).
        #
        # The prompt instructs the LLM: "Produce exactly one task per
        # contract boundary listed above." The LLM has circumvented
        # this in two observed shapes:
        #
        # 1. ``snake-decomposer-1`` (2026-05-26) — LLM produced 5
        #    tasks for 2 contracts where 3 tasks all pointed at the
        #    same ``contract_file`` string. Dedupe by raw
        #    ``contract_file`` caught this.
        #
        # 2. ``snake-663-658`` (2026-05-27) — LLM produced 3 tasks
        #    for 2 contracts where the three contract_files were
        #    DIFFERENT strings but two of them named the same DOMAIN
        #    via different artifact types of that domain
        #    (``...game-core-engine-interface-contracts.md`` and
        #    ``...game-core-engine-architecture.md``). Dedupe by
        #    raw string missed this because the filenames differ;
        #    the third task ran in parallel, wrote to a sibling
        #    agent's file, and merge-conflicted.
        #
        # The fix dedupes by DOMAIN SLUG (extracted via
        # ``_extract_contract_domain_slug``), which collapses all
        # artifact-type variants of the same domain into one key.
        # The LLM stays in charge of description, product_intent,
        # acceptance criteria — everything that genuinely benefits
        # from LLM creativity. It just doesn't get to invent extra
        # task slots, regardless of which artifact type it names.
        #
        # Tasks lacking ``contract_file`` are treated as a violation
        # of the prompt's "Each task owns exactly one contract
        # boundary" rule and dropped.
        contract_count = len(usable_contracts)
        if len(raw_tasks) > contract_count:
            seen_domain_slugs: set[str] = set()
            deduped: List[Dict[str, Any]] = []
            for raw in raw_tasks:
                cf = (
                    str(raw.get("contract_file") or "").strip()
                    if isinstance(raw, dict)
                    else ""
                )
                if not cf:
                    logger.warning(
                        "[decompose_by_contract] dropping LLM task with "
                        "no contract_file: name=%r — violates 'one "
                        "task per contract' rule",
                        raw.get("name") if isinstance(raw, dict) else None,
                    )
                    continue
                domain_slug = _extract_contract_domain_slug(cf)
                if not domain_slug:
                    # Defensive: a contract_file we can't parse a
                    # domain from. Fall back to raw string so
                    # behavior degrades to the pre-domain-slug
                    # guard rather than silently passing.
                    domain_slug = cf
                if domain_slug in seen_domain_slugs:
                    logger.warning(
                        "[decompose_by_contract] dropping duplicate task "
                        "'%s' — domain '%s' (contract_file %s) already "
                        "claimed by earlier task (prompt requires "
                        "exactly one task per contract boundary)",
                        raw.get("name"),
                        domain_slug,
                        cf,
                    )
                    continue
                seen_domain_slugs.add(domain_slug)
                deduped.append(raw)

            logger.info(
                "[decompose_by_contract] over-fragmentation guard fired: "
                "LLM produced %d tasks for %d contracts → kept %d unique "
                "tasks after dedupe by domain slug",
                len(raw_tasks),
                contract_count,
                len(deduped),
            )
            raw_tasks = deduped

        # Build Task objects. IDs are synthetic — caller replaces with
        # kanban UUIDs when creating cards.
        constraints = constraints or ProjectConstraints()
        complexity_mode = constraints.complexity_mode
        tasks: List[Task] = []
        now = datetime.now(timezone.utc)

        for idx, raw in enumerate(raw_tasks, start=1):
            # Defensive coercion for every field we read from the LLM.
            # ``generate_structured_response`` parses JSON but does NOT
            # enforce the schema, so malformed-but-parseable responses
            # can leak through with wrong types: ``estimated_minutes:
            # null``, non-string ``description``, missing keys, etc.
            # Convert everything to the expected type or a safe
            # default, and raise RuntimeError on unrecoverable shape
            # drift so the caller's fallback path triggers cleanly.
            # Codex caught this on PR #327 review.
            if not isinstance(raw, dict):
                raise RuntimeError(
                    f"Contract decomposition task {idx} is not a "
                    f"dict: {type(raw).__name__}"
                )

            raw_minutes = raw.get("estimated_minutes")
            try:
                estimated_minutes = (
                    float(raw_minutes) if raw_minutes is not None else 8.0
                )
            except (TypeError, ValueError):
                estimated_minutes = 8.0
            estimated_hours = estimated_minutes / 60.0

            contract_file = str(raw.get("contract_file") or "")
            responsibility = str(raw.get("responsibility") or "")
            # Phase 1 framing field (GH-320): one-sentence product
            # intent surfaces in the agent's task instructions
            # alongside the contract responsibility so agents treat
            # the contract as a boundary, not a spec. Optional — if
            # the LLM omits it (older prompts, malformed response),
            # the instructions layer falls back to showing just the
            # contract responsibility without the intent preamble.
            raw_product_intent = raw.get("product_intent")
            product_intent = (
                str(raw_product_intent).strip()
                if raw_product_intent is not None
                else ""
            )

            raw_description = raw.get("description")
            description = (
                str(raw_description).strip() if raw_description is not None else ""
            )
            # Embed a structured marker in the description so the
            # contract-first metadata survives round-tripping through
            # kanban providers that don't persist Task.responsibility
            # or Task.source_context as top-level fields (e.g. Planka).
            # Agent prompt surfacing (build_tiered_instructions) parses
            # these markers as a fallback when task.responsibility is
            # absent after reload. Codex caught this on PR #327 review.
            # See ``_parse_contract_metadata`` in marcus_mcp/tools/task.py.
            #
            # Phase 1 adds product_intent to the marker so framing
            # survives the same round-trip even when source_context
            # doesn't persist. Empty intent is omitted from the
            # marker to keep the marker terse for legacy tasks.
            if responsibility or contract_file:
                marker_lines = [
                    "<!-- MARCUS_CONTRACT_FIRST",
                    f"responsibility: {responsibility}",
                    f"contract_file: {contract_file}",
                ]
                if product_intent:
                    marker_lines.append(f"product_intent: {product_intent}")
                marker_lines.append("-->")
                description = f"{description}\n\n" + "\n".join(marker_lines)

            raw_provides = raw.get("provides")
            provides = str(raw_provides) if raw_provides is not None else None

            raw_requires = raw.get("requires")
            if raw_requires is None or raw_requires == "None":
                requires: Optional[str] = None
            else:
                requires = str(raw_requires)

            raw_name = raw.get("name")
            name = str(raw_name) if raw_name is not None else f"Contract Task {idx}"

            # Parse acceptance criteria from the LLM's structured
            # output. The schema instructs the LLM to restate
            # contract requirements as verifiable outcomes — not
            # implementation details. Defensive: coerce to list of
            # strings, skip non-string elements.
            raw_criteria = raw.get("acceptance_criteria", [])
            if not isinstance(raw_criteria, list):
                raw_criteria = []
            acceptance_criteria = [
                str(c).strip() for c in raw_criteria if c is not None and str(c).strip()
            ]

            task = Task(
                id=f"contract_task_{idx}",
                name=name,
                description=description,
                status=TaskStatus.TODO,
                priority=Priority.HIGH,
                assigned_to=None,
                created_at=now,
                updated_at=now,
                due_date=None,
                estimated_hours=estimated_hours,
                labels=["contract_first", "implementation"],
                acceptance_criteria=acceptance_criteria,
                source_type="contract_first",
                source_context={
                    "contract_file": contract_file,
                    # #206 MVP: which file(s) this task is authorized to
                    # write. request_next_task consults this to skip
                    # tasks whose declared files are currently held by
                    # another in-progress task, preventing the
                    # verify-snake-4 class of merge conflicts. MVP is
                    # conservative — declared_files == [contract_file]
                    # when present, [] otherwise. See
                    # ``_extract_declared_files`` above.
                    "declared_files": _extract_declared_files(
                        responsibility=responsibility,
                        contract_file=contract_file,
                        contract_artifacts=contract_artifacts,
                    ),
                    "complexity_mode": complexity_mode,
                    # Also persist responsibility here so it round-trips
                    # through kanban providers that don't yet serialize
                    # ``Task.responsibility`` as a top-level field.
                    # ``build_tiered_instructions`` reads both sources
                    # when surfacing the CONTRACT RESPONSIBILITY layer.
                    "responsibility": responsibility,
                    # Phase 1 product-intent field (GH-320 framing
                    # layer). When present, build_tiered_instructions
                    # surfaces it as the "WHY THIS EXISTS" section
                    # above the contract responsibility to frame the
                    # contract as a coordination boundary rather than
                    # a prescriptive spec. Empty string is the
                    # sentinel for "no intent provided" — the
                    # instructions layer falls back gracefully.
                    "product_intent": product_intent,
                },
                provides=provides,
                requires=requires,
                responsibility=responsibility,
            )
            tasks.append(task)

        logger.info(
            f"[decompose_by_contract] Produced {len(tasks)} "
            f"contract-owned tasks from {len(usable_contracts)} domains"
        )

        # Issue #456: route both coverage augmenters through the
        # chain.  Chain order is load-bearing — spec_coverage sees
        # outcome_coverage's gap-fill tasks when scoring spec feature
        # coverage.  The chain's ``AugmentationResult`` carries:
        #
        # - ``augmented_tasks``: pre_existing + contract tasks plus
        #   any synthesized outcome / spec gap-fill tasks
        # - ``synthesized_ids``: IDs of every task the chain added
        # - ``telemetry``: namespaced by augmenter name, e.g.
        #   ``{"outcome_coverage": {intent_fidelity_score: ...},
        #     "spec_coverage": {spec_gap_count: ...}}``
        #
        # Codex P2 on PR #473: ``pre_existing_tasks`` carries
        # foundation tasks synthesized pre-fork by
        # ``_synthesize_shared_foundation``.  Including them in the
        # chain input makes them visible to spec_coverage's keyword
        # scan, preventing duplicate spec_gap synthesis when a
        # foundation task already implements a spec feature (e.g.
        # "Set up Auth foundation" covers spec keyword "auth").
        # Pre-Stage-4 the post-safety-check call site saw the
        # combined graph; threading foundation here restores that
        # visibility while keeping the augmenters inside the
        # decomposer.
        #
        # Behind MARCUS_OUTCOME_COVERAGE; both augmenters no-op with
        # empty telemetry on flag off / no outcomes / LLM error.
        chain_input_tasks = list(pre_existing_tasks or []) + tasks
        return await run_augmenter_chain(
            self._build_augmenter_chain(complexity_mode=constraints.complexity_mode),
            prd_analysis=prd_analysis,
            tasks=chain_input_tasks,
            contract_artifacts=contract_artifacts,
        )

    def _build_contract_decomposition_prompt(
        self,
        prd_analysis: PRDAnalysis,
        contract_artifacts: Dict[str, Dict[str, Any]],
    ) -> str:
        """
        Build the LLM prompt for contract-first decomposition.

        Embeds the project description and the full content of each contract
        artifact. Instructs the LLM to produce one task per contract
        boundary — task count is determined by the number of domain
        contracts, not by agent count (which is computed post-decomposition
        via CPM and returned as ``recommended_agents`` in the API response).

        Parameters
        ----------
        prd_analysis : PRDAnalysis
            PRD analysis with original description and complexity.
        contract_artifacts : Dict[str, Dict[str, Any]]
            Domain-keyed contract artifacts (only usable ones, per
            ``decompose_by_contract`` filtering).

        Returns
        -------
        str
            Fully rendered prompt text.

        Notes
        -----
        The prompt includes ALL contract content inline rather than
        summaries because downstream decomposition quality depends on the
        LLM seeing the actual interface signatures, types, and
        documentation. Truncation risk is managed by the contract
        generation prompt (``_ARTIFACT_PROMPT``) which already targets
        bounded artifacts.
        """
        contract_sections = []
        for domain_name, payload in contract_artifacts.items():
            artifacts = payload.get("artifacts", [])
            artifact_blocks = []
            for art in artifacts:
                filename = art.get("filename", "unknown")
                relative_path = art.get("relative_path", filename)
                artifact_type = art.get("artifact_type", "artifact")
                content = art.get("content", "")
                artifact_blocks.append(
                    f"### {filename} ({artifact_type})\n"
                    f"Path: {relative_path}\n\n"
                    f"```\n{content}\n```"
                )
            contract_sections.append(
                f"## Domain: {domain_name}\n\n" + "\n\n".join(artifact_blocks)
            )

        contracts_text = "\n\n---\n\n".join(contract_sections)

        return f"""You are decomposing a software project into tasks based \
on shared interface contracts.

## Project
{prd_analysis.original_description or "Project description unavailable"}

## Product Intent (READ THIS FIRST — it governs everything below)

The contracts in the next section are COORDINATION BOUNDARIES, not \
build specifications. They exist so parallel agents don't collide on \
shared surfaces. They do NOT describe the full job.

For each task you generate, the task description and product_intent \
fields together MUST carry the user-facing intent of the work:

1. The description field leads with the user-facing outcome — what \
the user sees, feels, or experiences when this works. Example: \
"users open the dashboard and see current weather updating every 10 \
minutes" — NOT "implements WeatherProvider interface with \
getCurrentWeather and getForecast methods."

2. The product_intent field is a one-sentence restatement of WHY \
this task exists from the user's perspective. Example: "the user \
checks the weather before leaving the house" — NOT "exposes the \
WeatherData shape to consumers." Product intent survives into the \
agent's task instructions as a reminder that the contract is a \
boundary, not a spec.

3. Each task implementer has LATITUDE over everything the contract \
does not explicitly govern: UI framework choice, loading/error \
states, styling, mock data strategy, helper methods, internal \
architecture. The contract only constrains the coordination surface \
shared with other agents. Agents building contract-first tasks should \
use professional judgment for everything outside that surface — just \
like they would on a feature-based task.

Treat the contract as a FLOOR (minimum to coordinate), not a CEILING \
(maximum to build). Do not enumerate contract methods in the \
description — the contract file itself lists them and the agent will \
read it. The description is for intent and framing.

## Contracts (Coordination Boundaries)

The following contracts define the interface surfaces where agents \
must agree to avoid collision. An agent owning a contract owns ONE \
side of ONE boundary — they must honor the shape, but they retain \
autonomy over HOW they implement it, what helper methods they add, \
what the UI looks like, and how they handle everything outside the \
contract.

{contracts_text}

## Decomposition Requirements

- Produce exactly one task per contract boundary listed above. \
Do not merge boundaries or split a single boundary across tasks.
- Each task owns exactly one contract boundary from the section
  above.
- Task descriptions must frame the work as user-facing outcomes
  first, contract obligations second.
- product_intent must be a single sentence naming the user-facing
  reason this task exists. Keep it plain-language and under 30
  words.
- DO NOT enumerate methods in the description. The contract file
  lists them; the description is for intent.
- DO NOT tell agents which files to write. Tell them which boundary
  they own. File structure emerges from ownership.
- Prefer natural splits (producer/consumer, frontend/backend,
  model/logic).
- Set provides/requires to describe semantic dependency relationships
  between tasks. Tasks without prerequisites set requires="None".
- Estimated minutes reality-based (4-15 min typical).

## Output Format

Return a JSON object with a "tasks" array. Each task has:
- name: Short imperative name
- description: User-facing framing first, contract obligation second
- responsibility: Contract interface owned (must reference an actual
  interface from above)
- contract_file: Relative path to the contract artifact
- product_intent: One-sentence plain-language statement of WHY this
  task exists from the user's perspective (not the interface's)
- provides: Semantic description of what this task delivers
- requires: Semantic description of what this task needs (or "None")
- estimated_minutes: Reality-based estimate

Return ONLY the JSON object. Do not include commentary.
"""

    async def _analyze_prd_deeply(
        self, prd_content: str, constraints: ProjectConstraints
    ) -> PRDAnalysis:
        """Perform deep analysis of PRD using AI with complexity-aware enrichment."""
        # Pipe-separated classification options for the prompt schema,
        # built from the taxonomy constants so the prompt and the
        # ``_bucket_label`` validator can never drift apart (#546).
        _domain_options = "|".join(sorted(DOMAIN_BUCKETS))
        _structural_options = "|".join(sorted(STRUCTURAL_CATEGORY_BUCKETS))
        # nosec B608: This is an AI prompt template, not SQL
        analysis_prompt = f"""
        Analyze this Product Requirements Document in detail:

        {prd_content}

        Provide a comprehensive analysis in the following EXACT JSON format:
        {{
            "functionalRequirements": [
                {{
                    "id": "unique_feature_id",
                    "name": "Feature Name",
                    "description": "Detailed description of the feature",
                    "priority": "high|medium|low",
                    "complexity": "atomic|simple|coordinated|distributed",
                    "requires_design_artifacts": true|false,
                    "affected_components": ["component1", "component2"]
                }}
            ],
            "integrationRequirements": [
                {{
                    "id": "integration_id",
                    "name": "Integration Name",
                    "description": "Description of integration/delivery mechanism",
                    "priority": "high|medium|low",
                    "complexity": "atomic|simple|coordinated|distributed",
                    "requires_design_artifacts": true|false,
                    "affected_components": ["component1", "component2"]
                }}
            ],
            "nonFunctionalRequirements": [
                {{
                    "id": "nfr_id",
                    "name": "Requirement Name",
                    "description": "Detailed description",
                    "category": "performance|security|usability|scalability"
                }}
            ],
            "technicalConstraints": ["constraint1", "constraint2"],
            "businessObjectives": ["objective1", "objective2"],
            "userPersonas": [
                {{
                    "name": "Persona Name",
                    "description": "Persona description",
                    "needs": ["need1", "need2"]
                }}
            ],
            "successMetrics": ["metric1", "metric2"],
            "implementationApproach": "agile|waterfall|iterative",
            "complexityAssessment": {{
                "technical": "low|medium|high",
                "timeline": "days|weeks|months",
                "resources": "small|medium|large"
            }},
            "riskFactors": [
                {{
                    "risk": "Risk description",
                    "impact": "low|medium|high",
                    "mitigation": "Mitigation strategy"
                }}
            ],
            "confidence": 0.85,
            "domain": "{_domain_options}",
            "structuralCategory": "{_structural_options}",
            "detectedTechStack": ["lowercase tech labels e.g. python, react"]
        }}

        CRITICAL RULES:
        - RESPECT EXPLICIT EXCLUSIONS: If the description says
          "Do not include X" or "Do not add Y", you MUST NOT create
          requirements for those items
        - ONLY include features and requirements explicitly mentioned
          or clearly implied
        - Do NOT add "best practices" features that were explicitly
          excluded
        - For functionalRequirements, use "id", "name", "description",
          "priority", "complexity", "requires_design_artifacts", and
          "affected_components" fields
        - For integrationRequirements, use "id", "name", "description",
          "priority", "complexity", "requires_design_artifacts", and
          "affected_components" fields
        - For nonFunctionalRequirements, use "id", "name", "description",
          and "category" fields
        - Generate meaningful IDs based on the feature name
          (e.g., "crud_operations", "user_auth")
        - Focus on extracting actionable, specific requirements that can
          be converted into development tasks
        - For "domain", pick the SINGLE best-fit label from the pipe-
          separated list in the schema. If none fit, use "other".
        - For "structuralCategory", pick the SINGLE best-fit label from
          the pipe-separated list in the schema. If none fit, use
          "other". This describes the SHAPE of the deliverable (e.g. a
          browser game is "game", a REST backend is "API service").

        COMPLEXITY MODE: {constraints.complexity_mode}

        Use complexity mode to control BOTH feature breadth and implementation depth:

        PROTOTYPE MODE - Speed-focused MVP (3-5 core features):
        FEATURE BREADTH:
        - Include ONLY the absolute minimum to demonstrate the concept
        - MINIMUM 3 features required (e.g., create, read, list)
        - Skip: Authentication (unless core to concept), admin UI, monitoring,
          logging, comprehensive error handling, data migration, backup/restore
        - Example "task management": create task, view tasks, list all tasks
        - Example "twitter clone": create tweets, view tweets, basic feed

        IMPLEMENTATION DEPTH:
        - Keep requirement descriptions minimal and basic
        - Use complexity: "atomic" or "simple"
        - Focus on happy path only
        - Example "User Auth": Just "basic login/signup"

        STANDARD MODE - Balanced production app (8-15 features):
        FEATURE BREADTH:
        - Include core features + essential supporting features
        - Include: Basic auth (if multi-user), error handling, basic tests
        - Skip: Advanced ops tooling, comprehensive observability, admin dashboards
        - Example "twitter clone": CRUD tweets, auth, following, feed, profiles,
          likes, search

        IMPLEMENTATION DEPTH:
        - Include standard implementation details in descriptions
        - Use complexity: "simple" or "coordinated"
        - Include basic error handling and validation
        - Example "User Auth": "Login, signup, password reset, session management"

        ENTERPRISE MODE - Production-ready system (8-12 broad feature areas):
        FEATURE BREADTH:
        - Cover the same production-readiness scope as before (auth,
          observability, resilience, data ops, admin, quality) but
          CONSOLIDATE related concerns into 8-12 BROAD feature areas,
          NOT 15-30 narrow ones (#607 step 5).
        - Each enterprise feature area must BUNDLE related concerns
          into a single requirement that an agent implements as a
          coordinated unit. Coarser tasks + richer descriptions, never
          many narrow tasks.
        - Example "twitter clone" enterprise as 8-12 areas, NOT 20-30:
          * Tweet lifecycle: CRUD + content moderation (one area)
          * Social graph: following, feed, profiles (one area)
          * Authentication and account: signup, login, password reset,
            MFA, OAuth, account recovery, audit (one area, not six)
          * Observability stack: monitoring, structured logging,
            alerting, health checks (one area, not four)
          * Security operations: audit logs, rate limiting, RBAC
            (one area, not three)
          * Data operations: backup/restore, migration scripts,
            archival (one area, not three)
          * Admin tooling: admin dashboard, user management, feature
            flags (one area, not three)
          * Quality and performance: comprehensive testing,
            performance monitoring (one area)

        IMPLEMENTATION DEPTH (the load-bearing change at enterprise):
        - Each feature description must be RICH and CONCRETE: list the
          specific capabilities, sub-flows, security hardening, error
          recovery, and edge cases that the feature must cover. The
          agent reading the description sees the FULL SCOPE, not a
          one-liner. The breadth comes from rich descriptions, not
          from splitting a feature into many requirements.
        - Use complexity: "coordinated" or "distributed".
        - Example "Authentication" description (one requirement,
          rich description — NOT split into six requirements):
          "Implement complete authentication: signup with email
          verification, login with password + optional MFA, session
          management with refresh tokens, password reset flow with
          rate-limited token issuance, OAuth integration for Google
          and GitHub, account recovery workflow, audit logging of all
          auth events, rate limiting on login + password reset,
          account lockout after N failed attempts."

        CRITICAL ENTERPRISE GUIDANCE (#607 step 5):
        - A complex enterprise project is NOT the same as MANY narrow
          tasks. Coarser tasks + richer descriptions > more tasks +
          thinner descriptions.
        - Target: 8-12 functional requirements. If you find yourself
          producing more than 12, CONSOLIDATE cross-cutting concerns
          (auth, observability, security ops, data ops, admin) into
          broader feature areas.
        - Do NOT split a feature into login / signup / password reset
          as separate requirements; those are sub-flows that belong
          in the description of a single "Authentication" requirement.

        CRITICAL: User exclusions ALWAYS override complexity mode defaults!
        - If user says "no authentication", omit it even in enterprise mode
        - If user says "just a simple X", use prototype guidance regardless of mode

        UNIQUENESS AND DEDUPLICATION:
        - ENSURE UNIQUENESS: Each functional requirement must represent a
          DISTINCT feature
          * Check that no two requirements describe the same functionality
          * Consolidate overlapping features (e.g., "User Auth" +
            "Login System" → "User Authentication")
          * IDs must be unique - never reuse an ID or create similar IDs
            for related features
          * If a feature appears in multiple contexts (e.g., auth for
            profiles and auth for messaging), create ONE requirement with
            both contexts listed in affected_components

        - AVOID OVER-DECOMPOSITION: Keep requirements at consistent
          granularity level
          * Don't split "User Authentication" into separate requirements
            for Login, Registration, Password Reset
          * These should be ONE requirement that will be broken into
            parallelizable subtasks during implementation
          * Parallelization happens at the subtask level, not the
            requirement level
          * Implementation details belong in the description, not as
            separate requirements

        - CROSS-CHECK FEATURE GROUPS: Before finalizing, verify no
          duplicate features exist across groups
          * Example: If both "User Profiles" and "Messaging" groups
            mention authentication, create a single "User Authentication"
            requirement with affected_components: ["user-profiles",
            "messaging"]
          * Same applies for other cross-cutting concerns like logging,
            validation, error handling

        COMPLEXITY CLASSIFICATION:
        - "atomic": Single file changes (e.g., set background color, update text)
        - "simple": One component feature (e.g., score display, button handler)
        - "coordinated": Multi-component feature requiring coordination
          (e.g., user auth with API + UI + DB, full CRUD operations)
        - "distributed": Multi-service architecture
          (e.g., microservices, separate auth/user/order services)

        DESIGN ARTIFACTS NEEDED:
        - Set "requires_design_artifacts" to true if the feature needs
          interface contracts, API specs, or data schemas for coordination
        - Set to false for atomic or simple features that don't need
          design documentation

        AFFECTED COMPONENTS:
        - List all components touched by this feature
        - Examples: ["frontend"], ["api", "database"], ["auth-service", "user-service"]
        - Use specific names like "api", "database", "frontend", "auth-service"

        TECHNICAL CONSTRAINTS:
        - Extract ALL technology constraints from the description
        - Include explicit constraints: "use X", "vanilla JS", "PostgreSQL"
        - Include exclusions: "no frameworks", "don't use React", "avoid ORM"
        - Convert to lowercase with hyphens: "vanilla-js", "no-react", "postgresql"

        EXCLUSION EXAMPLES:
        - If description says "Do not include API Security", return
          empty nonFunctionalRequirements array or omit security NFRs
        - If description says "Do not include API Response Time", do
          not add performance monitoring
        - If description says "just a simple X", do not add enterprise
          features
        """
        try:
            # PRD analysis returns a dense JSON schema (functional /
            # integration / non-functional requirements, personas,
            # complexity assessment, risk factors).  For a 4-domain
            # project this routinely exceeds the legacy 4096 cap;
            # safe_structured_call starts at 16384 and auto-retries
            # with doubled budget on truncation.
            from src.utils.structured_llm import safe_structured_call

            logger.info("Attempting to use LLM for PRD analysis...")

            try:
                analysis_data = await safe_structured_call(
                    llm=self.llm_client,
                    prompt=analysis_prompt,
                    operation="decompose_prd",
                )
                logger.info("Successfully parsed AI response as JSON")
            except (json.JSONDecodeError, ValueError) as e:
                from src.core.error_framework import AIProviderError, ErrorContext

                logger.error(f"Failed to parse AI response as JSON: {e}")

                raise AIProviderError(
                    "LLM",
                    "json_parsing",
                    context=ErrorContext(
                        operation="analyze_prd_deeply",
                        integration_name="advanced_prd_parser",
                        custom_context={
                            "prd_length": len(prd_content),
                            "parsing_error": str(e),
                            "details": (
                                "AI returned malformed JSON response. "
                                "This indicates an issue with the AI "
                                "provider configuration or the response "
                                "format. Please check your AI provider "
                                "settings and try again with a clearer "
                                "project description."
                            ),
                        },
                    ),
                )

            # Handle both snake_case and camelCase keys from AI response
            def get_key(
                data: Dict[str, Any], snake_key: str, camel_key: Optional[str] = None
            ) -> Any:
                """Get value from dict using either snake_case or camelCase key."""
                if camel_key is None:
                    # Convert snake_case to camelCase
                    parts = snake_key.split("_")
                    camel_key = parts[0] + "".join(
                        word.capitalize() for word in parts[1:]
                    )

                # Prefer camelCase (our template format)
                if camel_key in data:
                    return data[camel_key]
                elif snake_key in data:
                    logger.debug(
                        f"AI used snake_case '{snake_key}' instead of "
                        f"expected camelCase '{camel_key}'"
                    )
                    return data[snake_key]
                else:
                    return []  # Return empty list as default

            # Extract functional requirements and deduplicate
            functional_reqs = get_key(
                analysis_data, "functional_requirements", "functionalRequirements"
            )
            # Apply deduplication to prevent duplicate tasks
            functional_reqs = self._deduplicate_functional_requirements(functional_reqs)

            # Extract integration requirements (infrastructure/delivery mechanisms)
            # DISABLED: Phase 1 - Remove two-tier intent system
            # System handles infrastructure inherently
            integration_reqs: list[dict[str, Any]] = []
            # Was: get_key(analysis_data, "integration_requirements",
            #              "integrationRequirements")

            analysis = PRDAnalysis(
                functional_requirements=functional_reqs,
                integration_requirements=integration_reqs,
                non_functional_requirements=get_key(
                    analysis_data,
                    "non_functional_requirements",
                    "nonFunctionalRequirements",
                ),
                technical_constraints=get_key(
                    analysis_data, "technical_constraints", "technicalConstraints"
                ),
                business_objectives=get_key(
                    analysis_data, "business_objectives", "businessObjectives"
                ),
                user_personas=get_key(analysis_data, "user_personas", "userPersonas"),
                success_metrics=get_key(
                    analysis_data, "success_metrics", "successMetrics"
                ),
                # Note: template uses 'implementationApproach',
                # but old responses might use 'recommendedImplementation'
                implementation_approach=(
                    analysis_data.get("implementationApproach")
                    or analysis_data.get("implementation_approach")
                    or analysis_data.get("recommendedImplementation")
                    or "agile_iterative"
                ),
                complexity_assessment=get_key(
                    analysis_data, "complexity_assessment", "complexityAssessment"
                )
                or {},
                risk_factors=get_key(analysis_data, "risk_factors", "riskFactors"),
                confidence=analysis_data.get("confidence", 0.8),
                original_description=prd_content,  # NEW: Preserve original description
                # Coarse project classification (Marcus #546 Phase 0).
                # Bucketed immediately so a free-text LLM answer can
                # never propagate past this point.
                domain=_bucket_label(analysis_data.get("domain"), DOMAIN_BUCKETS),
                structural_category=_bucket_label(
                    analysis_data.get("structuralCategory")
                    or analysis_data.get("structural_category"),
                    STRUCTURAL_CATEGORY_BUCKETS,
                ),
                detected_tech_stack=_normalize_tech_stack(
                    analysis_data.get("detectedTechStack")
                    or analysis_data.get("detected_tech_stack")
                ),
            )

            # Validate minimum feature counts based on complexity mode
            feature_count = len(analysis.functional_requirements)
            if constraints.complexity_mode == "prototype" and feature_count < 3:
                logger.warning(
                    f"Prototype mode requires minimum 3 features, but AI generated "
                    f"{feature_count}. Project may be incomplete."
                )
            elif constraints.complexity_mode == "standard" and feature_count < 8:
                logger.warning(
                    f"Standard mode expects 8-15 features, but AI generated "
                    f"{feature_count}. Project may be incomplete."
                )
            elif constraints.complexity_mode == "enterprise" and feature_count < 8:
                logger.warning(
                    f"Enterprise mode expects 8-12 broad feature areas, but AI "
                    f"generated {feature_count}. Project may be incomplete."
                )

            # Issue #449: extract user-visible outcomes when the
            # MARCUS_OUTCOME_COVERAGE flag is on.  Outcomes are
            # attached to PRDAnalysis so both decomposer paths
            # (parse_prd_to_tasks / decompose_by_contract) carry them
            # through to the coverage check after task generation.
            #
            # Failure to extract is logged but not fatal — outcomes
            # default to an empty list and the downstream coverage
            # check gracefully no-ops on empty input.  Keeps the
            # existing PRD pipeline robust when the secondary LLM
            # call fails for reasons unrelated to PRD analysis itself.
            if is_outcome_coverage_enabled():
                try:
                    analysis.user_outcomes = await extract_user_outcomes(
                        spec=prd_content, llm_client=self.llm_client
                    )
                    logger.info(
                        f"Extracted {len(analysis.user_outcomes)} user "
                        f"outcomes for intent fidelity coverage"
                    )
                except Exception as outcome_exc:
                    # Catch broadly: timeouts, API errors, parse
                    # errors.  Outcome extraction is a secondary LLM
                    # call; the docstring above promises "logged but
                    # not fatal" — narrow ``except ValueError`` would
                    # let transient API errors bubble up to the outer
                    # ``except Exception`` and crash PRD analysis.
                    logger.warning(
                        "User-outcome extraction failed; intent fidelity "
                        "will be unmeasurable for this project: %s",
                        outcome_exc,
                    )

            return analysis

        except Exception as e:
            from src.core.error_framework import AIProviderError, ErrorContext
            from src.core.error_monitoring import record_error_for_monitoring

            # Create comprehensive AI provider error with actionable context
            ai_error = AIProviderError(
                "LLM",
                "prd_analysis",
                context=ErrorContext(
                    operation="analyze_prd_deeply",
                    integration_name="advanced_prd_parser",
                    custom_context={
                        "prd_length": len(prd_content),
                        "prd_preview": (
                            prd_content[:200] + "..."
                            if len(prd_content) > 200
                            else prd_content
                        ),
                        "error_type": type(e).__name__,
                        "original_error": str(e),
                        "troubleshooting_steps": [
                            ("Check AI provider API credentials and " "configuration"),
                            "Verify network connectivity to AI provider",
                            "Try simplifying the project description",
                            "Check AI provider service status",
                            (
                                "Ensure project description is in English "
                                "and well-structured"
                            ),
                        ],
                        "details": (
                            f"AI analysis of project requirements failed. "
                            f"This prevents automatic task generation from "
                            f"your project description. "
                            f"The AI provider "
                            f"({self.llm_client.__class__.__name__}) "
                            f"encountered an error: {str(e)}. "
                            f"Please check your AI configuration and try "
                            f"again. If the problem persists, contact "
                            f"support with this error context."
                        ),
                    },
                ),
            )

            # Record for monitoring and raise the error
            record_error_for_monitoring(ai_error)
            logger.error(f"PRD analysis failed: {ai_error}")

            # Raise the error instead of falling back to simulation
            raise ai_error

    async def _discover_domains(
        self,
        functional_requirements: List[Dict[str, Any]],
        complexity_mode: Optional[str] = None,
    ) -> Dict[str, List[str]]:
        """
        Use AI to discover natural domain groupings from functional requirements.

        Parameters
        ----------
        functional_requirements : List[Dict[str, Any]]
            List of functional requirements with id, name, description, etc.
        complexity_mode : Optional[str]
            Project complexity mode: ``"prototype"``, ``"standard"``,
            or ``"enterprise"``. When ``"prototype"``, the prompt asks
            the LLM for exactly 1 domain so trivial projects (snake
            game, todo app, etc.) don't get split into multiple
            domains. This honors the prototype-mode contract of
            "speed over granularity" — see bug #649 root cause 3.
            ``None`` or any non-prototype value falls back to the
            size-based floor below.

        Returns
        -------
        Dict[str, List[str]]
            Mapping of domain_name -> [feature_ids]
            Example: {"User Management": ["user_reg", "user_login"],
                     "Todo Management": ["todo_create", "todo_list"]}
        """
        if not functional_requirements:
            return {}

        # Build feature list for AI prompt
        feature_list = []
        for idx, req in enumerate(functional_requirements, 1):
            feature_id = req.get("id", f"feature_{idx}")
            feature_name = req.get("name", "Unknown Feature")
            description = req.get("description", "")
            affected_components = req.get("affected_components", [])
            complexity = req.get("complexity", "simple")

            feature_list.append(
                f"{idx}. {feature_name} (ID: {feature_id})\n"
                f"   Description: {description}\n"
                f"   Components: {', '.join(affected_components)}\n"
                f"   Complexity: {complexity}"
            )

        features_text = "\n\n".join(feature_list)

        # Determine target number of domains based on project size.
        #
        # Bug #649 root cause 3: prototype mode now forces a single
        # domain so trivial projects (≤5 features) don't get split
        # into "Game Physics" + "Game Presentation" for a 50-line
        # snake game. For non-prototype small projects the floor was
        # lowered from "2-3" to "1-3" so the LLM can still return 1
        # when the project genuinely fits one domain — without
        # preventing 2-3 when warranted.
        num_features = len(functional_requirements)
        if complexity_mode == "prototype":
            target_domains = "1"
        elif num_features <= 5:
            target_domains = "1-3"
        elif num_features <= 15:
            target_domains = "3-5"
        elif num_features <= 30:
            target_domains = "4-7"
        else:
            target_domains = "6-10"

        prompt = f"""Analyze these features and group them into logical domains.

Each domain should represent a cohesive area of functionality that requires
coordination and shared design artifacts (API contracts, data models, etc.).

Consider:
- Shared data models (features touching same entities)
- Integration points (features that communicate)
- Common components (UI, backend services, databases)
- Semantic similarity (related business functionality)

Features:
{features_text}

Return JSON with {target_domains} domains (adaptive to project size):
{{
  "domains": [
    {{
      "name": "Descriptive Domain Name",
      "feature_ids": ["feature_id1", "feature_id2"],
      "rationale": "Why these features belong together (1 sentence)"
    }}
  ]
}}

IMPORTANT:
- Use the exact feature IDs from above
- Every feature MUST be assigned to exactly one domain
- Domain names should be descriptive (e.g., "User Management System",
  "Product Catalog", "Payment Processing")
- Group by COORDINATION NEEDS, not just technical similarity

Provide ONLY valid JSON, no preamble."""

        try:
            # Domain discovery output is typically small (a handful of
            # domains with feature-id arrays). Start at 2048 — the
            # helper escalates on truncation if a project has many
            # functional requirements that bloat the response.
            from src.utils.structured_llm import safe_structured_call

            domain_data = await safe_structured_call(
                llm=self.llm_client,
                prompt=prompt,
                operation="discover_domains",
                initial_max_tokens=2048,
            )
            domains_list = domain_data.get("domains", [])

            # Convert to simple dict mapping
            domains = {}
            for domain in domains_list:
                domain_name = domain.get("name", "Unknown Domain")
                feature_ids = domain.get("feature_ids", [])
                rationale = domain.get("rationale", "")

                domains[domain_name] = feature_ids
                logger.info(
                    f"Discovered domain '{domain_name}' with {len(feature_ids)} "
                    f"features: {rationale}"
                )

            # Validate: Ensure all features are assigned
            assigned_features = set()
            for feature_ids in domains.values():
                assigned_features.update(feature_ids)

            all_feature_ids = {
                req.get("id", f"feature_{i+1}")
                for i, req in enumerate(functional_requirements)
            }
            unassigned = all_feature_ids - assigned_features

            if unassigned:
                logger.warning(
                    f"AI did not assign {len(unassigned)} features to domains: "
                    f"{unassigned}. Creating 'Other' domain."
                )
                domains["Other"] = list(unassigned)

            # Additional validation: Check for semantically similar feature names
            # This helps detect potential duplicates that passed through deduplication
            feature_names = [
                (req.get("id"), req.get("name")) for req in functional_requirements
            ]

            normalized_names: Dict[str, Tuple[str, str]] = {}
            for fid, fname in feature_names:
                if not fname or not fid:
                    continue
                # Type narrowing: fid and fname are guaranteed to be str here
                fid_str: str = fid
                fname_str: str = fname
                normalized = fname_str.lower().strip()
                # Normalize variations
                normalized = normalized.replace("authentication", "auth")
                normalized = normalized.replace("authorization", "auth")
                normalized = normalized.replace(" system", "")
                normalized = normalized.replace(" feature", "")
                normalized = normalized.replace(" component", "")
                normalized = normalized.replace(" service", "")
                normalized = normalized.replace("management", "mgmt")

                if normalized in normalized_names:
                    logger.warning(
                        f"Potential duplicate features detected: "
                        f"'{fname_str}' (ID: {fid_str}) and "
                        f"'{normalized_names[normalized][1]}' "
                        f"(ID: {normalized_names[normalized][0]}) "
                        f"have similar normalized names: '{normalized}'. "
                        f"Consider consolidating these features."
                    )
                else:
                    normalized_names[normalized] = (fid_str, fname_str)

            return domains

        except Exception as e:
            logger.warning(
                f"Domain discovery failed: {e}. Falling back to single domain."
            )
            # Fallback: Create single domain with all features
            all_ids = [
                req.get("id", f"feature_{i+1}")
                for i, req in enumerate(functional_requirements)
            ]
            return {"Project Domain": all_ids}

    async def _create_bundled_design_tasks(
        self,
        domains: Dict[str, List[str]],
        functional_requirements: List[Dict[str, Any]],
        complexity_mode: str,
    ) -> List[Dict[str, Any]]:
        """
        Create bundled design tasks, one per domain.

        Parameters
        ----------
        domains : Dict[str, List[str]]
            Mapping of domain_name -> [feature_ids]
        functional_requirements : List[Dict[str, Any]]
            All functional requirements
        complexity_mode : str
            Project complexity mode: "prototype", "standard", or "enterprise"

        Returns
        -------
        List[Dict[str, Any]]
            List of bundled design tasks
        """
        bundled_design_tasks = []

        # Create a lookup map for requirements
        req_map = {req.get("id"): req for req in functional_requirements}

        for domain_name, feature_ids in domains.items():
            # Get all requirements for this domain (filter out None values)
            domain_reqs = [req_map[fid] for fid in feature_ids if fid in req_map]

            if not domain_reqs:
                continue

            # Build detailed description including all features in this domain
            feature_descriptions = []
            for idx, req in enumerate(domain_reqs, 1):
                feature_name = req.get("name", "Unknown Feature")
                description = req.get("description", "")

                feature_descriptions.append(
                    f"{idx}. {feature_name.upper()}\n" f"   {description}"
                )

            features_text = "\n\n".join(feature_descriptions)

            # Create task description
            task_description = f"""Design the architecture for the {domain_name} \
which encompasses the following features:

{features_text}

Your design should define:
- Component boundaries (what components exist and their responsibilities)
- Data flows (how data moves between components)
- Integration points (how components communicate)
- Shared data models (schemas, entities, etc.)

Create design artifacts such as:
- Architecture diagrams (component relationships, data flow)
- API contracts (endpoint definitions, request/response schemas)
- Data models (database schemas, entity relationships)
- Integration specifications (how components communicate)"""

            # Create bundled design task
            task_id = f"design_{domain_name.lower().replace(' ', '_')}"

            bundled_design_tasks.append(
                {
                    "id": task_id,
                    "name": f"Design {domain_name}",
                    "description": task_description,
                    "type": self.TASK_TYPE_DESIGN,
                    "domain_name": domain_name,
                    "feature_ids": feature_ids,  # Track which features this covers
                    "priority": "high",  # Design tasks should run first
                    "estimated_hours": self._get_learned_task_duration(
                        "design", default_minutes=6.0 * len(domain_reqs)
                    )
                    / 60.0,  # Scale with number of features
                    "labels": ["design", "architecture", domain_name.lower()],
                }
            )

            logger.info(
                f"Created bundled design task '{task_id}' for domain "
                f"'{domain_name}' covering {len(feature_ids)} features"
            )

        return bundled_design_tasks

    async def _generate_task_hierarchy(
        self, analysis: PRDAnalysis, constraints: ProjectConstraints
    ) -> Dict[str, List[str]]:
        """Generate hierarchical task structure."""
        hierarchy: Dict[str, List[str]] = {}

        # Store task metadata for later use
        self._task_metadata = {}

        # Filter requirements based on project size
        project_size = (constraints.quality_requirements or {}).get(
            "project_size", "medium"
        )

        # #683 Cause 1: determine which features are CORE (serve an
        # in-scope user outcome) via one LLM call, so the filter keeps
        # them and trims only scope-creep — instead of dropping by list
        # position. Gated on outcome coverage (which provides the
        # outcomes). On flag-off / no outcomes / LLM error we pass
        # ``None``: the filter still caps by the complexity tier
        # (deterministic, far better than the old team_size cut) and #683
        # Cause 2 backstops any core feature that slips through.
        protected_ids: Optional[Set[str]] = None
        from src.config.outcome_coverage_config import is_outcome_coverage_enabled

        if is_outcome_coverage_enabled() and analysis.user_outcomes:
            try:
                from src.marcus_mcp.coordinator.outcome_coverage import (
                    map_core_feature_ids_with_llm,
                )

                protected_ids = await map_core_feature_ids_with_llm(
                    requirements=analysis.functional_requirements,
                    outcomes=analysis.user_outcomes,
                    llm_client=self.llm_client,
                )
            except Exception as exc:  # noqa: BLE001 — graceful degradation
                # Codex P2 on PR #688: a transient mapping failure must NOT
                # reintroduce the outcome-dropping behavior this fixes. With
                # outcome coverage ON, the safe fallback is to protect ALL
                # current requirements (treat every feature as core) so an
                # over-cap list can't trim a real feature by list position.
                # The deterministic tier cap still applies; #683 Cause 2
                # backstops anything genuinely beyond it.
                logger.warning(
                    "#683 Cause 1: core-feature mapping failed; protecting "
                    "all requirements so none are dropped by position: %s",
                    exc,
                )
                protected_ids = {
                    str(r.get("id") or r.get("name") or "")
                    for r in analysis.functional_requirements
                    if (r.get("id") or r.get("name"))
                }

        functional_requirements = self._filter_requirements_by_size(
            analysis.functional_requirements,
            project_size,
            constraints.team_size,
            # Pass original PRD for specificity detection
            analysis.original_description,
            protected_ids,
        )

        # Get complexity mode from constraints (passed from create_project)
        complexity_mode = constraints.complexity_mode

        # STEP 1: Discover domains from functional requirements.
        # Bug #649 root cause 3: forward ``complexity_mode`` so prototype
        # projects collapse to a single domain (no over-decomposition).
        domains = await self._discover_domains(
            functional_requirements, complexity_mode=complexity_mode
        )
        logger.info(f"Discovered {len(domains)} domains: {list(domains.keys())}")

        # STEP 2: Create bundled design tasks (one per domain)
        bundled_design_tasks = await self._create_bundled_design_tasks(
            domains, functional_requirements, complexity_mode
        )

        # Store bundled design tasks (they don't belong to any epic)
        if bundled_design_tasks:
            design_epic_id = "epic_design_architecture"
            hierarchy[design_epic_id] = []

            for task in bundled_design_tasks:
                self._task_metadata[task["id"]] = {
                    "original_name": task["name"],
                    "type": task["type"],
                    "epic_id": design_epic_id,
                    "domain_name": task["domain_name"],
                    "feature_ids": task["feature_ids"],
                    "description": task[
                        "description"
                    ],  # Store the full detailed description
                    "estimated_hours": task["estimated_hours"],
                    "labels": task["labels"],
                    "priority": task["priority"],
                }
                hierarchy[design_epic_id].append(task["id"])

        # Store domain mapping for later dependency resolution
        self._domain_mapping = domains  # feature_id -> domain_name lookup
        self._bundled_designs = {
            task["domain_name"]: task["id"] for task in bundled_design_tasks
        }

        # Validate functional requirements for duplicates before creating epics
        req_ids = [req.get("id") for req in functional_requirements]
        logger.info(
            f"Creating epics from {len(functional_requirements)} functional "
            f"requirements: {req_ids}"
        )

        # Check for duplicate IDs (exclude None which will be generated later)
        non_none_ids = [req_id for req_id in req_ids if req_id is not None]
        if len(non_none_ids) != len(set(non_none_ids)):
            from collections import Counter

            id_counts = Counter(non_none_ids)
            duplicates = [req_id for req_id, count in id_counts.items() if count > 1]
            logger.error(
                f"DUPLICATE REQUIREMENT IDs DETECTED: {duplicates} - "
                f"This will create duplicate tasks! Check deduplication logic."
            )

        # Create epics from functional requirements
        for i, req in enumerate(functional_requirements):
            # Prefer standardized 'id' field from template
            req_id = req.get("id")

            if not req_id:
                # Fallback: generate ID from name/feature/description
                feature_name = (
                    req.get("name")
                    or req.get("feature")
                    or req.get("description")
                    or f"requirement_{i}"
                )

                # Generate clean feature ID
                feature_id = feature_name.lower()
                # Remove common words and clean up
                for word in ["for", "the", "a", "an", "and", "or", "with", "using"]:
                    feature_id = feature_id.replace(f" {word} ", " ")
                # Convert to ID format
                feature_id = (
                    feature_id.strip()
                    .replace(" ", "_")
                    .replace("-", "_")
                    .replace(":", "")
                )
                # Remove any non-alphanumeric characters except underscore
                feature_id = "".join(
                    c if c.isalnum() or c == "_" else "" for c in feature_id
                )

                # If we still don't have a good ID, use the index
                if not feature_id or feature_id == "feature":
                    feature_id = f"req_{i}"

                req_id = feature_id
                logger.debug(
                    f"Generated fallback ID '{req_id}' for requirement "
                    f"without 'id' field"
                )

            epic_id = f"epic_{req_id}"
            hierarchy[epic_id] = []

            # Break epic into smaller tasks
            epic_tasks = await self._break_down_epic(req, analysis, constraints)
            logger.debug(f"Epic {epic_id} broken down into {len(epic_tasks)} tasks")

            # Store task metadata for later use
            for task in epic_tasks:
                self._task_metadata[task["id"]] = {
                    "original_name": task["name"],
                    "type": task["type"],
                    "epic_id": epic_id,
                    "requirement": req,
                }

            hierarchy[epic_id] = [task["id"] for task in epic_tasks]

        # REMOVED: Integration epic generation loop
        # (Phase 1 - Remove two-tier intent system)
        # Integration requirements now disabled (set to empty list at line 572)
        # System handles infrastructure inherently

        # Add non-functional requirement tasks (skip for prototype projects)
        if project_size not in ["prototype", "mvp"]:
            nfr_epic_id = "epic_non_functional"
            # Filter NFRs based on project size
            filtered_nfrs = self._filter_nfrs_by_size(
                analysis.non_functional_requirements, project_size
            )
            nfr_tasks = await self._create_nfr_tasks(filtered_nfrs, constraints)

            # Store NFR task metadata
            for task in nfr_tasks:
                self._task_metadata[task["id"]] = {
                    "original_name": task["name"],
                    "type": task["type"],
                    "epic_id": nfr_epic_id,
                    "description": task.get("description", ""),
                    "nfr_data": task.get("nfr_data", {}),
                }

            hierarchy[nfr_epic_id] = [task["id"] for task in nfr_tasks]

        # Add infrastructure and setup tasks (minimal for prototype projects)
        if project_size not in [
            "prototype",
            "mvp",
        ]:  # Prototype projects skip infrastructure
            infra_epic_id = "epic_infrastructure"
            infra_tasks = await self._create_infrastructure_tasks(
                analysis, constraints, project_size
            )

            # Store infrastructure task metadata
            for task in infra_tasks:
                self._task_metadata[task["id"]] = {
                    "original_name": task["name"],
                    "type": task["type"],
                    "epic_id": infra_epic_id,
                }

            hierarchy[infra_epic_id] = [task["id"] for task in infra_tasks]

        return hierarchy

    async def _create_detailed_tasks(
        self,
        task_hierarchy: Dict[str, List[str]],
        analysis: PRDAnalysis,
        constraints: ProjectConstraints,
    ) -> List[Task]:
        """
        Create detailed Task objects with rich metadata.

        Uses parallel AI calls for performance - all task descriptions are
        generated concurrently instead of sequentially.
        """
        import asyncio

        # Collect all task generation jobs for parallel execution
        task_generation_jobs = []
        task_sequence = 1

        for epic_id, task_ids in list(task_hierarchy.items()):
            # Don't skip integration epics - check if any task is integration
            is_integration_epic = any(
                self._task_metadata.get(tid, {}).get("is_integration", False)
                for tid in task_ids
            )

            # Skip deployment epics EXCEPT integration epics
            # Integration epics (MCP server, API server) are core delivery
            deploy_target = constraints.deployment_target
            if not is_integration_epic and self._should_skip_epic(
                epic_id, deploy_target
            ):
                logger.info(
                    f"Skipping {epic_id} for " f"deployment_target={deploy_target}"
                )
                continue

            for task_id in task_ids:
                # Don't skip integration tasks - core delivery mechanisms
                task_meta = self._task_metadata.get(task_id, {})
                is_integration = task_meta.get("is_integration", False)

                # Skip deployment tasks EXCEPT integration tasks
                # Integration tasks (MCP/API server) are core delivery
                if not is_integration and self._should_skip_task(
                    task_id, epic_id, constraints.deployment_target
                ):
                    logger.info(
                        f"Skipping task {task_id} for "
                        f"deployment_target={constraints.deployment_target}"
                    )
                    continue

                # Add task generation to parallel jobs instead of awaiting
                task_generation_jobs.append(
                    self._generate_detailed_task(
                        task_id, epic_id, analysis, constraints, task_sequence
                    )
                )
                task_sequence += 1

        # Execute all task generations in parallel with error handling
        logger.info(
            f"Generating {len(task_generation_jobs)} task descriptions in parallel..."
        )
        task_results = await asyncio.gather(
            *task_generation_jobs, return_exceptions=True
        )

        # Filter out exceptions and collect valid tasks
        tasks = []
        failed_count = 0
        for idx, result in enumerate(task_results):
            if isinstance(result, Task):
                tasks.append(result)
            elif isinstance(result, Exception):
                failed_count += 1
                logger.error(
                    f"Task generation failed for task {idx + 1}: {result}",
                    exc_info=result,
                )
                # Continue with other tasks

        if failed_count > 0:
            logger.warning(
                f"Task generation completed with {failed_count} failures. "
                f"Successfully generated {len(tasks)}/{len(task_results)} tasks."
            )
        else:
            logger.info(f"Successfully generated all {len(tasks)} tasks in parallel")

        return tasks

    async def _generate_detailed_task(
        self,
        task_id: str,
        epic_id: str,
        analysis: PRDAnalysis,
        constraints: ProjectConstraints,
        sequence: int,
    ) -> Task:
        """
        Generate a detailed task using AI descriptions directly.

        This creates tasks with clean, AI-generated descriptions instead of
        template boilerplate, while preserving Design/Implement/Test methodology
        through task names and labels.
        """
        # Check if this is a bundled design task (already has description)
        if (
            hasattr(self, "_task_metadata")
            and task_id in self._task_metadata
            and self._task_metadata[task_id].get("type") == self.TASK_TYPE_DESIGN
            and epic_id == "epic_design_architecture"
        ):
            # This is a bundled design task - it already has all its details
            # from _create_bundled_design_tasks().
            # Just convert the metadata to a Task object.
            metadata = self._task_metadata[task_id]
            task_name = metadata["original_name"]
            domain_name = metadata.get("domain_name", "Project Domain")

            # Use the pre-built description from _create_bundled_design_tasks
            # which includes all features in this domain
            task_description = metadata.get(
                "description", f"Design the architecture for {domain_name}."
            )

            # Use the estimated hours calculated during bundled design creation
            # (scaled by number of features in the domain)
            estimated_hours = metadata.get("estimated_hours", 0.1)

            # Map priority string to Priority enum
            priority_str = metadata.get("priority", "high")
            priority_map = {
                "high": Priority.HIGH,
                "medium": Priority.MEDIUM,
                "low": Priority.LOW,
            }
            priority = priority_map.get(priority_str, Priority.HIGH)

            # Create the Task object for bundled design
            task = Task(
                id=task_id,
                name=task_name,
                description=task_description,
                status=TaskStatus.TODO,
                priority=priority,
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                due_date=None,
                estimated_hours=estimated_hours,
                dependencies=[],
                labels=metadata.get("labels", ["design", "architecture"]),
                source_type="bundled_design",
                source_context={
                    "domain_name": domain_name,
                    "feature_ids": metadata.get("feature_ids", []),
                },
            )

            feature_count = len(metadata.get("feature_ids", []))
            logger.info(
                f"Created bundled design Task object: {task_name} "
                f"(domain: {domain_name}, features: {feature_count})"
            )
            return task

        # Extract task type from task_id
        task_type = self._extract_task_type(task_id)

        # Find matching requirement from AI analysis
        relevant_req = self._find_matching_requirement(task_id, analysis)

        # Check if we have stored metadata for this task
        # (NFR tasks, infrastructure tasks store metadata)
        has_metadata = (
            hasattr(self, "_task_metadata") and task_id in self._task_metadata
        )

        if relevant_req:
            # Get base information from requirement
            base_description = relevant_req.get("description", "")
            feature_name = relevant_req.get("name", "")

            # Create task name with phase prefix
            task_name = f"{task_type.title()} {feature_name}"

            # Generate task-type-specific description using LLM with constraints
            description = await self._generate_task_description_for_type(
                base_description=base_description,
                task_type=task_type,
                feature_name=feature_name,
                constraints=analysis.technical_constraints,
                original_description=analysis.original_description,
            )

            # Get estimated hours based on task type
            # CRITICAL: Use learned median from historical data
            # Falls back to reality-based estimates (4-8 minutes)
            if task_type == "design":
                estimated_minutes = self._get_learned_task_duration(
                    "design", default_minutes=6.0
                )
            elif task_type == "implement":
                estimated_minutes = self._get_learned_task_duration(
                    "implement", default_minutes=8.0
                )
            elif task_type == "test":
                estimated_minutes = self._get_learned_task_duration(
                    "test", default_minutes=6.0
                )
            else:
                estimated_minutes = self._get_learned_task_duration(
                    task_type, default_minutes=7.0
                )

            # Convert to hours for backward compatibility with existing system
            estimated_hours = estimated_minutes / 60

        elif has_metadata:
            # NFR or infrastructure task - use metadata with AI enhancement
            metadata = self._task_metadata[task_id]
            task_name = metadata["original_name"]

            # Use stored description if available, otherwise generate with AI
            base_description = metadata.get("description", "")
            if not base_description:
                # Generate description using AI (for infrastructure tasks)
                base_description = f"Set up and configure {task_name}"

            # For NFR tasks, enhance the description with AI
            if metadata.get("type") == "nfr":
                description = await self._generate_task_description_for_type(
                    base_description=base_description,
                    task_type="nfr",
                    feature_name=task_name,
                    constraints=analysis.technical_constraints,
                    original_description=analysis.original_description,
                )
            else:
                # Infrastructure/setup tasks - use description as-is or enhance
                description = base_description

            # Get estimated hours for NFR/infrastructure tasks
            estimated_minutes = self._get_learned_task_duration(
                metadata.get("type", "infrastructure"), default_minutes=8.0
            )
            estimated_hours = estimated_minutes / 60
            feature_name = task_name

        else:
            # No matching requirement AND no metadata
            # This means AI analysis failed or task_id mismatch
            from src.core.error_framework import AIProviderError, ErrorContext

            available_req_ids = [
                req.get("id") for req in analysis.functional_requirements
            ]
            error_msg = (
                f"Failed to generate task '{task_id}': "
                f"No matching requirement found in AI analysis and no stored "
                f"metadata. This usually means the AI service failed to properly "
                f"analyze your project description, or there's a mismatch "
                f"between generated task IDs and requirements. "
                f"Available requirements: {available_req_ids}"
            )
            logger.error(error_msg)
            raise AIProviderError(
                provider_name="llm_client",
                operation="generate_detailed_task",
                context=ErrorContext(
                    operation="generate_detailed_task",
                    integration_name="advanced_prd_parser",
                    custom_context={
                        "task_id": task_id,
                        "epic_id": epic_id,
                        "requirement_count": len(analysis.functional_requirements),
                        "available_requirements": available_req_ids,
                        "has_metadata": has_metadata,
                    },
                ),
            )

        # Generate labels (methodology preserved here)
        labels = self._generate_task_labels(task_type, feature_name, analysis)

        # Generate acceptance criteria for validation.
        # Normalize task_type: the generator expects "implementation"
        # and "testing" but callers may pass "implement" and "test".
        criteria_type = {
            "implement": "implementation",
            "test": "testing",
        }.get(task_type, task_type)
        acceptance_criteria = self._generate_acceptance_criteria(
            criteria_type, {}, task_name
        )

        # Issue #607 step 3 — test-pair rollup. The implement task carries
        # the test-coverage criteria that previously lived on a separate
        # ``Test {feature}`` task. Surfaced to the agent via
        # ``request_next_task`` (see ``src/marcus_mcp/tools/task.py``) and
        # consumed by ``WorkAnalyzer._extract_criteria``, which reads the
        # list-of-strings shape declared on the ``Task`` dataclass.
        #
        # Codex P1 (PR #608 review): _extract_task_type defaults unknown
        # task IDs to "implement" (with a logged warning) — so non-feature
        # tasks like ``infra_setup`` / ``infra_ci_cd`` / NFR requirement
        # tasks would otherwise inherit test-coverage criteria they have
        # no business carrying (an infra-setup task should not be asked
        # to provide happy-path / invalid-input behavior tests). Gate on
        # the *canonical* type recorded in ``_task_metadata`` instead,
        # which is populated for every task_id at decomposition time
        # (bundled designs at line ~1981, feature work via
        # ``_break_down_epic`` at ~2071, NFRs at ~2096, infrastructure
        # at ~2118). ``self.TASK_TYPE_IMPLEMENTATION`` is the canonical
        # marker for true feature implementation tasks emitted by
        # ``_select_task_pattern``.
        task_meta_type = (
            self._task_metadata.get(task_id, {}).get("type")
            if hasattr(self, "_task_metadata")
            else None
        )
        completion_criteria: Optional[List[str]] = None
        if task_type == "implement" and task_meta_type == self.TASK_TYPE_IMPLEMENTATION:
            completion_criteria = self._generate_test_coverage_criteria(
                feature_name=feature_name,
                base_description=(
                    relevant_req.get("description", "") if relevant_req else ""
                ),
            )

        # Create task with clean AI description
        task = Task(
            id=task_id,
            name=task_name,
            description=description,  # ✅ Clean AI content, no template noise
            status=TaskStatus.TODO,
            priority=self._determine_priority({"type": task_type}, analysis),
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=estimated_hours,
            dependencies=[],  # Will be filled by dependency inference
            labels=labels,
            acceptance_criteria=acceptance_criteria,
            completion_criteria=completion_criteria,
            # Store context for reference
            source_type="nlp_project",
            source_context={
                "prd_analysis": (
                    analysis.__dict__ if hasattr(analysis, "__dict__") else {}
                ),
                "requirement": relevant_req,
                "task_type": task_type,
                "constraints": (
                    constraints.__dict__ if hasattr(constraints, "__dict__") else {}
                ),
            },
        )

        return task

    def _extract_task_type(self, task_id: str) -> str:
        """
        Extract task type (design/implement/test) from task_id.

        Parameters
        ----------
        task_id : str
            Task ID in format like "epic_1_design_1" or "epic_1_implement_1"

        Returns
        -------
        str
            Task type: "design", "implement", or "test"
        """
        task_id_lower = task_id.lower()

        if "design" in task_id_lower:
            return "design"
        elif "implement" in task_id_lower:
            return "implement"
        elif "test" in task_id_lower:
            return "test"
        else:
            # Default to implement for unknown types
            logger.warning(
                f"Could not extract task type from {task_id}, defaulting to 'implement'"
            )
            return "implement"

    def _find_matching_requirement(
        self, task_id: str, analysis: PRDAnalysis
    ) -> Optional[Dict[str, Any]]:
        """
        Find the AI requirement that matches this task_id.

        Parameters
        ----------
        task_id : str
            Task ID like "task_todo_list_management_design" or
            "nfr_task_performance_requirement"
        analysis : PRDAnalysis
            Complete PRD analysis with requirements

        Returns
        -------
        Optional[Dict[str, Any]]
            Matching requirement dict or None if not found
        """
        # Get all requirements from analysis (functional + non-functional)
        functional_reqs = getattr(analysis, "functional_requirements", [])
        non_functional_reqs = getattr(analysis, "non_functional_requirements", [])

        all_requirements = []
        if functional_reqs:
            all_requirements.extend(
                functional_reqs
                if isinstance(functional_reqs, list)
                else [functional_reqs]
            )
        if non_functional_reqs:
            all_requirements.extend(
                non_functional_reqs
                if isinstance(non_functional_reqs, list)
                else [non_functional_reqs]
            )

        if not all_requirements:
            logger.warning("No requirements found in analysis")
            return None

        # Extract requirement ID from task_id
        # task_todo_list_management_design -> todo_list_management
        # nfr_task_performance_requirement -> performance_requirement
        # task_user_auth_implement -> user_auth

        if task_id.startswith("nfr_task_"):
            # Non-functional requirement - strip "nfr_task_" AND phase suffix
            # Example: nfr_task_scalability_implement -> scalability
            parts = task_id.replace("nfr_task_", "").rsplit("_", 1)
            req_id = parts[0] if parts else task_id.replace("nfr_task_", "")
        elif task_id.startswith("task_"):
            # Functional requirement - extract between "task_" and last "_phase"
            parts = task_id.replace("task_", "").rsplit("_", 1)
            req_id = parts[0] if parts else task_id
        else:
            logger.warning(f"Unknown task_id format: {task_id}")
            return None

        logger.debug(f"Extracted req_id '{req_id}' from task_id '{task_id}'")

        # Find matching requirement by ID
        for req in all_requirements:
            # Convert to dict if it's a Pydantic model
            req_dict: Dict[str, Any]
            if hasattr(req, "dict"):
                req_dict = req.dict()
            elif hasattr(req, "__dict__"):
                req_dict = req.__dict__
            elif isinstance(req, dict):
                req_dict = req
            else:
                continue

            # Check if this requirement matches
            if req_dict.get("id") == req_id:
                logger.debug(
                    f"Matched requirement: task_id='{task_id}' -> "
                    f"req_id='{req_id}' -> '{req_dict.get('name')}'"
                )
                return req_dict

        logger.warning(f"No requirement found with id={req_id} for task_id={task_id}")
        return None

    def _deduplicate_functional_requirements(
        self, requirements: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Remove duplicate functional requirements based on ID and semantic similarity.

        This prevents the AI from creating duplicate tasks when it generates
        similar requirements with different names (e.g., "User Auth" and
        "Authentication System" for the same feature).

        Parameters
        ----------
        requirements : List[Dict[str, Any]]
            Raw functional requirements from AI analysis

        Returns
        -------
        List[Dict[str, Any]]
            Deduplicated requirements

        Notes
        -----
        Deduplication strategy:
        1. Check for exact duplicate IDs
        2. Normalize feature names to detect semantic duplicates:
           - Remove common suffixes: " system", " feature", " component", " module"
           - Normalize variations: "authentication" → "auth", "authorization" → "auth"
        3. Keep first occurrence, log and skip duplicates
        """
        seen_ids = set()
        seen_names_normalized = set()
        deduplicated = []

        for req in requirements:
            req_id = req.get("id", "").lower().strip()
            req_name = req.get("name", "").lower().strip()

            # Normalize name for similarity checking
            normalized_name = req_name
            # Remove common prefix variations
            for prefix in ["user ", "admin ", "system "]:
                if normalized_name.startswith(prefix):
                    normalized_name = normalized_name[len(prefix) :]
            # Remove common suffix variations
            for suffix in [" system", " feature", " component", " module", " service"]:
                normalized_name = normalized_name.replace(suffix, "")
            # Normalize common variations
            normalized_name = normalized_name.replace("authentication", "auth")
            normalized_name = normalized_name.replace("authorization", "auth")
            normalized_name = normalized_name.replace("management", "mgmt")

            # Check for duplicate ID
            if req_id in seen_ids:
                logger.warning(
                    f"Duplicate requirement ID detected: '{req_id}' - "
                    f"'{req.get('name')}' - SKIPPING (AI violated uniqueness "
                    f"constraint)"
                )
                continue

            # Check for semantic duplicate (similar name)
            if normalized_name in seen_names_normalized:
                logger.warning(
                    f"Duplicate requirement detected (similar name): "
                    f"'{req.get('name')}' (normalized: '{normalized_name}') - "
                    f"SKIPPING (consolidate with existing requirement)"
                )
                continue

            # Add to results
            seen_ids.add(req_id)
            seen_names_normalized.add(normalized_name)
            deduplicated.append(req)

        if len(deduplicated) < len(requirements):
            logger.info(
                f"Deduplication removed {len(requirements) - len(deduplicated)} "
                f"duplicate functional requirements"
            )

        return deduplicated

    def _format_constraints_for_prompt(self, constraints: List[str]) -> str:
        """
        Format technical constraints for inclusion in AI prompts.

        Converts constraint tags like "vanilla-js" into readable descriptions
        that help the AI understand what to include/exclude.

        Parameters
        ----------
        constraints : List[str]
            List of constraint tags (e.g., ["vanilla-js", "no-frameworks"])

        Returns
        -------
        str
            Human-readable constraint description for AI prompts
        """
        if not constraints:
            return ""

        # Separate positive and negative constraints
        positive = []
        negative = []

        for constraint in constraints:
            if constraint.startswith("no-"):
                # Convert "no-X" to "do not use X"
                tech = constraint[3:].replace("-", " ")
                negative.append(tech)
            else:
                # Convert "vanilla-js" to "vanilla JavaScript"
                tech = constraint.replace("-", " ")
                positive.append(tech)

        parts = []
        if positive:
            parts.append(f"Use: {', '.join(positive)}")
        if negative:
            parts.append(f"Do not use: {', '.join(negative)}")

        return ". ".join(parts) if parts else ""

    def _check_constraint_violations(
        self, description: str, constraints: List[str]
    ) -> List[str]:
        """
        Check if a task description violates any technical constraints.

        Parameters
        ----------
        description : str
            The generated task description
        constraints : List[str]
            List of constraint tags to check against

        Returns
        -------
        List[str]
            List of detected violations (empty if no violations)
        """
        violations = []
        description_lower = description.lower()

        # Check for specific "no-X" constraints
        # This catches things like "no-react", "no-orm", "no-typescript", etc.
        for constraint in constraints:
            if constraint.startswith("no-"):
                tech = constraint[3:]  # Remove "no-" prefix
                # Normalize both the tech name and description for comparison
                tech_normalized = tech.replace("-", " ").lower()

                # Check if the technology appears in the description
                if tech_normalized in description_lower:
                    violations.append(f"Mentions '{tech}' but constraint prohibits it")

        # Special handling for "no-frameworks" - look for the word "framework"
        if "no-frameworks" in constraints or "vanilla-js" in constraints:
            if "framework" in description_lower:
                violations.append(
                    "Mentions 'framework' but constraints prohibit frameworks"
                )

        return violations

    async def _generate_task_description_for_type(
        self,
        base_description: str,
        task_type: str,
        feature_name: str,
        constraints: Optional[List[str]] = None,
        original_description: Optional[str] = None,
    ) -> str:
        """
        Use LLM to generate task-type-specific descriptions with constraint awareness.

        This ensures Design/Implement/Test tasks get appropriate descriptions
        that are then passed to subtasks during decomposition. Technical constraints
        are incorporated to ensure generated descriptions respect project requirements.

        Parameters
        ----------
        base_description : str
            Original requirement description
        task_type : str
            Task type: "design", "implement", or "test"
        feature_name : str
            Name of the feature being worked on
        constraints : Optional[List[str]], default=None
            Technical constraints to respect (e.g., "vanilla-js", "no-frameworks")
        original_description : Optional[str], default=None
            Original user description for context

        Returns
        -------
        str
            Task-type-specific description that respects constraints
        """
        # Format constraints for the prompt
        # IMPORTANT (GH-143): Only include tech constraints in DESIGN tasks.
        # Design tasks establish the tech stack; implementation tasks get it from
        # design docs via get_task_context(). This prevents conflicting guidance.
        constraint_text = ""
        if constraints and task_type == "design":
            formatted_constraints = self._format_constraints_for_prompt(constraints)
            if formatted_constraints:
                constraint_text = (
                    f"\n\nTECHNICAL CONSTRAINTS (MUST FOLLOW):\n{formatted_constraints}"
                )

        # Include original description context if available
        original_context = ""
        if original_description:
            original_context = f"\nOriginal Request: {original_description}"

        prompt = f"""Given this feature requirement:

Feature: {feature_name}
Requirement: {base_description}{original_context}{constraint_text}

Generate a clear, specific description for a **{task_type.upper()}** task.

Guidelines:
- For DESIGN tasks: Create specifications for coordination (NOT implementation
  prescription). Define: component boundaries, what needs to communicate,
  data flow patterns, integration points. DO NOT specify: exact API paths,
  exact field names, exact function signatures, exact technologies. Agents
  discover these through get_task_context() and document via log_artifact().
  Example: "Frontend auth component communicates with backend auth service"
  NOT "LoginForm calls POST /api/v1/auth/login with {{email, password}}".
- For IMPLEMENT tasks: Focus on coding, building features, integrating
  components, writing the actual code. Agents use get_task_context() to
  see design artifacts from dependencies. DO NOT specify technologies -
  implementation agents discover the tech stack from design documentation.
- For TEST tasks: Focus on writing tests, creating test scenarios,
  validation, test coverage, quality assurance.

IMPORTANT FOR DESIGN TASKS: Your description MUST respect the technical
constraints listed above (if any) to ensure the design documents the chosen
tech stack for implementation agents.

Provide ONLY the description (3-4 sentences), no preamble or
explanation."""

        try:
            # Create a simple context object
            class SimpleContext:
                def __init__(self, max_tokens: int) -> None:
                    self.max_tokens = max_tokens

            context = SimpleContext(max_tokens=200)

            # Use LLM to generate task-specific description
            result = await self.llm_client.analyze(
                prompt, context, operation="generate_task_detail"
            )
            description: str = str(result) if result else ""
            description = description.strip()

            # Validate that generated description doesn't violate constraints
            if constraints:
                violations = self._check_constraint_violations(description, constraints)
                if violations:
                    logger.warning(
                        f"Generated description has constraint violations: "
                        f"{violations}. Description: {description}"
                    )
                    # Note: We log but don't fail
                    # AI can be retried or manually corrected

            return description

        except Exception as e:
            from src.core.error_framework import AIProviderError, ErrorContext

            error_msg = (
                f"AI service failed to generate {task_type} task description for "
                f"'{feature_name}'. The AI service may be unavailable, "
                f"rate-limited, or encountered an error. Cannot proceed without "
                f"AI-generated descriptions. Original error: {str(e)}"
            )
            logger.error(error_msg)
            raise AIProviderError(
                provider_name="llm_client",
                operation="generate_task_description",
                context=ErrorContext(
                    operation="generate_task_description",
                    integration_name="advanced_prd_parser",
                    custom_context={
                        "task_type": task_type,
                        "feature_name": feature_name,
                        "base_description": base_description[:200],
                        "original_error": str(e),
                    },
                ),
            ) from e

    def _generate_task_labels(
        self, task_type: str, feature_name: str, analysis: PRDAnalysis
    ) -> List[str]:
        """
        Generate labels for a task to preserve D/I/T methodology.

        Parameters
        ----------
        task_type : str
            "design", "implement", or "test"
        feature_name : str
            Name of the feature being worked on
        analysis : PRDAnalysis
            Complete PRD analysis

        Returns
        -------
        List[str]
            List of labels for the task
        """
        labels = []

        # Add task type label (preserves methodology)
        labels.append(task_type)

        # Add technology/domain labels from analysis
        tech_stack = getattr(analysis, "technical_requirements", {})
        if isinstance(tech_stack, dict):
            # Add backend/frontend labels
            if tech_stack.get("backend"):
                labels.append("backend")
            if tech_stack.get("frontend"):
                labels.append("frontend")
            if tech_stack.get("database"):
                labels.append("database")

        # Add priority-based labels
        project_type = getattr(analysis, "project_type", "")
        if project_type:
            labels.append(project_type.lower())

        # Add feature-based labels (extract key terms)
        feature_lower = feature_name.lower()
        if "api" in feature_lower or "endpoint" in feature_lower:
            labels.append("api")
        if "auth" in feature_lower or "login" in feature_lower:
            labels.append("authentication")
        if "user" in feature_lower:
            labels.append("user-management")
        if "database" in feature_lower or "model" in feature_lower:
            labels.append("data")

        # Remove duplicates while preserving order
        seen = set()
        unique_labels = []
        for label in labels:
            if label not in seen:
                seen.add(label)
                unique_labels.append(label)

        return unique_labels

    async def _infer_smart_dependencies(
        self, tasks: List[Task], analysis: PRDAnalysis
    ) -> List[Dict[str, Any]]:
        """Use AI to infer intelligent dependencies."""
        # Use the existing dependency inferer with AI enhancement
        dependency_graph = await self.dependency_inferer.infer_dependencies(tasks)

        # Convert to result format
        dependencies = []
        for edge in dependency_graph.edges:
            dependencies.append(
                {
                    "dependent_task_id": edge.dependent_task_id,
                    "dependency_task_id": edge.dependency_task_id,
                    "dependency_type": edge.dependency_type,
                    "confidence": edge.confidence,
                    "reasoning": edge.reasoning,
                    "source": edge.source,  # GH-129: Track dependency source
                }
            )

        # BUGFIX: Filter out ONLY pattern-based design→implement/test dependencies.
        # Pattern-based inference incorrectly adds ALL design tasks as dependencies
        # for ALL implement/test tasks. We filter ONLY pattern-sourced ones and
        # preserve dependencies from other sources (PRD logic, manual, etc.).
        filtered_dependencies = []

        for dep in dependencies:
            dep_task = next(
                (t for t in tasks if t.id == dep["dependency_task_id"]), None
            )
            dependent_task = next(
                (t for t in tasks if t.id == dep["dependent_task_id"]), None
            )

            # Only filter design→implement/test dependencies FROM PATTERN MATCHING
            # Preserve deps from other sources (PRD bundled designs, manual, etc.)
            is_design_task = dep_task and dep_task.name.lower().startswith("design ")
            is_implement_or_test_task = dependent_task and (
                dependent_task.name.lower().startswith("implement ")
                or dependent_task.name.lower().startswith("test ")
            )
            is_pattern_sourced = dep.get("source") == "pattern_matching"

            # Filter out ONLY pattern-sourced design→implement/test (GH-129)
            # This preserves PRD bundled design deps and other manually created deps
            if is_design_task and is_implement_or_test_task and is_pattern_sourced:
                # Type narrowing: we know both are not None from conditions above
                assert dependent_task is not None
                assert dep_task is not None
                logger.debug(
                    f"Filtered pattern-based design dependency: "
                    f"{dependent_task.name} -x-> {dep_task.name}"
                )
                continue

            filtered_dependencies.append(dep)

        dependencies = filtered_dependencies
        logger.info(
            f"Filtered {len(dependencies)} dependencies "
            f"(removed design→implement/test)"
        )

        # Add PRD-specific dependencies (domain-aware)
        prd_dependencies = await self._add_prd_specific_dependencies(tasks, analysis)
        dependencies.extend(prd_dependencies)

        return dependencies

    async def _assess_implementation_risks(
        self, tasks: List[Task], analysis: PRDAnalysis, constraints: ProjectConstraints
    ) -> Dict[str, Any]:
        """Assess implementation risks with AI analysis."""
        risk_assessment: Dict[str, Any] = {
            "overall_risk_level": "medium",
            "risk_factors": [],
            "mitigation_strategies": [],
            "critical_path_risks": [],
            "resource_risks": [],
            "timeline_risks": [],
        }

        # Analyze complexity risks
        complexity_risks = await self._analyze_complexity_risks(tasks, analysis)
        risk_factors_list: List[Dict[str, Any]] = risk_assessment["risk_factors"]
        risk_factors_list.extend(complexity_risks)

        # Analyze constraint risks
        constraint_risks = await self._analyze_constraint_risks(tasks, constraints)
        timeline_risks_list: List[Dict[str, Any]] = risk_assessment["timeline_risks"]
        timeline_risks_list.extend(constraint_risks)

        # Generate mitigation strategies
        mitigation = await self._generate_mitigation_strategies(
            risk_factors_list, tasks, analysis
        )
        risk_assessment["mitigation_strategies"] = mitigation

        # Calculate overall risk level
        risk_count = len(risk_assessment["risk_factors"])
        if risk_count < 3:
            risk_assessment["overall_risk_level"] = "low"
        elif risk_count > 6:
            risk_assessment["overall_risk_level"] = "high"

        return risk_assessment

    async def _predict_timeline(
        self,
        tasks: List[Task],
        dependencies: List[Dict[str, Any]],
        constraints: ProjectConstraints,
    ) -> Dict[str, Any]:
        """Predict project timeline with AI-enhanced estimation."""
        # Calculate critical path
        total_effort = sum(task.estimated_hours or 8 for task in tasks)

        # Adjust for team size and parallel work
        # Ensure team_productivity is at least 1 to avoid division by zero
        team_productivity = max(
            1, min(constraints.team_size, len(tasks) // 2)
        )  # Diminishing returns, min 1
        parallel_factor = 0.7 if team_productivity > 1 else 1.0

        # Calculate duration
        working_hours_per_day = 6  # Assume 6 productive hours per day
        estimated_days = (total_effort * parallel_factor) / (
            team_productivity * working_hours_per_day
        )

        # Add buffer for unknowns and coordination overhead
        buffer_factor = 1.3  # 30% buffer
        estimated_days *= buffer_factor

        # Timeline prediction
        start_date = datetime.now(timezone.utc)
        estimated_completion = start_date + timedelta(days=estimated_days)

        timeline = {
            "estimated_duration_days": int(estimated_days),
            "estimated_completion_date": estimated_completion.isoformat(),
            "total_effort_hours": total_effort,
            "critical_path_tasks": await self._identify_critical_path_tasks(
                tasks, dependencies
            ),
            "milestone_dates": await self._calculate_milestone_dates(
                start_date, estimated_days
            ),
            "confidence_interval": {
                "optimistic_days": int(estimated_days * 0.8),
                "pessimistic_days": int(estimated_days * 1.4),
            },
        }

        return timeline

    async def _analyze_resource_requirements(
        self, tasks: List[Task], analysis: PRDAnalysis, constraints: ProjectConstraints
    ) -> Dict[str, Any]:
        """Analyze resource requirements."""
        # Skill requirements analysis
        skill_requirements = await self._analyze_skill_requirements(tasks, analysis)

        # Tool and technology requirements
        tech_requirements = await self._analyze_tech_requirements(analysis, constraints)

        # External dependency analysis
        external_deps = await self._analyze_external_dependencies(analysis)

        return {
            "required_skills": skill_requirements,
            "technology_stack": tech_requirements,
            "external_dependencies": external_deps,
            "estimated_team_size": self._calculate_optimal_team_size(
                tasks, constraints
            ),
            "specialized_roles_needed": await self._identify_specialized_roles(
                tasks, analysis
            ),
        }

    async def _generate_success_criteria(
        self, analysis: PRDAnalysis, tasks: List[Task]
    ) -> List[str]:
        """Generate project success criteria."""
        criteria = []

        # Add criteria from business objectives
        for objective in analysis.business_objectives:
            criteria.append(f"Business objective met: {objective}")

        # Add criteria from success metrics
        criteria.extend(analysis.success_metrics)

        # Add technical completion criteria
        criteria.append("All development tasks completed successfully")
        criteria.append("All tests passing with required coverage")
        criteria.append("Application deployed and accessible")

        # Add quality criteria
        if analysis.non_functional_requirements:
            criteria.append("Non-functional requirements satisfied")

        return criteria

    def _calculate_generation_confidence(
        self, analysis: PRDAnalysis, tasks: List[Task]
    ) -> float:
        """Calculate confidence in task generation quality."""
        factors = []

        # PRD analysis confidence
        factors.append(analysis.confidence)

        # Task detail completeness
        detailed_tasks = sum(
            1 for task in tasks if task.description and len(task.description) > 20
        )
        task_detail_score = detailed_tasks / len(tasks) if tasks else 0
        factors.append(task_detail_score)

        # Requirement coverage
        req_count = len(analysis.functional_requirements) + len(
            analysis.non_functional_requirements
        )
        # Expect 3 tasks per requirement
        coverage_score = min(len(tasks) / max(req_count * 3, 1), 1.0)
        factors.append(coverage_score)

        return sum(factors) / len(factors) if factors else 0.5

    # Removed fallback simulation methods - now uses proper Marcus
    # Error Framework. When AI analysis fails, the system will raise
    # appropriate errors with actionable feedback

    # Additional helper methods would be implemented here...
    def _select_task_pattern(
        self, requirement: Dict[str, Any], complexity_mode: str = "standard"
    ) -> List[Dict[str, str]]:
        """
        Select task pattern based on feature complexity and project mode.

        Implements intelligent task pattern selection to avoid over-engineering
        simple features while maintaining proper structure for complex ones.

        Parameters
        ----------
        requirement : Dict[str, Any]
            The requirement dictionary containing:
            - id: Feature identifier
            - name: Feature name
            - complexity: One of "atomic", "simple", "coordinated", "distributed"
            - requires_design_artifacts: Boolean (optional)
        complexity_mode : str, optional
            Project complexity mode: "prototype", "standard", or "enterprise"
            Default is "standard"

        Returns
        -------
        List[Dict[str, str]]
            List of task dictionaries, each containing:
            - id: Task identifier
            - name: Task name
            - type: Task type ("design", "implementation", or "testing")

        Notes
        -----
        Task patterns by complexity and mode:

        Prototype Mode (speed-focused):
        - atomic: 1 task (Implementation only)
        - simple: 1 task (Implementation only)
        - coordinated: 2 tasks (Implementation + Testing)
        - distributed: 2 tasks (Implementation + Testing)

        Standard Mode (balanced):
        - atomic: 1 task (Implementation only)
        - simple: 2 tasks (Implementation + Testing)
        - coordinated: 3 tasks (Design + Implementation + Testing)
        - distributed: 3 tasks (Design + Implementation + Testing)

        Enterprise Mode (full traceability):
        - atomic: 2 tasks (Implementation + Testing)
        - simple: 3 tasks (Design + Implementation + Testing)
        - coordinated: 3 tasks (Design + Implementation + Testing)
        - distributed: 3 tasks (Design + Implementation + Testing)
        """
        # Validate complexity_mode
        if complexity_mode not in self.VALID_COMPLEXITY_MODES:
            logger.warning(
                f"Invalid complexity_mode '{complexity_mode}', "
                f"defaulting to 'standard'. "
                f"Valid modes: {self.VALID_COMPLEXITY_MODES}"
            )
            complexity_mode = "standard"

        req_id = requirement.get("id", "feature")
        feature_name = requirement.get("name", "Feature")
        complexity = requirement.get("complexity", "coordinated")  # Backward compatible

        # Validate complexity
        if complexity not in self.VALID_COMPLEXITIES:
            logger.warning(
                f"Invalid complexity '{complexity}', defaulting to 'coordinated'. "
                f"Valid complexities: {self.VALID_COMPLEXITIES}"
            )
            complexity = "coordinated"

        tasks = []

        # Check if bundled domain designs exist (GH-108)
        has_bundled_designs = (
            hasattr(self, "_bundled_designs") and self._bundled_designs
        )

        # Determine task pattern based on complexity and mode.
        #
        # Issue #607 step 3 (test-pair rollup): the paired ``Test {feature}``
        # task that previously accompanied every ``Implement {feature}`` task
        # at standard/enterprise complexity has been REMOVED. The behaviors
        # that must be tested are instead populated as ``completion_criteria``
        # on the implement task itself, and the worker prompt makes TDD a
        # project-wide standard ("write tests first against the criteria,
        # watch them fail, then make them pass"). The runtime validator
        # (``_validate_runtime``) remains the enforcement gate that tests
        # exist and pass. Net effect: one less task per feature per implement,
        # without losing the testing contract.
        if complexity_mode == "prototype":
            # Prototype: Speed over structure - skip design tasks.
            # Every complexity gets exactly one implement task; the test
            # behaviors are rolled up onto its completion_criteria.
            tasks.append(
                {
                    "id": f"task_{req_id}_implement",
                    "name": f"Implement {feature_name}",
                    "type": self.TASK_TYPE_IMPLEMENTATION,
                }
            )

        elif complexity_mode == "enterprise":
            # Enterprise: Full traceability with design tasks for simple+
            # features. Atomic features skip design (too simple to need
            # coordination artifacts). SKIP per-feature designs if bundled
            # domain designs exist (GH-108).
            if complexity != "atomic" and not has_bundled_designs:
                tasks.append(
                    {
                        "id": f"task_{req_id}_design",
                        "name": f"Design {feature_name}",
                        "type": self.TASK_TYPE_DESIGN,
                    }
                )
            tasks.append(
                {
                    "id": f"task_{req_id}_implement",
                    "name": f"Implement {feature_name}",
                    "type": self.TASK_TYPE_IMPLEMENTATION,
                }
            )

        else:  # standard mode (default)
            # Design ONLY for coordinated/distributed (produces coordination
            # artifacts). Atomic/simple: just implement.  SKIP per-feature
            # designs if bundled domain designs exist (GH-108).
            if complexity in ["coordinated", "distributed"] and not has_bundled_designs:
                tasks.append(
                    {
                        "id": f"task_{req_id}_design",
                        "name": f"Design {feature_name}",
                        "type": self.TASK_TYPE_DESIGN,
                    }
                )
            tasks.append(
                {
                    "id": f"task_{req_id}_implement",
                    "name": f"Implement {feature_name}",
                    "type": self.TASK_TYPE_IMPLEMENTATION,
                }
            )

        return tasks

    async def _break_down_epic(
        self,
        req: Dict[str, Any],
        analysis: PRDAnalysis,
        constraints: ProjectConstraints,
    ) -> List[Dict[str, Any]]:
        """
        Break down epic into smaller tasks using intelligent task pattern selection.

        This method now uses _select_task_pattern() to determine the appropriate
        number and type of tasks based on feature complexity and project mode.

        Parameters
        ----------
        req : Dict[str, Any]
            The requirement dictionary containing complexity metadata
        analysis : PRDAnalysis
            The full PRD analysis context
        constraints : ProjectConstraints
            Project constraints including quality requirements

        Returns
        -------
        List[Dict[str, Any]]
            List of task dictionaries for this epic
        """
        # Ensure requirement has valid ID and name (fallback generation)
        req_id = req.get("id")
        feature_name = req.get("name")

        # Fallback to other possible field names if template wasn't followed
        if not feature_name:
            feature_name = req.get("feature") or req.get("description") or "feature"
            logger.warning(
                f"AI deviated from template format. Expected 'name' "
                f"field but got: {list(req.keys())}"
            )

        if not req_id:
            # Generate ID from feature name as fallback
            feature_id = feature_name.lower()
            # Remove common words and clean up
            for word in ["for", "the", "a", "an", "and", "or", "with", "using"]:
                feature_id = feature_id.replace(f" {word} ", " ")
            # Convert to ID format
            feature_id = (
                feature_id.strip().replace(" ", "_").replace("-", "_").replace(":", "")
            )
            # Remove any non-alphanumeric characters except underscore
            feature_id = "".join(
                c if c.isalnum() or c == "_" else "" for c in feature_id
            )

            # If we still don't have a good ID, use the index from
            # functional requirements
            if not feature_id or feature_id == "feature":
                req_index = (
                    analysis.functional_requirements.index(req)
                    if req in analysis.functional_requirements
                    else 0
                )
                feature_id = f"req_{req_index}"

            req_id = feature_id
            logger.warning(
                f"AI deviated from template format. Expected 'id' "
                f"field, generated: {req_id}"
            )

        # Inject the normalized ID and name back into requirement
        # to ensure _select_task_pattern gets consistent values
        req["id"] = req_id
        req["name"] = feature_name

        # Get complexity mode from constraints (passed from create_project)
        complexity_mode = constraints.complexity_mode

        # Use intelligent task pattern selection
        tasks = self._select_task_pattern(req, complexity_mode)

        return tasks

    async def _create_nfr_tasks(
        self, nfrs: List[Dict[str, Any]], constraints: ProjectConstraints
    ) -> List[Dict[str, Any]]:
        """Create non-functional requirement tasks."""
        tasks = []
        for i, nfr in enumerate(nfrs):
            # Prefer standardized fields from template
            nfr_id = nfr.get("id")
            nfr_name = nfr.get("name")

            # Fallback to other fields if template wasn't followed
            if not nfr_name:
                nfr_name = (
                    nfr.get("requirement") or nfr.get("description") or f"NFR {i+1}"
                )
                if nfr.get("requirement") or nfr.get("description"):
                    logger.warning(
                        f"NFR deviated from template format. Expected "
                        f"'name' field but got: {list(nfr.keys())}"
                    )

            if not nfr_id:
                # Generate clean ID as fallback
                req_id = nfr_name.lower().replace(" ", "_").replace("-", "_")
                req_id = "".join(c if c.isalnum() or c == "_" else "" for c in req_id)
                nfr_id = req_id or str(i)
                logger.warning(
                    f"NFR deviated from template format. Expected 'id' "
                    f"field, generated: {nfr_id}"
                )

            # Get the description from the NFR data
            nfr_description = nfr.get("description", "")

            tasks.append(
                {
                    "id": f"nfr_task_{nfr_id}",
                    "name": f"Implement {nfr_name}",
                    "type": "nfr",
                    "description": nfr_description,  # Store the NFR description
                    "nfr_data": nfr,  # Store full NFR data for later use
                }
            )
        return tasks

    async def _create_infrastructure_tasks(
        self,
        analysis: PRDAnalysis,
        constraints: ProjectConstraints,
        project_size: str = "medium",
    ) -> List[Dict[str, Any]]:
        """Create infrastructure and setup tasks."""
        tasks = []

        # Always include basic setup for all project sizes
        tasks.append(
            {
                "id": "infra_setup",
                "name": "Set up development environment",
                "type": "setup",
            }
        )

        # Add CI/CD for standard+ projects
        if project_size in ["standard", "medium", "large", "enterprise"]:
            tasks.append(
                {
                    "id": "infra_ci_cd",
                    "name": "Configure CI/CD pipeline",
                    "type": "infrastructure",
                }
            )

        # Add deployment infrastructure only for enterprise projects
        if project_size in ["enterprise", "large"]:
            tasks.append(
                {
                    "id": "infra_deploy",
                    "name": "Set up deployment infrastructure",
                    "type": "deployment",
                }
            )

        return tasks

    def _extract_task_info(
        self, task_id: str, epic_id: str, analysis: PRDAnalysis
    ) -> Dict[str, Any]:
        """Extract task information from analysis."""
        return {
            "id": task_id,
            "epic_id": epic_id,
            "type": "development",  # Default type
            "complexity": "medium",
        }

    async def _enhance_task_with_ai(
        self,
        task_info: Dict[str, Any],
        analysis: PRDAnalysis,
        constraints: ProjectConstraints,
    ) -> Dict[str, Any]:
        """Enhance task with PRD-aware details following board quality standards."""
        task_id = task_info.get("id", "unknown")
        epic_id = task_info.get("epic_id", "unknown")

        # Get original task metadata
        task_metadata = self._task_metadata.get(task_id, {})
        original_name = task_metadata.get("original_name", "")

        # Extract meaningful context from PRD analysis
        project_context = self._extract_project_context(analysis, task_id, epic_id)

        # Generate context-aware task details
        if "design" in task_id.lower():
            name, description = self._generate_design_task(
                project_context, task_id, original_name
            )
            task_type = "design"
            estimated_hours = 6 / 60  # 6 minutes in hours
        elif "implement" in task_id.lower():
            name, description = self._generate_implementation_task(
                project_context, task_id, original_name
            )
            task_type = "implementation"
            estimated_hours = 8 / 60  # 8 minutes in hours
        elif "test" in task_id.lower():
            name, description = self._generate_testing_task(
                project_context, task_id, original_name
            )
            task_type = "testing"
            estimated_hours = 6 / 60  # 6 minutes in hours
        elif "setup" in task_id.lower() or "infra" in task_id.lower():
            name, description = self._generate_infrastructure_task(
                project_context, task_id, original_name
            )
            task_type = "setup"
            estimated_hours = 10 / 60  # 10 minutes in hours
        else:
            name, description = self._generate_generic_task(
                project_context, task_id, original_name
            )
            task_type = "feature"
            estimated_hours = 7 / 60  # 7 minutes in hours

        # Generate appropriate labels based on context and requirements
        labels = self._generate_labels(task_type, project_context, constraints)

        # Add feature label based on epic_id to group related tasks
        # This ensures tasks from the same feature share a common label
        # for phase enforcement
        if epic_id and epic_id.startswith("epic_"):
            feature_name = epic_id.replace("epic_", "").replace("_", "-")
            labels.append(f"feature:{feature_name}")

        # Generate acceptance criteria based on task type
        acceptance_criteria = self._generate_acceptance_criteria(
            task_type, project_context, name
        )

        # Generate subtasks to break down the work
        subtasks = self._generate_subtasks(task_type, project_context, name)

        return {
            "name": name,
            "description": description,
            "estimated_hours": estimated_hours,
            "labels": labels,
            "due_date": None,
            "acceptance_criteria": acceptance_criteria,
            "subtasks": subtasks,
        }

    def _determine_priority(
        self, task_info: Dict[str, Any], analysis: PRDAnalysis
    ) -> Priority:
        """Determine task priority."""
        task_type = task_info.get("type", "development")

        if task_type in ["setup", "infrastructure"]:
            return Priority.HIGH
        elif task_type in ["design", "planning"]:
            return Priority.HIGH
        elif task_type in ["testing", "deployment"]:
            return Priority.MEDIUM
        else:
            return Priority.MEDIUM

    # Additional helper methods would continue to be implemented...
    async def _add_prd_specific_dependencies(
        self, tasks: List[Task], analysis: PRDAnalysis
    ) -> List[Dict[str, Any]]:
        """
        Add PRD-specific dependencies, including bundled design dependencies.

        Creates dependencies from implement/test tasks to their domain's bundled
        design task to ensure coordination.
        """
        dependencies = []

        # Check if we have bundled designs and domain mapping
        if not hasattr(self, "_bundled_designs") or not hasattr(
            self, "_domain_mapping"
        ):
            return []

        # Create a reverse mapping: feature_id -> domain_name
        feature_to_domain = {}
        for domain_name, feature_ids in self._domain_mapping.items():
            for feature_id in feature_ids:
                feature_to_domain[feature_id] = domain_name

        # For each task, check if it needs to depend on a bundled design
        for task in tasks:
            # Handle NFR (Non-Functional Requirement) tasks FIRST
            # NFR tasks are cross-cutting concerns (performance, security, etc.)
            # They should depend on ALL bundled design tasks since they affect
            # the entire system architecture
            if task.id.startswith("nfr_task_"):
                # Add dependencies to ALL bundled design tasks
                for domain_name, design_task_id in self._bundled_designs.items():
                    dependencies.append(
                        {
                            "dependent_task_id": task.id,
                            "dependency_task_id": design_task_id,
                            "dependency_type": "architectural",
                            "confidence": 1.0,
                            "reasoning": (
                                f"NFR implementation requires {domain_name} "
                                f"architecture to be defined. NFRs are cross-cutting "
                                f"concerns that affect all system components."
                            ),
                            "source": "prd_bundled_design",
                        }
                    )
                    logger.debug(
                        f"Added NFR bundled design dependency: "
                        f"{task.id} -> {design_task_id}"
                    )
                continue  # Skip to next task

            task_id_lower = task.id.lower()

            # Only implement and test tasks depend on design
            if "implement" not in task_id_lower and "test" not in task_id_lower:
                continue

            # Extract feature_id from task_id
            # (e.g., "task_user_login_implement" -> "user_login")
            # Task IDs are in format: "task_{feature_id}_{type}"
            parts = task.id.split("_")
            if len(parts) >= 3 and parts[0] == "task":
                # Find the feature_id (everything between "task_" and the type suffix)
                type_suffixes = ["design", "implement", "test"]
                # Remove "task_" prefix
                remainder = "_".join(parts[1:])
                # Remove type suffix
                feature_id = remainder
                for suffix in type_suffixes:
                    if remainder.endswith(f"_{suffix}"):
                        feature_id = remainder[: -len(f"_{suffix}")]
                        break

                # Find which domain this feature belongs to
                feature_domain: Optional[str] = feature_to_domain.get(feature_id)

                if feature_domain:
                    # Get the bundled design task ID for this domain
                    design_task_id = self._bundled_designs.get(feature_domain)

                    if design_task_id:
                        # Add dependency: implement/test task depends on bundled design
                        dependencies.append(
                            {
                                "dependent_task_id": task.id,
                                "dependency_task_id": design_task_id,
                                "dependency_type": "architectural",
                                # High confidence - explicit bundled design dep
                                "confidence": 1.0,
                                "reasoning": (
                                    f"Implement/test tasks must wait for "
                                    f"{feature_domain} design to define "
                                    f"architecture and interfaces"
                                ),
                                "source": "prd_bundled_design",
                            }
                        )
                        logger.debug(
                            f"Added bundled design dependency: "
                            f"{task.id} -> {design_task_id}"
                        )

        logger.info(f"Added {len(dependencies)} bundled design dependencies to tasks")
        return dependencies

    async def _analyze_complexity_risks(
        self, tasks: List[Task], analysis: PRDAnalysis
    ) -> List[Dict[str, Any]]:
        """Analyze complexity-related risks."""
        return [
            {
                "type": "technical_complexity",
                "description": "Complex integration requirements",
                "impact": "medium",
            }
        ]

    async def _analyze_constraint_risks(
        self, tasks: List[Task], constraints: ProjectConstraints
    ) -> List[Dict[str, Any]]:
        """Analyze constraint-related risks."""
        risks = []
        if constraints.deadline:
            total_effort = sum(task.estimated_hours or 8 for task in tasks)
            # All datetimes should be UTC-aware. If a naive deadline is passed,
            # assume it's UTC and normalize it to prevent TypeError.
            deadline_utc = (
                constraints.deadline.replace(tzinfo=timezone.utc)
                if constraints.deadline.tzinfo is None
                else constraints.deadline
            )
            days_available = (deadline_utc - datetime.now(timezone.utc)).days
            if (
                total_effort > days_available * constraints.team_size * 6
            ):  # 6 hours per day
                risks.append(
                    {
                        "type": "timeline_pressure",
                        "description": "Insufficient time for planned work",
                        "impact": "high",
                    }
                )
        return risks

    async def _generate_mitigation_strategies(
        self, risks: List[Dict[str, Any]], tasks: List[Task], analysis: PRDAnalysis
    ) -> List[str]:
        """Generate risk mitigation strategies."""
        return [
            "Regular risk assessment reviews",
            "Maintain project buffer time",
            "Implement incremental delivery approach",
        ]

    async def _identify_critical_path_tasks(
        self, tasks: List[Task], dependencies: List[Dict[str, Any]]
    ) -> List[str]:
        """Identify tasks on the critical path."""
        # Simplified - return setup and deployment tasks as critical
        return [
            task.id
            for task in tasks
            if any(label in ["setup", "deployment"] for label in task.labels)
        ]

    async def _calculate_milestone_dates(
        self, start_date: datetime, duration_days: float
    ) -> Dict[str, str]:
        """Calculate key milestone dates."""
        milestones = {}
        milestones["design_complete"] = (
            start_date + timedelta(days=duration_days * 0.25)
        ).isoformat()
        milestones["development_complete"] = (
            start_date + timedelta(days=duration_days * 0.75)
        ).isoformat()
        milestones["testing_complete"] = (
            start_date + timedelta(days=duration_days * 0.9)
        ).isoformat()
        return milestones

    async def _analyze_skill_requirements(
        self, tasks: List[Task], analysis: PRDAnalysis
    ) -> List[str]:
        """Analyze required skills."""
        skills = set()
        for constraint in analysis.technical_constraints:
            if "react" in constraint.lower():
                skills.add("React")
            if "python" in constraint.lower():
                skills.add("Python")
            if "postgres" in constraint.lower():
                skills.add("PostgreSQL")
        return list(skills)

    async def _analyze_tech_requirements(
        self, analysis: PRDAnalysis, constraints: ProjectConstraints
    ) -> List[str]:
        """Analyze technology requirements."""
        return analysis.technical_constraints

    async def _analyze_external_dependencies(self, analysis: PRDAnalysis) -> List[str]:
        """Analyze external dependencies."""
        return ["Third-party API integrations", "External service providers"]

    def _calculate_optimal_team_size(
        self, tasks: List[Task], constraints: ProjectConstraints
    ) -> int:
        """Calculate optimal team size."""
        task_complexity = len(tasks)
        if task_complexity < 10:
            return min(2, constraints.team_size)
        elif task_complexity < 25:
            return min(4, constraints.team_size)
        else:
            return min(6, constraints.team_size)

    async def _identify_specialized_roles(
        self, tasks: List[Task], analysis: PRDAnalysis
    ) -> List[str]:
        """Identify specialized roles needed."""
        roles = ["Full-stack Developer"]

        # Check for UI/UX needs
        if any("design" in task.name.lower() for task in tasks):
            roles.append("UI/UX Designer")

        # Check for DevOps needs
        if any(
            "deploy" in task.name.lower() or "infrastructure" in task.name.lower()
            for task in tasks
        ):
            roles.append("DevOps Engineer")

        return roles

    def _extract_project_context(
        self, analysis: PRDAnalysis, task_id: str, epic_id: str
    ) -> Dict[str, Any]:
        """Extract meaningful project context from PRD analysis."""
        context = {
            "business_objectives": (
                analysis.business_objectives[:3]
                if analysis.business_objectives
                else ["deliver working solution"]
            ),
            "technical_constraints": (
                analysis.technical_constraints[:3]
                if analysis.technical_constraints
                else ["standard web application"]
            ),
            "functional_requirements": (
                analysis.functional_requirements[:5]
                if analysis.functional_requirements
                else []
            ),
            "project_type": "web application",  # Default
            "domain": "general",
        }

        # First, check if this task is for a specific functional requirement
        task_specific_domain = None
        task_specific_type = None

        # Extract the feature from task_id
        # (e.g., task_crud_operations_design -> crud_operations)
        if "task_" in task_id:
            parts = task_id.split("_")
            if len(parts) >= 3:
                feature_parts = parts[1:-1]  # Remove 'task' prefix and action suffix
                feature_id = "_".join(feature_parts)

                # Find the matching functional requirement
                for req in analysis.functional_requirements:
                    req_feature = req.get("feature", "").lower().replace(" ", "_")
                    if req_feature == feature_id:
                        # Determine domain based on requirement
                        feature_val = req.get("feature", "")
                        desc_val = req.get("description", "")
                        req_text = f"{feature_val} {desc_val}".lower()

                        crud_keywords = ["crud", "create", "read", "update", "delete"]
                        if any(word in req_text for word in crud_keywords):
                            task_specific_domain = "crud_operations"
                            task_specific_type = "REST API"
                        elif any(
                            word in req_text
                            for word in ["auth", "login", "jwt", "token"]
                        ):
                            task_specific_domain = "user_management"
                            task_specific_type = "authentication system"
                        elif any(
                            word in req_text
                            for word in ["validation", "validate", "verify"]
                        ):
                            task_specific_domain = "validation"
                            task_specific_type = "input validation system"
                        elif any(
                            word in req_text
                            for word in ["property", "properties", "schema", "model"]
                        ):
                            task_specific_domain = "data_modeling"
                            task_specific_type = "data model"
                        break

        # Use task-specific domain if found, otherwise fall back to general analysis
        if task_specific_domain:
            context["domain"] = task_specific_domain
            context["project_type"] = task_specific_type
        else:
            # Determine general project type from overall requirements
            all_text = " ".join(
                [
                    " ".join(analysis.business_objectives),
                    " ".join(analysis.technical_constraints),
                    " ".join(
                        [
                            req.get("description", "")
                            for req in analysis.functional_requirements
                        ]
                    ),
                ]
            ).lower()

            # Use more specific matching to avoid false positives
            if any(word in all_text for word in ["api", "rest", "endpoint", "crud"]):
                context["domain"] = "backend_services"
                context["project_type"] = "REST API"
            elif any(
                word in all_text for word in ["ui", "interface", "frontend", "react"]
            ):
                context["domain"] = "frontend"
                context["project_type"] = "frontend application"
            elif any(word in all_text for word in ["data", "analytics", "report"]):
                context["domain"] = "data_analytics"
                context["project_type"] = "data analytics platform"
            elif any(
                word in all_text for word in ["ecommerce", "shop", "cart", "product"]
            ):
                context["domain"] = "ecommerce"
                context["project_type"] = "e-commerce platform"

        # Extract specific requirements that match this task/epic
        relevant_requirements = []
        for req in analysis.functional_requirements:
            req_text = req.get("description", "").lower()
            if (
                task_id.lower() in req_text
                or epic_id.lower() in req_text
                or any(keyword in req_text for keyword in task_id.lower().split("_"))
            ):
                relevant_requirements.append(req)

        context["relevant_requirements"] = relevant_requirements[
            :2
        ]  # Top 2 most relevant

        return context

    def _generate_design_task(
        self, context: Dict[str, Any], task_id: str, original_name: str = ""
    ) -> Tuple[str, str]:
        """Generate design task name and description using PRD context."""
        domain = context["domain"]
        project_type = context["project_type"]
        objectives = context["business_objectives"]

        # Extract feature name from original name
        # (e.g., "Design CRUD Operations" -> "CRUD Operations")
        feature_name = original_name.replace("Design ", "") if original_name else ""

        if domain == "crud_operations":
            # Use original feature name if available, otherwise use generic
            name = original_name if original_name else "Design CRUD API Architecture"
            description = (
                f"Create architectural design and documentation for CRUD "
                f"operations in {project_type}. Define API endpoints, "
                f"document request/response formats, plan error handling "
                f"strategies, and design pagination approach. Deliverables: "
                f"API specification document, data flow diagrams, and "
                f"architectural decisions. Goal: "
                f"{objectives[0] if objectives else 'efficient data management'}."
            )
        elif domain == "data_modeling":
            name = original_name if original_name else "Design Data Model and Schema"
            description = (
                f"Design data architecture and create documentation for "
                f"{project_type}. Research data requirements, create entity "
                f"relationship diagrams, document field specifications and "
                f"constraints. Plan migration strategy and define validation "
                f"rules. Deliverables: ER diagrams, schema documentation, and "
                f"data dictionary. Focus on: "
                f"{objectives[0] if objectives else 'scalable data architecture'}."
            )
        elif domain == "validation":
            name = original_name if original_name else "Design Input Validation System"
            description = (
                f"Design validation strategy and create documentation for "
                f"{project_type}. Research validation requirements, define "
                f"validation rules and patterns, plan error handling approach. "
                f"Document security considerations and sanitization procedures. "
                f"Deliverables: validation specification document, error "
                f"message catalog, and security guidelines. Goal: "
                f"{objectives[0] if objectives else 'data integrity and security'}."
            )
        elif domain == "user_management":
            name = original_name if original_name else "Design User Authentication Flow"
            description = (
                f"Design authentication architecture and create documentation "
                f"for {project_type}. Research security requirements, create "
                f"user flow diagrams, document authentication patterns and "
                f"session management approach. Plan security protocols and "
                f"define user account lifecycle. Deliverables: authentication "
                f"flow diagrams, security documentation, and API specifications. "
                f"Goal: {objectives[0] if objectives else 'secure user access'}."
            )
        elif domain == "frontend":
            name = (
                original_name if original_name else "Design User Interface Architecture"
            )
            description = (
                f"Create detailed UI/UX design for {project_type}. Include "
                f"component hierarchy, design system, responsive layouts, and "
                f"user interaction patterns. Focus on achieving: "
                f"{objectives[0] if objectives else 'excellent user experience'}. "
                f"Define accessibility standards and usability requirements."
            )
        elif domain == "backend_services":
            name = original_name if original_name else "Design API Architecture"
            description = (
                f"Design API architecture for {project_type}. Research "
                f"requirements, document API specifications, define endpoint "
                f"patterns and data contracts. Create architectural diagrams "
                f"and technical documentation. Deliverables: API documentation, "
                f"architectural decisions, and interface specifications. "
                f"Focus on: {objectives[0] if objectives else 'scalable API design'}."
            )
        elif domain == "ecommerce":
            name = (
                original_name if original_name else "Design E-commerce User Experience"
            )
            description = (
                f"Design comprehensive e-commerce user experience for "
                f"{project_type}. Include product catalog, shopping cart, "
                f"checkout flow, user accounts, and order management. "
                f"Optimize for: "
                f"{objectives[0] if objectives else 'seamless shopping experience'}."
            )
        else:
            # For any other domain, use original name or create one from
            # feature
            name = (
                original_name
                if original_name
                else (
                    f"Design {feature_name if feature_name else project_type.title()} "
                    f"Architecture"
                )
            )
            description = (
                f"Research and design architecture for {project_type}. Create "
                f"documentation defining approach, patterns, and specifications. "
                f"Plan component structure and integration points. Deliverables: "
                f"design documentation, architectural diagrams, and technical "
                f"specifications. Goal: "
                f"{objectives[0] if objectives else 'effective solution delivery'}."
            )

        # Add specific requirements if available
        if context["relevant_requirements"]:
            req = context["relevant_requirements"][0]
            description += (
                f" Specific requirement: {req.get('description', '')[:100]}..."
            )

        return name, description

    def _generate_implementation_task(
        self, context: Dict[str, Any], task_id: str, original_name: str = ""
    ) -> Tuple[str, str]:
        """Generate implementation task name and description using PRD context."""
        domain = context["domain"]
        project_type = context["project_type"]
        tech_constraints = context["technical_constraints"]

        if domain == "user_management":
            name = (
                original_name
                if original_name
                else "Implement User Authentication Service"
            )
            description = (
                f"Build secure user authentication service for {project_type}. "
                f"Implement user registration, login, JWT token management, "
                f"password hashing with bcrypt, and session handling. "
                f"Technology stack: {', '.join(tech_constraints)}. Include "
                f"rate limiting, email verification, and comprehensive error "
                f"handling."
            )
        elif domain == "frontend":
            name = original_name if original_name else "Build User Interface Components"
            description = (
                f"Develop responsive UI components for {project_type}. Create "
                f"reusable component library, implement state management, handle "
                f"user interactions, and ensure accessibility compliance. Using: "
                f"{', '.join(tech_constraints)}. Include loading states, error "
                f"boundaries, and responsive design."
            )
        elif domain == "backend_services":
            name = original_name if original_name else "Develop Backend API Services"
            objectives = context.get("business_objectives", [])
            description = (
                f"Implement backend API services for {project_type} following "
                f"the design specifications. Build endpoints, business logic, "
                f"data validation, and error handling. Include appropriate tests "
                f"and logging. Technology: {', '.join(tech_constraints)}. Goal: "
                f"{objectives[0] if objectives else 'working implementation'}."
            )
        elif domain == "ecommerce":
            name = original_name if original_name else "Build E-commerce Core Features"
            description = (
                f"Implement core e-commerce functionality for {project_type}. "
                f"Build product catalog, shopping cart, checkout process, "
                f"payment integration, and order management. Stack: "
                f"{', '.join(tech_constraints)}. Include inventory management "
                f"and order tracking."
            )
        elif domain == "crud_operations":
            name = original_name if original_name else "Implement CRUD API Endpoints"
            description = (
                f"Build complete CRUD (Create, Read, Update, Delete) "
                f"functionality for {project_type}. Implement RESTful endpoints "
                f"with proper HTTP methods, request/response handling, data "
                f"validation, and error responses. Technology: "
                f"{', '.join(tech_constraints)}. Include pagination, filtering, "
                f"and sorting capabilities."
            )
        elif domain == "data_modeling":
            name = (
                original_name
                if original_name
                else "Implement Data Models and Database Layer"
            )
            description = (
                f"Create data models and database integration for "
                f"{project_type}. Define schemas, implement ORM/ODM models, "
                f"set up migrations, add indexes for performance, and implement "
                f"data validation. Stack: {', '.join(tech_constraints)}. Include "
                f"relationships, constraints, and data integrity rules."
            )
        elif domain == "validation":
            name = (
                original_name
                if original_name
                else "Implement Input Validation and Sanitization"
            )
            description = (
                f"Build comprehensive validation layer for {project_type}. "
                f"Implement input validation rules, data sanitization, type "
                f"checking, business rule validation, and error message "
                f"formatting. Technology: {', '.join(tech_constraints)}. Include "
                f"XSS prevention, SQL injection protection, and data format "
                f"validation."
            )
        else:
            # Extract feature name from original name
            feature_name = (
                original_name.replace("Implement ", "") if original_name else ""
            )
            name = (
                original_name
                if original_name
                else (
                    f"Implement "
                    f"{feature_name if feature_name else project_type.title()} "
                    f"Core Features"
                )
            )
            description = (
                f"Build core functionality for {project_type}. Implement "
                f"business logic, data processing, user interfaces, and system "
                f"integrations. Using: {', '.join(tech_constraints)}. Include "
                f"proper error handling, logging, and performance optimization."
            )

        # Add specific requirements if available
        if context["relevant_requirements"]:
            req = context["relevant_requirements"][0]
            description += (
                f" Addresses requirement: {req.get('description', '')[:100]}..."
            )

        return name, description

    def _generate_testing_task(
        self, context: Dict[str, Any], task_id: str, original_name: str = ""
    ) -> Tuple[str, str]:
        """Generate testing task name and description using PRD context."""
        domain = context["domain"]
        project_type = context["project_type"]

        if domain == "user_management":
            name = (
                original_name
                if original_name
                else "Test Authentication Security Features"
            )
            description = (
                f"Create comprehensive test suite for user authentication in "
                f"{project_type}. Include unit tests for login/registration, "
                f"integration tests for JWT flows, security testing for password "
                f"policies, and end-to-end user journey tests. Achieve >80% code "
                f"coverage."
            )
        elif domain == "frontend":
            name = original_name if original_name else "Test User Interface Components"
            description = (
                f"Develop UI testing suite for {project_type}. Include component "
                f"unit tests, user interaction tests, accessibility testing, "
                f"responsive design validation, and cross-browser compatibility "
                f"tests. Test all user flows and error states."
            )
        elif domain == "backend_services":
            name = (
                original_name
                if original_name
                else "Test API Functionality and Performance"
            )
            description = (
                f"Create API testing suite for {project_type}. Include endpoint "
                f"unit tests, integration tests, load testing, security testing, "
                f"and error handling validation. Test data validation, "
                f"authentication, and business logic. Achieve >80% coverage."
            )
        elif domain == "ecommerce":
            name = (
                original_name if original_name else "Test E-commerce Transaction Flows"
            )
            description = (
                f"Develop comprehensive testing for {project_type}. Test "
                f"shopping cart functionality, checkout process, payment "
                f"integration, order management, and inventory updates. Include "
                f"security testing for payment processing and fraud prevention."
            )
        elif domain == "crud_operations":
            name = (
                original_name
                if original_name
                else "Test CRUD Operations and API Endpoints"
            )
            description = (
                f"Create comprehensive test suite for CRUD operations in "
                f"{project_type}. Test all HTTP methods (GET, POST, PUT, DELETE), "
                f"validate request/response formats, test error handling, "
                f"pagination, filtering, and edge cases. Include load testing for "
                f"concurrent operations. Achieve >80% coverage."
            )
        elif domain == "data_modeling":
            name = (
                original_name
                if original_name
                else "Test Data Models and Database Operations"
            )
            description = (
                f"Develop database testing suite for {project_type}. Test model "
                f"validations, database constraints, migrations, relationships, "
                f"data integrity, and transaction handling. Include performance "
                f"testing for queries and indexes. Validate data consistency and "
                f"error scenarios."
            )
        elif domain == "validation":
            name = (
                original_name if original_name else "Test Input Validation and Security"
            )
            description = (
                f"Create validation testing suite for {project_type}. Test all "
                f"validation rules, boundary conditions, invalid inputs, injection "
                f"attempts, XSS prevention, and error message accuracy. Include "
                f"fuzz testing and security vulnerability scanning. Ensure "
                f"comprehensive input sanitization coverage."
            )
        else:
            # Extract feature name from original name
            feature_name = original_name.replace("Test ", "") if original_name else ""
            name = (
                original_name
                if original_name
                else (
                    f"Test {feature_name if feature_name else project_type.title()} "
                    f"Functionality"
                )
            )
            description = (
                f"Create comprehensive test suite for {project_type}. Include "
                f"unit tests, integration tests, and end-to-end testing. Validate "
                f"business logic, user workflows, and system reliability. Achieve "
                f">80% code coverage."
            )

        return name, description

    def _generate_infrastructure_task(
        self, context: Dict[str, Any], task_id: str, original_name: str = ""
    ) -> Tuple[str, str]:
        """Generate infrastructure task name and description using PRD context."""
        project_type = context["project_type"]
        tech_constraints = context["technical_constraints"]

        if "setup" in task_id.lower():
            name = original_name if original_name else "Setup Development Environment"
            description = (
                f"Configure complete development environment for {project_type}. "
                f"Set up local development stack, database, environment variables, "
                f"development tools, and project dependencies. Technology: "
                f"{', '.join(tech_constraints)}. Include Docker containers, hot "
                f"reloading, and debugging tools."
            )
        elif "ci" in task_id.lower() or "cd" in task_id.lower():
            name = original_name if original_name else "Configure CI/CD Pipeline"
            description = (
                f"Set up continuous integration and deployment for {project_type}. "
                f"Configure automated testing, code quality checks, building, and "
                f"deployment to staging/production. Using: "
                f"{', '.join(tech_constraints)}. Include security scanning and "
                f"performance monitoring."
            )
        elif "deploy" in task_id.lower():
            name = original_name if original_name else "Setup Production Deployment"
            description = (
                f"Configure production infrastructure for {project_type}. Set up "
                f"hosting, load balancing, monitoring, logging, backup systems, "
                f"and security measures. Technology: {', '.join(tech_constraints)}. "
                f"Include scaling strategy and disaster recovery."
            )
        else:
            name = original_name if original_name else "Configure System Infrastructure"
            description = (
                f"Set up core infrastructure for {project_type}. Configure "
                f"servers, databases, caching, monitoring, and security systems. "
                f"Stack: {', '.join(tech_constraints)}. Include performance "
                f"optimization and maintenance procedures."
            )

        return name, description

    def _generate_generic_task(
        self, context: Dict[str, Any], task_id: str, original_name: str = ""
    ) -> Tuple[str, str]:
        """Generate generic task name and description using PRD context."""
        project_type = context["project_type"]
        objectives = context["business_objectives"]

        # Try to infer from task_id what this might be about
        if "nfr" in task_id.lower():
            # Use original_name if available to preserve unique NFR names
            if original_name and original_name != "":
                name = original_name
            else:
                # Extract NFR type from task_id if possible (e.g., nfr_task_performance)
                nfr_type = task_id.replace("nfr_task_", "").replace("_", " ").title()
                if nfr_type and nfr_type != task_id:
                    name = f"Implement {nfr_type} Requirements"
                else:
                    name = "Implement Non-Functional Requirements"
            # Use the stored NFR description if available
            task_metadata = self._task_metadata.get(task_id, {})
            stored_description = task_metadata.get("description", "")
            if stored_description:
                description = stored_description
            else:
                # Fallback to generic description
                description = (
                    f"Address performance, security, and scalability "
                    f"requirements for {project_type}. Implement caching, "
                    f"optimize database queries, add security headers, and ensure "
                    f"system reliability. Target: "
                    f"{objectives[0] if objectives else 'system performance'}."
                )
        elif any(keyword in task_id.lower() for keyword in ["req_0", "req_1", "req_2"]):
            req_index = next(
                (
                    i
                    for i, keyword in enumerate(["req_0", "req_1", "req_2"])
                    if keyword in task_id.lower()
                ),
                0,
            )
            if req_index < len(context["functional_requirements"]):
                req = context["functional_requirements"][req_index]
                req_desc = req.get("description", "feature requirement")
                name = f"Implement {req_desc[:30]}..."
                description = (
                    f"Complete implementation of: {req_desc}. For {project_type} "
                    f"to achieve: "
                    f"{objectives[0] if objectives else 'project goals'}."
                )
            else:
                name = f"Implement Core {project_type.title()} Feature"
                description = (
                    f"Build essential functionality for {project_type}. "
                    f"Implement core business logic, user interactions, and "
                    f"system integrations to achieve: "
                    f"{objectives[0] if objectives else 'project success'}."
                )
        else:
            name = f"Develop {project_type.title()} Component"
            description = (
                f"Build and integrate component for {project_type}. Implement "
                f"required functionality, ensure proper testing, and maintain "
                f"code quality standards. Supports: "
                f"{objectives[0] if objectives else 'project objectives'}."
            )

        return name, description

    def _generate_labels(
        self, task_type: str, context: Dict[str, Any], constraints: ProjectConstraints
    ) -> List[str]:
        """Generate appropriate labels following Board Quality Standards taxonomy."""
        labels = []

        # Component labels
        domain = context["domain"]
        if domain == "user_management":
            labels.append("component:authentication")
        elif domain == "frontend":
            labels.append("component:frontend")
        elif domain == "backend_services":
            labels.append("component:backend")
        elif domain == "ecommerce":
            labels.append("component:ecommerce")
        else:
            # For REST API projects, use API label
            project_type = context.get("project_type", "").lower()
            if "api" in project_type or "rest" in project_type:
                labels.append("component:api")
            else:
                labels.append("component:backend")  # Default

        # Type labels
        if task_type == "design":
            labels.append("type:design")
        elif task_type == "implementation":
            labels.append("type:feature")
        elif task_type == "testing":
            labels.append("type:testing")
        elif task_type == "setup":
            labels.append("type:setup")
        else:
            labels.append("type:feature")

        # Priority labels (default to medium)
        labels.append("priority:medium")

        # Skill labels based on constraints
        if constraints.available_skills:
            for skill in constraints.available_skills[:1]:  # Take first skill
                if skill.lower() in ["react", "vue", "angular"]:
                    labels.append("skill:frontend")
                    break
                elif skill.lower() in ["node.js", "nodejs", "python", "java"]:
                    labels.append("skill:backend")
                    break
                elif skill.lower() in ["docker", "kubernetes", "aws"]:
                    labels.append("skill:devops")
                    break
                else:
                    labels.append(f"skill:{skill.lower()}")
                    break
        else:
            labels.append("skill:fullstack")

        # Complexity labels
        if task_type in ["design", "setup"]:
            labels.append("complexity:moderate")
        elif task_type == "testing":
            labels.append("complexity:simple")
        else:
            labels.append("complexity:moderate")

        return labels

    def _generate_acceptance_criteria(
        self, task_type: str, context: Dict[str, Any], task_name: str
    ) -> List[str]:
        """Generate acceptance criteria based on task type and context."""
        criteria = []

        if task_type == "design":
            criteria = [
                "Design documentation is complete with all components specified",
                "User flows and wireframes are created and reviewed",
                "Technical architecture is documented and approved",
                "Design system components are defined",
                "Accessibility requirements are documented",
            ]
        elif task_type == "implementation":
            criteria = [
                "All functionality is implemented as per specifications",
                "All tests run successfully without errors",
                "Code follows project coding standards and conventions",
                "API endpoints are documented and tested",
                "Error handling and validation are implemented",
                "Performance meets defined benchmarks",
            ]
        elif task_type == "testing":
            criteria = [
                "All test cases are written and documented",
                "Unit tests achieve >80% code coverage",
                "Integration tests cover all API endpoints",
                "End-to-end tests validate user workflows",
                "Performance tests meet SLA requirements",
                "Test results are documented and reviewed",
            ]
        elif task_type == "setup":
            criteria = [
                "Development environment runs successfully",
                "All dependencies are installed and documented",
                "Configuration files are properly set up",
                "Database migrations run without errors",
                "README includes setup instructions",
                "Team members can successfully run the project",
            ]
        elif task_type == "deployment":
            criteria = [
                "Application deploys successfully to target environment",
                "All environment variables are configured",
                "Health checks pass in production",
                "Monitoring and logging are operational",
                "Rollback procedure is documented and tested",
                "Performance meets production requirements",
            ]
        else:
            # Generic criteria for feature tasks
            criteria = [
                f"{task_name} is fully implemented and functional",
                "Feature works as specified in requirements",
                "Code is tested and passes all tests",
                "Documentation is updated",
                "Code review is completed and approved",
            ]

        # Add context-specific criteria
        if context.get("domain") == "user_management":
            criteria.append(
                "Security requirements are met (authentication, authorization)"
            )
            criteria.append("User data privacy is properly handled")
        elif context.get("domain") == "ecommerce":
            criteria.append("Payment processing is secure and PCI compliant")
            criteria.append("Order workflow is thoroughly tested")

        return criteria[:5]  # Return top 5 most relevant criteria

    def _generate_test_coverage_criteria(
        self,
        feature_name: str,
        base_description: str = "",
    ) -> List[str]:
        """Generate test-coverage criteria strings for a feature.

        Issue #607 step 3 — these strings replace the per-feature ``Test
        {feature}`` board task. They are attached to the paired ``Implement
        {feature}`` task as ``completion_criteria`` and surfaced to the
        agent via ``request_next_task``. Combined with the worker prompt's
        TDD-as-standard directive (write tests first, watch fail, then
        make pass), this preserves the testing contract without the
        per-feature task explosion documented in #607.

        The criteria name *behaviors that must be tested*, not test
        framework or structure: the agent picks pytest / unittest / jest
        / whatever, decides assertion style, and writes the actual tests.
        The runtime validator (``_validate_runtime``) is the enforcement
        gate that tests exist and pass.

        Parameters
        ----------
        feature_name : str
            Feature being implemented (e.g., ``"User Login"``,
            ``"Mark Complete"``). Used to anchor criteria to the feature.
        base_description : str, optional
            Feature requirement description. Reserved for future use by
            heuristics that infer feature-specific edge cases; ignored
            today so the helper is fully deterministic.

        Returns
        -------
        List[str]
            Non-empty list of test-behavior criterion strings. Each string
            names a behavior to cover; no string names a framework, test
            file, assertion library, or other implementation HOW.
        """
        # ``base_description`` is intentionally accepted but not used
        # in this first cut — keeping the signature future-proof for
        # heuristics that would mine the description for domain-specific
        # cases (e.g., monetary rounding, timezone handling) without
        # changing every call site. Reference it to make the intent
        # clear to readers and silence ``unused argument`` linters.
        _ = base_description

        # Defaults cover the four behavior categories that an LLM agent
        # cannot fake under TDD without writing real tests first:
        # happy path, invalid input, error/edge cases, and a contract
        # statement tying tests to the feature. Phrasing is deliberately
        # generic enough to apply to any feature while still being
        # specific to ``feature_name``.
        criteria: List[str] = [
            (
                f"Tests cover the happy path for {feature_name} with valid "
                f"input and expected behavior."
            ),
            (
                f"Tests cover invalid input handling for {feature_name} "
                f"(missing fields, malformed values, type mismatches)."
            ),
            (
                f"Tests cover error and edge cases for {feature_name} "
                f"(boundaries, empty/large inputs, repeated calls)."
            ),
            (
                "Tests were written before the implementation, watched "
                "fail, then made to pass — and were not modified to fit "
                "the implementation."
            ),
        ]
        return criteria

    def _generate_subtasks(
        self, task_type: str, context: Dict[str, Any], task_name: str
    ) -> List[str]:
        """Generate subtasks to break down the work."""
        subtasks = []

        if task_type == "design":
            subtasks = [
                "Research existing solutions and best practices",
                "Create initial wireframes and mockups",
                "Design component hierarchy and data flow",
                "Document API contracts and interfaces",
                "Create design system tokens and components",
                "Review design with stakeholders",
            ]
        elif task_type == "implementation":
            # Parse the task name to understand what we're implementing
            if "authentication" in task_name.lower():
                subtasks = [
                    "Set up authentication middleware",
                    "Implement user registration endpoint",
                    "Create login/logout functionality",
                    "Add password reset flow",
                    "Implement JWT token management",
                    "Add session management",
                    "Create user profile endpoints",
                ]
            elif "database" in task_name.lower():
                subtasks = [
                    "Design database schema",
                    "Create migration scripts",
                    "Set up database connections",
                    "Implement data models",
                    "Add database indexes",
                    "Create seed data scripts",
                ]
            elif "api" in task_name.lower():
                subtasks = [
                    "Define API endpoints and routes",
                    "Implement request validation",
                    "Create response serializers",
                    "Add error handling middleware",
                    "Implement rate limiting",
                    "Add API documentation",
                ]
            else:
                # Generic implementation subtasks
                subtasks = [
                    "Create data models and schemas",
                    "Implement business logic layer",
                    "Create API endpoints",
                    "Add input validation",
                    "Implement error handling",
                    "Add integration tests",
                ]
        elif task_type == "testing":
            subtasks = [
                "Write unit test specifications",
                "Implement unit tests for models",
                "Create integration test suite",
                "Add API endpoint tests",
                "Write end-to-end test scenarios",
                "Set up test data fixtures",
                "Configure test automation",
            ]
        elif task_type == "setup":
            subtasks = [
                "Initialize project repository",
                "Set up development dependencies",
                "Configure build tools",
                "Create environment configuration",
                "Set up database connections",
                "Configure linting and formatting",
                "Create development scripts",
            ]
        elif task_type == "deployment":
            subtasks = [
                "Create deployment configuration",
                "Set up CI/CD pipeline",
                "Configure environment variables",
                "Set up monitoring and alerts",
                "Create deployment scripts",
                "Configure load balancing",
                "Set up backup procedures",
            ]
        else:
            # Generic feature subtasks
            subtasks = [
                f"Plan {task_name} implementation",
                "Implement core functionality",
                "Add data persistence layer",
                "Create user interface components",
                "Write tests",
                "Update documentation",
            ]

        # Customize based on context
        if context.get("tech_stack"):
            tech = context["tech_stack"]
            if "React" in tech and task_type == "implementation":
                subtasks.extend(
                    [
                        "Create React components",
                        "Set up component state management",
                        "Add component styling",
                    ]
                )
            elif "Django" in tech and task_type == "implementation":
                subtasks.extend(
                    [
                        "Create Django models",
                        "Add Django views and serializers",
                        "Configure Django admin",
                    ]
                )

        return subtasks[:7]  # Return top 7 most relevant subtasks

    def _should_skip_epic(self, epic_id: str, deployment_target: str) -> bool:
        """Determine if an epic should be skipped based on deployment target."""
        # Skip deployment and production epics for local development
        if deployment_target == "local":
            skip_keywords = [
                "deployment",
                "production",
                "deploy",
                "release",
                "hosting",
                "infrastructure",
            ]
            return any(keyword in epic_id.lower() for keyword in skip_keywords)

        # Skip advanced deployment features for dev environment
        elif deployment_target == "dev":
            skip_keywords = [
                "production",
                "scaling",
                "monitoring",
                "optimization",
                "disaster_recovery",
            ]
            return any(keyword in epic_id.lower() for keyword in skip_keywords)

        # Include everything for prod and remote
        return False

    def _should_skip_task(
        self, task_id: str, epic_id: str, deployment_target: str
    ) -> bool:
        """Determine if a task should be skipped based on deployment target."""
        # Design tasks are deployment-agnostic - never skip them.
        # GH-180 pattern: strong signal (task type) overrides weak signal
        # (keyword match). Without this, LLM-generated domain names like
        # "Gameplay Domain" produce task IDs containing "domain", falsely
        # matching the DNS/hosting skip keyword.
        task_meta = self._task_metadata.get(task_id, {})
        if task_meta.get("type") == self.TASK_TYPE_DESIGN:
            return False

        task_lower = task_id.lower()

        # Skip deployment tasks for local development
        if deployment_target == "local":
            skip_keywords = [
                "deploy",
                "production",
                "hosting",
                "server",
                "cloud",
                "aws",
                "azure",
                "gcp",
                "kubernetes",
                "docker",
                "container",
                "load_balancer",
                "cdn",
                "ssl",
                "domain",
                "dns",
            ]
            return any(keyword in task_lower for keyword in skip_keywords)

        # Skip production-specific tasks for dev environment
        elif deployment_target == "dev":
            skip_keywords = [
                "production",
                "prod_",
                "scaling",
                "auto_scale",
                "load_balancer",
                "disaster_recovery",
                "backup",
                "monitoring",
                "alerting",
                "performance_optimization",
                "cdn",
                "multi_region",
            ]
            return any(keyword in task_lower for keyword in skip_keywords)

        # Include everything for prod and remote
        return False

    def _filter_requirements_by_size(
        self,
        requirements: List[Dict[str, Any]],
        project_size: str,
        team_size: int,
        prd_content: str,
        protected_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Filter functional requirements based on project size and team capacity.

        Now respects user-specified requirements - if the user explicitly listed
        features, all of them are kept regardless of project size.

        Parameters
        ----------
        requirements : List[Dict[str, Any]]
            Functional requirements from AI analysis
        project_size : str
            Project size (prototype, standard, enterprise)
        team_size : int
            Number of team members
        prd_content : str
            Original PRD content to detect specificity

        Returns
        -------
        List[Dict[str, Any]]
            Filtered requirements
        """
        original_count = len(requirements)
        original_ids = [
            req.get("id", req.get("name", "unknown")) for req in requirements
        ]

        # Detect if user provided explicit requirements
        specificity = self._detect_prompt_specificity(prd_content)

        if specificity == "explicit":
            # User explicitly listed requirements - keep ALL of them
            logger.info(
                f"User provided explicit requirements ({original_count} items), "
                f"keeping all regardless of project_size={project_size}"
            )

            # Warn if count seems high for project size
            expected_ranges = {
                "prototype": (3, 5),
                "mvp": (3, 5),
                "standard": (8, 15),
                "small": (5, 10),
                "medium": (8, 15),
                "enterprise": (15, 30),
                "large": (15, 30),
            }

            if project_size in expected_ranges:
                min_expected, max_expected = expected_ranges[project_size]
                if original_count > max_expected:
                    logger.warning(
                        f"User specified {original_count} requirements, "
                        f"which is high for {project_size} mode "
                        f"(expected {min_expected}-{max_expected}). "
                        f"Consider using a larger project size."
                    )
                elif original_count < min_expected:
                    logger.warning(
                        f"User specified {original_count} requirements, "
                        f"which is low for {project_size} mode "
                        f"(expected {min_expected}-{max_expected}). "
                        f"Consider using a smaller project size."
                    )

            return requirements

        # Guided mode - AI features, apply capacity filtering
        logger.info(
            f"AI-guided requirements ({original_count} items), "
            f"filtering for project_size={project_size}"
        )

        # #683 Cause 1: cap by the project's complexity tier, NOT team
        # size. ``project_size`` IS the complexity mode; team size governs
        # parallelism, not scope. The old ``requirements[:max(3, team_size)]``
        # cut a "medium" project (whose own expected range is 8-15) down to
        # 3 by list position, silently dropping core features (the snake
        # collision / game-over / restart loss in #683). The cap is the
        # upper bound of the tier's expected feature range.
        tier_caps = {
            "prototype": 5,
            "mvp": 5,
            "small": 10,
            "standard": 15,
            "medium": 15,
            "enterprise": 30,
            "large": 30,
        }
        cap = tier_caps.get(project_size, 15)

        # #683 Cause 1: never drop a CORE feature (one that serves an
        # in-scope user outcome, per the LLM mapping the caller passes in).
        # Keep all protected requirements first; fill the rest of the cap
        # with non-core features in order; trim only genuine scope-creep.
        protected = protected_ids or set()

        def _rid(req: Dict[str, Any]) -> str:
            return str(req.get("id") or req.get("name") or "unknown")

        core = [r for r in requirements if _rid(r) in protected]
        non_core = [r for r in requirements if _rid(r) not in protected]

        # Core features are never dropped, even if they alone exceed the
        # cap (correctness floor — Cause 2 would otherwise have to rebuild
        # them). Non-core fills whatever capacity remains.
        remaining = max(0, cap - len(core))
        filtered = core + non_core[:remaining]
        # Preserve original ordering for stability/readability.
        kept_ids = {_rid(r) for r in filtered}
        filtered = [r for r in requirements if _rid(r) in kept_ids]

        if len(filtered) < original_count:
            filtered_ids = [_rid(r) for r in filtered]
            dropped_ids = [i for i in original_ids if i not in filtered_ids]
            logger.warning(
                f"Filtered {original_count} -> {len(filtered)} "
                f"(size={project_size}, cap={cap}, "
                f"core_protected={len(core)}). "
                f"Kept: {filtered_ids}, Dropped: {dropped_ids}"
            )
        if len(core) > cap:
            logger.warning(
                f"#683 Cause 1: {len(core)} core feature(s) exceed the "
                f"{project_size} cap of {cap}; keeping all core features "
                f"(scope floor) — consider a larger project_size."
            )

        return filtered

    def _detect_prompt_specificity(self, prd_content: str) -> str:
        """
        Detect if user provided explicit requirements or open-ended description.

        Returns
        -------
        str
            "explicit" - User listed specific requirements/features
            "guided" - Open-ended description, AI should generate features

        Examples
        --------
        Explicit:
            - "Create these tools: foo, bar, baz"
            - "Features: 1. X, 2. Y, 3. Z"
            - Contains bullet/numbered lists of features

        Guided:
            - "Build a Twitter clone"
            - "Create a task management system"
        """
        content_lower = prd_content.lower()

        # Strong explicit indicators
        explicit_patterns = [
            "create these",
            "create the following",
            "implement these",
            "build these",
            "these tools:",
            "these features:",
            "these functions:",
            "these mcp tools:",
            "tools:",
            "features:",
            "functions:",
            "requirements:",
        ]

        # Check for explicit patterns
        has_explicit_pattern = any(
            pattern in content_lower for pattern in explicit_patterns
        )

        # Check for list formatting (bullets or numbers)
        lines = prd_content.split("\n")
        list_lines = sum(
            1
            for line in lines
            if line.strip().startswith(("-", "*", "•"))
            or (len(line.strip()) > 0 and line.strip()[0].isdigit() and "." in line[:5])
        )

        # If 3+ list items, likely explicit
        has_list_structure = list_lines >= 3

        if has_explicit_pattern or has_list_structure:
            return "explicit"
        else:
            return "guided"

    def _filter_nfrs_by_size(
        self, nfrs: List[Dict[str, Any]], project_size: str
    ) -> List[Dict[str, Any]]:
        """Filter non-functional requirements based on project size."""
        if project_size in ["prototype", "mvp", "small"]:
            # Prototype: Skip NFRs entirely or just basic auth
            essential_nfrs = []
            for nfr in nfrs:
                nfr_type = nfr.get("type", "").lower()
                if "auth" in nfr_type:
                    essential_nfrs.append(nfr)
            return essential_nfrs[:1]  # Maximum 1 NFR for prototypes
        elif project_size in ["standard", "medium"]:
            # Standard: Keep 2-3 most important NFRs (security, performance)
            return nfrs[:2]
        else:
            # Enterprise: include all NFRs
            return nfrs
