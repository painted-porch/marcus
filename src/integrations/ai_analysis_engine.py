"""
AI-powered analysis and decision engine for Marcus.

This module provides intelligent task assignment, blocker resolution, and project
risk analysis using Claude API. It includes comprehensive fallback mechanisms
for when the AI service is unavailable.

The engine helps with:
- Optimal task-to-agent matching based on skills and capacity
- Generating detailed task instructions
- Analyzing and resolving blockers
- Identifying project risks and mitigation strategies

Examples
--------
>>> engine = AIAnalysisEngine()
>>> await engine.initialize()
>>> task = await engine.match_task_to_agent(tasks, agent, project_state)
>>> instructions = await engine.generate_task_instructions(task, agent)
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import anthropic

from src.core.models import (
    BlockerReport,
    Priority,
    ProjectRisk,
    ProjectState,
    RiskLevel,
    Task,
    WorkerStatus,
)
from src.cost_tracking.ai_usage_middleware import (
    ai_usage_middleware,
)


class AIAnalysisEngine:
    """
    AI-powered analysis and decision engine using Claude API.

    This class provides intelligent analysis for project management decisions,
    including task assignment optimization, blocker resolution, and risk analysis.
    It gracefully falls back to rule-based approaches when AI is unavailable.

    Attributes
    ----------
    client : Optional[anthropic.Anthropic]
        Anthropic API client, None if unavailable
    model : str
        Claude model to use for analysis
    prompts : Dict[str, str]
        Template prompts for different analysis types

    Examples
    --------
    >>> engine = AIAnalysisEngine()
    >>> await engine.initialize()
    >>> # Engine is ready for analysis tasks

    Notes
    -----
    Requires an Anthropic API key in ``config_marcus.json``
    (``ai.anthropic_api_key``) or via the ``CLAUDE_API_KEY`` env var.
    ``ANTHROPIC_API_KEY`` is intentionally NOT consulted: setting it in the
    shell switches Claude Code from subscription to API billing, so Marcus
    uses ``CLAUDE_API_KEY`` to leave Claude Code's auth untouched. Works in
    fallback mode without the API key.
    """

    def __init__(self) -> None:
        """
        Initialize the AI Analysis Engine.

        Attempts to set up the Anthropic client with various compatibility
        approaches for different library versions.
        """
        # Initialize Anthropic client with better error handling
        self.client: Optional[anthropic.Anthropic] = None
        self.current_project_id: Optional[str] = None
        self.current_agent_id: Optional[str] = None
        try:
            # Get API key from config first, fall back to environment
            from src.config.marcus_config import get_config

            config = get_config()
            api_key = config.ai.anthropic_api_key or os.environ.get("CLAUDE_API_KEY")
            if not api_key:
                print(
                    "⚠️  Anthropic API key not found - "
                    "AI features will use fallback mode",
                    file=sys.stderr,
                )
                self.client = None
            else:
                # Try different initialization approaches based on version
                try:
                    # First try simple initialization
                    self.client = anthropic.Anthropic(api_key=api_key)
                    # Don't print during initialization - it interferes with MCP stdio
                except TypeError as te:
                    # If we get a TypeError about proxies, just use basic initialization
                    if "proxies" in str(te):
                        # Retry without any extra parameters
                        self.client = anthropic.Anthropic(api_key=api_key)
                        # Successfully initialized
                    else:
                        raise te

        except Exception:
            # Failed to initialize Anthropic client
            # AI features will use fallback responses
            # Don't print to stderr as it interferes with MCP stdio protocol
            self.client = None
            config = None

        self.model: str = (
            getattr(getattr(config, "ai", None), "model", None)
            or "claude-3-5-sonnet-20241022"
        )  # Using Sonnet 3.5 for speed/cost balance

        # Analysis prompts
        self.prompts: Dict[str, str] = {
            "task_assignment": """You are an AI Project Manager \
analyzing task assignments.

Given:
- Available tasks: {tasks}
- Agent profile: {agent}
- Project state: {project_state}

Analyze and recommend the SINGLE BEST task for this agent considering:
1. Agent's skills match task requirements
2. Agent's current capacity and workload
3. Task priority and dependencies
4. Project timeline and critical path

Return JSON:
{{
    "recommended_task_id": "id",
    "confidence_score": 0.0-1.0,
    "reasoning": "explanation"
}}""",
            "task_instructions": """You are generating detailed task \
instructions for a developer.

Task: {task}
Assigned to: {agent}

The agent is a skilled developer. Give them the WHAT, not the HOW.

IMPORTANT: Look at the task data to determine the task type \
(check the 'type' field). Generate instructions appropriate for the task type:

For DESIGN tasks:
- Focus on planning, architecture, and specifications
- Include creating diagrams, API specs, data models
- NO implementation code yet
- Deliverables: design documents, wireframes, API contracts

For IMPLEMENTATION tasks:
- Focus on actual coding and building
- **Critical: Use Test-Driven Development (TDD)**
  - Write tests FIRST before any implementation
  - Follow Red-Green-Refactor cycle
- Reference the design specifications
- Deliverables: working code with tests

For TESTING tasks:
- Focus on test scenarios and coverage
- Include unit, integration, and edge cases
- Deliverables: test suites with good coverage

For DOCUMENTATION tasks:
- Read the actual source file for every component you document
- Verify every prop name, parameter, type, and default against the code
- Do NOT document intended or design-spec APIs — document what exists
- Deliverables: README or API docs that match the implementation exactly

Generate clear, actionable instructions that:
1. Define the task objective based on its type
2. List specific steps appropriate for the task type
3. Include acceptance criteria
4. Note any dependencies or prerequisites
5. Keep complexity appropriate to the task scope \
(simple tasks get simple instructions)

Keep instructions BRIEF (under 15 lines). Do NOT write:
- Step-by-step implementation plans
- Specific test case names or test code
- File names, class names, or code structure
- Numbered phases or sub-phases

Format as structured text the developer can follow.

The agent decides the approach. You define the goal and constraints.""",
            "blocker_analysis": """Analyze this blocker and suggest resolution:

Task ID: {task_id}
Blocker: {description}
Severity: {severity}
{agent_context}
{task_context}

