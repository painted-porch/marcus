"""
AI-Powered Task Assignment for Marcus.

This module upgrades the basic task assignment logic to use
Phase 1-4 AI capabilities for intelligent task selection.
"""

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

from src.ai.core.ai_engine import MarcusAIEngine
from src.ai.types import AssignmentContext
from src.core.models import Priority, Task, TaskStatus
from src.integrations.ai_analysis_engine import AIAnalysisEngine
from src.intelligence.dependency_inferer import DependencyGraph
from src.intelligence.dependency_inferer_hybrid import HybridDependencyInferer

logger = logging.getLogger(__name__)


class AITaskAssignmentEngine:
    """
    Intelligent task assignment using Phase 1-4 capabilities.

    Features:
    - Phase 1: Safety checks (no deploy before implement)
    - Phase 2: Dependency analysis (prioritize unblocking tasks)
    - Phase 3: AI-powered agent matching
    - Phase 4: Predictive impact analysis
    """

    def __init__(
        self,
        ai_engine: MarcusAIEngine,
        project_tasks: List[Task],
        ai_analysis_engine: Optional[AIAnalysisEngine] = None,
    ):
        self.ai_engine = ai_engine
        self.project_tasks = project_tasks
        # Use hybrid dependency inferer for better accuracy with fewer API calls
        self.dependency_inferer = HybridDependencyInferer(ai_analysis_engine)

    async def find_optimal_task_for_agent(
        self,
        agent_id: str,
        agent_info: Dict[str, Any],
        available_tasks: List[Task],
        assigned_task_ids: Set[str],
    ) -> Optional[Task]:
        """
        Find the best task for an agent using AI capabilities.

        This replaces the basic skill/priority matching with intelligent analysis.
        """
        if not available_tasks:
            return None

        logger.info(
            f"Finding optimal task for agent {agent_id} "
            f"from {len(available_tasks)} tasks"
        )

        # Step 1: Safety filtering (Phase 1)
        safe_tasks = await self._filter_safe_tasks(available_tasks)
        logger.info(f"After safety filtering: {len(safe_tasks)} tasks remain")

        if not safe_tasks:
            return None

        # Step 2: Dependency analysis (Phase 2)
        dependency_scores = await self._analyze_dependencies(safe_tasks)

        # Step 3: AI-powered analysis (Phase 3)
        ai_scores = await self._get_ai_recommendations(safe_tasks, agent_info)

        # Step 4: Predictive impact (Phase 4)
        impact_scores = await self._predict_task_impact(safe_tasks)

        # Step 5: Combine scores intelligently
        best_task = await self._select_best_task(
            safe_tasks, dependency_scores, ai_scores, impact_scores, agent_info
        )

        if best_task:
            logger.info(f"Selected task '{best_task.name}' for agent {agent_id}")
        else:
            logger.info(f"No suitable task found for agent {agent_id}")
        return best_task

    async def _filter_safe_tasks(self, tasks: List[Task]) -> List[Task]:
        """Phase 1: Filter out unsafe tasks (e.g., deployment before implementation)."""
        safe_tasks = []

        for task in tasks:
            # Check if this is a deployment/release task
            if self._is_deployment_task(task):
                # Check if dependencies are complete
                if not await self._are_dependencies_complete(task):
                    logger.warning(
                        f"Filtering out unsafe task: {task.name} - "
                        f"dependencies incomplete"
                    )
                    continue

                # Additional safety check with AI (if method exists)
                if hasattr(self.ai_engine, "check_deployment_safety"):
                    safety_check = await self.ai_engine.check_deployment_safety(
                        task, self.project_tasks
                    )
                    if not safety_check.get("safe", False):
                        logger.warning(
                            f"AI safety check failed for: {task.name} - "
                            f"{safety_check.get('reason')}"
                        )
                        continue

            safe_tasks.append(task)

        return safe_tasks

    def _build_explicit_graph(self) -> DependencyGraph:
        """Build the dependency graph from declared task dependencies.

        Inference exists to fill in MISSING dependencies; on a board
        whose tasks declare their edges it is pure overhead, and its
        O(n^2) pattern sweep on the request path pins the event loop
        for minutes per request at a few hundred tasks (the #737 full
        run hit client timeouts at 620 tasks; Simon 44db8b8b).

        Returns
        -------
        DependencyGraph
            The graph of declared edges, in the same shape the
            inferer produces, sufficient for critical-path scoring.
        """
        nodes = {t.id: t for t in self.project_tasks}
        adjacency: Dict[str, List[str]] = {}
        reverse: Dict[str, List[str]] = {}
        for task in self.project_tasks:
            for dep_id in task.dependencies or []:
                if dep_id in nodes:
                    adjacency.setdefault(dep_id, []).append(task.id)
                    reverse.setdefault(task.id, []).append(dep_id)
        graph = DependencyGraph(
            nodes=nodes,
            edges=[],
            adjacency_list=adjacency,
            reverse_adjacency=reverse,
        )
        # Declared edges are user input and may contain a cycle, which
        # would spin get_critical_path's reconstruction forever; a
        # cyclic board forfeits the critical-path bonus, nothing more.
        if graph.has_cycle():
            return DependencyGraph(
                nodes=nodes, edges=[], adjacency_list={}, reverse_adjacency={}
            )
        return graph

    async def _analyze_dependencies(self, tasks: List[Task]) -> Dict[str, float]:
        """Phase 2: Analyze task dependencies and prioritize unblocking tasks."""
        dependency_scores = {}

        # Build dependency graph: from declared edges when the board
        # is explicitly wired; inference only fills in missing ones.
        if any(t.dependencies for t in self.project_tasks):
            dependency_graph = self._build_explicit_graph()
        else:
            dependency_graph = await self.dependency_inferer.infer_dependencies(
                self.project_tasks
            )

        # The critical path is a property of the graph, not of any one
        # task, so compute it once (not once per task) and look up
        # membership in O(1). Likewise, count how many TODO tasks each
        # task unblocks in a single pass over the board rather than a
        # nested scan per task. Both were O(n^2) in task count on the
        # request path and froze large boards (issue #743).
        critical_path = set(dependency_graph.get_critical_path())
        unblock_counts: Dict[str, int] = defaultdict(int)
        for other_task in self.project_tasks:
            if other_task.status == TaskStatus.TODO:
                # Dedupe per dependent: ``dependencies`` is a plain list that
                # some generation paths may fill with duplicate ids, and the
                # original membership test counted each dependent task once.
                for dep_id in set(other_task.dependencies):
                    unblock_counts[dep_id] += 1

        for task in tasks:
            unblocked_count = unblock_counts.get(task.id, 0)
            is_critical = task.id in critical_path

            # Calculate dependency score
            score = unblocked_count * 0.5
            if is_critical:
                score += 0.5

            dependency_scores[task.id] = score
            logger.debug(
                f"Task {task.name}: unblocks {unblocked_count}, "
                f"critical: {is_critical}, score: {score}"
            )

        return dependency_scores

    async def _get_ai_recommendations(
        self, tasks: List[Task], agent_info: Dict[str, Any]
    ) -> Dict[str, float]:
        """Phase 3: Get AI-powered recommendations for agent-task matching."""
        ai_scores = {}

        # Get AI recommendations for each task
        for task in tasks:
            # Prepare context for AI analysis
            context = AssignmentContext(
                task=task,
                agent_id=agent_info["worker_id"],
                agent_status=agent_info,
                available_tasks=tasks,
                project_context={
                    "total_tasks": len(self.project_tasks),
                    "completed_tasks": len(
                        [t for t in self.project_tasks if t.status == TaskStatus.DONE]
                    ),
                    "project_phase": self._detect_project_phase(),
                },
                team_status={},  # Could include other agents' status
            )

            # Use hybrid AI decision framework (if method exists)
            if hasattr(self.ai_engine, "analyze_task_assignment"):
                ai_analysis = await self.ai_engine.analyze_task_assignment(context)
            else:
                # Fallback to default scoring
                ai_analysis = {"suitability_score": 0.5, "confidence": 1.0}

            # Extract score from AI analysis
            score = ai_analysis.get("suitability_score", 0.5)
            score *= ai_analysis.get("confidence", 1.0)

            ai_scores[task.id] = score
            logger.debug(
                f"AI score for {task.name}: {score} "
                f"(confidence: {ai_analysis.get('confidence')})"
            )

        return ai_scores

    async def _predict_task_impact(self, tasks: List[Task]) -> Dict[str, float]:
        """Phase 4: Predict the impact of completing each task."""
        impact_scores = {}

        for task in tasks:
            # Predict how completing this task affects project timeline
            # (if method exists)
            if hasattr(self.ai_engine, "predict_task_impact"):
                impact_analysis = await self.ai_engine.predict_task_impact(
                    task,
                    self.project_tasks,
                    {
                        "current_velocity": self._calculate_velocity(),
                        "team_size": 3,  # Could be dynamic
                    },
                )
            else:
                # Fallback to default impact scoring
                impact_analysis = {"impact_score": 0.5, "confidence": 1.0}

            # Score based on timeline reduction and risk mitigation
            timeline_impact = (
                impact_analysis.get("timeline_reduction_days", 0) / 10
            )  # Normalize
            risk_reduction = impact_analysis.get("risk_reduction", 0)

            score = (timeline_impact * 0.6) + (risk_reduction * 0.4)
            impact_scores[task.id] = min(score, 1.0)  # Cap at 1.0

            logger.debug(
                f"Impact score for {task.name}: {score} "
                f"(timeline: {timeline_impact}, risk: {risk_reduction})"
            )

        return impact_scores

    async def _select_best_task(
        self,
        tasks: List[Task],
        dependency_scores: Dict[str, float],
        ai_scores: Dict[str, float],
        impact_scores: Dict[str, float],
        agent_info: Dict[str, Any],
    ) -> Optional[Task]:
        """Combine all scores to select the best task."""
        best_task = None
        best_combined_score = -1.0

        # Weights for different factors
        weights = {
            "skill_match": 0.15,  # Basic skill matching (reduced)
            "priority": 0.15,  # Task priority (reduced)
            "dependencies": 0.25,  # Unblocking other tasks (important)
            "ai_recommendation": 0.30,  # AI suitability analysis (most important)
            "impact": 0.15,  # Project impact prediction
        }

        for task in tasks:
            # Basic skill match score
            skill_score = self._calculate_skill_match(
                task, agent_info.get("skills", [])
            )

            # Priority score
            priority_score = {
                Priority.URGENT: 1.0,
                Priority.HIGH: 0.8,
                Priority.MEDIUM: 0.5,
                Priority.LOW: 0.2,
            }.get(task.priority, 0.5)

            # Get scores from our analysis
            dep_score = dependency_scores.get(task.id, 0)
            ai_score = ai_scores.get(task.id, 0.5)
            impact_score = impact_scores.get(task.id, 0.5)

            # Calculate combined score
            combined_score = (
                skill_score * weights["skill_match"]
                + priority_score * weights["priority"]
                + dep_score * weights["dependencies"]
                + ai_score * weights["ai_recommendation"]
                + impact_score * weights["impact"]
            )

            logger.debug(
                f"Task {task.name} scores - "
                f"skill: {skill_score:.2f}, priority: {priority_score:.2f}, "
                f"deps: {dep_score:.2f}, ai: {ai_score:.2f}, "
                f"impact: {impact_score:.2f}, combined: {combined_score:.2f}"
            )

            if combined_score > best_combined_score:
                best_combined_score = combined_score
                best_task = task

        return best_task

    def _is_deployment_task(self, task: Task) -> bool:
        """Check if task is deployment-related."""
        keywords = ["deploy", "release", "production", "launch", "rollout"]
        task_lower = task.name.lower()
        return any(keyword in task_lower for keyword in keywords)

    async def _are_dependencies_complete(self, task: Task) -> bool:
        """Check if all task dependencies are complete."""
        if not task.dependencies:
            return True

        for dep_id in task.dependencies:
            dep_task = next((t for t in self.project_tasks if t.id == dep_id), None)
            if dep_task and dep_task.status != TaskStatus.DONE:
                return False

        return True

    def _detect_project_phase(self) -> str:
        """Detect current project phase based on task completion."""
        total_tasks = len(self.project_tasks)
        if total_tasks == 0:
            return "initialization"

        completed = len([t for t in self.project_tasks if t.status == TaskStatus.DONE])
        completion_ratio = completed / total_tasks

        if completion_ratio < 0.1:
            return "foundation"
        elif completion_ratio < 0.3:
            return "early_development"
        elif completion_ratio < 0.6:
            return "active_development"
        elif completion_ratio < 0.8:
            return "integration"
        elif completion_ratio < 0.95:
            return "testing"
        else:
            return "deployment"

    def _calculate_velocity(self) -> float:
        """Calculate team velocity (tasks per day)."""
        # Simplified - in reality would track completion times
        completed_tasks = [t for t in self.project_tasks if t.status == TaskStatus.DONE]
        if not completed_tasks:
            return 2.0  # Default estimate

        # Assume project started 30 days ago (would track actual dates)
        days_elapsed = 30
        return len(completed_tasks) / days_elapsed

    def _calculate_skill_match(self, task: Task, agent_skills: List[str]) -> float:
        """Calculate skill match between agent and task."""
        if not agent_skills or not task.labels:
            return 0.5  # Neutral score

        matching_skills = set(agent_skills) & set(task.labels)
        return len(matching_skills) / len(task.labels)


# Function to integrate into marcus_mcp_server.py
async def find_optimal_task_for_agent_ai_powered(
    agent_id: str,
    agent_status: Dict[str, Any],
    project_tasks: List[Task],
    available_tasks: List[Task],
    assigned_task_ids: Set[str],
    ai_engine: MarcusAIEngine,
) -> Optional[Task]:
    """
    AI-powered task assignment to replace the basic version.

    This should be called from request_next_task in marcus_mcp_server.py
    """
    # Initialize AI assignment engine
    assignment_engine = AITaskAssignmentEngine(ai_engine, project_tasks)

    # Find optimal task
    optimal_task = await assignment_engine.find_optimal_task_for_agent(
        agent_id=agent_id,
        agent_info=agent_status,
        available_tasks=available_tasks,
        assigned_task_ids=assigned_task_ids,
    )

    return optimal_task
