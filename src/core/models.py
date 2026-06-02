"""
Core data models for Marcus.

This module defines the fundamental data structures used throughout the Marcus system,
including tasks, workers, assignments, and project state tracking.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone  # noqa: F401
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskStatus(Enum):
    """
    Enumeration of possible task states.

    Attributes
    ----------
    TODO : str
        Task is created but not started
    IN_PROGRESS : str
        Task is actively being worked on
    DONE : str
        Task is completed
    BLOCKED : str
        Task cannot proceed due to dependencies or issues
    """

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


class RiskLevel(Enum):
    """
    Enumeration of risk severity levels.

    Attributes
    ----------
    LOW : str
        Minimal impact on project timeline or quality
    MEDIUM : str
        Moderate impact requiring attention
    HIGH : str
        Significant impact requiring immediate action
    CRITICAL : str
        Severe impact threatening project success
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Priority(Enum):
    """
    Enumeration of task priority levels.

    Attributes
    ----------
    LOW : str
        Can be deferred without impact
    MEDIUM : str
        Should be completed in normal course
    HIGH : str
        Should be prioritized over medium/low tasks
    URGENT : str
        Requires immediate attention
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


@dataclass
class RecoveryInfo:
    """
    Information about task recovery from a previous agent.

    This is operational data that the next agent needs to avoid
    duplicating work or taking conflicting approaches.

    Parameters
    ----------
    recovered_at : datetime
        When the recovery occurred
    recovered_from_agent : str
        Agent ID that previously worked on this task
    previous_progress : int
        Progress percentage (0-100) at time of recovery
    time_spent_minutes : float
        Total time spent by previous agent in minutes
    recovery_reason : str
        Why recovery happened (e.g., "lease_expired", "agent_crashed")
    instructions : str
        Multi-line guidance for next agent about what to check
    previous_agent_branch : Optional[str]
        Git branch the previous agent was working on (e.g., "marcus/agent-3").
        In worktree mode, each agent works on an isolated branch. The new
        agent should merge this branch to pick up committed work.
    recovery_expires_at : Optional[datetime]
        When this recovery info becomes stale (default 24 hours)

    Notes
    -----
    Recovery info expires after 24 hours by default. After expiration,
    the info is considered stale and should be moved to audit trail only.
    """

    recovered_at: datetime
    recovered_from_agent: str
    previous_progress: int
    time_spent_minutes: float
    recovery_reason: str
    instructions: str
    previous_agent_branch: Optional[str] = None
    recovery_expires_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary for JSON serialization.

        Returns
        -------
        Dict[str, Any]
            Dictionary representation with ISO format timestamps
        """
        return {
            "recovered_at": self.recovered_at.isoformat(),
            "recovered_from_agent": self.recovered_from_agent,
            "previous_progress": self.previous_progress,
            "time_spent_minutes": round(self.time_spent_minutes, 1),
            "recovery_reason": self.recovery_reason,
            "instructions": self.instructions,
            "previous_agent_branch": self.previous_agent_branch,
            "recovery_expires_at": (
                self.recovery_expires_at.isoformat()
                if self.recovery_expires_at
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RecoveryInfo":
        """
        Create from dictionary (for deserialization).

        Parameters
        ----------
        data : Dict[str, Any]
            Dictionary with recovery info fields

        Returns
        -------
        RecoveryInfo
            Reconstructed RecoveryInfo instance
        """
        from dateutil.parser import parse

        return cls(
            recovered_at=parse(data["recovered_at"]),
            recovered_from_agent=data["recovered_from_agent"],
            previous_progress=data["previous_progress"],
            time_spent_minutes=data["time_spent_minutes"],
            recovery_reason=data["recovery_reason"],
            instructions=data["instructions"],
            previous_agent_branch=data.get("previous_agent_branch"),
            recovery_expires_at=(
                parse(data["recovery_expires_at"])
                if data.get("recovery_expires_at")
                else None
            ),
        )


@dataclass
class Task:
    """
    Represents a work item in the project management system.

    Parameters
    ----------
    id : str
        Unique identifier for the task
    name : str
        Short descriptive name of the task
    description : str
        Detailed description of what needs to be done
    status : TaskStatus
        Current state of the task
    priority : Priority
        Urgency level of the task
    assigned_to : Optional[str]
        ID of the worker assigned to this task
    created_at : datetime
        Timestamp when task was created
    updated_at : datetime
        Timestamp of last modification
    due_date : Optional[datetime]
        Target completion date
    estimated_hours : float
        Estimated time to complete in hours
    actual_hours : float, default=0.0
        Actual time spent on task
    dependencies : List[str], optional
        List of task IDs that must be completed first
    labels : List[str], optional
        Tags for categorizing the task
    project_id : Optional[str]
        ID of the project this task belongs to
    project_name : Optional[str]
        Name of the project this task belongs to
    is_subtask : bool, default=False
        Whether this task is a subtask of a parent task
    parent_task_id : Optional[str]
        ID of the parent task if this is a subtask
    subtask_index : Optional[int]
        Position within parent task's subtasks (for ordering)
    provides : Optional[str]
        What interface/functionality this task provides for dependent tasks
    requires : Optional[str]
        What this task needs from its dependencies
    responsibility : Optional[str]
        Contract interface or module this task is responsible for implementing
        when the project is decomposed by contract-first decomposition
        (GH-320 PR 2). Names a specific interface from the shared contract
        artifact — e.g. ``"implements GameEngine interface from src/types.ts"``.
        When set, the agent prompt frames the task as ownership of this
        contract interface rather than a generic implementation task, and
        the agent is directed to read the contract artifact before writing
        code. None for tasks produced by the legacy feature-based decomposer.

    Notes
    -----
    Dependencies and labels are initialized as empty lists if not provided.
    Project context fields are optional for backwards compatibility.
    The unified dependency graph fields (is_subtask, parent_task_id, subtask_index)
    enable subtasks to be first-class citizens in the dependency graph while
    preserving organizational hierarchy.
    The provides/requires fields enable automatic cross-parent dependency wiring
    by matching semantic contracts between subtasks.
    The responsibility field is orthogonal to provides/requires: provides/requires
    describe the contract this task SATISFIES at the dependency-wiring layer;
    responsibility names the contract interface this task OWNS from an
    authoritative shared contract file the agent must read.
    """

    id: str
    name: str
    description: str
    status: TaskStatus
    priority: Priority
    assigned_to: Optional[str]
    created_at: datetime
    updated_at: datetime
    due_date: Optional[datetime]
    estimated_hours: float
    actual_hours: float = 0.0
    dependencies: List[str] = field(default_factory=list)
    dependency_types: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    project_id: Optional[str] = None
    project_name: Optional[str] = None

    # Fields for generalization support
    source_type: Optional[str] = None  # "nlp_project", "predefined", "github_issue"
    source_context: Optional[Dict[str, Any]] = None  # Original context data
    completion_criteria: Optional[List[str]] = (
        None  # Success conditions (#607 step 3+4: flat behavior strings)
    )
    acceptance_criteria: List[str] = field(default_factory=list)  # Checklist items
    validation_spec: Optional[str] = None  # How to validate completion

    # Fields for unified dependency graph
    is_subtask: bool = False  # True if this is a subtask of a parent task
    parent_task_id: Optional[str] = None  # ID of parent task if is_subtask=True
    subtask_index: Optional[int] = None  # Position within parent (for ordering)

    # Fields for cross-parent dependency wiring
    provides: Optional[str] = None  # What interface/functionality this task provides
    requires: Optional[str] = None  # What this task needs from dependencies

    # Field for contract-first decomposition (GH-320 PR 2)
    # Names the contract interface this task owns from a shared contract
    # artifact. Set by decompose_by_contract, surfaced in agent prompts.
    responsibility: Optional[str] = None

    # Exact file paths this task is expected to create or modify.
    # Set by the decomposer from the LLM response; used by agents and
    # completion checks to verify deliverables exist.
    output_paths: List[str] = field(default_factory=list)

    # Recovery information (if task was recovered from another agent)
    recovery_info: Optional["RecoveryInfo"] = None


@dataclass
class ProjectState:
    """
    Represents the current state of the entire project.

    Parameters
    ----------
    board_id : str
        Unique identifier of the project board
    project_name : str
        Name of the project
    total_tasks : int
        Total number of tasks in the project
    completed_tasks : int
        Number of tasks marked as done
    in_progress_tasks : int
        Number of tasks currently being worked on
    blocked_tasks : int
        Number of tasks that are blocked
    progress_percent : float
        Overall project completion percentage (0-100)
    overdue_tasks : List[Task]
        Tasks that have passed their due date
    team_velocity : float
        Average tasks completed per time period
    risk_level : RiskLevel
        Overall project risk assessment
    last_updated : datetime
        Timestamp of last state update
    """

    board_id: str
    project_name: str
    total_tasks: int
    completed_tasks: int
    in_progress_tasks: int
    blocked_tasks: int
    progress_percent: float
    overdue_tasks: List[Task]
    team_velocity: float
    risk_level: RiskLevel
    last_updated: datetime


@dataclass
class WorkerStatus:
    """
    Represents the current state and capabilities of a worker agent.

    Parameters
    ----------
    worker_id : str
        Unique identifier for the worker
    name : str
        Display name of the worker
    role : str
        Job role or specialization (e.g., "Frontend Developer")
    email : Optional[str]
        Contact email for the worker
    current_tasks : List[Task]
        Tasks currently assigned to this worker
    completed_tasks_count : int
        Total number of tasks completed by this worker
    capacity : int
        Maximum hours per week the worker can work
    skills : List[str]
        Technical skills and competencies
    availability : Dict[str, bool]
        Days of week when worker is available
    performance_score : float, default=1.0
        Performance rating (0.0-2.0, where 1.0 is baseline)

    Examples
    --------
    >>> worker = WorkerStatus(
    ...     worker_id="dev-001",
    ...     name="Alice Smith",
    ...     role="Backend Developer",
    ...     email="alice@example.com",
    ...     current_tasks=[],
    ...     completed_tasks_count=15,
    ...     capacity=40,
    ...     skills=["Python", "Django", "PostgreSQL"],
    ...     availability={"monday": True, "tuesday": True, ...}
    ... )
    """

    worker_id: str
    name: str
    role: str
    email: Optional[str]
    current_tasks: List[Task]
    completed_tasks_count: int
    capacity: int
    skills: List[str]
    availability: Dict[str, bool]
    performance_score: float = 1.0


@dataclass
class TaskAssignment:
    """
    Represents a task assignment to a specific worker.

    Parameters
    ----------
    task_id : str
        Unique identifier of the task
    task_name : str
        Name of the task being assigned
    description : str
        What needs to be done
    instructions : str
        Detailed instructions for completing the task
    estimated_hours : float
        Expected time to complete
    priority : Priority
        Task urgency level
    dependencies : List[str]
        Other tasks that must be completed first
    assigned_to : str
        Worker ID receiving the assignment
    assigned_at : datetime
        Timestamp of assignment
    due_date : Optional[datetime]
        Target completion date
    workspace_path : Optional[str], default=None
        Path to the workspace directory for this task
    forbidden_paths : List[str], optional
        Paths the worker should not access

    Notes
    -----
    The workspace_path and forbidden_paths are used for security isolation
    to prevent workers from accessing unauthorized directories.
    """

    task_id: str
    task_name: str
    description: str
    instructions: str
    estimated_hours: float
    priority: Priority
    dependencies: List[str]
    assigned_to: str
    assigned_at: datetime
    due_date: Optional[datetime]
    workspace_path: Optional[str] = None
    forbidden_paths: List[str] = field(default_factory=list)
    baseline_commit: Optional[str] = None


@dataclass
class BlockerReport:
    """
    Represents a blocker or impediment reported by a worker.

    Parameters
    ----------
    task_id : str
        ID of the blocked task
    reporter_id : str
        ID of the worker reporting the blocker
    description : str
        Detailed description of the blocker
    severity : RiskLevel
        How severely this impacts the task
    reported_at : datetime
        When the blocker was reported
    resolved : bool, default=False
        Whether the blocker has been resolved
    resolution : Optional[str], default=None
        Description of how the blocker was resolved
    resolved_at : Optional[datetime], default=None
        When the blocker was resolved

    Examples
    --------
    >>> blocker = BlockerReport(
    ...     task_id="TASK-123",
    ...     reporter_id="dev-001",
    ...     description="Cannot access production database",
    ...     severity=RiskLevel.HIGH,
    ...     reported_at=datetime.now(timezone.utc)
    ... )
    """

    task_id: str
    reporter_id: str
    description: str
    severity: RiskLevel
    reported_at: datetime
    resolved: bool = False
    resolution: Optional[str] = None
    resolved_at: Optional[datetime] = None


@dataclass
class ProjectRisk:
    """
    Represents an identified risk to the project.

    Parameters
    ----------
    risk_type : str
        Category of risk (e.g., "technical", "resource", "timeline")
    description : str
        Detailed description of the risk
    severity : RiskLevel
        Potential impact severity
    probability : float
        Likelihood of occurrence (0.0-1.0)
    impact : str
        Description of potential impact if risk materializes
    mitigation_strategy : str
        Plan to reduce or eliminate the risk
    identified_at : datetime
        When the risk was identified

    Examples
    --------
    >>> risk = ProjectRisk(
    ...     risk_type="technical",
    ...     description="Legacy API may be deprecated",
    ...     severity=RiskLevel.MEDIUM,
    ...     probability=0.3,
    ...     impact="Would require rewriting integration layer",
    ...     mitigation_strategy="Begin migration to new API",
    ...     identified_at=datetime.now(timezone.utc)
    ... )
    """

    risk_type: str
    description: str
    severity: RiskLevel
    probability: float
    impact: str
    mitigation_strategy: str
    identified_at: datetime