Consider the agent's skills, experience level, and current workload \
when providing suggestions.
Tailor resolution steps to their capabilities and provide learning \
opportunities if appropriate.

Provide JSON response:
{{
    "root_cause": "analysis",
    "impact_assessment": "description",
    "resolution_steps": [
        "step1 tailored to agent skills",
        "step2"
    ],
    "required_resources": ["resource1"],
    "estimated_hours": number,
    "escalation_needed": boolean,
    "prevention_measures": ["measure1"],
    "learning_opportunities": ["skill gaps identified"],
    "recommended_collaborators": ["team members with needed skills"],
    "skill_match_confidence": "high/medium/low"
}}""",
            "project_risk": """Analyze project risks based on current state:

Project State: {project_state}
Recent Blockers: {recent_blockers}
Team Status: {team_status}

Identify risks and provide JSON:
{{
    "risks": [
        {{
            "description": "risk description",
            "likelihood": "low|medium|high",
            "impact": "low|medium|high",
            "mitigation": "suggested action"
        }}
    ],
    "overall_health": "healthy|at_risk|critical",
    "recommended_actions": ["action1", "action2"]
}}""",
        }

    async def initialize(self) -> None:
        """
        Initialize the AI engine and test connectivity.

        Verifies the Anthropic client can communicate with the API by
        sending a test message. Disables the client if the test fails.

        Examples
        --------
        >>> engine = AIAnalysisEngine()
        >>> await engine.initialize()
        ✅ AI Engine connection verified
        """
        if not self.client:
            print(
                "⚠️  AI Engine running in fallback mode (no Anthropic client)",
                file=sys.stderr,
            )
            return

        # Test connection
        try:
            self.client.messages.create(
                model=self.model,
                max_tokens=10,
                messages=[{"role": "user", "content": "test"}],
            )
            # Connection verified - wrap client for token tracking
            self.client = ai_usage_middleware.wrap_ai_provider(self.client)
        except Exception:
            # AI Engine test failed - will use fallback responses
            # Don't print to stderr as it interferes with MCP stdio protocol
            self.client = None  # Disable client if test fails

    async def match_task_to_agent(
        self,
        available_tasks: List[Task],
        agent: WorkerStatus,
        project_state: ProjectState,
    ) -> Optional[Task]:
        """
        Find the optimal task for an agent using AI analysis.

        Parameters
        ----------
        available_tasks : List[Task]
            List of unassigned tasks to choose from
        agent : WorkerStatus
            Agent profile including skills and capacity
        project_state : ProjectState
            Current state of the project

        Returns
        -------
        Optional[Task]
            The best matching task, or None if no suitable task found

        Examples
        --------
        >>> task = await engine.match_task_to_agent(
        ...     tasks, agent, ProjectState.HEALTHY
        ... )
        >>> print(f"Assigned: {task.name} to {agent.name}")

        Notes
        -----
        Falls back to skill-based matching if AI is unavailable.
        Considers up to 10 tasks to avoid context limits.
        """
        # Set project context for token tracking
        self.current_project_id = project_state.project_name or project_state.board_id
        self.current_agent_id = agent.worker_id
        if not available_tasks:
            return None

        if not self.client:
            # Fallback: Simple skill-based matching
            return self._fallback_task_matching(available_tasks, agent)

        # Prepare data for AI analysis
        tasks_data = [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "priority": t.priority.value,
                "estimated_hours": t.estimated_hours,
                "labels": t.labels,
                "dependencies": t.dependencies,
            }
            for t in available_tasks[:10]  # Limit to 10 tasks for context
        ]

        agent_data = {
            "id": agent.worker_id,
            "name": agent.name,
            "role": agent.role,
            "skills": agent.skills,
            "current_capacity": agent.capacity,
            "completed_tasks": agent.completed_tasks_count,
        }

        # Serialize ProjectState dataclass to JSON
        project_state_data = {
            "board_id": project_state.board_id,
            "project_name": project_state.project_name,
            "total_tasks": project_state.total_tasks,
            "completed_tasks": project_state.completed_tasks,
            "in_progress_tasks": project_state.in_progress_tasks,
            "blocked_tasks": project_state.blocked_tasks,
            "progress_percent": project_state.progress_percent,
            "team_velocity": project_state.team_velocity,
            "risk_level": (
                project_state.risk_level.value
                if hasattr(project_state.risk_level, "value")
                else str(project_state.risk_level)
            ),
            "last_updated": (
                project_state.last_updated.isoformat()
                if hasattr(project_state.last_updated, "isoformat")
                else str(project_state.last_updated)
            ),
        }

        prompt = self.prompts["task_assignment"].format(
            tasks=json.dumps(tasks_data, indent=2),
            agent=json.dumps(agent_data, indent=2),
            project_state=json.dumps(project_state_data, indent=2),
        )

        try:
            response = await self._call_claude(prompt)
            result = json.loads(response)

            # Find the recommended task
            task_id = result.get("recommended_task_id")
            for task in available_tasks:
                if task.id == task_id:
                    return task

        except Exception as e:
            print(f"AI task matching failed: {e}", file=sys.stderr)

        # Fallback to simple matching
        return self._fallback_task_matching(available_tasks, agent)

    def _fallback_task_matching(
        self, tasks: List[Task], agent: WorkerStatus
    ) -> Optional[Task]:
        """
        Match tasks to agent using skill-based scoring without AI.

        Parameters
        ----------
        tasks : List[Task]
            Available tasks to match
        agent : WorkerStatus
            Agent to match tasks for

        Returns
        -------
        Optional[Task]
            Best matching task based on priority and skills

        Notes
        -----
        Scores tasks based on:
        - Priority (urgent=4, high=3, medium=2, low=1)
        - Skill matches (2 points per matching skill)
        """
        # Score tasks based on priority and skill match
        best_score = -1
        best_task = None

        priority_scores = {
            Priority.URGENT: 10,  # Heavily prioritize urgent tasks in fallback mode
            Priority.HIGH: 3,
            Priority.MEDIUM: 2,
            Priority.LOW: 1,
        }

        for task in tasks:
            score = priority_scores.get(task.priority, 1)

            # Check skill match
            if agent.skills and task.labels:
                skill_match = len(set(agent.skills) & set(task.labels))
                score += skill_match * 2

            if score > best_score:
                best_score = score
                best_task = task

        return best_task

    async def generate_task_instructions(
        self, task: Task, agent: Optional[WorkerStatus] = None
    ) -> str:
        """
        Generate detailed instructions for a task.

        Parameters
        ----------
        task : Task
            The task to generate instructions for
        agent : Optional[WorkerStatus], default=None
            The agent assigned to the task

        Returns
        -------
        str
            Detailed task instructions formatted as markdown

        Examples
        --------
        >>> instructions = await engine.generate_task_instructions(task, agent)
        >>> print(instructions)
        ## Task Assignment for Alice
        ...

        Notes
        -----
        Uses AI to generate context-aware instructions when available,
        otherwise provides structured fallback instructions.
        """
        if not self.client:
            # Fallback instructions when AI is not available
            return self._generate_fallback_instructions(task, agent)

        # Determine task type from parent metadata, name, or labels
        # CRITICAL: Check parent task type first for subtasks (GH-XXX)
        if hasattr(task, "_parent_task_type"):
            task_type = task._parent_task_type
        else:
            # Fall back to inferring from name or labels
            task_type = "implementation"  # default
            task_labels = getattr(task, "labels", []) or []

            if "design" in task.name.lower() or "type:design" in task_labels:
                task_type = "design"
            elif "test" in task.name.lower() or "type:testing" in task_labels:
                task_type = "testing"
            elif (
                any(kw in task.name.lower() for kw in ("readme", "document", "docs"))
                or "type:documentation" in task_labels
            ):
                task_type = "documentation"

        task_data: Dict[str, Any] = {
            "name": task.name,
            "description": task.description,
            "priority": task.priority.value,
            "estimated_hours": task.estimated_hours,
            "dependencies": task.dependencies,
            "labels": getattr(task, "labels", []) or [],
            "type": task_type,
        }

        # Contract-first ownership (GH-320 PR 2). When the task is a
        # contract-first task, surface the responsibility and contract
        # file into the LLM prompt so the generated instructions
        # emphasize contract ownership and read-the-contract-first
        # behavior.
        #
        # Uses ``_parse_contract_metadata`` so the detection works
        # regardless of whether the task was freshly constructed from
        # the decomposer (``Task.responsibility`` set) or reloaded from
        # a kanban provider that stripped the field during persistence
        # (falls back to source_context and then to the
        # MARCUS_CONTRACT_FIRST description marker). Codex P1 from
        # PR #327 review.
        from src.marcus_mcp.tools.task import _parse_contract_metadata

        contract_meta = _parse_contract_metadata(task)
        if contract_meta["responsibility"]:
            task_data["responsibility"] = contract_meta["responsibility"]
            if contract_meta["contract_file"]:
                task_data["contract_file"] = contract_meta["contract_file"]
            task_data["contract_first"] = True

        agent_data = {
            "name": agent.name if agent else "Unknown",
            "role": agent.role if agent else "Developer",
            "skills": agent.skills if agent else [],
        }

        prompt = self.prompts["task_instructions"].format(
            task=json.dumps(task_data, indent=2), agent=json.dumps(agent_data, indent=2)
        )

        try:
            instructions = await self._call_claude(prompt)
            return instructions
        except Exception as e:
            print(f"AI instruction generation failed: {e}", file=sys.stderr)
            return self._generate_fallback_instructions(task, agent)

    def _generate_fallback_instructions(
        self, task: Task, agent: Optional[WorkerStatus]
    ) -> str:
        """
        Generate fallback instructions when AI is not available.

        Parameters
        ----------
        task : Task
            Task to generate instructions for
        agent : Optional[WorkerStatus]
            Agent assigned to the task

        Returns
        -------
        str
            Structured task instructions in markdown format
        """
        agent_name = agent.name if agent else "Team Member"

        # Determine task type (check parent metadata first for subtasks)
        if hasattr(task, "_parent_task_type"):
            task_type = task._parent_task_type
        else:
            task_type = "implementation"
            task_labels = getattr(task, "labels", []) or []

            if "design" in task.name.lower() or "type:design" in task_labels:
                task_type = "design"
            elif "test" in task.name.lower() or "type:testing" in task_labels:
                task_type = "testing"

        # Generate type-specific instructions
        if task_type == "design":
            implementation_steps = """2. **Design Steps**
   - Research existing patterns and best practices
   - Create architecture diagrams
   - Define API endpoints and data models
   - Document component interfaces
   - Create wireframes or mockups if needed"""

            definition_of_done = """3. **Definition of Done**
   - Design documentation is complete
   - API specifications are defined
   - Data models are documented
   - Technical approach is clear"""

        elif task_type == "testing":
            implementation_steps = """2. **Testing Steps**
   - Review the implementation to understand functionality
   - Write unit tests for individual components
   - Create integration tests for workflows
   - Add edge case and error handling tests
   - Ensure minimum 80% code coverage"""

            definition_of_done = """3. **Definition of Done**
   - All tests pass successfully
   - Code coverage meets requirements
   - Edge cases are tested
   - Test documentation is complete"""

        else:  # implementation
            implementation_steps = """2. **Implementation Steps**
   - Review the design specifications
   - Break down the task into smaller subtasks
   - Start with the core functionality
   - Follow project coding standards
   - **MANDATORY**: Use Test-Driven Development (TDD)**
   - Write failing tests FIRST (Red)
   - Implement minimal code to pass tests (Green)
   - Refactor while keeping tests green (Refactor)
   """

            definition_of_done = """3. **Definition of Done**
   - All requirements from description are met
   - Code is tested and reviewed
   - Documentation is updated
   - Code follows project standards"""

        return f"""## Task Assignment for {agent_name}

**Task:** {task.name}

**Description:** {task.description}

**Priority:** {task.priority.value}
**Estimated Hours:** {task.estimated_hours}
**Type:** {task_type.capitalize()}

### Instructions:

1. **Review Requirements**
   - Read the task description carefully
   - Check any linked documentation
   - Identify dependencies: \
{', '.join(task.dependencies) if task.dependencies else 'None'}

{implementation_steps}

{definition_of_done}

4. **Communication**
   - Report progress regularly
   - Ask for clarification if needed
   - Report any blockers immediately

**Labels:** {', '.join(task.labels) if task.labels else 'None'}

Good luck with your task!"""

    async def analyze_blocker(
        self,
        task_id: str,
        description: str,
        severity: str,
        agent: Optional["WorkerStatus"] = None,
        task: Optional["Task"] = None,
    ) -> Dict[str, Any]:
        """
        Analyze a blocker and suggest resolution steps.

        Parameters
        ----------
        task_id : str
            ID of the blocked task
        description : str
            Detailed description of the blocker
        severity : str
            Severity level (low, medium, high, urgent)
        agent : Optional[WorkerStatus]
            Agent who reported the blocker (for context-aware suggestions)
        task : Optional[Task]
            Full task details (for enhanced analysis)

        Returns
        -------
        Dict[str, Any]
            Analysis results including:
            - root_cause: Identified cause
            - impact_assessment: Impact description
            - resolution_steps: List of steps tailored to agent skills
            - required_resources: Needed resources
            - estimated_hours: Time to resolve
            - escalation_needed: Boolean
            - prevention_measures: Future prevention steps
            - learning_opportunities: Skills/knowledge gaps identified
            - recommended_collaborators: Team members who could help

        Examples
        --------
        >>> analysis = await engine.analyze_blocker(
        ...     "TASK-123",
        ...     "Database connection timeout",
        ...     "high",
        ...     agent=worker_status,
        ...     task=task_obj
        ... )
        >>> print(analysis['resolution_steps'])
        ['Check database server status', ...]
        """
        if not self.client:
            return self._generate_fallback_blocker_analysis(
                description, severity, agent, task
            )

        # Prepare agent context
        agent_context = ""
        if agent:
            agent_context = f"""
Agent Profile:
- ID: {agent.worker_id}
- Name: {agent.name}
- Role: {agent.role}
- Skills: {', '.join(agent.skills) if agent.skills else 'None specified'}
- Current Tasks: {len(agent.current_tasks)} active tasks
- Completed Tasks: {agent.completed_tasks_count}
- Performance Score: {agent.performance_score}
"""

        # Prepare task context
        task_context = ""
        if task:
            task_context = f"""
Task Details:
- Name: {task.name}
- Description: {task.description}
- Priority: {task.priority.value if task.priority else 'Not set'}
- Estimated Hours: {task.estimated_hours}
- Dependencies: {', '.join(task.dependencies) if task.dependencies else 'None'}
- Labels: {', '.join(task.labels) if task.labels else 'None'}
"""

        prompt = self.prompts["blocker_analysis"].format(
            task_id=task_id,
            description=description,
            severity=severity,
            agent_context=agent_context,
            task_context=task_context,
        )

        # Issue #676: differentiate the two failure modes so the log is
        # attributable instead of a bare "AI blocker analysis failed:
        # Expecting value". A transport/LLM error and a malformed (non-JSON)
        # response are distinct problems; both fall back, but the cause
        # should be visible. Behavior is unchanged (always falls back).
        try:
            response = await self._call_claude(prompt)
        except Exception as e:
            print(f"AI blocker analysis: LLM call failed: {e}", file=sys.stderr)
            return self._generate_fallback_blocker_analysis(
                description, severity, agent, task
            )
        try:
            result: Dict[str, Any] = json.loads(response)
            return result
        except (json.JSONDecodeError, TypeError) as e:
            print(
                f"AI blocker analysis: non-JSON LLM response ({e}); "
                f"first 200 chars: {(response or '')[:200]!r}",
                file=sys.stderr,
            )
            return self._generate_fallback_blocker_analysis(
                description, severity, agent, task
            )

    def _generate_fallback_blocker_analysis(
        self,
        description: str,
        severity: str,
        agent: Optional["WorkerStatus"] = None,
        task: Optional["Task"] = None,
    ) -> Dict[str, Any]:
        """
        Generate fallback blocker analysis without AI.

        Parameters
        ----------
        description : str
            Blocker description
        severity : str
            Severity level
        agent : Optional[WorkerStatus]
            Agent who reported the blocker
        task : Optional[Task]
            Task details

        Returns
        -------
        Dict[str, Any]
            Basic analysis with agent-aware resolution steps
        """
        # Basic analysis
        base_steps = [
            "Review the blocker description",
            "Identify required resources",
            "Research potential solutions",
            "Document resolution steps",
        ]

        # Add agent-specific guidance if available
        if agent and agent.skills:
            skill_based_steps = []
            if "python" in [s.lower() for s in agent.skills]:
                skill_based_steps.append(
                    "Check Python documentation and Stack Overflow"
                )
            if "database" in [s.lower() for s in agent.skills]:
                skill_based_steps.append("Review database logs and connection settings")
            if "frontend" in [s.lower() for s in agent.skills]:
                skill_based_steps.append("Check browser console and network requests")

            if skill_based_steps:
                base_steps = skill_based_steps + base_steps

        # Adjust resources based on agent experience
        resources = ["Team lead", "Subject matter expert"]
        if agent and agent.performance_score < 0.7:
            resources.insert(0, "Senior developer (mentoring)")

        return {
            "root_cause": "Analysis needed",
            "impact_assessment": f"Blocker reported: {description}",
            "resolution_steps": base_steps,
            "required_resources": resources,
            "estimated_hours": 2 if severity == "low" else 4,
            "escalation_needed": severity in ["high", "urgent"],
            "prevention_measures": [
                "Improve documentation",
                "Add monitoring",
                "Review process",
            ],
            "learning_opportunities": ["Root cause analysis", "Problem solving"],
            "recommended_collaborators": ["Team lead", "Senior developer"],
            "skill_match_confidence": "medium",
        }

    async def generate_clarification(
        self, task: Task, question: str, context: str = ""
    ) -> str:
        """
        Generate clarification for a task-related question.

        Parameters
        ----------
        task : Task
            The task in question
        question : str
            The question needing clarification
        context : str, optional
            Additional context for the question

        Returns
        -------
        str
            Clarification response

        Examples
        --------
        >>> clarification = await engine.generate_clarification(
        ...     task,
        ...     "What database should I use?",
        ...     "Working on user authentication"
        ... )
        """
        if not self.client:
            return (
                f"Please clarify: {question}\n\nTask: {task.name}\nContext: {context}"
            )

        prompt = f"""Help clarify this question about a task:

Task: {task.name}
Description: {task.description}
Question: {question}
Context: {context}

Provide a helpful clarification that guides the developer."""

        try:
            return await self._call_claude(prompt)
        except Exception as e:
            print(f"AI clarification failed: {e}", file=sys.stderr)
            return (
                f"Please clarify: {question}\n\nTask: {task.name}\nContext: {context}"
            )

    async def analyze_project_risks(
        self,
        project_state: ProjectState,
        recent_blockers: List[BlockerReport],
        team_status: List[WorkerStatus],
    ) -> List[ProjectRisk]:
        """
        Analyze and identify project risks.

        Parameters
        ----------
        project_state : ProjectState
            Current project state
        recent_blockers : List[BlockerReport]
            Recent blockers encountered
        team_status : List[WorkerStatus]
            Current team member status

        Returns
        -------
        List[ProjectRisk]
            Identified risks with mitigation strategies

        Examples
        --------
        >>> risks = await engine.analyze_project_risks(
        ...     ProjectState.AT_RISK,
        ...     recent_blockers,
        ...     team_status
        ... )
        >>> for risk in risks:
        ...     print(f"{risk.description}: {risk.mitigation}")

        Notes
        -----
        Analyzes up to 10 recent blockers to identify patterns.
        Falls back to basic risk assessment if AI unavailable.
        """
        if not self.client:
            return self._generate_fallback_risk_analysis(project_state)

        # Prepare data
        blockers_data = [
            {
                "task_id": b.task_id,
                "description": b.description,
                "severity": b.severity.value,
                "reported_at": b.reported_at.isoformat(),
            }
            for b in recent_blockers[-10:]  # Last 10 blockers
        ]

        team_data = [
            {
                "name": w.name,
                "role": w.role,
                "current_tasks": len(w.current_tasks),
                "capacity": w.capacity,
            }
            for w in team_status
        ]

        # Serialize ProjectState dataclass to JSON
        project_state_data = {
            "board_id": project_state.board_id,
            "project_name": project_state.project_name,
            "total_tasks": project_state.total_tasks,
            "completed_tasks": project_state.completed_tasks,
            "in_progress_tasks": project_state.in_progress_tasks,
            "blocked_tasks": project_state.blocked_tasks,
            "progress_percent": project_state.progress_percent,
            "team_velocity": project_state.team_velocity,
            "risk_level": (
                project_state.risk_level.value
                if hasattr(project_state.risk_level, "value")
                else str(project_state.risk_level)
            ),
            "last_updated": (
                project_state.last_updated.isoformat()
                if hasattr(project_state.last_updated, "isoformat")
                else str(project_state.last_updated)
            ),
        }

        prompt = self.prompts["project_risk"].format(
            project_state=json.dumps(project_state_data, indent=2),
            recent_blockers=json.dumps(blockers_data, indent=2),
            team_status=json.dumps(team_data, indent=2),
        )

        try:
            response = await self._call_claude(prompt)
            result = json.loads(response)

            # Convert to ProjectRisk objects
            risks = []
            for risk_data in result.get("risks", []):
                likelihood_map = {"low": 0.2, "medium": 0.5, "high": 0.8}

                impact_severity = {
                    "low": RiskLevel.LOW,
                    "medium": RiskLevel.MEDIUM,
                    "high": RiskLevel.HIGH,
                }

                risk = ProjectRisk(
                    risk_type="project",
                    description=risk_data["description"],
                    severity=impact_severity.get(risk_data["impact"], RiskLevel.MEDIUM),
                    probability=likelihood_map.get(risk_data["likelihood"], 0.5),
                    impact=risk_data.get("impact", "Unknown impact"),
                    mitigation_strategy=risk_data["mitigation"],
                    identified_at=datetime.now(timezone.utc),
                )
                risks.append(risk)

            return risks

        except Exception as e:
            print(f"AI risk analysis failed: {e}", file=sys.stderr)
            return self._generate_fallback_risk_analysis(project_state)

    def _generate_fallback_risk_analysis(
        self, project_state: ProjectState
    ) -> List[ProjectRisk]:
        """
        Generate fallback risk analysis without AI.

        Parameters
        ----------
        project_state : ProjectState
            Current project state

        Returns
        -------
        List[ProjectRisk]
            Basic risks based on project state
        """
        risks = []

        if project_state.risk_level == RiskLevel.HIGH:
            risks.append(
                ProjectRisk(
                    risk_type="timeline",
                    description="Project timeline at risk",
                    severity=RiskLevel.HIGH,
                    probability=0.7,
                    impact="Potential delays in delivery",
                    mitigation_strategy=(
                        "Review task priorities and resource allocation"
                    ),
                    identified_at=datetime.now(timezone.utc),
                )
            )

        return risks

    async def analyze_project_health(
        self,
        project_state: ProjectState,
        recent_activities: List[Dict[str, Any]],
        team_status: Any,
    ) -> Dict[str, Any]:
        """
        Analyze overall project health using AI.

        Parameters
        ----------
        project_state : ProjectState
            Current project state
        recent_activities : List[Dict[str, Any]]
            Recent project activities/events
        team_status : Any
            Current team status information (can be dict or list)

        Returns
        -------
        Dict[str, Any]
            Project health analysis with overall health, timeline prediction,
            risk factors, recommendations, and resource optimization
        """
        if not self.client:
            return self._generate_fallback_health_analysis(project_state, team_status)

        # Serialize ProjectState dataclass to JSON
        project_state_data = {
            "board_id": project_state.board_id,
            "project_name": project_state.project_name,
            "total_tasks": project_state.total_tasks,
            "completed_tasks": project_state.completed_tasks,
            "in_progress_tasks": project_state.in_progress_tasks,
            "blocked_tasks": project_state.blocked_tasks,
            "progress_percent": project_state.progress_percent,
            "team_velocity": project_state.team_velocity,
            "risk_level": project_state.risk_level.value,
            "last_updated": (
                project_state.last_updated.isoformat()
                if hasattr(project_state.last_updated, "isoformat")
                else str(project_state.last_updated)
            ),
            "overdue_tasks": len(project_state.overdue_tasks),
        }

        # Serialize team status if it's a list of WorkerStatus objects
        if isinstance(team_status, list) and len(team_status) > 0:
            # Assume it's a list of WorkerStatus objects
            team_status_data = [
                {
                    "worker_id": worker.worker_id,
                    "name": worker.name,
                    "role": worker.role,
                    "current_tasks_count": len(worker.current_tasks),
                    "completed_tasks_count": worker.completed_tasks_count,
                    "capacity": worker.capacity,
                    "performance_score": getattr(worker, "performance_score", 1.0),
                }
                for worker in team_status
            ]
        else:
            team_status_data = team_status

        # Create project health analysis prompt
        prompt = f"""Analyze the health of this software project \
and provide comprehensive insights.

Project State:
{json.dumps(project_state_data, indent=2)}

Recent Activities:
{json.dumps(recent_activities, indent=2)}

Team Status:
{json.dumps(team_status_data, indent=2)}

Provide a detailed analysis including:
1. Overall project health assessment (green/yellow/red)
2. Timeline prediction with confidence level
3. Key risk factors
4. Actionable recommendations
5. Resource optimization suggestions

Return JSON in this format:
{{
    "overall_health": "green|yellow|red",
    "timeline_prediction": {{
        "on_track": true|false,
        "estimated_completion": "date or description",
        "confidence": 0.0-1.0,
        "critical_path_risks": ["risk1", "risk2"]
    }},
    "risk_factors": [
        {{
            "type": "resource|timeline|technical|quality",
            "description": "detailed description",
            "severity": "low|medium|high",
            "mitigation": "suggested action"
        }}
    ],
    "recommendations": [
        {{
            "priority": "high|medium|low",
            "action": "specific action to take",
            "expected_impact": "what this will achieve"
        }}
    ],
    "resource_optimization": [
        {{
            "suggestion": "optimization suggestion",
            "impact": "expected improvement"
        }}
    ]
}}"""

        try:
            response = await self._call_claude(prompt)
            result: Dict[str, Any] = json.loads(response)
            return result

        except Exception as e:
            print(f"AI health analysis failed: {e}", file=sys.stderr)
            return self._generate_fallback_health_analysis(project_state, team_status)

    def _generate_fallback_health_analysis(
        self, project_state: ProjectState, team_status: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Generate fallback health analysis without AI.

        Parameters
        ----------
        project_state : ProjectState
            Current project state
        team_status : Any
            Team status information (can be dict or list)

        Returns
        -------
        Dict[str, Any]
            Basic health analysis based on metrics
        """
        # Determine overall health based on metrics
        overall_health = "green"
        if project_state.risk_level == RiskLevel.HIGH:
            overall_health = "red"
        elif (
            project_state.risk_level == RiskLevel.MEDIUM
            or project_state.blocked_tasks > 2
        ):
            overall_health = "yellow"

        # Calculate timeline prediction
        completion_rate = project_state.completed_tasks / max(
            project_state.total_tasks, 1
        )
        on_track = completion_rate >= (project_state.progress_percent / 100.0)

        risk_factors = []

        # Check for resource risks
        if project_state.blocked_tasks > 0:
            risk_factors.append(
                {
                    "type": "resource",
                    "description": (
                        f"{project_state.blocked_tasks} tasks are " "currently blocked"
                    ),
                    "severity": "high" if project_state.blocked_tasks > 2 else "medium",
                    "mitigation": "Review and resolve blockers urgently",
                }
            )

        # Check for timeline risks
        if len(project_state.overdue_tasks) > 0:
            risk_factors.append(
                {
                    "type": "timeline",
                    "description": (
                        f"{len(project_state.overdue_tasks)} tasks are " "overdue"
                    ),
                    "severity": "high",
                    "mitigation": "Reassign or reprioritize overdue tasks",
                }
            )

        # Generate recommendations
        recommendations = []
        if project_state.team_velocity < 3.0:
            recommendations.append(
                {
                    "priority": "high",
                    "action": "Review team capacity and task complexity",
                    "expected_impact": "Improve velocity by 20-30%",
                }
            )

        if project_state.blocked_tasks > 0:
            recommendations.append(
                {
                    "priority": "high",
                    "action": "Conduct blocker review session",
                    "expected_impact": "Unblock tasks and restore progress",
                }
            )

        return {
            "overall_health": overall_health,
            "timeline_prediction": {
                "on_track": on_track,
                "estimated_completion": "Based on current velocity",
                "confidence": 0.6 if on_track else 0.3,
                "critical_path_risks": (
                    ["Resource constraints"] if project_state.blocked_tasks > 0 else []
                ),
            },
            "risk_factors": risk_factors,
            "recommendations": recommendations,
            "resource_optimization": [
                {
                    "suggestion": "Balance workload across team members",
                    "impact": "Reduce bottlenecks and improve throughput",
                }
            ],
        }

    async def analyze_feature_request(self, feature_description: str) -> Dict[str, Any]:
        """
        Analyze a feature request and generate appropriate tasks.

        Parameters
        ----------
        feature_description : str
            Natural language description of the feature to implement

        Returns
        -------
        Dict[str, Any]
            Dictionary containing required tasks with details

        Examples
        --------
        >>> result = await engine.analyze_feature_request("Add user profile page")
        >>> tasks = result["required_tasks"]
        """
        # Fallback for when Claude is unavailable
        if not self.client:
            return self._analyze_feature_request_fallback(feature_description)

        try:
            prompt = f"""Analyze this feature request and generate a \
comprehensive task breakdown.

Feature Request: {feature_description}

Generate a detailed task list for implementing this feature. Consider:
1. Design/planning tasks
2. Backend implementation needs
3. Frontend/UI requirements
4. Database/data model changes
5. API endpoints required
6. Security considerations
7. Testing requirements
8. Documentation needs

Return JSON with this exact format:
{{
    "required_tasks": [
        {{
            "name": "specific task name",
            "description": "detailed description of what needs to be done",
            "estimated_hours": integer,
            "labels": ["appropriate", "labels"],
            "critical": true/false,
            "task_type": \
"design|backend|frontend|database|testing|documentation|security"
        }}
    ],
    "feature_complexity": "simple|moderate|complex",
    "technical_requirements": ["list", "of", "technical", "needs"],
    "dependencies_on_existing": [
        "authentication",
        "api",
        "database"
    ] // existing systems this feature depends on
}}

Be specific and actionable. Each task should be self-contained \
and assignable to a developer."""

            response = await self._call_claude(prompt)

            # Parse JSON response
            try:
                result: Dict[str, Any] = json.loads(response)
                return result
            except json.JSONDecodeError:
                # If JSON parsing fails, try to extract structured data
                print(
                    "Failed to parse AI response as JSON, using fallback",
                    file=sys.stderr,
                )
                return self._analyze_feature_request_fallback(feature_description)

        except Exception as e:
            print(f"AI feature analysis failed: {e}, using fallback", file=sys.stderr)
            return self._analyze_feature_request_fallback(feature_description)

    def _analyze_feature_request_fallback(
        self, feature_description: str
    ) -> Dict[str, Any]:
        """Fallback feature analysis when AI is unavailable."""
        feature_lower = feature_description.lower()
        tasks = []

        # Always start with design
        tasks.append(
            {
                "name": f"Design {feature_description}",
                "description": (
                    f"Create technical design and architecture "
                    f"for {feature_description}"
                ),
                "estimated_hours": 4,
                "labels": ["design", "planning"],
                "critical": False,
                "task_type": "design",
            }
        )

        # Analyze feature type and add appropriate tasks
        if any(word in feature_lower for word in ["api", "endpoint", "service"]):
            tasks.append(
                {
                    "name": f"Implement API for {feature_description}",
                    "description": "Build backend API endpoints and business logic",
                    "estimated_hours": 12,
                    "labels": ["backend", "api"],
                    "critical": True,
                    "task_type": "backend",
                }
            )

        if any(word in feature_lower for word in ["ui", "page", "screen", "interface"]):
            tasks.append(
                {
                    "name": f"Build UI for {feature_description}",
                    "description": "Create user interface components and interactions",
                    "estimated_hours": 10,
                    "labels": ["frontend", "ui"],
                    "critical": True,
                    "task_type": "frontend",
                }
            )

        # Always add testing and documentation
        tasks.extend(
            [
                {
                    "name": f"Test {feature_description}",
                    "description": "Write unit tests and perform integration testing",
                    "estimated_hours": 6,
                    "labels": ["testing", "qa"],
                    "critical": False,
                    "task_type": "testing",
                },
                {
                    "name": f"Document {feature_description}",
                    "description": "Create user and technical documentation",
                    "estimated_hours": 3,
                    "labels": ["documentation"],
                    "critical": False,
                    "task_type": "documentation",
                },
            ]
        )

        return {
            "required_tasks": tasks,
            "feature_complexity": "moderate",
            "technical_requirements": [],
            "dependencies_on_existing": [],
        }

    async def analyze_integration_points(
        self, feature_tasks: List[Task], existing_tasks: List[Task]
    ) -> Dict[str, Any]:
        """
        Analyze how new feature tasks should integrate with existing project tasks.

        Parameters
        ----------
        feature_tasks : List[Task]
            Tasks for the new feature
        existing_tasks : List[Task]
            Current project tasks

        Returns
        -------
        Dict[str, Any]
            Integration analysis including dependencies and phase

        Examples
        --------
        >>> integration = await engine.analyze_integration_points(
        ...     new_tasks, project_tasks
        ... )
        >>> phase = integration["suggested_phase"]
        """
        # Fallback for when Claude is unavailable
        if not self.client:
            return self._analyze_integration_fallback(feature_tasks, existing_tasks)

        try:
            # Prepare task summaries
            feature_summary = [
                {
                    "name": t.name,
                    "labels": t.labels,
                    "type": getattr(t, "task_type", "unknown"),
                }
                for t in feature_tasks
            ]

            existing_summary = [
                {
                    "id": t.id,
                    "name": t.name,
                    "labels": t.labels,
                    "status": t.status.value,
                    "description": (
                        t.description[:100] + "..."
                        if len(t.description) > 100
                        else t.description
                    ),
                }
                for t in existing_tasks
            ]

            prompt = f"""Analyze how these new feature tasks should \
integrate with the existing project.

New Feature Tasks:
{json.dumps(feature_summary, indent=2)}

Existing Project Tasks:
{json.dumps(existing_summary, indent=2)}

Determine:
1. Which existing tasks the new feature depends on (by task ID)
2. The appropriate project phase for these tasks
3. Integration complexity and risks
4. Recommended task ordering

Return JSON with this format:
{{
    "dependent_task_ids": ["list", "of", "existing", "task", "ids"],
    "suggested_phase": "initial|development|testing|deployment|maintenance",
    "integration_complexity": "low|medium|high",
    "confidence": 0.0-1.0,
    "integration_risks": ["list", "of", "potential", "issues"],
    "recommendations": [
        {{
            "type": "dependency|ordering|resource",
            "description": "specific recommendation"
        }}
    ],
    "rationale": "explanation of integration approach"
}}"""

            response = await self._call_claude(prompt)

            # Parse JSON response
            try:
                result: Dict[str, Any] = json.loads(response)
                return result
            except json.JSONDecodeError:
                print(
                    "Failed to parse AI response as JSON, using fallback",
                    file=sys.stderr,
                )
                return self._analyze_integration_fallback(feature_tasks, existing_tasks)

        except Exception as e:
            print(
                f"AI integration analysis failed: {e}, using fallback", file=sys.stderr
            )
            return self._analyze_integration_fallback(feature_tasks, existing_tasks)

    def _analyze_integration_fallback(
        self, feature_tasks: List[Task], existing_tasks: List[Task]
    ) -> Dict[str, Any]:
        """Fallback integration analysis when AI is unavailable."""
        # Analyze existing task states
        completed = [t for t in existing_tasks if t.status.value == "done"]
        in_progress = [t for t in existing_tasks if t.status.value == "in_progress"]

        # Find potential dependencies based on labels
        dependent_ids = []
        for existing in existing_tasks:
            for feature in feature_tasks:
                # Check for label overlap
                if existing.labels and feature.labels:
                    if set(existing.labels) & set(feature.labels):
                        dependent_ids.append(existing.id)
                        break

        # Determine phase
        if len(completed) == 0:
            phase = "initial"
        elif len(completed) / len(existing_tasks) > 0.8:
            phase = "maintenance"
        elif any("test" in t.name.lower() for t in in_progress):
            phase = "testing"
        else:
            phase = "development"

        return {
            "dependent_task_ids": list(set(dependent_ids)),
            "suggested_phase": phase,
            "integration_complexity": "medium",
            "confidence": 0.7,
            "integration_risks": [],
            "recommendations": [],
            "rationale": "Based on task label matching and project progress",
        }

    async def _call_claude(self, prompt: str) -> str:
        """
        Call Claude API with error handling and token tracking.

        Parameters
        ----------
        prompt : str
            The prompt to send to Claude

        Returns
        -------
        str
            Claude's response text

        Raises
        ------
        Exception
            If the API call fails or client is unavailable
        """
        if not self.client:
            raise Exception("Anthropic client not available")

        try:
            # Set context for token tracking if available
            if self.current_project_id and self.current_agent_id:
                ai_usage_middleware.set_project_context(
                    self.current_agent_id, self.current_project_id
                )

            response = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                temperature=0.7,
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract token usage if available
            if hasattr(response, "usage"):
                usage = response.usage
                if self.current_project_id:
                    # Manually track tokens since we're calling the API directly
                    import asyncio

                    from src.cost_tracking.token_tracker import token_tracker

                    asyncio.create_task(
                        token_tracker.track_tokens(
                            project_id=self.current_project_id,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            model=self.model,
                            metadata={
                                "agent_id": self.current_agent_id,
                                "function": "ai_analysis_engine",
                                "prompt_length": len(prompt),
                            },
                        )
                    )

            # Handle different response block types
            content = response.content[0]
            if hasattr(content, "text"):
                text = str(content.text).strip()
            else:
                text = str(content).strip()

            # Extract JSON from response (LLMs often wrap in fences or add prose)
            start = text.find("{") if "{" in text else text.find("[")
            if start != -1:
                end = text.rfind("}") if "{" in text else text.rfind("]")
                if end != -1:
                    text = text[start : end + 1]

            return text

        except Exception as e:
            print(f"Error calling Claude: {e}", file=sys.stderr)
            raise

    async def generate_structured_response(
        self,
        prompt: str,
        system_prompt: str = "",
        response_format: Optional[Dict[str, Any]] = None,
        *,
        operation: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate a structured JSON response from the AI based on a schema.

        This method is used for task decomposition and other operations requiring
        structured output. It only works when an AI provider is configured.

        Parameters
        ----------
        prompt : str
            The user prompt describing what to generate
        system_prompt : str, optional
            System instructions for the AI
        response_format : Dict[str, Any], optional
            JSON schema describing expected response structure

        Returns
        -------
        Dict[str, Any]
            Structured response matching the schema

        Raises
        ------
        RuntimeError
            If no AI client is available (no API key configured)

        Examples
        --------
        >>> result = await engine.generate_structured_response(
        ...     prompt="Break down this task",
        ...     system_prompt="You are a task decomposition expert",
        ...     response_format={"type": "object", "properties": {...}}
        ... )
        """
        # Use LLM abstraction layer to support all providers (Anthropic, OpenAI, local)
        try:
            from src.ai.providers.llm_abstraction import LLMAbstraction

            # Initialize LLM abstraction (supports local models via config)
            llm = LLMAbstraction()

            # Build full prompt with schema instructions and system prompt
            full_prompt = prompt
            if system_prompt:
                full_prompt = f"{system_prompt}\n\n{prompt}"

            if response_format:
                schema_str = json.dumps(response_format, indent=2)
                full_prompt = f"""{full_prompt}

IMPORTANT: Respond with valid JSON matching this exact schema:
{schema_str}

Your response must be pure JSON with no markdown formatting, \
explanations, or additional text."""

            # Delegate to the shared safe_structured_call helper, which
            # owns the default budget (16384, up from the Haiku-3-era
            # 4096) and the auto-retry-on-truncation policy. Single
            # source of truth across every structured-JSON caller in
            # Marcus — see src/utils/structured_llm.py.
            from src.utils.structured_llm import safe_structured_call

            return await safe_structured_call(
                llm=llm,
                prompt=full_prompt,
                operation=operation or "generate_structured_response",
            )

        except (json.JSONDecodeError, ValueError) as e:
            # The helper already logged a structured_llm.retry event for
            # any truncation-driven retries that were attempted before
            # this exception bubbled out — re-raise as RuntimeError so
            # callers keep their existing failure semantics.
            raise RuntimeError(
                f"AI returned invalid JSON: {e}. This may indicate the AI "
                "did not follow the schema correctly."
            )
        except Exception as e:
            error_msg = str(e)
            # Provide helpful error messages based on what went wrong
            if "No AI providers" in error_msg or "No LLM providers" in error_msg:
                raise RuntimeError(
                    "No AI provider configured. Task decomposition "
                    "requires an AI provider. "
                    "Please configure one of the following in "
                    "config_marcus.json:\n"
                    "- Local model (Ollama): Set ai.provider='local' "
                    "and ai.local_model\n"
                    "- Anthropic: Set ai.provider='anthropic' "
                    "and ai.anthropic_api_key\n"
                    "- OpenAI: Set ai.provider='openai' "
                    "and ai.openai_api_key"
                )
            print(f"Error generating structured response: {e}", file=sys.stderr)
            raise RuntimeError(f"Failed to generate structured response: {e}")
